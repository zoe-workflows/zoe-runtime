"""Upgrade fixture: 0.1.0 state.db must remain readable and recoverable in 0.2.0.

0.1.0 wrote ``PRAGMA user_version=1`` with four tables (``tasks``/``runs``/
``operations``/``events``) and stored each task as a JSON document. 0.2.0 keeps
that schema, so compatibility is not proven by the version number alone: field
interpretation, phase handling and recovery decisions must still work.

The fixture below deliberately writes rows **without** going through 0.2.0's
``create()``, and pins the 0.1.0 field set literally. If 0.2.0 silently renames
or drops a field, these tests fail instead of the user's data failing.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src._store import Store
from src._pipeline import start
from src._util import Error, digest

# The exact field set produced by 0.1.0's create(). Pinned on purpose.
LEGACY_TASK_FIELDS = (
    "task_id",
    "project_id",
    "title",
    "row_version",
    "state",
    "phase",
    "spec_revision",
    "spec_hash",
    "config",
    "base_sha",
    "branch",
    "workspace",
    "delivery_mode",
    "created_at",
    "attempts",
    "current_run_id",
    "block_code",
    "candidate_sha",
    "validated_sha",
    "pr",
    "acceptance",
)


class FakeDocker:
    """Stand-in for the Docker backend during reconcile."""

    def __init__(self, state=None):
        self.state = state

    def inspect(self, name):
        return self.state

    def stop(self, name):
        return None


class UpgradeFixture(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.home = self.root / "state"
        self.store = Store(self.home)
        # validate_config() resolves the repo path, so the frozen task copy must
        # hold the resolved form too; otherwise the policy digest differs.
        self.config = {
            "schema_version": 1,
            "project_id": "legacy",
            "repo": str(self.repo.resolve()),
            "backend": "local-process",
            "network": "none",
            "compatibility_verified": True,
            "agent": "codebuddy",
            "checks": [{"id": "unit", "argv": ["true"]}],
            "allowed_paths": ["src.txt"],
            "authorization_ref": "legacy-scope",
            "authorized_actions": ["create", "execute", "publish", "accept"],
            "ci_required": True,
        }
        (self.home / "projects").mkdir(parents=True, exist_ok=True)
        (self.home / "projects" / "legacy.json").write_text(json.dumps(self.config))

    def tearDown(self):
        self.store.db.close()
        self._td.cleanup()

    def legacy_task(self, task_id, state, phase, **overrides):
        """Build a task document exactly as 0.1.0 would have written it."""
        workspace = self.home / "tasks" / task_id / "worktree"
        workspace.mkdir(parents=True, exist_ok=True)
        spec = "# legacy spec\nAC-001 legacy acceptance\n"
        (self.home / "tasks" / task_id).mkdir(parents=True, exist_ok=True)
        (self.home / "tasks" / task_id / "spec-1.json").write_text(json.dumps({"text": spec}))
        task = {
            "task_id": task_id,
            "project_id": "legacy",
            "title": "legacy " + task_id,
            "row_version": 1,
            "state": state,
            "phase": phase,
            "spec_revision": 1,
            "spec_hash": digest(spec.encode()),
            "config": dict(self.config),
            "base_sha": "a" * 40,
            "branch": "zoe/" + task_id,
            "workspace": str(workspace),
            "delivery_mode": "local_artifact",
            "created_at": 1700000000.0,
            "attempts": 0,
            "current_run_id": None,
            "block_code": None,
            "candidate_sha": None,
            "validated_sha": None,
            "pr": None,
            "acceptance": None,
        }
        task.update(overrides)
        return task

    def write(self, task, runs=(), version=1):
        """Insert raw rows, bypassing 0.2.0 code paths."""
        self.store.db.execute(
            "INSERT INTO tasks VALUES(?,?,?)",
            (task["task_id"], version, json.dumps(task)),
        )
        for run in runs:
            self.store.db.execute(
                "INSERT INTO runs VALUES(?,?,?)",
                (run["run_id"], run["task_id"], json.dumps(run)),
            )

    def run_doc(
        self,
        task_id,
        run_id,
        status,
        pid=None,
        identity=None,
        container=None,
        launch_stage="started",
    ):
        return {
            "run_id": run_id,
            "task_id": task_id,
            "status": status,
            "pid": pid,
            "identity": identity,
            "container": container,
            "started_at": 1700000001.0,
            "launch_stage": launch_stage,
        }


class TestLegacyFieldsPreserved(UpgradeFixture):
    def test_all_legacy_fields_survive_read(self):
        """0.2.0 must not drop or rename a 0.1.0 field."""
        task = self.legacy_task("task-legacy00", "READY", "prepare")
        self.write(task)

        loaded = self.store.task("task-legacy00")

        for field in LEGACY_TASK_FIELDS:
            self.assertIn(field, loaded, "0.1.0 field lost during upgrade: " + field)
        self.assertEqual(loaded["row_version"], 1)
        self.assertEqual(loaded["spec_revision"], 1)


class TestLegacyReady(UpgradeFixture):
    def test_ready_task_is_readable_and_resumable(self):
        self.write(self.legacy_task("task-ready", "READY", "prepare"))

        t = self.store.task("task-ready")

        self.assertEqual(t["state"], "READY")
        self.assertEqual(t["phase"], "prepare")
        # Resumable: state is accepted by start() and no writer is in doubt.
        self.assertIn(t["state"], ("READY", "PAUSED", "BLOCKED"))
        self.assertNotEqual(t.get("block_code"), "PROCESS_UNKNOWN")


class TestLegacyPaused(UpgradeFixture):
    def test_paused_task_retains_work(self):
        task = self.legacy_task(
            "task-paused",
            "PAUSED",
            "implement",
            attempts=2,
            candidate_sha="c" * 40,
            validated_sha="c" * 40,
        )
        self.write(task, [self.run_doc("task-paused", "run-old1", "exited")])

        t = self.store.task("task-paused")

        self.assertEqual(t["state"], "PAUSED")
        self.assertEqual(t["phase"], "implement", "paused work must keep its phase")
        self.assertEqual(t["attempts"], 2, "retry budget must survive upgrade")
        self.assertEqual(t["validated_sha"], "c" * 40)
        self.assertIn(t["state"], ("READY", "PAUSED", "BLOCKED"))


class TestLegacyBlocked(UpgradeFixture):
    def test_blocked_task_resumes_with_reason(self):
        self.write(
            self.legacy_task(
                "task-blocked",
                "BLOCKED",
                "implement",
                block_code="VALIDATION_FAILED",
            )
        )

        t = self.store.task("task-blocked")

        self.assertEqual(t["state"], "BLOCKED")
        self.assertEqual(t["block_code"], "VALIDATION_FAILED")
        self.assertIn(t["state"], ("READY", "PAUSED", "BLOCKED"))


class TestLegacyPublishInterrupted(UpgradeFixture):
    def test_publish_interruption_reconciles_before_reencode(self):
        """A dead writer on a legacy *container* run is now "unknown", not exited.

        v0.5.0 removed the container backend, so a historical docker run can no
        longer be observed at all (§18.4). The honest answer is
        ``PROCESS_UNKNOWN`` — the phase and the evidence survive, the run is not
        closed out, and `task recover --confirm-stop` is the only exit path.
        """
        task = self.legacy_task(
            "task-pub",
            "RUNNING",
            "publish",
            current_run_id="run-pub1",
            candidate_sha="d" * 40,
            validated_sha="d" * 40,
        )
        run = self.run_doc(
            "task-pub",
            "run-pub1",
            "running",
            pid=99999999,
            identity="stale-identity",
            container="zoe-dead",
        )
        self.write(task, [run])

        t = self.store.task("task-pub")
        from src._pipeline import reconcile

        # Dead PID -> supervisor gone; the container itself is unobservable.
        reconciled = reconcile(
            self.store, t, FakeDocker(state={"Running": False, "Status": "exited"})
        )

        self.assertEqual(reconciled["state"], "BLOCKED")
        self.assertEqual(reconciled["block_code"], "PROCESS_UNKNOWN")
        self.assertEqual(reconciled["phase"], "publish", "must not restart from implement")
        self.assertEqual(reconciled["validated_sha"], "d" * 40)

        # Not auto-closed: an unobservable execution is never declared finished.
        r = self.store.get_run("run-pub1")
        self.assertEqual(r["status"], "running")


class TestLegacyReviewReady(UpgradeFixture):
    def test_review_ready_keeps_evidence_linked(self):
        task = self.legacy_task(
            "task-review",
            "REVIEW_READY",
            "ci",
            candidate_sha="e" * 40,
            validated_sha="e" * 40,
            validated_fingerprint="fp-legacy",
            pr={"number": 7, "url": "https://example.invalid/pr/7"},
        )
        self.write(task)

        t = self.store.task("task-review")

        self.assertEqual(t["state"], "REVIEW_READY")
        self.assertEqual(t["validated_sha"], "e" * 40)
        self.assertEqual(
            t["validated_fingerprint"], "fp-legacy", "verification evidence must stay linked"
        )
        # No acceptance was recorded in 0.1.0; 0.2.0 must not invent one.
        self.assertIsNone(t["acceptance"])
        self.assertEqual(t["pr"]["number"], 7)


class TestLegacyTerminal(UpgradeFixture):
    def test_done_task_is_not_implicitly_restarted(self):
        self.write(self.legacy_task("task-done", "DONE", "acceptance"))

        req = {
            "schema_version": 1,
            "request_id": "r-done",
            "expected_version": 1,
            "authorization_ref": "legacy-scope",
        }
        with self.assertRaises(Error) as ctx:
            start(self.store, "task-done", req)
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")

    def test_cancelled_task_is_not_implicitly_restarted(self):
        self.write(self.legacy_task("task-cancel", "CANCELLED", "implement"))

        req = {
            "schema_version": 1,
            "request_id": "r-cancel",
            "expected_version": 1,
            "authorization_ref": "legacy-scope",
        }
        with self.assertRaises(Error) as ctx:
            start(self.store, "task-cancel", req)
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")


class TestLegacyOperationsIntact(UpgradeFixture):
    def test_legacy_operation_ids_are_not_replayed(self):
        """A 0.1.0 request_id must not be silently re-executed by 0.2.0."""
        task = self.legacy_task("task-op", "READY", "prepare")
        self.write(task)
        req = {"schema_version": 1, "request_id": "legacy-req", "x": 1}
        self.store.db.execute(
            "INSERT INTO operations VALUES(?,?,?)",
            (
                "legacy-req",
                digest(["create", None, req]),
                json.dumps(
                    {
                        "status": "succeeded",
                        "task_id": "task-op",
                        "action": "create",
                        "request_id": "legacy-req",
                    }
                ),
            ),
        )

        key, prior = self.store.operation(req, "create")

        # Same payload -> returns the recorded result instead of re-running.
        self.assertEqual(key, "legacy-req")
        self.assertIsNotNone(prior)
        self.assertEqual(prior["task_id"], "task-op")

    def test_legacy_operation_with_changed_payload_conflicts(self):
        task = self.legacy_task("task-op2", "READY", "prepare")
        self.write(task)
        req = {"schema_version": 1, "request_id": "legacy-req2", "x": 1}
        self.store.db.execute(
            "INSERT INTO operations VALUES(?,?,?)",
            (
                "legacy-req2",
                digest(["create", None, req]),
                json.dumps(
                    {
                        "status": "succeeded",
                        "task_id": "task-op2",
                        "action": "create",
                        "request_id": "legacy-req2",
                    }
                ),
            ),
        )

        with self.assertRaises(Error) as ctx:
            self.store.operation(
                {"schema_version": 1, "request_id": "legacy-req2", "x": 2}, "create"
            )
        self.assertEqual(ctx.exception.code, "REQUEST_CONFLICT")


if __name__ == "__main__":
    unittest.main()
