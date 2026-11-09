"""v1.5.0 授权边界：白名单显式、模型字段矩阵、执行准入、确认记录、统计口径。

这一批用例对应设计文档 ``docs/model-selection-authorization-design.md`` 的
M0/M2/M3/M7/M8 与 §5/§6。它们全部是**可判错**的断言：每条失败都能指到具体
字段或具体计数，而不是"看起来更严格了"。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from src import _config, _pipeline, _profiles, _routing
from src._util import Error
from src.core import Store, atomic, create, git

ROOT = Path(__file__).resolve().parents[1]


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
    """A temp repository plus a config factory; no CLI, no network."""

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
            "project_id": "authz",
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

    def normalized(self, **overrides):
        return _profiles.normalize_config(self.config(**overrides))


class TestAllowlistMustBeExplicit(Fixture):
    """M3：声明了 routing 段就必须显式声明白名单，explicit 与 auto 一视同仁。"""

    def test_auto_without_allowlist_is_refused(self):
        with self.assertRaises(Error) as ctx:
            self.normalized(routing={"mode": "auto"})
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("allowed_profiles must be declared explicitly", str(ctx.exception))

    def test_explicit_without_allowlist_is_refused_too(self):
        """二审指出的缺口：explicit 缺省"全部 profile"同样让配置等于授权。"""
        with self.assertRaises(Error) as ctx:
            self.normalized(routing={"mode": "explicit"})
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")

    def test_declared_allowlist_is_kept_verbatim(self):
        c = self.normalized(routing={"mode": "explicit", "allowed_profiles": ["cb"]})
        self.assertEqual(c["routing"]["allowed_profiles"], ["cb"])

    def test_allowlist_cannot_reference_an_undefined_profile(self):
        with self.assertRaises(Error) as ctx:
            self.normalized(routing={"mode": "explicit", "allowed_profiles": ["ghost"]})
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")

    def test_explicit_null_is_refused_not_treated_as_absent(self):
        """P0 回归：键存在但为 null 不等于"没有 routing 段"。

        按值判空会让 ``"routing": null`` 同时跳过白名单显式声明与人工确认——它是
        唯一能让两条守卫一起失效的写法。
        """
        with self.assertRaises(Error) as ctx:
            self.normalized(routing=None)
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("null", str(ctx.exception))

    def test_null_routing_still_needs_a_confirmation_on_the_read_path(self):
        c = self.config(routing=None)
        _config.validate_config(c, strict=False)
        status = _config.attestation_status(c, None)
        self.assertFalse(status["confirmed"])
        self.assertEqual(status["allowlist_source"], "unknown")

    def test_default_profile_must_be_inside_the_allowlist(self):
        with self.assertRaises(Error) as ctx:
            self.normalized(
                agent_profiles={"cb": profile(), "other": profile(model="m-2")},
                routing={"mode": "explicit", "allowed_profiles": ["other"]},
            )
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("default_agent_profile", str(ctx.exception))

    def test_no_routing_section_keeps_the_historical_semantics(self):
        c = _profiles.normalize_config(self.config())
        self.assertNotIn("routing", c)
        status = _config.attestation_status(c, None)
        self.assertEqual(status["allowlist_source"], "legacy_config_is_authorization")
        self.assertTrue(status["confirmed"])


class TestModelFieldMatrix(Fixture):
    """M2：四种组合，外加会不会被空白/类型绕过。"""

    def test_model_with_allow_false_passes(self):
        c = self.normalized(agent_profiles={"cb": profile(allow_cli_default_model=False)})
        self.assertFalse(_profiles.model_unresolved(c["agent_profiles"]["cb"]))

    def test_model_with_allow_true_is_refused_instead_of_silently_ignored(self):
        with self.assertRaises(Error) as ctx:
            self.normalized(agent_profiles={"cb": profile(allow_cli_default_model=True)})
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        message = str(ctx.exception)
        self.assertIn("silently ignored", message)
        # 两种修法都要写在错误里，否则用户只知道"不行"，不知道"怎么改"。
        self.assertIn("Remove the flag", message)
        self.assertIn("remove model", message)

    def test_missing_model_and_missing_flag_is_refused(self):
        broken = profile()
        del broken["model"]
        with self.assertRaises(Error) as ctx:
            self.normalized(agent_profiles={"cb": broken})
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")

    def test_missing_model_with_allow_true_follows_the_cli_default(self):
        broken = profile()
        del broken["model"]
        broken["allow_cli_default_model"] = True
        c = self.normalized(agent_profiles={"cb": broken})
        self.assertTrue(_profiles.model_unresolved(c["agent_profiles"]["cb"]))

    def test_blank_and_padded_models_are_refused(self):
        for value in ("", "   ", " m-primary "):
            with self.subTest(model=value):
                with self.assertRaises(Error) as ctx:
                    self.normalized(agent_profiles={"cb": profile(model=value)})
                self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
                self.assertIn("whitespace", str(ctx.exception))

    def test_non_string_model_is_refused(self):
        for value in (1, True, ["m"], {"id": "m"}):
            with self.subTest(model=value):
                with self.assertRaises(Error) as ctx:
                    self.normalized(agent_profiles={"cb": profile(model=value)})
                self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")

    def test_non_boolean_flag_is_refused(self):
        with self.assertRaises(Error):
            self.normalized(agent_profiles={"cb": profile(allow_cli_default_model="true")})


class TestLegacyCannotBeDeclared(Fixture):
    """M7：v2 配置里的 legacy 标记会跳过 model 必填，必须拒绝。"""

    def test_declaring_legacy_in_a_v2_profile_is_policy_denied(self):
        broken = profile(legacy=True)
        del broken["model"]
        with self.assertRaises(Error) as ctx:
            self.normalized(agent_profiles={"cb": broken})
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")
        self.assertIn("controller-reserved", str(ctx.exception))

    def test_declaring_legacy_never_falls_back_to_the_model_message(self):
        """错误必须指向"不能声明 legacy"，而不是被 model 必填报错掩盖。"""
        with self.assertRaises(Error) as ctx:
            self.normalized(agent_profiles={"cb": profile(legacy=True)})
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")
        self.assertNotIn("must declare model", str(ctx.exception))

    def test_schema_v1_migration_still_produces_a_legacy_profile(self):
        legacy = self.config(schema_version=1, agent="codebuddy")
        for key in ("agent_profiles", "default_agent_profile"):
            legacy.pop(key)
        c = _profiles.normalize_config(legacy)
        pid = next(iter(c["agent_profiles"]))
        self.assertTrue(c["agent_profiles"][pid]["legacy"])
        self.assertTrue(_profiles.model_unresolved(c["agent_profiles"][pid]))


class TestExecutionContract(Fixture):
    """M0/M8：读路径不因新规则失败，执行入口必须报具体字段。"""

    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.db.close)

    def write_raw(self, c):
        atomic(self.store.home / "projects" / "authz.json", c)

    def legacy_ambiguous(self):
        """旧版本能写进去的配置：model 与 allow_cli_default_model 同时存在。"""
        return self.config(agent_profiles={"cb": profile(allow_cli_default_model=True)})

    def test_reading_an_old_invalid_config_does_not_raise(self):
        self.write_raw(self.legacy_ambiguous())
        c = self.store.config("authz")  # M0：读路径不得抛
        self.assertEqual(c["project_id"], "authz")

    def test_reading_reports_the_policy_problems(self):
        self.write_raw(self.legacy_ambiguous())
        problems = self.store.config_problems("authz")
        self.assertTrue(problems)
        self.assertTrue(
            any(problem["code"] == "INPUT_INCOMPLETE" for problem in problems), problems
        )

    def test_the_contract_check_names_the_field_instead_of_policy_changed(self):
        old = self.legacy_ambiguous()
        self.write_raw(old)
        # 摘要与冻结配置完全相等：唯一的拦截来源必须是执行契约校验。
        with self.assertRaises(Error) as ctx:
            _config.check_execution_contract(old)
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertNotEqual(ctx.exception.code, "POLICY_CHANGED")

    def test_dispatch_entry_raises_the_config_error_first(self):
        old = self.legacy_ambiguous()
        self.write_raw(old)
        with self.assertRaises(Error) as ctx:
            _pipeline._require_executable(self.store, "authz", old)
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")


class TestPlaceholders(Fixture):
    """M1：占位符必须在派发前被发现，且指向具体字段。"""

    def test_placeholders_are_reported_by_field_and_skip_free_text(self):
        c = self.config()
        c["repo"] = "/absolute/path/to/repo"
        c["agent_profiles"]["cb"]["model"] = "REPLACE_WITH_VERIFIED_MODEL_ID"
        c["agent_profiles"]["cb"]["description"] = "写 REPLACE_WITH_ 不算占位符"
        hits = _config.find_placeholders(c)
        fields = {hit["field"] for hit in hits}
        self.assertIn("repo", fields)
        self.assertIn("agent_profiles.cb.model", fields)
        self.assertNotIn("agent_profiles.cb.description", fields)

    def test_registration_refuses_an_unfilled_template(self):
        c = self.config()
        c["authorization_ref"] = "REPLACE_WITH_REAL_SCOPE"
        with self.assertRaises(Error) as ctx:
            _config.validate_config(c)
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("authorization_ref", str(ctx.exception))
        self.assertEqual(ctx.exception.details["fields"][0]["field"], "authorization_ref")

    def test_doctor_separates_policy_readiness_from_environment_readiness(self):
        clean = _profiles.normalize_config(self.config())
        self.assertTrue(_config.doctor(clean, probe=False)["policy_ready"])
        broken = _profiles.normalize_config(self.config())
        broken["agent_profiles"]["cb"]["model"] = "REPLACE_WITH_VERIFIED_MODEL_ID"
        report = _config.doctor(broken, probe=False)
        self.assertFalse(report["policy_ready"])
        self.assertTrue(report["policy_problems"])

    def test_doctor_is_not_ready_while_a_placeholder_remains(self):
        c = _profiles.normalize_config(self.config())
        c["agent_profiles"]["cb"]["model"] = "REPLACE_WITH_VERIFIED_MODEL_ID"
        report = _config.doctor(c, probe=False)
        self.assertFalse(report["placeholders_ok"])
        self.assertFalse(report["ready"])
        self.assertTrue(report["placeholders"])


class TestAttestation(Fixture):
    """§5：确认记录绑定配置摘要；缺失与失效都必须说清是哪一种。"""

    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.db.close)

    def c(self):
        return _profiles.normalize_config(
            self.config(routing={"mode": "explicit", "allowed_profiles": ["cb"]})
        )

    def record(self, c, **overrides):
        value = {
            "schema_version": 1,
            "project_id": c["project_id"],
            "policy_hash": _profiles.policy_digest(c),
            "confirmed_profiles": ["cb"],
            "runtime_version": "test",
            "confirmed_at": 0,
            "confirmed_by": "test",
        }
        value.update(overrides)
        return value

    def test_missing_record_is_unknown_not_confirmed(self):
        status = _config.attestation_status(self.c(), None)
        self.assertFalse(status["confirmed"])
        self.assertEqual(status["allowlist_source"], "unknown")
        self.assertEqual(status["reason"], "missing")

    def test_matching_record_confirms(self):
        c = self.c()
        status = _config.attestation_status(c, self.record(c))
        self.assertTrue(status["confirmed"])
        self.assertEqual(status["allowlist_source"], "confirmed")

    def test_changing_only_the_model_invalidates_the_confirmation(self):
        """白名单一个键都没变，但换了模型：授权对象已经变了。"""
        before = self.c()
        record = self.record(before)
        after = _profiles.normalize_config(
            self.config(
                agent_profiles={"cb": profile(model="m-expensive")},
                routing={"mode": "explicit", "allowed_profiles": ["cb"]},
            )
        )
        status = _config.attestation_status(after, record)
        self.assertFalse(status["confirmed"])
        self.assertEqual(status["reason"], "policy_changed_since_attestation")

    def test_a_narrower_confirmation_blocks_the_unconfirmed_candidate(self):
        """`project attest --profiles cb` 之后，cb2 不能再被派发。"""
        c = _profiles.normalize_config(
            self.config(
                agent_profiles={"cb": profile(), "cb2": profile(model="m-2")},
                routing={"mode": "explicit", "allowed_profiles": ["cb", "cb2"]},
            )
        )
        record = self.record(c, confirmed_profiles=["cb"])
        self.assertTrue(_config.attestation_status(c, record, "cb")["confirmed"])
        blocked = _config.attestation_status(c, record, "cb2")
        self.assertFalse(blocked["confirmed"])
        self.assertEqual(blocked["reason"], "candidate_not_confirmed")

    def test_a_new_profile_inside_the_allowlist_also_invalidates(self):
        before = self.c()
        record = self.record(before)
        after = _profiles.normalize_config(
            self.config(
                agent_profiles={"cb": profile(), "cb2": profile(model="m-2")},
                routing={"mode": "explicit", "allowed_profiles": ["cb", "cb2"]},
            )
        )
        self.assertFalse(_config.attestation_status(after, record)["confirmed"])

    def test_a_malformed_record_is_not_a_confirmation(self):
        """回归：记录缺字段/类型不对时，旧实现跳过收窄比较直接放行。"""
        c = _profiles.normalize_config(
            self.config(
                agent_profiles={"cb": profile(), "cb2": profile(model="m-2")},
                routing={"mode": "explicit", "allowed_profiles": ["cb", "cb2"]},
            )
        )
        malformed = (
            {"policy_hash": _profiles.policy_digest(c), "confirmed_profiles": "cb"},
            {"policy_hash": _profiles.policy_digest(c)},
            {"policy_hash": _profiles.policy_digest(c), "confirmed_profiles": []},
            {"confirmed_profiles": ["cb"]},
        )
        for record in malformed:
            status = _config.attestation_status(c, record, "cb2")
            self.assertFalse(status["confirmed"], record)
            self.assertEqual(status["reason"], "record_invalid", record)

    def test_a_corrupt_record_on_disk_is_reported_not_treated_as_missing(self):
        self.store.write_attestation("authz", self.record(self.c()))
        path = self.store.attestation_path("authz")
        path.write_text("{not json")
        status = _config.attestation_status(self.c(), self.store.attestation("authz"), "cb")
        self.assertFalse(status["confirmed"])
        self.assertEqual(status["reason"], "record_invalid")


class TestPolicyProblemsAreReportedTogether(Fixture):
    """M1 回归：读路径一次报全，而不是"修一条、再来一条"。"""

    def test_every_broken_profile_and_the_routing_section_are_reported(self):
        c = self.config(
            agent_profiles={"cb": profile(model=None), "cb2": profile(model=None)},
            routing={"mode": "explicit"},
        )
        problems: list[dict] = []
        _config.validate_config(c, strict=False, problems=problems)
        fields = {item["field"] for item in problems}
        self.assertIn("agent_profiles.cb", fields)
        self.assertIn("agent_profiles.cb2", fields)
        self.assertTrue(any("allowed_profiles" in item["message"] for item in problems))
        self.assertTrue(all(item["code"] for item in problems))

    def test_the_read_path_keeps_the_original_profiles(self):
        """半规范化结果不得写回：否则"配置非法"会表现为"策略漂移"。"""
        c = self.config(agent_profiles={"cb": profile(model=None)}, routing={"mode": "explicit"})
        before = json.loads(json.dumps(c))
        _config.validate_config(c, strict=False, problems=[])
        self.assertEqual(c["agent_profiles"], before["agent_profiles"])


class TestAttributionThreeConditions(Fixture):
    """§6：身份可归属 ∧ 结果可评价 ∧ 原因已归因，三者都要成立。"""

    def comparable(self, **kwargs):
        return _routing.attribution(purpose="task", model_identity_value="reported_match", **kwargs)

    def test_clean_run_is_comparable(self):
        record = self.comparable(outcome="completed", failure_class=None)
        self.assertTrue(record["model_capability_comparable"])

    def test_unobserved_identity_success_is_not_comparable(self):
        record = _routing.attribution(
            outcome="completed",
            failure_class=None,
            model_identity_value="unobserved",
            purpose="task",
        )
        self.assertFalse(record["model_capability_comparable"])
        self.assertEqual(record["attempt_metrics"], "counted")

    def test_environment_failure_is_not_comparable(self):
        record = self.comparable(outcome="failed", failure_class="environment")
        self.assertFalse(record["model_capability_comparable"])

    def test_reported_mismatch_is_not_comparable(self):
        record = _routing.attribution(
            outcome="completed",
            failure_class=None,
            model_identity_value="reported_mismatch",
            purpose="task",
        )
        self.assertFalse(record["model_capability_comparable"])

    def test_agent_failed_is_not_comparable_but_keeps_the_fact(self):
        record = self.comparable(
            outcome="failed", failure_class="executor", block_code="AGENT_FAILED"
        )
        self.assertFalse(record["model_capability_comparable"])
        self.assertEqual(record["attempt_metrics"], "counted")

    def test_verified_failure_is_comparable(self):
        record = self.comparable(
            outcome="failed", failure_class="verification", block_code="CHECK_FAILED"
        )
        self.assertTrue(record["model_capability_comparable"])

    def test_unresolved_model_is_not_comparable_even_when_reported(self):
        record = self.comparable(
            outcome="failed",
            failure_class="verification",
            block_code="CHECK_FAILED",
            model_unresolved=True,
        )
        self.assertFalse(record["model_capability_comparable"])

    def test_probe_runs_are_never_comparable(self):
        record = _routing.attribution(
            outcome="completed",
            failure_class=None,
            model_identity_value="reported_match",
            purpose="probe",
        )
        self.assertFalse(record["model_capability_comparable"])


class DispatchFixture(Fixture):
    """create 需要的完整前置：真实 git 仓库 + spec + 状态库。"""

    def setUp(self):
        super().setUp()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "test")
        git(self.repo, "config", "user.email", "test@localhost")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.db.close)
        self.spec = self.root / "spec.json"
        self.spec.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "title": "Gate",
                    "task_type": "bugfix",
                    "acceptance_criteria": [
                        {
                            "id": "AC-001",
                            "statement": "change exists",
                            "verification": {"type": "check", "check_id": "unit"},
                        }
                    ],
                }
            )
        )

    def write(self, c):
        atomic(self.store.home / "projects" / "authz.json", c)

    def request(self):
        return {
            "schema_version": 2,
            "request_id": "req-" + uuid.uuid4().hex,
            "authorization_ref": "scope-1",
            "project_id": "authz",
            "title": "Gate",
            "spec_file": str(self.spec),
            "delivery_mode": "local_artifact",
        }

    def routed(self):
        return self.config(routing={"mode": "explicit", "allowed_profiles": ["cb"]})

    def routable_task(self):
        self.write(self.routed())
        self.store.write_attestation("authz", _pipeline_attest(self.store, self.routed()))
        return create(self.store, self.request())["task_id"]


class TestDispatchGate(DispatchFixture):
    """create 是派发入口：授权未确认时必须在启动前拒绝。"""

    def test_create_is_refused_until_the_allowlist_is_confirmed(self):
        self.write(self.routed())
        with self.assertRaises(Error) as ctx:
            create(self.store, self.request())
        self.assertEqual(ctx.exception.code, "ALLOWLIST_UNCONFIRMED")
        details = ctx.exception.details
        self.assertEqual(details["reason"], "missing")
        self.assertIn("project attest", details["next"])
        # 拒绝发生在派发之前：没有留下半个任务。
        self.assertEqual(self.store.tasks(), [])

    def test_a_probe_is_not_exempt_from_the_allowlist_gate(self):
        """S3 反例 2：`purpose: "probe"` 只改质量归因，不改授权/费用/身份闸门。

        没有确认记录时 probe 也必须在 create 处被拒——否则"标定一个贵模型"本身
        就成了绕过授权去调用它的入口（probe 是计费的真实调用）。
        """
        self.write(self.routed())
        request = self.request()
        request["purpose"] = "probe"
        with self.assertRaises(Error) as ctx:
            create(self.store, request)
        self.assertEqual(ctx.exception.code, "ALLOWLIST_UNCONFIRMED")
        self.assertEqual(ctx.exception.details["reason"], "missing")
        # 拒绝发生在派发之前：没有任务、没有 run、没有确认记录。
        self.assertEqual(self.store.tasks(), [])
        self.assertIsNone(self.store.attestation("authz"))

    def test_create_passes_after_the_allowlist_is_confirmed(self):
        c = self.routed()
        self.write(c)
        self.store.write_attestation("authz", _pipeline_attest(self.store, c))
        result = create(self.store, self.request())
        self.assertEqual(result["route_decision"], "selected")
        # 报告要写"授权：confirmed"，宿主必须能从响应里拿到这个事实，
        # 而不是自己去读 attest 文件或猜。
        self.assertEqual(result["allowlist_source"], "confirmed")
        self.assertEqual(len(result["policy_hash"]), 64)


def _pipeline_attest(store, c):
    """A confirmation record shaped exactly like ``project attest`` writes."""
    return {
        "schema_version": 1,
        "project_id": c["project_id"],
        "policy_hash": _profiles.policy_digest(store.config("authz")),
        "confirmed_profiles": ["cb"],
        "runtime_version": "test",
        "confirmed_at": 0,
        "confirmed_by": "test",
    }


class TestAttributedRate(DispatchFixture):
    """§6：`pass_rate` 是全量运维指标，`pass_rate_attributed` 才用于能力比较。"""

    def setUp(self):
        super().setUp()
        self.task_id = self.routable_task()

    def add_run(self, run_id, **overrides):
        now = time.time()
        run = {
            "run_id": run_id,
            "task_id": self.task_id,
            "status": "exited",
            "started_at": now,
            "ended_at": now,
            "combination_id": "cmb-1",
            "task_type": "bugfix",
            "spec_scale": "S",
            "profile_id": "cb",
            "model_identity": "reported_match",
            "verification": {"passed": False},
            "attribution": _routing.attribution(
                outcome="completed",
                failure_class=None,
                model_identity_value="reported_match",
                purpose="task",
            ),
        }
        run.update(overrides)
        self.store.run(run)
        return run

    def test_only_attributed_samples_enter_the_comparison_rate(self):
        self.add_run(
            "run-unobserved",
            model_identity="unobserved",
            attribution=_routing.attribution(
                outcome="completed",
                failure_class=None,
                model_identity_value="unobserved",
                purpose="task",
            ),
        )
        self.add_run(
            "run-env",
            failure_class="environment",
            attribution=_routing.attribution(
                outcome="failed",
                failure_class="environment",
                model_identity_value="reported_match",
                purpose="task",
            ),
        )
        self.add_run(
            "run-catch-all",
            failure_class="executor",
            block_code="AGENT_FAILED",
            attribution=_routing.attribution(
                outcome="failed",
                failure_class="executor",
                model_identity_value="reported_match",
                purpose="task",
                block_code="AGENT_FAILED",
            ),
        )
        self.add_run("run-clean", verification={"passed": True})

        record = _routing.performance_record(self.store, "authz")
        # 环境失败连 attempt 都不计；其余三个计数，但只有一个可比较。
        self.assertEqual(record["counts"]["runs_evaluable"], 3)
        self.assertEqual(record["counts"]["runs_excluded_environment"], 1)
        self.assertEqual(record["counts"]["runs_cause_unconfirmed"], 1)
        bucket = record["buckets"][0]
        self.assertEqual(bucket["attempt"]["n"], 3)
        self.assertEqual(bucket["attempt"]["n_attributed"], 1)
        self.assertEqual(bucket["attempt"]["pass_rate_attributed"], 1.0)
        self.assertEqual(bucket["reliability"]["executor_failures_unconfirmed"], 1)
        # 全量口径仍然可见（运维用），两条并存、互不覆盖。
        self.assertEqual(bucket["attempt"]["pass_rate"], round(1 / 3, 4))
        # 排除原因按 (组合, 原因) 分开记：只按组合合并会把两种原因压成一条。
        reasons = [item["reason"] for item in record["not_comparable"]]
        self.assertTrue(any("model_identity=unobserved" in r for r in reasons), reasons)
        self.assertTrue(any("catch-all" in r for r in reasons), reasons)
        self.assertEqual(sum(item["n"] for item in record["not_comparable"]), 2)

    def test_rate_is_null_when_nothing_is_attributable(self):
        self.add_run(
            "run-unobserved",
            model_identity="unobserved",
            attribution=_routing.attribution(
                outcome="completed",
                failure_class=None,
                model_identity_value="unobserved",
                purpose="task",
            ),
        )
        bucket = _routing.performance_record(self.store, "authz")["buckets"][0]
        self.assertEqual(bucket["attempt"]["n_attributed"], 0)
        self.assertIsNone(bucket["attempt"]["pass_rate_attributed"])
