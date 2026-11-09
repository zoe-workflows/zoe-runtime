"""v1.4.1：runner 退出条件是公开契约，不是隐藏的 tick 计数。

v1.4.0 的 `run()` 用一个 `while ticks < 2000 and not done` 的循环退出，
`MAX_TICKS` 会先于任何终态命中 —— 于是「一次 group start 持续推进」在约 400 秒
后被静默截断，返回值里没有任何一个字段说明为什么停下。这里的用例锁住新契约：

* `exit_reason` 与 `runtime.lease.exit_reason` 同源、可解释；
* `MAX_TICKS` 降级为安全上限，命中时原因记为 `safety_cap` 并给出续接信息；
* 静止的组（无可推进项）与已经交付的组必须给出**不同**的原因。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from test_v130_runner import RunnerCase

from src import _group_runner
from src._proc import process_identity
from src._util import Error


class StopReasonTable(unittest.TestCase):
    """`stop_reason` 只看组记录，因此可以逐行钉住。"""

    def reason(self, state="RUNNING", blockers=()):
        return _group_runner.stop_reason({"state": state, "runtime": {"blockers": list(blockers)}})

    def test_terminal_and_paused_states(self):
        self.assertEqual(self.reason("DONE"), "completed")
        self.assertEqual(self.reason("CANCELLED"), "cancelled")
        self.assertEqual(self.reason("PAUSED"), "paused")
        self.assertEqual(self.reason("REVIEW_READY"), "review_ready")

    def test_design_review_is_a_named_stop_point(self):
        """审查由宿主执行：没有结论时停下，而不是让组凭空完成。"""
        self.assertEqual(
            self.reason("BLOCKED", [{"code": "DESIGN_REVIEW_REQUIRED"}]),
            "awaiting_design_review",
        )
        self.assertEqual(
            self.reason("BLOCKED", [{"code": "DESIGN_REVIEW_FAILED"}]),
            "review_failed",
        )

    def test_budget_and_slots_are_distinguished_from_a_real_blocker(self):
        self.assertEqual(
            self.reason("BLOCKED", [{"code": "GROUP_BUDGET_EXHAUSTED"}]),
            "budget_exhausted",
        )
        self.assertEqual(self.reason("BLOCKED", [{"code": "SLOT_BUSY"}]), "slots")
        self.assertEqual(self.reason("BLOCKED", [{"code": "DEPENDENCY_BLOCKED"}]), "blocked")
        self.assertEqual(self.reason("BLOCKED", []), "blocked")

    def test_idle_group_is_not_called_done(self):
        self.assertEqual(self.reason("RUNNING"), "no_progress")

    def test_every_reason_has_an_explanation(self):
        produced = {
            self.reason("DONE"),
            self.reason("CANCELLED"),
            self.reason("PAUSED"),
            self.reason("REVIEW_READY"),
            self.reason("RUNNING"),
            self.reason("BLOCKED"),
            self.reason("BLOCKED", [{"code": "DESIGN_REVIEW_REQUIRED"}]),
            self.reason("BLOCKED", [{"code": "DESIGN_REVIEW_FAILED"}]),
            self.reason("BLOCKED", [{"code": "GROUP_BUDGET_EXHAUSTED"}]),
            self.reason("BLOCKED", [{"code": "SLOT_BUSY"}]),
            "lease_stale",
            "safety_cap",
            "crashed",
        }
        for reason in produced:
            with self.subTest(reason=reason):
                self.assertIn(reason, _group_runner.EXIT_REASONS)
                self.assertTrue(_group_runner.EXIT_REASONS[reason])


class RunnerLoop(RunnerCase):
    def lease(self, gid: str) -> str:
        """Write the lease record `attach()` expects, as `group start` would."""
        lease_id = "lease-" + os.urandom(4).hex()
        group = self.store.group(gid)
        group["runtime"]["lease"] = {
            "lease_id": lease_id,
            "state": "running",
            "pid": os.getpid(),
            "start_token": process_identity(os.getpid()),
        }
        group["state"] = "RUNNING"
        self.store.save_group(group, "test_marker")
        return lease_id

    def test_safety_cap_is_reported_instead_of_dying_silently(self):
        gid = self.created()
        lease_id = self.lease(gid)
        # 一个永远「还有进展」的 tick：只有安全上限能停下循环。
        with (
            mock.patch.object(_group_runner, "tick", return_value=True),
            mock.patch.object(_group_runner, "TICK_INTERVAL", 0),
        ):
            result = _group_runner.run(self.store, gid, lease_id, max_ticks=3)

        self.assertEqual(result["ticks"], 3)
        self.assertEqual(result["exit_reason"], "safety_cap")
        self.assertFalse(result["completed"], "推进停止不等于任务完成")
        self.assertTrue(result["stop"]["resumable"])
        self.assertIn("group start", result["stop"]["resume_hint"])
        # 同一事实必须落到记录里，否则宿主重启后读不到原因。
        lease = self.store.group(gid)["runtime"]["lease"]
        self.assertEqual(lease["exit_reason"], "safety_cap")
        self.assertEqual(lease["ticks"], 3)
        self.assertEqual(lease["state"], "exited")

    def test_a_crashing_tick_is_crashed_not_no_progress(self):
        """崩溃不是"本轮没有可推进项"。

        旧实现把任何异常都留成默认的 ``no_progress``，而它属于 ``_RESUMABLE``：
        宿主读到"重启就能接着做"，于是重启一个必然在同一个 tick 上再崩一次的
        推进器，真正的错误只留在 stderr。
        """
        for boom in (Error("STATE_CONFLICT", "record is corrupt"), OSError("disk gone")):
            with self.subTest(boom=type(boom).__name__):
                gid = self.created()
                lease_id = self.lease(gid)
                with mock.patch.object(_group_runner, "tick", side_effect=boom):
                    with self.assertRaises(type(boom)):
                        _group_runner.run(self.store, gid, lease_id, max_ticks=10)

                lease = self.store.group(gid)["runtime"]["lease"]
                self.assertEqual(lease["exit_reason"], "crashed")
                self.assertEqual(lease["state"], "exited")
                self.assertNotIn("crashed", _group_runner._RESUMABLE)
                self.assertIn("crashed", _group_runner._NEEDS_HUMAN)

    def test_stopped_group_says_why_and_that_it_is_unfinished(self):
        gid = self.created()
        lease_id = self.lease(gid)
        with mock.patch.object(_group_runner, "tick", return_value=False):
            result = _group_runner.run(self.store, gid, lease_id, max_ticks=10)

        self.assertEqual(result["ticks"], 1)
        self.assertEqual(result["exit_reason"], "no_progress")
        self.assertFalse(result["completed"])
        self.assertTrue(result["stop"]["needs_human"] is False)
        self.assertEqual(result["state"], "RUNNING")

    def test_review_ready_is_not_resumable_and_hints_review_then_accept(self):
        """``review_ready`` 是"等人拍板"，不是"重启推进器"。

        `group start` 只接受 READY/PAUSED/BLOCKED，而此时组状态是
        REVIEW_READY；给 start 提示只会换来 STATE_CONFLICT。
        """
        gid = self.created()
        lease_id = self.lease(gid)
        group = self.store.group(gid)
        group["state"] = "REVIEW_READY"
        self.store.save_group(group, "test_marker")
        with mock.patch.object(_group_runner, "tick", return_value=False):
            result = _group_runner.run(self.store, gid, lease_id, max_ticks=10)

        self.assertEqual(result["exit_reason"], "review_ready")
        self.assertFalse(result["stop"]["resumable"])
        self.assertTrue(result["stop"]["needs_human"])
        self.assertIn("group accept", result["stop"]["resume_hint"])
        self.assertNotIn("group start", result["stop"]["resume_hint"])

    def test_delivered_group_reports_completed_and_is_not_resumed(self):
        gid = self.created()
        lease_id = self.lease(gid)
        group = self.store.group(gid)
        group["state"] = "DONE"
        self.store.save_group(group, "test_marker")
        with mock.patch.object(_group_runner, "tick", return_value=False):
            result = _group_runner.run(self.store, gid, lease_id, max_ticks=10)

        self.assertEqual(result["exit_reason"], "completed")
        self.assertTrue(result["completed"])
        self.assertFalse(result["stop"]["resumable"])
        self.assertIsNone(result["stop"]["resume_hint"])

    def test_lease_taken_over_stops_the_loop_without_writing(self):
        gid = self.created()
        lease_id = self.lease(gid)
        stale = _group_runner._conflict("taken over", "lease_stale")
        with mock.patch.object(_group_runner, "tick", side_effect=stale):
            result = _group_runner.run(self.store, gid, lease_id, max_ticks=10)

        self.assertEqual(result["exit_reason"], "lease_stale")
        self.assertFalse(result["stop"]["resumable"])

    def test_zero_ticks_is_rejected(self):
        gid = self.created()
        lease_id = self.lease(gid)
        with self.assertRaises(Error) as caught:
            _group_runner.run(self.store, gid, lease_id, max_ticks=0)
        self.assertEqual(caught.exception.code, "INPUT_INCOMPLETE")


class LegacyAlias(unittest.TestCase):
    def test_max_ticks_is_no_longer_the_production_bound(self):
        """旧名保留，但指向安全上限；400 秒不再是设计意图。"""
        self.assertEqual(_group_runner.MAX_TICKS, _group_runner.SAFETY_MAX_TICKS)
        self.assertGreaterEqual(_group_runner.SAFETY_MAX_TICKS, 100_000)


if __name__ == "__main__":
    unittest.main()
