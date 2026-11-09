"""v0.4.1 qualification：四项运行时检查由真实运行证明（03 §1）。

替换的是"硬编码 INCOMPLETE"：以前 ``qualification run`` 只做 cli-found/version，
永远拿不到 VERIFIED，而 ``start`` 又要求 VERIFIED —— 死锁。

现在的实现：
- 检查 1/2：本机探测；
- 检查 3/4/5：一次完整 implement 运行（create -> start -> 监督进程 -> run_pipeline），
  从 run 记录的 ``agent_result`` 与工作区 diff 反读 auth / 非交互编辑 / 解析；
- 检查 6：另一次运行，先证明进程树被确认停止，再证明能续跑。
唯一的豁免是临时项目 ``require_real_e2e: false``（不能用自己的产物解锁自己）。

测试用替身 CLI（PATH 前置），不调用真实模型。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import _profiles, _qualification
from src._config import validate_config
from src._store import Store
from src._util import atomic, git

STUB_OK = """#!/bin/sh
if [ "$1" = "--version" ]; then echo 9.9.9; exit 0; fi
mkdir -p src
printf 'qualified\\n' > src/stub-output.txt
echo '{"type":"result","subtype":"success","is_error":false,"result":"stub wrote src/stub-output.txt"}'
"""

STUB_SLOW = """#!/bin/sh
if [ "$1" = "--version" ]; then echo 9.9.9; exit 0; fi
sleep 5
mkdir -p src
printf 'qualified\\n' > src/stub-output.txt
echo '{"type":"result","subtype":"success","is_error":false,"result":"slow stub wrote src/stub-output.txt"}'
"""

STUB_AUTH = """#!/bin/sh
if [ "$1" = "--version" ]; then echo 9.9.9; exit 0; fi
echo '{"type":"error","subtype":"error_auth","is_error":true,"result":"Not logged in. Run login first"}'
"""


class QualFixture(unittest.TestCase):
    stub = STUB_OK

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        self.repo = root / "repo"
        (self.repo / "src").mkdir(parents=True)
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "test")
        git(self.repo, "config", "user.email", "test@localhost")
        (self.repo / "src" / "a.txt").write_text("initial\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")

        self.bin = root / "bin"
        self.bin.mkdir()
        stub_path = self.bin / "codebuddy"
        stub_path.write_text(self.stub)
        stub_path.chmod(0o755)

        self.fixture = root / "fixture"
        self.fixture.mkdir()
        (self.fixture / "spec.md").write_text("# Qualification\nAC-001 write a file\n")
        (self.fixture / "checks.json").write_text(json.dumps([{"id": "unit", "argv": ["true"]}]))
        (self.fixture / "expect.json").write_text(json.dumps({}))

        self.store = Store(root / "state")
        self.register_base(root)
        self.out = root / "qual.json"
        self._env = mock.patch.dict(
            os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.store.db.close()
        self._td.cleanup()

    def register_base(self, root: Path) -> None:
        """源项目：严格模式（require_real_e2e=true）仍然不能挡住 qualification。"""
        config = {
            "schema_version": 2,
            "project_id": "base",
            "repo": str(self.repo),
            "backend": "local-process",
            "network": "none",
            "default_agent_profile": "cb",
            "agent_profiles": {
                "cb": {
                    "adapter": "codebuddy",
                    "model": "stub-model",
                    "backend": "codebuddy-cli",
                    "expected_version": "9.9.9",
                    "permission_profile": "isolated-edit",
                }
            },
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute"],
            "allowed_paths": ["src"],
            "checks": [{"id": "unit", "argv": ["true"]}],
            "require_real_e2e": True,
        }
        validated = validate_config(config, base_dir=root)
        with self.store.lock():
            atomic(self.store.home / "projects" / "base.json", validated)

    def qualify(self, stop_after: float = 1.0, timeout: float = 300.0) -> dict:
        return _qualification.run(
            self.store,
            {
                "project_id": "base",
                "profile": "cb",
                "fixture": str(self.fixture),
                "repo": str(self.repo),
                "out": str(self.out),
                "timeout": timeout,
                "stop_after": stop_after,
            },
        )

    def record(self) -> dict:
        return json.loads(self.out.read_text())["records"][0]


class TestQualificationPasses(QualFixture):
    stub = STUB_SLOW

    def test_all_six_checks_pass_on_a_real_run(self):
        result = self.qualify()
        self.assertEqual(result["real_e2e_status"], "PASS", result)
        self.assertEqual(result["outstanding"], [])

        record = self.record()
        statuses = {item["id"]: item["status"] for item in record["checks"]}
        self.assertEqual(
            statuses,
            {
                "cli-found": "PASS",
                "version": "PASS",
                "auth": "PASS",
                "noninteractive-edit": "PASS",
                "parse": "PASS",
                "stop-resume": "PASS",
            },
        )
        self.assertTrue(record["stopped_and_resumed"])
        self.assertEqual(record["source"], "real_e2e")
        self.assertEqual(record["test_suite_version"], "qual-v2")
        self.assertTrue(record["evidence_refs"], "evidence must point at the runs it used")

    def test_qualification_does_not_unlock_its_own_source_project(self):
        """豁免只作用于临时项目：源项目仍是 IMPLEMENTED，必须显式指向证据。"""
        self.qualify()
        binding = _profiles.resolve_binding(self.store.config("base"), "cb")
        self.assertEqual(binding.capability, "IMPLEMENTED")


class TestQualificationFailures(QualFixture):
    stub = STUB_AUTH

    def test_auth_failure_is_reported_not_hidden(self):
        result = self.qualify()
        self.assertEqual(result["real_e2e_status"], "INCOMPLETE")
        statuses = {item["id"]: item["status"] for item in self.record()["checks"]}
        self.assertEqual(statuses["auth"], "FAIL")
        self.assertNotEqual(statuses["stop-resume"], "PASS")
        self.assertEqual(self.record()["source"], "local_admin")


class TestFixtureValidation(QualFixture):
    def test_repo_with_remote_is_refused(self):
        git(self.repo, "remote", "add", "origin", "https://example.invalid/repo.git")
        with self.assertRaises(Exception) as ctx:
            self.qualify()
        self.assertEqual(getattr(ctx.exception, "code", None), "POLICY_DENIED")

    def test_missing_fixture_keeps_runtime_checks_incomplete(self):
        result = _qualification.run(
            self.store,
            {
                "project_id": "base",
                "profile": "cb",
                "fixture": None,
                "repo": str(self.repo),
                "out": str(self.out),
                "timeout": 30,
            },
        )
        self.assertEqual(result["real_e2e_status"], "INCOMPLETE")
        self.assertEqual(
            sorted(result["outstanding"]),
            ["auth", "noninteractive-edit", "parse", "stop-resume"],
        )


if __name__ == "__main__":
    unittest.main()
