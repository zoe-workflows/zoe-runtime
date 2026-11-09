"""v0.4 review fix, group 3: lifecycle lock scope and ordering.

The supervisor must hold ``maintenance`` SH + ``writer`` EX for the whole run
(01 §5.1), taken in the 01 §5.2 order (``maintenance`` -> ``writer`` ->
``control``). Before this fix ``start`` took them and released them immediately
after spawning, so during the agent's entire execution no writer lock was held
and ``control`` was acquired *before* ``maintenance`` — a lock-order inversion
against ``state migrate`` (which takes ``maintenance`` EX then ``control``).
"""

import json
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from src._pipeline import lifecycle_locks, release_run_tree
from src._platform import get_platform
from src._store import Store
from src._util import Error


class LockFixture(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.home = Path(self.td.name) / "home"
        self.store = Store(self.home)
        self.plat = get_platform()

    def tearDown(self):
        self.store.db.close()
        self.td.cleanup()

    def writer_busy(self, task_id="t1"):
        """Whether the writer lock is currently held by someone else."""
        path = self.store.lock_path(f"tasks/{task_id}/writer")
        try:
            handle = self.plat.lock_exclusive(path, blocking=False)
        except Error:
            return True
        self.plat.unlock(handle)
        return False


class TestLifecycleLocks(LockFixture):
    def test_writer_lock_is_held_for_the_whole_block(self):
        self.assertFalse(self.writer_busy())
        with lifecycle_locks(self.store, "t1"):
            self.assertTrue(self.writer_busy(), "writer lock must be held during the run")
        self.assertFalse(self.writer_busy(), "writer lock must be released afterwards")

    def test_control_is_held_when_requested(self):
        with lifecycle_locks(self.store, "t1", control=True):
            with self.assertRaises(Error):
                with self.store.lock(blocking=False):
                    pass
        with self.store.lock(blocking=False):
            pass

    def test_control_is_free_when_not_requested(self):
        """The supervisor runs the pipeline outside the short-operation lock."""
        with lifecycle_locks(self.store, "t1"):
            with self.store.lock(blocking=False):
                pass

    def test_locks_are_released_on_exception(self):
        with self.assertRaises(RuntimeError):
            with lifecycle_locks(self.store, "t1", control=True):
                raise RuntimeError("boom")
        self.assertFalse(self.writer_busy())
        with self.store.lock(blocking=False):
            pass

    def test_maintenance_is_shared_not_exclusive(self):
        """Two runs on different tasks may share the maintenance barrier."""
        with lifecycle_locks(self.store, "t1"):
            handle = self.plat.lock_shared(self.store.lock_path("maintenance"))
            self.plat.unlock(handle)

    def test_maintenance_blocks_migration(self):
        with lifecycle_locks(self.store, "t1"):
            with self.assertRaises(Error):
                self.plat.lock_exclusive(self.store.lock_path("maintenance"), blocking=False)

    def test_supervisor_can_take_over_a_lock_start_still_holds(self):
        """The supervisor is spawned while `start` holds the lock.

        With a fail-fast acquire the supervisor would kill itself with TASK_BUSY
        before `start` ever released it, so the hand-off must be a bounded wait.
        """
        import threading

        acquired = threading.Event()
        failure = []

        def supervisor():
            try:
                with lifecycle_locks(self.store, "t1", wait=10):
                    acquired.set()
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                failure.append(exc)

        held = self.plat.lock_exclusive(self.store.lock_path("tasks/t1/writer"), blocking=False)
        thread = threading.Thread(target=supervisor)
        thread.start()
        try:
            # While `start` holds it the supervisor must not have the lock yet.
            time.sleep(0.3)
            self.assertFalse(acquired.is_set())
        finally:
            self.plat.unlock(held)
        thread.join(timeout=10)
        self.assertEqual(failure, [])
        self.assertTrue(acquired.is_set(), "supervisor must take over once released")

    def test_second_writer_is_refused_with_task_busy(self):
        with lifecycle_locks(self.store, "t1"):
            with self.assertRaises(Error) as ctx:
                with lifecycle_locks(self.store, "t1"):
                    pass
        self.assertEqual(ctx.exception.code, "TASK_BUSY")
        self.assertFalse(self.writer_busy())


class TestReconcileWriterProbe(LockFixture):
    """``reconcile`` 在 ``start`` 的锁内不能把自己判成"写者还在"。

    ``flock`` 对同一文件的第二个 fd 不可重入，所以在 ``lifecycle_locks`` 里再探
    一次 writer 锁**必然**失败。旧的 ``reconcile`` 把这个失败读成
    ``PROCESS_UNKNOWN``，于是崩溃后的 ``task start`` 永远被自己的锁挡回去——错误码
    还指向"进程仍在运行"这个不存在的事实。
    """

    def dead_task(self, task_id, run_id):
        task = {
            "task_id": task_id,
            "project_id": "p1",
            "state": "RUNNING",
            "phase": "implement",
            "current_run_id": run_id,
            "config": {},
            "row_version": 1,
        }
        self.store.db.execute(
            "INSERT INTO tasks(id,version,data) VALUES(?,?,?)",
            (task_id, 1, json.dumps(task)),
        )
        self.store.run(
            {
                "run_id": run_id,
                "task_id": task_id,
                "status": "running",
                "pid": 999999,
                "start_token": "1.000000",
                "group_token": None,
                "dispatch_state": "started",
                "backend": "local-process",
                "execution_backend": "local-process",
            }
        )
        return self.store.task(task_id)

    def test_probe_still_reports_an_external_writer(self):
        from src._pipeline import reconcile

        task = self.dead_task("t-probe", "r-probe")
        with lifecycle_locks(self.store, "t-probe"):
            reconciled = reconcile(self.store, task)
        self.assertEqual(reconciled["block_code"], "PROCESS_UNKNOWN")

    def test_self_held_writer_lock_closes_the_run_out(self):
        from src._pipeline import reconcile

        task = self.dead_task("t-self", "r-self")
        with lifecycle_locks(self.store, "t-self"):
            reconciled = reconcile(self.store, task, writer_held=True)
        self.assertEqual(reconciled["state"], "BLOCKED")
        self.assertEqual(reconciled["block_code"], "RUN_INTERRUPTED")
        self.assertEqual(self.store.get_run("r-self")["status"], "exited")


class TestLockPaths(LockFixture):
    """`start` and `state migrate` must contend on the *same* files (01 §5.1)."""

    def test_paths_match_the_contract(self):
        from src._store import lock_path

        home = self.store.home  # resolved
        self.assertEqual(Path(lock_path(home, "control")), home / "control.lock")
        self.assertEqual(Path(lock_path(home, "maintenance")), home / "maintenance.lock")
        self.assertEqual(
            Path(lock_path(home, "tasks/t1/writer")),
            home / "tasks" / "t1" / ".writer.lock",
        )
        # The store resolves every lock name through the same helper.
        for name in ("control", "maintenance", "tasks/t1/writer"):
            self.assertEqual(Path(self.store.lock_path(name)), Path(lock_path(home, name)))

    def test_migration_contends_with_a_held_control_lock(self):
        from src import _migrations

        held = self.plat.lock_exclusive(self.store.lock_path("control"), blocking=False)
        try:
            with self.assertRaises(Error) as ctx:
                _migrations.migrate(self.home, 3, dry_run=True)
        finally:
            self.plat.unlock(held)
        self.assertEqual(ctx.exception.code, "SLOT_BUSY")


class TestBoundedLockWait(LockFixture):
    def test_bounded_wait_gives_up_after_timeout(self):
        path = self.store.lock_path("probe")
        held = self.plat.lock_exclusive(path, blocking=False)
        try:
            started = time.monotonic()
            with self.assertRaises(Error) as ctx:
                self.plat.lock_exclusive(path, blocking=True, timeout=0.2)
            elapsed = time.monotonic() - started
        finally:
            self.plat.unlock(held)
        self.assertEqual(ctx.exception.code, "SLOT_BUSY")
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 5.0, "timeout must actually bound the wait")

    def test_bounded_wait_succeeds_when_free(self):
        handle = self.plat.lock_exclusive(self.store.lock_path("free"), timeout=1.0)
        self.plat.unlock(handle)


