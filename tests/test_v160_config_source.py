"""v1.6: 配置来源可观测 —— "改了 project.json 但没 register" 不再静默。

现场症状（一次真实批次）：改了 CLI 的 ``settings.json`` 里的模型，派发的任务却一直
用旧模型名，402/429 反复失败，排查花了 6~8 轮 resume/re-dispatch。事后读代码，adapter
根本不读 ``settings.json``（``codebuddy.py`` 只在 profile 声明 ``model`` 时拼
``--model``）；真正的静默点在更前面：

* 运行器只读 ``<home>/projects/<id>.json``（``project register`` 的落盘副本）；
* 磁盘上的 ``project.json`` 从此不再被读；
* 于是"改了配置文件、没重新注册"完全无声——``doctor`` 照常 ready，派发照常用旧值。

这里锁定三件事：
1. ``register`` 记下来源文件与字段摘要（sidecar，不进 ``policy_digest``）；
2. ``doctor`` 能回答"磁盘上的配置与注册时是否一致、改了哪些顶层键"；
3. 兜底码 ``AGENT_FAILED`` 上带一条**未确认线索**（``hint``），码本身不变。
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from src import _config_source, cli
from src._agents import base as agent_base
from src._store import Store


class Fixture(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.home = self.root / "home"
        self.store = Store(self.home)
        self.config_path = self.root / "project.json"
        repo = self.root / "repo"
        repo.mkdir()
        self.config = {
            "schema_version": 1,
            "project_id": "demo",
            "repo": str(repo),
            "backend": "local-process",
            "network": "none",
            "compatibility_verified": True,
            "agent": "codebuddy",
            "checks": [{"id": "unit", "argv": ["true"]}],
            "allowed_paths": ["src.txt"],
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute"],
            "ci_required": False,
            "max_attempts": 3,
        }
        self.write_config()

    def tearDown(self):
        self.store.db.close()
        self._td.cleanup()

    def write_config(self, **overrides):
        if overrides:
            self.config = {**self.config, **overrides}
        self.config_path.write_text(json.dumps(self.config))

    def run_cli(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv)
        return code, json.loads(buf.getvalue())


class TestRemember(Fixture):
    def test_missing_record_is_not_recorded_and_not_changed(self):
        state = _config_source.state(self.store, "demo", self.config)
        self.assertFalse(state["recorded"])
        self.assertIsNone(state["changed"])
        self.assertEqual(state["reason"], "not_recorded")
        # "没有记录"不等于"没有变化"：提示必须留空，不能编一条出来。
        self.assertIsNone(_config_source.hint(state))

    def test_same_file_after_register(self):
        _config_source.remember(self.store, "demo", self.config_path, self.config)
        state = _config_source.state(self.store, "demo", self.config)
        self.assertTrue(state["recorded"])
        self.assertFalse(state["changed"])
        self.assertEqual(state["changed_fields"], [])
        self.assertEqual(state["reason"], "same")
        self.assertEqual(state["source"], str(self.config_path.resolve()))
        self.assertIsNone(_config_source.hint(state))

    def test_edit_without_reregister_is_reported_with_field_names(self):
        _config_source.remember(self.store, "demo", self.config_path, self.config)
        self.write_config(allowed_paths=["src.txt", "docs"])
        state = _config_source.state(self.store, "demo", self.config)
        self.assertTrue(state["changed"])
        self.assertEqual(state["changed_fields"], ["allowed_paths"])
        hint = _config_source.hint(state)
        self.assertIn("allowed_paths", hint)
        self.assertIn("project register", hint)

    def test_model_change_is_localised_to_the_profile_key(self):
        _config_source.remember(self.store, "demo", self.config_path, self.config)
        profiles = {
            "cb-run": {"adapter": "codebuddy", "backend": "codebuddy-cli", "model": "new-model"}
        }
        self.write_config(agent_profiles=profiles)
        state = _config_source.state(self.store, "demo", self.config)
        self.assertEqual(state["changed_fields"], ["agent_profiles"])

    def test_deleted_source_is_unknown_not_unchanged(self):
        _config_source.remember(self.store, "demo", self.config_path, self.config)
        self.config_path.unlink()
        state = _config_source.state(self.store, "demo", self.config)
        self.assertTrue(state["recorded"])
        self.assertFalse(state["source_exists"])
        self.assertIsNone(state["changed"])
        self.assertEqual(state["reason"], "source_missing")


class TestCli(Fixture):
    def test_register_records_source_and_doctor_reports_it(self):
        code, payload = self.run_cli(
            ["--home", str(self.home), "project", "register", "--config", str(self.config_path)]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["registered"], "demo")
        self.assertEqual(payload["source"], str(self.config_path.resolve()))

        # `doctor` 本体在此打桩：本用例验的是"字段接上了没有"，不是环境探测本身
        # （探测会起子进程，取决于本机装了什么 CLI）。
        from unittest import mock

        fake = {"ready": True, "checks": {}, "project_id": "demo"}
        with mock.patch.object(cli, "doctor", lambda c, profile=None: dict(fake)):
            code, doctor = self.run_cli(["--home", str(self.home), "doctor", "--project", "demo"])
            self.assertEqual(code, 0)
            self.assertTrue(doctor["config_source"]["recorded"])
            self.assertFalse(doctor["config_source"]["changed"])

            # 改了文件但不注册：doctor 必须说出来，且不影响 ready（注册副本才是权威）。
            self.write_config(allowed_paths=["src.txt", "docs"])
            code, doctor = self.run_cli(["--home", str(self.home), "doctor", "--project", "demo"])
            self.assertEqual(code, 0)
            self.assertTrue(doctor["ready"])
            self.assertTrue(doctor["config_source"]["changed"])
            self.assertEqual(doctor["config_source"]["changed_fields"], ["allowed_paths"])
            self.assertIn("hint", doctor["config_source"])


class TestFailureHint(unittest.TestCase):
    def test_model_rejection_stays_agent_failed_but_carries_a_hint(self):
        # 码不变：`MODEL_UNAVAILABLE` 的可靠识别需要适配器 marker（契约变更），
        # 靠文本改码会把"未归因"伪造成"已归因"。
        self.assertEqual(
            agent_base.classify_failure("Error: Unknown model 'glm-5.3-flash'"),
            agent_base.OUTCOME_FAILED,
        )
        self.assertEqual(
            agent_base.failure_hint("Error: Unknown model 'glm-5.3-flash'"), "model_not_found"
        )
        result = agent_base.AgentResult(
            agent_id="codebuddy",
            agent_version="2.111.0",
            outcome=agent_base.OUTCOME_FAILED,
            exit_code=1,
            summary="Error: Unknown model 'glm-5.3-flash'",
        )
        with self.assertRaises(Exception) as ctx:
            result.require_completed()
        self.assertEqual(ctx.exception.code, "AGENT_FAILED")
        self.assertEqual(ctx.exception.details["hint"], "model_not_found")

    def test_gateway_and_unknown_are_distinguished(self):
        self.assertEqual(agent_base.failure_hint("502 Bad Gateway"), "gateway_error")
        self.assertIsNone(agent_base.failure_hint("the agent gave up"))

    def test_completed_result_has_no_hint_channel(self):
        result = agent_base.AgentResult(
            agent_id="codebuddy",
            agent_version="2.111.0",
            outcome=agent_base.OUTCOME_COMPLETED,
            exit_code=0,
            summary="done",
        )
        result.require_completed()  # 不抛错
