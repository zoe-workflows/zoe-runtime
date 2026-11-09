"""Orchestration record: goal, dispatch, decisions, pending questions, ownership.

Answers "what did the user want, what was dispatched, why, who is being waited
on, and why the next step hasn't run" — distinct from execution state (which only
answers "where is the agent"). Stored one row per project in the v3
``orchestration`` table; version is a decision-level CAS (07 §2.1).
"""

from __future__ import annotations

import time
from typing import Any

from src import _quota
from src._store import Store
from src._util import Error, require, safe_id

#: Top-level keys a client may patch. ``decisions``/``budget``/``version`` are
#: controller-only and must never appear in a patch.
_PATCHABLE = {
    "goal",
    "authorization_scope",
    "dispatched",
    "not_dispatched",
    "pending_questions",
    "waiting_on",
    "next_action",
}

#: Patch value shapes. ``owner`` is deliberately absent: ownership is a claim
#: protocol (``claim`` / ``claim --force``), not an ordinary field, and letting a
#: patch write it produced a record whose ``owner`` was a string.
_PATCH_TYPES = {
    "goal": str,
    "authorization_scope": list,
    "dispatched": list,
    "not_dispatched": list,
    "pending_questions": list,
}

#: Budget counter keys. Hosts may never submit these (08 §3).
_BUDGET_COUNTERS = {
    "fix_rounds_used",
    "retries_used",
    "wall_clock_used",
    "runs_used",
}


def _empty(project_id: str) -> dict:
    return {
        "schema_version": 1,
        "project_id": project_id,
        "version": 0,
        "goal": "",
        "authorization_scope": [],
        "dispatched": [],
        "not_dispatched": [],
        "decisions": [],
        "pending_questions": [],
        "waiting_on": None,
        "next_action": None,
        "owner": None,
        "budget": {},
        "updated_at": None,
    }


def _validate_patch(patch: dict) -> None:
    require(isinstance(patch, dict), "INPUT_INCOMPLETE", "patch must be an object")
    # Budget counters are controller-owned (08 §3). Checked before the unknown
    # field test so a host that tries to steer the budget gets POLICY_DENIED
    # rather than a generic "unknown field" (13 §7 case 1).
    if "budget" in patch or any(k in patch for k in _BUDGET_COUNTERS):
        raise Error("POLICY_DENIED", "budget counters are controller-owned")
    if "owner" in patch:
        raise Error(
            "POLICY_DENIED",
            "ownership changes through 'orchestration claim' / 'claim --force', not a patch",
        )
    unknown = set(patch) - _PATCHABLE
    require(
        not unknown,
        "INPUT_INCOMPLETE",
        "Unknown orchestration fields: " + ", ".join(sorted(unknown)),
    )
    # 形状必须在写盘前校验。``owner: "host"`` 曾让此后每次 mutation 在
    # ``require_owner`` 里抛 AttributeError（INTERNAL_ERROR，无法归类）。
    for key, kind in _PATCH_TYPES.items():
        if key in patch:
            require(
                isinstance(patch[key], kind),
                "INPUT_INCOMPLETE",
                f"patch.{key} must be a {kind.__name__}, got {type(patch[key]).__name__}",
            )
    for key in ("waiting_on", "next_action"):
        if key in patch and patch[key] is not None:
            require(
                isinstance(patch[key], (str, dict)),
                "INPUT_INCOMPLETE",
                f"patch.{key} must be a string, an object or null",
            )


def show(store: Store, project_id: str) -> dict:
    """Return the project's orchestration record (empty record if absent)."""
    safe_id(project_id)
    record = store.orchestration(project_id)
    if record is None:
        return _empty(project_id)
    return record


def requester_host(req: dict) -> str | None:
    """Which host is issuing ``req`` (07 §5).

    Accepts the two shapes hosts actually send: ``{"owner": {"host": ...}}`` on
    orchestration mutations and ``{"by": ...}`` / ``{"host": ...}`` on task
    requests. Returns ``None`` when the request does not name itself.
    """
    owner = req.get("owner")
    if isinstance(owner, dict) and owner.get("host"):
        return str(owner["host"])
    for key in ("by", "host"):
        if req.get(key):
            return str(req[key])
    return None


