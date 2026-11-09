"""Readable summary versions produced by the orchestrator via the controller.

The controller owns execution evidence and structured progress; the orchestrator
owns the human-readable summary. Summaries are versioned, immutable, bound to a
``through_event_id`` and a spec revision, and can go stale without ever being
deleted. Summary failures must never affect task state (06 §7).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from src._store import Store
from src._util import atomic, atomic_text, digest, is_integer, redact_secrets, require, safe_id

SUMMARY_LIMIT_BYTES = 8 * 1024


def _summaries_dir(store: Store, t: dict) -> Path:
    return store.path(t) / "summaries"


def _index_path(store: Store, t: dict) -> Path:
    return _summaries_dir(store, t) / "index.json"


def _report_path(store: Store, t: dict) -> Path:
    return store.path(t) / "report.md"


def _load_index(store: Store, t: dict) -> dict:
    path = _index_path(store, t)
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"record_version": 1, "latest": 0, "entries": []}
    return {"record_version": 1, "latest": 0, "entries": []}


def _save_index(store: Store, t: dict, index: dict) -> None:
    atomic(_index_path(store, t), index)


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    return value


def submit(store: Store, task_id: str, req: dict[str, Any]) -> dict:
    """Submit a summary version. Idempotent by ``request_id``."""
    t = store.task(task_id)
    require(
        is_integer(req.get("schema_version")) and req.get("schema_version") in (1, 2),
        "INPUT_INCOMPLETE",
        f"schema_version must be 1 or 2, got {req.get('schema_version')!r}",
    )
    key = safe_id(req.get("request_id"))
    project_id = req.get("project_id")
    require(
        project_id == t["project_id"],
        "PROJECT_MISMATCH",
        "project_id does not match the task's project",
    )
    run_id = req.get("run_id")
    if run_id:
        r = store.get_run(run_id)
        require(
            r and r.get("task_id") == task_id,
            "TASK_RUN_MISMATCH",
            "run_id does not belong to this task",
        )

    through = int(req.get("through_event_id", 0))
    max_id = store.max_event_id(task_id)
    require(
        through <= max_id,
        "INPUT_INCOMPLETE",
        "through_event_id must not reference future events",
    )

    spec_revision = int(req.get("spec_revision", 0))
    stale = spec_revision != t.get("spec_revision")

    # Validate evidence_refs resolvable and belonging to this task (06 §3).
    evidence_refs = req.get("evidence_refs") or []
    require(isinstance(evidence_refs, list), "INPUT_INCOMPLETE", "evidence_refs must be an array")
    for ref in evidence_refs:
        require(
            _evidence_ref_belongs(store, t, ref),
            "TASK_RUN_MISMATCH",
            f"evidence ref {ref!r} not resolvable for this task",
        )

    summary_text = req.get("summary") or ""
    truncated = len(summary_text.encode("utf-8", "replace")) > SUMMARY_LIMIT_BYTES
    if truncated:
        summary_text = summary_text.encode("utf-8", "replace")[:SUMMARY_LIMIT_BYTES].decode(
            "utf-8", "replace"
        )

    # Redact credential values before persisting (06 §6).
    redacted = _redact(
        {
            "schema_version": 1,
            "task_id": task_id,
            "project_id": project_id,
            "run_id": run_id,
            "spec_revision": spec_revision,
            "through_event_id": through,
            "generated_by": req.get("generated_by"),
            "summary": summary_text,
            "facts": req.get("facts") or [],
            "agent_claims": req.get("agent_claims") or [],
            "orchestrator_advice": req.get("orchestrator_advice"),
            "human_decisions": req.get("human_decisions") or [],
            "next_action": req.get("next_action"),
            "evidence_refs": evidence_refs,
            "truncated": truncated,
        }
    )

    index = _load_index(store, t)
    entries = index.get("entries", [])
    # Same through_event_id: replace and mark prior superseded_by (06 §3).
    existing = next((e for e in entries if e.get("through_event_id") == through), None)
    if existing:
        new_version = int(existing["version"])
    else:
        new_version = int(index.get("latest", 0)) + 1

    entry = {
        "version": new_version,
        "through_event_id": through,
        "status": "stale" if stale else "active",
        "created_at": time.time(),
        "generated_by": req.get("generated_by"),
        "request_id": key,
        "spec_revision": spec_revision,
        "summary_hash": digest(redacted),
    }
    if existing:
        entry["superseded_by"] = new_version
        existing["superseded_by"] = new_version
        existing["status"] = "superseded"
        # Keep the prior entry as history.
    # Mark all other active entries stale when this one is active.
    if not stale:
        for e in entries:
            if e is not existing and e.get("status") == "active":
                e["status"] = "stale"
    if existing:
        existing.update(entry)
    else:
        entries.append(entry)
    index["latest"] = max(int(e["version"]) for e in entries)
    index["entries"] = sorted(entries, key=lambda e: e["version"])
    _save_index(store, t, index)

    version_path = _summaries_dir(store, t) / f"{new_version:04d}-{through}.json"
    atomic(version_path, redacted)
    _render_report(store, t, index)
    return {
        "status": "succeeded",
        "task_id": task_id,
        "summary_version": new_version,
        "through_event_id": through,
        "stale": stale,
        "truncated": truncated,
    }


def _evidence_ref_belongs(store: Store, t: dict, ref: str) -> bool:
    """Resolve a run-product evidence ref against this task's runs."""
    # ref forms: validation-<run>, changes-<run>, agent-result-<run>, publication-<run>
    parts = ref.rsplit("-", 1)
    if len(parts) != 2:
        return False
    run_id = parts[1]
    r = store.get_run(run_id)
    return bool(r and r.get("task_id") == t["task_id"])


