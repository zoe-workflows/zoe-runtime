"""v0.4.1 准入策略：能力态从"闸门"降级为"标签"。

修复的是死锁：``qualification run`` 只做 cli-found/version 两项，其余四项永远
INCOMPLETE，于是没有任何 backend 能变成 VERIFIED；而 ``start`` 又要求 VERIFIED，
结果是"三个 CLI 都装好了，一个都起不来"。

新语义：
- 默认（``require_real_e2e: false``）放行未验证执行器，但把
  ``unverified`` / ``trust_level`` 写进绑定、响应、run 记录与报告；
- 严格项目（``require_real_e2e: true``）保留原来的拒绝行为；
- ``max_active_tasks`` 放开到 1..8（单任务唯一写者不变量不变）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src import _profiles
from src._config import MAX_ACTIVE_TASKS, doctor, validate_config
from src._pipeline import supervisor_env
from src._util import Error, digest, git


class AdmissionFixture(unittest.TestCase):
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
        self.raw = self.config()

    def tearDown(self):
        self._td.cleanup()

    def config(self, **overrides):
        value = {
            "schema_version": 2,
            "project_id": "admission",
            "repo": str(self.repo),
            "backend": "local-process",
            "network": "none",
            "default_agent_profile": "cb",
            "agent_profiles": {
                "cb": {
                    "adapter": "codebuddy",
                    "model": "stub-model",
                    "backend": "codebuddy-cli",
                    "expected_version": "1.2.3",
                    "permission_profile": "isolated-edit",
                }
            },
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute"],
            "allowed_paths": ["src.txt"],
            "checks": [{"id": "unit", "argv": ["true"]}],
        }
        value.update(overrides)
        return value


class TestMaxActiveTasks(AdmissionFixture):
    def test_parallel_dispatch_range_is_accepted(self):
        for limit in (1, 2, MAX_ACTIVE_TASKS):
            validated = validate_config(self.config(max_active_tasks=limit))
            self.assertEqual(validated["max_active_tasks"], limit)

    def test_out_of_range_is_rejected(self):
        for limit in (0, -1, MAX_ACTIVE_TASKS + 1):
            with self.assertRaises(Error) as ctx:
                validate_config(self.config(max_active_tasks=limit))
            self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")

    def test_default_stays_single_slot(self):
        validated = validate_config(self.config())
        self.assertEqual(validated.get("max_active_tasks", 1), 1)


class TestRequireRealE2e(AdmissionFixture):
    def test_default_is_permissive(self):
        validated = validate_config(self.config())
        self.assertIs(validated["require_real_e2e"], False)

    def test_default_is_applied_by_normalization(self):
        """默认值必须在 normalize_config 里落，(任务, 项目) 两处摘要才一致。"""
        self.assertNotIn("require_real_e2e", self.raw)
        normalized = _profiles.normalize_config(self.raw)
        self.assertIs(normalized["require_real_e2e"], False)
        # 规范化是幂等的：再算一次摘要不会因为新增默认值而漂移。
        copy = _profiles.normalize_config(self.config())
        self.assertEqual(_profiles.policy_digest(normalized), digest(copy))

    def test_doctor_reports_trust_separately_from_readiness(self):
        validated = validate_config(self.config())
        result = doctor(validated)
        self.assertFalse(result["trusted"], "no real e2e record for codebuddy-cli")
        self.assertFalse(result["checks"]["adapter_approved"])
        self.assertTrue(result["checks"]["adapter_unverified"])
        # ready 只反映环境：平台层可用时应为 True，与"是否验证过"无关。
        platform_ok = (
            result["checks"]["git"]
            and result["checks"]["platform_lock"]
            and result["checks"]["platform_tree"]
            and result["checks"]["process_identity"]
        )
        self.assertEqual(result["ready"], bool(platform_ok))

    def test_strict_project_is_not_ready_without_evidence(self):
        validated = validate_config(self.config(require_real_e2e=True))
        self.assertFalse(doctor(validated)["ready"])


class TestRequireRunnablePolicy(AdmissionFixture):
    def test_permissive_mode_returns_the_unverified_binding(self):
        validated = validate_config(self.config())
        binding = _profiles.resolve_binding(validated, "cb")
        self.assertTrue(binding.unverified)
        self.assertEqual(binding.trust_level, "unverified")
        returned = _profiles.require_runnable(binding, require_real_e2e=False)
        self.assertIs(returned, binding)
        self.assertTrue(returned.to_dict()["unverified"])

    def test_strict_mode_still_refuses(self):
        validated = validate_config(self.config())
        binding = _profiles.resolve_binding(validated, "cb")
        with self.assertRaises(Error) as ctx:
            _profiles.require_runnable(binding)
        self.assertEqual(ctx.exception.code, "ADAPTER_UNVERIFIED")


class TestSupervisorEnv(unittest.TestCase):
    def test_relative_pythonpath_is_absolutized(self):
        """supervisor 以 cwd=<home> 启动，相对 PYTHONPATH 会失效。"""
        original = os.environ.get("PYTHONPATH")
        try:
            os.environ["PYTHONPATH"] = os.pathsep.join(["src", "./rel"])
            env = supervisor_env()
            for part in env["PYTHONPATH"].split(os.pathsep):
                self.assertTrue(os.path.isabs(part), part)
            self.assertNotIn("src", env["PYTHONPATH"].split(os.pathsep))
        finally:
            if original is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = original

    def test_the_package_root_is_on_pythonpath(self):
        """源码模式：包根由运行时补上，不依赖外部 PYTHONPATH。"""
        from src import _pipeline

        root = str(Path(_pipeline.__file__).resolve().parent.parent)
        parts = supervisor_env()["PYTHONPATH"].split(os.pathsep)
        self.assertIn(root, parts)
        self.assertEqual(parts.count(root), 1)

    def test_importable_supervisor_passes_the_precheck(self):
        from src._pipeline import require_supervisor_importable

        env = supervisor_env()
        require_supervisor_importable(env, Path(tempfile.gettempdir()), Path("unused.log"))

    def test_broken_environment_fails_fast(self):
        """An environment where ``import src`` fails must fail before dispatch.

        The probe shadows the package with a broken ``src.py`` instead of relying
        on the package being absent: once ``zoe-runtime`` is installed (CI, any
        ``.venv``), a bogus ``PYTHONPATH`` alone no longer breaks the import, and
        the old version of this test silently asserted nothing.
        """
        from src._pipeline import require_supervisor_importable

        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "src.py").write_text(
                "raise ImportError('boom: shadowed for the precheck test')\n", encoding="utf-8"
            )
            env = {**os.environ, "PYTHONPATH": directory}
            with self.assertRaises(Error) as ctx:
                require_supervisor_importable(env, Path(tempfile.gettempdir()), Path("unused.log"))
        self.assertEqual(ctx.exception.code, "ENVIRONMENT_ERROR")
        self.assertIn("pip install zoe-runtime", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