def require_owner(store: Store, project_id: str, req: dict) -> dict | None:
    """Refuse a mutation from a host that is not the recorded owner (07 §5).

    A project with no recorded owner is unclaimed, so any host may act; once a
    host has claimed it, every task-level mutation has to name that host.
    Nothing is auto-overridden — the caller must ``claim --force`` instead.
    """
    record = store.orchestration(project_id)
    if record is None:
        return None
    owner = record.get("owner")
    # 记录可能由旧版本或外部工具写入；``owner`` 不是对象时按“未认领”处理，
    # 而不是让 ``.get`` 把每个 mutation 变成 INTERNAL_ERROR。
    recorded = owner.get("host") if isinstance(owner, dict) else None
    if not recorded:
        return record
    if requester_host(req) != recorded:
        raise Error(
            "NOT_OWNER",
            f"Project {project_id} is owned by {recorded}; "
            f"claim it (or pass owner.host) before mutating",
        )
    return record


def update(store: Store, req: dict[str, Any]) -> dict:
    """Apply a decision-level CAS update (07 §3.1)."""
    project_id = safe_id(req.get("project_id"))
    require("expected_version" in req, "INPUT_INCOMPLETE", "expected_version required")
    expected = int(req["expected_version"])
    patch = req.get("patch") or {}
    _validate_patch(patch)
    require("version" not in patch, "INPUT_INCOMPLETE", "version is controller-incremented")
    require(
        "decisions" not in patch,
        "INPUT_INCOMPLETE",
        "decisions are append-only; use append_decisions",
    )

    append = req.get("append_decisions") or []
    require(isinstance(append, list), "INPUT_INCOMPLETE", "append_decisions must be an array")
    for item in append:
        require(isinstance(item, dict), "INPUT_INCOMPLETE", "decision entries must be objects")
        require(
            item.get("action") and item.get("reason"),
            "INPUT_INCOMPLETE",
            "decisions require action and reason",
        )

    # Ownership is checked before the CAS so a non-owner cannot even learn
    # whether its version was current (07 §5).
    require_owner(store, project_id, req)

    current = store.orchestration(project_id)
    current_version = current["version"] if current else 0
    require(
        current_version == expected,
        "DECISION_CONFLICT",
        "Orchestration version conflict; refresh expected_version",
    )

    base = current if current else _empty(project_id)
    # Merge patch over allowed keys.
    for key, value in patch.items():
        base[key] = value
    base["decisions"] = list(base.get("decisions") or []) + [
        {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "by": (req.get("owner") or {}).get("host")
            if isinstance(req.get("owner"), dict)
            else req.get("by"),
            **item,
        }
        for item in append
    ]
    base.pop("version", None)
    base.pop("updated_at", None)
    record = store.upsert_orchestration(project_id, expected, base)
    return record


def _slots(store: Store, project_id: str) -> dict:
    """Concurrency snapshot using the *project's* limit (see ``_pipeline.slots``).

    Imported lazily: ``_pipeline`` imports this module, so a module-level import
    would be circular.
    """
    from src._pipeline import slots

    try:
        config = store.config(project_id)
    except (Error, OSError):
        # 项目未登记或配置不可读时，快照仍要给出计数事实；限额退回默认值。
        config = {}
    return slots(store, config)


