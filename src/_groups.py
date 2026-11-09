"""Task groups: frozen plans (v3), legacy ordered records (v2), derived status.

Two record shapes live here on purpose:

* ``record_version: 2`` — the v1.0 "ordered items" record. It is a *report*
  container: the host dispatches one item at a time and nothing advances on its
  own. Kept readable, never rewritten (TGD §12.3).
* ``record_version: 3`` — the task-group-delivery record (TGD §4.2). A frozen,
  immutable ``definition`` (requirements, static dependency graph, member spec
  sha256s, group checks, budget caps) is separated from a mutable ``runtime``
  (task bindings, integrated SHAs, lease, budget counters, check results).

The separation is what makes ``plan_digest`` stable: it is a projection of the
*definition only*, so counting a start or integrating a member never invalidates
the plan (TGD §4.3, test T31).

Planning is split in two steps so that "validation" is really pure:
``resolve_plan_inputs`` does every read (git, design files, specs, symlinks) and
freezes the answers into a snapshot; ``validate_plan`` decides on that snapshot
alone and can be table-tested without a filesystem (TGD §5.2).
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

from src import _contracts
from src._config import authorize
from src._store import PROTOCOL_VERSION, SCHEMA_VERSION, Store
from src._util import VERSION, Error, atomic, digest, git, read, require, safe_id

#: Plan request contract version accepted by ``group create``.
PLAN_SCHEMA_VERSION = 2

#: Legacy (v1.0) group record; ``ordered_items`` semantics, no dependency graph.
LEGACY_RECORD_VERSION = 2

#: Task-group-delivery record (TGD §4.2).
RECORD_VERSION = 3

#: v1.3 only accepts ``local_artifact`` groups (TGD §9.4 / §12.5).
SUPPORTED_DELIVERY_MODES = ("local_artifact",)

#: Member states are **derived** (TGD §4.7); the five persisted facts are
#: ``task_id``, ``validated_sha``, ``integrated_sha``, ``invalidated``,
#: ``last_block``. Nothing else about a member is stored as a state.
MEMBER_STATES = (
    "INTEGRATED",
    "INVALIDATED",
    "BLOCKED",
    "CANCELLED",
    "PAUSED",
    "VALIDATED",
    "EXECUTING",
    "READY",
    "WAITING",
)

#: Group states (TGD §6.1). Same names as the task state machine, no ``DRAFT``:
#: ``group plan`` is a stateless preview.
GROUP_STATES = ("READY", "RUNNING", "BLOCKED", "PAUSED", "REVIEW_READY", "DONE", "CANCELLED")

_GROUP_TERMINAL = ("DONE", "CANCELLED")


def _fail(code: str, message: str, kind: str | None = None, **details) -> None:
    payload = dict(details)
    if kind is not None:
        payload["kind"] = kind
    raise Error(code, message, details=payload or None)


def group_dir(store: Store, group_id: str) -> Path:
    """``ZOE_HOME/groups/<gid>/`` — sibling of ``tasks/<id>/`` (TGD §4.5)."""
    return store.home / "groups" / safe_id(group_id)


def is_v3(group: dict) -> bool:
    return group.get("record_version") == RECORD_VERSION


def require_v3(group: dict, action: str) -> None:
    """Legacy records keep working for ``status``/``dispatch`` only (TGD §12.3)."""
    if is_v3(group):
        return
    _fail(
        "CAPABILITY_UNSUPPORTED",
        f"`group {action}` requires a record_version=3 group; this group is "
        f"record_version={group.get('record_version', 1)}. Recreate it from a plan "
        "request (`members` + `requirements`) to use the dependency graph.",
        kind="legacy_group",
        group_id=group.get("group_id"),
    )


# --------------------------------------------------------------------------- #
# plan inputs: every read happens here, once                              (5.2)
# --------------------------------------------------------------------------- #


def _repo_path(repo: Path, value: str) -> Path:
    """Resolve a repo-relative path, refusing absolute paths and escapes."""
    require(isinstance(value, str) and value, "INPUT_INCOMPLETE", "Path must be a string")
    raw = Path(value)
    require(
        not raw.is_absolute() and not raw.drive,
        "INPUT_INCOMPLETE",
        f"Path {value!r} must be relative to the repository",
    )
    require(
        ".." not in raw.parts,
        "INPUT_INCOMPLETE",
        f"Path {value!r} must not escape the repository with '..'",
    )
    return (repo / raw).resolve()


def repo_relative_path(repo: Path, value: str) -> Path:
    """Public entry to the repo-relative rule (absolute paths and ``..`` refused)."""
    return _repo_path(repo, value)


def _contained(base: Path, target: Path) -> bool:
    """Directory-boundary containment, not a string-prefix test (TGD §5.2-1).

    ``src/export`` therefore does **not** contain ``src/exported``.
    """
    if os.name == "nt":  # Windows compares case-folded, reports keep the original
        base_text, target_text = str(base).casefold(), str(target).casefold()
        base_path, target_path = Path(base_text), Path(target_text)
    else:
        base_path, target_path = base, target
    try:
        return target_path == base_path or target_path.is_relative_to(base_path)
    except (ValueError, OSError):  # pragma: no cover - different drives
        return False


def resolve_plan_inputs(store: Store, req: dict[str, Any]) -> dict[str, Any]:
    """Read and **freeze** everything a plan decision depends on (TGD §5.2)."""
    require(
        req.get("schema_version") == PLAN_SCHEMA_VERSION,
        "INPUT_INCOMPLETE",
        f"Group plan schema_version must be {PLAN_SCHEMA_VERSION}",
    )
    c = store.config(req["project_id"])
    authorize(c, req, "create")
    repo = Path(c["repo"])

    delivery_mode = req.get("delivery_mode", "local_artifact")
    if delivery_mode == "pull_request":
        _fail(
            "CAPABILITY_UNSUPPORTED",
            "v1.3 supports only local_artifact groups; a pull_request group cannot be "
            "accepted by the controller. Record an external close instead.",
            kind="group_pr_unsupported",
            note="v1.3 仅支持 local_artifact 组验收",
        )
    require(
        delivery_mode in SUPPORTED_DELIVERY_MODES,
        "INPUT_INCOMPLETE",
        "delivery_mode must be one of " + ", ".join(SUPPORTED_DELIVERY_MODES),
    )

    base_ref = req.get("base_ref") or c.get("base_ref", "HEAD")
    require(
        isinstance(base_ref, str) and base_ref and not base_ref.startswith("-"),
        "INPUT_INCOMPLETE",
        "Invalid base_ref",
    )
    base_sha = git(repo, "rev-parse", "--verify", base_ref + "^{commit}")

    design_files = req.get("design_files") or []
    require(
        isinstance(design_files, list) and design_files,
        "INPUT_INCOMPLETE",
        "design_files must be a non-empty array",
    )
    manifest = []
    for name in design_files:
        path = _repo_path(repo, name)
        require(path.is_file(), "INPUT_INCOMPLETE", f"Design file not found: {name}")
        manifest.append({"path": str(name), "sha256": digest(path.read_bytes())})
    manifest.sort(key=lambda item: item["path"])

    project_paths = list(c.get("allowed_paths") or [])
    resolved_project_paths = [(repo / entry).resolve() for entry in project_paths]

    members = []
    raw_members = req.get("members")
    require(
        isinstance(raw_members, list) and raw_members,
        "INPUT_INCOMPLETE",
        "members must be a non-empty array",
    )
    for raw in raw_members:
        require(isinstance(raw, dict), "INPUT_INCOMPLETE", "Each member must be an object")
        key = safe_id(raw.get("task_key"))
        spec_file = raw.get("spec_file")
        require(
            isinstance(spec_file, str) and spec_file,
            "INPUT_INCOMPLETE",
            f"Member {key} requires spec_file",
        )
        spec_path = _repo_path(repo, spec_file)
        require(spec_path.is_file(), "INPUT_INCOMPLETE", f"Spec file not found: {spec_file}")
        # `_contracts.load_spec` validates the spec against the frozen project
        # policy; the *text* is what gets frozen and hashed below.
        spec = _contracts.load_spec(spec_path, c)
        document = spec.get("document")
        require(
            isinstance(document, dict),
            "INPUT_INCOMPLETE",
            f"Member {key} requires a structured spec v2 (acceptance_criteria); a "
            "Markdown spec has no AC ids to map requirements onto",
        )
        criteria = [
            item for item in (document.get("acceptance_criteria") or []) if isinstance(item, dict)
        ]
        expected_paths = list(raw.get("expected_paths") or [])
        resolved_expected = []
        for value in expected_paths:
            path = _repo_path(repo, value)
            resolved_expected.append(
                {
                    "path": value,
                    # A symlink may point outside the repository; the resolved
                    # form is what the boundary check uses (TGD §5.2-6).
                    "resolved": str(path),
                    "within": any(_contained(entry, path) for entry in resolved_project_paths),
                }
            )
        depends_on = raw.get("depends_on") or []
        require(
            isinstance(depends_on, list) and all(isinstance(item, str) for item in depends_on),
            "INPUT_INCOMPLETE",
            f"Member {key}: depends_on must be a string array",
        )
        members.append(
            {
                "task_key": key,
                "title": raw.get("title"),
                "spec_file": spec_file,
                "spec_text": spec["text"],
                "spec_sha256": digest(spec["text"].encode()),
                "spec_document": document,
                "spec_contract_version": spec["contract_version"],
                "spec_allowed_paths": list(document.get("allowed_paths") or []),
                "ac_ids": [item.get("id") for item in criteria],
                "ac_checks": [
                    item.get("verification", {}).get("check_id")
                    for item in criteria
                    if (item.get("verification") or {}).get("type") == "check"
                ],
                "ac_manual": [
                    item.get("id")
                    for item in criteria
                    if (item.get("verification") or {}).get("type") == "manual"
                ],
                "depends_on": list(depends_on),
                "requirement_ids": list(raw.get("requirement_ids") or []),
                "acceptance_ids": list(raw.get("acceptance_ids") or []),
                "expected_paths": expected_paths,
                "expected_paths_resolved": resolved_expected,
                "task_type": raw.get("task_type"),
                "requested_profile": raw.get("requested_profile"),
                "check_ids": list(raw.get("check_ids") or []),
            }
        )

    group_check_ids = req.get("group_checks") or []
    require(
        isinstance(group_check_ids, list) and group_check_ids,
        "INPUT_INCOMPLETE",
        "group_checks must be a non-empty array of check ids",
    )
    configured = {check["id"]: check for check in c.get("checks") or []}
    group_checks = []
    for check_id in group_check_ids:
        require(
            check_id in configured,
            "INPUT_INCOMPLETE",
            f"group check {check_id!r} is not configured in the project",
        )
        group_checks.append(
            {
                "check_id": check_id,
                "command_digest": digest(configured[check_id]["argv"]),
                # All declared group checks run inside the integration worktree
                # before the ref moves (TGD §8.2 "quick check").
                "quick": True,
            }
        )

    budget = req.get("budget") or {}
    return {
        "request": req,
        "config": c,
        "project_id": c["project_id"],
        "base_ref": base_ref,
        "base_sha": base_sha,
        "design_manifest": manifest,
        "design_digest": digest(manifest),
        "requirements": list(req.get("requirements") or []),
        "members": members,
        "group_checks": group_checks,
        "budget": {
            "max_starts": budget.get("max_starts"),
            "max_wall_seconds": budget.get("max_wall_seconds"),
            # 组修复轮数上限：缺省 = 0 轮，即"这个计划不授权任何修复执行"（§12）。
            "max_group_repairs": budget.get("max_group_repairs"),
        },
        # 设计一致性审查（§11.1 第二层）。缺省不注入，见 `_review.required`。
        "review": req.get("review"),
        "delivery_mode": delivery_mode,
        "allowed_paths": project_paths,
        "env": {
            "runtime_version": VERSION,
            "schema_version": SCHEMA_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "platform": os.name,
            "base_sha": base_sha,
            "resolved_project_paths": [str(item) for item in resolved_project_paths],
        },
    }


# --------------------------------------------------------------------------- #
# pure validation                                                       (5.2)
# --------------------------------------------------------------------------- #


def _topological(members: list[dict]) -> list[str]:
    """Kahn's algorithm; raises ``cycle`` when no order exists."""
    keys = [member["task_key"] for member in members]
    incoming = {key: 0 for key in keys}
    outgoing = {key: [] for key in keys}
    for member in members:
        for dep in member["depends_on"]:
            incoming[member["task_key"]] += 1
            outgoing[dep].append(member["task_key"])
    ready = sorted(key for key, count in incoming.items() if count == 0)
    order: list[str] = []
    while ready:
        key = ready.pop(0)
        order.append(key)
        for nxt in sorted(outgoing[key]):
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)
                ready.sort()
    if len(order) != len(keys):
        _fail(
            "INPUT_INCOMPLETE",
            "member depends_on graph contains a cycle: "
            + ", ".join(sorted(set(keys) - set(order))),
            kind="cycle",
        )
    return order


