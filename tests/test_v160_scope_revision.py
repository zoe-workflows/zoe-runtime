"""v1.6: ``SCOPE_VIOLATION`` 在 spec 派生范围下只能靠 ``task revise`` 解除。

现场复现的问题（一次真实并行批次）：改 spec 的 ``allowed_paths`` 之后 ``resume`` 与
``rebind`` 都"无效"，于是宿主的做法是 ``cancel`` + 从零重派，把 worker 已经写好的
代码当成废料。真因不是 frozen policy 不更新，而是**更新的不是决定范围的那份数据**：

* ``task_scope`` 只在 create / revise 时由 :func:`_resolve_task_scope` 重算；
* ``rebind`` 只更新 ``policy_revision``，不动 ``task_scope``。

于是"改项目配置 + rebind"能通过恢复前的闸门（``policy_revision`` 变了），却在下一轮
撞同一个 ``SCOPE_VIOLATION``——白烧一次付费尝试，还给出"已经修好了"的错觉。

这里锁定两件事：
1. ``scope_source="spec"`` 时，未 ``revise`` 的恢复必须被**拦在启动之前**；
2. ``revise`` 放宽 spec 之后，工作区保留、agent 重新跑一次即可通过。
"""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from test_runtime import FakePublisher

from src._util import atomic, digest, git
from src.core import Store, create, rebind, revise, run_pipeline

CHECKS = [{"id": "lint", "argv": ["true"]}]


class Backend:
    """Worker writes whatever the test asks for; controller checks pass."""

    def __init__(self, worker_paths=()):
        self.calls = []
        self.worker_paths = list(worker_paths)

    def inspect(self, name):
        return None

    def execute(self, store, t, r, argv, log_name, worker=False, prompt=None):
        self.calls.append(log_name)
        if worker:
            for name in self.worker_paths:
                target = Path(t["workspace"]) / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("changed\n")
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
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        self.store = Store(self.root / "state")
        self.spec = self.root / "spec.json"
        self.write_spec(["src.txt"])
        self.write_policy(["src.txt"])

    def tearDown(self):
        self.store.db.close()
        self._td.cleanup()

    # ---------------------------------------------------------------- helpers

    def spec_document(self, allowed):
        """Spec whose own scope is ``allowed``.

        ``AC-2`` 明确要求更新 ``docs``：这正是"spec 自相矛盾"的形态（合同要求改
        docs、范围又只授权 src.txt）。``_incident_basis`` 靠"缺失路径出现在 spec
        文本里"判定 ``policy-too-narrow``，这里给出这个前提。
        """
        return {
            "schema_version": 2,
            "title": "Deliver src",
            "allowed_paths": list(allowed),
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "statement": "implement src",
                    "verification": {"type": "check", "check_id": "lint"},
                },
                {
                    "id": "AC-2",
                    "statement": "更新 docs 使用说明",
                    "verification": {
                        "type": "manual",
                        "who": "human",
                        "instructions": "打开 docs/guide.md，确认 Usage 一节与实现一致",
                    },
                },
            ],
        }

    def write_spec(self, allowed):
        self.spec.write_text(json.dumps(self.spec_document(allowed)))

    def write_policy(self, allowed):
        config = {
            "schema_version": 1,
            "project_id": "demo",
            "repo": str(self.repo),
            "backend": "local-process",
            "network": "none",
            "compatibility_verified": True,
            "agent": "codebuddy",
            "checks": [dict(check) for check in CHECKS],
            "allowed_paths": list(allowed),
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute", "publish", "accept"],
            "ci_required": False,
            "max_attempts": 3,
        }
        atomic(self.store.home / "projects/demo.json", config)
        return config

    def request(self, **kwargs):
        return dict(
            schema_version=2,
            request_id="req-" + uuid.uuid4().hex,
            authorization_ref="scope-1",
            reason="widen the scope the spec declared too narrowly",
            **kwargs,
        )

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

    def run_task(self, t, backend):
        t = self.store.task(t["task_id"])
        rid = "run-" + uuid.uuid4().hex[:16]
        self.store.run({"run_id": rid, "task_id": t["task_id"], "status": "running"})
        t.update(current_run_id=rid, state="RUNNING")
        self.store.save(t)
        return run_pipeline(self.store, t["task_id"], backend, FakePublisher())


