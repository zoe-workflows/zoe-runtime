"""v0.5.1 W2: dispatch replay, and what ``TASK_BUSY`` is allowed to mean.

Two claims are tested here, and they are deliberately different:

* **same request** → replay. The finished response comes back byte-for-byte; an
  open intent comes back as ``dispatch_pending`` with ``run_id: null``. Neither
  path writes anything.
* **different request** → every check runs again. If a writer holds the lock the
  answer is ``TASK_BUSY`` (with a read-only ``details`` snapshot), never a
  fabricated success: a crashed ``spawn_intent`` must not be reported as
  "already running".

The last two tests use **real child processes** with a synchronisation barrier,
because the bug being fixed is a race between two processes, not a code path
inside one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_runtime import RuntimeFixture

from src import _pipeline
from src._pipeline import INTENT_GRACE_SECONDS, SUPERVISOR_LOCK_WAIT
from src._platform import get_platform
from src._store import Store
from src._util import Error, safe_id
from src.core import atomic, reconcile, start

SRC = Path(__file__).resolve().parents[1]

#: A child process that (a) waits for its peer inside the *first* idempotency
#: probe, so both really do miss, and (b) replaces only `spawn`, because this
#: test is about lock contention, not about launching a supervisor.
DRIVER = r"""
import json, os, sys, time, types
from pathlib import Path

root, home, barrier, me, other, job_file = sys.argv[1:7]
sys.path.insert(0, root)
os.environ["PYTHONPATH"] = root

from src import _pipeline
from src._platform import get_platform
from src._store import Store

jobs = json.loads(Path(job_file).read_text())
job = jobs[0] if me == "a" else jobs[1]

store = Store(home)
real = Store.operation_prior
state = {"first": True}


def patched(self, request, action, task=None):
    if state["first"]:
        state["first"] = False
        Path(barrier, me + ".ready").write_text("1")
        deadline = time.time() + 30
        while not Path(barrier, other + ".ready").exists():
            if time.time() > deadline:
                break
            time.sleep(0.01)
    return real(self, request, action, task)


Store.operation_prior = patched
plat = get_platform()
plat.spawn = lambda *a, **k: types.SimpleNamespace(
    pid=os.getpid(), start_token="fake-token", group_token="fake-group"
)

try:
    value = _pipeline.start(store, job["task_id"], job["request"])
    payload = {"ok": True, "value": value}
except BaseException as exc:
    payload = {
        "ok": False,
        "code": getattr(exc, "code", type(exc).__name__),
        "message": str(exc),
    }
