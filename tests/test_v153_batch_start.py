"""v1.5.3：批量派发脚本的失败分流（`scripts/zoe-batch-start.sh`）。

这条脚本存在的理由是**一个错误码一种药**，所以每条用例都在问"它对了吗"：

| 返回 | 正确动作 | 反例（会静默造成重复派发或掩盖配置错误） |
|---|---|---|
| `TASK_BUSY` | 同 id 同载荷重放 | 换新 id：把"锁被占"变成第二条派发路径 |
| `SLOT_BUSY` | 换新 id（旧 id 已被记为失败） | 同 id 重放：永远拿回那条失败记录 |
| `REQUEST_CONFLICT` | **不重试**，指出 id 被谁占用 | 自动换 id：把"文件配错任务"洗成成功 |
| `ENVIRONMENT_ERROR` 等 | 不重试，原样上报 | 重试三次浪费 6 秒并掩盖原因 |

另外两条是"输出可靠"：结果逐条落盘（不被 stdout 截断），以及 `start` 之后逐条
确认状态——`accepted` 只表示受理。
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
SCRIPT = SKILL / "scripts" / "zoe-batch-start.sh" if SKILL else None

#: 最小 zoe 替身：只实现 `task status` 与 `task start`。
#:
#: 每个请求文件里的 ``request_id`` 都会被记到 ``$FAKE_CALLS``，测试据此判断
#: "是重放了同一份请求，还是偷偷换了一个新 id"——这是本文件的核心判据。
FAKE_ZOE = """#!/bin/sh
if [ "$1" = "task" ] && [ "$2" = "status" ]; then
  task=$3
  case " ${FAKE_INPUT_REQUIRED:-} " in
    *" $task "*) echo '{"state": "BLOCKED", "block_code": "INPUT_REQUIRED", "row_version": 3,
      "config": {"authorization_ref": "auth-demo"}}'; exit 0 ;;
  esac
  case " ${FAKE_REVIEW_READY:-} " in
    *" $task "*) echo '{"state": "REVIEW_READY", "block_code": null, "row_version": 5,
      "config": {"authorization_ref": "auth-demo"}}'; exit 0 ;;
  esac
  echo '{"state": "READY", "block_code": null, "row_version": 4,
    "config": {"authorization_ref": "auth-demo"}}'
  exit 0
fi
if [ "$1" = "task" ] && [ "$2" = "start" ]; then
  file=""
  while [ $# -gt 0 ]; do
    [ "$1" = "--request-file" ] && file=$2
    shift
  done
  rid=$(sed -n 's/.*"request_id": *"\\([^"]*\\)".*/\\1/p' "$file")
  printf '%s\\n' "$rid" >> "$FAKE_CALLS"
  case "${FAKE_MODE:-ok}" in
    ok)
      echo '{"accepted": true, "dispatch_pending": false, "run_id": "run-1"}'; exit 0 ;;
    busy_once)
      if [ ! -f "$FAKE_BUSY_MARK" ]; then
        : > "$FAKE_BUSY_MARK"
        echo '{"error": {"code": "TASK_BUSY", "message": "Task writer lock is held"}}'
        exit 3
      fi
      echo '{"accepted": true, "dispatch_pending": false, "run_id": "run-1"}'; exit 0 ;;
    slot)
      echo '{"error": {"code": "SLOT_BUSY", "message": "2/2 active runs"}}'; exit 3 ;;
    conflict)
      echo '{"error": {"code": "REQUEST_CONFLICT",
        "message": "Request ID reused with different payload",
        "details": {"request_id": "x",
        "existing": {"task_id": "task-other", "action": "start", "status": "succeeded"}}}}'
      exit 3 ;;
    env_error)
      echo '{"error": {"code": "ENVIRONMENT_ERROR", "message": "doctor reports not ready"}}'
      exit 4 ;;
    *)
      echo '{"error": {"code": "UNEXPECTED", "message": "unknown FAKE_MODE"}}'; exit 2 ;;
  esac
