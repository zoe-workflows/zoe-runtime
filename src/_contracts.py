"""Explicit request/spec validation (no schema engine at runtime).

A spec may be either the legacy Markdown text contract (v1, requires ``AC-`` ids)
or the structured ``spec.json`` contract (v2 / v3). The structured form is
validated here for exactly the things the controller can check: unique acceptance
ids, verification references that exist in the approved check list, non-blocking
open questions, and ``allowed_paths`` that only *tighten* the project policy.
Business correctness is never decided here.

**v3 的唯一目的**是让"验收"成为可判定的东西，而不是一句整体结论：

* ``acceptance_review`` —— 这个任务是否必须被逐条 AC 判定过才能 accept；
* ``decisions`` —— 设计裁定（D 类）在 spec 里有一席之地，open 的裁定阻断验收；
* ``progress_refs`` —— AC 与外部需求文档的复选框绑定，勾选成为验收前置条件；
* ``verification.judge`` / ``supporting_checks`` —— manual AC 的判者，以及
  "机器能判的那一半"（必须是项目已批准的 check，不新增执行面）。

v2 仍然是合法输入。但 v3 字段出现在 v2 里**必须报错**，不能静默忽略：
旧运行器会照单收下然后谁都不按它执行，这正是 2026-11-06 事故里"改了 spec
模板仍然没有闸门"的那半个原因。
"""

from __future__ import annotations

import json
from pathlib import Path

from src import _paths
from src._util import Error, is_integer, require

SPEC_CONTRACT_VERSION = 3

#: 仍然合法的旧版结构化契约。v2 没有 v3 字段，行为与 v0.5 一致。
SPEC_ACCEPTED_VERSIONS = (2, 3)

#: manual AC 的判者。``independent`` 表示判者不能是产生该改动的同一上下文。
JUDGE_KINDS = ("independent", "owner", "human")

#: 逐条 AC 判定的执行者类型。
REVIEW_MODES = ("agent", "human", "either")

#: 设计裁定状态。``open`` 不阻断 create，但阻断 accept。
DECISION_STATUSES = ("open", "resolved")

#: 只在 v3 才被读取的顶层键（出现在 v2 里直接拒绝）。
V3_TOP_LEVEL_KEYS = (
    "acceptance_review",
    "decisions",
    "progress_refs",
    "depends_on",
)

#: Marker written into generated drafts. It must fail validation wherever it
#: survives: ``safe_id`` only guards id-shaped fields, so a placeholder in
#: ``title`` / an acceptance statement / an instructions block would otherwise
#: pass and reach an agent as literal text (config-friction v5 §B8).
PLACEHOLDER = "<FILL:"

#: Verification semantics frozen *inside* the spec revision file (v0.5.1 §6.2.1).
#: A revision frozen before v0.5.1 has no marker and is therefore
#: ``FULL_SEMANTICS``: an old spec that happens to carry ``verification_scope``
#: (old versions did not reject unknown top-level keys) must not be narrowed.
FULL_SEMANTICS = 1
SCOPE_SEMANTICS = 2


def prefix_covered(path: str, allowed) -> bool:
    """Delegates to the one path-comparison module (never a bare ``startswith``)."""
    return _paths.covers(path, allowed)


#: Backwards-compatible alias for call sites written before it went public.
_prefix_covered = prefix_covered


def find_placeholder(value, where: str = "") -> str | None:
    """Return the location of the first ``<FILL:`` marker, or ``None``."""
    if isinstance(value, str):
        return where or "<root>" if PLACEHOLDER in value else None
    if isinstance(value, dict):
        for key, item in value.items():
            hit = find_placeholder(item, f"{where}.{key}" if where else str(key))
            if hit:
                return hit
    elif isinstance(value, list):
        for index, item in enumerate(value):
            hit = find_placeholder(item, f"{where}[{index}]")
            if hit:
                return hit
    return None


def reject_placeholders(value, where: str = "request") -> None:
    """Refuse a payload that still carries a generated draft marker (v5 §B8).

    Runs on the whole request, not just id-shaped fields: ``safe_id`` protects
    ``project_id`` / ``check_id`` but nothing would stop a ``<FILL:...>`` title
    or acceptance statement from reaching an agent as literal instruction text.
    """
    hit = find_placeholder(value)
    if not hit:
        return
    raise Error(
        "INPUT_INCOMPLETE",
        f"Unreplaced draft placeholder {PLACEHOLDER}...> at {hit}",
        details={
            "count": 1,
            "errors": [
                {
                    "code": "INPUT_INCOMPLETE",
                    "field": f"{where}:{hit}",
                    "reason": "unreplaced draft placeholder",
                    "message": f"fill in {hit} before creating the task",
                    "suggested_change": "replace the placeholder text with a real value",
                    "requires_approval": False,
                }
            ],
        },
    )


