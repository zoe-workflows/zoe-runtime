"""Project context bundle: rebuild the host's working material from one project.

Every read/write binds a project explicitly; the context bundle is the single
entry point for a host switching projects. It is rebuilt from persistent
materials only — never from chat history — and is bounded (default 32 KiB) with
priority-based truncation (10 §4).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from src import _orchestration, _summaries
from src._store import Store
from src._util import Error, require, safe_id

CONTEXT_LIMIT_BYTES = 32 * 1024


def _repo_identity(c: dict) -> dict:
    repo = Path(c["repo"])
    common = repo / ".git"
    if common.is_file():
        # gitdir file (worktree/submodule): read the actual common dir.
        try:
            target = common.read_text().strip().split(":", 1)[1]
            common = Path(target)
        except (IndexError, OSError):
            common = repo / ".git"
    remote = c.get("remote", "origin")
    return {"root": c["repo"], "common_dir": str(common), "remote": remote}


def build(store: Store, project_id: str, task_id: str | None = None) -> dict:
    """Build a bounded context bundle for ``project_id`` (optionally a task)."""
    safe_id(project_id)
    c = store.config(project_id)
    record = _orchestration.show(store, project_id)

    task = None
    spec = None
    pending = list(record.get("pending_questions") or [])
    summary = None
    missing: list[str] = []

    if task_id:
        try:
            task = store.task(task_id)
        except Error:
            missing.append("task")
        if task is not None:
            require(
                task["project_id"] == project_id,
                "PROJECT_MISMATCH",
                "task_id does not belong to this project",
            )
            spec_rev = task.get("spec_revision", 1)
            spec_path = store.path(task) / f"spec-{spec_rev}.json"
            if spec_path.is_file():
                try:
                    json.loads(spec_path.read_text())  # validate parseability only
                    spec = {
                        "revision": spec_rev,
                        "ref": f"spec-{spec_rev}.json",
                        "acceptance_digest": task.get("spec_hash"),
                    }
                except (OSError, json.JSONDecodeError):
                    missing.append("spec")
            else:
                missing.append("spec")
            # Recent active summary.
            try:
                index = _summaries.list_summaries(store, task_id)
                active = [e for e in index.get("entries", []) if e.get("status") == "active"]
                if active:
                    latest = max(active, key=lambda e: e["version"])
                    summary = {
                        "version": latest["version"],
                        "status": "active",
                        "through_event_id": latest["through_event_id"],
                    }
            except Error:
                summary = None
            # The Agent's own outstanding questions live on the task record, not
            # on the orchestration record; without this merge the context bundle
            # a host reloads after switching projects would not mention them.
            for question in task.get("pending_questions") or []:
                if question.get("answer"):
                    continue
                if any(q.get("question_id") == question.get("question_id") for q in pending):
                    continue
                pending.append(
                    {
                        "task_id": task["task_id"],
                        "question_id": question.get("question_id"),
                        "text": question.get("text"),
                    }
                )

    bundle = {
        "schema_version": 1,
        "project_id": project_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": _repo_identity(c),
        "goal": record.get("goal"),
        "spec": spec,
        "decisions": [
            d
            for d in (record.get("decisions") or [])
            if d.get("action") in ("business_decision", "user_decision")
        ],
        "task": (
            {
                "task_id": task["task_id"],
                "state": task["state"],
                "block_code": task.get("block_code"),
            }
            if task
            else None
        ),
        "pending_questions": pending,
        "summary": summary,
        "allowed_next": _allowed_next(record, task),
        "truncated": False,
        "missing": missing,
    }

    return _enforce_limit(bundle)


def _size(bundle: dict) -> int:
    return len(json.dumps(bundle, ensure_ascii=False).encode("utf-8", "replace"))


def _enforce_limit(bundle: dict) -> dict:
    """Truncate by priority until the bundle fits ``CONTEXT_LIMIT_BYTES`` (10 §4).

    Dropping decisions and the summary is not always enough — a task with many
    open questions is far over the limit on its own — so the remaining lists are
    halved until the bundle fits. Truncation is always flagged, never silent.
    """
    if _size(bundle) <= CONTEXT_LIMIT_BYTES:
        return bundle
    bundle["truncated"] = True
    bundle["decisions"] = []
    bundle["summary"] = None
    for key in ("pending_questions", "allowed_next"):
        while bundle.get(key) and _size(bundle) > CONTEXT_LIMIT_BYTES:
            bundle[key] = bundle[key][: max(1, len(bundle[key]) // 2)]
            if len(bundle[key]) == 1 and _size(bundle) > CONTEXT_LIMIT_BYTES:
                # One oversized entry is worse than no entry: drop it entirely.
                bundle[key] = []
                break
    return bundle


def _allowed_next(record: dict, task: dict | None) -> list[dict]:
    """What a host may do *next*, per state.

    ``BLOCKED`` used to produce only ``task answer`` for ``INPUT_REQUIRED`` and
    nothing at all otherwise — so the takeover path was silent exactly for the
    states that need an action (a timed-out or failed run). The list is
    deliberately **narrower** than "every command that parses": ``task resume``
    on ``PROCESS_UNKNOWN`` is refused by the runtime (the old writer may still
    be alive), so that state points at reconciliation instead of promising a
    resume that will fail.
    """
    actions = []
    if task is None:
        actions.append({"action": "task create"})
    else:
        state = task.get("state")
        block = task.get("block_code")
        if state == "READY":
            actions.append({"action": "task start"})
        elif state == "BLOCKED" and block == "INPUT_REQUIRED":
            # 等回答：`task resume` 会被 runtime 拒（STATE_CONFLICT），不要并列。
            actions.append({"action": "task answer"})
        elif state == "BLOCKED" and block == "PROCESS_UNKNOWN":
            # 旧执行不可观测：先对账收口，不提供 resume。
            actions.append({"action": "task recover"})
        elif state in ("PAUSED", "BLOCKED"):
            actions.append({"action": "task resume"})
        elif state == "REVIEW_READY":
            actions.append({"action": "task accept"})
    actions.append({"action": "orchestration update"})
    return actions
