"""Pre-publication drift classification (v0.5 §7).

``REMOTE_DRIFT`` and ``EXTRA_COMMIT`` were registered in the exit-code map but
never implemented. This module is that implementation, with one deliberate
constraint: the three drift kinds share the existing ``REMOTE_DRIFT`` code and
are distinguished by a structured ``drift_kind`` in ``error.details``. No new
error code is introduced.

The second constraint is structural, not stylistic: :func:`probe` **never
raises**. If checking the remote threw, the caller's "persist the evidence, then
block" ordering would silently stop working — the throw would happen before the
first line that writes ``publication.json``. Returning a classification and
letting the caller decide makes "evidence always exists" a property of the
control flow rather than of the caller's discipline.

A third: ``remote_unknown`` (could not confirm) is kept strictly apart from the
three confirmed drifts. "We could not check" is not "nothing changed" and it is
not "something changed".
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from src._util import Error

DRIFT_NONE = "none"
DRIFT_BASE_ADVANCED = "base_advanced"
DRIFT_BASE_REWRITTEN = "base_rewritten"
DRIFT_CANDIDATE_DIVERGED = "candidate_diverged"
DRIFT_REMOTE_UNKNOWN = "remote_unknown"

#: Kinds that block publication with ``REMOTE_DRIFT`` (exit 4).
DRIFT_KINDS = (DRIFT_BASE_ADVANCED, DRIFT_BASE_REWRITTEN, DRIFT_CANDIDATE_DIVERGED)

REASON_TASK_REF_MISSING = "task_ref_missing"

#: Hard bounds: a hung remote must not hang the supervisor.
FETCH_TIMEOUT_SECONDS = 60
REMOTE_QUERY_TIMEOUT_SECONDS = 30

_MESSAGES = {
    DRIFT_BASE_ADVANCED: "目标分支已前进，当前交付需要重新处理基线。",
    DRIFT_BASE_REWRITTEN: "目标分支历史已被重写，任务基线不再成立。",
    DRIFT_CANDIDATE_DIVERGED: "任务分支在远端已变化，控制器不会覆盖或强推。",
    DRIFT_REMOTE_UNKNOWN: "无法确认远端状态，不能当作无漂移继续发布。",
}


def _git_read(repo, *args, timeout: float = REMOTE_QUERY_TIMEOUT_SECONDS):
    """Run a read-only git command and return ``(code, stdout, stderr)``.

    Never raises: an unobservable remote is a classification, not an exception.
    ``code`` is ``None`` when the command could not run at all.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "", ""
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _result(kind, reason=None, **kwargs):
    value = {
        "kind": kind,
        "reason": reason,
        "base_sha": None,
        "observed_base_sha": None,
        "candidate_sha": None,
        "observed_task_sha": None,
        "checked_at": time.time(),
    }
    value.update(kwargs)
    return value


def _publication_history(store, t) -> bool:
    """Whether this task ever ran ``Publisher.publish`` for this state."""
    if t.get("published_sha"):
        return True
    try:
        return (store.path(t) / "publication.json").is_file()
    except OSError:
        return False


def probe(store, t, c) -> dict:
    """Classify the remote state relative to the frozen baseline.

    Returns ``{"kind", "reason", "base_sha", "observed_base_sha",
    "candidate_sha", "observed_task_sha", "checked_at"}`` and never raises.
    """
    try:
        return _probe(store, t, c)
    except Exception:  # noqa: BLE001 - structural guarantee, see module docstring
        return _result(
            DRIFT_REMOTE_UNKNOWN,
            "git_error",
            base_sha=t.get("base_sha"),
            candidate_sha=t.get("candidate_sha"),
        )


