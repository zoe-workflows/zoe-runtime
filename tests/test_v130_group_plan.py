"""v1.3 group plan contract: refusal before write, immutable digest, replays.

Cases from ``docs/task-group-delivery-design.md`` §15:

* **T02** a cycle (and an uncovered requirement) is refused *before* anything is
  created — zero tasks, zero group records, so zero agents can start;
* **T06** the same ``request_id`` replays the first answer instead of creating a
  second group;
* **T29** ``delivery_mode=pull_request`` is refused at creation with
  ``group_pr_unsupported`` — v1.3 has no PR close-out path;
* **T31** runtime facts never change ``plan_digest`` or ``revision``;
* §4.5 the per-member route rehearsal is written to ``routes/<key>-a001.json``
  and mirrored into ``runtime.policy_snapshot.routes``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from src import _groups
from src._store import Store
from src._util import Error, atomic, git


class PlanCase(unittest.TestCase):
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

    def config(self, **overrides):
        value = {
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
        value.update(overrides)
        return value

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
        (self.repo / "specs" / (key + ".json")).write_text(json.dumps(document))

    def member(self, key, depends_on=None):
        return {
            "task_key": key,
            "title": key.upper(),
            "spec_file": f"specs/{key}.json",
            "depends_on": list(depends_on or []),
            "requirement_ids": ["REQ-" + key.upper()],
            "acceptance_ids": ["AC-1"],
            "expected_paths": [f"src/{key}.txt"],
            "check_ids": ["unit"],
        }

    def plan(self, **overrides):
        request = {
            "schema_version": 2,
            "request_id": "req-" + uuid.uuid4().hex,
            "project_id": "demo",
            "title": "Two members",
            "design_files": ["docs/design.md"],
            "base_ref": "main",
            "delivery_mode": "local_artifact",
            "requirements": [
                {"id": "REQ-A", "statement": "a is delivered"},
                {"id": "REQ-B", "statement": "b is delivered"},
            ],
            "members": [self.member("a"), self.member("b")],
            "group_checks": ["integration"],
            "budget": {"max_starts": 4, "max_wall_seconds": None},
            "authorization_ref": "scope-1",
        }
        request.update(overrides)
        return request

    def counts(self):
        groups = self.store.db.execute("SELECT COUNT(*) AS n FROM groups").fetchone()["n"]
        tasks = self.store.db.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        return groups, tasks


class TestRefusalBeforeWrite(PlanCase):
    def test_a_cycle_is_refused_with_zero_side_effects(self):
        """T02: no group record, no task ⇒ no agent can ever be started."""
        failing = self.plan(
            members=[self.member("a", ["b"]), self.member("b", ["a"])],
        )
        with self.assertRaises(Error) as ctx:
            _groups.create(self.store, failing)
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertEqual(ctx.exception.details["kind"], "cycle")
        self.assertEqual(self.counts(), (0, 0))
        self.assertFalse((self.store.home / "groups").exists())

    def test_an_uncovered_requirement_is_refused_with_zero_side_effects(self):
        failing = self.plan(
            requirements=[
                {"id": "REQ-A", "statement": "a"},
                {"id": "REQ-B", "statement": "b"},
                {"id": "REQ-ORPHAN", "statement": "nobody owns this"},
            ]
        )
        with self.assertRaises(Error) as ctx:
            _groups.create(self.store, failing)
        self.assertEqual(ctx.exception.details["kind"], "coverage_missing")
        self.assertEqual(self.counts(), (0, 0))

    def test_a_pull_request_group_is_refused_at_creation(self):
        """T29: v1.3 only closes out `local_artifact`; there is no degraded path."""
        with self.assertRaises(Error) as ctx:
            _groups.create(self.store, self.plan(delivery_mode="pull_request"))
        self.assertEqual(ctx.exception.code, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(ctx.exception.details["kind"], "group_pr_unsupported")
        self.assertEqual(self.counts(), (0, 0))


class TestReplay(PlanCase):
    def test_the_same_request_id_replays_instead_of_creating_twice(self):
        """T06: a retried `create` is the same git, not a second group."""
        request = self.plan()
        first = _groups.create(self.store, request)
        second = _groups.create(self.store, request)
        self.assertEqual(first["group_id"], second["group_id"])
        self.assertEqual(self.counts()[0], 1)

    def test_a_request_id_shared_across_actions_is_refused_not_crossed(self):
        """T06: one id answering two actions is the failure mode to prevent.

        The v1.3 implementation is stricter than "each action gets its own slot":
        a `request_id` is bound to the first action that used it, so reusing it
        for another action is `REQUEST_CONFLICT` instead of a silent replay of
        the wrong answer. Either way the caller never gets `create`'s body back
        from `pause`.
        """
        from src import _group_runner

        shared = "req-" + uuid.uuid4().hex
        created = _groups.create(self.store, dict(self.plan(), request_id=shared))
        gid = created["group_id"]
        group = self.store.group(gid)
        group["state"] = "READY"
        self.store.save_group(group, "test_marker")

        with self.assertRaises(Error) as ctx:
            _group_runner.pause(
                self.store,
                gid,
                {
                    "schema_version": 2,
                    "request_id": shared,
                    "expected_version": self.store.group(gid)["row_version"],
                    "authorization_ref": "scope-1",
                },
            )
        self.assertEqual(ctx.exception.code, "REQUEST_CONFLICT")
        # And the rejected call changed nothing.
        self.assertEqual(self.store.group(gid)["state"], "READY")

    def test_a_second_pause_is_not_a_replay_of_the_first(self):
        """T06: the second call answers for itself (here: it is refused)."""
        from src import _group_runner

        gid = _groups.create(self.store, self.plan())["group_id"]
        first = _group_runner.pause(
            self.store,
            gid,
            {
                "schema_version": 2,
                "request_id": "req-" + uuid.uuid4().hex,
                "expected_version": self.store.group(gid)["row_version"],
                "authorization_ref": "scope-1",
            },
        )
        self.assertEqual(first["state"], "PAUSED")
        with self.assertRaises(Error) as ctx:
            _group_runner.pause(
                self.store,
                gid,
                {
                    "schema_version": 2,
                    "request_id": "req-" + uuid.uuid4().hex,
                    "expected_version": self.store.group(gid)["row_version"],
                    "authorization_ref": "scope-1",
                },
            )
        # A replay would have returned `first` again; a real second call refuses.
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")

    def test_a_different_payload_on_the_same_request_id_is_a_conflict(self):
        request = self.plan()
        _groups.create(self.store, request)
        with self.assertRaises(Error) as ctx:
            _groups.create(self.store, dict(request, title="a different plan"))
        self.assertEqual(ctx.exception.code, "REQUEST_CONFLICT")


class TestCrashBetweenArtifactsAndRecord(PlanCase):
    """T09: files first, then the row — and a crash in between is not a group."""

    def test_a_failed_record_write_leaves_no_ghost_group(self):
        real = self.store.save_group

        def boom(group, reason):
            if reason == "group_created":
                raise Error("ENVIRONMENT_ERROR", "disk full")
            return real(group, reason)

        with mock.patch.object(self.store, "save_group", side_effect=boom):
            with self.assertRaises(Error):
                _groups.create(self.store, self.plan())

        # Nothing claims to be a group: the failure is not a half-created one.
        self.assertEqual(self.counts(), (0, 0))
        self.assertFalse((self.store.home / "groups" / "group-fake").exists())

        # The artefacts are still on disk and readable, which is what "reconcile
        # from the file" needs (§4.6). They are *not* deleted: deleting them would
        # destroy the only evidence of what was attempted.
        directories = [path for path in (self.store.home / "groups").iterdir() if path.is_dir()]
        self.assertEqual(len(directories), 1)
        frozen = json.loads((directories[0] / "plan-r001.json").read_text())
        self.assertIn("plan_digest", frozen)
        self.assertIn("definition", frozen)


class TestTwoProjectsSameTaskKey(PlanCase):
    """T21: same `task_key` in two projects must never share a path or a record."""

    def second_project(self):
        config = self.config()
        config["project_id"] = "other"
        atomic(self.store.home / "projects" / "other.json", config)
        return "other"

    def plan_for(self, project_id):
        return dict(self.plan(), project_id=project_id)

    def test_the_same_task_key_in_two_projects_stays_separate(self):
        pid = self.second_project()
        first = _groups.create(self.store, self.plan())
        second = _groups.create(self.store, self.plan_for(pid))
        self.assertNotEqual(first["group_id"], second["group_id"])

        one = _groups.group_dir(self.store, first["group_id"])
        two = _groups.group_dir(self.store, second["group_id"])
        self.assertNotEqual(one, two)
        for directory in (one, two):
            self.assertTrue((directory / "specs" / "a.json").is_file())

        # Each record names its own project, and neither sees the other's members.
        self.assertEqual(self.store.group(first["group_id"])["project_id"], "demo")
        self.assertEqual(self.store.group(second["group_id"])["project_id"], pid)
        self.assertEqual(self.counts()[0], 2)

    def test_a_member_task_binds_to_its_own_group_only(self):
        pid = self.second_project()
        first = _groups.create(self.store, self.plan())
        second = _groups.create(self.store, self.plan_for(pid))
        runtime_one = _groups.member_runtime(self.store.group(first["group_id"]), "a")
        runtime_two = _groups.member_runtime(self.store.group(second["group_id"]), "a")
        self.assertIsNone(runtime_one["task_id"])
        self.assertIsNone(runtime_two["task_id"])
        # The frozen spec snapshots live under each group's own directory.
        spec_one = _groups.group_dir(self.store, first["group_id"]) / "specs" / "a.json"
        spec_two = _groups.group_dir(self.store, second["group_id"]) / "specs" / "a.json"
        self.assertNotEqual(spec_one, spec_two)


class TestDigestIsAPlanProjection(PlanCase):
    def test_runtime_facts_do_not_move_the_digest_or_the_revision(self):
        """T31: only a changed *plan* is a new revision."""
        created = _groups.create(self.store, self.plan())
        gid = created["group_id"]
        before = self.store.group(gid)
        digest_before, revision_before = before["plan_digest"], before["revision"]

        group = self.store.group(gid)
        group["runtime"]["budget"]["starts_reserved"] = 3
        group["runtime"]["members"][0]["validated_sha"] = "a" * 40
        group["runtime"]["members"][0]["integrated_sha"] = "a" * 40
        group["runtime"]["integration_sha"] = "a" * 40
        group["runtime"]["lease"] = {"lease_id": "l-1", "state": "spawn_observed", "pid": 1}
        group["runtime"]["checks"] = [
            {"check_id": "integration", "subject_sha": "a" * 40, "status": "PASS"}
        ]
        group["op_seq"] = 7
        self.store.save_group(group, "test_runtime_facts")

        after = self.store.group(gid)
        self.assertEqual(after["plan_digest"], digest_before)
        self.assertEqual(after["revision"], revision_before)
        # Re-validating the same request against the same repo reproduces the
        # digest exactly: it is a projection of the plan, not of the record.
        recomputed = _groups.validate_plan(_groups.resolve_plan_inputs(self.store, self.plan()))
        self.assertEqual(recomputed["plan_digest"], digest_before)

    def test_the_frozen_plan_file_matches_the_record(self):
        created = _groups.create(self.store, self.plan())
        gid = created["group_id"]
        frozen = json.loads((_groups.group_dir(self.store, gid) / "plan-r001.json").read_text())
        self.assertEqual(frozen["plan_digest"], created["plan_digest"])
        self.assertEqual(frozen["definition"], self.store.group(gid)["definition"])


class TestRouteRehearsal(PlanCase):
    def test_each_member_gets_a_route_preview_file(self):
        """§4.5: a rehearsal artefact per member, mirrored into the runtime."""
        atomic(
            self.store.home / "projects" / "demo.json",
            self.config(routing={"mode": "explicit", "allowed_profiles": ["p-cb"]}),
        )
        created = _groups.create(self.store, self.plan())
        gid = created["group_id"]
        directory = _groups.group_dir(self.store, gid) / "routes"

        for key in ("a", "b"):
            path = directory / f"{key}-a001.json"
            self.assertTrue(path.is_file(), f"missing route preview for {key}")
            body = json.loads(path.read_text())
            self.assertEqual(body["task_key"], key)
            self.assertIn("decision", body)
            # It must say out loud that it is not an admission.
            self.assertIn("not an admission", body["note"])

        runtime = self.store.group(gid)["runtime"]
        self.assertIn("routes", runtime["policy_snapshot"])

    def test_no_route_files_without_a_routing_block(self):
        created = _groups.create(self.store, self.plan())
        directory = _groups.group_dir(self.store, created["group_id"]) / "routes"
        self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
