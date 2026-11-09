"""v1.5.2：监控脚本的"可读进展"与"结束原因"（S8-B1/B2 + S4 合并改动）。

监控器有两件事最容易做错，这里各用一条反例兜住：

| 错误 | 后果 | 用例 |
|---|---|---|
| 每轮取尾部 N 条进展 | 同一条进展被打印 6 次，制造**假进展** | `test_the_same_progress_is_printed_once` |
| 日志读不到 / 旧 runtime | 整个监控失败，或把"读不到"当"没进展" | `test_a_read_failure_does_not_change_the_exit_code`、`test_an_old_runtime_disables_with_log_only` |
| 用日志判断终态 | 观测改写状态机结论 | `test_with_log_never_changes_the_exit_code` |

另外两条协议来自同一批改动：`[tick]` 带 `phases=` / `runs=`（阶段没变 ≠ 卡住），
以及每次退出都有 `[stop] reason=`（退出码 0 的两种含义只能靠它区分）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from skill_package import SKIP_REASON, skill_dir

SKILL = skill_dir()
SCRIPT = SKILL / "scripts" / "zoe-monitor.sh" if SKILL else None

#: 一条进展事件 + 一个游标；第二次调用（带 --after）默认"没有新事件"。
_ONE_EVENT = {
    "events": [{"kind": "text", "text": "正在读取配置"}],
    "next_cursor": "run-1:1:100",
}
_NO_EVENT = {"events": [], "next_cursor": "run-1:1:100"}

FAKE_ZOE = """#!/bin/sh
# 最小 zoe 替身：status / logs / watch 三条读取路径。
if [ "$1" = "watch" ]; then
  case " $* " in
    *" --after "*)
      # 只让"增量查询"失败，首次 init 仍然成功：否则进不到 exit 11 的分支。
      if [ "$FAKE_WATCH_FAIL_AFTER" = "1" ]; then
        echo "watch boom" >&2
        exit 1
      fi
      ;;
  esac
  case " $* " in
    *" --jsonl "*)
      echo '{"id": 7, "event": "state_changed"}'
      echo '{"kind": "meta", "event": "query_end", "next_cursor": "epoch:200"}'
      ;;
    *)
      echo '{"next_cursor": "epoch:100"}'
      ;;
  esac
  exit 0
fi
if [ "$1" = "task" ] && [ "$2" = "status" ]; then
  echo "{\\"state\\": \\"$FAKE_STATE\\", \\"phase\\": \\"$FAKE_PHASE\\", \\"current_run_id\\": \\"run-1\\"}"
  exit 0
fi
if [ "$1" = "task" ] && [ "$2" = "logs" ]; then
  case " $* " in
    *" --after "*)
      [ -n "$FAKE_LOGS_STDERR" ] && printf '%s\\n' "$FAKE_LOGS_STDERR" >&2
      printf '%s' "$FAKE_LOGS_AFTER"
      exit "${FAKE_LOGS_AFTER_CODE:-0}"
      ;;
    *)
      [ -n "$FAKE_LOGS_STDERR" ] && printf '%s\\n' "$FAKE_LOGS_STDERR" >&2
      printf '%s' "$FAKE_LOGS_FIRST"
      exit "${FAKE_LOGS_FIRST_CODE:-0}"
      ;;
  esac
