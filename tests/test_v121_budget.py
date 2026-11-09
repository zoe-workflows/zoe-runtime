"""v1.2.1 F2: failure attribution and the billable-attempt budget.

Two invariants:

* ``attempts`` is monotonic — it counts real agent launches and is never rewritten;
* a failure is waived only with evidence (spec demands the path / the check is not
  referenced by any acceptance criterion / it is an environment failure), at most
  once per ``run_id``, and a waived run never becomes a billable one.

A policy incident that was not repaired refuses to spend another attempt *before*
the debit, so "resume again and hope" costs neither budget nor an agent start.
Environment incidents (auth) never require a policy change: re-login must be
enough to continue.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from test_runtime import FakePublisher

from src._pipeline import _billable_attempts
from src._util import Error, atomic, digest, git
from src.core import Store, create, rebind, run_pipeline

CHECKS = [{"id": "lint", "argv": ["true"]}]


class Backend:
    def __init__(self, files=None, auth_error=False):
        self.calls = []
        self.files = dict(files or {})
        self.auth_error = auth_error

    def inspect(self, name):
        return None

    def execute(self, store, t, r, argv, log_name, worker=False, prompt=None):
        self.calls.append(log_name)
        if not worker:
            return 0
        if self.auth_error:
            raise Error("AGENT_AUTH_REQUIRED", "Authentication required. Please /login")
        workspace = Path(t["workspace"])
        (workspace / "src.txt").write_text("implemented\n")
        for name, text in self.files.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
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
        self.write_policy(["src.txt"], max_attempts=3)
        self.write_spec("implement src")

    def tearDown(self):
        self.store.db.close()
        self._td.cleanup()

    # ---------------------------------------------------------------- helpers

    def write_policy(self, allowed, max_attempts=3):
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
            "max_attempts": max_attempts,
        }
        atomic(self.store.home / "projects/demo.json", config)
        return config

    def write_spec(self, statement):
        self.spec.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "title": "Deliver",
                    "acceptance_criteria": [
                        {
                            "id": "AC-1",
                            "statement": statement,
                            "verification": {"type": "check", "check_id": "lint"},
                        }
                    ],
                }
            )
        )

    def new_task(self):
        result = create(
            self.store,
            dict(
                schema_version=1,
                request_id="req-" + uuid.uuid4().hex,
                authorization_ref="scope-1",
                project_id="demo",
                title="Deliver",
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

    def waived(self, t):
        return t.get("budget_waivers") or []


class TestPolicyAttribution(Fixture):
    def test_spec_required_path_waives_the_attempt(self):
        # spec 明确要求 docs/x.md → 是策略太窄，不是 agent 越界
        self.write_spec("write docs/x.md and src")
        backend = Backend(files={"docs/x.md": "doc\n"})
        t = self.run_task(self.new_task(), backend)

        self.assertEqual(t["state"], "BLOCKED")
        self.assertEqual(t["block_code"], "SCOPE_VIOLATION")
        self.assertEqual(t["block_details"]["missing_paths"], ["docs"])
        self.assertEqual(t["attempts"], 1)  # 真实启动次数保持单调
        self.assertEqual(_billable_attempts(t), 0)  # 但不算进预算
        self.assertEqual(len(self.waived(t)), 1)
        self.assertEqual(self.waived(t)[0]["basis"], "policy-too-narrow")
        self.assertEqual(self.waived(t)[0]["run_id"], t["current_run_id"])

    def test_resuming_without_repair_costs_nothing_and_starts_nothing(self):
        self.write_spec("write docs/x.md and src")
        backend = Backend(files={"docs/x.md": "doc\n"})
        t = self.run_task(self.new_task(), backend)
        agent_calls = backend.calls.count("agent")

        t = self.run_task(t, backend)
        self.assertEqual(t["block_code"], "SCOPE_VIOLATION")
        self.assertEqual(t["attempts"], 1)
        self.assertEqual(_billable_attempts(t), 0)
        self.assertEqual(backend.calls.count("agent"), agent_calls)  # 没有启动 agent
        self.assertEqual(len(self.waived(t)), 1)  # 也没有重复豁免

    def test_after_the_policy_is_repaired_the_task_proceeds(self):
        self.write_spec("write docs/x.md and src")
        backend = Backend(files={"docs/x.md": "doc\n"})
        t = self.run_task(self.new_task(), backend)
        self.assertEqual(t["block_code"], "SCOPE_VIOLATION")

        self.write_policy(["src.txt", "docs"])
        # rebind 使 policy_revision 变化，恢复前判定放行
        fresh = self.store.task(t["task_id"])
        rebind(
            self.store,
            fresh["task_id"],
            dict(
                schema_version=2,
                request_id="req-" + uuid.uuid4().hex,
                authorization_ref="scope-1",
                expected_version=fresh["row_version"],
                reason="allow docs",
                target_policy_hash=digest(self.store.config("demo")),
            ),
        )
        t = self.run_task(t, backend)
        self.assertEqual(t["state"], "REVIEW_READY")
        self.assertEqual(t["attempts"], 2)
        self.assertEqual(_billable_attempts(t), 1)

    def test_agent_overstepping_is_not_waived(self):
        # spec 从未提到 docs → agent 越界，责任在 agent，正常占用预算
        self.write_spec("implement src")
        backend = Backend(files={"docs/x.md": "doc\n"})
        t = self.run_task(self.new_task(), backend)

        self.assertEqual(t["block_code"], "SCOPE_VIOLATION")
        self.assertEqual(t["attempts"], 1)
        self.assertEqual(_billable_attempts(t), 1)
        self.assertEqual(self.waived(t), [])
        self.assertIsNone((t.get("last_block") or {}).get("basis"))

        # 未改配置也能继续：这一轮算 agent 的
        t = self.run_task(t, backend)
        self.assertEqual(t["attempts"], 2)
        self.assertEqual(backend.calls.count("agent"), 2)


class TestEnvironmentAttribution(Fixture):
    def test_auth_failure_is_waived_and_resumes_without_policy_change(self):
        backend = Backend(auth_error=True)
        t = self.run_task(self.new_task(), backend)
        self.assertEqual(t["block_code"], "AGENT_AUTH_REQUIRED")
        self.assertEqual(_billable_attempts(t), 0)
        self.assertEqual(self.waived(t)[0]["basis"], "environment")

        backend.auth_error = False  # 重新登录后，策略一个字都没改
        t = self.run_task(t, backend)
        self.assertEqual(t["state"], "REVIEW_READY")
        self.assertEqual(t["attempts"], 2)
        self.assertEqual(_billable_attempts(t), 1)


class TestBudgetAccounting(Fixture):
    def test_waivers_are_keyed_by_run_id_not_by_count(self):
        task = {"attempts": 3, "budget_waivers": [{"run_id": "r1"}, {"run_id": "r1"}]}
        # 同一个 run 重复记录不重复减免
        self.assertEqual(_billable_attempts(task), 2)
        self.assertEqual(task["attempts"], 3)  # 单调记录不被改写

    def test_billable_budget_allows_a_retry_after_a_waiver(self):
        self.write_policy(["src.txt"], max_attempts=1)
        backend = Backend(auth_error=True)
        t = self.run_task(self.new_task(), backend)
        self.assertEqual(self.waived(t)[0]["basis"], "environment")
        self.assertEqual(_billable_attempts(t), 0)

        # max_attempts=1：若不是豁免，第二次 resume 会 RETRY_EXHAUSTED
        backend.auth_error = False
        t = self.run_task(t, backend)
        self.assertNotEqual(t["block_code"], "RETRY_EXHAUSTED")
        self.assertEqual(t["state"], "REVIEW_READY")
        self.assertEqual(t["attempts"], 2)
        self.assertEqual(_billable_attempts(t), 1)


if __name__ == "__main__":
    unittest.main()
