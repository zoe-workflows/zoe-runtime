"""v0.5.0 pipeline integration: pinned launcher, drift gate, branch hardening.

These tests drive the real ``run_pipeline`` with in-process backend doubles (as
the rest of the suite does): they exercise control flow and evidence, never a
real agent or a remote host.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from test_runtime import FakeDocker, FakePublisher, RuntimeFixture

from src import _discovery, _pipeline
from src.core import Error, atomic, create, doctor, git


def make_cli(directory: Path, name: str = "codebuddy", version: str = "1.0.0") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f'#!/bin/sh\necho "{version}"\n')
    path.chmod(0o755)
    return path


class RecordingBackend(FakeDocker):
    """Records the argv the pipeline actually asked to launch."""

    def __init__(self):
        super().__init__()
        self.argv = []

    def execute(self, store, t, r, argv, log_name, worker=False, prompt=None):
        self.argv.append(list(argv))
        return super().execute(store, t, r, argv, log_name, worker, prompt)


class PipelineTest(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.stub_dir = self.root / "cli"
        self.stub = make_cli(self.stub_dir)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.stub_dir) + os.pathsep + self._old_path
        _discovery.clear_cache()

    def tearDown(self):
        _discovery.clear_cache()
        os.environ["PATH"] = self._old_path
        super().tearDown()

    def publication(self, t):
        path = self.s.path(t) / "publication.json"
        self.assertTrue(path.is_file(), "publication evidence must exist")
        return json.loads(path.read_text())

    # ------------------------------------------------------------- launcher pin

    def test_the_run_launches_the_path_doctor_reports(self):
        backend = RecordingBackend()
        t = self.run_task(self.new("local_artifact"), b=backend)
        self.assertEqual(t["state"], "REVIEW_READY")
        run = self.s.get_run(t["current_run_id"])
        reported = doctor(self.s.config("demo"))["resolution"]

        self.assertEqual(reported["path"], str(self.stub))
        self.assertEqual(run["resolved_path"], str(self.stub))
        self.assertEqual(run["resolution_source"], "process-path")
        self.assertEqual(run["launch"]["argv"][0], str(self.stub))
        # Only the agent launch is pinned; check units keep their own argv.
        self.assertGreaterEqual(len(backend.argv), 2)
        self.assertEqual(backend.argv[0][0], str(self.stub), "the agent uses the pinned path")

    def test_probe_and_run_agree_on_the_binary_including_the_legacy_alias(self):
        """§14: `resolve_cli`, `probe_plan` and the launched argv are one path."""
        from src._profiles import probe_profile

        # `local` is the legacy alias of `local-process`; comparing raw strings
        # would skip pinning entirely for it.
        self.c["backend"] = "local"
        atomic(self.s.home / "projects/demo.json", self.c)
        _discovery.clear_cache()

        c = self.s.config("demo")
        probe = probe_profile(c, c.get("default_agent_profile"))
        t = self.run_task(self.new("local_artifact"), b=RecordingBackend())
        run = self.s.get_run(t["current_run_id"])

        self.assertEqual(probe["command"][0], str(self.stub))
        self.assertEqual(run["launch"]["argv"][0], str(self.stub))
        self.assertEqual(run["resolved_path"], str(self.stub))
        self.assertEqual(run["resolution_source"], "process-path")
        # The probe measured this binary, so the run record inherits that fact
        # instead of leaving a second, unprobed version claim.
        self.assertEqual(run["cli_version"], "1.0.0")

    def test_a_different_binary_between_adjacent_runs_is_recorded(self):
        # A failing check keeps the task in `implement`, so the next run really
        # launches the agent again — that is the "adjacent runs" case.
        first = self.run_task(self.new("local_artifact"), b=FakeDocker(fail=True))
        self.assertEqual(first["state"], "BLOCKED")

        other_dir = self.root / "cli2"
        other = make_cli(other_dir, version="9.9.9")
        os.environ["PATH"] = str(other_dir) + os.pathsep + self._old_path
        _discovery.clear_cache()

        self.run_task(self.s.task(first["task_id"]), b=RecordingBackend())
        rows, _, _ = self.s.events(first["task_id"])
        events = [e for e in rows if e["type"] == "environment_drift"]
        self.assertEqual(len(events), 1, "one adjacent-run change, one event")
        self.assertEqual(events[0]["previous_path"], str(self.stub))
        self.assertEqual(events[0]["resolved_path"], str(other))

    # ------------------------------------------------------------- drift gate

    def test_local_artifact_never_touches_the_remote(self):
        t = self.run_task(self.new("local_artifact"), p=FakePublisher())
        self.assertEqual(t["state"], "REVIEW_READY")
        self.assertFalse((self.s.path(t) / "publication.json").exists())
        events, _, _ = self.s.events(t["task_id"])
        self.assertFalse([e for e in events if e["type"] == "remote_drift"])

    def test_pull_request_without_a_remote_blocks_as_unknown(self):
        t = self.run_task(self.new("pull_request"), p=FakePublisher(fail=True))
        self.assertEqual(t["state"], "BLOCKED")
        self.assertEqual(t["block_code"], "REMOTE_UNKNOWN")
        self.assertEqual(t["phase"], "publish")
        evidence = self.publication(t)
        self.assertEqual(evidence["status"], "blocked_by_drift")
        self.assertEqual(evidence["drift"]["kind"], "remote_unknown")

    def test_advanced_base_blocks_with_remote_drift_evidence(self):
        self.add_remote()
        # Freeze the baseline first, then move the target: only then is the
        # verified evidence stale with respect to the integration state.
        task = self.new("pull_request")
        (self.repo / "src.txt").write_text("upstream moved\n")
        git(self.repo, "commit", "-qam", "upstream")
        git(self.repo, "push", "origin", "main")

        t = self.run_task(task, p=FakePublisher())
        self.assertEqual(t["state"], "BLOCKED")
        self.assertEqual(t["block_code"], "REMOTE_DRIFT")
        evidence = self.publication(t)
        self.assertEqual(evidence["status"], "blocked_by_drift")
        self.assertEqual(evidence["drift"]["kind"], "base_advanced")
        self.assertNotEqual(evidence["drift"]["observed_base_sha"], evidence["drift"]["base_sha"])

        rows, _, _ = self.s.events(t["task_id"])
        events = [e for e in rows if e["type"] == "remote_drift"]
        self.assertEqual(len(events), 1)
        # Event payloads are merged flat into the event record.
        self.assertEqual(events[0]["kind"], "base_advanced")

        # §7.4.2: the blocked task is never auto-cleared, and the only recovery
        # path is a new task frozen on the new baseline — with no inherited
        # acceptance verdict.
        self.assertEqual(self.s.task(t["task_id"])["block_code"], "REMOTE_DRIFT")
        fresh = self.new("pull_request")
        self.assertEqual(fresh["base_sha"], git(self.repo, "rev-parse", "main"))
        self.assertNotEqual(fresh["base_sha"], task["base_sha"])
        self.assertIsNone(fresh.get("acceptance"))

    def test_clean_drift_check_still_publishes_and_keeps_the_evidence(self):
        self.add_remote()
        t = self.run_task(self.new("pull_request"), p=FakePublisher())
        self.assertEqual(t["state"], "REVIEW_READY")
        self.assertEqual(t["phase"], "ci")
        # The publisher double writes no publication record of its own, but the
        # drift check that allowed the publish must still be on disk.
        evidence = self.publication(t)
        self.assertEqual(evidence["drift"]["kind"], "none")
        self.assertEqual(evidence["drift"]["observed_base_sha"], t["base_sha"])


class BranchValidationTest(RuntimeFixture):
    def test_invalid_branch_names_are_refused_by_git(self):
        for branch in ("zoe/task-..x", "zoe/a b", "zoe/HEAD~1", "-zoe/x", "zoe/x.lock"):
            with self.subTest(branch=branch):
                with self.assertRaises(Error) as ctx:
                    _pipeline.require_valid_branch(self.repo, branch)
                self.assertEqual(ctx.exception.code, "POLICY_DENIED")

    def test_generated_branches_pass(self):
        _pipeline.require_valid_branch(self.repo, "zoe/task-0123456789abcdef")

    def test_create_stops_when_the_branch_is_refused(self):
        """Defense in depth: `create` must not proceed on an invalid branch."""
        refused = Error("POLICY_DENIED", "Invalid task branch name")
        with patch.object(_pipeline, "require_valid_branch", side_effect=refused):
            with self.assertRaises(Error) as ctx:
                create(
                    self.s,
                    self.req(
                        project_id="demo",
                        title="x",
                        spec_file=str(self.spec),
                        delivery_mode="local_artifact",
                    ),
                )
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")
        self.assertEqual(self.s.tasks(), [], "no task may be persisted after a refusal")


if __name__ == "__main__":
    unittest.main()
