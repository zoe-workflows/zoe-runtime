"""Zoe Runtime CLI entry point.

``main(argv=None) -> int`` parses arguments, dispatches to the runtime, and
converts known errors into a single structured JSON shape. Both the console
entry point (``zoe = "src.cli:main"``) and the module entry point
(``python -m src``) call this same function, so a packaged install never
loses the background supervisor launch path.

Return value is the process exit code (0 on success, non-zero otherwise). Legacy
commands keep the published ``4`` for every business error; the v0.3 commands use
the finer mapping from the design (§12) and every response carries ``error.code``
so a host can branch without parsing text.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from src import (
    _agents,
    _config_source,
    _context,
    _contracts,
    _coverage,
    _diff,
    _digest,
    _groups,
    _logstream,
    _migrations,
    _prepare,
    _profiles,
    _progress,
    _reports,
    _requirements,
    _routing,
    _store,
    _task_review,
)
from src import _orchestration as _orch
from src import _qualification as qualification_mod
from src import _summaries as summaries_mod
from src import core as runtime
from src._config import MAX_ACTIVE_TASKS, path_warnings
from src._pipeline import SUPERVISOR_LOCK_WAIT, slots
from src._platform import get_platform
from src._util import Error, digest, redact_secrets, require
from src.core import *  # noqa: F401,F403  (re-exported for tests/legacy)

EXIT_OK = 0
EXIT_ERROR = 4

#: Commands introduced or re-specified in v0.3.0+. They use the granular exit
#: codes below; older commands keep exit code 4 so existing scripts do not break.
V2_COMMANDS = {
    "capabilities", "agent", "group", "state", "watch", "qualification",
    "evidence", "orchestration", "route",
}

#: ``task create/start/stop/revise/accept`` published exit code 4 in v0.2 and keep
#: it. The mutations added in v0.3.0+ use the granular mapping (design §12).
V2_TASK_ACTIONS = {
    "reassign", "rebind", "events", "logs", "recover", "answer", "summary",
    "handoff", "close-external", "review", "coverage", "graph",
}

_EXIT_CODES = {
    "INPUT_INCOMPLETE": 2,
    "SPEC_CLARIFICATION_REQUIRED": 2,
    "STATE_CONFLICT": 3,
    "REQUEST_CONFLICT": 3,
    "SLOT_BUSY": 3,
    "HANDOFF_STALE": 3,
    "EVIDENCE_INVALID": 3,
    "EVIDENCE_MISSING": 3,
    "CURSOR_EXPIRED": 3,
    "PROTOCOL_UNSUPPORTED": 3,
    "MAINTENANCE_REQUIRED": 3,
    # The skill wrapper treats any non-zero exit as a rejection; 5 names the
    # environment class so a human reading logs sees "wrong runtime", not "bad input".
    "RUNTIME_INCOMPATIBLE": 5,
    "ENVIRONMENT_ERROR": 4,
    "CAPABILITY_UNSUPPORTED": 4,
    "ADAPTER_UNVERIFIED": 4,
    "ADAPTER_VERSION_MISMATCH": 4,
    "ADAPTER_BACKEND_MISMATCH": 4,
    "MIGRATION_REQUIRED": 4,
    "SCHEMA_UNSUPPORTED": 4,
    "PROCESS_UNKNOWN": 4,
    "PROMPT_TOO_LARGE": 4,
    "OUTPUT_LIMIT_EXCEEDED": 4,
    "RUN_TIMEOUT": 4,
    "QUOTA_EXHAUSTED": 4,
    "TASK_BUSY": 4,
    "REPO_BUSY": 4,
    "PUBLISH_BUSY": 4,
    "MAINTENANCE_BUSY": 4,
    "DEPENDENCY_BLOCKED": 4,
    "DEPENDENCY_STALE": 4,
    "PATH_CONFLICT": 4,
    "QUALIFICATION_REQUIRED": 4,
    "INCOMPLETE_QUALIFICATION": 4,
    "DECISION_CONFLICT": 4,
    "NOT_OWNER": 4,
    "BUDGET_EXHAUSTED": 4,
    "SCOPE_VIOLATION": 4,
    "EXTRA_COMMIT": 4,
    "PROJECT_MISMATCH": 4,
    "TASK_RUN_MISMATCH": 4,
    "SESSION_SCOPE_MISMATCH": 4,
    "MISSING_CONTEXT": 4,
    "RESOURCE_CONFLICT": 4,
    "REMOTE_DRIFT": 4,
    # v1.6：任务的冻结基线已不在目标分支上（rebase / force-push / 分支被改写）。
    # 4 与 REMOTE_DRIFT 同族：不是"重试即可"，也不是 POLICY_CHANGED 那条 rebind 路径，
    # 下一步是确认目标分支现状后重冻结，而不是强推。
    "BASE_DRIFT": 4,
    "AUTH_REQUIRED": 5,
    "POLICY_DENIED": 5,
    "POLICY_CHANGED": 5,
    # v1.5.0：声明了 routing 段但候选集合未被确认（或确认已因配置变化失效）。
    # 5 = 一个闸门拒绝了派发，与 POLICY_DENIED 同族，但下一步不同：这里要做的是
    # "确认一次"，不是"改配置"。
    "ALLOWLIST_UNCONFIRMED": 5,
    # v1.5.1：`project attest --expect-policy-hash/--expect-profiles` 与锁内读到的
    # 当前配置不一致。5 = 一个闸门拒绝了写入（不是"重试即可"，也不是 POLICY_CHANGED
    # 的 `task rebind` 路径），下一步是重新预览、重新取得同意。
    "ATTEST_PRECONDITION_FAILED": 5,
    "PERMISSION_PROFILE_UNBOUND": 5,
    "PLATFORM_UNSUPPORTED": 5,
    "REMOTE_UNKNOWN": 6,
    # v1.2 execution-combination routing (§15.4). 4 = "the environment cannot do
    # it", 5 = "a gate refused", 3 = "the record/decision is not trustworthy".
    # MODEL_UNAVAILABLE belongs to the deferred `route probe-model` surface
    # (§15.2), so no current code path emits it; the mapping is frozen anyway so
    # the contract does not shift when that command lands.
    "MODEL_UNAVAILABLE": 4,
    "MODEL_MISMATCH": 5,
    "ENDPOINT_UNVERIFIED": 5,
    "SPEND_LIMIT_EXCEEDED": 5,
    "SPEND_UNVERIFIABLE": 5,
    "ROUTE_NO_ELIGIBLE": 4,
    "ROUTE_AMBIGUOUS": 3,
    "ROUTE_RECORD_INVALID": 3,
    # v1.3 task-group delivery (TGD §11.3) — the four codes and their numbers
    # are taken verbatim from the frozen table: an integration whose ref cannot
    # be explained is a *refusal to conclude*, and the design puts every
    # integration code at 5 so a host has one branch for "delivery stopped".
    "INTEGRATION_CONFLICT": 5,
    "INTEGRATION_CHECK_FAILED": 5,
    "INTEGRATION_UNKNOWN": 5,
    "GROUP_BUDGET_EXHAUSTED": 5,
    # v1.7 逐条 AC 判定（第二层验收）。3 = 记录不可信/未准备好，5 = 一个闸门
    # 拒绝了验收。分开的理由与组级一致：前者要"补一份结论"，后者要"改实现"。
    "ACCEPTANCE_UNREVIEWED": 3,
    "ACCEPTANCE_UNACKNOWLEDGED": 3,
    "ACCEPTANCE_REVIEW_FAILED": 5,
    "DECISIONS_OPEN": 5,
    # verify 阶段发现需求文档的复选框没跟着代码走：不是测试失败，是交付物缺失。
    "PROGRESS_NOT_TICKED": 5,
    # 上游需求没闭环就派发下游：等，不是改。宿主应当去推上游，而不是重试这一条。
    "DEPENDENCY_NOT_MET": 5,
}


def _granular_exit_codes(args) -> bool:
    """Whether this invocation uses the v0.3 error→exit mapping."""
    cmd = getattr(args, "cmd", "")
    return cmd in V2_COMMANDS or (cmd == "task" and getattr(args, "action", "") in V2_TASK_ACTIONS)


def output(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _default_home() -> str:
    return os.environ.get("ZOE_HOME", str(Path.home() / ".local/share/zoe"))


#: One line per command path, applied to the parser in ``_build_parser``.
#:
#: A single table, not 55 scattered ``help=`` keywords: the same table is what a
#: host or a doc gate can compare against, and it makes "this command has no
#: description" a visible omission instead of an accident.
_COMMAND_HELP = {
    # project / host
    "project": "注册项目、读取项目上下文",
    "project register": "注册项目配置（不认证兼容性，也不授予隔离）",
    "project context": "输出项目上下文与身份链（只读）",
    "project attest": "确认当前配置允许派发的 profile 集合（绑定配置摘要，改变即失效）",
    "project quota": "额度耗尽闸门：只读查看，或由人显式解除（保留事故记录）",
    "project quota show": "只读：当前是否有额度事故在阻止派发",
    "project quota clear": "显式解除闸门；理由写入记录，不验证额度是否真的恢复",
    "doctor": "就绪度诊断：环境、执行器、检查命令可解析性（不带 --json 时输出人读摘要）",
    "reconcile": "按持久化的进程树事实收口项目内已知 Run（可能改变 Run 状态）",
    "demo": "跑一次纯仿真的演示流水线（不调用真实 Agent、不写远端）",
    "capabilities": "输出运行器能力面（适配器、并发、观测、不支持项）",
    "agent": "执行器 profile 能力与探测",
    "agent list": "列出项目内 profile 的能力态与证据来源",
    "agent probe": "有界探测该 profile 的 CLI 版本（不产生模型调用）",
    "route": "执行组合路由（预览/统计）",
    "route plan": "预览组合决策（不建任务、不写决策文件）",
    "route stats": "只读聚合历史组合表现（不参与选择）",
    # groups
    "group": "任务组：冻结计划 → 串行集成 → 一次验收（组内串行）",
    "group plan": "预演计划（--project+--request-file）或查看已有组准入快照",
    "group create": "冻结计划（不派发）",
    "group start": "起一次推进器（短命进程，不是守护服务）",
    "group dispatch": "显式派发一个成员（与推进器同一准入函数）",
    "group status": "组状态与进度（--refresh 先对账）",
    "group pause": "暂停推进（默认 drain；--stop 同时停成员进程树）",
    "group resume": "重新预检/对账并换新租约",
    "group cancel": "取消（不是验收；--external 记录外部收口）",
    "group accept": "组级人工验收（成员不能自验收）",
    "group review": "提交/查询设计一致性审查结论（宿主执行，Runtime 只校验）",
    "group repair": "有界修复：一次只修一条 FAIL finding",
    "group report": "七段组报告（--write 落盘）",
    "internal-group": "推进器入口（由 group start 派生；宿主不得直接调用）",
    # state / streams
    "state": "状态库迁移与恢复（停机执行）",
    "state migrate": "把状态库升到当前 schema（先写备份）",
    "state restore": "从备份目录恢复状态库（需 --confirm）",
    "watch": "查询/跟随状态事件流（epoch 游标，有界，不常驻）",
    "qualification": "本机 CLI 资格验证",
    "qualification run": "对某 (project, profile) 跑资格验证并产出证据",
    "evidence": "资格证据登记",
    "evidence record": "把一次资格验证登记为可用证据",
    "orchestration": "项目级编排记录（决策级 CAS，一行）",
    "orchestration show": "读取编排记录",
    "orchestration resume": "读取目标/已派发/待答问题与下一步（接管入口）",
    "orchestration claim": "以某宿主身份占用项目（多宿主防并行写）",
    "orchestration release": "释放项目占用",
    "orchestration update": "更新编排记录（需 expected_version）",
    # tasks
    "task": "单个任务的创建、派发、观察、变更与验收",
    "task create": "冻结 Spec 并建任务（READY，不派发）",
    "task list": "列出任务（可按项目过滤）",
    "task status": "任务状态快照（--refresh 先对账；含观测事实 progress）",
    "task report": "交付视图（验收项/检查明细/阻断/边界）",
    "task events": "按任务分页读取状态事件（可跟随）",
    "task logs": "读取某次 run 的日志（默认快照；--follow 为增量流）",
    "task start": "派发执行（写者唯一；受槽位与准入约束）",
    "task resume": "从暂停/阻塞/未完成阶段继续",
    "task pause": "暂停（核实安全停止后再收口）",
    "task cancel": "取消任务（不是验收）",
    "task revise": "保存新 Spec（版本 +1，旧验证失效）",
    "task accept": "人工验收（组成员被拒，须走 group accept）",
    "task reassign": "更换实现者 profile（有活跃写者时拒绝）",
    "task rebind": "显式接纳新项目策略（旧证据失效）",
    "task close-external": "记录「交付在 Zoe 之外发生」（不是验收结论）",
    "task answer": "回答 Agent 提问（只解除阻塞，不自行续跑）",
    "task recover": "人工收口状态不可观测的 Run（--run 缺省取 current_run_id）",
    "task summary": "版本化可读摘要",
    "task summary submit": "提交一份绑定事件游标的摘要",
    "task summary list": "列出摘要版本",
    "task handoff": "交接材料",
    "task handoff show": "输出续接所需的结构化交接材料",
    "internal": "supervisor 入口（由 task start 派生；宿主不得直接调用）",
}

#: Extra examples for the commands where the first correct invocation is not
#: obvious from the flags alone.
_COMMAND_EPILOG = {
    "task start": "示例：zoe task start TASK --request-file start.json",
    "task logs": "示例：zoe task logs TASK --follow --timeout 60（--run 缺省取当前 run）",
    "watch": "示例：zoe watch --project P --follow --jsonl --after 1793500000:120 --timeout 60",
    "group plan": "示例：zoe group plan --project P --request-file plan.json（预演，不写任何东西）",
}


def _apply_command_help(parser, prefix: str = "") -> None:
    """Attach the one-line descriptions from ``_COMMAND_HELP``.

    ``argparse`` only lists a sub-command's help when the parser was created with
    ``help=`` — the pseudo action that carries it is not created otherwise. Since
    the text lives in one table here, the pseudo actions are rebuilt in
    declaration order (``action.choices`` keeps insertion order) so the help
    listing stays in the same order as the code.
    """
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        existing = {pseudo.dest: pseudo for pseudo in getattr(action, "_choices_actions", [])}
        rebuilt = []
        for name, child in action.choices.items():
            path = prefix + name
            text = _COMMAND_HELP.get(path)
            pseudo = existing.get(name)
            if pseudo is None and text:
                pseudo = action._ChoicesPseudoAction(name, (), text)
            elif pseudo is not None and text:
                pseudo.help = text
            if pseudo is not None:
                rebuilt.append(pseudo)
            if text:
                child.description = text
                if not child.epilog and _COMMAND_EPILOG.get(path):
                    child.epilog = _COMMAND_EPILOG[path]
            _apply_command_help(child, path + " ")
        action._choices_actions = rebuilt


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="zoe",
        description=(
            "Zoe Runtime：单机单用户的任务编排（local-process 执行、受管进程树、"
            "workspace 恢复）。"
        ),
        epilog=(
            "常用路径：project register → doctor → task create → task start → "
            "task status / task logs --follow → task report。\n"
            "所有命令默认输出 JSON；--json/--jsonl 为兼容或行式输出选项。"
        ),
    )
    p.add_argument("--home", default=_default_home())
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    register = sub.add_parser("project")
    project_actions = register.add_subparsers(dest="action", required=True)
    reg = project_actions.add_parser("register")
    reg.add_argument("--config", required=True)
    reg.add_argument(
        "--dry-run",
        action="store_true",
        help="校验配置并输出注册结果，不写状态库、不写项目配置",
    )
    reg.add_argument("--json", action="store_true")
    ctx = project_actions.add_parser("context")
    ctx.add_argument("project_id")
    ctx.add_argument("--task")
    ctx.add_argument("--json", action="store_true")
    # v1.5.0 §5：授权确认记录。与 register 分开，是因为"配置内容没变也要能确认
    # 一次"——靠摘要变化触发确认是做不到的（注册相同内容摘要不变）。
    att = project_actions.add_parser("attest")
    att.add_argument("--project", required=True, dest="project")
    att.add_argument(
        "--profiles",
        help="收窄要确认的候选（逗号分隔）；只能小于等于当前允许集合，不能扩大",
    )
    att.add_argument("--dry-run", action="store_true", help="只输出将要写入的内容")
    # v1.5.1：预览与写入之间的 TOCTOU 只能靠"在写入锁内比对"关掉。不带这两个参数
    # 时行为与 v1.5.0 完全一致（只能事后发现不一致）。
    att.add_argument(
        "--expect-policy-hash",
        dest="expect_policy_hash",
        help="写入前在锁内比对：当前配置摘要必须等于这个值，否则拒绝写入（不落记录）",
    )
    att.add_argument(
        "--expect-profiles",
        dest="expect_profiles",
        help="写入前在锁内比对：确认集合必须等于这个集合（逗号分隔），否则拒绝写入",
    )
    att.add_argument("--json", action="store_true")
    # v1.6 §B4：改配置前先看影响面。只读，且复用 rebind 的 spec 校验，
    # 避免"预览说还合法、真 rebind 却被拒"。
    df = project_actions.add_parser("diff")
    df.add_argument("--config", required=True, help="候选配置；只比较，不写")
    df.add_argument("--project", help="缺省取配置里的 project_id")
    df.add_argument("--json", action="store_true")
    # v1.6：额度耗尽的项目级闸门。读与解除分成两个动作：`show` 是只读事实，
    # `clear` 是人的决定（"额度回来了/换了账号"），时钟推断不能代替它。
    q = project_actions.add_parser("quota")
    quota_actions = q.add_subparsers(dest="quota_action", required=True)
    qs = quota_actions.add_parser("show")
    qs.add_argument("--project", required=True, dest="project")
    qs.add_argument("--json", action="store_true")
    qc = quota_actions.add_parser("clear")
    qc.add_argument("--project", required=True, dest="project")
    qc.add_argument("--why", required=True, help="为什么确认额度已恢复（写入记录）")
    qc.add_argument("--actor", help="谁确认的；缺省记 human")
    qc.add_argument("--json", action="store_true")

    # doctor: --project is optional; --require-ready forces non-zero when a
    # project is not ready (for scripting). Without --project, only the host is
    # diagnosed and no state directory or database is created.
    doc = sub.add_parser("doctor")
    doc.add_argument("--project")
    doc.add_argument("--agent-profile")
    doc.add_argument("--require-ready", action="store_true")
    doc.add_argument("--json", action="store_true")

    rec = sub.add_parser("reconcile")
    rec.add_argument("--project", required=True)
    rec.add_argument("--json", action="store_true")

    # Demo: isolated, simulated, no real agent/remote write.
    demo = sub.add_parser("demo")
    demo.add_argument("--json", action="store_true")

    caps = sub.add_parser("capabilities")
    caps.add_argument("--json", action="store_true")
    # Compatibility gate used by the skill wrapper (§2.2): the runtime parses the
    # skill's declaration, because the skill must not depend on jq/python one-liners.
    caps.add_argument("--check-requirements", metavar="FILE")

    agent = sub.add_parser("agent")
    agent_actions = agent.add_subparsers(dest="action", required=True)
    a = agent_actions.add_parser("list")
    a.add_argument("--project", required=True)
    a.add_argument("--json", action="store_true")
    a = agent_actions.add_parser("probe")
    a.add_argument("--project", required=True)
    a.add_argument("--profile", required=True)
    a.add_argument("--json", action="store_true")

    route = sub.add_parser("route")
    route_actions = route.add_subparsers(dest="action", required=True)
    a = route_actions.add_parser("plan")
    a.add_argument("--project", required=True)
    # 请求可选：没有请求时预览"默认/显式"路径，行为可预测且可解释。
    a.add_argument("--request-file")
    a.add_argument("--profile")
    a.add_argument("--json", action="store_true")
    # §15.1：不做隐藏探测。带 __version__ 的 CLI 调用代价可观，必须能被关掉。
    a.add_argument("--no-probe", dest="probe", action="store_false")
    a.set_defaults(probe=True)
    a = route_actions.add_parser("stats")
    a.add_argument("--project", required=True)
    a.add_argument("--window", type=int, default=30, help="lookback days (default 30)")
    a.add_argument("--json", action="store_true")
    # 默认**不写**：例行调用不得产生不受管理的长期累积文件（§15.3）。
    a.add_argument("--write", action="store_true")

    group = sub.add_parser("group")
    group_actions = group.add_subparsers(dest="action", required=True)
    a = group_actions.add_parser("create")
    a.add_argument("--request-file", required=True)
    a = group_actions.add_parser("status")
    a.add_argument("id")
    a.add_argument("--refresh", action="store_true")
    a.add_argument("--json", action="store_true")
    # `plan` has two forms (TGD §11.1): validate a plan request without writing
    # anything, or show the dispatch admission view of an existing group.
    a = group_actions.add_parser("plan")
    a.add_argument("id", nargs="?")
    a.add_argument("--project")
    a.add_argument("--request-file")
    a.add_argument("--json", action="store_true")
    a = group_actions.add_parser("dispatch")
    a.add_argument("id")
    a.add_argument("--item", required=True)
    a.add_argument("--request-file", required=True)
    a = group_actions.add_parser("start")
    a.add_argument("id")
    a.add_argument("--request-file", required=True)
    a = group_actions.add_parser("pause")
    a.add_argument("id")
    a.add_argument("--request-file", required=True)
    a.add_argument("--stop", action="store_true")
    a = group_actions.add_parser("resume")
    a.add_argument("id")
    a.add_argument("--request-file", required=True)
    a = group_actions.add_parser("cancel")
    a.add_argument("id")
    a.add_argument("--request-file", required=True)
    a.add_argument("--stop", action="store_true")
    a.add_argument("--external", action="store_true")
    a = group_actions.add_parser("accept")
    a.add_argument("id")
    a.add_argument("--request-file", required=True)
    # 设计一致性审查（§11.1 第二层）：无 --request-file 时是只读查询。
    a = group_actions.add_parser("review")
    a.add_argument("id")
    a.add_argument("--request-file")
    a.add_argument("--json", action="store_true")
    # 有界修复（§12）：一次执行只针对一条 FAIL finding。
    a = group_actions.add_parser("repair")
    a.add_argument("id")
    a.add_argument("--request-file", required=True)
    a = group_actions.add_parser("report")
    a.add_argument("id")
    a.add_argument("--write", action="store_true")
    a.add_argument("--json", action="store_true")
    # TGD §11.1: `--format markdown|json`. `--json` stays as the older spelling
    # of `--format json` so existing callers keep working.
    a.add_argument("--format", choices=("markdown", "json"), default=None)
    a = sub.add_parser("internal-group")
    a.add_argument("group_id")
    a.add_argument("lease_id")

    state = sub.add_parser("state")
    state_actions = state.add_subparsers(dest="action", required=True)
    a = state_actions.add_parser("migrate")
    # v1.3 raises the current schema to 4; the default follows the binary rather
    # than a frozen literal, so a v3 database is one command away from usable.
    a.add_argument("--to", type=int, default=_store.SCHEMA_VERSION)
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--json", action="store_true")
    a = state_actions.add_parser("restore")
    a.add_argument("--from", dest="from_dir", required=True)
    a.add_argument("--confirm", action="store_true")

    watch = sub.add_parser(
        "watch",
        help="查询/跟随状态事件流（epoch 游标，有界，不常驻）",
        description=(
            "有界事件查询：按 project/group/task 过滤，游标形如 <epoch>:<event_id>。"
            "--follow 逐行输出 JSON 直到 --timeout 到期；首行/末行是 kind=meta 的"
            "游标行（follow_start / follow_end），末行的 next_cursor 可直接用于下一轮 --after。"
            "中途没有匹配事件时不输出事件行。"
        ),
        epilog=(
            "示例：zoe watch --project P --follow --jsonl --after 1793500000:120 --timeout 60"
        ),
    )
    watch.add_argument("--project", help="只看该项目的任务/组事件（校验事件归属）")
    watch.add_argument("--group", help="只看该组的事件及其成员任务事件")
    watch.add_argument(
        "--task", dest="task_filter", help="只看该任务的事件（与 --project/--group 取交集）"
    )
    watch.add_argument("--after", help="续接游标 <epoch>:<event_id>；缺省从头开始")
    watch.add_argument("--timeout", type=float, default=20, help="--follow 的生存时长（秒，默认 20）")
    watch.add_argument("--max-events", type=int, default=200, help="单页最多事件数（默认 200）")
    watch.add_argument("--max-pages", type=int, default=3, help="单次调用最多翻页数（默认 3）")
    watch.add_argument("--follow", action="store_true", help="持续输出直到 --timeout 到期（非常驻服务）")
    watch.add_argument("--jsonl", action="store_true", help="非 follow 模式下一行一个事件，而不是一个数组")
    watch.add_argument("--json", action="store_true", help="兼容选项：本命令默认输出 JSON")

    qual = sub.add_parser("qualification")
    qual_actions = qual.add_subparsers(dest="action", required=True)
    a = qual_actions.add_parser("run")
    # Qualification is always bound to a project: the evidence key and the
    # profile digest are resolved from that project's configuration.
    a.add_argument("--project", dest="project_id", required=True)
    a.add_argument("--profile", required=True)
    a.add_argument("--fixture")
    a.add_argument("--repo", required=True)
    a.add_argument("--out", required=True)
    # 真实运行的预算：整体截止时间与"跑多久之后叫停"（用于证明树能被停止）。
    a.add_argument("--timeout", type=float, default=1800.0)
    a.add_argument("--stop-after", type=float, default=3.0)
    a.add_argument("--replace", action="store_true")
    a.add_argument("--json", action="store_true")

    ev = sub.add_parser("evidence")
    ev_actions = ev.add_subparsers(dest="action", required=True)
    a = ev_actions.add_parser("record")
    a.add_argument("--qualification", required=True)
    a.add_argument("--json", action="store_true")

    orch = sub.add_parser("orchestration")
    orch_actions = orch.add_subparsers(dest="action", required=True)
    for name in ("show", "resume"):
        a = orch_actions.add_parser(name)
        a.add_argument("--project", required=True)
        a.add_argument("--json", action="store_true")
    # release must name the owner it is releasing; the handler reads ``args.host``.
    a = orch_actions.add_parser("release")
    a.add_argument("--project", required=True)
    a.add_argument("--as", dest="host", required=True)
    a.add_argument("--json", action="store_true")
    a = orch_actions.add_parser("update")
    a.add_argument("--request-file", required=True)
    a = orch_actions.add_parser("claim")
    a.add_argument("--project", required=True)
    a.add_argument("--as", dest="host", required=True)
    a.add_argument("--force", action="store_true")
    a.add_argument("--session-id")
    a.add_argument("--json", action="store_true")

    task = sub.add_parser("task")
    actions = task.add_subparsers(dest="action", required=True)
    a = actions.add_parser("create")
    a.add_argument("--request-file", required=True)
    a = actions.add_parser(
        "prepare",
        help="只读准备：复用项目配置、预检 spec 与 checks、生成草稿",
        description=(
            "不写状态库、不建任务。默认只预检；--out 保存报告；--emit 生成 "
            "spec.json 与 create.json 草稿（已存在则保留）。"
        ),
    )
    a.add_argument("--project", help="已注册的项目 id")
    a.add_argument("--repo", help="仓库路径；用于匹配已注册项目，或生成注册草案")
    a.add_argument("--spec", help="待创建的 spec 文件")
    a.add_argument("--out", help="把完整预检报告写到该文件")
    a.add_argument("--emit", help="生成草稿的目录")
    a.add_argument("--title", help="草稿标题；缺省写入 <FILL:...> 占位符")
    a.add_argument("--profile", help="用于校验的 agent profile；缺省用项目默认")
    a.add_argument("--force", action="store_true", help="覆盖已存在的 --out 报告")
    a.add_argument("--json", action="store_true")
    a = actions.add_parser("list")
    a.add_argument("--project")
    # v1.6：任务记录含冻结 `config` 副本，40 个任务时整包输出会明显臃肿。
    # 字段名拼错必须报错（静默返回 null 是这类投影最常见的坑）。
    a.add_argument(
        "--fields",
        metavar="A,B",
        help="只输出这些顶层字段（逗号分隔）；未知字段报 INPUT_INCOMPLETE",
    )
    a.add_argument("--json", action="store_true")
    a = actions.add_parser(
        "digest",
        help="一批任务的事实聚合（状态 + 最近内容），不判断完成",
        description=(
            "一次调用输出多任务视图：state/phase/耗时/静默/日志字节 + 最近一条文本与"
            "工具调用。只聚合事实，不产出结论，不推进状态。"
        ),
        epilog=(
            "示例：zoe task digest --project P --targets \"T1 T2\" --json；"
            "任务多时用 --no-content 只读状态"
        ),
    )
    a.add_argument("--project", required=True)
    a.add_argument("--targets", help="空格分隔的 task id；缺省取项目内所有非终态任务")
    a.add_argument(
        "--tail-bytes",
        type=int,
        help=f"日志读取窗口（1..{_logstream.MAX_TAIL_BYTES}，默认 {_logstream.DEFAULT_TAIL_BYTES}）",
    )
    a.add_argument("--lines", type=int, default=2, help="每任务最多几条内容行（1..10，默认 2）")
    a.add_argument(
        "--no-content",
        dest="no_content",
        action="store_true",
        help="只读状态，不读日志（任务多或 CLI 慢时降级）",
    )
    a.add_argument("--json", action="store_true", help="兼容选项：本命令默认输出 JSON")
    a = actions.add_parser(
        "coverage",
        help="需求 marker 覆盖账本（只读）",
        description=(
            "按冻结 spec 的 progress_refs 汇总需求覆盖：covered / pending / missing /"
            " duplicate / unmapped。`cleared` 是唯一可以被读成「清单清零」的判据。"
        ),
        epilog=(
            "示例：zoe task coverage --project P --markers-file .zoe/requirements.txt --json"
        ),
    )
    a.add_argument("--project", required=True)
    a.add_argument("--markers-file", required=True, help="每行一个 marker，忽略空行与 # 注释")
    a.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="markdown 用于贴进汇报（含未闭环逐条清单）",
    )
    a.add_argument("--json", action="store_true", help="兼容选项：等同于 --format json")
    a = actions.add_parser(
        "graph",
        help="依赖图与阻塞点（只读）",
        description=(
            "按 spec 的 depends_on（marker）与各任务的 progress_refs 求依赖图，"
            "输出节点、依赖边、阻塞点与环。判定与 start 门禁同源：上游必须 DONE + "
            "accepted + 逐条判定满足。"
        ),
        epilog="示例：zoe task graph --project P --format markdown",
    )
    a.add_argument("--project", required=True)
    a.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="markdown 用于「谁堵了谁」的汇报",
    )
    a.add_argument("--json", action="store_true", help="兼容选项：等同于 --format json")
    a = actions.add_parser(
        "review",
        help="逐条 AC 判定：无 --request-file 时只读查询",
        description=(
            "提交一份绑定 validated_sha 的逐条判定（schemas/task-review.v1.json）。"
            "非法输出留档为 invalid 并被拒绝：它不会变成一次通过。"
        ),
    )
    a.add_argument("id")
    a.add_argument("--request-file")
    a.add_argument("--json", action="store_true", help="兼容选项：本命令默认输出 JSON")
    for name in ("status", "report"):
        a = actions.add_parser(name)
        a.add_argument("id")
        a.add_argument("--refresh", action="store_true")
        a.add_argument("--json", action="store_true")
        a.add_argument("--format", choices=["json", "markdown"], default="json")
        if name == "report":
            # 只写 delivery-<rev>.md，绝不碰 summary submit 渲染的 report.md。
            a.add_argument("--write", action="store_true")
    a = actions.add_parser(
        "events",
        help="按任务分页读取状态事件（可跟随）",
        description="单调 event id 分页；--follow 时逐行输出 JSON，直到 --timeout 到期。",
        epilog="示例：zoe task events TASK --after 120 --follow --timeout 60",
    )
    a.add_argument("id", help="task id")
    a.add_argument("--after", type=int, default=0, help="只返回 id 大于该值的事件（默认 0）")
    a.add_argument("--limit", type=int, default=100, help="单页上限 1..500（默认 100）")
    a.add_argument("--follow", action="store_true", help="持续输出新事件直到 --timeout 到期")
    a.add_argument("--timeout", type=float, default=20, help="--follow 的生存时长（秒，默认 20）")
    a.add_argument("--max-events", type=int, default=200)
    a.add_argument("--max-pages", type=int, default=3)
    a.add_argument("--json", action="store_true", help="兼容选项：本命令默认输出 JSON")
    a = actions.add_parser(
        "logs",
        help="读取某次 run 的日志（默认快照；--after 增量快照；--follow 增量流）",
        description=(
            "默认返回一次有界尾部快照（--tail-bytes 缺省按 65536，上限 1048576），"
            "只投影完整记录，响应里 snapshot=true 并给出续读游标。--after 从游标继续读快照"
            "（非阻塞增量）。--follow 改为增量流：只输出完整记录，无新增时静默；"
            "游标形如 <run>:<generation>:<offset>。"
        ),
        epilog=(
            "示例：zoe task logs TASK --follow --projection auto --timeout 60；"
            "zoe task logs TASK --follow --tail-bytes 65536；"
            "zoe task logs TASK --after <run>:1:4096"
        ),
    )
    a.add_argument("id", help="task id")
    a.add_argument("--run", help="指定 run；缺省取 current_run_id（历史 run 必须显式给）")
    a.add_argument("--log", default="agent", help="日志名，默认 agent（check 运行为 check-<id>）")
    a.add_argument(
        "--tail-bytes",
        type=int,
        default=None,
        help=(
            "窗口字节上限（1..1048576）。快照缺省 65536；--follow 缺省不回看（从当前末尾开始），"
            "显式给出才回看该窗口；与 --cursor 同用时被忽略"
        ),
    )
    a.add_argument("--follow", action="store_true", help="增量跟随（默认从文件末尾开始）")
    a.add_argument(
        "--after",
        metavar="CURSOR",
        help=(
            "从该游标继续读快照（形状 run:gen:offset，与 --follow 的 cursor 同构；"
            "不能与 --follow 同用）"
        ),
    )
    a.add_argument("--cursor", help="续读游标 <run>:<generation>:<offset>（含 run，跨轮转有效）")
    a.add_argument(
        "--projection",
        choices=("auto", "raw", "events"),
        default="auto",
        help="auto=逐行识别结构化记录；raw=纯文本（仍脱敏）；events=只输出结构化动作",
    )
    a.add_argument("--timeout", type=float, default=60, help="--follow 的生存时长（秒，默认 60）")
    a.add_argument(
        "--max-bytes", type=int, default=0, help="本次输出上限（0=不限）；触达时返回续读游标"
    )
    a.add_argument("--json", action="store_true", help="兼容选项：快照默认输出 JSON")
    for name in ("start", "resume", "pause", "cancel", "revise", "accept",
                 "reassign", "rebind", "close-external"):
        a = actions.add_parser(name)
        a.add_argument("id")
        a.add_argument("--request-file", required=True)
    a = actions.add_parser("answer")
    a.add_argument("id")
    a.add_argument("--request-file", required=True)
    a = actions.add_parser("recover")
    a.add_argument("id")
    a.add_argument(
        "--run",
        help="要收口的 run；缺省取任务的 current_run_id（响应里的 run_source 写来源）",
    )
    a.add_argument("--confirm-stop", action="store_true")
    # 输出本来就是 JSON；保留 --json 是为了脚本能用统一的写法，而不是记住哪个
    # 子命令"恰好"接受它。
    a.add_argument("--json", action="store_true", help="兼容选项：本命令默认输出 JSON")
    summary = actions.add_parser("summary")
    summary_actions = summary.add_subparsers(dest="summary_action", required=True)
    a = summary_actions.add_parser("submit")
    a.add_argument("id")
    a.add_argument("--request-file", required=True)
    a = summary_actions.add_parser("list")
    a.add_argument("id")
    a.add_argument("--json", action="store_true")
    handoff = actions.add_parser("handoff")
    handoff_actions = handoff.add_subparsers(dest="handoff_action", required=True)
    a = handoff_actions.add_parser("show")
    a.add_argument("id")
    a.add_argument("--json", action="store_true")
    a = sub.add_parser("internal")
    a.add_argument("run_id")
    a.add_argument("attempt_id")
    _apply_command_help(p)
    return p


def _parse(args):
    p = _build_parser()
    return p.parse_args(args)


def doctor_without_project(home: str) -> dict:
    """Diagnose the host environment without touching the state database.

    Returns a dict with the same top-level shape as a project doctor, plus a
    human-readable ``verdict``. Does not create ``home`` or ``state.db``.
    """
    checks = {}
    for name in ("git", "gh"):
        checks[name] = _which(name)
    # With `local-process` as the only execution backend the CLI runs on this
    # host, so its absence *is* an execution precondition. gh is only needed for
    # GitHub publication. Docker is not checked at all: it left the product scope
    # in v0.5.0, and requiring it would be a false prerequisite (§18.3).
    checks["codebuddy"] = _which("codebuddy") or _which("cbc")
    checks["process_identity"] = _process_identity_available()
    from src._config import _platform_lock_ok, _platform_tree_ok

    checks["platform_lock"] = _platform_lock_ok()
    checks["platform_tree"] = _platform_tree_ok()
    checks["note"] = (
        "local-process is the only execution backend: git, the agent CLI and the "
        "platform layer (lock/identity/tree) are prerequisites; gh is only required "
        "for GitHub publication."
    )
    ready = bool(checks["git"] and checks["platform_lock"] and checks["platform_tree"])
    resolution = _host_resolution()
    return {
        "version": VERSION,
        "project_id": None,
        "backend": "local-process",
        "checks": checks,
        "ready": ready,
        "resolution": resolution.to_dict() if resolution else None,
        # No project/profile is bound here, so there is no launch environment to
        # measure the version in — say so instead of leaving it silently null.
        "resolution_notes": (list(resolution.notes) if resolution else [])
        + ["version not probed: no project profile is bound"],
        # v0.6 (M3) owns these two fields; v0.5 keeps the conservative values.
        "native_resume": False,
        "resume_mode": "workspace",
        "network": "host (local-process: not restricted by Zoe)",
        "adapters": _agents.catalog(),
        "verdict": (
            "host basics OK"
            if ready
            else "git and the platform layer (lock/identity/tree) are required"
        ),
    }


def _host_resolution():
    """Resolve the default agent CLI for the host-level doctor (§6.8).

    No project is registered here, so there is no profile to bind: the version is
    resolved for the default CodeBuddy binary only, and the result is advisory.
    """
    from src import _discovery

    try:
        return _discovery.resolve_cli("codebuddy")
    except Error:
        return None


def _which(name: str):
    import shutil
    return bool(shutil.which(name))


def _process_identity_available() -> bool:
    try:
        return bool(runtime.process_identity(os.getpid()))
    except (OSError, Exception):
        return False


def run_demo(home: str) -> dict:
    """Run an isolated, simulated pipeline using the test doubles.

    Never invokes a real agent, never writes to a remote, and ignores the
    user's real ZOE_HOME by using a fresh temporary directory.
    """
    from src.demo import simulate

    return simulate()


def capabilities() -> dict:
    """Registry capability listing, per ``(adapter_id, backend)``."""
    return {
        "api_version": 2,
        "version": VERSION,
        "protocol_revision": _agents.registry.PROTOCOL_REVISION,
        **_agents.capability_summary(),
        # F4: the same names `runtime-requirements.json` may demand through
        # `required_capabilities`, taken from the single supported set.
        "features": sorted(_requirements.SUPPORTED_CAPABILITIES),
        # 两个不同的事实，两个字段（§10.1）：实例常量与口径，而不是某个项目的阈值。
        # 计数是全局的，限额来自派发项目自身的配置；跨项目的非对称性是现状行为。
        "concurrency": {
            "max_supported": MAX_ACTIVE_TASKS,
            "default": 1,
            "count_scope": "global",
            "limit_source": "project_config",
            "note": (
                "active runs are counted across all projects under one ZOE_HOME; "
                "the limit applied is the dispatching project's own max_active_tasks"
            ),
        },
        # 观测能力面（v2 §6）：说清"本版真的支持哪些观测行为/语义版本"。
        # 它不是命令清单——参数存在不等于行为实现（`watch --project` 就是反例）。
        "observation": {
            "event_filter": ["project", "group", "task"],
            "event_filter_version": 1,
            "cursor_advances_over_scanned": True,
            "unattributed_events_counted": True,
            "logs_follow": 1,
            "logs_cursor": "run:generation:offset",
            "logs_projection": ["auto", "raw", "events"],
            "logs_rotation_detection": ["replaced", "truncated"],
            # v1.5.2：快照改为有界读 + 字节定界，并新增非阻塞增量读（--after）。
            # 三种截断分开报告，宿主不能把"事件被裁"读成"日志丢了"。
            "logs_snapshot": 2,
            "logs_after": 1,
            "logs_window": {
                "default_tail_bytes": _logstream.DEFAULT_TAIL_BYTES,
                "max_tail_bytes": _logstream.MAX_TAIL_BYTES,
                "byte_aligned": True,
                "complete_records_only": True,
                "blank_lines_skipped": True,
                "max_events": _logstream.MAX_SNAPSHOT_EVENTS,
            },
            "logs_truncation": ["window", "events", "content"],
            "progress": 1,
            "progress_fields": [
                "observed_at", "last_output_at", "quiet_seconds", "process_state", "log_generation",
            ],
            "process_states": ["alive", "exited", "unknown"],
            "observations_stale": 1,
            "checks_preflight": 1,
            "checks_preflight_statuses": ["ok", "shell", "deferred", "unresolved", "invalid"],
            # v1.6：跨任务事实聚合（`task digest`）、任务记录投影（`task list --fields`）、
            # 派发前基线校验（`BASE_DRIFT`）。digest 只聚合事实、不产结论——这一行说的是
            # "宿主可以少发 N 条命令"，不是"可以少看证据"。
            "task_digest": 1,
            "task_digest_fields": ["state", "phase", "elapsed", "quiet", "bytes", "content"],
            "task_list_fields": 1,
            "base_drift_preflight": 1,
            # v1.6：进入 REVIEW_READY 时写下的并行基线事实（`concurrent_base`）。
            # 与 `base_drift_preflight` 是两个方向：那个回答"基线还在不在历史里"，
            # 这个回答"基线之后目标分支做了什么"。事实，不是门禁。
            "base_drift_review": 1,
            "base_drift_review_fields": [
                "kind", "behind_by", "overlapping_paths", "reverts_merged_work",
            ],
        },
        # v1.7：第二层验收（逐条 AC 判定 + 覆盖账本）。单独一块，不塞进
        # `observation`：那一块讲的是"看得见什么"，这一块讲的是"谁判过"。
        "acceptance": {
            "task_review": 1,
            "item_statuses": ["PASS", "FAIL", "NOT_VERIFIED"],
            "evidence_kinds": ["check", "evidence", "doc", "decision"],
            "review_modes": list(_contracts.REVIEW_MODES),
            "judge_kinds": list(_contracts.JUDGE_KINDS),
            # 宿主据此判断能不能用 v3 字段：旧运行器会静默忽略它们，
            # 静默忽略比拒绝更危险（作者会以为已经声明了门禁）。
            "spec_contract_version": _contracts.SPEC_CONTRACT_VERSION,
            "spec_contract_versions_accepted": list(_contracts.SPEC_ACCEPTED_VERSIONS),
            "progress_refs_gate": 1,
            "coverage": 1,
            # v1.7：`depends_on` 在 start 时用 ledger 同源的 covered 判定阻断。
            "dependencies": 1,
            "dependency_markers_field": "depends_on",
        },
        "adapters": _agents.catalog(),
        # v1.3 (TGD §5.6): capability and config digest move in the same response
        # and the same release, so "the skill asked for it" and "the runtime can
        # do it" cannot drift apart.
        "task_groups": {
            "record_version": _groups.RECORD_VERSION,
            "plan_schema_version": _groups.PLAN_SCHEMA_VERSION,
            "delivery_modes": list(_groups.SUPPORTED_DELIVERY_MODES),
            "dependency_graph": True,
            "serial_execution": True,
            "per_group_runner": True,
            "lease_handshake": True,
            "drain_pause": True,
            "budget": ["max_starts", "max_wall_seconds", "max_group_repairs"],
            "group_checks": True,
            "acceptance": "group",
            # v1.4：设计一致性审查由**宿主**在独立上下文执行并提交，Runtime 只做
            # 契约校验与门禁。`default` 说清缺省语义：计划不声明就不参与验收。
            "design_review": {
                "contract": "schemas/group-review.v1.json",
                "statuses": ["PASS", "FAIL", "NOT_VERIFIED"],
                "verdicts": ["PASS", "FAIL", "NOT_VERIFIED"],
                "required_by": "plan.review.required",
                "default": "not_required",
                "executed_by": "host",
                "gates": ["REVIEW_READY", "group accept"],
            },
            # v1.4：有界修复。一次执行只对应一条 FAIL finding，受
            # `budget.max_group_repairs` 约束，不开放动态任务图。
            "group_repair": {
                "bounded_by": "budget.max_group_repairs",
                "default_max": 0,
                "one_finding_per_run": True,
                "requires_fail_finding": True,
            },
            "not_supported": [
                # Each entry is a product decision, not an unimplemented branch.
                "group_pull_request_delivery",
                "group_parallel_members",
                "group_auto_retry",
                "unbounded_dynamic_task_graph",
            ],
        },
        "hosts": [
            {"host": "workbuddy", "package": ".", "real_e2e": "NOT_RUN"},
            {"host": "openclaw", "package": ".build/openclaw", "real_e2e": "NOT_RUN"},
        ],
        "not_supported": [
            "native_session_resume", "parallel_writers",
            # v1.3 reverses the v1.2 refusal: a static member dependency graph is
            # now scheduled, so `automatic_dag_scheduling` is gone from this list.
            # What stays refused is narrower and must be named narrowly: merging
            # into the *target* branch on its own. Controlled integration into
            # `zoe/group-<id>` is a different, reported operation (TGD §2.2-9).
            "automatic_merge_to_target_branch",
            # v0.5.0: containers are out of the product scope entirely, so the
            # honest entry is "no container execution", not "no socket mount".
            "runtime_plugin_loading", "container_execution",
            # v1.2 §16.7 的四条"本版不做"，逐条列出而不是含糊地说"支持路由"。
            # 第四条目是"是否只用了指定模型"这个问题的准确答案：Zoe 只能做到
            # "请求指定 + 事后作废"，网关级事前强制不在它的能力范围内。
            "model_quality_scoring",
            "internal_routing_control",
            "performance_based_exclusion",
            "strict_model_enforcement_at_gateway",
        ],
    }


def _agent_command(store, args) -> dict:
    c = store.config(args.project)
    if args.action == "list":
        return {"api_version": 2, "project_id": c["project_id"],
                "default_agent_profile": c["default_agent_profile"],
                "profiles": _profiles.profile_views(c)}
    return _profiles.probe_profile(c, args.profile)


def _route_command(store, args) -> dict:
    """``route plan`` / ``route stats`` (§15.1, §15.3)."""
    c = store.config(args.project)
    if args.action == "plan":
        request = read(args.request_file) if getattr(args, "request_file", None) else {}
        if getattr(args, "profile", None):
            request = {**request, "agent_profile": args.profile}
        # Preview only: no task, no decision file, no event. 预览与真实决策调用
        # 同一个 `decide()`，所以"预览说会选它"和"实际选了它"不会分叉。
        plan = _routing.plan_route(
            c,
            request,
            trigger="plan",
            probe=bool(getattr(args, "probe", True)),
        )
        return {
            "api_version": 2,
            "project_id": c["project_id"],
            "mode": plan["mode"],
            "decision": plan["decision"],
            "reason": plan["reason"],
            "task_type": plan["task_type"],
            "task_type_source": plan["task_type_source"],
            "selected_profile": plan["selected_profile"],
            "requirements": plan["requirements"],
            "candidates": plan["candidates"],
            "layers": plan["layers"],
            "order": plan["order"],
            "fallback_used": plan.get("fallback_used"),
            "routing_digest": plan["routing_digest"],
            "policy_revision": plan["policy_revision"],
            "snapshot_digest": plan["snapshot_digest"],
            "snapshot_budget": plan["snapshot"]["budget"],
            "options": plan.get("options"),
            "gaps": plan.get("gap_codes"),
            "wrote": False,
        }
    if args.action == "stats":
        require(args.window >= 1, "INPUT_INCOMPLETE", "--window must be at least 1 day")
        record = _routing.performance_record(store, c["project_id"], window_days=args.window)
        path = store.home / "route" / c["project_id"] / f"stats-{int(record['as_of'])}.json"
        if args.write:
            from src._util import atomic

            atomic(path, record)
        record.update(api_version=2, wrote=bool(args.write),
                      stats_path=str(path) if args.write else None)
        return record
    raise Error("INPUT_INCOMPLETE", f"Unknown route action {args.action!r}")


def _task_events(store, args) -> dict:
    events, cursor, has_more = store.events(args.id, args.after, args.limit)
    return {"api_version": 2, "task_id": args.id, "events": events,
            "next_cursor": cursor, "has_more": has_more}


def _validate_logs_args(args) -> None:
    """Refuse option combinations instead of silently ignoring one of them.

    ``--after`` and ``--follow`` are two *different* cursors (a log cursor and a
    stream position) and only one of them can drive a call: honouring one while
    ignoring the other would show "history" to a host that believes it is
    watching live output.
    """
    after = getattr(args, "after", None)
    if after:
        if getattr(args, "follow", False):
            raise Error(
                "INPUT_INCOMPLETE",
                "--after is for snapshots and cannot be combined with --follow; "
                "use --after for a snapshot or --cursor for following",
                details={"field": "after"},
            )
        if _logstream.parse_cursor(after) is None:
            raise Error(
                "INPUT_INCOMPLETE",
                "after must look like run:generation:offset",
                details={"field": "after", "reason": "expected shape run:<generation>:<offset>"},
            )
    tail = getattr(args, "tail_bytes", None)
    if tail is not None:
        require(
            1 <= int(tail) <= _logstream.MAX_TAIL_BYTES,
            "INPUT_INCOMPLETE",
            "tail-bytes must be 1..1048576",
        )


def _log_generation(store, t: dict, run_id: str) -> int:
    """Generation of the log *right now*, read from ``progress.json``.

    The old snapshot hard-coded generation ``1``; after a rotation that cursor
    addressed a file that no longer exists. ``progress.json`` already tracks the
    generation counter, so it is read rather than guessed — and a missing or
    unreadable file still falls back to ``1`` rather than inventing a value.
    """
    try:
        data = read(_progress.path(store, t, run_id))
    except (OSError, ValueError):
        return 1
    if not isinstance(data, dict):
        return 1
    try:
        return max(1, int(data.get("log_generation") or 1))
    except (TypeError, ValueError):
        return 1


def _task_logs(store, args) -> dict:
    """One-shot bounded snapshot. This is a **snapshot**, not a stream (v2 §3-3).

    Bounded in three independent ways (v2.2 §S8-D-3), each reported separately:

    * the byte window — ``stat`` + ``seek``/``read``, never ``read_bytes()``;
    * complete records only — framing happens on bytes, decoding after, so a
      window that starts inside a multi-byte character cannot corrupt the first
      record and a half-written line is never projected;
    * the event count — the *newest* ``MAX_SNAPSHOT_EVENTS`` are kept.
    """
    t = store.task(args.id)
    run_id = args.run or t.get("current_run_id")
    require(run_id, "INPUT_INCOMPLETE", "No run recorded for this task")
    r = store.get_run(run_id)
    require(r, "RUN_NOT_FOUND", run_id)
    window = getattr(args, "tail_bytes", None)
    window = _logstream.DEFAULT_TAIL_BYTES if window is None else int(window)
    require(
        1 <= window <= _logstream.MAX_TAIL_BYTES,
        "INPUT_INCOMPLETE",
        "tail-bytes must be 1..1048576",
    )
    directory = store.path(t) / "runs" / run_id
    path = directory / (args.log + ".log")
    require(path.is_file(), "INPUT_INCOMPLETE", f"No log named {args.log!r} in run {run_id}")

    size = path.stat().st_size
    generation = _log_generation(store, t, run_id)
    after = _logstream.parse_cursor(getattr(args, "after", None))
    rotated = False
    if after is not None:
        if after[0] != run_id:
            raise Error(
                "PROJECT_MISMATCH",
                f"cursor belongs to run {after[0]!r}, not {run_id!r}",
                details={"cursor_run": after[0], "run_id": run_id},
            )
        if after[2] > size:
            # Truncated or replaced: normal for a live log, so not an error. Do
            # not re-read the whole file — hand back an empty window plus the
            # position to restart from.
            rotated = True
            start = 0
        else:
            start = _logstream.align_offset(path, after[2], size)
    else:
        start = _logstream.window_start(path, size, window)

    if rotated:
        data = b""
    elif after is not None:
        # ``--after`` must make progress even when the next record is longer than
        # the window (see ``_logstream.one_record_span``).
        length = _logstream.one_record_span(path, start, size, window)
        data = _logstream.read_window(path, start, length)
    else:
        data = _logstream.read_window(path, start, min(window, size - start))
    projection = getattr(args, "projection", "auto")
    events, complete_end = _logstream.project_complete_lines(data, start, projection)
    cursor = _logstream.format_cursor(run_id, generation, complete_end)
    # ``text``/``events`` 同时为空有四种完全不同的原因，响应必须自己说清是哪一种：
    # 否则宿主只能靠"tail_start_offset=0 且 bytes=518018 却 text='' "这种反推去猜，
    # 而最容易得出的结论是"读取有 bug"——这个字段就是为了消灭那个误判。
    empty_reason = None
    if not data:
        if rotated:
            empty_reason = "rotated"
        elif after is not None:
            # 增量读：游标之后没有新的完整记录（已追平）。这是心跳，不是异常。
            empty_reason = "no_new_records"
        else:
            # 尾部窗口整段落在同一条记录里（单条 > 窗口），窗口内一个换行都没有。
            empty_reason = "no_newline_in_window"
    elif not events:
        # 只有半行/空行：窗口非空，但没有一条完整记录可投影。
        empty_reason = "no_complete_line"
    payload = {
        "api_version": 2,
        "task_id": args.id,
        "run_id": run_id,
        "log": args.log,
        "bytes": size,
        # Byte-window truncation: there are older records to the left.
        "truncated": start > 0,
        # 空窗口的原因（null 表示确实有内容）。四种取值见 _empty_reason 的注释。
        "empty_reason": empty_reason,
        "text": redact_secrets(data.decode("utf-8", "replace")),
        "redaction": "best-effort only; raw logs may still contain sensitive data",
        "snapshot": True,
        "follow_hint": (
            "use --after <cursor> for the next non-blocking page, or --follow (--cursor to "
            "resume) to stream"
        ),
        # Resuming from here continues after the last **complete** record.
        "cursor": cursor,
        "next_cursor": cursor,
        "tail_start_offset": start,
        "events": events[-_logstream.MAX_SNAPSHOT_EVENTS:],
        "events_truncated": len(events) > _logstream.MAX_SNAPSHOT_EVENTS,
        "projection": projection,
        "run": {key: r.get(key) for key in
                ("adapter_id", "adapter_backend", "profile_id", "expected_version",
                 "capability", "result_schema", "session_id", "status")},
    }
    if after is not None:
        # "More" means "there is usable content after what I returned". A page
        # that could not include one complete record would otherwise keep
        # answering has_more=true with the same cursor — an infinite page loop.
        payload["has_more"] = complete_end > start and complete_end < size
    if rotated:
        reset = _logstream.format_cursor(run_id, generation, 0)
        payload.update(
            rotated=True,
            reset_cursor=reset,
            cursor=reset,
            next_cursor=reset,
            tail_start_offset=0,
            truncated=False,
            has_more=False,
        )
    return payload


def _logs_follow(store, args) -> None:
    """Stream one run's log as JSON lines (v2 §3-3).

    Semantics frozen for this version: an explicit ``--run`` (or a ``--cursor``)
    pins the run; without one the resolved run id is printed once and then
    pinned, so a host is never silently switched to a newer run. Output happens
    only when a **complete** line appeared — silence is not filled with
    synthetic activity.
    """
    t = store.task(args.id)
    cursor = _logstream.parse_cursor(getattr(args, "cursor", None))
    if cursor is not None:
        run_id, generation, offset = cursor
    else:
        run_id = args.run or t.get("current_run_id")
        require(run_id, "INPUT_INCOMPLETE", "No run recorded for this task")
        generation, offset = _log_generation(store, t, run_id), None
    r = store.get_run(run_id)
    require(r, "RUN_NOT_FOUND", run_id)
    path = store.path(t) / "runs" / run_id / (args.log + ".log")
    require(path.is_file(), "INPUT_INCOMPLETE", f"No log named {args.log!r} in run {run_id}")
    if offset is None:
        size = path.stat().st_size
        # Only an *explicit* --tail-bytes asks for a look-back. ``None`` keeps the
        # published meaning ("what happens next"): start at the current end.
        window = getattr(args, "tail_bytes", None)
        offset = _logstream.window_start(path, size, int(window)) if window else size
    stream = _logstream.LogFollower(path, offset=offset, generation=generation)
    meta = {
        "kind": "meta",
        "run_id": run_id,
        "log": args.log,
        "projection": args.projection,
        "cursor": stream.cursor(run_id)["text"],
        "redaction": "best-effort only; raw logs may still contain sensitive data",
        "note": "emitted only when a complete record appeared; silence is not a failure",
    }
    print(json.dumps(meta, ensure_ascii=False, sort_keys=True), flush=True)

    # ``0`` means "no per-invocation cap"; a positive value is a byte budget.
    cap = int(getattr(args, "max_bytes", 0) or 0)
    remaining = cap if cap > 0 else None
    deadline = time.time() + float(getattr(args, "timeout", 60) or 60)
    idle = 0
    status_checked = 0.0
    while time.time() < deadline:
        records = stream.poll()
        for item in records:
            if item["kind"] == "line":
                for event in _logstream.project_line(item["text"], args.projection):
                    if item.get("truncated"):
                        event["truncated"] = True
                    line = json.dumps(event, ensure_ascii=False, sort_keys=True)
                    print(line, flush=True)
                    if remaining is not None:
                        remaining -= len(line.encode("utf-8")) + 1
            else:
                payload = {**item, "run_id": run_id, "cursor": stream.cursor(run_id)["text"]}
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
                if remaining is not None:
                    remaining -= 96
            if remaining is not None and remaining < 0:
                # 触达输出上限：给出原因与续读游标，不静默截断。
                print(
                    json.dumps(
                        {
                            "kind": "limit",
                            "reason": "max_bytes",
                            "cursor": stream.cursor(run_id)["text"],
                            "hint": "resume with --cursor to continue",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    flush=True,
                )
                return
        if records:
            idle = 0
            continue
        idle += 1
        # A finished run has nothing more to say: drain and leave instead of
        # waiting out the whole timeout.
        if idle % 5 == 0 and time.time() - status_checked > 1.0:
            status_checked = time.time()
            current = store.get_run(run_id) or {}
            finished = current.get("status") in ("exited", "failed", "unknown") or current.get(
                "launch_stage"
            ) == "finished"
            if finished and idle >= 10:
                print(
                    json.dumps(
                        {
                            "kind": "run_finished",
                            "run_id": run_id,
                            "cursor": stream.cursor(run_id)["text"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    flush=True,
                )
                return
        time.sleep(0.2)


def _exit_code(args, error: Error) -> int:
    if not _granular_exit_codes(args):
        return EXIT_ERROR
    return _EXIT_CODES.get(getattr(error, "code", ""), 1)


def _orchestration_command(store, args) -> dict:
    if args.action == "show":
        return _orch.show(store, args.project)
    if args.action == "resume":
        return _orch.resume(store, args.project)
    if args.action == "claim":
        return _orch.claim(store, args.project, args.host, args.force, getattr(args, "session_id", None))
    if args.action == "release":
        return _orch.release(store, args.project, args.host)
    return _orch.update(store, read(args.request_file))


def _project_task_rows(rows: list[dict], fields) -> list[dict]:
    """``task list --fields`` 的投影（v1.6）。

    字段名拼错必须报错：静默返回 ``null`` 会让宿主把"我写错了字段名"读成"这个任务没有
    这个信息"，而那正好是投影类接口最常见的误判。字段集合按**本次返回的记录**求并集，
    不维护白名单——顶层键随版本增长，硬编码白名单会立刻过期。
    """
    names = [part.strip() for part in str(fields or "").split(",") if part.strip()]
    if not names or not rows:
        return rows
    available: set[str] = set()
    for row in rows:
        available.update(row)
    unknown = [name for name in names if name not in available]
    if unknown:
        raise Error(
            "INPUT_INCOMPLETE",
            "unknown task field(s): " + ", ".join(unknown),
            details={"field": "fields", "unknown": unknown, "available": sorted(available)},
        )
    return [{name: row.get(name) for name in names} for row in rows]


def _event_filters(args) -> dict:
    """Filter arguments shared by ``watch`` and ``task events``."""
    return {
        "project_id": getattr(args, "project", None),
        "group_id": getattr(args, "group", None),
        "task_id": getattr(args, "task_filter", None) or getattr(args, "task_id", None),
    }


def _validate_event_filters(store, filters: dict) -> dict:
    """Refuse filter combinations that cannot be explained by the records.

    A filter that silently returns nothing is worse than an error: the host
    reads "no events" as "nothing happened" (v2 §3-1). Ownership is checked up
    front, so an empty page always means "no matching events".
    """
    project_id = filters.get("project_id")
    group_id = filters.get("group_id")
    task_id = filters.get("task_id")
    if group_id is not None:
        group = store.group(group_id)
        if project_id is not None and group.get("project_id") != project_id:
            raise Error(
                "PROJECT_MISMATCH",
                f"group {group_id} belongs to {group.get('project_id')!r}, not {project_id!r}",
            )
        project_id = project_id or group.get("project_id")
    if task_id is not None:
        task = store.task(task_id)
        if project_id is not None and task.get("project_id") != project_id:
            raise Error(
                "PROJECT_MISMATCH",
                f"task {task_id} belongs to {task.get('project_id')!r}, not {project_id!r}",
            )
        if group_id is not None and task.get("group_id") != group_id:
            raise Error(
                "INPUT_INCOMPLETE",
                f"task {task_id} is not a member of group {group_id}",
            )
        project_id = project_id or task.get("project_id")
    return {"project_id": project_id, "group_id": group_id, "task_id": task_id}


def _watch_once(store, after: str, max_events: int, max_pages: int, filters=None) -> dict:
    """一次有界事件查询（04 §4.3）：游标 + epoch + 分页上限 + 过滤。

    游标按**已扫描**范围推进（``store.events_filtered`` 的约定）：即使这一页
    没有匹配事件，``next_cursor`` 也必须前进，否则宿主会在无关事件上无限重扫。
    """
    epoch = store.epoch()
    filters = filters or {}
    if ":" in after:
        cur_epoch, cur_id = after.split(":", 1)
    else:
        cur_epoch, cur_id = epoch, after
    if cur_epoch != epoch:
        return {
            "epoch": epoch,
            "cursor_status": "cursor_expired",
            "events": [],
            "next_cursor": f"{epoch}:0",
            "has_more": False,
            "scanned": 0,
            "unattributed": 0,
            "filters": filters,
        }
    start = int(cur_id or 0)
    max_id = store.max_event_id(None)
    # Bounded pagination: at most max_pages pulls of max_events each (04 §4.3).
    pages = max(1, int(max_pages))
    all_events = []
    next_id = start
    has_more = False
    unattributed = 0
    filtered = any(value is not None for value in filters.values())
    for _ in range(pages):
        if filtered:
            events, next_id, has_more, skipped = store.events_filtered(
                next_id, max_events, **filters
            )
            unattributed += skipped
        else:
            # 无过滤时保持原路径：读的是"全部事件"，语义与 v1.4 完全一致。
            events, next_id, has_more = store.events(None, next_id, max_events)
        all_events.extend(events)
        if not has_more or not events:
            break
    cursor_status = "ok" if has_more or all_events else "heartbeat"
    return {
        "epoch": epoch,
        "cursor_status": cursor_status,
        "events": all_events,
        "next_cursor": f"{epoch}:{next_id}",
        "has_more": has_more,
        "scanned": max_id,
        # 扫描过但没有项目归属的事件数：它们不会被猜进任何项目。
        "unattributed": unattributed,
        "filters": filters,
    }


def _watch(store, args) -> dict:
    """Bounded event query with epoch cursor (04 §4.3)."""
    filters = _validate_event_filters(store, _event_filters(args))
    return _watch_once(
        store, args.after or "0", args.max_events, args.max_pages, filters
    )


def _print_event(event) -> None:
    print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)


def _watch_follow(store, args, filters=None) -> None:
    """流式事件：一行一个 JSON，直到 ``--timeout`` 到期。

    宿主用 ``readline`` 就能拿到增量进度，等价于 ``tmux pipe-pane`` 但带游标，
    不需要长期轮询，也不需要 MCP/HTTP 服务。无匹配事件时**不输出任何事件行**：
    空返回是心跳，不是失败，也不是"没有进展"的证据。

    游标契约（v1.5.1 起固定）：首行与末行是 ``kind="meta"`` 的游标行——
    ``follow_start`` 给起点，``follow_end`` 给下一次 ``--after`` 可直接使用的
    ``next_cursor``。此前 ``--follow`` 只在 ``cursor_expired`` 时给游标，宿主想
    续读只能自己拼 ``epoch:id``；拼错就会重放或跳事件。事件行本身不带游标语义。
    终止判断仍应读 ``task status``：这里只提供"看到什么"，不提供"结束了"。
    """
    filters = filters if filters is not None else _validate_event_filters(
        store, _event_filters(args)
    )
    epoch = store.epoch()
    deadline = time.time() + float(args.timeout)
    # ``task events --after`` 传的是整数，``watch --after`` 传的是 ``epoch:id``：
    # 统一成字符串再判断，否则整数上做 ``in`` 会直接 TypeError。
    cursor = str(args.after) if args.after else f"{epoch}:0"
    if ":" not in cursor:
        # 裸 event_id 也接受，但对外通告的游标必须是完整形式：宿主会直接把它
        # 传给下一轮 --after，两种形态混着用最容易写成"看起来对"的错游标。
        cursor = f"{epoch}:{cursor}"
    _print_event(
        {
            "kind": "meta",
            "event": "follow_start",
            "epoch": epoch,
            "cursor": cursor,
            "timeout": float(args.timeout),
            "note": "events follow; the last meta line carries next_cursor",
        }
    )
    while time.time() < deadline:
        payload = _watch_once(store, cursor, args.max_events, args.max_pages, filters)
        if payload["cursor_status"] == "cursor_expired":
            _print_event(
                {
                    "kind": "meta",
                    "event": "follow_end",
                    "cursor_status": "cursor_expired",
                    "next_cursor": payload["next_cursor"],
                    "epoch": payload["epoch"],
                    "reason": "cursor_expired",
                }
            )
            return
        for event in payload["events"]:
            _print_event(event)
        cursor = payload["next_cursor"]
        if not payload["events"]:
            time.sleep(max(0.05, min(0.5, deadline - time.time())))
    _print_event(
        {
            "kind": "meta",
            "event": "follow_end",
            "cursor_status": "ok",
            "next_cursor": cursor,
            "epoch": epoch,
            "reason": "timeout",
        }
    )


def _group_command(store, args):
    """``zoe group <action>`` — one dispatcher, two record generations."""
    from src import _group_runner, _integration, _review

    action = args.action
    if action == "create":
        output(_groups.create(store, read(args.request_file)))
        return EXIT_OK
    if action == "plan":
        if args.project:
            # Preview form: validate a plan request, write nothing (TGD §11.1).
            require(
                args.request_file,
                "INPUT_INCOMPLETE",
                "`group plan --project P` requires --request-file",
            )
            output(_groups.preview(store, read(args.request_file)))
            return EXIT_OK
        require(args.id, "INPUT_INCOMPLETE", "`group plan` requires a group id")
        group = store.group(args.id)
        if _groups.is_v3(group):
            output(_group_runner.tick_snapshot(store, args.id))
        else:
            output(_group_plan_legacy(store, args.id))
        return EXIT_OK
    if action == "status":
        output(_groups.status(store, args.id, refresh=bool(args.refresh)))
        return EXIT_OK
    if action == "start":
        output(_group_runner.begin(store, args.id, read(args.request_file)))
        return EXIT_OK
    if action == "dispatch":
        output(_group_dispatch(store, args.id, args.item, read(args.request_file)))
        return EXIT_OK
    if action == "pause":
        output(_group_runner.pause(store, args.id, read(args.request_file), stop=bool(args.stop)))
        return EXIT_OK
    if action == "resume":
        output(_group_runner.resume(store, args.id, read(args.request_file)))
        return EXIT_OK
    if action == "cancel":
        request = read(args.request_file)
        if args.external:
            output(_integration.close_external(store, args.id, request))
        else:
            output(
                _group_runner.cancel(store, args.id, request, stop=bool(args.stop))
            )
        return EXIT_OK
    if action == "accept":
        output(_integration.accept(store, args.id, read(args.request_file)))
        return EXIT_OK
    if action == "review":
        if args.request_file:
            output(_review.submit(store, args.id, read(args.request_file)))
            return EXIT_OK
        # 只读形态：宿主需要知道"还差哪些 AC"，而不是只有一句 NOT_RUN。
        group = store.group(args.id)
        _groups.require_v3(group, "review")
        body = _review.summary(store, group)
        # `state` 是**审查**状态；组状态单独给一个键，避免两个含义撞在一起。
        body.update(
            {
                "api_version": 2,
                "group_id": args.id,
                "group_state": group["state"],
                "required_acceptance_ids": _review.required_acceptance_ids(group),
                "next_actions": _review.next_actions(store, group, _review.current(store, group)),
            }
        )
        output(body)
        return EXIT_OK
    if action == "repair":
        output(_group_runner.repair(store, args.id, read(args.request_file)))
        return EXIT_OK
    if action == "report":
        group = store.group(args.id)
        snapshot = _integration.report(store, args.id)
        text = _integration.render_report(snapshot)
        fmt = args.format or ("json" if args.json else "markdown")
        if args.write:
            written = _integration.persist_report(store, group, snapshot, text)
            output({**snapshot, **written} if fmt == "json" else written)
        elif fmt == "json":
            output(snapshot)
        else:
            print(text)
        return EXIT_OK
    raise Error("INPUT_INCOMPLETE", f"Unknown group action {action}")


def _group_dispatch(store, group_id, item_id, req) -> dict:
    """Single-step dispatch of one group item (05 §2.2).

    A v3 group dispatches through the runner's own function so that
    ``group dispatch`` is the *same* path as the tick: admission, budget,
    serialisation and the frozen spec snapshot cannot diverge between "a human
    pressed the button" and "the tick pressed the button" (TGD §7.6).
    """
    from src import _group_runner

    group = store.group(group_id)
    if _groups.is_v3(group):
        req = dict(req)
        # Admission is consulted by `_pipeline.start`; call it here first so the
        # refusal carries the group/member kind instead of a generic slot error.
        _group_runner.admission(
            store,
            {"group_id": group_id, "item_id": item_id, "task_id": None},
        )
        result = _group_runner.dispatch(store, group, item_id)
        return {
            "api_version": 2,
            "operation_id": req.get("request_id"),
            "status": "succeeded",
            "group_id": group_id,
            "item_id": item_id,
            "task_id": result["created"]["task_id"],
            "state": result["created"]["state"],
            "started": result["started"].get("status"),
            # 只报"已受理"，不报"已推进"（TGD §7.1）。
            "completed": False,
        }
    with store.lock():
        item = next(
            (i for i in group.get("ordered_items") or [] if i["item_id"] == item_id), None
        )
        require(item, "INPUT_INCOMPLETE", "Unknown group item")
        require(item.get("task_id") is None, "STATE_CONFLICT", "Group item already bound")
        req = dict(req)
        req.setdefault("group_id", group_id)
        req.setdefault("item_id", item_id)
        return create(store, req)


def _group_plan_legacy(store, group_id) -> dict:
    """Read-only admission view for a legacy ordered group (05 §2.1)."""
    group = store.group(group_id)
    items = group.get("ordered_items") or []
    try:
        project_config = store.config(group["project_id"])
    except Error:
        project_config = {}
    eligible = []
    dispatchable = []
    blocked = []
    for item in items:
        if item.get("task_id"):
            blocked.append({"item_id": item["item_id"], "code": "ALREADY_BOUND"})
            continue
        try:
            config = store.config(group["project_id"])
            binding = _profiles.resolve_binding(config)
            capability = binding.capability
            require_real_e2e = bool(config.get("require_real_e2e"))
        except Error:
            capability = "UNVERIFIED"
            require_real_e2e = True
        # 与 start 同一策略：默认只要求"能跑"，严格项目才要求真实 e2e。
        if capability == "VERIFIED" or not require_real_e2e:
            eligible.append(
                {"item_id": item["item_id"], "profile": binding.profile_id, "capability": capability}
            )
            dispatchable.append({"item_id": item["item_id"]})
        else:
            blocked.append({"item_id": item["item_id"], "code": "QUALIFICATION_REQUIRED"})
    return {
        "group_id": group_id,
        "record_version": group.get("record_version", 1),
        "eligible": eligible,
        "dispatchable_now": dispatchable,
        "blocked": blocked,
        "conflicts": [],
        # 与 `start` 共用同一统计；它是快照，不是准入（§10.2）。
        "slots": slots(store, project_config),
        "note": "observation only; dispatch re-checks every condition",
    }


def _task_answer(store, task_id, req) -> dict:
    """Save an answer to a pending agent question (07 §4.1).

    Idempotent by ``request_id`` like every other mutation, and owner-gated
    (§10.10). It only clears the block — it never resumes the run itself, so the
    host explicitly decides between ``task resume`` (clarification) and
    ``task revise`` (the answer changed the acceptance criteria).
    """
    with store.lock():
        key, prior = store.operation(req, "answer", task_id)
        if prior:
            return prior
        try:
            t = store.task(task_id)
            authorize(t["config"], req, "execute")
            _orch.require_owner(store, t["project_id"], req)
            require(
                t.get("block_code") == "INPUT_REQUIRED",
                "STATE_CONFLICT",
                "Task is not waiting for input",
            )
            require(
                t.get("row_version") == req.get("expected_version"),
                "STATE_CONFLICT",
                "Refresh expected_version",
            )
            question_id = req.get("question_id")
            require(question_id, "INPUT_INCOMPLETE", "question_id required")
            if req.get("answered_by") == "spec":
                require(
                    req.get("answer_ref"), "INPUT_INCOMPLETE", "spec answers require a reference"
                )
            answered = None
            for question in t.get("pending_questions") or []:
                if question.get("question_id") == question_id:
                    question["answer"] = req.get("answer")
                    question["answered_by"] = req.get("answered_by")
                    question["answer_ref"] = req.get("answer_ref")
                    answered = question
            require(answered is not None, "INPUT_INCOMPLETE", "Unknown question_id")
            if t.get("phase") == "execute":
                # Legacy records written by v0.4.0 (see `run_pipeline`): no stage
                # consumes "execute", so a resume would silently do nothing.
                t["phase"] = "implement"
            t.update(state="READY", block_code=None, needs_input=False)
            result = {
                "status": "succeeded",
                "task_id": task_id,
                "question_id": question_id,
                "state": "READY",
                "phase": t["phase"],
                "row_version": t["row_version"] + 1,
            }
            store.save(
                t,
                "question_answered",
                operation=(key, result),
                actor_kind="host",
                request_id=key,
            )
            return result
        except Exception as e:
            store.finish(
                key,
                {
                    "status": "failed",
                    "error": {"code": getattr(e, "code", "INPUT_INCOMPLETE"), "message": str(e)},
                },
            )
            raise


def _task_recover(store, task_id, run_id, confirm_stop) -> dict:
    """Controlled recovery of an unknown execution domain (05 §2.4).

    ``--run`` 可省略：缺省取任务的 ``current_run_id``。这不是"猜一个 run"——
    它就是 ``task status`` 显示的那个 run，也是唯一能让人把"我看的是哪一次"
    写进命令的地方。显式给了 ``--run`` 时以显式值为准，并在响应的
    ``run_source`` 里写清楚来源。
    """
    require(confirm_stop, "INPUT_INCOMPLETE", "--confirm-stop required for recovery")
    with store.lock():
        t = store.task(task_id)
        source = "explicit"
        if not run_id:
            run_id = t.get("current_run_id")
            source = "current_run"
            require(
                run_id,
                "INPUT_INCOMPLETE",
                "No run recorded for this task; pass --run explicitly",
            )
        r = store.get_run(run_id)
        require(r, "RUN_NOT_FOUND", run_id)
        steps = []
        start_token = r.get("start_token") or r.get("identity")
        supervisor_alive = alive(r.get("pid"), start_token)
        steps.append({"step": "supervisor_identity", "result": "alive" if supervisor_alive else "dead"})
        if supervisor_alive:
            return {
                "run_id": run_id,
                "run_source": source,
                "steps": steps,
                "final_state": "RUNNING",
                "quota_released": False,
            }
        # Stop the tree if present.
        tree_alive_state = None
        if r.get("group_token"):
            from src._platform.base import ProcessHandle
            handle = ProcessHandle(pid=r.get("pid"), start_token=start_token,
                                   group_token=r.get("group_token"), popen=None,
                                   platform=get_platform())
            tree_alive_state = get_platform().tree_alive(handle)
            # 三态，不能折叠成布尔：None 是"无法判定"，既不是"活着"（可以继续
            # 等待）也不是"已死"（可以放行）。旧写法把 None 记成 dead，于是
            # 观测失败反而成了解锁条件。
            steps.append(
                {
                    "step": "process_tree",
                    "result": "unknown"
                    if tree_alive_state is None
                    else ("alive" if tree_alive_state else "dead"),
                }
            )
            if tree_alive_state is None:
                return {
                    "run_id": run_id,
                    "run_source": source,
                    "steps": steps,
                    "final_state": "PROCESS_UNKNOWN",
                    "quota_released": False,
                }
            if tree_alive_state:
                stop = get_platform().stop_tree(handle, timeout=10)
                steps.append({"step": "stop_tree", "result": "stopped", "remaining": stop["remaining"]})
                require(stop["remaining"] == 0, "PROCESS_UNKNOWN", "Tree still running")
        # Writer lock acquirable?
        try:
            with store.lock(f"tasks/{task_id}/writer", False):
                pass
            steps.append({"step": "writer_lock", "result": "acquired"})
        except Error:
            steps.append({"step": "writer_lock", "result": "held"})
            return {
                "run_id": run_id,
                "run_source": source,
                "steps": steps,
                "final_state": "PROCESS_UNKNOWN",
                "quota_released": False,
            }
        r.update(status="exited", interrupted=True, reconciled_at=time.time())
        store.run(r)
        t.update(state="BLOCKED", block_code="RUN_INTERRUPTED")
        store.save(t, "recovered")
        return {
            "run_id": run_id,
            "run_source": source,
            "steps": steps,
            "final_state": "RUN_INTERRUPTED",
            "quota_released": True,
        }


def _attest_execution_view(c, profile_ids) -> list:
    """确认现场必须能看到"确认的是什么"：模型、端点、权限档，而不只是 profile 名。"""
    rows = []
    for pid in profile_ids:
        profile = c["agent_profiles"].get(pid) or {}
        rows.append(
            {
                "profile_id": pid,
                "adapter": profile.get("adapter"),
                "model": profile.get("model"),
                # 别名属于"被确认的内容"：pin_model 接受 alias_confirmed，所以人
                # 在 attest 时就应该看到自己确认的是哪一组等价名。
                "model_aliases": list(profile.get("model_aliases") or []),
                "provider": profile.get("provider"),
                "endpoint_ref": profile.get("endpoint_ref"),
                "permission_profile": profile.get("permission_profile"),
                "cost_class": profile.get("cost_class"),
                "model_unresolved": _profiles.model_unresolved(profile),
            }
        )
    return rows


def _split_profile_ids(value):
    """``a,b`` → ``['a', 'b']``；缺省或全空返回 ``None``（"没指定" ≠ "空集合"）。"""
    if not value:
        return None
    items = [item.strip() for item in str(value).split(",") if item.strip()]
    return items or None


def _attest_candidates(c, profiles_arg):
    """返回 ``(candidates, scope, scope_note)``：``--profiles`` 只能收窄，不能扩大。"""
    routing = c.get("routing")
    if routing is None:
        candidates = sorted(c["agent_profiles"])
        scope = "all_profiles"
        scope_note = "no routing section: configured means authorized (historical semantics)"
    else:
        candidates = list(routing["allowed_profiles"])
        scope = "routing.allowed_profiles"
        scope_note = "routing.allowed_profiles"
    if profiles_arg:
        selected = [item.strip() for item in profiles_arg.split(",") if item.strip()]
        require(selected, "INPUT_INCOMPLETE", "--profiles is empty")
        outside = sorted(set(selected) - set(candidates))
        require(
            not outside,
            "POLICY_DENIED",
            "attest --profiles may only narrow the allowed set; not authorized: "
            + ", ".join(outside)
            + ". Widen it in the configuration and re-register instead.",
        )
        candidates = selected
    return candidates, scope, scope_note


def _attest_payload(c, candidates, policy_hash, scope) -> dict:
    from src._util import VERSION

    return {
        "schema_version": 1,
        "project_id": c["project_id"],
        "policy_hash": policy_hash,
        "confirmed_profiles": list(candidates),
        "runtime_version": VERSION,
        "confirmed_at": time.time(),
        "confirmed_by": "project attest",
        "scope": scope,
        "note": (
            "Confirms the candidate set for this exact policy_hash. It is not a record "
            "of who wrote the original allowlist, and it does not resist a program with "
            "write access to this home."
        ),
    }


def _attest_precondition_mismatch(policy_hash, candidates, expect_hash, expect_profiles):
    """比对"用户看过的"与"现在要写的"；无差异返回 ``None``。

    v1.5.1：``--dry-run`` 展示的摘要与正式写入之间存在真实窗口（dry-run 显示 H1、
    用户同意、写入前另一进程改了模型，写入时算出 H2）。这个函数只在**写入锁内**
    被调用，所以不一致能被拒绝而不是被记成确认——但读一次再写一次只能缩短窗口，
    不能消除；比对与写入必须在同一个受保护操作里。
    """
    if expect_hash is not None and expect_hash != policy_hash:
        return {
            "field": "policy_hash",
            "expected": expect_hash,
            "observed": policy_hash,
            "expected_revision": expect_hash[:16],
            "observed_revision": policy_hash[:16],
        }
    if expect_profiles is not None:
        wanted = sorted(set(expect_profiles))
        found = sorted(set(candidates))
        if wanted != found:
            return {
                "field": "confirmed_profiles",
                "expected": wanted,
                "observed": found,
                "added": [pid for pid in found if pid not in wanted],
                "removed": [pid for pid in wanted if pid not in found],
            }
    return None


def _require_attest_preconditions(policy_hash, candidates, expect_hash, expect_profiles):
    mismatch = _attest_precondition_mismatch(
        policy_hash, candidates, expect_hash, expect_profiles
    )
    if mismatch is None:
        return
    raise Error(
        "ATTEST_PRECONDITION_FAILED",
        "attest refused: the configuration no longer matches what was confirmed. "
        "Nothing was written. Re-run `project attest --dry-run`, show the new "
        "execution[] and ask for confirmation again.",
        details={
            "kind": "attest_precondition_failed",
            "wrote": False,
            **mismatch,
            "next": "project attest --project <id> --dry-run",
        },
    )


def _project_attest(store, args) -> dict:
    """v1.5.0 §5：为当前配置写入一次授权确认记录。

    与 ``project register`` 分开存放，因为"配置内容没变也要能确认一次"——注册相同
    内容不会改变摘要，靠摘要变化触发确认是做不到的。记录绑定 ``policy_digest``，
    之后任何执行配置变化（换模型、换端点、换权限档）都会让它失效，需要重新确认。

    v1.5.1：写路径在**同一把 control 锁内**重读配置、重算摘要，再与
    ``--expect-policy-hash`` / ``--expect-profiles`` 比对；不一致以
    ``ATTEST_PRECONDITION_FAILED`` 拒绝写入。不带 ``--expect-*`` 时行为与 v1.5.0
    一致（只能事后发现不一致）。

    记录只证明"这份配置被确认过"，不证明"当初是谁写的白名单"，也不防同机写权限
    的伪造——不要把它描述成审批或防篡改机制。
    """
    from src._config import check_execution_contract
    from src._util import read, safe_id

    project_id = safe_id(args.project)
    config_path = store.home / "projects" / (project_id + ".json")
    expect_hash = (getattr(args, "expect_policy_hash", None) or "").strip() or None
    expect_profiles = _split_profile_ids(getattr(args, "expect_profiles", None))

    def snapshot():
        """读一次配置并算出本次要确认的对象（锁内调用才有一致性）。"""
        c = check_execution_contract(read(config_path), base_dir=config_path.parent)
        candidates, scope, scope_note = _attest_candidates(c, args.profiles)
        return c, candidates, scope, scope_note, _profiles.policy_digest(c)

    expectation = None
    if args.dry_run:
        c, candidates, scope, scope_note, policy_hash = snapshot()
        previous = store.attestation(c["project_id"])
        # 预览也把比对结果报出来（不改行为、不拒绝）：这样"带 --expect-* 预演一次"
        # 本身就能验证宿主下一轮写入会不会被拒。
        if expect_hash is not None or expect_profiles is not None:
            mismatch = _attest_precondition_mismatch(
                policy_hash, candidates, expect_hash, expect_profiles
            )
            expectation = {"matched": mismatch is None, **(mismatch or {})}
    else:
        with store.lock():
            c, candidates, scope, scope_note, policy_hash = snapshot()
            _require_attest_preconditions(
                policy_hash, candidates, expect_hash, expect_profiles
            )
            previous = store.attestation(c["project_id"])
            store.write_attestation(
                c["project_id"], _attest_payload(c, candidates, policy_hash, scope)
            )
    return {
        "api_version": 2,
        "project_id": c["project_id"],
        "dry_run": bool(args.dry_run),
        "policy_hash": policy_hash,
        "policy_revision": policy_hash[:16],
        "scope": scope,
        "scope_note": scope_note,
        "confirmed_profiles": list(candidates),
        "execution": _attest_execution_view(c, candidates),
        "expectation": expectation,
        "previous": previous,
        "wrote": not args.dry_run,
        "path": str(store.attestation_path(c["project_id"])),
    }


def _project_quota(store, args) -> dict:
    """额度闸门的读与显式解除（v1.6）。

    ``show`` 只读：没有事故时 ``gate`` 是 ``null``，不伪造一个"未耗尽"的布尔——
    那会和"读不到"混在一起。``clear`` 保留事故记录，只追加解除字段；它记录的是
    人的判断，**不**证明额度真的恢复了（下次派发若仍然没预算，闸门会再触发）。
    """
    from src import _quota
    from src._util import safe_id

    project_id = safe_id(args.project)
    path = str(_quota.gate_path(store, project_id))
    if args.quota_action == "show":
        gate = _quota.active_gate(store, project_id)
        return {
            "api_version": 2,
            "project_id": project_id,
            "blocking": gate is not None,
            "gate": gate,
            "record": _quota.read_gate(store, project_id),
            "path": path,
            "note": (
                "blocking=true means dispatch is refused until `project quota clear`; "
                "an expired reset does not unlock by itself"
            ),
        }
    require(
        isinstance(args.why, str) and args.why.strip(),
        "INPUT_INCOMPLETE",
        "--why must state why the quota is believed to be back",
    )
    previous = _quota.read_gate(store, project_id)
    if previous is None:
        # 没有事故可解除时不要写一条空记录：那会让后来的人以为"曾经耗尽过"。
        return {
            "api_version": 2,
            "project_id": project_id,
            "cleared": False,
            "reason": "no quota incident was recorded for this project",
            "gate": None,
            "path": path,
        }
    with store.lock():
        gate = _quota.clear(
            store,
            project_id,
            why=args.why.strip(),
            actor=getattr(args, "actor", None),
        )
    return {
        "api_version": 2,
        "project_id": project_id,
        "cleared": True,
        "previous": previous,
        "gate": gate,
        "path": path,
        "note": (
            "clearing records a human decision; it does not verify the quota is back. "
            "A dispatch that still has no budget will trip the gate again."
        ),
    }


def _register_dry_run(args) -> dict:
    """Validate a project profile without writing state or project config (v5 §B2).

    Runs before ``Store(...)`` is constructed, so it cannot create ``state.db``.
    ``will_overwrite`` is a pure filesystem question — no database is opened.
    """
    config_path = Path(args.config)
    c = validate_config(read(config_path), base_dir=config_path.parent)
    policy_hash = digest(c)
    target = Path(args.home) / "projects" / (c["project_id"] + ".json")
    return {
        "api_version": 2,
        "dry_run": True,
        "valid": True,
        "project_id": c["project_id"],
        "policy_hash": policy_hash,
        "policy_revision": policy_hash[:16],
        "would_write": str(target),
        "will_overwrite": target.exists(),
        "warnings": path_warnings(c),
        "normalized": c,
    }


def main(argv=None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    store = None
    try:
        if args.cmd == "demo":
            result = run_demo(args.home)
            output(result)
            return EXIT_OK
        if args.cmd == "capabilities":
            if args.check_requirements:
                # Read-only and project-free: a skill must be able to gate a
                # runtime before any project exists on the host.
                output(_requirements.check(args.check_requirements))
            else:
                output(capabilities())
            return EXIT_OK
        if args.cmd == "state":
            if args.action == "restore":
                output(_migrations.restore(args.home, args.from_dir, args.confirm))
            else:
                output(_migrations.migrate(args.home, args.to, args.dry_run))
            return EXIT_OK

        # Host-only diagnosis must not initialize the store (which would create
        # state.db); everything else needs a Store.
        if args.cmd == "doctor" and not getattr(args, "project", None):
            result = doctor_without_project(args.home)
            if args.json:
                output(result)
            else:
                _print_doctor_human(result)
            return EXIT_OK

        # 注册预检（v5 §B2）：校验、规范化、覆盖判断都在 Store 之前完成，
        # 因此不创建 state.db、不写 projects/<id>.json、不取项目锁。
        if args.cmd == "project" and args.action == "register" and getattr(args, "dry_run", False):
            output(_register_dry_run(args))
            return EXIT_OK

        # 任务准备（v5 §B7）：只读，不建 state.db、不建任务。必须在 Store
        # 之前分支，否则"预检"就会留下状态库。
        if args.cmd == "task" and args.action == "prepare":
            output(
                _prepare.prepare(
                    args.home,
                    project=args.project,
                    repo=args.repo,
                    spec=args.spec,
                    out=args.out,
                    emit=args.emit,
                    title=args.title,
                    profile=args.profile,
                    force=args.force,
                )
            )
            return EXIT_OK

        store = runtime.Store(args.home)

        if args.cmd == "project":
            if args.action == "context":
                output(_context.build(store, args.project_id, getattr(args, "task", None)))
                return EXIT_OK
            if args.action == "attest":
                output(_project_attest(store, args))
                return EXIT_OK
            if args.action == "diff":
                output(_diff.project_diff(store, args.config, args.project))
                return EXIT_OK
            if args.action == "quota":
                output(_project_quota(store, args))
                return EXIT_OK
            config_path = Path(args.config)
            c = validate_config(read(config_path), base_dir=config_path.parent)
            with store.lock():
                atomic(store.home / "projects" / (c["project_id"] + ".json"), c)
            # v1.6：记下"这份注册是从哪个文件读的"。运行器此后只读落盘副本，
            # 没有这条记录时"改了 project.json 但没 register"完全静默——症状会被
            # 误读成 adapter 缓存了模型名（见 `_config_source` 模块说明）。
            source = _config_source.remember(store, c["project_id"], config_path, c)
            output({"registered": c["project_id"], "note": "Registration does not certify compatibility or isolation.",
                    "source": source["path"], "profiles": _profiles.profile_views(c)})
            return EXIT_OK
        if args.cmd == "doctor":
            c = store.config(args.project)
            result = doctor(c, getattr(args, "agent_profile", None))
            # 只回答"磁盘上的配置与注册时是否一致"，不参与 `ready`：注册副本才是
            # 权威，提示只负责把"改完没重新注册"从静默变成可见。
            result["config_source"] = _config_source.state(store, args.project, c)
            hint = _config_source.hint(result["config_source"])
            if hint:
                result["config_source"]["hint"] = hint
            if getattr(args, "require_ready", False) and not result.get("ready"):
                output(result)
                return EXIT_ERROR
            output(result)
            return EXIT_OK
        if args.cmd == "agent":
            output(_agent_command(store, args))
            return EXIT_OK
        if args.cmd == "group":
            return _group_command(store, args)
        if args.cmd == "internal-group":
            from src import _group_runner

            output(_group_runner.run(store, args.group_id, args.lease_id))
            return EXIT_OK
        if args.cmd == "watch":
            if getattr(args, "follow", False):
                _watch_follow(store, args)
                return EXIT_OK
            payload = _watch(store, args)
            if getattr(args, "jsonl", False):
                # ``--jsonl`` has a real effect outside ``--follow``: one line per
                # event instead of one pretty-printed array.
                for event in payload["events"]:
                    _print_event(event)
                _print_event(
                    {
                        "next_cursor": payload["next_cursor"],
                        "cursor_status": payload["cursor_status"],
                        "has_more": payload["has_more"],
                        "unattributed": payload["unattributed"],
                    }
                )
                return EXIT_OK
            output(payload)
            return EXIT_OK
        if args.cmd == "qualification":
            output(qualification_mod.run(store, vars(args)))
            return EXIT_OK
        if args.cmd == "evidence":
            output(qualification_mod.record_evidence(store, vars(args)))
            return EXIT_OK
        if args.cmd == "orchestration":
            output(_orchestration_command(store, args))
            return EXIT_OK
        if args.cmd == "route":
            output(_route_command(store, args))
            return EXIT_OK
        if args.cmd == "internal":
            _run_internal(store, args)
            return EXIT_OK
        if args.cmd == "reconcile":
            with store.lock():
                values = [reconcile(store, t) for t in store.tasks() if t["project_id"] == args.project]
            output(values)
            return EXIT_OK

        # task subcommands
        if args.action == "list":
            rows = [t for t in store.tasks() if not args.project or t["project_id"] == args.project]
            output(_project_task_rows(rows, getattr(args, "fields", None)))
            return EXIT_OK
        if args.action == "digest":
            output(
                _digest.digest(
                    store,
                    project_id=args.project,
                    targets=str(getattr(args, "targets", "") or "").split() or None,
                    tail_bytes=getattr(args, "tail_bytes", None),
                    lines=int(getattr(args, "lines", 2) or 2),
                    content=not getattr(args, "no_content", False),
                )
            )
            return EXIT_OK
        if args.action in ("status", "report"):
            _status_or_report(store, args)
            return EXIT_OK
        if args.action == "events":
            if getattr(args, "follow", False):
                _watch_follow(
                    store,
                    args,
                    filters={"project_id": None, "group_id": None, "task_id": args.id},
                )
                return EXIT_OK
            output(_task_events(store, args))
            return EXIT_OK
        if args.action == "logs":
            _validate_logs_args(args)
            if getattr(args, "follow", False):
                _logs_follow(store, args)
                return EXIT_OK
            output(_task_logs(store, args))
            return EXIT_OK
        if args.action == "coverage":
            markers = _coverage.read_markers(args.markers_file)
            body = _coverage.ledger(store, args.project, markers)
            if getattr(args, "format", "json") == "markdown":
                print(_coverage.render(body))
            else:
                output(body)
            return EXIT_OK
        if args.action == "graph":
            body = _coverage.graph(store, args.project)
            if getattr(args, "format", "json") == "markdown":
                print(_coverage.render_graph(body))
            else:
                output(body)
            return EXIT_OK
        if args.action == "review":
            if not getattr(args, "request_file", None):
                output(_task_review.summary(store, store.task(args.id)))
                return EXIT_OK
            output(_task_review.submit(store, args.id, read(args.request_file)))
            return EXIT_OK
        if args.action == "summary":
            if args.summary_action == "list":
                output(summaries_mod.list_summaries(store, args.id))
            else:
                output(summaries_mod.submit(store, args.id, read(args.request_file)))
            return EXIT_OK
        if args.action == "handoff":
            output(summaries_mod.handoff_show(store, args.id))
            return EXIT_OK
        if args.action == "answer":
            output(_task_answer(store, args.id, read(args.request_file)))
            return EXIT_OK
        if args.action == "recover":
            output(_task_recover(store, args.id, args.run, args.confirm_stop))
            return EXIT_OK

        req = read(args.request_file)
        if args.action == "create":
            value = create(store, req)
        elif args.action in ("start", "resume"):
            value = start(store, args.id, req, args.action)
        elif args.action in ("pause", "cancel"):
            value = stop(store, args.id, req, args.action == "cancel")
        elif args.action == "revise":
            value = revise(store, args.id, req)
        elif args.action == "reassign":
            value = reassign(store, args.id, req)
        elif args.action == "rebind":
            value = rebind(store, args.id, req)
        elif args.action == "close-external":
            value = close_external(store, args.id, req)
        else:
            value = accept(store, args.id, req)
        output(value)
        return EXIT_OK
    except (Error, KeyError, ValueError, OSError) as e:
        error = e if isinstance(e, Error) else Error(
            "INPUT_INCOMPLETE" if isinstance(e, KeyError) else "ENVIRONMENT_ERROR", str(e))
        # ``details`` is additive: a host that only reads ``code`` is unaffected,
        # a host that needs the drift kind never parses the message (§11.1).
        output({"error": error.to_dict()})
        return _exit_code(args, error)
    finally:
        # 连接生命周期跟随命令：短命进程里这是卫生问题，长驻路径（watch、
        # internal-group）里则是必须显式关闭的资源。
        if store is not None:
            store.close()


#: ``SUPERVISOR_LOCK_WAIT`` is defined in ``_pipeline`` (where
#: ``INTENT_GRACE_SECONDS`` derives from it) and re-exported here so hosts and
#: tests can patch the CLI-level name without touching the pipeline module.


def _run_internal(store, args) -> None:
    """Supervisor entry: hold the lifecycle locks, CAS handshake (02 §3), run.

    The old single-fd parameter form is gone: the handshake now verifies
    ``attempt_id`` + ``lock_owner`` + pid identity in one short transaction, and
    rejects without side effects on any mismatch.

    The supervisor is the long-lived holder of ``maintenance`` SH + ``writer`` EX
    (01 §5.1); ``start`` only probes them. That is what makes "one writer per
    workspace" an OS-level fact for the whole run instead of a DB convention, and
    what lets ``reconcile`` use "writer lock acquirable" as a close-out gate.
    """
    r = store.get_run(args.run_id)
    require(r, "RUN_NOT_FOUND", args.run_id)
    task_id = r["task_id"]
    # `wait`: `start` still holds these locks while it spawns us, so take them
    # over with a bounded wait instead of failing with TASK_BUSY.
    with lifecycle_locks(store, task_id, wait=SUPERVISOR_LOCK_WAIT):
        try:
            with store.lock():
                r = store.get_run(args.run_id)
                require(r, "RUN_NOT_FOUND", args.run_id)
                task = store.task(task_id)
                start_token = r.get("start_token") or r.get("identity")
                require(
                    r.get("pid") == os.getpid()
                    and start_token == process_identity(os.getpid()),
                    "POLICY_DENIED",
                    "Internal supervisor identity mismatch",
                )
                # CAS: dispatch_state + attempt_id + lock_owner must all match.
                require(
                    r.get("dispatch_state") == "spawn_observed",
                    "STATE_CONFLICT",
                    "Run dispatch_state is not spawn_observed",
                )
                require(
                    r.get("attempt_id") == args.attempt_id,
                    "POLICY_DENIED",
                    "attempt_id mismatch; another supervisor owns this run",
                )
                lock_owner = r.get("lock_owner") or {}
                require(
                    lock_owner.get("run_id") == args.run_id
                    and lock_owner.get("attempt_id") == args.attempt_id,
                    "POLICY_DENIED",
                    "lock_owner mismatch",
                )
                require(
                    task["current_run_id"] == args.run_id
                    and task["state"] in ("RUNNING", "PAUSING"),
                    "STATE_CONFLICT",
                    "Run is not the active launch",
                )
                require(
                    r.get("protocol_version", 4) == 4,
                    "PROTOCOL_UNSUPPORTED",
                    "Run protocol_version mismatch",
                )
                r.update(status="running", dispatch_state="acknowledged")
                store.run(r)
            run_pipeline(store, task_id)
        finally:
            # Stop the tree *before* the writer lock is released, so the workspace
            # is never unlocked while a writer could still be running.
            try:
                release_run_tree(store, args.run_id)
            except (Error, OSError):
                pass


def _delivery_report(store, args, t) -> None:
    """``task report``: one snapshot, one document, optional on-disk copy (W4).

    ``--format json`` without ``--write`` keeps returning the task record
    unchanged. With ``--write`` it returns a small receipt instead, because the
    point of the flag is the file, and ``report_path`` is the only new field
    (§12).
    """
    snap = _reports.snapshot(store, t)
    text = _reports.render(snap)
    receipt = None
    if getattr(args, "write", False):
        receipt = _reports.persist(store, t, snap, text)
    if args.format == "markdown":
        print(text)
        return
    output(receipt or t)


def _status_or_report(store, args) -> None:
    with store.lock():
        t = store.task(args.id)
        if args.refresh:
            t = reconcile(store, t)
            r = store.get_run(t.get("current_run_id")) if t.get("current_run_id") else None
            if t["phase"] == "ci" and (not r or r["status"] == "exited") and t["state"] not in TERMINAL:
                q = Publisher(t["config"]).refresh(t)
                t.update(pr=q.get("pr", t["pr"]),
                         state="REVIEW_READY" if q["ready"] else "BLOCKED",
                         block_code=None if q["ready"] else "CI_PENDING")
                store.save(t, "ci_refreshed")
    if getattr(args, "action", "") == "report":
        _delivery_report(store, args, t)
        return
    if args.format == "markdown":
        run = store.get_run(t["current_run_id"]) if t.get("current_run_id") else None
        trust = (run or {}).get("trust_level") or "unknown"
        trust_note = (
            " (unverified adapter: no real end-to-end record)"
            if trust in ("unverified", "unknown")
            else ""
        )
        close = t.get("external_close") or {}
        print(
            f'# {t["title"]}\n\n- Task: {t["task_id"]}\n- Spec: {t["spec_revision"]}\n'
            f'- State: {t["state"]} / {t["phase"]}\n- Candidate: {t.get("candidate_sha")}\n'
            f'- Block: {t.get("block_code")}\n- PR: {(t.get("pr") or {}).get("url")}\n'
            f'- Acceptance: {(t.get("acceptance") or {}).get("decision", "pending")}\n'
            f'- Agent profile: {t.get("agent_profile_id")} '
            f'({t.get("adapter_id")}/{t.get("adapter_backend")})\n'
            f'- Trust: {trust}{trust_note}\n'
            f'- Evidence: {t.get("current_evidence_id")}\n'
            f'- Deployment: NOT_MANAGED'
        )
        if close:
            # 外部收口不是验收：这一行必须显式出现，否则 CANCELLED 会被读成
            # "用户自己取消的任务"，而不是"交付发生了、但 Zoe 没验证过"。
            print(
                "- 外部收口（未通过 Zoe 验收）：{} · {} · claim={}".format(
                    close.get("external_ref"), close.get("reason"), close.get("claim")
                )
            )
        progress = _progress.view(store, t, run)
        if progress is not None:
            print(
                "- Observation: bytes={} quiet={}s process={}{}".format(
                    progress.get("bytes"),
                    progress.get("quiet_seconds"),
                    progress.get("process_state"),
                    "（观测已过期）" if progress.get("observations_stale") else "",
                )
            )
    else:
        # 观测事实是加法字段：`state`/`phase` 仍是控制器状态，`progress` 只回答
        # "有没有新输出、最后一次观测是什么时候"，不推断卡死或推进。
        run = store.get_run(t["current_run_id"]) if t.get("current_run_id") else None
        progress = _progress.view(store, t, run)
        payload = dict(t)
        if progress is not None:
            payload["progress"] = progress
        output(payload)


def _print_doctor_human(result: dict) -> None:
    checks = result.get("checks", {})

    def cell(name):
        v = checks.get(name)
        if v is True:
            return "ok"
        if v is False or v is None:
            return "not found"
        return str(v)

    print(f"Zoe Runtime {result.get('version')}")
    for name in ("python", "git", "codebuddy", "gh"):
        if name == "python":
            v = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            print(f"  {name:<11} {v:<14} ok")
            continue
        print(f"  {name:<11} {cell(name):<14}")
    print(f"  process-id  {cell('process_identity')}")
    print(f"  platform-lk {cell('platform_lock')}")
    print(f"  platform-tr {cell('platform_tree')}")
    print("  " + "-" * 30)
    print("  project      (未指定；用 zoe doctor --project P 检查项目就绪度)")
    print(f"  verdict      {result.get('verdict')}")


if __name__ == "__main__":
    raise SystemExit(main())
