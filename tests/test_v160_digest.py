"""v1.6: 跨任务事实聚合、任务记录投影、派发前基线校验。

三件事分开验，因为它们回答三个不同的问题：

* ``base_reachable`` —— "这棵冻结的树还在目标分支上吗"（True/False/None 三态，
  ``None`` 表示判定不了，必须与"漂移"分开）；
* ``_hot_paths`` —— "本批有几个候选动同一个文件"（只看声明了 task_scope 的任务，
  否则"沿用项目范围"会被算成"所有任务都重叠"）；
* ``_project_task_rows`` —— "字段名拼错了吗"（拼错必须报错，不能静默返回 null）。

全量端到端（真任务的 status + logs）在真实项目上人工验收，不放进单测：那需要
一个跑过的 run，构造它的成本远高于它证明的东西。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src import _digest, _worktree
from src.cli import _project_task_rows
from src._util import Error


def _git(repo: Path, *args: str) -> str:
    """Run git in a throwaway repo without depending on the ambient identity."""
    proc = subprocess.run(
        ["git", "-c", "user.name=zoe", "-c", "user.email=zoe@example.com", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return proc.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


class BaseReachable(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-b", "main")
        self.base = _commit(self.repo, "a.txt")

    def tearDown(self):
        self._tmp.cleanup()

    def test_ancestor_is_reachable(self):
        # 分支只是前进了：base 仍是祖先，这是正常状态，不是漂移。
        _commit(self.repo, "b.txt")
        self.assertIs(_worktree.base_reachable(self.repo, self.base, "main"), True)

    def test_rewritten_history_is_drift(self):
        # 孤儿提交不在 main 的历史里：等价于 rebase / force-push 之后基线被丢弃。
        _git(self.repo, "checkout", "--orphan", "other")
        orphan = _commit(self.repo, "c.txt")
        _git(self.repo, "checkout", "main")
        self.assertIs(_worktree.base_reachable(self.repo, orphan, "main"), False)

    def test_missing_object_is_drift(self):
        # 仓库是好的，但对象没了（被 gc 回收/强推后不可达）——基线确实丢了。
        self.assertIs(_worktree.base_reachable(self.repo, "a" * 40, "main"), False)

    def test_unreadable_repo_is_undecidable(self):
        # 判定不了不等于漂移：返回 None，调用方不得据此拒绝派发。
        self.assertIsNone(_worktree.base_reachable(self.repo / "nope", self.base, "main"))

    def test_empty_base_is_undecidable(self):
        self.assertIsNone(_worktree.base_reachable(self.repo, None, "main"))


class HotPaths(unittest.TestCase):
    def test_counts_only_declared_scope(self):
        rows = [
            {"task_id": "T1", "scope": ["a.py", "b.py"]},
            {"task_id": "T2", "scope": ["b.py"]},
            # task_scope=null 表示"沿用项目范围"：算进来会让所有任务看起来都重叠。
            {"task_id": "T3", "scope": None},
        ]
        self.assertEqual(
            _digest._hot_paths(rows),
            [{"path": "b.py", "tasks": ["T1", "T2"], "count": 2}],
        )

    def test_single_touch_is_not_hot(self):
        rows = [{"task_id": "T1", "scope": ["a.py"]}, {"task_id": "T2", "scope": ["b.py"]}]
        self.assertEqual(_digest._hot_paths(rows), [])

    def test_sorted_by_count_then_path(self):
        rows = [
            {"task_id": "T1", "scope": ["x.py", "y.py"]},
            {"task_id": "T2", "scope": ["x.py", "y.py"]},
            {"task_id": "T3", "scope": ["y.py"]},
        ]
        hot = _digest._hot_paths(rows)
        self.assertEqual([item["path"] for item in hot], ["y.py", "x.py"])
        self.assertEqual(hot[0]["count"], 3)


class ListFields(unittest.TestCase):
    ROWS = [{"task_id": "T1", "state": "READY", "phase": "implement"}]

    def test_projects_in_requested_order(self):
        self.assertEqual(
            _project_task_rows(self.ROWS, "state,task_id"),
            [{"state": "READY", "task_id": "T1"}],
        )

    def test_without_fields_returns_rows_unchanged(self):
        self.assertEqual(_project_task_rows(self.ROWS, None), self.ROWS)
        self.assertEqual(_project_task_rows(self.ROWS, "  "), self.ROWS)

    def test_typo_is_an_error_not_a_null(self):
        with self.assertRaises(Error) as caught:
            _project_task_rows(self.ROWS, "task_id,stte")
        self.assertEqual(caught.exception.code, "INPUT_INCOMPLETE")
        self.assertEqual(caught.exception.details["unknown"], ["stte"])
        self.assertIn("state", caught.exception.details["available"])


class ContentItems(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        run = self.root / "runs" / "run-1"
        run.mkdir(parents=True)
        self.log = run / "agent.log"
        lines = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "a"}]}},
            # 同一条工具调用连续出现两次：去重前会渲染成两行"动"，制造"一直在动"。
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "b"}]}},
        ]
        self.log.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
        self.store = SimpleNamespace(path=lambda t: self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_dedups_and_keeps_newest(self):
        # 日志有 4 条记录、其中两条工具调用完全相同；去重后是 3 条，取最后 2 条。
        items, note = _digest._content_items(self.store, {"task_id": "T1"}, "run-1", 65536, 2)
        self.assertIsNone(note)
        self.assertEqual([item["kind"] for item in items], ["tool", "text"])
        self.assertEqual(items[-1]["text"], "b")
        # 工具目标取命令的第一个 token，不把整个 heredoc 塞进一行。
        self.assertEqual(items[0]["target"], "pytest")

    def test_missing_log_reports_note(self):
        items, note = _digest._content_items(self.store, {"task_id": "T1"}, "run-nope", 65536, 2)
        self.assertEqual(items, [])
        self.assertEqual(note, "no_agent_log")

    def test_no_content_does_not_read_logs(self):
        store = SimpleNamespace(path=lambda t: self.root, get_run=lambda rid: None)
        row = _digest._task_row(
            store, {"task_id": "T1", "current_run_id": "run-1"}, 1000.0, 65536, 2, False
        )
        self.assertEqual(row["content"], [])
        self.assertEqual(row["content_note"], "content_skipped")


class TargetSelection(unittest.TestCase):
    def test_cross_project_target_is_refused(self):
        tasks = {
            "T1": {"task_id": "T1", "project_id": "other", "state": "RUNNING"},
        }
        store = SimpleNamespace(
            task=lambda tid: tasks[tid],
            tasks=lambda: list(tasks.values()),
            path=lambda t: Path("/nonexistent"),
        )
        with self.assertRaises(Error) as caught:
            _digest.digest(store, project_id="mine", targets=["T1"], content=False)
        self.assertEqual(caught.exception.code, "PROJECT_MISMATCH")

    def test_defaults_skip_terminal_states(self):
        tasks = [
            {"task_id": "T1", "project_id": "mine", "state": "DONE", "current_run_id": None},
            {"task_id": "T2", "project_id": "mine", "state": "RUNNING", "current_run_id": None},
        ]
        store = SimpleNamespace(
            task=lambda tid: next(t for t in tasks if t["task_id"] == tid),
            tasks=lambda: tasks,
            get_run=lambda rid: None,
            path=lambda t: Path("/nonexistent"),
        )
        result = _digest.digest(store, project_id="mine", content=False)
        self.assertEqual([row["task_id"] for row in result["tasks"]], ["T2"])
        # 事实聚合不产出结论：结果里没有"完成/失败"这类判定字段。
        self.assertNotIn("verdict", result)
        self.assertEqual(result["note"].split(":")[0], "facts only")


if __name__ == "__main__":
    unittest.main()
