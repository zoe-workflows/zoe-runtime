"""v1.3 legacy group records (TGD §12.3, P1).

A ``record_version: 2`` group is an **ordered list**, not a plan. Upgrading it
silently would mean inventing ``depends_on`` edges nobody wrote, so v1.3 keeps
it readable and readable *only*:

| command | behaviour |
|---|---|
| ``status`` / ``dispatch`` | unchanged (`ordered_items` semantics) |
| ``start`` / ``report`` / ``accept`` / ``pause`` / ``resume`` / ``cancel`` ||
| ↳ | ``CAPABILITY_UNSUPPORTED`` + ``kind=legacy_group`` |
| ``state migrate`` | does not rewrite the record |

The last row is the important one: a migration that "helpfully" filled in the
graph would produce a group that advances on edges no human approved.
"""

from __future__ import annotations

import uuid

from test_v130_group_accept import AcceptCase

from src import _groups
from src._util import Error


class TestLegacyCreation(AcceptCase):
    def legacy(self):
        return _groups.create(
            self.store,
            {
                "schema_version": 2,
                "request_id": "req-" + uuid.uuid4().hex,
                "project_id": "demo",
                "title": "ordered only",
                "authorization_ref": "scope-1",
                "ordered_items": [
                    {"item_id": "a", "title": "A", "spec_ref": "specs/a.json"},
                    {"item_id": "b", "title": "B", "spec_ref": "specs/b.json"},
                ],
            },
        )

    def test_an_ordered_request_stays_an_ordered_record(self):
        created = self.legacy()
        self.assertEqual(created["record_version"], 2)
        group = self.store.group(created["group_id"])
        self.assertFalse(_groups.is_v3(group))
        self.assertIn("ordered_items", group)
        self.assertNotIn("definition", group)
        # No runner facts were invented for it.
        self.assertNotIn("runtime", group)

    def test_ordered_items_cannot_carry_a_dependency_graph(self):
        with self.assertRaises(Error) as ctx:
            _groups.create(
                self.store,
                {
                    "schema_version": 2,
                    "request_id": "req-" + uuid.uuid4().hex,
                    "project_id": "demo",
                    "title": "mixed",
                    "authorization_ref": "scope-1",
                    "ordered_items": [
                        {
                            "item_id": "a",
                            "title": "A",
                            "spec_ref": "specs/a.json",
                            "depends_on": ["b"],
                        }
                    ],
                },
            )
        self.assertEqual(ctx.exception.code, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(ctx.exception.details["kind"], "legacy_group")

    def test_a_shell_expression_is_not_a_material_reference(self):
        with self.assertRaises(Error) as ctx:
            _groups.create(
                self.store,
                {
                    "schema_version": 2,
                    "request_id": "req-" + uuid.uuid4().hex,
                    "project_id": "demo",
                    "title": "x",
                    "authorization_ref": "scope-1",
                    "ordered_items": [
                        {"item_id": "a", "title": "A", "spec_ref": "$(cat/etc/passwd)"}
                    ],
                },
            )
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")


class TestLegacyIsReadableButInert(AcceptCase):
    """`status` keeps working; every command that implies a runner does not."""

    def setUp(self):
        super().setUp()
        self.gid = _groups.create(
            self.store,
            {
                "schema_version": 2,
                "request_id": "req-" + uuid.uuid4().hex,
                "project_id": "demo",
                "title": "ordered only",
                "authorization_ref": "scope-1",
                "ordered_items": [
                    {"item_id": "a", "title": "A", "spec_ref": "specs/a.json"},
                ],
            },
        )["group_id"]

    def request(self):
        return {
            "schema_version": 2,
            "request_id": "req-" + uuid.uuid4().hex,
            "expected_version": 1,
            "authorization_ref": "scope-1",
            "reason": "why not",
        }

    def test_status_still_reads_a_legacy_record(self):
        status = _groups.status(self.store, self.gid)
        self.assertEqual(status["group_id"], self.gid)
        self.assertEqual(status["record_version"], 2)
        self.assertIn("note", status)

    def test_every_runner_command_is_refused_with_one_reason(self):
        from src import _group_runner, _integration

        calls = {
            "start": lambda: _group_runner.begin(
                self.store, self.gid, self.request(), spawn=lambda *a, **k: None
            ),
            "pause": lambda: _group_runner.pause(self.store, self.gid, self.request()),
            "resume": lambda: _group_runner.resume(
                self.store, self.gid, self.request(), spawn=lambda *a, **k: None
            ),
            "cancel": lambda: _group_runner.cancel(self.store, self.gid, self.request()),
            "accept": lambda: _integration.accept(
                self.store, self.gid, dict(self.request(), decision="accepted", actor="human")
            ),
        }
        for action, call in calls.items():
            with self.subTest(action=action):
                with self.assertRaises(Error) as ctx:
                    call()
                self.assertEqual(ctx.exception.code, "CAPABILITY_UNSUPPORTED")
                self.assertEqual(ctx.exception.details["kind"], "legacy_group")
                self.assertIn("plan request", str(ctx.exception))

    def test_the_report_command_is_refused_too(self):
        from src import _integration

        with self.assertRaises(Error) as ctx:
            _integration.report(self.store, self.gid)
        self.assertEqual(ctx.exception.code, "CAPABILITY_UNSUPPORTED")
        self.assertEqual(ctx.exception.details["kind"], "legacy_group")

    def test_a_legacy_record_is_never_rewritten(self):
        before = self.store.group(self.gid)
        _groups.status(self.store, self.gid)
        after = self.store.group(self.gid)
        self.assertEqual(before, after)
        self.assertNotIn("definition", after)
        self.assertNotIn("runtime", after)
