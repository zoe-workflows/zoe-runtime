"""Preview what a configuration change does to in-flight tasks (v6 §B4).

``rebind`` is per task and demands ``target_policy_hash`` plus ``expected_version``.
Without a preview the operator only discovers the blast radius by attempting the
rebind — which is exactly the "fix one field, find the next breakage" loop this
whole design exists to remove.

Everything here is read-only, and ``spec_valid_under_new_policy`` calls the same
``_contracts.validate_spec_text`` that ``rebind`` calls: a preview that claims a
frozen spec still fits must never disagree with the real call.
"""

from __future__ import annotations

from pathlib import Path

from src import _config, _contracts
from src._util import Error, digest, read

#: States that can still be rebound. A ``RUNNING`` task must be paused first,
#: so it is listed (the operator needs to see it) but flagged in ``note``.
ACTIVE_STATES = ("READY", "PAUSED", "BLOCKED", "RUNNING")

#: The same predicate ``start`` uses before dispatching.
DISPATCHABLE = ("READY", "PAUSED", "BLOCKED")


def _flatten(config: dict | None) -> dict[str, object]:
    """Field-level view of a normalised config.

    ``checks`` and ``agent_profiles`` are keyed by id so a reordered list is not
    reported as a change; everything else compares as a whole value.
    """
    out: dict[str, object] = {}
    for key, value in (config or {}).items():
        if key == "checks" and isinstance(value, list):
            for index, check in enumerate(value):
                # A stored config may predate validation (hand-edited file), so
                # never assume the element shape here.
                if not isinstance(check, dict):
                    out[f"checks[{index}]"] = check
                    continue
                ident = check.get("id", index)
                for field, item in check.items():
                    out[f"checks[{ident}].{field}"] = item
        elif key == "agent_profiles" and isinstance(value, dict):
            for ident, profile in value.items():
                if not isinstance(profile, dict):
                    out[f"agent_profiles[{ident}]"] = profile
                    continue
                for field, item in profile.items():
                    out[f"agent_profiles[{ident}].{field}"] = item
        else:
            out[key] = value
    return out


def changed_fields(current: dict | None, new: dict) -> list[str]:
    if current is None:
        return sorted(_flatten(new))
    left, right = _flatten(current), _flatten(new)
    return sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))


def _affected(store, project_id: str, new: dict, new_hash: str) -> tuple[list, list]:
    rows: list[dict] = []
    commands: list[str] = []
    for task in store.tasks():
        config = task.get("config") or {}
        if config.get("project_id") != project_id:
            continue
        state = task.get("state")
        if state not in ACTIVE_STATES:
            continue
        # ``rebind`` reads the frozen snapshot from disk and validates its raw
        # text, so the preview must do exactly the same thing.
        snapshot = Path(store.path(task)) / f"spec-{task.get('spec_revision')}.json"
        # 快照丢了就等于 rebind 一定会失败（它读同一个文件），所以这里必须是
        # ``False`` 而不是 ``None``：``None`` 会被宿主读成"未知，可以试试"。
        valid, note = False, "spec snapshot missing; rebind would fail — revise the task first"
        if snapshot.exists():
            valid, note = True, None
            try:
                _contracts.validate_spec_text(read(snapshot).get("text"), new)
            except Error as exc:
                valid, note = False, str(exc)
        if state == "RUNNING":
            note = (note + "; " if note else "") + "running: pause before rebinding"
        required = task.get("policy_revision") != new_hash[:16]
        rows.append(
            {
                "task_id": task["task_id"],
                "state": state,
                "row_version": task.get("row_version"),
                "policy_revision": task.get("policy_revision"),
                "rebind_required": required,
                "spec_valid_under_new_policy": valid,
                "note": note,
            }
        )
        if required and valid and state in DISPATCHABLE:
            commands.append(
                f"zoe task rebind {task['task_id']} --request-file rebind-{task['task_id']}.json"
            )
    return rows, commands


def project_diff(store, config_path, project_id: str | None = None) -> dict:
    """Compare a candidate config with the registered one, read-only."""
    path = Path(config_path)
    new = _config.validate_config(read(path), base_dir=path.parent)
    pid = project_id or new["project_id"]
    if pid != new["project_id"]:
        raise Error(
            "PROJECT_MISMATCH",
            f"config declares {new['project_id']!r} but --project is {pid!r}",
        )

    stored_path = Path(store.home) / "projects" / f"{pid}.json"
    current = read(stored_path) if stored_path.exists() else None
    new_hash = digest(new)
    current_hash = digest(current) if current is not None else None
    rows, commands = _affected(store, pid, new, new_hash)

    return {
        "api_version": 2,
        "project_id": pid,
        "registered": current is not None,
        "policy_changed": current_hash != new_hash,
        "changed_fields": changed_fields(current, new),
        "current_policy_hash": current_hash,
        "new_policy_hash": new_hash,
        "current_policy_revision": current_hash[:16] if current_hash else None,
        "new_policy_revision": new_hash[:16],
        "affected_tasks": rows,
        "suggested_commands": commands,
    }
