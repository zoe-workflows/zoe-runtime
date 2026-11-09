"""v0.5.1 W10 (optional): `--request-file -` and `error.hint`.

Both are small host-ergonomics fixes with one hard rule: they must not weaken a
verdict. A hint never supplies a default value, and reading from stdin must not
give a second, different parser the chance to disagree with the file path.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import cli
from src._util import Error, git, read

HINTED = (
    "INPUT_INCOMPLETE",
    "STATE_CONFLICT",
    "SLOT_BUSY",
    "POLICY_CHANGED",
    "ADAPTER_UNVERIFIED",
)


class TestStdinRequests(unittest.TestCase):
    def test_read_dash_uses_stdin(self):
        with mock.patch("sys.stdin", io.StringIO('{"request_id": "r1"}')):
            self.assertEqual(read("-"), {"request_id": "r1"})

    def test_the_cli_accepts_dash_for_a_request_file(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root) / "repo"
            repo.mkdir()
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.name", "test")
            git(repo, "config", "user.email", "test@localhost")
            (repo / "src.txt").write_text("initial\n")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "initial")
            self.check_register(str(repo))

    def check_register(self, repo_path):
        """`project register --config -` reads the same JSON from stdin."""
        config = {
            "schema_version": 1,
            "project_id": "demo",
            "repo": repo_path,
            "backend": "local-process",
            "network": "none",
            "compatibility_verified": True,
            "agent": "codebuddy",
            "checks": [{"id": "lint", "argv": ["true"]}],
            "allowed_paths": ["src.txt"],
            "authorization_ref": "stdin-1",
            "authorized_actions": ["create"],
        }
        with tempfile.TemporaryDirectory() as home:
            buffer = io.StringIO()
            with (
                mock.patch("sys.stdout", buffer),
                mock.patch("sys.stdin", io.StringIO(json.dumps(config))),
            ):
                code = cli.main(["--home", str(home), "project", "register", "--config", "-"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buffer.getvalue())["registered"], "demo")


class TestErrorHints(unittest.TestCase):
    def test_the_five_common_codes_carry_a_hint(self):
        for code in HINTED:
            with self.subTest(code):
                value = Error(code, "boom").to_dict()
                self.assertEqual(value["code"], code)
                self.assertTrue(value["hint"])

    def test_a_hint_never_replaces_the_message_or_details(self):
        value = Error("SLOT_BUSY", "2/1 active runs", details={"limit": 1}).to_dict()
        self.assertEqual(value["message"], "2/1 active runs")
        self.assertEqual(value["details"], {"limit": 1})

    def test_unlisted_codes_keep_the_old_shape(self):
        self.assertEqual(
            Error("POLICY_DENIED", "no").to_dict(), {"code": "POLICY_DENIED", "message": "no"}
        )

    def test_task_busy_keeps_its_advice_inside_details(self):
        value = Error("TASK_BUSY", "held", details={"task_id": "t"}).to_dict()
        self.assertEqual(value["details"], {"task_id": "t"})
        self.assertNotIn("hint", value, "TASK_BUSY puts its advice in details")


if __name__ == "__main__":
    unittest.main()
