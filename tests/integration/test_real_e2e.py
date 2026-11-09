"""One real end-to-end cell of the §19.2 matrix, on this host.

It is skipped unless ``ZOE_REAL_E2E=1``; a skipped run is not evidence and must
never be reported as one (``docs/capabilities.md`` keeps `real-e2e: NOT_RUN`
until this file has actually run on the target platform).

Since v0.5.0 there is no container cell to test: the execution backend is always
``local-process``, so this file qualifies the local path on macOS or Windows.

Required environment:

``ZOE_E2E_REPO``       throwaway git clone the agent may edit
``ZOE_E2E_CHECKS``     JSON array of ``{"id": ..., "argv": [...]}`` checks
``ZOE_E2E_SPEC``       path to a spec file (Markdown with ``AC-`` ids, or spec v2)
``ZOE_E2E_CELLS``      optional ``adapter:backend:profile`` list, comma separated
``ZOE_E2E_EXPECT_VERSION`` / agent credential variables per adapter
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT
ENABLED = os.environ.get("ZOE_REAL_E2E") == "1"
DEFAULT_CELLS = ("codebuddy:codebuddy-cli:real-codebuddy",)


def zoe(home, *argv, timeout=600):
    env = {**os.environ, "PYTHONPATH": str(SRC), "ZOE_HOME": str(home)}
    proc = subprocess.run(
        [sys.executable, "-m", "src", "--home", str(home), *argv],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    return proc


def load(proc):
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"non-JSON output (rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        ) from None


@unittest.skipUnless(ENABLED, "set ZOE_REAL_E2E=1 to run the real local-CLI matrix")
class TestRealEndToEndCell(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.home = Path(self._td.name) / "state"
        self.repo = Path(os.environ["ZOE_E2E_REPO"])
        self.spec = os.environ["ZOE_E2E_SPEC"]
        self.checks = json.loads(os.environ["ZOE_E2E_CHECKS"])
        self.expected_version = os.environ.get("ZOE_E2E_EXPECT_VERSION")
        self.cells = tuple(os.environ.get("ZOE_E2E_CELLS", ",".join(DEFAULT_CELLS)).split(","))

    def tearDown(self):
        self._td.cleanup()

    def config(self, adapter, backend, profile):
        from src._agents import registry

        entry = registry.require_entry(adapter)
        return {
            "schema_version": 2,
            "project_id": "e2e",
            "repo": str(self.repo),
            # v0.5.0: local-process is the only execution backend.
            "backend": "local-process",
            "default_agent_profile": profile,
            "agent_profiles": {
                profile: {
                    "adapter": adapter,
                    "backend": backend,
                    "expected_version": self.expected_version,
                    "permission_profile": entry.factory().permission_profiles[0],
                    "credential_refs": ["model-" + adapter.split("-")[0]],
                }
            },
            "checks": self.checks,
            "allowed_paths": ["."],
            "authorization_ref": "e2e-scope",
            "authorized_actions": ["create", "execute", "publish", "accept"],
        }

    def test_cell(self):
        for cell in self.cells:
            adapter, backend, profile = (part.strip() for part in cell.split(":"))
            with self.subTest(cell=cell):
                self._run_cell(adapter, backend, profile)

    def _run_cell(self, adapter, backend, profile):
        directory = Path(self._td.name)
        config_path = directory / (f"config-{adapter}.json")
        config_path.write_text(json.dumps(self.config(adapter, backend, profile)))
        proc = zoe(self.home, "project", "register", "--config", str(config_path))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        doctor = load(zoe(self.home, "doctor", "--project", "e2e", "--json"))
        self.assertTrue(doctor["ready"], doctor["checks"])
        # The binary the doctor reports is the binary the run must launch.
        resolution = doctor["resolution"]
        self.assertIsNotNone(resolution, doctor["resolution_notes"])
        self.assertTrue(os.path.isabs(resolution["path"]))

        request = {
            "schema_version": 2,
            "request_id": f"e2e-{adapter}-{uuid.uuid4().hex[:8]}",
            "project_id": "e2e",
            "title": "real e2e " + adapter,
            "spec_file": self.spec,
            "agent_profile": profile,
            "delivery_mode": "local_artifact",
            "authorization_ref": "e2e-scope",
        }
        request_path = directory / (f"create-{adapter}.json")
        request_path.write_text(json.dumps(request))
        created = load(zoe(self.home, "task", "create", "--request-file", str(request_path)))
        task_id = created["task_id"]

        start = dict(
            request,
            request_id=request["request_id"] + "-start",
            expected_version=created["row_version"],
        )
        start_path = directory / (f"start-{adapter}.json")
        start_path.write_text(json.dumps(start))
        load(zoe(self.home, "task", "start", task_id, "--request-file", str(start_path)))

        deadline = time.time() + int(os.environ.get("ZOE_E2E_TIMEOUT", "1800"))
        while time.time() < deadline:
            status = load(zoe(self.home, "task", "status", task_id, "--refresh", "--json"))
            if status["state"] in ("REVIEW_READY", "DONE", "BLOCKED", "CANCELLED"):
                break
            time.sleep(5)
        self.assertEqual(status["state"], "REVIEW_READY", json.dumps(status)[:2000])
        run = status["current_run_id"]
        self.assertTrue(run)
        self.assertEqual(status["adapter_backend"], backend)
        self.assertTrue(status["current_evidence_id"])

        # The run record is the audit trail: the pinned path and the reported path
        # must be the same string, not merely "both resolvable".
        with sqlite3.connect(self.home / "state.db") as db:
            (data,) = db.execute("SELECT data FROM runs WHERE id=?", (run,)).fetchone()
        record = json.loads(data)
        self.assertEqual(record["resolved_path"], resolution["path"])
        self.assertEqual(record["launch"]["argv"][0], resolution["path"])
        self.assertTrue(
            (self.home / "tasks" / task_id / "runs" / run / "session" / adapter / backend).is_dir(),
            "session dir must be isolated per (adapter, backend)",
        )


if __name__ == "__main__":
    unittest.main()
