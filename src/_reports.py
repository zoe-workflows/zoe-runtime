"""Delivery report: one command, one pasteable record (v0.5.1 W4).

``task report --format markdown`` used to print a fixed short table, which forced
a host to aggregate ``status + events + validation.json`` itself. This module
renders the delivery view instead: acceptance verdicts, per-check detail, the
blockers, and — most importantly — what was **not** verified.

Two boundaries are deliberate:

* the disk file is ``tasks/<task>/delivery-<spec_rev>.md``, never ``report.md``
  (that one is rendered by ``summary submit`` from a progress summary; sharing a
  path would make the two overwrite each other);
* the whole document is built from **one** snapshot taken at the start. No
  section re-reads the task, so a report can never mix two different moments
  (§9.2.1).
"""

from __future__ import annotations

import time
from pathlib import Path

from src import _contracts, _evidence, _task_review
from src._util import Error, atomic_text, digest, git, read
from src._util import redact_secrets as redact

#: Hard cap for the whole document (§9.6). Kept well under a typical chat
#: attachment limit and small enough to stay readable.
MAX_REPORT_BYTES = 16 * 1024

#: Truncation priority: keep these in this order, drop the rest (§9.6).
_PRIORITY = ("blockers", "boundary", "acceptance", "header", "checks")

_SECTIONS_ORDER = ("header", "acceptance", "checks", "blockers", "boundary")


def _iso(ts) -> str:
    if not ts:
        return "unknown"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def report_path(store, t: dict) -> Path:
    """Where this spec revision's delivery view lives (§9.1)."""
    return store.path(t) / f"delivery-{t['spec_revision']}.md"


def _frozen_document(store, t: dict) -> dict | None:
    """Read the frozen spec *text* for AC mapping; never the ``document`` copy."""
    return _task_review.frozen_document(store, t)


def _run_rows(store, t: dict, r: dict | None) -> list[dict]:
    if not r or not r.get("run_id"):
        return []
    path = store.path(t) / "runs" / r["run_id"] / "validation.json"
    if not path.is_file():
        return []
    try:
        rows = read(path)
    except (OSError, ValueError):
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _run_evidence(store, t: dict, r: dict | None) -> dict | None:
    """Evidence **of this run only**; never a previous run's PASS (§9.2.1)."""
    if not r or not r.get("run_id"):
        return None
    path = store.path(t) / "runs" / r["run_id"] / "evidence.json"
    if not path.is_file():
        return None
    try:
        return read(path)
    except (OSError, ValueError):
        return None


