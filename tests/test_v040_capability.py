"""v0.4 review fixes, group 1 (CLI crashes) and group 2 (capability key).

Group 1
- ``orchestration release`` must accept ``--as`` and enforce ownership instead of
  raising ``AttributeError`` on ``args.host``.
- ``qualification run`` must be bound to a project (``--project``), otherwise the
  handler fails on ``req["project_id"]``.

Group 2
- The capability key is ``(adapter, backend, execution_backend, platform,
  version, runtime_digest)``. An identity-bound record (one that carries a
  ``runtime_digest``) must be rejected when the platform or the CLI fingerprint
  changed, and when the six checks / stop-resume are not all confirmed.
- A record without a digest keeps the legacy version-only semantics, so existing
  operator evidence is not silently invalidated.
- ``qualification run`` must emit a bundle that ``load_evidence`` can consume,
  which is what makes the qualification loop closable at all.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import _config as _config_mod
from src import _profiles, _qualification
from src._agents import registry
from src._store import Store
from src._util import Error

SRC = Path(__file__).resolve().parents[1]


def _run(*argv, home):
    env = {**os.environ, "PYTHONPATH": str(SRC), "ZOE_HOME": home}
    return subprocess.run(
        [sys.executable, "-m", "src", *argv],
        capture_output=True,
        text=True,
        env=env,
    )


def _claim(home, host):
    return _run("orchestration", "claim", "--project", "demo", "--as", host, "--json", home=home)


def _release(home, host):
    return _run("orchestration", "release", "--project", "demo", "--as", host, "--json", home=home)


PASS_CHECKS = [
    {"id": "cli-found", "status": "PASS"},
    {"id": "version", "status": "PASS"},
    {"id": "auth", "status": "PASS"},
    {"id": "noninteractive-edit", "status": "PASS"},
    {"id": "parse", "status": "PASS"},
    {"id": "stop-resume", "status": "PASS"},
]


class CapabilityFixture(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.home = self.root / "home"
        self.store = Store(self.home)
        (self.home / "projects").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.store.db.close()
        self.td.cleanup()

    # -- fixtures ---------------------------------------------------------- #
    def project(self, evidence=None, expected_version="1.2.3", backend="codebuddy-cli"):
        profile = {
            "adapter": "codebuddy",
            "model": "stub-model",
            "backend": backend,
            "expected_version": expected_version,
            "permission_profile": "isolated-edit",
        }
        if evidence is not None:
            profile["compatibility_evidence"] = str(evidence)
        config = {
            "schema_version": 2,
            "project_id": "demo",
            "repo": str(self.repo),
            "backend": "local-process",
            "default_agent_profile": "p1",
            "agent_profiles": {"p1": profile},
            "checks": [{"id": "unit", "argv": ["true"]}],
            "allowed_paths": ["src.txt"],
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute", "publish", "accept"],
        }
        (self.home / "projects" / "demo.json").write_text(json.dumps(config))
        return config

    def evidence(
        self,
        *,
        digest="sha256:abc",
        platform="macos-test-arm64",
        checks=None,
        stopped=True,
        version="1.2.3",
        adapter="codebuddy",
        backend="codebuddy-cli",
    ):
        record = {
            "adapter_id": adapter,
            "backend": backend,
            "version": version,
            "source": "real_e2e",
            "platform": platform,
            "execution_backend": "local-process",
            "runtime_digest": digest,
            "stopped_and_resumed": stopped,
            "checks": PASS_CHECKS if checks is None else checks,
            "recorded_at": "2026-10-27",
        }
        path = self.root / f"ev-{adapter}-{backend}-{version}-{platform}-{digest}.json"
        path.write_text(json.dumps({"schema_version": 1, "records": [record]}))
        return path

    def binding(self, evidence, platform="macos-test-arm64", digest="sha256:abc"):
        c = _profiles.normalize_config(self.project(evidence=evidence))
        with mock.patch.multiple(
            _qualification,
            platform_id=mock.Mock(return_value=platform),
            current_runtime_digest=mock.Mock(return_value=digest),
        ):
            return _profiles.resolve_binding(c, "p1")


class TestCapabilityKey(CapabilityFixture):
    def test_matching_platform_and_digest_verifies(self):
        binding = self.binding(self.evidence())
        self.assertEqual(binding.capability, "VERIFIED")
        self.assertIsNotNone(_profiles.require_runnable(binding))

    def test_platform_change_invalidates_evidence(self):
        binding = self.binding(self.evidence(platform="windows-other-x86"))
        self.assertEqual(binding.capability, "IMPLEMENTED")
        self.assertIn("platform", binding.capability_reason)
        with self.assertRaises(Error) as ctx:
            _profiles.require_runnable(binding)
        self.assertEqual(ctx.exception.code, "ADAPTER_UNVERIFIED")

    def test_runtime_digest_change_invalidates_evidence(self):
        binding = self.binding(self.evidence(digest="sha256:other"))
        self.assertEqual(binding.capability, "IMPLEMENTED")
        with self.assertRaises(Error) as ctx:
            _profiles.require_runnable(binding)
        self.assertEqual(ctx.exception.code, "ADAPTER_UNVERIFIED")

    def test_undiscoverable_cli_never_verifies(self):
        binding = self.binding(self.evidence(), digest=None)
        self.assertEqual(binding.capability, "IMPLEMENTED")

    def test_incomplete_checks_stay_unverified(self):
        checks = [dict(c) for c in PASS_CHECKS]
        checks[2] = {"id": "auth", "status": "INCOMPLETE"}
        binding = self.binding(self.evidence(checks=checks))
        self.assertEqual(binding.capability, "UNVERIFIED")
        self.assertIn("auth", binding.capability_reason)

    def test_missing_stop_resume_stays_unverified(self):
        binding = self.binding(self.evidence(stopped=False))
        self.assertEqual(binding.capability, "UNVERIFIED")
        self.assertIn("stop-resume", binding.capability_reason)

    def test_legacy_record_without_digest_keeps_old_semantics(self):
        """Existing operator records must not be invalidated by the new key."""
        path = self.root / "legacy-evidence.json"
        path.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "adapter_id": "codebuddy",
                            "backend": "codebuddy-cli",
                            "version": "1.2.3",
                            "source": "local_admin",
                            "platform": "linux",
                            "recorded_at": "2026-10-27",
                        }
                    ]
                }
            )
        )
        binding = self.binding(path)
        self.assertEqual(binding.capability, "VERIFIED")

    def test_from_dict_accepts_qualification_field_names(self):
        record = registry.Verification.from_dict(
            {
                "adapter_id": "codebuddy",
                "adapter_backend": "codebuddy-cli",
                "agent_version": "1.2.3",
                "platform": "macos-test-arm64",
                "runtime_digest": "sha256:abc",
                "stopped_and_resumed": True,
                "checks": PASS_CHECKS,
            }
        )
        self.assertEqual(record.backend, "codebuddy-cli")
        self.assertEqual(record.version, "1.2.3")
        self.assertTrue(record.identity_bound)
        self.assertTrue(record.stopped_and_resumed)
        self.assertEqual(len(record.checks), 6)


class TestQualificationLoop(CapabilityFixture):
    def test_output_is_consumable_as_evidence(self):
        """The bundle ``qualification run`` writes must load as evidence."""
        c = self.project()
        out = self.root / "qual.json"
        with (
            mock.patch.multiple(
                _qualification,
                _find_cli=mock.Mock(return_value="/usr/local/bin/codebuddy"),
                _version_of=mock.Mock(return_value="1.2.3"),
                _runtime_digest=mock.Mock(return_value="sha256:abc"),
            ),
            mock.patch.multiple(
                _config_mod,
                _platform_lock_ok=mock.Mock(return_value=True),
                _platform_tree_ok=mock.Mock(return_value=True),
            ),
        ):
            result = _qualification.run(
                self.store,
                {"project_id": "demo", "profile": "p1", "repo": str(self.repo), "out": str(out)},
            )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["records"], 1)
        self.assertTrue(out.is_file())

        bundle = json.loads(out.read_text())
        self.assertIn("records", bundle)
        record = bundle["records"][0]
        for key in ("adapter_backend", "agent_version", "platform", "runtime_digest", "checks"):
            self.assertIn(key, record)

        # It parses as evidence but must NOT unlock the backend: four of the six
        # checks could not run without a real turn.
        loaded = _profiles.load_evidence(
            {"compatibility_evidence": str(out)}, "codebuddy", "codebuddy-cli"
        )
        self.assertIsNotNone(loaded)
        self.assertTrue(loaded.identity_bound)

        project = dict(c)
        project["agent_profiles"] = {
            "p1": {**c["agent_profiles"]["p1"], "compatibility_evidence": str(out)}
        }
        normalized = _profiles.normalize_config(project)
        with mock.patch.multiple(
            _qualification,
            platform_id=mock.Mock(return_value=record["platform"]),
            current_runtime_digest=mock.Mock(return_value="sha256:abc"),
        ):
            binding = _profiles.resolve_binding(normalized, "p1")
        self.assertEqual(binding.capability, "UNVERIFIED")
        self.assertIn("incomplete", binding.capability_reason)


class TestCliWiring(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.home = self.td.name

    def tearDown(self):
        self.td.cleanup()

    def test_orchestration_release_accepts_as_and_enforces_owner(self):
        claim = _claim(self.home, "hostA")
        self.assertEqual(claim.returncode, 0, claim.stderr)
        self.assertEqual(json.loads(claim.stdout)["owner"]["host"], "hostA")

        released = _release(self.home, "hostA")
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertIsNone(json.loads(released.stdout)["owner"])

        _claim(self.home, "hostB")
        denied = _release(self.home, "hostA")
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(json.loads(denied.stdout)["error"]["code"], "NOT_OWNER")

    def test_qualification_run_requires_project(self):
        missing = _run(
            "qualification",
            "run",
            "--profile",
            "p",
            "--repo",
            self.home,
            "--out",
            os.path.join(self.home, "q.json"),
            home=self.home,
        )
        self.assertEqual(missing.returncode, 2, missing.stderr)
        self.assertIn("--project", missing.stderr)

        # With --project the handler is reached (and reports the missing config,
        # not a KeyError on 'project_id').
        present = _run(
            "qualification",
            "run",
            "--project",
            "demo",
            "--profile",
            "p",
            "--repo",
            self.home,
            "--out",
            os.path.join(self.home, "q.json"),
            "--json",
            home=self.home,
        )
        self.assertNotIn("project_id", present.stdout)
        self.assertNotIn("Traceback", present.stderr)


if __name__ == "__main__":
    unittest.main()
