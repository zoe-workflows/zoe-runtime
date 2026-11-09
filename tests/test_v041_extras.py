"""v0.4.1 收尾项：交接扩展、运行时绑定策略、watch 流式输出。

对应三件事：
1. ``handoff.json`` 补上 ``failed_checks`` / ``remaining_work``（06 §8），
   接手方不必重跑一遍才知道卡在哪、还剩什么。
2. ``runtime_binding``：默认 strict（指纹变了就不再声称 VERIFIED），
   显式 ``warn`` 才容忍"版本对、指纹变"。
3. ``watch --follow``：一行一个事件 JSON，宿主 ``readline`` 即可消费。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from src._agents import registry
from src._agents.registry import Verification
from src._pipeline import handoff_failed_checks, handoff_remaining_work
from src._store import Store


class TestHandoffRemainingWork(unittest.TestCase):
    def test_review_ready_only_needs_acceptance(self):
        self.assertEqual(
            handoff_remaining_work({"state": "REVIEW_READY", "phase": "acceptance"}),
            ["human acceptance"],
        )

    def test_blocked_names_the_blocker_first(self):
        work = handoff_remaining_work(
            {"state": "BLOCKED", "phase": "verify", "block_code": "CHECK_FAILED"}
        )
        self.assertEqual(work[0], "resolve CHECK_FAILED")
        self.assertEqual(work[1:], ["publish stage", "ci stage", "acceptance stage"])

    def test_paused_resumes_the_current_phase(self):
        work = handoff_remaining_work({"state": "PAUSED", "phase": "implement"})
        self.assertEqual(work[0], "resume implement")

    def test_completed_has_nothing_left(self):
        self.assertEqual(handoff_remaining_work({"state": "COMPLETED"}), [])


class TestHandoffFailedChecks(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._td.name) / "state")

    def tearDown(self):
        self.store.db.close()
        self._td.cleanup()

    def test_reads_validation_results(self):
        task = {"task_id": "task-1"}
        run = {"run_id": "run-1"}
        directory = self.store.home / "tasks" / "task-1" / "runs" / "run-1"
        directory.mkdir(parents=True)
        (directory / "validation.json").write_text(
            json.dumps(
                [
                    {"id": "unit", "status": "PASS", "exit_code": 0},
                    {"id": "lint", "status": "FAIL", "exit_code": 2},
                ]
            )
        )
        failed = handoff_failed_checks(self.store, task, run)
        # v1.3 (F3) adds `error_class` so the repair owner is explicit: exit 2 is
        # a test failure, not an environment failure.
        self.assertEqual(
            failed,
            [{"id": "lint", "status": "FAIL", "exit_code": 2, "error_class": "test"}],
        )

    def test_missing_validation_is_empty(self):
        task = {"task_id": "task-2"}
        run = {"run_id": "run-2"}
        self.assertEqual(handoff_failed_checks(self.store, task, run), [])


class TestRuntimeBinding(unittest.TestCase):
    def record(self) -> Verification:
        return Verification(
            backend="codebuddy-cli",
            version="9.9.9",
            source="real_e2e",
            platform="other-os",
            runtime_digest="digest-from-another-host",
            stopped_and_resumed=True,
            checks=tuple(
                {"id": name, "status": "PASS"}
                for name in (
                    "cli-found",
                    "version",
                    "auth",
                    "noninteractive-edit",
                    "parse",
                    "stop-resume",
                )
            ),
        )

    def test_strict_default_refuses_a_foreign_fingerprint(self):
        state, reason, verification = registry.resolve(
            "codebuddy",
            "codebuddy-cli",
            "9.9.9",
            self.record(),
            platform="this-host",
            runtime_digest="digest-here",
        )
        self.assertEqual(state, registry.CAPABILITY_IMPLEMENTED)
        self.assertIsNone(verification)
        self.assertIn("re-run qualification", reason or "")

    def test_warn_mode_accepts_but_says_why(self):
        state, reason, verification = registry.resolve(
            "codebuddy",
            "codebuddy-cli",
            "9.9.9",
            self.record(),
            platform="this-host",
            runtime_digest="digest-here",
            allow_runtime_mismatch=True,
        )
        self.assertEqual(state, registry.CAPABILITY_VERIFIED)
        self.assertIsNotNone(verification)
        self.assertIn("runtime_binding=warn", reason or "")


class _FakeWatchStore:
    """只实现 _watch_once 需要的三个方法。"""

    def __init__(self, events):
        self._events = list(events)

    def epoch(self):
        return "e1"

    def max_event_id(self, project):
        return max((e["id"] for e in self._events), default=0)

    def events(self, project, after, limit):
        pending = [e for e in self._events if e["id"] > after][:limit]
        if not pending:
            return [], after, False
        return pending, pending[-1]["id"], False


class _Args:
    after = None
    timeout = 0.4
    max_events = 50
    max_pages = 1


class TestWatchFollow(unittest.TestCase):
    @staticmethod
    def _records(buffer):
        return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]

    @staticmethod
    def _events(records):
        return [item for item in records if item.get("kind") != "meta"]

    def test_streams_one_json_per_line(self):
        from src.cli import _watch_follow

        store = _FakeWatchStore(
            [{"id": 1, "type": "task_created"}, {"id": 2, "type": "start_reserved"}]
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _watch_follow(store, _Args())
        records = self._records(buffer)
        events = self._events(records)
        self.assertEqual([event["type"] for event in events], ["task_created", "start_reserved"])
        self.assertEqual(events[1]["id"], 2)

    def test_meta_lines_bracket_the_stream_with_a_usable_cursor(self):
        """游标契约：起点一行、终点一行，宿主不必自己拼 ``epoch:id``。"""
        from src.cli import _watch_follow

        store = _FakeWatchStore([{"id": 1, "type": "task_created"}])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _watch_follow(store, _Args())
        records = self._records(buffer)
        self.assertEqual(records[0]["event"], "follow_start")
        self.assertEqual(records[0]["cursor"], "e1:0")
        self.assertEqual(records[-1]["event"], "follow_end")
        self.assertEqual(records[-1]["reason"], "timeout")
        # 下一轮 --after 可以直接使用末行的 next_cursor。
        self.assertEqual(records[-1]["next_cursor"], "e1:1")
        self.assertTrue(all(item["kind"] == "meta" for item in (records[0], records[-1])))

    def test_silence_emits_no_event_lines_but_still_closes_the_stream(self):
        from src.cli import _watch_follow

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _watch_follow(_FakeWatchStore([]), _Args())
        records = self._records(buffer)
        self.assertEqual(self._events(records), [])
        self.assertEqual(records[-1]["event"], "follow_end")
        self.assertEqual(records[-1]["cursor_status"], "ok")

    def test_integer_after_from_task_events_is_normalised(self):
        """``task events --after`` 是整数；不能因为缺 ``:`` 就崩在 ``in`` 上。"""
        from src.cli import _watch_follow

        store = _FakeWatchStore([{"id": 121, "type": "phase_update"}])
        args = _Args()
        args.after = 120
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _watch_follow(store, args)
        records = self._records(buffer)
        self.assertEqual(records[0]["cursor"], "e1:120")
        self.assertEqual([event["id"] for event in self._events(records)], [121])
        self.assertEqual(records[-1]["next_cursor"], "e1:121")

    def test_expired_cursor_is_reported_in_band(self):
        from src.cli import _watch_follow

        store = _FakeWatchStore([])
        args = _Args()
        args.after = "old-epoch:7"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _watch_follow(store, args)
        records = self._records(buffer)
        payload = records[-1]
        self.assertEqual(payload["cursor_status"], "cursor_expired")
        self.assertEqual(payload["next_cursor"], "e1:0")
        self.assertEqual(payload["reason"], "cursor_expired")


if __name__ == "__main__":
    unittest.main()
