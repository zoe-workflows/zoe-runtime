"""v1.4 设计一致性审查与有界修复（TGD §11 / §12）。

用例按设计文档的编号命名，便于对账；这里覆盖的是"**没有证据不算通过**"这条
主线在新增两层的具体形态：

* T18  设计审查漏 AC / 伪证据引用 → 输出无效，整份不接受；
* §11.2 ``subject_sha`` / ``design_digest`` 绑定 → 换版本、换设计即作废；
* §11.3 计划要求审查时，没有有效结论不得进入 ``REVIEW_READY``，``group accept`` 拒绝；
* §11.3 ``NOT_VERIFIED`` 可以被记录，但必须被列出，且不会被写成"通过"；
* §12 修复必须绑定一条 ``FAIL`` finding、受 ``budget.max_group_repairs`` 约束，
  并且与成员走**同一条**派发/集成路径。
"""

from __future__ import annotations

import json
import unittest
import uuid

from test_v130_group_delivery import Fixture as GroupFixture

from src import _group_runner, _groups, _integration, _review
from src._util import Error, atomic

MEMBER_A = {
    "task_key": "a",
    "title": "A",
    "spec_file": "specs/a.json",
    "depends_on": [],
    "requirement_ids": ["REQ-1"],
    "acceptance_ids": ["AC-1"],
    "expected_paths": ["src/a.txt"],
    "check_ids": ["unit"],
}
REQUIREMENT = [{"id": "REQ-1", "statement": "a is delivered"}]


class ReviewFixture(GroupFixture):
    """一个成员、一条需求，可选地要求设计审查。"""

    def group_with(self, **overrides):
        values = {
            "members": [dict(MEMBER_A)],
            "requirements": list(REQUIREMENT),
        }
        values.update(overrides)
        return self.created(**values)["group_id"]

    def deliver(self, gid, task_key="a"):
        """把成员驱动到 REVIEW_READY 并集成，返回 (task, group)。"""
        task = self.drive(self.member_task(gid, task_key))
        group = self.group(gid)
        group["state"] = "RUNNING"
        self.store.save_group(group, "test_marker")
        _group_runner.tick(self.store, gid)
        return task, self.group(gid)

    def review_request(self, group, status="PASS", acceptance_id="AC-1", **overrides):
        document = {
            "schema_version": 1,
            "subject_sha": group["runtime"]["integration_sha"],
            "design_digest": group["design_digest"],
            "reviewer": {"kind": "agent", "independent_context": True},
            "items": [
                {
                    "acceptance_id": acceptance_id,
                    "status": status,
                    "finding": None if status == "PASS" else "see the diff",
                    "locations": ["src/a.txt"],
                    "evidence_refs": ["group:" + group["runtime"]["integration_sha"]],
                }
            ],
        }
        document.update(overrides)
        return {
            "schema_version": 2,
            "request_id": "req-" + uuid.uuid4().hex,
            "actor": "reviewer-bot",
            "authorization_ref": "scope-1",
            "review": document,
        }

    def submitted(self, gid, status="PASS", **overrides):
        group = self.group(gid)
        return _review.submit(
            self.store, gid, self.review_request(group, status=status, **overrides)
        )


