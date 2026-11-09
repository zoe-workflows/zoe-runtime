"""v1.3 prerequisites: `auth_mode` (F5) and check `error_class` (F3).

Design references: ``docs/task-group-delivery-design.md`` §2.3 (P0-5, P0-6),
§5.4 and §5.5.

The point of both features is the same: a report must be able to say **who owns
the failure**. "The agent deleted the test" is a test failure; "the check could
not run" is an environment failure and must not burn the implementation budget;
"the login is unknown" is neither "authenticated" nor "broken".
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from src import _pipeline, _profiles
from src._util import Error


def config(**profile_overrides) -> dict:
    profile = {
        "adapter": "codebuddy",
        "model": "stub",
        "backend": "codebuddy-cli",
        "expected_version": "1.2.3",
        "permission_profile": "isolated-edit",
    }
    profile.update(profile_overrides)
    return {
        "schema_version": 2,
        "project_id": "demo",
        "repo": os.getcwd(),
        "backend": "local",
        "default_agent_profile": "p-cb",
        "agent_profiles": {"p-cb": profile},
        "checks": [{"id": "unit", "argv": ["true"]}],
        "allowed_paths": ["src"],
        "authorization_ref": "scope-1",
        "authorized_actions": ["create", "execute", "publish", "accept"],
        "max_attempts": 3,
        "max_active_tasks": 1,
    }


def normalized(**profile_overrides) -> dict:
    return _profiles.normalize_config(config(**profile_overrides))


class TestAuthModeRegistration(unittest.TestCase):
    """§5.5: `api_key` is the one mode with a hard registration precondition."""

    def test_api_key_without_credential_refs_is_refused_at_registration(self):
        with self.assertRaises(Error) as ctx:
            normalized(auth_mode="api_key")
        self.assertEqual(ctx.exception.code, "AUTH_REQUIRED")

    def test_an_unknown_auth_mode_is_refused(self):
        with self.assertRaises(Error) as ctx:
            normalized(auth_mode="oauth")
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")

    def test_a_declared_auth_mode_is_not_rewritten(self):
        """Re-writing defaults would change `policy_digest` for untouched configs."""
        c = normalized(auth_mode="cli_login")
        self.assertEqual(c["agent_profiles"]["p-cb"]["auth_mode"], "cli_login")
        plain = normalized()
        self.assertNotIn("auth_mode", plain["agent_profiles"]["p-cb"])

    def test_an_undeclared_auth_mode_reads_as_unknown(self):
        self.assertEqual(_profiles.auth_mode({"model": "x"}), "unknown")


class TestAuthModeBeforeStart(unittest.TestCase):
    """§5.5: the second judgement happens before `start`, not only at register."""

    def binding(self, **profile_overrides):
        return _profiles.resolve_binding(normalized(**profile_overrides), "p-cb")

    def test_api_key_with_a_missing_variable_is_refused(self):
        binding = self.binding(auth_mode="api_key", credential_refs=["CODEBUDDY_ABSENT_KEY"])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEBUDDY_ABSENT_KEY", None)
            with self.assertRaises(Error) as ctx:
                _profiles.require_runnable(binding, require_real_e2e=False)
            self.assertEqual(ctx.exception.code, "AUTH_REQUIRED")
            self.assertEqual(ctx.exception.details["auth_mode"], "api_key")

    def test_api_key_with_the_variable_present_is_runnable_and_reports_present(self):
        binding = self.binding(auth_mode="api_key", credential_refs=["CODEBUDDY_PRESENT_KEY"])
        with mock.patch.dict(os.environ, {"CODEBUDDY_PRESENT_KEY": "secret"}):
            _profiles.require_runnable(binding, require_real_e2e=False)
            state = _profiles.auth_state(binding)
        self.assertEqual(state["login_state"], "present")
        self.assertTrue(state["credential_present"])
        # The value itself must never appear in the state object.
        self.assertNotIn("secret", repr(state))

    def test_cli_login_without_evidence_is_unknown_not_authenticated(self):
        state = _profiles.auth_state(self.binding(auth_mode="cli_login"))
        self.assertEqual(state["login_state"], "unknown")
        self.assertIn("not proven", state["login_basis"])

    def test_a_probe_that_reports_an_auth_failure_makes_the_login_absent(self):
        state = _profiles.auth_state(
            self.binding(auth_mode="cli_login"),
            {"probe_status": "PASSED", "text": "Authentication required. Please use /login"},
        )
        self.assertEqual(state["login_state"], "absent")

    def test_a_version_probe_does_not_upgrade_unknown_to_present(self):
        state = _profiles.auth_state(
            self.binding(auth_mode="cli_login"),
            {"probe_status": "PASSED", "text": "1.2.3"},
        )
        self.assertEqual(state["login_state"], "unknown")

    def test_the_profile_view_carries_the_basis(self):
        c = normalized(auth_mode="cli_login")
        view = _profiles.profile_views(c)[0]
        self.assertEqual(view["login_state"], "unknown")
        self.assertTrue(view["login_basis"])


class TestErrorClass(unittest.TestCase):
    """§5.4: four classes, three statuses, no fourth state."""

    def test_codes_map_to_owners(self):
        table = {
            "ENVIRONMENT_ERROR": "environment",
            "AGENT_AUTH_REQUIRED": "environment",
            "COMMAND_FAILED": "environment",
            "POLICY_DENIED": "policy",
            "SCOPE_VIOLATION": "policy",
            "RUN_TIMEOUT": "timeout",
            "CHECK_FAILED": "test",
            "AGENT_FAILED": "test",
        }
        for code, expected in table.items():
            with self.subTest(code=code):
                self.assertEqual(_pipeline.error_class(code), expected)

    def test_exit_codes_separate_missing_binaries_from_failing_tests(self):
        self.assertEqual(_pipeline.error_class(None, 127), "environment")
        self.assertEqual(_pipeline.error_class(None, 126), "environment")
        self.assertEqual(_pipeline.error_class(None, 124), "timeout")
        self.assertEqual(_pipeline.error_class(None, 1), "test")
        self.assertIsNone(_pipeline.error_class(None, None))


class _Backend:
    """A backend whose check either fails with a code or returns an exit status."""

    def __init__(self, *, code=None, exit_code=0):
        self.code = code
        self.exit_code = exit_code

    def execute(self, store, task, run, argv, label):
        if self.code:
            raise Error(self.code, "check could not run")
        return self.exit_code


class TestCheckRowsCarryTheirClass(unittest.TestCase):
    """T14: a deleted test is a `test` failure; an unrunnable check is not."""

    def run_checks(self, backend):
        results, errors = _pipeline.run_checks(
            backend,
            None,
            None,
            None,
            [{"id": "unit", "argv": ["true"]}],
            None,
            "marker",
        )
        return results[0]

    def test_a_failing_check_counts_against_the_repair_budget(self):
        row = self.run_checks(_Backend(exit_code=1))
        self.assertEqual(row["status"], "FAIL")
        self.assertEqual(row["error_class"], "test")

    def test_a_missing_binary_is_an_environment_failure(self):
        row = self.run_checks(_Backend(exit_code=127))
        self.assertEqual(row["status"], "FAIL")
        self.assertEqual(row["error_class"], "environment")

    def test_a_timeout_is_a_failure_not_a_pass(self):
        row = self.run_checks(_Backend(exit_code=124))
        self.assertEqual(row["status"], "FAIL")
        self.assertEqual(row["error_class"], "timeout")

    def test_a_check_that_cannot_execute_is_explained(self):
        row = self.run_checks(_Backend(code="ENVIRONMENT_ERROR"))
        self.assertEqual(row["status"], "FAIL")
        self.assertEqual(row["error_class"], "environment")
        self.assertEqual(row["error"]["code"], "ENVIRONMENT_ERROR")

    def test_a_pass_carries_no_class(self):
        row = self.run_checks(_Backend(exit_code=0))
        self.assertEqual(row["status"], "PASS")
        self.assertIsNone(row["error_class"])