def snapshot(store, t: dict) -> dict:
    """Take the single snapshot the whole report is rendered from (§9.2.1)."""
    r = store.get_run(t["current_run_id"]) if t.get("current_run_id") else None
    rows = _run_rows(store, t, r)
    evidence = _run_evidence(store, t, r)
    # 一次快照只读一次冻结 spec：两处各读一次会允许"文档是 A、裁定是 B"的
    # 混读，而这份快照的全部意义就是同一时刻。
    document = _frozen_document(store, t)
    return {
        "task_id": t["task_id"],
        "title": t.get("title") or t["task_id"],
        "row_version": t.get("row_version"),
        "spec_revision": t["spec_revision"],
        "state": t.get("state"),
        "phase": t.get("phase"),
        "branch": t.get("branch"),
        "profile_id": t.get("agent_profile_id"),
        "adapter_id": t.get("adapter_id"),
        "adapter_backend": t.get("adapter_backend"),
        "attempts": t.get("attempts", 0),
        "max_attempts": (t.get("config") or {}).get("max_attempts", 3),
        "candidate_sha": t.get("candidate_sha"),
        "validated_sha": t.get("validated_sha"),
        # 零候选是"这次没有任何可交付改动"，与"还没验证过"是两件事：
        # 前者 `candidate_sha == base_sha`，后者 `candidate_sha is None`。
        "empty_candidate": bool(t.get("empty_candidate")),
        "evidence_id": (evidence or {}).get("evidence_id"),
        "run_id": (r or {}).get("run_id"),
        "trust_level": (r or {}).get("trust_level"),
        "verification_source": (r or {}).get("verification_source"),
        "started_at": (r or {}).get("started_at"),
        "ended_at": (r or {}).get("ended_at"),
        "reconciled_at": (r or {}).get("reconciled_at"),
        "block_code": t.get("block_code"),
        "last_error": t.get("last_error"),
        "acceptance": t.get("acceptance") or {},
        "rows": rows,
        "verification_semantics": (evidence or {}).get(
            "verification_semantics", _contracts.FULL_SEMANTICS
        ),
        "generated_at": time.time(),
        "document": document,
        # 第二层验收（v1.7）：逐条 AC 判定。没有它，manual 项只能写
        # "未记录逐项人工结论"——一句诚实的空白，也是一次货真价实的漏验。
        "review": _task_review.summary(store, t),
        "open_decisions": _contracts.open_decisions(document),
        "diff": _diff_stat(t),
        "workspace": t.get("workspace"),
        "base_sha": t.get("base_sha"),
        # v1.2 §16.7：报告的第一句话必须回答"到底跑了什么组合"，而不是只报
        # profile 名。全部取值来自已落盘的记录，报告不做任何推断。
        # 标识必须与下面的指纹**同源**：有 run 时用 run 冻结的（实测版本）组合，
        # 否则报告会把"pin 版本的 id"配"实测版本的指纹"，两个组合拼成一行。
        "combination_id": (r or {}).get("combination_id") or t.get("combination_id"),
        "frozen_execution": (r or {}).get("frozen_execution") or t.get("selected_execution") or {},
        "cli_version": (r or {}).get("cli_version"),
        "cli_version_mismatch": bool((r or {}).get("cli_version_mismatch")),
        "requested_model": (r or {}).get("requested_model", t.get("requested_model")),
        "reported_model": (r or {}).get("reported_model"),
        "reported_models": (r or {}).get("reported_models") or [],
        "model_identity": (r or {}).get("model_identity"),
        # 结论的依据与输入分开放在报告里：``alias_confirmed`` 只能被读成"与声明的
        # 别名集合一致"，读者不该从 identity 字符串反推它是不是逐字相等。
        "identity_basis": (r or {}).get("identity_basis"),
        "model_aliases": (r or {}).get("model_aliases") or [],
        "request_echoes": [
            entry.get("model")
            for entry in ((r or {}).get("reported_models") or [])
            if isinstance(entry, dict) and entry.get("scope") == "request_echo"
        ],
        "observation_scope": (r or {}).get("observation_scope"),
        "model_unresolved": bool((r or {}).get("model_unresolved", t.get("model_unresolved"))),
        "combination_verified": (r or {}).get("combination_verified"),
        "endpoint": (r or {}).get("endpoint") or t.get("endpoint") or {},
        "route_id": t.get("route_id"),
        "route_reason": t.get("route_reason"),
        "route_decision": t.get("route_decision"),
        "task_type": t.get("task_type"),
        "requirements": t.get("requirements") or {},
        "spend": (r or {}).get("spend") or {},
        "attribution": (r or {}).get("attribution") or {},
        "purpose": t.get("purpose") or "task",
        # F2：豁免次数必须能对上账。`attempts` 是真实启动次数，`billable` 才是
        # 门禁口径；差值是预算豁免（配置/环境归因），按 run_id 幂等。
        "billable_attempts": max(
            0,
            int(t.get("attempts") or 0)
            - len({w.get("run_id") for w in (t.get("budget_waivers") or []) if w.get("run_id")}),
        ),
        "waived_runs": [
            w.get("run_id") for w in (t.get("budget_waivers") or []) if w.get("run_id")
        ],
        # F1-b：外部收口的任务不是 DONE，报告必须自己说清楚。
        "external_close": t.get("external_close") or {},
    }


def _diff_stat(t: dict) -> str:
    """Read-only ``git diff --stat`` between the baseline and the candidate (§9.4)."""
    if not t.get("candidate_sha") or not t.get("workspace"):
        return "NOT_RUN"
    if not Path(t["workspace"]).exists():
        return "NOT_RUN"
    try:
        text = git(t["workspace"], "diff", "--stat", f"{t['base_sha']}..{t['candidate_sha']}")
    except Error:
        return "unavailable (git diff failed)"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "no changes"


