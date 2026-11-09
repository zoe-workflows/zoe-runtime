"""Delivery-evidence negatives (design 1.0 §19.1 "证据" suite).

Six ways a candidate could be made to look bigger or safer than it is:

* an ignored file is smuggled in;
* a symlink is counted as a regular file;
* a filename with a newline splits the manifest;
* a protected path is deleted to dodge a review;
* a repository lives inside the controller's own runtime;
* the delivered tree changes between verification and publication.

The controller must *record what is really there* and refuse the rest; none of
these may end in a quieter result than the truth.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src._backend import fingerprint, manifest, scope
from src._util import Error, git


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class EvidenceFixture(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.repo = Path(self._td.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "test")
        git(self.repo, "config", "user.email", "test@localhost")
        write(self.repo / "src.txt", "initial\n")
        write(self.repo / ".github" / "workflows" / "ci.yml", "on: push\n")
        write(self.repo / ".gitignore", "*.log\nsecret.txt\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "initial")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-b", "zoe/task-evidence")
        self.config = {
            "allowed_paths": ["src.txt", ".github", "notes"],
            "protected_paths": [".github", ".env"],
        }

    def tearDown(self):
        self._td.cleanup()

    def task(self):
        return {
            "workspace": str(self.repo),
            "config": self.config,
            "base_sha": self.base,
            "branch": "zoe/task-evidence",
        }

    def names(self):
        return {entry[0] for entry in manifest(self.task())}


class TestIgnoredFilesAreNotClaimed(EvidenceFixture):
    def test_ignored_file_is_absent_from_the_manifest(self):
        write(self.repo / "notes.log", "runaway\n")
        write(self.repo / "secret.txt", "token\n")
        write(self.repo / "notes" / "kept.md", "kept\n")
        names = self.names()
        self.assertIn("notes/kept.md", names)
        self.assertNotIn("notes.log", names, "a .gitignore bypass must not become deliverable")
        self.assertNotIn("secret.txt", names)
        # And it is not silently reported as a scope violation either.
        scope(self.task())


class TestSymlinksAreRecordedAsLinks(EvidenceFixture):
    def test_symlink_is_not_followed(self):
        os.symlink("/etc/hostname", self.repo / "notes-link")
        entries = {entry[0]: entry for entry in manifest(self.task())}
        self.assertEqual(entries["notes-link"][1], "symlink")
        self.assertEqual(entries["notes-link"][2], "/etc/hostname")
        # No content digest was produced, so a target swap cannot fake a file.
        self.assertEqual(len(entries["notes-link"]), 3)


class TestFilenamesWithNewlines(EvidenceFixture):
    def test_newline_in_filename_does_not_split_the_manifest(self):
        tricky = "notes/line\nbreak.md"
        write(self.repo / tricky, "x\n")
        names = self.names()
        self.assertIn(tricky, names, "the manifest must keep -z semantics")
        self.assertEqual(len([name for name in names if name.startswith("notes/line")]), 1)


class TestProtectedPaths(EvidenceFixture):
    def test_deleting_a_protected_file_is_refused(self):
        (self.repo / ".github" / "workflows" / "ci.yml").unlink()
        with self.assertRaises(Error) as ctx:
            scope(self.task())
        self.assertEqual(ctx.exception.code, "SCOPE_VIOLATION")

    def test_editing_a_protected_file_is_refused(self):
        write(self.repo / ".github" / "workflows" / "ci.yml", "on: pull_request\n")
        with self.assertRaises(Error) as ctx:
            scope(self.task())
        self.assertEqual(ctx.exception.code, "SCOPE_VIOLATION")


class TestControllerLocationsAreNeverExecuted(unittest.TestCase):
    def test_repo_inside_the_controller_package_is_refused(self):
        from src._config import validate_config

        package = Path(__file__).resolve().parents[1] / "src"
        with self.assertRaises(Error) as ctx:
            validate_config({"schema_version": 2, "project_id": "x", "repo": str(package)})
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")


class TestFingerprintTracksTheWholeTree(EvidenceFixture):
    def test_any_deliverable_change_moves_the_fingerprint(self):
        before = fingerprint(self.task())
        write(self.repo / "notes" / "new.md", "added\n")
        self.assertNotEqual(before, fingerprint(self.task()))
        write(self.repo / "notes" / "new.md", "added again\n")
        self.assertNotEqual(before, fingerprint(self.task()))

    def test_ignored_churn_does_not_move_the_fingerprint(self):
        before = fingerprint(self.task())
        write(self.repo / "noise.log", "ignored\n")
        self.assertEqual(before, fingerprint(self.task()))


if __name__ == "__main__":
    unittest.main()
