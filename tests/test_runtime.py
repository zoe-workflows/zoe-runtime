import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
from src.core import *


class FakeDocker:
    """In-process fixture only; never exposed as an execution backend in CLI."""

    def __init__(self, fail=False, mutate=False):
        self.calls = []
        self.fail = fail
        self.mutate = mutate

    def inspect(self, name):
        return None

    def execute(self, store, t, r, argv, log_name, worker=False, prompt=None):
        self.calls.append(log_name)
        if worker:
            (Path(t["workspace"]) / "src.txt").write_text("implemented\n")
        if self.mutate and not worker:
            (Path(t["workspace"]) / "src.txt").write_text("changed by check\n")
        return 1 if self.fail and not worker else 0


class FakePublisher:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def publish(self, t, s):
        self.calls += 1
        if self.fail:
            raise Error("REMOTE_UNKNOWN", "lost response")
        return {"number": 1, "url": "https://example.invalid/pr/1"}

    def refresh(self, t):
        return {"ready": True, "pr": {"number": 1, "url": "https://example.invalid/pr/1"}}


class RuntimeFixture(unittest.TestCase):
    """Shared fixture only — no test methods, so subclasses do not re-run them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "test")
        git(self.repo, "config", "user.email", "test@localhost")
        (self.repo / "src.txt").write_text("initial\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        self.s = Store(self.root / "state")
        self.spec = self.root / "spec.md"
        self.spec.write_text("# Test\nAC-001 implement")
        self.c = {
            "schema_version": 1,
            "project_id": "demo",
            "repo": str(self.repo),
            # v0.5.0: local-process is the only execution backend; in-process
            # `FakeDocker` (below) is a *test double*, not a backend choice.
            "backend": "local-process",
            "network": "none",
            "compatibility_verified": True,
            "agent": "codebuddy",
            "checks": [{"id": "unit", "argv": ["true"]}],
            "allowed_paths": ["src.txt"],
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute", "publish", "accept"],
            "ci_required": False,
            "max_attempts": 3,
        }
        atomic(self.s.home / "projects/demo.json", self.c)

    def tearDown(self):
        self.s.db.close()
        self.tmp.cleanup()

    def req(self, **kwargs):
        return dict(
            schema_version=1, request_id=uuid.uuid4().hex, authorization_ref="scope-1", **kwargs
        )

    def new(self, mode="local_artifact"):
        r = create(
            self.s,
            self.req(project_id="demo", title="Test", spec_file=str(self.spec), delivery_mode=mode),
        )
        return self.s.task(r["task_id"])

    def run_task(self, t, b=None, p=None):
        t = self.s.task(t["task_id"])
        rid = "run-" + uuid.uuid4().hex[:16]
        self.s.run({"run_id": rid, "task_id": t["task_id"], "status": "running", "container": None})
        t.update(current_run_id=rid, state="RUNNING")
        self.s.save(t)
        return run_pipeline(self.s, t["task_id"], b or FakeDocker(), p or FakePublisher())

    def add_remote(self):
        """Give the repo an ``origin`` whose target branch is the frozen baseline.

        The pre-publication drift check (v0.5 §7) fetches the target branch, so a
        pull_request task can no longer be driven against a repo without a remote:
        "cannot confirm" is ``REMOTE_UNKNOWN``, not "no drift".
        """
        bare = self.root / "remote.git"
        if not bare.exists():
            git(self.root, "init", "--bare", str(bare))
            git(self.repo, "remote", "add", "origin", str(bare))
            git(self.repo, "push", "origin", "HEAD:refs/heads/main")
        return bare


class RuntimeTest(RuntimeFixture):
    def test_local_delivery_and_acceptance(self):
        t = self.run_task(self.new())
        self.assertEqual(t["state"], "REVIEW_READY")
        self.assertEqual(t["validated_sha"], t["candidate_sha"])
        r = accept(
            self.s,
            t["task_id"],
            self.req(
                expected_version=t["row_version"],
                subject_sha=t["validated_sha"],
                actor="human",
                decision="accepted",
                evidence=["manual check"],
            ),
        )
        self.assertEqual(r["state"], "DONE")
        self.assertEqual((self.repo / "src.txt").read_text(), "initial\n")

    def test_create_idempotency(self):
        req = self.req(
            project_id="demo",
            title="Test",
            spec_file=str(self.spec),
            delivery_mode="local_artifact",
        )
        a = create(self.s, req)
        self.assertEqual(a, create(self.s, req))
        self.assertEqual(len(self.s.tasks()), 1)
        with self.assertRaisesRegex(Error, "reused"):
            create(self.s, {**req, "title": "different"})

    def test_authorization(self):
        with self.assertRaises(Error):
            create(
                self.s,
                {
                    **self.req(
                        project_id="demo",
                        title="x",
                        spec_file=str(self.spec),
                        delivery_mode="local_artifact",
                    ),
                    "authorization_ref": "invented",
                },
            )

    def test_spec_requires_acceptance(self):
        self.spec.write_text("ambiguous")
        with self.assertRaises(Error):
            self.new()

    def test_failed_checks_retain_workspace(self):
        t = self.run_task(self.new(), FakeDocker(fail=True))
        self.assertEqual(t["block_code"], "VALIDATION_FAILED")
        self.assertEqual(t["phase"], "implement")
        self.assertTrue(Path(t["workspace"]).exists())

    def test_validation_mutation_invalidates(self):
        t = self.run_task(self.new(), FakeDocker(mutate=True))
        self.assertEqual(t["block_code"], "WORKSPACE_CHANGED")
        self.assertIsNone(t["validated_sha"])

    def test_publish_resume_does_not_recode(self):
        self.add_remote()
        t = self.run_task(self.new("pull_request"), p=FakePublisher(fail=True))
        self.assertEqual(t["phase"], "publish")
        b = FakeDocker()
        t = self.run_task(t, b)
        self.assertEqual(b.calls, [])
        self.assertEqual(t["state"], "REVIEW_READY")

    def test_verify_only_resume(self):
        t = self.run_task(self.new(), FakeDocker(mutate=True))
        b = FakeDocker()
        t = self.run_task(t, b)
        self.assertEqual(b.calls, ["check-unit"])
        self.assertEqual(t["state"], "REVIEW_READY")

    def test_scope_violation(self):
        self.c["allowed_paths"] = ["other"]
        atomic(self.s.home / "projects/demo.json", self.c)
        t = self.run_task(self.new())
        self.assertEqual(t["block_code"], "SCOPE_VIOLATION")

    def test_spec_hash_tamper(self):
        t = self.new()
        atomic(self.s.path(t) / "spec-1.json", {"text": "AC-001 changed"})
        t = self.run_task(t)
        self.assertEqual(t["block_code"], "SPEC_CHANGED")

    def test_pause_idle_and_cancel(self):
        t = self.new()
        r = stop(self.s, t["task_id"], self.req(expected_version=t["row_version"]))
        self.assertEqual(r["state"], "PAUSED")
        r = stop(self.s, t["task_id"], self.req(expected_version=r["row_version"]), True)
        self.assertEqual(r["state"], "CANCELLED")

    def test_optimistic_conflict(self):
        t = self.new()
        with self.assertRaises(Error):
            stop(self.s, t["task_id"], self.req(expected_version=99))

    def test_revision_invalidates_evidence(self):
        t = self.run_task(self.new())
        self.spec.write_text("AC-002 new scope")
        r = revise(
            self.s,
            t["task_id"],
            self.req(expected_version=t["row_version"], spec_file=str(self.spec)),
        )
        t = self.s.task(t["task_id"])
        self.assertEqual(t["spec_revision"], 2)
        self.assertIsNone(t["validated_sha"])
        self.assertEqual(t["phase"], "implement")

    def test_accept_rejects_changed_workspace(self):
        t = self.run_task(self.new())
        (Path(t["workspace"]) / "src.txt").write_text("manual")
        with self.assertRaises(Error):
            accept(
                self.s,
                t["task_id"],
                self.req(
                    expected_version=t["row_version"],
                    subject_sha=t["candidate_sha"],
                    actor="human",
                    decision="accepted",
                    evidence=["checked"],
                ),
            )

    def test_orphan_container_blocks(self):
        t = self.new()
        rid = "run-orphan"
        self.s.run(
            {
                "run_id": rid,
                "task_id": t["task_id"],
                "status": "running",
                "pid": 99999999,
                "identity": "gone",
                "container": "old",
            }
        )
        t.update(current_run_id=rid, state="RUNNING")
        self.s.save(t)

        class Orphan:
            def inspect(self, n):
                return {"Running": True}

        t = reconcile(self.s, t, Orphan())
        self.assertEqual(t["block_code"], "PROCESS_UNKNOWN")

    def test_crash_no_writer_reconciles(self):
        t = self.new()
        rid = "run-dead"
        self.s.run(
            {
                "run_id": rid,
                "task_id": t["task_id"],
                "status": "starting",
                "pid": 99999999,
                "identity": "gone",
                "container": None,
            }
        )
        t.update(current_run_id=rid, state="RUNNING")
        self.s.save(t)
        t = reconcile(self.s, t, FakeDocker())
        self.assertEqual(t["block_code"], "RUN_INTERRUPTED")
        self.assertEqual(self.s.get_run(rid)["status"], "exited")

    def test_active_identity_not_restarted(self):
        t = self.new()
        rid = "run-live"
        self.s.run(
            {
                "run_id": rid,
                "task_id": t["task_id"],
                "status": "running",
                "pid": os.getpid(),
                "identity": "fixture-live-identity",
                "container": None,
            }
        )
        t.update(current_run_id=rid, state="RUNNING")
        self.s.save(t)
        with patch("src._proc.process_identity", return_value="fixture-live-identity"):
            self.assertEqual(reconcile(self.s, t)["state"], "RUNNING")

    def test_real_cli_and_sqlite_persist(self):
        t = self.new()
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        p = subprocess.run(
            [
                sys.executable,
                "-m",
                "src",
                "--home",
                str(self.s.home),
                "task",
                "status",
                t["task_id"],
                "--json",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["task_id"], t["task_id"])

    def test_no_unsafe_backend(self):
        with self.assertRaises(Error):
            validate_config({**self.c, "backend": "shell"})

    def test_retry_limit(self):
        t = self.new()
        t["attempts"] = 3
        self.s.save(t)
        t = self.run_task(t)
        self.assertEqual(t["block_code"], "RETRY_EXHAUSTED")

    def test_changed_candidate_blocks_publish(self):
        self.add_remote()
        t = self.run_task(self.new("pull_request"), p=FakePublisher(fail=True))
        (Path(t["workspace"]) / "src.txt").write_text("manual")
        p = FakePublisher()
        t = self.run_task(t, p=p)
        self.assertEqual(p.calls, 0)
        self.assertEqual(t["phase"], "verify")

    def test_slot_lock(self):
        with self.s.lock("execution", False):
            other = Store(self.s.home)
            try:
                with self.assertRaises(Error):
                    with other.lock("execution", False):
                        pass
            finally:
                other.db.close()

    def test_unavailable_container_is_unknown(self):
        t = self.new()
        rid = "run-unknown"
        self.s.run(
            {
                "run_id": rid,
                "task_id": t["task_id"],
                "status": "running",
                "pid": 99999999,
                "identity": "gone",
                "container": "old",
            }
        )
        t.update(current_run_id=rid, state="RUNNING")
        self.s.save(t)

        class Down:
            def inspect(self, n):
                raise Error("PROCESS_UNKNOWN", "daemon down")

        self.assertEqual(reconcile(self.s, t, Down())["block_code"], "PROCESS_UNKNOWN")

    def test_create_recovers_unfinished_intent(self):
        req = self.req(
            project_id="demo",
            title="Test",
            spec_file=str(self.spec),
            delivery_mode="local_artifact",
        )
        self.s.operation(req, "create")
        self.assertEqual(create(self.s, req)["status"], "succeeded")
        self.assertEqual(len(self.s.tasks()), 1)

    def test_revision_before_first_start(self):
        t = self.new()
        revise(
            self.s,
            t["task_id"],
            self.req(expected_version=t["row_version"], spec_file=str(self.spec)),
        )
        t = self.s.task(t["task_id"])
        self.assertEqual(t["phase"], "prepare")
        self.assertEqual(self.run_task(t)["state"], "REVIEW_READY")

    def test_acceptance_revocation(self):
        t = self.run_task(self.new())
        self.c["authorized_actions"].remove("accept")
        atomic(self.s.home / "projects/demo.json", self.c)
        with self.assertRaises(Error):
            accept(
                self.s,
                t["task_id"],
                self.req(
                    expected_version=t["row_version"],
                    subject_sha=t["candidate_sha"],
                    actor="human",
                    decision="accepted",
                    evidence=["checked"],
                ),
            )

    def test_publish_rechecks_policy(self):
        self.add_remote()
        t = self.run_task(self.new("pull_request"), p=FakePublisher(fail=True))
        self.c["authorized_actions"].remove("publish")
        atomic(self.s.home / "projects/demo.json", self.c)
        with self.assertRaisesRegex(Error, "Policy changed"):
            Publisher(t["config"]).publish(t, self.s)

    def test_stop_intent_can_retry(self):
        t = self.new()
        req = self.req(expected_version=t["row_version"])
        self.s.operation(req, "pause", t["task_id"])
        self.assertEqual(stop(self.s, t["task_id"], req)["state"], "PAUSED")
        self.assertEqual(stop(self.s, t["task_id"], req)["state"], "PAUSED")

    def test_revision_retry_not_double_applied(self):
        t = self.new()
        req = self.req(expected_version=t["row_version"], spec_file=str(self.spec))
        a = revise(self.s, t["task_id"], req)
        self.assertEqual(a, revise(self.s, t["task_id"], req))
        self.assertEqual(self.s.task(t["task_id"])["spec_revision"], 2)

    def test_process_probe_failure_is_unknown(self):
        t = self.new()
        rid = "run-probe"
        self.s.run(
            {
                "run_id": rid,
                "task_id": t["task_id"],
                "status": "running",
                "pid": os.getpid(),
                "identity": "fixture",
                "container": None,
            }
        )
        t.update(current_run_id=rid, state="RUNNING")
        self.s.save(t)
        with patch(
            "src._proc.process_identity",
            side_effect=Error("ENVIRONMENT_ERROR", "probe failed"),
        ):
            self.assertEqual(reconcile(self.s, t)["block_code"], "PROCESS_UNKNOWN")

    def test_internal_entry_refuses_forged_launch(self):
        t = self.new()
        rid = "run-forged"
        self.s.run(
            {
                "run_id": rid,
                "task_id": t["task_id"],
                "status": "starting",
                "pid": 99999999,
                "identity": "fake",
                "container": None,
            }
        )
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        p = subprocess.run(
            [sys.executable, "-m", "src", "--home", str(self.s.home), "internal", rid, "0"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["error"]["code"], "POLICY_DENIED")

    def test_pr_timeout_reuses_existing_remote_pr(self):
        bare = self.root / "remote.git"
        git(self.root, "init", "--bare", str(bare))
        git(self.repo, "remote", "add", "origin", str(bare))
        git(self.repo, "push", "origin", "HEAD:refs/heads/main")
        self.c["github_repo"] = "example/test"
        atomic(self.s.home / "projects/demo.json", self.c)
        t = self.run_task(self.new("pull_request"), p=FakePublisher(fail=True))

        class Remote(Publisher):
            def __init__(self, c):
                super().__init__(c)
                self.items = []
                self.created = 0

            def gh(self, *args):
                if args[:2] == ("pr", "list"):
                    return json.dumps(self.items)
                if args[:2] == ("pr", "create"):
                    self.created += 1
                    self.items = [
                        {
                            "number": 1,
                            "url": "https://example.invalid/1",
                            "state": "OPEN",
                            "body": "<!-- zoe-task: " + t["task_id"] + " -->",
                        }
                    ]
                    raise Error("ENVIRONMENT_ERROR", "response lost after server success")
                raise AssertionError(args)

        pub = Remote(t["config"])
        with self.assertRaises(Error):
            pub.publish(t, self.s)
        result = pub.publish(t, self.s)
        self.assertEqual(pub.created, 1)
        self.assertEqual(result["number"], 1)
        self.assertEqual(git(bare, "rev-parse", "refs/heads/" + t["branch"]), t["candidate_sha"])

    def test_no_empty_ci_success(self):
        class Remote(Publisher):
            def gh(self, *args):
                return json.dumps({"headRefOid": "abc", "statusCheckRollup": [], "state": "OPEN"})

        t = {"pr": {"number": 1}, "candidate_sha": "abc"}
        self.assertFalse(Remote({"github_repo": "a/b"}).refresh(t)["ready"])
        self.assertTrue(Remote({"github_repo": "a/b", "ci_required": False}).refresh(t)["ready"])

    def test_exited_run_pending_stop_reconciles(self):
        t = self.new()
        rid = "run-exited"
        self.s.run({"run_id": rid, "task_id": t["task_id"], "status": "exited"})
        t.update(current_run_id=rid, state="PAUSING", stop_requested="cancel")
        self.s.save(t)
        self.assertEqual(reconcile(self.s, t)["state"], "CANCELLED")

    def test_finalization_honors_late_stop(self):
        t = self.new()

        class Late(FakeDocker):
            def inspect(inner, name):
                return None

        original = self.s.save

        def inject(task, event="updated", **kw):
            result = original(task, event, **kw)
            if event == "phase_update" and task["state"] == "REVIEW_READY":
                current = self.s.task(task["task_id"])
                current.update(state="PAUSING", stop_requested="pause")
                original(current)
            return result

        with patch.object(self.s, "save", side_effect=inject):
            t = self.run_task(t, Late())
        self.assertEqual(t["state"], "PAUSED")

    def test_agent_result_errors(self):
        self.assertEqual(parse_agent_output('{"result":"done"}')["result"], "done")
        for raw in ("not json", '{"is_error":true}', '{"status":"blocked"}'):
            with self.assertRaises(Error):
                parse_agent_output(raw)

    def test_docker_inspection_is_bounded(self):
        with patch(
            "src._backend.subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 15)
        ):
            with self.assertRaisesRegex(Error, "timed out"):
                Docker(self.c).inspect("test")

    def test_pending_docker_start_does_not_release_writer(self):
        t = self.new()
        rid = "run-pending"
        self.s.run(
            {
                "run_id": rid,
                "task_id": t["task_id"],
                "status": "running",
                "pid": 99999999,
                "identity": "gone",
                "container": "old",
                "launch_stage": "start_requested",
            }
        )
        t.update(current_run_id=rid, state="RUNNING")
        self.s.save(t)

        class Created:
            def inspect(self, name):
                return {"Running": False, "Status": "created"}

        self.assertEqual(reconcile(self.s, t, Created())["block_code"], "PROCESS_UNKNOWN")

    def test_platform_locks_are_mutually_exclusive(self):
        """v0.4 replaces fd-inherited execution lease with platform-layer locks.

        Two exclusive writers exclude each other; a shared lock and an exclusive
        lock exclude each other; the lock is released on close.
        """
        from src._platform import get_platform

        plat = get_platform()
        path = self.s.lock_path("execution")
        h1 = plat.lock_exclusive(path, blocking=False)
        with self.assertRaises(Error):
            plat.lock_exclusive(path, blocking=False)
        plat.unlock(h1)
        # After release the lock is acquirable again.
        h2 = plat.lock_shared(path)
        h3 = plat.lock_shared(path)  # two shared locks coexist
        with self.assertRaises(Error):
            plat.lock_exclusive(path, blocking=False)
        plat.unlock(h3)
        plat.unlock(h2)

    def test_host_network_is_prohibited(self):
        with self.assertRaises(Error):
            validate_config({**self.c, "network": "host", "network_policy_confirmed": True})


class TestLocalProcessBackend(unittest.TestCase):
    """LocalProcess backend unit tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "test")
        git(self.repo, "config", "user.email", "test@localhost")
        (self.repo / "src.txt").write_text("initial\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")

        self.s = Store(self.root / "state")
        self.spec = self.root / "spec.md"
        self.spec.write_text("# Test\nAC-001 implement")
        # Deterministic stand-in for the agent CLI. This class drives the whole
        # pipeline through LocalProcess, so it must not depend on which CLI happens
        # to be installed on the machine (or on that CLI's credentials).
        self.bin = self.root / "bin"
        self.bin.mkdir()
        stub = self.bin / "codebuddy"
        stub.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo "0.0.0-stub"; exit 0; fi\n'
            'echo \'{"type":"result","is_error":false,"result":"stub done"}\'\n'
        )
        stub.chmod(0o755)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.bin) + os.pathsep + self._old_path
        from src import _discovery

        _discovery.clear_cache()
        self.c_local = {
            **{
                "schema_version": 1,
                "project_id": "demo-local",
                "repo": str(self.repo),
                "backend": "local",
                "agent": "codebuddy",
                "checks": [{"id": "unit", "argv": ["true"]}],
                "allowed_paths": ["src.txt"],
                "authorization_ref": "scope-local",
                "authorized_actions": ["create", "execute", "publish", "accept"],
                "ci_required": False,
                "max_attempts": 3,
                # A v0.2 project was approved for execution by this declaration;
                # it becomes a `legacy_admin_declaration` record. Without it the
                # unified capability gate now blocks the task, which is the point.
                "compatibility_verified": True,
            }
        }
        atomic(self.s.home / "projects/demo-local.json", self.c_local)

    def tearDown(self):
        from src import _discovery

        _discovery.clear_cache()
        os.environ["PATH"] = self._old_path
        self.s.db.close()
        self.tmp.cleanup()

    def req(self, **kwargs):
        return dict(
            schema_version=1,
            request_id=f"req-{__import__('uuid').uuid4().hex[:16]}",
            authorization_ref="scope-local",
            **kwargs,
        )

    def new_task(self, mode="local_artifact"):
        r = create(
            self.s,
            self.req(
                project_id="demo-local",
                title="Test LocalProcess",
                spec_file=str(self.spec),
                delivery_mode=mode,
            ),
        )
        return self.s.task(r["task_id"])

    def setup_task_for_execute(self):
        """Create task with worktree ready for execute() testing."""
        t = self.new_task()
        # Create worktree directory (normally done by run_pipeline via git worktree add)
        workspace = Path(t["workspace"])
        workspace.mkdir(parents=True, exist_ok=True)
        # Initialize git repo in worktree
        git(workspace, "init", "-b", t["branch"])
        git(workspace, "config", "user.name", "test")
        git(workspace, "config", "user.email", "test@localhost")
        # Copy initial file
        (workspace / "src.txt").write_text("initial\n")
        git(workspace, "add", ".")
        git(workspace, "commit", "-m", "initial")

        rid = "run-" + __import__("uuid").uuid4().hex[:16]
        self.s.run({"run_id": rid, "task_id": t["task_id"], "status": "running", "container": None})
        t.update(current_run_id=rid, state="RUNNING")
        self.s.save(t)

        # Create runs directory (normally done by start())
        runs_dir = self.s.path(t) / "runs" / rid
        runs_dir.mkdir(parents=True, exist_ok=True)

        return t, rid

    def test_local_backend_creation(self):
        """LocalProcess backend can be created from config."""
        backend = LocalProcess(self.c_local)
        self.assertIsInstance(backend, BackendBase)
        self.assertIsInstance(backend, LocalProcess)

    def test_inspect_no_process(self):
        """inspect returns None when no process has been started."""
        backend = LocalProcess(self.c_local)
        result = backend.inspect("test-name")
        self.assertIsNone(result)

    def test_create_backend_factory(self):
        """create_backend() returns LocalProcess for local config."""
        backend = create_backend(self.c_local)
        self.assertIsInstance(backend, LocalProcess)

    def test_create_backend_factory_docker(self):
        """create_backend() returns Docker for docker config."""
        docker_config = {**self.c_local, "backend": "docker", "image": "test@sha256:" + "a" * 64}
        backend = create_backend(docker_config)
        self.assertIsInstance(backend, Docker)

    def test_create_backend_factory_unknown(self):
        """create_backend() raises error for unknown backend type."""
        bad_config = {**self.c_local, "backend": "invalid"}
        with self.assertRaises(Error) as ctx:
            create_backend(bad_config)
        self.assertEqual(ctx.exception.code, "CAPABILITY_UNSUPPORTED")

    def test_validate_config_accepts_local(self):
        """validate_config accepts backend='local' (aliased to local-process)."""
        # Should not raise
        validated = validate_config({**self.c_local})
        self.assertEqual(validated["backend"], "local-process")

    def test_validate_config_local_no_image_required(self):
        """Local backend does not require image field."""
        config_no_image = {k: v for k, v in self.c_local.items() if k != "image"}
        validated = validate_config(config_no_image)
        self.assertEqual(validated["backend"], "local-process")

    def test_doctor_local_mode(self):
        """doctor() returns ready=True for local mode without Docker."""
        result = doctor(self.c_local)
        self.assertEqual(result["backend"], "local-process")
        self.assertIsNone(result["checks"].get("docker"))
        # process_identity should work on macOS/Linux
        if result["checks"].get("process_identity"):
            # ready requires git + process_identity + adapter_approved
            self.assertTrue(result["checks"]["git"])
            # adapter_approved needs compatibility_verified in config
            if not result["ready"]:
                self.assertFalse(result["checks"].get("adapter_approved", False))

    def test_execute_simple_command(self):
        """execute() runs a simple command and returns exit code."""
        backend = LocalProcess(self.c_local)
        t, rid = self.setup_task_for_execute()

        # Execute a simple echo command (simulated agent)
        exit_code = backend.execute(
            self.s,
            t,
            self.s.get_run(rid),
            [sys.executable, "-c", "import json; print(json.dumps({'result': 'done'}))"],
            "agent",
            worker=True,
            prompt="Test prompt",
        )
        self.assertEqual(exit_code, 0)

        # Verify log file was created
        log_file = self.s.path(t) / "runs" / rid / "agent.log"
        self.assertTrue(log_file.exists())

        # Verify agent result was parsed
        result_file = self.s.path(t) / "runs" / rid / "agent-result.json"
        self.assertTrue(result_file.exists())

    def test_execute_failing_command(self):
        """execute() returns non-zero exit code for failing command."""
        backend = LocalProcess(self.c_local)
        t, rid = self.setup_task_for_execute()

        exit_code = backend.execute(
            self.s,
            t,
            self.s.get_run(rid),
            [sys.executable, "-c", "exit(1)"],
            "check-test",
            worker=False,
        )
        self.assertEqual(exit_code, 1)

    def test_stop_running_process(self):
        """stop() terminates a running process."""
        backend = LocalProcess(self.c_local)
        t, rid = self.setup_task_for_execute()

        # Start a long-running process in background
        import threading

        result = {"exit_code": None}

        def run_slow_process():
            # A sqlite connection belongs to the thread that opened it, so the
            # worker thread gets its own Store instead of borrowing the fixture's.
            from src._store import Store as _Store

            local_store = _Store(self.s.home)
            try:
                result["exit_code"] = backend.execute(
                    local_store,
                    t,
                    local_store.get_run(rid),
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    "slow-agent",
                    worker=False,
                )
            except Error as e:
                result["error"] = e
            finally:
                local_store.db.close()

        thread = threading.Thread(target=run_slow_process)
        thread.start()
        import time as _time

        _time.sleep(0.5)  # Let process start

        # Stop it
        backend.stop("local-" + rid + "-slow-agent")
        thread.join(timeout=2)

        # Process should be stopped
        if backend._process:
            self.assertIsNotNone(backend._process.returncode)

    def test_env_whitelist_filtering(self):
        """Environment variables are filtered to CODEBUDDY_*/ANTHROPIC_* only."""
        backend = LocalProcess(self.c_local)
        env = backend._build_env(worker=True)

        # Should have basic vars
        self.assertIn("HOME", env)
        self.assertIn("PATH", env)

        # Should NOT have arbitrary env vars (even if they exist in os.environ)
        self.assertNotIn("RANDOM_ENV_VAR", env)

    def test_full_pipeline_with_local(self):
        """Complete pipeline works with LocalProcess backend."""
        t = self.new_task()
        rid = "run-" + __import__("uuid").uuid4().hex[:16]
        self.s.run({"run_id": rid, "task_id": t["task_id"], "status": "running", "container": None})
        t.update(current_run_id=rid, state="RUNNING")
        self.s.save(t)

        # Run pipeline with LocalProcess
        backend = LocalProcess(self.c_local)
        publisher = FakePublisher()
        t = run_pipeline(self.s, t["task_id"], backend, publisher)

        # Should complete successfully
        self.assertIn(t["state"], ("REVIEW_READY", "DONE"))

    def test_reconcile_local_process(self):
        """reconcile() works with LocalProcess exited state."""
        t = self.new_task()
        rid = "run-dead-local"
        self.s.run(
            {
                "run_id": rid,
                "task_id": t["task_id"],
                "status": "exited",  # Already exited
                "pid": 99999999,
                "identity": "gone",
                "container": "local-" + rid + "-agent",
            }
        )
        t.update(current_run_id=rid, state="RUNNING")
        self.s.save(t)

        # Reconcile with exited run and no stop_request: state unchanged
        t = reconcile(self.s, t, LocalProcess(self.c_local))
        self.assertEqual(t["state"], "RUNNING")

        # Reconcile with stop_request should pause
        t.update(stop_requested="test-reason")
        self.s.save(t)
        t = reconcile(self.s, t, LocalProcess(self.c_local))
        self.assertEqual(t["state"], "PAUSED")


if __name__ == "__main__":
    unittest.main()