def spec_scope(document: dict | None) -> list[str] | None:
    """The task scope a spec declares, normalized, or ``None`` when it declares none.

    ``None`` and ``[]`` mean different things and must not be merged: ``None`` is
    "inherit the project scope" (historical behaviour), ``[]`` is "declares an
    empty scope", which is refused at ``create`` because a task that may write
    nowhere cannot deliver anything.
    """
    if not isinstance(document, dict) or "allowed_paths" not in document:
        return None
    values = document.get("allowed_paths")
    if values is None:
        return None
    if not isinstance(values, list):
        return None
    # 只用于已通过 ``validate_spec_text`` 的文档；这里仍做一次防御性过滤，
    # 因为调用方（scope 冻结）不接受 ``None`` 条目，也不应看到重复项。
    out: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = _paths.normalize(value)
        if normalized and normalized not in out:
            out.append(normalized)
    return out


def verification_scope(document: dict | None, checks: list[dict]) -> list[str] | None:
    """Return the check ids the spec *declares*, in canonical (config) order.

    ``None`` means "no declaration at all" — every check runs, which is the
    pre-v0.5.1 behaviour. An explicit list is a *selection*, not an approval:
    it is validated here for internal consistency only.

    Rules (§6.2):

    * the key must be absent (full run) or an object with a non-empty
      ``checks`` array of ids that exist in the project's frozen check list;
    * every ``verification.check_id`` used by an acceptance criterion must be
      inside the declared list, or the spec contradicts itself;
    * duplicates are de-duplicated, not rejected.
    """
    if not isinstance(document, dict) or "verification_scope" not in document:
        return None
    declared = document.get("verification_scope")
    require(
        isinstance(declared, dict),
        "INPUT_INCOMPLETE",
        "verification_scope must be an object with a checks array",
    )
    ids = declared.get("checks")
    require(
        isinstance(ids, list) and ids,
        "INPUT_INCOMPLETE",
        "verification_scope.checks must be a non-empty array",
    )
    require(
        all(isinstance(item, str) and item for item in ids),
        "INPUT_INCOMPLETE",
        "verification_scope.checks must contain check ids",
    )
    known = [check["id"] for check in checks]
    unknown = sorted({item for item in ids if item not in known})
    require(
        not unknown,
        "INPUT_INCOMPLETE",
        "verification_scope references unknown checks: " + ", ".join(unknown),
    )
    wanted = set(ids)
    for criterion in document.get("acceptance_criteria") or []:
        if not isinstance(criterion, dict):
            continue
        verification = criterion.get("verification") or {}
        if verification.get("type") != "check":
            continue
        check_id = verification.get("check_id")
        require(
            check_id in wanted,
            "INPUT_INCOMPLETE",
            "Acceptance {} references check {!r} outside verification_scope".format(
                criterion.get("id"), check_id
            ),
        )
    return [check_id for check_id in known if check_id in wanted]


def frozen_semantics(frozen: dict) -> int:
    """Read the verification semantics marker from a frozen spec file.

    Missing marker = a revision frozen by v0.5.0 or earlier = full semantics.
    Any other value is refused rather than guessed (§6.2.1).
    """
    value = frozen.get("verification_semantics", FULL_SEMANTICS)
    require(
        value in (FULL_SEMANTICS, SCOPE_SEMANTICS),
        "SPEC_CHANGED",
        f"Unknown verification semantics {value!r}",
    )
    return value


def parse_spec(text: str, config: dict) -> tuple[int, dict | None]:
    """Parse and validate a spec **from its frozen text** (single entry point).

    ``spec-<rev>.json`` also stores a human-readable ``document`` copy; it is
    never used for decisions. Reading only the bytes that were hashed removes
    the "validate one field, act on another" gap (§6.3).
    """
    return validate_spec_text(text, config)


# --------------------------------------------------------------------------- #
# v3 只读访问器：所有下游（pipeline / reports / review / coverage）都走这里，
# 不各自 ``document.get(...)``，否则默认值会在四个地方各写一遍。
# --------------------------------------------------------------------------- #


