"""逐条 AC 判定（任务级验收的第二层，v1.7）。

组级设计审查（:mod:`src._review`）已经回答了"这份结论是不是对当前集成版本的
有效结论"。单任务缺的是同一个问题的另一半：**谁说这条 AC 通过了、依据是什么、
对哪个 SHA**。没有这一层，``accept`` 只能收一个整体 ``decision``，报告里 manual
项永远是"未记录逐项人工结论"——也就是 2026-11-06 那次事故的形状：代码写了、
测试绿了、验收闭环 0%。

四条不可协商的规则（前三条与组级同构，理由相同）：

1. **绑定**：``subject_sha`` 必须等于 ``task.validated_sha``。换一个候选，旧结论
   立即作废（记录文件名就是 SHA）。
2. **全覆盖**：spec 里每一个 AC 都必须出现且只出现一次。漏一项不是"默认通过"，
   是**无效输出**。
3. **可追溯**：每条 ``evidence_refs`` 都必须解析到本任务这一版的真实记录。
   编造的引用与缺失的引用同样被拒。
4. **独立性**（只有任务级需要）：判者不能是产生这份 candidate 的那个上下文。
   ``reviewer.profile_id`` / ``reviewer.run_id`` / ``reviewer.actor`` 任一与产生
   candidate 的 run 相同即 ``SELF_REVIEW_DENIED``。"worker 说自己复核完了"从来
   不是一次判定。

非法输出记为 ``invalid`` 并留档，然后报错：它**不会**变成一次通过。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from src import _contracts
from src._store import Store
from src._util import Error, atomic, digest, read, require

#: 审查输出契约版本（``schemas/task-review.v1.json``）。
REVIEW_SCHEMA_VERSION = 1

#: 逐条 AC 的三态。**没有**"跳过"：没检查就写 NOT_VERIFIED。
ITEM_STATUSES = ("PASS", "FAIL", "NOT_VERIFIED")

#: 可以支撑 accept 的结论。NOT_VERIFIED 不阻断，但必须被逐条显式确认。
SATISFYING_VERDICTS = ("PASS", "NOT_VERIFIED")

#: ``summary()["state"]``。区分"spec 没要求"和"要求了但没做"。
STATE_NOT_REQUIRED = "NOT_REQUIRED"
STATE_NOT_RUN = "NOT_RUN"
STATE_STALE = "STALE"
STATE_INVALID = "INVALID"


# --------------------------------------------------------------------------- #
# 冻结 spec 的读法（只读 text，不信 document 副本）
# --------------------------------------------------------------------------- #


def frozen_document(store: Store, t: dict) -> dict | None:
    """Read the frozen spec *text* for AC mapping; never the ``document`` copy."""
    path = store.path(t) / "spec-{}.json".format(t["spec_revision"])
    if not path.is_file():
        return None
    try:
        frozen = read(path)
        text = frozen.get("text")
        if not isinstance(text, str) or digest(text.encode()) != t.get("spec_hash"):
            return None
        _, document = _contracts.parse_spec(text, t.get("config") or {})
    except (OSError, ValueError, Error):
        return None
    return document


def required_acceptance_ids(store: Store, t: dict) -> list[str]:
    """本任务必须被判定覆盖的 AC（来自冻结 spec，按声明顺序）。"""
    return _contracts.acceptance_ids(frozen_document(store, t))


def required(store: Store, t: dict) -> bool:
    return _contracts.review_required(frozen_document(store, t))


def independent_required(store: Store, t: dict) -> bool:
    return _contracts.acceptance_review_block(frozen_document(store, t))["independent"]


def review_mode(store: Store, t: dict) -> str:
    return _contracts.acceptance_review_block(frozen_document(store, t))["mode"]


def manual_ids(store: Store, t: dict) -> list[str]:
    """``judge`` 不是机器能判的 AC：它们的结论只能由 review 提供。"""
    document = frozen_document(store, t)
    return list(_contracts.manual_verifications(document))


# --------------------------------------------------------------------------- #
# 落盘位置
# --------------------------------------------------------------------------- #


def reviews_dir(store: Store, t: dict) -> Path:
    return store.path(t) / "reviews"


def review_path(store: Store, t: dict, subject_sha: str) -> Path:
    """结果只针对**明确版本**：文件名就是 subject_sha。"""
    return reviews_dir(store, t) / (subject_sha + ".json")


# --------------------------------------------------------------------------- #
# 证据解析
# --------------------------------------------------------------------------- #


def _passing_checks(store: Store, t: dict, subject_sha: str) -> set[str]:
    """本任务在当前判定版本上 PASS 过的 check id 集合。

    两个来源都采信，但都要求指向**这一个** candidate：

    * 当前 run 的 ``validation.json``（控制器执行的那一次）；
    * ``evidence/<id>.json`` 里的 ``check_results``（与 SHA 绑定、可复算）。
    """
    found: set[str] = set()
    if t.get("validated_sha") != subject_sha:
        return found
    run_id = t.get("current_run_id")
    if run_id:
        path = store.path(t) / "runs" / run_id / "validation.json"
        if path.is_file():
            try:
                rows = read(path)
            except (OSError, ValueError):
                rows = []
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and row.get("status") == "PASS" and row.get("id"):
                    found.add(row["id"])
    evidence_id = t.get("current_evidence_id")
    if evidence_id:
        path = store.path(t) / "evidence" / (evidence_id + ".json")
        if path.is_file():
            try:
                record = read(path)
            except (OSError, ValueError):
                record = {}
            if isinstance(record, dict) and record.get("candidate_sha") == subject_sha:
                for row in record.get("check_results") or []:
                    if isinstance(row, dict) and row.get("status") == "PASS" and row.get("id"):
                        found.add(row["id"])
    return found


def _path_gap(value) -> str | None:
    """``locations`` / ``doc:`` 只接受仓库相对路径。"""
    if not isinstance(value, str) or not value.strip():
        return "含空值或非字符串路径"
    raw = Path(value)
    if raw.is_absolute() or raw.drive:
        return "{} 是绝对路径".format(value)
    if ".." in raw.parts:
        return "{} 用 '..' 逃逸".format(value)
    return None


def resolve_evidence(store: Store, t: dict, ref: str, subject_sha: str) -> str | None:
    """解析一条 evidence ref；返回错误说明，``None`` 表示可采信。

    支持的形态全部要求指向**这个** candidate：

    * ``check:<id>``      —— 当前 run 的 validation 或 evidence 里有 PASS 记录；
    * ``evidence:<id>``   —— 必须是本任务当前 ``current_evidence_id``；
    * ``doc:<path>``      —— 仓库相对路径，且该文件在候选树里存在；
    * ``decision:<id>``   —— spec 里的设计裁定 id（用于"依据某条拍板"）。
    """
    if not isinstance(ref, str) or not ref:
        return "evidence_refs 含非字符串条目"
    kind, _, value = ref.partition(":")
    if not value:
        return "证据引用 {!r} 缺少 ':' 后的标识".format(ref)
    if kind == "check":
        if value in _passing_checks(store, t, subject_sha):
            return None
        return "证据引用 {} 在当前 candidate 上没有通过记录".format(ref)
    if kind == "evidence":
        if value != t.get("current_evidence_id"):
            return "证据引用 {} 不是当前 candidate 的证据".format(ref)
        if not (store.path(t) / "evidence" / (value + ".json")).is_file():
            return "证据引用 {} 的记录文件缺失".format(ref)
        return None
    if kind == "doc":
        gap = _path_gap(value)
        if gap:
            return "证据引用 {} 的路径无效：{}".format(ref, gap)
        root = t.get("workspace")
        if not root or not (Path(root) / value).exists():
            return "证据引用 {} 指向的文件不在候选树里".format(ref)
        return None
    if kind == "decision":
        document = frozen_document(store, t) or {}
        known = {"decision:" + str(item.get("id")) for item in _contracts.decisions(document)}
        if ref in known:
            return None
        return "证据引用 {} 不是本 spec 声明的裁定".format(ref)
    return "证据引用 {!r} 形态未知（应为 check:/evidence:/doc:/decision:）".format(ref)


# --------------------------------------------------------------------------- #
# 独立性
# --------------------------------------------------------------------------- #


def _self_review_gaps(store: Store, t: dict, reviewer: dict) -> list[str]:
    """判者与 candidate 产生者的重叠。任一命中即拒绝。"""
    gaps: list[str] = []
    run_id = reviewer.get("run_id")
    if run_id and run_id == t.get("current_run_id"):
        gaps.append("reviewer.run_id 就是产出这份 candidate 的 run")
    profile_id = reviewer.get("profile_id")
    if profile_id and profile_id == t.get("agent_profile_id"):
        gaps.append("reviewer.profile_id 与实现该 candidate 的 profile 相同")
    actor = reviewer.get("actor")
    if actor and actor == t.get("run_actor"):
        gaps.append("reviewer.actor 就是产出这份 candidate 的执行者")
    return gaps


# --------------------------------------------------------------------------- #
# 契约校验
# --------------------------------------------------------------------------- #


def _normalise_items(
    store: Store, t: dict, doc: dict, required_ids: list[str]
) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    raw_items = doc.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return [], ["items 必须是非空数组"]

    seen: dict[str, int] = {}
    items: list[dict] = []
    manual = set(manual_ids(store, t))
    for index, raw in enumerate(raw_items):
        where = "items[{}]".format(index)
        if not isinstance(raw, dict):
            errors.append(where + " 不是对象")
            continue
        acceptance_id = raw.get("acceptance_id")
        if not isinstance(acceptance_id, str) or not acceptance_id:
            errors.append(where + ".acceptance_id 缺失")
            continue
        if acceptance_id in seen:
            errors.append("{} 出现了多次（{}）".format(acceptance_id, where))
            continue
        seen[acceptance_id] = index
        if acceptance_id not in required_ids:
            errors.append(acceptance_id + " 不在本 spec 的 AC 内（凭空多出的验收项）")
            continue
        status = raw.get("status")
        if status not in ITEM_STATUSES:
            errors.append(
                "{}.status 必须是 {}，实际为 {!r}".format(
                    acceptance_id, "/".join(ITEM_STATUSES), status
                )
            )
            continue
        finding = raw.get("finding")
        if status in ("FAIL", "NOT_VERIFIED"):
            if not isinstance(finding, str) or not finding.strip():
                errors.append(
                    "{} 状态为 {} 却没有 finding 说明".format(acceptance_id, status)
                )
        locations = raw.get("locations") or []
        if not isinstance(locations, list):
            errors.append(acceptance_id + ".locations 必须是数组")
            locations = []
        for value in locations:
            gap = _path_gap(value)
            if gap:
                errors.append("{}: {}".format(acceptance_id, gap))
        refs = raw.get("evidence_refs") or []
        if not isinstance(refs, list):
            errors.append(acceptance_id + ".evidence_refs 必须是数组")
            refs = []
        items.append(
            {
                "acceptance_id": acceptance_id,
                "status": status,
                "manual": acceptance_id in manual,
                "finding": finding.strip() if isinstance(finding, str) else None,
                "locations": list(locations),
                "evidence_refs": list(refs),
                "suggested_action": raw.get("suggested_action"),
            }
        )

    missing = [item for item in required_ids if item not in seen]
    if missing:
        errors.append("审查未覆盖必需 AC：" + ", ".join(missing))
    return items, errors


def validate(store: Store, t: dict, doc: Any) -> tuple[dict | None, list[str]]:
    """校验一份提交的逐条判定。返回 ``(review, errors)``。

    ``errors`` 非空即**无效输出**：调用方必须整份拒绝，不能挑出"看起来没问题
    的那几条"凑一个通过。
    """
    if not isinstance(doc, dict):
        return None, ["审查结果必须是 JSON 对象"]

    errors: list[str] = []
    if doc.get("schema_version") != REVIEW_SCHEMA_VERSION:
        errors.append("schema_version 必须是 {}".format(REVIEW_SCHEMA_VERSION))

    subject_sha = t.get("validated_sha")
    if not subject_sha:
        return None, ["任务还没有 validated_sha；先完成 verify，再对某一版做判定"]
    if doc.get("subject_sha") != subject_sha:
        errors.append(
            "subject_sha 必须等于当前 validated_sha（{}），实际为 {}".format(
                subject_sha[:12], str(doc.get("subject_sha"))[:12]
            )
        )

    required_ids = required_acceptance_ids(store, t)
    if not required_ids:
        return None, ["冻结 spec 读不到 AC；无法判定覆盖关系"]

    items, item_errors = _normalise_items(store, t, doc, required_ids)
    errors.extend(item_errors)

    for item in items:
        for ref in item["evidence_refs"]:
            gap = resolve_evidence(store, t, ref, subject_sha)
            if gap:
                errors.append("{}: {}".format(item["acceptance_id"], gap))

    reviewer = doc.get("reviewer")
    if reviewer is not None and not isinstance(reviewer, dict):
        errors.append("reviewer 必须是对象")
        reviewer = None
    reviewer = reviewer or {}
    kind = reviewer.get("kind") or doc.get("reviewer_kind") or "agent"
    if kind not in ("agent", "human"):
        errors.append("reviewer.kind 必须是 'agent' 或 'human'")
    mode = review_mode(store, t)
    if mode != "either" and kind != mode:
        errors.append(
            "本 spec 要求 mode={}，实际 reviewer.kind={}".format(mode, kind)
        )

    # 独立性：声明 + 机械比对。只认声明会让"worker 自审"照样通过。
    if independent_required(store, t):
        if reviewer.get("independent_context") is not True:
            errors.append(
                "spec 要求独立判者：reviewer.independent_context 必须为 true"
            )
        gaps = _self_review_gaps(store, t, reviewer)
        if gaps:
            errors.append("SELF_REVIEW: " + "; ".join(gaps))
    if doc.get("actor") and reviewer.get("actor") and doc["actor"] != reviewer["actor"]:
        errors.append("actor 与 reviewer.actor 不一致：提交人与判者必须是同一个归属")

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
        "task_id": t["task_id"],
        "project_id": t.get("project_id"),
        "spec_revision": t.get("spec_revision"),
        "spec_hash": t.get("spec_hash"),
        "subject_sha": subject_sha,
        "evidence_id": t.get("current_evidence_id"),
        "verdict": verdict,
        "reviewer": {
            "kind": kind,
            "actor": reviewer.get("actor") or doc.get("actor"),
            "profile_id": reviewer.get("profile_id"),
            "run_id": reviewer.get("run_id"),
            "combination_id": reviewer.get("combination_id"),
            "independent_context": reviewer.get("independent_context"),
        },
        "items": items,
        "items_digest": digest(items),
        "generated_at": time.time(),
    }
    return review, []


# --------------------------------------------------------------------------- #
# 提交与读取
# --------------------------------------------------------------------------- #


def _record_attempt(store: Store, t: dict, entry: dict) -> None:
    t["reviews"] = list(t.get("reviews") or []) + [entry]
    # ``Store.save`` 自己开事务；再包一层 ``tx()`` 会在同一连接上 BEGIN 两次。
    store.save(t, "task_review_" + entry["status"])


def _counts(items: list[dict]) -> dict:
    return {
        status: sum(1 for item in items if item.get("status") == status)
        for status in ITEM_STATUSES
    }


def submit(store: Store, task_id: str, req: dict[str, Any]) -> dict[str, Any]:
    """``task review TASK --request-file review.json``：提交一份逐条判定。"""
    from src._config import authorize

    with store.lock():
        key, prior = store.operation(req, "task_review", task_id)
        if prior:
            return prior
        try:
            t = store.task(task_id)
            config = store.config(t["project_id"])
            # 判定属于验收证据链，复用项目的 accept 授权，不要求新配置项。
            authorize(config, req, "accept")
            require(
                req.get("actor"),
                "INPUT_INCOMPLETE",
                "actor required: an unattributed review is not evidence",
            )
            require(
                t.get("validated_sha"),
                "STATE_CONFLICT",
                "任务还没有 validated_sha；先完成 verify，再对某一版做判定",
            )
            review, errors = validate(
                store, t, req.get("review") or req.get("result") or req
            )
            if errors:
                _record_attempt(
                    store,
                    t,
                    {
                        "status": "invalid",
                        "request_id": req.get("request_id"),
                        "actor": req.get("actor"),
                        "subject_sha": req.get("subject_sha") or t.get("validated_sha"),
                        "errors": errors,
                        "at": time.time(),
                    },
                )
                raise Error(
                    "EVIDENCE_INVALID",
                    "逐条判定无效，不予接受："
                    + "; ".join(errors[:5])
                    + ("" if len(errors) <= 5 else "（共 {} 项）".format(len(errors))),
                    details={"kind": "task_review_invalid", "errors": errors},
                )

            path = review_path(store, t, t["validated_sha"])
            atomic(path, review)
            _record_attempt(
                store,
                t,
                {
                    "status": "recorded",
                    "request_id": req.get("request_id"),
                    "actor": req.get("actor"),
                    "verdict": review["verdict"],
                    "subject_sha": review["subject_sha"],
                    "items_digest": review["items_digest"],
                    "path": str(path),
                    "at": time.time(),
                },
            )
            result = {
                "api_version": 2,
                "operation_id": req.get("request_id"),
                "status": "succeeded",
                "accepted": True,
                "completed": True,
                "task_id": task_id,
                "subject_sha": review["subject_sha"],
                "verdict": review["verdict"],
                "counts": _counts(review["items"]),
                "unresolved": unresolved(review),
                "not_verified": not_verified(review),
                "review_required": required(store, t),
                "path": str(path),
                "next_actions": next_actions(store, t, review),
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


def current(store: Store, t: dict) -> dict | None:
    """读回**当前版本**的判定；文件、绑定或摘要不符一律视为不存在。"""
    subject_sha = t.get("validated_sha")
    if not subject_sha:
        return None
    path = review_path(store, t, subject_sha)
    if not path.is_file():
        return None
    try:
        review = read(path)
    except (OSError, ValueError):
        return None
    if not isinstance(review, dict):
        return None
    if review.get("task_id") != t["task_id"]:
        return None
    if review.get("subject_sha") != subject_sha:
        return None
    if review.get("items_digest") != digest(review.get("items")):
        return None
    return review


def unresolved(review: dict | None) -> list[dict]:
    """阻断性 finding：``FAIL`` 项。"""
    if not review:
        return []
    return [item for item in review.get("items") or [] if item.get("status") == "FAIL"]


def not_verified(review: dict | None) -> list[dict]:
    if not review:
        return []
    return [
        item for item in review.get("items") or [] if item.get("status") == "NOT_VERIFIED"
    ]


def _reason(body: dict) -> str:
    state = body["state"]
    if state == STATE_NOT_REQUIRED:
        return "spec 未声明 acceptance_review.required；逐条判定未参与验收"
    if state == STATE_NOT_RUN:
        return "spec 要求逐条判定，但当前 candidate 还没有有效结论"
    if state == STATE_STALE:
        return "只有更早版本的判定结论；candidate 已变化，必须重新判定"
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
    return "全部必需 AC 已逐条判定通过"


def criteria_from(document: dict | None) -> list[dict]:
    """冻结 spec 的 AC 形状，供 CLI 与验收脚本交叉核对。

    只读查询返回它，是为了让"逐条核对"不必再读一遍 spec 文件：调用方拿到的
    `task review` 输出本身就是完整输入，少一个可能过期的副本。
    """
    found: list[dict] = []
    if not isinstance(document, dict):
        return found
    for item in document.get("acceptance_criteria") or []:
        if not isinstance(item, dict):
            continue
        verification = item.get("verification") or {}
        found.append(
            {
                "id": item.get("id"),
                "statement": item.get("statement"),
                "type": verification.get("type"),
                "check_id": verification.get("check_id"),
                "judge": verification.get("judge"),
                "supporting_checks": list(verification.get("supporting_checks") or []),
            }
        )
    return found


def summary(store: Store, t: dict) -> dict:
    """``task status`` / ``task report`` / accept 门禁共用的一份只读视图。"""
    # 一次快照只读一次冻结 spec：四个字段各读一次会允许"读到 A、判成 B"。
    document = frozen_document(store, t)
    block = _contracts.acceptance_review_block(document)
    required_flag = bool(block["required"])
    subject_sha = t.get("validated_sha")
    review = current(store, t)
    attempts = list(t.get("reviews") or [])
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
        state = STATE_NOT_REQUIRED
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
        "independent": bool(block["independent"]),
        "mode": block["mode"],
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
        "required_acceptance_ids": _contracts.acceptance_ids(document),
        # 逐条核对脚本靠这两项工作，不必再读一份可能过期的 spec 副本。
        "criteria": criteria_from(document),
        "open_decisions": [
            {
                "id": item.get("id"),
                "question": item.get("question"),
                "recommendation": item.get("recommendation"),
            }
            for item in _contracts.open_decisions(document)
        ],
        "path": str(review_path(store, t, subject_sha)) if subject_sha else None,
    }
    body["reason"] = _reason(body)
    return body


def satisfied(store: Store, t: dict) -> bool:
    """accept 门禁用：不需要判定视为满足，需要则必须有有效结论。"""
    return bool(summary(store, t)["satisfied"])


def next_actions(store: Store, t: dict, review: dict | None) -> list[str]:
    actions: list[str] = []
    if unresolved(review):
        actions.append(
            "fix the FAIL items, then re-verify and re-run: "
            "zoe task review {} --request-file review.json".format(t["task_id"])
        )
        return actions
    if not satisfied(store, t):
        actions.append(
            "zoe task review {} --request-file review.json".format(t["task_id"])
        )
        return actions
    actions.append("zoe task accept {} --request-file accept.json".format(t["task_id"]))
    return actions