print(json.dumps(payload))
"""


def patched_dispatch():
    """Patch the two environment-dependent steps of a *dispatch*.

    ``spawn`` returns a fake handle so no supervisor is launched, and the
    pre-flight import probe is skipped because the replay logic is what is under
    test (the probe itself is covered by ``test_v041_admission``).
    """
    platform = get_platform()
    handle = SimpleNamespace(pid=os.getpid(), start_token="fake", group_token="fake")
    return (
        mock.patch.object(platform, "spawn", return_value=handle),
        mock.patch.object(_pipeline, "require_supervisor_importable", return_value=None),
    )


class ReplayFixture(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.task = self.new()

    def launch(self, req, action="start", task=None):
        task = task or self.task
        spawn, probe = patched_dispatch()
        with spawn, probe:
            return start(self.s, task["task_id"], req, action)

    def event_count(self):
        rows, _, _ = self.s.events(self.task["task_id"], 0, 500)
        return len(rows)

    def body(self, value):
        return json.dumps(value, sort_keys=True)


class TestReplay(ReplayFixture):
    def test_a_finished_request_is_replayed_byte_for_byte(self):
        req = self.req(expected_version=self.task["row_version"])
        first = self.launch(req)
        events = self.event_count()
        version = self.s.task(self.task["task_id"])["row_version"]

        second = self.launch(req)

        self.assertEqual(self.body(second), self.body(first))
        self.assertEqual(self.s.task(self.task["task_id"])["row_version"], version)
        self.assertEqual(self.event_count(), events, "a replay writes no event")
        self.assertEqual(len(self.s.runs(self.task["task_id"])), 1, "no second dispatch")

    def test_an_open_intent_is_reported_as_pending_without_borrowing_a_run(self):
        req = self.req(expected_version=self.task["row_version"])
        self.s.operation(req, "start", self.task["task_id"])  # intent row, never finished

        value = self.launch(req)

        self.assertTrue(value["dispatch_pending"])
        self.assertTrue(value["accepted"])
        self.assertFalse(value["completed"])
        self.assertIsNone(value["run_id"], "an unconfirmed request must not borrow a run")
        self.assertNotIn("row_version", value, "the top level is not a task snapshot")
        self.assertNotIn("already_running", value)
        self.assertEqual(
            set(value["task_snapshot"]),
            {"state", "phase", "row_version", "current_run_id"},
        )

    def test_same_id_with_different_content_conflicts(self):
        req = self.req(expected_version=self.task["row_version"])
        self.launch(req)
        with self.assertRaises(Error) as ctx:
            self.launch({**req, "strategy": "workspace"})
        self.assertEqual(ctx.exception.code, "REQUEST_CONFLICT")

    def test_same_id_across_actions_conflicts(self):
        """``action`` goes into the hash, not the key (§7.3)."""
        req = self.req(expected_version=self.task["row_version"])
        self.s.operation(req, "start", self.task["task_id"])
        with self.assertRaises(Error) as ctx:
            self.launch(req, action="resume")
        self.assertEqual(ctx.exception.code, "REQUEST_CONFLICT")

    def test_a_different_request_with_a_held_writer_is_task_busy_with_details(self):
        req = self.req(expected_version=self.task["row_version"])
        platform = get_platform()
        held = platform.lock_exclusive(
            self.s.lock_path(f"tasks/{self.task['task_id']}/writer"), blocking=False
        )
        try:
            with self.assertRaises(Error) as ctx:
                self.launch(req)
        finally:
            platform.unlock(held)
        self.assertEqual(ctx.exception.code, "TASK_BUSY")
        details = ctx.exception.details or {}
        for key in ("task_id", "state", "phase", "run_id", "dispatch_state", "hint"):
            self.assertIn(key, details)
        self.assertEqual(details["task_id"], self.task["task_id"])

    def test_the_lock_failure_recheck_turns_the_race_into_a_replay(self):
        """Both probes must miss *and* the lock must fail for this to reproduce.

        That is exactly the window the design closes: a re-read after
        ``TASK_BUSY`` finds the intent the winner just wrote, so the loser
        replays instead of reporting a bogus conflict.
        """
        req = self.req(expected_version=self.task["row_version"])
        self.s.operation(req, "start", self.task["task_id"])
        real = Store.operation_prior
        calls = {"count": 0}

        def miss_first_once(store, request, action, task=None):
            calls["count"] += 1
            if calls["count"] == 1:
                return safe_id(request["request_id"]), None, None
            return real(store, request, action, task)

        platform = get_platform()
        held = platform.lock_exclusive(
            self.s.lock_path(f"tasks/{self.task['task_id']}/writer"), blocking=False
        )
        spawn, probe = patched_dispatch()
        try:
            with mock.patch.object(Store, "operation_prior", miss_first_once), spawn, probe:
                value = start(self.s, self.task["task_id"], req)
        finally:
            platform.unlock(held)
        self.assertEqual(calls["count"], 2, "pre-check plus post-lock re-check")
        self.assertTrue(value["dispatch_pending"])

    def test_an_abandoned_intent_is_closed_out_by_reconcile(self):
        req = self.req(expected_version=self.task["row_version"])
        key, _ = self.s.operation(req, "start", self.task["task_id"])
        self.assertTrue(self.launch(req)["dispatch_pending"], "not aged out yet")

        row = self.s.db.execute("SELECT data FROM operations WHERE id=?", (key,)).fetchone()
        data = json.loads(row[0])
        data["created_at"] = data["created_at"] - INTENT_GRACE_SECONDS - 1
        self.s.db.execute("UPDATE operations SET data=? WHERE id=?", (json.dumps(data), key))

        reconcile(self.s, self.s.task(self.task["task_id"]))

        value = self.launch(req)
        self.assertFalse(value.get("dispatch_pending", False))
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["error"]["code"], "PROCESS_UNKNOWN")
        self.assertEqual(len(self.s.runs(self.task["task_id"])), 0, "never re-spawned")

    def test_the_grace_window_is_above_the_supervisor_handshake(self):
        self.assertGreater(INTENT_GRACE_SECONDS, SUPERVISOR_LOCK_WAIT)


class TestCrossProcessRace(RuntimeFixture):
    """Two real processes, one synchronisation barrier (§7.7)."""

    def run_pair(self, jobs):
        home = self.s.home
        self.s.db.close()
        try:
            with tempfile.TemporaryDirectory() as barrier:
                job_file = Path(barrier) / "jobs.json"
                job_file.write_text(json.dumps(jobs))
                script = Path(barrier) / "driver.py"
                script.write_text(DRIVER)
                procs = []
                for me, other in (("a", "b"), ("b", "a")):
                    procs.append(
                        subprocess.Popen(
                            [
                                sys.executable,
                                str(script),
                                str(SRC),
                                str(home),
                                barrier,
                                me,
                                other,
                                str(job_file),
                            ],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            env={**os.environ, "PYTHONPATH": str(SRC)},
                        )
                    )
                out = []
                for proc in procs:
                    stdout, stderr = proc.communicate(timeout=120)
                    self.assertEqual(proc.returncode, 0, stderr)
                    out.append(json.loads(stdout.strip().splitlines()[-1]))
                return out
        finally:
            self.s = Store(home)

    def test_two_processes_replaying_one_request_never_see_task_busy(self):
        task = self.new()
        request = self.req(expected_version=task["row_version"])
        results = self.run_pair([{"task_id": task["task_id"], "request": request}] * 2)

        for result in results:
            self.assertNotEqual(result.get("code"), "TASK_BUSY", result)
        winners = [r for r in results if r["ok"] and not r["value"].get("dispatch_pending")]
        losers = [r for r in results if r["ok"] and r["value"].get("dispatch_pending")]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 1, results)
        self.assertIsNotNone(winners[0]["value"]["run_id"])
        self.assertIsNone(losers[0]["value"]["run_id"])
        self.assertEqual(len(self.s.runs(task["task_id"])), 1, "dispatched exactly once")

    def test_concurrent_admission_never_exceeds_the_limit(self):
        self.c["max_active_tasks"] = 1
        atomic(self.s.home / "projects/demo.json", self.c)
        first, second = self.new(), self.new()
        results = self.run_pair(
            [
                {
                    "task_id": first["task_id"],
                    "request": self.req(expected_version=first["row_version"]),
                },
                {
                    "task_id": second["task_id"],
                    "request": self.req(expected_version=second["row_version"]),
                },
            ]
        )

        accepted = [r for r in results if r["ok"]]
        refused = [r for r in results if not r["ok"]]
        self.assertEqual(len(accepted), 1, results)
        self.assertEqual(len(refused), 1, results)
        self.assertEqual(refused[0]["code"], "SLOT_BUSY")


if __name__ == "__main__":
    unittest.main()