def _header(snap: dict) -> str:
    ended = snap["ended_at"]
    started = snap["started_at"]
    extra: list[str] = []
    if ended and started:
        seconds = int(float(ended) - float(started))
        timing = f"{_iso(started)} / {_iso(ended)}（{seconds}s，来源: observed）"
    elif snap["reconciled_at"]:
        # `reconciled_at` is when the controller *noticed*, not when the process
        # exited. Printing it as the end time (or deriving a duration from it)
        # would turn an absence into a measurement (§9.3).
        timing = f"{_iso(started)} / unknown"
        extra.append(
            "- Reconciled at: {}（发现结束，非真实退出时间；不计算耗时）".format(
                _iso(snap["reconciled_at"])
            )
        )
    elif ended:
        timing = f"unknown / {_iso(ended)}（开始时间未记录，不计算耗时）"
    elif started:
        timing = f"{_iso(started)} / running"
    else:
        timing = "unknown / unknown"

    trust = snap["trust_level"] or "unknown"
    executor = "{}（{} / {}）· trust: {}".format(
        snap["profile_id"], snap["adapter_id"], snap["adapter_backend"], trust
    )
    if trust in ("unverified", "unknown"):
        executor += "（无 real-e2e 记录）"
    trust_line = "- Trust: {}（source: {}）".format(trust, snap["verification_source"] or "none")
    lines = [
        "# {}".format(snap["title"]),
        "",
        "- Task: {}（Spec rev {}）".format(snap["task_id"], snap["spec_revision"]),
        "- Run: {}（attempt {}）".format(snap["run_id"] or "none", snap["attempts"]),
        "- Candidate: {} ｜ Evidence: {} ｜ Generated: {}".format(
            (snap["candidate_sha"] or "none")[:12] or "none",
            snap["evidence_id"] or "none (this run)",
            _iso(snap["generated_at"]),
        )
        + (
            " ｜ **EMPTY：与 base_sha 相同，本次没有任何可交付改动**"
            if snap.get("empty_candidate")
            else ""
        ),
        "- State / Phase: {} / {}".format(snap["state"], snap["phase"]),
        "- Branch: {}".format(snap["branch"]),
        f"- Executor: {executor}",
        trust_line,
        *_combination_lines(snap),
        "- Attempts: {} / {}".format(snap["attempts"], snap["max_attempts"])
        + (
            "（其中 {} 次为配置/环境豁免，不计入预算）".format(len(snap.get("waived_runs") or []))
            if snap.get("waived_runs")
            else ""
        ),
        *(
            [
                "- 外部收口（未通过 Zoe 验收）：{} · {} · claim={}".format(
                    snap["external_close"].get("external_ref"),
                    snap["external_close"].get("reason"),
                    snap["external_close"].get("claim"),
                )
            ]
            if snap.get("external_close")
            else []
        ),
        f"- Started / Ended: {timing}",
        *extra,
        "- Row version: {}".format(snap["row_version"]),
    ]
    lines.extend(_scope_lines(snap))
    lines.append("- Diff: {}".format(snap["diff"]))
    lines.append("")
    return "\n".join(lines)