class TestSpecScopedViolation(Fixture):
    def test_rebind_cannot_clear_a_spec_scoped_violation(self):
        t = self.new_task()
        self.assertEqual(t["scope_source"], "spec")
        self.assertEqual(t["task_scope"], ["src.txt"])

        # worker 越界写 docs/：spec 又要求更新 docs，于是归因为"策略过窄"。
        backend = Backend(worker_paths=["docs/guide.md"])
        t = self.run_task(t, backend)
        self.assertEqual(t["state"], "BLOCKED")
        self.assertEqual(t["block_code"], "SCOPE_VIOLATION")
        self.assertEqual((t["last_block"] or {}).get("basis"), "policy-too-narrow")
        self.assertEqual((t["last_block"] or {}).get("scope_source"), "spec")
        self.assertEqual(backend.calls.count("agent"), 1)

        # 放宽项目范围 + rebind：闸门（policy_revision）会放行……
        self.write_policy(["src.txt", "docs"])
        rebind(
            self.store,
            t["task_id"],
            self.request(
                expected_version=self.store.task(t["task_id"])["row_version"],
                target_policy_hash=digest(self.store.config("demo")),
            ),
        )
        # ……但下一轮仍然只能在同一个地方停下——范围没变，结论就不会变。
        t = self.run_task(self.store.task(t["task_id"]), backend)
        self.assertEqual(t["state"], "BLOCKED")
        self.assertEqual(t["block_code"], "SCOPE_VIOLATION")
        # 关键：错误本身要说清唯一出路，宿主不必靠记忆或翻文档判断。
        details = t["block_details"] or {}
        self.assertEqual(details.get("scope_source"), "spec")
        self.assertIn("task revise", details.get("next_action") or "")
        self.assertIn("rebind does NOT", details.get("next_action") or "")
        # 一次付费尝试都没有发生（范围判定发生在 verify，agent 不会重跑）。
        self.assertEqual(backend.calls.count("agent"), 1)
        self.assertEqual(t["attempts"], 1)

    def test_revise_widens_the_scope_and_keeps_the_worktree(self):
        backend = Backend(worker_paths=["docs/guide.md"])
        t = self.run_task(self.new_task(), backend)
        self.assertEqual(t["block_code"], "SCOPE_VIOLATION")
        workspace = Path(t["workspace"])
        self.assertTrue(workspace.exists())

        # 顺序不能反：spec 的 allowed_paths 只能是项目范围的**子集**，所以先扩项目
        # 范围并 rebind 采纳新策略，再 revise 把 spec 的范围放宽到新上限。
        self.write_policy(["src.txt", "docs"])
        rebind(
            self.store,
            t["task_id"],
            self.request(
                expected_version=self.store.task(t["task_id"])["row_version"],
                target_policy_hash=digest(self.store.config("demo")),
            ),
        )
        self.write_spec(["src.txt", "docs"])
        revise(
            self.store,
            t["task_id"],
            self.request(
                expected_version=self.store.task(t["task_id"])["row_version"],
                spec_file=str(self.spec),
            ),
        )
        t = self.store.task(t["task_id"])
        self.assertEqual(t["state"], "READY")
        self.assertEqual(t["spec_revision"], 2)
        self.assertEqual(t["scope_source"], "spec")
        self.assertEqual(t["task_scope"], ["src.txt", "docs"])
        # 工作区与证据都不该被换掉：revise 不是 cancel。
        self.assertEqual(t["workspace"], str(workspace))
        self.assertIsNone(t["last_block"])
        # 只改了 allowed_paths ⇒ 实现要求没变 ⇒ 直接按新范围重判这棵树。
        self.assertEqual(t["phase"], "verify")

        t = self.run_task(t, backend)
        self.assertEqual(t["state"], "REVIEW_READY")
        # 已经写好的代码被复用：没有第二次 agent 费用。
        self.assertEqual(backend.calls.count("agent"), 1)

    def test_project_scoped_violation_still_accepts_rebind(self):
        """回归：spec 不声明范围时，改项目配置 + rebind 仍是正解。"""
        document = self.spec_document([])
        document.pop("allowed_paths")
        self.spec.write_text(json.dumps(document))

        backend = Backend(worker_paths=["docs/guide.md"])
        task = self.new_task()
        self.assertEqual(task["scope_source"], "project")
        self.assertIsNone(task["task_scope"])
        task = self.run_task(task, backend)
        self.assertEqual(task["block_code"], "SCOPE_VIOLATION")

        self.write_policy(["src.txt", "docs"])
        rebind(
            self.store,
            task["task_id"],
            self.request(
                expected_version=self.store.task(task["task_id"])["row_version"],
                target_policy_hash=digest(self.store.config("demo")),
            ),
        )
        backend.worker_paths = ["src.txt"]
        task = self.run_task(self.store.task(task["task_id"]), backend)
        self.assertEqual(task["state"], "REVIEW_READY")
        self.assertEqual(backend.calls.count("agent"), 2)