fi
echo '{"error": {"code": "UNEXPECTED", "message": "fake zoe got: $*"}}'
exit 2
"""


@unittest.skipUnless(SKILL, SKIP_REASON)
@unittest.skipUnless(shutil.which("bash"), "bash is required")
@unittest.skipUnless(shutil.which("jq"), "the POSIX batch script depends on jq")
class TestBatchStart(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.zoe = self.root / "zoe"
        self.zoe.write_text(FAKE_ZOE)
        self.zoe.chmod(0o755)
        self.calls = self.root / "calls.txt"
        self.calls.write_text("")
        self.out = self.root / "out"
        self.plan = self.root / "plan.json"

    def write_plan(self, items, *, as_array=False):
        payload = items if as_array else {"items": items}
        self.plan.write_text(json.dumps(payload), encoding="utf-8")
        return self.plan

    def run_batch(self, *extra, **env_overrides):
        env = dict(os.environ)
        env.update(
            {
                "ZOE_BIN": str(self.zoe),
                "FAKE_CALLS": str(self.calls),
                "FAKE_BUSY_MARK": str(self.root / "busy.done"),
                "FAKE_MODE": "ok",
            }
        )
        env.update(env_overrides)
        return subprocess.run(
            [
                "bash",
                str(SCRIPT),
                "--project",
                "demo",
                "--plan",
                str(self.plan),
                "--out",
                str(self.out),
                *extra,
            ],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def results(self):
        path = self.out / "results.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def request_ids(self):
        return [line for line in self.calls.read_text().splitlines() if line.strip()]

    # -- 正常路径 ----------------------------------------------------------- #

    def test_one_line_of_json_per_task_and_all_dispatched(self):
        self.write_plan(
            [{"task_id": "task-a", "label": "T9a"}, {"task_id": "task-b", "label": "T9b"}]
        )
        result = self.run_batch("--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.results()
        self.assertEqual([row["label"] for row in rows], ["T9a", "T9b"])
        self.assertEqual({row["outcome"] for row in rows}, {"dispatched"})
        # 逐条 stdout 也是 NDJSON：宿主不会只看到最后一条。
        ndjson = [line for line in result.stdout.splitlines() if line.startswith("{")]
        self.assertEqual(len(ndjson), 2)

    def test_the_manifest_binds_each_request_file_to_its_task(self):
        self.write_plan([{"task_id": "task-a", "label": "T9a"}])
        self.run_batch()
        rows = [
            json.loads(line)
            for line in (self.out / "manifest.jsonl").read_text().splitlines()
            if line.strip()
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], "task-a")
        self.assertIn("task-a", rows[0]["request_file"])

    def test_a_bare_array_plan_is_accepted(self):
        self.write_plan([{"task_id": "task-a"}], as_array=True)
        self.assertEqual(self.run_batch().returncode, 0)
        self.assertEqual(len(self.results()), 1)

    def test_dispatch_pending_is_not_reported_as_dispatched(self):
        self.write_plan([{"task_id": "task-a", "label": "T9a"}])
        # 让替身只回 dispatch_pending：这是"受理但没确认起进程"，
        # 与"已派发"是两件事，报告里不能合并。
        fake = FAKE_ZOE.replace(
            'echo \'{"accepted": true, "dispatch_pending": false, "run_id": "run-1"}\'; exit 0 ;;',
            'echo \'{"accepted": true, "dispatch_pending": true, "run_id": null}\'; exit 0 ;;',
        )
        self.zoe.write_text(fake)
        self.zoe.chmod(0o755)
        self.run_batch()
        self.assertEqual(self.results()[0]["outcome"], "dispatch_pending")

    # -- 失败分流：本文件的核心 --------------------------------------------- #

    def test_task_busy_replays_the_same_request_id(self):
        """锁被占时换 id 会制造第二条派发路径；正确动作是原样重放。"""
        self.write_plan([{"task_id": "task-a", "label": "T9a"}])
        result = self.run_batch(FAKE_MODE="busy_once")
        self.assertEqual(result.returncode, 0, result.stdout)
        ids = self.request_ids()
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(self.results()[0]["outcome"], "dispatched")

    def test_slot_busy_is_not_retried_without_a_budget(self):
        self.write_plan([{"task_id": "task-a", "label": "T9a"}])
        result = self.run_batch(FAKE_MODE="slot")
        self.assertEqual(result.returncode, 10)
        self.assertEqual(len(self.request_ids()), 1)
        row = self.results()[0]
        self.assertEqual(row["outcome"], "slot_busy")
        self.assertEqual(row["error_code"], "SLOT_BUSY")

    def test_request_conflict_is_never_retried_and_names_the_owner(self):
        """这条反例最贵：自动换 id 会把"文件配错任务"洗成成功。"""
        self.write_plan([{"task_id": "task-a", "label": "T9a"}])
        result = self.run_batch(FAKE_MODE="conflict")
        self.assertEqual(result.returncode, 10)
        self.assertEqual(len(self.request_ids()), 1, "conflict must not be retried")
        row = self.results()[0]
        self.assertEqual(row["outcome"], "conflict")
        self.assertIn("task-other", row["detail"])

    def test_environment_errors_are_reported_without_retry(self):
        self.write_plan([{"task_id": "task-a", "label": "T9a"}])
        result = self.run_batch(FAKE_MODE="env_error")
        self.assertEqual(result.returncode, 10)
        self.assertEqual(len(self.request_ids()), 1)
        row = self.results()[0]
        self.assertEqual(row["outcome"], "failed")
        self.assertEqual(row["error_code"], "ENVIRONMENT_ERROR")

    # -- 派发前闸门 --------------------------------------------------------- #

    def test_a_review_ready_task_is_not_started(self):
        self.write_plan([{"task_id": "task-a", "label": "T9a"}])
        result = self.run_batch(FAKE_REVIEW_READY="task-a")
        self.assertEqual(result.returncode, 10)
        self.assertEqual(self.request_ids(), [])
        self.assertEqual(self.results()[0]["outcome"], "skipped")

    def test_a_task_waiting_for_an_answer_is_not_started(self):
        """INPUT_REQUIRED 时派发会让 agent 在没有答案的情况下重跑一遍。"""
        self.write_plan([{"task_id": "task-a", "label": "T9a"}])
        result = self.run_batch(FAKE_INPUT_REQUIRED="task-a")
        self.assertEqual(result.returncode, 10)
        self.assertEqual(self.request_ids(), [])
        self.assertEqual(self.results()[0]["error_code"], "INPUT_REQUIRED")

    # -- 用法 --------------------------------------------------------------- #

    def test_an_empty_plan_is_a_usage_error(self):
        self.write_plan([])
        self.assertEqual(self.run_batch().returncode, 2)

    def test_a_missing_task_id_is_reported_not_dispatched(self):
        self.write_plan([{"label": "T9a"}])
        result = self.run_batch()
        self.assertEqual(result.returncode, 10)
        self.assertEqual(self.request_ids(), [])


if __name__ == "__main__":
    unittest.main()
