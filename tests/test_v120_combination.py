"""v1.2 execution-combination routing: config, decision, gates, report.

These tests are deliberately mostly **pure**: `decide()` takes a snapshot dict, so
the three-layer rules (L1 declared capability, L2 integration fact, L3 history
never used) are table-driven and need no CLI, no network and no clock. The
integration tests patch `collect_snapshot` so "doctor says ready" is a stated
assumption instead of an ambient property of the machine running the suite.

Real end-to-end status stays ``NOT_RUN`` in ``docs/capabilities.md``.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from src import _config, _profiles, _routing, _worktree
from src.cli import _EXIT_CODES, _build_parser, _route_command, capabilities
from src.core import Error, Store, atomic, create, digest, git, read

ROOT = Path(__file__).resolve().parents[1]


def ready_snapshot(*profile_ids, version="1.2.3"):
    """A snapshot in which every named candidate is runnable."""
    return {
        "candidates": {
            pid: {
                "profile_id": pid,
                "ready": True,
                "resolution_path": f"/usr/bin/{pid}",
                "resolution_source": "path",
                "cli_version": version,
                "error": None,
            }
            for pid in profile_ids
        },
        "budget": {
            "candidates": len(profile_ids),
            "probes": len(profile_ids),
            "elapsed_ms": 1,
            "exhausted": False,
        },
    }


class ConfigFixture(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def profile(self, **overrides):
        value = {
            "adapter": "codebuddy",
            "backend": "codebuddy-cli",
            "permission_profile": "isolated-edit",
            "expected_version": "1.2.3",
            "model": "stub-model",
        }
        value.update(overrides)
        return value

    def config(self, **overrides):
        value = {
            "schema_version": 2,
            "project_id": "routed",
            "repo": str(ROOT),
            "backend": "local",
            "default_agent_profile": "cb",
            "agent_profiles": {"cb": self.profile()},
            "checks": [{"id": "unit", "argv": ["true"]}],
            "allowed_paths": ["src.txt"],
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute"],
        }
        value.update(overrides)
        # v1.5.0 M3：声明 routing 段必须显式声明白名单。本 fixture 的用例测的是
        # 路由决策本身，不是白名单规则，所以这里补成"唯一那个 profile"；白名单
        # 缺省被拒的规则由 TestAllowlistMustBeExplicit 直接构造配置覆盖。
        routing = value.get("routing")
        if isinstance(routing, dict) and "allowed_profiles" not in routing:
            routing = dict(routing)
            routing["allowed_profiles"] = sorted(value["agent_profiles"])
            value["routing"] = routing
        return value

    def normalized(self, **overrides):
        return _profiles.normalize_config(self.config(**overrides))


class TestConfigNormalization(ConfigFixture):
    def test_no_routing_section_keeps_the_document_unsectioned(self):
        """§16.1: absent means absent — no writeback, so the digest is unchanged."""
        c = self.normalized()
        self.assertNotIn("routing", c)
        self.assertNotIn("endpoints", c)

    def test_routing_defaults_are_written_inside_the_section_only(self):
        c = self.normalized(routing={"mode": "auto"})
        routing = c["routing"]
        self.assertEqual(routing["allowed_profiles"], ["cb"])
        self.assertEqual(routing["fallback"], "ask")
        self.assertEqual(routing["max_assignments"], 2)
        self.assertEqual(routing["cost_ceiling"], "premium")
        self.assertEqual(
            routing["spend_guard"],
            {
                "max_cost_per_task": None,
                "max_cost_per_day": None,
                "currency": None,
                "on_exceed": "block",
            },
        )
        self.assertIs(routing["use_performance"], False)

    def test_history_never_drives_selection(self):
        """C9: `use_performance=true` is refused, not accepted-and-ignored."""
        with self.assertRaises(Error) as ctx:
            self.normalized(routing={"mode": "auto", "use_performance": True})
        self.assertEqual(ctx.exception.code, "CAPABILITY_UNSUPPORTED")

    def test_speculative_guard_is_refused(self):
        with self.assertRaises(Error) as ctx:
            self.normalized(routing={"mode": "auto", "spend_guard": {"on_exceed": "warn"}})
        self.assertEqual(ctx.exception.code, "CAPABILITY_UNSUPPORTED")

    def test_default_profile_must_be_inside_the_approved_set(self):
        with self.assertRaises(Error) as ctx:
            self.normalized(routing={"mode": "auto", "allowed_profiles": ["other"]})
        self.assertIn(ctx.exception.code, ("INPUT_INCOMPLETE",))

    def test_rule_may_not_prefer_a_profile_outside_the_approved_set(self):
        with self.assertRaises(Error) as ctx:
            self.normalized(
                routing={
                    "mode": "auto",
                    "rules": [{"task_type": "bugfix", "prefer": ["ghost"]}],
                }
            )
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")

    def test_duplicate_rule_for_one_task_type_is_ambiguous(self):
        with self.assertRaises(Error) as ctx:
            self.normalized(
                routing={
                    "mode": "auto",
                    "rules": [
                        {"task_type": "*", "prefer": ["cb"]},
                        {"task_type": "*", "prefer": ["cb"]},
                    ],
                }
            )
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")

    def test_profile_must_declare_a_model(self):
        with self.assertRaises(Error) as ctx:
            self.normalized(agent_profiles={"cb": self.profile(model=None)})
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("model", str(ctx.exception))

    def test_escape_hatch_is_explicit_and_visible(self):
        c = self.normalized(
            agent_profiles={"cb": self.profile(model=None, allow_cli_default_model=True)}
        )
        self.assertTrue(_routing.model_unresolved(c["agent_profiles"]["cb"]))

    def test_legacy_v1_shape_is_migrated_without_breaking_the_digest(self):
        legacy = {
            "schema_version": 1,
            "project_id": "legacy",
            "repo": str(ROOT),
            "backend": "local",
            "agent": "codebuddy",
            "checks": [{"id": "unit", "argv": ["true"]}],
            "allowed_paths": ["src.txt"],
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute"],
        }
        normalized = _profiles.normalize_config(legacy)
        profile = normalized["agent_profiles"][normalized["default_agent_profile"]]
        self.assertTrue(profile["legacy"])
        self.assertTrue(_profiles.model_unresolved(profile))

    def test_normalization_adds_no_v12_keys_to_an_old_config(self):
        """§16.1/§19-1: 旧配置的 digest 只能因为码里**原本就有**的缺省而变。"""
        old = self.config()
        old["agent_profiles"]["cb"] = {
            "adapter": "codebuddy",
            "backend": "codebuddy-cli",
            "permission_profile": "isolated-edit",
            "model": "m1",
            "expected_version": "1.2.3",
        }
        normalized = _profiles.normalize_config(old)
        # 段本身只在配置里写过时存在。
        self.assertNotIn("routing", normalized)
        self.assertNotIn("endpoints", normalized)
        profile = normalized["agent_profiles"]["cb"]
        for new_field in (
            "provider",
            "model_variant",
            "reasoning_effort",
            "capabilities",
            "endpoint_ref",
            "unit_price",
            "cost_class",
            "execution",
            "allow_cli_default_model",
        ):
            self.assertNotIn(new_field, profile)
        # v1.0.0 就存在的缺省仍会写回（image / network_profile 等），这不算漂移；
        # 但它们必须构成"新增键"的全部。
        added = set(profile) - set(old["agent_profiles"]["cb"])
        self.assertLessEqual(added, {"image", "network_profile"})

    def test_unknown_profile_field_is_refused(self):
        """§19-12: 未知字段必须让注册失败，而不是被静默忽略。

        白名单在 **profile** 层（`PROFILE_FIELDS`）；项目顶层允许额外键，这是
        v1.0.0 起的既有行为，不在本版收紧范围内——测试只断言真正存在的闸门。
        """
        value = self.config()
        value["agent_profiles"]["cb"]["reasoning_efforts"] = "high"  # 拼错的字段名
        with self.assertRaises(Error) as ctx:
            _profiles.normalize_config(value)
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")

    def test_declared_field_that_no_adapter_maps_is_refused(self):
        """§6.3: 'no flag' is not the test; 'cannot reach a real config' is."""
        c = self.normalized(agent_profiles={"cb": self.profile(reasoning_effort="high")})
        # 绑定（= 注册）阶段就拒绝，而不是等到启动时才发现装配不上。
        with self.assertRaises(Error) as ctx:
            _profiles.resolve_binding(c, "cb")
        self.assertEqual(ctx.exception.code, "CAPABILITY_UNSUPPORTED")

    def test_opencode_maps_provider_into_the_model_form(self):
        c = self.normalized(
            agent_profiles={
                "oc": self.profile(
                    adapter="opencode",
                    backend="opencode-cli",
                    provider="anthropic",
                    expected_version="0.3.0",
                )
            },
            default_agent_profile="oc",
        )
        binding = _profiles.resolve_binding(c, "oc")
        self.assertEqual(binding.execution["mapped"]["provider"], "anthropic")
        launch = _profiles.launch_for(binding, _context(binding))
        self.assertIn("anthropic/stub-model", launch.argv)

    def test_endpoint_declaration_is_recorded_by_name_only(self):
        c = self.normalized(
            endpoints={"onprem": {"class": "private"}},
            agent_profiles={"cb": self.profile(endpoint_ref="onprem")},
        )
        self.assertEqual(c["endpoints"]["onprem"]["attestation"], "config-declared")
        endpoint = _routing.endpoint_of(c, c["agent_profiles"]["cb"])
        self.assertEqual(
            endpoint, {"id": "onprem", "class": "private", "attestation": "config-declared"}
        )

    def test_dangling_endpoint_reference_is_a_config_defect(self):
        c = self.normalized(agent_profiles={"cb": self.profile(endpoint_ref="ghost")})
        with self.assertRaises(Error) as ctx:
            _routing.endpoint_of(c, c["agent_profiles"]["cb"])
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")


def _context(binding):
    from src._agents.base import RunContext

    return RunContext(
        task_id="t",
        run_id="r",
        adapter_id=binding.adapter_id,
        adapter_backend=binding.backend,
        workspace=str(ROOT),
        prompt_file=str(ROOT / "p.txt"),
        prompt_hash="x",
        profile_id=binding.profile_id,
        profile_hash=binding.profile_hash,
        spec_revision=1,
        spec_hash="y",
        model=binding.profile.get("model"),
        permission_profile=binding.profile.get("permission_profile"),
        provider=binding.profile.get("provider"),
    )


class TestCombinationId(unittest.TestCase):
    def profile(self, **overrides):
        value = {"model": "m1", "permission_profile": "isolated-edit"}
        value.update(overrides)
        return value

    def build(self, **overrides):
        arguments = {
            "adapter_id": "codebuddy",
            "backend": "codebuddy-cli",
            "profile": self.profile(),
            "cli_version": "1.2.3",
            "endpoint": None,
        }
        arguments.update(overrides)
        return _routing.combination_id(**arguments)

    def test_is_prefixed_and_stable(self):
        first, second = self.build(), self.build()
        self.assertTrue(first.startswith("cmb-"))
        self.assertEqual(len(first), 20)
        self.assertEqual(first, second)

    def test_changes_with_model_version_provider_variant_effort(self):
        base = self.build()
        for change in (
            {"profile": self.profile(model="m2")},
            {"profile": self.profile(model_variant="thinking")},
            {"profile": self.profile(reasoning_effort="high")},
            {"profile": self.profile(provider="p")},
            {"profile": self.profile(permission_profile="full")},
            {"profile": self.profile(approved_tools=["Write"])},
            {"cli_version": "1.2.4"},
            {"endpoint": {"id": "onprem", "class": "private"}},
        ):
            self.assertNotEqual(base, self.build(**change), change)

    def test_no_absolute_paths_or_credentials_leak_in(self):
        value = json.dumps(self.build())
        self.assertNotIn("/", value)
        self.assertNotIn("Users", value)

    def test_argv_digest_cannot_enter_the_identity(self):
        """§19-3: 命令行摘要与组合标识是两件事——换路径不许变成"换了组合"。"""
        with self.assertRaises(TypeError):
            self.build(argv_audit_sha256="deadbeef")


class TestDecide(ConfigFixture):
    def routing_config(self, routing=None, **overrides):
        base = {"mode": "auto"}
        base.update(routing or {})
        config = dict(
            agent_profiles={
                "cb": self.profile(cost_class="low", capabilities={"input_modalities": ["text"]}),
                "cb2": self.profile(capabilities={"input_modalities": ["text"]}),
                "oc": self.profile(
                    adapter="opencode",
                    backend="opencode-cli",
                    capabilities={"input_modalities": ["text", "image"]},
                ),
            },
            routing=base,
        )
        config.update(overrides)
        return self.normalized(**config)

    def requirements(self, **values):
        return _routing.normalize_requirements(values or None, {"cost_class": "low"})

    def test_rule_preference_wins_and_reports_its_source(self):
        c = self.routing_config(
            {"rules": [{"task_type": "bugfix", "prefer": ["oc"]}], "fallback": "ask"}
        )
        plan = _routing.decide(
            c, self.requirements(), ready_snapshot("cb", "cb2", "oc"), task_type="bugfix"
        )
        self.assertEqual(plan["decision"], "selected")
        self.assertEqual(plan["selected_profile"], "oc")
        self.assertEqual(plan["reason"], "rule_preference")
        self.assertEqual(plan["order"]["source"], "user_preference")

    def test_single_candidate_is_its_own_reason_not_a_rule_hit(self):
        c = self.normalized(
            agent_profiles={"cb": self.profile()},
            routing={"mode": "auto", "rules": [{"task_type": "*", "prefer": ["cb"]}]},
        )
        plan = _routing.decide(c, self.requirements(), ready_snapshot("cb"))
        self.assertEqual(plan["reason"], "only_eligible")
        self.assertEqual(plan["order"]["source"], "only_eligible")

    def test_ambiguous_is_an_answer_not_a_guess(self):
        c = self.routing_config({"fallback": "ask"})
        plan = _routing.decide(
            c, self.requirements(), ready_snapshot("cb", "cb2", "oc"), task_type="feature"
        )
        self.assertEqual(plan["decision"], "ambiguous")
        self.assertEqual(plan["reason"], "no_rule")
        self.assertEqual(plan["fallback_used"], "ask")

    def test_explicit_fallback_is_recorded_as_such(self):
        c = self.routing_config({"fallback": "default_profile"})
        plan = _routing.decide(
            c, self.requirements(), ready_snapshot("cb", "cb2", "oc"), task_type="feature"
        )
        self.assertEqual(plan["selected_profile"], "cb")
        self.assertEqual(plan["reason"], "fallback_default")
        self.assertEqual(plan["fallback_used"], "default_profile")

    def test_project_default_outside_the_allowlist_is_denied(self):
        """默认 profile 也受白名单约束（SKILL.md：同时约束显式指定与自动选择）。

        规范化期会查 ``default in allowed``，但读路径是 ``strict=False``，且某个
        profile 规范化失败时配置会原样返回——那条路径上只有 ``decide()`` 能拦。
        """
        c = self.routing_config({"mode": "explicit", "allowed_profiles": ["cb"]})
        c["default_agent_profile"] = "cb2"
        with self.assertRaises(Error) as ctx:
            _routing.decide(c, self.requirements(), ready_snapshot("cb", "cb2"))
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")

    def test_layers_have_no_history_dimension(self):
        c = self.routing_config()
        plan = _routing.decide(c, self.requirements(), ready_snapshot("cb"), task_type="docs")
        self.assertNotIn("l3_excluded", plan["layers"])
        self.assertEqual(set(plan["layers"]), {"l1_excluded", "l2_excluded"})

    def test_cost_ceiling_is_an_exclusion_not_a_ranking(self):
        c = self.routing_config(
            {"cost_ceiling": "low", "rules": [{"task_type": "*", "prefer": ["cb2"]}]}
        )
        plan = _routing.decide(
            c, self.requirements(), ready_snapshot("cb", "cb2"), task_type="feature"
        )
        excluded = {item["profile_id"]: item["code"] for item in plan["layers"]["l1_excluded"]}
        self.assertEqual(excluded.get("cb2"), "COST_CEILING_EXCEEDED")
        self.assertEqual(plan["selected_profile"], "cb")

    def test_modality_requirement_excludes_on_declared_capability(self):
        c = self.routing_config()
        requirements = self.requirements(input_modalities=["image"])
        plan = _routing.decide(
            c, requirements, ready_snapshot("cb", "cb2", "oc"), task_type="feature"
        )
        self.assertEqual(
            [item["profile_id"] for item in plan["candidates"] if item["status"] == "selected"],
            ["oc"],
        )

    def test_context_budget_over_the_nominal_window_is_refused(self):
        c = self.routing_config()
        c["agent_profiles"]["cb"]["capabilities"]["context_window"] = 1000
        requirements = self.requirements(context_budget=4000)
        plan = _routing.decide(
            c, requirements, ready_snapshot("cb", "cb2", "oc"), task_type="feature"
        )
        detail = [item for item in plan["layers"]["l1_excluded"] if item["profile_id"] == "cb"][0]
        self.assertIn("exceeds nominal context_window", detail["detail"])

    def test_structured_result_also_requires_a_json_protocol(self):
        c = self.normalized(
            agent_profiles={
                "cb": self.profile(capabilities={"structured_result": True}),
                "oc": self.profile(
                    adapter="opencode",
                    backend="opencode-cli",
                    capabilities={"structured_result": True},
                ),
            },
            routing={"mode": "auto"},
        )
        requirements = self.requirements(structured_result=True)
        plan = _routing.decide(c, requirements, ready_snapshot("cb", "oc"), task_type="feature")
        self.assertEqual(plan["selected_profile"], "cb")
        codes = {item["profile_id"]: item["code"] for item in plan["layers"]["l1_excluded"]}
        self.assertEqual(codes.get("oc"), "CAPABILITY_UNSUPPORTED")

    def test_required_residency_without_a_mappable_endpoint_is_unverified(self):
        c = self.routing_config(
            {"rules": [{"task_type": "*", "prefer": ["cb"]}]},
            endpoints={"onprem": {"class": "private"}},
        )
        c["agent_profiles"]["cb"]["endpoint_ref"] = "onprem"
        requirements = self.requirements(data_residency="required")
        plan = _routing.decide(
            c, requirements, ready_snapshot("cb", "cb2", "oc"), task_type="feature"
        )
        detail = [item for item in plan["layers"]["l1_excluded"] if item["profile_id"] == "cb"][0]
        self.assertEqual(detail["code"], "ENDPOINT_UNVERIFIED")

    def test_real_e2e_policy_is_a_gate_on_integration_evidence(self):
        c = self.routing_config(require_real_e2e=True)
        plan = _routing.decide(
            c, self.requirements(), ready_snapshot("cb", "cb2", "oc"), task_type="feature"
        )
        self.assertEqual(plan["decision"], "no_eligible")
        self.assertEqual(
            {item["code"] for item in plan["layers"]["l2_excluded"]}, {"ADAPTER_UNVERIFIED"}
        )

    def test_probe_budget_counts_candidates_and_no_probe_skips_them(self):
        """§19-10: 探测次数 == 候选数；`--no-probe` 时为 0。"""
        c = self.normalized(agent_profiles={"cb": self.profile(), "cb2": self.profile()})
        calls = []

        def doctor(config, pid, probe=True):
            calls.append((pid, probe))
            return {
                "ready": True,
                "checks": {"adapter": {}},
                "resolution": {"path": "/usr/bin/agent", "version": "1.2.3", "source": "path"},
            }

        snapshot = _routing.collect_snapshot(c, ["cb", "cb2"], doctor_fn=doctor)
        self.assertEqual(snapshot["budget"]["probes"], 2)
        calls.clear()
        snapshot = _routing.collect_snapshot(c, ["cb", "cb2"], probe=False, doctor_fn=doctor)
        self.assertEqual(snapshot["budget"]["probes"], 0)
        self.assertTrue(calls and all(probe is False for _, probe in calls))

    def test_environment_failure_excludes_with_a_code_not_a_silent_skip(self):
        c = self.routing_config()
        snapshot = ready_snapshot("cb", "cb2", "oc")
        snapshot["candidates"]["oc"] = {
            "profile_id": "oc",
            "ready": False,
            "resolution_path": None,
            "error": {"code": "ENVIRONMENT_ERROR", "message": "codebuddy not found"},
        }
        plan = _routing.decide(c, self.requirements(), snapshot, task_type="feature")
        codes = {item["profile_id"]: item["code"] for item in plan["layers"]["l2_excluded"]}
        self.assertEqual(codes.get("oc"), "ENVIRONMENT_ERROR")

    def test_explicit_profile_outside_the_approved_set_is_denied(self):
        c = self.routing_config({"allowed_profiles": ["cb", "cb2"]})
        with self.assertRaises(Error) as ctx:
            _routing.decide(c, self.requirements(), ready_snapshot("oc"), explicit_profile="oc")
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")

    def test_explicit_profile_is_never_replaced_by_another_one(self):
        c = self.routing_config(
            {"allowed_profiles": ["cb", "cb2"], "rules": [{"task_type": "*", "prefer": ["cb"]}]}
        )
        snapshot = ready_snapshot("cb", "cb2")
        snapshot["candidates"]["cb2"] = {
            "profile_id": "cb2",
            "ready": False,
            "resolution_path": None,
            "error": {"code": "ENVIRONMENT_ERROR", "message": "not installed"},
        }
        plan = _routing.decide(c, self.requirements(), snapshot, explicit_profile="cb2")
        self.assertEqual(plan["decision"], "no_eligible")
        self.assertIsNone(plan["selected_profile"])

    def test_decision_is_deterministic(self):
        c = self.routing_config({"rules": [{"task_type": "*", "prefer": ["cb2"]}]})
        arguments = (c, self.requirements(), ready_snapshot("cb", "cb2", "oc"))
        first = _routing.decide(*arguments, task_type="feature")
        second = _routing.decide(*arguments, task_type="feature")
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_snapshot_digest_ignores_host_paths(self):
        first = ready_snapshot("cb")
        second = ready_snapshot("cb")
        second["candidates"]["cb"]["resolution_path"] = "/opt/homebrew/bin/cb"
        self.assertEqual(_routing.snapshot_digest(first), _routing.snapshot_digest(second))


class TestRequirements(ConfigFixture):
    def test_pin_defaults_to_premium_cost_class(self):
        premium = _routing.normalize_requirements(None, {"cost_class": "premium"})
        self.assertTrue(premium["pin_model"])
        self.assertEqual(premium["pin_model_source"], "cost_class_default")
        cheap = _routing.normalize_requirements(None, {"cost_class": "low"})
        self.assertFalse(cheap["pin_model"])

    def test_escape_hatch_and_pin_are_mutually_exclusive(self):
        with self.assertRaises(Error) as ctx:
            _routing.normalize_requirements(
                {"pin_model": True},
                {"model": None, "allow_cli_default_model": True, "cost_class": "premium"},
            )
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")

    def test_unknown_requirement_is_refused(self):
        with self.assertRaises(Error) as ctx:
            _routing.normalize_requirements({"quality": "high"}, {})
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")

    def test_session_resume_is_refused_by_declaration(self):
        c = self.normalized(agent_profiles={"cb": self.profile()}, routing={"mode": "auto"})
        plan = _routing.decide(
            c,
            _routing.normalize_requirements({"session_resume": True}, {}),
            ready_snapshot("cb"),
        )
        self.assertEqual(plan["decision"], "no_eligible")
        self.assertEqual(plan["candidates"][0]["code"], "CAPABILITY_UNSUPPORTED")


class TestModelIdentity(unittest.TestCase):
    def test_no_report_is_unobserved_not_matched(self):
        identity = _routing.model_identity("m1", None, [])
        self.assertEqual(identity["model_identity"], "unobserved")
        self.assertEqual(identity["observation_scope"], "none")

    def test_single_matching_report(self):
        identity = _routing.model_identity("m1", "m1", [{"model": "m1", "scope": "final_result"}])
        self.assertEqual(identity["model_identity"], "reported_match")
        self.assertEqual(identity["observation_scope"], "final_result")

    def test_mismatch_is_reported_with_both_values(self):
        identity = _routing.model_identity("m1", "m2", [{"model": "m2", "scope": "final_result"}])
        self.assertEqual(identity["model_identity"], "reported_mismatch")
        self.assertEqual(identity["reported_model"], "m2")

    def test_two_models_is_mixed(self):
        identity = _routing.model_identity(
            "m1",
            "m1",
            [{"model": "m1", "scope": "main_call"}, {"model": "m2", "scope": "main_call"}],
        )
        self.assertEqual(identity["model_identity"], "mixed")
        self.assertEqual(identity["observation_scope"], "mixed")

    def test_unresolved_request_cannot_claim_a_match(self):
        identity = _routing.model_identity(None, "m1", [{"model": "m1", "scope": "final_result"}])
        self.assertEqual(identity["model_identity"], "unobserved")

    def test_pin_blocks_unobserved_and_mismatch_only(self):
        pinned = {"pin_model": True}
        self.assertTrue(_routing.pin_blocks(pinned, "unobserved"))
        self.assertTrue(_routing.pin_blocks(pinned, "reported_mismatch"))
        self.assertFalse(_routing.pin_blocks(pinned, "reported_match"))
        self.assertFalse(_routing.pin_blocks({"pin_model": False}, "reported_mismatch"))


class TestCostAndSpend(ConfigFixture):
    def test_three_sources_are_never_mixed(self):
        reported = _routing.cost_observation({"cost": 0.5, "input_tokens": 1000}, {})
        self.assertEqual(reported, {"amount": 0.5, "cost_source": "reported"})
        estimated = _routing.cost_observation(
            {"cost": None, "input_tokens": 2000, "output_tokens": 1000},
            {"unit_price": {"input_per_1k": 1.0, "output_per_1k": 2.0, "currency": "USD"}},
        )
        self.assertEqual(estimated["cost_source"], "estimated")
        self.assertAlmostEqual(estimated["amount"], 2.0 + 2.0)
        unavailable = _routing.cost_observation({"cost": None}, {})
        self.assertEqual(unavailable, {"amount": None, "cost_source": "unavailable"})

    def test_limits_are_configured_only_when_declared(self):
        c = self.normalized(routing={"mode": "auto"})
        self.assertEqual(_routing.spend_limits(c)["max_cost_per_task"], None)

    def test_unverifiable_cost_with_a_configured_limit_is_refused(self):
        c = self.normalized(routing={"mode": "auto", "spend_guard": {"max_cost_per_task": 1.0}})
        store = Store(self.root / "state")
        self.addCleanup(store.db.close)
        with self.assertRaises(Error) as ctx:
            _routing.spend_gate(
                store,
                {"task_id": "t", "project_id": "routed"},
                c,
                {"amount": None, "cost_source": "unavailable"},
            )
        self.assertEqual(ctx.exception.code, "SPEND_UNVERIFIABLE")

    def test_currency_mismatch_is_not_a_comparison(self):
        c = self.normalized(
            routing={"mode": "auto", "spend_guard": {"max_cost_per_task": 1.0, "currency": "USD"}},
            agent_profiles={
                "cb": self.profile(
                    unit_price={"input_per_1k": 1, "output_per_1k": 1, "currency": "CNY"}
                )
            },
        )
        store = Store(self.root / "state")
        self.addCleanup(store.db.close)
        with self.assertRaises(Error) as ctx:
            _routing.spend_gate(
                store,
                {"task_id": "t", "project_id": "routed", "agent_profile_id": "cb"},
                c,
                {"amount": 0.1, "cost_source": "reported"},
            )
        self.assertEqual(ctx.exception.code, "SPEND_UNVERIFIABLE")


class TestAttribution(unittest.TestCase):
    def test_probe_runs_never_enter_quality_metrics(self):
        record = _routing.attribution(
            outcome="completed",
            failure_class=None,
            model_identity_value="reported_match",
            purpose="probe",
        )
        self.assertEqual(record["attempt_metrics"], "excluded")
        self.assertEqual(record["delivery_metrics"], "excluded")

    def test_environment_failures_are_not_the_executors_result(self):
        record = _routing.attribution(
            outcome="failed",
            failure_class="environment",
            model_identity_value="reported_match",
            purpose="task",
        )
        self.assertEqual(record["attempt_metrics"], "excluded")

    def test_unobserved_identity_allows_reliability_but_not_quality(self):
        record = _routing.attribution(
            outcome="completed",
            failure_class=None,
            model_identity_value="unobserved",
            purpose="task",
        )
        self.assertEqual(record["attempt_metrics"], "counted")
        self.assertEqual(record["delivery_metrics"], "excluded")

    def test_clean_run_counts(self):
        record = _routing.attribution(
            outcome="completed",
            failure_class=None,
            model_identity_value="reported_match",
            purpose="task",
        )
        self.assertEqual(record["delivery_metrics"], "counted")

    def test_failure_codes_map_to_classes(self):
        self.assertEqual(_routing.failure_class_of("AGENT_AUTH_REQUIRED"), "auth_permission")
        self.assertEqual(_routing.failure_class_of("VALIDATION_FAILED"), "verification")
        self.assertEqual(_routing.failure_class_of("MODEL_MISMATCH"), "executor")
        self.assertEqual(_routing.failure_class_of(None), None)


class TestWorktreeQuarantine(unittest.TestCase):
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
        self.task = {
            "task_id": "task-x",
            "project_id": "routed",
            "workspace": str(self.repo),
            "base_sha": git(self.repo, "rev-parse", "HEAD"),
            "config": {"allowed_paths": ["src.txt"]},
        }

    def tearDown(self):
        self.store.db.close()
        self._td.cleanup()

    def test_changes_are_archived_then_the_workspace_is_clean(self):
        self.task["config"]["allowed_paths"] = ["src.txt", "new.txt"]
        baseline = _worktree.baseline(self.task)
        self.assertTrue(baseline["available"])
        run = {"run_id": "run-x", "workspace_baseline": baseline}
        (self.repo / "src.txt").write_text("worse\n")
        (self.repo / "new.txt").write_text("untracked\n")
        record = _worktree.quarantine(self.store, self.task, run, "MODEL_MISMATCH")
        self.assertEqual(record["cleanup"], "done")
        patch = Path(record["patch_path"])
        self.assertTrue(patch.is_file())
        self.assertIn("git apply --stat", record["inspect"])
        text = patch.read_text()
        self.assertIn("new.txt", text)  # untracked files are captured, not lost
        self.assertIn("initial", (self.repo / "src.txt").read_text())
        self.assertFalse((self.repo / "new.txt").exists())

    def test_unrestorable_patch_aborts_and_keeps_the_work(self):
        """§19-29: 还原性校验失败时，一个字节都不许丢。"""
        baseline = _worktree.baseline(self.task)
        run = {"run_id": "run-abs", "workspace_baseline": baseline}
        (self.repo / "src.txt").write_text("precious work\n")
        real = subprocess.run

        def fake(argv, *args, **kwargs):
            if list(argv)[:2] == ["git", "apply"]:
                raise subprocess.CalledProcessError(1, argv)
            return real(argv, *args, **kwargs)

        with mock.patch("src._worktree.subprocess.run", side_effect=fake):
            record = _worktree.quarantine(self.store, self.task, run, "MODEL_MISMATCH")
        self.assertEqual(record["cleanup"], "aborted")
        self.assertIn("not restorable", record["detail"])
        self.assertEqual((self.repo / "src.txt").read_text(), "precious work\n")

    def test_files_outside_allowed_paths_are_left_and_reported(self):
        """`clean -fd` is scoped: deleting what we cannot prove is ours is worse."""
        baseline = _worktree.baseline(self.task)
        run = {"run_id": "run-out", "workspace_baseline": baseline}
        (self.repo / "src.txt").write_text("worse\n")
        (self.repo / "outside.txt").write_text("not ours\n")
        record = _worktree.quarantine(self.store, self.task, run, "MODEL_MISMATCH")
        self.assertEqual(record["cleanup"], "partial")
        self.assertIn("outside.txt", record["residue_paths"])
        self.assertIn("initial", (self.repo / "src.txt").read_text())
        self.assertTrue((self.repo / "outside.txt").exists())

    def test_a_dirty_start_is_never_cleaned(self):
        (self.repo / "src.txt").write_text("user work\n")
        baseline = _worktree.baseline(self.task)
        self.assertTrue(baseline["dirty_paths"])
        record = _worktree.quarantine(
            self.store,
            self.task,
            {"run_id": "run-y", "workspace_baseline": baseline},
            "MODEL_MISMATCH",
        )
        self.assertEqual(record["cleanup"], "skipped")
        self.assertEqual((self.repo / "src.txt").read_text(), "user work\n")

    def test_quarantine_is_idempotent(self):
        baseline = _worktree.baseline(self.task)
        run = {"run_id": "run-z", "workspace_baseline": baseline}
        (self.repo / "src.txt").write_text("worse\n")
        first = _worktree.quarantine(self.store, self.task, run, "MODEL_MISMATCH")
        second = _worktree.quarantine(self.store, self.task, run, "MODEL_MISMATCH")
        self.assertEqual(first["patch_sha256"], second["patch_sha256"])
        self.assertEqual(second["detail"], "already quarantined")


class TestCliSurface(unittest.TestCase):
    def test_route_is_a_real_command(self):
        parser = _build_parser()
        help_text = parser.format_help()
        self.assertIn("route", help_text)

    def test_new_exit_codes_are_frozen(self):
        self.assertEqual(_EXIT_CODES["MODEL_MISMATCH"], 5)
        self.assertEqual(_EXIT_CODES["MODEL_UNAVAILABLE"], 4)
        self.assertEqual(_EXIT_CODES["SPEND_UNVERIFIABLE"], 5)
        self.assertEqual(_EXIT_CODES["ROUTE_AMBIGUOUS"], 3)
        self.assertEqual(_EXIT_CODES["ROUTE_RECORD_INVALID"], 3)
        self.assertEqual(_EXIT_CODES["INPUT_INCOMPLETE"], 2)

    def test_not_supported_lists_what_v12_does_not_do(self):
        listed = capabilities()["not_supported"]
        for name in (
            "model_quality_scoring",
            "internal_routing_control",
            "performance_based_exclusion",
            "strict_model_enforcement_at_gateway",
        ):
            self.assertIn(name, listed)

    def test_capabilities_doc_never_claims_gateway_enforcement(self):
        """§19-27: 文档不得把"请求指定 + 事后作废"写成"保证只用指定模型"。"""
        text = (ROOT / "docs" / "capabilities.md").read_text()
        self.assertIn("strict_model_enforcement_at_gateway", text)
        for claim in ("保证只用指定模型", "确保只使用指定模型", "强制使用指定模型"):
            self.assertNotIn(claim, text)

    def test_there_is_no_way_to_override_a_gate_by_flag(self):
        text = _build_parser().format_help()
        for forbidden in ("apply-worktree", "restore-patch", "override-model-check"):
            self.assertNotIn(forbidden, text)


class RoutedFixture(ConfigFixture):
    """A registered project with `routing.mode=auto`, and a stubbed snapshot."""

    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "test")
        git(self.repo, "config", "user.email", "test@localhost")
        (self.repo / "src.txt").write_text("initial\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        self.spec = self.root / "spec.json"
        self.spec.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "title": "Routed work",
                    # §7.2: `route.task_type` > `spec.task_type` > unknown.
                    "task_type": "bugfix",
                    "acceptance_criteria": [
                        {
                            "id": "AC-001",
                            "statement": "the change exists",
                            "verification": {"type": "check", "check_id": "unit"},
                        }
                    ],
                }
            )
        )
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.db.close)
        self.config_value = self.project()
        self.write_config(self.config_value)

    def attest(self, c):
        """写一次授权确认记录（v1.5.0 §5）。

        本文件的用例验证的是路由与执行行为；确认流程本身、以及"配置变了确认就
        失效"由 TestAttestation 单独覆盖。声明了 routing 段又没有确认记录时，
        create/start/reassign 会一律 ALLOWLIST_UNCONFIRMED，所以这里先确认。
        """
        if c.get("routing") is None:
            return
        self.store.write_attestation(
            c["project_id"],
            {
                "schema_version": 1,
                "project_id": c["project_id"],
                "policy_hash": _profiles.policy_digest(c),
                "confirmed_profiles": (
                    c["routing"].get("allowed_profiles") or sorted(c["agent_profiles"])
                ),
                "runtime_version": "test",
                "confirmed_at": 0,
                "confirmed_by": "test",
            },
        )

    def write_config(self, c):
        """写项目配置并同时确认一次（v1.5.0 §5）。

        用例验证的是路由与执行行为；"配置一变确认就失效"由 TestAttestation 覆盖，
        所以这里每次都重新确认，避免每个用例都要先跑一遍确认流程。

        摘要必须按**读路径**规范化后的内容计算：``repo`` 会被 resolve（macOS 上
        ``/var`` → ``/private/var``），拿未规范化的配置算摘要会让确认永远匹配不上。
        """
        atomic(self.store.home / "projects" / "routed.json", c)
        normalized = _config.validate_config(
            json.loads(json.dumps(c)), base_dir=self.store.home / "projects"
        )
        self.attest(normalized)

    def project(self, **overrides):
        value = super().config(
            repo=str(self.repo),
            routing={
                "mode": "auto",
                "allowed_profiles": ["cb"],
                "rules": [{"task_type": "bugfix", "prefer": ["cb"]}],
            },
        )
        value.update(overrides)
        return value

    def request(self, **overrides):
        value = dict(
            schema_version=2,
            request_id="req-" + uuid.uuid4().hex,
            authorization_ref="scope-1",
            project_id="routed",
            title="Routed",
            spec_file=str(self.spec),
            delivery_mode="local_artifact",
        )
        value.update(overrides)
        return value

    def create(self, **overrides):
        with mock.patch(
            "src._routing.collect_snapshot", side_effect=lambda c, ids, **kw: ready_snapshot(*ids)
        ):
            return create(self.store, self.request(**overrides))

    def run_task(self, task):
        from src.core import run_pipeline

        task = self.store.task(task["task_id"])
        run_id = "run-" + uuid.uuid4().hex[:16]
        # `start` 会在派发前采基线；这里绕过 start 直接跑 pipeline，必须同样
        # 采一次，否则隔离退化为 skipped（"没有基线就无法区分改动归属"）。
        self.store.run(
            {
                "run_id": run_id,
                "task_id": task["task_id"],
                "status": "running",
                "container": None,
                "workspace_baseline": _worktree.baseline(task),
            }
        )
        task.update(current_run_id=run_id, state="RUNNING")
        self.store.save(task)
        return run_pipeline(self.store, task["task_id"], FakeBackend(), FakePublisher())

    def premium_project(self, **routing):
        base = {"mode": "auto", "allowed_profiles": ["cb"], "rules": []}
        base.update(routing)
        config = self.project(
            agent_profiles={"cb": self.profile(cost_class="premium")}, routing=base
        )
        self.write_config(config)
        return config


class TestCreateWithRouting(RoutedFixture):
    def test_decision_is_written_before_the_task_and_verified_later(self):
        result = self.create()
        task = self.store.task(result["task_id"])
        self.assertEqual(result["route_decision"], "selected")
        # 只有一个候选时理由必须说"唯一候选"，而不是假装偏好起了作用。
        self.assertEqual(result["route_reason"], "only_eligible")
        self.assertEqual(task["task_type"], "bugfix")
        self.assertTrue(task["combination_id"].startswith("cmb-"))
        self.assertTrue(task["route_id"].startswith("rt-"))
        path = self.store.path(task) / "route-01.json"
        self.assertTrue(path.is_file())
        document = read(path)
        self.assertEqual(document["content_sha256"], task["route_content_sha256"])
        self.assertEqual(document["selected_execution"]["profile_id"], "cb")
        self.assertEqual(document["trigger"], "create")
        self.assertEqual(document["mode"], "auto")

    def test_record_integrity_is_checked_at_start(self):
        result = self.create()
        task = self.store.task(result["task_id"])
        path = self.store.path(task) / "route-01.json"
        document = read(path)
        document["decision"] = "ambiguous"  # tamper, keep the digest
        atomic(path, document)
        from src.core import start

        with self.assertRaises(Error) as ctx:
            start(self.store, task["task_id"], self.request(expected_version=task["row_version"]))
        self.assertEqual(ctx.exception.code, "ROUTE_RECORD_INVALID")
        self.assertEqual(ctx.exception.details["problem"], "digest_mismatch")

    def test_no_routing_project_keeps_the_v10_response_shape(self):
        plain = super().config(repo=str(self.repo))
        atomic(self.store.home / "projects" / "plain.json", plain)
        request = self.request()
        request["project_id"] = "plain"
        result = create(self.store, request)
        for key in ("route_id", "route_reason", "route_decision", "routing_digest"):
            self.assertNotIn(key, result)
        task = self.store.task(result["task_id"])
        self.assertFalse((self.store.path(task) / "route-01.json").exists())

    def test_ambiguous_routing_refuses_creation(self):
        self.write_config(
            self.project(
                agent_profiles={
                    "cb": self.profile(),
                    "cb2": self.profile(),
                },
                routing={
                    "mode": "auto",
                    "allowed_profiles": ["cb", "cb2"],
                    "rules": [],
                    "fallback": "ask",
                },
                default_agent_profile="cb",
            ),
        )
        with self.assertRaises(Error) as ctx:
            self.create()
        self.assertEqual(ctx.exception.code, "ROUTE_AMBIGUOUS")
        self.assertEqual(self.store.tasks(), [])

    def test_no_eligible_reports_the_gaps(self):
        self.write_config(
            self.project(
                routing={"mode": "auto", "allowed_profiles": ["cb"], "rules": []},
                agent_profiles={"cb": self.profile(capabilities={"input_modalities": ["text"]})},
            ),
        )
        with self.assertRaises(Error) as ctx:
            self.create(route={"requirements": {"input_modalities": ["image"]}})
        self.assertEqual(ctx.exception.code, "ROUTE_NO_ELIGIBLE")
        codes = {gap["code"] for gap in ctx.exception.details["gaps"]}
        self.assertIn("CAPABILITY_UNSUPPORTED", codes)

    def test_explicit_profile_reports_its_own_exclusion_code(self):
        """§7.4: an explicit choice fails with *its* code, not a generic one."""
        self.write_config(
            self.project(
                routing={"mode": "auto", "allowed_profiles": ["cb"], "rules": []},
                agent_profiles={"cb": self.profile(capabilities={"input_modalities": ["text"]})},
            ),
        )
        with self.assertRaises(Error) as ctx:
            self.create(agent_profile="cb", route={"requirements": {"input_modalities": ["image"]}})
        self.assertEqual(ctx.exception.code, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(ctx.exception.details["profile_id"], "cb")

    def test_cost_ceiling_is_reported_before_capability_gaps(self):
        """L0 precedes L1: the first unmet layer is the one reported."""
        self.write_config(
            self.project(
                routing={
                    "mode": "auto",
                    "allowed_profiles": ["cb"],
                    "rules": [],
                    "cost_ceiling": "low",
                }
            ),
        )
        with self.assertRaises(Error) as ctx:
            self.create(route={"requirements": {"input_modalities": ["image"]}})
        codes = {gap["code"] for gap in ctx.exception.details["gaps"]}
        self.assertEqual(codes, {"COST_CEILING_EXCEEDED"})


class TestReportLine(RoutedFixture):
    def test_report_states_the_combination_and_the_model_observation(self):
        from src import _reports

        result = self.create()
        task = self.store.task(result["task_id"])
        snap = _reports.snapshot(self.store, task)
        text = _reports._header(snap)
        self.assertIn("执行组合：" + task["combination_id"], text)
        self.assertIn("模型观测：未报告", text)
        self.assertIn("集成事实：未验证", text)
        self.assertIn("选择依据：route " + task["route_id"], text)
        self.assertIn("质量数据：", text)
        self.assertNotIn("/usr/bin", text)

    def test_escape_hatch_profile_reports_an_unresolved_model(self):
        """§19-20: 转义口允许注册，但报告必须承认"运行模型未解析"。"""
        from src import _reports

        profile = dict(self.profile(), allow_cli_default_model=True)
        profile.pop("model", None)
        self.write_config(
            self.project(agent_profiles={"cb": profile}),
        )
        result = self.create()
        task = self.store.task(result["task_id"])
        self.assertTrue(task["model_unresolved"])
        text = _reports._header(_reports.snapshot(self.store, task))
        self.assertIn("运行模型未解析", text)
        self.assertIn("集成事实：未验证", text)


class TestRouteStats(RoutedFixture):
    def test_stats_are_read_only_and_never_used_for_selection(self):
        """§19-9: 调用前后文件系统与 DB 都不该被动到。"""
        self.create()

        def tree():
            return sorted(
                (str(path.relative_to(self.store.home)), path.stat().st_mtime_ns)
                for path in self.store.home.rglob("*")
            )

        before = tree()
        record = _routing.performance_record(self.store, "routed", window_days=30)
        self.assertEqual(record["schema_version"], 2)
        self.assertIs(record["used_for_selection"], False)
        self.assertEqual(record["counts"]["tasks_assigned"], 1)
        self.assertEqual(record["buckets"], [])
        self.assertEqual(tree(), before)

    def test_window_is_bounded_by_the_cutoff(self):
        """A fixed, explainable cutoff: a task older than the window is not counted."""
        self.create()
        record = _routing.performance_record(self.store, "routed", window_days=0)
        self.assertEqual(record["counts"]["tasks_assigned"], 0)
        self.assertGreater(record["as_of"], 0)
        self.assertGreaterEqual(record["as_of"], record["window"]["since"])


class TestPolicyRecheckAfterRebind(RoutedFixture):
    def test_narrowing_the_approved_set_blocks_instead_of_renaming(self):
        result = self.create()
        task = self.store.task(result["task_id"])
        # A second profile is allowed, then the policy narrows to it only.
        narrowed = self.project(
            agent_profiles={"cb": self.profile(), "cb2": self.profile()},
            routing={"mode": "auto", "allowed_profiles": ["cb2"], "rules": []},
            default_agent_profile="cb2",
        )
        blk = _routing.combination_allowed(narrowed, "cb", task.get("requirements") or {})
        self.assertIsNotNone(blk)
        self.assertEqual(blk[0], "POLICY_DENIED")
        self.assertIn("allowed_profiles", blk[1])

    def test_an_allowed_combination_passes_the_recheck(self):
        result = self.create()
        task = self.store.task(result["task_id"])
        c = _profiles.normalize_config(self.project())
        self.assertIsNone(_routing.combination_allowed(c, "cb", task.get("requirements") or {}))


class FakeBackend:
    """In-process backend double: never a real agent, never a real CLI."""

    def __init__(self, text="implemented\n"):
        self.text = text

    def inspect(self, name):
        return None

    def execute(self, store, t, r, argv, log_name, worker=False, prompt=None):
        # 真实 backend 会把子进程输出落到 runs/<run>/<log_name>.log；桩必须同样
        # 落一条，否则"闸门失败后日志仍在"这条验收项测不到。
        run_dir = store.path(t) / "runs" / r["run_id"]
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / (log_name + ".log")).write_text("fake agent output\n")
        if worker:
            (Path(t["workspace"]) / "src.txt").write_text(self.text)
        return 0


class FakePublisher:
    def publish(self, t, s):
        return {"number": 1, "url": "https://example.invalid/pr/1"}

    def refresh(self, t):
        return {"ready": True, "pr": {"number": 1, "url": "https://example.invalid/pr/1"}}


class TestGatesAtRunTime(RoutedFixture):
    """Gate A (model identity) and gate B (spend) run *after* the parse layer."""

    def test_unobserved_model_under_pin_blocks_before_any_candidate_sha(self):
        self.premium_project()
        result = self.create()
        task = self.store.task(result["task_id"])
        self.assertTrue(task["requirements"]["pin_model"])
        # `run_pipeline` 把闸门失败收口成 BLOCKED（它是 supervisor 的入口，
        # 不该把异常抛给宿主），所以断言跑在记录上，而不是异常上。
        self.run_task(task)
        blocked = self.store.task(task["task_id"])
        self.assertEqual(blocked["state"], "BLOCKED")
        self.assertEqual(blocked["block_code"], "MODEL_MISMATCH")
        self.assertIsNone(blocked["candidate_sha"])
        run = self.store.get_run(blocked["current_run_id"])
        self.assertEqual(run["model_identity"], "unobserved")
        self.assertEqual(run["observation_scope"], "none")
        self.assertEqual(run["failure_class"], None)
        # 归因必须写明：可靠性能算，质量不能算。
        self.assertEqual(run["attribution"]["attempt_metrics"], "counted")
        self.assertEqual(run["attribution"]["delivery_metrics"], "excluded")
        self.assertIn(
            "worktree_quarantined",
            [event["type"] for event in self.store.events(task["task_id"])[0]],
        )

    def test_unverifiable_cost_with_a_limit_blocks_after_recording(self):
        self.premium_project(spend_guard={"max_cost_per_task": 0.01})
        result = self.create(route={"requirements": {"pin_model": False}})
        task = self.store.task(result["task_id"])
        self.run_task(task)
        blocked = self.store.task(task["task_id"])
        self.assertEqual(blocked["block_code"], "SPEND_UNVERIFIABLE")
        self.assertIsNone(blocked["candidate_sha"])
        run = self.store.get_run(blocked["current_run_id"])
        # 先记录、后闸门：费用本身必须留在 run 上，否则"为什么被拦"不可复算。
        self.assertEqual(run["spend"]["cost_source"], "unavailable")

    def test_no_limits_and_no_pin_lets_the_run_reach_verification(self):
        self.write_config(
            self.project(agent_profiles={"cb": self.profile(cost_class="low")}),
        )
        result = self.create()
        task = self.store.task(result["task_id"])
        self.run_task(task)
        done = self.store.task(task["task_id"])
        self.assertIsNotNone(done["candidate_sha"])
        run = self.store.get_run(done["current_run_id"])
        self.assertTrue(run["combination_id"].startswith("cmb-"))
        # create 只能用 pin 版本算；这里必须体现**实测**版本。若两者不同，
        # 组合 id 必须不同，并且留下 combination_changed 事件。
        self.assertEqual(run["frozen_execution"]["cli_version_pinned"], "1.2.3")
        self.assertTrue(run["frozen_execution"]["argv_audit_sha256"])
        if run["frozen_execution"]["cli_version_effective"] != "1.2.3":
            self.assertNotEqual(run["combination_id"], task["combination_id"])
            types = [event["type"] for event in self.store.events(task["task_id"])[0]]
            self.assertIn("combination_changed", types)
        self.assertEqual(run["purpose"], "task")
        self.assertEqual(run["task_type"], "bugfix")
        self.assertEqual(run["spec_scale"], "S")

    def test_probe_runs_are_recorded_but_never_counted(self):
        self.write_config(
            self.project(agent_profiles={"cb": self.profile(cost_class="low")}),
        )
        result = self.create(purpose="probe")
        task = self.store.task(result["task_id"])
        self.run_task(task)
        run = self.store.get_run(self.store.task(task["task_id"])["current_run_id"])
        self.assertEqual(run["purpose"], "probe")
        self.assertEqual(run["attribution"]["attempt_metrics"], "excluded")
        record = _routing.performance_record(self.store, "routed")
        self.assertEqual(record["buckets"], [])


class TestReassignAuto(RoutedFixture):
    def test_auto_reassign_is_a_fresh_decision_with_a_new_revision(self):
        from src.core import reassign

        result = self.create()
        task = self.store.task(result["task_id"])
        request = self.request(
            target_profile="auto",
            reason="handover",
            expected_version=task["row_version"],
        )
        with mock.patch(
            "src._routing.collect_snapshot", side_effect=lambda c, ids, **kw: ready_snapshot(*ids)
        ):
            reply = reassign(self.store, task["task_id"], request)
        self.assertEqual(reply["assignment_revision"], 2)
        self.assertTrue((self.store.path(task) / "route-02.json").is_file())
        self.assertEqual(reply["route_id"][:3], "rt-")
        updated = self.store.task(task["task_id"])
        self.assertEqual(updated["assignment_revision"], 2)
        self.assertEqual(
            updated["route_content_sha256"],
            read(self.store.path(task) / "route-02.json")["content_sha256"],
        )

    def test_reassign_is_refused_while_the_writer_is_unknown(self):
        """§19-19: 三态未决时换人只会让两个写入者重叠。"""
        from src.core import reassign

        result = self.create()
        task = self.store.task(result["task_id"])
        task.update(state="BLOCKED", block_code="PROCESS_UNKNOWN")
        self.store.save(task)
        with self.assertRaises(Error) as ctx:
            reassign(
                self.store,
                task["task_id"],
                self.request(
                    target_profile="auto",
                    reason="handover",
                    expected_version=self.store.task(task["task_id"])["row_version"],
                ),
            )
        self.assertEqual(ctx.exception.code, "PROCESS_UNKNOWN")

    def test_reassign_after_quarantine_does_not_reuse_the_dirty_work(self):
        """§19-31/33: 正常换人不隔离；隔离后的新 run 从干净基线开始。"""
        from src.core import reassign

        self.premium_project()
        result = self.create()
        task = self.store.task(result["task_id"])
        self.run_task(task)
        blocked = self.store.task(task["task_id"])
        self.assertEqual(blocked["block_code"], "MODEL_MISMATCH")
        old_run = self.store.get_run(blocked["current_run_id"])
        quarantine = (old_run.get("block_details") or {}).get("worktree_quarantine") or {}
        self.assertEqual(quarantine.get("cleanup"), "done")
        self.assertIsNone(old_run.get("candidate_sha"))

        self.write_config(
            self.project(agent_profiles={"cb": self.profile(cost_class="premium", model="stub-b")}),
        )
        # 改配置必然触发 POLICY_CHANGED，换人之前必须先 rebind（§16.5）。
        from src.core import rebind

        rebind(
            self.store,
            task["task_id"],
            self.request(
                expected_version=blocked["row_version"],
                reason="adopt cheaper combination",
                target_policy_hash=digest(self.store.config("routed")),
            ),
        )
        rebound = self.store.task(task["task_id"])
        with mock.patch(
            "src._routing.collect_snapshot", side_effect=lambda c, ids, **kw: ready_snapshot(*ids)
        ):
            reassign(
                self.store,
                task["task_id"],
                self.request(
                    target_profile="auto",
                    reason="handover",
                    expected_version=rebound["row_version"],
                ),
            )
        updated = self.store.task(task["task_id"])
        # 换人本身不是隔离：工作区仍可用，且基线现在是干净的。
        baseline = _worktree.baseline(updated)
        self.assertEqual(baseline["dirty_paths"], [])
        # 旧 run 的证据不会被"下一次成功"洗白。
        self.assertIsNone(self.store.get_run(blocked["current_run_id"]).get("candidate_sha"))
        # 归档留在**旧 run** 目录里：换人不是清扫现场，证据也不搬到新 run。
        self.assertTrue(
            (
                self.store.path(updated) / "runs" / blocked["current_run_id"] / "worktree.patch"
            ).is_file()
        )

    def test_auto_without_a_routing_section_is_refused(self):
        from src.core import reassign

        plain = super().config(repo=str(self.repo))
        self.write_config(plain)
        request = self.request(delivery_mode="local_artifact")
        result = create(self.store, request)
        task = self.store.task(result["task_id"])
        with self.assertRaises(Error) as ctx:
            reassign(
                self.store,
                task["task_id"],
                self.request(
                    target_profile="auto",
                    reason="handover",
                    expected_version=task["row_version"],
                ),
            )
        self.assertEqual(ctx.exception.code, "CAPABILITY_UNSUPPORTED")


class TestPlanIsReadOnly(RoutedFixture):
    def test_route_plan_writes_nothing(self):
        """§19-10: 预览决策不得留下文件、事件或 row_version 变化。"""
        config = self.store.config("routed")  # 先把 DB 连接起来，再取快照

        def tree():
            return sorted(
                str(path.relative_to(self.store.home)) for path in self.store.home.rglob("*")
            )

        before = tree()
        args = _build_parser().parse_args(
            ["--home", str(self.store.home), "route", "plan", "--project", "routed"]
        )
        with mock.patch(
            "src._routing.collect_snapshot", side_effect=lambda c, ids, **kw: ready_snapshot(*ids)
        ):
            plan = _route_command(self.store, args)
        self.assertFalse(plan["wrote"])
        self.assertEqual(plan["decision"], "selected")
        self.assertEqual(tree(), before)
        self.assertEqual(self.store.tasks(), [])
        self.assertEqual(config.get("routing", {}).get("mode"), "auto")


class TestAcceptanceContract(RoutedFixture):
    """One test per §19 item that the earlier suites only implied."""

    def test_endpoint_unverified_is_refused_before_the_task_exists(self):
        """§19-6: 不创建任务，也就没有任何任务内容被发出。"""
        profile = dict(self.profile(), endpoint_ref="onprem")
        self.write_config(
            self.project(
                agent_profiles={"cb": profile}, endpoints={"onprem": {"class": "private"}}
            ),
        )
        with self.assertRaises(Error) as ctx:
            self.create(route={"requirements": {"data_residency": "required"}})
        self.assertEqual(ctx.exception.code, "ENDPOINT_UNVERIFIED")
        self.assertEqual(list((self.store.home / "tasks").glob("task-*")), [])

    def test_replay_probes_nothing_and_keeps_the_same_combination(self):
        """§19-16: 幂等重放不得再碰环境。"""
        calls = []

        def snapshot(c, ids, **kwargs):
            calls.append(list(ids))
            return ready_snapshot(*ids)

        with mock.patch("src._routing.collect_snapshot", side_effect=snapshot):
            first = create(self.store, self.request(request_id="req-fixed"))
            second = create(self.store, self.request(request_id="req-fixed"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(first["route_id"], second["route_id"])

    def test_spend_limit_stops_before_publish_without_switching_profile(self):
        """§19-23: 越线即停，且**不**自动换人。"""
        self.write_config(
            self.project(
                agent_profiles={"cb": self.profile(cost_class="low")},
                routing={
                    "mode": "auto",
                    "allowed_profiles": ["cb"],
                    "rules": [],
                    "spend_guard": {"max_cost_per_task": 0.01, "currency": "USD"},
                },
            ),
        )
        result = self.create()
        task = self.store.task(result["task_id"])
        assignments_before = len(task.get("assignments") or [])
        with mock.patch(
            "src._routing.cost_observation",
            return_value={"amount": 5.0, "cost_source": "reported", "currency": "USD"},
        ):
            self.run_task(task)
        blocked = self.store.task(task["task_id"])
        self.assertEqual(blocked["block_code"], "SPEND_LIMIT_EXCEEDED")
        self.assertIsNone(blocked["candidate_sha"])
        self.assertNotIn(blocked["phase"], ("publish", "ci"))
        self.assertEqual(len(blocked.get("assignments") or []), assignments_before)

    def test_model_gate_leaves_no_evidence_and_keeps_the_log(self):
        """§19-25: 不可信的一次运行不许留下任何可被误读为证据的东西。"""
        self.premium_project()
        result = self.create()
        task = self.store.task(result["task_id"])
        self.run_task(task)
        blocked = self.store.task(task["task_id"])
        run = self.store.get_run(blocked["current_run_id"])
        run_dir = self.store.path(blocked) / "runs" / run["run_id"]
        self.assertTrue((run_dir / "agent.log").is_file())
        self.assertFalse((run_dir / "evidence.json").exists())
        self.assertIsNone(blocked["candidate_sha"])
        self.assertIsNone(blocked["validated_sha"])
        self.assertNotIn(blocked["phase"], ("publish", "ci"))

    def test_policy_narrowing_five_step_flow_stays_open(self):
        """§19-18: 收窄策略后 pause→rebind→BLOCKED→reassign→start 不得死锁。"""
        from src.core import reassign, rebind

        self.write_config(
            self.project(
                agent_profiles={
                    "cb": self.profile(model="stub-model"),
                    "cb2": self.profile(model="stub-2"),
                },
                routing={
                    "mode": "auto",
                    "allowed_profiles": ["cb", "cb2"],
                    "rules": [{"task_type": "bugfix", "prefer": ["cb"]}],
                },
            ),
        )
        created = self.create()
        task = self.store.task(created["task_id"])
        self.assertEqual(task["agent_profile_id"], "cb")

        # 1) 策略收窄：cb 不再被允许，默认档也换成 cb2。
        narrowed = self.project(
            agent_profiles={
                "cb": self.profile(model="stub-model"),
                "cb2": self.profile(model="stub-2"),
            },
            routing={"mode": "auto", "allowed_profiles": ["cb2"], "rules": []},
            default_agent_profile="cb2",
        )
        self.write_config(narrowed)

        # 2) rebind：接受新策略，正在跑的 cb 组合按新规则复核 → BLOCKED。
        rebind(
            self.store,
            task["task_id"],
            self.request(
                expected_version=task["row_version"],
                reason="policy narrowed",
                # rebind 要的是**规范化后**的当前策略摘要，不是文件里的字面值。
                target_policy_hash=digest(self.store.config("routed")),
            ),
        )
        blocked = self.store.task(task["task_id"])
        self.assertEqual(blocked["state"], "BLOCKED")
        self.assertEqual(blocked["block_code"], "POLICY_DENIED")
        # 3b 不清空工作区与历史证据：这不是执行失败。
        self.assertTrue(blocked.get("workspace"))
        self.assertIsNotNone(blocked.get("spec_hash"))

        # 3) reassign(auto)：按新策略重新决策 → cb2，任务回到可启动。
        with mock.patch(
            "src._routing.collect_snapshot", side_effect=lambda c, ids, **kw: ready_snapshot(*ids)
        ):
            reply = reassign(
                self.store,
                task["task_id"],
                self.request(
                    target_profile="auto",
                    reason="adopt narrowed policy",
                    expected_version=blocked["row_version"],
                ),
            )
        self.assertEqual(reply["assignment_revision"], 2)
        recovered = self.store.task(task["task_id"])
        self.assertEqual(recovered["state"], "READY")
        self.assertIsNone(recovered.get("block_code"))
        self.assertEqual(recovered["agent_profile_id"], "cb2")
        # 4) start 的前置条件已满足：没有残留阻断，组合与路由记录一致。
        self.assertEqual(
            recovered["route_content_sha256"],
            read(self.store.path(recovered) / "route-02.json")["content_sha256"],
        )

    def test_only_the_accepting_combination_earns_delivery_credit(self):
        """§19-8: A 失败、B 完成 → accepted 只记 B，A 只记一次尝试失败。"""
        from src.core import accept

        # A：premium 默认 pin，工具不报模型 → 闸门 A 拦下（不产生候选）。
        self.premium_project()
        first = self.create()
        self.run_task(self.store.task(first["task_id"]))
        blocked = self.store.task(first["task_id"])
        self.assertEqual(blocked["state"], "BLOCKED")

        self.write_config(
            self.project(
                agent_profiles={"cb": self.profile(cost_class="premium", model="stub-b")},
                authorized_actions=["create", "execute", "accept"],
            ),
        )
        second = self.create(route={"requirements": {"pin_model": False}})
        self.run_task(self.store.task(second["task_id"]))
        ready = self.store.task(second["task_id"])
        self.assertEqual(ready["state"], "REVIEW_READY")
        accept(
            self.store,
            ready["task_id"],
            self.request(
                expected_version=ready["row_version"],
                subject_sha=ready["validated_sha"],
                actor="human",
                decision="accepted",
                evidence=["reviewed"],
            ),
        )

        record = _routing.performance_record(self.store, "routed")
        buckets = {bucket["combination_id"]: bucket for bucket in record["buckets"]}
        # 分桶用的是 run 上**实测冻结**的组合（可能与任务 pin 的组合不同），
        # 这正是"实际跑了什么"与"打算跑什么"的区别所在。
        failed_run = self.store.get_run(blocked["current_run_id"])
        accepted_run = self.store.get_run(ready["current_run_id"])
        failed = buckets[failed_run["combination_id"]]
        accepted = buckets[accepted_run["combination_id"]]
        self.assertEqual(failed["attempt"]["failed"], 1)
        self.assertEqual(failed["delivery"]["accepted"], 0)
        self.assertEqual(accepted["attempt"]["passed"], 1)
        self.assertEqual(accepted["delivery"]["accepted"], 1)
        self.assertEqual(record["counts"]["tasks_accepted"], 1)


if __name__ == "__main__":
    unittest.main()
