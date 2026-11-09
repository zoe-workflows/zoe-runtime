"""v1.3 group acceptance and close-out (TGD §15: T18, T20, T30).

A group is accepted **once**, on **one** integration SHA, by a **human**, and
only for ``local_artifact``. Everything in this module is a refusal test: the
happy path is pinned by ``test_v130_group_delivery.py::TestEndToEndDelivery``,
because "it can say yes" is only worth something when the ways to say yes by
accident are closed.

* **T18** a required check with no result for the current ``integration_sha``
  keeps the group out of ``REVIEW_READY`` and makes ``accept`` refuse with
  ``kind=stale_checks`` — "passed last time" is not "passed this time".
* **T20** ``group cancel --external`` records a delivery Zoe never verified:
  ``CANCELLED``, ``accepted:false``, no member counts as integrated, and the
  record cannot be turned into an acceptance afterwards.
* **T30** ``evidence_refs`` must exist, belong to this group, and match the
  final SHA; a citation that fails any of the three is listed by name.
"""

from __future__ import annotations

import json
import uuid

from test_v130_group_delivery import Fixture

from src import _group_runner, _groups, _integration
from src._util import Error


class AcceptCase(Fixture):
    """A one-member group driven to ``REVIEW_READY``."""

    def ready_group(self, task_key="a"):
        gid = self.created(
            members=[
                {
                    "task_key": task_key,
                    "title": task_key.upper(),
                    "spec_file": f"specs/{task_key}.json",
                    "depends_on": [],
                    "requirement_ids": ["REQ-1"],
                    "acceptance_ids": ["AC-1"],
                    "expected_paths": ["src/a.txt"],
                    "check_ids": ["unit"],
                }
            ],
            requirements=[{"id": "REQ-1", "statement": "a is delivered"}],
        )["group_id"]
        task = self.drive(self.member_task(gid, task_key))
        assert task["state"] == "REVIEW_READY", task["state"]
        group = self.group(gid)
        group["state"] = "RUNNING"
        self.store.save_group(group, "test_marker")
        _group_runner.tick(self.store, gid)
        group = self.group(gid)
        assert group["state"] == "REVIEW_READY", group["state"]
        return gid, task, group

    def accept_req(self, group, evidence_refs, decision="accepted"):
        return {
            "schema_version": 2,
            "request_id": "req-" + uuid.uuid4().hex,
            "expected_version": group["row_version"],
            "decision": decision,
            "actor": "human",
            "reason": "reviewed",
            "authorization_ref": "scope-1",
            "evidence_refs": evidence_refs,
        }

    def current_refs(self, group, task):
        return [
            "group:" + group["runtime"]["integration_sha"],
            "task:" + task["task_id"],
        ]


class TestGroupChecksAreBoundToTheSubject(AcceptCase):
    """T18: a check result for another SHA is a stale result, not a pass."""

    def test_a_missing_group_check_record_is_refused(self):
        gid, task, group = self.ready_group()
        subject = group["runtime"]["integration_sha"]
        record = _integration.checks_dir(self.store, gid) / (subject + ".json")
        self.assertTrue(record.is_file(), record)
        record.unlink()
        with self.assertRaises(Error) as ctx:
            _integration.accept(
                self.store, gid, self.accept_req(group, self.current_refs(group, task))
            )
        self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID")
        self.assertIn("no group check record", " ".join(ctx.exception.details["unmet"]))
        self.assertNotEqual(self.group(gid)["state"], "DONE")

    def test_a_check_record_bound_to_another_sha_is_refused(self):
        """The record must answer for *this* SHA, not for the previous one."""
        gid, task, group = self.ready_group()
        subject = group["runtime"]["integration_sha"]
        record_path = _integration.checks_dir(self.store, gid) / (subject + ".json")
        body = json.loads(record_path.read_text())
        body["subject_sha"] = "0" * 40
        record_path.write_text(json.dumps(body))
        with self.assertRaises(Error) as ctx:
            _integration.accept(
                self.store, gid, self.accept_req(group, self.current_refs(group, task))
            )
        self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID")
        self.assertNotEqual(self.group(gid)["state"], "DONE")


