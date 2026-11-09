"""v1.5.1：检查必须跑在**任务 worktree** 上，而不是项目仓库上。

这是"预检当执行用"和"cwd 指错"两类错误共用的负向验收（技能侧 §S4）：

* 让**原仓库通过、候选失败**的检查，最终结论必须是 `FAIL`；
* 反向的那一条（只有候选才有的文件）必须是 `PASS`，否则"全部失败"也能骗过前一条。

机制上由 `run_checks` 的解析 cwd 与 backend 的 `spawn(cwd=t["workspace"])` 共同保证，
这条用例把它们钉在一起：任何一处被改成仓库根，`FAIL` 那一条就会变绿。
"""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from src import _pipeline
from src._backend import create_backend
from src._util import git
from src.core import Store, atomic, create, validate_config


def commit_all(path: Path, message: str) -> None:
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "test")
    git(path, "config", "user.email", "test@localhost")
    git(path, "add", ".")
    git(path, "commit", "-m", message)


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "src.txt").write_text("initial\n")
        # 只有原仓库有的文件：候选 worktree 上没有它。
        (self.repo / "repo-only.txt").write_text("main branch only\n")
        commit_all(self.repo, "initial")

        self.store = Store(self.root / "state")
        self.addCleanup(self.store.db.close)
        self.spec = self.root / "spec.md"
        self.spec.write_text("# Test\nAC-001 implement")

        self.bin = self.root / "bin"
        self.bin.mkdir()
        stub = self.bin / "codebuddy"
        stub.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo "0.0.0-stub"; exit 0; fi\n'
            'echo \'{"type":"result","is_error":false,"result":"stub done"}\'\n'
        )
        stub.chmod(0o755)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.bin) + os.pathsep + self._old_path
        self.addCleanup(setattr, os.environ, "PATH", self._old_path)
        from src import _discovery

        _discovery.clear_cache()
        self.addCleanup(_discovery.clear_cache)

        self.config = validate_config(
            {
                "schema_version": 1,
                "project_id": "cwd-demo",
                "repo": str(self.repo),
                "backend": "local",
                "agent": "codebuddy",
                "checks": [{"id": "unit", "argv": ["true"]}],
                "allowed_paths": ["src.txt"],
                "authorization_ref": "scope-cwd",
                "authorized_actions": ["create", "execute"],
                "ci_required": False,
                "max_attempts": 1,
                "compatibility_verified": True,
            }
        )
        atomic(self.store.home / "projects/cwd-demo.json", self.config)

    def task_with_candidate(self, marker: str) -> tuple[dict, dict]:
        """建一个任务，并把 workspace 换成**只含 marker 文件**的候选目录。"""
        created = create(
            self.store,
            {
                "schema_version": 1,
                "request_id": "req-" + uuid.uuid4().hex[:16],
                "authorization_ref": "scope-cwd",
                "project_id": "cwd-demo",
                "title": "cwd",
                "spec_file": str(self.spec),
                "delivery_mode": "local_artifact",
            },
        )
        task = self.store.task(created["task_id"])
        workspace = Path(task["workspace"])
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "src.txt").write_text("initial\n")
        (workspace / marker).write_text("candidate only\n")
        commit_all(workspace, "initial")

        run_id = "run-" + uuid.uuid4().hex[:16]
        self.store.run({"run_id": run_id, "task_id": task["task_id"], "status": "running"})
        task.update(current_run_id=run_id, state="RUNNING")
        self.store.save(task)
        (self.store.path(task) / "runs" / run_id).mkdir(parents=True, exist_ok=True)
        return task, self.store.get_run(run_id)

    def run_one(self, task, run, argv):
        results, _errors = _pipeline.run_checks(
            create_backend(task["config"]),
            self.store,
            task,
            run,
            [{"id": "cwd-probe", "argv": argv}],
            None,
            "marker",
        )
        return results[0]


class TestChecksRunInTheCandidate(Fixture):
    def test_a_check_that_only_passes_in_the_repo_is_a_failure(self):
        task, run = self.task_with_candidate("candidate-only.txt")
        row = self.run_one(task, run, ["sh", "-c", "test -f repo-only.txt"])
        self.assertEqual(row["status"], "FAIL", row)

    def test_a_check_that_only_passes_in_the_candidate_passes(self):
        task, run = self.task_with_candidate("candidate-only.txt")
        row = self.run_one(task, run, ["sh", "-c", "test -f candidate-only.txt"])
        self.assertEqual(row["status"], "PASS", row)

    def test_the_repo_root_itself_would_have_passed(self):
        """前提校验：同一命令在原仓库上确实是通过的，否则上面的 FAIL 没意义。"""
        task, run = self.task_with_candidate("candidate-only.txt")
        row = self.run_checks_at_repo(task, run, ["sh", "-c", "test -f repo-only.txt"])
        self.assertEqual(row["status"], "PASS", row)

    def run_checks_at_repo(self, task, run, argv):
        """把 workspace 临时指回仓库：证明 FAIL 来自 cwd，不是命令本身跑不起来。"""
        original = task["workspace"]
        try:
            task["workspace"] = str(self.repo)
            return self.run_one(task, run, argv)
        finally:
            task["workspace"] = original


if __name__ == "__main__":
    unittest.main()
