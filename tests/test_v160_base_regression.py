"""v1.6: 进入 REVIEW_READY 时记下"目标分支在基线之后做了什么"。

现场复现的问题（一次并行批次）：宿主在评审前手跑 ``git diff --stat HEAD..<cand>``，
看到候选里"删除已合入代码"，于是 ``merge --abort`` + 重派。三次重派里没有一次是代码
问题——真实事实是**候选落后于目标分支**，两-dot diff 把"别人新合入的文件"显示成了
"这个候选要删掉的文件的负像"。

判据因此必须是三个不同的事实，而不是一个布尔：

* ``behind_by``：目标分支前进了几个提交；
* ``overlapping_paths``：前进的改动与候选落在同一批文件上 ⇒ 合并后**必须重新验证**；
* ``reverts_merged_work``：候选删掉了基线之后被别人改过的文件 ⇒ 合并会丢掉那些改动。

三者都不改状态：并行批次里"落后"是常态，把它判成失败只会逼出 abort+重派。
"""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from test_runtime import FakePublisher

from src import _baseline
from src._util import atomic, git
from src.core import Store, create, run_pipeline

CHECKS = [{"id": "lint", "argv": ["true"]}]


class Backend:
    def __init__(self, write=(), delete=()):
        self.calls = []
        self.write = list(write)
        self.delete = list(delete)

    def inspect(self, name):
        return None

    def execute(self, store, t, r, argv, log_name, worker=False, prompt=None):
        self.calls.append(log_name)
        if worker:
            workspace = Path(t["workspace"])
            for name in self.write:
                target = workspace / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("changed by the agent\n")
            for name in self.delete:
                (workspace / name).unlink(missing_ok=True)
            return 0
        return 0


class Fixture(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "test")
        git(self.repo, "config", "user.email", "test@localhost")
        (self.repo / "src.txt").write_text("initial\n")
        (self.repo / "shared.py").write_text("shared = 1\n")
        (self.repo / "legacy.py").write_text("legacy = 1\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        self.store = Store(self.root / "state")
        self.spec = self.root / "spec.json"
        self.spec.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "title": "Deliver src",
                    "allowed_paths": ["src.txt", "shared.py", "legacy.py"],
                    "acceptance_criteria": [
                        {
                            "id": "AC-1",
                            "statement": "implement src",
                            "verification": {"type": "check", "check_id": "lint"},
                        }
                    ],
                }
            )
        )
        atomic(
            self.store.home / "projects/demo.json",
            {
                "schema_version": 1,
                "project_id": "demo",
                "repo": str(self.repo),
                "backend": "local-process",
                "network": "none",
                "compatibility_verified": True,
                "agent": "codebuddy",
                "checks": [dict(check) for check in CHECKS],
                "allowed_paths": ["src.txt", "shared.py", "legacy.py"],
                "authorization_ref": "scope-1",
                "authorized_actions": ["create", "execute", "publish", "accept"],
                "ci_required": False,
                "max_attempts": 3,
            },
        )

    def tearDown(self):
        self.store.db.close()
        self._td.cleanup()

    # ---------------------------------------------------------------- helpers

    def new_task(self):
        result = create(
            self.store,
            dict(
                schema_version=1,
                request_id="req-" + uuid.uuid4().hex,
                authorization_ref="scope-1",
                project_id="demo",
                title="Deliver src",
                spec_file=str(self.spec),
                delivery_mode="local_artifact",
            ),
        )
        return self.store.task(result["task_id"])

    def commit_on_main(self, name, text):
        target = self.repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", f"another task: {name}")

    def run_task(self, t, backend):
        t = self.store.task(t["task_id"])
        rid = "run-" + uuid.uuid4().hex[:16]
        self.store.run({"run_id": rid, "task_id": t["task_id"], "status": "running"})
        t.update(current_run_id=rid, state="RUNNING")
        self.store.save(t)
        return run_pipeline(self.store, t["task_id"], backend, FakePublisher())


