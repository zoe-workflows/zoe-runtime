"""v1.6 配额耗尽：分类、降级证据、项目级闸门、CLI 读与解除。

测试针对宿主依赖的行为，而不是实现形状：

* 中文额度横幅必须分类成 ``QUOTA_EXHAUSTED``，不能因为含 "429" 就退化成
  ``AGENT_RATE_LIMITED``——两者的恢复建议相反（等重置 vs 退避重试）；
* 没有 capture 元数据时，退出码是唯一信号，但**日志里的证据不能丢**；
* 一次额度耗尽是项目级事实：闸门跨任务生效，且过了重置时间也不自动放行；
* 解除是人的决定，没有事故时不能凭空写出一条记录。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from test_runtime import RuntimeFixture

from src import _pipeline, _quota, _routing, _store, cli
from src._agents import base as agent_base

#: 重置时刻必须相对"现在"生成。这里曾写死成 ``2026-11-06 14:24:37``，于是当天
#: 14:24 之后 ``gate["expired"]`` 变 True，一条与代码无关的用例开始永久失败——
#: 定时炸弹式的基线噪声，比断言写松更危险（它会被读成"这次改动引入的回归"）。
RESET_AT = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
QUOTA_BANNER = f"429 使用量已超出频率限制，将在 {RESET_AT} 重置"


class TestClassification(unittest.TestCase):
    def test_chinese_quota_banner_is_quota_not_rate_limit(self):
        self.assertEqual(
            agent_base.classify_failure(QUOTA_BANNER), agent_base.OUTCOME_QUOTA_EXHAUSTED
        )
        self.assertEqual(
            agent_base.OUTCOME_ERRORS[agent_base.OUTCOME_QUOTA_EXHAUSTED], "QUOTA_EXHAUSTED"
        )

    def test_plain_rate_limit_stays_rate_limited(self):
        # 没有额度词、也没有重置时刻：这是可以退避的限流，不是额度耗尽。
        self.assertEqual(
            agent_base.classify_failure("429 Too Many Requests; retry after 60 seconds"),
            agent_base.OUTCOME_RATE_LIMITED,
        )

    def test_quota_words_without_429_are_still_quota(self):
        for text in (
            "You have exceeded your current quota, please check your plan",
            "账户额度已用完",
            "insufficient_quota",
            # 402 与 429 是同一类：预算/余额问题，退避没有意义。
            "402 Payment Required",
            "insufficient balance",
            "no credit remaining",
        ):
            self.assertEqual(
                agent_base.classify_failure(text), agent_base.OUTCOME_QUOTA_EXHAUSTED, text
            )

    def test_empty_text_is_not_quota(self):
        self.assertFalse(_quota.looks_exhausted(""))
        self.assertFalse(_quota.looks_exhausted(None))

    def test_failure_class_is_environment_not_permission(self):
        # 归到 auth_permission 会诱导 reassign：换 profile 修不好一个空额度池。
        self.assertEqual(_routing.failure_class_of("QUOTA_EXHAUSTED"), "environment")


class TestResetParsing(unittest.TestCase):
    def test_absolute_local_stamp(self):
        got = _quota.reset_at(QUOTA_BANNER)
        self.assertEqual(got["source"], "absolute")
        self.assertEqual(got["timezone"], "local")
        self.assertEqual(got["value"], RESET_AT)
        self.assertGreater(got["epoch"], 0)

    def test_absolute_with_offset_is_labelled(self):
        got = _quota.reset_at("quota resets at 2026-11-06T14:24:37Z")
        self.assertEqual(got["timezone"], "utc")

    def test_relative_retry_after(self):
        got = _quota.reset_at("rate limited; retry after 2 hours", now=1000.0)
        self.assertEqual(got["source"], "relative")
        self.assertEqual(got["epoch"], 1000.0 + 7200)

    def test_no_stamp_returns_none(self):
        self.assertIsNone(_quota.reset_at("429 使用量已超出频率限制"))


class TestGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _store.Store(self._tmp.name)
        self.project = "p1"

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_trip_then_clear(self):
        self.assertIsNone(_quota.active_gate(self.store, self.project))
        gate = _quota.trip(
            self.store,
            self.project,
            task_id="t1",
            run_id="r1",
            text=QUOTA_BANNER,
        )
        self.assertEqual(gate["reset_at"], RESET_AT)
        self.assertEqual(gate["task_id"], "t1")
        active = _quota.active_gate(self.store, self.project)
        self.assertIsNotNone(active)
        self.assertFalse(active["expired"])

        cleared = _quota.clear(
            self.store, self.project, why="budget renewed", actor="alice"
        )
        self.assertEqual(cleared["cleared_by"], "alice")
        self.assertEqual(cleared["cleared_reason"], "budget renewed")
        self.assertIsNone(_quota.active_gate(self.store, self.project))

    def test_expired_reset_does_not_unlock_by_itself(self):
        gate = _quota.trip(
            self.store, self.project, task_id="t1", run_id="r1", text=QUOTA_BANNER
        )
        later = gate["reset_at_epoch"] + 60
        still = _quota.active_gate(self.store, self.project, now=later)
        self.assertIsNotNone(still)
        self.assertTrue(still["expired"])

    def test_corrupt_record_stays_blocking(self):
        path = _quota.gate_path(self.store, self.project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        active = _quota.active_gate(self.store, self.project)
        self.assertTrue(active["_corrupt"])
        self.assertEqual(_quota.details(active)["kind"], "quota_gate_corrupt")

    def test_details_carry_reset_and_evidence(self):
        gate = _quota.trip(
            self.store, self.project, task_id="t1", run_id="r1", text=QUOTA_BANNER
        )
        details = _quota.details(gate)
        self.assertEqual(details["kind"], "quota_gate")
        self.assertEqual(details["quota_reset_at"], RESET_AT)
        self.assertIn("使用量", details["evidence"])


class TestDegradedEvidence(unittest.TestCase):
    """No capture metadata: the exit code is the signal, the log is the evidence."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _store.Store(self._tmp.name)
        self.task = {
            "task_id": "t1",
            "project_id": "p1",
            "workspace": str(Path(self._tmp.name) / "worktree"),
        }
        self.run = {"run_id": "run-1", "task_id": "t1"}
        run_dir = self.store.path(self.task) / "runs" / "run-1"
        run_dir.mkdir(parents=True)
        (run_dir / "agent.log").write_text(
            "starting implementation\n" + QUOTA_BANNER + "\n", encoding="utf-8"
        )

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _parse(self, code=1, adapter_id="codebuddy"):
        return _pipeline._agent_result(
            self.store,
            self.task,
            self.run,
            binding=SimpleNamespace(adapter_id=adapter_id, backend="codebuddy-cli"),
            spec=SimpleNamespace(agent_version="2.111.0"),
            code=code,
        )

    def test_exit_code_only_keeps_the_log_evidence(self):
        result = self._parse()
        self.assertEqual(result.outcome, agent_base.OUTCOME_QUOTA_EXHAUSTED)
        self.assertIn("使用量", result.summary)
        self.assertEqual(self.run["result_schema"], "exit-code-only")
        self.assertTrue(self.run["agent_evidence"]["degraded"])
        self.assertEqual(self.run["agent_evidence"]["source"], "agent.log")
        self.assertIn("使用量", self.run["agent_evidence"]["excerpt"])

    def test_exit_zero_is_still_completed(self):
        result = self._parse(code=0)
        self.assertEqual(result.outcome, agent_base.OUTCOME_COMPLETED)

    def test_missing_log_degrades_without_inventing_evidence(self):
        (self.store.path(self.task) / "runs" / "run-1" / "agent.log").unlink()
        result = self._parse()
        # 无证据时只能落到兜底码，但必须标明这是降级结论。
        self.assertEqual(result.outcome, agent_base.OUTCOME_FAILED)
        self.assertEqual(self.run["agent_evidence"]["excerpt"], "")