def acceptance_ids(document: dict | None) -> list[str]:
    """Declared AC ids, in spec order. The single source for "必须被判定" 的集合。"""
    if not isinstance(document, dict):
        return []
    found: list[str] = []
    for criterion in document.get("acceptance_criteria") or []:
        if not isinstance(criterion, dict):
            continue
        ac_id = criterion.get("id")
        if isinstance(ac_id, str) and ac_id and ac_id not in found:
            found.append(ac_id)
    return found


def acceptance_review_block(document: dict | None) -> dict:
    """``acceptance_review`` with defaults applied (v2 恒为空块)."""
    block = (document or {}).get("acceptance_review") if isinstance(document, dict) else None
    if not isinstance(block, dict):
        block = {}
    return {
        "required": bool(block.get("required")),
        "mode": block.get("mode") or "either",
        "independent": bool(block.get("independent")),
    }


def review_required(document: dict | None) -> bool:
    return acceptance_review_block(document)["required"]


def manual_verifications(document: dict | None) -> dict[str, dict]:
    """``ac_id -> verification``，只收 ``type=manual`` 的条目。"""
    found: dict[str, dict] = {}
    if not isinstance(document, dict):
        return found
    for criterion in document.get("acceptance_criteria") or []:
        if not isinstance(criterion, dict):
            continue
        verification = criterion.get("verification") or {}
        if verification.get("type") == "manual":
            found[criterion.get("id")] = verification
    return found


def decisions(document: dict | None) -> list[dict]:
    """设计裁定（D 类）。v2 spec 恒为空列表。"""
    if not isinstance(document, dict):
        return []
    return [item for item in (document.get("decisions") or []) if isinstance(item, dict)]


def open_decisions(document: dict | None) -> list[dict]:
    """尚未拍板的裁定。它们不阻断 create，但阻断 accept。"""
    return [item for item in decisions(document) if item.get("status") != "resolved"]


def progress_refs(document: dict | None) -> list[dict]:
    if not isinstance(document, dict):
        return []
    return [item for item in (document.get("progress_refs") or []) if isinstance(item, dict)]


def depends_on(document: dict | None) -> list[str]:
    """上游需求标记（``R-10`` 一类）。v2 spec 恒为空列表。

    用 **marker** 而不是 ``task_id``：任务 id 由运行时生成、不可预先声明，而
    marker 是需求文档里本来就有的稳定编号。这样"谁依赖谁"能在派发之前写下来。
    """
    if not isinstance(document, dict):
        return []
    found: list[str] = []
    for item in document.get("depends_on") or []:
        if isinstance(item, str) and item.strip() and item not in found:
            found.append(item)
    return found


def _validate_acceptance_review(document: dict) -> None:
    block = document.get("acceptance_review")
    if block is None:
        return
    require(
        isinstance(block, dict),
        "INPUT_INCOMPLETE",
        "acceptance_review must be an object",
    )
    require(
        isinstance(block.get("required", False), bool),
        "INPUT_INCOMPLETE",
        "acceptance_review.required must be a boolean",
    )
    require(
        block.get("mode", "either") in REVIEW_MODES,
        "INPUT_INCOMPLETE",
        "acceptance_review.mode must be " + "/".join(REVIEW_MODES),
    )
    require(
        isinstance(block.get("independent", False), bool),
        "INPUT_INCOMPLETE",
        "acceptance_review.independent must be a boolean",
    )


def _validate_decisions(document: dict) -> None:
    items = document.get("decisions")
    if items is None:
        return
    require(isinstance(items, list), "INPUT_INCOMPLETE", "decisions must be an array")
    seen: set[str] = set()
    for index, item in enumerate(items):
        require(
            isinstance(item, dict),
            "INPUT_INCOMPLETE",
            f"decisions[{index}] must be an object",
        )
        decision_id = item.get("id")
        require(
            isinstance(decision_id, str) and decision_id,
            "INPUT_INCOMPLETE",
            f"decisions[{index}] needs an id",
        )
        require(
            decision_id not in seen,
            "INPUT_INCOMPLETE",
            "Duplicate decision id " + decision_id,
        )
        seen.add(decision_id)
        require(
            isinstance(item.get("question"), str) and item["question"],
            "INPUT_INCOMPLETE",
            "Decision {} needs a question".format(decision_id),
        )
        options = item.get("options")
        require(
            isinstance(options, list)
            and len(options) >= 2
            and all(isinstance(option, str) and option.strip() for option in options),
            "INPUT_INCOMPLETE",
            "Decision {} needs at least two non-empty options".format(decision_id),
        )
        status = item.get("status", "open")
        require(
            status in DECISION_STATUSES,
            "INPUT_INCOMPLETE",
            "Decision {} status must be open|resolved".format(decision_id),
        )
        if status == "resolved":
            # 裁定必须有拍板人和结论：只有 status=resolved 而没有署名，
            # 等于允许有人把"我决定了"写成事实而不承担归属。
            require(
                isinstance(item.get("resolved_by"), str) and item["resolved_by"].strip(),
                "INPUT_INCOMPLETE",
                "Decision {} resolved without resolved_by".format(decision_id),
            )
            require(
                isinstance(item.get("resolution"), str) and item["resolution"].strip(),
                "INPUT_INCOMPLETE",
                "Decision {} resolved without a resolution text".format(decision_id),
            )


