"""v1.3 group runner: the chain, the lease, the slot, the budget, the pause.

Cases from ``docs/task-group-delivery-design.md`` §15:

* **T01** a three-member dependency chain advances through one integration
  history: every member ends on the *same* ``integration_sha``;
* **T25** the gate is the *group* slot ∩ the global slot: a member still
  executing keeps the next one from starting, even when the global slot is free;
* **T26** an exactly exhausted budget still allows integration, group checks and
  acceptance — the budget gates new starts, never the closing steps;
* **T19** pause reports `draining` and the active run ids instead of pretending
  the child processes are gone, and a draining group refuses new members;
* **T22** a group whose budget is spent is `BLOCKED/GROUP_BUDGET_EXHAUSTED`, not
  "silently idle";
* **T07** a second ``group start`` is refused while a runner is observable, and a
  gone runner is taken over instead of blocking the group forever;
* **T08** an unobservable runner keeps ``PROCESS_UNKNOWN`` instead of guessing.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from test_v130_group_accept import AcceptCase

from src import _group_runner, _groups, _integration
from src._store import Store
from src._util import Error, atomic, git


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.txt").write_text("base\n")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "design.md").write_text("# design\n")
        (self.repo / "specs").mkdir()
        for key in ("a", "b"):
            self.write_spec(key)
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "test")
        git(self.repo, "config", "user.email", "test@localhost")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.db.close)
        self.addCleanup(self._td.cleanup)
        atomic(self.store.home / "projects" / "demo.json", self.config())

    def config(self, **overrides):
        value = {
            "schema_version": 2,
            "project_id": "demo",
            "repo": str(self.repo),
            "backend": "local",
            "default_agent_profile": "p-cb",
            "agent_profiles": {
                "p-cb": {
                    "adapter": "codebuddy",
                    "model": "stub",
                    "backend": "codebuddy-cli",
                    "expected_version": "1.2.3",
                    "permission_profile": "isolated-edit",
                }
            },
            "checks": [
                {"id": "unit", "argv": ["true"]},
                {"id": "integration", "argv": ["true"]},
            ],
            "allowed_paths": ["src", "specs", "docs"],
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute", "publish", "accept"],
            "max_attempts": 3,
            "max_active_tasks": 1,
        }
        value.update(overrides)
        return value

    def write_spec(self, key):
        document = {
            "schema_version": 2,
            "title": "Member " + key,
            "open_questions": [],
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "statement": "works",
                    "verification": {"type": "check", "check_id": "unit"},
                }
            ],
            "allowed_paths": ["src", "specs"],
        }
        (self.repo / "specs" / (key + ".json")).write_text(json.dumps(document))

    def plan(self, **overrides):
        request = {
            "schema_version": 2,
            "request_id": "req-" + uuid.uuid4().hex,
            "project_id": "demo",
            "title": "Two independent members",
            "design_files": ["docs/design.md"],
            "base_ref": "main",
            "delivery_mode": "local_artifact",
            "requirements": [
                {"id": "REQ-A", "statement": "a is delivered"},
                {"id": "REQ-B", "statement": "b is delivered"},
            ],
            "members": [
                {
                    "task_key": "a",
                    "title": "A",
                    "spec_file": "specs/a.json",
                    "depends_on": [],
                    "requirement_ids": ["REQ-A"],
                    "acceptance_ids": ["AC-1"],
                    "expected_paths": ["src/a.txt"],
                    "check_ids": ["unit"],
                },
                {
                    "task_key": "b",
                    "title": "B",
                    "spec_file": "specs/b.json",
                    "depends_on": [],
                    "requirement_ids": ["REQ-B"],
                    "acceptance_ids": ["AC-1"],
                    "expected_paths": ["src/b.txt"],
                    "check_ids": ["unit"],
                },
            ],
            "group_checks": ["integration"],
            "budget": {"max_starts": 4, "max_wall_seconds": None},
            "authorization_ref": "scope-1",
        }
        request.update(overrides)
        return request

    def created(self, **overrides) -> str:
        return _groups.create(self.store, self.plan(**overrides))["group_id"]

    def executing_member(self, gid: str, task_key: str) -> dict:
        """A member whose dispatch intent has not been observed yet."""
        group = self.store.group(gid)
        task_id = "task-" + uuid.uuid4().hex[:16]
        run_id = "run-" + uuid.uuid4().hex[:16]
        task = {
            "task_id": task_id,
            "project_id": group["project_id"],
            "state": "RUNNING",
            "phase": "implement",
            "current_run_id": run_id,
            "group_id": gid,
            "item_id": task_key,
            "delivery_mode": "group_member",
            "row_version": 1,
        }
        with self.store.tx():
            self.store.db.execute("INSERT INTO tasks VALUES(?,?,?)", (task_id, 1, json.dumps(task)))
        self.store.run(
            {
                "run_id": run_id,
                "task_id": task_id,
                "status": "running",
                "dispatch_state": "spawn_observed",
                "started_at": 0.0,
            }
        )
        group = self.store.group(gid)
        _groups.member_runtime(group, task_key)["task_id"] = task_id
        self.store.save_group(group, "test_marker")
        return self.store.group(gid)


class TestSerialSlot(RunnerCase):
    def test_a_running_member_keeps_the_next_one_from_starting(self):
        """T25: `min(group_free, global_free)` — the group slot is the binding one."""
        gid = self.created()
        group = self.executing_member(gid, "a")
        self.assertEqual(_group_runner.slots_available(self.store, group), 0)
        gate = _group_runner.dispatch_gate(self.store, group)
        self.assertFalse(gate["ok"])
        self.assertTrue(gate.get("deferred"))
        # Not a blocker: waiting for a slot is normal, not a fault.
        self.assertIsNone(gate["blocked"])

    def test_a_finished_member_frees_the_group_slot(self):
        gid = self.created()
        self.executing_member(gid, "a")
        group = self.store.group(gid)
        task = self.store.task(_groups.member_runtime(group, "a")["task_id"])
        task.update(state="REVIEW_READY")
        self.store.save(task)
        group = self.store.group(gid)
        self.assertEqual(_group_runner.slots_available(self.store, group), 1)
        self.assertTrue(_group_runner.dispatch_gate(self.store, group)["ok"])

    def test_tick_asks_the_gate_before_creating_any_member_task(self):
        """T25 wiring: a closed gate means `tick` creates no member at all."""
        gid = self.created()
        group = self.store.group(gid)
        group["state"] = "RUNNING"
        self.store.save_group(group, "test_marker")
        closed = {"ok": False, "blocked": None, "slots": 0, "deferred": True}
        with mock.patch.object(_group_runner, "dispatch_gate", return_value=closed):
            _group_runner.tick(self.store, gid)
        group = self.store.group(gid)
        self.assertIsNone(_groups.member_runtime(group, "a")["task_id"])
        self.assertIsNone(_groups.member_runtime(group, "b")["task_id"])


class TestBudgetOnlyGatesStarts(RunnerCase):
    def test_an_exhausted_start_budget_is_a_named_blocker(self):
        """T22: spent budget is reported, not hidden behind an idle group."""
        gid = self.created()
        group = self.store.group(gid)
        group["runtime"]["budget"]["starts_reserved"] = 4
        group["state"] = "RUNNING"
        self.store.save_group(group, "test_marker")
        _group_runner.tick(self.store, gid)
        group = self.store.group(gid)
        self.assertEqual(group["state"], "BLOCKED")
        codes = [item["code"] for item in group["runtime"]["blockers"]]
        self.assertIn("GROUP_BUDGET_EXHAUSTED", codes)
        gate = _group_runner.dispatch_gate(self.store, group)
        self.assertEqual(gate["blocked"]["kind"], "starts")

    def test_a_released_start_is_not_billed(self):
        gid = self.created()
        group = self.store.group(gid)
        group["runtime"]["budget"].update(
            starts_reserved=4,
            starts_released=1,
            release_basis=[{"run_id": "run-x", "code": "SPAWN_FAILED"}],
        )
        self.assertEqual(_group_runner.starts_billable(group), 3)
        self.assertLess(_group_runner.starts_billable(group), 4)

    def test_a_release_refunds_exactly_one_start(self):
        """回归：释放不得回退 ``starts_reserved``。

        旧实现同时 ``reserved -= 1`` 与 ``released += 1``，而 billable 是两者之差，
        于是每次释放退还两个名额，``max_starts`` 可被系统性超发。
        """
        gid = self.created()
        group = self.store.group(gid)
        group["runtime"]["budget"]["starts_reserved"] = 2
        self.store.save_group(group, "test_marker")
        _group_runner._release_start(
            self.store, gid, "a", reason="spawn failed", code="SPAWN_FAILED"
        )
        after = self.store.group(gid)
        budget = after["runtime"]["budget"]
        self.assertEqual(_group_runner.starts_billable(after), 1)
        self.assertEqual(budget["starts_reserved"], 2)
        self.assertEqual(budget["starts_released"], 1)

    def test_the_wall_clock_cap_counts_sleep_and_waiting(self):
        gid = self.created()
        group = self.store.group(gid)
        group["runtime"]["budget"]["first_running_at"] = 1_000.0
        group["runtime"]["budget"]["finalized_at"] = 1_100.0
        group["definition"]["budget_caps"]["max_wall_seconds"] = 50
        self.assertEqual(_group_runner.wall_seconds(group), 100)
        gate = _group_runner.dispatch_gate(self.store, group)
        self.assertEqual(gate["blocked"]["kind"], "wall_seconds")


class TestPauseHonesty(RunnerCase):
    def test_pause_reports_draining_and_the_active_runs(self):
        """T19: a pause confirms *dispatch stopped*, not that children are gone."""
        gid = self.created()
        group = self.executing_member(gid, "a")
        group["state"] = "RUNNING"
        self.store.save_group(group, "test_marker")
        result = _group_runner.pause(
            self.store,
            gid,
            {
                "schema_version": 2,
                "request_id": "req-" + uuid.uuid4().hex,
                "expected_version": self.store.group(gid)["row_version"],
                "authorization_ref": "scope-1",
            },
        )
        self.assertTrue(result["draining"])
        self.assertEqual(len(result["active_run_ids"]), 1)
        self.assertEqual(result["state"], "RUNNING")
        self.assertIn("not proof", result["note"])

    def paused(self, gid: str) -> dict:
        self.executing_member(gid, "a")
        group = self.store.group(gid)
        group["state"] = "RUNNING"
        self.store.save_group(group, "test_marker")
        return _group_runner.pause(
            self.store,
            gid,
            {
                "schema_version": 2,
                "request_id": "req-" + uuid.uuid4().hex,
                "expected_version": self.store.group(gid)["row_version"],
                "authorization_ref": "scope-1",
            },
        )

    def finish_active_run(self, gid: str) -> None:
        group = self.store.group(gid)
        task = self.store.task(_groups.member_runtime(group, "a")["task_id"])
        task["state"] = "REVIEW_READY"
        task["current_run_id"] = None
        self.store.save(task)

    def test_a_draining_group_refuses_a_new_member_even_with_a_free_slot(self):
        """§7.4: `draining` is a gate, not a note in the response."""
        gid = self.created()
        self.paused(gid)
        self.finish_active_run(gid)
        group = self.store.group(gid)
        self.assertEqual(self.store.group(gid)["runtime"]["draining"], True)
        self.assertEqual(_group_runner.slots_available(self.store, group), 1)
        with self.assertRaises(Error) as ctx:
            _group_runner.admission(
                self.store, {"group_id": gid, "item_id": "b", "task_id": "task-other"}
            )
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")
        self.assertEqual(ctx.exception.details["kind"], "group_state")
        self.assertTrue(ctx.exception.details["draining"])

    def test_the_dispatch_gate_defers_instead_of_blocking_while_draining(self):
        gid = self.created()
        self.paused(gid)
        self.finish_active_run(gid)
        gate = _group_runner.dispatch_gate(self.store, self.store.group(gid))
        self.assertFalse(gate["ok"])
        self.assertTrue(gate["draining"])
        # A pause is a human decision, not a fault: no blocker is reported.
        self.assertIsNone(gate["blocked"])

    def test_a_drained_group_lands_on_paused_not_running(self):
        gid = self.created()
        self.paused(gid)
        group = self.store.group(gid)
        views = _groups.members_view(self.store, group)
        state, _ = _groups.derive_group_state(group, views)
        # The active member is still executing: the group is draining, not paused.
        self.assertEqual(state, "RUNNING")
        self.finish_active_run(gid)
        group = self.store.group(gid)
        views = _groups.members_view(self.store, group)
        state, _ = _groups.derive_group_state(group, views)
        self.assertEqual(state, "PAUSED")

    def test_resume_clears_the_drain_flag(self):
        gid = self.created()
        self.paused(gid)
        self.finish_active_run(gid)
        group = self.store.group(gid)
        group["state"] = "PAUSED"
        self.store.save_group(group, "test_marker")
        from src import _profiles

        with (
            mock.patch.object(_profiles, "require_runnable", return_value=None),
            mock.patch.object(_group_runner, "_preflight", return_value={"ok": True}),
            mock.patch.object(_group_runner, "begin", return_value={"lease_id": "l-new"}),
        ):
            _group_runner.resume(
                self.store,
                gid,
                {
                    "schema_version": 2,
                    "request_id": "req-" + uuid.uuid4().hex,
                    "expected_version": self.store.group(gid)["row_version"],
                    "authorization_ref": "scope-1",
                },
                spawn=lambda *a, **k: None,
            )
        self.assertFalse(self.store.group(gid)["runtime"]["draining"])


class TestThreeMemberChain(AcceptCase):
    """T01: one chain, one integration history, one acceptance."""

    def chain(self):
        self.write_spec("c")
        return self.created(
            members=[
                {
                    "task_key": "a",
                    "title": "A",
                    "spec_file": "specs/a.json",
                    "depends_on": [],
                    "requirement_ids": ["REQ-A"],
                    "acceptance_ids": ["AC-1"],
                    "expected_paths": ["src/a.txt"],
                    "check_ids": ["unit"],
                },
                {
                    "task_key": "b",
                    "title": "B",
                    "spec_file": "specs/b.json",
                    "depends_on": ["a"],
                    "requirement_ids": ["REQ-B"],
                    "acceptance_ids": ["AC-1"],
                    "expected_paths": ["src/b.txt"],
                    "check_ids": ["unit"],
                },
                {
                    "task_key": "c",
                    "title": "C",
                    "spec_file": "specs/c.json",
                    "depends_on": ["b"],
                    "requirement_ids": ["REQ-C"],
                    "acceptance_ids": ["AC-1"],
                    "expected_paths": ["src/c.txt"],
                    "check_ids": ["unit"],
                },
            ],
            requirements=[
                {"id": "REQ-A", "statement": "a"},
                {"id": "REQ-B", "statement": "b"},
                {"id": "REQ-C", "statement": "c"},
            ],
        )["group_id"]

    def tick(self, gid):
        group = self.store.group(gid)
        group["state"] = "RUNNING"
        self.store.save_group(group, "test_marker")
        # The tick would really spawn the next member; spawning a supervisor is
        # not what this test is about, so the dispatch step is observed, not run.
        with mock.patch.object(_group_runner, "dispatch") as dispatched:
            _group_runner.tick(self.store, gid)
        return dispatched

    def test_every_member_lands_on_the_same_integration_sha(self):
        gid = self.chain()
        for key in ("a", "b", "c"):
            task = self.drive(self.member_task(gid, key))
            self.assertEqual(task["state"], "REVIEW_READY")
            self.tick(gid)

        group = self.group(gid)
        self.assertEqual(group["state"], "REVIEW_READY")
        final = group["runtime"]["integration_sha"]
        members = group["runtime"]["members"]
        # §8.2: each member keeps the merge that carried it in, so the SHAs are a
        # chain that ends at `integration_sha` — not one repeated value.
        self.assertEqual(members[-1]["integrated_sha"], final)
        self.assertEqual([item["task_key"] for item in members], ["a", "b", "c"], "chain order")
        for item in members:
            self.assertTrue(
                _integration.is_ancestor(self.repo, item["integrated_sha"], final),
                f"{item['task_key']} is not contained in the final integration",
            )
        self.assertEqual(_groups.progress(self.store, group)["integrated"], 3)

    def test_a_later_member_waits_until_its_dependency_integrates(self):
        gid = self.chain()
        self.drive(self.member_task(gid, "a"))
        group = self.group(gid)
        before = {
            item["task_key"]: item["state"] for item in _groups.members_view(self.store, group)
        }
        self.assertEqual(before["b"], "WAITING")
        self.assertEqual(before["c"], "WAITING")
        self.tick(gid)
        group = self.group(gid)
        after = {
            item["task_key"]: item["state"] for item in _groups.members_view(self.store, group)
        }
        self.assertEqual(after["a"], "INTEGRATED")
        self.assertEqual(after["b"], "READY")
        self.assertEqual(after["c"], "WAITING")

    def test_the_chain_is_accepted_once_at_the_end(self):
        gid = self.chain()
        for key in ("a", "b", "c"):
            self.drive(self.member_task(gid, key))
            self.tick(gid)
        group = self.group(gid)
        accepted = _integration.accept(
            self.store,
            gid,
            {
                "schema_version": 2,
                "request_id": "req-" + uuid.uuid4().hex,
                "expected_version": group["row_version"],
                "decision": "accepted",
                "actor": "human",
                "reason": "chain reviewed",
                "authorization_ref": "scope-1",
                "evidence_refs": ["group:" + group["runtime"]["integration_sha"]]
                + ["task:" + item["task_id"] for item in group["runtime"]["members"]],
            },
        )
        self.assertTrue(accepted["accepted"])
        self.assertEqual(self.group(gid)["state"], "DONE")


class TestExhaustedBudgetStillCloses(AcceptCase):
    """T26: the last billed start must still be able to finish."""

    def test_the_last_start_can_be_integrated_checked_and_accepted(self):
        gid = self.created(
            members=[
                {
                    "task_key": "a",
                    "title": "A",
                    "spec_file": "specs/a.json",
                    "depends_on": [],
                    "requirement_ids": ["REQ-A"],
                    "acceptance_ids": ["AC-1"],
                    "expected_paths": ["src/a.txt"],
                    "check_ids": ["unit"],
                }
            ],
            requirements=[{"id": "REQ-A", "statement": "a"}],
            budget={"max_starts": 1, "max_wall_seconds": None},
        )["group_id"]
        self.drive(self.member_task(gid, "a"))
        group = self.group(gid)
        group["runtime"]["budget"]["starts_reserved"] = 1
        group["state"] = "RUNNING"
        self.store.save_group(group, "test_marker")
        self.assertEqual(_group_runner.starts_billable(group), 1)
        self.assertFalse(_group_runner.dispatch_gate(self.store, group)["ok"])

        _group_runner.tick(self.store, gid)

        group = self.group(gid)
        # Integration and the group check happened *after* the budget was spent.
        self.assertEqual(group["state"], "REVIEW_READY")
        self.assertIsNotNone(group["runtime"]["integration_sha"])
        self.assertTrue(_integration.checks_are_current(self.store, group))

        accepted = _integration.accept(
            self.store,
            gid,
            {
                "schema_version": 2,
                "request_id": "req-" + uuid.uuid4().hex,
                "expected_version": group["row_version"],
                "decision": "accepted",
                "actor": "human",
                "reason": "done",
                "authorization_ref": "scope-1",
                "evidence_refs": ["group:" + group["runtime"]["integration_sha"]],
            },
        )
        self.assertTrue(accepted["accepted"])
        self.assertEqual(self.group(gid)["state"], "DONE")


class TestRunnerTakeover(RunnerCase):
    """T07/T08: one runner per group, and "unknown" is not "gone"."""

    def start_request(self):
        return {
            "schema_version": 2,
            "request_id": "req-" + uuid.uuid4().hex,
            "authorization_ref": "scope-1",
        }

    def leased(self, gid, **lease):
        group = self.store.group(gid)
        group["state"] = "READY"
        group["runtime"]["lease"] = {
            "lease_id": "l-run",
            "state": "spawn_observed",
            "pid": None,
            "start_token": None,
            **lease,
        }
        self.store.save_group(group, "test_marker")
        return self.store.group(gid)

    def test_a_second_start_is_refused_while_the_runner_is_observable(self):
        gid = self.created()
        self.leased(gid, pid=os.getpid(), start_token=None)
        with (
            mock.patch("src._pipeline.require_supervisor_importable", return_value=None),
            mock.patch.object(_group_runner, "_identity_observable", return_value=True),
            self.assertRaises(Error) as ctx,
        ):
            _group_runner.begin(self.store, gid, self.start_request(), spawn=lambda *a, **k: None)
        self.assertEqual(ctx.exception.code, "SLOT_BUSY")
        self.assertEqual(ctx.exception.details["kind"], "group_runner")
        self.assertEqual(self.store.group(gid)["runtime"]["lease"]["lease_id"], "l-run")

    def test_an_unobservable_runner_keeps_process_unknown(self):
        gid = self.created()
        self.leased(gid, pid=424242, start_token="token-from-another-boot")
        with (
            mock.patch("src._pipeline.require_supervisor_importable", return_value=None),
            mock.patch.object(_group_runner, "_identity_observable", return_value=None),
        ):
            with self.assertRaises(Error) as ctx:
                _group_runner.begin(
                    self.store, gid, self.start_request(), spawn=lambda *a, **k: None
                )
        self.assertEqual(ctx.exception.code, "PROCESS_UNKNOWN")
        self.assertEqual(ctx.exception.details["kind"], "runner_unknown")
        self.assertEqual(self.store.group(gid)["runtime"]["lease"]["lease_id"], "l-run")

    def test_an_auth_failure_can_be_fixed_and_started_again(self):
        """T13: a login fix is not a policy change — retry, don't rebind."""
        gid = self.created()
        with (
            mock.patch("src._pipeline.require_supervisor_importable", return_value=None),
            mock.patch.object(
                _group_runner, "_preflight", side_effect=Error("AGENT_AUTH_REQUIRED", "no login")
            ),
            self.assertRaises(Error) as ctx,
        ):
            _group_runner.begin(self.store, gid, self.start_request(), spawn=lambda *a, **k: None)
        self.assertEqual(ctx.exception.code, "AGENT_AUTH_REQUIRED")
        # The failure left nothing behind: no lease, no RUNNING group to fake life.
        failed = self.store.group(gid)
        self.assertEqual(failed["state"], "READY")
        self.assertIsNone(failed["runtime"].get("lease"))

        with (
            mock.patch("src._pipeline.require_supervisor_importable", return_value=None),
            mock.patch.object(_group_runner, "_preflight", return_value={"ok": True}),
            mock.patch.object(_group_runner, "_default_spawn", return_value={"pid": 7}),
        ):
            result = _group_runner.begin(self.store, gid, self.start_request())
        self.assertTrue(result["accepted"])
        recovered = self.store.group(gid)
        self.assertEqual(recovered["state"], "RUNNING")
        self.assertEqual(recovered["runtime"]["lease"]["state"], "spawn_observed")

    def test_a_gone_runner_is_taken_over_instead_of_blocking_forever(self):
        gid = self.created()
        self.leased(gid, pid=424242, start_token=None)
        with (
            mock.patch("src._pipeline.require_supervisor_importable", return_value=None),
            mock.patch.object(_group_runner, "_identity_observable", return_value=False),
            mock.patch.object(_group_runner, "_preflight", return_value={"ok": True}),
            mock.patch.object(_group_runner, "_default_spawn", return_value={"pid": 7}),
        ):
            _group_runner.begin(self.store, gid, self.start_request())
        lease = self.store.group(gid)["runtime"]["lease"]
        self.assertNotEqual(lease["lease_id"], "l-run")
        self.assertIn("gone", lease["reason"])
