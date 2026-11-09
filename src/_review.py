"""设计一致性审查（TGD §11.1 第二层 / §11.2 输出契约 / T18）。

Runtime **不生产**审查结论。审查由独立上下文的 Agent（或人）产出，宿主提交，
Runtime 只回答一个问题：*这份结论是不是对本组最终版本的有效结论？* 这和
"实现者说自己做完了"是同一条拒绝理由，只是换成了"审查者说自己通过了"。

三条不可协商的规则：

1. **绑定**：``subject_sha`` 必须等于当前 ``integration_sha``，``design_digest``
   必须等于冻结设计摘要。换一个集成版本、换一批设计文件，旧结论立即作废
   ——"上一次通过"不是"这一次通过"。
2. **全覆盖**：计划里每一个必需 AC 都必须出现且只出现一次。漏一项不是"默认
   通过"，是**无效输出**（T18），整份审查不接受。
3. **可追溯**：每条 ``evidence_refs`` 都要能解析到本组的真实记录。编造的引用
   与缺失的引用同样被拒。

非法输出记为 ``invalid`` 并留档，然后返回错误：它**不会**变成一次通过，也不会
被静默丢弃。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from src import _groups
from src._store import Store
from src._util import Error, atomic, digest, read, require

#: 审查输出契约版本（``schemas/group-review.v1.json``）。
REVIEW_SCHEMA_VERSION = 1

#: 逐条 AC 的三态（§11.1）。**没有**"跳过"：没检查就写 NOT_VERIFIED。
ITEM_STATUSES = ("PASS", "FAIL", "NOT_VERIFIED")

#: 可以支撑 REVIEW_READY 的结论。NOT_VERIFIED 不阻断，但必须被列出来。
SATISFYING_VERDICTS = ("PASS", "NOT_VERIFIED")

#: ``summary()["state"]`` 的取值。区分"计划没要求"和"要求了但没做"：
#: 前者是产品事实，后者是待办。
STATE_NOT_APPLICABLE = "NOT_APPLICABLE"
STATE_NOT_REQUIRED = "NOT_REQUIRED"
STATE_NOT_RUN = "NOT_RUN"
STATE_STALE = "STALE"
STATE_INVALID = "INVALID"


def reviews_dir(store: Store, group_id: str) -> Path:
    """``groups/<gid>/reviews/``（TGD §4.5 建议布局）。"""
    return _groups.group_dir(store, group_id) / "reviews"


def review_path(store: Store, group_id: str, subject_sha: str) -> Path:
    """结果只针对**明确版本**：文件名就是 subject_sha（§4.7 约束）。"""
    return reviews_dir(store, group_id) / (subject_sha + ".json")


# --------------------------------------------------------------------------- #
# 需求侧：这份计划要求审查吗 / 要覆盖哪些 AC
# --------------------------------------------------------------------------- #


def required(group: dict) -> bool:
    """计划是否声明 ``review.required``。

    缺省为 ``False``：v1.3 冻结的组没有这个字段，按 §19.3"新能力字段默认缺失
    而非自动注入"保持旧行为，绝不因为升级了二进制就让一个已冻结的组突然多出
    一道以前不存在的门。
    """
    block = (group.get("definition") or {}).get("review") or {}
    return bool(block.get("required"))


def required_acceptance_ids(group: dict) -> list[str]:
    """本组必须被审查覆盖的 AC，按成员声明顺序去重。"""
    found: list[str] = []
    for member in (group.get("definition") or {}).get("members") or []:
        for acceptance_id in member.get("acceptance_ids") or []:
            if acceptance_id not in found:
                found.append(acceptance_id)
    return found


# --------------------------------------------------------------------------- #
# 证据解析：编造的引用必须被拒
# --------------------------------------------------------------------------- #


def _integrated_tasks(group: dict) -> dict[str, str]:
    """``task_id -> integrated_sha``，成员与修复单元一视同仁。"""
    found: dict[str, str] = {}
    for unit in _groups.all_units(group):
        runtime = _groups.member_runtime(group, unit["task_key"])
        if runtime.get("task_id") and runtime.get("integrated_sha"):
            found[runtime["task_id"]] = runtime["integrated_sha"]
    return found


def _passing_checks(store: Store, group: dict, subject_sha: str) -> tuple[set, set]:
    """``(组级通过检查, 已集成成员的检查)`` —— 两种 ``check:<id>`` 都算数。

    组级检查落在 ``checks/<sha>.json``；成员级检查的证据是它自己的 run 记录，
    这里只采信"该成员已集成到 subject_sha"这一事实加 spec 里声明的 check id，
    不去猜测运行细节。
    """
    from src import _integration

    group_checks: set[str] = set()
    path = _integration.checks_dir(store, group["group_id"]) / (subject_sha + ".json")
    if path.is_file():
        try:
            record = read(path)
        except (OSError, ValueError):
            record = {}
        if record.get("subject_sha") == subject_sha:
            group_checks = {
                row["check_id"] for row in record.get("checks") or [] if row.get("status") == "PASS"
            }
    member_checks: set[str] = set()
    for member in (group.get("definition") or {}).get("members") or []:
        runtime = _groups.member_runtime(group, member["task_key"])
        if runtime.get("integrated_sha") == subject_sha and not runtime.get("invalidated"):
            member_checks |= set(member.get("check_ids") or [])
    return group_checks, member_checks


def _resolve_evidence(store: Store, group: dict, ref: str, subject_sha: str) -> str | None:
    """解析一条 evidence ref；返回错误说明，``None`` 表示可采信。

    支持的三种形态，全部要求指向**这个**集成版本：

    * ``check:<id>``   —— 组级检查或已集成成员声明过的检查，且状态为 PASS；
    * ``group:<sha>``  —— 只能是当前 ``integration_sha`` 且记录在盘上；
    * ``task:<id>``    —— 本组成员（含修复单元）且其 ``integrated_sha`` 就是当前版本。
    """
    from src import _integration

    if not isinstance(ref, str) or not ref:
        return "evidence_refs 含非字符串条目"
    kind, _, value = ref.partition(":")
    if not value:
        return f"证据引用 {ref!r} 缺少 ':' 后的标识"
    if kind == "check":
        group_checks, member_checks = _passing_checks(store, group, subject_sha)
        if value in group_checks or value in member_checks:
            return None
        return f"证据引用 {ref} 在当前集成版本上没有通过记录"
    if kind == "group":
        if value != subject_sha:
            return f"组证据 {ref} 不是当前 integration_sha"
        if not (_integration.checks_dir(store, group["group_id"]) / (value + ".json")).is_file():
            return f"组证据 {ref} 的记录文件缺失"
        return None
    if kind == "task":
        integrated = _integrated_tasks(group)
        if value not in integrated:
            return f"任务证据 {ref} 不是本组的已集成成员"
        if integrated[value] != subject_sha:
            return f"任务证据 {ref} 不属于最终集成版本"
        try:
            task = store.task(value)
        except Error:
            return f"任务证据 {ref} 指向不存在的任务"
        if not task.get("current_evidence_id"):
            return f"任务证据 {ref} 没有证据记录"
        path = store.path(task) / "evidence" / (task["current_evidence_id"] + ".json")
        if not path.is_file():
            return f"任务证据 {ref} 的证据文件缺失"
        return None
    return f"证据引用 {ref!r} 形态未知（应为 check:<id> / group:<sha> / task:<id>）"


def _path_gap(value) -> str | None:
    """``locations`` 只接受仓库相对路径：绝对路径与 ``..`` 逃逸一律拒绝。"""
    if not isinstance(value, str) or not value.strip():
        return "locations 含空值或非字符串"
    raw = Path(value)
    if raw.is_absolute() or raw.drive:
        return f"location {value!r} 是绝对路径"
    if ".." in raw.parts:
        return f"location {value!r} 用 '..' 逃逸"
    return None


# --------------------------------------------------------------------------- #
# 契约校验（纯判定 + 只读证据解析）
# --------------------------------------------------------------------------- #


def _normalise_items(doc: dict, group: dict) -> tuple[list[dict], list[str]]:
    """逐条校验并规范化；返回 (items, errors)。空 errors 才可能被接受。"""
    errors: list[str] = []
    raw_items = doc.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return [], ["items 必须是非空数组"]

    required_ids = required_acceptance_ids(group)
    seen: dict[str, int] = {}
    items: list[dict] = []
    for index, raw in enumerate(raw_items):
        where = f"items[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{where} 不是对象")
            continue
        acceptance_id = raw.get("acceptance_id")
        if not isinstance(acceptance_id, str) or not acceptance_id:
            errors.append(f"{where}.acceptance_id 缺失")
            continue
        if acceptance_id in seen:
            errors.append(f"{acceptance_id} 出现了多次（{where}）")
            continue
        seen[acceptance_id] = index
        if acceptance_id not in required_ids:
            errors.append(f"{acceptance_id} 不在本计划映射的 AC 内（凭空多出的验收项）")
            continue
        status = raw.get("status")
        if status not in ITEM_STATUSES:
            errors.append(
                f"{acceptance_id}.status 必须是 {'/'.join(ITEM_STATUSES)}，实际为 {status!r}"
            )
            continue
        finding = raw.get("finding")
        if status in ("FAIL", "NOT_VERIFIED"):
            if not isinstance(finding, str) or not finding.strip():
                errors.append(f"{acceptance_id} 状态为 {status} 却没有 finding 说明")
        locations = raw.get("locations") or []
        if not isinstance(locations, list):
            errors.append(f"{acceptance_id}.locations 必须是数组")
            locations = []
        for value in locations:
            gap = _path_gap(value)
            if gap:
                errors.append(f"{acceptance_id}: {gap}")
        refs = raw.get("evidence_refs") or []
        if not isinstance(refs, list):
            errors.append(f"{acceptance_id}.evidence_refs 必须是数组")
            refs = []
        items.append(
            {
                "acceptance_id": acceptance_id,
                "status": status,
                "finding": finding.strip() if isinstance(finding, str) else None,
                "locations": list(locations),
                "evidence_refs": list(refs),
                "suggested_action": raw.get("suggested_action"),
            }
        )

    missing = [acceptance_id for acceptance_id in required_ids if acceptance_id not in seen]
    if missing:
        errors.append("审查未覆盖必需 AC：" + ", ".join(missing))
    return items, errors


def validate(store: Store, group: dict, doc: Any) -> tuple[dict | None, list[str]]:
    """校验一份提交的审查结论。返回 ``(review, errors)``。

    ``errors`` 非空即**无效输出**：调用方必须把它记为失败，不能挑出"看起来
    没问题的那几条"凑一个通过（§11.2 最后一段）。
    """
    errors: list[str] = []
    if not isinstance(doc, dict):
        return None, ["审查结果必须是 JSON 对象"]

    if doc.get("schema_version") != REVIEW_SCHEMA_VERSION:
        errors.append(f"schema_version 必须是 {REVIEW_SCHEMA_VERSION}")

    runtime = group.get("runtime") or {}
    subject_sha = runtime.get("integration_sha")
    if not subject_sha:
        return None, ["组还没有 integration_sha，无法把审查绑定到某个版本"]
    if doc.get("subject_sha") != subject_sha:
        errors.append(
            "subject_sha 必须等于当前 integration_sha（{}），实际为 {}".format(
                subject_sha[:12], str(doc.get("subject_sha"))[:12]
            )
        )
    if doc.get("design_digest") != group.get("design_digest"):
        errors.append("design_digest 与冻结设计不一致：审查的不是这份设计")

    items, item_errors = _normalise_items(doc, group)
    errors.extend(item_errors)

    for item in items:
        for ref in item["evidence_refs"]:
            gap = _resolve_evidence(store, group, ref, subject_sha)
            if gap:
                errors.append(f"{item['acceptance_id']}: {gap}")

    reviewer = doc.get("reviewer")
    if reviewer is not None and not isinstance(reviewer, dict):
        errors.append("reviewer 必须是对象")
        reviewer = None
    reviewer = reviewer or {}
    kind = reviewer.get("kind") or doc.get("reviewer_kind") or "agent"
    if kind not in ("agent", "human"):
        errors.append("reviewer.kind 必须是 'agent' 或 'human'")

    if errors:
        return None, errors

    verdict = (
        "FAIL"
        if any(item["status"] == "FAIL" for item in items)
        else "NOT_VERIFIED"
        if any(item["status"] == "NOT_VERIFIED" for item in items)
        else "PASS"
    )
    review = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "group_id": group["group_id"],
        "project_id": group.get("project_id"),
        "subject_sha": subject_sha,
        "design_digest": group["design_digest"],
        "plan_digest": group.get("plan_digest"),
        "plan_revision": group.get("revision"),
        "verdict": verdict,
        "reviewer": {
            "kind": kind,
            "actor": reviewer.get("actor") or doc.get("actor"),
            "profile_id": reviewer.get("profile_id"),
            "combination_id": reviewer.get("combination_id"),
            "independent_context": reviewer.get("independent_context"),
        },
        "items": items,
        "items_digest": digest(items),
        "generated_at": time.time(),
    }
    return review, []


# --------------------------------------------------------------------------- #
# 落盘与读取
# --------------------------------------------------------------------------- #


def _record_attempt(store: Store, group: dict, entry: dict) -> None:
    runtime = group["runtime"]
    runtime["reviews"] = list(runtime.get("reviews") or []) + [entry]
    with store.tx():
        store.save_group(group, "group_review_" + entry["status"])


def submit(store: Store, group_id: str, req: dict[str, Any]) -> dict[str, Any]:
    """``group review ID --request-file review.json``：提交一份审查结论。

    幂等键与其它写命令一致（``store.operation``）。**非法输出会被留档并报错**，
    这样"审查失败了"和"审查没做"在记录上是两件不同的事。
    """
    from src._config import authorize

    with store.lock():
        key, prior = store.operation(req, "group_review", group_id)
        if prior:
            return prior
        try:
            group = store.group(group_id)
            _groups.require_v3(group, "review")
            config = store.config(group["project_id"])
            # 审查属于验收证据链，复用项目的 accept 授权，不要求新配置项。
            authorize(config, req, "accept")
            require(
                req.get("actor"),
                "INPUT_INCOMPLETE",
                "actor required: an unattributed review is not evidence",
            )

            runtime = group["runtime"]
            subject_sha = runtime.get("integration_sha")
            require(
                subject_sha,
                "STATE_CONFLICT",
                "组还没有集成版本；先集成成员，再对最终版本做设计审查",
            )

            review, errors = validate(store, group, req.get("review") or req.get("result") or req)
            if errors:
                _record_attempt(
                    store,
                    group,
                    {
                        "status": "invalid",
                        "request_id": req.get("request_id"),
                        "actor": req.get("actor"),
                        "subject_sha": req.get("subject_sha") or subject_sha,
                        "errors": errors,
                        "at": time.time(),
                    },
                )
                raise Error(
                    "EVIDENCE_INVALID",
                    "审查输出无效，不予接受："
                    + "; ".join(errors[:5])
                    + ("" if len(errors) <= 5 else f"（共 {len(errors)} 项）"),
                    details={"kind": "review_invalid", "errors": errors},
                )

            path = review_path(store, group_id, subject_sha)
            atomic(path, review)
            record = {
                "status": "recorded",
                "request_id": req.get("request_id"),
                "actor": req.get("actor"),
                "verdict": review["verdict"],
                "subject_sha": subject_sha,
                "design_digest": review["design_digest"],
                "items_digest": review["items_digest"],
                "path": str(path),
                "at": time.time(),
            }
            _record_attempt(store, group, record)

            # 审查结论会改变组状态（PASS 可进 REVIEW_READY，FAIL 退回 RUNNING）。
            # 立即重新派生并落盘，而不是等下一次 `status --refresh`：宿主提交完结论
            # 紧接着就要能 `group accept`，否则"刚通过却仍被拒"是另一个坑。
            from src import _group_runner

            group = _group_runner._derive(store, store.group(group_id), None)
            with store.tx():
                store.save_group(group, "group_review_state")

            result = {
                "api_version": 2,
                "operation_id": req.get("request_id"),
                "status": "succeeded",
                "accepted": True,
                "completed": True,
                "group_id": group_id,
                "state": group["state"],
                **_groups.response_fields(group),
                "review": {
                    "verdict": review["verdict"],
                    "subject_sha": subject_sha,
                    "design_digest": review["design_digest"],
                    "items_digest": review["items_digest"],
                    "counts": _counts(review["items"]),
                    "path": str(path),
                },
                "review_required": required(group),
                "unresolved": unresolved(review),
                "not_verified": not_verified(review),
                "next_actions": next_actions(store, group, review),
            }
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


def current(store: Store, group: dict) -> dict | None:
    """读回**当前版本**的审查结论；文件、绑定或摘要不符一律视为不存在。"""
    subject_sha = (group.get("runtime") or {}).get("integration_sha")
    if not subject_sha:
        return None
    path = review_path(store, group["group_id"], subject_sha)
    if not path.is_file():
        return None
    try:
        review = read(path)
    except (OSError, ValueError):
        return None
    if not isinstance(review, dict):
        return None
    if review.get("group_id") != group["group_id"]:
        return None
    if review.get("subject_sha") != subject_sha:
        return None
    if review.get("design_digest") != group.get("design_digest"):
        return None
    if review.get("items_digest") != digest(review.get("items")):
        return None
    return review


def _counts(items: list[dict]) -> dict:
    return {
        status: sum(1 for item in items if item.get("status") == status) for status in ITEM_STATUSES
    }


def unresolved(review: dict | None) -> list[dict]:
    """阻断性 finding：``FAIL`` 项（§11.2 / §12）。"""
    if not review:
        return []
    return [item for item in review.get("items") or [] if item.get("status") == "FAIL"]


def not_verified(review: dict | None) -> list[dict]:
    if not review:
        return []
    return [item for item in review.get("items") or [] if item.get("status") == "NOT_VERIFIED"]


def summary(store: Store, group: dict) -> dict:
    """``group status`` / ``group report`` / 状态派生共用的一份只读视图。"""
    if not _groups.is_v3(group):
        return {
            "required": False,
            "state": STATE_NOT_APPLICABLE,
            "satisfied": True,
            "reason": "legacy ordered group: no design artefacts to review",
        }
    required_flag = required(group)
    runtime = group.get("runtime") or {}
    subject_sha = runtime.get("integration_sha")
    review = current(store, group)
    attempts = list(runtime.get("reviews") or [])
    last_invalid = next(
        (entry for entry in reversed(attempts) if entry.get("status") == "invalid"), None
    )
    stale = (
        bool(subject_sha)
        and not review
        and any(
            entry.get("status") == "recorded" and entry.get("subject_sha") != subject_sha
            for entry in attempts
        )
    )

    if review:
        state = review["verdict"]
        satisfied = state in SATISFYING_VERDICTS
    elif not required_flag:
        state = STATE_NOT_REQUIRED if subject_sha else STATE_NOT_RUN
        satisfied = True
    elif stale:
        state = STATE_STALE
        satisfied = False
    elif last_invalid:
        state = STATE_INVALID
        satisfied = False
    else:
        state = STATE_NOT_RUN
        satisfied = False

    body = {
        "required": required_flag,
        "state": state,
        "satisfied": satisfied,
        "subject_sha": subject_sha,
        "review_subject_sha": (review or {}).get("subject_sha"),
        "verdict": (review or {}).get("verdict"),
        "counts": _counts((review or {}).get("items") or []),
        "unresolved": unresolved(review),
        "not_verified": not_verified(review),
        "items": (review or {}).get("items") or [],
        "reviewer": (review or {}).get("reviewer"),
        "recorded_at": (review or {}).get("generated_at"),
        "attempts": len(attempts),
        "invalid_errors": (last_invalid or {}).get("errors"),
        "path": str(review_path(store, group["group_id"], subject_sha)) if subject_sha else None,
    }
    body["reason"] = _reason(body)
    return body


def _reason(body: dict) -> str:
    state = body["state"]
    if state in (STATE_NOT_APPLICABLE, STATE_NOT_REQUIRED):
        return "本计划未声明 review.required；设计一致性审查未参与验收"
    if state == STATE_NOT_RUN:
        return "计划要求设计审查，但当前集成版本还没有有效结论"
    if state == STATE_STALE:
        return "只有更早版本的审查结论；集成版本已变化，必须重新审查"
    if state == STATE_INVALID:
        return "上一次提交被判定为无效输出，需重新提交"
    if state == "FAIL":
        return "存在未解决的 FAIL finding：{}".format(
            ", ".join(item["acceptance_id"] for item in body.get("unresolved") or [])
        )
    if state == "NOT_VERIFIED":
        return "无 FAIL，但存在未验证项：{}".format(
            ", ".join(item["acceptance_id"] for item in body.get("not_verified") or [])
        )
    return "全部必需 AC 通过设计一致性审查"


def satisfied(store: Store, group: dict) -> bool:
    """验收门禁用的判定：不需要审查视为满足，需要则必须有有效结论。"""
    return bool(summary(store, group)["satisfied"])


def next_actions(store: Store, group: dict, review: dict | None) -> list[str]:
    gid = group["group_id"]
    actions: list[str] = []
    if unresolved(review):
        cap = (group.get("definition", {}).get("budget_caps") or {}).get("max_group_repairs")
        if cap:
            actions.append(f"group repair {gid} --request-file repair.json")
        actions.append(f"group review {gid} --request-file review.json  # 修复后重新审查")
        return actions
    if not summary(store, group)["satisfied"]:
        actions.append(f"group review {gid} --request-file review.json")
        return actions
    actions.append(f"group accept {gid} --request-file accept.json")
    return actions