def _validate_depends_on(document: dict) -> None:
    """``depends_on`` 的 marker 列表。

    自依赖在这里就拒绝：一条任务声称依赖自己认领的需求，只可能是编号写错，
    而"派发时永远等不到自己"这种失败很难在事后看出来。
    """
    items = document.get("depends_on")
    if items is None:
        return
    require(isinstance(items, list), "INPUT_INCOMPLETE", "depends_on must be an array")
    seen: set[str] = set()
    for index, item in enumerate(items):
        require(
            isinstance(item, str) and item.strip(),
            "INPUT_INCOMPLETE",
            f"depends_on[{index}] must be a non-empty marker (e.g. R-10)",
        )
        require(
            item not in seen,
            "INPUT_INCOMPLETE",
            "Duplicate depends_on marker " + item,
        )
        seen.add(item)
    own = {
        ref.get("marker")
        for ref in progress_refs(document)
        if isinstance(ref.get("marker"), str) and ref.get("marker")
    }
    overlap = sorted(seen & own)
    require(
        not overlap,
        "INPUT_INCOMPLETE",
        "depends_on 不能包含本任务自己认领的 marker：{}（自依赖永远等不到）".format(
            ", ".join(overlap)
        ),
    )


def _validate_progress_refs(document: dict, ids: set[str], config: dict) -> None:
    items = document.get("progress_refs")
    if items is None:
        return
    require(isinstance(items, list), "INPUT_INCOMPLETE", "progress_refs must be an array")
    for index, item in enumerate(items):
        require(
            isinstance(item, dict),
            "INPUT_INCOMPLETE",
            f"progress_refs[{index}] must be an object",
        )
        ac_id = item.get("acceptance_id")
        require(
            ac_id in ids,
            "INPUT_INCOMPLETE",
            "progress_refs[{}] references unknown acceptance {}".format(index, ac_id),
        )
        doc = item.get("doc")
        require(
            isinstance(doc, str) and doc.strip(),
            "INPUT_INCOMPLETE",
            "progress_refs[{}].doc required".format(index),
        )
        normalized = _paths.normalize(doc)
        require(
            normalized not in ("", ".") and not Path(normalized).is_absolute(),
            "INPUT_INCOMPLETE",
            "progress_refs[{}].doc must be a repository-relative path".format(index),
        )
        require(
            isinstance(item.get("marker"), str) and item["marker"].strip(),
            "INPUT_INCOMPLETE",
            "progress_refs[{}].marker required".format(index),
        )


