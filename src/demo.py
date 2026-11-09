"""Isolated, simulated demonstration of the Zoe Runtime pipeline.

``zoe demo`` shows the success and rejection paths of the controller without
touching a real agent CLI or a remote. It reuses the same test doubles used
by the test suite, so nothing here is an execution backend — the demonstration
is educational only and never counts as real verification.

It runs in a fresh temporary directory, ignoring the user's real ZOE_HOME, and
never reads real project configuration or publishing credentials.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from src import core as rt


class _DemoDocker:
    """In-process fixture; not an execution backend."""

    def __init__(self, fail=False, mutate=False):
        self.calls = []
        self.fail = fail
        self.mutate = mutate

    def inspect(self, name):
        return None

    def execute(self, store, t, r, argv, log_name, worker=False, prompt=None):
        self.calls.append(log_name)
        if worker:
            (Path(t["workspace"]) / "src.txt").write_text("implemented\n")
        if self.mutate and not worker:
            (Path(t["workspace"]) / "src.txt").write_text("changed by check\n")
        return 1 if self.fail and not worker else 0


class _DemoPublisher:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def publish(self, t, s):
        self.calls += 1
        if self.fail:
            raise rt.Error("REMOTE_UNKNOWN", "lost response")
        return {"number": 1, "url": "https://example.invalid/pr/1"}

    def refresh(self, t):
        return {"ready": True, "pr": {"number": 1, "url": "https://example.invalid/pr/1"}}


def _make_project(root: Path):
    repo = root / "repo"
    repo.mkdir()
    rt.git(repo, "init", "-b", "main")
    rt.git(repo, "config", "user.name", "demo")
    rt.git(repo, "config", "user.email", "demo@localhost")
    (repo / "src.txt").write_text("initial\n")
    rt.git(repo, "add", ".")
    rt.git(repo, "commit", "-m", "initial")
    spec = root / "spec.md"
    spec.write_text("# Demo\nAC-001 implement")
    config = {
        "schema_version": 1,
        "project_id": "demo",
        "repo": str(repo),
        # v0.5.0: local-process is the only execution backend.
        "backend": "local-process",
        "network": "none",
        "compatibility_verified": True,
        "agent": "codebuddy",
        "checks": [{"id": "unit", "argv": ["true"]}],
        "allowed_paths": ["src.txt"],
        "authorization_ref": "demo-scope",
        "authorized_actions": ["create", "execute", "publish", "accept"],
        "ci_required": False,
        "max_attempts": 3,
    }
    return repo, spec, config


def simulate() -> dict:
    """Run the simulated pipeline and return a machine-readable trace.

    The trace explicitly records that this is a simulation: no real agent was
    invoked, no remote was written, and no supervisor launch was verified.
    """
    with tempfile.TemporaryDirectory(prefix="zoe-demo-") as td:
        root = Path(td)
        repo, spec, config = _make_project(root)
        store = rt.Store(root / "state")
        try:
            rt.atomic(store.home / "projects" / "demo.json", config)

            # Success path: create -> simulated execution -> verify -> simulated acceptance.
            req = dict(
                schema_version=1,
                request_id="demo-create-001",
                project_id="demo",
                title="Demo task",
                spec_file=str(spec),
                delivery_mode="local_artifact",
                authorization_ref="demo-scope",
            )
            created = rt.create(store, req)
            t = store.task(created["task_id"])

            # Simulate a run the same way the tests do (inject doubles, not a
            # real backend).
            rid = "run-demo-success"
            store.run({"run_id": rid, "task_id": t["task_id"], "status": "running", "container": None})
            t.update(current_run_id=rid, state="RUNNING")
            store.save(t)
            success = rt.run_pipeline(store, t["task_id"], _DemoDocker(), _DemoPublisher())

            success_steps = [
                "create",
                "simulated execution",
                "verify (fingerprint before/after)",
                "simulated acceptance",
            ]

            # Rejection path: agent claims done but independent check fails.
            req2 = dict(req, request_id="demo-create-002", title="Demo rejection")
            created2 = rt.create(store, req2)
            t2 = store.task(created2["task_id"])
            rid2 = "run-demo-reject"
            store.run({"run_id": rid2, "task_id": t2["task_id"], "status": "running", "container": None})
            t2.update(current_run_id=rid2, state="RUNNING")
            store.save(t2)
            rejected = rt.run_pipeline(store, t2["task_id"], _DemoDocker(fail=True), _DemoPublisher())

            trace = {
                "simulation": True,
                "real_agent_invoked": False,
                "real_remote_write": False,
                "supervisor_launch_verified": False,
                "scenario": {
                    "success": {
                        "steps": success_steps,
                        "final_state": success["state"],
                        "candidate_sha": success.get("candidate_sha"),
                    },
                    "rejection": {
                        "steps": [
                            "create",
                            "simulated execution (agent reports done)",
                            "independent check fails",
                        ],
                        "final_state": rejected["state"],
                        "block_code": rejected.get("block_code"),
                    },
                },
                "note": (
                    "This demonstration uses test doubles; it does not represent "
                    "real CodeBuddy execution. See docs/capabilities.md."
                ),
            }
            return trace
        finally:
            store.db.close()


if __name__ == "__main__":
    print(json.dumps(simulate(), ensure_ascii=False, indent=2))
