"""v1.3 task-group delivery: cross-cutting regression cases.

**These cases do not own a T-number.** The frozen matrix in
``docs/task-group-delivery-design.md`` §15 is carried by the per-package files:

``test_v130_prereq.py`` (P0) · ``test_v130_group_plan.py`` (P1) ·
``test_v130_legacy_groups.py`` (P1) · ``test_v130_group_member.py`` (P2) ·
``test_v130_runner.py`` (P3) · ``test_v130_integration.py`` (P4) ·
``test_v130_group_accept.py`` (P4).

This module covers the seams *between* packages, where a fault is neither one
package's bug nor the other's, and uses its own ``F-xx`` numbering so it can
never be mistaken for matrix coverage:

* F-01  invalid plans are refused **all-or-nothing** (cycle, coverage, path
        escape, unresolvable AC, missing budget),
* F-02  ``group plan --project`` writes nothing,
* F-03  ``plan_digest`` is a projection of the immutable definition only,
* F-04  a legacy ordered record cannot be started,
* F-05  a tampered frozen spec is refused with ``SPEC_CHANGED``,
* F-06  the dispatch gate is ``min(group_free, global_free)``,
* F-07  an exhausted budget stops dispatch but not reconcile,
* F-08  a ref that moved without a ``prepared`` record is ``INTEGRATION_UNKNOWN``,
* F-09  ``task accept`` refuses a group member,
* F-10  a group member task never publishes on its own.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from src import _group_runner, _groups, _integration, _pipeline
from src._store import Store
from src._util import Error, atomic, git


class PlanCase(unittest.TestCase):
    """Pure validation: a synthetic snapshot, no filesystem, no git."""

    def member(self, key, **overrides):
        base = {
            "task_key": key,
            "title": key.upper(),
            "spec_sha256": "sha-" + key,
            "spec_document": {"schema_version": 2},
            "spec_contract_version": 2,
            "spec_allowed_paths": ["src"],
            "ac_ids": ["AC-1"],
            "ac_checks": ["unit"],
            "ac_manual": [],
            "depends_on": [],
            "requirement_ids": ["REQ-1"],
            "acceptance_ids": ["AC-1"],
            "expected_paths": ["src/" + key],
            "expected_paths_resolved": [
                {"path": "src/" + key, "resolved": "/repo/src/" + key, "within": True}
            ],
            "task_type": "feature",
            "requested_profile": None,
            "check_ids": ["unit"],
        }
        base.update(overrides)
        return base

    def snapshot(self, members=None, **overrides):
        values = {
            "request": {"title": "t", "schema_version": 2},
            "config": {},
            "project_id": "demo",
            "base_ref": "main",
            "base_sha": "0" * 40,
            "design_manifest": [{"path": "d.md", "sha256": "x"}],
            "design_digest": "design",
            "requirements": [{"id": "REQ-1", "statement": "do it", "source": None}],
            "members": members if members is not None else [self.member("a")],
            "group_checks": [{"check_id": "unit", "command_digest": "sha-unit", "quick": True}],
            "budget": {"max_starts": 3, "max_wall_seconds": None},
            "delivery_mode": "local_artifact",
            "allowed_paths": ["src"],
            "env": {},
        }
        values.update(overrides)
        return values

    def test_a_valid_plan_produces_a_stable_digest(self):
        plan = _groups.validate_plan(self.snapshot())
        self.assertEqual(plan["order"], ["a"])
        self.assertEqual(len(plan["plan_digest"]), 64)
        self.assertEqual(plan["plan_digest"], _groups.validate_plan(self.snapshot())["plan_digest"])

    def test_the_digest_ignores_runtime_facts(self):
        """T02: counting a start or integrating a member must not change it."""
        first = _groups.validate_plan(self.snapshot())
        mutated = self.snapshot()
        mutated["env"] = {"runtime_version": "9.9.9"}
        mutated["members"][0]["task_id"] = "task-abc"
        mutated["members"][0]["validated_sha"] = "f" * 40
        second = _groups.validate_plan(mutated)
        self.assertEqual(first["plan_digest"], second["plan_digest"])

    def test_a_cycle_is_refused(self):
        snapshot = self.snapshot(
            members=[
                self.member("a", depends_on=["b"]),
                self.member("b", depends_on=["a"]),
            ]
        )
        with self.assertRaises(Error) as ctx:
            _groups.validate_plan(snapshot)
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("cycle", str(ctx.exception))

    def test_an_uncovered_requirement_is_named(self):
        snapshot = self.snapshot(
            requirements=[
                {"id": "REQ-1", "statement": "a"},
                {"id": "REQ-2", "statement": "b"},
            ]
        )
        with self.assertRaises(Error) as ctx:
            _groups.validate_plan(snapshot)
        self.assertEqual(ctx.exception.details["kind"], "coverage_missing")
        self.assertEqual(ctx.exception.details["uncovered_requirements"], ["REQ-2"])

    def test_expected_paths_must_stay_inside_the_policy_boundary(self):
        snapshot = self.snapshot(
            members=[
                self.member(
                    "a",
                    expected_paths_resolved=[
                        {"path": "deploy/x", "resolved": "/repo/deploy/x", "within": False}
                    ],
                )
            ]
        )
        with self.assertRaises(Error) as ctx:
            _groups.validate_plan(snapshot)
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")

    def test_an_acceptance_check_must_resolve_to_a_declared_check(self):
        snapshot = self.snapshot(
            members=[self.member("a", ac_checks=["not-declared"], check_ids=["unit"])]
        )
        with self.assertRaises(Error) as ctx:
            _groups.validate_plan(snapshot)
        self.assertIn("not-declared", str(ctx.exception))

    def test_every_acceptance_criterion_must_be_mapped(self):
        snapshot = self.snapshot(
            members=[self.member("a", ac_ids=["AC-1", "AC-2"], acceptance_ids=["AC-1"])]
        )
        with self.assertRaises(Error) as ctx:
            _groups.validate_plan(snapshot)
        self.assertIn("AC-2", str(ctx.exception))

    def test_max_starts_is_required_and_never_derived(self):
        snapshot = self.snapshot(budget={"max_starts": None, "max_wall_seconds": None})
        with self.assertRaises(Error) as ctx:
            _groups.validate_plan(snapshot)
        self.assertIn("max_starts", str(ctx.exception))

    def test_a_spec_may_only_tighten_the_project_policy(self):
        snapshot = self.snapshot(members=[self.member("a", spec_allowed_paths=["src", "deploy"])])
        with self.assertRaises(Error) as ctx:
            _groups.validate_plan(snapshot)
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")

    def test_overlapping_expected_paths_is_a_hint_not_an_edge(self):
        snapshot = self.snapshot(members=[self.member("a"), self.member("b")])
        plan = _groups.validate_plan(snapshot)
        self.assertEqual(plan["definition"]["members"][1]["depends_on"], [])
        self.assertTrue(any("hint" in warning for warning in plan["warnings"]))


class Fixture(unittest.TestCase):
    """A real repo, a real store, and a frozen two-member group."""

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
        self.write_spec("a", depends_on_none=True)
        self.write_spec("b", depends_on_none=True)
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

    def write_spec(self, key, depends_on_none=True, mutate=None):
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
        if mutate:
            mutate(document)
        path = self.repo / "specs" / (key + ".json")
        path.write_text(json.dumps(document))
        return path

    def plan(self, **overrides):
        request = {
            "schema_version": 2,
            "request_id": "req-" + uuid.uuid4().hex,
            "project_id": "demo",
            "title": "Two members",
            "design_files": ["docs/design.md"],
            "base_ref": "main",
            "delivery_mode": "local_artifact",
            "requirements": [
                {"id": "REQ-1", "statement": "a is delivered"},
                {"id": "REQ-2", "statement": "b is delivered"},
            ],
            "members": [
                {
                    "task_key": "a",
                    "title": "A",
                    "spec_file": "specs/a.json",
                    "depends_on": [],
                    "requirement_ids": ["REQ-1"],
                    "acceptance_ids": ["AC-1"],
                    "expected_paths": ["src/a.txt"],
                    "check_ids": ["unit"],
                },
                {
                    "task_key": "b",
                    "title": "B",
                    "spec_file": "specs/b.json",
                    "depends_on": ["a"],
                    "requirement_ids": ["REQ-2"],
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

    def created(self, **overrides):
        return _groups.create(self.store, self.plan(**overrides))

    def group(self, gid):
        return self.store.group(gid)

    def operation(self, action="group_start"):
        return {
            "schema_version": 2,
            "request_id": "req-" + uuid.uuid4().hex,
            "authorization_ref": "scope-1",
            "expected_version": 1,
            "action": action,
        }

    # -- driving a real member through the pipeline ------------------------ #
    # Shared with ``test_v130_group_accept.py``: acceptance needs a genuinely
    # REVIEW_READY group, and a second copy of this dance would drift.

    def fake_publisher(self):
        from test_runtime import FakePublisher

        return FakePublisher()

    def member_task(self, gid, task_key="a"):
        group = self.group(gid)
        snapshot = _groups.group_dir(self.store, gid) / "specs" / f"{task_key}.json"
        created = _pipeline.create(
            self.store,
            {
                "schema_version": 2,
                "request_id": "req-" + uuid.uuid4().hex,
                "authorization_ref": "scope-1",
                "project_id": "demo",
                "title": task_key.upper(),
                "spec_file": str(snapshot),
                "delivery_mode": "group_member",
                "group_id": gid,
                "item_id": task_key,
            },
        )
        task = self.store.task(created["task_id"])
        self.assertEqual(task["state"], "READY")
        self.assertEqual(task["delivery_mode"], "group_member")
        # A member consumes the group's *current* integration result: the first
        # one starts from the base, every later one from its predecessor's merge.
        self.assertEqual(
            task["consumed_input_sha"],
            group["runtime"].get("integration_sha") or group["base_sha"],
        )
        # Binding is persisted in the same transaction as the task.
        bound = _groups.member_runtime(self.group(gid), task_key)
        self.assertEqual(bound["task_id"], task["task_id"])
        return task

    def drive(self, task, body="implemented\n"):
        """Run the member to REVIEW_READY with a backend that writes its own file.

        Each member writes ``src/<item_id>.txt``: a chain of three members that
        all rewrote ``src/a.txt`` would conflict during integration, which would
        be a property of the fixture rather than of the code under test.
        """
        key = task.get("item_id") or "a"

        class Backend:
            def inspect(self, name):
                return None

            def execute(self, store, t, r, argv, log_name, worker=False, prompt=None):
                if worker:
                    target = Path(t["workspace"]) / "src" / f"{key}.txt"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(body)
                return 0

        rid = "run-" + uuid.uuid4().hex[:16]
        self.store.run(
            {"run_id": rid, "task_id": task["task_id"], "status": "running", "started_at": 0.0}
        )
        task.update(current_run_id=rid, state="RUNNING")
        self.store.save(task)
        return _pipeline.run_pipeline(self.store, task["task_id"], Backend(), self.fake_publisher())


class TestPlanFreezing(Fixture):
    def test_preview_writes_nothing(self):
        """T05: `group plan --project` is a decision, not a record."""
        before = (
            sorted(path.name for path in (self.store.home / "groups").glob("*"))
            if (self.store.home / "groups").exists()
            else []
        )
        result = _groups.preview(self.store, self.plan())
        self.assertEqual(result["status"], "preview")
        self.assertFalse(result["wrote"])
        self.assertEqual(result["members"], ["a", "b"])
        after = (
            sorted(path.name for path in (self.store.home / "groups").glob("*"))
            if (self.store.home / "groups").exists()
            else []
        )
        self.assertEqual(before, after)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM groups").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)

    def test_create_freezes_plan_specs_and_baseline(self):
        result = self.created()
        gid = result["group_id"]
        directory = _groups.group_dir(self.store, gid)
        plan = json.loads((directory / "plan-r001.json").read_text())
        self.assertEqual(plan["plan_digest"], result["plan_digest"])
        self.assertEqual(plan["base_sha"], result["base_sha"])
        self.assertTrue((directory / "baseline.json").is_file())
        self.assertTrue((directory / "design-manifest.json").is_file())
        for key in ("a", "b"):
            snapshot = json.loads((directory / "specs" / (key + ".json")).read_text())
            self.assertIn("text", snapshot)
            self.assertIn("verification_semantics", snapshot)
        group = self.group(gid)
        self.assertEqual(group["record_version"], _groups.RECORD_VERSION)
        self.assertEqual(group["state"], "READY")
        self.assertEqual(group["delivery_mode"], "local_artifact")
        # Nothing was dispatched: `create` freezes, it does not start.
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)

    def test_pull_request_groups_are_refused_at_creation(self):
        with self.assertRaises(Error) as ctx:
            self.created(delivery_mode="pull_request")
        self.assertEqual(ctx.exception.code, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(ctx.exception.details["kind"], "group_pr_unsupported")

    def test_a_design_file_escape_is_refused(self):
        with self.assertRaises(Error) as ctx:
            self.created(design_files=["../escape.md"])
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")

    def test_a_missing_requirement_coverage_is_refused_before_writing(self):
        with self.assertRaises(Error) as ctx:
            self.created(
                requirements=[
                    {"id": "REQ-1", "statement": "a"},
                    {"id": "REQ-2", "statement": "b"},
                    {"id": "REQ-9", "statement": "z"},
                ]
            )
        self.assertEqual(ctx.exception.details["kind"], "coverage_missing")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM groups").fetchone()[0], 0)


class TestMemberLifecycle(Fixture):
    def test_legacy_records_cannot_be_started(self):
        """T19: a v2 ordered record keeps reporting, never advancing."""
        legacy = _groups.create(
            self.store,
            {
                "schema_version": 2,
                "request_id": "req-" + uuid.uuid4().hex,
                "project_id": "demo",
                "title": "legacy",
                "authorization_ref": "scope-1",
                "ordered_items": [{"item_id": "i1", "title": "first", "spec_ref": "specs/a.json"}],
            },
        )
        self.assertEqual(legacy["record_version"], _groups.LEGACY_RECORD_VERSION)
        with self.assertRaises(Error) as ctx:
            _group_runner.begin(self.store, legacy["group_id"], self.operation())
        self.assertEqual(ctx.exception.code, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(ctx.exception.details["kind"], "legacy_group")

    def test_a_legacy_record_cannot_grow_a_dependency_graph(self):
        with self.assertRaises(Error) as ctx:
            _groups.create(
                self.store,
                {
                    "schema_version": 2,
                    "request_id": "req-" + uuid.uuid4().hex,
                    "project_id": "demo",
                    "authorization_ref": "scope-1",
                    "ordered_items": [
                        {
                            "item_id": "i1",
                            "title": "first",
                            "spec_ref": "s",
                            "depends_on": ["i0"],
                        }
                    ],
                },
            )
        self.assertEqual(ctx.exception.code, "CAPABILITY_UNSUPPORTED")

    def test_a_tampered_frozen_spec_is_refused(self):
        """T20: the snapshot is the authority, not the repo file."""
        gid = self.created()["group_id"]
        snapshot = _groups.group_dir(self.store, gid) / "specs" / "a.json"
        payload = json.loads(snapshot.read_text())
        payload["text"] = payload["text"].replace("works", "works differently")
        snapshot.write_text(json.dumps(payload))
        group = self.group(gid)
        with self.assertRaises(Error) as ctx:
            _group_runner.dispatch(self.store, group, "a")
        self.assertEqual(ctx.exception.code, "SPEC_CHANGED")

    def test_members_wait_for_their_dependencies(self):
        """T22: a member with an unintegrated dependency is READY only after it."""
        gid = self.created()["group_id"]
        group = self.group(gid)
        states = {
            view["task_key"]: view["state"] for view in _groups.members_view(self.store, group)
        }
        self.assertEqual(states, {"a": "READY", "b": "WAITING"})

    def test_dispatch_refuses_a_member_whose_dependency_is_not_integrated(self):
        """T22 的另一半：``group dispatch`` 不得绕过依赖图。

        tick 按拓扑序迭代，但"顺序"不是"约束"：没有这道闸门时，宿主可以直接
        派发 B（depends_on=[A]），B 的产物随后被合并进不含 A 的 integration_sha。
        """
        gid = self.created()["group_id"]
        with self.assertRaises(Error) as ctx:
            _group_runner.dispatch(self.store, self.group(gid), "b")
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")
        self.assertEqual(ctx.exception.details["kind"], "dependencies_unmet")
        self.assertEqual(ctx.exception.details["depends_on"], ["a"])
        # 拒绝发生在 create 之前：不留"已绑定、从未启动"的半写入成员。
        self.assertIsNone(_groups.member_runtime(self.store.group(gid), "b").get("task_id"))

    def test_an_integrated_member_is_frozen(self):
        """T21: revise/reassign/rebind are refused once the product landed."""
        gid = self.created()["group_id"]
        group = self.group(gid)
        _groups.member_runtime(group, "a")["integrated_sha"] = "a" * 40
        self.store.save_group(group, "test_marker")
        task = {"group_id": gid, "item_id": "a"}
        for action in ("revise", "reassign", "rebind"):
            with self.assertRaises(Error) as ctx:
                _pipeline._require_member_mutable(self.store, task, action)
            self.assertEqual(ctx.exception.details["kind"], "integrated_member")

    def test_task_accept_refuses_a_group_member(self):
        """T07: one acceptance authority per delivery."""
        gid = self.created()["group_id"]
        fake = {
            "task_id": "task-x",
            "project_id": "demo",
            "delivery_mode": "group_member",
            "group_id": gid,
            "state": "REVIEW_READY",
            "row_version": 3,
            "config": self.store.config("demo"),
        }
        with mock.patch.object(self.store, "task", return_value=fake):
            with self.assertRaises(Error) as ctx:
                _pipeline.accept(
                    self.store,
                    "task-x",
                    {
                        "schema_version": 2,
                        "request_id": "req-" + uuid.uuid4().hex,
                        "expected_version": 3,
                        "decision": "accepted",
                        "actor": "human",
                        "authorization_ref": "scope-1",
                        "evidence": [{"ref": "x"}],
                    },
                )
        self.assertEqual(ctx.exception.details["kind"], "group_member")

    def test_a_non_member_task_cannot_bind_to_a_group(self):
        """绑定只发生在 create，且必须是 ``group_member``。

        否则 `local_artifact` + `group_id` 会绕过冻结 spec 校验绑成成员，而
        `task accept` 的自验收拦截按 `delivery_mode` 判断，拦不住它。
        """
        gid = self.created()["group_id"]
        with self.assertRaises(Error) as ctx:
            _pipeline.create(
                self.store,
                {
                    "schema_version": 1,
                    "request_id": "req-" + uuid.uuid4().hex,
                    "authorization_ref": "scope-1",
                    "project_id": "demo",
                    "title": "A",
                    "spec_file": str(self.repo / "specs" / "a.json"),
                    "delivery_mode": "local_artifact",
                    "group_id": gid,
                    "item_id": "a",
                },
            )
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("delivery_mode=group_member", str(ctx.exception))
        # 拒绝必须发生在绑定之前：成员记录仍未被占用。
        self.assertIsNone(_groups.member_runtime(self.group(gid), "a").get("task_id"))


class TestRunnerGates(Fixture):
    def test_the_gate_is_min_group_and_global(self):
        """T25: a free global slot cannot start a second member."""
        gid = self.created()["group_id"]
        group = self.group(gid)
        self.assertEqual(_group_runner.slots_available(self.store, group), 1)
        _groups.member_runtime(group, "a")["task_id"] = "task-busy"
        group["runtime"]["members"][0]["task_id"] = "task-busy"
        with mock.patch.object(_group_runner, "_is_reserved", return_value=True):
            self.assertEqual(_group_runner.slots_available(self.store, group), 0)
            gate = _group_runner.dispatch_gate(self.store, group)
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["slots"], 0)

    def test_budget_exhaustion_blocks_dispatch_but_not_acceptance(self):
        """T26: reconcile/integrate/checks/accept keep working."""
        gid = self.created()["group_id"]
        group = self.group(gid)
        group["runtime"]["budget"]["starts_reserved"] = group["definition"]["budget_caps"][
            "max_starts"
        ]
        gate = _group_runner.dispatch_gate(self.store, group)
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["blocked"]["code"], "GROUP_BUDGET_EXHAUSTED")
        # The same record still reconciles without touching the budget.
        reconciled = _group_runner.reconcile(self.store, group)
        self.assertEqual(
            reconciled["runtime"]["budget"]["starts_reserved"],
            group["definition"]["budget_caps"]["max_starts"],
        )

    def test_admission_refuses_a_paused_group(self):
        gid = self.created()["group_id"]
        group = self.group(gid)
        group["state"] = "PAUSED"
        self.store.save_group(group, "test_marker")
        with self.assertRaises(Error) as ctx:
            _group_runner.admission(self.store, {"group_id": gid, "item_id": "a", "task_id": None})
        self.assertEqual(ctx.exception.details["kind"], "group_state")

    def test_admission_refuses_an_integrated_member(self):
        gid = self.created()["group_id"]
        group = self.group(gid)
        group["state"] = "RUNNING"
        _groups.member_runtime(group, "a")["integrated_sha"] = "a" * 40
        self.store.save_group(group, "test_marker")
        with self.assertRaises(Error) as ctx:
            _group_runner.admission(self.store, {"group_id": gid, "item_id": "a", "task_id": None})
        self.assertEqual(ctx.exception.details["kind"], "integrated_member")

    def test_pause_drains_and_stop_is_reported_honestly(self):
        gid = self.created()["group_id"]
        group = self.group(gid)
        group["state"] = "RUNNING"
        self.store.save_group(group, "test_marker")
        result = _group_runner.pause(
            self.store,
            gid,
            {**self.operation("group_pause"), "expected_version": group["row_version"]},
        )
        self.assertEqual(result["state"], "PAUSED")
        self.assertFalse(result["draining"])
        self.assertIn("not proof", result["note"])


class TestIntegrationLedger(Fixture):
    def _ledger(self, gid, **overrides):
        value = {
            "op_id": "iop-" + uuid.uuid4().hex[:16],
            "group_id": gid,
            "member": "a",
            "seq": 1,
            "status": "intent",
            "base_sha": self.group(gid)["base_sha"],
            "candidate_sha": "b" * 40,
            "integration_branch": self.group(gid)["definition"]["integration_branch"],
            "created_at": 0.0,
            "result_sha": None,
            "worktree": None,
            "conflict_dir": None,
            "failures": [],
        }
        value.update(overrides)
        return value

    def test_a_ref_that_moved_without_a_prepared_record_is_unknown(self):
        """T24: 'probably merged' is not a record."""
        gid = self.created()["group_id"]
        group = self.group(gid)
        branch = group["definition"]["integration_branch"]
        _integration.ensure_branch(self.store, group)
        # Move the branch behind the controller's back, then claim an intent.
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        moved = subprocess.run(
            ["git", "commit-tree", tree, "-p", group["base_sha"], "-m", "moved"],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        ).stdout.strip()
        git(self.repo, "update-ref", "refs/heads/" + branch, moved)
        ledger = self._ledger(gid)
        _integration._write_ledger(self.store, gid, ledger)
        group["runtime"]["open_operations"] = [ledger["op_id"]]
        self.store.save_group(group, "test_marker")
        with self.assertRaises(Error) as ctx:
            _integration.reconcile(self.store, self.group(gid))
        self.assertEqual(ctx.exception.code, "INTEGRATION_UNKNOWN")
        self.assertEqual(ctx.exception.details["kind"], "intent_ref_moved")

    def test_a_prepared_record_completes_after_the_ref_moved(self):
        gid = self.created()["group_id"]
        group = self.group(gid)
        branch = group["definition"]["integration_branch"]
        _integration.ensure_branch(self.store, group)
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        result_sha = subprocess.run(
            ["git", "commit-tree", tree, "-p", group["base_sha"], "-m", "integrated"],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        ).stdout.strip()
        git(self.repo, "update-ref", "refs/heads/" + branch, result_sha)
        ledger = self._ledger(gid, status="prepared", result_sha=result_sha)
        _integration._write_ledger(self.store, gid, ledger)
        group["runtime"]["open_operations"] = [ledger["op_id"]]
        self.store.save_group(group, "test_marker")
        reconciled = _integration.reconcile(self.store, self.group(gid))
        self.assertEqual(reconciled["runtime"]["integration_sha"], result_sha)
        self.assertEqual(_groups.member_runtime(reconciled, "a")["integrated_sha"], result_sha)
        self.assertEqual(reconciled["runtime"]["open_operations"], [])

    def test_integration_worktree_is_reported_not_deleted(self):
        gid = self.created()["group_id"]
        group = self.group(gid)
        worktree = _groups.group_dir(self.store, gid) / "integration" / "iop-x" / "wt"
        worktree.mkdir(parents=True)
        group["runtime"]["integration_worktree"] = str(worktree)
        notes = _integration.cleanup_notes(group)
        self.assertTrue(any(str(worktree) in note for note in notes))


class TestAcceptanceGates(Fixture):
    def test_acceptance_lists_every_unmet_condition(self):
        """T30: no check record for the current SHA means no acceptance."""
        gid = self.created()["group_id"]
        group = self.group(gid)
        unmet = _integration._acceptance_gates(
            self.store, group, {"actor": "human", "authorization_ref": "scope-1"}
        )
        text = "; ".join(unmet)
        self.assertIn("REVIEW_READY", text)
        self.assertIn("check results are stale or incomplete", text)
        self.assertIn("evidence_refs", text)

    def test_group_checks_are_bound_to_the_subject_sha(self):
        gid = self.created()["group_id"]
        group = self.group(gid)
        self.assertFalse(_integration.checks_are_current(self.store, group))
        group["runtime"]["integration_sha"] = "c" * 40
        self.assertFalse(_integration.checks_are_current(self.store, group))

    def test_report_renders_the_seven_sections_and_the_boundary(self):
        gid = self.created()["group_id"]
        snapshot = _integration.report(self.store, gid)
        text = _integration.render_report(snapshot)
        for heading in (
            "需求覆盖",
            "成员与执行组合",
            "SHA 链",
            "组级检查明细",
            "阻塞历史",
            "预算与时间",
            "交付边界声明",
        ):
            self.assertIn(heading, text)
        self.assertIn("NOT_RUN", text)
        self.assertIn("无沙箱", text)
        self.assertIn("结构覆盖", text)

    def test_status_derives_states_and_counts_without_writing(self):
        gid = self.created()["group_id"]
        before = self.group(gid)["row_version"]
        status = _groups.status(self.store, gid)
        self.assertEqual(status["state"], "READY")
        self.assertEqual(status["progress"]["members_total"], 2)
        self.assertEqual(status["progress"]["ready"], 1)
        self.assertEqual(status["progress"]["waiting"], 1)
        self.assertEqual(self.group(gid)["row_version"], before)
        self.assertIn("structural coverage", status["note"])


class TestEndToEndDelivery(Fixture):
    """One member, one integration, one acceptance — the whole contract.

    A real pipeline run is driven with a fake backend (the same technique as
    ``test_v051_report``): the member task reaches ``REVIEW_READY``, the runner
    integrates it into ``zoe/group-<id>``, the group check runs on that SHA, and
    ``group accept`` closes the group.
    """

    def test_the_group_delivers_through_integration_and_one_acceptance(self):
        gid = self.created(
            members=[
                {
                    "task_key": "a",
                    "title": "A",
                    "spec_file": "specs/a.json",
                    "depends_on": [],
                    "requirement_ids": ["REQ-1"],
                    "acceptance_ids": ["AC-1"],
                    "expected_paths": ["src/a.txt"],
                    "check_ids": ["unit"],
                }
            ],
            requirements=[{"id": "REQ-1", "statement": "a is delivered"}],
        )["group_id"]

        task = self.drive(self.member_task(gid))
        self.assertEqual(task["state"], "REVIEW_READY")
        self.assertEqual(task["delivery_mode"], "group_member")

        # The group is still READY: nothing advances without a runner.
        self.assertEqual(self.group(gid)["state"], "READY")

        group = self.group(gid)
        group["state"] = "RUNNING"
        self.store.save_group(group, "test_marker")
        _group_runner.tick(self.store, gid)

        group = self.group(gid)
        member = _groups.member_runtime(group, "a")
        self.assertIsNotNone(member["integrated_sha"])
        self.assertEqual(group["runtime"]["integration_sha"], member["integrated_sha"])
        self.assertEqual(group["state"], "REVIEW_READY")

        # The check record is bound to the integration SHA.
        record = _integration._read_check_record(
            self.store, gid, group["runtime"]["integration_sha"]
        )
        self.assertIsNotNone(record)
        self.assertEqual([row["check_id"] for row in record["checks"]], ["integration"])
        self.assertEqual(record["checks"][0]["status"], "PASS")

        # Base branch is untouched: integration happens on its own branch.
        branch = group["definition"]["integration_branch"]
        head = git(self.repo, "rev-parse", "refs/heads/" + branch).strip()
        self.assertEqual(head, group["runtime"]["integration_sha"])
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/main").strip(), group["base_sha"])

        accepted = _integration.accept(
            self.store,
            gid,
            {
                "schema_version": 2,
                "request_id": "req-" + uuid.uuid4().hex,
                "expected_version": group["row_version"],
                "decision": "accepted",
                "actor": "human",
                "reason": "looks right",
                "authorization_ref": "scope-1",
                "evidence_refs": [
                    "group:" + group["runtime"]["integration_sha"],
                    "task:" + task["task_id"],
                ],
            },
        )
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["state"], "DONE")
        self.assertEqual(self.group(gid)["state"], "DONE")
        # The member task is closed by the group, not by `task accept`.
        self.assertEqual(self.store.task(task["task_id"])["state"], "REVIEW_READY")

    def test_acceptance_is_refused_without_a_task_evidence_ref(self):
        gid = self.created(
            members=[
                {
                    "task_key": "a",
                    "title": "A",
                    "spec_file": "specs/a.json",
                    "depends_on": [],
                    "requirement_ids": ["REQ-1"],
                    "acceptance_ids": ["AC-1"],
                    "expected_paths": ["src/a.txt"],
                    "check_ids": ["unit"],
                }
            ],
            requirements=[{"id": "REQ-1", "statement": "a is delivered"}],
        )["group_id"]
        self.drive(self.member_task(gid))
        group = self.group(gid)
        group["state"] = "RUNNING"
        self.store.save_group(group, "test_marker")
        _group_runner.tick(self.store, gid)
        group = self.group(gid)
        with self.assertRaises(Error) as ctx:
            _integration.accept(
                self.store,
                gid,
                {
                    "schema_version": 2,
                    "request_id": "req-" + uuid.uuid4().hex,
                    "expected_version": group["row_version"],
                    "decision": "accepted",
                    "actor": "human",
                    "authorization_ref": "scope-1",
                    "evidence_refs": ["run:run-unknown"],
                },
            )
        self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID")
        self.assertIn("unknown reference form", str(ctx.exception))
        self.assertEqual(self.group(gid)["state"], "REVIEW_READY")


class TestSchemaFour(unittest.TestCase):
    """T11: schema v4 is a semantics gate, and it refuses what it cannot read."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.home = Path(self._td.name) / "state"
        self.home.mkdir(parents=True, exist_ok=True)

    def legacy_db(self, record):
        import sqlite3

        from src._store import _TABLES

        db = sqlite3.connect(self.home / "state.db")
        db.executescript(_TABLES + "\nPRAGMA user_version=3;")
        db.execute(
            "INSERT INTO groups VALUES(?,?,?)",
            (record["group_id"], 1, json.dumps(record)),
        )
        db.commit()
        db.close()

    def test_a_v3_database_must_be_migrated(self):
        from src import _migrations
        from src._store import SCHEMA_VERSION

        self.legacy_db(
            {
                "record_version": 2,
                "group_id": "group-legacy",
                "project_id": "demo",
                "title": "legacy",
                "ordered_items": [],
                "created_at": 0.0,
            }
        )
        with self.assertRaises(Error) as ctx:
            Store(self.home)
        self.assertEqual(ctx.exception.code, "MIGRATION_REQUIRED")
        self.assertIn(f"--to {SCHEMA_VERSION}", str(ctx.exception))

        result = _migrations.migrate(self.home, SCHEMA_VERSION)
        self.assertTrue(result["changed"])
        store = Store(self.home)
        self.addCleanup(store.db.close)
        # A legacy record is kept readable, never rewritten into a v3 plan.
        self.assertEqual(store.group("group-legacy")["record_version"], 2)

    def test_an_unreadable_group_record_stops_the_migration(self):
        from src import _migrations
        from src._store import SCHEMA_VERSION

        self.legacy_db({"record_version": 9, "group_id": "group-future"})
        with self.assertRaises(Error) as ctx:
            _migrations.migrate(self.home, SCHEMA_VERSION)
        self.assertEqual(ctx.exception.code, "SCHEMA_UNSUPPORTED")
        self.assertIn("record_version=9", str(ctx.exception))

    def test_open_group_has_no_task_counting_decisions(self):
        """A group record never counts as an active task or a run."""
        store = Store(self.home)
        self.addCleanup(store.db.close)
        self.assertEqual(store.tasks(), [])


if __name__ == "__main__":
    unittest.main()