def validate_spec_text(text: str, config: dict) -> tuple[int, dict | None]:
    """Return ``(contract_version, document)`` or raise a structured error."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        require("AC-" in text, "INPUT_INCOMPLETE", "Spec requires acceptance IDs")
        return 1, None
    require(isinstance(document, dict), "INPUT_INCOMPLETE", "Spec JSON must be an object")
    version = document.get("schema_version")
    require(
        is_integer(version) and version in SPEC_ACCEPTED_VERSIONS,
        "INPUT_INCOMPLETE",
        "Spec schema_version must be one of "
        + " / ".join(str(v) for v in SPEC_ACCEPTED_VERSIONS),
    )
    # v3 字段出现在 v2 里必须**报错**，不能静默收下：旧运行器不会执行它们，
    # 而作者会以为自己已经声明了验收门禁（这正是 2026-11-06 的失败模式）。
    if version < 3:
        stray = [key for key in V3_TOP_LEVEL_KEYS if key in document]
        require(
            not stray,
            "INPUT_INCOMPLETE",
            "Spec 字段 {} 需要 schema_version 3（当前 {}）；"
            "在 v2 上它们不会被任何执行路径读取".format(", ".join(stray), version),
        )
    # 草稿占位符必须失败：``safe_id`` 只管 id 形状，title / statement /
    # instructions 里的 ``<FILL:...>`` 会原样进入 prompt（v5 §B8）。
    reject_placeholders(document, "spec")
    require(
        isinstance(document.get("title"), str) and document["title"],
        "INPUT_INCOMPLETE",
        "Spec requires a title",
    )
    criteria = document.get("acceptance_criteria")
    require(
        isinstance(criteria, list) and criteria,
        "INPUT_INCOMPLETE",
        "Spec requires acceptance_criteria",
    )
    ids = set()
    # ``config`` 未必经过 ``validate_config``（例如 prepare 的未注册模式、
    # 已冻结 spec 的 draft 拷贝），非对象条目不能在这里变成裸 TypeError。
    checks = {
        check.get("id")
        for check in config.get("checks", [])
        if isinstance(check, dict) and check.get("id")
    }
    for criterion in criteria:
        require(
            isinstance(criterion, dict),
            "INPUT_INCOMPLETE",
            "Acceptance criterion must be an object",
        )
        ac_id = criterion.get("id")
        require(isinstance(ac_id, str) and ac_id, "INPUT_INCOMPLETE", "Acceptance id required")
        require(ac_id not in ids, "INPUT_INCOMPLETE", "Duplicate acceptance id " + ac_id)
        ids.add(ac_id)
        require(
            isinstance(criterion.get("statement"), str) and criterion["statement"],
            "INPUT_INCOMPLETE",
            "Acceptance statement required for " + ac_id,
        )
        verification = criterion.get("verification") or {}
        kind = verification.get("type")
        require(
            kind in ("check", "manual"),
            "INPUT_INCOMPLETE",
            f"Acceptance {ac_id} needs verification.type check|manual",
        )
        if kind == "check":
            require(
                verification.get("check_id") in checks,
                "INPUT_INCOMPLETE",
                "Acceptance {} references an unknown check {!r}".format(
                    ac_id, verification.get("check_id")
                ),
            )
        else:
            require(
                isinstance(verification.get("instructions"), str) and verification["instructions"],
                "INPUT_INCOMPLETE",
                f"Manual acceptance {ac_id} needs instructions",
            )
        if version < 3:
            stray = [
                key
                for key in ("judge", "supporting_checks")
                if key in verification
            ]
            require(
                not stray,
                "INPUT_INCOMPLETE",
                "Acceptance {} 的 verification.{} 需要 schema_version 3".format(
                    ac_id, "/".join(stray)
                ),
            )
        else:
            # manual AC 的判者。缺省不放行：没有判者的 manual 项必然退化成
            # 报告里的"未记录逐项人工结论"，也就是 2026-11-06 事故的形状。
            if kind == "manual":
                judge = verification.get("judge")
                require(
                    judge in JUDGE_KINDS,
                    "INPUT_INCOMPLETE",
                    "Manual acceptance {} needs verification.judge in {}".format(
                        ac_id, "/".join(JUDGE_KINDS)
                    ),
                )
                # 辅助证据只能引用**已批准的** check：不允许 spec 内联 shell
                # 命令，那会绕开 checks 白名单与 CHECK_ESCAPES_WORKSPACE。
                supporting = verification.get("supporting_checks") or []
                require(
                    isinstance(supporting, list)
                    and all(isinstance(item, str) and item for item in supporting),
                    "INPUT_INCOMPLETE",
                    f"Acceptance {ac_id} supporting_checks must be an array of check ids",
                )
                unknown = [item for item in supporting if item not in checks]
                require(
                    not unknown,
                    "INPUT_INCOMPLETE",
                    "Acceptance {} supporting_checks 引用未批准的 check：{}".format(
                        ac_id, ", ".join(unknown)
                    ),
                )
    # 显式验证范围：键缺省=全量；声明了就必须与 AC 自洽（v0.5.1 §6.2）。
    verification_scope(document, config.get("checks") or [])
    if version >= 3:
        _validate_acceptance_review(document)
        _validate_decisions(document)
        _validate_progress_refs(document, ids, config)
        _validate_depends_on(document)
    blocking = [q for q in (document.get("open_questions") or []) if q]
    require(
        not blocking,
        "SPEC_CLARIFICATION_REQUIRED",
        "Blocking open questions must be resolved before start",
    )
    paths = document.get("allowed_paths")
    if paths is not None:
        require(
            isinstance(paths, list) and all(isinstance(p, str) and p.strip() for p in paths),
            "INPUT_INCOMPLETE",
            "allowed_paths must be an array of non-empty strings "
            "(omit the key entirely to inherit the project scope)",
        )
        normalized_paths: list[str] = []
        for path in paths:
            normalized = _paths.normalize(path)
            # ``""`` / ``"."`` / 绝对路径都不是仓库内相对范围：前两者会让包含
            # 判定退化成"任意位置"，后者在 git 相对路径比较里永远匹配不上。
            require(
                normalized not in ("", ".") and not Path(normalized).is_absolute(),
                "INPUT_INCOMPLETE",
                f"allowed_paths entry {path!r} must be a repository-relative path",
            )
            if normalized not in normalized_paths:
                normalized_paths.append(normalized)
        # 显式空范围与"没有声明"是两件事，不得合并（v5 §B0-a）。
        require(
            normalized_paths,
            "POLICY_DENIED",
            "allowed_paths declares an empty scope; a task that may write nowhere cannot deliver",
        )
        # ``None`` 表示没有项目边界（prepare 的未注册模式只做结构校验）。
        project_paths = config.get("allowed_paths")
        if project_paths is not None:
            widened = [p for p in normalized_paths if not _prefix_covered(p, project_paths)]
            require(
                not widened,
                "POLICY_DENIED",
                "Spec allowed_paths widen the project policy: " + ", ".join(widened),
            )
        # 显式声明保护路径本身或其子路径 = 要求修改受保护目录，准备期即拒；
        # 声明保护路径的**祖先**是合法的（"允许 src，但 src/security 除外"）。
        protected = list(config.get("protected_paths") or [])
        explicit = [p for p in normalized_paths if _paths.declared_under(p, protected)]
        require(
            not explicit,
            "POLICY_DENIED",
            "Spec allowed_paths declare protected paths: "
            + ", ".join(explicit)
            + " (protected entries win at delivery time; request the parent directory instead)",
        )
    # 返回**这份 spec 实际使用的**版本，不是当前常量：v2 spec 被记成 3 会让
    # 后续按 v3 语义去读一个没有 v3 字段的文档（`acceptance_review.required`
    # 恒为 false，恰好不出错，但记录失真到无法审计）。
    return version, document


def load_spec(path: str | Path, config: dict) -> dict:
    """Read, validate and freeze a spec file into the task material record.

    The frozen file gets a fourth key, ``verification_semantics``: the marker
    that decides whether ``verification_scope`` inside ``text`` may be
    interpreted at all. Writing it at freeze time (create/revise) is what makes
    "old revisions stay full" a fact of the data instead of a default value
    (§6.2.1).

    The path may also point at an **already frozen** record (the shape this
    function returns, which is what ``groups/<gid>/specs/<task_key>.json``
    holds). That is what lets a group member task be created from a snapshot
    with identical validation: the *text* is validated again, never the wrapper
    (TGD §4.4).
    """
    raw = Path(path).read_text()
    frozen = _frozen_spec(raw)
    if frozen is not None:
        text = frozen["text"]
        version, document = validate_spec_text(text, config)
        require(
            version == frozen["contract_version"],
            "SPEC_CHANGED",
            "Frozen spec declares contract_version={} but its text validates as {}".format(
                frozen["contract_version"], version
            ),
        )
        semantics = frozen_semantics(frozen)
        return {
            "text": text,
            "contract_version": version,
            "document": document,
            "verification_semantics": semantics,
        }
    version, document = validate_spec_text(raw, config)
    return {
        "text": raw,
        "contract_version": version,
        "document": document,
        "verification_semantics": SCOPE_SEMANTICS,
    }


def _frozen_spec(raw: str) -> dict | None:
    """Recognise the frozen wrapper; ``None`` means "this is the spec itself"."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if not isinstance(parsed.get("text"), str) or "contract_version" not in parsed:
        return None
    return parsed


def require_change_request(req: dict, extra: tuple[str, ...] = ()) -> None:
    """Common shape for v2 mutation requests (reassign / rebind / revise)."""
    require(
        is_integer(req.get("schema_version")) and req.get("schema_version") in (1, 2),
        "INPUT_INCOMPLETE",
        f"schema_version must be 1 or 2, got {req.get('schema_version')!r}",
    )
    require(req.get("request_id"), "INPUT_INCOMPLETE", "request_id required")
    if req.get("schema_version") == 2:
        for key in ("expected_version", "authorization_ref", "reason") + extra:
            require(key in req, "INPUT_INCOMPLETE", "Request requires " + key)