class TestGateBlocksDispatch(RuntimeFixture):
    """闸门必须在**产生 run 之前**拒绝，否则"没派发"与"派发失败"分不清。"""

    def test_start_is_refused_while_gate_is_active(self):
        from src.core import start
        from src._util import Error

        task = self.new()
        _quota.trip(
            self.s,
            "demo",
            task_id=task["task_id"],
            run_id=None,
            text=QUOTA_BANNER,
        )
        with self.assertRaises(Error) as ctx:
            start(
                self.s,
                task["task_id"],
                self.req(expected_version=task["row_version"]),
                "start",
            )
        self.assertEqual(ctx.exception.code, "QUOTA_EXHAUSTED")
        self.assertEqual(ctx.exception.details["kind"], "quota_gate")
        self.assertEqual(ctx.exception.details["quota_reset_at"], RESET_AT)
        # 拒绝发生在 spawn 之前：没有 run，也没有扣一次实现预算。
        self.assertEqual(self.s.runs(task["task_id"]), [])
        self.assertEqual(self.s.task(task["task_id"])["attempts"], 0)


class TestFailureTripsGate(RuntimeFixture):
    """一次配额失败必须同时留下三样东西：任务阻塞、项目闸门、可读快照。"""

    def test_pipeline_quota_failure_trips_project_gate_and_handoff(self):
        from unittest import mock

        from src._util import Error

        task = self.new()
        with mock.patch.object(
            _pipeline,
            "_agent_result",
            side_effect=Error("QUOTA_EXHAUSTED", QUOTA_BANNER),
        ):
            result = self.run_task(task)

        self.assertEqual(result["block_code"], "QUOTA_EXHAUSTED")
        # 额度恢复后继续的是同一次实现，不是重来。
        self.assertEqual(result["phase"], "implement")
        self.assertEqual(
            result["block_details"]["quota_reset_at"], RESET_AT
        )

        gate = _quota.active_gate(self.s, "demo")
        self.assertIsNotNone(gate)
        self.assertEqual(gate["task_id"], task["task_id"])
        self.assertEqual(gate["run_id"], result["current_run_id"])

        handoff_path = (
            self.s.path(result) / "runs" / result["current_run_id"] / "handoff.json"
        )
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        self.assertTrue(handoff["worktree_path"])
        self.assertEqual(handoff["run_id"], result["current_run_id"])
        self.assertEqual(handoff["quota_gate"]["task_id"], task["task_id"])


