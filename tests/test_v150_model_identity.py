"""v1.5.1 模型观测：请求回显、供应商侧模型名、声明等价（alias_confirmed）。

这些用例来自一次真实 CLI 采集（codebuddy 2.111.0，2026-11-04），不是构造出的形状：

* ``system``/``init`` 行回显的是**请求**：``model = "custom-local:BC-DeepSeek-V4-Flash"``；
* ``assistant`` 行里 ``message.model`` 是**供应商侧**：``deepseek-v4.1-flash``；
* 两者的字符串不同，因此"按字面比对"会把一次正常运行判成 ``mixed``。

结论必须是"观测到了什么"的直接后果，而不是推断：

* 只有请求回显 ⇒ ``unobserved``（回显不证明用了什么）；
* 上报值在 profile 声明的 ``model_aliases`` 里 ⇒ ``alias_confirmed``；
* 没有声明等价名 ⇒ ``mixed``（两个不同的名字，不能自己假设它们相等）。
"""

from __future__ import annotations

import contextlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from src import _profiles, _routing
from src._agents import base
from src._agents.codebuddy import CodeBuddyAdapter
from src._pipeline import _observe_run
from src.cli import _build_parser, _task_recover

#: 一次真实 stream-json 采集的最小等价形状（内容已脱敏，键名与层级保持原样）。
STREAM = "\n".join(
    [
        '{"type":"system","subtype":"init","session_id":"s1",'
        '"model":"custom-local:BC-DeepSeek-V4-Flash"}',
        '{"type":"assistant","session_id":"s1",'
        '"message":{"role":"assistant","model":"deepseek-v4.1-flash",'
        '"content":[{"type":"text","text":"OK"}]}}',
        '{"type":"result","subtype":"success","is_error":false,"result":"OK",'
        '"session_id":"s1","total_cost_usd":0.001,'
        '"usage":{"input_tokens":10,"output_tokens":2}}',
    ]
)

REQUESTED = "BC-DeepSeek-V4-Flash"
ALIASES = ["deepseek-v4.1-flash", "custom-local:BC-DeepSeek-V4-Flash"]


def capture(raw: str) -> base.ProcessCapture:
    return base.ProcessCapture(
        adapter_id="codebuddy",
        adapter_backend="codebuddy-cli",
        exit_code=0,
        raw_text=raw,
        truncated=False,
        expected_version="2.111.0",
    )


def profile(**overrides) -> dict:
    value = {
        "adapter": "codebuddy",
        "backend": "codebuddy-cli",
        "permission_profile": "isolated-edit-and-run",
        "model": REQUESTED,
    }
    value.update(overrides)
    return value


class TestModelOf(unittest.TestCase):
    def test_provider_block_is_read(self):
        """``--output-format json`` 里顶层没有 model，只有 providerData.model。"""
        obj = {"type": "assistant", "providerData": {"model": "deepseek-v4.1-flash"}}
        self.assertEqual(base.AgentAdapterBase.model_of(obj), "deepseek-v4.1-flash")

    def test_provider_block_nested_in_message_is_read(self):
        obj = {"message": {"providerData": {"model": "provider-id"}}}
        self.assertEqual(base.AgentAdapterBase.model_of(obj), "provider-id")

    def test_placeholder_is_not_an_identifier(self):
        """ "unknown" 是"工具没说"，把它当成模型值会把未观测改写成不匹配。"""
        self.assertIsNone(base.AgentAdapterBase.model_of({"model": "unknown"}))
        self.assertIsNone(base.AgentAdapterBase.model_of({"model": "  "}))

    def test_message_model_still_wins_over_prose(self):
        obj = {"result": "the model was m2", "message": {"model": "m1"}}
        self.assertEqual(base.AgentAdapterBase.model_of(obj), "m1")


class TestObserveModels(unittest.TestCase):
    def setUp(self):
        self.adapter = CodeBuddyAdapter()
        self.result = self.adapter.parse_output(capture(STREAM))

    def test_terminal_observation_ignores_the_request_echo(self):
        self.assertEqual(self.result.outcome, base.OUTCOME_COMPLETED)
        self.assertEqual(self.result.reported_model, "deepseek-v4.1-flash")

    def test_echo_is_kept_for_audit_with_its_own_scope(self):
        models = {entry["model"]: entry["scope"] for entry in self.result.reported_models}
        self.assertEqual(models["custom-local:BC-DeepSeek-V4-Flash"], "request_echo")
        self.assertEqual(models["deepseek-v4.1-flash"], "main_call")


