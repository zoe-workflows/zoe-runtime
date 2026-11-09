"""Serial integration and group-level acceptance (TGD §8, §9).

The controller merges one validated member at a time into
``zoe/group-<id>`` inside its **own** worktree, records the result, and only
then moves the branch ref. Git effects cannot join the SQLite transaction, so
the bookkeeping is a three-stage ledger::

    intent -> prepared -> done
              \\-> conflict / failed

``prepared`` is written **before** ``update-ref``. That ordering is the whole
point: a crash after the ref moves but before the record is committed is
recoverable (the file says which SHA to expect), while the reverse order leaves
a moved branch that no record ever described.

Group checks are bound to a ``subject_sha`` — the integration SHA they were run
against. ``NOT_RUN`` and results for another subject are never "passing": they
cannot promote a group to ``REVIEW_READY`` (TGD §9.3).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from src import _evidence, _groups, _review
from src._backend import fingerprint, manifest
from src._config import authorize
from src._store import Store
from src._util import Error, atomic, digest, git, read, require

#: Hard ceiling for one group check. A hung check must not hang the runner.
CHECK_TIMEOUT_SECONDS = 600

#: Ledger statuses. ``intent``/``prepared`` are "unfinished"; the rest are final.
OPEN_STATUSES = ("intent", "prepared")


def _unavailable(message: str, kind: str, **details) -> Error:
    payload = dict(details)
    payload["kind"] = kind
    return Error("INTEGRATION_UNKNOWN", message, details=payload)


def integration_dir(store: Store, group_id: str) -> Path:
    return _groups.group_dir(store, group_id) / "integration"


def checks_dir(store: Store, group_id: str) -> Path:
    return _groups.group_dir(store, group_id) / "checks"


def _ledger_path(store: Store, group_id: str, op_id: str) -> Path:
    return integration_dir(store, group_id) / (op_id + ".json")


def _write_ledger(store: Store, group_id: str, ledger: dict) -> Path:
    path = _ledger_path(store, group_id, ledger["op_id"])
    atomic(path, ledger)
    return path


def _record_operation(store: Store, ledger: dict) -> None:
    """Persist the ledger status in ``operations`` (same row, updated in place)."""
    payload = {
        "status": ledger["status"],
        "action": "group_integrate",
        "group_id": ledger["group_id"],
        "member": ledger["member"],
        "op_id": ledger["op_id"],
        "result_sha": ledger.get("result_sha"),
        "created_at": ledger.get("created_at"),
        "updated_at": time.time(),
    }
    store.db.execute(
        "INSERT OR REPLACE INTO operations VALUES(?,?,?)",
        (ledger["op_id"], digest(["group_integrate", ledger["op_id"]]), _json(payload)),
    )


def _json(value) -> str:
    return json.dumps(value)


def _git_capture(cwd, *args):
    """Run git without raising: merge conflicts are an expected outcome."""
    proc = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.attributesFile=/dev/null",
            *args,
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _ref_sha(repo, ref: str) -> str | None:
    code, out = _git_capture(repo, "rev-parse", "--verify", ref)
    return out.strip() if code == 0 and out.strip() else None


def is_ancestor(repo, ancestor: str | None, descendant: str | None) -> bool:
    """True when ``ancestor`` is already reachable from ``descendant``.

    This is the §8.2 "重试前探可达性" probe: a candidate that is already an
    ancestor of the integration branch must not be merged a second time.
    """
    if not ancestor or not descendant:
        return False
    code, _ = _git_capture(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    return code == 0


def recover_result_sha(repo, observed: str, candidate: str) -> str | None:
    """Recover the integration commit for ``candidate`` from a moved branch.

    Only a merge commit carrying ``candidate`` as a parent counts. A branch that
    merely *contains* the candidate (fast-forward, someone else's merge) has no
    recorded result and must not be adopted as this group's integration (§8.3).
    """
    if not observed or not candidate:
        return None
    code, out = _git_capture(repo, "rev-list", "--parents", "-n", "1", observed)
    if code != 0:
        return None
    parents = out.split()[1:]
    return observed if len(parents) >= 2 and candidate in parents else None


def ensure_branch(store: Store, group: dict) -> str:
    """Create the integration branch at ``base_sha`` without touching main."""
    branch = group["definition"]["integration_branch"]
    repo = Path(store.config(group["project_id"])["repo"])
    ref = "refs/heads/" + branch
    if _ref_sha(repo, ref) is None:
        git(repo, "branch", branch, group["base_sha"])
    return branch


def manifest_hash(worktree) -> str:
    """Tree identity of an integration worktree (files + contents, not a SHA)."""
    return digest(manifest({"workspace": str(worktree)}))


# --------------------------------------------------------------------------- #
# preconditions                                                          (8.2)
# --------------------------------------------------------------------------- #


def integration_preconditions(store: Store, group: dict, task_key: str) -> dict:
    """Return the member task after proving it may be integrated.

    Four conditions, all required (TGD §8.2). Diagnostics may be *read* but never
    integrated: a FAIL in the evidence is a repair task, not a delivery.
    """
    runtime = _groups.member_runtime(group, task_key)
    require(runtime.get("task_id"), "STATE_CONFLICT", "member has no task yet")
    task = store.task(runtime["task_id"])
    require(
        task.get("group_id") == group["group_id"],
        "STATE_CONFLICT",
        "task does not belong to this group",
    )
    require(
        task.get("state") == "REVIEW_READY",
        "STATE_CONFLICT",
        f"member {task_key} is {task.get('state')}; only REVIEW_READY can be integrated",
    )
    require(
        runtime.get("validated_sha") == task.get("validated_sha") == task.get("candidate_sha"),
        "STATE_CONFLICT",
        f"member {task_key}: validated_sha, task.validated_sha and candidate_sha disagree",
    )
    current_policy = digest(store.config(group["project_id"]))[:16]
    require(
        task.get("policy_revision") == current_policy,
        "POLICY_CHANGED",
        f"member {task_key} was verified under a different policy revision",
    )
    # Evidence is re-checked against the *current* workspace: a FAIL, a changed
    # manifest or a different candidate is not a delivery (§8.2-2).
    _evidence.verify(store, task, fingerprint(task))
    return task


# --------------------------------------------------------------------------- #
# integrate                                                              (8.2)
# --------------------------------------------------------------------------- #


def integrate(store: Store, group: dict, task_key: str) -> dict:
    """Merge one validated member's candidate into the integration branch."""
    task = integration_preconditions(store, group, task_key)
    runtime = group["runtime"]
    policy_revision = digest(store.config(group["project_id"]))[:16]
    require(
        runtime["policy_snapshot"]["policy_revision"] == policy_revision,
        "POLICY_CHANGED",
        "project policy changed since the group was created",
    )
    branch = ensure_branch(store, group)
    base = runtime.get("integration_sha") or group["base_sha"]
    repo = Path(store.config(group["project_id"])["repo"])

    op_id = "iop-" + uuid.uuid4().hex[:16]
    seq = int(runtime.get("integration_seq") or 0) + 1
    ledger = {
        "op_id": op_id,
        "group_id": group["group_id"],
        "member": task_key,
        "seq": seq,
        "status": "intent",
        "base_sha": base,
        "candidate_sha": task["candidate_sha"],
        "integration_branch": branch,
        "created_at": time.time(),
        "result_sha": None,
        "merged_candidate_sha": task["candidate_sha"],
        "quick_checks": [],
        "tree_fingerprint": None,
        "worktree": None,
        "conflict_dir": None,
        "failures": [],
    }
    runtime["integration_seq"] = seq
    with store.tx():
        _write_ledger(store, group["group_id"], ledger)
        _record_operation(store, ledger)
        runtime.setdefault("open_operations", []).append(op_id)
        runtime["last_op_id"] = op_id
        store.save_group(group, "group_integration_intent")

    # §8.2 "重试前探可达性": an earlier attempt may already have landed this
    # candidate. Merging it again would double the history, so adopt the existing
    # merge commit — or block when it cannot be identified.
    observed = _ref_sha(repo, "refs/heads/" + branch)
    if is_ancestor(repo, task["candidate_sha"], observed):
        return _adopt_already_merged(store, group, ledger, observed, repo)

    return _merge_and_record(store, group, ledger, base, repo, attempt=0)


def _new_worktree(store: Store, group: dict, ledger: dict, attempt: int) -> Path:
    """One worktree per attempt: an old half-merged tree is never reused."""
    name = "wt" if attempt == 0 else f"wt-r{attempt}"
    worktree = integration_dir(store, group["group_id"]) / ledger["op_id"] / name
    worktree.parent.mkdir(parents=True, exist_ok=True)
    return worktree


def _adopt_already_merged(store: Store, group: dict, ledger: dict, observed: str, repo) -> dict:
    """The branch already contains the candidate: record it, do not merge again."""
    result_sha = recover_result_sha(repo, observed, ledger["candidate_sha"])
    if not result_sha:
        raise _unavailable(
            f"candidate {ledger['candidate_sha'][:12]} is already on "
            f"{ledger['integration_branch']} but no integration commit can be identified",
            "merged_unrecoverable",
            op_id=ledger["op_id"],
            observed_sha=observed,
            candidate_sha=ledger["candidate_sha"],
        )
    ledger.update(status="prepared", result_sha=result_sha, recovered_from=observed)
    _write_ledger(store, group["group_id"], ledger)
    _record_operation(store, ledger)
    return _finish(store, group, ledger, repo, ref_already_moved=True)


def _merge_and_record(
    store: Store, group: dict, ledger: dict, base: str, repo, attempt: int
) -> dict:
    """Merge the ledger's candidate in an isolated worktree, then move the ref.

    Every failure keeps the ledger and the worktree on disk: a retry must start a
    new ``op_id`` so the failed attempt stays auditable (§8.2).
    """
    task_key = ledger["member"]
    branch = ledger["integration_branch"]
    op_id = ledger["op_id"]
    worktree = _new_worktree(store, group, ledger, attempt)
    git(repo, "worktree", "add", "--detach", str(worktree), base)
    ledger["worktree"] = str(worktree)

    candidate_sha = ledger["candidate_sha"]
    code, output = _git_capture(
        worktree,
        "-c",
        "user.name=Zoe Controller",
        "-c",
        "user.email=zoe@localhost",
        "merge",
        "--no-ff",
        "--no-edit",
        candidate_sha,
        "-m",
        f"zoe: integrate {task_key} ({candidate_sha[:12]})",
    )
    if code != 0:
        conflict = integration_dir(store, group["group_id"]) / op_id / "conflict"
        conflict.mkdir(parents=True, exist_ok=True)
        status_code, status_text = _git_capture(worktree, "status", "--porcelain")
        (conflict / "status.txt").write_text(status_text)
        (conflict / "merge-output.txt").write_text(output)
        _git_capture(worktree, "merge", "--abort")
        ledger.update(status="conflict", conflict_dir=str(conflict), failures=[output[-4000:]])
        _write_ledger(store, group["group_id"], ledger)
        _record_operation(store, ledger)
        with store.tx():
            _close_op(group, op_id)
            _set_blockers(
                group,
                [
                    {
                        "member": task_key,
                        "code": "INTEGRATION_CONFLICT",
                        "conflict_dir": str(conflict),
                        "next_action": "resolve manually, then create a follow-up group",
                    }
                ],
            )
            store.save_group(group, "group_integration_conflict")
        raise Error(
            "INTEGRATION_CONFLICT",
            f"merging {task_key} into {branch} conflicted; diagnostics kept",
            details={
                "kind": "merge_conflict",
                "conflict_dir": str(conflict),
                "op_id": op_id,
                "integration_sha_unchanged": group["runtime"].get("integration_sha"),
            },
        )

    result_sha = _ref_sha(worktree, "HEAD")
    require(result_sha, "INTEGRATION_UNKNOWN", "integration worktree has no HEAD")

    quick = run_checks(store, group, worktree, result_sha, quick_only=True)
    ledger["quick_checks"] = quick
    failed = [row for row in quick if row["status"] != "PASS"]
    if failed:
        ledger.update(status="failed", result_sha=result_sha, failures=failed)
        _write_ledger(store, group["group_id"], ledger)
        _record_operation(store, ledger)
        with store.tx():
            _close_op(group, op_id)
            _set_blockers(
                group,
                [
                    {
                        "member": task_key,
                        "code": "INTEGRATION_CHECK_FAILED",
                        "check_ids": [row["check_id"] for row in failed],
                        "op_id": op_id,
                        "worktree": str(worktree),
                    }
                ],
            )
            store.save_group(group, "group_integration_check_failed")
        raise Error(
            "INTEGRATION_CHECK_FAILED",
            "quick group checks failed on the integration candidate: "
            + ", ".join(row["check_id"] for row in failed),
            details={
                "kind": "group_checks",
                "op_id": op_id,
                "worktree": str(worktree),
                "integration_sha_unchanged": group["runtime"].get("integration_sha"),
                "note": "the prepared ledger and the worktree are kept; retry needs a new op_id",
            },
        )

    ledger.update(
        status="prepared",
        result_sha=result_sha,
        tree_fingerprint=manifest_hash(worktree),
    )
    # File first, then the row: a crash in between is recoverable (§8.3).
    _write_ledger(store, group["group_id"], ledger)
    _record_operation(store, ledger)
    return _finish(store, group, ledger, repo, ref_already_moved=False)


def _finish(store: Store, group: dict, ledger: dict, repo, ref_already_moved: bool) -> dict:
    """Move the branch ref (unless it already points at the result) and commit.

    The ref update is conditional on the previous value: a non-fast-forward or a
    stale base is never retried with force, it becomes an ``INTEGRATION_UNKNOWN``
    that §8.3 reconciliation resolves from the observed ref.
    """
    runtime = group["runtime"]
    branch = ledger["integration_branch"]
    base = ledger["base_sha"]
    result_sha = ledger["result_sha"]
    op_id = ledger["op_id"]
    if not ref_already_moved:
        try:
            git(repo, "update-ref", "refs/heads/" + branch, result_sha, base)
        except Error as exc:
            raise _unavailable(
                f"could not move {branch} from {base[:12]} to {result_sha[:12]}: {exc}",
                "ref_update_failed",
                op_id=op_id,
                branch=branch,
                expected_base=base,
                result_sha=result_sha,
            ) from None

    ledger["status"] = "done"
    _write_ledger(store, group["group_id"], ledger)
    _record_operation(store, ledger)
    with store.tx():
        runtime["integration_sha"] = result_sha
        runtime["integration_branch_created"] = True
        if ledger.get("worktree"):
            runtime["integration_worktree"] = ledger["worktree"]
        runtime["checks"] = [
            row for row in runtime.get("checks") or [] if row.get("subject_sha") == result_sha
        ]
        member_runtime = _groups.member_runtime(group, ledger["member"])
        member_runtime["integrated_sha"] = result_sha
        member_runtime["validated_sha"] = ledger.get("merged_candidate_sha")
        member_runtime["invalidated"] = False
        member_runtime["last_block"] = None
        _close_op(group, op_id)
        _set_blockers(group, [])
        store.save_group(group, "group_member_integrated")
    return ledger


def _close_op(group: dict, op_id: str) -> None:
    runtime = group["runtime"]
    runtime["open_operations"] = [
        item for item in runtime.get("open_operations") or [] if item != op_id
    ]
    runtime["last_op_id"] = op_id


def _set_blockers(group: dict, blockers: list[dict]) -> None:
    group["runtime"]["blockers"] = list(blockers)


# --------------------------------------------------------------------------- #
# reconcile                                                              (8.3)
# --------------------------------------------------------------------------- #


def reconcile(store: Store, group: dict) -> dict:
    """Close out unfinished integration ledgers. Files win over the DB."""
    group_id = group["group_id"]
    runtime = group["runtime"]
    for op_id in list(runtime.get("open_operations") or []):
        path = _ledger_path(store, group_id, op_id)
        if not path.is_file():
            raise _unavailable(
                f"integration ledger {op_id} is recorded but its file is missing",
                "ledger_missing",
                op_id=op_id,
            )
        ledger = read(path)
        status = ledger.get("status")
        if status == "done":
            _apply_done(store, group, ledger)
            continue
        if status == "intent":
            # `prepared` must always precede the ref update, so an intent whose
            # candidate already reached the branch has no recorded result: adopt
            # the identifiable merge commit, otherwise stop rather than guess.
            branch = ledger["integration_branch"]
            repo = Path(store.config(group["project_id"])["repo"])
            observed = _ref_sha(repo, "refs/heads/" + branch)
            if observed is None or observed == ledger["base_sha"]:
                # Nothing was executed. Replay is allowed exactly once (§8.3).
                _replay_intent(store, group, ledger, repo)
                continue
            if is_ancestor(repo, ledger["candidate_sha"], observed):
                result_sha = recover_result_sha(repo, observed, ledger["candidate_sha"])
                if not result_sha:
                    raise _unavailable(
                        f"integration {op_id}: the candidate is on {branch} but the "
                        "merge commit cannot be identified",
                        "intent_merged_unrecoverable",
                        op_id=op_id,
                        observed_sha=observed,
                        candidate_sha=ledger["candidate_sha"],
                    )
                ledger.update(status="prepared", result_sha=result_sha, recovered_from=observed)
                _write_ledger(store, group["group_id"], ledger)
                _record_operation(store, ledger)
                _apply_done(store, group, ledger)
                continue
            raise _unavailable(
                f"integration {op_id} was recorded as intent but the branch already moved",
                "intent_ref_moved",
                op_id=op_id,
                observed_sha=observed,
                expected_base=ledger["base_sha"],
            )
        if status == "prepared":
            branch = ledger["integration_branch"]
            repo = Path(store.config(group["project_id"])["repo"])
            observed = _ref_sha(repo, "refs/heads/" + branch)
            if observed == ledger["result_sha"]:
                # Ref moved, record not committed: complete the bookkeeping.
                _apply_done(store, group, ledger)
                continue
            if observed == ledger["base_sha"]:
                # Ref never moved: replay the (idempotent) update, then finish.
                git(
                    repo,
                    "update-ref",
                    "refs/heads/" + branch,
                    ledger["result_sha"],
                    ledger["base_sha"],
                )
                _apply_done(store, group, ledger)
                continue
            raise _unavailable(
                f"integration {op_id} is prepared but {branch} is at an unexpected value",
                "ref_unexpected",
                op_id=op_id,
                observed_sha=observed,
                expected=ledger["result_sha"],
            )
        # conflict / failed: final. Keep the ledger, drop it from the open list.
        with store.tx():
            _close_op(group, op_id)
            store.save_group(group, "group_integration_closed")
    return group


def _replay_intent(store: Store, group: dict, ledger: dict, repo) -> None:
    """Re-execute an intent that never reached the branch — at most once.

    A replay uses a fresh worktree under a new name so a half-merged tree from
    the crashed attempt cannot contaminate it, while the ``op_id`` stays the
    same so the audit trail keeps one row per logical integration (§8.2/§8.3).
    """
    attempts = int(ledger.get("replay_count") or 0)
    if attempts >= 1:
        raise _unavailable(
            f"integration {ledger['op_id']} was replayed once and still did not land",
            "replay_exhausted",
            op_id=ledger["op_id"],
            attempts=attempts,
        )
    branch = ledger["integration_branch"]
    if _ref_sha(repo, "refs/heads/" + branch) is None:
        git(repo, "branch", branch, ledger["base_sha"])
    ledger["replay_count"] = attempts + 1
    _merge_and_record(store, group, ledger, ledger["base_sha"], repo, attempt=attempts + 1)


def _apply_done(store: Store, group: dict, ledger: dict) -> None:
    runtime = group["runtime"]
    result_sha = ledger.get("result_sha")
    if not result_sha:
        raise _unavailable(
            "integration {} is marked done without a result_sha".format(ledger["op_id"]),
            "result_missing",
            op_id=ledger["op_id"],
        )
    # The file is the authority (§8.3): leaving it at `prepared` while the DB
    # says `done` would recreate exactly the inconsistency reconciliation exists
    # to prevent. Rewrite it before the row.
    if ledger.get("status") != "done":
        ledger["status"] = "done"
        ledger["reconciled_at"] = time.time()
        _write_ledger(store, group["group_id"], ledger)
    with store.tx():
        _record_operation(store, ledger)
        runtime["integration_sha"] = result_sha
        runtime["integration_worktree"] = ledger.get("worktree") or runtime.get(
            "integration_worktree"
        )
        member_runtime = _groups.member_runtime(group, ledger["member"])
        member_runtime["integrated_sha"] = result_sha
        member_runtime["validated_sha"] = ledger.get("merged_candidate_sha")
        member_runtime["invalidated"] = False
        member_runtime["last_block"] = None
        _close_op(group, ledger["op_id"])
        store.save_group(group, "group_integration_reconciled")


# --------------------------------------------------------------------------- #
# group checks                                                            (9.2)
# --------------------------------------------------------------------------- #


def _execute_check(argv: list[str], cwd: Path, log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "wb") as out:
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                stdout=out,
                stderr=subprocess.STDOUT,
                timeout=CHECK_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return 124
        except OSError as exc:
            out.write(str(exc).encode())
            return 127
    return proc.returncode


def run_checks(
    store: Store, group: dict, worktree: Path, subject_sha: str, quick_only: bool = False
) -> list[dict]:
    """Run the declared group checks in ``worktree``; never raises for a failure."""
    config = store.config(group["project_id"])
    configured = {check["id"]: check for check in config.get("checks") or []}
    directory = checks_dir(store, group["group_id"]) / subject_sha
    rows = []
    for item in group["definition"]["group_checks"]:
        if quick_only and not item.get("quick"):
            continue
        check_id = item["check_id"]
        entry = configured.get(check_id)
        if entry is None:
            rows.append(
                {
                    "check_id": check_id,
                    "status": "NOT_RUN",
                    "exit_code": None,
                    "command_digest": item["command_digest"],
                    "log_ref": None,
                    "at": time.time(),
                    "reason": "check is no longer configured",
                }
            )
            continue
        log = directory / (check_id + ".log")
        code = _execute_check(list(entry["argv"]), worktree, log)
        rows.append(
            {
                "check_id": check_id,
                "status": "PASS" if code == 0 else "FAIL",
                "exit_code": code,
                "command_digest": digest(entry["argv"]),
                "log_ref": str(log),
                "at": time.time(),
            }
        )
    return rows


def run_group_checks(store: Store, group: dict) -> dict:
    """Run (or reuse) the group checks for the **current** integration SHA."""
    runtime = group["runtime"]
    subject_sha = runtime.get("integration_sha")
    require(subject_sha, "STATE_CONFLICT", "no integration_sha to check")
    worktree = runtime.get("integration_worktree")
    if not worktree or not Path(worktree).is_dir():
        raise _unavailable(
            f"the integration worktree for {subject_sha[:12]} is gone; the fingerprint cannot be "
            "recomputed, so acceptance cannot be proven",
            "worktree_missing",
            subject_sha=subject_sha,
        )
    existing = _read_check_record(store, group["group_id"], subject_sha)
    if existing and all(row.get("status") == "PASS" for row in existing["checks"]):
        _apply_checks(
            store, group, subject_sha, existing["checks"], existing["workspace_fingerprint"]
        )
        return existing

    rows = run_checks(store, group, Path(worktree), subject_sha)
    record = {
        "schema_version": 1,
        "group_id": group["group_id"],
        "subject_sha": subject_sha,
        "design_digest": group["design_digest"],
        "plan_revision": group["revision"],
        "policy_revision": digest(store.config(group["project_id"]))[:16],
        "checks": rows,
        "workspace_fingerprint": {
            "manifest_hash": manifest_hash(worktree),
            "head_sha": _ref_sha(worktree, "HEAD"),
        },
        "generated_at": time.time(),
    }
    path = checks_dir(store, group["group_id"]) / (subject_sha + ".json")
    atomic(path, record)
    _apply_checks(store, group, subject_sha, rows, record["workspace_fingerprint"])
    return record


def _apply_checks(store: Store, group: dict, subject_sha, rows, fingerprint_record) -> None:
    """Mirror the on-disk record into the group record and persist it.

    The file is authoritative (it is what `accept` re-reads); the runtime copy
    exists so ``group status`` can render without a second filesystem walk.
    """
    runtime = group["runtime"]
    runtime["checks"] = [{**row, "subject_sha": subject_sha} for row in rows]
    runtime["workspace_fingerprint"] = fingerprint_record
    runtime["checks_subject_sha"] = subject_sha
    with store.tx():
        store.save_group(group, "group_checks_recorded")


def _read_check_record(store: Store, group_id: str, subject_sha: str) -> dict | None:
    path = checks_dir(store, group_id) / (subject_sha + ".json")
    if not path.is_file():
        return None
    try:
        return read(path)
    except (OSError, ValueError):
        return None


def checks_are_current(store: Store, group: dict) -> bool:
    """Whether every required group check passed **on the current SHA**."""
    runtime = group["runtime"]
    subject = runtime.get("integration_sha")
    if not subject:
        return False
    record = _read_check_record(store, group["group_id"], subject)
    if not record or record.get("subject_sha") != subject:
        return False
    required = {item["check_id"] for item in group["definition"]["group_checks"]}
    passed = {row["check_id"] for row in record.get("checks") or [] if row.get("status") == "PASS"}
    return required <= passed


# --------------------------------------------------------------------------- #
# acceptance                                                             (9.4)
# --------------------------------------------------------------------------- #


def _acceptance_gates(store: Store, group: dict, req: dict) -> list[str]:
    """Every unmet condition, so one call reports all of them (TGD §9.4)."""
    unmet = []
    gid = group["group_id"]
    runtime = group["runtime"]
    # §9.4: only `local_artifact` groups can be accepted. Creation refuses
    # `pull_request` already, so this is the second door — the one that a legacy
    # record or a hand-edited row would otherwise walk through.
    mode = group.get("delivery_mode") or group["definition"].get("delivery_mode")
    if mode not in (None, "local_artifact"):
        unmet.append(f"delivery_mode is {mode!r}; only local_artifact groups can be accepted")
    if group["state"] != "REVIEW_READY":
        unmet.append(f"group state is {group['state']}, not REVIEW_READY")
    repo = Path(store.config(group["project_id"])["repo"])
    branch = group["definition"]["integration_branch"]
    observed = _ref_sha(repo, "refs/heads/" + branch)
    integration_sha = runtime.get("integration_sha")
    if observed != integration_sha:
        unmet.append(
            "integration branch {} is at {}, the record says {}".format(
                branch, (observed or "absent")[:12], (integration_sha or "none")[:12]
            )
        )
    worktree = runtime.get("integration_worktree")
    if not worktree or not Path(worktree).is_dir():
        unmet.append("integration worktree is missing")
    else:
        head = _ref_sha(worktree, "HEAD")
        if head != integration_sha:
            unmet.append(
                f"integration worktree HEAD {str(head)[:12]} != {str(integration_sha)[:12]}"
            )
        record = _read_check_record(store, gid, integration_sha or "")
        if not record:
            unmet.append("no group check record for the current integration_sha")
        else:
            expected = (record.get("workspace_fingerprint") or {}).get("manifest_hash")
            actual = manifest_hash(worktree)
            if expected != actual:
                unmet.append("integration worktree fingerprint changed since the checks ran")
            required = {item["check_id"] for item in group["definition"]["group_checks"]}
            passed = {
                row["check_id"] for row in record.get("checks") or [] if row.get("status") == "PASS"
            }
            missing = sorted(required - passed)
            if missing:
                unmet.append("group checks not passing on this SHA: " + ", ".join(missing))
    if not checks_are_current(store, group):
        unmet.append("recorded check results are stale or incomplete")
    # 设计一致性审查（§11.3）。计划没声明时不伪装成"审查通过"，而是如实说明
    # 这一层未参与验收——交付报告里必须能看到这个边界。
    review = _review.summary(store, group)
    if review.get("required") and not review.get("satisfied"):
        unmet.append("design review ({}): {}".format(review.get("state"), review.get("reason")))
    if runtime.get("blockers"):
        unmet.append("group still reports blockers")
    refs = req.get("evidence_refs") or []
    if not isinstance(refs, list) or not refs:
        unmet.append("request carries no evidence_refs")
    unmet.extend(_evidence_attribution_gaps(store, group, refs, integration_sha))
    if not req.get("actor"):
        unmet.append("request has no actor")
    if not req.get("authorization_ref"):
        unmet.append("request has no authorization_ref")
    return unmet


def _evidence_attribution_gaps(store, group, refs, integration_sha) -> list[str]:
    """Every cited evidence ref must exist, belong to this group, and match the SHA.

    Two accepted forms:

    * ``group:<integration_sha>`` — the group check record for the final SHA;
    * ``task:<task_id>`` — a member of this group whose ``integrated_sha`` is the
      final integration SHA **or an ancestor of it**, and whose evidence file is
      present and intact.

    The ancestor form is required by §8.2: serial integration gives every member
    its own ``integrated_sha`` (the merge that carried it in), and only the last
    one equals ``integration_sha``. Demanding equality would make every member's
    evidence uncitable in a group with more than one member — the check would be
    strict about a detail rather than about attribution.

    Anything else is refused by name rather than being silently tolerated: an
    unciteable reference is the same defect as a missing one.
    """
    gaps = []
    integrated = {
        item["task_id"]: item.get("integrated_sha")
        for item in _groups.all_runtimes(group)
        if item.get("task_id")
    }
    repo = Path(store.config(group["project_id"])["repo"])
    for ref in refs:
        if not isinstance(ref, str) or not ref:
            gaps.append("evidence_refs contains a non-string entry")
            continue
        kind, _, value = ref.partition(":")
        if kind == "group":
            if value != integration_sha:
                gaps.append(f"group evidence {ref} is not for the current integration_sha")
                continue
            path = checks_dir(store, group["group_id"]) / (value + ".json")
            if not path.is_file():
                gaps.append(f"group evidence {ref} file is missing")
            continue
        if kind == "task":
            if value not in integrated:
                gaps.append(f"task evidence {ref} does not belong to this group")
                continue
            member_sha = integrated[value]
            if member_sha != integration_sha and not is_ancestor(repo, member_sha, integration_sha):
                gaps.append(f"task evidence {ref} is not part of the final integration")
                continue
            try:
                task = store.task(value)
            except Error:
                gaps.append(f"task evidence {ref} refers to an unknown task")
                continue
            if not task.get("current_evidence_id"):
                gaps.append(f"task evidence {ref} has no recorded evidence")
                continue
            path = store.path(task) / "evidence" / (task["current_evidence_id"] + ".json")
            if not path.is_file():
                gaps.append(f"task evidence {ref} file is missing")
            continue
        gaps.append(
            f"evidence {ref!r} has an unknown reference form "
            "(expected group:<subject_sha> or task:<task_id>)"
        )
    return gaps


def accept(store: Store, group_id: str, req: dict[str, Any]) -> dict[str, Any]:
    """Record a human acceptance decision for a ``local_artifact`` group."""
    with store.lock():
        key, prior = store.operation(req, "group_accept", group_id)
        if prior:
            return prior
        try:
            group = store.group(group_id)
            _groups.require_v3(group, "accept")
            config = store.config(group["project_id"])
            authorize(config, req, "accept")
            require(
                req.get("decision") in ("accepted", "rejected"),
                "INPUT_INCOMPLETE",
                "decision must be 'accepted' or 'rejected'",
            )
            require(
                group["row_version"] == req.get("expected_version"),
                "STATE_CONFLICT",
                "Refresh expected_version",
            )
            record = {
                "decision": req["decision"],
                "actor": req.get("actor"),
                "reason": req.get("reason"),
                "authorization_ref": req.get("authorization_ref"),
                "evidence_refs": list(req.get("evidence_refs") or []),
                "subject_sha": group["runtime"].get("integration_sha"),
                "at": time.time(),
            }
            if req["decision"] == "rejected":
                # A rejection is a record, not a state change: the group stays
                # REVIEW_READY so the fix + re-verify loop is described explicitly.
                group["runtime"]["acceptance"] = record
                group["runtime"]["rejections"] = list(group["runtime"].get("rejections") or []) + [
                    record
                ]
                result = {
                    "api_version": 2,
                    "operation_id": req.get("request_id"),
                    "status": "succeeded",
                    "accepted": False,
                    "completed": True,
                    "group_id": group_id,
                    "state": group["state"],
                    **_groups.envelope(store, group),
                    "evidence_refs": list(record.get("evidence_refs") or []),
                    "decision": "rejected",
                    "row_version": group["row_version"] + 1,
                    "next_actions": [
                        "fix the group (new follow-up group or member rebind), then re-integrate",
                        f"group cancel {group_id} --request-file cancel.json",
                    ],
                }
                with store.tx():
                    store.save_group(group, "group_acceptance_rejected")
                store.finish(key, result)
                return result

            unmet = _acceptance_gates(store, group, req)
            if unmet:
                detail = {"kind": "acceptance_unmet", "unmet": unmet}
                # A stale result is a distinct cause, not "some check missing":
                # the host must re-run checks for the current integration_sha.
                stale = _groups.stale_check_ids(group)
                if stale:
                    detail["kind"] = "stale_checks"
                    detail["check_ids"] = stale
                raise Error(
                    "EVIDENCE_INVALID",
                    "acceptance refused; unmet condition(s): " + "; ".join(unmet),
                    details=detail,
                )
            group["runtime"]["acceptance"] = record
            group["state"] = "DONE"
            group["runtime"]["budget"]["finalized_at"] = time.time()
            review = _review.summary(store, group)
            result = {
                "api_version": 2,
                "operation_id": req.get("request_id"),
                "status": "succeeded",
                "accepted": True,
                "completed": True,
                "group_id": group_id,
                "state": "DONE",
                **_groups.envelope(store, group),
                "evidence_refs": list(record.get("evidence_refs") or []),
                "decision": "accepted",
                "integration_sha": group["runtime"]["integration_sha"],
                "row_version": group["row_version"] + 1,
                "acceptance": record,
                "review": review,
                "boundary": [
                    "design conformance review: {}".format(review["state"]),
                    "no sandbox: local processes run as this user",
                    "member tasks stay REVIEW_READY; they are closed by the group",
                ],
            }
            with store.tx():
                store.save_group(group, "group_accepted")
            store.finish(key, result)
            return result
        except Exception as exc:
            store.finish(
                key,
                {
                    "status": "failed",
                    "error": {
                        "code": getattr(exc, "code", "INTERNAL_ERROR"),
                        "message": str(exc),
                    },
                },
            )
            raise


def close_external(store: Store, group_id: str, req: dict[str, Any]) -> dict[str, Any]:
    """``group cancel --external``: record a delivery Zoe never verified."""
    with store.lock():
        key, prior = store.operation(req, "group_close_external", group_id)
        if prior:
            return prior
        try:
            group = store.group(group_id)
            _groups.require_v3(group, "cancel")
            authorize(store.config(group["project_id"]), req, "execute")
            require(
                req.get("claim") in ("verified", "asserted"),
                "INPUT_INCOMPLETE",
                "claim must be 'verified' (checked by a human) or 'asserted'",
            )
            for field in ("external_ref", "actor"):
                require(req.get(field), "INPUT_INCOMPLETE", f"{field} required")
            runtime = group["runtime"]
            require(
                not runtime.get("lease") or runtime["lease"].get("state") != "spawn_observed",
                "STATE_CONFLICT",
                "a runner is still observed; stop it before closing the group",
            )
            record = {
                "reason": req.get("reason"),
                "external_ref": req["external_ref"],
                "merged_sha": req.get("merged_sha"),
                "actor": req["actor"],
                "claim": req["claim"],
                "at": time.time(),
                "note": "closed outside Zoe; not accepted",
            }
            runtime["external_close"] = record
            group["state"] = "CANCELLED"
            group["runtime"]["budget"]["finalized_at"] = time.time()
            group["runtime"]["blockers"] = [
                {"code": "CLOSED_EXTERNAL", "external_ref": record["external_ref"]}
            ]
            result = {
                "api_version": 2,
                "operation_id": req.get("request_id"),
                "status": "succeeded",
                "accepted": False,
                "completed": True,
                "group_id": group_id,
                "state": "CANCELLED",
                **_groups.envelope(store, group),
                "evidence_refs": list(req.get("evidence_refs") or []),
                "external_close": record,
                "row_version": group["row_version"] + 1,
            }
            with store.tx():
                store.save_group(group, "group_closed_external")
            store.finish(key, result)
            return result
        except Exception as exc:
            store.finish(
                key,
                {
                    "status": "failed",
                    "error": {
                        "code": getattr(exc, "code", "INTERNAL_ERROR"),
                        "message": str(exc),
                    },
                },
            )
            raise


# --------------------------------------------------------------------------- #
# report                                                                 (9.6)
# --------------------------------------------------------------------------- #


def report(store: Store, group_id: str) -> dict:
    group = store.group(group_id)
    _groups.require_v3(group, "report")
    views = _groups.members_view(store, group)
    runtime = group["runtime"]
    subject = runtime.get("integration_sha")
    check_record = _read_check_record(store, group_id, subject) if subject else None
    rows = members_rows(store, group, views)
    return {
        "api_version": 2,
        "group_id": group_id,
        "project_id": group["project_id"],
        "title": group["definition"].get("title") or group_id,
        "state": group["state"],
        "revision": group["revision"],
        "plan_digest": group["plan_digest"],
        "design_digest": group["design_digest"],
        "base_sha": group["base_sha"],
        "integration_sha": subject,
        "delivery_mode": group["delivery_mode"],
        "requirements": group["definition"]["requirements"],
        "members": rows,
        "group_checks": (check_record or {}).get("checks") or [],
        "checks_subject_sha": subject,
        "checks_fresh": checks_are_current(store, group),
        "workspace_fingerprint": (check_record or {}).get("workspace_fingerprint"),
        "progress": _groups.progress(store, group),
        "review": _review.summary(store, group),
        "repairs": [
            {
                "task_key": item["task_key"],
                "title": item.get("title"),
                "parent_acceptance_id": item.get("parent_acceptance_id"),
                "affected_members": list(item.get("affected_members") or []),
                "task_id": item.get("task_id"),
                "integrated_sha": item.get("integrated_sha"),
                "created_at": item.get("created_at"),
            }
            for item in _groups.repair_units(group)
        ],
        "budget_caps": group["definition"]["budget_caps"],
        "blockers": runtime.get("blockers") or [],
        "acceptance": runtime.get("acceptance"),
        "rejections": runtime.get("rejections") or [],
        "external_close": runtime.get("external_close"),
        "generated_at": time.time(),
    }


def members_rows(store: Store, group: dict, views: list[dict]) -> list[dict]:
    rows = []
    for view in views:
        task = store.task(view["task_id"]) if view.get("task_id") else None
        run = (
            store.get_run(task.get("current_run_id"))
            if task and task.get("current_run_id")
            else None
        )
        rows.append(
            {
                **view,
                "profile_id": (task or {}).get("agent_profile_id"),
                "combination_id": (task or {}).get("combination_id"),
                "cli_version": (run or {}).get("cli_version")
                or ((task or {}).get("selected_execution") or {}).get("cli_version_pin"),
                "auth_mode": (
                    ((task or {}).get("config") or {})
                    .get("agent_profiles", {})
                    .get((task or {}).get("agent_profile_id"), {})
                    .get("auth_mode", "unknown")
                ),
            }
        )
    return rows


def _render_review(review: dict) -> list[str]:
    """设计一致性审查的逐条偏差（§11.2）。没有结论时如实说明是哪一种"没有"。"""
    lines = ["### 设计一致性审查（逐条偏差）", ""]
    if not review:
        return lines + ["- 尚无审查记录。", ""]
    lines.append("- 状态：**{}**".format(review.get("state")))
    lines.append("- 是否参与验收：{}".format("是" if review.get("required") else "否"))
    if review.get("reason"):
        lines.append("- 说明：{}".format(review["reason"]))
    counts = review.get("counts") or {}
    if counts.get("PASS") or counts.get("FAIL") or counts.get("NOT_VERIFIED"):
        lines.append(
            "- 计数：PASS {} ｜ FAIL {} ｜ NOT_VERIFIED {}".format(
                counts.get("PASS", 0), counts.get("FAIL", 0), counts.get("NOT_VERIFIED", 0)
            )
        )
    reviewer = review.get("reviewer") or {}
    if reviewer:
        lines.append(
            "- 审查者：{} ｜ actor {} ｜ 独立上下文 {}".format(
                reviewer.get("kind") or "unknown",
                reviewer.get("actor") or "-",
                "是" if reviewer.get("independent_context") else "未声明",
            )
        )
    items = review.get("items") or []
    if items:
        lines += ["", "| AC | 结论 | 说明 | 位置 | 证据 |", "|---|---|---|---|---|"]
        for item in items:
            lines.append(
                "| {} | {} | {} | {} | {} |".format(
                    item.get("acceptance_id"),
                    item.get("status"),
                    (item.get("finding") or "—").replace("|", "\\|"),
                    ", ".join(item.get("locations") or []) or "—",
                    ", ".join(item.get("evidence_refs") or []) or "—",
                )
            )
    unresolved = review.get("unresolved") or []
    if unresolved:
        lines.append("")
        lines.append(
            "- **未解决阻断项**："
            + "；".join(
                "{} {}".format(item["acceptance_id"], item.get("suggested_action") or "").strip()
                for item in unresolved
            )
        )
    not_verified = review.get("not_verified") or []
    if not_verified:
        lines.append("")
        lines.append(
            "- **未验证项（不计入通过）**："
            + ", ".join(item["acceptance_id"] for item in not_verified)
        )
    if review.get("path"):
        lines.append("- 结论文件：`{}`".format(review["path"]))
    lines.append("")
    return lines


def render_report(snap: dict) -> str:
    """Seven sections, in the order fixed by TGD §9.6."""
    lines = ["# {}（任务组交付报告）".format(snap.get("title") or snap["group_id"]), ""]
    lines += [
        "- Group: {} ｜ State: {} ｜ Revision: {}".format(
            snap["group_id"], snap["state"], snap["revision"]
        ),
        "- Plan digest: {}".format(snap["plan_digest"]),
        "- Design digest: {}".format(snap["design_digest"]),
        "- Base → Integration: {} → {}".format(
            (snap["base_sha"] or "")[:12], (snap["integration_sha"] or "none")[:12]
        ),
        "- 设计一致性审查：**{}**｜参与验收：{}".format(
            (snap.get("review") or {}).get("state"),
            "是" if (snap.get("review") or {}).get("required") else "否",
        ),
        "",
    ]

    lines += ["## 1. 需求覆盖（结构覆盖，非语义验证）", ""]
    ownership: dict[str, list[dict]] = {}
    for member in snap["members"]:
        for rid in member["requirement_ids"]:
            ownership.setdefault(rid, []).append(member)
    lines += ["| REQ | 成员 | AC | 成员状态 |", "|---|---|---|---|"]
    for item in snap["requirements"]:
        owners = ownership.get(item["id"]) or []
        if not owners:
            lines.append("| {} | （无） | — | 覆盖缺失 |".format(item["id"]))
            continue
        for member in owners:
            lines.append(
                "| {} | {} | {} | {} |".format(
                    item["id"],
                    member["task_key"],
                    ", ".join(member["acceptance_ids"]) or "—",
                    member["state"],
                )
            )
    lines.append("")
    lines.append(
        "> 结构覆盖已通过不等于需求已满足；语义判断只来自 check 型 AC 的实际执行，"
        "以及下面这一节的设计一致性审查——它绑定的版本必须就是本次集成版本。"
    )
    lines.append("")
    lines += _render_review(snap.get("review") or {})
    lines.append("")

    lines += ["## 2. 成员与执行组合", ""]
    lines += [
        "| 成员 | 状态 | profile | combination | CLI | auth_mode |",
        "|---|---|---|---|---|---|",
    ]
    for member in snap["members"]:
        lines.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                member["task_key"],
                member["state"],
                member.get("profile_id") or "-",
                member.get("combination_id") or "-",
                member.get("cli_version") or "-",
                member.get("auth_mode") or "unknown",
            )
        )
    repairs = snap.get("repairs") or []
    if repairs:
        lines.append("")
        lines.append("组级修复单元（限定在一条 finding 与已批准范围内）：")
        for repair in repairs:
            lines.append(
                "- {} ← {} ｜ 影响成员 {} ｜ task {} ｜ integrated {}".format(
                    repair.get("task_key"),
                    repair.get("parent_acceptance_id") or "-",
                    ", ".join(repair.get("affected_members") or []) or "—",
                    repair.get("task_id") or "—",
                    (repair.get("integrated_sha") or "—")[:12],
                )
            )
    lines.append("")

    lines += ["## 3. SHA 链", ""]
    lines.append("- base: `{}`".format(snap["base_sha"]))
    for member in snap["members"]:
        lines.append(
            "- {} validated=`{}` integrated=`{}`".format(
                member["task_key"],
                (member.get("validated_sha") or "none")[:12],
                (member.get("integrated_sha") or "none")[:12],
            )
        )
    lines.append("- integration: `{}`".format(snap["integration_sha"] or "none"))
    lines.append("")

    lines += ["## 4. 组级检查明细", ""]
    if not snap["group_checks"]:
        lines.append(
            "- 尚无组级检查记录（未在 `{}` 上执行）".format((snap["integration_sha"] or "—")[:12])
        )
    else:
        lines += ["| check | 状态 | exit | 日志 |", "|---|---|---|---|"]
        for row in snap["group_checks"]:
            lines.append(
                "| {} | {} | {} | {} |".format(
                    row.get("check_id"),
                    row.get("status"),
                    "—" if row.get("exit_code") is None else row.get("exit_code"),
                    row.get("log_ref") or "—",
                )
            )
        if not snap["checks_fresh"]:
            lines.append("")
            lines.append(
                "- 注意：当前 `integration_sha` 上没有全部必需检查通过，`NOT_RUN`/`stale` "
                "不计入成功。"
            )
    lines.append("")

    lines += ["## 5. 阻塞历史与最小必要决策", ""]
    if not snap["blockers"]:
        lines.append("- （空）")
    for blocker in snap["blockers"]:
        lines.append(
            "- {}：{}".format(blocker.get("member") or "group", blocker.get("code") or "UNKNOWN")
            + ("（{}）".format(blocker["reason"]) if blocker.get("reason") else "")
        )
    for rejection in snap["rejections"]:
        lines.append(
            "- 验收被拒绝：{}（{}）".format(
                rejection.get("reason") or "无原因", rejection.get("actor")
            )
        )
    lines.append("")

    progress = snap["progress"]
    caps = snap.get("budget_caps") or {}
    lines += ["## 6. 预算与时间", ""]
    lines += [
        "- 计费启动额度：{}/{}（已预留 {}，已释放 {}）".format(
            progress["starts_billable"],
            progress["max_starts"],
            progress["starts_reserved"],
            progress["starts_released"],
        ),
        "- 组级修复轮数：{}/{}（上限由计划 `budget.max_group_repairs` 给出，"
        "缺省为 0 即不授权修复执行）".format(
            progress.get("repairs_total", 0), caps.get("max_group_repairs", 0)
        ),
        "- 实际 Agent 进程启动数：以每个 run 的 `pid`/`launch_stage` 为证据，"
        "与上面的计费额度不是同一口径",
        "- 墙钟时长（含机器休眠与等待）：{}".format(
            "未设时长上限"
            if progress["max_wall_seconds"] is None
            else "{}s / {}s".format(
                progress["wall_seconds_inclusive"] or 0, progress["max_wall_seconds"]
            )
        ),
        "",
    ]

    review = snap.get("review") or {}
    lines += ["## 7. 交付边界声明", ""]
    lines += [
        "- 无沙箱：`local-process` 以本机账户运行，`allowed_paths` 提供问责而非预防。",
        "- 设计一致性审查：{}（{}）".format(review.get("state"), review.get("reason") or "—"),
        "- PR 组不支持：`delivery_mode=pull_request` 在创建时即被拒绝。",
    ]
    if snap.get("external_close"):
        close = snap["external_close"]
        lines.append(
            "- 外部收口（未通过 Zoe 验收）：{} · {} · claim={}".format(
                close.get("external_ref"), close.get("reason"), close.get("claim")
            )
        )
    not_run = [row["check_id"] for row in snap["group_checks"] if row.get("status") == "NOT_RUN"]
    if not_run:
        lines.append("- 未运行项：{}".format(", ".join(not_run)))
    lines.append("- 成员任务保持 REVIEW_READY；它们由组统一收口，`task accept` 会拒绝。")
    lines.append("")
    return "\n".join(lines)


def persist_report(store: Store, group: dict, snap: dict, text: str) -> dict:
    from src._util import atomic_text

    path = _groups.group_dir(store, group["group_id"]) / "reports" / "delivery.md"
    atomic_text(path, text)
    return {
        "api_version": 2,
        "group_id": group["group_id"],
        "report_path": str(path),
        "generated_at": snap["generated_at"],
    }


def cleanup_notes(group: dict) -> list[str]:
    """Leftover worktrees are **reported**, never silently discarded (TGD §8.2)."""
    runtime = group["runtime"]
    notes = [
        f"open integration operation {op_id}" for op_id in runtime.get("open_operations") or []
    ]
    worktree = runtime.get("integration_worktree")
    if worktree and os.path.isdir(worktree):
        notes.append(f"integration worktree kept: {worktree}")
    return notes
