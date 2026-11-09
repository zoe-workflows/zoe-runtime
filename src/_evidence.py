"""Delivery evidence: build, persist and re-check the controller's own proof.

Evidence binds the frozen spec, the candidate tree, the approved check policy and
the check results into one immutable record. It is produced by the controller
from files it read itself; agent claims never enter this record. Stale or
mismatched evidence blocks publication and acceptance instead of being recomputed
"as if" it were the original proof (multi-agent design §15.2).

v0.5.1 adds the *scope* dimension: which checks this run executed and which were
declared out of scope (``NOT_RUN``). ``check_policy_hash`` stays the digest of
the **full** check list — it describes what the project declared, not what this
run exercised — so old and new evidence stay comparable. Proving "this evidence
matches the frozen scope" is a separate job, done in :func:`verify`.
"""

from __future__ import annotations

import time

from src import _contracts
from src._util import Error, atomic, digest, read, require

#: ``validation.json`` row statuses. ``NOT_RUN`` means "not declared for this
#: run"; it is neither a pass nor a failure (§3).
PASS = "PASS"
FAIL = "FAIL"
NOT_RUN = "NOT_RUN"


def selected_rows(rows: list[dict]) -> list[dict]:
    """Rows that were actually executed (``PASS`` / ``FAIL``)."""
    return [row for row in rows if row.get("status") in (PASS, FAIL)]


def skipped_rows(rows: list[dict]) -> list[dict]:
    """Rows recorded as ``NOT_RUN``."""
    return [row for row in rows if row.get("status") == NOT_RUN]


def failed_rows(rows: list[dict]) -> list[dict]:
    """Rows that failed, including rows that failed by raising (§6.4.1)."""
    return [row for row in rows if row.get("status") == FAIL]


def build(
    t: dict,
    check_results: list[dict],
    manifest_hash: str,
    checks: list[dict],
    verifier_image: str | None = None,
    adapter: dict | None = None,
    verification_semantics: int = _contracts.FULL_SEMANTICS,
) -> dict:
    policy_hash = digest(checks)
    evidence_id = (
        "ev-"
        + digest(
            {
                "task_id": t["task_id"],
                "spec_hash": t["spec_hash"],
                "candidate_sha": t.get("candidate_sha"),
                "manifest_hash": manifest_hash,
                "check_policy_hash": policy_hash,
            }
        )[:24]
    )
    rows = [row for row in check_results if isinstance(row, dict)]
    return {
        "schema_version": 1,
        "evidence_id": evidence_id,
        "task_id": t["task_id"],
        "project_id": t["project_id"],
        "spec_revision": t["spec_revision"],
        "spec_hash": t["spec_hash"],
        "candidate_sha": t.get("candidate_sha"),
        # 候选与 base 相同时，下面那些 PASS 只说明"基线本来就能过"。抄写这个
        # 布尔是为了让读证据的人不必自己拿两个 SHA 比对（也要防"两边都为空"被
        # 当成相等）。它不参与 evidence_id 的摘要，只做标注。
        "empty_candidate": bool(t.get("empty_candidate")),
        "manifest_hash": manifest_hash,
        "check_policy_hash": policy_hash,
        # Which checks this run exercised, which it did not, and under which
        # semantics — copied from the frozen spec file, never inferred here.
        "selected_check_ids": sorted(row["id"] for row in selected_rows(rows)),
        "not_run_check_ids": sorted(row["id"] for row in skipped_rows(rows)),
        "verification_semantics": verification_semantics,
        "verifier_image_digest": verifier_image,
        "check_results": check_results,
        "adapter": adapter,
        "source": "controller",
        "created_at": time.time(),
    }


def persist(store, t: dict, run_id: str, evidence: dict) -> dict:
    atomic(store.path(t) / "runs" / run_id / "evidence.json", evidence)
    atomic(store.path(t) / "evidence" / (evidence["evidence_id"] + ".json"), evidence)
    return evidence


def load(store, t: dict, evidence_id: str | None = None) -> dict:
    """Read the referenced evidence; a missing or altered file is a hard block."""
    evidence_id = evidence_id or t.get("current_evidence_id")
    require(evidence_id, "EVIDENCE_MISSING", "Task has no recorded evidence")
    path = store.path(t) / "evidence" / (evidence_id + ".json")
    try:
        evidence = read(path)
    except (OSError, ValueError) as exc:
        raise Error("EVIDENCE_INVALID", f"Evidence {evidence_id} unreadable: {exc}") from exc
    require(
        evidence.get("evidence_id") == evidence_id, "EVIDENCE_INVALID", "Evidence identity mismatch"
    )
    return evidence