def _combination_lines(snap: dict) -> list[str]:
    """执行组合 / 集成事实 / 模型观测 / 选择依据 / 质量数据（§16.7）。

    每一行都必须能落到一条已记录的事实上；没有事实就写"未解析/无组合标识"，
    不允许用 profile 名反推"应该是哪个模型"，也不允许把未观测写成"匹配"。
    """
    fingerprint = snap.get("frozen_execution") or {}
    combination = snap.get("combination_id")
    if combination:
        detail = " · ".join(
            part
            for part in (
                snap.get("profile_id"),
                "{} / {}".format(snap.get("adapter_id"), snap.get("adapter_backend")),
                f"model={fingerprint.get('model') or 'unresolved'}",
                (
                    f"variant={fingerprint['model_variant']}"
                    if fingerprint.get("model_variant")
                    else None
                ),
                (
                    f"effort={fingerprint['reasoning_effort']}"
                    if fingerprint.get("reasoning_effort")
                    else None
                ),
                (
                    f"endpoint={snap['endpoint'].get('id')}"
                    if (snap.get("endpoint") or {}).get("id")
                    else None
                ),
            )
            if part
        )
        combination_line = f"- 执行组合：{combination}（{detail}）"
    else:
        combination_line = "- 执行组合：无组合标识（v1.1 之前创建的任务）"

    version = snap.get("cli_version") or fingerprint.get("cli_version_effective")
    verified = snap.get("combination_verified")
    if verified is True:
        integration = "已验证（版本 {}、结果协议 {}）".format(version or "unknown", "agent-adapter")
    elif verified is False:
        integration = "未验证（版本 {}、成本/模型观测不足）".format(version or "unknown")
    else:
        integration = "未验证（本 run 尚未产生可评估的观测）"
    if snap.get("cli_version_mismatch"):
        integration += "；pin 与实测版本不一致"

    identity = snap.get("model_identity") or "unobserved"
    if snap.get("model_unresolved"):
        model_line = "- 模型观测：运行模型未解析（allow_cli_default_model / legacy profile）"
    elif identity == "unobserved":
        model_line = "- 模型观测：未报告（工具未在输出中给出模型标识）"
    elif identity == "alias_confirmed":
        # 把"按声明等价"写成"已证明使用该模型"会越级：这里的依据是配置里的
        # model_aliases，不是逐字比对的结果。
        model_line = (
            "- 模型观测：requested={} reported={} identity=alias_confirmed"
            "（上报值与 profile 声明的 model_aliases 一致；逐字相等未确认，scope: {}）"
        ).format(
            snap.get("requested_model") or "none",
            snap.get("reported_model") or "none",
            snap.get("observation_scope") or "none",
        )
    else:
        model_line = "- 模型观测：requested={} reported={} identity={}（scope: {}）".format(
            snap.get("requested_model") or "none",
            snap.get("reported_model") or "none",
            identity,
            snap.get("observation_scope") or "none",
        )

    if snap.get("route_id"):
        basis = "- 选择依据：route {}（{}，{}）".format(
            snap["route_id"],
            snap.get("route_reason") or "unknown",
            snap.get("route_decision") or "selected",
        )
        if snap.get("task_type"):
            basis += "；task_type={}".format(snap["task_type"])
    else:
        basis = "- 选择依据：显式指定 / 项目默认 profile（未启用路由）"

    attribution = snap.get("attribution") or {}
    if snap.get("purpose") == "probe":
        quality = "- 质量数据：不计入（probe 运行只提供集成事实）"
    elif not attribution:
        quality = "- 质量数据：本 run 尚无归因记录"
    else:
        quality = "- 质量数据：attempt={} delivery={}（{}）".format(
            attribution.get("attempt_metrics", "unknown"),
            attribution.get("delivery_metrics", "unknown"),
            attribution.get("reason", "no reason recorded"),
        )
    spend = snap.get("spend") or {}
    if spend.get("cost_source"):
        quality += "；cost={}（{}）".format(
            "unavailable" if spend.get("amount") is None else round(float(spend["amount"]), 4),
            spend["cost_source"],
        )
    return [combination_line, f"- 集成事实：{integration}", model_line, basis, quality]


def _scope_lines(snap: dict) -> list[str]:
    all_ids = [row.get("id") for row in snap["rows"]]
    executed = [row.get("id") for row in _evidence.selected_rows(snap["rows"])]
    skipped = [row.get("id") for row in _evidence.skipped_rows(snap["rows"])]
    if not snap["rows"]:
        return [
            "- Verification scope: NOT_RUN（本 run 没有验证记录）",
            "- Project-wide verification: **NOT_RUN**",
        ]
    document = snap["document"]
    declared = (
        snap["verification_semantics"] == _contracts.SCOPE_SEMANTICS
        and isinstance(document, dict)
        and "verification_scope" in document
    )
    if declared:
        scope = "显式范围 {}/{}（未执行: {}）".format(
            len(executed), len(all_ids), ", ".join(skipped) or "无"
        )
    else:
        # The spec asked for everything (or predates scope entirely): say "全量",
        # do not describe it as a deliberate selection.
        scope = f"全量 {len(executed)}/{len(all_ids)}"
    if skipped:
        project = "**NOT_RUN**（本任务只验证声明范围；合并门禁需跑全量）"
    elif set(executed) == set(all_ids) and all(
        row.get("status") == _evidence.PASS for row in snap["rows"]
    ):
        project = "PASS（本 run 执行了项目全部 check）"
    else:
        project = "**NOT_RUN**"
    return [
        "- Verification scope: " + scope,
        "- Project-wide verification: " + project,
    ]


