"""Task lifecycle orchestration: create, start, stop, revise, reassign, rebind, accept."""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from src import (
    _baseline,
    _contracts,
    _coverage,
    _discovery,
    _drift,
    _evidence,
    _groups,
    _orchestration,
    _paths,
    _profiles,
    _progress,
    _quota,
    _routing,
    _task_review,
    _worktree,
)
from src._agents import base as agent_base
from src._agents.base import (
    DEFAULT_MAX_OUTPUT_BYTES,
    AgentResult,
    ProcessCapture,
    RunContext,
)
from src._backend import fingerprint, scope
from src._config import (
    attestation_status,
    authorize,
    check_execution_contract,
    doctor,
    refuse_docker_backend,
    resolve_check,
)
from src._platform import get_platform
from src._proc import alive
from src._publisher import Publisher
from src._store import Store
from src._util import TERMINAL, Error, atomic, check_disk_space, digest, git, read, require

#: Delivery modes the controller knows. ``group_member`` (TGD §6.3) stays at
#: ``REVIEW_READY``: it is neither published nor accepted on its own.
GROUP_MEMBER = "group_member"
DELIVERY_MODES = ("pull_request", "local_artifact", GROUP_MEMBER)


def _binding(t: dict):
    """Resolve a task's frozen configuration, normalizing a v0.2 document.

    A task created by v0.2 froze a v1 configuration without ``agent_profiles``.
    Normalizing a copy here keeps old work resumable instead of failing with
    "Project configuration is not normalized" (design §14: v0.2.0 data migration
    must not strand existing tasks).
    """
    config = t["config"]
    if not isinstance(config.get("agent_profiles"), dict):
        config = _profiles.normalize_config(json.loads(json.dumps(config)))
    return _profiles.resolve_binding(config, t.get("agent_profile_id"))


def _gitattributes_paths(repo):
    """Yield ``.gitattributes`` files under ``repo``, skipping the object store.

    安全检查的语义不变（仓库里任一 ``.gitattributes`` 声明了 ``filter=`` 就拒绝），
    但 ``Path.rglob`` 会连 ``.git`` 一起遍历：那里的对象/包文件数量与仓库历史成正比，
    而且 git 从不把属性应用到 ``.git`` 自身。剪掉这一棵子树即可，不需要放弃覆盖。
    """
    skip = {".git"}
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in skip]
        if ".gitattributes" in files:
            yield Path(root) / ".gitattributes"


def _reply(req: dict, body: dict, recommended_action: str | None = None) -> dict:
    """Unified v2 response envelope (design §12).

    ``api_version`` / ``operation_id`` / ``evidence_refs`` are present on every
    mutation so a host never has to infer them from the command it called.
    """
    value = {"api_version": 2, "operation_id": req.get("request_id"), **body}
    if "evidence_refs" not in value:
        evidence = value.get("current_evidence_id") or value.get("evidence")
        value["evidence_refs"] = [evidence] if isinstance(evidence, str) else []
    if recommended_action is not None:
        value.setdefault("recommended_action", recommended_action)
    return value


#: 阻塞码的修复责任。策略类：改项目策略并显式 rebind 后才能继续；环境/认证类：
#: 重新预检即可继续，**不得**要求先改策略——重新登录、修 PATH、恢复网络之后
#: 必须能接着跑，否则「策略摘要未变」会把人锁死在门外。
_POLICY_INCIDENTS = {"SCOPE_VIOLATION", "VERIFICATION_NOT_APPLICABLE"}
_ENVIRONMENT_INCIDENTS = {"AGENT_AUTH_REQUIRED", "ENVIRONMENT_ERROR", "ENVIRONMENT_DRIFT"}


def _runs_active(store: Store, t: dict) -> bool:
    """True when the task still has a run the controller cannot call exited."""
    r = store.get_run(t["current_run_id"]) if t.get("current_run_id") else None
    return bool(r and r.get("status") != "exited")


def _revalidation_product(store: Store, t: dict) -> tuple[dict | None, bool]:
    """Product identity a rebind may re-verify in place (F1).

    ``validated_sha``/``validated_fingerprint`` are *evidence*, and the first
    rebind clears them. The product basis is therefore written once (under
    ``revalidation.product``) and reused by every later rebind: deriving it from
    ``validated_fingerprint`` again would lose the basis after the first rebind
    and turn the second rebind back into a dead end.

    首次重验的基准必须取自**已签发的证据**，不能取自当前工作树：否则"先手工
    改文件、再 rebind"会把人改的内容当成 agent 的产物重新签字，`accept` 也只
    校验到篡改后的树。只有从未签发过证据时才退回 ``candidate_sha`` + 当前树，
    因为那时还没有可对照的基准。
    """
    product = (t.get("revalidation") or {}).get("product")
    w = Path(t["workspace"])
    if not w.exists():
        return product, False
    try:
        head = git(w, "rev-parse", "HEAD")
        current = fingerprint(t)
    except Error:
        return product, False
    if not product:
        if t.get("validated_sha") and t.get("validated_fingerprint"):
            product = {
                "sha": t["validated_sha"],
                "fingerprint": t["validated_fingerprint"],
            }
        elif t.get("candidate_sha") and head == t["candidate_sha"]:
            product = {"sha": head, "fingerprint": current}
    intact = bool(product) and head == product["sha"] and current == product["fingerprint"]
    return product, intact


def _revalidation_blockers(store: Store, t: dict) -> list[str]:
    """Why a rebind may not re-verify in place (empty list = it may)."""
    blockers = []
    # `group_member` delivers locally too (it stops at REVIEW_READY and the group
    # integrates it), so re-verification in place is legitimate for it.
    if t.get("delivery_mode") not in ("local_artifact", GROUP_MEMBER):
        blockers.append(
            "delivery_mode={} delivers through a remote; local evidence cannot be "
            "re-verified, reconcile the real publication state instead".format(
                t.get("delivery_mode")
            )
        )
    if t.get("block_code") == "PROCESS_UNKNOWN":
        blockers.append("execution domain unknown; recover the writer first")
    if _runs_active(store, t):
        blockers.append("an agent run is still active")
    return blockers


def _group_admission(store: Store, t: dict) -> None:
    """Group admission for a member task, resolved lazily to avoid a cycle."""
    if not t.get("group_id"):
        return
    from src import _group_runner

    _group_runner.admission(store, t)


def _require_member_mutable(store: Store, t: dict, action: str) -> None:
    """An integrated member is frozen: its product is already in the branch.

    Allowing ``reassign``/``rebind``/``revise`` here would rewrite the meaning of
    an integration that already happened, so the only correction path is a new
    follow-up group (TGD §6.4, §8.4).
    """
    if not t.get("group_id"):
        return
    try:
        group = store.group(t["group_id"])
    except Error:
        # 读不到组记录 ≠ "未集成"。已集成成员的产品已经在分支里，"无法证明未集成"
        # 就必须按已集成处理（保守方向）；静默 return 会让 revise/reassign/rebind
        # 改写一次已经发生的集成，而这条不变量正是它们唯一要守的东西。
        raise Error(
            "STATE_CONFLICT",
            "group {!r} is unreadable; cannot prove member {!r} is not integrated".format(
                t["group_id"], t.get("item_id")
            ),
            details={
                "kind": "group_unreadable",
                "group_id": t["group_id"],
                "task_key": t.get("item_id"),
                "next_action": "restore the group record, or create a follow-up group",
            },
        ) from None
    if not _groups.is_v3(group):
        return
    key = t.get("item_id")
    runtime = next((item for item in group["runtime"]["members"] if item["task_key"] == key), None)
    if runtime and runtime.get("integrated_sha") and not runtime.get("invalidated"):
        raise Error(
            "STATE_CONFLICT",
            "member {!r} is already integrated into {}; {} is refused. Create a "
            "follow-up group to change delivered work.".format(
                key, runtime["integrated_sha"][:12], action
            ),
            details={
                "kind": "integrated_member",
                "group_id": group["group_id"],
                "task_key": key,
                "integrated_sha": runtime["integrated_sha"],
                "next_action": "create a follow-up group with base_ref=<integration_sha>",
            },
        )


def _waived_run_ids(t: dict) -> set[str]:
    return {str(w.get("run_id")) for w in (t.get("budget_waivers") or []) if w.get("run_id")}


def _billable_attempts(t: dict) -> int:
    """Launches that count against ``max_attempts``.

    ``attempts`` stays monotonic: it records how many times an agent was really
    started, and that fact is never rewritten (handover/upgrade keep it too). A
    waiver is a **separate persisted record keyed by** ``run_id``, so a replay or
    a crash cannot refund the same run twice.
    """
    return max(0, int(t.get("attempts") or 0) - len(_waived_run_ids(t)))


def _incident_basis(store: Store, t: dict, code: str) -> str | None:
    """Attribute a failure to policy/environment, or to the agent (``None``).

    Deliberately *not* a code table: ``SCOPE_VIOLATION`` is waived only when the
    frozen spec itself demands the offending path, and a check that produced no
    verdict is waived only when no acceptance criterion references it. When the
    attribution cannot be established, nothing is waived.
    """
    if code == "SCOPE_VIOLATION":
        missing = list((t.get("block_details") or {}).get("missing_paths") or [])
        if not missing:
            return None
        try:
            spec_text = read(store.path(t) / f"spec-{t['spec_revision']}.json")["text"]
        except (OSError, ValueError, KeyError, TypeError):
            # 这是在**失败处理分支**里调用的：冻结 spec 读不到时若再抛异常，
            # BLOCKED 就只存在内存里，任务会停在 RUNNING 且真因丢失。读不到
            # 等于"无法证明策略过窄"，按不豁免处理（保守方向）。
            return None
        return "policy-too-narrow" if all(path in spec_text for path in missing) else None
    if code == "VERIFICATION_NOT_APPLICABLE":
        return "check-not-in-spec"
    if code in _ENVIRONMENT_INCIDENTS:
        return "environment"
    return None


def _require_config_repaired(t: dict) -> None:
    """Refuse to spend another attempt on a policy incident that was not repaired.

    Only *policy* incidents are gated this way. Environment incidents (re-login,
    PATH, network) deliberately pass through: their fix does not change the policy
    digest, and requiring one would block the only working remedy.

    判据必须对齐"范围来自哪里"（v1.6）。``task_scope`` 只在 create/revise 时由
    :func:`_resolve_task_scope` 重算，``rebind`` 只更新 ``policy_revision``。所以
    spec 派生范围（``scope_source="spec"``）下的 ``policy-too-narrow``，改项目配置
    再 ``rebind`` 会**通过这道闸**却在下一轮撞同一个 ``SCOPE_VIOLATION``——代价是
    一次付费尝试加一次误判（"rebind 修好了"）。那种情况下唯一能改变范围的动作是
    ``task revise``，闸门必须只说这一条。
    """
    last = t.get("last_block") or {}
    if last.get("code") not in _POLICY_INCIDENTS:
        return
    if last.get("basis") not in ("policy-too-narrow", "check-not-in-spec"):
        return
    if last.get("basis") == "policy-too-narrow" and last.get("scope_source") == "spec":
        if t.get("spec_revision") == last.get("spec_revision"):
            # 结构化 details 与 verify 阶段抛出的那条同形（`next_action`），宿主
            # 可以只写一个分支：两次都读 block_details.next_action，不必按阶段猜。
            raise Error(
                last["code"],
                "Blocked because the frozen spec's own scope is too narrow: `rebind` does "
                "not recompute task_scope, so widening the project policy cannot clear "
                "this. Widen `allowed_paths` in the spec and run `task revise` — the "
                "worktree and everything the agent already wrote are kept.",
                details={
                    "scope_source": "spec",
                    "spec_revision": t.get("spec_revision"),
                    "next_action": (
                        "spec-scoped: widen `allowed_paths` in the spec, then `task revise` "
                        "(rebind does NOT recompute task_scope; the worktree is kept)"
                    ),
                },
            )
        return
    if t.get("policy_revision") == last.get("policy_revision"):
        raise Error(
            last["code"],
            "Blocked by configuration, and the project policy has not changed since "
            "the last block: fix the policy and `task rebind` before resuming.",
            details={
                "scope_source": last.get("scope_source"),
                "policy_revision": t.get("policy_revision"),
                "next_action": "fix the project policy, then `task rebind`",
            },
        )


def supervisor_env() -> dict:
    """子进程环境：继承当前环境，并把 PYTHONPATH 每一项绝对化。

    supervisor 以 ``cwd=<home>`` 启动，``PYTHONPATH=src`` 这类相对路径会随之失效
    （源码模式的典型失败）。派发前统一转绝对路径，避免把配置错误变成
    ``BLOCKED / PROCESS_UNKNOWN``。
    """
    env = dict(os.environ)
    raw = env.get("PYTHONPATH")
    parts = [os.path.abspath(item) for item in raw.split(os.pathsep) if item] if raw else []
    # 源码模式：supervisor 以 ``cwd=<home>`` 启动，``import src`` 只能靠 PYTHONPATH。
    # 从本模块位置推出包根（安装态是 site-packages，源码态是仓库根）并幂等追加，
    # 否则"代码在、但当前解释器找不到"会被预检报成 ENVIRONMENT_ERROR，用户照着
    # 提示去 ``pip install`` 也只是换一种失败方式。
    root = str(Path(__file__).resolve().parent.parent)
    if root not in parts:
        parts.append(root)
    if parts:
        env["PYTHONPATH"] = os.pathsep.join(parts)
    else:
        env.pop("PYTHONPATH", None)
    return env


