"""v1.7 第二层验收：逐条 AC 判定、设计裁定、进度绑定与覆盖账本。

这一批的出发点是一次真实事故：7 个任务合并、全量测试 1164 条全绿，而 28 条
需求里真正验收闭环的是 0 条——"pytest 过了"被当成了"做完了"。

所以这里的用例**不是**测新功能能不能跑，而是测那几种"看起来通过了"的情形
会不会被拦住：

* spec 要求逐条判定却没有结论 → accept 被拒；
* worker 自己提交的判定 → 被拒；
* `NOT_VERIFIED` 想默认放行 → 被拒；
* 设计裁定还 open → 被拒；
* 需求文档的复选框没勾 → candidate 不产生。
"""

from __future__ import annotations

import json
import unittest

from test_runtime import FakeDocker, FakePublisher, RuntimeFixture

from src import _coverage, _pipeline, _task_review
from src._task_review import submit as submit_review
from src.core import Error, atomic, git

run_pipeline = _pipeline.run_pipeline
accept = _pipeline.accept


def v3_spec(**overrides):
    document = {
        "schema_version": 3,
        "title": "Test",
        "allowed_paths": ["src.txt"],
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "statement": "unit 检查通过",
                "verification": {"type": "check", "check_id": "unit"},
            },
            {
                "id": "AC-2",
                "statement": "人工确认目标行为",
                "verification": {
                    "type": "manual",
                    "judge": "independent",
                    "instructions": "看 src.txt 是否仍是原内容",
                },
            },
        ],
        "acceptance_review": {"required": True, "mode": "either", "independent": True},
    }
    document.update(overrides)
    return document