class TestReviewContract(ReviewFixture):
    """T18：不合法输出整份不接受，并且留档说明为什么。"""

    def setUp(self):
        super().setUp()
        self.gid = self.group_with(review={"required": True})
        self.task, self.delivered = self.deliver(self.gid)

    def test_a_valid_review_reaches_review_ready(self):
        # 已经集成、检查已通过，但审查还没做：组不能停在 REVIEW_READY。
        self.assertIsNotNone(self.delivered["runtime"]["integration_sha"])
        self.assertEqual(self.delivered["state"], "RUNNING")
        state = _review.summary(self.store, self.delivered)["state"]
        self.assertEqual(state, _review.STATE_NOT_RUN)

        result = self.submitted(self.gid)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["review"]["verdict"], "PASS")
        self.assertEqual(self.group(self.gid)["state"], "REVIEW_READY")

    def test_a_missing_acceptance_id_invalidates_the_whole_review(self):
        """T18：漏一项不是"其余都过了"，是无效输出。"""
        group = self.group(self.gid)
        request = self.review_request(group)
        request["review"]["items"] = []
        with self.assertRaises(Error) as ctx:
            _review.submit(self.store, self.gid, request)
        self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID")
        self.assertEqual(ctx.exception.details["kind"], "review_invalid")
        # 留档为 invalid，但没有产生任何"通过"结论
        stored = self.group(self.gid)
        self.assertEqual(stored["runtime"]["reviews"][-1]["status"], "invalid")
        self.assertEqual(self.group(self.gid)["state"], "RUNNING")
        self.assertFalse(
            _review.review_path(
                self.store, self.gid, stored["runtime"]["integration_sha"]
            ).is_file()
        )

    def test_an_acceptance_id_outside_the_plan_is_refused(self):
        group = self.group(self.gid)
        request = self.review_request(group)
        request["review"]["items"] = [{**request["review"]["items"][0], "acceptance_id": "AC-99"}]
        with self.assertRaises(Error) as ctx:
            _review.submit(self.store, self.gid, request)
        self.assertIn("不在本计划映射的 AC 内", str(ctx.exception))

    def test_a_fabricated_check_reference_is_refused(self):
        """T18：伪证据与缺失证据同样被拒。"""
        group = self.group(self.gid)
        request = self.review_request(group)
        request["review"]["items"][0]["evidence_refs"] = ["check:no-such-check"]
        with self.assertRaises(Error) as ctx:
            _review.submit(self.store, self.gid, request)
        self.assertEqual(ctx.exception.details["kind"], "review_invalid")
        self.assertIn("没有通过记录", str(ctx.exception))

    def test_a_check_reference_that_really_passed_is_accepted(self):
        group = self.group(self.gid)
        request = self.review_request(group)
        # `integration` 是组级检查，在当前 SHA 上真的 PASS 过。
        request["review"]["items"][0]["evidence_refs"] = ["check:integration"]
        result = _review.submit(self.store, self.gid, request)
        self.assertTrue(result["accepted"])

    def test_a_review_of_another_subject_is_refused(self):
        group = self.group(self.gid)
        request = self.review_request(group, subject_sha="d" * 40)
        with self.assertRaises(Error) as ctx:
            _review.submit(self.store, self.gid, request)
        self.assertIn("subject_sha", str(ctx.exception))

    def test_a_review_of_another_design_is_refused(self):
        group = self.group(self.gid)
        request = self.review_request(group, design_digest="0" * 64)
        with self.assertRaises(Error) as ctx:
            _review.submit(self.store, self.gid, request)
        self.assertIn("design_digest", str(ctx.exception))

    def test_an_absolute_path_in_locations_is_refused(self):
        group = self.group(self.gid)
        request = self.review_request(group)
        request["review"]["items"][0]["locations"] = ["/etc/passwd"]
        with self.assertRaises(Error) as ctx:
            _review.submit(self.store, self.gid, request)
        self.assertIn("绝对路径", str(ctx.exception))

    def test_an_unknown_status_is_refused(self):
        group = self.group(self.gid)
        request = self.review_request(group, status="SKIPPED")
        with self.assertRaises(Error) as ctx:
            _review.submit(self.store, self.gid, request)
        self.assertIn("status", str(ctx.exception))

    def test_a_review_needs_an_actor(self):
        group = self.group(self.gid)
        request = self.review_request(group)
        request.pop("actor")
        with self.assertRaises(Error) as ctx:
            _review.submit(self.store, self.gid, request)
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")


