"""CLI discovery: resolution order, bounded fallbacks and argv[0] pinning (§6).

Everything here is hermetic: stubs are small shell scripts in a temporary
directory, and ``PATH``/``SHELL`` are patched per test. The suite must pass on a
machine with no agent CLI installed at all.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src import _discovery
from src._agents.base import LaunchSpec
from src._platform import is_windows
from src._util import Error

POSIX_ONLY = unittest.skipIf(is_windows(), "interactive-shell fallback is POSIX only")


def make_stub(directory: Path, name: str, body: str = 'echo "1.0.0"') -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return path


def parse_semver(text):
    import re

    found = re.search(r"\d+\.\d+\.\d+", text)
    return found.group(0) if found else None


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.known = self.root / "known"
        self.known.mkdir()
        _discovery.clear_cache()

    def tearDown(self):
        _discovery.clear_cache()
        self.tmp.cleanup()

    def patch_path(self, directory: Path):
        return patch.dict(os.environ, {"PATH": str(directory)}, clear=False)

    def facts(self, name, **overrides):
        facts = {
            "aliases": (name,),
            "common_paths": (),
            "install_test": f"command -v {name}",
            "version_argv": ("--version",),
            "headless": "AVAILABLE",
            "config_sources": "UNKNOWN",
        }
        facts.update(overrides)
        return patch.dict(_discovery.CLI_FACTS, {name: facts})

    # ------------------------------------------------------------------ order

    def test_process_path_wins_and_is_absolute(self):
        stub = make_stub(self.bin, "zoe-faux-cli")
        with self.patch_path(self.bin), self.facts("zoe-faux-cli"):
            resolution = _discovery.resolve_cli("zoe-faux-cli")
        self.assertEqual(resolution.path, str(stub))
        self.assertEqual(resolution.source, _discovery.SOURCE_PROCESS_PATH)
        self.assertTrue(os.path.isabs(resolution.path))

    def test_known_location_is_used_when_the_path_misses(self):
        stub = make_stub(self.known, "zoe-faux-cli")
        with self.patch_path(self.bin), self.facts("zoe-faux-cli", common_paths=(str(stub),)):
            resolution = _discovery.resolve_cli("zoe-faux-cli")
        self.assertEqual(resolution.path, str(stub))
        self.assertEqual(resolution.source, _discovery.SOURCE_KNOWN_LOCATION)
        self.assertEqual(resolution.notes, ())

    def test_multi_version_selection_is_reported_not_hidden(self):
        first = make_stub(self.bin, "zoe-faux-cli")
        second = make_stub(self.known, "zoe-faux-cli")
        with self.patch_path(self.bin), self.facts("zoe-faux-cli", common_paths=(str(second),)):
            resolution = _discovery.resolve_cli("zoe-faux-cli")
        self.assertEqual(resolution.path, str(first))
        notes = " ".join(resolution.notes)
        self.assertIn(str(first), notes)
        self.assertIn(str(second), notes)
        self.assertIn("multiple versions found", notes)

    def test_alias_is_tried_when_the_primary_name_is_absent(self):
        alias = make_stub(self.bin, "cbc-stub")
        with (
            self.patch_path(self.bin),
            self.facts("zoe-faux-cli", aliases=("zoe-absent", "cbc-stub")),
        ):
            resolution = _discovery.resolve_cli("zoe-faux-cli")
        self.assertEqual(resolution.path, str(alias))

    def test_unresolved_records_a_note_instead_of_guessing(self):
        with (
            self.patch_path(self.bin),
            self.facts("zoe-faux-cli"),
            patch.dict(os.environ, {"SHELL": ""}),
        ):
            resolution = _discovery.resolve_cli("zoe-faux-cli")
        self.assertIsNone(resolution.path)
        self.assertEqual(resolution.source, _discovery.SOURCE_UNRESOLVED)
        self.assertTrue(resolution.notes)
        self.assertIn(resolution.source, _discovery.SOURCES)

    # --------------------------------------------------------------- explicit

    def test_explicit_path_wins_and_is_reported(self):
        stub = make_stub(self.known, "zoe-faux-cli")
        make_stub(self.bin, "zoe-faux-cli")
        with self.patch_path(self.bin), self.facts("zoe-faux-cli"):
            resolution = _discovery.resolve_cli("zoe-faux-cli", explicit=str(stub))
        self.assertEqual(resolution.path, str(stub))
        self.assertEqual(resolution.source, _discovery.SOURCE_EXPLICIT)

    def test_relative_explicit_path_is_recorded_and_skipped(self):
        make_stub(self.bin, "zoe-faux-cli")
        with self.patch_path(self.bin), self.facts("zoe-faux-cli"):
            resolution = _discovery.resolve_cli("zoe-faux-cli", explicit="./bin/zoe-faux-cli")
        self.assertEqual(resolution.source, _discovery.SOURCE_PROCESS_PATH)
        self.assertIn("executable absolute path", " ".join(resolution.notes))

    def test_non_executable_explicit_path_is_skipped(self):
        plain = self.known / "zoe-faux-cli"
        plain.write_text("not executable")
        stub = make_stub(self.bin, "zoe-faux-cli")
        with self.patch_path(self.bin), self.facts("zoe-faux-cli"):
            resolution = _discovery.resolve_cli("zoe-faux-cli", explicit=str(plain))
        self.assertEqual(resolution.path, str(stub))

    # ------------------------------------------------------------ login shell

    @POSIX_ONLY
    def test_login_shell_is_used_as_the_last_resort(self):
        target = make_stub(self.known, "zoe-faux-cli")
        shell = make_stub(self.bin, "fake-shell", 'echo "rc noise"\necho "$ZOE_SHELL_PATH"')
        with (
            self.patch_path(self.bin),
            self.facts("zoe-faux-cli"),
            patch.dict(os.environ, {"SHELL": str(shell), "ZOE_SHELL_PATH": str(target)}),
        ):
            resolution = _discovery.resolve_cli("zoe-faux-cli")
        self.assertEqual(resolution.path, str(target))
        self.assertEqual(resolution.source, _discovery.SOURCE_LOGIN_SHELL)

    @POSIX_ONLY
    def test_login_shell_output_that_is_not_a_path_is_discarded(self):
        shell = make_stub(self.bin, "fake-shell", 'echo "zoe-faux-cli () { :; }"')
        with (
            self.patch_path(self.bin),
            self.facts("zoe-faux-cli"),
            patch.dict(os.environ, {"SHELL": str(shell)}),
        ):
            resolution = _discovery.resolve_cli("zoe-faux-cli")
        self.assertIsNone(resolution.path)
        self.assertIn("no absolute path", " ".join(resolution.notes))

    @POSIX_ONLY
    def test_login_shell_is_bounded_and_does_not_block(self):
        shell = make_stub(self.bin, "fake-shell", "exec /bin/sleep 30")
        started = time.monotonic()
        with (
            self.patch_path(self.bin),
            self.facts("zoe-faux-cli"),
            patch.dict(os.environ, {"SHELL": str(shell)}),
        ):
            resolution = _discovery.resolve_cli("zoe-faux-cli", timeout=0.5)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10.0)
        self.assertIsNone(resolution.path)
        self.assertIn("timeout", " ".join(resolution.notes))

    @POSIX_ONLY
    def test_missing_shell_is_skipped_without_falling_back_to_sh(self):
        with (
            self.patch_path(self.bin),
            self.facts("zoe-faux-cli"),
            patch.dict(os.environ, {"SHELL": ""}),
        ):
            resolution = _discovery.resolve_cli("zoe-faux-cli")
        self.assertIn("no interactive shell", " ".join(resolution.notes))

    # ------------------------------------------------------------------ names

    def test_shell_metacharacters_never_reach_a_shell(self):
        for name in ("codebuddy; rm -rf /", "$(id)", "a b", "-rf", ""):
            with self.subTest(name=name):
                with self.assertRaises(Error):
                    _discovery.resolve_cli(name)

    # ------------------------------------------------------------ version/env

    def test_version_is_probed_in_the_launch_environment_only(self):
        stub = make_stub(
            self.bin,
            "zoe-faux-cli",
            'if [ "$1" = "--version" ]; then echo "zoe-faux $ZOE_TEST_SECRET"; fi',
        )
        with (
            self.patch_path(self.bin),
            self.facts("zoe-faux-cli"),
            patch.dict(os.environ, {"ZOE_TEST_SECRET": "2.5.1"}),
        ):
            declared = _discovery.resolve_cli(
                "zoe-faux-cli", env_names=("ZOE_TEST_SECRET",), parse_version=parse_semver
            )
            _discovery.clear_cache()
            undeclared = _discovery.resolve_cli(
                "zoe-faux-cli", env_names=(), parse_version=parse_semver
            )
        self.assertEqual(str(stub), declared.path)
        self.assertEqual(declared.version, "2.5.1")
        self.assertIsNone(undeclared.version, "undeclared env names must not leak in")

    def test_resolution_is_cached_per_process(self):
        make_stub(self.bin, "zoe-faux-cli")
        with self.patch_path(self.bin), self.facts("zoe-faux-cli"):
            first = _discovery.resolve_cli("zoe-faux-cli")
            second = _discovery.resolve_cli("zoe-faux-cli")
        self.assertIs(first, second)

    # ----------------------------------------------------------------- pinning

    def test_pinning_rewrites_only_argv0(self):
        stub = make_stub(self.bin, "zoe-faux-cli")
        launch = LaunchSpec(
            argv=("zoe-faux-cli", "-p", "--output-format", "json"),
            env_names=("ZOE_API_KEY",),
            writable_dirs=("/tmp/work",),
        )
        with self.patch_path(self.bin):
            pinned = _discovery.pin_launcher(launch, "local-process")
        self.assertEqual(pinned.argv[0], str(stub))
        self.assertEqual(pinned.argv[1:], launch.argv[1:])
        self.assertEqual(pinned.env_names, launch.env_names)
        self.assertEqual(pinned.writable_dirs, launch.writable_dirs)
        self.assertEqual(pinned.extra["resolution"]["source"], "process-path")

    def test_pinning_applies_to_the_legacy_local_alias(self):
        stub = make_stub(self.bin, "zoe-faux-cli")
        launch = LaunchSpec(argv=("zoe-faux-cli",))
        with self.patch_path(self.bin):
            pinned = _discovery.pin_launcher(launch, "local")
        self.assertEqual(pinned.argv[0], str(stub))

    def test_pinning_never_touches_a_container_backend(self):
        launch = LaunchSpec(argv=("codebuddy",))
        self.assertIs(_discovery.pin_launcher(launch, "docker"), launch)

    def test_an_absolute_argv0_is_respected_not_re_resolved(self):
        """A profile that already pinned a path keeps it, even if PATH differs."""
        chosen = make_stub(self.known, "zoe-faux-cli")
        make_stub(self.bin, "zoe-faux-cli")  # a different binary, earlier on PATH
        launch = LaunchSpec(argv=(str(chosen), "--flag"))
        with self.patch_path(self.bin):
            pinned = _discovery.pin_launcher(launch, "local-process")
        self.assertEqual(pinned.argv, (str(chosen), "--flag"))
        self.assertEqual(pinned.extra["resolution"]["source"], "explicit")

    def test_unresolved_launch_keeps_the_bare_name_and_records_why(self):
        launch = LaunchSpec(argv=("zoe-absent-cli",))
        with self.patch_path(self.bin), patch.dict(os.environ, {"SHELL": ""}):
            pinned = _discovery.pin_launcher(launch, "local-process")
        self.assertEqual(pinned.argv[0], "zoe-absent-cli")
        self.assertEqual(pinned.extra["resolution"]["source"], "unresolved")
        self.assertIsNone(pinned.extra["resolution"]["path"])


class FactsContractTest(unittest.TestCase):
    """``CLI_FACTS`` is detection-only and must not drift from the registry."""

    LAUNCH_FIELDS = {
        "promptTransport",
        "prompt_transport",
        "sendKeys",
        "send_keys",
        "permission_mode_flag",
        "argv",
        "flags",
    }

    def test_every_registered_adapter_has_a_name(self):
        from src._agents import registry

        for entry in registry.entries():
            self.assertIn(entry.adapter_id, _discovery.ADAPTER_CLI, entry.adapter_id)
            self.assertIn(_discovery.ADAPTER_CLI[entry.adapter_id], _discovery.CLI_FACTS)

    def test_facts_carry_no_launch_shape(self):
        for name, facts in _discovery.CLI_FACTS.items():
            for field in self.LAUNCH_FIELDS:
                self.assertNotIn(field, facts, f"{name}: launch decision in CLI_FACTS")

    def test_common_paths_are_absolute(self):
        for name, facts in _discovery.CLI_FACTS.items():
            for path in facts["common_paths"]:
                self.assertTrue(os.path.isabs(path), f"{name}: {path} is not absolute")

    def test_aliases_are_valid_binary_names(self):
        for name, facts in _discovery.CLI_FACTS.items():
            self.assertEqual(facts["aliases"][0], name)
            for alias in facts["aliases"]:
                self.assertRegex(alias, r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


if __name__ == "__main__":
    unittest.main()
