"""v1.3 group member branch (TGD §15: T28, plus §6.3/§6.4).

A member is not a standalone task that happens to live in a group. It has its
own contract:

* it stops at ``REVIEW_READY`` and never publishes or accepts itself (§6.3);
* once its work is inside ``integration_sha`` it is **frozen** — ``revise``,
  ``reassign`` and ``rebind`` all refuse with ``STATE_CONFLICT`` /
  ``kind=integrated_member`` (T28), because rewriting it would change the meaning
  of an integration that already happened;
* the correction path is a new follow-up group, not an in-place edit (§8.4).
"""

from __future__ import annotations

import uuid

from test_v130_group_accept import AcceptCase

from src import _groups, _pipeline
from src._util import Error


class MemberCase(AcceptCase):
    def change_request(self, task, **extra):
        request = {
            "schema_version": 2,
            "request_id": "req-" + uuid.uuid4().hex,
            "expected_version": task["row_version"],
            "authorization_ref": "scope-1",
            "reason": "tidy up",
        }
        request.update(extra)
        return request

    def frozen_member(self):
        gid, task, group = self.ready_group()
        runtime = _groups.member_runtime(self.group(gid), "a")
        assert runtime["integrated_sha"], "the member must be integrated first"
        return gid, self.store.task(task["task_id"]), group


class TestIntegratedMemberIsFrozen(MemberCase):
    """T28: three mutation doors, one answer, with the same reason."""

    def assert_frozen(self, ctx, action):
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")
        self.assertEqual(ctx.exception.details["kind"], "integrated_member")
        self.assertIn(action, str(ctx.exception))
        self.assertIn("follow-up group", ctx.exception.details["next_action"])

    def test_revise_is_refused(self):
        gid, task, _ = self.frozen_member()
        spec = _groups.group_dir(self.store, gid) / "specs" / "a.json"
        with self.assertRaises(Error) as ctx:
            _pipeline.revise(
                self.store, task["task_id"], self.change_request(task, spec_file=str(spec))
            )
        self.assert_frozen(ctx, "revise")

    def test_reassign_is_refused(self):
        gid, task, _ = self.frozen_member()
        with self.assertRaises(Error) as ctx:
            _pipeline.reassign(
                self.store, task["task_id"], self.change_request(task, target_profile="p-cb")
            )
        self.assert_frozen(ctx, "reassign")

    def test_rebind_is_refused(self):
        gid, task, _ = self.frozen_member()
        from src._util import digest

        with self.assertRaises(Error) as ctx:
            _pipeline.rebind(
                self.store,
                task["task_id"],
                self.change_request(task, target_policy_hash=digest(self.store.config("demo"))),
            )
        self.assert_frozen(ctx, "rebind")

    def test_the_refusal_names_the_group_and_the_sha(self):
        gid, task, group = self.frozen_member()
        with self.assertRaises(Error) as ctx:
            _pipeline.reassign(
                self.store, task["task_id"], self.change_request(task, target_profile="p-cb")
            )
        self.assertEqual(ctx.exception.details["group_id"], gid)
        self.assertEqual(ctx.exception.details["task_key"], "a")
        self.assertEqual(
            ctx.exception.details["integrated_sha"], self.group(gid)["runtime"]["integration_sha"]
        )

    def test_an_invalidated_member_is_mutable_again(self):
        """§6.4 第 2 行：失效是出口，不是死锁。"""
        gid, task, _ = self.frozen_member()
        group = self.group(gid)
        _groups.member_runtime(group, "a")["invalidated"] = True
        self.store.save_group(group, "test_marker")
        spec = _groups.group_dir(self.store, gid) / "specs" / "a.json"
        # It may still be refused for another reason (the SHA no longer matches),
        # but no longer because the member is frozen.
        try:
            _pipeline.revise(
                self.store, task["task_id"], self.change_request(task, spec_file=str(spec))
            )
        except Error as exc:
            self.assertNotEqual((exc.details or {}).get("kind"), "integrated_member")


class TestMemberCannotCloseItself(MemberCase):
    """§6.3: the group closes the member, not the member."""

    def test_task_accept_refuses_a_group_member(self):
        gid, task, group = self.frozen_member()
        with self.assertRaises(Error) as ctx:
            _pipeline.accept(
                self.store,
                task["task_id"],
                {
                    "schema_version": 2,
                    "request_id": "req-" + uuid.uuid4().hex,
                    "expected_version": task["row_version"],
                    "authorization_ref": "scope-1",
                },
            )
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")
        self.assertEqual(ctx.exception.details["kind"], "group_member")

    def test_a_member_task_does_not_publish_on_its_own(self):
        """The pipeline stops at REVIEW_READY: no PR, no merge, no self-accept."""
        gid, task, group = self.frozen_member()
        self.assertEqual(task["delivery_mode"], "group_member")
        self.assertEqual(task["state"], "REVIEW_READY")
        self.assertIsNone(task.get("publication"))
        self.assertNotEqual(task["state"], "DONE")