def resume(store: Store, project_id: str) -> dict:
    """Recovery snapshot for a host resuming a project (07 §6).

    Everything here is derived from persistent records, so a brand-new host
    session can restate the goal, what is running, what is waiting and what may
    be done next without consulting chat history (§11.1).
    """
    record = show(store, project_id)
    active_runs = []
    dispatchable = []
    stale = []
    unanswered = []
    for t in store.tasks():
        if t["project_id"] != project_id:
            continue
        r = store.get_run(t.get("current_run_id")) if t.get("current_run_id") else None
        active = bool(r and r.get("status") != "exited")
        if active:
            active_runs.append(
                {"task_id": t["task_id"], "state": t["state"], "block_code": t.get("block_code")}
            )
        elif t["state"] in ("READY", "PAUSED", "BLOCKED"):
            # A task blocked on an unknown execution domain is not dispatchable:
            # `start` refuses it, and saying otherwise would invite a double writer.
            if t["state"] != "BLOCKED" or t.get("block_code") != "PROCESS_UNKNOWN":
                # 额度耗尽时"下一步是 resume"是错的：闸门会拒绝，或更糟——它尚未
                # 触发时真的启动一次 Agent。先解除闸门，再谈是否继续。
                action = (
                    "task answer"
                    if t.get("block_code") == "INPUT_REQUIRED"
                    else (
                        "project quota clear"
                        if t.get("block_code") == "QUOTA_EXHAUSTED"
                        else "task resume"
                    )
                )
                dispatchable.append(
                    {
                        "task_id": t["task_id"],
                        "state": t["state"],
                        "block_code": t.get("block_code"),
                        "action": action,
                    }
                )
        if t.get("block_code") == "INPUT_REQUIRED":
            for question in t.get("pending_questions") or []:
                if not question.get("answer"):
                    unanswered.append(
                        {
                            "task_id": t["task_id"],
                            "question_id": question.get("question_id"),
                            "text": question.get("text"),
                        }
                    )
        stale_summary = _stale_summary(store, t)
        if stale_summary:
            stale.append(stale_summary)
    # Questions recorded on the orchestration record itself are still surfaced:
    # hosts may ask the controller to track non-Agent decisions there.
    pending = list(record.get("pending_questions") or []) + unanswered
    # 接手方必须第一眼看到"现在派发一定会被拒"：否则它会把 start 的
    # QUOTA_EXHAUSTED 当成一次偶发失败，然后换一个任务再试。
    gate = _quota.active_gate(store, project_id)
    return {
        # 只读快照，与 `start` 共用同一统计；它不是"一定成功"的许可（§10.2）。
        "slots": _slots(store, project_id),
        "quota_gate": _quota.details(gate) if gate else None,
        "orchestration": {
            "goal": record.get("goal"),
            "version": record.get("version"),
            "waiting_on": record.get("waiting_on"),
            "next_action": record.get("next_action"),
        },
        "active_runs": active_runs,
        "pending_questions": pending,
        "dispatchable_now": dispatchable,
        "stale_summaries": stale,
        "next_action": record.get("next_action"),
        "guard": "read-only snapshot; every mutation re-checks version",
    }


def _stale_summary(store: Store, t: dict) -> dict | None:
    """Return a stale-summary entry when the active summary lags the event log."""
    from src import _summaries

    try:
        index = _summaries.list_summaries(store, t["task_id"])
    except Error:
        return None
    active = [e for e in index.get("entries", []) if e.get("status") == "active"]
    if not active:
        return None
    latest = max(active, key=lambda e: e["version"])
    head = store.max_event_id(t["task_id"])
    if latest.get("through_event_id", 0) >= head:
        return None
    return {
        "task_id": t["task_id"],
        "summary_version": latest["version"],
        "through_event_id": latest.get("through_event_id"),
        "current_event_id": head,
    }


def claim(
    store: Store,
    project_id: str,
    host: str,
    force: bool = False,
    session_id: str | None = None,
) -> dict:
    """Take ownership of a project's orchestration (07 §5)."""
    safe_id(project_id)
    require(isinstance(host, str) and host, "INPUT_INCOMPLETE", "host required")
    current = store.orchestration(project_id)
    if current and current.get("owner") and not force:
        raise Error(
            "NOT_OWNER",
            f"Project owned by {current['owner'].get('host')}; use --force to take over",
        )
    base = current if current else _empty(project_id)
    base["owner"] = {
        "host": host,
        "session_id": session_id,
        "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    expected = current["version"] if current else 0
    base.pop("version", None)
    base.pop("updated_at", None)
    return store.upsert_orchestration(project_id, expected, base)


def release(store: Store, project_id: str, host: str) -> dict:
    safe_id(project_id)
    current = store.orchestration(project_id)
    require(current, "INPUT_INCOMPLETE", "No orchestration record")
    require(
        (current.get("owner") or {}).get("host") == host,
        "NOT_OWNER",
        "Not the current owner",
    )
    base = dict(current)
    base["owner"] = None
    base.pop("version", None)
    base.pop("updated_at", None)
    return store.upsert_orchestration(project_id, current["version"], base)