def list_summaries(store: Store, task_id: str) -> dict:
    t = store.task(task_id)
    index = _load_index(store, t)
    return {"task_id": task_id, **index}


def _render_report(store: Store, t: dict, index: dict) -> None:
    """Render report.md from the latest active summary (06 §2)."""
    active = [e for e in index.get("entries", []) if e.get("status") == "active"]
    if not active:
        return
    latest = max(active, key=lambda e: e["version"])
    path = _summaries_dir(store, t) / f"{latest['version']:04d}-{latest['through_event_id']}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    lines = [
        f"# {t.get('title', t['task_id'])}",
        "",
        f"- Task: {t['task_id']}",
        f"- Summary version: {latest['version']} (through event {latest['through_event_id']})",
        f"- Generated by: {data.get('generated_by')}",
        "",
        data.get("summary", ""),
    ]
    if data.get("facts"):
        lines += ["", "## Facts"] + [f"- {f}" for f in data["facts"]]
    if data.get("agent_claims"):
        lines += ["", "## Agent claims"] + [f"- {c}" for c in data["agent_claims"]]
    if data.get("next_action"):
        lines += ["", "## Next action", data["next_action"]]
    # `report.md` is read by humans (and by hosts that paste it), so it must be
    # Markdown text — `atomic` would wrap it in a JSON object.
    atomic_text(_report_path(store, t), "\n".join(lines) + "\n")


def handoff_show(store: Store, task_id: str) -> dict:
    """Structured handoff material (06 §8)."""
    t = store.task(task_id)
    runs = store.runs(task_id)
    handoff = {}
    for r in reversed(runs):
        path = store.path(t) / "runs" / r["run_id"] / "handoff.json"
        if path.is_file():
            try:
                handoff = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                handoff = {}
            break
    return {
        "task_id": task_id,
        "project_id": t["project_id"],
        "state": t["state"],
        "phase": t["phase"],
        "spec_revision": t.get("spec_revision"),
        "candidate_sha": t.get("candidate_sha"),
        "block_code": t.get("block_code"),
        "handoff": handoff,
    }
