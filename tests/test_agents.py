"""Adapter registry, adapter protocol and profile contract tests (v0.3.0).

Covers the contract from the multi-agent design §9/§10/§11:

* capability is three-state and granular in ``(adapter_id, backend)``;
* an unverified adapter is blocked, never "approximately compatible";
* extended adapters get no argv passthrough and no shared evidence;
* every adapter parses a positive terminal structure, and truncated output,
  missing terminators and error events can never become success;
* profile validation is per backend (codex), and reserved parameters are refused.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src import _profiles
from src._agents import base, registry
from src._agents.claude_code import ClaudeCodeAdapter
from src._agents.codebuddy import CodeBuddyAdapter
from src._agents.codex import CodexAdapter
from src._agents.opencode import OpenCodeAdapter
from src._util import Error, git


def capture(raw, exit_code=0, truncated=False, expected_version=None):
    return base.ProcessCapture(
        adapter_id="codebuddy",
        adapter_backend="codebuddy-cli",
        exit_code=exit_code,
        raw_text=raw,
        truncated=truncated,
        expected_version=expected_version,
    )


def context(**overrides):
    values = dict(
        task_id="task-1",
        run_id="run-1",
        adapter_id="codebuddy",
        adapter_backend="codebuddy-cli",
        workspace="/work",
        prompt_file="/state/prompt.txt",
        prompt_hash="0" * 64,
        profile_id="p1",
        profile_hash="0" * 64,
        spec_revision=1,
        spec_hash="1" * 64,
        expected_version="1.2.3",
        permission_profile="isolated-edit",
    )
    values.update(overrides)
    return base.RunContext(**values)


class TwoBackendAdapter(base.AgentAdapterBase):
    """Test-only adapter with two backends; used only to prove granularity."""

    id = "demo"
    kind = "extended"
    default_backend = "backend-a"
    supported_backends = ("backend-a", "backend-b")
    output_protocol = "json-final"

    def build_launch(self, context):  # pragma: no cover - never launched
        raise NotImplementedError

    def parse_output(self, capture):  # pragma: no cover - never parsed
        raise NotImplementedError


def two_backend_entry():
    return registry.AdapterEntry("demo", "extended", 1, TwoBackendAdapter)


class TestRegistryContract(unittest.TestCase):
    def test_builtin_and_extended_are_distinguished(self):
        ids = {entry.adapter_id: entry.kind for entry in registry.entries()}
        self.assertEqual(ids["codebuddy"], "builtin")
        self.assertEqual(ids["opencode"], "builtin")
        self.assertEqual(ids["claude-code"], "builtin")
        self.assertEqual(ids["codex"], "extended")

    def test_no_adapter_claims_verification_in_this_build(self):
        for entry in registry.entries():
            for backend in entry.adapter.supported_backends:
                state, _, _ = entry.capability(backend, "1.0.0")
                self.assertEqual(state, base.CAPABILITY_IMPLEMENTED, entry.adapter_id)

    def test_three_state_transitions(self):
        entry = registry.get("codebuddy")
        self.assertEqual(entry.capability("codebuddy-cli", "1.0.0")[0], "IMPLEMENTED")
        extra = registry.Verification(backend="codebuddy-cli", version="1.0.0")
        self.assertEqual(entry.capability("codebuddy-cli", "1.0.0", extra)[0], "VERIFIED")
        # Same backend, different exact version -> UNVERIFIED (never "close enough").
        self.assertEqual(entry.capability("codebuddy-cli", "1.0.1", extra)[0], "UNVERIFIED")
        # No pinned version -> cannot claim VERIFIED.
        self.assertEqual(entry.capability("codebuddy-cli", None, extra)[0], "UNVERIFIED")

    def test_backend_granularity_is_isolated(self):
        entry = two_backend_entry()
        extra = registry.Verification(backend="backend-a", version="1.0.0")
        self.assertEqual(entry.capability("backend-a", "1.0.0", extra)[0], "VERIFIED")
        self.assertEqual(entry.capability("backend-b", "1.0.0", extra)[0], "IMPLEMENTED")

    def test_fact_pending_adapter_can_never_be_verified(self):
        """§10.4: before facts are frozen the state is IMPLEMENTED, evidence or not."""
        entry = registry.get("codex")
        backend = entry.adapter.supported_backends[0]
        evidence = registry.Verification(backend=backend, version="1.0.0", source="real_e2e")
        state, reason, verification = entry.capability(backend, "1.0.0", evidence)
        self.assertEqual(state, base.CAPABILITY_IMPLEMENTED)
        self.assertIsNone(verification)
        self.assertIn("FACT_PENDING", reason)

    def test_unregistered_adapter_is_blocked(self):
        with self.assertRaises(Error) as ctx:
            registry.require_runnable("gemini-cli", "default", "1.0.0")
        self.assertEqual(ctx.exception.code, "ADAPTER_UNVERIFIED")

    def test_backend_mismatch_is_its_own_error(self):
        with self.assertRaises(Error) as ctx:
            registry.resolve("codebuddy", "anthropic-api", "1.0.0")
        self.assertEqual(ctx.exception.code, "ADAPTER_BACKEND_MISMATCH")

    def test_require_runnable_reports_version_mismatch(self):
        entry = registry.get("codebuddy")
        extra = registry.Verification(backend="codebuddy-cli", version="1.2.3")
        self.assertIsNotNone(
            registry.require_runnable("codebuddy", "codebuddy-cli", "1.2.3", extra)
        )
        with self.assertRaises(Error) as ctx:
            registry.require_runnable("codebuddy", "codebuddy-cli", "9.9.9", extra)
        self.assertEqual(ctx.exception.code, "ADAPTER_VERSION_MISMATCH")
        self.assertTrue(entry.adapter.supported_backends)


class TestLaunchSpecContract(unittest.TestCase):
    def test_relative_and_workspace_writable_paths_are_refused(self):
        for directory in ("relative/dir", "/work", "/", "/tmp/../etc"):
            spec = base.LaunchSpec(argv=("agent",), writable_dirs=(directory,))
            with self.assertRaises(Error):
                base.validate_launch_spec(spec)

    def test_interactive_launch_is_refused(self):
        spec = base.LaunchSpec(argv=("agent",), non_interactive=False)
        with self.assertRaises(Error) as ctx:
            base.validate_launch_spec(spec)
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")

    def test_control_characters_are_refused(self):
        with self.assertRaises(Error):
            base.validate_launch_spec(base.LaunchSpec(argv=("agent", "a\nb")))

    def test_round_trip(self):
        spec = base.LaunchSpec(
            argv=("a", "b"),
            stdin_file="/p/prompt.txt",
            env_names=("ANTHROPIC_API_KEY",),
            writable_dirs=("/tmp/x",),
        )
        self.assertEqual(base.LaunchSpec.from_dict(spec.to_dict()), spec)


class TestCodeBuddyAdapter(unittest.TestCase):
    def setUp(self):
        self.adapter = CodeBuddyAdapter()

    def test_launch_keeps_prompt_out_of_argv(self):
        spec = self.adapter.build_launch(context())
        self.assertEqual(spec.stdin_file, "/state/prompt.txt")
        self.assertEqual(spec.output_protocol, "json-final")
        self.assertFalse(any("prompt" in arg for arg in spec.argv))
        self.assertNotIn("cat", " ".join(spec.argv))

    def test_success(self):
        result = self.adapter.parse_output(
            capture(
                '{"type":"result","subtype":"success","is_error":false,"result":"done",'
                '"session_id":"s1","total_cost_usd":0.02}'
            )
        )
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.session_id, "s1")
        self.assertEqual(result.usage["cost"], 0.02)
        result.require_completed()

    def test_error_mapping(self):
        cases = {
            "Invalid API key provided": "auth_error",
            "Permission denied by settings": "permission_denied",
            "429 rate limit exceeded": "rate_limited",
        }
        for message, expected in cases.items():
            raw = json.dumps({"type": "result", "is_error": True, "result": message})
            self.assertEqual(self.adapter.parse_output(capture(raw)).outcome, expected)

    def test_unknown_structure_is_invalid_not_success(self):
        with self.assertRaises(Error) as ctx:
            self.adapter.parse_output(capture('{"foo":"bar"}'))
        self.assertEqual(ctx.exception.code, "AGENT_RESULT_INVALID")

    def test_truncated_output_is_refused(self):
        with self.assertRaises(Error) as ctx:
            self.adapter.parse_output(capture('{"result":"done"}', truncated=True))
        self.assertEqual(ctx.exception.code, "OUTPUT_LIMIT_EXCEEDED")

    def test_non_zero_exit_is_never_success(self):
        raw = '{"type":"result","result":"done"}'
        self.assertEqual(self.adapter.parse_output(capture(raw, exit_code=3)).outcome, "failed")


class TestClaudeCodeAdapter(unittest.TestCase):
    def setUp(self):
        self.adapter = ClaudeCodeAdapter()

    def test_terminal_result_and_session(self):
        raw = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "ok",
                "session_id": "abc",
            }
        )
        result = self.adapter.parse_output(capture(raw))
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.session_id, "abc")

    def test_missing_terminal_is_invalid(self):
        with self.assertRaises(Error):
            self.adapter.parse_output(capture(json.dumps({"type": "assistant", "text": "hi"})))

    def test_progress_across_lines_uses_final_object(self):
        raw = "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init"}),
                json.dumps(
                    {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
                ),
            ]
        )
        self.assertEqual(self.adapter.parse_output(capture(raw)).outcome, "completed")


class TestOpenCodeAdapter(unittest.TestCase):
    def setUp(self):
        self.adapter = OpenCodeAdapter()

    def test_event_stream_requires_terminal_event(self):
        stream = "\n".join(
            [
                json.dumps({"type": "step_start"}),
                json.dumps({"type": "text", "text": "working"}),
                json.dumps({"type": "step_finish", "result": "done"}),
            ]
        )
        self.assertEqual(self.adapter.parse_output(capture(stream)).outcome, "completed")

    def test_stream_without_terminal_is_invalid(self):
        stream = json.dumps({"type": "text", "text": "still going"})
        result = self.adapter.parse_output(capture(stream))
        self.assertEqual(result.outcome, "invalid_output")
        with self.assertRaises(Error) as ctx:
            result.require_completed()
        self.assertEqual(ctx.exception.code, "AGENT_RESULT_INVALID")

    def test_error_event_maps_to_auth_error(self):
        stream = "\n".join(
            [
                json.dumps({"type": "step_start"}),
                json.dumps({"type": "error", "message": "unauthorized: invalid api key"}),
            ]
        )
        self.assertEqual(self.adapter.parse_output(capture(stream)).outcome, "auth_error")

    def test_unknown_events_are_counted_not_terminal(self):
        stream = "\n".join(
            [
                json.dumps({"type": "brand_new_event"}),
                json.dumps({"type": "another_event"}),
            ]
        )
        self.assertEqual(self.adapter.parse_output(capture(stream)).outcome, "invalid_output")


class TestCodexAdapter(unittest.TestCase):
    def setUp(self):
        self.adapter = CodexAdapter()
        self.backend = self.adapter.supported_backends[0]

    def test_no_unverified_backend_is_named(self):
        """§9.2/§10.4: no backend identity is asserted while facts are pending."""
        self.assertEqual(self.adapter.supported_backends, ("backend-pending",))
        self.assertEqual(self.adapter.facts_status, "FACT_PENDING")

    def test_backend_must_be_declared(self):
        with self.assertRaises(Error) as ctx:
            self.adapter.validate_profile(
                {
                    "adapter": "codex",
                    "model": "stub-model",
                    "background": self.backend,
                    "permission_profile": "isolated-edit-codex",
                }
            )
        self.assertEqual(ctx.exception.code, "ADAPTER_BACKEND_MISMATCH")

    def test_launch_is_blocked_while_facts_are_pending(self):
        with self.assertRaises(Error) as ctx:
            self.adapter.build_launch(context(adapter_id="codex", adapter_backend=self.backend))
        self.assertEqual(ctx.exception.code, "ADAPTER_VERSION_MISMATCH")

    def test_probe_is_blocked_while_facts_are_pending(self):
        profile = {
            "adapter": "codex",
            "model": "stub-model",
            "backend": self.backend,
            "permission_profile": "isolated-edit-codex",
            "credential_refs": ["model-codex"],
        }
        with self.assertRaises(Error) as ctx:
            self.adapter.probe_plan(profile)
        self.assertEqual(ctx.exception.code, "ADAPTER_VERSION_MISMATCH")

    def test_only_the_adapter_credential_namespace_is_accepted(self):
        with self.assertRaises(Error) as ctx:
            self.adapter.validate_profile(
                {
                    "adapter": "codex",
                    "model": "stub-model",
                    "backend": self.backend,
                    "permission_profile": "isolated-edit-codex",
                    "credential_refs": ["OPENAI_API_KEY"],
                }
            )
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")

    def test_provisional_parser_handles_both_protocols(self):
        self.assertEqual(
            self.adapter.parse_output(capture('{"type":"result","result":"done"}')).outcome,
            "completed",
        )
        stream = "\n".join(
            [
                json.dumps({"type": "progress"}),
                json.dumps({"type": "run.completed", "output": "done"}),
            ]
        )
        self.assertEqual(self.adapter.parse_output(capture(stream)).outcome, "completed")

    # -- the eight §19.1 cases ------------------------------------------- #
    def test_normal(self):
        result = self.adapter.parse_output(capture('{"type":"result","result":"ok"}'))
        self.assertEqual(result.outcome, "completed")

    def test_error_event(self):
        stream = "\n".join([json.dumps({"type": "error", "message": "boom"})])
        self.assertEqual(self.adapter.parse_output(capture(stream)).outcome, "failed")

    def test_auth_failure_is_mapped(self):
        stream = json.dumps({"type": "error", "message": "unauthorized: invalid api key"})
        self.assertEqual(self.adapter.parse_output(capture(stream)).outcome, "auth_error")

    def test_permission_block_is_mapped(self):
        stream = json.dumps({"type": "fatal", "message": "permission denied: tool not allowed"})
        self.assertEqual(self.adapter.parse_output(capture(stream)).outcome, "permission_denied")

    def test_rate_limit_is_mapped(self):
        stream = json.dumps({"type": "error", "message": "429 rate limit exceeded"})
        self.assertEqual(self.adapter.parse_output(capture(stream)).outcome, "rate_limited")

    def test_truncated_output_is_refused(self):
        with self.assertRaises(Error) as ctx:
            self.adapter.parse_output(capture('{"type":"result","result":"ok"}', truncated=True))
        self.assertEqual(ctx.exception.code, "OUTPUT_LIMIT_EXCEEDED")

    def test_oversized_output_is_refused(self):
        spec = base.LaunchSpec(argv=("codex",), max_output_bytes=1024)
        capture_obj = base.ProcessCapture(
            adapter_id="codex",
            adapter_backend=self.backend,
            exit_code=0,
            raw_text="x" * 4096,
            truncated=True,
            max_output_bytes=spec.max_output_bytes,
        )
        with self.assertRaises(Error) as ctx:
            self.adapter.parse_output(capture_obj)
        self.assertEqual(ctx.exception.code, "OUTPUT_LIMIT_EXCEEDED")

    def test_unknown_event_is_never_terminal(self):
        stream = "\n".join(
            [json.dumps({"type": "brand_new_event"}), json.dumps({"type": "another_event"})]
        )
        self.assertEqual(self.adapter.parse_output(capture(stream)).outcome, "invalid_output")

    def test_missing_terminator_is_invalid(self):
        result = self.adapter.parse_output(capture('{"type": "progress"}'))
        self.assertEqual(result.outcome, "invalid_output")

    def test_non_zero_exit_is_never_success(self):
        self.assertEqual(
            self.adapter.parse_output(
                capture('{"type":"result","result":"done"}', exit_code=2)
            ).outcome,
            "failed",
        )


class ProfileFixture(unittest.TestCase):
    """Shared project/config fixture; every test gets its own temp repo."""

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

    def tearDown(self):
        self._td.cleanup()

    def config(self, profiles=None, **overrides):
        value = {
            "schema_version": 2,
            "project_id": "demo",
            "repo": str(self.repo),
            "backend": "local",
            "default_agent_profile": "p-codebuddy",
            "agent_profiles": profiles
            or {
                "p-codebuddy": {
                    "adapter": "codebuddy",
                    "model": "stub-model",
                    "backend": "codebuddy-cli",
                    "image": "registry/zoe@sha256:" + "a" * 64,
                    "expected_version": "1.2.3",
                    "permission_profile": "isolated-edit",
                },
            },
            "checks": [{"id": "unit", "argv": ["true"]}],
            "allowed_paths": ["src.txt"],
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute", "publish", "accept"],
        }
        value.update(overrides)
        return value

    def evidence(self, adapter_id, backend, version, source="local_admin"):
        path = self.root / (f"compat-{adapter_id}-{backend}.json")
        path.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "adapter_id": adapter_id,
                            "backend": backend,
                            "version": version,
                            "source": source,
                            "platform": "linux",
                            "recorded_at": "2026-10-27",
                        },
                    ]
                }
            )
        )
        return str(path)


class TestProfileNormalization(ProfileFixture):
    def test_v1_config_becomes_legacy_profile(self):
        c = _profiles.normalize_config(
            {
                "schema_version": 1,
                "repo": str(self.repo),
                "backend": "local",
                "agent": "codebuddy",
                "compatibility_verified": True,
                "agent_env": ["CODEBUDDY_API_KEY"],
                "checks": [{"id": "unit", "argv": ["true"]}],
                "allowed_paths": ["src.txt"],
            }
        )
        self.assertEqual(c["default_agent_profile"], "legacy-codebuddy")
        profile = c["agent_profiles"]["legacy-codebuddy"]
        self.assertTrue(profile["legacy"])
        self.assertEqual(profile["credential_refs"], ["CODEBUDDY_API_KEY"])
        binding = _profiles.resolve_binding(c)
        self.assertEqual(binding.capability, "VERIFIED")
        self.assertEqual(binding.verification.source, "legacy_admin_declaration")

    def test_v1_without_declaration_stays_implemented(self):
        c = _profiles.normalize_config(
            {
                "schema_version": 1,
                "repo": str(self.repo),
                "backend": "local",
                "agent": "codebuddy",
                "checks": [],
                "allowed_paths": ["src.txt"],
            }
        )
        self.assertEqual(_profiles.resolve_binding(c).capability, "IMPLEMENTED")

    def test_v2_without_evidence_is_implemented(self):
        c = _profiles.normalize_config(self.config())
        with self.assertRaises(Error) as ctx:
            _profiles.require_runnable(_profiles.resolve_binding(c))
        self.assertEqual(ctx.exception.code, "ADAPTER_UNVERIFIED")

    def test_evidence_unlocks_only_its_exact_version(self):
        profiles = {
            "p1": {
                "adapter": "codebuddy",
                "model": "stub-model",
                "backend": "codebuddy-cli",
                "image": "registry/zoe@sha256:" + "a" * 64,
                "expected_version": "1.2.3",
                "permission_profile": "isolated-edit",
                "compatibility_evidence": self.evidence("codebuddy", "codebuddy-cli", "1.2.3"),
            }
        }
        c = _profiles.normalize_config(self.config(profiles=profiles, default_agent_profile="p1"))
        binding = _profiles.resolve_binding(c, "p1")
        self.assertEqual(binding.capability, "VERIFIED")
        self.assertIsNotNone(_profiles.require_runnable(binding))

        mismatch = json.loads(json.dumps(c))
        mismatch["agent_profiles"]["p1"]["expected_version"] = "1.2.4"
        with self.assertRaises(Error) as ctx:
            _profiles.require_runnable(_profiles.resolve_binding(mismatch, "p1"))
        self.assertEqual(ctx.exception.code, "ADAPTER_VERSION_MISMATCH")

    def test_evidence_cannot_unlock_a_fact_pending_adapter(self):
        """§10.4: an operator record must not promote codex above IMPLEMENTED."""
        backend = CodexAdapter.supported_backends[0]
        profiles = {
            "p1": {
                "adapter": "codex",
                "model": "stub-model",
                "backend": backend,
                "image": "registry/zoe@sha256:" + "a" * 64,
                "expected_version": "0.9.0",
                "permission_profile": "isolated-edit-codex",
                "credential_refs": ["model-codex"],
                "compatibility_evidence": self.evidence("codex", backend, "0.9.0"),
            }
        }
        c = _profiles.normalize_config(self.config(profiles=profiles, default_agent_profile="p1"))
        binding = _profiles.resolve_binding(c, "p1")
        self.assertEqual(binding.capability, "IMPLEMENTED")
        with self.assertRaises(Error) as ctx:
            _profiles.require_runnable(binding)
        self.assertEqual(ctx.exception.code, "ADAPTER_UNVERIFIED")

    def test_evidence_for_another_backend_is_a_mismatch(self):
        backend = CodexAdapter.supported_backends[0]
        profiles = {
            "p1": {
                "adapter": "codex",
                "model": "stub-model",
                "backend": backend,
                "image": "registry/zoe@sha256:" + "a" * 64,
                "expected_version": "0.9.0",
                "permission_profile": "isolated-edit-codex",
                "credential_refs": ["model-codex"],
                "compatibility_evidence": self.evidence("codex", "another-backend", "0.9.0"),
            }
        }
        c = self.config(profiles=profiles, default_agent_profile="p1")
        with self.assertRaises(Error) as ctx:
            _profiles.resolve_binding(_profiles.normalize_config(c), "p1")
        self.assertEqual(ctx.exception.code, "ADAPTER_BACKEND_MISMATCH")

    def test_evidence_for_another_adapter_is_a_mismatch(self):
        profiles = {
            "p1": {
                "adapter": "codebuddy",
                "model": "stub-model",
                "backend": "codebuddy-cli",
                "image": "registry/zoe@sha256:" + "a" * 64,
                "expected_version": "1.2.3",
                "permission_profile": "isolated-edit",
                "compatibility_evidence": self.evidence("claude-code", "codebuddy-cli", "1.2.3"),
            }
        }
        c = self.config(profiles=profiles, default_agent_profile="p1")
        with self.assertRaises(Error) as ctx:
            _profiles.resolve_binding(_profiles.normalize_config(c), "p1")
        self.assertEqual(ctx.exception.code, "ADAPTER_BACKEND_MISMATCH")

    def test_reserved_parameter_is_refused(self):
        profiles = {
            "p1": {
                "adapter": "codebuddy",
                "model": "stub-model",
                "backend": "codebuddy-cli",
                "image": "registry/zoe@sha256:" + "a" * 64,
                "permission_profile": "isolated-edit",
                "agent_flags": ["--dangerously-skip-permissions"],
            }
        }
        with self.assertRaises(Error) as ctx:
            _profiles.normalize_config(self.config(profiles=profiles, default_agent_profile="p1"))
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")

    def test_legacy_flags_migrate_to_typed_options(self):
        c = _profiles.normalize_config(
            {
                "schema_version": 1,
                "repo": str(self.repo),
                "backend": "local",
                "agent": "codebuddy",
                "agent_flags": ["--model", "x", "--max-turns", "4"],
                "checks": [],
                "allowed_paths": ["src.txt"],
            }
        )
        profile = c["agent_profiles"]["legacy-codebuddy"]
        self.assertEqual(profile["model"], "x")
        self.assertEqual(profile["max_turns"], 4)
        with self.assertRaises(Error):
            _profiles.migrate_legacy_flags(["--dangerous"])

    def test_credentials_are_prefix_scoped_per_adapter(self):
        profiles = {
            "p1": {
                "adapter": "claude-code",
                "model": "stub-model",
                "backend": "anthropic-api",
                "image": "registry/zoe@sha256:" + "a" * 64,
                "permission_profile": "isolated-edit",
                "credential_refs": ["CODEBUDDY_API_KEY"],
            }
        }
        c = _profiles.normalize_config(self.config(profiles=profiles, default_agent_profile="p1"))
        with self.assertRaises(Error) as ctx:
            _profiles.resolve_binding(c, "p1")
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")

    def test_permission_profile_is_adapter_specific(self):
        profiles = {
            "p1": {
                "adapter": "codex",
                "model": "stub-model",
                "backend": CodexAdapter.supported_backends[0],
                "image": "registry/zoe@sha256:" + "a" * 64,
                "permission_profile": "isolated-edit",
                "credential_refs": ["model-codex"],
            }
        }
        c = _profiles.normalize_config(self.config(profiles=profiles, default_agent_profile="p1"))
        with self.assertRaises(Error) as ctx:
            _profiles.resolve_binding(c, "p1")
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")


class TestPermissionBinding(ProfileFixture):
    """§6/§10.2: a named permission profile must be auditable, never implied."""

    def test_unbound_profile_is_reported_as_not_enforced(self):
        # opencode 没有非交互权限开关（permission_switch=False）：标签存在但无法
        # 证明执行，这一点必须如实报告，且不构成拒绝。
        profiles = {
            "p1": {
                "adapter": "opencode",
                "model": "stub-model",
                "backend": "opencode-cli",
                "image": "registry/zoe@sha256:" + "a" * 64,
                "expected_version": "1.2.3",
                "permission_profile": "isolated-edit",
                "compatibility_evidence": self.evidence("opencode", "opencode-cli", "1.2.3"),
            }
        }
        c = _profiles.normalize_config(self.config(profiles=profiles, default_agent_profile="p1"))
        binding = _profiles.resolve_binding(c, "p1")
        self.assertEqual(binding.permission["profile"], "isolated-edit")
        self.assertFalse(binding.permission["enforced"])
        self.assertIsNotNone(_profiles.require_runnable(binding))

    def test_require_permission_binding_fails_closed(self):
        # 取一个"没有权限开关"的适配器（opencode）：此时只能靠 profile 显式
        # 要求 fail closed，因为 enforced=False 本身不是配置缺陷。
        profiles = {
            "p1": {
                "adapter": "opencode",
                "model": "stub-model",
                "backend": "opencode-cli",
                "image": "registry/zoe@sha256:" + "a" * 64,
                "expected_version": "1.2.3",
                "permission_profile": "isolated-edit",
                "require_permission_binding": True,
                "compatibility_evidence": self.evidence("opencode", "opencode-cli", "1.2.3"),
            }
        }
        c = _profiles.normalize_config(self.config(profiles=profiles, default_agent_profile="p1"))
        with self.assertRaises(Error) as ctx:
            _profiles.require_runnable(_profiles.resolve_binding(c, "p1"))
        self.assertEqual(ctx.exception.code, "PERMISSION_PROFILE_UNBOUND")

    def test_claude_code_isolated_edit_binds_the_permission_switch(self):
        """无人值守的一轮必须有审批通道，否则写文件必被拒（设计 Q3）。"""
        spec = ClaudeCodeAdapter().build_launch(
            context(
                adapter_id="claude-code",
                adapter_backend="anthropic-api",
                permission_profile="isolated-edit",
            )
        )
        argv = list(spec.argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertTrue(spec.extra["permission"]["enforced"])
        # 不得用"全放行"换可用性。
        self.assertNotIn("bypassPermissions", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_every_declared_profile_of_a_switch_adapter_is_bound(self):
        """有开关却不绑定 = 配置缺陷。

        ``permission_switch=True`` 的适配器一旦声明了某档位却没给 argv，该档位
        在无人值守下必然失败（写文件被拒 → 烧完超时 → 伪装成 ``AGENT_FAILED``）。
        这条不变量此前只能等 ``require_runnable`` 在运行时发现，claude-code 就是
        这样带着一个未绑定档位发版的。
        """
        for entry in registry.entries():
            adapter = entry.adapter
            if not adapter.permission_switch:
                continue
            unbound = sorted(set(adapter.permission_profiles) - set(adapter.permission_bindings))
            self.assertEqual(
                unbound,
                [],
                f"{entry.adapter_id} declares permission profiles with no argv binding",
            )

    def test_approved_tools_counts_as_enforcement_for_claude_code(self):
        adapter = ClaudeCodeAdapter()
        spec = adapter.build_launch(
            context(
                adapter_id="claude-code",
                adapter_backend="anthropic-api",
                permission_profile="isolated-edit",
                options={"approved_tools": ["Read", "Edit"]},
            )
        )
        self.assertTrue(spec.extra["permission"]["enforced"])
        self.assertIn("--allowedTools", spec.argv)

    def test_claude_code_passes_each_tool_as_its_own_argv(self):
        """``--allowedTools`` 是 nargs="+"：整串拼成一个 argv 会一个都不匹配。"""
        spec = ClaudeCodeAdapter().build_launch(
            context(
                adapter_id="claude-code",
                adapter_backend="anthropic-api",
                permission_profile="isolated-edit",
                options={"approved_tools": ["Read", "Edit"]},
            )
        )
        argv = list(spec.argv)
        index = argv.index("--allowedTools")
        self.assertEqual(argv[index + 1 : index + 3], ["Read", "Edit"])
        self.assertEqual(argv.count("--allowedTools"), 1)

    def test_launch_records_permission_state_and_session_dir(self):
        # codebuddy 有 --permission-mode：isolated-edit 必须是真实 argv，否则
        # `-p` 无人值守下没有审批通道，写入类工具必被拒。
        spec = CodeBuddyAdapter().build_launch(context(session_dir="/state/run/session/x/y"))
        self.assertEqual(spec.extra["permission"]["enforced"], True)
        self.assertIn("--permission-mode", spec.argv)
        self.assertIn("acceptEdits", spec.argv)
        # 不得用 --dangerously-skip-permissions：那等于"全放行"（设计 Q3）。
        self.assertNotIn("-y", spec.argv)
        self.assertNotIn("--dangerously-skip-permissions", spec.argv)
        self.assertEqual(spec.session_dir, "/state/run/session/x/y")

    def test_session_dir_must_be_absolute_and_contained(self):
        for value in ("relative/dir", "/state/../etc", "/"):
            spec = base.LaunchSpec(argv=("agent",), session_dir=value)
            with self.assertRaises(Error):
                base.validate_launch_spec(spec)


class TestPromptLimit(unittest.TestCase):
    def test_large_prompt_is_refused(self):
        with self.assertRaises(Error) as ctx:
            _profiles.check_prompt_size(b"x" * (base.PROMPT_LIMIT_BYTES + 1))
        self.assertEqual(ctx.exception.code, "PROMPT_TOO_LARGE")
        _profiles.check_prompt_size(b"x" * base.PROMPT_LIMIT_BYTES)


if __name__ == "__main__":
    unittest.main()