def _acceptance_table(snap: dict) -> str:
    document = snap["document"]
    if not isinstance(document, dict):
        return "## 验收项\n\n- spec 为 Markdown，无验收项映射；以 `检查明细` 为准。\n"
    by_id = {row.get("id"): row for row in snap["rows"]}
    review = snap.get("review") or {}
    items = {
        item.get("acceptance_id"): item
        for item in review.get("items") or []
        if isinstance(item, dict)
    }
    reviewer = review.get("reviewer") or {}
    lines = ["## 验收项", "", "| AC | 结论 | 依据 |", "|---|---|---|"]
    unjudged: list[str] = []
    for criterion in document.get("acceptance_criteria") or []:
        if not isinstance(criterion, dict):
            continue
        ac_id = criterion.get("id")
        verification = criterion.get("verification") or {}
        item = items.get(ac_id)
        if verification.get("type") == "manual":
            if item:
                # 逐条判定存在时，结论来自判者记录，不再写"未记录"。
                basis = (
                    item.get("finding")
                    or ", ".join(item.get("evidence_refs") or [])
                    or "见 review 记录"
                )
                lines.append(
                    "| {} | {} | {}：{} |".format(
                        ac_id, item.get("status"), reviewer.get("kind") or "review", basis
                    )
                )
            else:
                # 没有判定记录时仍然不说 PASS：这句空白本身就是待办。
                unjudged.append(ac_id)
                lines.append(
                    "| {} | 未记录逐项人工结论 | manual：{} |".format(
                        ac_id, verification.get("instructions") or "需人工确认"
                    )
                )
            continue
        check_id = verification.get("check_id")
        row = by_id.get(check_id)
        if row is None:
            lines.append(f"| {ac_id} | 无本期证据 | check `{check_id}` 未在本 run 执行 |")
        elif row.get("status") == _evidence.NOT_RUN:
            lines.append(f"| {ac_id} | INVALID | check `{check_id}` 不在声明范围内；核对 spec |")
        else:
            lines.append(
                "| {} | {} | check `{}` exit {} |".format(
                    ac_id, row.get("status"), check_id, row.get("exit_code")
                )
            )
    lines.append("")
    if unjudged:
        # 措辞刻意避开 ``NOT_RUN``：那个词在本报告里是 check 级保留语义
        # （"未声明执行"），混用会让读者以为某条 check 没跑。
        lines.append(
            "- 逐条判定：**未执行**（{} 条 manual AC 没有 review 记录）".format(len(unjudged))
        )
    elif review.get("verdict"):
        lines.append(
            "- 逐条判定：{}（{}；判者 {}；{}）".format(
                review.get("verdict"),
                review.get("state"),
                reviewer.get("actor") or reviewer.get("kind") or "unknown",
                "独立上下文" if reviewer.get("independent_context") else "未声明独立上下文",
            )
        )
    not_verified = [
        item for item in review.get("items") or [] if item.get("status") == "NOT_VERIFIED"
    ]
    if not_verified:
        lines.append("")
        lines.append("### 未验证项（NOT_VERIFIED，不计为通过）")
        lines.append("")
        for item in not_verified:
            lines.append(
                "- {}：{}".format(
                    item.get("acceptance_id"),
                    item.get("finding") or "未说明原因",
                )
            )
    open_items = snap.get("open_decisions") or []
    if open_items:
        lines.append("")
        lines.append("### 待拍板（阻断验收）")
        lines.append("")
        for item in open_items:
            lines.append(
                "- {}：{}（建议 {}）".format(
                    item.get("id"),
                    item.get("question"),
                    item.get("recommendation") or "未给建议",
                )
            )
    lines.append("")
    acceptance = snap["acceptance"] or {}
    if acceptance.get("decision"):
        lines.append(
            "- 任务整体验收：{}（actor: {}, at {}）".format(
                acceptance.get("decision"), acceptance.get("actor"), _iso(acceptance.get("at"))
            )
        )
    else:
        lines.append("- 任务整体验收：未验收")
    lines.append("")
    return "\n".join(lines)


def _cap(items: list, limit: int) -> tuple[list, int]:
    """Keep a section small: the truncation budget is spent on *what*, not *how many*."""
    return items[:limit], max(0, len(items) - limit)