class TestReviewGates(ReviewFixture):
    """§11.3：审查参与 REVIEW_READY 与 accept，而不是只出现在报告里。"""

    def test_acceptance_is_refused_without_a_review(self):
        gid = self.group_with(review={"required": True})
        task, group = self.deliver(gid)
        self.assertEqual(group["state"], "RUNNING")
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
                    "evidence_refs": ["group:" + group["runtime"]["integration_sha"]],
                },
            )
        self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID")
        self.assertIn("not REVIEW_READY", str(ctx.exception))

    def test_a_fail_finding_keeps_the_group_out_of_review_ready(self):
        gid = self.group_with(review={"required": True, "mode": "agent"})
        self.deliver(gid)
        result = self.submitted(gid, status="FAIL")
        self.assertEqual(result["review"]["verdict"], "FAIL")
        self.assertEqual(len(result["unresolved"]), 1)

        # 提交 FAIL 之后重新派生：组退回 RUNNING，阻塞项点名了那条 finding。
        group = self.group(gid)
        _group_runner.reconcile(self.store, group)
        group = self.group(gid)
        self.assertEqual(group["state"], "RUNNING")
        codes = [item.get("code") for item in group["runtime"]["blockers"]]
        self.assertIn("DESIGN_REVIEW_FAILED", codes)
        finding = next(
            item
            for item in group["runtime"]["blockers"]
            if item.get("code") == "DESIGN_REVIEW_FAILED"
        )
        self.assertEqual(finding["acceptance_ids"], ["AC-1"])

    def test_not_verified_is_listed_and_never_reported_as_a_pass(self):
        gid = self.group_with(review={"required": True})
        self.deliver(gid)
        result = self.submitted(gid, status="NOT_VERIFIED")
        self.assertEqual(result["review"]["verdict"], "NOT_VERIFIED")
        self.assertEqual([item["acceptance_id"] for item in result["not_verified"]], ["AC-1"])
        self.assertEqual(self.group(gid)["state"], "REVIEW_READY")

        text = _integration.render_report(_integration.report(self.store, gid))
        self.assertIn("NOT_VERIFIED", text)
        self.assertIn("未验证项（不计入通过）", text)

    def test_a_review_of_an_older_integration_is_stale(self):
        gid = self.group_with(review={"required": True})
        self.deliver(gid)
        self.submitted(gid)
        self.assertEqual(self.group(gid)["state"], "REVIEW_READY")

        group = self.group(gid)
        group["runtime"]["integration_sha"] = "e" * 40
        self.store.save_group(group, "test_marker")
        state = _review.summary(self.store, self.group(gid))
        self.assertEqual(state["state"], _review.STATE_STALE)
        self.assertFalse(state["satisfied"])

    def test_a_plan_without_a_review_block_keeps_the_old_behaviour(self):
        """§19.3：新能力字段默认缺失，不因升级二进制就多出一道门。"""
        gid = self.group_with()
        _, group = self.deliver(gid)
        self.assertFalse(_review.required(group))
        self.assertEqual(group["state"], "REVIEW_READY")
        summary = _review.summary(self.store, group)
        self.assertEqual(summary["state"], _review.STATE_NOT_REQUIRED)
        self.assertTrue(summary["satisfied"])

    def test_create_warns_when_the_plan_declares_no_review(self):
        result = _groups.create(
            self.store,
            self.plan(members=[dict(MEMBER_A)], requirements=list(REQUIREMENT)),
        )
        self.assertTrue(
            any("no `review` block" in warning for warning in result["warnings"]),
            result["warnings"],
        )


