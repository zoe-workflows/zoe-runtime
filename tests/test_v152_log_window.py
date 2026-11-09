"""v1.5.2：日志读取面的可读化（快照有界读 / 字节定界 / `--after` 增量）。

这里断言的是**读取行为**，不是实现形状。评审给的 11 条门槛逐条对应一个用例：

| 门槛 | 用例 |
|---|---|
| 1 不带 `--tail-bytes` 的 `--follow` 仍从 EOF 开始 | `test_follow_without_a_window_still_starts_at_eof` |
| 2 显式窗口时首条输出是完整记录 | `test_follow_with_a_window_starts_on_a_record` |
| 3 快照与 follow 对同一区间一致 | `test_snapshot_and_follow_agree_on_same_range` |
| 4 尾部半行不会被游标越过 | `test_cursor_never_crosses_incomplete_record` |
| 5 大文件读取有上界 | `test_snapshot_read_is_bounded` |
| 6 `--with-log` 不重复、不改退出码 | 见 `tests/test_v152_monitor_progress.py` |
| 7 回绕/截断有明确响应 | `test_after_offset_beyond_size_reports_rotated` |
| 11 新选项对外契约自洽 | `TestAfterContract` 各例 |

一致性的适用边界（§6.3）：单条记录 ≤ 64 KiB。超长行时 follow 会切成"截断片段 +
后续片段"，快照按物理行处理，两者不可比——这里不把那种情形当作反例。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import _logstream
from src.cli import _build_parser, _logs_follow, _task_logs, _validate_logs_args
from src._util import Error

RUN = "run-1"


class _FakeStore:
    """Minimum surface ``_task_logs`` / ``_logs_follow`` touch."""

    def __init__(self, base, run=None):
        self._base = Path(base)
        self._task = {"task_id": "task-1", "project_id": "p1", "current_run_id": RUN}
        self._run = run or {"run_id": RUN, "status": "running", "adapter_id": "codebuddy"}

    def path(self, _task):
        return self._base

    def task(self, _task_id):
        return self._task

    def get_run(self, _run_id):
        return self._run


class _Args:
    id = "task-1"
    run = None
    log = "agent"
    projection = "auto"
    follow = False
    cursor = None
    after = None
    tail_bytes = None
    timeout = 0.3
    max_bytes = 0


def _args(**overrides) -> _Args:
    args = _Args()
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


class _LogCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.run_dir = self.root / "runs" / RUN
        self.run_dir.mkdir(parents=True)
        self.log = self.run_dir / "agent.log"
        self.store = _FakeStore(self.root)

    def write(self, data: bytes):
        self.log.write_bytes(data)
        return self.log

    def snapshot(self, **overrides) -> dict:
        return _task_logs(self.store, _args(**overrides))

    def follow_lines(self, **overrides) -> list[dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _logs_follow(self.store, _args(**overrides))
        return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# 1. 纯函数：窗口起点 / 完整记录边界                                          #
# --------------------------------------------------------------------------- #
class TestWindowHelpers(_LogCase):
    def test_window_start_keeps_current_line_when_already_aligned(self):
        """``size - window`` 恰在换行之后：保留该行，不能跳到下一行。"""
        self.write(b"aaa\nbbb\nccc\n")
        size = self.log.stat().st_size
        self.assertEqual(_logstream.window_start(self.log, size, 8), 4)

    def test_window_start_skips_partial_head_within_window(self):
        self.write(b"aaaa\nbbbb\n")
        size = self.log.stat().st_size
        # 10 - 6 = 4，落在 "aaaa" 的第二个字符上 ⇒ 跳过头部到 5。
        self.assertEqual(_logstream.window_start(self.log, size, 6), 5)

    def test_window_without_any_newline_returns_eof(self):
        """窗口内没有换行 ⇒ 空结果（EOF），不是"读全文件"。"""
        self.write(b"x" * 32)
        self.assertEqual(_logstream.window_start(self.log, 32, 8), 32)

    def test_window_larger_than_file_starts_at_zero(self):
        self.write(b"aa\nbb\n")
        self.assertEqual(_logstream.window_start(self.log, 6, 65536), 0)

    def test_complete_offset_is_the_end_of_the_last_complete_record(self):
        data = b"one\ntwo\npart"
        self.assertEqual(_logstream.complete_offset(data, 0), len(b"one\ntwo\n"))
        self.assertEqual(_logstream.complete_offset(data, 100), 108)

    def test_align_offset_moves_forward_never_back(self):
        self.write(b"aaaa\nbbbb\n")
        size = self.log.stat().st_size
        self.assertEqual(_logstream.align_offset(self.log, 5, size), 5, "already aligned")
        self.assertEqual(_logstream.align_offset(self.log, 7, size), 10, "forward to the boundary")
        self.assertEqual(_logstream.align_offset(self.log, 0, size), 0)


# --------------------------------------------------------------------------- #
# 2. 快照：完整记录、游标、边界                                                #
# --------------------------------------------------------------------------- #
class TestSnapshot(_LogCase):
    def test_partial_last_line_is_not_projected(self):
        record = json.dumps({"type": "result", "subtype": "success"}).encode()
        self.write(record + b"\n" + b'{"type":"res')
        payload = self.snapshot()
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["kind"], "result")
        # 半行只留在 text 里排障，不生成事件。
        self.assertIn('{"type":"res', payload["text"])

    def test_cursor_never_crosses_incomplete_record(self):
        record = json.dumps({"type": "result", "subtype": "success"}).encode()
        self.write(record + b"\n" + b'{"type":"res')
        first = self.snapshot()
        cursor = _logstream.parse_cursor(first["cursor"])
        self.assertEqual(cursor[2], len(record) + 1)
        self.assertLessEqual(cursor[2], len(self.log.read_bytes()))

        # 续读能补齐这条记录，且不重放已返回的那条。
        with open(self.log, "ab") as handle:
            handle.write(b'ult"}\n')
        second = self.snapshot(after=first["cursor"])
        self.assertEqual([event["kind"] for event in second["events"]], ["result"])

    def test_snapshot_reports_the_three_truncations_separately(self):
        lines = [json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 600}]}})]
        lines += [
            json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": f"m{index}"}]}}
            )
            for index in range(300)
        ]
        self.write(("\n".join(lines) + "\n").encode())

        payload = self.snapshot(tail_bytes=1_048_576)
        # 事件数量截断：保留最近的，不是最早的。
        self.assertTrue(payload["events_truncated"])
        self.assertEqual(len(payload["events"]), _logstream.MAX_SNAPSHOT_EVENTS)
        self.assertEqual(payload["events"][-1]["text"], "m299")
        # 字节窗口没有截断：三种截断互不冒充。
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["tail_start_offset"], 0)

    def test_a_long_text_marks_clipped_not_events_truncated(self):
        record = {"type": "assistant", "message": {"content": [{"type": "text", "text": "y" * 600}]}}
        self.write((json.dumps(record) + "\n").encode())
        payload = self.snapshot()
        self.assertFalse(payload["events_truncated"])
        self.assertTrue(payload["events"][0]["clipped"])
        self.assertLessEqual(len(payload["events"][0]["text"]), _logstream._TEXT_LIMIT)

    def test_blank_lines_emit_nothing_in_both_modes(self):
        self.write(b"a\n\n\nb\n")
        payload = self.snapshot()
        texts = [event["text"] for event in payload["events"]]
        self.assertEqual(texts, ["a", "b"])
        followed = [
            item["text"] for item in self.follow_lines(cursor=f"{RUN}:1:0") if item["kind"] != "meta"
        ]
        self.assertEqual(followed, ["a", "b"])

    def test_snapshot_read_is_bounded(self):
        """200 MiB 稀疏日志：只读窗口，绝不 ``read_bytes()``。"""
        size = 200 * 1024 * 1024
        head = json.dumps({"type": "system", "subtype": "init"}).encode() + b"\n"
        tail = b"".join(
            json.dumps({"type": "result", "subtype": f"r{index}"}).encode() + b"\n"
            for index in range(3)
        )
        with open(self.log, "wb") as handle:
            handle.write(head)
            handle.truncate(size)
            handle.seek(size - len(tail))
            handle.write(tail)

        requested = []
        original = _logstream.read_window

        def counting(path, start, length):
            requested.append((start, length))
            return original(path, start, length)

        def forbidden(*_args, **_kwargs):
            raise AssertionError("the snapshot must not read the whole log")

        with mock.patch.object(_logstream, "read_window", counting), mock.patch.object(
            Path, "read_bytes", forbidden
        ):
            payload = self.snapshot(tail_bytes=4096)

        self.assertEqual(payload["bytes"], size)
        self.assertEqual(len(requested), 1)
        self.assertLessEqual(requested[0][1], 4096, "the read must not exceed the window")
        self.assertTrue(payload["truncated"])
        # 窗口头部落在一行中间 ⇒ 该行被跳过；其后的完整记录仍然可用。
        self.assertEqual(
            [event["subtype"] for event in payload["events"]], ["r1", "r2"]
        )
        self.assertGreater(payload["tail_start_offset"], size - 4096)


# --------------------------------------------------------------------------- #
# 3. 一致性不变量：同一区间，快照 == follow                                    #
# --------------------------------------------------------------------------- #
class TestConsistencyInvariant(_LogCase):
    def test_snapshot_and_follow_agree_on_same_range(self):
        """同一 ``[a, b)``：``snapshot(a,b).events == follow(a→b).events`` 逐条相等。

        三个前提（§S8-D-4，缺一条这个等式就不成立，也不是反例）：

        1. **单条记录 ≤ 64 KiB**：超长行时 follow 会切成"截断片段 + 后续片段"，
           快照按物理行处理，两者本就不该相等；
        2. **端点落在完整记录边界**：半行不参与比较；
        3. **projection 相同**：`auto` / `raw` / `events` 分别比较，不混用。
        """
        records = [
            {"type": "system", "subtype": "init", "model": "stub"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "开始"}]}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/a/b.py"}}
                    ]
                },
            },
            {"type": "assistant", "message": {"content": []}},
            {"type": "result", "subtype": "success", "is_error": False},
        ]
        body = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
        self.write(("banner line\n" + body + "\n\n").encode())

        for projection in ("auto", "raw", "events"):
            with self.subTest(projection=projection):
                payload = self.snapshot(tail_bytes=1_048_576, projection=projection)
                followed = [
                    line
                    for line in self.follow_lines(projection=projection, cursor=f"{RUN}:1:0")
                    if line.get("kind") != "meta"
                ]
                self.assertEqual(payload["events"], followed)


# --------------------------------------------------------------------------- #
# 4. `--after` 对外契约                                                       #
# --------------------------------------------------------------------------- #
class TestAfterContract(_LogCase):
    def test_after_rejects_follow_combination(self):
        with self.assertRaises(Error) as caught:
            _validate_logs_args(_args(after=f"{RUN}:1:0", follow=True))
        self.assertEqual(caught.exception.code, "INPUT_INCOMPLETE")
        self.assertEqual(caught.exception.details["field"], "after")

    def test_after_rejects_a_malformed_cursor(self):
        with self.assertRaises(Error) as caught:
            _validate_logs_args(_args(after="4096"))
        self.assertEqual(caught.exception.details["field"], "after")

    def test_tail_bytes_is_validated_after_defaulting(self):
        with self.assertRaises(Error):
            _validate_logs_args(_args(tail_bytes=0))
        with self.assertRaises(Error):
            _validate_logs_args(_args(tail_bytes=_logstream.MAX_TAIL_BYTES + 1))
        _validate_logs_args(_args(tail_bytes=None))

    def test_after_cursor_run_mismatch_raises_project_mismatch(self):
        self.write(b'{"type":"result"}\n')
        with self.assertRaises(Error) as caught:
            self.snapshot(after="other-run:1:0")
        self.assertEqual(caught.exception.code, "PROJECT_MISMATCH")
        self.assertEqual(caught.exception.details["cursor_run"], "other-run")

    def test_after_offset_beyond_size_reports_rotated(self):
        self.write(b'{"type":"result"}\n')
        payload = self.snapshot(after=f"{RUN}:1:999999")
        self.assertTrue(payload["rotated"])
        self.assertEqual(payload["events"], [])
        self.assertEqual(payload["reset_cursor"], f"{RUN}:1:0")
        self.assertEqual(payload["cursor"], payload["reset_cursor"])
        self.assertFalse(payload["has_more"], "rotated is not a paging signal")

    def test_after_pages_until_has_more_is_false(self):
        records = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": f"m{index}"}]}}
            for index in range(12)
        ]
        self.write(("\n".join(json.dumps(record) for record in records) + "\n").encode())

        cursor = f"{RUN}:1:0"
        seen = []
        for _ in range(50):
            payload = self.snapshot(after=cursor, tail_bytes=64)
            seen.extend(payload["events"])
            self.assertTrue(payload["tail_start_offset"] >= 0)
            cursor = payload["next_cursor"]
            if not payload["has_more"]:
                break
        self.assertEqual([event["text"] for event in seen], [f"m{index}" for index in range(12)])
        self.assertFalse(payload["has_more"])

    def test_plain_snapshot_has_no_has_more(self):
        self.write(b'{"type":"result"}\n')
        self.assertNotIn("has_more", self.snapshot())


# --------------------------------------------------------------------------- #
# 5. follow：默认语义与显式窗口                                                #
# --------------------------------------------------------------------------- #
class TestFollowWindow(_LogCase):
    def test_follow_without_a_window_still_starts_at_eof(self):
        self.write(b'{"type":"result","subtype":"old"}\n')
        lines = self.follow_lines()
        self.assertEqual([line["kind"] for line in lines], ["meta"])
        # 起点就是 EOF：不传 --tail-bytes 时没有任何回看（门槛 1 的第一个半句）。
        self.assertEqual(
            _logstream.parse_cursor(lines[0]["cursor"])[2], self.log.stat().st_size
        )

    def test_follow_with_a_window_starts_on_a_record(self):
        head = b"x" * 200  # 没有换行的"半行"，只能被跳过
        record = json.dumps({"type": "result", "subtype": "success"}).encode()
        self.write(head + b"\n" + record + b"\n")
        lines = [
            line
            for line in self.follow_lines(tail_bytes=len(record) + 8)
            if line["kind"] != "meta"
        ]
        self.assertTrue(lines)
        self.assertEqual(lines[0]["kind"], "result")

    def test_follow_cursor_wins_over_tail_bytes(self):
        self.write(b'{"type":"result","subtype":"old"}\n')
        lines = [
            line
            for line in self.follow_lines(cursor=f"{RUN}:1:0", tail_bytes=1)
            if line["kind"] != "meta"
        ]
        self.assertEqual([line["kind"] for line in lines], ["result"])

    def test_follow_cursor_uses_the_progress_generation(self):
        self.write(b"")
        (self.run_dir / "progress.json").write_text(
            json.dumps({"run_id": RUN, "log_generation": 3, "bytes": 0}), encoding="utf-8"
        )
        meta = self.follow_lines()[0]
        self.assertEqual(_logstream.parse_cursor(meta["cursor"])[1], 3)


# --------------------------------------------------------------------------- #
# 6. parser 表面                                                              #
# --------------------------------------------------------------------------- #
class TestParserSurface(unittest.TestCase):
    def _logs(self):
        parser = _build_parser()
        for action in parser._actions:
            if action.dest == "cmd":
                return action.choices["task"]._subparsers._group_actions[0].choices["logs"]
        raise AssertionError("task logs parser not found")

    def test_tail_bytes_default_is_unspecified(self):
        parsed = self._logs().parse_args(["TASK"])
        self.assertIsNone(parsed.tail_bytes)
        self.assertIsNone(parsed.after)

    def test_after_help_states_the_follow_exclusion(self):
        action = next(a for a in self._logs()._actions if "--after" in a.option_strings)
        self.assertIn("run:gen:offset", action.help)
        self.assertIn("不能与 --follow 同用", action.help)
        # 门槛 11 要求与 §D-2-a 的 help 文案**逐字**一致：宿主的排障话术来自这里。
        self.assertEqual(
            action.help,
            "从该游标继续读快照（形状 run:gen:offset，与 --follow 的 cursor 同构；"
            "不能与 --follow 同用）",
        )


if __name__ == "__main__":
    unittest.main()
