"""v1.3 serial integration: crash reconciliation and the no-double-merge rules.

Cases from ``docs/task-group-delivery-design.md`` §15:

* **T10** a ``prepared`` ledger whose ref already moved completes ``done`` and
  never merges twice;
* **T17** a conflicting candidate stops at ``INTEGRATION_CONFLICT`` with the
  diagnostics kept and ``integration_sha`` untouched;
* **T27** a ``prepared`` ledger whose ref did **not** move replays the
  idempotent ``update-ref`` and then completes;
* §8.3 row 4 — an ``intent`` that never landed is replayed **once**;
* §8.3 row 6 — an ``intent`` whose candidate is already on the branch is adopted
  from the identifiable merge commit, or blocked when it cannot be identified;
* §8.2 retry probe — ``recover_result_sha`` only accepts a real merge commit, so
  a branch that merely *contains* the candidate is never adopted.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from src import _groups, _integration
from src._store import Store
from src._util import Error, atomic, git

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


class IntegrationCase(unittest.TestCase):
    """A real repo, a real store and a frozen two-member group."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.txt").write_text("base\n")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "design.md").write_text("# design\n")
        (self.repo / "specs").mkdir()
        for key in ("a", "b"):
            self.write_spec(key)
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "test")
        git(self.repo, "config", "user.email", "test@localhost")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.db.close)
        self.addCleanup(self._td.cleanup)
        atomic(self.store.home / "projects" / "demo.json", self.config())

    def config(self):
        return {
            "schema_version": 2,
            "project_id": "demo",
            "repo": str(self.repo),
            "backend": "local",
            "default_agent_profile": "p-cb",
            "agent_profiles": {
                "p-cb": {
                    "adapter": "codebuddy",
                    "model": "stub",
                    "backend": "codebuddy-cli",
                    "expected_version": "1.2.3",
                    "permission_profile": "isolated-edit",
                }
            },
            # `integration` is deliberately *not* quick: a quick-check run has
            # nothing to execute, so these tests exercise the ledger, not checks.
            "checks": [
                {"id": "unit", "argv": ["true"]},
                {"id": "integration", "argv": ["true"]},
            ],
            "allowed_paths": ["src", "specs", "docs"],
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute", "publish", "accept"],
            "max_attempts": 3,
            "max_active_tasks": 1,
        }

    def write_spec(self, key):
        document = {
            "schema_version": 2,
            "title": "Member " + key,
            "open_questions": [],
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "statement": "works",
                    "verification": {"type": "check", "check_id": "unit"},
                }
            ],
            "allowed_paths": ["src", "specs"],
        }
        (self.repo / "specs" / (key + ".json")).write_text(__import__("json").dumps(document))

    def plan(self):
        return {
            "schema_version": 2,
            "request_id": "req-" + uuid.uuid4().hex,
            "project_id": "demo",
            "title": "Two members",
            "design_files": ["docs/design.md"],
            "base_ref": "main",
            "delivery_mode": "local_artifact",
            "requirements": [
                {"id": "REQ-1", "statement": "a is delivered"},
                {"id": "REQ-2", "statement": "b is delivered"},
            ],
            "members": [
                {
                    "task_key": "a",
                    "title": "A",
                    "spec_file": "specs/a.json",
                    "depends_on": [],
                    "requirement_ids": ["REQ-1"],
                    "acceptance_ids": ["AC-1"],
                    "expected_paths": ["src/a.txt"],
                    "check_ids": ["unit"],
                },
                {
                    "task_key": "b",
                    "title": "B",
                    "spec_file": "specs/b.json",
                    "depends_on": [],
                    "requirement_ids": ["REQ-2"],
                    "acceptance_ids": ["AC-1"],
                    "expected_paths": ["src/b.txt"],
                    "check_ids": ["unit"],
                },
            ],
            "group_checks": ["integration"],
            "budget": {"max_starts": 4, "max_wall_seconds": None},
            "authorization_ref": "scope-1",
        }

    # -- git helpers ------------------------------------------------------- #

    def head(self) -> str:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def commit_file(self, name: str, body: str, parent: str | None = None) -> str:
        """A candidate commit on its own branch, without touching ``main``."""
        ref = "cand-" + uuid.uuid4().hex[:8]
        if parent:
            git(self.repo, "checkout", "-q", "-B", ref, parent)
        else:
            git(self.repo, "checkout", "-q", "-b", ref)
        path = self.repo / "src" / name
        path.write_text(body)
        git(self.repo, "add", "src/" + name)
        git(self.repo, "commit", "-q", "-m", "candidate " + name)
        sha = self.head()
        git(self.repo, "checkout", "-q", "main")
        return sha

    def merge_commit(self, base: str, candidate: str, message="merged", tree=None) -> str:
        """An explicit two-parent commit — what ``merge --no-ff`` would create.

        ``tree`` defaults to the candidate's tree: a real merge of a branch that
        only touched files the base did not change keeps those changes.
        """
        if tree is None:
            tree = self.tree_of(candidate)
        return subprocess.run(
            ["git", "commit-tree", tree, "-p", base, "-p", candidate, "-m", message],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            check=True,
            env=GIT_ENV,
        ).stdout.strip()

    def tree_of(self, sha: str) -> str:
        return subprocess.run(
            ["git", "rev-parse", sha + "^{tree}"],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def plain_commit(self, base: str, message="plain") -> str:
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return subprocess.run(
            ["git", "commit-tree", tree, "-p", base, "-m", message],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            check=True,
            env=GIT_ENV,
        ).stdout.strip()

    def created(self) -> dict:
        return _groups.create(self.store, self.plan())

    def ledger(self, gid: str, **overrides) -> dict:
        group = self.store.group(gid)
        value = {
            "op_id": "iop-" + uuid.uuid4().hex[:16],
            "group_id": gid,
            "member": "a",
            "seq": 1,
            "status": "intent",
            "base_sha": group["base_sha"],
            "candidate_sha": "b" * 40,
            "integration_branch": group["definition"]["integration_branch"],
            "created_at": 0.0,
            "result_sha": None,
            "merged_candidate_sha": "b" * 40,
            "worktree": None,
            "conflict_dir": None,
            "failures": [],
        }
        value.update(overrides)
        return value

    def opened(self, gid: str, ledger: dict) -> dict:
        group = self.store.group(gid)
        _integration._write_ledger(self.store, gid, ledger)
        group["runtime"]["open_operations"] = [ledger["op_id"]]
        self.store.save_group(group, "test_marker")
        return self.store.group(gid)

    def branch_sha(self, gid: str, branch: str) -> str | None:
        return _integration._ref_sha(self.repo, "refs/heads/" + branch)


class TestRecoverResultSha(IntegrationCase):
    """§8.2: only a real merge commit may be adopted as the integration result."""

    def test_a_merge_commit_is_recovered(self):
        gid = self.created()["group_id"]
        group = self.store.group(gid)
        candidate = self.commit_file("b.txt", "candidate\n")
        merge = self.merge_commit(group["base_sha"], candidate)
        self.assertEqual(_integration.recover_result_sha(self.repo, merge, candidate), merge)

    def test_a_plain_descendant_is_not_recovered(self):
        """`contains the candidate` is not `is the integration of the candidate`."""
        self.created()
        candidate = self.commit_file("b.txt", "candidate\n")
        plain = self.plain_commit(candidate)
        self.assertIsNone(_integration.recover_result_sha(self.repo, plain, candidate))

    def test_ancestry_is_decided_by_git_not_by_strings(self):
        gid = self.created()["group_id"]
        group = self.store.group(gid)
        candidate = self.commit_file("b.txt", "candidate\n")
        self.assertTrue(_integration.is_ancestor(self.repo, group["base_sha"], candidate))
        self.assertFalse(_integration.is_ancestor(self.repo, candidate, group["base_sha"]))
        self.assertFalse(_integration.is_ancestor(self.repo, None, candidate))


class TestPreparedReconciliation(IntegrationCase):
    def test_a_prepared_ledger_with_the_ref_unmoved_finishes_the_ref_update(self):
        """T27: the ref update is idempotent; nothing is merged a second time."""
        gid = self.created()["group_id"]
        group = self.store.group(gid)
        branch = group["definition"]["integration_branch"]
        _integration.ensure_branch(self.store, group)
        candidate = self.commit_file("b.txt", "candidate\n")
        result_sha = self.merge_commit(group["base_sha"], candidate)
        ledger = self.ledger(gid, status="prepared", result_sha=result_sha, member="a")
        self.opened(gid, ledger)
        self.assertEqual(self.branch_sha(gid, branch), group["base_sha"])

        reconciled = _integration.reconcile(self.store, self.store.group(gid))

        self.assertEqual(self.branch_sha(gid, branch), result_sha)
        self.assertEqual(reconciled["runtime"]["integration_sha"], result_sha)
        self.assertEqual(_groups.member_runtime(reconciled, "a")["integrated_sha"], result_sha)
        self.assertEqual(reconciled["runtime"]["open_operations"], [])

    def test_a_prepared_ledger_whose_ref_moved_is_a_bookkeeping_only_completion(self):
        """T10: ref already at ``result_sha`` ⇒ `done`, no merge, no new commit."""
        gid = self.created()["group_id"]
        group = self.store.group(gid)
        branch = group["definition"]["integration_branch"]
        _integration.ensure_branch(self.store, group)
        candidate = self.commit_file("b.txt", "candidate\n")
        result_sha = self.merge_commit(group["base_sha"], candidate)
        git(self.repo, "update-ref", "refs/heads/" + branch, result_sha)
        ledger = self.ledger(gid, status="prepared", result_sha=result_sha)
        self.opened(gid, ledger)

        reconciled = _integration.reconcile(self.store, self.store.group(gid))

        self.assertEqual(self.branch_sha(gid, branch), result_sha)
        self.assertEqual(reconciled["runtime"]["integration_sha"], result_sha)
        self.assertEqual(
            _integration.read(_integration._ledger_path(self.store, gid, ledger["op_id"]))[
                "status"
            ],
            "done",
        )

    def test_a_prepared_ledger_at_an_unexpected_ref_stops(self):
        gid = self.created()["group_id"]
        group = self.store.group(gid)
        branch = group["definition"]["integration_branch"]
        _integration.ensure_branch(self.store, group)
        other = self.plain_commit(group["base_sha"])
        git(self.repo, "update-ref", "refs/heads/" + branch, other)
        self.opened(gid, self.ledger(gid, status="prepared", result_sha=self.plain_commit(other)))
        with self.assertRaises(Error) as ctx:
            _integration.reconcile(self.store, self.store.group(gid))
        self.assertEqual(ctx.exception.code, "INTEGRATION_UNKNOWN")
        self.assertEqual(ctx.exception.details["kind"], "ref_unexpected")


class TestIntentReconciliation(IntegrationCase):
    def test_an_intent_that_never_landed_is_replayed_once(self):
        """§8.3 row 4: nothing happened ⇒ replay, same ``op_id``, new worktree."""
        gid = self.created()["group_id"]
        group = self.store.group(gid)
        branch = group["definition"]["integration_branch"]
        _integration.ensure_branch(self.store, group)
        candidate = self.commit_file("b.txt", "candidate\n")
        ledger = self.ledger(gid, candidate_sha=candidate, merged_candidate_sha=candidate)
        self.opened(gid, ledger)

        reconciled = _integration.reconcile(self.store, self.store.group(gid))

        result_sha = reconciled["runtime"]["integration_sha"]
        self.assertIsNotNone(result_sha)
        self.assertEqual(self.branch_sha(gid, branch), result_sha)
        stored = _integration.read(_integration._ledger_path(self.store, gid, ledger["op_id"]))
        self.assertEqual(stored["status"], "done")
        self.assertEqual(stored["replay_count"], 1)
        self.assertEqual(stored["op_id"], ledger["op_id"])

    def test_a_second_replay_is_refused(self):
        """§8.2: the replay allowance is one, not a loop."""
        gid = self.created()["group_id"]
        group = self.store.group(gid)
        _integration.ensure_branch(self.store, group)
        candidate = self.commit_file("b.txt", "candidate\n")
        ledger = self.ledger(
            gid, candidate_sha=candidate, merged_candidate_sha=candidate, replay_count=1
        )
        self.opened(gid, ledger)
        with self.assertRaises(Error) as ctx:
            _integration.reconcile(self.store, self.store.group(gid))
        self.assertEqual(ctx.exception.code, "INTEGRATION_UNKNOWN")
        self.assertEqual(ctx.exception.details["kind"], "replay_exhausted")

    def test_an_intent_whose_candidate_is_already_merged_is_adopted(self):
        """§8.3 row 6: adopt the merge commit instead of merging again."""
        gid = self.created()["group_id"]
        group = self.store.group(gid)
        branch = group["definition"]["integration_branch"]
        _integration.ensure_branch(self.store, group)
        candidate = self.commit_file("b.txt", "candidate\n")
        merge = self.merge_commit(group["base_sha"], candidate)
        git(self.repo, "update-ref", "refs/heads/" + branch, merge)
        ledger = self.ledger(gid, candidate_sha=candidate, merged_candidate_sha=candidate)
        self.opened(gid, ledger)

        reconciled = _integration.reconcile(self.store, self.store.group(gid))

        self.assertEqual(reconciled["runtime"]["integration_sha"], merge)
        self.assertEqual(self.branch_sha(gid, branch), merge)
        self.assertEqual(_groups.member_runtime(reconciled, "a")["integrated_sha"], merge)
        stored = _integration.read(_integration._ledger_path(self.store, gid, ledger["op_id"]))
        self.assertEqual(stored["status"], "done")
        self.assertEqual(stored["recovered_from"], merge)

    def test_an_intent_with_an_unidentifiable_result_stops(self):
        """A branch that merely contains the candidate is not an integration."""
        gid = self.created()["group_id"]
        group = self.store.group(gid)
        branch = group["definition"]["integration_branch"]
        _integration.ensure_branch(self.store, group)
        candidate = self.commit_file("b.txt", "candidate\n")
        plain = self.plain_commit(candidate)
        git(self.repo, "update-ref", "refs/heads/" + branch, plain)
        self.opened(gid, self.ledger(gid, candidate_sha=candidate, merged_candidate_sha=candidate))
        with self.assertRaises(Error) as ctx:
            _integration.reconcile(self.store, self.store.group(gid))
        self.assertEqual(ctx.exception.code, "INTEGRATION_UNKNOWN")
        self.assertEqual(ctx.exception.details["kind"], "intent_merged_unrecoverable")

    def test_a_branch_that_moved_without_the_candidate_stops(self):
        gid = self.created()["group_id"]
        group = self.store.group(gid)
        branch = group["definition"]["integration_branch"]
        _integration.ensure_branch(self.store, group)
        git(self.repo, "update-ref", "refs/heads/" + branch, self.plain_commit(group["base_sha"]))
        self.opened(gid, self.ledger(gid))
        with self.assertRaises(Error) as ctx:
            _integration.reconcile(self.store, self.store.group(gid))
        self.assertEqual(ctx.exception.code, "INTEGRATION_UNKNOWN")
        self.assertEqual(ctx.exception.details["kind"], "intent_ref_moved")


class TestChecksGoStale(IntegrationCase):
    def test_a_new_integration_sha_makes_the_recorded_checks_stale(self):
        """T16: results bound to the previous SHA cannot speak for the new one."""
        gid = self.created()["group_id"]
        group = self.store.group(gid)
        group["state"] = "REVIEW_READY"
        group["runtime"]["integration_sha"] = "a" * 40
        group["runtime"]["checks"] = [
            {"check_id": "integration", "subject_sha": "b" * 40, "status": "PASS"}
        ]
        self.store.save_group(group, "test_marker")
        group = self.store.group(gid)

        self.assertEqual(_groups.stale_check_ids(group), ["integration"])
        state, blockers = _groups.derive_group_state(group, _groups.members_view(self.store, group))
        self.assertEqual(state, "RUNNING")
        self.assertIn("STALE_CHECKS", [item["code"] for item in blockers])

    def test_a_check_for_the_current_sha_is_not_stale(self):
        gid = self.created()["group_id"]
        group = self.store.group(gid)
        group["runtime"]["integration_sha"] = "a" * 40
        group["runtime"]["checks"] = [
            {"check_id": "integration", "subject_sha": "a" * 40, "status": "PASS"}
        ]
        self.store.save_group(group, "test_marker")
        self.assertEqual(_groups.stale_check_ids(self.store.group(gid)), [])


class TestMergeConflict(IntegrationCase):
    def test_a_conflicting_candidate_keeps_diagnostics_and_the_old_sha(self):
        """T17: conflict is isolated; ``integration_sha`` never moves on failure."""
        gid = self.created()["group_id"]
        group = self.store.group(gid)
        _integration.ensure_branch(self.store, group)
        # Two candidates that both rewrite src/a.txt: the second must conflict.
        first = self.commit_file("a.txt", "first\n")
        second = self.commit_file("a.txt", "second\n", parent=group["base_sha"])
        # The integration branch really contains `first`, so merging `second`
        # (base→second) onto it conflicts on src/a.txt.
        merge = self.merge_commit(group["base_sha"], first, tree=self.tree_of(first))
        branch = group["definition"]["integration_branch"]
        git(self.repo, "update-ref", "refs/heads/" + branch, merge)
        group = self.store.group(gid)
        group["runtime"]["integration_sha"] = merge
        self.store.save_group(group, "test_marker")

        ledger = self.ledger(
            gid,
            member="b",
            base_sha=merge,
            candidate_sha=second,
            merged_candidate_sha=second,
        )
        with self.assertRaises(Error) as ctx:
            _integration._merge_and_record(
                self.store, self.store.group(gid), ledger, merge, self.repo, attempt=0
            )
        self.assertEqual(ctx.exception.code, "INTEGRATION_CONFLICT")
        conflict_dir = Path(ctx.exception.details["conflict_dir"])
        self.assertTrue(conflict_dir.is_dir())
        self.assertTrue((conflict_dir / "merge-output.txt").is_file())
        after = self.store.group(gid)
        self.assertEqual(after["runtime"]["integration_sha"], merge)
        self.assertEqual(after["runtime"]["blockers"][0]["code"], "INTEGRATION_CONFLICT")