class TestBoundedRepair(ReviewFixture):
    """§12：修复的入口、上限与执行路径。"""

    def repaired(self, **budget):
        values = {"max_starts": 4, "max_wall_seconds": None, "max_group_repairs": 1}
        values.update(budget)
        gid = self.group_with(review={"required": True}, budget=values)
        self.drive(self.member_task(gid, "a"))
        group = self.group(gid)
        group["state"] = "RUNNING"
        self.store.save_group(group, "test_marker")
        _group_runner.tick(self.store, gid)
        self.submitted(gid, status="FAIL")
        return gid

    def unit_task(self, gid, task_key):
        """建一个单元任务（成员或修复）；不像成员夹具那样假设 consumed_input_sha。

        修复的 ``consumed_input_sha`` 是当时的集成版本，不是组基线——这正是
        "下游基于上游产物"的表达。
        """
        from src import _pipeline

        snapshot = _groups.group_dir(self.store, gid) / "specs" / (task_key + ".json")
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
        return self.store.task(created["task_id"])

    def repair_request(self, acceptance_id="AC-1", **overrides):
        request = {
            "schema_version": 2,
            "request_id": "req-" + uuid.uuid4().hex,
            "acceptance_id": acceptance_id,
            "spec_file": "specs/a.json",
            "title": "fix AC-1",
            "affected_members": ["a"],
            "expected_paths": ["src/a.txt"],
            "authorization_ref": "scope-1",
        }
        request.update(overrides)
        return request

    def test_a_repair_without_a_plan_budget_is_refused(self):
        gid = self.group_with(review={"required": True})
        self.deliver(gid)
        self.submitted(gid, status="FAIL")
        with self.assertRaises(Error) as ctx:
            _group_runner.repair(self.store, gid, self.repair_request())
        self.assertEqual(ctx.exception.code, "GROUP_BUDGET_EXHAUSTED")
        self.assertEqual(ctx.exception.details["kind"], "group_repair_not_authorized")

    def test_a_repair_needs_a_fail_finding(self):
        gid = self.group_with(
            review={"required": True},
            budget={"max_starts": 4, "max_wall_seconds": None, "max_group_repairs": 1},
        )
        self.deliver(gid)
        # 没有审查结论时，修复无从谈起
        with self.assertRaises(Error) as ctx:
            _group_runner.repair(self.store, gid, self.repair_request())
        self.assertEqual(ctx.exception.details["kind"], "repair_without_finding")

    def test_a_repair_is_refused_for_a_passing_acceptance(self):
        gid = self.group_with(
            review={"required": True},
            budget={"max_starts": 4, "max_wall_seconds": None, "max_group_repairs": 1},
        )
        self.deliver(gid)
        self.submitted(gid, status="PASS")
        with self.assertRaises(Error) as ctx:
            _group_runner.repair(self.store, gid, self.repair_request())
        self.assertEqual(ctx.exception.details["kind"], "repair_without_finding")

    def test_the_repair_budget_is_a_hard_cap(self):
        gid = self.repaired()
        first = _group_runner.repair(self.store, gid, self.repair_request())
        self.assertEqual(first["repair"]["task_key"], "repair-1")
        self.assertEqual(first["repair_budget"], {"used": 1, "max": 1})

        with self.assertRaises(Error) as ctx:
            _group_runner.repair(self.store, gid, self.repair_request())
        self.assertEqual(ctx.exception.code, "GROUP_BUDGET_EXHAUSTED")
        self.assertEqual(ctx.exception.details["kind"], "group_repair_exhausted")

    def test_the_plan_digest_is_unaffected_by_a_repair(self):
        """修复是 runtime 事实：往计划里插成员就等于篡改摘要。"""
        gid = self.repaired()
        before = self.group(gid)["plan_digest"]
        _group_runner.repair(self.store, gid, self.repair_request())
        after = self.group(gid)
        self.assertEqual(after["plan_digest"], before)
        self.assertNotIn("repair-1", [item["task_key"] for item in after["definition"]["members"]])
        self.assertIn("repair-1", [item["task_key"] for item in after["runtime"]["repairs"]])

    def test_a_repair_runs_through_the_same_dispatch_and_integration_path(self):
        gid = self.repaired()
        _group_runner.repair(self.store, gid, self.repair_request())

        group = self.group(gid)
        self.assertIn("repair-1", _group_runner.ready_members(group))
        # 修复单元排在所有成员之后：它修的是整合后暴露的问题。
        self.assertEqual(_group_runner._order(group), ["a", "repair-1"])
        view = next(
            item
            for item in _groups.members_view(self.store, group)
            if item["task_key"] == "repair-1"
        )
        self.assertEqual(view["kind"], "repair")
        self.assertEqual(view["state"], "READY")

        old_sha = group["runtime"]["integration_sha"]
        repair_task = self.drive(self.unit_task(gid, "repair-1"))
        self.assertEqual(repair_task["state"], "REVIEW_READY")
        _group_runner.tick(self.store, gid)

        group = self.group(gid)
        unit = _groups.member_runtime(group, "repair-1")
        self.assertIsNotNone(unit["integrated_sha"])
        self.assertEqual(group["runtime"]["integration_sha"], unit["integrated_sha"])
        self.assertNotEqual(group["runtime"]["integration_sha"], old_sha)

        # 集成版本前移 ⇒ 旧检查与旧审查都作废，组不能回到 REVIEW_READY。
        self.assertTrue(_integration.checks_are_current(self.store, group))
        self.assertEqual(_review.summary(self.store, group)["state"], _review.STATE_STALE)
        self.assertEqual(group["state"], "RUNNING")

        # 对新版本重新审查后，才允许验收。
        result = self.submitted(gid)
        self.assertEqual(result["review"]["verdict"], "PASS")
        self.assertEqual(self.group(gid)["state"], "REVIEW_READY")

    def test_the_report_shows_repair_budget_and_units(self):
        gid = self.repaired()
        _group_runner.repair(self.store, gid, self.repair_request())
        text = _integration.render_report(_integration.report(self.store, gid))
        self.assertIn("组级修复单元", text)
        self.assertIn("repair-1", text)
        self.assertIn("组级修复轮数：1/1", text)


