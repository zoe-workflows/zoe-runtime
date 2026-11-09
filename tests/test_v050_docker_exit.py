"""v0.5.0 Docker exit, stage one (§18).

Stage one is not "delete the container code". It is:

* old configurations are refused **explicitly** and never rewritten into
  ``local-process`` (no silent change of isolation boundary);
* ``doctor`` no longer treats docker as a prerequisite;
* historical container runs stay readable, but unobservable execution is
  reported as unknown, and a frozen docker task cannot be driven further.

The container backend class itself is only removed in a later batch (M6), so
these tests target the refusal surface, not the absence of the class.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from test_runtime import RuntimeFixture

from src import _pipeline
from src._config import doctor, validate_config
from src.core import Error, start

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*argv, home: Path):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "ZOE_HOME": str(home)}
    return subprocess.run(
        [sys.executable, "-m", "src", *argv], capture_output=True, text=True, env=env
    )


class DockerConfigRefusalTest(RuntimeFixture):
    def legacy_config(self) -> dict:
        return {
            "schema_version": 1,
            "project_id": "legacy",
            "repo": str(self.repo),
            "backend": "docker",
            "image": "legacy@sha256:" + "c" * 64,
            "network": "none",
            "agent": "codebuddy",
        }

    def test_docker_config_is_refused_with_a_migration_hint(self):
        with self.assertRaises(Error) as ctx:
            validate_config(dict(self.legacy_config()))
        error = ctx.exception
        self.assertEqual(error.code, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(error.details["configured_backend"], "docker")
        self.assertEqual(error.details["supported_backends"], ["local-process"])
        self.assertTrue(error.details["migration"])
        self.assertEqual(error.to_dict()["details"]["scope"], "project")

    def test_the_refusal_does_not_rewrite_the_configuration(self):
        config = self.legacy_config()
        with self.assertRaises(Error):
            validate_config(config)
        self.assertEqual(config["backend"], "docker", "no implicit switch to local-process")
        self.assertIn("image", config, "old fields stay parseable")

    def test_doctor_refuses_instead_of_reporting_a_false_prerequisite(self):
        with self.assertRaises(Error) as ctx:
            doctor(self.legacy_config())
        self.assertEqual(ctx.exception.code, "CAPABILITY_UNSUPPORTED")

    def test_doctor_is_ready_without_a_docker_binary(self):
        real_which = __import__("shutil").which

        def no_docker(name):
            return None if name == "docker" else real_which(name)

        with patch("src._config.shutil.which", side_effect=no_docker):
            report = doctor(self.s.config("demo"))
        self.assertTrue(report["ready"])
        self.assertNotIn("docker", report["checks"], "docker is no longer a readiness input")
        self.assertNotIn("docker_ready", report["checks"])
        self.assertTrue(report["runtime_ready"])

    def test_cli_reports_the_refusal_as_json_with_details(self):
        atomic = __import__("src._util", fromlist=["atomic"]).atomic
        atomic(self.s.home / "projects/legacy.json", self.legacy_config())
        path = self.s.home / "projects/legacy.json"
        before = path.read_text()

        result = run_cli("doctor", "--project", "legacy", "--json", home=self.s.home)

        self.assertEqual(result.returncode, 4, result.stderr)
        error = json.loads(result.stdout)["error"]
        self.assertEqual(error["code"], "CAPABILITY_UNSUPPORTED")
        self.assertTrue(error["details"]["migration"])
        self.assertEqual(path.read_text(), before, "the on-disk configuration is untouched")


class LegacyContainerRunTest(RuntimeFixture):
    def legacy_task(self, container="zoe-run-legacy-agent"):
        t = self.new("local_artifact")
        rid = "run-" + "0" * 16
        self.s.run(
            {
                "run_id": rid,
                "task_id": t["task_id"],
                "status": "running",
                "container": container,
                "launch_stage": "started",
                "supervisor_pid": 999999,
            }
        )
        t.update(current_run_id=rid, state="RUNNING", phase="implement")
        self.s.save(t)
        return self.s.task(t["task_id"]), rid

    def test_a_historical_container_run_stays_readable(self):
        t, rid = self.legacy_task()
        run = self.s.get_run(rid)
        self.assertEqual(run["container"], "zoe-run-legacy-agent")
        self.assertIsNotNone(self.s.task(t["task_id"]))
        rows, _, _ = self.s.events(t["task_id"])
        self.assertTrue(rows)

    def test_unobservable_execution_is_reported_as_unknown_and_not_closed(self):
        t, rid = self.legacy_task()
        reconciled = _pipeline.reconcile(self.s, t)
        self.assertEqual(reconciled["state"], "BLOCKED")
        self.assertEqual(reconciled["block_code"], "PROCESS_UNKNOWN")
        self.assertEqual(self.s.get_run(rid)["status"], "running", "never auto-closed")
        self.assertTrue(_pipeline._run_uncertain(self.s.get_run(rid), None))

    def test_logs_and_report_stay_readable_for_a_container_run(self):
        """B2: reading a historical run must not need the removed backend."""
        import contextlib
        import io
        import json as jsonlib
        from types import SimpleNamespace

        from src.cli import _status_or_report, _task_logs

        t, rid = self.legacy_task()
        run_dir = self.s.path(t) / "runs" / rid
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "agent.log").write_text("legacy container output\n")

        logs = _task_logs(
            self.s, SimpleNamespace(id=t["task_id"], run=rid, tail_bytes=1024, log="agent")
        )
        self.assertIn("legacy container output", logs["text"])
        self.assertEqual(logs["run"]["status"], "running")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _status_or_report(
                self.s, SimpleNamespace(id=t["task_id"], refresh=False, format="json")
            )
        self.assertEqual(jsonlib.loads(buffer.getvalue())["current_run_id"], rid)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _status_or_report(
                self.s, SimpleNamespace(id=t["task_id"], refresh=False, format="markdown")
            )
        self.assertIn(t["task_id"], buffer.getvalue())

    def test_a_local_run_label_is_not_mistaken_for_a_container(self):
        self.assertFalse(_pipeline._is_container_run({"container": "local-run-1-agent"}))
        self.assertTrue(_pipeline._is_container_run({"container": "zoe-run-1-agent"}))

    def test_confirm_stop_is_the_only_exit_for_an_unobservable_run(self):
        """The documented escape hatch must actually work (§18.4)."""
        from src.cli import _task_recover

        t, rid = self.legacy_task()
        result = _task_recover(self.s, t["task_id"], rid, True)
        self.assertEqual(result["final_state"], "RUN_INTERRUPTED")
        self.assertEqual(self.s.get_run(rid)["status"], "exited")
        self.assertEqual(self.s.task(t["task_id"])["block_code"], "RUN_INTERRUPTED")
        self.assertNotIn("process_tree", [step["step"] for step in result["steps"]])

    def test_a_frozen_docker_task_cannot_be_resumed(self):
        t = self.new("local_artifact")
        t["config"] = {**t["config"], "backend": "docker"}
        self.s.save(t)
        with self.assertRaises(Error) as ctx:
            start(
                self.s,
                t["task_id"],
                self.req(expected_version=t["row_version"], actor="human"),
            )
        self.assertEqual(ctx.exception.code, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(ctx.exception.details["scope"], "task")

    def test_the_pipeline_also_refuses_a_frozen_docker_task(self):
        t = self.new("local_artifact")
        t["config"] = {**t["config"], "backend": "docker"}
        self.s.save(t)
        rid = "run-" + "1" * 16
        self.s.run({"run_id": rid, "task_id": t["task_id"], "status": "running", "container": None})
        t.update(current_run_id=rid, state="RUNNING")
        self.s.save(t)
        with self.assertRaises(Error) as ctx:
            _pipeline.run_pipeline(self.s, t["task_id"], _pipeline.create_backend_for(t["config"]))
        self.assertEqual(ctx.exception.code, "CAPABILITY_UNSUPPORTED")


class SchemaTest(unittest.TestCase):
    def test_schema_no_longer_offers_the_docker_backend(self):
        schema = json.loads((ROOT / "schemas" / "project-profile.v2.json").read_text())
        self.assertNotIn("docker", schema["properties"]["backend"]["enum"])
        self.assertEqual(schema["properties"]["backend"]["enum"], ["local-process", "local"])


if __name__ == "__main__":
    unittest.main()
