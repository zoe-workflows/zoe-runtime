"""The compatibility gate: ``zoe capabilities --check-requirements FILE``.

The gate is the only runtime check a skill can rely on (§2.2), so its edge cases
are pinned here: prereleases, the exclusive upper bound, API/revision mismatch and
unusable declarations. Every rejection is a non-zero exit, never a warning.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from src import _requirements
from src._util import Error
from src.cli import main

VALID = {
    "package": "zoe-runtime",
    "command": "zoe",
    "min_version": "1.0.0",
    "max_version_exclusive": "2.0.0",
    "api_version": 2,
    "protocol_revision": 1,
}


class GateCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

    def write(self, value) -> Path:
        path = self.dir / "runtime-requirements.json"
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        return path

    def cli(self, path) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["capabilities", "--check-requirements", str(path)])
        return code, json.loads(out.getvalue())


class TestVersionOrder(GateCase):
    def test_numeric_parts_not_lexicographic(self):
        self.assertEqual(_requirements.compare("0.5.10", "0.5.9"), 1)
        self.assertEqual(_requirements.compare("0.5.1", "0.5.1"), 0)
        self.assertEqual(_requirements.compare("0.9.0", "0.10.0"), -1)

    def test_prerelease_sorts_below_its_release(self):
        self.assertEqual(_requirements.compare("0.5.1rc1", "0.5.1"), -1)
        self.assertEqual(_requirements.compare("0.5.1", "0.5.1rc1"), 1)

    def test_short_versions_are_padded(self):
        self.assertEqual(_requirements.compare("0.5", "0.5.0"), 0)

    def test_prerelease_numbers_are_compared_naturally(self):
        """``rc9`` < ``rc10``: 字典序会反过来，而方向错误的后果是放行更老的运行时。"""
        self.assertEqual(_requirements.compare("1.5.1rc9", "1.5.1rc10"), -1)
        self.assertEqual(_requirements.compare("1.5.1rc10", "1.5.1rc9"), 1)
        self.assertEqual(_requirements.compare("1.5.1rc2", "1.5.1rc10"), -1)

    def test_garbage_is_refused_not_guessed(self):
        with self.assertRaises(Error):
            _requirements.parse_version("latest")


class TestAcceptance(GateCase):
    def test_the_current_runtime_satisfies_the_default_window(self):
        result = _requirements.check(self.write(VALID))
        self.assertTrue(result["ok"])
        self.assertEqual(result["runtime"]["api_version"], 2)

    def test_cli_exits_zero_and_reports_the_comparison(self):
        code, payload = self.cli(self.write(VALID))
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertIn("requirements_path", payload)

    def test_an_older_skill_window_still_accepts_this_runtime(self):
        self.assertTrue(_requirements.check(self.write({**VALID, "min_version": "0.4.0"}))["ok"])

    def test_required_capabilities_are_checked_by_name(self):
        """T04: a skill names the surfaces it needs instead of guessing versions."""
        accepted = _requirements.check(
            self.write({**VALID, "required_capabilities": ["task_groups", "group_budget"]})
        )
        self.assertEqual(
            accepted["requirements"]["required_capabilities"],
            ["task_groups", "group_budget"],
        )

    def test_an_unknown_required_capability_is_a_refusal(self):
        code, payload = self.cli(
            self.write({**VALID, "required_capabilities": ["group_parallel_members"]})
        )
        self.assertEqual(code, 5)
        self.assertEqual(payload["error"]["code"], "RUNTIME_INCOMPATIBLE")
        self.assertIn("not supported", payload["error"]["details"]["failures"][0])

    def test_an_empty_capability_list_is_still_valid(self):
        self.assertTrue(
            _requirements.check(self.write({**VALID, "required_capabilities": []}))["ok"]
        )


class TestRejection(GateCase):
    def assertRejected(self, declaration, needle):
        path = self.write(declaration)
        code, payload = self.cli(path)
        self.assertEqual(code, 5)
        self.assertEqual(payload["error"]["code"], "RUNTIME_INCOMPATIBLE")
        self.assertIn(needle, payload["error"]["details"]["failures"][0])

    def test_min_version_above_this_runtime(self):
        # 2.0.0 is above the current runtime (1.0.0): the window must reject it.
        self.assertRejected({**VALID, "min_version": "2.0.0"}, "min_version")

    def test_prerelease_does_not_satisfy_a_release_minimum(self):
        # 0.5.1rc1 < 0.5.1: a test prerelease is not a reason to relax a
        # published skill's window (§2.2).
        self.assertFalse(_requirements.compare("0.5.1rc1", VALID["min_version"]) >= 0)

    def test_exclusive_upper_bound(self):
        self.assertRejected({**VALID, "max_version_exclusive": "0.5.1"}, "max_version_exclusive")

    def test_an_empty_window_is_named_as_such(self):
        # min == max 没有任何运行时可满足；两条各自的原因看起来都像"改一个字段就好"。
        with self.assertRaises(Error) as ctx:
            _requirements.check(
                self.write({**VALID, "min_version": "2.0.0", "max_version_exclusive": "2.0.0"})
            )
        self.assertIn("empty version window", str(ctx.exception))

    def test_unparseable_bound_names_the_field(self):
        with self.assertRaises(Error) as ctx:
            _requirements.check(self.write({**VALID, "max_version_exclusive": "v2.0.0"}))
        message = str(ctx.exception)
        self.assertIn("max_version_exclusive", message)
        self.assertIn("not a version", message)

    def test_api_version_mismatch(self):
        self.assertRejected({**VALID, "api_version": 3}, "api_version")

    def test_protocol_revision_mismatch(self):
        self.assertRejected({**VALID, "protocol_revision": 2}, "protocol_revision")


class TestUnusableDeclaration(GateCase):
    def test_missing_file_is_input_incomplete(self):
        code, payload = self.cli(self.dir / "nope.json")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "INPUT_INCOMPLETE")

    def test_invalid_json_is_input_incomplete(self):
        code, payload = self.cli(self.write("{not json"))
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "INPUT_INCOMPLETE")

    def test_missing_field_is_input_incomplete(self):
        broken = dict(VALID)
        del broken["protocol_revision"]
        code, payload = self.cli(self.write(broken))
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "INPUT_INCOMPLETE")

    def test_wrong_type_is_input_incomplete(self):
        code, payload = self.cli(self.write({**VALID, "api_version": "2"}))
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "INPUT_INCOMPLETE")

    def test_booleans_are_not_integers_here(self):
        code, payload = self.cli(self.write({**VALID, "protocol_revision": True}))
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "INPUT_INCOMPLETE")


class TestPublicSurface(GateCase):
    def test_capabilities_without_the_flag_is_unchanged(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["capabilities", "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["api_version"], 2)
        self.assertIn("adapters", payload)
        self.assertNotIn("ok", payload)


if __name__ == "__main__":
    unittest.main()