class TestModelIdentity(unittest.TestCase):
    def setUp(self):
        self.result = CodeBuddyAdapter().parse_output(capture(STREAM))

    def identify(self, aliases=None, requested=REQUESTED):
        return _routing.model_identity(
            requested, self.result.reported_model, self.result.reported_models, aliases
        )

    def test_echo_alone_proves_nothing(self):
        identity = _routing.model_identity(
            REQUESTED,
            None,
            [{"model": "custom-local:BC-DeepSeek-V4-Flash", "scope": "request_echo"}],
        )
        self.assertEqual(identity["model_identity"], "unobserved")
        self.assertEqual(identity["observation_scope"], "none")
        self.assertEqual(identity["request_echoes"], ["custom-local:BC-DeepSeek-V4-Flash"])

    def test_without_declared_alias_the_provider_name_is_a_mismatch(self):
        identity = self.identify()
        self.assertEqual(identity["model_identity"], "reported_mismatch")
        self.assertTrue(_routing.pin_blocks({"pin_model": True}, identity["model_identity"]))

    def test_declared_alias_yields_alias_confirmed_not_a_match(self):
        identity = self.identify(aliases=ALIASES)
        self.assertEqual(identity["model_identity"], "alias_confirmed")
        self.assertEqual(identity["identity_basis"], "declared_alias")
        self.assertEqual(identity["observation_scope"], "main_call")
        self.assertEqual(identity["unmatched_models"], [])
        self.assertFalse(_routing.pin_blocks({"pin_model": True}, identity["model_identity"]))

    def test_a_model_outside_the_declared_set_is_still_a_mismatch(self):
        identity = _routing.model_identity(
            REQUESTED,
            "other-model",
            [{"model": "other-model", "scope": "final_result"}],
            ALIASES,
        )
        self.assertEqual(identity["model_identity"], "reported_mismatch")
        self.assertEqual(identity["unmatched_models"], ["other-model"])

    def test_exact_match_keeps_its_own_basis(self):
        identity = _routing.model_identity(
            REQUESTED, REQUESTED, [{"model": REQUESTED, "scope": "final_result"}], ALIASES
        )
        self.assertEqual(identity["model_identity"], "reported_match")
        self.assertEqual(identity["identity_basis"], "exact")

    def test_unobserved_never_counts_as_a_verified_combination(self):
        """「工具没说」不是「对上了」：闸门这样判，组合事实也必须这样判。

        否则同一个 run 会在 ``block_code=MODEL_MISMATCH`` 之外被写成
        ``combination_verified=true``——一句"该组合被实测过"的谎话。
        """
        run = {
            "cli_version": "1.2.4",
            "result_schema": "agent-adapter",
            "model_unresolved": False,
        }
        usage = {"input_tokens": 10, "output_tokens": 5, "cost": 0.01}
        for identity, expected in (
            ("reported_match", True),
            ("alias_confirmed", True),
            ("unobserved", False),
            ("reported_mismatch", False),
            ("mixed", False),
            (None, False),
        ):
            self.assertIs(
                _routing.combination_verified(
                    run=run, usage=usage, model_identity_value=identity
                ),
                expected,
                identity,
            )

    def test_alias_confirmed_is_counted_but_not_capability_comparable(self):
        record = _routing.attribution(
            outcome="completed",
            failure_class=None,
            model_identity_value="alias_confirmed",
            purpose="task",
        )
        self.assertEqual(record["attempt_metrics"], "counted")
        self.assertEqual(record["delivery_metrics"], "counted")
        self.assertFalse(record["model_capability_comparable"])
        self.assertIn("declared model_aliases", record["reason"])