class TestEvidenceAttribution(AcceptCase):
    """T30: a citation is only worth its attribution."""

    def assert_refused(self, group, refs, needle):
        with self.assertRaises(Error) as ctx:
            _integration.accept(self.store, group["group_id"], self.accept_req(group, refs))
        self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID")
        unmet = " ".join(ctx.exception.details["unmet"])
        self.assertIn(needle, unmet)
        return unmet

    def test_a_group_ref_for_another_sha_is_refused(self):
        gid, task, group = self.ready_group()
        refs = ["group:" + "f" * 40, "task:" + task["task_id"]]
        self.assert_refused(group, refs, "not for the current integration_sha")

    def test_a_task_ref_from_another_group_is_refused(self):
        gid, task, group = self.ready_group()
        refs = ["group:" + group["runtime"]["integration_sha"], "task:task-not-in-this-group"]
        self.assert_refused(group, refs, "does not belong to this group")

    def test_a_task_ref_without_its_evidence_file_is_refused(self):
        gid, task, group = self.ready_group()
        stored = self.store.task(task["task_id"])
        evidence_id = stored.get("current_evidence_id")
        self.assertTrue(evidence_id, "the driven member must carry evidence")
        path = self.store.path(stored) / "evidence" / (evidence_id + ".json")
        self.assertTrue(path.is_file(), path)
        path.unlink()
        self.assert_refused(group, self.current_refs(group, task), "file is missing")

    def test_an_unknown_reference_form_is_refused(self):
        gid, task, group = self.ready_group()
        refs = ["reviewed-by-me"]
        self.assert_refused(group, refs, "unknown reference form")

    def test_the_full_attribution_list_is_reported_at_once(self):
        """One round trip, every gap: a host should not guess one at a time."""
        gid, task, group = self.ready_group()
        refs = ["group:" + "f" * 40, "task:someone-else", "vibes"]
        with self.assertRaises(Error) as ctx:
            _integration.accept(self.store, gid, self.accept_req(group, refs))
        unmet = " ".join(ctx.exception.details["unmet"])
        self.assertIn("not for the current integration_sha", unmet)
        self.assertIn("does not belong to this group", unmet)
        self.assertIn("unknown reference form", unmet)


ENVELOPE = {
    "api_version",
    "operation_id",
    "group_id",
    "revision",
    "state",
    "plan_digest",
    "progress",
    "blockers",
    "next_actions",
    "event_cursor",
}

PROGRESS = {
    "members_total",
    "integrated",
    "executing",
    "waiting",
    "blocked",
    "integration_sha",
    "starts_billable",
    "max_starts",
    "wall_seconds_inclusive",
    "max_wall_seconds",
}


class TestEnvelopeUniformity(AcceptCase):
    """§11.2: one envelope, so a host never needs a second call to know where it is."""

    def assert_envelope(self, body):
        missing = sorted(ENVELOPE - set(body))
        self.assertFalse(missing, f"missing envelope fields: {missing}")
        progress_keys = sorted(PROGRESS - set(body["progress"]))
        self.assertFalse(progress_keys, f"missing progress fields: {progress_keys}")

    def test_status_carries_the_envelope(self):
        gid = self.created()["group_id"]
        self.assert_envelope(_groups.status(self.store, gid))

    def test_pause_carries_the_envelope(self):
        gid = self.created()["group_id"]
        body = _group_runner.pause(
            self.store,
            gid,
            {
                "schema_version": 2,
                "request_id": "req-" + uuid.uuid4().hex,
                "expected_version": self.group(gid)["row_version"],
                "authorization_ref": "scope-1",
            },
        )
        self.assert_envelope(body)

    def test_cancel_carries_the_envelope(self):
        gid = self.created()["group_id"]
        body = _group_runner.cancel(
            self.store,
            gid,
            {
                "schema_version": 2,
                "request_id": "req-" + uuid.uuid4().hex,
                "expected_version": self.group(gid)["row_version"],
                "authorization_ref": "scope-1",
            },
        )
        self.assert_envelope(body)

    def test_accept_carries_the_envelope_and_the_evidence(self):
        gid, task, group = self.ready_group()
        body = _integration.accept(
            self.store, gid, self.accept_req(group, self.current_refs(group, task))
        )
        self.assert_envelope(body)
        self.assertTrue(body["evidence_refs"])

    def test_external_close_carries_the_envelope(self):
        gid = self.created()["group_id"]
        body = _integration.close_external(
            self.store,
            gid,
            {
                "schema_version": 2,
                "request_id": "req-" + uuid.uuid4().hex,
                "expected_version": self.group(gid)["row_version"],
                "authorization_ref": "scope-1",
                "claim": "asserted",
                "external_ref": "https://example.invalid/pr/9",
                "actor": "human",
            },
        )
        self.assert_envelope(body)
        self.assertEqual(body["state"], "CANCELLED")

    def test_the_wall_clock_cap_is_visible_even_when_it_is_null(self):
        """A null ceiling must read as "no limit", not as a missing field."""
        gid = self.created()["group_id"]
        body = _groups.status(self.store, gid)
        self.assertIn("max_wall_seconds", body["progress"])
        self.assertIsNone(body["progress"]["max_wall_seconds"])