class TestResumeSnapshot(RuntimeFixture):
    """接手指令不能自相矛盾：文档说"不要 resume"，快照也不该建议 resume。"""

    def test_resume_surfaces_gate_and_suggests_clearing_it(self):
        from src import _orchestration

        task = self.new()
        record = self.s.task(task["task_id"])
        record.update(state="BLOCKED", block_code="QUOTA_EXHAUSTED")
        self.s.save(record, "quota_blocked")
        _quota.trip(
            self.s, "demo", task_id=task["task_id"], run_id=None, text=QUOTA_BANNER
        )

        view = _orchestration.resume(self.s, "demo")
        self.assertEqual(view["quota_gate"]["kind"], "quota_gate")
        self.assertEqual(view["quota_gate"]["quota_reset_at"], RESET_AT)
        rows = {row["task_id"]: row for row in view["dispatchable_now"]}
        self.assertEqual(rows[task["task_id"]]["action"], "project quota clear")


class TestCliQuota(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _store.Store(self._tmp.name)
        self.project = "p1"

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _show(self):
        return cli._project_quota(
            self.store, SimpleNamespace(quota_action="show", project=self.project)
        )

    def test_show_then_clear(self):
        _quota.trip(
            self.store, self.project, task_id="t1", run_id="r1", text=QUOTA_BANNER
        )
        shown = self._show()
        self.assertTrue(shown["blocking"])
        self.assertEqual(shown["gate"]["reset_at"], RESET_AT)

        cleared = cli._project_quota(
            self.store,
            SimpleNamespace(
                quota_action="clear", project=self.project, why="budget renewed", actor=None
            ),
        )
        self.assertTrue(cleared["cleared"])
        self.assertEqual(cleared["previous"]["task_id"], "t1")
        self.assertFalse(self._show()["blocking"])

    def test_clear_without_incident_writes_nothing(self):
        result = cli._project_quota(
            self.store,
            SimpleNamespace(
                quota_action="clear", project=self.project, why="just in case", actor=None
            ),
        )
        self.assertFalse(result["cleared"])
        self.assertFalse(_quota.gate_path(self.store, self.project).exists())


if __name__ == "__main__":
    unittest.main()