def _check_table(snap: dict) -> str:
    rows = snap["rows"]
    if not rows:
        return "## 检查明细\n\n- 本 run 没有 `validation.json`（未进入验证阶段）。\n"
    kept, dropped = _cap(rows, 40)
    lines = ["## 检查明细", "", "| check | 状态 | exit | 日志 |", "|---|---|---|---|"]
    for row in kept:
        status = row.get("status")
        if status == _evidence.NOT_RUN:
            detail = "未纳入本任务声明范围"
        elif isinstance(row.get("error"), dict):
            detail = "{}: {}".format(row["error"].get("code"), row["error"].get("message"))
        else:
            detail = "runs/{}/check-{}.log".format(snap["run_id"], row.get("id"))
        exit_code = row.get("exit_code")
        lines.append(
            "| {} | {} | {} | {} |".format(
                row.get("id"),
                status,
                "—" if exit_code is None else exit_code,
                detail.replace("|", "\\|"),
            )
        )
    if dropped:
        lines.append(f"| … | | | 还有 {dropped} 条 check 未列出 |")
    lines.append("")
    return "\n".join(lines)


def _blockers(snap: dict) -> str:
    items = []
    if snap["block_code"]:
        items.append("- 阻塞：{}（{}）".format(snap["block_code"], snap["last_error"] or "无详情"))
    failed, more_failed = _cap(_evidence.failed_rows(snap["rows"]), 20)
    for row in failed:
        detail = ""
        if isinstance(row.get("error"), dict):
            detail = "（{}）".format(row["error"].get("code"))
        exit_code = row.get("exit_code")
        items.append(
            "- 检查失败：{} exit {}{}".format(
                row.get("id"), "—" if exit_code is None else exit_code, detail
            )
        )
    skipped, more_skipped = _cap(_evidence.skipped_rows(snap["rows"]), 20)
    for row in skipped:
        items.append("- 未执行：{}（{}）".format(row.get("id"), row.get("reason") or "未声明"))
    if more_failed:
        items.append(f"- …还有 {more_failed} 条失败未列出（见 validation.json）")
    if more_skipped:
        items.append(f"- …还有 {more_skipped} 条未执行未列出（见 validation.json）")
    if not items:
        items.append("- （空）")
    return "## 阻断与未完成\n" + "\n".join(items) + "\n"


def _boundary(snap: dict) -> str:
    items = []
    trust = snap["trust_level"] or "unknown"
    if trust in ("unverified", "unknown"):
        items.append(
            f"- 执行器 trust_level={trust}：本次运行无真实 e2e 证据，不得表述为「已验证」。"
        )
    skipped = _evidence.skipped_rows(snap["rows"])
    if skipped or not snap["rows"]:
        items.append("- 项目全量验证：NOT_RUN；合并门禁由 CI/PR 负责。")
    else:
        items.append("- 项目全量验证：本 run 执行了项目全部 check（仍不等于合并后通过）。")
    items.append("- 合并与验收未由控制器执行；accepted ≠ 已上线。")
    return "## 交付边界声明\n" + "\n".join(items) + "\n"


def render(snap: dict) -> str:
    """Render the markdown document, honouring the 16 KiB priority cut (§9.6)."""
    sections = {
        "header": _header(snap),
        "acceptance": _acceptance_table(snap),
        "checks": _check_table(snap),
        "blockers": _blockers(snap),
        "boundary": _boundary(snap),
    }
    full = "\n".join(sections[name] for name in _SECTIONS_ORDER)
    if len(full.encode("utf-8")) <= MAX_REPORT_BYTES:
        return redact(full)

    tail = "\ntruncated: true\n"
    budget = MAX_REPORT_BYTES - len(tail.encode("utf-8"))
    kept = set()
    for name in _PRIORITY:
        size = len(sections[name].encode("utf-8"))
        if size <= budget:
            kept.add(name)
            budget -= size
    text = "\n".join(sections[name] for name in _SECTIONS_ORDER if name in kept)
    return redact(text + tail)


def persist(store, t: dict, snap: dict, text: str) -> dict:
    """Write ``delivery-<spec_rev>.md`` for an already-taken snapshot."""
    path = report_path(store, t)
    atomic_text(path, text)
    return {
        "api_version": 2,
        "task_id": snap["task_id"],
        "spec_revision": snap["spec_revision"],
        "run_id": snap["run_id"],
        "report_path": str(path),
        "generated_at": snap["generated_at"],
    }
