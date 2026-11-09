"""CLI-level tests added for the 0.2.0 preview.

Covers the new user entry points that the runtime tests do not:
- ``zoe doctor`` without a project (no state directory created).
- ``zoe demo`` is a simulation and never invokes a real agent/remote.
- Errors are emitted as structured JSON, never a traceback.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]


def _run(*argv, home=None):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    if home is not None:
        env["ZOE_HOME"] = home
    return subprocess.run(
        [sys.executable, "-m", "src", *argv],
        capture_output=True,
        text=True,
        env=env,
    )


class TestDoctorWithoutProject(unittest.TestCase):
    def test_does_not_create_state_dir(self):
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "state")
            p = _run("doctor", "--json", home=home)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertFalse(os.path.exists(home), "host doctor must not create state.db")

    def test_json_shape_backcompat(self):
        p = _run("doctor", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        for key in (
            "version",
            "project_id",
            "checks",
            "ready",
            "native_resume",
            "resume_mode",
            "network",
        ):
            self.assertIn(key, data)


class TestDemoSimulation(unittest.TestCase):
    def test_demo_is_simulation(self):
        p = _run("demo")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertTrue(data["simulation"])
        self.assertFalse(data["real_agent_invoked"])
        self.assertFalse(data["real_remote_write"])
        self.assertFalse(data["supervisor_launch_verified"])
        self.assertEqual(data["scenario"]["success"]["final_state"], "REVIEW_READY")
        self.assertEqual(data["scenario"]["rejection"]["block_code"], "VALIDATION_FAILED")

    def test_demo_ignores_real_home(self):
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "real-home")
            p = _run("demo", home=home)
            self.assertEqual(p.returncode, 0, p.stderr)
            # Demo runs in its own temp dir; the user's home must not be touched.
            self.assertFalse(os.path.exists(home))


class TestErrorIsJson(unittest.TestCase):
    def test_unknown_command_is_json_error(self):
        p = _run("task", "status", "no-such-task", "--json")
        self.assertNotEqual(p.returncode, 0)
        # Must be a JSON error, not a Python traceback.
        self.assertNotIn("Traceback", p.stdout)
        data = json.loads(p.stdout)
        self.assertIn("error", data)
        self.assertIn("code", data["error"])


if __name__ == "__main__":
    unittest.main()
