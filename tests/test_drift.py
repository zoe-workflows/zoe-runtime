"""Pre-publication drift classification (§7), driven by real local repositories.

Every case is built from a bare "server" repository and a clone, so the three
drift kinds are produced by actually moving, rewriting or diverging refs — not by
stubbing git output. No network and no GitHub are involved.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from src import _drift
from src._util import Error, git


class FakeStore:
    """Minimal store: the probe only ever asks where a task's evidence lives."""

    def __init__(self, root: Path):
        self.root = root

    def path(self, t):
        return self.root / t["task_id"]


class FailingStore:
    """Raises something the probe's per-step error handling does not expect."""

    def path(self, t):
        raise RuntimeError("state directory is unreadable")


class DriftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server = self.root / "server.git"
        git(self.root, "init", "--bare", "-b", "main", str(self.server))
        self.work = self.root / "work"
        self.work.mkdir()
        git(self.work, "init", "-b", "main")
        git(self.work, "config", "user.name", "test")
        git(self.work, "config", "user.email", "test@localhost")
        (self.work / "src.txt").write_text("initial\n")
        git(self.work, "add", ".")
        git(self.work, "commit", "-m", "initial")
        git(self.work, "remote", "add", "origin", str(self.server))
        git(self.work, "push", "origin", "main")
        self.base_sha = self.head(self.work)
        self.store = FakeStore(self.root / "state")
        self.t = {
            "task_id": "task-drift",
            "workspace": str(self.work),
            "branch": "zoe/task-drift",
            "base_sha": self.base_sha,
            "candidate_sha": self.candidate(),
            "published_sha": None,
        }
        self.c = {"repo": str(self.work), "remote": "origin", "target_branch": "main"}

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def head(repo) -> str:
        import subprocess

        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()

    def candidate(self) -> str:
        git(self.work, "checkout", "-q", "-B", "zoe/task-drift", "main")
        (self.work / "src.txt").write_text("implemented\n")
        git(self.work, "commit", "-qam", "implement")
        sha = self.head(self.work)
        git(self.work, "checkout", "-q", "main")
        return sha

    def advance_main(self):
        (self.work / "src.txt").write_text("upstream moved\n")
        git(self.work, "commit", "-qam", "upstream")
        git(self.work, "push", "origin", "main")

    def force_push_main(self):
        """A genuinely different history: the frozen base is no longer an ancestor."""
        git(self.work, "checkout", "-q", "--orphan", "rewritten")
        (self.work / "src.txt").write_text("rewritten root\n")
        git(self.work, "add", ".")
        git(self.work, "commit", "-qm", "rewritten root")
        git(self.work, "push", "--force", "origin", "HEAD:refs/heads/main")
        git(self.work, "checkout", "-q", "main")

    def diverge_task_branch(self):
        """Someone else moved the task branch; the base is untouched."""
        git(self.work, "checkout", "-q", "-B", "zoe/task-drift", "main")
        (self.work / "src.txt").write_text("someone else\n")
        git(self.work, "commit", "-qam", "foreign")
        git(self.work, "push", "origin", "zoe/task-drift")
        git(self.work, "checkout", "-q", "main")

    # -------------------------------------------------------------- no drift

    def test_first_publish_without_a_task_branch_has_no_drift(self):
        result = _drift.probe(self.store, self.t, self.c)
        self.assertEqual(result["kind"], _drift.DRIFT_NONE)
        self.assertIsNone(result["observed_task_sha"])
        self.assertIsNone(result["reason"])

    def test_published_task_sha_is_not_drift(self):
        git(self.work, "push", "origin", f"{self.t['candidate_sha']}:refs/heads/zoe/task-drift")
        published = {**self.t, "published_sha": self.t["candidate_sha"]}
        result = _drift.probe(self.store, published, self.c)
        self.assertEqual(result["kind"], _drift.DRIFT_NONE)
        self.assertEqual(result["observed_task_sha"], self.t["candidate_sha"])

    # ------------------------------------------------------------ base drift

    def test_advanced_base_is_distinguishable_from_a_rewrite(self):
        self.advance_main()
        result = _drift.probe(self.store, self.t, self.c)
        self.assertEqual(result["kind"], _drift.DRIFT_BASE_ADVANCED)
        self.assertEqual(result["base_sha"], self.base_sha)
        self.assertNotEqual(result["observed_base_sha"], self.base_sha)
        self.assertEqual(_drift.error_code(result), "REMOTE_DRIFT")

    def test_force_pushed_base_is_a_rewrite(self):
        # A genuinely different history: the frozen base is no longer an
        # ancestor of the target. `reset --hard` + commit would still be
        # "advanced", which is a different verdict.
        self.force_push_main()
        result = _drift.probe(self.store, self.t, self.c)
        self.assertEqual(result["kind"], _drift.DRIFT_BASE_REWRITTEN)

    def test_unresolvable_base_is_unknown_not_a_rewrite(self):
        """A git failure must never be reported as "history was rewritten"."""
        self.advance_main()
        result = _drift.probe(self.store, {**self.t, "base_sha": "f" * 40}, self.c)
        self.assertEqual(result["kind"], _drift.DRIFT_REMOTE_UNKNOWN)
        self.assertEqual(result["reason"], "git_error")

    # ------------------------------------------------------- candidate drift

    def test_remote_task_branch_at_another_commit_is_a_divergence(self):
        self.diverge_task_branch()
        result = _drift.probe(self.store, self.t, self.c)
        self.assertEqual(result["kind"], _drift.DRIFT_CANDIDATE_DIVERGED)
        self.assertIsNotNone(result["observed_task_sha"])

    def test_deleted_task_ref_after_publication_is_a_divergence(self):
        """Silently recreating a branch deleted by someone else would be a repair."""
        evidence = self.store.path(self.t)
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "publication.json").write_text('{"status": "confirmed"}')
        result = _drift.probe(self.store, self.t, self.c)
        self.assertEqual(result["kind"], _drift.DRIFT_CANDIDATE_DIVERGED)
        self.assertEqual(result["reason"], _drift.REASON_TASK_REF_MISSING)

    # ------------------------------------------------------ unobservable cases

    def test_unreachable_remote_is_unknown_not_clean(self):
        result = _drift.probe(self.store, self.t, {"repo": str(self.work), "remote": "nope"})
        self.assertEqual(result["kind"], _drift.DRIFT_REMOTE_UNKNOWN)
        self.assertEqual(result["reason"], "fetch_failed")
        self.assertEqual(_drift.error_code(result), "REMOTE_UNKNOWN")

    def test_a_hanging_remote_is_bounded_not_fatal(self):
        """A hung fetch must end as `remote_unknown`, within the timeout (§7.8)."""
        import os
        from unittest.mock import patch

        stub_dir = self.root / "hang-bin"
        stub_dir.mkdir()
        stub = stub_dir / "git"
        stub.write_text("#!/bin/sh\nexec /bin/sleep 30\n")
        stub.chmod(0o755)
        started = time.monotonic()
        with (
            patch.dict(os.environ, {"PATH": str(stub_dir)}),
            patch.object(_drift, "FETCH_TIMEOUT_SECONDS", 0.3),
        ):
            result = _drift.probe(self.store, self.t, self.c)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10.0)
        self.assertEqual(result["kind"], _drift.DRIFT_REMOTE_UNKNOWN)
        self.assertEqual(result["reason"], "fetch_failed")

    def test_option_like_remote_is_refused_before_git_runs(self):
        result = _drift.probe(self.store, self.t, {**self.c, "remote": "--upload-pack=evil"})
        self.assertEqual(result["kind"], _drift.DRIFT_REMOTE_UNKNOWN)
        self.assertEqual(result["reason"], "git_error")

    def test_probe_never_raises(self):
        result = _drift.probe(FailingStore(), self.t, self.c)
        self.assertEqual(result["kind"], _drift.DRIFT_REMOTE_UNKNOWN)
        self.assertEqual(result["reason"], "git_error")

    def test_probe_never_raises_on_a_broken_task_record(self):
        for broken in ({}, {"task_id": "x"}, {"task_id": "x", "branch": "b"}):
            with self.subTest(task=broken):
                self.assertEqual(
                    _drift.probe(self.store, broken, self.c)["kind"], _drift.DRIFT_REMOTE_UNKNOWN
                )

    # ----------------------------------------------------------- error detail

    def test_error_details_carry_the_kind_for_hosts_that_only_read_json(self):
        self.advance_main()
        result = _drift.probe(self.store, self.t, self.c)
        error = _drift.error(result, self.t)
        self.assertIsInstance(error, Error)
        self.assertEqual(error.code, "REMOTE_DRIFT")
        self.assertEqual(error.details["drift_kind"], _drift.DRIFT_BASE_ADVANCED)
        self.assertEqual(error.details["task_id"], self.t["task_id"])
        self.assertEqual(error.details["evidence_ref"], "publication.json")
        self.assertEqual(error.to_dict()["details"]["drift_kind"], "base_advanced")

    def test_a_rewrite_reaches_hosts_with_the_same_code_and_its_own_kind(self):
        self.force_push_main()
        error = _drift.error(_drift.probe(self.store, self.t, self.c), self.t)
        self.assertEqual(error.code, "REMOTE_DRIFT")
        self.assertEqual(error.to_dict()["details"]["drift_kind"], "base_rewritten")

    def test_a_divergence_reaches_hosts_with_the_same_code_and_its_own_kind(self):
        self.diverge_task_branch()
        error = _drift.error(_drift.probe(self.store, self.t, self.c), self.t)
        self.assertEqual(error.code, "REMOTE_DRIFT")
        self.assertEqual(error.to_dict()["details"]["drift_kind"], "candidate_diverged")

    def test_messages_are_distinct_per_kind(self):
        messages = {_drift.message({"kind": kind}) for kind in _drift.DRIFT_KINDS}
        messages.add(_drift.message({"kind": _drift.DRIFT_REMOTE_UNKNOWN}))
        self.assertEqual(len(messages), 4)


if __name__ == "__main__":
    unittest.main()
