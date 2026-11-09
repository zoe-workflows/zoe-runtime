"""v1.2.1 F1 / F1-b: rebind re-verification and external close.

The invariant under test is the one the field run broke: a policy change only
invalidates **evidence**, never the product. When the workspace still holds the
verified candidate and no writer is active, `rebind` must put the task back into
`verify` so a resume re-runs the checks and signs new evidence — without paying
for a second agent run — and a second `rebind` must keep that basis.

Everything here is unit / simulated. No agent account, no remote.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from test_runtime import FakePublisher

from src._util import atomic, digest, git
from src.core import Store, accept, close_external, create, rebind, run_pipeline

CHECKS = [{"id": "lint", "argv": ["true"]}]


class Backend:
    """In-process backend: the worker writes a tracked file, checks return 0."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def inspect(self, name):
        return None

    def execute(self, store, t, r, argv, log_name, worker=False, prompt=None):
        self.calls.append(log_name)
        if worker:
            (Path(t["workspace"]) / "src.txt").write_text("implemented\n")
            return 0
        return 1 if log_name[len("check-") :] in self.fail else 0


class Fixture(unittest.TestCase):
    delivery_mode = "local_artifact"

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
        self.spec.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "title": "Deliver src",
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
        self.write_policy(["src.txt"])

    def tearDown(self):
        self.store.db.close()
        self._td.cleanup()

    # ---------------------------------------------------------------- helpers

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
                delivery_mode=self.delivery_mode,
            ),
        )
        return self.store.task(result["task_id"])

    def run_task(self, t, backend=None):
        t = self.store.task(t["task_id"])
        rid = "run-" + uuid.uuid4().hex[:16]
        self.store.run({"run_id": rid, "task_id": t["task_id"], "status": "running"})
        t.update(current_run_id=rid, state="RUNNING")
        self.store.save(t)
        return run_pipeline(self.store, t["task_id"], backend or Backend(), FakePublisher())

    def rebind(self, t, target=None):
        fresh = self.store.task(t["task_id"])
        return rebind(
            self.store,
            fresh["task_id"],
            self.request(
                expected_version=fresh["row_version"],
                reason="policy widened",
                target_policy_hash=target or digest(self.store.config("demo")),
            ),
        )


class TestRevalidate(Fixture):
    def test_rebind_reverifies_without_restarting_the_agent(self):
        backend = Backend()
        t = self.run_task(self.new_task(), backend)
        self.assertEqual(t["state"], "REVIEW_READY")
        first_sha = t["validated_sha"]
        self.assertIsNotNone(t["current_evidence_id"])
        self.assertEqual(backend.calls.count("agent"), 1)

        self.write_policy(["src.txt", "docs"])
        result = self.rebind(t)
        t = self.store.task(t["task_id"])

        # 证据失效，产物保留，任务回到可重验的位置
        self.assertEqual(t["state"], "READY")
        self.assertEqual(t["phase"], "verify")
        self.assertTrue(result["revalidate"]["pending"])
        self.assertEqual(result["revalidate"]["blockers"], [])
        self.assertEqual(t["revalidation"]["product"]["sha"], first_sha)
        self.assertIsNone(t["validated_sha"])
        self.assertIsNone(t["current_evidence_id"])
        self.assertIn("acceptance", result["invalidated"])

        t = self.run_task(t, backend)
        self.assertEqual(t["state"], "REVIEW_READY")
        # 只重跑 checks，没有第二个 agent
        self.assertEqual(backend.calls.count("agent"), 1)
        self.assertEqual(backend.calls.count("check-lint"), 2)
        self.assertEqual(t["validated_sha"], first_sha)
        # 证据是内容寻址的：同样的产物 + 同样的检查结果 → 同一个 evidence_id
        # 是正确行为，不能用"id 变了"来证明重验发生过；checks 跑了两遍才是证据。
        self.assertIsNotNone(t["current_evidence_id"])
        # 重验完成即清 pending，避免下一次 rebind 误判
        self.assertIsNone(t.get("revalidation"))

        accepted = accept(
            self.store,
            t["task_id"],
            self.request(
                expected_version=self.store.task(t["task_id"])["row_version"],
                decision="accepted",
                actor="human",
                evidence="reviewed in the PR",
                subject_sha=t["validated_sha"],
            ),
        )
        self.assertEqual(accepted["state"], "DONE")

    def test_a_second_rebind_keeps_the_same_product_basis(self):
        backend = Backend()
        t = self.run_task(self.new_task(), backend)
        self.write_policy(["src.txt", "docs"])
        self.rebind(t)
        t = self.store.task(t["task_id"])
        product = t["revalidation"]["product"]
        runs_before = len(self.store.runs(t["task_id"]))

        # 第一次 rebind 已清空 validated_fingerprint；第二次不能因此失去基准
        self.rebind(t)
        t = self.store.task(t["task_id"])
        self.assertTrue((t["revalidation"] or {}).get("pending"))
        self.assertEqual(t["revalidation"]["product"], product)
        self.assertEqual(t["state"], "READY")
        self.assertEqual(t["phase"], "verify")
        self.assertEqual(len(self.store.runs(t["task_id"])), runs_before)

    def test_drift_after_rebind_blocks_instead_of_reverifying(self):
        backend = Backend()
        t = self.run_task(self.new_task(), backend)
        self.write_policy(["src.txt", "docs"])
        self.rebind(t)
        t = self.store.task(t["task_id"])
        (Path(t["workspace"]) / "src.txt").write_text("tampered\n")

        result = self.rebind(t)
        t = self.store.task(t["task_id"])
        self.assertFalse(result["revalidate"]["pending"])
        self.assertEqual(t["state"], "BLOCKED")
        self.assertEqual(t["block_code"], "WORKSPACE_CHANGED")
        self.assertEqual(t["revalidation"]["pending"], False)

    def test_drift_before_the_first_rebind_blocks_instead_of_reverifying(self):
        """首次 rebind 的基准必须来自已签发的证据，不能来自当前工作树。

        否则"先手工改文件、再 rebind"会把人改的内容当成 agent 产物重新签字：
        `accept` 只校验 `subject_sha` 与（篡改后的）指纹，照样通过。
        """
        backend = Backend()
        t = self.run_task(self.new_task(), backend)
        self.assertEqual(t["state"], "REVIEW_READY")
        (Path(t["workspace"]) / "src.txt").write_text("tampered\n")
        self.write_policy(["src.txt", "docs"])

        result = self.rebind(t)
        t = self.store.task(t["task_id"])
        self.assertFalse(result["revalidate"]["pending"])
        self.assertEqual(result["revalidate"]["blockers"], [])
        self.assertEqual(t["state"], "BLOCKED")
        self.assertEqual(t["block_code"], "WORKSPACE_CHANGED")
        self.assertEqual(t["revalidation"]["pending"], False)
        # 旧证据已清，且没有产出/保留新的候选身份
        self.assertIsNone(t["validated_sha"])
        self.assertIsNone(t["current_evidence_id"])

    def test_rebind_before_any_evidence_only_swaps_the_policy(self):
        """没有产物基准 ≠ 基准丢失：从未验证过的任务不该被 WORKSPACE_CHANGED 挡住。"""
        t = self.new_task()
        self.assertEqual(t["state"], "READY")
        self.write_policy(["src.txt", "docs"])

        result = self.rebind(t)
        t = self.store.task(t["task_id"])
        self.assertFalse(result["revalidate"]["pending"])
        self.assertEqual(result["revalidate"]["blockers"], [])
        self.assertEqual(t["state"], "READY")
        self.assertIsNone(t.get("block_code"))

    def test_process_unknown_stays_blocked(self):
        backend = Backend()
        t = self.run_task(self.new_task(), backend)
        self.store.run(
            {"run_id": t["current_run_id"], "task_id": t["task_id"], "status": "unknown"}
        )
        t = self.store.task(t["task_id"])
        t.update(state="BLOCKED", block_code="PROCESS_UNKNOWN")
        self.store.save(t)
        self.write_policy(["src.txt", "docs"])

        with self.assertRaises(Exception) as ctx:
            self.rebind(t)
        self.assertEqual(getattr(ctx.exception, "code", None), "PROCESS_UNKNOWN")