class TestReleaseRunTree(LockFixture):
    def test_missing_run_is_a_noop(self):
        release_run_tree(self.store, "run-missing")

    def test_unknown_tree_is_a_noop(self):
        """A tree token that no longer resolves must not raise."""
        self.store.run(
            {
                "run_id": "run-1",
                "task_id": "t1",
                "pid": 999999,
                "start_token": "stale",
                "group_token": "999999",
            }
        )
        release_run_tree(self.store, "run-1")

    def test_run_without_tree_token_is_a_noop(self):
        self.store.run({"run_id": "run-2", "task_id": "t1", "pid": None, "group_token": None})
        release_run_tree(self.store, "run-2")


class TestSupervisorOwnsTheLocks(LockFixture):
    """``_run_internal`` acquires the lifecycle locks before the CAS handshake."""

    def _run_record(self, run_id, attempt_id):
        self.store.run(
            {
                "run_id": run_id,
                "task_id": "t1",
                "pid": 1,
                "start_token": "x",
                "dispatch_state": "spawn_observed",
                "attempt_id": attempt_id,
                "lock_owner": {"run_id": run_id, "attempt_id": attempt_id},
                "protocol_version": 4,
            }
        )
        return Namespace(run_id=run_id, attempt_id=attempt_id)

    def test_supervisor_refuses_to_start_when_a_writer_holds_the_lock(self):
        from src import cli

        args = self._run_record("run-x", "A")
        held = self.plat.lock_exclusive(self.store.lock_path("tasks/t1/writer"), blocking=False)
        try:
            # Keep the bounded wait short: the production value is generous on
            # purpose (it covers supervisor start-up), which is too slow for a test.
            with mock.patch.object(cli, "SUPERVISOR_LOCK_WAIT", 0.3):
                with self.assertRaises(Error) as ctx:
                    cli._run_internal(self.store, args)
        finally:
            self.plat.unlock(held)
        # Lock acquisition happens *before* the handshake, so the failure is
        # TASK_BUSY rather than a policy/state error.
        self.assertEqual(ctx.exception.code, "TASK_BUSY")

    def test_supervisor_releases_the_locks_after_a_failed_handshake(self):
        from src.cli import _run_internal

        # No task row exists, so the handshake fails with TASK_NOT_FOUND.
        args = self._run_record("run-y", "A")
        with self.assertRaises(Error):
            _run_internal(self.store, args)
        self.assertFalse(
            self.writer_busy("t1"), "a failed supervisor must not leak the writer lock"
        )
        with self.store.lock(blocking=False):
            pass


if __name__ == "__main__":
    unittest.main()