def validate_plan(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Decide on a frozen snapshot. No IO, no clock, no environment reads."""
    members = snapshot["members"]
    keys = [member["task_key"] for member in members]

    # 1 — identity ---------------------------------------------------------
    require(len(set(keys)) == len(keys), "INPUT_INCOMPLETE", "task_key must be unique")
    for member in members:
        require(
            isinstance(member.get("title"), str) and member["title"],
            "INPUT_INCOMPLETE",
            f"Member {member['task_key']} requires a title",
        )
        require(
            bool(member.get("spec_sha256")),
            "INPUT_INCOMPLETE",
            f"Member {member['task_key']} has no frozen spec digest",
        )

    # 2 — dependency graph -------------------------------------------------
    for member in members:
        unknown = sorted(set(member["depends_on"]) - set(keys))
        require(
            not unknown,
            "INPUT_INCOMPLETE",
            "member {} depends on unknown task_key(s): {}".format(
                member["task_key"], ", ".join(unknown)
            ),
        )
        require(
            member["task_key"] not in member["depends_on"],
            "INPUT_INCOMPLETE",
            f"Member {member['task_key']} depends on itself",
        )
    order = _topological(members)

    # 3 — requirement coverage --------------------------------------------
    requirement_ids = [item.get("id") for item in snapshot["requirements"]]
    require(
        len(set(requirement_ids)) == len(requirement_ids),
        "INPUT_INCOMPLETE",
        "requirement ids must be unique",
    )
    for item in snapshot["requirements"]:
        require(
            isinstance(item.get("id"), str)
            and item["id"]
            and isinstance(item.get("statement"), str)
            and item["statement"],
            "INPUT_INCOMPLETE",
            "Every requirement needs an id and a statement",
        )
    covered = {rid for member in members for rid in member["requirement_ids"]}
    unknown_requirements = sorted(covered - set(requirement_ids))
    require(
        not unknown_requirements,
        "INPUT_INCOMPLETE",
        "members reference unknown requirement ids: " + ", ".join(unknown_requirements),
    )
    missing = [rid for rid in requirement_ids if rid not in covered]
    if missing:
        _fail(
            "INPUT_INCOMPLETE",
            "no member covers requirement(s): " + ", ".join(missing),
            kind="coverage_missing",
            uncovered_requirements=missing,
        )

    # 4 — acceptance mapping ----------------------------------------------
    group_check_ids = {item["check_id"] for item in snapshot["group_checks"]}
    for member in members:
        key = member["task_key"]
        declared = member["acceptance_ids"]
        require(declared, "INPUT_INCOMPLETE", f"Member {key} must list acceptance_ids")
        require(
            len(set(declared)) == len(declared),
            "INPUT_INCOMPLETE",
            f"Member {key} lists a duplicate acceptance id",
        )
        unknown_acs = sorted(set(declared) - set(member["ac_ids"]))
        require(
            not unknown_acs,
            "INPUT_INCOMPLETE",
            "member {} lists acceptance id(s) absent from its spec: {}".format(
                key, ", ".join(unknown_acs)
            ),
        )
        unflagged = [ac for ac in member["ac_ids"] if ac not in declared]
        require(
            not unflagged,
            "INPUT_INCOMPLETE",
            "member {} does not map acceptance id(s): {}".format(key, ", ".join(unflagged)),
        )
        available = set(member["check_ids"]) | group_check_ids
        outside = sorted({check for check in member["ac_checks"] if check not in available})
        require(
            not outside,
            "INPUT_INCOMPLETE",
            "member {} maps a check-type acceptance to check id(s) it neither owns nor "
            "declares as a group check: {}".format(key, ", ".join(outside)),
        )
        require(
            len(set(member["check_ids"])) == len(member["check_ids"]),
            "INPUT_INCOMPLETE",
            f"Member {key} lists a duplicate check id",
        )

    # 5 — spec paths may only tighten the project policy -------------------
    for member in members:
        widened = [
            path
            for path in member["spec_allowed_paths"]
            if not _contracts.prefix_covered(path, snapshot["allowed_paths"])
        ]
        require(
            not widened,
            "POLICY_DENIED",
            "member {} spec widens the project allowed_paths: {}".format(
                member["task_key"], ", ".join(widened)
            ),
        )

    # 6 — expected_paths are a planning hint, and must still be inside policy
    for member in members:
        outside = [item["path"] for item in member["expected_paths_resolved"] if not item["within"]]
        require(
            not outside,
            "POLICY_DENIED",
            "member {} expected_paths fall outside every project allowed_paths entry: {}".format(
                member["task_key"], ", ".join(outside)
            ),
        )

    # 8 — budget -----------------------------------------------------------
    max_starts = snapshot["budget"]["max_starts"]
    require(
        isinstance(max_starts, int) and not isinstance(max_starts, bool) and max_starts > 0,
        "INPUT_INCOMPLETE",
        "budget.max_starts is required and must be a positive integer "
        "(it is never derived from the member count)",
    )
    wall = snapshot["budget"]["max_wall_seconds"]
    require(
        wall is None or (isinstance(wall, int | float) and not isinstance(wall, bool) and wall > 0),
        "INPUT_INCOMPLETE",
        "budget.max_wall_seconds must be a positive number or null",
    )
    # 组修复轮数（§9 预算"至少覆盖…组修复轮数"，§12）。显式给出才写进预算：
    # 缺省不是"可以无限修"，而是 0。
    repairs = snapshot["budget"].get("max_group_repairs")
    require(
        repairs is None
        or (isinstance(repairs, int) and not isinstance(repairs, bool) and repairs >= 0),
        "INPUT_INCOMPLETE",
        "budget.max_group_repairs must be a non-negative integer when present",
    )

    review_block = snapshot.get("review")
    if review_block is not None:
        require(
            isinstance(review_block, dict),
            "INPUT_INCOMPLETE",
            'review must be an object such as {"required": true}',
        )
        review_required = review_block.get("required", True)
        require(
            isinstance(review_required, bool),
            "INPUT_INCOMPLETE",
            "review.required must be a boolean",
        )
        review_mode = review_block.get("mode") or "agent"
        require(
            review_mode in ("agent", "human", "either"),
            "INPUT_INCOMPLETE",
            "review.mode must be 'agent', 'human' or 'either'",
        )

    definition = {
        "title": snapshot["request"].get("title") or "",
        "requirements": [
            {
                "id": item["id"],
                "statement": item["statement"],
                "source": item.get("source"),
            }
            for item in snapshot["requirements"]
        ],
        "members": [
            {
                "task_key": member["task_key"],
                "title": member["title"],
                "spec_sha256": member["spec_sha256"],
                "spec_snapshot": "specs/{}.json".format(member["task_key"]),
                "depends_on": list(member["depends_on"]),
                "requirement_ids": list(member["requirement_ids"]),
                "acceptance_ids": list(member["acceptance_ids"]),
                "expected_paths": list(member["expected_paths"]),
                "task_type": member.get("task_type"),
                "requested_profile": member.get("requested_profile"),
                "check_ids": list(member["check_ids"]),
            }
            for member in members
        ],
        "group_checks": [
            {
                "check_id": item["check_id"],
                "command_digest": item["command_digest"],
                "quick": bool(item["quick"]),
            }
            for item in snapshot["group_checks"]
        ],
        "budget_caps": {
            "max_starts": max_starts,
            "max_wall_seconds": wall,
        },
    }
    # 可选能力只在显式声明时进入 definition，这样 v1.3 计划重放得到逐字节相同的
    # `plan_digest`（§19.3：新能力字段默认缺失，而不是自动注入旧策略）。
    if repairs is not None:
        definition["budget_caps"]["max_group_repairs"] = repairs
    if review_block is not None:
        definition["review"] = {"required": review_required, "mode": review_mode}
    return {
        "definition": definition,
        "order": order,
        "plan_digest": plan_digest(snapshot, definition),
        "design_digest": snapshot["design_digest"],
        "base_sha": snapshot["base_sha"],
        "delivery_mode": snapshot["delivery_mode"],
        "warnings": _plan_warnings(snapshot, members),
    }


def _plan_warnings(snapshot: dict, members: list[dict]) -> list[str]:
    """Advisory notes. None of them changes a verdict.

    ``expected_paths`` overlap is reported because "these members will touch the
    same directory" is useful to a human; it never creates an implicit
    dependency edge (TGD §5.2-2) and it never grants a path (TGD §5.2-3).
    """
    warnings = []
    owners: dict[str, list[str]] = {}
    for member in members:
        for item in member["expected_paths"]:
            owners.setdefault(item, []).append(member["task_key"])
    for path, keys in sorted(owners.items()):
        if len(keys) > 1:
            warnings.append(
                "expected_paths {!r} is declared by {} members ({}); v1.3 runs the "
                "group serially, so this is a hint, not an ordering edge".format(
                    path, len(keys), ", ".join(sorted(keys))
                )
            )
    if snapshot["budget"]["max_wall_seconds"] is None:
        warnings.append("no wall-clock budget was set (max_wall_seconds: null)")
    if snapshot.get("review") is None:
        # 不静默：一个没有声明审查的计划，验收就只覆盖自动检查这一层。
        warnings.append(
            "plan declares no `review` block: design-conformance review will not gate "
            "acceptance (structural coverage only)"
        )
    warnings.append(
        "expected_paths are a planning hint only; authorisation still comes from "
        "project allowed_paths intersected with the member spec"
    )
    return warnings


def plan_digest(snapshot: dict[str, Any], definition: dict[str, Any]) -> str:
    """Digest of the **immutable projection only** (TGD §4.3).

    Runtime facts (task ids, validated/integrated SHAs, lease, budget counters,
    check results), ``op_seq``, ``revision`` and timestamps are deliberately
    excluded: counting a start must not invalidate the plan.
    """
    projection: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "project_id": snapshot["project_id"],
        "base_sha": snapshot["base_sha"],
        "design_digest": snapshot["design_digest"],
        "delivery_mode": snapshot["delivery_mode"],
        "requirements": definition["requirements"],
        "members": definition["members"],
        "group_checks": definition["group_checks"],
        "budget_caps": definition["budget_caps"],
    }
    if "review" in definition:
        projection["review"] = definition["review"]
    return digest(projection)


# --------------------------------------------------------------------------- #
# create                                                                 (5.1)
# --------------------------------------------------------------------------- #


def create(store: Store, req: dict[str, Any]) -> dict[str, Any]:
    """Freeze a group.

    The two request shapes share ``schema_version: 2`` (the legacy ordered record
    was published under it), so the discriminator is ``ordered_items`` versus
    ``members``: a request that carries ``ordered_items`` gets the record it was
    written for, byte for byte. Silently reinterpreting it as a dependency-graph
    plan would drop every ``depends_on`` and produce a group that cannot advance.
    """
    with store.lock():
        key, prior = store.operation(req, "group_create")
        if prior:
            return prior
        try:
            if (req or {}).get("ordered_items") is not None:
                result = _create_legacy(store, req)
            else:
                result = _create_plan(store, req)
            store.finish(key, result)
            return result
        except Exception as exc:
            store.finish(
                key,
                {
                    "status": "failed",
                    "error": {
                        "code": getattr(exc, "code", "INPUT_INCOMPLETE"),
                        "message": str(exc),
                    },
                },
            )
            raise


def _create_legacy(store: Store, req: dict[str, Any]) -> dict[str, Any]:
    """v1.0 record: ordered items, no dependency graph, no runner."""
    c = store.config(req["project_id"])
    authorize(c, req, "create")
    items = req.get("ordered_items")
    require(
        isinstance(items, list) and items,
        "INPUT_INCOMPLETE",
        "ordered_items must be a non-empty array",
    )
    # A legacy record stays a plain ordered list: it has no place for a static
    # dependency graph, and the runner refuses to advance it (TGD §12.3).
    if "depends_on" in req or any("depends_on" in (item or {}) for item in items):
        _fail(
            "CAPABILITY_UNSUPPORTED",
            "a legacy ordered group cannot carry depends_on; send a plan request "
            "(`members` + `requirements`) to use the dependency graph",
            kind="legacy_group",
        )
    seen = set()
    normalized = []
    for item in items:
        require(isinstance(item, dict), "INPUT_INCOMPLETE", "Each item must be an object")
        item_id = safe_id(item.get("item_id"))
        require(item_id not in seen, "INPUT_INCOMPLETE", "Duplicate item_id " + item_id)
        seen.add(item_id)
        require(
            isinstance(item.get("title"), str) and item["title"],
            "INPUT_INCOMPLETE",
            "Each item requires a title",
        )
        spec_ref = item.get("spec_ref")
        require(
            isinstance(spec_ref, str) and spec_ref,
            "INPUT_INCOMPLETE",
            "Each item requires a spec_ref",
        )
        require(
            not any(token in spec_ref for token in ("$(", "`", "|", ";")),
            "POLICY_DENIED",
            "spec_ref must be a material reference, not an expression",
        )
        normalized.append(
            {
                "item_id": item_id,
                "title": item["title"],
                "spec_ref": spec_ref,
                "task_id": None,
            }
        )
    gid = "group-" + uuid.uuid4().hex[:16]
    group = {
        "record_version": LEGACY_RECORD_VERSION,
        "group_id": gid,
        "project_id": c["project_id"],
        "title": req.get("title") or gid,
        "source": req.get("source"),
        "ordered_items": normalized,
        "created_at": time.time(),
    }
    with store.tx():
        store.save_group(group, "group_created")
        return {
            "api_version": 2,
            "operation_id": req.get("request_id"),
            "status": "succeeded",
            "group_id": gid,
            "record_version": LEGACY_RECORD_VERSION,
            "row_version": 1,
            "items": len(normalized),
            "completed": True,
            "note": "legacy ordered group: it never starts or advances its own items",
        }


def _create_plan(store: Store, req: dict[str, Any]) -> dict[str, Any]:
    snapshot = resolve_plan_inputs(store, req)
    gid = "group-" + uuid.uuid4().hex[:16]
    plan = validate_plan(snapshot)
    definition = plan["definition"]
    # The integration branch is derived from the (random) group id, so it is
    # attached after validation: it is not part of the plan identity and is
    # deliberately excluded from `plan_digest`.
    definition["integration_branch"] = "zoe/" + gid
    now = time.time()

    directory = group_dir(store, gid)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Frozen artefacts first, record second (TGD §4.6): a crash between the two
    # leaves orphan files that reconcile as "unfinished", never a record that
    # points at artefacts that do not exist.
    atomic(
        directory / "plan-r001.json",
        {
            "schema_version": PLAN_SCHEMA_VERSION,
            "plan_digest": plan["plan_digest"],
            "design_digest": plan["design_digest"],
            "base_sha": plan["base_sha"],
            "delivery_mode": plan["delivery_mode"],
            "definition": definition,
            "request_id": req.get("request_id"),
            "created_at": now,
        },
    )
    atomic(
        directory / "design-manifest.json",
        {
            "design_digest": plan["design_digest"],
            "files": snapshot["design_manifest"],
            "generated_at": now,
        },
    )
    atomic(
        directory / "baseline.json",
        {
            **snapshot["env"],
            "project_id": snapshot["project_id"],
            "group_id": gid,
            "captured_at": now,
            "adapters": _adapter_snapshot(snapshot["config"]),
        },
    )
    specs = directory / "specs"
    for member in snapshot["members"]:
        # The spec is frozen as the *same structure* ``_contracts.load_spec``
        # returns, so a member task can pass the snapshot path to `task create`
        # and get identical validation against frozen bytes (TGD §4.4).
        atomic(
            specs / (member["task_key"] + ".json"),
            {
                "text": member["spec_text"],
                "contract_version": member["spec_contract_version"],
                "document": member["spec_document"],
                "verification_semantics": _contracts.SCOPE_SEMANTICS,
            },
        )

    # Route rehearsal per member (TGD §4.5, §11.1): the *same* pure `plan_route`
    # the task path uses, written as a preview artefact. It is advisory — the
    # member task re-decides at `task start` with live facts. No probing here:
    # freezing a plan must not depend on a CLI that happens to answer today.
    routes = _route_previews(snapshot, directory, probe=False)

    group = {
        "record_version": RECORD_VERSION,
        "group_id": gid,
        "project_id": snapshot["project_id"],
        "state": "READY",
        "revision": 1,
        "op_seq": 0,
        "plan_digest": plan["plan_digest"],
        "design_digest": plan["design_digest"],
        "base_sha": plan["base_sha"],
        "delivery_mode": plan["delivery_mode"],
        "definition": definition,
        "runtime": {
            "integration_sha": None,
            "integration_branch_created": False,
            "integration_worktree": None,
            "integration_seq": 0,
            "lease": None,
            "policy_snapshot": {
                "policy_revision": digest(snapshot["config"])[:16],
                "routes": routes["combinations"],
            },
            "budget": {
                "starts_reserved": 0,
                "starts_released": 0,
                "release_basis": [],
                "first_running_at": None,
                "finalized_at": None,
            },
            "members": [
                {
                    "task_key": member["task_key"],
                    "task_id": None,
                    "validated_sha": None,
                    "integrated_sha": None,
                    "invalidated": False,
                    "last_block": None,
                }
                for member in definition["members"]
            ],
            "checks": [],
            "blockers": [],
            "last_op_id": None,
            "open_operations": [],
            "draining": False,
            "external_close": None,
            # 设计一致性审查的提交历史（含被判定为无效的那些）与组级修复单元。
            "reviews": [],
            "repairs": [],
        },
        "authorization_ref": req.get("authorization_ref"),
        "created_at": now,
        "updated_at": now,
    }
    with store.tx():
        store.save_group(group, "group_created")
    return {
        "api_version": 2,
        "operation_id": req.get("request_id"),
        "status": "succeeded",
        "group_id": gid,
        "record_version": RECORD_VERSION,
        "plan_digest": plan["plan_digest"],
        "design_digest": plan["design_digest"],
        "base_sha": plan["base_sha"],
        "integration_branch": definition["integration_branch"],
        "members": [member["task_key"] for member in definition["members"]],
        "order": plan["order"],
        "row_version": 1,
        "accepted": True,
        "completed": True,
        "plan_path": str(directory / "plan-r001.json"),
        "warnings": plan["warnings"],
        "note": "plan frozen; nothing was dispatched (run `group start` for that)",
    }


def _adapter_snapshot(config: dict) -> list[dict]:
    """Read-only capability snapshot; a broken profile is recorded, not raised."""
    from src import _profiles

    found = []
    for profile_id in sorted(config.get("agent_profiles") or {}):
        try:
            binding = _profiles.resolve_binding(config, profile_id)
        except Error as exc:
            found.append(
                {
                    "profile_id": profile_id,
                    "capability": "UNVERIFIED",
                    "error": {"code": exc.code, "message": str(exc)},
                }
            )
            continue
        found.append(
            {
                "profile_id": profile_id,
                "adapter_id": binding.adapter_id,
                "backend": binding.backend,
                "capability": binding.capability,
                "auth_mode": (binding.profile or {}).get("auth_mode", "unknown"),
                "model": binding.profile.get("model"),
            }
        )
    return found


# --------------------------------------------------------------------------- #
# preview                                                                (11.1)
# --------------------------------------------------------------------------- #


def _route_previews(snapshot: dict, directory: Path | None, probe: bool) -> dict:
    """Per-member route rehearsal (TGD §4.5), written as ``routes/<key>-a001.json``.

    This is the *same* pure ``plan_route`` the task path uses, recorded once per
    member so a host can explain "why this profile" before anything is
    dispatched. It is a rehearsal, not an admission: the member task re-decides
    at ``task start`` with live facts, and these files never authorise a run.
    """
    from src import _routing

    config = snapshot["config"]
    previews: dict[str, dict] = {}
    combinations: dict[str, dict] = {}
    if config.get("routing") is None:
        return {"routes": previews, "combinations": combinations}
    profiles = config.get("agent_profiles") or {}
    for item in snapshot["members"]:
        key = item["task_key"]
        try:
            plan = _routing.plan_route(
                config,
                {"task_type": item.get("task_type")},
                spec_document=item["spec_document"],
                task_id="-",
                trigger="group_plan",
                probe=probe,
                explicit_profile=item.get("requested_profile"),
            )
        except Error as exc:
            previews[key] = {"task_key": key, "decision": "blocked", "error": exc.to_dict()}
            continue
        selected = plan.get("selected_profile")
        facts = ((plan.get("snapshot") or {}).get("candidates") or {}).get(selected) or {}
        combination = None
        if selected and facts.get("adapter_id") and facts.get("backend"):
            combination = _routing.combination_id(
                adapter_id=facts["adapter_id"],
                backend=facts["backend"],
                profile=profiles.get(selected) or {},
                cli_version=facts.get("cli_version"),
                endpoint=None,
            )
        previews[key] = {
            "schema_version": 1,
            "task_key": key,
            "task_type": plan.get("task_type"),
            "mode": plan.get("mode"),
            "decision": plan.get("decision"),
            "reason": plan.get("reason"),
            "fallback_used": plan.get("fallback_used"),
            "selected_profile": selected,
            "combination_id": combination,
            "probed": probe,
            "candidates": plan.get("candidates"),
            "note": (
                "rehearsal only: the member task decides again at `task start`; "
                "this file is not an admission and does not reserve a slot"
            ),
        }
        if combination:
            combinations[key] = {"combination_id": combination}
        if directory is not None:
            atomic(directory / "routes" / f"{key}-a001.json", previews[key])
    return {"routes": previews, "combinations": combinations}


def preview(store: Store, req: dict[str, Any]) -> dict[str, Any]:
    """``group plan --project P --request-file plan.json``: validate, do not write.

    Same two steps as ``create``; nothing is written, so an unwritable plan can be
    discussed without leaving a half-frozen record behind.
    """
    snapshot = resolve_plan_inputs(store, req)
    plan = validate_plan(snapshot)
    # A preview may probe (nothing is written): the whole point is to show what
    # the router would do *on this host* before the plan is frozen.
    rehearsed = _route_previews(snapshot, None, probe=True)
    route_preview = rehearsed["routes"]
    return {
        "api_version": 2,
        "operation_id": req.get("request_id"),
        "status": "preview",
        "project_id": snapshot["project_id"],
        "plan_digest": plan["plan_digest"],
        "design_digest": plan["design_digest"],
        "base_sha": plan["base_sha"],
        "delivery_mode": plan["delivery_mode"],
        "members": [member["task_key"] for member in plan["definition"]["members"]],
        "order": plan["order"],
        "group_checks": [item["check_id"] for item in plan["definition"]["group_checks"]],
        "budget_caps": plan["definition"]["budget_caps"],
        "baseline": snapshot["env"],
        "route_preview": route_preview,
        "warnings": plan["warnings"],
        "wrote": False,
        "note": "preview only: no group record, task or event was written",
    }


# --------------------------------------------------------------------------- #
# bindings / release
# --------------------------------------------------------------------------- #


def bind_item(store: Store, group_id: str, item_id: str, task_id: str) -> None:
    """Bind a created task to a group member inside the caller's transaction."""
    group = store.group(group_id)
    if is_v3(group):
        bind_member(store, group, item_id, task_id)
        return
    items = group.get("ordered_items") or []
    match = next((item for item in items if item["item_id"] == safe_id(item_id)), None)
    require(match is not None, "INPUT_INCOMPLETE", "Unknown group item " + str(item_id))
    require(
        match.get("task_id") in (None, task_id),
        "STATE_CONFLICT",
        "Group item {} is already bound to {}".format(item_id, match.get("task_id")),
    )
    match["task_id"] = task_id
    store.save_group(group, "group_item_bound")


def repair_units(group: dict) -> list[dict]:
    """组级修复单元（TGD §12）。

    修复**不在** ``definition`` 里：冻结计划是不可变的，往里面插成员等于篡改
    计划摘要。修复是 runtime 事实，挂在 ``runtime.repairs`` 下，字段与成员对齐，
    所以 ``dispatch`` / ``integrate`` / ``members_view`` 不必分叉成两套代码。
    """
    return list((group.get("runtime") or {}).get("repairs") or [])


def all_units(group: dict) -> list[dict]:
    """成员的统一视图：冻结成员 + 运行期修复单元，顺序稳定。"""
    members = list((group.get("definition") or {}).get("members") or [])
    return [{**item, "kind": "member"} for item in members] + [
        {**item, "kind": "repair"} for item in repair_units(group)
    ]


def all_runtimes(group: dict) -> list[dict]:
    """成员与修复单元的 runtime 记录合并（对账、证据、派生共用一份）。"""
    runtime = group.get("runtime") or {}
    return list(runtime.get("members") or []) + repair_units(group)


def member(group: dict, task_key: str) -> dict:
    """Unit definition by ``task_key``: a frozen member or a repair unit."""
    key = safe_id(task_key)
    match = next((item for item in all_units(group) if item["task_key"] == key), None)
    require(match is not None, "INPUT_INCOMPLETE", "Unknown group member " + key)
    return match


def member_runtime(group: dict, task_key: str) -> dict:
    key = safe_id(task_key)
    match = next((item for item in all_runtimes(group) if item["task_key"] == key), None)
    require(match is not None, "INPUT_INCOMPLETE", "Unknown group member " + key)
    return match


def bind_member(store: Store, group: dict, task_key: str, task_id: str) -> None:
    """Bind a member task. One member, one task — never a second writer."""
    require_v3(group, "dispatch")
    require(
        group["state"] in ("READY", "RUNNING"),
        "STATE_CONFLICT",
        f"group is {group['state']}; members cannot be bound in that state",
    )
    runtime = member_runtime(group, task_key)
    require(
        runtime.get("task_id") in (None, task_id),
        "STATE_CONFLICT",
        "group member {} is already bound to {}".format(task_key, runtime.get("task_id")),
    )
    runtime["task_id"] = task_id
    runtime["last_block"] = None
    store.save_group(group, "group_member_bound")


# --------------------------------------------------------------------------- #
# derived state                                                          (4.7)
# --------------------------------------------------------------------------- #


def _task_or_none(store: Store, task_id: str | None) -> dict | None:
    if not task_id:
        return None
    try:
        return store.task(task_id)
    except Error:
        return None


def member_state(group: dict, runtime: dict, task: dict | None) -> tuple[str, str | None]:
    """Derived member state and the reason, in the priority order of TGD §4.7."""
    if runtime.get("integrated_sha") and not runtime.get("invalidated"):
        return "INTEGRATED", None
    if runtime.get("invalidated"):
        return "INVALIDATED", "evidence or policy no longer matches; rebind and re-verify"
    if task is not None:
        state = task.get("state")
        if state == "BLOCKED":
            return "BLOCKED", task.get("block_code")
        if state == "CANCELLED":
            return "CANCELLED", task.get("block_code")
        if state in ("PAUSED", "PAUSING"):
            return "PAUSED", None
        if state == "REVIEW_READY":
            if runtime.get("validated_sha") and runtime["validated_sha"] == task.get(
                "candidate_sha"
            ):
                return "VALIDATED", None
            # Review-ready without a matching validated SHA is not "validated":
            # nothing proved which bytes were checked.
            return "BLOCKED", "REVIEW_READY without a matching validated_sha"
        if state == "RUNNING":
            return "EXECUTING", None
        if state == "DONE":
            # A member is closed by the group, not by itself: `task accept` is
            # refused for group members (TGD §6.3).
            return "VALIDATED", "task was closed outside the group"
        if state == "READY":
            satisfied = _dependencies_satisfied(group, runtime["task_key"])
            return ("READY", None) if satisfied else ("WAITING", "dependency not integrated")
    satisfied = _dependencies_satisfied(group, runtime["task_key"])
    if task is None:
        return ("READY", None) if satisfied else ("WAITING", "dependency not integrated")
    return ("WAITING", "task exists but is not review ready")


def _dependencies_satisfied(group: dict, task_key: str) -> bool:
    item = member(group, task_key)
    for dep in item["depends_on"]:
        runtime = member_runtime(group, dep)
        if runtime.get("integrated_sha") and not runtime.get("invalidated"):
            continue
        return False
    return True


def members_view(store: Store, group: dict) -> list[dict]:
    """Every member with its derived state — read-only, no writes."""
    if not is_v3(group):
        return [
            {
                "item_id": item["item_id"],
                "title": item["title"],
                "task_id": item.get("task_id"),
                "state": "BOUND" if item.get("task_id") else "NOT_STARTED",
            }
            for item in group.get("ordered_items") or []
        ]
    out = []
    for item in all_units(group):
        runtime = member_runtime(group, item["task_key"])
        task = _task_or_none(store, runtime.get("task_id"))
        state, reason = member_state(group, runtime, task)
        out.append(
            {
                "task_key": item["task_key"],
                "kind": item.get("kind", "member"),
                "title": item["title"],
                "depends_on": list(item["depends_on"]),
                "requirement_ids": list(item["requirement_ids"]),
                "acceptance_ids": list(item["acceptance_ids"]),
                "state": state,
                "reason": reason,
                "task_id": runtime.get("task_id"),
                "validated_sha": runtime.get("validated_sha"),
                "integrated_sha": runtime.get("integrated_sha"),
                "invalidated": bool(runtime.get("invalidated")),
                "task": (
                    {
                        "state": task.get("state"),
                        "phase": task.get("phase"),
                        "block_code": task.get("block_code"),
                        "candidate_sha": task.get("candidate_sha"),
                        "profile_id": task.get("agent_profile_id"),
                        "combination_id": task.get("combination_id"),
                    }
                    if task
                    else None
                ),
            }
        )
    return out


def all_integrated(group: dict) -> bool:
    return all(
        runtime.get("integrated_sha") and not runtime.get("invalidated")
        for runtime in all_runtimes(group)
    )


def derive_group_state(
    group: dict, views: list[dict], review: dict | None = None
) -> tuple[str, list[dict]]:
    """Group state from member states plus runtime facts (TGD §6.1/§6.2).

    ``review`` 是 ``_review.summary()`` 的结果（可选，纯函数不读盘）。它参与
    判定而不是被写进状态：计划要求设计审查、当前版本却没有有效结论时，组不能
    停在 REVIEW_READY——"还没审查"与"审查通过"不是同一个状态。
    """
    state = group["state"]
    if state in _GROUP_TERMINAL:
        return state, []
    runtime = group["runtime"]
    if state == "PAUSED":
        return "PAUSED", list(runtime.get("blockers") or [])
    blockers: list[dict] = []
    for view in views:
        if view["state"] in ("BLOCKED", "CANCELLED", "INVALIDATED"):
            blockers.append(
                {
                    "member": view["task_key"],
                    "code": (view.get("task") or {}).get("block_code")
                    or ("INVALIDATED" if view["state"] == "INVALIDATED" else None)
                    or view["state"],
                    "state": view["state"],
                }
            )
        elif view["state"] == "WAITING" and view["reason"]:
            blockers.append(
                {
                    "member": view["task_key"],
                    "code": "DEPENDENCY_BLOCKED",
                    "kind": "dependency_failed",
                    "state": "WAITING",
                    "reason": view["reason"],
                }
            )
    # §7.4 drain 的落点：`pause`（不带 --stop）不打断正在跑的成员，只拦住新派发。
    # 安全点 = 没有任何成员在执行；到这一步才真正 PAUSED。只有 `resume` 能离开，
    # 不能让"恰好集成完了"把它直接推去 REVIEW_READY。
    if runtime.get("draining"):
        if any(view["state"] == "EXECUTING" for view in views):
            return "RUNNING", blockers
        return "PAUSED", blockers
    # 设计一致性审查是 REVIEW_READY 的必需条件，不是"额外的好消息"（§11.3）。
    review_blocker = _review_blocker(review)
    if group.get("state") == "REVIEW_READY":
        # `accept` moves it to DONE; anything else (a stale check, a new
        # integration, an unresolved finding) sends it back to RUNNING (§6.2).
        if _required_checks_pass(group) and review_blocker is None:
            return "REVIEW_READY", blockers
        if review_blocker is not None:
            blockers.append(review_blocker)
        else:
            blockers.append(_stale_blocker(group))
        return "RUNNING", blockers
    if all_integrated(group):
        if _required_checks_pass(group):
            if review_blocker is None:
                return "REVIEW_READY", blockers
            blockers.append(review_blocker)
            return "RUNNING", blockers
        body = _stale_blocker(group)
        if body.get("check_ids"):
            blockers.append(body)
        return "RUNNING", blockers
    active = [view for view in views if view["state"] in ("EXECUTING", "VALIDATED", "READY")]
    if not active:
        return "BLOCKED", blockers
    return "RUNNING", blockers


def stale_check_ids(group: dict) -> list[str]:
    """Required checks whose only recorded results belong to another subject."""
    runtime = group["runtime"]
    subject = runtime.get("integration_sha")
    required = {item["check_id"] for item in group["definition"]["group_checks"]}
    recorded: dict[str, set] = {}
    for item in runtime.get("checks") or []:
        recorded.setdefault(item["check_id"], set()).add(item.get("subject_sha"))
    return sorted(
        check_id
        for check_id in required
        if recorded.get(check_id) and subject not in recorded[check_id]
    )


def _stale_blocker(group: dict) -> dict:
    return {
        "code": "STALE_CHECKS",
        "kind": "stale_checks",
        "check_ids": stale_check_ids(group),
        "subject_sha": group["runtime"].get("integration_sha"),
        "reason": "recorded results belong to a different integration_sha; re-run the checks",
    }


def _review_blocker(review: dict | None) -> dict | None:
    """把"设计审查未满足"翻译成一个可执行的最小阻塞项（§12）。

    返回 ``None`` 表示不阻塞。区分四种原因，是因为对应的下一步不同：没做审查
    要去提交，旧版本的结论要重做，``FAIL`` 走修复预算，无效输出要重新提交。
    """
    if not review or review.get("satisfied"):
        return None
    state = review.get("state")
    blocker = {
        "code": "DESIGN_REVIEW_REQUIRED",
        "kind": "design_review",
        "review_state": state,
        "subject_sha": review.get("subject_sha"),
        "reason": review.get("reason"),
    }
    findings = review.get("unresolved") or []
    if findings:
        blocker["code"] = "DESIGN_REVIEW_FAILED"
        blocker["acceptance_ids"] = [item["acceptance_id"] for item in findings]
    return blocker


def _required_checks_pass(group: dict) -> bool:
    runtime = group["runtime"]
    subject = runtime.get("integration_sha")
    if not subject:
        return False
    required = {item["check_id"] for item in group["definition"]["group_checks"]}
    passed = {
        item["check_id"]
        for item in runtime.get("checks") or []
        if item.get("subject_sha") == subject and item.get("status") == "PASS"
    }
    return required <= passed


def progress(store: Store, group: dict) -> dict:
    views = members_view(store, group)
    counts = {state: 0 for state in MEMBER_STATES}
    for view in views:
        counts[view["state"]] = counts.get(view["state"], 0) + 1
    runtime = group["runtime"]
    budget = runtime["budget"]
    billable = max(0, budget["starts_reserved"] - budget["starts_released"])
    wall = None
    if budget.get("first_running_at"):
        end = budget.get("finalized_at") or time.time()
        wall = int(end - budget["first_running_at"])
    return {
        "members_total": len(views),
        "members_frozen": len((group.get("definition") or {}).get("members") or []),
        "repairs_total": len((group.get("runtime") or {}).get("repairs") or []),
        "integrated": counts.get("INTEGRATED", 0),
        "executing": counts.get("EXECUTING", 0),
        "validated": counts.get("VALIDATED", 0),
        "waiting": counts.get("WAITING", 0),
        "ready": counts.get("READY", 0),
        "blocked": counts.get("BLOCKED", 0) + counts.get("INVALIDATED", 0),
        "integration_sha": runtime.get("integration_sha"),
        "starts_billable": billable,
        "starts_reserved": budget["starts_reserved"],
        "starts_released": budget["starts_released"],
        "max_starts": group["definition"]["budget_caps"]["max_starts"],
        # 命名如其所是：墙钟时长，包含机器休眠与等待（TGD §7.3）。
        "wall_seconds_inclusive": wall,
        "max_wall_seconds": group["definition"]["budget_caps"]["max_wall_seconds"],
    }


def status(store: Store, group_id: str, refresh: bool = False) -> dict[str, Any]:
    """``group status``. Read-only by default; ``refresh`` reconciles first."""
    group = store.group(group_id)
    if refresh and is_v3(group):
        from src import _group_runner

        group = _group_runner.reconcile(store, group)
    if not is_v3(group):
        return _legacy_status(store, group)
    views = members_view(store, group)
    runtime = group["runtime"]
    lease = runtime.get("lease") or {}
    # 局部导入：`_review` 在模块级依赖本模块，放在顶部会形成循环。
    from src import _review

    review = _review.summary(store, group)
    return {
        "api_version": 2,
        # Read-only: the cursor is a view id, never an ``op_seq`` increment.
        "operation_id": f"grp-{group['group_id']}-status-r{group['revision']}"
        f"-s{int(group.get('op_seq') or 0)}",
        "evidence_refs": [
            row["evidence_ref"] for row in runtime.get("checks") or [] if row.get("evidence_ref")
        ],
        "group_id": group["group_id"],
        "project_id": group["project_id"],
        "record_version": RECORD_VERSION,
        "state": group["state"],
        "revision": group["revision"],
        "plan_digest": group["plan_digest"],
        "design_digest": group["design_digest"],
        "base_sha": group["base_sha"],
        "delivery_mode": group["delivery_mode"],
        "row_version": group["row_version"],
        "members": views,
        "progress": progress(store, group),
        "checks": list(runtime.get("checks") or []),
        "blockers": list(runtime.get("blockers") or []),
        # 推进是否停下、为什么停下，是宿主必须能读到的**事实**（v1.4.1）：
        # `exit_reason` 与 `group start` 返回值同源（_group_runner.EXIT_REASONS）。
        "lease": {
            "lease_id": lease.get("lease_id"),
            "state": lease.get("state"),
            "pid": lease.get("pid"),
            "ticks": lease.get("ticks"),
            "exited_at": lease.get("exited_at"),
            "exit_reason": lease.get("exit_reason"),
        }
        if lease
        else None,
        "review": review,
        "repairs": [
            {
                "task_key": item["task_key"],
                "task_id": item.get("task_id"),
                "parent_acceptance_id": item.get("parent_acceptance_id"),
                "affected_members": list(item.get("affected_members") or []),
                "state": next(
                    (view["state"] for view in views if view["task_key"] == item["task_key"]),
                    None,
                ),
            }
            for item in repair_units(group)
        ],
        "next_actions": next_actions(group, views),
        "event_cursor": f"{store.epoch()}:{store.max_event_id(None)}",
        "note": (
            "structural coverage only: a mapped requirement is not a satisfied "
            "requirement; semantic judgement comes from the design review "
            "(`group review`) or the actual check runs"
        ),
    }


def response_fields(group: dict) -> dict:
    """Fields *every* group response carries (TGD §11.2).

    One helper instead of five copies: a host that reads ``revision`` or
    ``plan_digest`` from ``group status`` must not find them missing from
    ``group resume``. ``blockers`` is the current fact, not a history.
    """
    return {
        "revision": group["revision"],
        "plan_digest": group["plan_digest"],
        "blockers": list((group.get("runtime") or {}).get("blockers") or []),
    }


def envelope(store: Store, group: dict) -> dict:
    """The full §11.2 envelope for a *mutating* group command.

    ``response_fields`` covers the identity of the group; this adds the three
    things a host needs in order to keep working without a second call:
    how far along the group is (``progress``), what to do next
    (``next_actions``) and where to resume the event stream (``event_cursor``).

    ``group status`` builds the same envelope inline because it can avoid the
    second member scan; everything else must not drift from it.
    """
    views = members_view(store, group)
    body = {
        **response_fields(group),
        "progress": progress(store, group),
        "next_actions": next_actions(group, views),
        "event_cursor": f"{store.epoch()}:{store.max_event_id(None)}",
    }
    return body


def next_actions(group: dict, views: list[dict]) -> list[str]:
    gid = group["group_id"]
    if group["state"] == "DONE":
        return []
    if group["state"] == "CANCELLED":
        return []
    if group["state"] == "REVIEW_READY":
        return [f"group accept {gid} --request-file accept.json", f"group report {gid}"]
    if group["state"] == "PAUSED":
        return [f"group resume {gid} --request-file resume.json"]
    if group["state"] == "READY":
        return [f"group start {gid} --request-file start.json"]
    if group["state"] == "BLOCKED":
        return [f"group status {gid} --refresh", f"group resume {gid} --request-file resume.json"]
    return [f"group status {gid} --refresh"]


def _legacy_status(store: Store, group: dict) -> dict[str, Any]:
    items = []
    for item in group.get("ordered_items") or []:
        task = _task_or_none(store, item.get("task_id"))
        items.append(
            {
                "item_id": item["item_id"],
                "title": item["title"],
                "spec_ref": item["spec_ref"],
                "task_id": item.get("task_id"),
                "state": task["state"] if task else "NOT_STARTED",
                "phase": task["phase"] if task else None,
                "block_code": task.get("block_code") if task else None,
            }
        )
    return {
        "api_version": 2,
        "group_id": group["group_id"],
        "project_id": group["project_id"],
        "record_version": LEGACY_RECORD_VERSION,
        "title": group["title"],
        "row_version": group["row_version"],
        "items": items,
        "members": items,
        "next_item": next(
            (
                item["item_id"]
                for item in items
                if item["state"] in ("NOT_STARTED", "BLOCKED", "PAUSED")
            ),
            None,
        ),
        "note": "legacy ordered group: a report only, it never starts the next task",
    }


# --------------------------------------------------------------------------- #
# request ids / revisions
# --------------------------------------------------------------------------- #


def next_request_id(group: dict, action: str) -> str:
    """``grp-<gid>-<action>-r<revision>-s<seq>`` (TGD §0.6-2).

    The ``op_seq`` counter is what keeps a second ``pause`` from replaying the
    first one: same group, same action, same revision, different sequence.
    """
    return "grp-{}-{}-r{}-s{}".format(
        group["group_id"], action, group["revision"], int(group.get("op_seq") or 0)
    )


def bump_op_seq(group: dict) -> None:
    group["op_seq"] = int(group.get("op_seq") or 0) + 1


def summarize_markdown(store: Store, group: dict) -> str:
    """Compact progress view written to ``reports/progress.md``."""
    views = members_view(store, group)
    info = progress(store, group)
    lines = [
        f"# {group['group_id']}",
        "",
        f"- State: {group['state']} ｜ Revision: {group['revision']}",
        f"- Plan digest: {group['plan_digest']}",
        f"- Base: {group['base_sha']} ｜ Integration: {info['integration_sha']}",
        f"- Members: {info['integrated']}/{info['members_total']} integrated, "
        f"{info['executing']} executing, {info['blocked']} blocked",
        f"- Starts: {info['starts_billable']}/{info['max_starts']} billable "
        f"({info['starts_released']} released)",
        "",
        "| member | state | task | integrated |",
        "|---|---|---|---|",
    ]
    for view in views:
        lines.append(
            "| {} | {} | {} | {} |".format(
                view["task_key"],
                view["state"],
                (view["task_id"] or "-"),
                ((view["integrated_sha"] or "-")[:12]),
            )
        )
    lines.append("")
    return "\n".join(lines)


def write_progress(store: Store, group: dict) -> Path:
    from src._util import atomic_text

    path = group_dir(store, group["group_id"]) / "reports" / "progress.md"
    atomic_text(path, summarize_markdown(store, group))
    return path


def read_plan(store: Store, group_id: str) -> dict:
    path = group_dir(store, group_id) / "plan-r001.json"
    require(path.is_file(), "INPUT_INCOMPLETE", "Frozen plan is missing for " + group_id)
    return read(path)