class TestPullRequestDelivery(Fixture):
    def test_pr_delivery_is_not_revalidated_locally(self):
        backend = Backend()
        t = self.run_task(self.new_task(), backend)
        self.assertEqual(t["state"], "REVIEW_READY")
        # PR 交付的真实流水线需要远端对账（本地没有 remote 时是 REMOTE_UNKNOWN），
        # 所以这里直接把"已发布、待验收"这一状态建模出来，只测 rebind 的判定。
        t.update(delivery_mode="pull_request", phase="ci", state="REVIEW_READY")
        self.store.save(t)
        t = self.store.task(t["task_id"])

        self.write_policy(["src.txt", "docs"])
        result = self.rebind(t)
        t = self.store.task(t["task_id"])
        self.assertFalse(result["revalidate"]["pending"])
        self.assertTrue(any("delivery_mode" in item for item in result["revalidate"]["blockers"]))
        self.assertEqual(t["state"], "REVIEW_READY")
        self.assertEqual(t["phase"], "ci")


class TestExternalClose(Fixture):
    def test_external_close_is_cancelled_and_never_accepted(self):
        backend = Backend()
        t = self.run_task(self.new_task(), backend)
        self.assertEqual(t["state"], "REVIEW_READY")

        result = close_external(
            self.store,
            t["task_id"],
            self.request(
                expected_version=self.store.task(t["task_id"])["row_version"],
                reason="merged outside Zoe",
                external_ref="https://example.invalid/pr/9",
                merged_sha=t["candidate_sha"],
                actor="human",
                claim="verified",
            ),
        )
        t = self.store.task(t["task_id"])
        self.assertEqual(result["state"], "CANCELLED")
        self.assertEqual(t["state"], "CANCELLED")
        self.assertEqual(t["block_code"], "CLOSED_EXTERNAL")
        self.assertEqual(t["external_close"]["claim"], "verified")
        self.assertFalse(result["accepted"])

        # 外部收口的任务不能再被验收
        with self.assertRaises(Exception) as ctx:
            accept(
                self.store,
                t["task_id"],
                self.request(
                    expected_version=self.store.task(t["task_id"])["row_version"],
                    decision="accepted",
                    actor="human",
                    evidence="x",
                    subject_sha=t["validated_sha"],
                ),
            )
        self.assertEqual(getattr(ctx.exception, "code", None), "STATE_CONFLICT")

    def test_external_close_refuses_an_active_run(self):
        self.store.run({"run_id": "run-live", "task_id": "task-x", "status": "running"})
        t = self.new_task()
        t = self.store.task(t["task_id"])
        t.update(current_run_id="run-live", state="RUNNING")
        self.store.save(t)

        with self.assertRaises(Exception) as ctx:
            close_external(
                self.store,
                t["task_id"],
                self.request(
                    expected_version=t["row_version"],
                    reason="merged outside Zoe",
                    external_ref="https://example.invalid/pr/9",
                    actor="human",
                    claim="asserted",
                ),
            )
        self.assertEqual(getattr(ctx.exception, "code", None), "STATE_CONFLICT")
        self.assertEqual(self.store.task(t["task_id"])["state"], "RUNNING")


if __name__ == "__main__":
    unittest.main()
