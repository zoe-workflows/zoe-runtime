"""v1.5.1：监控脚本的退出码语义（技能侧 §S5 的验收反例 4）。

脚本的正常退出有**两种**含义：`[done] all targets final` 与 `[stop] max rounds reached`，
两者都返回 0。把后者读成"完成"是这份 skill 最贵的一类错误，所以这里用一个
`zoe` 替身把三种退出原因都跑一遍：

| 目标状态 | 期望 |
|---|---|
| RUNNING + `--max-rounds 1` | exit 0，末行是 `[stop] max rounds reached`，**没有** `[done]` |
| REVIEW_READY | exit 10（等人工验收，不是终态也不是故障） |
| DONE | exit 0，`[done] all targets final` |

顺带验证两条协议：游标在事件落盘**之后**才推进；事件文件不含 meta 行。
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

FAKE_ZOE = """#!/bin/sh
# 最小 zoe 替身：只实现本脚本用到的三条读取路径。
if [ "$1" = "watch" ]; then
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
  echo "{\\"state\\": \\"$FAKE_STATE\\"}"
  exit 0
fi
echo '{"error": {"code": "UNEXPECTED", "message": "fake zoe got: $*"}}'
exit 2
"""


@unittest.skipUnless(SKILL, SKIP_REASON)
@unittest.skipUnless(shutil.which("bash"), "bash is required")
@unittest.skipUnless(shutil.which("jq"), "the POSIX monitor depends on jq")
class TestMonitorExitCodes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.zoe = self.root / "zoe"
        self.zoe.write_text(FAKE_ZOE)
        self.zoe.chmod(0o755)
        self.cursor = self.root / "cursor.txt"
        self.events = self.root / "events.jsonl"

    def run_monitor(self, state: str, *extra: str):
        env = dict(os.environ)
        env["ZOE_BIN"] = str(self.zoe)
        env["ZOE_MONITOR_SLEEP"] = "0"
        env["FAKE_STATE"] = state
        result = subprocess.run(
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
            # 监控循环本身是"不限轮数"的；测试必须自己上闸，否则一个状态没被
            # 识别就会挂住整个测试进程（这正是脚本里 exit 10/11 要防的形状）。
            timeout=60,
        )
        return result

    def test_a_running_task_past_the_window_is_not_done(self):
        result = self.run_monitor("RUNNING", "--max-rounds", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[stop] max rounds reached", result.stdout)
        self.assertNotIn("[done] all targets final", result.stdout)
        # 窗口到期时游标仍然推进到本页末尾：续跑不会重放这一段。
        self.assertEqual(self.cursor.read_text().strip(), "epoch:200")

    def test_events_are_written_before_the_cursor_moves(self):
        self.run_monitor("RUNNING", "--max-rounds", "1")
        lines = [line for line in self.events.read_text().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(json.loads(lines[0])["id"], 7)
        # meta 行不落盘：它不是事件，混进去会让下游把它当状态变化。
        self.assertNotIn("query_end", self.events.read_text())

    def test_review_ready_stops_for_a_human_instead_of_reporting_done(self):
        result = self.run_monitor("REVIEW_READY")
        self.assertEqual(result.returncode, 10, result.stdout)
        self.assertIn("[needs-human] T1 REVIEW_READY", result.stdout)
        self.assertIn("[pause] remaining targets need a human", result.stdout)
        self.assertNotIn("[done]", result.stdout)

    def test_a_final_state_is_the_only_thing_that_says_done(self):
        result = self.run_monitor("DONE")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("[final] T1 DONE", result.stdout)
        self.assertIn("[done] all targets final", result.stdout)


if __name__ == "__main__":
    unittest.main()