class TestExternalClose(AcceptCase):
    """T20: closing outside Zoe is a record, never an acceptance."""

    def close_req(self, group, claim="asserted"):
        return {
            "schema_version": 2,
            "request_id": "req-" + uuid.uuid4().hex,
            "expected_version": group["row_version"],
            "authorization_ref": "scope-1",
            "claim": claim,
            "external_ref": "https://example.invalid/pr/7",
            "actor": "human",
            "reason": "merged outside Zoe",
        }

    def test_external_close_is_cancelled_and_never_accepted(self):
        gid = self.created()["group_id"]
        group = self.group(gid)
        result = _integration.close_external(self.store, gid, self.close_req(group))
        self.assertEqual(result["state"], "CANCELLED")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["external_close"]["claim"], "asserted")
        after = self.group(gid)
        self.assertEqual(after["state"], "CANCELLED")
        self.assertIsNone(after["runtime"].get("acceptance"))

    def test_an_external_close_satisfies_no_integration(self):
        """No member is integrated, so nothing downstream may treat it as done."""
        gid = self.created()["group_id"]
        result = _integration.close_external(self.store, gid, self.close_req(self.group(gid)))
        self.assertEqual(result["state"], "CANCELLED")
        after = self.group(gid)
        self.assertFalse(_groups.all_integrated(after))
        self.assertEqual(after["runtime"]["members"][0].get("integrated_sha"), None)
        self.assertEqual(_groups.progress(self.store, after)["integrated"], 0)

    def test_acceptance_after_an_external_close_is_refused(self):
        gid = self.created()["group_id"]
        _integration.close_external(self.store, gid, self.close_req(self.group(gid)))
        group = self.group(gid)
        with self.assertRaises(Error) as ctx:
            _integration.accept(
                self.store,
                gid,
                self.accept_req(
                    group, ["group:" + (group["runtime"].get("integration_sha") or "x")]
                ),
            )
        self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID")

    def test_an_observed_runner_blocks_the_close(self):
        """A close is not a way to forget a running process (§9.5)."""
        gid = self.created()["group_id"]
        group = self.group(gid)
        group["runtime"]["lease"] = {
            "lease_id": "l-1",
            "state": "spawn_observed",
            "pid": None,
            "start_token": None,
        }
        self.store.save_group(group, "test_marker")
        with self.assertRaises(Error) as ctx:
            _integration.close_external(self.store, gid, self.close_req(self.group(gid)))
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")
        self.assertNotEqual(self.group(gid)["state"], "CANCELLED")

    def test_the_close_record_is_kept_verbatim(self):
        gid = self.created()["group_id"]
        _integration.close_external(
            self.store, gid, self.close_req(self.group(gid), claim="verified")
        )
        record = json.loads((_groups.group_dir(self.store, gid) / "plan-r001.json").read_text())
        self.assertTrue(record)  # the frozen plan is untouched by a close
        stored = self.group(gid)["runtime"]["external_close"]
        self.assertEqual(stored["claim"], "verified")
        self.assertEqual(stored["external_ref"], "https://example.invalid/pr/7")
        self.assertIn("not accepted", stored["note"])
