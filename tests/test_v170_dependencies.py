"""v1.7 依赖门禁：上游必须**已闭环**，不是"任务存在"。

2026-11-06 的事故里，R-27（契约冻结复核）在它推翻的 R-10 还没跑通时写下了
"已冻结"。复盘时容易说成"缺少检查"，其实是**输入缺失**：依赖从未声明，
runtime 无从知道 R-27 依赖 R-10，只能把 worker 的结论当成事实。

所以这一批测的不是"图算法",而是三种"看起来能派、实际不该派"的情形：

* 上游任务根本没人认领（marker 只出现在 depends_on 里）；
* 上游跑到了 REVIEW_READY（代码写完 ≠ 需求闭环）；
* 上游被接受了，但下游还没开始（这一条必须放行，否则门禁会变成永久阻塞）。
"""

from __future__ import annotations

import json
import unittest

from test_runtime import FakeDocker, FakePublisher, RuntimeFixture

from src import _coverage, _pipeline
from src.core import Error, atomic, git

accept = _pipeline.accept

MARKERS = ("R-01", "R-02", "R-09")


def spec(marker: str, depends_on=None):
    document = {
        "schema_version": 3,
        "title": "Test",
        "allowed_paths": ["src.txt", "plan.md"],
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "statement": "unit 检查通过",
                "verification": {"type": "check", "check_id": "unit"},
            }
        ],
        "progress_refs": [
            {"acceptance_id": "AC-1", "doc": "plan.md", "marker": marker}
        ],
    }
    if depends_on is not None:
        document["depends_on"] = depends_on
    return document


class DependencyGateTest(RuntimeFixture):
    def setUp(self):
        super().setUp()
        # 依赖靠 marker 认领，而 marker 认领走 progress_refs——那条链在 verify 阶段
        # 要求需求文档里的复选框已勾选。先准备好"已勾选"的文档，否则测的是
        # PROGRESS_NOT_TICKED，不是依赖。
        (self.repo / "plan.md").write_text(
            "".join("- [x] {} 需求\n".format(marker) for marker in MARKERS)
        )
        git(self.repo, "add", "plan.md")
        git(self.repo, "commit", "-m", "plan")
        self.c["allowed_paths"] = ["src.txt", "plan.md"]
        atomic(self.s.home / "projects/demo.json", self.c)

    def write_spec(self, document):
        # `RuntimeFixture` 默认给的是 Markdown spec；依赖只在 v3 JSON 里存在。
        self.spec = self.root / "spec.json"
        self.spec.write_text(json.dumps(document, ensure_ascii=False))
        return self.spec

    def upstream(self, marker="R-01"):
        self.write_spec(spec(marker))
        return self.run_task(self.new())

    def accept_task(self, t):
        t = self.s.task(t["task_id"])
        return accept(
            self.s,
            t["task_id"],
            self.req(
                expected_version=t["row_version"],
                subject_sha=t["validated_sha"],
                actor="owner",
                decision="accepted",
                evidence=["manual sign-off"],
            ),
        )

    # ------------------------------------------------------------ 契约

    def test_self_dependency_is_refused_at_create(self):
        document = spec("R-01", depends_on=["R-01"])
        self.write_spec(document)
        with self.assertRaises(Error) as ctx:
            self.new()
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("自依赖", str(ctx.exception))

    def test_depends_on_in_a_v2_spec_is_refused(self):
        document = spec("R-01", depends_on=["R-00"])
        document["schema_version"] = 2
        document["progress_refs"] = []
        self.write_spec(document)
        with self.assertRaises(Error) as ctx:
            self.new()
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("depends_on", str(ctx.exception))

    # ------------------------------------------------------------ 判定

    def test_marker_without_any_owner_blocks(self):
        self.write_spec(spec("R-02", depends_on=["R-01"]))
        t = self.new()
        state = _coverage.dependency_state(self.s, self.s.task(t["task_id"]))
        self.assertFalse(state["satisfied"])
        self.assertEqual(state["blocking"][0]["reason"], "no_task_claims_marker")
        self.assertEqual(state["blocking"][0]["marker"], "R-01")

    def test_review_ready_upstream_is_not_closed(self):
        """上游代码写完但没验收 —— 这正是 R-27 当初踩的那一级。"""
        up = self.upstream("R-01")
        self.assertEqual(up["state"], "REVIEW_READY")
        self.write_spec(spec("R-02", depends_on=["R-01"]))
        down = self.new()
        state = _coverage.dependency_state(self.s, self.s.task(down["task_id"]))
        self.assertFalse(state["satisfied"])
        self.assertEqual(state["blocking"][0]["reason"], "upstream_not_closed")
        self.assertEqual(state["blocking"][0]["task_id"], up["task_id"])
        self.assertEqual(state["blocking"][0]["state"], "REVIEW_READY")

    def test_accepted_upstream_satisfies_the_gate(self):
        up = self.upstream("R-01")
        self.accept_task(up)
        self.write_spec(spec("R-02", depends_on=["R-01"]))
        down = self.new()
        state = _coverage.dependency_state(self.s, self.s.task(down["task_id"]))
        self.assertTrue(state["satisfied"], state)
        self.assertEqual(state["declared"], ["R-01"])

    def test_start_refuses_with_dependency_not_met(self):
        """派发被拒，而不是"派出去了再 BLOCKED"：后者白烧一次 run。"""
        self.write_spec(spec("R-02", depends_on=["R-09"]))
        t = self.new()
        t = self.s.task(t["task_id"])
        with self.assertRaises(Error) as ctx:
            _pipeline.start(
                self.s,
                t["task_id"],
                self.req(expected_version=t["row_version"]),
            )
        self.assertEqual(ctx.exception.code, "DEPENDENCY_NOT_MET")
        self.assertEqual(
            [item["marker"] for item in ctx.exception.details["blocking"]], ["R-09"]
        )

    # ------------------------------------------------------------ 图

    def test_graph_reports_edges_blocked_and_ready(self):
        up = self.upstream("R-01")
        self.write_spec(spec("R-02", depends_on=["R-01"]))
        self.new()
        body = _coverage.graph(self.s, "demo")
        self.assertEqual(len(body["nodes"]), 2)
        self.assertEqual(len(body["edges"]), 1)
        self.assertEqual(body["edges"][0]["to_task_id"], up["task_id"])
        self.assertFalse(body["edges"][0]["satisfied"])
        self.assertEqual(body["blocked"][0]["marker"], "R-01")
        self.assertFalse(body["ready"])
        self.assertEqual(body["cycles"], [])

    def test_graph_detects_a_cycle_between_two_tasks(self):
        self.write_spec(spec("R-01", depends_on=["R-02"]))
        self.new()
        self.write_spec(spec("R-02", depends_on=["R-01"]))
        self.new()
        body = _coverage.graph(self.s, "demo")
        self.assertTrue(body["cycles"], body)
        # 环必须是闭合的：首尾同一个任务。
        self.assertEqual(body["cycles"][0][0], body["cycles"][0][-1])


if __name__ == "__main__":
    unittest.main()