class TestConcurrentBase(Fixture):
    def test_overlap_is_reported_after_another_task_touches_the_same_file(self):
        task = self.new_task()
        # 另一个任务在 main 上改了同一个文件（这就是并行批次的常态）。
        self.commit_on_main("shared.py", "shared = 2  # from another task\n")
        backend = Backend(write=["src.txt", "shared.py"])
        task = self.run_task(task, backend)

        self.assertEqual(task["state"], "REVIEW_READY")
        view = task["concurrent_base"]
        self.assertEqual(view["kind"], _baseline.KIND_OVERLAP)
        self.assertEqual(view["behind_by"], 1)
        self.assertEqual(view["overlapping_paths"], ["shared.py"])
        self.assertEqual(view["reverts_merged_work"], [])
        self.assertTrue(view["message"])

    def test_unrelated_advance_is_not_reported_as_overlap(self):
        task = self.new_task()
        self.commit_on_main("unrelated.py", "other = 1\n")
        task = self.run_task(task, Backend(write=["src.txt"]))

        view = task["concurrent_base"]
        self.assertEqual(view["kind"], _baseline.KIND_BASE_ADVANCED)
        self.assertEqual(view["behind_by"], 1)
        self.assertEqual(view["overlapping_paths"], [])

    def test_no_advance_is_none_and_does_not_block(self):
        task = self.run_task(self.new_task(), Backend(write=["src.txt"]))
        view = task["concurrent_base"]
        self.assertEqual(view["kind"], _baseline.KIND_NONE)
        self.assertEqual(view["behind_by"], 0)
        self.assertEqual(task["state"], "REVIEW_READY")

    def test_deleting_a_file_someone_else_just_changed_is_flagged(self):
        task = self.new_task()
        self.commit_on_main("legacy.py", "legacy = 2  # fixed by another task\n")
        task = self.run_task(task, Backend(write=["src.txt"], delete=["legacy.py"]))

        view = task["concurrent_base"]
        self.assertEqual(view["kind"], _baseline.KIND_REVERTS)
        self.assertEqual(view["reverts_merged_work"], ["legacy.py"])
        # 提示，不是门禁：状态仍是"等人工验收"，由人决定。
        self.assertEqual(task["state"], "REVIEW_READY")
        self.assertEqual(task["block_code"], None)


class TestDeletionIsNotAWorkspaceChange(Fixture):
    """删除受控文件的候选必须能正常走到 REVIEW_READY。

    回归：``manifest`` 曾把"索引里有、工作树里没有"的条目记成 ``absent``。删除一个文件
    时，提交前的指纹含这条、提交后的不含，于是 ``Commit changed candidate tree`` 必然
    触发——删除类交付（清理死代码、删旧模块）从来不可能通过验证，而错误码指向
    ``WORKSPACE_CHANGED``，看起来像"工作区被改坏了"。
    """

    def test_candidate_that_only_deletes_a_file_is_accepted(self):
        task = self.run_task(self.new_task(), Backend(write=["src.txt"], delete=["legacy.py"]))
        self.assertEqual(task["state"], "REVIEW_READY", task.get("last_error"))
        self.assertIsNotNone(task["candidate_sha"])
        self.assertFalse(task["empty_candidate"])

    def test_deletion_only_candidate_is_not_empty(self):
        # 只删不改也算交付（candidate != base），不能落进"零改动"的口径。
        task = self.run_task(self.new_task(), Backend(delete=["legacy.py"]))
        self.assertEqual(task["state"], "REVIEW_READY", task.get("last_error"))
        self.assertNotEqual(task["candidate_sha"], task["base_sha"])


class TestProbeIsSafe(unittest.TestCase):
    def test_unknown_repository_is_unknown_not_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            view = _baseline.probe(tmp, "a" * 40, "b" * 40, "HEAD")
        self.assertIn(view["kind"], (_baseline.KIND_UNKNOWN, _baseline.KIND_NONE))
        # 无论如何都不能抛错：它在 REVIEW_READY 的路径上。
        self.assertIn("checked_at", view)

    def test_missing_candidate_is_none_with_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            view = _baseline.probe(tmp, "a" * 40, None, "HEAD")
        self.assertEqual(view["kind"], _baseline.KIND_NONE)
        self.assertEqual(view["reason"], "no_candidate")
