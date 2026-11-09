"""Git/PR publication with reconciliation (controller-owned writes)."""

from __future__ import annotations

import json
import re

from src._config import is_authorized
from src._profiles import policy_digest
from src._util import atomic, command, digest, git, require


class Publisher:
    def __init__(self, c):
        self.c = c

    def gh(self, *args):
        return command(["gh", *args], cwd=self.c["repo"])

    def refresh(self, t):
        if not t.get("pr"):
            return {"ready": False}
        data = json.loads(
            self.gh(
                "pr",
                "view",
                str(t["pr"]["number"]),
                "--repo",
                self.c["github_repo"],
                "--json",
                "number,url,headRefOid,state,mergeCommit,statusCheckRollup,reviewDecision",
            )
        )
        checks = data.get("statusCheckRollup") or []
        success = {
            x.get("name", x.get("context"))
            for x in checks
            if x.get("conclusion") == "SUCCESS" or x.get("state") == "SUCCESS"
        }
        required = set(self.c.get("required_ci_checks", []))
        ci = required.issubset(success) and (bool(required) or not self.c.get("ci_required", True))
        return {
            "pr": data,
            "ready": data["headRefOid"] == t["candidate_sha"]
            and ci
            and data.get("reviewDecision") != "CHANGES_REQUESTED",
        }

    def publish(self, t, store):
        c = self.c
        require(
            policy_digest(c) == digest(store.config(t["project_id"])),
            "POLICY_CHANGED",
            "Policy changed before publishing",
        )
        require(
            is_authorized(c, "publish"),
            "AUTH_REQUIRED",
            "Publishing not authorized",
        )
        require(
            re.fullmatch(r"[\w.-]+/[\w.-]+", c.get("github_repo", "")),
            "INPUT_INCOMPLETE",
            "github_repo owner/name required",
        )
        remote = c.get("remote", "origin")
        require(not remote.startswith("-"), "INPUT_INCOMPLETE", "Invalid remote")
        w = t["workspace"]
        branch = t["branch"]
        sha = t["candidate_sha"]
        effect = store.path(t) / "publication.json"
        atomic(effect, {"status": "intent", "sha": sha, "branch": branch})
        # A mismatch is not overwritten: user explicitly reconciles branch drift.
        old = git(w, "ls-remote", remote, "refs/heads/" + branch).split()
        if old and old[0] != sha:
            require(
                t.get("published_sha") == old[0], "REMOTE_UNKNOWN", "Remote task branch changed"
            )
        if not old or old[0] != sha:
            git(w, "push", remote, sha + ":refs/heads/" + branch)
        atomic(effect, {"status": "pushed", "sha": sha, "branch": branch})
        candidates = json.loads(
            self.gh(
                "pr",
                "list",
                "--repo",
                c["github_repo"],
                "--head",
                branch,
                "--base",
                c.get("target_branch", "main"),
                "--state",
                "all",
                "--json",
                "number,url,body,state",
            )
        )
        marker = "<!-- zoe-task: " + t["task_id"] + " -->"
        require(len(candidates) <= 1, "REMOTE_UNKNOWN", "Multiple PR candidates")
        if candidates:
            require(
                marker in candidates[0].get("body", ""),
                "REMOTE_UNKNOWN",
                "PR ownership marker absent",
            )
            require(
                candidates[0]["state"] == "OPEN", "REMOTE_UNKNOWN", "Task PR is closed or merged"
            )
            pr = candidates[0]
        else:
            body = store.path(t) / "pr-body.md"
            body.write_text(
                marker + "\n\nAutomated candidate; human review required.\n\n"
                "Validation evidence retained locally.\n"
            )
            self.gh(
                "pr",
                "create",
                "--repo",
                c["github_repo"],
                "--head",
                branch,
                "--base",
                c.get("target_branch", "main"),
                "--title",
                "Zoe " + t["task_id"],
                "--body-file",
                str(body),
                "--draft",
            )
            candidates = json.loads(
                self.gh(
                    "pr",
                    "list",
                    "--repo",
                    c["github_repo"],
                    "--head",
                    branch,
                    "--state",
                    "open",
                    "--json",
                    "number,url,body",
                )
            )
            require(
                len(candidates) == 1 and marker in candidates[0].get("body", ""),
                "REMOTE_UNKNOWN",
                "Cannot identify created PR",
            )
            pr = candidates[0]
        atomic(effect, {"status": "confirmed", "sha": sha, "pr": pr})
        return pr