class TestRunObservationWiring(unittest.TestCase):
    """``_observe_run`` 必须把 profile 的声明带到判定里，否则别名等于没写。"""

    class Store:
        def __init__(self):
            self.saved = []

        def run(self, record):
            self.saved.append(dict(record))

    def observe(self, aliases):
        result = CodeBuddyAdapter().parse_output(capture(STREAM))
        binding = SimpleNamespace(profile=profile(model_aliases=aliases) if aliases else profile())
        record = {"run_id": "run-1", "status": "exited"}
        _observe_run(self.Store(), {"purpose": "task"}, record, binding, result)
        return record

    def test_run_record_carries_identity_basis_and_aliases(self):
        record = self.observe(ALIASES)
        self.assertEqual(record["model_identity"], "alias_confirmed")
        self.assertEqual(record["identity_basis"], "declared_alias")
        self.assertEqual(record["model_aliases"], ALIASES)
        self.assertEqual(record["reported_model"], "deepseek-v4.1-flash")

    def test_run_without_aliases_reports_a_mismatch(self):
        record = self.observe(None)
        self.assertEqual(record["model_identity"], "reported_mismatch")
        self.assertEqual(record["identity_basis"], "mismatch")


class TestProfileValidation(unittest.TestCase):
    def setUp(self):
        self.adapter = CodeBuddyAdapter()

    def test_alias_set_is_accepted(self):
        self.adapter.validate_profile(profile(model_aliases=ALIASES))

    def test_alias_without_model_is_refused(self):
        value = profile(model_aliases=ALIASES)
        value.pop("model")
        with self.assertRaises(base.Error) as caught:
            self.adapter.validate_profile(value)
        self.assertEqual(caught.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("model_aliases requires model", str(caught.exception))

    def test_duplicate_alias_is_refused(self):
        with self.assertRaises(base.Error) as caught:
            self.adapter.validate_profile(profile(model_aliases=["m", "m"]))
        self.assertEqual(caught.exception.code, "INPUT_INCOMPLETE")

    def test_alias_must_be_a_string_array(self):
        with self.assertRaises(base.Error):
            self.adapter.validate_profile(profile(model_aliases=["m", 3]))

    def test_normalize_keeps_the_field_and_still_resolves_the_model(self):
        with tempfile.TemporaryDirectory() as repo:
            config = {
                "schema_version": 2,
                "project_id": "demo",
                "repo": repo,
                "backend": "local",
                "default_agent_profile": "cb",
                "agent_profiles": {"cb": profile(model_aliases=ALIASES)},
                "allowed_paths": ["src"],
                "authorization_ref": "scope-1",
                "authorized_actions": ["create"],
            }
            normalized = _profiles.normalize_config(config)
        self.assertEqual(normalized["agent_profiles"]["cb"]["model_aliases"], ALIASES)
        self.assertFalse(_profiles.model_unresolved(normalized["agent_profiles"]["cb"]))


class TestRecoverDefaults(unittest.TestCase):
    """``task recover`` 的 --run 缺省值必须有来源可查，且 --json 可直接通过。"""

    class Store:
        def __init__(self, current_run="run-1"):
            self.task_record = {
                "task_id": "task-1",
                "project_id": "p1",
                "state": "BLOCKED",
                "current_run_id": current_run,
            }
            self.run_record = {"run_id": "run-1", "pid": 123, "status": "unknown"}
            self.events = []

        @contextlib.contextmanager
        def lock(self, name=None, blocking=True):
            yield

        def task(self, task_id):
            return self.task_record

        def get_run(self, run_id):
            return self.run_record if run_id == self.run_record["run_id"] else None

        def run(self, record):
            pass

        def save(self, task, event, **kwargs):
            self.events.append(event)

    def recover(self, store, run_id):
        with mock.patch("src.cli.alive", return_value=False):
            return _task_recover(store, "task-1", run_id, True)

    def test_current_run_id_is_the_default_and_says_so(self):
        record = self.recover(self.Store(), None)
        self.assertEqual(record["run_id"], "run-1")
        self.assertEqual(record["run_source"], "current_run")
        self.assertEqual(record["final_state"], "RUN_INTERRUPTED")

    def test_explicit_run_is_marked_explicit(self):
        record = self.recover(self.Store(), "run-1")
        self.assertEqual(record["run_source"], "explicit")

    def test_missing_run_is_refused_rather_than_guessed(self):
        with self.assertRaises(base.Error) as caught:
            self.recover(self.Store(current_run=None), None)
        self.assertEqual(caught.exception.code, "INPUT_INCOMPLETE")

    def test_parser_accepts_json_and_optional_run(self):
        args = _build_parser().parse_args(["task", "recover", "task-1", "--confirm-stop", "--json"])
        self.assertIsNone(args.run)
        self.assertTrue(args.json)


if __name__ == "__main__":
    unittest.main()
