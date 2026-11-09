"""Distribution proof: the installed wheel works with no source tree nearby.

This is the executable form of the release checks (§3.2). ``grep "schemas"``
returning nothing is only a hint; the conclusion has to come from running the
installed console script outside the checkout (boundary 5 of §3.3).

The suite is skipped unless ``ZOE_DIST_PYTHON`` names the interpreter of a fresh
environment that installed ``dist/*.whl``. CI sets it; a local ``pytest`` run
should not pretend it validated a distribution.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from src import __version__

ROOT = Path(__file__).resolve().parents[1]
DIST_PYTHON = os.environ.get("ZOE_DIST_PYTHON")

SKIP_REASON = (
    "set ZOE_DIST_PYTHON=/path/to/venv/bin/python (an install of dist/*.whl) "
    "to run the distribution proof"
)


def dist_run(*argv, cwd=None) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.pop("ZOE_HOME", None)
    return subprocess.run(
        [DIST_PYTHON, "-m", "src", *argv],
        cwd=cwd or tempfile.gettempdir(),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@unittest.skipUnless(DIST_PYTHON, SKIP_REASON)
class TestInstalledDistribution(unittest.TestCase):
    def test_the_interpreter_is_outside_this_checkout(self):
        """Otherwise the "installed" run could silently import ``src/``."""
        probe = subprocess.run(
            [
                DIST_PYTHON,
                "-c",
                "import src, pathlib; print(pathlib.Path(src.__file__).resolve())",
            ],
            # cwd matters more than usual here: the package is named `src`, so a
            # process started in this checkout would import the source tree even
            # though the wheel is installed.
            cwd=tempfile.gettempdir(),
            capture_output=True,
            text=True,
            env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
            timeout=60,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        location = Path(probe.stdout.strip())
        self.assertFalse(
            ROOT in location.parents or location.parent == ROOT,
            f"distribution check imported the source tree: {location}",
        )
        self.assertIn("site-packages", location.as_posix())

    def test_version_matches_the_package_metadata(self):
        result = dist_run("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), __version__)

    def test_capabilities_from_outside_the_source_tree(self):
        result = dist_run("capabilities", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["api_version"], 2)
        self.assertEqual(payload["version"], __version__)
        self.assertIn("protocol_revision", payload)
        self.assertIn("adapters", payload)
        self.assertNotIn(str(ROOT), result.stdout)

    def test_requirements_gate_from_outside_the_source_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-requirements.json"
            path.write_text(
                json.dumps(
                    {
                        "package": "zoe-runtime",
                        "command": "zoe",
                        "min_version": __version__,
                        "max_version_exclusive": "99.0.0",
                        "api_version": 2,
                        "protocol_revision": 1,
                    }
                ),
                encoding="utf-8",
            )
            accepted = dist_run("capabilities", "--check-requirements", str(path))
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertTrue(json.loads(accepted.stdout)["ok"])

            path.write_text(
                path.read_text(encoding="utf-8").replace(__version__, "99.0.0"),
                encoding="utf-8",
            )
            rejected = dist_run("capabilities", "--check-requirements", str(path))
            self.assertEqual(rejected.returncode, 5, rejected.stderr)
            self.assertEqual(json.loads(rejected.stdout)["error"]["code"], "RUNTIME_INCOMPATIBLE")

    def test_demo_guardrails_without_an_agent_account(self):
        result = dist_run("demo", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["simulation"])
        self.assertFalse(payload["real_agent_invoked"])
        self.assertFalse(payload["real_remote_write"])
        self.assertEqual(payload["scenario"]["success"]["final_state"], "REVIEW_READY")


if __name__ == "__main__":
    unittest.main()