def _expected(store, t: dict, evidence: dict) -> tuple[int, list[str], list[str]]:
    """Recompute ``(semantics, expected ids, all ids)`` from the frozen objects.

    This is the difference between "the record is internally consistent" and
    "the record describes the scope that was actually frozen" (§6.5).
    """
    config = t.get("config") or {}
    checks = [check for check in (config.get("checks") or []) if isinstance(check, dict)]
    all_ids = [check["id"] for check in checks]
    path = store.path(t) / f"spec-{t['spec_revision']}.json"
    try:
        frozen = read(path)
    except (OSError, ValueError) as exc:
        raise Error("EVIDENCE_INVALID", f"Frozen spec is unreadable: {exc}") from exc
    text = frozen.get("text")
    require(isinstance(text, str), "EVIDENCE_INVALID", "Frozen spec text is missing")
    semantics = frozen.get("verification_semantics", _contracts.FULL_SEMANTICS)
    require(
        semantics in (_contracts.FULL_SEMANTICS, _contracts.SCOPE_SEMANTICS),
        "EVIDENCE_INVALID",
        "Frozen spec has unknown verification semantics",
    )
    try:
        _, document = _contracts.parse_spec(text, config)
        declared = _contracts.verification_scope(document, checks)
    except Error as exc:
        raise Error("EVIDENCE_INVALID", "Frozen spec no longer validates: " + str(exc)) from None
    if semantics == _contracts.FULL_SEMANTICS or declared is None:
        expected = list(all_ids)
    else:
        expected = declared
    return semantics, expected, all_ids


def _verify_scope(store, t: dict, evidence: dict) -> None:
    semantics, expected, all_ids = _expected(store, t, evidence)

    stored = evidence.get("verification_semantics", _contracts.FULL_SEMANTICS)
    require(
        stored in (_contracts.FULL_SEMANTICS, _contracts.SCOPE_SEMANTICS),
        "EVIDENCE_INVALID",
        f"Evidence has unknown verification semantics {stored!r}",
    )
    # Deleting the marker does not help: missing means "full", and the expected
    # set is then recomputed as "every check" (§6.5-4).
    require(
        stored == semantics,
        "EVIDENCE_INVALID",
        "Evidence verification semantics do not match the frozen spec revision",
    )

    rows = [row for row in (evidence.get("check_results") or []) if isinstance(row, dict)]
    derived_selected = sorted(row.get("id") for row in selected_rows(rows))
    derived_not_run = sorted(row.get("id") for row in skipped_rows(rows))
    unknown_status = [
        row.get("id") for row in rows if row.get("status") not in (PASS, FAIL, NOT_RUN)
    ]
    require(not unknown_status, "EVIDENCE_INVALID", "Evidence has unknown check statuses")

    selected = evidence.get("selected_check_ids")
    selected = sorted(selected) if isinstance(selected, list) else derived_selected
    not_run = evidence.get("not_run_check_ids")
    not_run = sorted(not_run) if isinstance(not_run, list) else derived_not_run

    require(
        selected == sorted(expected),
        "EVIDENCE_INVALID",
        "Evidence covers {} but the frozen scope is {}".format(
            ", ".join(selected) or "nothing", ", ".join(expected) or "nothing"
        ),
    )
    require(
        not (set(selected) & set(not_run)),
        "EVIDENCE_INVALID",
        "Evidence lists the same check as both executed and not run",
    )
    require(
        set(selected) | set(not_run) == set(all_ids),
        "EVIDENCE_INVALID",
        "Evidence does not account for every configured check",
    )
    require(
        derived_selected == selected and derived_not_run == not_run,
        "EVIDENCE_INVALID",
        "Evidence ids disagree with its own check results",
    )
    require(
        len(derived_selected) == len(set(derived_selected)),
        "EVIDENCE_INVALID",
        "Evidence contains duplicate check results",
    )
    require(
        all(row.get("id") in set(not_run) for row in skipped_rows(rows)),
        "EVIDENCE_INVALID",
        "NOT_RUN may only appear on checks outside the declared scope",
    )


def verify(
    store,
    t: dict,
    manifest_hash: str,
    evidence_id: str | None = None,
    require_all_pass: bool = True,
) -> dict:
    """Re-check recorded evidence against the candidate, the spec **and** the scope."""
    evidence = load(store, t, evidence_id)
    require(
        evidence.get("spec_hash") == t.get("spec_hash"),
        "EVIDENCE_INVALID",
        "Evidence was produced for a different spec revision",
    )
    require(
        evidence.get("candidate_sha") == t.get("validated_sha"),
        "EVIDENCE_INVALID",
        "Evidence was produced for a different candidate",
    )
    require(
        evidence.get("manifest_hash") == manifest_hash,
        "EVIDENCE_INVALID",
        "Delivered files changed since verification",
    )
    _verify_scope(store, t, evidence)
    if require_all_pass:
        # Diagnostic evidence with a FAIL is readable and handover-able, but it
        # is not a delivery record (§6.5-5).
        rows = selected_rows(
            [row for row in (evidence.get("check_results") or []) if isinstance(row, dict)]
        )
        failed = [str(row.get("id")) for row in rows if row.get("status") != PASS]
        require(
            not failed,
            "EVIDENCE_INVALID",
            "Evidence contains failing checks; it cannot be delivered: " + ", ".join(failed),
        )
    return evidence