class TestReviewArtifacts(ReviewFixture):
    """落盘布局与可重放性（§7 建议布局）。"""

    def test_the_review_is_written_under_reviews_by_subject(self):
        gid = self.group_with(review={"required": True})
        self.deliver(gid)
        self.submitted(gid)
        group = self.group(gid)
        path = _review.review_path(self.store, gid, group["runtime"]["integration_sha"])
        self.assertTrue(path.is_file())
        on_disk = json.loads(path.read_text())
        self.assertEqual(on_disk["subject_sha"], group["runtime"]["integration_sha"])
        self.assertEqual(on_disk["verdict"], "PASS")
        self.assertEqual(
            on_disk["items_digest"], _review.current(self.store, group)["items_digest"]
        )

    def test_a_tampered_review_file_is_not_accepted_as_current(self):
        gid = self.group_with(review={"required": True})
        self.deliver(gid)
        self.submitted(gid)
        group = self.group(gid)
        path = _review.review_path(self.store, gid, group["runtime"]["integration_sha"])
        payload = json.loads(path.read_text())
        payload["items"][0]["status"] = "FAIL"
        atomic(path, payload)
        # items_digest 不再匹配：结论作废，退回"没有有效结论"。
        self.assertIsNone(_review.current(self.store, group))
        self.assertFalse(_review.summary(self.store, group)["satisfied"])

    def test_a_replayed_review_request_returns_the_same_answer(self):
        gid = self.group_with(review={"required": True})
        self.deliver(gid)
        group = self.group(gid)
        request = self.review_request(group)
        first = _review.submit(self.store, gid, request)
        second = _review.submit(self.store, gid, request)
        self.assertEqual(first["review"]["items_digest"], second["review"]["items_digest"])
        # 重放不重复写历史
        self.assertEqual(len(self.group(gid)["runtime"]["reviews"]), 1)


if __name__ == "__main__":
    unittest.main()
