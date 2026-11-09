"""v1.5.2 批量并行派发的摩擦点：运行期最小修正（B3 / B4）。

对应技能侧设计 ``zoe-workflows/docs/2026-11-04-batch-parallel-dispatch-friction.md``：

- **B3** ``timeout_seconds`` 此前完全没有校验，也没任何可观测出口。写成 ``"3600"``
  只会在 run 监视循环里异步炸掉。现在：写入/执行路径拒绝，读路径只记 ``problems``
  （否则一份写错的历史配置连 ``doctor`` 都打不开），``doctor`` 回显生效值。
- **B4** ``BLOCKED`` 只对 ``INPUT_REQUIRED`` 给出下一步，其余 ``BLOCKED``（超时、
  失败）什么都不给——接管入口在最需要动作的状态上是空的。现在按 ``block_code``
  分流，并且**不**给 ``PROCESS_UNKNOWN`` 发 ``task resume``（运行时本来就会拒它）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src import _config, _context, _profiles
from src._util import Error


def profile(**overrides):
    value = {
        "adapter": "codebuddy",
        "backend": "codebuddy-cli",
        "permission_profile": "isolated-edit",
        "model": "m-primary",
    }
    value.update(overrides)
    return value


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "src.txt").write_text("initial\n")

    def config(self, **overrides):
        value = {
            "schema_version": 2,
            "project_id": "batch",
            "repo": str(self.repo),
            "backend": "local-process",
            "default_agent_profile": "cb",
            "agent_profiles": {"cb": profile()},
            "checks": [{"id": "unit", "argv": ["true"]}],
            "allowed_paths": ["src.txt"],
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute"],
        }
        value.update(overrides)
        return value


class TestRunTimeoutValidation(Fixture):
    """B3：一个字段，三个读者（校验期 / 执行期 / doctor）必须口径一致。"""

    def test_declared_value_is_echoed_by_doctor(self):
        c = _profiles.normalize_config(self.config(timeout_seconds=900))
        report = _config.doctor(c, probe=False)
        self.assertEqual(report["run_timeout_seconds"], 900)
        self.assertEqual(report["checks"]["run_timeout_seconds"], 900)
        self.assertEqual(report["run_timeout_declared"], 900)
        self.assertTrue(report["checks"]["run_timeout_valid"])

    def test_missing_value_echoes_the_default_and_says_it_was_absent(self):
        c = _profiles.normalize_config(self.config())
        report = _config.doctor(c, probe=False)
        self.assertEqual(report["run_timeout_seconds"], _config.DEFAULT_RUN_TIMEOUT_SECONDS)
        self.assertIsNone(report["run_timeout_declared"])
        # 没写字段不是错误：默认值本身就是合法配置。
        self.assertTrue(report["checks"]["run_timeout_valid"])

    def test_registration_refuses_a_string(self):
        with self.assertRaises(Error) as ctx:
            _config.validate_config(self.config(timeout_seconds="3600"))
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertEqual(ctx.exception.details["errors"][0]["field"], "timeout_seconds")

    def test_registration_refuses_a_bool(self):
        # bool 是 int 的子类：漏了这个分支，`True` 会被当成 1 秒。
        with self.assertRaises(Error) as ctx:
            _config.validate_config(self.config(timeout_seconds=True))
        self.assertEqual(ctx.exception.details["errors"][0]["field"], "timeout_seconds")

    def test_registration_refuses_both_ends_out_of_range(self):
        for value in (_config.MIN_RUN_TIMEOUT_SECONDS - 1, _config.MAX_RUN_TIMEOUT_SECONDS + 1):
            with self.subTest(value=value):
                with self.assertRaises(Error) as ctx:
                    _config.validate_config(self.config(timeout_seconds=value))
                self.assertEqual(
                    ctx.exception.details["errors"][0]["field"], "timeout_seconds"
                )

    def test_bounds_themselves_are_accepted(self):
        for value in (_config.MIN_RUN_TIMEOUT_SECONDS, _config.MAX_RUN_TIMEOUT_SECONDS):
            with self.subTest(value=value):
                c = _config.validate_config(self.config(timeout_seconds=value))
                self.assertEqual(c["timeout_seconds"], value)

    def test_read_path_records_the_problem_without_raising(self):
        """读路径不能让一个写错的字段把 ``doctor`` 一起弄坏：能看到才有得修。"""
        problems: list[dict] = []
        c = _config.validate_config(
            self.config(timeout_seconds="3600"), strict=False, problems=problems
        )
        self.assertEqual(c["timeout_seconds"], "3600")
        fields = [problem["field"] for problem in problems]
        self.assertIn("timeout_seconds", fields)

    def test_effective_value_never_raises_from_the_monitoring_loop(self):
        for value in (None, "3600", True, 0, 10**9):
            with self.subTest(value=value):
                payload = {} if value is None else {"timeout_seconds": value}
                self.assertEqual(
                    _config.run_timeout_seconds(payload),
                    _config.DEFAULT_RUN_TIMEOUT_SECONDS,
                )

    def test_effective_value_returns_the_declared_integer(self):
        self.assertEqual(_config.run_timeout_seconds({"timeout_seconds": 120}), 120)


class TestAllowedNextOnBlocked(unittest.TestCase):
    """B4：接管入口在最需要动作的状态上不能是空的。"""

    def actions(self, state, block=None):
        task = {"state": state, "block_code": block}
        return [item["action"] for item in _context._allowed_next({}, task)]

    def test_blocked_after_a_timeout_offers_resume(self):
        for block in ("RUN_TIMEOUT", "CHECK_FAILED", "RETRY_EXHAUSTED", "RUN_INTERRUPTED"):
            with self.subTest(block=block):
                self.assertIn("task resume", self.actions("BLOCKED", block))

    def test_input_required_offers_answer_and_not_resume(self):
        actions = self.actions("BLOCKED", "INPUT_REQUIRED")
        self.assertIn("task answer", actions)
        # resume 会被 runtime 拒（STATE_CONFLICT）：并列给出等于先骗一次再报错。
        self.assertNotIn("task resume", actions)

    def test_process_unknown_points_at_reconciliation_instead_of_resume(self):
        actions = self.actions("BLOCKED", "PROCESS_UNKNOWN")
        self.assertIn("task recover", actions)
        self.assertNotIn("task resume", actions)

    def test_paused_still_offers_resume(self):
        self.assertIn("task resume", self.actions("PAUSED"))

    def test_terminal_states_offer_no_execution(self):
        for state in ("DONE", "CANCELLED"):
            with self.subTest(state=state):
                actions = self.actions(state)
                for action in ("task start", "task resume", "task answer"):
                    self.assertNotIn(action, actions)


class TestSharedResourcesIsADeclarationOnly(Fixture):
    """B5：字段接受的是一条**声明**，不是互斥锁。

    这里只锁住"它能被规范化并原样保留"这件事——过度承诺在这份字段上已经发生过
    一次（注释声称共享即串行，代码里没有任何实现），所以 docs 里已改为"仅声明"，
    不能靠一条测试来证明"没有串行"，但可以锁住它别被悄悄删掉。
    """

    def test_shared_resources_round_trips(self):
        c = _config.validate_config(
            self.config(shared_resources=[{"kind": "port", "id": "5432"}])
        )
        self.assertEqual(c["shared_resources"], [{"kind": "port", "id": "5432"}])


class TestSkillDocAnchors(unittest.TestCase):
    """设计文档的每一项都有一个"能被 grep 到的落点"。

    这不是文风检查：上一轮的缺陷正是"运行期改了、文档没改"（`timeout_seconds`
    两仓都没写）或"文档承诺了、代码没有"（`shared_resources` 串行化）。这里只锁
    关键锚点，保证删除/回退时失败，而不是靠读一遍 diff。
    """

    @classmethod
    def setUpClass(cls):
        from skill_package import SKIP_REASON, skill_dir

        cls.skill = skill_dir()
        if cls.skill is None:
            raise unittest.SkipTest(SKIP_REASON)

    def text(self, relative):
        return (self.skill / relative).read_text(encoding="utf-8")

    def test_skill_md_carries_the_dispatch_gate_and_overlap_precheck(self):
        body = self.text("SKILL.md")
        for anchor in ("派发闸门", "并行重叠预检", "共享集成点", "三步诊断"):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, body)
        # 两段式入口必须从主流程可达，否则等于没发布。
        self.assertIn("zoe-resume.sh", body)
        self.assertIn("zoe-attest.sh", body)

    def test_decomposition_patterns_refuses_the_append_only_shortcut(self):
        body = self.text("references/decomposition-patterns.md")
        self.assertIn("共享集成点", body)
        self.assertIn("前置 scaffold", body)
        self.assertIn("顺序追加", body)  # 作为反例出现，不是建议
        # 三维度：拆分依据不能靠时长/行数。
        for anchor in ("可独立验收", "可独立恢复", "依赖与冲突"):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, body)

    def test_recovery_policy_covers_timeout_and_points_at_the_script(self):
        body = self.text("references/recovery-policy.md")
        self.assertIn("RUN_TIMEOUT", body)
        self.assertIn("zoe-resume.sh", body)

    def test_runtime_contract_documents_the_single_timeout_definition(self):
        body = self.text("references/runtime-contract.md")
        self.assertIn("timeout_seconds", body)
        self.assertIn("run_timeout_seconds", body)
        self.assertIn("[stop] reason=", body)
        self.assertIn("--max-seconds", body)

    def test_worker_prompt_requires_slice_by_slice_evidence(self):
        self.assertIn("可交付切片", self.text("references/worker-prompt.md"))


if __name__ == "__main__":
    unittest.main()