def require_supervisor_importable(env: dict, home, log: Path) -> None:
    """派发前自检：同一解释器、同一 cwd、同一环境下能否 import src。

    失败时立即返回可诊断的 ``ENVIRONMENT_ERROR``，而不是让任务在 supervisor
    根本没起来的情况下以 ``PROCESS_UNKNOWN`` 收场。
    """
    try:
        probe = subprocess.run(
            [sys.executable, "-c", "import src"],
            cwd=str(home),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Error(
            "ENVIRONMENT_ERROR", f"Cannot start the supervisor interpreter: {exc}"
        ) from None
    if probe.returncode != 0:
        detail = " | ".join(
            line for line in (probe.stderr or probe.stdout or "").strip().splitlines()[-3:]
        )
        raise Error(
            "ENVIRONMENT_ERROR",
            "Supervisor cannot import src: "
            + (detail or f"exit {probe.returncode}")
            + ". Run `pip install zoe-runtime`, or set PYTHONPATH to an absolute path "
            f"(log: {log}).",
        )


def require_valid_branch(repo, branch: str) -> None:
    """Defense-in-depth on the task branch name (§6.7).

    ``task_id`` is controller-generated and ``safe_id`` already excludes ``/``,
    ``..``, ``@{`` and a leading ``-``, so this is hardening, not a security
    fix. The ``refs/heads/`` form is used deliberately: ``check-ref-format
    --branch`` is more permissive and accepts ``@{-1}`` style expansions.

    A leading ``-`` is rejected explicitly: ``refs/heads/-x`` is a *valid ref*,
    but ``git push origin -x`` would read it as an option.
    """
    require(
        isinstance(branch, str) and branch and not branch.startswith("-"),
        "POLICY_DENIED",
        f"Invalid task branch name {branch!r}",
    )
    try:
        git(repo, "check-ref-format", "refs/heads/" + branch)
    except Error:
        raise Error("POLICY_DENIED", f"Invalid task branch name {branch!r}") from None


def _resolve_task_scope(spec_document, config: dict) -> tuple[list[str] | None, str]:
    """Decide the writable scope for a task (config-friction v5 §B0-a).

    ``None`` means "inherit the project scope" — the historical behaviour, kept
    for specs that declare nothing and for tasks frozen before ``task_scope``
    existed. An empty result is refused rather than falling back to the project
    scope: silently widening a narrowed scope is the one failure this function
    exists to prevent.
    """
    declared = _contracts.spec_scope(spec_document)
    if declared is None:
        return None, "project"
    effective = _paths.subtract(declared, config.get("protected_paths") or [])
    require(
        effective,
        "POLICY_DENIED",
        "Task scope is entirely covered by protected_paths; nothing would be writable",
    )
    return effective, "spec"


def _without_scope(document) -> dict | None:
    """Spec 文档去掉 ``allowed_paths`` 后的形态，用于"只改了范围"的判等。"""
    if not isinstance(document, dict):
        return None
    return {key: value for key, value in document.items() if key != "allowed_paths"}


def _scope_only_change(store: Store, t: dict, document) -> bool:
    """这次修订是否**只**改了 ``allowed_paths``。

    只有这种修订可以复用工作区里已有的产物：实现要求一个字没变，重跑 agent 只会
    把同样的代码再写一遍（还可能顺手改坏）。读不到旧 spec、或任一侧不是结构化文档
    时返回 ``False``——宁可多跑一次 agent，也不能把一份没按新要求实现的代码判成通过。
    """
    if not isinstance(document, dict):
        return False
    try:
        previous = json.loads(read(store.path(t) / f"spec-{t['spec_revision']}.json")["text"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not isinstance(previous, dict):
        return False
    return _without_scope(previous) == _without_scope(document)


def _require_attested(
    store: Store,
    project_id: str,
    config: dict[str, Any],
    profile_id: str | None = None,
) -> dict[str, Any]:
    """v1.5.0 §5：这份配置的候选集合必须被用户确认过。

    只回答"确认过没有"，不回答"配置合不合法"（那是 ``check_execution_contract``）
    也不回答"配置变了没有"（那是 ``POLICY_CHANGED``）。三者混用会让错误信息指向
    错误的下一步。

    ``profile_id`` 给出"这次实际要用哪个候选"：确认记录可以比白名单更窄
    （``project attest --profiles``），那种收窄必须生效，否则记录只是装饰。
    """
    status = attestation_status(config, store.attestation(project_id), profile_id)
    if not status["confirmed"]:
        if status.get("reason") == "candidate_not_confirmed":
            raise Error(
                "ALLOWLIST_UNCONFIRMED",
                "The selected profile is inside the configured allowlist but was not part "
                "of the confirmed set; confirm it or pick a confirmed candidate",
                details={**status, "next": f"zoe project attest --project {project_id}"},
            )
        raise Error(
            "ALLOWLIST_UNCONFIRMED",
            "This project's allowed execution profiles have not been confirmed for the "
            "current configuration; review the candidates and confirm once",
            details={**status, "next": f"zoe project attest --project {project_id}"},
        )
    return status


def _require_executable(
    store: Store, project_id: str, config: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """v1.5.0 M8：执行准入 = 配置合法 + 授权已确认（不针对具体候选）。

    返回 ``(checked, allowlist)``：``checked`` 是**校验并规范化后**的副本，调用方
    必须用它继续构造绑定。丢弃它意味着"校验的是副本、执行的是原件"——默认值与
    路径解析都可能不是同一份。
    """
    checked = check_execution_contract(config, base_dir=store.home / "projects")
    return checked, _require_attested(store, project_id, checked)


def create(store: Store, req: dict[str, Any]) -> dict[str, Any]:
    """Create a task from a frozen spec. Idempotent by ``request_id``."""
    with store.lock():
        key, prior = store.operation(req, "create")
        if prior:
            return prior
        try:
            # 草稿占位符先于授权校验：``--emit`` 产出的 create.json 里连
            # ``authorization_ref`` 都是占位符，先报它才能让人知道该填什么（v5 §B8）。
            _contracts.reject_placeholders(req)
            c = store.config(req["project_id"])
            # 依次回答三个不同问题：配置合不合法 → 凭据是否匹配 → 确认过没有。
            # 合法性必须排在最前：占位符/策略缺陷不是"授权失败"，先报
            # AUTH_DENIED 会把用户指向错误的一步（v1.5.0 M8）。
            c, allowlist = _require_executable(store, req["project_id"], c)
            authorize(c, req, "create")
            spec = _contracts.load_spec(req["spec_file"], c)
            delivery_mode = req.get("delivery_mode")
            require(
                delivery_mode in DELIVERY_MODES,
                "INPUT_INCOMPLETE",
                "Invalid delivery_mode",
            )
            require(
                bool(req.get("group_id")) == bool(req.get("item_id")),
                "INPUT_INCOMPLETE",
                "group_id and item_id must be given together",
            )
            # v3 组里只有 group_member 会走冻结 spec 校验与组内推进；若允许别的
            # delivery_mode 带着 group_id 绑定，成员就绕过了 spec 校验，且
            # `task accept` 的自验收拦截（按 delivery_mode 判）也拦不住它。
            # legacy ordered 组沿用旧语义（item 绑定 + 各自验收），不受此约束。
            if req.get("group_id"):
                require(
                    delivery_mode == GROUP_MEMBER
                    or not _groups.is_v3(store.group(req["group_id"])),
                    "INPUT_INCOMPLETE",
                    "group_id/item_id require delivery_mode=group_member: a v3 group "
                    "member is closed by its group, never by `task accept`",
                )
            purpose = req.get("purpose") or "task"
            require(
                purpose in ("task", "probe"),
                "INPUT_INCOMPLETE",
                "purpose must be 'task' or 'probe' (probe results never enter quality metrics)",
            )
            plan = None
            if c.get("routing") is not None or req.get("route") is not None:
                # 五步流程里唯一"改变派发给谁"的一步：快照 + 纯函数决策。
                # 显式路径同样走完 L0/L1/L2，失败即报该候选的具体排除码，不回落。
                plan = _routing.plan_route(
                    c, req, spec_document=spec.get("document"), trigger="create"
                )
                _routing.raise_for_plan(plan)
                profile_id = plan["selected_profile"]
            else:
                profile_id = req.get("agent_profile") or c["default_agent_profile"]
            binding = _profiles.resolve_binding(c, profile_id)
            # 候选已定：确认记录若比白名单更窄，这里必须拦下未确认的那个候选。
            allowlist = _require_attested(store, req["project_id"], c, binding.profile_id)
            if req.get("agent") is not None:
                require(
                    req["agent"] == binding.adapter_id,
                    "CAPABILITY_UNSUPPORTED",
                    "agent {!r} does not match profile adapter {!r}".format(
                        req["agent"], binding.adapter_id
                    ),
                )
            for attr in _gitattributes_paths(c["repo"]):
                require(
                    not re.search(r"filter\s*=", attr.read_text(errors="replace")),
                    "CAPABILITY_UNSUPPORTED",
                    "Repository requires Git filters; unsupported",
                )
            base = git(c["repo"], "rev-parse", "--verify", c.get("base_ref", "HEAD") + "^{commit}")
            id = "task-" + uuid.uuid4().hex[:16]
            branch = "zoe/" + id
            require_valid_branch(c["repo"], branch)
            directory = store.home / "tasks" / id
            directory.mkdir(parents=True)
            atomic(directory / "spec-1.json", spec)
            group_id, item_id = req.get("group_id"), req.get("item_id")
            # A group member is created from the group's frozen spec snapshot and
            # is closed by `group accept`, never by `task accept` (TGD §6.3).
            member_spec = None
            consumed_input_sha = None
            if delivery_mode == GROUP_MEMBER:
                require(
                    group_id,
                    "INPUT_INCOMPLETE",
                    "delivery_mode=group_member requires group_id and item_id",
                )
                group = store.group(group_id)
                _groups.require_v3(group, "dispatch")
                member_spec = _groups.member(group, item_id)
                require(
                    digest(spec["text"].encode()) == member_spec["spec_sha256"],
                    "SPEC_CHANGED",
                    "spec_file does not match the frozen spec snapshot of member "
                    + member_spec["task_key"],
                )
                require(
                    group["state"] in ("READY", "RUNNING"),
                    "STATE_CONFLICT",
                    f"group is {group['state']}; members cannot be created now",
                )
                consumed_input_sha = group["runtime"].get("integration_sha") or group["base_sha"]
                for dep in member_spec["depends_on"]:
                    dep_sha = _groups.member_runtime(group, dep).get("integrated_sha")
                    require(
                        dep_sha,
                        "STATE_CONFLICT",
                        f"upstream member {dep} is not integrated yet",
                    )
                    try:
                        git(c["repo"], "merge-base", "--is-ancestor", dep_sha, consumed_input_sha)
                    except Error:
                        raise Error(
                            "STATE_CONFLICT",
                            f"consumed_input_sha does not contain upstream {dep}",
                            details={"kind": "dependency_not_integrated", "upstream": dep},
                        ) from None
            endpoint = _routing.endpoint_of(c, binding.profile)
            execution = _routing.selected_execution(
                binding=binding,
                endpoint=endpoint,
                # §12.1：create 只能用 pin 版本算，实测值由 start 补。
                cli_version=binding.profile.get("expected_version"),
                policy_revision=digest(c)[:16],
                routing=_routing.routing_digest(c),
            )
            decision = None
            if plan is not None:
                decision = _routing.build_decision(
                    c=c,
                    plan=plan,
                    requirements=plan["requirements"],
                    task_type=plan["task_type"],
                    task_type_source=plan["task_type_source"],
                    task_id=id,
                    assignment_revision=1,
                    trigger="create",
                    requested_profile=req.get("agent_profile"),
                    execution=execution,
                    project_id=c["project_id"],
                )
            task_scope, scope_source = _resolve_task_scope(spec.get("document"), c)
            t = {
                "task_id": id,
                "project_id": c["project_id"],
                "title": req["title"],
                "row_version": 1,
                "record_version": 2,
                "state": "READY",
                "phase": "prepare",
                "spec_revision": 1,
                "spec_hash": digest(spec["text"].encode()),
                "spec_contract_version": spec["contract_version"],
                "config": c,
                # 任务级可写范围（v5 §B0-a）：``None`` = 沿用项目范围；列表 =
                # spec 声明收紧后的结果，且已扣掉完全落在 protected 内的条目。
                "task_scope": task_scope,
                "scope_source": scope_source,
                "base_sha": base,
                "branch": branch,
                "workspace": str(directory / "worktree"),
                "delivery_mode": req["delivery_mode"],
                "created_at": time.time(),
                "attempts": 0,
                "current_run_id": None,
                "block_code": None,
                "candidate_sha": None,
                "validated_sha": None,
                "pr": None,
                "acceptance": None,
                "source": req.get("source"),
                "group_id": group_id,
                "item_id": item_id,
                # 组成员冻结"建任务时上游已集成到哪"：它让"这个任务基于什么基线
                # 实现"成为可查的事实，而不是依赖顺序的假设（TGD §6.3）。
                "consumed_input_sha": consumed_input_sha,
                "group_spec_sha256": (member_spec or {}).get("spec_sha256"),
                "agent_profile_id": binding.profile_id,
                "adapter_id": binding.adapter_id,
                "adapter_backend": binding.backend,
                "assignment_revision": 1,
                "policy_revision": digest(c)[:16],
                "current_evidence_id": None,
                "last_progress_at": time.time(),
                # v1.2 §12.1 / §14.1：冻结"实际跑的是什么"，而不是只冻结 profile 名。
                "selected_execution": execution,
                "combination_id": execution["combination_id"],
                "requested_model": binding.profile.get("model"),
                "model_unresolved": execution["model_unresolved"],
                "endpoint": endpoint,
                "purpose": purpose,
                "requirements": (plan or {}).get("requirements"),
                "task_type": (plan or {}).get("task_type"),
                "task_type_source": (plan or {}).get("task_type_source"),
                "route_id": (decision or {}).get("route_id"),
                "route_content_sha256": (decision or {}).get("content_sha256"),
                "route_reason": (plan or {}).get("reason"),
                "snapshot_digest": (plan or {}).get("snapshot_digest"),
                "assignments": [
                    {
                        "assignment_revision": 1,
                        "profile_id": binding.profile_id,
                        "adapter_id": binding.adapter_id,
                        "adapter_backend": binding.backend,
                        "at": time.time(),
                        "reason": (
                            "route:" + plan["reason"]
                            if plan is not None
                            else ("explicit" if req.get("agent_profile") else "initial assignment")
                        ),
                    }
                ],
            }
            if decision is not None:
                # 顺序固定（§15.5）：决策文件先落盘，再在同一事务里写任务记录；
                # 反过来会留下"任务引用了不存在的决策"这种不可检测状态。
                _routing.write_decision(store, t, decision)
            with store.tx():
                store.db.execute("INSERT INTO tasks VALUES(?,?,?)", (id, 1, json.dumps(t)))
                store.event(
                    id,
                    {
                        "event": "created",
                        "type": "created",
                        "payload_version": 2,
                        "state": t["state"],
                        "phase": t["phase"],
                        "actor_kind": "host",
                        "request_id": req.get("request_id"),
                        "summary": "task created from frozen spec",
                    },
                )
                if group_id:
                    _groups.bind_item(store, group_id, item_id, id)
                result = _reply(
                    req,
                    {
                        "status": "succeeded",
                        "task_id": id,
                        "row_version": 1,
                        "accepted": True,
                        "completed": True,
                        "profile_id": binding.profile_id,
                        "adapter_id": binding.adapter_id,
                        "adapter_backend": binding.backend,
                        "capability": binding.capability,
                        "permission": binding.permission,
                        # v1.5.0 §5.3：报告要写"授权：confirmed"，宿主就得先拿到
                        # 这个事实。不给的话它只能去读 attest 文件或自己猜。
                        "allowlist_source": allowlist["allowlist_source"],
                        "policy_hash": allowlist.get("policy_hash") or digest(c),
                    },
                    recommended_action="start",
                )
                if plan is not None:
                    if decision is not None:
                        store.event(
                            id,
                            {
                                "event": "route_decided",
                                "type": "route_decided",
                                "payload_version": 2,
                                "route_id": decision["route_id"],
                                "decision": plan["decision"],
                                "profile_id": plan["selected_profile"],
                                "reason": plan["reason"],
                                "candidate_count": len(plan["candidates"]),
                                "excluded_count": len(plan["layers"]["l1_excluded"])
                                + len(plan["layers"]["l2_excluded"]),
                            },
                        )
                    # 只有真正走过路由的项目才新增响应字段：没有 routing 段的
                    # 配置保持 v1.0.0 的响应形状（§19.1）。
                    result.update(
                        route_id=decision["route_id"],
                        route_reason=plan["reason"],
                        route_decision=plan["decision"],
                        routing_digest=plan["routing_digest"],
                        snapshot_digest=plan["snapshot_digest"],
                    )
                store.finish(key, result)
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


#: How long the supervisor waits to take over the lifecycle locks from `start`.
#: `start` releases them right after the supervisor is observed, so this only has
#: to cover process start-up; it bounds the wait so a genuinely busy workspace
#: still fails instead of hanging forever.
SUPERVISOR_LOCK_WAIT = 30.0

#: How long a persisted dispatch intent may stay unfinished before `reconcile`
#: closes it out. It must exceed the supervisor handshake plus the spawn window,
#: otherwise a normal launch would be mistaken for an abandoned one (§7.4-4).
INTENT_GRACE_SECONDS = SUPERVISOR_LOCK_WAIT * 2

#: Bounded re-probe window after a writer-lock conflict (§7.4-2).
#:
#: The winner writes its intent *after* taking the writer lock, so a single
#: re-read can still land in the microseconds between "lock acquired" and
#: "intent persisted". Polling closes that gap; it cannot invent a success,
#: because only a row whose hash matches *this* request (same id, same action,
#: same payload) ever produces a replay. A different request keeps probing into
#: nothing and still receives ``TASK_BUSY``.
REPLAY_RECHECK_SECONDS = 1.0
REPLAY_RECHECK_INTERVAL = 0.02


def slots(store, config: dict, exclude: str | None = None) -> dict:
    """Read-only concurrency snapshot; **not** an admission decision (§10.2).

    Active runs are counted **globally** (every project under one ``ZOE_HOME``),
    while the limit comes from the **dispatching project's** own configuration.
    Those are two different facts and are reported as two fields.

    The authoritative admission check still happens inside ``start``'s critical
    section; two callers can read the same ``free`` value and one of them will
    still be refused.
    """
    limit = int(config.get("max_active_tasks", 1))
    active = sorted(
        other["task_id"]
        for other in store.tasks()
        if other["task_id"] != exclude and other.get("state") in ("RUNNING", "PAUSING")
    )
    return {
        "limit": limit,
        "active_global": len(active),
        "free": max(0, limit - len(active)),
        "limit_source": "project_config",
        "active_tasks": active,
        "note": "snapshot, not admission: `start` re-checks under its critical section",
    }


def _collect_abandoned_intents(store, t: dict) -> bool:
    """Close out dispatch intents that never became a run (§7.4-4).

    Two conditions, both required: the task is not ``RUNNING``/``PAUSING`` (so
    the intent cannot belong to a live launch) and the intent is older than
    :data:`INTENT_GRACE_SECONDS` (so the ``start`` window between "intent
    written" and "run recorded" is never touched). Only the operations row is
    written — nothing is re-spawned, and a later ``finish()`` may still overwrite
    it if the original request really did complete.
    """
    if t.get("state") in ("RUNNING", "PAUSING"):
        return False
    now = time.time()
    collected = False
    for key, data in store.operation_intents(t["task_id"]):
        created = data.get("created_at") or t.get("updated_at") or t.get("created_at") or 0.0
        if now - float(created) <= INTENT_GRACE_SECONDS:
            continue
        store.finish(
            key,
            {
                "status": "failed",
                "accepted": False,
                "completed": True,
                "task_id": t["task_id"],
                "error": {
                    "code": "PROCESS_UNKNOWN",
                    "message": "Dispatch intent abandoned; reconcile before retrying",
                },
            },
        )
        collected = True
    return collected


def _is_container_run(r) -> bool:
    """Whether this run executed inside a container.

    Local runs also persist a ``container`` label (``local-<run>-<log>``): the
    field predates the local backend, so its presence alone says nothing about
    where the process ran. Only a non-``local-`` label (historical docker runs
    keep their ``zoe-`` prefix) denotes container execution — and those records
    stay readable after the backend was removed (§18.4 B2).
    """
    if r.get("execution_backend") == "docker" or r.get("backend") == "docker":
        return True
    name = r.get("container")
    return isinstance(name, str) and bool(name) and not name.startswith("local-")


def reconcile(store, t, backend=None, *, writer_held=False):
    """Reconcile a task's current run against the persisted process-tree facts.

    Design §3.1: only when *both* the supervisor and the managed process tree are
    confirmed gone may the run be closed out. A live tree with a dead supervisor
    is ``PROCESS_UNKNOWN`` (never auto-released). Uses persisted
    ``pid/start_token/group_token``; never depends on in-memory backend state.

    ``writer_held`` 表示调用方**已经持有**这个任务的 writer 锁（``start`` 就是
    这样调用它的）。``flock`` 对同一文件的第二个 fd 不可重入，此时再探一次必然
    ``SLOT_BUSY``，于是"崩溃后 start 恢复"这条路径会被自己的锁判成
    ``PROCESS_UNKNOWN``——那不是"进程还在"，是探测方法错了。
    """
    _collect_abandoned_intents(store, t)
    r = store.get_run(t.get("current_run_id")) if t.get("current_run_id") else None
    if not r:
        return t
    if r.get("status") == "exited":
        if t.get("stop_requested"):
            t.update(
                state="CANCELLED" if t["stop_requested"] == "cancel" else "PAUSED",
                stop_requested=None,
                block_code=None,
            )
            return store.save(t, "stop_reconciled")
        return t

    # 1. Verify the supervisor's own liveness/identity (never signals the target).
    pid = r.get("pid")
    start_token = r.get("start_token") or r.get("identity")
    try:
        supervisor_alive = alive(pid, start_token)
    except Error:
        t.update(state="BLOCKED", block_code="PROCESS_UNKNOWN")
        return store.save(t, "identity_unknown")

    # 2. An unconfirmed dispatch window is never auto-closed.
    if r.get("dispatch_state") in ("spawn_intent", "spawn_observed") and supervisor_alive is False:
        t.update(state="BLOCKED", block_code="PROCESS_UNKNOWN")
        return store.save(t, "unconfirmed_dispatch")

    if supervisor_alive:
        return t

    # 3. Supervisor is dead. A historical docker run can no longer be observed:
    #    the container backend left the product scope in v0.5.0, so "cannot tell"
    #    is the only honest answer (§18.4). It is closed out by an explicit
    #    `task recover --confirm-stop`, never auto-released.
    if _is_container_run(r):
        t.update(state="BLOCKED", block_code="PROCESS_UNKNOWN")
        return store.save(t, "legacy_container_unknown")
    if r.get("group_token"):
        plat = get_platform()
        from src._platform.base import ProcessHandle

        handle = ProcessHandle(
            pid=pid,
            start_token=start_token,
            group_token=r.get("group_token"),
            popen=None,
            platform=plat,
        )
        tree = plat.tree_alive(handle)
        if tree is None:
            t.update(state="BLOCKED", block_code="PROCESS_UNKNOWN")
            return store.save(t, "tree_unknown")
        if tree:
            t.update(state="BLOCKED", block_code="PROCESS_UNKNOWN")
            return store.save(t, "orphan_writer")

    # 4. The writer lock must be acquirable before close-out — unless this very
    #    call already holds it (`start`), in which case the probe is meaningless
    #    and always fails.
    if not writer_held:
        try:
            with store.lock(f"tasks/{t['task_id']}/writer", False):
                pass
        except Error as exc:
            # 只有"锁被占用"才是 PROCESS_UNKNOWN 的证据；锁文件本身打不开
            # （权限/磁盘）是环境故障，不能让调用方误判成"进程还在跑"。
            if exc.code not in ("SLOT_BUSY", "TASK_BUSY"):
                raise
            t.update(state="BLOCKED", block_code="PROCESS_UNKNOWN")
            return store.save(t, "writer_held")

    r["status"] = "exited"
    r["interrupted"] = True
    r["reconciled_at"] = time.time()
    store.run(r)
    stop = t.get("stop_requested")
    t.update(
        state=("CANCELLED" if stop == "cancel" else "PAUSED") if stop else "BLOCKED",
        # 不要覆盖流水线已经写下的阻塞原因：RUN_INTERRUPTED 只描述"进程没了"，
        # 真正的失败码（WORKTREE_*, CHECK_FAILED, POLICY_DENIED, …）更有诊断价值。
        block_code=None if stop else (t.get("block_code") or "RUN_INTERRUPTED"),
        stop_requested=None,
    )
    return store.save(t, "reconciled")


def create_backend_for(config):
    from src._backend import create_backend

    return create_backend(config)


def _stop_tree_from_run(r) -> None:
    """Stop a run's process tree using persisted tokens (platform layer)."""
    from src._platform.base import ProcessHandle

    plat = get_platform()
    handle = ProcessHandle(
        pid=r.get("pid"),
        start_token=r.get("start_token") or r.get("identity"),
        group_token=r.get("group_token"),
        popen=None,
        platform=plat,
    )
    result = plat.stop_tree(handle, timeout=10)
    require(result["remaining"] == 0, "PROCESS_UNKNOWN", "Process tree still running")


@contextlib.contextmanager
def lifecycle_locks(store, task_id: str, control: bool = False, wait: float | None = None):
    """Hold the lifecycle locks (01 §5.1) in the 01 §5.2 order.

    ``maintenance`` SH, then the task's ``writer`` EX, then optionally the
    ``control`` short-operation lock. Taking the lifecycle locks *before* control
    is what keeps this from deadlocking against ``state migrate``, which takes
    ``maintenance`` EX and then control.

    ``wait=None`` (``start``) is fail-fast: a held writer lock means another
    writer owns this workspace, so return ``TASK_BUSY`` immediately. A bounded
    ``wait`` is required by the supervisor, which is spawned *while* ``start``
    still holds the lock it is about to take over.
    """
    plat = get_platform()
    writer_path = store.lock_path(f"tasks/{task_id}/writer")
    maintenance = plat.lock_shared(store.lock_path("maintenance"))
    try:
        try:
            if wait is None:
                writer = plat.lock_exclusive(writer_path, blocking=False)
            else:
                writer = plat.lock_exclusive(writer_path, blocking=True, timeout=wait)
        except Error as exc:
            # 只把"锁被占用"翻译成 TASK_BUSY。锁路径打不开（权限、只读挂载、
            # 磁盘故障）报的是 PROCESS_UNKNOWN；把它改写成"有别的写者占着"
            # 会让排障方向整个错掉。
            if exc.code not in ("SLOT_BUSY", "TASK_BUSY"):
                raise
            raise Error("TASK_BUSY", "Task writer lock is held") from None
        try:
            if control:
                with store.lock():
                    yield
            else:
                yield
        finally:
            plat.unlock(writer)
    finally:
        plat.unlock(maintenance)


def release_run_tree(store, run_id: str) -> None:
    """Best-effort stop of a run's process tree.

    Called while the supervisor still holds the writer lock, so the lock is never
    dropped while a writer could still be modifying the workspace. A confirmed
    clean exit makes this a no-op.
    """
    r = store.get_run(run_id)
    if not r or not r.get("group_token"):
        return
    from src._platform.base import ProcessHandle

    plat = get_platform()
    handle = ProcessHandle(
        pid=r.get("pid"),
        start_token=r.get("start_token") or r.get("identity"),
        group_token=r["group_token"],
        popen=None,
        platform=plat,
    )
    try:
        if plat.tree_alive(handle):
            plat.stop_tree(handle, timeout=10)
    except Error:
        # An unverifiable tree is left to `reconcile`, which reports
        # PROCESS_UNKNOWN rather than pretending it exited.
        pass


def _run_uncertain(r, backend) -> bool:
    """Whether a run's exit state is uncertain after the pipeline finishes.

    A historical docker run is unobservable now that the container backend left
    the product scope, so it is reported as uncertain rather than "exited"
    (§18.4). For local-process, the persisted tree tokens decide: a live tree
    (or an undeterminable one) is uncertain.
    """
    # 一个还没走到 launch 的 run 不可能留下孤儿写者（例如 RETRY_EXHAUSTED 在
    # 启动 Agent 之前就失败）。此时 group_token 还指向 supervisor 自己，若照常
    # 探测，"监督进程活着"会被误判成"写者未知"，把真实失败码盖成 PROCESS_UNKNOWN。
    # ``create_requested``/``start_requested`` 是 ``_do_launch`` 之前写下的阶段：
    # 它们只说明"打算启动"，不能当作"已经有进程树"。
    if r.get("launch_stage") in (None, "", "create_requested", "start_requested"):
        return False
    if _is_container_run(r):
        return True
    # local-process: tree observation.
    plat = get_platform()
    if r.get("group_token"):
        from src._platform.base import ProcessHandle

        handle = ProcessHandle(
            pid=r.get("pid"),
            start_token=r.get("start_token") or r.get("identity"),
            group_token=r.get("group_token"),
            popen=None,
            platform=plat,
        )
        try:
            tree = plat.tree_alive(handle)
        except Error:
            return True
        if tree is None:
            return True
        return tree
    # No tree token and no container: unconfirmed launch windows are uncertain.
    return r.get("dispatch_state") in ("spawn_intent", "spawn_observed")


def _last_cli_version(store: Store, t: dict) -> str | None:
    """Reuse the last measured CLI version when this ``start`` does not probe.

    publish/ci/acceptance 阶段不跑 doctor。若此时把版本记成 ``None``，同一个
    组合每次续跑都会算出新的 ``combination_id``，历史统计会被拆成互不相关的桶。
    """
    for run in reversed(store.runs(t["task_id"])):
        version = run.get("cli_version") or (run.get("frozen_execution") or {}).get(
            "cli_version_effective"
        )
        if version:
            return version
    return None


def _busy(store: Store, id: str, error: Error) -> Error:
    """Turn a bare ``TASK_BUSY`` into a read-only fact sheet (§7.4-5).

    ``details`` reuses the shape v0.5.0 introduced for ``REMOTE_DRIFT``. A host
    that only branches on ``code`` is unaffected; a host that would otherwise
    guess whether the task is running can read the snapshot instead — and is
    told to refresh rather than retry blindly.
    """
    try:
        t = store.task(id)
    except Error:
        return error
    run = store.get_run(t.get("current_run_id")) if t.get("current_run_id") else None
    return Error(
        "TASK_BUSY",
        "Task writer lock is held",
        details={
            "task_id": id,
            "state": t.get("state"),
            "phase": t.get("phase"),
            "run_id": (run or {}).get("run_id") or t.get("current_run_id"),
            "dispatch_state": (run or {}).get("dispatch_state"),
            "hint": "read-only snapshot; use `task status --refresh`",
        },
    )


def _replay(store: Store, id: str, req: dict[str, Any], prior, status) -> dict[str, Any]:
    """Answer a repeated request without dispatching or writing anything (§7.4-3).

    A finished record is returned byte-for-byte. A still-open intent is reported
    as ``dispatch_pending``: accepted, but *not* proven to be running. In
    particular ``run_id`` is ``null`` — run records carry no ``request_id``, so
    there is no way to prove that the task's current run belongs to this
    request, and borrowing it would report someone else's execution.
    """
    _orchestration.require_owner(store, store.task(id)["project_id"], req)
    if prior:
        return prior
    t = store.task(id)
    return _reply(
        req,
        {
            "status": "accepted",
            "accepted": True,
            "completed": False,
            "dispatch_pending": True,
            "run_id": None,
            "task_id": id,
            "task_snapshot": {
                "state": t.get("state"),
                "phase": t.get("phase"),
                "row_version": t.get("row_version"),
                "current_run_id": t.get("current_run_id"),
            },
        },
    )


def start(store: Store, id: str, req: dict[str, Any], action: str = "start") -> dict[str, Any]:
    """Start (or resume) a task's supervised run. Returns before the worker finishes.

    A repeated request is replayed *before* taking the writer lock, and once
    more if the lock turns out to be held: checking only before the lock leaves
    a window in which two same-id requests both miss and the second is reported
    as ``TASK_BUSY`` (§7.4-2). A different ``request_id`` never borrows this
    path — it goes through every authorisation, owner, version, spec and quota
    check, and gets ``TASK_BUSY`` with ``details`` if a writer holds the lock.
    """
    key, prior, status = store.operation_prior(req, action, id)  # ① 无锁前置查询
    if prior or status == "intent":
        return _replay(store, id, req, prior, status)
    try:
        return _start_locked(store, id, req, action)
    except Error as error:
        if error.code != "TASK_BUSY":
            raise
        # ② 锁失败补查：有界轮询，因为赢家是在拿到锁之后才写入 intent 的。
        deadline = time.monotonic() + REPLAY_RECHECK_SECONDS
        while True:
            key, prior, status = store.operation_prior(req, action, id)
            if prior or status == "intent":
                return _replay(store, id, req, prior, status)
            if time.monotonic() >= deadline:
                break
            time.sleep(REPLAY_RECHECK_INTERVAL)
        raise _busy(store, id, error) from None  # ③ 真冲突：保持原错误


def _start_locked(store: Store, id: str, req: dict[str, Any], action: str) -> dict[str, Any]:
    # Lifecycle and control locks in 01 §5.2 order; see `lifecycle_locks`.
    with lifecycle_locks(store, id, control=True):
        # Reuse successful request before checking optimistic task version.
        key, prior = store.operation(req, action, id)
        if prior:
            return prior
        # spawn 失败时要能判断"预约走到哪一步了"；哨兵让 except 分支不必依赖
        # 局部变量是否已赋值。
        rid = None
        r = None
        try:
            # `lifecycle_locks` 已经持有 writer EX：探测必须让位，否则一定自冲突。
            t = reconcile(store, store.task(id), writer_held=True)
            c = t["config"]
            # 历史 docker 任务只读：拒绝续跑，不换后端接着干（§18.4）。
            refuse_docker_backend(t["config"], where="task")
            authorize(c, req, "execute")
            # §10.10: a non-owner host may read but never dispatch.
            _orchestration.require_owner(store, t["project_id"], req)
            # v1.6：额度耗尽的项目级闸门。放在这里而不是 `doctor` 之后，因为
            # doctor 会真的探测执行器（起子进程、耗时间），而"额度没了"这个结论
            # 与探测结果无关。`resume` 走同一条路径，因此同样被拒——恢复一次派发
            # 就是花掉一次本就不存在的额度；解除必须显式 `project quota clear`。
            gate = _quota.active_gate(store, t["project_id"])
            if gate is not None:
                raise Error(
                    "QUOTA_EXHAUSTED",
                    "Dispatch is gated: this project ran out of quota and the gate has not "
                    "been cleared. Wait for the reset, then run "
                    "`project quota clear --project <id> --why ...`.",
                    details=_quota.details(gate),
                )
            current = store.config(t["project_id"])
            # v1.5.0 M8：先判"这份冻结配置还能不能由本版本执行"，再判"项目策略
            # 有没有漂移"。旧配置可能一字未变却已不符合新规则（两侧摘要相等），
            # 那时必须报具体字段，而不是 POLICY_CHANGED。
            check_execution_contract(c, base_dir=store.home / "projects")
            require(
                _profiles.policy_digest(c) == digest(current),
                "POLICY_CHANGED",
                "Project policy changed; review task before executing",
            )
            # 授权可能在恢复前被撤销：新尝试一律按当前配置重新确认。
            # 传 profile_id：确认范围可以比白名单更窄，收窄要生效。
            _require_attested(store, t["project_id"], current, t.get("agent_profile_id"))
            require(
                t["row_version"] == req.get("expected_version"),
                "STATE_CONFLICT",
                "Refresh expected_version",
            )
            if t.get("route_id"):
                # R9：决策文件先落盘、任务后写（create 保证的顺序）。启动时再校验
                # 一次全文完整性，否则"记录被改过"会以"程序在跑，日志在撒谎"的
                # 形式出现，而不是以一个明确的拒绝出现（§15.5）。
                _routing.read_decision(store, t)
            require(
                t["state"] in ("READY", "PAUSED", "BLOCKED"), "STATE_CONFLICT", "Task not resumable"
            )
            # v1.7：上游必须**已闭环**（DONE + accepted + 逐条判定满足），不是"任务存在"。
            # R-27 那次能写下"契约已冻结"，是因为依赖从未声明——runtime 无从查，
            # 于是 worker 的结论成了唯一信息源。查在 start 而不是 prepare：上游任务
            # 通常在 spec 写完之后才建，prepare 阶段判定只会制造假阻塞。
            dependency = _coverage.dependency_state(store, t)
            if not dependency["satisfied"]:
                raise Error(
                    "DEPENDENCY_NOT_MET",
                    "上游需求未闭环，现在派发等于让下游为没做完的东西背书："
                    + "、".join(item["marker"] for item in dependency["blocking"]),
                    details=dependency,
                )
            # v1.6：首次派发前确认冻结基线仍在目标分支历史里。rebase / 强推之后再开
            # worktree，worker 会在一棵已被丢弃的树上干活，最后以 VALIDATION_FAILED
            # 收场——时间白花，现象还像代码问题。
            # 只在 workspace 还没建时校验：resume 继续的是已冻结的 worktree，此刻拦住
            # 它只会把正在推进的任务卡死，而合并不了的问题由 `project diff` 与人工处理。
            if not Path(t["workspace"]).exists():
                reachable = _worktree.base_reachable(
                    Path(c["repo"]), t.get("base_sha"), c.get("base_ref", "HEAD")
                )
                if reachable is False:
                    raise Error(
                        "BASE_DRIFT",
                        "task baseline is no longer in the target branch history",
                        details={
                            "base_sha": t.get("base_sha"),
                            "base_ref": c.get("base_ref", "HEAD"),
                            "hint": (
                                "确认目标分支现状后重冻结基线（task revise / 换基线重派），"
                                "不要强推覆盖；已产出的候选需要重新验证"
                            ),
                        },
                    )
            require(
                t.get("block_code") != "PROCESS_UNKNOWN",
                "PROCESS_UNKNOWN",
                "Resolve old writer first",
            )
            # 13 §5 case 5: resuming with a pending question would re-run the agent
            # without the answer it is waiting for. Answer first (`task answer`),
            # and use `revise` when the answer changes the acceptance criteria.
            require(
                t.get("block_code") != "INPUT_REQUIRED",
                "STATE_CONFLICT",
                "Answer the pending questions with `task answer` before resuming",
            )
            require(
                req.get("strategy", "auto") in ("auto", "workspace"),
                "CAPABILITY_UNSUPPORTED",
                "Native sessions are not implemented; use workspace",
            )
            # 准入策略：默认放行未验证执行器，把"未验证"写进响应与 run 记录；
            # 只有 require_real_e2e=true 的项目才恢复"无真实 e2e 记录即拒绝"。
            binding = _profiles.require_runnable(
                _binding(t), require_real_e2e=bool(c.get("require_real_e2e"))
            )
            # 组级准入是唯一入口（TGD §7.6）：runner、`group dispatch` 和这里的
            # `task start/resume` 走同一个函数，没有"我从另一条路进来所以不排队"。
            _group_admission(store, t)
            report: dict[str, Any] = {}
            if t["phase"] in ("prepare", "implement", "verify"):
                report = doctor(c, binding.profile_id)
                require(
                    report["ready"],
                    "ENVIRONMENT_ERROR",
                    "doctor failed; verify the execution environment and profile",
                )
            # Refuse new dispatch when disk is near-full; keep existing work intact.
            check_disk_space(store.home)
            # 跨任务并发上限：默认 1，配置 raise 后允许 N 个任务同时活跃。
            # 单任务"唯一写者"不变量不受影响（每个任务各有 writer 锁与 worktree）。
            # 与 `orchestration resume` / `group plan` 的 `slots` 共用同一统计，
            # 但**准入仍在这里**：快照只是口径，不是许可（§10.2）。
            snapshot = slots(store, c, exclude=id)
            require(
                snapshot["active_global"] < snapshot["limit"],
                "SLOT_BUSY",
                f"{snapshot['active_global']}/{snapshot['limit']} active runs; reconcile first: "
                + ", ".join(snapshot["active_tasks"]),
            )
            # The lifecycle locks are already held by `lifecycle_locks`, so the
            # spawn below happens under them. `start` releases them as soon as the
            # supervisor is observed; the supervisor then re-acquires them and
            # holds them for the run's whole lifetime (01 §5.1).
            plat = get_platform()
            attempt_id = uuid.uuid4().hex
            rid = "run-" + uuid.uuid4().hex[:16]
            # §12.2：冻结实测组合。`create` 只能用 pin 版本，实测值在这里补，
            # 并重算 combination_id —— pin 与实测不一致必须体现为两个组合。
            resolution = (report.get("resolution") or {}) if report else {}
            measured_version = resolution.get("version") or _last_cli_version(store, t)
            endpoint = t.get("endpoint") or _routing.endpoint_of(c, binding.profile)
            selected = t.get("selected_execution") or _routing.selected_execution(
                binding=binding,
                endpoint=endpoint,
                cli_version=binding.profile.get("expected_version"),
                policy_revision=digest(c)[:16],
                routing=_routing.routing_digest(c),
            )
            frozen = _routing.frozen_execution(
                selected,
                binding=binding,
                endpoint=endpoint,
                measured_version=measured_version,
                resolved_path=resolution.get("path"),
                resolution_source=resolution.get("source"),
            )
            # §12.9.1：基线必须在 spawn **之前**落盘，否则隔离时无法区分
            # "本次 run 的改动"与"用户原有的未提交改动"。
            workspace_baseline = _worktree.baseline(t)
            spec_document = (t.get("spec") or {}).get("document") or {}
            r = {
                "run_id": rid,
                "task_id": id,
                "status": "reserved",
                "pid": None,
                "identity": None,
                "start_token": None,
                "group_token": None,
                "container": None,
                "dispatch_state": "spawn_intent",
                "attempt_id": attempt_id,
                "lock_owner": {
                    "run_id": rid,
                    "attempt_id": attempt_id,
                    "state": "owned",
                    "acquired_at": time.time(),
                },
                "protocol_version": 4,
                "session_mode": "new",
                "started_at": time.time(),
                "record_version": 2,
                "adapter_id": binding.adapter_id,
                "adapter_backend": binding.backend,
                "adapter_revision": binding.entry.protocol_revision,
                "profile_id": binding.profile_id,
                "profile_hash": binding.profile_hash,
                "image_digest": binding.profile.get("image"),
                "expected_version": binding.profile.get("expected_version"),
                "capability": binding.capability,
                "unverified": binding.unverified,
                "trust_level": binding.trust_level,
                "verification_source": (
                    binding.verification.source if binding.verification else None
                ),
                "assignment_revision": t.get("assignment_revision", 1),
                "host": (t.get("source") or {}).get("host")
                if isinstance(t.get("source"), dict)
                else None,
                # v1.2 §12.1 / §14.1：run 记录自带"跑的是哪个组合"，历史统计才
                # 不需要靠"当时配置长什么样"反推。
                "frozen_execution": frozen,
                "combination_id": frozen["combination_id"],
                "combination_verified": False,
                "cli_version": frozen["cli_version_effective"],
                "requested_model": binding.profile.get("model"),
                "model_unresolved": frozen["model_unresolved"],
                "endpoint": endpoint,
                "purpose": t.get("purpose") or "task",
                "task_type": t.get("task_type") or "unknown",
                "spec_scale": _routing.spec_scale(
                    len(spec_document.get("acceptance_criteria") or [])
                ),
                "workspace_baseline": workspace_baseline,
                "spend": None,
                "attribution": None,
            }
            t.update(
                state="RUNNING",
                current_run_id=rid,
                stop_requested=None,
                block_code=None,
                block_details=None,
                last_progress_at=time.time(),
            )
            result = _reply(
                req,
                {
                    "status": "succeeded",
                    "accepted": True,
                    "completed": False,
                    "task_id": id,
                    "run_id": rid,
                    "attempt_id": attempt_id,
                    "dispatch_state": "spawn_intent",
                    "row_version": t["row_version"] + 1,
                    "permission": binding.permission,
                    "capability": binding.capability,
                    "unverified": binding.unverified,
                    "trust_level": binding.trust_level,
                    "verification_source": r["verification_source"],
                },
            )
            # 未验证派发是一等事件：宿主可据此在汇报里标注可信度，而不必猜。
            store.save(
                t,
                "unverified_dispatch" if binding.unverified else "start_reserved",
                operation=(key, result),
                run=r,
                payload={
                    "capability": binding.capability,
                    "trust_level": binding.trust_level,
                    "verification_source": r["verification_source"],
                },
            )
            # 组合是否变了由 `run_pipeline` 在**真正冻结**时判定并记事件：
            # 一次 start 若从未启动成功，就没有"用过的组合"，不该留下变更记录。
            # SPAWN_INTENT is persisted above (strictly before spawn, W3/W4).
            log = store.path(t) / "runs" / rid / "supervisor.log"
            log.parent.mkdir(parents=True)
            argv = [
                sys.executable,
                "-m",
                "src",
                "--home",
                str(store.home),
                "internal",
                rid,
                attempt_id,
            ]
            child_env = supervisor_env()
            require_supervisor_importable(child_env, store.home, log)
            with open(log, "ab") as out:
                # `isolate_tree=False`: the supervisor must outlive `start`,
                # so it must not be put in a kill-on-close job. It creates its
                # own job for the agent tree instead.
                child = plat.spawn(
                    argv, cwd=str(store.home), env=child_env, stdout=out, isolate_tree=False
                )
            r["pid"] = child.pid
            r["identity"] = child.start_token
            r["start_token"] = child.start_token
            r["group_token"] = child.group_token
            r["status"] = "starting"
            r["dispatch_state"] = "spawn_observed"
            store.run(r)
            result = _reply(
                req,
                {
                    "status": "succeeded",
                    "accepted": True,
                    "completed": False,
                    "task_id": id,
                    "run_id": rid,
                    "attempt_id": attempt_id,
                    "dispatch_state": "spawn_observed",
                    "row_version": t["row_version"],
                    "adapter_id": binding.adapter_id,
                    "adapter_backend": binding.backend,
                    "permission": binding.permission,
                    "capability": binding.capability,
                    "unverified": binding.unverified,
                    "trust_level": binding.trust_level,
                    "verification_source": r.get("verification_source"),
                },
            )
            if t.get("route_id"):
                # 只有走过路由的任务才新增字段：无 routing 段的旧配置保持
                # v1.0.0 的响应形状（§19.1）。
                result.update(
                    route_id=t["route_id"],
                    combination_id=frozen["combination_id"],
                    cli_version_effective=frozen["cli_version_effective"],
                )
            store.finish(key, result)
            return result
        except Exception as e:
            # spawn 明确失败不等于"执行域未知"：这里能确定没有 agent 在跑，就该停在
            # 一个需要人处置的**确定**状态。否则任务留在 RUNNING + spawn_intent/
            # pid=None，下一次 reconcile 只能报 PROCESS_UNKNOWN（`start` 会拒绝
            # 它），而 `slots()` 仍按 state=RUNNING 计数，这个并发槽位会一直被占着，
            # 只能靠人工 `task recover --confirm-stop` 释放。
            # 已在 ``lifecycle_locks`` 的 control 锁内，不能再取一次（同文件 flock）。
            try:
                current = store.task(id)
                if (
                    rid
                    and r is not None
                    and current.get("current_run_id") == rid
                    and current.get("state") == "RUNNING"
                    and not r.get("pid")
                    and r.get("dispatch_state") == "spawn_intent"
                ):
                    current.update(
                        state="BLOCKED",
                        block_code=getattr(e, "code", "ENVIRONMENT_ERROR"),
                        last_error=str(e),
                        block_details=getattr(e, "details", None),
                    )
                    r["status"] = "exited"
                    r["ended_at"] = time.time()
                    store.save(current, "spawn_failed", run=r)
            except (Error, OSError):
                # 回滚失败不得顶替原始错误：原始错误才是用户要看的。
                pass
            store.finish(
                key,
                {
                    "status": "failed",
                    "error": {"code": getattr(e, "code", "INTERNAL_ERROR"), "message": str(e)},
                },
            )
            raise


def stop(store: Store, id: str, req: dict[str, Any], cancel: bool = False) -> dict[str, Any]:
    """Request a pause or cancel; confirms the writer stopped before finalizing."""
    with store.lock():
        key, prior = store.operation(req, "cancel" if cancel else "pause", id)
        if prior:
            return prior
        t = store.task(id)
        authorize(t["config"], req, "execute")
        require(
            t["row_version"] == req.get("expected_version"),
            "STATE_CONFLICT",
            "Refresh expected_version",
        )
        require(t["state"] not in TERMINAL, "STATE_CONFLICT", "Terminal task")
        r = store.get_run(t.get("current_run_id")) if t.get("current_run_id") else None
        requested = "cancel" if cancel else "pause"
        if r and r.get("status") != "exited":
            start_token = r.get("start_token") or r.get("identity")
            if not alive(r.get("pid"), start_token):
                require(
                    r.get("dispatch_state") not in ("spawn_intent", "spawn_observed"),
                    "PROCESS_UNKNOWN",
                    "Unconfirmed launch; reconcile process tree first",
                )
                if _is_container_run(r):
                    # A legacy docker run cannot be stopped or observed; the run
                    # is closed out by `task recover --confirm-stop` instead of
                    # pretending a stop happened (§18.4).
                    raise Error(
                        "PROCESS_UNKNOWN",
                        "Legacy container run cannot be observed; "
                        "use `task recover --confirm-stop`",
                    )
                _stop_tree_from_run(r)
                r.update(status="exited", ended_at=time.time())
                store.run(r)
                t.update(state="CANCELLED" if cancel else "PAUSED", stop_requested=None)
            else:
                t.update(state="PAUSING", stop_requested=requested)
        else:
            t.update(state="CANCELLED" if cancel else "PAUSED", stop_requested=None)
        result = _reply(
            req,
            {
                "status": "succeeded",
                "task_id": id,
                "state": t["state"],
                "row_version": t["row_version"] + 1,
            },
            recommended_action="resume" if not cancel else None,
        )
        store.save(t, requested, operation=(key, result))
        return result


def revise(store: Store, id: str, req: dict[str, Any]) -> dict[str, Any]:
    """Save a new spec revision and invalidate prior validation/acceptance."""
    with store.lock():
        key, prior = store.operation(req, "revise", id)
        if prior:
            return prior
        t = store.task(id)
        authorize(t["config"], req, "execute")
        _orchestration.require_owner(store, t["project_id"], req)
        _require_member_mutable(store, t, "revise")
        require(
            t["row_version"] == req.get("expected_version"),
            "STATE_CONFLICT",
            "Refresh expected_version",
        )
        require(
            t["state"] in ("READY", "PAUSED", "BLOCKED", "REVIEW_READY"),
            "STATE_CONFLICT",
            "Pause first",
        )
        r = store.get_run(t.get("current_run_id")) if t.get("current_run_id") else None
        require(not r or r["status"] == "exited", "PROCESS_UNKNOWN", "Resolve writer first")
        spec = _contracts.load_spec(req["spec_file"], t["config"])
        # 修订可以收窄也可以放宽（在项目范围内），范围必须跟着新 spec 重算。
        task_scope, scope_source = _resolve_task_scope(spec.get("document"), t["config"])
        # v1.6：如果这次修订**只**改了 `allowed_paths`，实现要求一个字没变，那么
        # 工作区里那份代码仍然是有效的产物——应该按新范围重新判定它（verify），
        # 而不是再付一次 agent 费用把同样的内容重写一遍。checks 不过会走
        # `VALIDATION_FAILED`，那一步再把 phase 拨回 implement，代价只是一次本地
        # 检查；反过来（无条件回到 implement）代价是一次付费尝试加一次覆盖写。
        scope_only = _scope_only_change(store, t, spec.get("document"))
        reuse_tree = scope_only and Path(t["workspace"]).exists()
        t["spec_revision"] += 1
        atomic(store.path(t) / f"spec-{t['spec_revision']}.json", spec)
        t.update(
            spec_hash=digest(spec["text"].encode()),
            spec_contract_version=spec["contract_version"],
            task_scope=task_scope,
            scope_source=scope_source,
            phase=(
                "verify"
                if reuse_tree
                else ("implement" if Path(t["workspace"]).exists() else "prepare")
            ),
            state="READY",
            validated_sha=None,
            validated_fingerprint=None,
            current_evidence_id=None,
            empty_candidate=False,
            acceptance=None,
            block_code=None,
            # 新 spec 本身也是一种修复：上一轮的策略类阻塞判定不再适用，
            # 否则"缩小 spec 范围"这条正确解法会被恢复前判定挡住。
            last_block=None,
        )
        result = _reply(
            req,
            {"status": "succeeded", "task_id": id, "row_version": t["row_version"] + 1},
            recommended_action="start",
        )
        store.save(t, "spec_revised", operation=(key, result))
        return result


_PHASE_ORDER = ("prepare", "implement", "verify", "publish", "ci", "acceptance")


def handoff_remaining_work(t: dict) -> list:
    """交接清单里的"还没做完的事"（06 §8）。

    只由控制器状态推导，不接受 Agent 自述：当前阶段之后的阶段 + 当前阻塞/暂停的
    处理动作。REVIEW_READY 时唯一剩下的是人工验收。
    """
    state = t.get("state")
    phase = t.get("phase") or "prepare"
    index = _PHASE_ORDER.index(phase) if phase in _PHASE_ORDER else 1
    later = [f"{name} stage" for name in _PHASE_ORDER[index + 1 :]]
    if state == "COMPLETED":
        return []
    if state == "REVIEW_READY":
        return ["human acceptance"]
    if state == "BLOCKED":
        return [f"resolve {t.get('block_code') or 'blocker'}"] + later
    if state in ("PAUSED", "PAUSING"):
        return [f"resume {phase}"] + later
    return [f"finish {phase}"] + later


def validation_rows(store, t: dict, r) -> list[dict]:
    """Read ``runs/<run>/validation.json`` (a bare array, one row per check).

    The controller is the only writer, and the file is written *before* any
    check error is re-raised, so the diagnostics survive (§6.4.1).
    """
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


def handoff_failed_checks(store, t: dict, r: dict) -> list:
    """上一个 run 里没通过的验证项（06 §8），供接手方直接复用。

    只有 ``FAIL`` 是失败——包括被异常打断的行（超时/进程消失/输出超限）。
    ``NOT_RUN`` 表示"本次未声明执行"，既不是通过也不是失败，单列在
    :func:`handoff_not_run_checks` 并带上原因。
    """
    return [
        {
            "id": str(row.get("id")),
            "status": row.get("status"),
            "exit_code": row.get("exit_code"),
            # F3：接手方要能区分"测试失败"和"环境装不出来"，否则会把环境问题
            # 当成业务缺陷反复重写代码。
            "error_class": row.get("error_class")
            or error_class(
                (row.get("error") or {}).get("code")
                if isinstance(row.get("error"), dict)
                else None,
                row.get("exit_code"),
            ),
            **({"error": row["error"]} if isinstance(row.get("error"), dict) else {}),
        }
        for row in _evidence.failed_rows(validation_rows(store, t, r))
    ]


def handoff_not_run_checks(store, t: dict, r: dict) -> list:
    """本次明确未执行的验证项（``NOT_RUN``），带声明原因。"""
    return [
        {"id": str(row.get("id")), "reason": row.get("reason")}
        for row in _evidence.skipped_rows(validation_rows(store, t, r))
    ]


def _handoff_observation(store, t: dict, r: dict) -> dict | None:
    """最近一次 run 的静默/字节事实（只转述观测层，不推断卡死）。

    收尾快照的意义在于"接手方不必再抓 N 次日志"。这些数字观测层已经落盘，
    这里只挑与"停机前最后状态"相关的几项，不重新计算。
    """
    view = _progress.view(store, t, r)
    if not view:
        return None
    return {
        "quiet_seconds": view.get("quiet_seconds"),
        "log_bytes": view.get("bytes"),
        "process_state": view.get("process_state"),
        "observations_stale": view.get("observations_stale"),
        "observed_at": view.get("observed_at"),
    }


def _write_handoff(store, t: dict, r: dict) -> None:
    """Write the handoff snapshot for the run that just ended.

    Called from ``finally``: any failure here (deleted workspace, damaged git
    metadata) must not replace the failure that stopped the pipeline, so the
    caller guards it. The snapshot exists to be read by the next host, and a
    missing one silently downgrades ``reassign``'s staleness check.
    """
    atomic(
        store.path(t) / "runs" / r["run_id"] / "handoff.json",
        {
            "task": t["task_id"],
            "phase": t["phase"],
            "state": t["state"],
            "head": git(t["workspace"], "rev-parse", "HEAD"),
            "fingerprint": fingerprint(t),
            "status": git(t["workspace"], "status", "--porcelain"),
            "last_error": t.get("last_error"),
            "adapter_id": t.get("adapter_id"),
            "adapter_backend": t.get("adapter_backend"),
            "profile_id": t.get("agent_profile_id"),
            "assignment_revision": t.get("assignment_revision", 1),
            "attempts_used": t.get("attempts", 0),
            "spec_hash": t.get("spec_hash"),
            # 06 §8：接手方不必重跑一遍才知道卡在哪、还剩什么。
            "block_code": t.get("block_code"),
            # 接手方要能直接找到现场，而不是从 home 目录里猜 worktree 名。
            "worktree_path": str(t.get("workspace") or ""),
            "run_id": r["run_id"],
            "spec_revision": t.get("spec_revision"),
            "policy_revision": t.get("policy_revision"),
            # 停机前最后一次观测事实（静默/字节/进程态）：配额耗尽时这一份就是
            # "它到底停在哪"的全部答案，不必再读日志推断。
            "observation": _handoff_observation(store, t, r),
            # 项目级配额闸门（含 reset 时间与证据）。未发生过配额事故时为 None。
            "quota_gate": _quota.read_gate(store, t["project_id"]),
            "failed_checks": handoff_failed_checks(store, t, r),
            "not_run_checks": handoff_not_run_checks(store, t, r),
            "remaining_work": handoff_remaining_work(t),
        },
    )


def frozen_scope(store, t: dict, config: dict) -> tuple[int, list[str] | None]:
    """Return ``(semantics, declared_ids_or_None)`` from the frozen spec revision.

    One byte string is both re-checked against ``spec_hash`` and parsed, so
    "validate ``text``, act on ``document``" cannot split the two (§6.3). A
    revision without a semantics marker (v0.5.0 and earlier) is full: a scope
    written into its text is **ignored**, never silently honoured.
    """
    path = store.path(t) / f"spec-{t['spec_revision']}.json"
    require(path.is_file(), "SPEC_CHANGED", "Frozen spec is missing")
    try:
        frozen = read(path)
    except (OSError, ValueError):
        raise Error("SPEC_CHANGED", "Frozen spec is unreadable") from None
    text = frozen.get("text")
    require(isinstance(text, str), "SPEC_CHANGED", "Frozen spec text is missing")
    require(digest(text.encode()) == t["spec_hash"], "SPEC_CHANGED", "Frozen spec was modified")
    semantics = _contracts.frozen_semantics(frozen)
    _, document = _contracts.parse_spec(text, config)
    declared = (
        _contracts.verification_scope(document, config["checks"])
        if semantics == _contracts.SCOPE_SEMANTICS
        else None
    )
    return semantics, declared


#: F3: why a check did not pass. The four classes have different repair
#: owners, so "FAIL" alone is not an actionable record (TGD §5.4).
ERROR_CLASSES = ("test", "environment", "timeout", "policy")

_ENVIRONMENT_CODES = {
    "ENVIRONMENT_ERROR",
    "AGENT_AUTH_REQUIRED",
    "ENVIRONMENT_DRIFT",
    "COMMAND_FAILED",
    "RUNTIME_INCOMPATIBLE",
}
_POLICY_CODES = {"POLICY_DENIED", "SCOPE_VIOLATION", "PERMISSION_PROFILE_UNBOUND"}
_TIMEOUT_CODES = {"RUN_TIMEOUT", "STOP_REQUESTED"}

#: Exit codes a POSIX shell uses for "cannot execute" (127) and "not executable"
#: (126). They are environment failures, not test failures.
_ENVIRONMENT_EXIT_CODES = (126, 127)
_TIMEOUT_EXIT_CODE = 124


def error_class(code: str | None, exit_code: int | None = None) -> str | None:
    """Classify a check outcome so the report can name the right repair owner."""
    if code:
        if code in _ENVIRONMENT_CODES:
            return "environment"
        if code in _POLICY_CODES:
            return "policy"
        if code in _TIMEOUT_CODES:
            return "timeout"
        return "test"
    if exit_code is None:
        return None
    if exit_code in _ENVIRONMENT_EXIT_CODES:
        return "environment"
    if exit_code == _TIMEOUT_EXIT_CODE:
        return "timeout"
    return "test"


#: 需求文档里的复选框行。只认行首的 ``- [ ]`` / ``- [x]``：正文里出现的
#: "checkbox" 字样不能被当成进度（否则一条说明文字就能让门禁通过）。
_TICKED_LINE = re.compile(r"^\s*[-*+]\s*\[[xX]\]")


def _progress_ref_gaps(document, worktree) -> list[dict]:
    """v3 ``progress_refs``：声明的复选框必须已在候选树里被勾选。

    进度回写不是"合并之后的收尾动作"，而是这次交付的产物：清单没有跟着代码
    一起前进，就等于"做了但没人能看见"。把它放在 verify 阶段，是因为这里
    已经有一棵可判定的候选树。

    失败形态分开命名，因为处置不同：

    * ``doc_missing``   —— 文档不在候选树里（多半是没写进 ``allowed_paths``）；
    * ``marker_missing`` —— 找不到该 marker（改名了，或这条需求根本不存在）；
    * ``not_ticked``    —— 找到了但仍是 ``- [ ]``（真的没更新）。
    """
    refs = _contracts.progress_refs(document)
    if not refs:
        return []
    root = Path(worktree) if worktree else None
    gaps: list[dict] = []
    for ref in refs:
        doc = ref.get("doc")
        marker = ref.get("marker")
        acceptance_id = ref.get("acceptance_id")
        base = {
            "acceptance_id": acceptance_id,
            "doc": doc,
            "marker": marker,
        }
        if not root:
            gaps.append({**base, "reason": "no_workspace"})
            continue
        path = root / str(doc)
        if not path.is_file():
            gaps.append({**base, "reason": "doc_missing"})
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            gaps.append({**base, "reason": "doc_unreadable"})
            continue
        line = next((row for row in text.splitlines() if marker and marker in row), None)
        if line is None:
            gaps.append({**base, "reason": "marker_missing"})
        elif not _TICKED_LINE.match(line):
            gaps.append({**base, "reason": "not_ticked", "line": line.strip()[:120]})
    return gaps


def _persist_validation(store, t, r, results) -> None:
    """Write the check rows for this run.

    Hand-rolled ``STOP_REQUESTED`` and non-``Error`` exceptions leave
    ``run_checks`` immediately; without writing here, ``validation.json`` is
    missing and ``handoff.json`` then reports ``failed_checks: []`` — telling the
    next host "the last round had no failures" when it was actually interrupted.
    """
    atomic(store.path(t) / "runs" / r["run_id"] / "validation.json", results)


def run_checks(backend, store, t, r, checks, declared, marker):
    """Execute the declared checks, keeping every diagnostic (§6.4.1).

    A check that raises is recorded as ``FAIL`` with ``error.code`` and the run
    continues, so one timeout does not erase the other results. ``NOT_RUN`` is
    reserved for "not declared": failures must reach handoff and the report.
    ``STOP_REQUESTED`` is a user intent, not a check result, and still aborts.

    Every row also carries ``error_class`` (F3): ``test`` counts against the
    repair budget, ``environment`` does not, ``timeout`` is a FAIL, ``policy``
    blocks for a human decision.
    """
    selected = set(declared) if declared is not None else {check["id"] for check in checks}
    results, errors = [], []
    for check in checks:
        if check["id"] not in selected:
            results.append(
                {
                    "id": check["id"],
                    "status": _evidence.NOT_RUN,
                    "exit_code": None,
                    "fingerprint": marker,
                    "reason": "not in declared verification_scope",
                }
            )
            continue
        # 执行前复校（v2 §4-3）：`doctor` 说能跑，执行时也必须能跑。只拦
        # `unresolved`（裸名不在 PATH）。`shell` / `deferred` 一律放行——前者
        # 内部命令本来就不可静态解析，后者要等任务自己安装。
        resolved = resolve_check(
            check["argv"],
            cwd=(t or {}).get("workspace"),
            search_path=os.environ.get("PATH"),
        )
        if resolved["status"] == "unresolved":
            problem = Error(
                "ENVIRONMENT_ERROR",
                f"check {check['id']!r} command {check['argv'][0]!r} cannot be resolved: "
                f"{resolved['hint']}",
            )
            results.append(
                {
                    "id": check["id"],
                    "status": _evidence.FAIL,
                    "exit_code": None,
                    "fingerprint": marker,
                    "reason": "check command unresolved (not executed)",
                    "error_class": "environment",
                    "error": {"code": problem.code, "message": str(problem)},
                }
            )
            errors.append(problem)
            continue
        try:
            code = backend.execute(store, t, r, check["argv"], "check-" + check["id"])
        except Error as exc:
            if exc.code == "STOP_REQUESTED":
                # 用户中断不是 check 结果，但已收集的结果必须先落盘（见上）。
                _persist_validation(store, t, r, results)
                raise
            results.append(
                {
                    "id": check["id"],
                    "status": _evidence.FAIL,
                    "exit_code": None,
                    "fingerprint": marker,
                    "reason": "execution error",
                    "error_class": error_class(exc.code),
                    "error": {"code": exc.code, "message": str(exc)},
                }
            )
            errors.append(exc)
            continue
        except Exception:
            # 非结构化异常（OSError 等）同样不能让已收集的诊断消失。
            _persist_validation(store, t, r, results)
            raise
        results.append(
            {
                "id": check["id"],
                "status": _evidence.PASS if code == 0 else _evidence.FAIL,
                "exit_code": code,
                "fingerprint": marker,
                "reason": None,
                "error_class": None if code == 0 else error_class(None, code),
            }
        )
    return results, errors


def judge_validation(results, selected) -> None:
    """Strict verdict: the declared set is covered exactly once and all PASS.

    "At least one PASS" is not enough: a missing, duplicated or unknown result
    must never be read as success (§6.4).
    """
    require(selected, "VALIDATION_FAILED", "Empty verification scope")
    executed = {}
    for row in results:
        if row.get("status") == _evidence.NOT_RUN:
            continue
        check_id = row.get("id")
        require(check_id not in executed, "VALIDATION_FAILED", "Duplicate check result")
        executed[check_id] = row
    require(sorted(executed) == sorted(selected), "VALIDATION_FAILED", "Selection mismatch")
    require(
        all(row.get("status") in (_evidence.PASS, _evidence.FAIL) for row in executed.values()),
        "VALIDATION_FAILED",
        "Unknown check status",
    )
    require(
        all(row.get("status") == _evidence.PASS for row in executed.values()),
        "VALIDATION_FAILED",
        "Independent checks failed",
    )


def latest_handoff(store, t, exclude_run=None):
    for run in reversed(store.runs(t["task_id"])):
        if run["run_id"] == exclude_run:
            continue
        path = store.path(t) / "runs" / run["run_id"] / "handoff.json"
        if path.is_file():
            try:
                return str(path), read(path)
            except (OSError, ValueError):
                return str(path), {}
    return None, None


def reassign(store: Store, id: str, req: dict[str, Any]) -> dict[str, Any]:
    """Stop-confirmed handover to another *verified* adapter profile (§13)."""
    with store.lock():
        key, prior = store.operation(req, "reassign", id)
        if prior:
            return prior
        t = reconcile(store, store.task(id))
        try:
            _contracts.require_change_request(req, ("target_profile",))
            authorize(t["config"], req, "execute")
            _orchestration.require_owner(store, t["project_id"], req)
            _require_member_mutable(store, t, "reassign")
            c = store.config(t["project_id"])
            # v1.5.0 M8：换执行配置属于"新的执行尝试"，同样要过配置校验与授权确认。
            # 顺序：配置合法性 → 策略漂移 → 目标候选 → 授权确认。漂移必须先报
            # POLICY_CHANGED：那一步的下一步是 rebind，而确认的下一步是 attest，报错
            # 顺序换了人就会先确认再发现要 rebind，白跑一趟。授权确认放在目标候选
            # 确定之后，是因为确认范围可以比白名单更窄，必须按"实际要用哪个"判。
            check_execution_contract(c, base_dir=store.home / "projects")
            require(
                digest(c) == _profiles.policy_digest(t["config"]),
                "POLICY_CHANGED",
                "Project policy changed; rebind before reassigning",
            )
            require(
                t["row_version"] == req.get("expected_version"),
                "STATE_CONFLICT",
                "Refresh expected_version",
            )
            require(
                t["state"] in ("READY", "PAUSED", "BLOCKED"),
                "STATE_CONFLICT",
                "Stop and confirm the writer before reassigning",
            )
            require(
                t.get("block_code") != "PROCESS_UNKNOWN",
                "PROCESS_UNKNOWN",
                "Resolve the old writer before reassigning",
            )
            require(
                t["phase"] in ("prepare", "implement", "verify"),
                "STATE_CONFLICT",
                "publish/ci/acceptance cannot change implementer",
            )
            r = store.get_run(t.get("current_run_id")) if t.get("current_run_id") else None
            require(
                not r or r.get("status") == "exited", "PROCESS_UNKNOWN", "An agent is still running"
            )
            revision = int(t.get("assignment_revision", 1)) + 1
            plan = None
            requested_target = req["target_profile"]
            if isinstance(requested_target, str) and requested_target == "auto":
                # v1.2 §7.3 / §14.4：重派也可以交给路由，但**必须是路由配置本身
                # 允许的**。没有 routing 段时"auto"等于"默认 profile"，那是一次
                # 未经批准的替换，所以这里拒绝而不是回落。
                require(
                    c.get("routing") is not None,
                    "CAPABILITY_UNSUPPORTED",
                    "target_profile=auto requires a routing section (mode=auto)",
                )
                plan = _routing.plan_route(
                    c,
                    {
                        "route": {
                            "task_type": t.get("task_type"),
                            "requirements": t.get("requirements"),
                        }
                    },
                    task_id=id,
                    assignment_revision=revision,
                    trigger="reassign",
                )
                _routing.raise_for_plan(plan)
                target_profile_id = plan["selected_profile"]
            else:
                target_profile_id = requested_target
            target = _profiles.require_runnable(
                _profiles.resolve_binding(c, target_profile_id),
                require_real_e2e=bool(c.get("require_real_e2e")),
            )
            # 目标候选定了才判"这个候选被确认过"：确认范围可以比白名单更窄。
            _require_attested(store, t["project_id"], c, target_profile_id)
            handoff_path, handoff = latest_handoff(store, t)
            workspace = Path(t["workspace"])
            if handoff_path and workspace.exists() and handoff:
                require(
                    handoff.get("fingerprint") == fingerprint(t),
                    "HANDOFF_STALE",
                    "Workspace drifted since the last run; record the change before reassigning",
                )
            endpoint = _routing.endpoint_of(c, target.profile)
            execution = _routing.selected_execution(
                binding=target,
                endpoint=endpoint,
                cli_version=target.profile.get("expected_version"),
                policy_revision=digest(c)[:16],
                routing=_routing.routing_digest(c),
            )
            decision = None
            if plan is not None:
                decision = _routing.build_decision(
                    c=c,
                    plan=plan,
                    requirements=plan["requirements"],
                    task_type=plan["task_type"],
                    task_type_source=plan["task_type_source"],
                    task_id=id,
                    assignment_revision=revision,
                    trigger="reassign",
                    requested_profile=None,
                    execution=execution,
                    project_id=c["project_id"],
                )
            record = {
                "assignment_revision": revision,
                "task_id": id,
                "at": time.time(),
                "reason": req.get("reason"),
                "request_id": req.get("request_id"),
                "previous": {
                    "profile_id": t.get("agent_profile_id"),
                    "adapter_id": t.get("adapter_id"),
                    "adapter_backend": t.get("adapter_backend"),
                },
                "next": {
                    "profile_id": target.profile_id,
                    "adapter_id": target.adapter_id,
                    "adapter_backend": target.backend,
                },
                "handoff": handoff_path,
                "budget": {
                    "attempts_used": t.get("attempts", 0),
                    "max_attempts": c.get("max_attempts", 3),
                },
                "invalidated": {
                    "validated_sha": t.get("validated_sha"),
                    "candidate_sha": t.get("candidate_sha"),
                    "evidence": t.get("current_evidence_id"),
                },
                "adapter_summary": (handoff or {}).get("last_error"),
            }
            atomic(store.path(t) / "assignments" / f"reassign-{revision:02d}.json", record)
            t.update(
                agent_profile_id=target.profile_id,
                adapter_id=target.adapter_id,
                adapter_backend=target.backend,
                assignment_revision=revision,
                selected_execution=execution,
                combination_id=execution["combination_id"],
                requested_model=target.profile.get("model"),
                model_unresolved=execution["model_unresolved"],
                endpoint=endpoint,
                requirements=(plan or {}).get("requirements", t.get("requirements")),
                route_id=(decision or {}).get("route_id", t.get("route_id")),
                route_content_sha256=(decision or {}).get(
                    "content_sha256", t.get("route_content_sha256")
                ),
                route_reason=(plan or {}).get("reason", t.get("route_reason")),
                state="READY",
                block_code=None,
                current_evidence_id=None,
                validated_sha=None,
                validated_fingerprint=None,
                candidate_sha=None,
                empty_candidate=False,
                acceptance=None,
                last_progress_at=time.time(),
                phase="implement" if workspace.exists() else "prepare",
            )
            t.setdefault("assignments", []).append(
                {
                    "assignment_revision": revision,
                    "profile_id": target.profile_id,
                    "adapter_id": target.adapter_id,
                    "adapter_backend": target.backend,
                    "at": time.time(),
                    "reason": req.get("reason"),
                }
            )
            result = _reply(
                req,
                {
                    "status": "succeeded",
                    "task_id": id,
                    "state": "READY",
                    "row_version": t["row_version"] + 1,
                    "assignment_revision": revision,
                    "profile_id": target.profile_id,
                    "adapter_id": target.adapter_id,
                    "adapter_backend": target.backend,
                    "permission": target.permission,
                },
                recommended_action="resume",
            )
            if decision is not None:
                # 决策文件先于任务记录落盘（§15.5）。
                _routing.write_decision(store, t, decision)
                result.update(
                    route_id=decision["route_id"],
                    route_reason=plan["reason"],
                    route_decision=plan["decision"],
                    combination_id=execution["combination_id"],
                )
            store.save(
                t,
                "agent_reassigned",
                operation=(key, result),
                payload={
                    "assignment_revision": revision,
                    "summary": "handover to " + target.profile_id,
                    "route_id": (decision or {}).get("route_id"),
                },
            )
            return result
        except Exception as e:
            store.finish(
                key,
                {
                    "status": "failed",
                    "error": {"code": getattr(e, "code", "INTERNAL_ERROR"), "message": str(e)},
                },
            )
            raise


def rebind(store: Store, id: str, req: dict[str, Any]) -> dict[str, Any]:
    """Explicitly adopt the current project policy; prior evidence stops counting."""
    with store.lock():
        key, prior = store.operation(req, "rebind", id)
        if prior:
            return prior
        t = store.task(id)
        try:
            _contracts.require_change_request(req, ("target_policy_hash",))
            authorize(store.config(t["project_id"]), req, "execute")
            _require_member_mutable(store, t, "rebind")
            require(
                t["row_version"] == req.get("expected_version"),
                "STATE_CONFLICT",
                "Refresh expected_version",
            )
            require(t["state"] not in TERMINAL, "STATE_CONFLICT", "Terminal task")
            c = store.config(t["project_id"])
            require(
                digest(c) == req["target_policy_hash"],
                "POLICY_CHANGED",
                "target_policy_hash is not the current project policy",
            )
            require(
                t.get("block_code") != "PROCESS_UNKNOWN",
                "PROCESS_UNKNOWN",
                "Resolve the writer first",
            )
            # A rebind may only adopt a policy the frozen spec still satisfies:
            # narrowing allowed_paths or removing a referenced check means the
            # spec must be revised first, not silently re-authorized.
            frozen_spec = read(store.path(t) / f"spec-{t['spec_revision']}.json")["text"]
            try:
                _contracts.validate_spec_text(frozen_spec, c)
            except Error as exc:
                raise Error(
                    "POLICY_CHANGED",
                    f"Frozen spec is not valid under the new policy ({exc}); revise it first",
                ) from exc
            previous = {
                "policy_revision": t.get("policy_revision"),
                "profile_id": t.get("agent_profile_id"),
            }
            binding = _profiles.resolve_binding(c, t.get("agent_profile_id"))
            endpoint = _routing.endpoint_of(c, binding.profile)
            execution = _routing.selected_execution(
                binding=binding,
                endpoint=endpoint,
                cli_version=binding.profile.get("expected_version"),
                policy_revision=digest(c)[:16],
                routing=_routing.routing_digest(c),
            )
            # 新策略**收窄**时，正在跑的组合同样要按新规则复核（§16.5）：
            # 不能因为"它是历史选择"就豁免。复核失败 → BLOCKED + POLICY_DENIED，
            # 但不清工作区、不删证据：这不是执行出错，是"该组合已不被允许"。
            combination_block = (
                _routing.combination_allowed(c, binding.profile_id, t.get("requirements") or {})
                if c.get("routing") is not None
                else None
            )
            # F1：策略变更只让**证据**失效，不让**产物**失效。本地产物、写者已
            # 收口、工作区与已记录的产物身份一致时，rebind 把任务放回 verify：
            # 重跑 checks 并重签证据，不重启 agent；否则只清证据、保留原阻塞，
            # 并在响应里说明卡在哪一步。`block_code` 不再被无条件清空——那会
            # 抹掉 PROCESS_UNKNOWN 之外也仍然有效的阻塞原因。
            product, product_intact = _revalidation_product(store, t)
            blockers = _revalidation_blockers(store, t)
            revalidate = bool(product_intact and not blockers)
            previous_block = t.get("last_block") or {}
            # 归因字段必须跟着走（v1.6）：rebind 只回答"我采纳了新策略"，不回答
            # "这次阻塞是谁的责任"。旧写法重建一个只含 code/run_id/policy_revision
            # 的记录，`basis` / `scope_source` / `spec_revision` 被一起丢掉，于是
            # 恢复前的闸门（`_require_config_repaired`）认为"这不是策略类阻塞"而直接
            # 放行——那正是"rebind 之后又跑了一遍、又撞同一个阻塞"的来源。
            carried = (
                {
                    "basis": previous_block.get("basis"),
                    "scope_source": previous_block.get("scope_source"),
                    "spec_revision": previous_block.get("spec_revision"),
                }
                if previous_block.get("code") == t.get("block_code")
                else {}
            )
            last_block = (
                {
                    "code": t.get("block_code"),
                    "run_id": t.get("current_run_id"),
                    "policy_revision": t.get("policy_revision"),
                    "at": time.time(),
                    **carried,
                }
                if t.get("block_code")
                else t.get("last_block")
            )
            t.update(
                config=c,
                policy_revision=digest(c)[:16],
                agent_profile_id=binding.profile_id,
                adapter_id=binding.adapter_id,
                adapter_backend=binding.backend,
                validated_sha=None,
                validated_fingerprint=None,
                current_evidence_id=None,
                empty_candidate=False,
                acceptance=None,
                selected_execution=execution,
                combination_id=execution["combination_id"],
                endpoint=endpoint,
                requested_model=binding.profile.get("model"),
                model_unresolved=execution["model_unresolved"],
                last_block=last_block,
                block_code=None if revalidate else t.get("block_code"),
                revalidation=(
                    {
                        "pending": True,
                        "product": product,
                        "since_policy": digest(c),
                        "set_at": time.time(),
                    }
                    if revalidate
                    else t.get("revalidation")
                ),
            )
            if revalidate:
                t.update(state="READY", phase="verify")
            elif product and not blockers and not product_intact:
                # 产物基准存在但已被破坏（工作区被改动）：不能拿它当候选继续
                # 重验，退回人工处置，而不是静默产出一份对不上的证据。**首次**
                # rebind 也要走这里——只在"第二次 rebind"才拦，等于允许"先改
                # 文件再 rebind"把人为改动洗成 agent 产物。没有基准（从未验证
                # 过的任务）不属于"基准丢失"，照旧只换策略。
                t.update(
                    state="BLOCKED",
                    block_code="WORKSPACE_CHANGED",
                    block_details={
                        "reason": "revalidation basis lost",
                        "hint": "restore the worktree, or revise the task to produce a new "
                        "candidate",
                    },
                    revalidation={"pending": False, "product": None},
                )
            if combination_block is not None:
                t.update(
                    state="BLOCKED",
                    block_code="POLICY_DENIED",
                    block_details={
                        "reason": combination_block[1],
                        "combination_id": execution["combination_id"],
                        "profile_id": binding.profile_id,
                        "hint": "reassign to an allowed profile, or restore the previous policy",
                    },
                    last_error="combination no longer allowed: " + combination_block[1],
                )
            result = _reply(
                req,
                {
                    "status": "succeeded",
                    "task_id": id,
                    "state": t["state"],
                    "row_version": t["row_version"] + 1,
                    "policy_revision": t["policy_revision"],
                    "invalidated": [
                        "validated_sha",
                        "validated_fingerprint",
                        "current_evidence_id",
                        "acceptance",
                    ],
                    "profile_id": binding.profile_id,
                    "capability": binding.capability,
                    "adapter_backend": binding.backend,
                    "permission": binding.permission,
                    # 宿主不必猜测"重绑之后能不能继续"：可重验时明确给出 pending，
                    # 不可重验时给出具体 blocker（而不是一句含糊的 POLICY_CHANGED）。
                    "revalidate": {
                        "pending": revalidate,
                        "product": product,
                        "blockers": blockers,
                    },
                },
                recommended_action="resume" if revalidate else None,
            )
            if combination_block is not None:
                result.update(
                    block_code="POLICY_DENIED",
                    block_details=t["block_details"],
                    recommended_action="reassign",
                )
            store.save(
                t,
                "policy_rebound",
                operation=(key, result),
                payload={"summary": "rebind", "previous": previous, "reason": req.get("reason")},
            )
            return result
        except Exception as e:
            store.finish(
                key,
                {
                    "status": "failed",
                    "error": {"code": getattr(e, "code", "INTERNAL_ERROR"), "message": str(e)},
                },
            )
            raise


def _tail_text(path, limit: int) -> str:
    """Read at most ``limit`` bytes from the end of a log; never raise."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def _agent_result(store, t, r, binding, spec, code):
    """Parse the run's bounded capture through the adapter (never through argv claims)."""
    run_dir = store.path(t) / "runs" / r["run_id"]
    if ProcessCapture.capture_present(run_dir):
        capture = ProcessCapture.from_run_dir(
            run_dir, "agent", binding.adapter_id, binding.backend, spec.agent_version
        )
        result = binding.adapter.parse_output(capture)
        r["result_schema"] = "agent-adapter"
    else:
        # Only a backend double or a third-party backend reaches this path: no
        # capture metadata means the adapter protocol was not exercised, so the
        # exit code is the sole signal and it is labelled as such.
        #
        # 退出码是唯一信号**不等于**没有证据：agent.log 里通常就有那一行说明
        # （"429 使用量已超出频率限制，将在 … 重置"）。此前这里把 outcome 硬写成
        # failed，于是"额度用完"在状态库里长成了 AGENT_FAILED（兜底码），
        # 排障只能再去翻日志、再靠人判断是不是代码问题。降级路径同样必须分类，
        # 并把"这是降级结论"标出来。
        text = _tail_text(run_dir / "agent.log", _quota.EVIDENCE_SCAN_BYTES)
        outcome = "completed" if code == 0 else agent_base.classify_failure(text)
        evidence = _quota.excerpt(text)
        note = "backend did not emit capture metadata; exit code only"
        result = AgentResult(
            agent_id=binding.adapter_id,
            agent_version=spec.agent_version or "",
            outcome=outcome,
            exit_code=code,
            summary=(note + ("; evidence: " + evidence if evidence else ""))[:4000],
        )
        r["result_schema"] = "exit-code-only"
        # 证据与结论分开放：summary 给人和报告读，`agent_evidence` 给对账与
        # 收尾快照读。宿主不必为了写 HANDOFF 再抓一次日志。
        r["agent_evidence"] = {
            "degraded": True,
            "source": "agent.log",
            "scan_bytes": _quota.EVIDENCE_SCAN_BYTES,
            "read_bytes": len(text),
            "excerpt": evidence,
        }
    r["agent_result"] = result.to_dict()
    r["session_id"] = result.session_id
    store.run(r)
    return result


def _observe_run(store, t, r, binding, result, measured_version=None) -> dict:
    """Parse layer: record what the tool said, **whatever** the outcome (§12.8).

    三件事必须在 ``require_completed()`` 之前完成，否则失败 run 会丢掉
    ``model_identity``/``spend``/``failure_class``，历史统计里就只剩成功样本，
    等于用"只统计成功者"的方式自我美化。
    """
    observation = _routing.model_identity(
        binding.profile.get("model"),
        result.reported_model,
        result.reported_models,
        # 声明等价名：请求名与供应商名不同时，只有 profile 能说明它们是不是
        # 同一个模型。不传就等于把"别名"读成 mismatch。
        binding.profile.get("model_aliases"),
    )
    spend = _routing.cost_observation(result.usage, binding.profile)
    failure_class = None
    outcome_code = None
    if result.outcome != agent_base.OUTCOME_COMPLETED:
        outcome_code = agent_base.OUTCOME_ERRORS.get(result.outcome, "AGENT_FAILED")
        failure_class = _routing.failure_class_of(outcome_code)
    r.update(
        model_identity=observation["model_identity"],
        observation_scope=observation["observation_scope"],
        # ``identity_basis`` 把"逐字相等"与"按声明等价"分开记：它是结论的依据，
        # 不是结论本身，读者不必从 identity 字符串反推口径。
        identity_basis=observation.get("identity_basis"),
        requested_model=observation["requested_model"],
        reported_model=observation["reported_model"],
        reported_models=observation["reported_models"],
        model_aliases=list(binding.profile.get("model_aliases") or []),
        spend=spend,
        failure_class=failure_class,
        cli_version=r.get("cli_version") or measured_version,
        purpose=t.get("purpose") or "task",
    )
    r["combination_verified"] = _routing.combination_verified(
        run=r,
        binding=binding,
        usage=result.usage,
        model_identity_value=observation["model_identity"],
    )
    r["attribution"] = _routing.attribution(
        outcome=result.outcome,
        failure_class=failure_class,
        model_identity_value=observation["model_identity"],
        purpose=r["purpose"],
        # v1.5.0：兜底码与"运行模型未解析"都不进"身份与归因明确样本"的通过率。
        block_code=outcome_code,
        model_unresolved=_profiles.model_unresolved(binding.profile),
    )
    store.run(r)
    return observation


def _block_and_quarantine(store, id, r, code: str, message: str, details: dict) -> Error:
    """Gate failure → ``BLOCKED`` + workspace quarantine, then the caller raises.

    顺序不可交换：先落状态（谁都能读到"为什么停"），再隔离工作区（改动移出但
    保留为 patch），最后抛错。抛错前完成持久化，才不会出现"报错了但记录没写"。
    """
    details = dict(details)
    details["gate"] = code
    with store.lock():
        current = store.task(id)
        current.update(
            state="BLOCKED",
            block_code=code,
            block_details=details,
            last_error=message[:4000],
        )
        store.save(current, "blocked", run_id=r["run_id"], payload={"block_code": code})
    record = _worktree.quarantine(store, current, r, code)
    details["worktree_quarantine"] = record
    with store.lock():
        current = store.task(id)
        current["block_details"] = details
        store.save(current, "worktree_quarantined", run_id=r["run_id"], payload=record)
    r["block_code"] = code
    r["block_details"] = details
    store.run(r)
    return Error(code, message, details=details)


def _gate_model_identity(store, t, r, binding) -> None:
    """Gate A: a pinned combination proceeds on ``reported_match`` / ``alias_confirmed``.

    ``unobserved`` 也阻断 —— "工具没说"不是"对上了"。这不是对模型的怀疑，而
    是对**证据**的要求：没有观测就没有"我只用了它"这句话。

    ``alias_confirmed`` 放行的前提是 profile 写了 ``model_aliases``：那是被
    ``attest`` 覆盖的配置声明，可审计。两者在 ``identity_basis`` 里分开记，
    报告不得把"按声明等价"写成"逐字一致"。
    """
    requirements = t.get("requirements") or {}
    identity = r.get("model_identity") or "unobserved"
    if not _routing.pin_blocks(requirements, identity):
        return
    requested = r.get("requested_model") or binding.profile.get("model")
    # 字段名与设计 §15.4 的错误契约一致（requested/reported/scope/identity/pin_model），
    # 另附全部观测，便于回答"哪个调用报告的哪个模型"。
    details = {
        "requested": requested,
        "reported": r.get("reported_model"),
        "scope": r.get("observation_scope"),
        "identity": identity,
        "identity_basis": r.get("identity_basis"),
        "pin_model": True,
        "reported_models": r.get("reported_models") or [],
        "request_echoes": [
            entry.get("model")
            for entry in (r.get("reported_models") or [])
            if entry.get("scope") == "request_echo"
        ],
        "declared_aliases": list(binding.profile.get("model_aliases") or []),
        "hint": "declare the model the tool reports (or its alias set), "
        "or disable pin_model for this route",
    }
    message = f"model identity gate: requested {requested!r}, observed {identity}" + (
        f" ({r.get('reported_model')!r})" if r.get("reported_model") else ""
    )
    raise _block_and_quarantine(store, t["task_id"], r, "MODEL_MISMATCH", message, details)


def _gate_spend(store, t, r, c, binding) -> None:
    """Gate B: the spend guard is evaluated *after* recording this run's cost."""
    try:
        _routing.spend_gate(store, t, c, r.get("spend") or {})
    except Error as error:
        if error.code not in ("SPEND_LIMIT_EXCEEDED", "SPEND_UNVERIFIABLE"):
            raise
        details = dict(error.details or {})
        details["combination_id"] = r.get("combination_id")
        blocked = _block_and_quarantine(store, t["task_id"], r, error.code, str(error), details)
        raise blocked from error


def _count_delivery(store, t) -> None:
    """Attribute the acceptance to the run that produced the accepted candidate.

    只认最新一个"通过闸门且完成"的 run：把验收摊给历史 run 会让一次返工看起来
    像多个组合都交付成功，这正是"按组合比较质量"最容易被污染的路径。
    """
    for run in reversed(store.runs(t["task_id"])):
        if run.get("status") != "exited":
            continue
        if run.get("block_code") or run.get("failure_class"):
            continue
        if (run.get("agent_result") or {}).get("outcome") != "completed":
            continue
        attribution = dict(run.get("attribution") or {})
        if attribution.get("attempt_metrics") == "excluded":
            return
        attribution["delivery_metrics"] = "counted"
        attribution["reason"] = "accepted by a human reviewer"
        run["attribution"] = attribution
        store.run(run)
        return


def _verifier_image(config) -> str | None:
    verification = config.get("verification") or {}
    return verification.get("image") if isinstance(verification, dict) else None


def _previous_resolved_path(store, task_id, exclude_run):
    """The ``resolved_path`` of the newest earlier run, if it recorded one."""
    for run in reversed(store.runs(task_id)):
        if run.get("run_id") == exclude_run:
            continue
        if run.get("resolved_path"):
            return run["resolved_path"]
    return None


def _publish_drift_gate(store, t, c) -> dict:
    """Classify remote drift, persist the evidence, then decide (§7.4.1).

    Order matters and is deliberately not "check, then write": the evidence is
    written *before* the error is raised, so there is no path where the CLI
    reports a drift that has no record on disk. The event, the evidence file and
    the raised error all carry the same ``probe()`` result.
    """
    result = _drift.probe(store, t, c)
    effect = store.path(t) / "publication.json"
    evidence = {"task_id": t["task_id"], **result}
    if result["kind"] != _drift.DRIFT_NONE:
        # Merge rather than replace: an earlier publication's PR link stays
        # readable, and the blocking verdict is added to it.
        try:
            record = read(effect) if effect.is_file() else {}
        except (OSError, ValueError):
            record = {}
        record.update(
            status="blocked_by_drift",
            sha=t.get("candidate_sha"),
            branch=t.get("branch"),
            drift=evidence,
        )
        atomic(effect, record)
        store.save(t, "remote_drift", payload=evidence)
        raise _drift.error(result, t)
    return evidence


def _merge_drift_evidence(store, t, evidence) -> None:
    """Keep the successful publication record and its drift check together."""
    effect = store.path(t) / "publication.json"
    try:
        current = read(effect) if effect.is_file() else {}
    except (OSError, ValueError):
        current = {}
    current["drift"] = evidence
    atomic(effect, current)


def run_pipeline(store, id, backend=None, publisher=None):
    t = store.task(id)
    require(t.get("current_run_id"), "STATE_CONFLICT", "Task has no reserved run")
    r = store.get_run(t["current_run_id"])
    require(r is not None, "RUN_NOT_FOUND", "Reserved run record is missing")
    c = t["config"]
    # A task frozen on the removed container backend must not be driven at all,
    # even if some path skipped `start` (§18.2-B1).
    refuse_docker_backend(c, where="task")
    if t.get("phase") == "execute":
        # Legacy records: v0.4.0 wrote this value when an agent asked a question,
        # but no stage consumes it, so a resumed run silently did nothing.
        # Normalize to the stage the question was raised in, before any stage
        # selection happens.
        with store.lock():
            t = store.task(id)
            t["phase"] = "implement"
            store.save(t, "phase_normalized")
    backend = backend or create_backend_for(c)
    publisher = publisher or Publisher(c)
    binding = None
    # 记录进入本 run 前的启动次数：失败归因要用"这次 run 到底有没有扣过预算"，
    # 而不是用阶段猜测——verify 阶段抛出的环境错误同样发生在一次已扣费的 run 里。
    attempts_at_entry = int(t.get("attempts") or 0)

    def update(**kw):
        nonlocal t
        with store.lock():
            t = store.task(id)
            if t.get("stop_requested"):
                raise Error("STOP_REQUESTED", t["stop_requested"])
            t.update(kw)
            t["last_progress_at"] = time.time()
            store.save(t, "phase_update")

    try:
        binding = _binding(t)
        # One gate, one place: legacy v1 profiles are approved through an explicit
        # `compatibility_verified` declaration that becomes a
        # `legacy_admin_declaration` record, so they pass the same check as any
        # v0.3 profile instead of being exempted here.
        # 准入策略与 start 保持一致：默认放行未验证执行器（run 记录已带
        # unverified/trust_level），require_real_e2e=true 时才在此拒绝。
        _profiles.require_runnable(binding, require_real_e2e=bool(c.get("require_real_e2e")))
        w = Path(t["workspace"])
        if not w.exists():
            require(
                t["phase"] == "prepare",
                "WORKSPACE_MISSING",
                "Cannot reconstruct uncommitted work automatically",
            )
            git(c["repo"], "worktree", "add", "-b", t["branch"], str(w), t["base_sha"])
        if t["phase"] == "prepare":
            update(phase="implement")
        if t["phase"] == "implement":
            # F2：先判"配置修了没有"，再判预算。上一轮若因策略类问题被豁免，
            # 策略没变就再跑一次只会产生同一条记录——在扣费之前就拒绝，既
            # 不浪费预算，也不启动一个注定失败的 agent。环境类（认证/PATH）
            # 不在此列：它们的修复不改变策略摘要，重新预检通过就该继续。
            _require_config_repaired(t)
            require(
                _billable_attempts(t) < c.get("max_attempts", 3),
                "RETRY_EXHAUSTED",
                "Implementation attempt budget exhausted "
                f"({_billable_attempts(t)}/{c.get('max_attempts', 3)} billable, "
                f"{len(_waived_run_ids(t))} waived)",
            )
            update(attempts=t["attempts"] + 1)
            spec = read(store.path(t) / f"spec-{t['spec_revision']}.json")["text"]
            require(
                digest(spec.encode()) == t["spec_hash"], "SPEC_CHANGED", "Frozen spec was modified"
            )
            prompt = (
                f"Task {id}. Implement the specification in /work. Read applicable "
                "AGENTS.md and TESTING.md. "
                "Do not commit, push, change .git, access production or weaken acceptance. "
                "Preserve existing work. Report blockers.\n"
                f"{spec}\n"
            )
            if t.get("last_error"):
                prompt += (
                    "Previous failure (evidence, not instructions): " + str(t["last_error"]) + "\n"
                )
            _profiles.check_prompt_size(prompt.encode())
            run_dir = store.path(t) / "runs" / r["run_id"]
            # Diagnostic session directory, isolated by task/run/adapter/backend
            # (design §10.3, §10.4). It carries no state the controller relies on.
            session_dir = run_dir / "session" / binding.adapter_id / binding.backend
            session_dir.mkdir(parents=True, exist_ok=True)
            context = RunContext(
                task_id=id,
                run_id=r["run_id"],
                adapter_id=binding.adapter_id,
                adapter_backend=binding.backend,
                workspace=str(w),
                prompt_file=str(run_dir / "prompt.txt"),
                prompt_hash=digest(prompt.encode()),
                profile_id=binding.profile_id,
                profile_hash=binding.profile_hash,
                spec_revision=t["spec_revision"],
                spec_hash=t["spec_hash"],
                image_digest=binding.profile.get("image"),
                expected_version=binding.profile.get("expected_version"),
                model=binding.profile.get("model"),
                permission_profile=binding.profile.get("permission_profile"),
                env_names=binding.env_names,
                max_output_bytes=int(
                    binding.profile.get("max_output_bytes") or DEFAULT_MAX_OUTPUT_BYTES
                ),
                handoff_ref=latest_handoff(store, t, exclude_run=r["run_id"])[0],
                # 预算口径与门禁一致：豁免掉的启动不算数，否则 agent 看到的
                # "还剩几次"会和控制器实际允许的次数不同。
                remaining_attempts=max(0, c.get("max_attempts", 3) - _billable_attempts(t)),
                session_dir=str(session_dir),
                options=binding.options,
            )
            spec_launch = _profiles.launch_for(binding, context)
            # §6.6: pin argv[0] to the absolute path the doctor reports, so
            # "doctor says it exists" and "start launches it" cannot disagree.
            # Only argv[0] changes: cwd and env names stay exactly as validated.
            spec_launch = _discovery.pin_launcher(
                spec_launch,
                c.get("backend"),
                env_names=binding.env_names,
                parse_version=binding.adapter.parse_probe,
            )
            resolution = dict(spec_launch.extra.get("resolution") or {})
            resolved_path = resolution.get("path")
            previous_path = _previous_resolved_path(store, id, r["run_id"])
            # §6.5 / D9：profile 里 pin 的 expected_version 必须和**本次启动环境**
            # 里测到的版本比对一次。此前 pin_launcher 从不带 parse_version，导致
            # run 记录的 cli_version 永远为 null，"版本绑定"只是文档承诺。
            #
            # 默认**记录**不一致，不拒绝：控制器也可能对着替代二进制运行（测试
            # 里的 stub、打包后的另一份安装），把"版本字符串不等"当成拒绝条件
            # 会让这些场景直接不可用，而不是变可疑。想把它变成硬门禁的项目显式
            # 声明 ``verify_cli_version: true`` —— 那时不一致就是拒绝。
            measured_version = resolution.get("version")
            expected_version = binding.profile.get("expected_version")
            version_mismatch = bool(
                measured_version and expected_version and measured_version != expected_version
            )
            if version_mismatch and c.get("verify_cli_version") is True:
                raise Error(
                    "ADAPTER_VERSION_MISMATCH",
                    f"CLI measured {measured_version!r} but profile "
                    f"{binding.profile_id} pins {expected_version!r} "
                    "(verify_cli_version=true)",
                )
            # Freeze the effective launch configuration on the run record before
            # any process exists, so a launch can always be audited afterwards.
            r.update(
                adapter_id=binding.adapter_id,
                adapter_backend=binding.backend,
                adapter_revision=binding.entry.protocol_revision,
                profile_id=binding.profile_id,
                profile_hash=binding.profile_hash,
                image_digest=binding.profile.get("image"),
                expected_version=binding.profile.get("expected_version"),
                prompt_hash=context.prompt_hash,
                launch=spec_launch.to_dict(),
                agent_version=binding.profile.get("expected_version"),
                resolved_path=resolved_path,
                resolution_source=resolution.get("source"),
                resolution_notes=list(resolution.get("notes") or []),
                # §6.5: the version measured in the launch environment, not the
                # profile's declared expectation (`agent_version` below).
                cli_version=measured_version,
                # 实测与 pin 不一致时如实记录：报告与排障都靠它，而不是靠
                # "配置里写了什么"。
                cli_version_mismatch=version_mismatch,
                # 统计口径的"坐标"必须落在 run 上：历史报告不能靠"当时的配置
                # 长什么样"反推它属于哪个 task_type / 规格桶。
                purpose=t.get("purpose") or r.get("purpose") or "task",
                task_type=t.get("task_type") or r.get("task_type") or "unknown",
                spec_scale=r.get("spec_scale")
                or _routing.spec_scale(
                    len(
                        ((t.get("spec") or {}).get("document") or {}).get("acceptance_criteria")
                        or []
                    )
                ),
                endpoint=r.get("endpoint") or t.get("endpoint"),
                requested_model=r.get("requested_model") or binding.profile.get("model"),
                model_unresolved=bool(t.get("model_unresolved")),
            )
            # 组合冻结在这里收口：`start` 用 doctor 的版本预填，这里换成**真正
            # 启动的那个二进制**测到的版本。argv 摘要一并记下，使"记录的组合"
            # 与"实际命令行"可以互相校验而不必信任任何一方。
            previous_combination = (t.get("selected_execution") or {}).get("combination_id")
            frozen = _routing.frozen_execution(
                r.get("frozen_execution") or t.get("selected_execution") or {},
                binding=binding,
                endpoint=r.get("endpoint") or _routing.endpoint_of(c, binding.profile),
                measured_version=measured_version,
                resolved_path=resolved_path,
                resolution_source=resolution.get("source"),
            )
            frozen["argv_audit_sha256"] = digest(spec_launch.to_dict())
            r.update(
                frozen_execution=frozen,
                combination_id=frozen["combination_id"],
                cli_version=frozen["cli_version_effective"],
            )
            store.run(r)
            if previous_combination and previous_combination != frozen["combination_id"]:
                # 选中→冻结之间组合变了（通常是实测 CLI 版本与 pin 不同）。
                # 静默改冻会让"同一任务两次 run"看起来是同一个组合。
                with store.lock():
                    t = store.task(id)
                    store.save(
                        t,
                        "combination_changed",
                        run_id=r["run_id"],
                        payload={
                            "from": previous_combination,
                            "to": frozen["combination_id"],
                            "cli_version_pinned": frozen.get("cli_version_pinned"),
                            "cli_version_effective": frozen.get("cli_version_effective"),
                        },
                    )
            if version_mismatch:
                with store.lock():
                    t = store.task(id)
                    store.save(
                        t,
                        "version_drift",
                        run_id=r["run_id"],
                        payload={
                            "measured": measured_version,
                            "pinned": expected_version,
                            "capability": binding.capability,
                        },
                    )
            if resolved_path and previous_path and previous_path != resolved_path:
                # Observation only: a different binary between adjacent runs of
                # the same task is worth knowing, but it is not a verdict.
                with store.lock():
                    t = store.task(id)
                    store.save(
                        t,
                        "environment_drift",
                        run_id=r["run_id"],
                        payload={
                            "previous_path": previous_path,
                            "resolved_path": resolved_path,
                            "resolution_source": resolution.get("source"),
                        },
                    )
            code = backend.execute(
                store, t, r, list(spec_launch.argv), "agent", worker=True, prompt=prompt
            )
            result = _agent_result(store, t, r, binding, spec_launch, code)
            # §12.8 解析层：无论成败都先记录观测（模型身份 / 费用 / 失败类别），
            # 否则失败 run 会从统计里消失，历史只剩"成功者样本"。
            _observe_run(store, t, r, binding, result, measured_version)
            result.require_completed()
            # Structured input requests pause the task for the controller (07 §4.0).
            # Only adapters that advertise ``supports_structured_input`` can raise
            # them; otherwise the field is ignored and the pipeline proceeds.
            if result.input_requests and getattr(binding.entry, "supports_structured_input", False):
                # The question is raised *inside* the implement stage, so the
                # phase must not move: `task answer` clears the block and a
                # second `start` re-enters implement to act on the answer.
                update(
                    state="BLOCKED",
                    block_code="INPUT_REQUIRED",
                    needs_input=True,
                    pending_questions=result.input_requests,
                )
                return
            # 闸门 A/B 在 verify 之前：不可信的一次运行不允许生产 candidate_sha，
            # 也不允许进入验证（§12.8）。
            _gate_model_identity(store, t, r, binding)
            _gate_spend(store, t, r, c, binding)
            scope(t)
            update(phase="verify")
        if t["phase"] == "verify":
            scope(t)
            before = fingerprint(t)
            semantics, declared = frozen_scope(store, t, c)
            results, errors = run_checks(backend, store, t, r, c["checks"], declared, before)
            after = fingerprint(t)
            # 先落盘再抛错：即使某条 check 超时/异常，诊断也不丢（§6.4.1）。
            _persist_validation(store, t, r, results)
            if errors:
                # 任务级错误码保持原值（RUN_TIMEOUT 仍是 RUN_TIMEOUT），
                # 宿主不需要为"被打断的 check"新增判断分支。
                raise errors[0]
            require(
                before == after,
                "WORKSPACE_CHANGED",
                "Validation modified tracked or untracked deliverable files",
            )
            every_check = [check["id"] for check in c["checks"]]
            judge_validation(results, declared if declared is not None else every_check)
            # v3：需求文档的复选框是交付物的一部分。放在 commit 之前，这样
            # "没勾选"不会变成一个已经产出 candidate、却永远不会被验收的任务。
            ref_gaps = _progress_ref_gaps(_task_review.frozen_document(store, t), w)
            if ref_gaps:
                raise Error(
                    "PROGRESS_NOT_TICKED",
                    "progress_refs 声明的进度条目未更新："
                    + "; ".join(
                        "{}({}) {}".format(gap["marker"], gap["doc"], gap["reason"])
                        for gap in ref_gaps[:5]
                    ),
                    details={"kind": "progress_refs", "gaps": ref_gaps},
                )
            git(w, "add", "--all")
            staged = git(w, "diff", "--cached", "--name-only")
            if staged:
                git(
                    w,
                    "-c",
                    "user.name=Zoe",
                    "-c",
                    "user.email=zoe@localhost",
                    "commit",
                    "-m",
                    "feat: " + id,
                )
            sha = git(w, "rev-parse", "HEAD")
            require(fingerprint(t) == after, "WORKSPACE_CHANGED", "Commit changed candidate tree")
            # 零改动是**合法**结果（只补证据、只做验证的任务确实不产生提交），
            # 但它必须被说出来：`candidate_sha == base_sha` 时证据里的 PASS 只是
            # "基线本来就能过"，不是"这次改动通过了"。不在这里拒绝（是否允许零
            # 改动是验收判断），但把它变成可查询的字段、事件与报告标记。
            empty_candidate = sha == t.get("base_sha")
            validated = {
                **t,
                "candidate_sha": sha,
                "validated_sha": sha,
                "empty_candidate": empty_candidate,
            }
            evidence = _evidence.build(
                validated,
                results,
                after,
                c["checks"],
                _verifier_image(c),
                {
                    "adapter_id": binding.adapter_id,
                    "adapter_backend": binding.backend,
                    "profile_id": binding.profile_id,
                    "expected_version": binding.profile.get("expected_version"),
                    "result_schema": r.get("result_schema"),
                },
                # 抄写冻结标记，不在这里推断语义（§6.2.1）。
                verification_semantics=semantics,
            )
            _evidence.persist(store, t, r["run_id"], evidence)
            # 指标 A 的分子是"独立验证通过"，不是"agent 退出码为 0"。只有走到
            # 这里才算通过；被闸门拦下或 check 失败的 run 停在前一步，分子不动。
            r.update(
                verification={
                    "passed": True,
                    "evidence_id": evidence["evidence_id"],
                    "at": time.time(),
                }
            )
            store.run(r)
            update(
                candidate_sha=sha,
                validated_sha=sha,
                validated_fingerprint=after,
                current_evidence_id=evidence["evidence_id"],
                phase="publish" if t["delivery_mode"] == "pull_request" else "acceptance",
                # 零候选标记随候选一起写：单独存一个布尔，宿主不必自己比对两个 SHA
                # （两个都为空时"相等"并不代表"没有改动"，那是"还没验证"）。
                empty_candidate=empty_candidate,
                # 重新验证已发生：`revalidation.pending` 只是"等着重验"的标记，
                # 留在这里会让下一次 rebind 误以为仍处于待重验状态。
                revalidation=None,
                # 验证通过即"上一次阻塞已被处理"，恢复前判定不再拿它拦人。
                last_block=None,
            )
            if empty_candidate:
                # 事件是给监控器和事件流的：`[needs-human]` 那一层不该自己算 diff。
                store.save(
                    t,
                    "empty_candidate",
                    payload={"base_sha": t.get("base_sha"), "candidate_sha": sha},
                )
        if t["phase"] == "publish":
            require(
                t.get("validated_sha") == git(w, "rev-parse", "HEAD")
                and t.get("validated_fingerprint") == fingerprint(t),
                "WORKSPACE_CHANGED",
                "Candidate changed after verification",
            )
            if t.get("current_evidence_id"):
                _evidence.verify(store, t, fingerprint(t))
            # §7.5: drift is a *read* judgement about whether the verified
            # evidence is still valid for the integration state, so it runs here
            # and not inside `Publisher` (whose failure semantics are about
            # reconciling writes). `local_artifact` never reaches this stage:
            # verify sends it straight to acceptance.
            drift = _publish_drift_gate(store, t, c)
            pr = publisher.publish(t, store)
            _merge_drift_evidence(store, t, drift)
            update(pr=pr, published_sha=t["candidate_sha"], phase="ci")
        if t["phase"] in ("ci", "acceptance"):
            # v1.6：进入 REVIEW_READY 前顺手记一件事——目标分支在这个任务的基线
            # 之后前进了多少、有没有和这个候选撞在同一批文件上（甚至被它删掉）。
            # 只写事实、不改状态：并行批次里"落后"是常态，拒绝只会逼宿主 abort
            # 重派；真正该拦的是"合并会丢掉别人刚合入的改动"，那一条也仍然由人
            # 决定（`reverts_merged_work` 非空）。见 `_baseline` 模块说明。
            concurrent = _baseline.probe(
                c.get("repo"),
                t.get("base_sha"),
                t.get("candidate_sha"),
                c.get("base_ref", "HEAD"),
            )
        if t["phase"] == "ci":
            result = publisher.refresh(t)
            if result.get("pr"):
                update(pr=result["pr"])
            update(
                state="REVIEW_READY" if result["ready"] else "BLOCKED",
                block_code=None if result["ready"] else "CI_PENDING",
                concurrent_base=concurrent,
            )
        elif t["phase"] == "acceptance":
            update(state="REVIEW_READY", concurrent_base=concurrent)
    except Exception as e:
        with store.lock():
            t = store.task(id)
            code = getattr(e, "code", "INTERNAL_ERROR")
            if code == "STOP_REQUESTED":
                # Finalization verifies container exit before declaring a safe pause.
                t.update(state="PAUSING")
            else:
                # Structured details travel with the block: a host that needs
                # `drift_kind` must not parse `last_error` (§11.1).
                t.update(
                    state="BLOCKED",
                    block_code=code,
                    last_error=str(e),
                    block_details=getattr(e, "details", None),
                )
                if code == "QUOTA_EXHAUSTED":
                    # 一次额度耗尽是**项目级**事实，不是这一条任务的状态：六个
                    # 并发撞的是同一个账号额度池，只把当前任务标 BLOCKED，其余
                    # 会继续试、继续撞。闸门记录在项目级文件里，跨任务生效。
                    gate = _quota.trip(
                        store,
                        t["project_id"],
                        task_id=id,
                        run_id=r["run_id"],
                        text=str(e),
                    )
                    # 任务级 details 只带本次证据；"什么时候能再派"以项目级闸门为准。
                    t["block_details"] = {
                        **(getattr(e, "details", None) or {}),
                        **_quota.details(gate),
                    }
                if code in (
                    "AGENT_FAILED",
                    "VALIDATION_FAILED",
                    "AGENT_PERMISSION_DENIED",
                    "AGENT_AUTH_REQUIRED",
                    "AGENT_RATE_LIMITED",
                    # 配额耗尽不改变阶段：额度恢复后继续的是同一次实现，不是重来。
                    "QUOTA_EXHAUSTED",
                    "SPEC_CLARIFICATION_REQUIRED",
                    "AGENT_RESULT_INVALID",
                    "OUTPUT_LIMIT_EXCEEDED",
                    "PROMPT_TOO_LARGE",
                ):
                    t["phase"] = "implement"
                if code == "WORKSPACE_CHANGED":
                    t["phase"] = "verify"
                    t["validated_sha"] = None
                # F2：按证据归因，只有"这一次 run 确实扣过预算"才可能豁免，
                # 且以 run_id 为幂等键——重放与崩溃恢复都不会重复退还。
                previous_block = t.get("last_block") or {}
                same_incident = previous_block.get("code") == code
                basis = _incident_basis(store, t, code) or (
                    previous_block.get("basis") if same_incident else None
                )
                debited = int(t.get("attempts") or 0) > attempts_at_entry
                if basis and debited and r["run_id"] not in _waived_run_ids(t):
                    t["budget_waivers"] = list(t.get("budget_waivers") or []) + [
                        {
                            "run_id": r["run_id"],
                            "code": code,
                            "basis": basis,
                            "at": time.time(),
                        }
                    ]
                # 恢复前判定的依据：谁的责任、哪一轮、当时的策略摘要。写成记录
                # 而不是靠 block_code，因为 rebind 会（也应该）清掉 block_code。
                # 「同一事件再次失败」保留最初的策略摘要与归因：否则第二次（在
                # 扣费前就被拦下的）记录会把自己的判据覆盖掉，守卫随即失效。
                t["last_block"] = {
                    "code": code,
                    "basis": basis,
                    "run_id": r["run_id"],
                    "policy_revision": (
                        previous_block.get("policy_revision")
                        if same_incident
                        else t.get("policy_revision")
                    ),
                    # 恢复前判定要知道"范围来自 spec 还是项目"（v1.6）：rebind 只改
                    # policy_revision，改不动 spec 派生的 task_scope。spec_revision
                    # **不**沿用旧值——它记的是"这一轮用的是哪份 spec"，若沿用最早
                    # 那份，一次 revise 之后闸门就再也不会拦人了。
                    "scope_source": t.get("scope_source"),
                    "spec_revision": t.get("spec_revision"),
                    "at": time.time(),
                }
            store.save(t, "pipeline_stopped")
    finally:
        with store.lock():
            r = store.get_run(r["run_id"])
            uncertain = _run_uncertain(r, backend)
            r.update(status="unknown" if uncertain else "exited", ended_at=time.time())
            t = store.task(id)
            if uncertain:
                # PROCESS_UNKNOWN 只描述"执行域不可观测"，不得顶替业务失败原因。
                # 已有具体码（AGENT_*、CHECK_FAILED、SCOPE_VIOLATION…）时保留它，
                # 否则排障证据会被一句"进程未知"销毁（实测：认证失败的
                # AGENT_AUTH_REQUIRED 曾在这里被覆盖）。
                t.update(
                    state="BLOCKED",
                    block_code=t.get("block_code") or "PROCESS_UNKNOWN",
                    execution_domain="unknown",
                )
            elif t.get("stop_requested"):
                t.update(
                    state="CANCELLED" if t["stop_requested"] == "cancel" else "PAUSED",
                    block_code=None,
                    stop_requested=None,
                )
            store.save(t, "run_finalized", run=r)
            if not uncertain and Path(t["workspace"]).exists():
                # 交接快照是诊断产物：写不出来只记录，不能顶替真正的失败原因，
                # 也不能让 run 的收尾半途中断（异常会从 finally 冒出并覆盖业务码）。
                try:
                    _write_handoff(store, t, r)
                except (Error, OSError, ValueError) as exc:
                    t["handoff_error"] = str(exc)
                    store.save(t, "handoff_failed")
    return store.task(id)


def accept(store: Store, id: str, req: dict[str, Any]) -> dict[str, Any]:
    """Record human acceptance. Requires decision, actor, evidence, and an
    unchanged candidate; pull_request mode additionally requires merge."""
    with store.lock():
        key, prior = store.operation(req, "accept", id)
        if prior:
            return prior
        t = store.task(id)
        authorize(store.config(t["project_id"]), req, "accept")
        # 两套验收语义会让人无法判断"到底谁批准了交付"（TGD §6.3）。
        if t.get("delivery_mode") == GROUP_MEMBER:
            raise Error(
                "STATE_CONFLICT",
                "this task is a group member; it is closed by the group, not by itself",
                details={
                    "kind": "group_member",
                    "group_id": t.get("group_id"),
                    "hint": "由 group accept 统一收口",
                },
            )
        require(
            _profiles.policy_digest(t["config"]) == digest(store.config(t["project_id"])),
            "POLICY_CHANGED",
            "Policy changed before acceptance",
        )
        require(
            t["row_version"] == req.get("expected_version"),
            "STATE_CONFLICT",
            "Refresh expected_version",
        )
        require(t["state"] == "REVIEW_READY", "STATE_CONFLICT", "Task not review ready")
        require(
            req.get("decision") in ("accepted", "rejected")
            and req.get("evidence")
            and req.get("actor"),
            "INPUT_INCOMPLETE",
            "Decision, human actor and evidence required",
        )
        require(
            req.get("subject_sha") == t["validated_sha"],
            "WORKSPACE_CHANGED",
            "Acceptance SHA mismatch",
        )
        require(
            t.get("validated_fingerprint") == fingerprint(t),
            "WORKSPACE_CHANGED",
            "Working tree changed",
        )
        if t.get("current_evidence_id"):
            _evidence.verify(store, t, fingerprint(t))

        # —— 第二层验收（v1.7）：逐条 AC 判定与设计裁定 ——
        #
        # 第一层（上面的 evidence 校验）只回答"声明的 checks 都跑过且通过"。
        # 它**不能**回答"每条 AC 是否真的闭环"：一条 manual AC 在 v2 spec 里
        # 只是一段自由文本，报告会老实写"未记录逐项人工结论"，而 accept 照样
        # 放行——那就是 2026-11-06 事故：代码写了 60%、验收闭环 0%、清单却报清零。
        document = _task_review.frozen_document(store, t)
        review_state = _task_review.summary(store, t)
        if req["decision"] == "accepted":
            open_items = _contracts.open_decisions(document)
            if open_items:
                raise Error(
                    "DECISIONS_OPEN",
                    "设计裁定未拍板，不能验收："
                    + ", ".join(str(item.get("id")) for item in open_items),
                    details={
                        "kind": "decisions_open",
                        "open": [
                            {
                                "id": item.get("id"),
                                "question": item.get("question"),
                                "options": item.get("options"),
                                "recommendation": item.get("recommendation"),
                            }
                            for item in open_items
                        ],
                        "next": "由 owner 拍板后 task revise 回写 decisions[].status=resolved",
                    },
                )
            if _contracts.review_required(document):
                if review_state.get("review_subject_sha") != t["validated_sha"]:
                    raise Error(
                        "ACCEPTANCE_UNREVIEWED",
                        "spec 要求逐条 AC 判定，但没有绑定当前 candidate 的结论",
                        details={
                            "kind": "acceptance_unreviewed",
                            "subject_sha": t["validated_sha"],
                            "state": review_state.get("state"),
                            "required_acceptance_ids": review_state.get(
                                "required_acceptance_ids"
                            ),
                            "next": "zoe task review {} --request-file review.json".format(id),
                        },
                    )
                if review_state.get("unresolved"):
                    raise Error(
                        "ACCEPTANCE_REVIEW_FAILED",
                        "逐条判定存在 FAIL 项，不能验收",
                        details={
                            "kind": "acceptance_review_failed",
                            "failed": [
                                item.get("acceptance_id")
                                for item in review_state["unresolved"]
                            ],
                        },
                    )
                # NOT_VERIFIED 不阻断，但必须被逐条显式确认：默认放行等于把
                # "没判"和"判过了"合并成同一个结果。
                not_verified_ids = [
                    item.get("acceptance_id") for item in review_state.get("not_verified") or []
                ]
                acknowledged = req.get("acknowledged_not_verified") or []
                if not_verified_ids and list(acknowledged) != not_verified_ids:
                    raise Error(
                        "ACCEPTANCE_UNACKNOWLEDGED",
                        "存在 NOT_VERIFIED 项，必须在验收请求里逐条确认",
                        details={
                            "kind": "acceptance_unacknowledged",
                            "not_verified": not_verified_ids,
                            "next": "重发 accept 请求，带 acknowledged_not_verified 与上面完全一致",
                        },
                    )
        if t["delivery_mode"] == "pull_request" and req["decision"] == "accepted":
            result = Publisher(t["config"]).refresh(t)
            require(
                result["ready"] and result["pr"]["state"] == "MERGED",
                "STATE_CONFLICT",
                "Merge and current checks required",
            )
            t["pr"] = result["pr"]
        if req["decision"] == "rejected":
            t["phase"] = "implement"
        if review_state.get("review_subject_sha"):
            # 验收记录必须能回答"这一版是谁判的、按什么判的"。只存一个
            # decision 会让三周后的人无法复核当时的结论。
            req = dict(req)
            req["review"] = {
                "verdict": review_state.get("verdict"),
                "subject_sha": review_state.get("review_subject_sha"),
                "counts": review_state.get("counts"),
                "reviewer": review_state.get("reviewer"),
                "items_digest_path": review_state.get("path"),
                "required": review_state.get("required"),
                "state": review_state.get("state"),
            }
        t.update(
            acceptance=req,
            state="DONE" if req["decision"] == "accepted" else "BLOCKED",
            block_code=None if req["decision"] == "accepted" else "ACCEPTANCE_REJECTED",
        )
        atomic(store.path(t) / "acceptance.json", req)
        if req["decision"] == "accepted":
            # §10.4：交付指标只在被验收之后才计入，且只记到**产出这份证据**的
            # run 上。事后不改写历史 run 的质量归因（不 backfill）。
            _count_delivery(store, t)
        result = _reply(
            req,
            {
                "status": "succeeded",
                "task_id": id,
                "state": t["state"],
                "row_version": t["row_version"] + 1,
            },
        )
        store.save(t, "human_acceptance", operation=(key, result))
        return result


def close_external(store: Store, id: str, req: dict[str, Any]) -> dict[str, Any]:
    """Close a task whose delivery happened **outside** Zoe (F1-b).

    Deliberately reuses the ``CANCELLED`` terminal state: Zoe never verified the
    candidate, so the task must never be counted as an accepted delivery. The
    record keeps what was merged, who closed it, and whether the claim was
    checked by a human (``verified``) or merely asserted (``asserted``).

    It refuses to run while the execution domain is unknown or a run is still
    active: closing is an accounting act, never a way to escape an unresolved
    writer.
    """
    with store.lock():
        key, prior = store.operation(req, "close_external", id)
        if prior:
            return prior
        t = store.task(id)
        try:
            _contracts.require_change_request(req, ("external_ref", "actor", "claim"))
            authorize(t["config"], req, "execute")
            require(t["state"] not in TERMINAL, "STATE_CONFLICT", "Terminal task")
            require(
                t["row_version"] == req.get("expected_version"),
                "STATE_CONFLICT",
                "Refresh expected_version",
            )
            require(
                t.get("block_code") != "PROCESS_UNKNOWN",
                "PROCESS_UNKNOWN",
                "Resolve the writer before closing the task",
            )
            require(
                not _runs_active(store, t),
                "STATE_CONFLICT",
                "An agent run is still active; stop and reconcile it first",
            )
            require(
                req.get("claim") in ("verified", "asserted"),
                "INPUT_INCOMPLETE",
                "claim must be 'verified' (checked by a human) or 'asserted' (user statement)",
            )
            record = {
                "reason": req.get("reason"),
                "external_ref": req["external_ref"],
                "merged_sha": req.get("merged_sha"),
                "actor": req["actor"],
                "claim": req["claim"],
                "at": time.time(),
                "note": "closed outside Zoe; not accepted",
            }
            t.update(
                state="CANCELLED",
                block_code="CLOSED_EXTERNAL",
                block_details={"reason": record["reason"], "external_ref": record["external_ref"]},
                external_close=record,
            )
            result = _reply(
                req,
                {
                    "status": "succeeded",
                    "task_id": id,
                    "state": t["state"],
                    "block_code": t["block_code"],
                    "external_close": record,
                    # 显式写死：外部收口不是验收，宿主不得把它当作 accepted。
                    "accepted": False,
                    "row_version": t["row_version"] + 1,
                },
                recommended_action=None,
            )
            store.save(t, "closed_external", operation=(key, result))
            return result
        except Exception as e:
            store.finish(
                key,
                {
                    "status": "failed",
                    "error": {"code": getattr(e, "code", "INTERNAL_ERROR"), "message": str(e)},
                },
            )
            raise
