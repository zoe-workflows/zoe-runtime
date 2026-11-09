"""v6 §B4: preview the blast radius of a configuration change before rebinding.

The dangerous failure mode is a preview that disagrees with the real call: it
would let someone conclude "nothing breaks" and then lose a run to
``POLICY_CHANGED``. So the interesting assertions here are about *agreement*
with ``rebind``'s own validation, not just about the shape of the report.
"""

from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from test_config_friction_v5 import Fixture, _spec

from src import _diff, _pipeline
from src._util import Error
from src.core import Store


class DiffFixture(Fixture):
    def create_task(self, project_id="demo", title="task", spec=None):
        with Store(str(self.home)) as store:
            return _pipeline.create(
                store,
                {
                    "schema_version": 2,
                    "request_id": "req-" + uuid.uuid4().hex,
                    "project_id": project_id,
                    "title": title,
                    "spec_file": spec or self.write_spec(_spec()),
                    "delivery_mode": "local_artifact",
                    "authorization_ref": "auth-demo",
                },
            )["task_id"]

    def candidate(self, **overrides) -> Path:
        document = dict(self.profile, **overrides)
        path = Path(self._tmp.name) / f"candidate-{uuid.uuid4().hex[:6]}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def diff(self, path, project_id=None):
        with Store(str(self.home)) as store:
            return _diff.project_diff(store, path, project_id)


class ChangedFields(DiffFixture):
    def test_identical_config_reports_no_change(self):
        self.register()
        self.create_task()
        report = self.diff(self.candidate())
        self.assertFalse(report["policy_changed"])
        self.assertEqual(report["changed_fields"], [])
        self.assertEqual(report["affected_tasks"][0]["rebind_required"], False)

    def test_scalar_change_is_named(self):
        self.register()
        self.create_task()
        report = self.diff(self.candidate(max_active_tasks=2))
        self.assertTrue(report["policy_changed"])
        self.assertEqual(report["changed_fields"], ["max_active_tasks"])

    def test_check_argv_change_is_named_per_field(self):
        self.register()
        self.create_task()
        report = self.diff(self.candidate(checks=[{"id": "unit", "argv": ["true", "-x"]}]))
        self.assertEqual(report["changed_fields"], ["checks[unit].argv"])

    def test_profile_field_change_is_named(self):
        self.register()
        profile = dict(self.profile["agent_profiles"]["cb"], model="other-model")
        report = self.diff(self.candidate(agent_profiles={"cb": profile}))
        self.assertEqual(report["changed_fields"], ["agent_profiles[cb].model"])

    def test_unregistered_project_reports_every_registration_field(self):
        self.home.mkdir(parents=True, exist_ok=True)
        report = self.diff(self.candidate())
        self.assertFalse(report["registered"])
        self.assertIsNone(report["current_policy_hash"])
        self.assertIn("project_id", report["changed_fields"])
        self.assertEqual(report["affected_tasks"], [])


class AffectedTasks(DiffFixture):
    def test_only_tasks_of_this_project_are_listed(self):
        self.register()
        mine = self.create_task()
        report = self.diff(self.candidate(max_active_tasks=2))
        self.assertEqual([row["task_id"] for row in report["affected_tasks"]], [mine])

    def test_rebind_required_follows_the_recorded_revision(self):
        self.register()
        first = self.create_task(title="first")
        report = self.diff(self.candidate(max_active_tasks=2))
        row = report["affected_tasks"][0]
        self.assertTrue(row["rebind_required"])
        self.assertEqual(row["policy_revision"][:16], report["current_policy_revision"])
        self.assertIn(first, report["suggested_commands"][0])

    def test_removing_a_referenced_check_invalidates_the_frozen_spec(self):
        self.register()
        self.create_task()
        report = self.diff(self.candidate(checks=[{"id": "lint", "argv": ["true"]}]))
        row = report["affected_tasks"][0]
        self.assertFalse(row["spec_valid_under_new_policy"])
        self.assertIn("unit", row["note"])

    def test_running_task_is_listed_but_not_rebound(self):
        self.register()
        task_id = self.create_task()
        with Store(str(self.home)) as store:
            task = store.task(task_id)
            task["state"] = "RUNNING"
            store.save(task)
        report = self.diff(self.candidate(max_active_tasks=2))
        row = report["affected_tasks"][0]
        self.assertEqual(row["state"], "RUNNING")
        self.assertIn("pause", row["note"])
        self.assertEqual(report["suggested_commands"], [])

    def test_finished_tasks_are_not_listed(self):
        self.register()
        task_id = self.create_task()
        with Store(str(self.home)) as store:
            task = store.task(task_id)
            task["state"] = "DONE"
            store.save(task)
        report = self.diff(self.candidate(max_active_tasks=2))
        self.assertEqual(report["affected_tasks"], [])


class Contract(DiffFixture):
    def test_project_id_mismatch_is_refused(self):
        self.register()
        with self.assertRaises(Error) as ctx:
            self.diff(self.candidate(), project_id="other")
        self.assertEqual(ctx.exception.code, "PROJECT_MISMATCH")

    def test_preview_agrees_with_rebind_validation(self):
        """The preview must not claim a spec fits when ``rebind`` would refuse it."""
        self.register()
        self.create_task()
        candidate = self.candidate(checks=[{"id": "lint", "argv": ["true"]}])
        report = self.diff(candidate)
        self.assertFalse(report["affected_tasks"][0]["spec_valid_under_new_policy"])

        with Store(str(self.home)) as store:
            task = store.task(report["affected_tasks"][0]["task_id"])
            task["state"] = "BLOCKED"
            task["block_code"] = "POLICY_CHANGED"
            store.save(task)
            with self.assertRaises(Error):
                _pipeline.rebind(
                    store,
                    task["task_id"],
                    {
                        "schema_version": 2,
                        "request_id": "req-" + uuid.uuid4().hex,
                        "target_policy_hash": report["new_policy_hash"],
                        "expected_version": task["row_version"],
                    },
                )

    def test_missing_snapshot_does_not_promise_a_rebind(self):
        """A lost snapshot makes ``rebind`` fail, so no command may be suggested."""
        self.register()
        task_id = self.create_task()
        snapshot = next((self.home / "tasks" / task_id).glob("spec-*.json"))
        snapshot.unlink()
        report = self.diff(self.candidate(max_active_tasks=2))
        row = report["affected_tasks"][0]
        self.assertFalse(row["spec_valid_under_new_policy"])
        self.assertIn("snapshot", row["note"])
        self.assertEqual(report["suggested_commands"], [])

    def test_report_is_read_only(self):
        self.register()
        self.create_task()
        stored = self.home / "projects" / "demo.json"
        before = (stored.read_text(), stored.stat().st_mtime_ns)
        self.diff(self.candidate(max_active_tasks=2))
        after = (stored.read_text(), stored.stat().st_mtime_ns)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