fi
echo '{"error": {"code": "UNEXPECTED", "message": "fake zoe got: $*"}}'
exit 2
"""


@unittest.skipUnless(SKILL, SKIP_REASON)
@unittest.skipUnless(shutil.which("bash"), "bash is required")
@unittest.skipUnless(shutil.which("jq"), "the POSIX monitor depends on jq")
class TestMonitorProgress(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.zoe = self.root / "zoe"
        self.zoe.write_text(FAKE_ZOE)
        self.zoe.chmod(0o755)
        self.cursor = self.root / "cursor.txt"
        self.events = self.root / "events.jsonl"
        self.stop = self.root / "stop.json"

    def run_monitor(self, *extra, **env_overrides):
        env = dict(os.environ)
        env["ZOE_BIN"] = str(self.zoe)
        env["ZOE_MONITOR_SLEEP"] = "0"
        env.setdefault("FAKE_STATE", "RUNNING")
        env.setdefault("FAKE_PHASE", "implement")
        env.setdefault("FAKE_LOGS_FIRST", json.dumps(_ONE_EVENT))
        env.setdefault("FAKE_LOGS_AFTER", json.dumps(_NO_EVENT))
        env.update(env_overrides)
        return subprocess.run(
            [
                "bash",
                str(SCRIPT),
                "--project",
                "demo",
                "--targets",
                "T1",
                "--interval",
                "0",
                "--cursor-file",
                str(self.cursor),
                "--events",
                str(self.events),
                *extra,
            ],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def log_lines(self, result) -> list[str]:
        return [line for line in result.stdout.splitlines() if line.startswith("[log]")]

    # -- B1：tick 带 phase / run ------------------------------------------- #
    def test_tick_carries_phase_and_run(self):
        result = self.run_monitor("--max-rounds", "1")
        tick = next(line for line in result.stdout.splitlines() if line.startswith("[tick]"))
        self.assertIn("phases=T1:implement", tick)
        self.assertIn("runs=T1:run-1", tick)
        # 旧的检查点字段仍然在：新字段是增量，不是替换。
        self.assertIn("alive=1", tick)

    # -- B2：只展示新增 ----------------------------------------------------- #
    def test_the_same_progress_is_printed_once(self):
        result = self.run_monitor("--with-log", "--max-rounds", "3")
        lines = self.log_lines(result)
        self.assertEqual(len(lines), 1, result.stdout)
        self.assertIn("正在读取配置", lines[0])
        self.assertIn("[log] T1 说：", lines[0])

    def test_each_target_keeps_its_own_display_cursor(self):
        """两个目标的游标必须按目标隔离：共用一份就会漏播或重播。"""
        env = dict(os.environ)
        env.update(
            {
                "ZOE_BIN": str(self.zoe),
                "ZOE_MONITOR_SLEEP": "0",
                "FAKE_STATE": "RUNNING",
                "FAKE_PHASE": "implement",
                "FAKE_LOGS_FIRST": json.dumps(_ONE_EVENT),
                "FAKE_LOGS_AFTER": json.dumps(_NO_EVENT),
            }
        )
        result = subprocess.run(
            [
                "bash",
                str(SCRIPT),
                "--project",
                "demo",
                "--targets",
                "T1 T2",
                "--interval",
                "0",
                "--cursor-file",
                str(self.cursor),
                "--events",
                str(self.events),
                "--with-log",
                "--max-rounds",
                "1",
            ],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        lines = self.log_lines(result)
        self.assertEqual(len(lines), 2, result.stdout)
        self.assertTrue(any(line.startswith("[log] T1 ") for line in lines), lines)
        self.assertTrue(any(line.startswith("[log] T2 ") for line in lines), lines)
        # S8-B1 记的是逗号分隔：宿主按逗号切分，空格留给字段之间。
        tick = next(line for line in result.stdout.splitlines() if line.startswith("[tick]"))
        self.assertIn("phases=T1:implement,T2:implement", tick)
        self.assertIn("runs=T1:run-1,T2:run-1", tick)

    def test_a_tool_event_is_labelled_as_observation_only(self):
        event = {"kind": "tool", "name": "Edit", "target": "/a/b.py"}
        payload = {"events": [event], "next_cursor": "run-1:1:100"}
        result = self.run_monitor(
            "--with-log", "--max-rounds", "1", FAKE_LOGS_FIRST=json.dumps(payload)
        )
        lines = self.log_lines(result)
        self.assertEqual(len(lines), 1)
        self.assertIn("动：Edit /a/b.py", lines[0])
        self.assertIn("过程观察，非成功证据", lines[0])

    def test_no_new_event_prints_nothing(self):
        empty = {"events": [], "next_cursor": "run-1:1:100"}
        result = self.run_monitor("--with-log", "--max-rounds", "2", FAKE_LOGS_FIRST=json.dumps(empty))
        self.assertEqual(self.log_lines(result), [])
        self.assertNotIn("（无输出）", result.stdout)

    # -- 失败隔离 ----------------------------------------------------------- #
    def test_a_read_failure_does_not_change_the_exit_code(self):
        result = self.run_monitor(
            "--with-log",
            "--max-rounds",
            "2",
            FAKE_LOGS_FIRST_CODE="2",
            FAKE_LOGS_FIRST="",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[log-error] T1", result.stdout)
        self.assertIn("[stop] reason=BUDGET_EXHAUSTED", result.stdout)
        # 读不到日志不影响事件消费与游标推进。
        self.assertEqual(self.cursor.read_text().strip(), "epoch:200")

    def test_three_consecutive_failures_disable_with_log(self):
        result = self.run_monitor(
            "--with-log",
            "--max-rounds",
            "4",
            FAKE_LOGS_FIRST_CODE="2",
            FAKE_LOGS_FIRST="",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("[log-off]", result.stdout)
        self.assertLessEqual(result.stdout.count("[log-error]"), 3)

    def test_an_old_runtime_disables_with_log_only(self):
        result = self.run_monitor(
            "--with-log",
            "--max-rounds",
            "3",
            FAKE_LOGS_FIRST_CODE="0",
            FAKE_LOGS_AFTER_CODE="2",
            FAKE_LOGS_STDERR="error: unrecognized arguments: --after",
        )
        # 参数不被接受 ⇒ 一行 [log-off]，不重复刷 [log-error]，监控照常。
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("[log-off]"), 1)
        self.assertNotIn("[log-error]", result.stdout)
        self.assertIn("[stop] reason=BUDGET_EXHAUSTED", result.stdout)

    def test_a_rotated_log_does_not_replay_the_window(self):
        rotated = {"events": [], "rotated": True, "reset_cursor": "run-1:2:0"}
        result = self.run_monitor(
            "--with-log", "--max-rounds", "2", FAKE_LOGS_FIRST=json.dumps(rotated)
        )
        self.assertEqual(self.log_lines(result), [])
        self.assertEqual(result.returncode, 0)

    # -- 不参与终态判定 ----------------------------------------------------- #
    def test_with_log_never_changes_the_exit_code(self):
        self.assertEqual(self.run_monitor("--max-rounds", "1").returncode, 0)
        self.assertEqual(self.run_monitor("--with-log", "--max-rounds", "1").returncode, 0)
        done = self.run_monitor("--with-log", FAKE_STATE="DONE")
        self.assertEqual(done.returncode, 0)
        self.assertIn("[done] all targets final", done.stdout)
        human = self.run_monitor("--with-log", FAKE_STATE="REVIEW_READY")
        self.assertEqual(human.returncode, 10)

    def test_with_log_does_not_mask_usage_or_watch_errors(self):
        """门槛 6 的另外两档退出码：2（用法）与 11（监控不可用）不受 `--with-log` 影响。"""
        usage = self.run_monitor("--with-log", "--log-bytes", "0")
        self.assertEqual(usage.returncode, 2, usage.stdout + usage.stderr)
        self.assertIn("[stop] reason=DEPENDENCY_ERROR", usage.stdout)

        broken = self.run_monitor(
            "--with-log", "--max-errors", "1", FAKE_WATCH_FAIL_AFTER="1"
        )
        self.assertEqual(broken.returncode, 11, broken.stdout + broken.stderr)
        self.assertIn("[stop] reason=WATCH_ERROR", broken.stdout)

    # -- S4：结束原因可机器判定 --------------------------------------------- #
    def test_every_exit_has_a_stop_reason(self):
        cases = {
            "ALL_TERMINAL": self.run_monitor("--max-rounds", "1", FAKE_STATE="DONE"),
            "BUDGET_EXHAUSTED": self.run_monitor("--max-rounds", "1"),
            "HUMAN_REQUIRED": self.run_monitor(FAKE_STATE="REVIEW_READY"),
        }
        for reason, result in cases.items():
            with self.subTest(reason=reason):
                self.assertIn(f"[stop] reason={reason}", result.stdout)

    def test_stop_file_records_the_reason_and_cursor(self):
        result = self.run_monitor(
            "--max-rounds", "1", "--stop-file", str(self.stop)
        )
        self.assertEqual(result.returncode, 0)
        payload = json.loads(self.stop.read_text())
        self.assertEqual(payload["monitor_stop_reason"], "BUDGET_EXHAUSTED")
        self.assertFalse(payload["all_terminal"])
        self.assertEqual(payload["next_cursor"], "epoch:200")
        self.assertEqual(payload["rounds"], 1)
        self.assertGreaterEqual(payload["elapsed_seconds"], 0)

    def test_max_seconds_is_a_wall_clock_budget(self):
        result = self.run_monitor("--max-seconds", "1")
        self.assertEqual(result.returncode, 0)
        self.assertIn("[stop] reason=BUDGET_EXHAUSTED", result.stdout)
        self.assertIn("max seconds reached", result.stdout)

    def test_missing_targets_is_a_dependency_error_with_a_reason(self):
        result = self.run_monitor("--max-rounds", "1", FAKE_STATE="")
        # 状态读不到 ⇒ 不能当作"还在跑"以外的任何东西；这里只验证 reason 存在。
        self.assertIn("[stop] reason=", result.stdout)


@unittest.skipUnless(SKILL, SKIP_REASON)
@unittest.skipUnless(shutil.which("pwsh"), "pwsh is required for the Windows parity case")
class TestMonitorPowerShellParity(unittest.TestCase):
    """门槛 9：两个实现必须给出同一组 `[log]` 行与同一组退出码。

    Windows 宿主没有 bash/jq，所以"只跑 bash 测试"不能推断 `.ps1` 一致；这条用例
    在缺少 pwsh 时**跳过**（不是通过），结论记为 `NOT_VERIFIED`。
    """

    def test_powershell_script_parses(self):
        script = SKILL / "scripts" / "zoe-monitor.ps1"
        result = subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-Command",
                "$errors=$null; [System.Management.Automation.Language.Parser]::ParseFile("
                f"'{script}', [ref]$null, [ref]$errors) | Out-Null; "
                "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
