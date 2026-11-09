"""v0.5.1 W1: explicit verification scope, strict verdict, evidence binding.

The point of these tests is that a *narrower* run can never be mistaken for a
broader one:

* absent scope → every check runs (the pre-v0.5.1 behaviour, unchanged);
* declared scope → the other checks are ``NOT_RUN``, not ``PASS`` and not
  ``FAIL``, and they are not silently executed either;
* a revision frozen *before* v0.5.1 keeps full semantics even if its text
  happens to contain ``verification_scope``;
* evidence must match the frozen scope, not merely be self-consistent.

Everything here is unit / simulated. No agent account, no remote.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from test_runtime import FakePublisher

from src import _contracts, _evidence, _pipeline
from src._util import Error, atomic, digest, git, read
from src.core import Store, accept, create, revise, run_pipeline

CHECKS = [
    {"id": "lint", "argv": ["true"]},
    {"id": "security", "argv": ["true"]},
    {"id": "slice_a", "argv": ["true"]},
    {"id": "slice_b", "argv": ["false"]},
]

ABSENT = object()


class Backend:
    """In-process check backend: records calls, can fail or raise per check id."""

    def __init__(self, fail=(), raise_for=None, code="RUN_TIMEOUT"):
        self.calls = []
        self.fail = set(fail)
        self.raise_for = raise_for
        self.code = code

    def inspect(self, name):
        return None

    def execute(self, store, t, r, argv, log_name, worker=False, prompt=None):
        self.calls.append(log_name)
        if worker:
            (Path(t["workspace"]) / "src.txt").write_text("implemented\n")
            return 0
        check_id = log_name[len("check-") :]
        if self.raise_for == check_id:
            raise Error(self.code, "Execution exceeded deadline")
        return 1 if check_id in self.fail else 0


class ScopeFixture(unittest.TestCase):
    ac_checks = ("lint", "slice_a")

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "test")
        git(self.repo, "config", "user.email", "test@localhost")
        (self.repo / "src.txt").write_text("initial\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        self.store = Store(self.root / "state")
        self.spec = self.root / "spec.json"
        self.config = {
            "schema_version": 1,
            "project_id": "demo",
            "repo": str(self.repo),
            "backend": "local-process",
            "network": "none",
            "compatibility_verified": True,
            "agent": "codebuddy",
            "checks": [dict(check) for check in CHECKS],
            "allowed_paths": ["src.txt"],
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute", "publish", "accept"],
            "ci_required": False,
            "max_attempts": 3,
        }
        atomic(self.store.home / "projects/demo.json", self.config)

    def tearDown(self):
        self.store.db.close()
        self._td.cleanup()

    # ------------------------------------------------------------------ helpers

    def spec_document(self, scope=ABSENT, ac_checks=None):
        document = {
            "schema_version": 2,
            "title": "Slice A",
            "acceptance_criteria": [
                {
                    "id": "AC-" + check_id,
                    "statement": "implement " + check_id,
                    "verification": {"type": "check", "check_id": check_id},
                }
                for check_id in (ac_checks if ac_checks is not None else self.ac_checks)
            ],
        }
        if scope is not ABSENT:
            document["verification_scope"] = {"checks": list(scope)}
        return document

    def write_spec(self, scope=ABSENT, ac_checks=None):
        self.spec.write_text(json.dumps(self.spec_document(scope, ac_checks)))
        return self.spec

    def req(self, **kwargs):
        return dict(
            schema_version=1,
            request_id="req-" + uuid.uuid4().hex,
            authorization_ref="scope-1",
            **kwargs,
        )

    def new(self, scope=ABSENT, ac_checks=None):
        self.write_spec(scope, ac_checks)
        result = create(
            self.store,
            self.req(
                project_id="demo",
                title="Slice A",
                spec_file=str(self.spec),
                delivery_mode="local_artifact",
            ),
        )
        return self.store.task(result["task_id"])

    def run_task(self, t, backend=None):
        t = self.store.task(t["task_id"])
        rid = "run-" + uuid.uuid4().hex[:16]
        self.store.run({"run_id": rid, "task_id": t["task_id"], "status": "running"})
        t.update(current_run_id=rid, state="RUNNING")
        self.store.save(t)
        return run_pipeline(self.store, t["task_id"], backend or Backend(), FakePublisher())

    def checked(self, backend):
        return sorted(call for call in backend.calls if call.startswith("check-"))

    def rows(self, t):
        return read(self.store.path(t) / "runs" / t["current_run_id"] / "validation.json")

    def evidence(self, t):
        return read(self.store.path(t) / "evidence" / (t["current_evidence_id"] + ".json"))


class TestDeclaration(ScopeFixture):
    def test_absent_scope_keeps_full_verification(self):
        t = self.run_task(self.new())
        self.assertEqual(t["state"], "REVIEW_READY")
        rows = {row["id"]: row["status"] for row in self.rows(t)}
        self.assertEqual(rows, {check["id"]: "PASS" for check in CHECKS})
        self.assertEqual(self.evidence(t)["selected_check_ids"], sorted(rows))
        self.assertEqual(self.evidence(t)["not_run_check_ids"], [])

    def test_declared_scope_marks_the_rest_not_run(self):
        backend = Backend(fail={"security", "slice_b"})
        t = self.run_task(self.new(scope=["lint", "slice_a"]), backend)
        self.assertEqual(t["state"], "REVIEW_READY")
        rows = {row["id"]: row for row in self.rows(t)}
        self.assertEqual(rows["lint"]["status"], "PASS")
        self.assertEqual(rows["slice_a"]["status"], "PASS")
        for skipped in ("security", "slice_b"):
            self.assertEqual(rows[skipped]["status"], "NOT_RUN")
            self.assertIn("verification_scope", rows[skipped]["reason"])
        # The declared scope is what ran: nothing outside it was executed.
        self.assertEqual(self.checked(backend), ["check-lint", "check-slice_a"])

    def test_a_skipped_check_that_would_fail_does_not_fail_the_run(self):
        # slice_b's argv is `false`; the double fails it on demand. Not running
        # it is the whole point, and must not be reported as a pass either.
        backend = Backend(fail={"slice_b"})
        t = self.run_task(self.new(scope=["lint", "slice_a"]), backend)
        self.assertEqual(t["state"], "REVIEW_READY")
        self.assertNotIn("check-slice_b", backend.calls)

    def test_duplicate_declarations_are_deduplicated(self):
        t = self.new(scope=["lint", "slice_a", "lint", "lint"])
        frozen = read(self.store.path(t) / "spec-1.json")
        self.assertIn('"verification_scope"', frozen["text"])
        report = self.run_task(t)
        self.assertEqual(report["state"], "REVIEW_READY")
        self.assertEqual(len(self.rows(report)), len(CHECKS))

    def test_empty_unknown_and_uncovered_declarations_are_refused(self):
        cases = {
            "empty": {"verification_scope": {"checks": []}},
            "not-a-list": {"verification_scope": {"checks": "lint"}},
            "non-string": {"verification_scope": {"checks": [1]}},
            "unknown": {"verification_scope": {"checks": ["nope"]}},
            "uncovered-ac": {"verification_scope": {"checks": ["lint"]}},
        }
        for name, patch in cases.items():
            with self.subTest(name):
                document = self.spec_document(scope=["lint", "slice_a"])
                document.update(patch)
                self.spec.write_text(json.dumps(document))
                with self.assertRaises(Error) as ctx:
                    create(
                        self.store,
                        self.req(
                            project_id="demo",
                            title="Slice A",
                            spec_file=str(self.spec),
                            delivery_mode="local_artifact",
                        ),
                    )
                self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")

    def test_revise_can_introduce_a_scope_as_a_new_revision(self):
        t = self.run_task(self.new())
        self.spec.write_text(json.dumps(self.spec_document(scope=["lint", "slice_a"])))
        result = revise(
            self.store,
            t["task_id"],
            self.req(expected_version=t["row_version"], spec_file=str(self.spec)),
        )
        self.assertEqual(result["status"], "succeeded")
        frozen = read(self.store.path(t) / "spec-2.json")
        self.assertEqual(frozen["verification_semantics"], _contracts.SCOPE_SEMANTICS)


class TestFrozenSemantics(ScopeFixture):
    """The marker decides; the text alone must never narrow an old revision."""

    def test_a_050_revision_ignores_scope_written_in_its_text(self):
        t = self.new(scope=["lint", "slice_a"])
        path = self.store.path(t) / "spec-1.json"
        frozen = read(path)
        frozen.pop("verification_semantics")
        # Only `text` is hashed, so the revision stays internally consistent: it
        # is exactly what a v0.5.0 freeze with a stray key would look like.
        atomic(path, frozen)
        backend = Backend()
        report = self.run_task(t, backend)
        self.assertEqual(report["state"], "REVIEW_READY")
        rows = {row["id"]: row["status"] for row in self.rows(report)}
        self.assertNotIn("NOT_RUN", rows.values(), "an old revision must stay full")
        self.assertEqual(self.checked(backend), sorted("check-" + c["id"] for c in CHECKS))

    def test_unknown_marker_is_refused(self):
        t = self.new(scope=["lint", "slice_a"])
        path = self.store.path(t) / "spec-1.json"
        frozen = read(path)
        frozen["verification_semantics"] = 3
        atomic(path, frozen)
        report = self.run_task(t)
        self.assertEqual(report["state"], "BLOCKED")
        self.assertEqual(report["block_code"], "SPEC_CHANGED")

    def test_marker_two_without_scope_still_runs_everything(self):
        t = self.new()
        path = self.store.path(t) / "spec-1.json"
        self.assertEqual(read(path)["verification_semantics"], _contracts.SCOPE_SEMANTICS)
        report = self.run_task(t)
        self.assertEqual(report["state"], "REVIEW_READY")
        self.assertEqual(len(self.rows(report)), len(CHECKS))

    def test_changing_only_the_document_copy_changes_nothing(self):
        t = self.new(scope=["lint", "slice_a"])
        path = self.store.path(t) / "spec-1.json"
        frozen = read(path)
        frozen["document"] = {"title": "a different, friendlier story"}
        atomic(path, frozen)
        backend = Backend()
        report = self.run_task(t, backend)
        self.assertEqual(report["state"], "REVIEW_READY")
        self.assertEqual(self.checked(backend), ["check-lint", "check-slice_a"])

    def test_tampered_text_is_refused(self):
        t = self.new(scope=["lint", "slice_a"])
        path = self.store.path(t) / "spec-1.json"
        frozen = read(path)
        frozen["text"] = frozen["text"].replace("Slice A", "Slice A (edited)")
        atomic(path, frozen)
        report = self.run_task(t)
        self.assertEqual(report["block_code"], "SPEC_CHANGED")


class TestStrictVerdict(ScopeFixture):
    def test_verdict_rejects_missing_duplicate_and_unknown_results(self):
        full = {"id": "lint", "status": "PASS", "exit_code": 0}
        cases = {
            "missing": ([full], ["lint", "security"]),
            "duplicate": ([full, dict(full)], ["lint"]),
            "unknown-status": ([dict(full, status="SKIPPED")], ["lint"]),
            "failure": ([dict(full, status="FAIL")], ["lint"]),
            "empty-scope": ([], []),
        }
        for name, (rows, selected) in cases.items():
            with self.subTest(name):
                with self.assertRaises(Error) as ctx:
                    _pipeline.judge_validation(rows, selected)
                self.assertEqual(ctx.exception.code, "VALIDATION_FAILED")

    def test_a_timed_out_check_keeps_diagnostics_and_its_error_code(self):
        t = self.run_task(self.new(scope=["lint", "slice_a"]), Backend(raise_for="slice_a"))
        self.assertEqual(t["state"], "BLOCKED")
        self.assertEqual(t["block_code"], "RUN_TIMEOUT")
        rows = {row["id"]: row for row in self.rows(t)}
        # The row is a FAIL, never NOT_RUN: it was declared, it just blew up.
        self.assertEqual(rows["slice_a"]["status"], "FAIL")
        self.assertEqual(rows["slice_a"]["error"]["code"], "RUN_TIMEOUT")
        self.assertEqual(rows["lint"]["status"], "PASS")
        self.assertEqual(rows["security"]["status"], "NOT_RUN")


class TestConsumers(ScopeFixture):
    def test_handoff_lists_failures_but_not_not_run_as_failures(self):
        t = self.run_task(self.new(scope=["lint", "slice_a"]))
        handoff = read(self.store.path(t) / "runs" / t["current_run_id"] / "handoff.json")
        self.assertEqual(handoff["failed_checks"], [])
        self.assertEqual(
            [item["id"] for item in handoff["not_run_checks"]], ["security", "slice_b"]
        )
        self.assertTrue(all(item["reason"] for item in handoff["not_run_checks"]))

    def test_handoff_keeps_exception_rows_as_failures(self):
        t = self.run_task(self.new(scope=["lint"], ac_checks=("lint",)), Backend(raise_for="lint"))
        # The run blocks, so the handoff is written from the blocked state.
        run = self.store.get_run(t["current_run_id"])
        failed = _pipeline.handoff_failed_checks(self.store, t, run)
        self.assertEqual([item["id"] for item in failed], ["lint"])
        self.assertEqual(failed[0]["error"]["code"], "RUN_TIMEOUT")
        skipped = _pipeline.handoff_not_run_checks(self.store, t, run)
        self.assertNotIn("lint", [item["id"] for item in skipped])

    def test_evidence_records_the_scope_it_ran_under(self):
        t = self.run_task(self.new(scope=["lint", "slice_a"]))
        evidence = self.evidence(t)
        self.assertEqual(evidence["selected_check_ids"], ["lint", "slice_a"])
        self.assertEqual(evidence["not_run_check_ids"], ["security", "slice_b"])
        self.assertEqual(evidence["verification_semantics"], _contracts.SCOPE_SEMANTICS)
        # The policy hash still describes the *project*, not this run.
        self.assertEqual(evidence["check_policy_hash"], digest(CHECKS))


class TestEvidenceBinding(ScopeFixture):
    """§6.5's failing example: self-consistent but not the frozen scope."""

    def test_selection_that_does_not_match_the_frozen_scope_is_refused(self):
        t = self.run_task(self.new(scope=["lint", "slice_a"]))
        path = self.store.path(t) / "evidence" / (t["current_evidence_id"] + ".json")
        evidence = read(path)
        evidence["selected_check_ids"] = ["slice_a"]
        evidence["not_run_check_ids"] = ["lint", "security", "slice_b"]
        evidence["check_results"] = [
            row
            for row in evidence["check_results"]
            if row["id"] in ("slice_a", "security", "slice_b")
        ]
        atomic(path, evidence)
        with self.assertRaises(Error) as ctx:
            _evidence.verify(self.store, t, t["validated_fingerprint"])
        self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID")

    def test_missing_and_overlapping_ids_are_refused(self):
        t = self.run_task(self.new(scope=["lint", "slice_a"]))
        path = self.store.path(t) / "evidence" / (t["current_evidence_id"] + ".json")
        original = read(path)
        for name, patch in {
            "overlap": {"not_run_check_ids": ["lint", "security", "slice_b"]},
            "incomplete": {"not_run_check_ids": ["security"]},
        }.items():
            with self.subTest(name):
                evidence = dict(original)
                evidence.update(patch)
                atomic(path, evidence)
                with self.assertRaises(Error) as ctx:
                    _evidence.verify(self.store, t, t["validated_fingerprint"])
                self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID")
        atomic(path, original)

    def test_deleting_the_semantics_field_cannot_widen_or_narrow_the_check(self):
        t = self.run_task(self.new(scope=["lint", "slice_a"]))
        path = self.store.path(t) / "evidence" / (t["current_evidence_id"] + ".json")
        evidence = read(path)
        for key in ("verification_semantics", "selected_check_ids", "not_run_check_ids"):
            evidence.pop(key, None)
        atomic(path, evidence)
        # Missing means "full semantics", and the frozen revision is scope-2, so
        # the evidence no longer describes it.
        with self.assertRaises(Error) as ctx:
            _evidence.verify(self.store, t, t["validated_fingerprint"])
        self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID")

    def test_unknown_semantics_value_is_refused(self):
        t = self.run_task(self.new(scope=["lint", "slice_a"]))
        path = self.store.path(t) / "evidence" / (t["current_evidence_id"] + ".json")
        evidence = read(path)
        evidence["verification_semantics"] = 7
        atomic(path, evidence)
        with self.assertRaises(Error) as ctx:
            _evidence.verify(self.store, t, t["validated_fingerprint"])
        self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID")

    def test_a_failing_row_cannot_be_delivered(self):
        t = self.run_task(self.new(scope=["lint", "slice_a"]))
        path = self.store.path(t) / "evidence" / (t["current_evidence_id"] + ".json")
        evidence = read(path)
        for row in evidence["check_results"]:
            if row["id"] == "lint":
                row["status"] = "FAIL"
                row["exit_code"] = 1
        atomic(path, evidence)
        # Readable evidence, refused delivery (§6.5-5).
        self.assertEqual(_evidence.load(self.store, t)["evidence_id"], evidence["evidence_id"])
        with self.assertRaises(Error):
            accept(
                self.store,
                t["task_id"],
                self.req(
                    expected_version=t["row_version"],
                    subject_sha=t["validated_sha"],
                    actor="human",
                    decision="accepted",
                    evidence=["checked"],
                ),
            )

    def test_old_style_evidence_for_an_old_revision_still_verifies(self):
        """Upgrade path: no marker in the spec, no new fields in the evidence."""
        t = self.run_task(self.new())
        spec_path = self.store.path(t) / "spec-1.json"
        frozen = read(spec_path)
        frozen.pop("verification_semantics")
        atomic(spec_path, frozen)
        path = self.store.path(t) / "evidence" / (t["current_evidence_id"] + ".json")
        evidence = read(path)
        for key in ("verification_semantics", "selected_check_ids", "not_run_check_ids"):
            evidence.pop(key, None)
        atomic(path, evidence)
        self.assertEqual(
            _evidence.verify(self.store, t, t["validated_fingerprint"])["evidence_id"],
            evidence["evidence_id"],
        )


if __name__ == "__main__":
    unittest.main()