def _probe(store, t, c) -> dict:
    base_sha = t.get("base_sha")
    candidate_sha = t.get("candidate_sha")
    branch = t.get("branch")
    remote = c.get("remote", "origin")
    target = c.get("target_branch", "main")

    # A remote name or ref that looks like an option is a configuration error,
    # not something to pass to git and hope for the best.
    if (
        not base_sha
        or not branch
        or not isinstance(remote, str)
        or remote.startswith("-")
        or not isinstance(target, str)
        or target.startswith("-")
    ):
        return _result(
            DRIFT_REMOTE_UNKNOWN,
            "git_error",
            base_sha=base_sha,
            candidate_sha=candidate_sha,
        )

    workspace = t.get("workspace")
    repo = Path(workspace) if workspace and Path(workspace).exists() else Path(c["repo"])

    # 1. Scoped fetch. Only the ref we need, and read through FETCH_HEAD: a
    #    global `fetch --prune` would rewrite remote-tracking refs and change the
    #    repository's visible state for everyone else using it.
    code, _, _ = _git_read(
        repo, "fetch", remote, "refs/heads/" + target, timeout=FETCH_TIMEOUT_SECONDS
    )
    if code != 0:
        return _result(
            DRIFT_REMOTE_UNKNOWN,
            "fetch_failed",
            base_sha=base_sha,
            candidate_sha=candidate_sha,
        )

    # 2. Baseline comparison.
    code, out, _ = _git_read(repo, "rev-parse", "FETCH_HEAD")
    observed_base = out.strip().splitlines()[-1].strip() if out.strip() else None
    if code != 0 or not observed_base:
        return _result(
            DRIFT_REMOTE_UNKNOWN,
            "git_error",
            base_sha=base_sha,
            candidate_sha=candidate_sha,
        )

    if observed_base != base_sha:
        code, _, _ = _git_read(repo, "merge-base", "--is-ancestor", base_sha, observed_base)
        if code == 0:
            # Distinguishable: the baseline is an ancestor, i.e. the target moved
            # forward and the previous verification may now be stale.
            return _result(
                DRIFT_BASE_ADVANCED,
                None,
                base_sha=base_sha,
                observed_base_sha=observed_base,
                candidate_sha=candidate_sha,
            )
        if code == 1:
            # Exit 1 means "not an ancestor". Anything else is a git failure and
            # must never be reported as "history was rewritten".
            return _result(
                DRIFT_BASE_REWRITTEN,
                None,
                base_sha=base_sha,
                observed_base_sha=observed_base,
                candidate_sha=candidate_sha,
            )
        return _result(
            DRIFT_REMOTE_UNKNOWN,
            "git_error",
            base_sha=base_sha,
            observed_base_sha=observed_base,
            candidate_sha=candidate_sha,
        )

    # 3. Task branch comparison.
    code, out, _ = _git_read(repo, "ls-remote", remote, "refs/heads/" + branch)
    if code != 0:
        return _result(
            DRIFT_REMOTE_UNKNOWN,
            "ls_remote_failed",
            base_sha=base_sha,
            observed_base_sha=observed_base,
            candidate_sha=candidate_sha,
        )
    tokens = out.split()
    observed_task = tokens[0] if tokens else None

    if observed_task is None:
        if _publication_history(store, t):
            # The branch this task already published to is gone. Recreating it
            # silently would "repair" an external deletion into a new branch, so
            # this is a blocking divergence with an explicit reason.
            return _result(
                DRIFT_CANDIDATE_DIVERGED,
                REASON_TASK_REF_MISSING,
                base_sha=base_sha,
                observed_base_sha=observed_base,
                candidate_sha=candidate_sha,
            )
        return _result(
            DRIFT_NONE,
            None,
            base_sha=base_sha,
            observed_base_sha=observed_base,
            candidate_sha=candidate_sha,
        )

    if observed_task in (candidate_sha, t.get("published_sha")):
        return _result(
            DRIFT_NONE,
            None,
            base_sha=base_sha,
            observed_base_sha=observed_base,
            candidate_sha=candidate_sha,
            observed_task_sha=observed_task,
        )
    return _result(
        DRIFT_CANDIDATE_DIVERGED,
        None,
        base_sha=base_sha,
        observed_base_sha=observed_base,
        candidate_sha=candidate_sha,
        observed_task_sha=observed_task,
    )


def error_code(result: dict) -> str:
    """Map a probe result onto the existing error-code table.

    Anything that is not a confirmed drift maps to ``REMOTE_UNKNOWN``: this
    helper fails closed, so a new kind added without thought cannot be reported
    as "probably fine".
    """
    if result.get("kind") in DRIFT_KINDS:
        return "REMOTE_DRIFT"
    return "REMOTE_UNKNOWN"


def message(result: dict) -> str:
    return _MESSAGES.get(result.get("kind"), _MESSAGES[DRIFT_REMOTE_UNKNOWN])


def details(result: dict, t: dict) -> dict:
    """Structured ``error.details`` for the CLI response (§11.3).

    Same shape for every kind: a host that only needs "why" reads
    ``details.drift_kind``, and the two verdicts that share one error code
    (``REMOTE_DRIFT``) stay distinguishable without parsing the message.
    """
    return {
        "drift_kind": result.get("kind"),
        "task_id": t.get("task_id"),
        "base_sha": result.get("base_sha"),
        "observed_base_sha": result.get("observed_base_sha"),
        "candidate_sha": result.get("candidate_sha"),
        "observed_task_sha": result.get("observed_task_sha"),
        "evidence_ref": "publication.json",
        "reason": result.get("reason"),
    }


def error(result: dict, t: dict) -> Error:
    """Build the blocking error carrying the same probe result the evidence has."""
    return Error(error_code(result), message(result), details=details(result, t))