class TaskReviewTest(RuntimeFixture):
    def set_spec(self, document):
        self.spec.write_text(json.dumps(document, ensure_ascii=False))
        return self.spec

    def review(self, **overrides):
        body = {
            "actor": "reviewer",
            "reviewer": {
                "kind": "agent",
                "actor": "reviewer",
                "profile_id": "reviewer-profile",
                "independent_context": True,
            },
            "items": [
                {"acceptance_id": "AC-1", "status": "PASS", "evidence_refs": ["check:unit"]},
                {"acceptance_id": "AC-2", "status": "PASS", "evidence_refs": ["doc:src.txt"]},
            ],
        }
        body.update(overrides)
        return body

    def accept_request(self, t, **overrides):
        body = self.req(
            expected_version=t["row_version"],
            subject_sha=t["validated_sha"],
            actor="owner",
            decision="accepted",
            evidence=["manual sign-off"],
        )
        body.update(overrides)
        return body

    # ------------------------------------------------------------ spec 契约

    def test_v2_spec_keeps_the_old_behaviour(self):
        """v2 任务不受新门禁影响：它们没有 acceptance_review 段。"""
        t = self.run_task(self.new())
        self.assertEqual(t["spec_contract_version"], 1)  # Markdown spec
        accept(self.s, t["task_id"], self.accept_request(t))

    def test_v3_fields_in_a_v2_spec_are_refused(self):
        document = {
            "schema_version": 2,
            "title": "Test",
            "allowed_paths": ["src.txt"],
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "statement": "x",
                    "verification": {"type": "check", "check_id": "unit"},
                }
            ],
            "acceptance_review": {"required": True},
        }
        self.set_spec(document)
        with self.assertRaises(Error) as ctx:
            self.new()
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("schema_version 3", str(ctx.exception))

    def test_manual_ac_without_judge_is_refused(self):
        document = v3_spec()
        document["acceptance_criteria"][1]["verification"] = {
            "type": "manual",
            "instructions": "look",
        }
        self.set_spec(document)
        with self.assertRaises(Error) as ctx:
            self.new()
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("judge", str(ctx.exception))

    def test_spec_version_is_recorded_as_declared(self):
        self.set_spec(v3_spec())
        t = self.new()
        self.assertEqual(t["spec_contract_version"], 3)

    def test_review_query_exposes_criteria_for_the_gate_script(self):
        """`task review` 的只读输出自带 AC 形状，脚本不必再读一份 spec 副本。"""
        self.set_spec(v3_spec())
        t = self.run_task(self.new())
        body = _task_review.summary(self.s, self.s.task(t["task_id"]))
        self.assertEqual([item["id"] for item in body["criteria"]], ["AC-1", "AC-2"])
        self.assertEqual(body["criteria"][1]["judge"], "independent")
        self.assertEqual(body["criteria"][0]["check_id"], "unit")

    # ------------------------------------------------------------ accept 门禁

    def test_accept_is_blocked_without_a_review(self):
        """事故核心：checks 全绿 + 没有逐条判定 ⇒ 不能验收。"""
        self.set_spec(v3_spec())
        t = self.run_task(self.new())
        self.assertEqual(t["state"], "REVIEW_READY")
        t = self.s.task(t["task_id"])
        with self.assertRaises(Error) as ctx:
            accept(self.s, t["task_id"], self.accept_request(t))
        self.assertEqual(ctx.exception.code, "ACCEPTANCE_UNREVIEWED")
        self.assertEqual(
            ctx.exception.details["required_acceptance_ids"], ["AC-1", "AC-2"]
        )

    def test_review_covering_every_ac_unblocks_accept(self):
        self.set_spec(v3_spec())
        t = self.run_task(self.new())
        t = self.s.task(t["task_id"])
        submit_review(
            self.s,
            t["task_id"],
            self.req(subject_sha=t["validated_sha"], **self.review()),
        )
        t = self.s.task(t["task_id"])
        result = accept(self.s, t["task_id"], self.accept_request(t))
        self.assertEqual(result["state"], "DONE")
        stored = json.loads((self.s.path(t) / "acceptance.json").read_text())
        self.assertEqual(stored["review"]["verdict"], "PASS")
        self.assertEqual(stored["review"]["subject_sha"], t["validated_sha"])

    def test_review_missing_one_ac_is_invalid(self):
        self.set_spec(v3_spec())
        t = self.run_task(self.new())
        t = self.s.task(t["task_id"])
        body = self.review()
        body["items"] = body["items"][:1]
        with self.assertRaises(Error) as ctx:
            submit_review(self.s, t["task_id"], self.req(subject_sha=t["validated_sha"], **body))
        self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID")
        self.assertIn("AC-2", str(ctx.exception))
        # 无效输出留档：它不能变成一次通过，也不能被静默丢弃。
        t = self.s.task(t["task_id"])
        self.assertEqual(t["reviews"][-1]["status"], "invalid")

    def test_worker_cannot_review_its_own_candidate(self):
        self.set_spec(v3_spec())
        t = self.run_task(self.new())
        t = self.s.task(t["task_id"])
        body = self.review()
        body["reviewer"]["profile_id"] = t["agent_profile_id"]
        with self.assertRaises(Error) as ctx:
            submit_review(self.s, t["task_id"], self.req(subject_sha=t["validated_sha"], **body))
        self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID")
        self.assertIn("SELF_REVIEW", str(ctx.exception))

    def test_not_verified_must_be_acknowledged_item_by_item(self):
        self.set_spec(v3_spec())
        t = self.run_task(self.new())
        t = self.s.task(t["task_id"])
        body = self.review()
        body["items"][1] = {
            "acceptance_id": "AC-2",
            "status": "NOT_VERIFIED",
            "finding": "没有独立环境复现",
        }
        submit_review(self.s, t["task_id"], self.req(subject_sha=t["validated_sha"], **body))
        t = self.s.task(t["task_id"])
        with self.assertRaises(Error) as ctx:
            accept(self.s, t["task_id"], self.accept_request(t))
        self.assertEqual(ctx.exception.code, "ACCEPTANCE_UNACKNOWLEDGED")
        self.assertEqual(ctx.exception.details["not_verified"], ["AC-2"])
        t = self.s.task(t["task_id"])
        accept(
            self.s,
            t["task_id"],
            self.accept_request(t, acknowledged_not_verified=["AC-2"]),
        )

    def test_open_decision_blocks_accept(self):
        self.set_spec(
            v3_spec(
                decisions=[
                    {
                        "id": "D-2",
                        "question": "契约判 R 还是 F",
                        "options": ["R", "F"],
                        "recommendation": "F",
                        "status": "open",
                    }
                ]
            )
        )
        t = self.run_task(self.new())
        t = self.s.task(t["task_id"])
        submit_review(
            self.s,
            t["task_id"],
            self.req(subject_sha=t["validated_sha"], **self.review()),
        )
        t = self.s.task(t["task_id"])
        with self.assertRaises(Error) as ctx:
            accept(self.s, t["task_id"], self.accept_request(t))
        self.assertEqual(ctx.exception.code, "DECISIONS_OPEN")
        self.assertEqual(ctx.exception.details["open"][0]["id"], "D-2")

    # ------------------------------------------------------------ 进度绑定

    def _add_plan(self, ticked: bool):
        (self.repo / "docs").mkdir(exist_ok=True)
        box = "x" if ticked else " "
        (self.repo / "docs" / "plan.md").write_text(
            "# 计划\n\n- [{}] R-14 通知 outbox 四态写入\n".format(box)
        )
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "plan")
        # 项目边界也要放宽：spec 的 allowed_paths 只能收紧，不能扩权。
        self.c["allowed_paths"] = ["src.txt", "docs/plan.md"]
        atomic(self.s.home / "projects/demo.json", self.c)

    def test_unticked_progress_ref_fails_verification(self):
        self._add_plan(ticked=False)
        self.set_spec(
            v3_spec(
                allowed_paths=["src.txt", "docs/plan.md"],
                progress_refs=[
                    {"acceptance_id": "AC-2", "doc": "docs/plan.md", "marker": "R-14"}
                ],
            )
        )
        t = self.run_task(self.new())
        self.assertEqual(t["state"], "BLOCKED")
        self.assertEqual(t["block_code"], "PROGRESS_NOT_TICKED")
        self.assertIn("not_ticked", t.get("last_error") or "")

    def test_ticked_progress_ref_is_accepted(self):
        self._add_plan(ticked=True)
        self.set_spec(
            v3_spec(
                allowed_paths=["src.txt", "docs/plan.md"],
                progress_refs=[
                    {"acceptance_id": "AC-2", "doc": "docs/plan.md", "marker": "R-14"}
                ],
            )
        )
        t = self.run_task(self.new())
        self.assertEqual(t["state"], "REVIEW_READY")

    # ------------------------------------------------------------ 覆盖账本

    def test_coverage_ledger_separates_missing_from_pending(self):
        self.set_spec(
            v3_spec(
                progress_refs=[
                    {"acceptance_id": "AC-2", "doc": "src.txt", "marker": "R-01"}
                ]
            )
        )
        t = self.run_task(self.new())
        body = _coverage.ledger(self.s, "demo", ["R-01", "R-09"])
        self.assertEqual([entry["marker"] for entry in body["pending"]], ["R-01"])
        self.assertEqual(body["missing"], ["R-09"])
        self.assertFalse(body["cleared"])
        # REVIEW_READY 是 pending 而不是 covered：代码写完不等于需求闭环。
        self.assertEqual(body["pending"][0]["state"], t["state"])


if __name__ == "__main__":
    unittest.main()
