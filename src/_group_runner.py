"""Group runner: one managed process per group, one writer per task (TGD §7).

The runner is **not** a daemon. ``group start`` spawns a short-lived controller
process and returns; when that process exits, progress stops. There is no
timer, no service unit and no second writer: member implementation is always
dispatched to ``_pipeline.start``, which keeps owning the per-task writer lock.

Three invariants are load-bearing and each has a test:

1. **A lease, not an epoch.** Every start mints a fresh ``lease_id``; a runner
   refuses to write when ``runtime.lease.lease_id`` is no longer its own, so a
   superseded runner cannot corrupt the record (``lease_stale``).
2. **Serial per group.** The dispatch gate takes ``min(group_free, global_free)``,
   so "global slot free" alone can never start a second member while the first is
   still executing (T25).
3. **Budget gates dispatch only.** Reconcile, integrate, group checks and human
   acceptance keep running after the budget is exhausted (T26).
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
import uuid
from pathlib import Path

from src import _groups, _integration, _profiles
from src._config import authorize
from src._platform import get_platform
from src._proc import process_identity
from src._store import Store
from src._util import Error, atomic, digest, git, require

#: How long the child runner waits to take the runner lock from ``group start``.
RUNNER_LOCK_WAIT = 30.0

#: One tick per interval; the loop is bounded by progress, not by wall time.
TICK_INTERVAL = 0.2

#: 危急兜底：**不是退出条件**（v1.4.1）。生产 runner 的退出由公开契约决定 ——
#: 终态、显式暂停、真实阻塞、已声明预算、租约被接管，或本轮确实无可推进项。
#: 200_000 个 tick ≈ 11 小时，只在 tick 自己卡死时兜底，命中时退出原因记为
#: ``safety_cap`` 并保存续接信息，而不是静默结束。
SAFETY_MAX_TICKS = 200_000

#: 兼容旧名。曾被当作生产退出条件（2000 tick ≈ 400 秒），会让「一次启动持续
#: 推进」在用户预算之外被截断，见 v1.4.1 变更说明。
MAX_TICKS = SAFETY_MAX_TICKS

_BUDGET_CODE = "GROUP_BUDGET_EXHAUSTED"

#: 退出原因 → 给宿主看的解释。键是**契约**：出现在 ``exit_reason`` 与
#: ``runtime.lease.exit_reason`` 里，宿主按它决定是否续跑。
EXIT_REASONS = {
    "completed": "组已到达 DONE，交付完成。",
    "cancelled": "组已取消，不会自动恢复。",
    "paused": "组已暂停（draining 已清空），只有显式 resume 才会继续。",
    "review_ready": "检查与审查通过，等待人工 group accept。",
    "awaiting_design_review": "等待设计审查结论；审查由宿主在独立上下文执行，runtime 只校验。",
    "review_failed": "设计审查未通过；修正走 group repair（受 max_group_repairs 约束）。",
    "budget_exhausted": "启动额度已耗尽；只停新派发，对账/集成/检查/验收仍可继续。",
    "slots": "全局执行槽位被其他任务占用；稍后重新 group start 即可。",
    "blocked": "存在阻塞项，需要人工处置（见 blockers）。",
    "no_progress": "本轮没有可推进项，任务仍未完成。",
    "lease_stale": "租约已被别的 runner 接管，本进程停止写入。",
    "safety_cap": "命中安全上限（tick 兜底），推进停止，任务仍未完成。",
    "crashed": "推进器因未预期错误退出；先看 supervisor/runner 日志，再决定是否续跑。",
}

#: 「重新跑推进器就能接着做」的原因：不需要人先做决定。
#: 与 ``_NEEDS_HUMAN`` 必须互斥——同时为真等于给宿主两个相反指令。
_RESUMABLE = frozenset({"budget_exhausted", "slots", "no_progress", "safety_cap"})

#: 这些原因意味着「需要人先做决定，重启 runner 没有意义」。
_NEEDS_HUMAN = frozenset(
    {"review_ready", "awaiting_design_review", "review_failed", "blocked", "crashed"}
)


def _resume_hint(group_id: str, reason: str) -> str | None:
    """停在哪个原因，就给哪条**可执行**命令。

    ``group start`` 只接受 READY/PAUSED/BLOCKED；``review_ready`` 时组状态就是
    REVIEW_READY，``group resume`` 也不接受它。此时给 ``group start`` 只会换来
    一个 ``STATE_CONFLICT``，所以评审/验收类原因必须指向 review/accept。
    """
    if reason in ("review_ready", "awaiting_design_review"):
        return (
            f"zoe group review {group_id} --request-file review.json; "
            f"zoe group accept {group_id} --request-file accept.json"
        )
    if reason == "review_failed":
        return f"zoe group repair {group_id} --request-file repair.json"
    if reason in ("blocked", "crashed"):
        # 崩溃时建议先读状态与日志：直接 start 只会在同一个 tick 上再崩一次。
        return f"zoe group status {group_id} --refresh  # 读 blockers 后再决定"
    if reason in _RESUMABLE:
        return f"zoe group start {group_id} --request-file start.json"
    return None


def stop_reason(group: dict) -> str:
    """从组记录推导退出原因。纯函数：同样的记录永远得到同一个原因。

    这是公开契约的一部分（``group status`` 的 ``lease.exit_reason`` 与
    ``group start`` 的返回值），所以它只看记录，不看进程内状态。
    """
    state = group.get("state")
    if state == "DONE":
        return "completed"
    if state == "CANCELLED":
        return "cancelled"
    if state == "PAUSED":
        return "paused"
    if state == "REVIEW_READY":
        return "review_ready"
    blockers = ((group.get("runtime") or {}).get("blockers")) or []
    codes = {item.get("code") for item in blockers if isinstance(item, dict)}
    if "DESIGN_REVIEW_REQUIRED" in codes:
        return "awaiting_design_review"
    if "DESIGN_REVIEW_FAILED" in codes:
        return "review_failed"
    if _BUDGET_CODE in codes:
        return "budget_exhausted"
    if codes & {"SLOT_BUSY", "TASK_BUSY"}:
        return "slots"
    if state == "BLOCKED" or codes:
        return "blocked"
    return "no_progress"


def _conflict(message: str, kind: str, **details) -> Error:
    payload = dict(details)
    payload["kind"] = kind
    return Error("STATE_CONFLICT", message, details=payload)


@contextlib.contextmanager
def runner_locks(store: Store, group_id: str, control: bool = False, wait: float | None = None):
    """``maintenance`` SH → ``groups/<gid>/.runner`` EX → optional ``control``.

    The order matters: ``state migrate`` takes ``maintenance`` EX and then
    ``control``, so any holder that grabs ``control`` first can deadlock it.
    """
    plat = get_platform()
    path = store.lock_path(f"groups/{group_id}/runner")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    maintenance = plat.lock_shared(store.lock_path("maintenance"))
    try:
        try:
            if wait is None:
                runner = plat.lock_exclusive(path, blocking=False)
            else:
                runner = plat.lock_exclusive(path, blocking=True, timeout=wait)
        except Error as exc:
            # 只有"锁被占用"才是 SLOT_BUSY；锁文件打不开（权限、只读挂载、磁盘）
            # 报的是 PROCESS_UNKNOWN，改写它会把环境故障说成"另一个推进器在跑"。
            if exc.code not in ("SLOT_BUSY", "TASK_BUSY"):
                raise
            raise Error(
                "SLOT_BUSY",
                "another group runner holds this group",
                details={
                    "kind": "group_runner",
                    "group_id": group_id,
                    "lease": _lease_snapshot(store, group_id),
                },
            ) from None
        try:
            if control:
                with store.lock():
                    yield
            else:
                yield
        finally:
            plat.unlock(runner)
    finally:
        plat.unlock(maintenance)


def _lease_snapshot(store: Store, group_id: str) -> dict | None:
    try:
        lease = (store.group(group_id)["runtime"] or {}).get("lease")
    except Error:
        return None
    if not lease:
        return None
    return {key: lease.get(key) for key in ("lease_id", "state", "pid", "start_token")}


def _identity_observable(pid, token) -> bool | None:
    """Three-state check: ``True`` alive, ``False`` gone, ``None`` unobservable."""
    if not pid:
        return False
    try:
        if token is None:
            return get_platform().alive(pid, None)
        current = process_identity(pid)
        if current is None:
            return None
        return current == token
    except Error:
        return None


# --------------------------------------------------------------------------- #
# start handshake                                                        (7.1)
# --------------------------------------------------------------------------- #


def _default_spawn(argv, cwd, env, log_path):
    plat = get_platform()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as out:
        return plat.spawn(argv, cwd=str(cwd), env=env, stdout=out, isolate_tree=False)


def begin(store: Store, group_id: str, req: dict, spawn=None) -> dict:
    """``group start``: preflight, mint a lease, spawn the runner, return.

    Steps 1–6 of TGD §7.1. The parent releases both locks *after* the child's
    ``pid``/``start_token`` are persisted (``spawn_observed``); the child then
    re-acquires them and CAS-checks the lease before it ticks.
    """
    from src._pipeline import require_supervisor_importable, supervisor_env

    key, prior, status = store.operation_prior(req, "group_start", group_id)
    if prior or status == "intent":
        return _replay(store, group_id, req, prior)

    with runner_locks(store, group_id, control=True):
        key, prior = store.operation(req, "group_start", group_id)
        if prior:
            return prior
        try:
            group = store.group(group_id)
            _groups.require_v3(group, "start")
            config = store.config(group["project_id"])
            authorize(config, req, "execute")
            require(
                group["state"] in ("READY", "PAUSED", "BLOCKED"),
                "STATE_CONFLICT",
                f"group is {group['state']}; only READY/PAUSED/BLOCKED can be started",
            )
            existing = group["runtime"].get("lease")
            if existing and existing.get("state") == "spawn_observed":
                observable = _identity_observable(existing.get("pid"), existing.get("start_token"))
                if observable is None:
                    raise Error(
                        "PROCESS_UNKNOWN",
                        "a previously spawned runner cannot be observed; "
                        "confirm with `group status --refresh` before starting another",
                        details={"kind": "runner_unknown", "lease": existing},
                    )
                if observable:
                    raise Error(
                        "SLOT_BUSY",
                        "a group runner is already observed for this group",
                        details={"kind": "group_runner", "lease": existing},
                    )
                # Not observable and not holding the lock: the lease is stale.
                # Fall through and mint a new one, recording why.
                reason = "previous runner pid is gone"
            else:
                reason = "new lease"

            preflight = _preflight(store, group, config)
            group["runtime"]["policy_snapshot"] = {
                **(group["runtime"].get("policy_snapshot") or {"routes": {}}),
                "policy_revision": digest(config)[:16],
            }
            lease_id = uuid.uuid4().hex
            group["runtime"]["lease"] = {
                "lease_id": lease_id,
                "owner": "group-runner",
                "state": "spawn_intent",
                "pid": None,
                "start_token": None,
                "reason": reason,
                "at": time.time(),
            }
            group["state"] = "RUNNING"
            budget = group["runtime"]["budget"]
            if not budget.get("first_running_at"):
                budget["first_running_at"] = time.time()
            # `op_seq` is deliberately untouched: starting is not a plan revision.
            with store.tx():
                store.save_group(group, "group_runner_starting")

            log = _groups.group_dir(store, group_id) / "runner.log"
            argv = [
                sys.executable,
                "-m",
                "src",
                "--home",
                str(store.home),
                "internal-group",
                group_id,
                lease_id,
            ]
            env = supervisor_env()
            require_supervisor_importable(env, store.home, log)
            child = (spawn or _default_spawn)(argv, store.home, env, log)
            lease = group["runtime"]["lease"]
            lease.update(
                state="spawn_observed",
                pid=getattr(child, "pid", None),
                start_token=getattr(child, "start_token", None),
                group_token=getattr(child, "group_token", None),
            )
            with store.tx():
                store.save_group(group, "group_runner_spawned")
            result = {
                "api_version": 2,
                "operation_id": req.get("request_id"),
                "status": "accepted",
                "accepted": True,
                "completed": False,
                "group_id": group_id,
                "state": group["state"],
                **_groups.envelope(store, group),
                "lease_id": lease_id,
                "runner": {"pid": lease.get("pid"), "state": "spawn_observed"},
                "preflight": preflight,
                "event_cursor": f"{store.epoch()}:{store.max_event_id(None)}",
                "next_actions": [
                    f"group status {group_id} --refresh",
                    f"watch --after {store.epoch()}:{store.max_event_id(None)}",
                ],
                # The runner is a process, not a service: say so instead of
                # implying the group advances while nobody is alive.
                "note": "progress continues only while this runner process lives; "
                "if it exits, run `group status --refresh` and `group start` again",
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


def _replay(store: Store, group_id: str, req: dict, prior) -> dict:
    if prior:
        return prior
    group = store.group(group_id)
    return {
        "api_version": 2,
        "operation_id": req.get("request_id"),
        "status": "accepted",
        "accepted": True,
        "completed": False,
        "dispatch_pending": True,
        "group_id": group_id,
        "state": group["state"],
        **_groups.envelope(store, group),
        "lease_id": (group["runtime"].get("lease") or {}).get("lease_id"),
        "note": "a start with this request_id is still open; it is not proof of progress",
    }


def _preflight(store: Store, group: dict, config: dict) -> dict:
    """Cheap, read-only checks before a runner may be spawned (TGD §6.2)."""
    from src._util import check_disk_space

    repo = Path(config["repo"])
    require(repo.is_dir(), "ENVIRONMENT_ERROR", "project repository is missing")
    base = group["base_sha"]
    try:
        git(repo, "cat-file", "-e", base + "^{commit}")
    except Error:
        raise Error(
            "STATE_CONFLICT",
            f"base_sha {base[:12]} is not present in the repository",
            details={"kind": "base_missing"},
        ) from None
    check_disk_space(store.home)
    binding_notes = []
    for profile_id in sorted(config.get("agent_profiles") or {}):
        try:
            binding = _profiles.require_runnable(
                _profiles.resolve_binding(config, profile_id),
                require_real_e2e=bool(config.get("require_real_e2e")),
            )
            binding_notes.append(
                {
                    "profile_id": profile_id,
                    "capability": binding.capability,
                    "trust_level": binding.trust_level,
                    "auth_mode": (binding.profile or {}).get("auth_mode", "unknown"),
                }
            )
        except Error as exc:
            binding_notes.append(
                {"profile_id": profile_id, "blocked_by": exc.code, "message": str(exc)}
            )
    return {
        "policy_revision": digest(config)[:16],
        "base_sha": base,
        "profiles": binding_notes,
        "registered_adapters_only": True,
    }


def attach(store: Store, group_id: str, lease_id: str) -> dict:
    """Steps 7–8: the child proves it is the lease holder before it may tick."""
    group = store.group(group_id)
    lease = group["runtime"].get("lease") or {}
    if lease.get("lease_id") != lease_id:
        raise _conflict(
            "group lease was taken over by another runner",
            "lease_stale",
            current=lease.get("lease_id"),
            mine=lease_id,
        )
    require(
        lease.get("pid") == os.getpid(),
        "POLICY_DENIED",
        "lease pid does not match this process",
    )
    require(
        lease.get("start_token") == process_identity(os.getpid()),
        "POLICY_DENIED",
        "lease process identity does not match",
    )
    return group


def _assert_lease(store: Store, group_id: str, lease_id: str) -> dict:
    group = store.group(group_id)
    current = (group["runtime"].get("lease") or {}).get("lease_id")
    if current != lease_id:
        raise _conflict(
            "this runner's lease is stale; stop writing",
            "lease_stale",
            current=current,
            mine=lease_id,
        )
    return group


# --------------------------------------------------------------------------- #
# tick                                                                   (7.2)
# --------------------------------------------------------------------------- #


def tick(store: Store, group_id: str, lease_id: str | None = None) -> bool:
    """One serial round. Returns ``True`` when another round may make progress."""
    if lease_id is not None:
        group = _assert_lease(store, group_id, lease_id)
    else:
        group = store.group(group_id)

    # A — reconcile first, always (never gated by budget or pause).
    group = reconcile(store, group)

    if group["state"] in ("PAUSED", "CANCELLED", "DONE"):
        _persist(store, group)
        return False

    # B — integrate finished candidates, in topological order.
    #
    # 成员视图每轮只构建一次：旧实现在循环里对每个 task_key 调 `_view()`，而
    # `_view()` 每次都重建**整个** ``members_view``（每个成员一次 ``store.task``），
    # 于是 n 个成员 → n² 次查询，并且每 200ms 重复一遍。集成会改写记录，那时才
    # 需要重建（下面的 ``views = None``）。
    views: dict[str, dict] | None = None
    for task_key in _order(group):
        if _bump_lease(store, group_id, lease_id) is None:
            return False
        if views is None:
            views = {view["task_key"]: view for view in _groups.members_view(store, group)}
        view = views.get(task_key)
        if view is None:
            raise Error("INPUT_INCOMPLETE", "unknown member " + task_key)
        if view["state"] != "VALIDATED":
            continue
        try:
            _integration.integrate(store, group, task_key)
        except Error as exc:
            if exc.code in (
                "INTEGRATION_CONFLICT",
                "INTEGRATION_CHECK_FAILED",
                "INTEGRATION_UNKNOWN",
                "EVIDENCE_INVALID",
            ):
                group = store.group(group_id)
                group["state"] = "BLOCKED"
                _persist(store, group)
                return False
            raise
        group = store.group(group_id)
        views = None

    # C — group checks once everything is integrated.
    if _groups.all_integrated(group) and group["runtime"].get("integration_sha"):
        if not _integration.checks_are_current(store, group):
            try:
                _integration.run_group_checks(store, group)
            except Error as exc:
                group = store.group(group_id)
                group["state"] = "BLOCKED"
                group["runtime"]["blockers"] = [
                    {"code": exc.code, "message": str(exc), "kind": (exc.details or {}).get("kind")}
                ]
                _persist(store, group)
                return False
            group = store.group(group_id)

    # D — dispatch: the *only* step gated by slots and budget.
    gate = dispatch_gate(store, group)
    if gate["ok"]:
        for task_key in ready_members(group)[: gate["slots"]]:
            if _bump_lease(store, group_id, lease_id) is None:
                return False
            try:
                dispatch(store, group, task_key)
            except Error as exc:
                if exc.code in ("SLOT_BUSY", "GROUP_BUDGET_EXHAUSTED"):
                    break
                group = store.group(group_id)
                group["runtime"]["blockers"] = [
                    {
                        "member": task_key,
                        "code": exc.code,
                        "message": str(exc),
                        "kind": (exc.details or {}).get("kind"),
                    }
                ]
                _persist(store, group)
                return False
            group = store.group(group_id)

    # E — derive state and persist the human-readable progress view.
    group = _derive(store, group, gate)
    _persist(store, group)
    # 复用本轮算好的 gate：`_has_dispatchable_or_active` 自己再算一次会重复
    # 读配置 + 校验 + 全局任务扫描（slots），而结果与 D 步完全相同。
    return _has_dispatchable_or_active(store, group, gate)


def _bump_lease(store: Store, group_id: str, lease_id: str | None):
    """Abort the round if another runner has taken over mid-tick."""
    if lease_id is None:
        return True
    try:
        _assert_lease(store, group_id, lease_id)
    except Error:
        return None
    return True


def _order(group: dict) -> list[str]:
    """Kahn order of the frozen graph, then repair units in creation order.

    修复单元永远排在所有成员之后：它修的是"整合后暴露的问题"，先于成员集成
    执行没有意义（§12）。

    环在计划期已由 ``validate_plan`` 拒绝；走到这里成环说明记录被外部改坏。
    此前的静默退回"声明顺序"会让集成顺序变成任意顺序且不报错，与
    ``_groups._topological`` 的"无解即失败"相反。
    """
    members = group["definition"]["members"]
    order = _groups._topological(members)
    return order + [item["task_key"] for item in _groups.repair_units(group)]


def _view(store: Store, group: dict, task_key: str) -> dict:
    for view in _groups.members_view(store, group):
        if view["task_key"] == task_key:
            return view
    raise Error("INPUT_INCOMPLETE", "unknown member " + task_key)


def _dependency_integrated(group: dict, dep: str) -> bool:
    runtime = _groups.member_runtime(group, dep)
    return bool(runtime.get("integrated_sha")) and not runtime.get("invalidated")


def dependencies_met(group: dict, item: dict) -> bool:
    """True when every dependency is integrated and not invalidated.

    Shared by ``ready_members`` (what the tick may start) and ``admission`` (what
    a host may start with ``group dispatch``). Keeping one predicate is what makes
    the two paths agree; previously only the tick checked dependencies, so
    ``group dispatch`` could open ``B`` while ``depends_on=[A]`` was still pending.
    """
    return all(_dependency_integrated(group, dep) for dep in item["depends_on"])


def ready_members(group: dict) -> list[str]:
    """Units that are ``READY`` and may be dispatched now (members + repairs)."""
    ready = []
    for runtime in _groups.all_runtimes(group):
        task_key = runtime["task_key"]
        item = _groups.member(group, task_key)
        if runtime.get("task_id") or runtime.get("integrated_sha"):
            continue
        if dependencies_met(group, item):
            ready.append(task_key)
    return ready


def starts_billable(group: dict) -> int:
    budget = group["runtime"]["budget"]
    return max(0, int(budget["starts_reserved"]) - int(budget["starts_released"]))


def wall_seconds(group: dict) -> int | None:
    """Wall clock since the first RUNNING, **including** sleeps and waits."""
    budget = group["runtime"]["budget"]
    if not budget.get("first_running_at"):
        return None
    end = budget.get("finalized_at") or time.time()
    return int(end - budget["first_running_at"])


def slots_available(store: Store, group: dict) -> int:
    """``min(group_free, global_free)`` — the fix for T25 (TGD §7.2)."""
    from src._pipeline import slots

    busy = 0
    for view in _groups.members_view(store, group):
        if view["state"] == "EXECUTING" or _is_reserved(store, view.get("task_id")):
            busy += 1
    group_free = max(0, 1 - busy)
    try:
        global_free = slots(store, store.config(group["project_id"]))["free"]
    except Error:
        global_free = 0
    return min(group_free, global_free)


def _is_reserved(store: Store, task_id: str | None) -> bool:
    """A dispatch intent whose run has not been observed yet still holds the slot."""
    if not task_id:
        return False
    try:
        task = store.task(task_id)
    except Error:
        return False
    if task.get("state") not in ("RUNNING", "PAUSING"):
        return False
    run = store.get_run(task.get("current_run_id")) if task.get("current_run_id") else None
    return bool(run) and run.get("dispatch_state") in ("spawn_intent", "spawn_observed")


def dispatch_gate(store: Store, group: dict) -> dict:
    """Budget, policy and drain gate **for new starts only** (TGD §7.2/§7.4)."""
    # §7.4: a pause that drained the active member must not be followed by a new
    # one. `draining` is written by `pause` and cleared only by `resume`, so it is
    # not a hint — it is the state of the pause. Deferral, not a blocker: waiting
    # for a human is normal, and the group state stays RUNNING/PAUSED.
    if group["runtime"].get("draining"):
        return {"ok": False, "blocked": None, "slots": 0, "deferred": True, "draining": True}
    caps = group["definition"]["budget_caps"]
    if starts_billable(group) >= caps["max_starts"]:
        return {
            "ok": False,
            "blocked": {"code": _BUDGET_CODE, "kind": "starts"},
            "slots": 0,
        }
    cap = caps.get("max_wall_seconds")
    elapsed = wall_seconds(group)
    if cap is not None and elapsed is not None and elapsed >= cap:
        return {
            "ok": False,
            "blocked": {"code": _BUDGET_CODE, "kind": "wall_seconds"},
            "slots": 0,
        }
    try:
        config = store.config(group["project_id"])
    except Error:
        return {"ok": False, "blocked": {"code": "ENVIRONMENT_ERROR"}, "slots": 0}
    if digest(config)[:16] != group["runtime"]["policy_snapshot"]["policy_revision"]:
        return {"ok": False, "blocked": {"code": "POLICY_CHANGED"}, "slots": 0}
    free = slots_available(store, group)
    if free == 0:
        return {"ok": False, "blocked": None, "slots": 0, "deferred": True}
    return {"ok": True, "blocked": None, "slots": free}


# --------------------------------------------------------------------------- #
# admission                                                              (7.6)
# --------------------------------------------------------------------------- #


def admission(store: Store, task: dict) -> None:
    """The single group admission gate (TGD §7.6).

    Called by the runner's ``tick``, by ``group dispatch`` and by
    ``_pipeline.start`` for member tasks. ``group dispatch`` therefore cannot
    bypass serialisation, budget or policy — it only replaces "the tick pressed
    the button" with "a human did".
    """
    group_id = task.get("group_id")
    if not group_id:
        return
    try:
        group = store.group(group_id)
    except Error:
        raise _conflict(
            "task names a group that does not exist",
            "group_missing",
            group_id=group_id,
        ) from None
    if not _groups.is_v3(group):
        return
    task_key = task.get("item_id")
    require(task_key, "STATE_CONFLICT", "group member task has no item_id")
    item = _groups.member(group, task_key)
    runtime = _groups.member_runtime(group, task_key)
    if runtime.get("task_id") not in (None, task.get("task_id")):
        raise _conflict(
            "another task is bound to this group member",
            "group_member",
            task_key=task_key,
            bound=runtime.get("task_id"),
        )
    if group["state"] not in ("READY", "RUNNING"):
        raise _conflict(
            f"group is {group['state']}; a member cannot be dispatched",
            "group_state",
            group_id=group_id,
            state=group["state"],
            next_action=f"group resume {group_id} --request-file resume.json",
        )
    # §7.4: a draining pause holds the group in RUNNING while the last member
    # reaches a safe point. Without this check the host (or the next tick) could
    # open a new member as soon as the slot frees up, which is exactly the
    # "pause that keeps working" the design forbids.
    if group["runtime"].get("draining"):
        raise _conflict(
            "group is draining a pause; no new member is dispatched",
            "group_state",
            group_id=group_id,
            draining=True,
            next_action=f"group resume {group_id} --request-file resume.json",
        )
    if runtime.get("integrated_sha") and not runtime.get("invalidated"):
        raise _conflict(
            "member is already integrated and frozen",
            "integrated_member",
            task_key=task_key,
            integrated_sha=runtime["integrated_sha"],
        )
    # 依赖图必须在**唯一闸门**里强制。tick 的 B 步按拓扑序迭代，但"顺序"不是
    # "约束"：没有这一条，``group dispatch G --item B`` 可以在 A 未集成时启动 B，
    # B 的产物随后被合并进不含 A 的 integration_sha（依赖图想防的语义冲突被静默绕过）。
    unmet = [dep for dep in item["depends_on"] if not _dependency_integrated(group, dep)]
    if unmet:
        raise _conflict(
            "member dependencies are not integrated yet",
            "dependencies_unmet",
            task_key=task_key,
            depends_on=unmet,
            next_action="integrate " + ", ".join(unmet) + " first",
        )
    caps = group["definition"]["budget_caps"]
    if starts_billable(group) >= caps["max_starts"]:
        raise Error(
            _BUDGET_CODE,
            "group start budget exhausted ({}/{} billable)".format(
                starts_billable(group), caps["max_starts"]
            ),
            details={"kind": "starts", "next_action": "change the plan (revision+1) or cancel"},
        )
    if slots_available(store, group) == 0:
        raise Error(
            "SLOT_BUSY",
            "group serial slot is busy; one member runs at a time in v1.3",
            details={"kind": "group_slot", "group_id": group_id},
        )
    config = store.config(group["project_id"])
    if digest(config)[:16] != group["runtime"]["policy_snapshot"]["policy_revision"]:
        raise Error(
            "POLICY_CHANGED",
            "project policy changed since the group was created; `group resume` accepts it",
            details={"kind": "policy", "group_id": group_id},
        )


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


def dispatch(store: Store, group: dict, task_key: str) -> dict:
    """Create + start one member task through the normal pipeline."""
    from src import _pipeline

    item = _groups.member(group, task_key)
    runtime = _groups.member_runtime(group, task_key)
    require(
        runtime.get("task_id") is None,
        "STATE_CONFLICT",
        "member {} already has task {}".format(task_key, runtime.get("task_id")),
    )
    # 依赖在**创建之前**拦一次。`admission` 也会拦（`_pipeline.start` 的守门人），
    # 但那时 create 已经把任务绑到成员上，会留下"已绑定、从未启动"的半写入成员：
    # 之后再 dispatch 只会得到"already has task"，而计划里没有任何命令能解绑。
    unmet = [dep for dep in item["depends_on"] if not _dependency_integrated(group, dep)]
    if unmet:
        raise _conflict(
            "member dependencies are not integrated yet",
            "dependencies_unmet",
            task_key=task_key,
            depends_on=unmet,
            next_action="integrate " + ", ".join(unmet) + " first",
        )
    seq = int(group.get("op_seq") or 0) + 1
    group_id = group["group_id"]
    spec_path = _groups.group_dir(store, group_id) / item["spec_snapshot"]
    require(spec_path.is_file(), "SPEC_CHANGED", "frozen member spec is missing")
    snapshot_text = _frozen_spec_text(spec_path)
    require(
        digest(snapshot_text.encode()) == item["spec_sha256"],
        "SPEC_CHANGED",
        f"frozen spec for {task_key} no longer matches its recorded sha256",
    )
    base_request = {
        "schema_version": 2,
        "authorization_ref": group.get("authorization_ref")
        or store.config(group["project_id"])["authorization_ref"],
        "project_id": group["project_id"],
        "source": {"host": "group-runner", "group_id": group_id, "item_id": task_key},
        "group_id": group_id,
        "item_id": task_key,
        "delivery_mode": "group_member",
        "consumed_input_sha": group["runtime"].get("integration_sha") or group["base_sha"],
        "group_spec_sha256": item["spec_sha256"],
    }
    create_req = {
        **base_request,
        "request_id": f"grp-{group_id}-dispatch-{task_key}-c{seq}",
        "title": item["title"],
        "spec_file": str(spec_path),
    }
    if item.get("requested_profile"):
        create_req["agent_profile"] = item["requested_profile"]

    # Budget first: the reservation is the fact the gate compares against, and
    # it must exist before a run can be spawned.
    group["runtime"]["budget"]["starts_reserved"] += 1
    group["op_seq"] = seq
    with store.tx():
        store.save_group(group, "group_start_reserved")
    try:
        created = _pipeline.create(store, create_req)
        start_req = {
            **base_request,
            "request_id": f"grp-{group_id}-dispatch-{task_key}-s{seq}",
            "expected_version": created["row_version"],
            "strategy": "auto",
        }
        started = _pipeline.start(store, created["task_id"], start_req, action="group_dispatch")
    except Exception as exc:
        error = exc if isinstance(exc, Error) else Error("INTERNAL_ERROR", str(exc))
        _release_start(
            store,
            group_id,
            task_key,
            reason="dispatch failed: " + str(error),
            code=error.code,
        )
        raise
    return {"created": created, "started": started, "spec_digest": item["spec_sha256"]}


def repair_budget(group: dict) -> tuple[int, int]:
    """``(已用, 上限)``。上限缺省为 0：不写 ``max_group_repairs`` 就是不授权修复。"""
    caps = group["definition"].get("budget_caps") or {}
    cap = caps.get("max_group_repairs")
    cap = int(cap) if isinstance(cap, int) and not isinstance(cap, bool) else 0
    return len(_groups.repair_units(group)), cap


def repair(store: Store, group_id: str, req: dict) -> dict:
    """``group repair``：把一条 FAIL finding 变成**一次**有界修复执行（§12）。

    这不是"再派一个 Agent 试试"，而是三道门共用一次执行：

    1. **有 finding 才修**。修的对象必须是当前版本审查里状态为 ``FAIL`` 的 AC。
       没有 finding 的改动是**新增需求**，必须走计划修订，不能伪装成 bug 修复
       （§12 最后一段）。
    2. **有预算才修**。计划里的 ``budget.max_group_repairs`` 是硬上限，缺省为 0。
    3. **范围只收紧**。修复 spec 的 ``allowed_paths`` 不能超出项目授权。

    修复单元挂在 ``runtime.repairs`` 而不是 ``definition``：冻结计划不可变，
    往里插成员等于篡改 ``plan_digest``。
    """
    from src import _contracts, _review

    with store.lock():
        key, prior = store.operation(req, "group_repair", group_id)
        if prior:
            return prior
        try:
            group = store.group(group_id)
            _groups.require_v3(group, "repair")
            config = store.config(group["project_id"])
            authorize(config, req, "execute")

            used, cap = repair_budget(group)
            if cap == 0:
                raise Error(
                    _BUDGET_CODE,
                    "this plan authorises no group repairs (budget.max_group_repairs is 0); "
                    "a new plan revision is required",
                    details={"kind": "group_repair_not_authorized", "max_group_repairs": cap},
                )
            if used >= cap:
                raise Error(
                    _BUDGET_CODE,
                    f"group repair budget exhausted ({used}/{cap})",
                    details={"kind": "group_repair_exhausted", "max_group_repairs": cap},
                )
            require(
                group["state"] in ("RUNNING", "REVIEW_READY"),
                "STATE_CONFLICT",
                f"group is {group['state']}; a repair can only be raised on a complete integration",
            )
            if not _groups.all_integrated(group):
                raise Error(
                    "STATE_CONFLICT",
                    "a repair targets findings on the final integration; some members are "
                    "still not integrated",
                    details={"kind": "repair_too_early"},
                )

            acceptance_id = req.get("acceptance_id")
            require(
                isinstance(acceptance_id, str) and acceptance_id,
                "INPUT_INCOMPLETE",
                "acceptance_id required: a repair must name the finding it fixes",
            )
            review = _review.current(store, group)
            findings = [
                item
                for item in (review or {}).get("items") or []
                if item.get("acceptance_id") == acceptance_id and item.get("status") == "FAIL"
            ]
            if not findings:
                raise Error(
                    "INPUT_INCOMPLETE",
                    f"{acceptance_id} has no FAIL finding on the current integration. A "
                    "repair is not the path for a new requirement — revise the plan instead",
                    details={"kind": "repair_without_finding", "acceptance_id": acceptance_id},
                )

            affected = list(req.get("affected_members") or [])
            frozen_keys = {item["task_key"] for item in group["definition"].get("members") or []}
            unknown = sorted(set(affected) - frozen_keys)
            require(
                not unknown,
                "INPUT_INCOMPLETE",
                "affected_members names unknown member(s): " + ", ".join(unknown),
            )

            spec_file = req.get("spec_file")
            require(
                isinstance(spec_file, str) and spec_file,
                "INPUT_INCOMPLETE",
                "spec_file required: a repair runs from a frozen spec like any other task",
            )
            repo = Path(config["repo"])
            spec_path = _groups.repo_relative_path(repo, spec_file)
            require(spec_path.is_file(), "INPUT_INCOMPLETE", f"Spec file not found: {spec_file}")
            # `load_spec` 已经校验安全范围与扩权；这里不再重复一遍校验，
            # 只取冻结所需的三件东西。
            spec = _contracts.load_spec(spec_path, config)
            document = spec.get("document") or {}

            task_key = f"repair-{used + 1}"
            existing = {item["task_key"] for item in _groups.all_units(group)}
            require(task_key not in existing, "STATE_CONFLICT", "repair unit already exists")

            directory = _groups.group_dir(store, group_id)
            # 先写不可变产物，再改记录（§4.6）：崩溃只会留下孤儿 spec 文件，
            # 不会留下一个指向不存在 spec 的修复单元。
            atomic(
                directory / "specs" / (task_key + ".json"),
                {
                    "text": spec["text"],
                    "contract_version": spec["contract_version"],
                    "document": spec["document"],
                    "verification_semantics": _contracts.SCOPE_SEMANTICS,
                },
            )

            unit = {
                "task_key": task_key,
                "kind": "repair",
                "title": req.get("title") or "repair " + acceptance_id,
                "spec_sha256": digest(spec["text"].encode()),
                "spec_snapshot": f"specs/{task_key}.json",
                # 修复不拥有需求或 AC：它修的是别人的 finding。
                "depends_on": [],
                "requirement_ids": [],
                "acceptance_ids": [],
                "expected_paths": list(req.get("expected_paths") or []),
                "task_type": req.get("task_type"),
                "requested_profile": req.get("profile") or req.get("requested_profile"),
                "check_ids": list(req.get("check_ids") or []),
                "parent_acceptance_id": acceptance_id,
                "parent_finding": findings[0].get("finding"),
                "affected_members": affected,
                "scope": list(document.get("allowed_paths") or []),
                "created_at": time.time(),
                "task_id": None,
                "validated_sha": None,
                "integrated_sha": None,
                "invalidated": False,
                "last_block": None,
            }
            runtime = group["runtime"]
            runtime["repairs"] = list(runtime.get("repairs") or []) + [unit]
            # 修复会让集成版本前移：旧 subject_sha 的组级检查与审查结论自然作废，
            # 由 `checks_are_current` / `_review.current` 判定，不在这里改历史。
            group["state"] = "RUNNING"
            used_after, cap_after = repair_budget(group)
            result = {
                "api_version": 2,
                "operation_id": req.get("request_id"),
                "status": "succeeded",
                "accepted": True,
                "completed": True,
                "group_id": group_id,
                **_groups.envelope(store, group),
                "state": "RUNNING",
                "repair": {
                    "task_key": task_key,
                    "parent_acceptance_id": acceptance_id,
                    "affected_members": affected,
                    "spec_digest": unit["spec_sha256"],
                },
                "repair_budget": {"used": used_after, "max": cap_after},
                "row_version": group["row_version"] + 1,
                "next_actions": [
                    f"group start {group_id} --request-file start.json",
                    f"group status {group_id} --refresh",
                ],
                "note": (
                    "repair recorded; it is dispatched serially like any member, and the new "
                    "integration_sha invalidates the old checks and review"
                ),
            }
            with store.tx():
                store.save_group(group, "group_repair_recorded")
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


def _frozen_spec_text(path: Path) -> str:
    """Read the *spec text* out of a frozen snapshot (never the wrapper ref)."""
    import json

    payload = json.loads(path.read_text())
    require(
        isinstance(payload, dict) and isinstance(payload.get("text"), str),
        "SPEC_CHANGED",
        "frozen member spec has an unexpected shape",
    )
    return payload["text"]


def _release_start(store: Store, group_id: str, task_key: str, reason: str, code: str) -> None:
    """Return a reserved start that never produced an Agent process.

    ``starts_reserved`` 是**单调的预留记录**，释放只增加 ``starts_released``：
    billable 由两者之差推导。旧实现同时 ``reserved -= 1``，一次释放把 billable
    扣两次，``max_starts`` 于是被系统性放宽（每次释放多退一个名额）。
    """
    group = store.group(group_id)
    budget = group["runtime"]["budget"]
    if starts_billable(group) <= 0:
        return
    basis = {
        "task_key": task_key,
        "reason": reason,
        "code": code,
        "at": time.time(),
    }
    basis_key = digest([task_key, reason, code])
    if any(item.get("basis_key") == basis_key for item in budget["release_basis"]):
        return
    basis["basis_key"] = basis_key
    budget["starts_released"] += 1
    budget["release_basis"].append(basis)
    with store.tx():
        store.save_group(group, "group_start_released")


def release_unstarted(store: Store, group: dict) -> dict:
    """Release reservations whose runs never reached ``launch_stage`` (TGD §7.3).

    The evidence is the run record itself: ``launch_stage`` absent means no agent
    process was ever created, so the start is not billable. Each run_id releases
    at most once, keyed inside ``release_basis``.
    """
    # 修复单元同样会预留 start：只遍历 members 会让"从未 launch 的 repair"
    # 永远占着一个计费名额（reconcile 用的是 ``all_runtimes``，两处必须一致）。
    for runtime in _groups.all_runtimes(group):
        task_id = runtime.get("task_id")
        if not task_id:
            continue
        try:
            task = store.task(task_id)
        except Error:
            continue
        run = store.get_run(task.get("current_run_id")) if task.get("current_run_id") else None
        if not run:
            continue
        if run.get("launch_stage") not in (None, ""):
            continue
        if run.get("status") in ("reserved", "starting", "running", "stopping", "unknown"):
            continue
        budget = group["runtime"]["budget"]
        basis_key = "run:" + run["run_id"]
        if any(item.get("basis_key") == basis_key for item in budget["release_basis"]):
            continue
        if starts_billable(group) <= 0:
            continue
        # 只记"这一次没有计费"，不动预留计数（见 `_release_start` 的说明）。
        budget["starts_released"] += 1
        budget["release_basis"].append(
            {
                "basis_key": basis_key,
                "run_id": run["run_id"],
                "task_key": runtime["task_key"],
                "reason": "run never reached launch_stage",
                "at": time.time(),
            }
        )
        with store.tx():
            store.save_group(group, "group_start_released")
    return group


# --------------------------------------------------------------------------- #
# reconcile / derive
# --------------------------------------------------------------------------- #


def reconcile(store: Store, group: dict) -> dict:
    """Close out ledgers and member runs. Never dispatches (TGD §7.5)."""
    from src import _pipeline

    group = _integration.reconcile(store, group)
    for runtime in _groups.all_runtimes(group):
        if not runtime.get("task_id"):
            continue
        try:
            task = store.task(runtime["task_id"])
            reconciled = _pipeline.reconcile(store, task)
        except Error:
            continue
        runtime["validated_sha"] = reconciled.get("validated_sha")
        runtime["last_block"] = (
            {"code": reconciled.get("block_code"), "message": reconciled.get("last_error")}
            if reconciled.get("block_code")
            else None
        )
        if reconciled.get("state") == "CANCELLED":
            group["runtime"]["blockers"] = [
                {
                    "member": runtime["task_key"],
                    "code": "DEPENDENCY_BLOCKED",
                    "reason": "a member was cancelled; the group cannot complete",
                }
            ]
    group = release_unstarted(store, group)
    return _derive(store, group, None)


def _derive(store: Store, group: dict, gate: dict | None) -> dict:
    from src import _review

    views = _groups.members_view(store, group)
    state, blockers = _groups.derive_group_state(group, views, review=_review.summary(store, group))
    if gate and gate.get("blocked") and state not in ("REVIEW_READY", "DONE", "CANCELLED"):
        state = "BLOCKED"
        blockers = list(blockers) + [dict(gate["blocked"], scope="dispatch")]
    group["state"] = state
    group["runtime"]["blockers"] = blockers
    if state in _groups._GROUP_TERMINAL:
        group["runtime"]["budget"]["finalized_at"] = (
            group["runtime"]["budget"].get("finalized_at") or time.time()
        )
    return group


def _persist(store: Store, group: dict) -> None:
    group["updated_at"] = time.time()
    with store.tx():
        store.save_group(group, "group_progress")
    try:
        _groups.write_progress(store, group)
    except OSError:
        pass


def _has_dispatchable_or_active(store: Store, group: dict, gate: dict | None = None) -> bool:
    if group["state"] in ("PAUSED", "CANCELLED", "DONE", "REVIEW_READY"):
        return False
    if gate is None:
        gate = dispatch_gate(store, group)
    if ready_members(group) and gate.get("ok"):
        return True
    return any(
        view["state"] in ("EXECUTING", "VALIDATED") for view in _groups.members_view(store, group)
    )


# --------------------------------------------------------------------------- #
# runner loop
# --------------------------------------------------------------------------- #


def run(store: Store, group_id: str, lease_id: str, max_ticks: int | None = None) -> dict:
    """The child runner: take over the locks, CAS the lease, tick until a stop原因.

    退出条件是**公开契约**（v1.4.1 §runner 生命周期）：终态、显式暂停、真实
    阻塞、已声明预算、租约被接管、或本轮无可推进项。``max_ticks`` 只是测试与
    兜底用的上限 —— 省略时用 ``SAFETY_MAX_TICKS``，命中时原因记为
    ``safety_cap`` 并在返回值里说明「推进停止、任务仍未完成」。

    推进器自身抛出未预期异常时记为 ``crashed``（属于 ``_NEEDS_HUMAN``）：
    "重启就能接着做"只对 ``no_progress`` / ``slots`` / ``budget_exhausted``
    这类**已判定**的停止成立，不能把崩溃也算进去。
    """
    limit = SAFETY_MAX_TICKS if max_ticks is None else int(max_ticks)
    require(limit >= 1, "INPUT_INCOMPLETE", "max_ticks must be >= 1")
    with runner_locks(store, group_id, wait=RUNNER_LOCK_WAIT):
        attach(store, group_id, lease_id)
        ticks = 0
        reason = "no_progress"
        failure: BaseException | None = None
        try:
            while ticks < limit:
                ticks += 1
                try:
                    advance = tick(store, group_id, lease_id)
                except Error as exc:
                    if (exc.details or {}).get("kind") == "lease_stale":
                        reason = "lease_stale"
                        break
                    raise
                if not advance:
                    reason = stop_reason(store.group(group_id))
                    break
                time.sleep(TICK_INTERVAL)
            else:
                reason = "safety_cap"
        except BaseException as exc:
            # 未预期异常（记录损坏、状态机冲突、磁盘/权限错误、Ctrl-C）不是"本轮
            # 没有可推进项"：记成 no_progress 会落进 `_RESUMABLE`，宿主于是重启一个
            # 必然再崩的推进器，而真正的错误只留在 stderr 里。
            reason = "crashed"
            failure = exc
            raise
        finally:
            try:
                _release_lease(store, group_id, lease_id, ticks, reason)
            except Exception:
                # 归还租约失败时（记录被 cancel/迁移重写）不得顶掉正在传播的原始
                # 异常；只有它自己才是唯一的错误时才向上抛。
                if failure is None:
                    raise
    group = store.group(group_id)
    return {
        "group_id": group_id,
        "state": group["state"],
        "lease_id": lease_id,
        "ticks": ticks,
        "exit_reason": reason,
        "completed": group["state"] == "DONE",
        "progress": _groups.progress(store, group),
        "stop": {
            "reason": reason,
            "explain": EXIT_REASONS.get(reason, ""),
            "resumable": reason in _RESUMABLE,
            "needs_human": reason in _NEEDS_HUMAN,
            # 推进停止 ≠ 任务失败：宿主据此决定是续跑、拍板还是收工。
            # 提示按原因给（review/accept/repair/status），不再一律 start——
            # start 在 REVIEW_READY 上会被状态机拒绝。
            "resume_hint": _resume_hint(group_id, reason),
        },
    }


def _release_lease(
    store: Store, group_id: str, lease_id: str, ticks: int, reason: str = "no_progress"
) -> None:
    group = store.group(group_id)
    lease = group["runtime"].get("lease") or {}
    if lease.get("lease_id") != lease_id:
        # A newer runner owns the record; do not touch it.
        return
    lease.update(
        state="exited",
        exited_at=time.time(),
        ticks=ticks,
        exit_reason=reason,
    )
    group["runtime"]["lease"] = lease
    with store.tx():
        store.save_group(group, "group_runner_exited")


# --------------------------------------------------------------------------- #
# pause / resume / cancel                                                (7.4)
# --------------------------------------------------------------------------- #


def pause(store: Store, group_id: str, req: dict, stop: bool = False) -> dict:
    """Stop dispatching. Drain by default; ``stop`` also halts active members."""
    with store.lock():
        key, prior = store.operation(req, "group_pause", group_id)
        if prior:
            return prior
        try:
            group = store.group(group_id)
            _groups.require_v3(group, "pause")
            authorize(store.config(group["project_id"]), req, "execute")
            require(
                group["row_version"] == req.get("expected_version"),
                "STATE_CONFLICT",
                "Refresh expected_version",
            )
            require(
                group["state"] in ("RUNNING", "BLOCKED", "READY"),
                "STATE_CONFLICT",
                f"cannot pause a group in state {group['state']}",
            )
            active = _active_run_ids(store, group)
            stopped = []
            if stop:
                for task_id, run_id in active:
                    stopped.append(_stop_member(store, task_id, run_id))
                active = _active_run_ids(store, group)
            group["runtime"]["draining"] = bool(active) and not stop
            if not active:
                group["state"] = "PAUSED"
            groups_budget = group["runtime"]["budget"]
            groups_budget.setdefault("paused_at", time.time())
            result = {
                "api_version": 2,
                "operation_id": req.get("request_id"),
                "status": "succeeded",
                "group_id": group_id,
                "state": group["state"],
                **_groups.envelope(store, group),
                "draining": group["runtime"]["draining"],
                "active_run_ids": [run_id for _, run_id in active],
                "stopped": stopped,
                "note": "a paused group stops new dispatch; this is not proof that "
                "no child process is alive",
            }
            with store.tx():
                store.save_group(group, "group_paused")
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


def resume(store: Store, group_id: str, req: dict, spawn=None) -> dict:
    """Re-preflight, re-reconcile, then hand the group a new lease.

    Resume deliberately reuses the question "may this group run at all?" instead
    of answering it again: the preflight is the same function ``group start``
    uses, and the new lease is minted by ``begin`` — one handshake, not two.
    """
    with store.lock():
        key, prior = store.operation(req, "group_resume", group_id)
        if prior:
            return prior
        try:
            group = store.group(group_id)
            _groups.require_v3(group, "resume")
            config = store.config(group["project_id"])
            authorize(config, req, "execute")
            require(
                group["state"] in ("PAUSED", "BLOCKED", "RUNNING", "READY"),
                "STATE_CONFLICT",
                f"cannot resume a group in state {group['state']}",
            )
            reconciled = reconcile(store, group)
            preflight = _preflight(store, reconciled, config)
            lease = reconciled["runtime"].get("lease") or {}
            if lease.get("state") == "spawn_observed":
                observable = _identity_observable(lease.get("pid"), lease.get("start_token"))
                if observable:
                    raise Error(
                        "SLOT_BUSY",
                        "a runner is still observed; `group pause --stop` first",
                        details={"kind": "group_runner", "lease": lease},
                    )
                if observable is None:
                    raise Error(
                        "PROCESS_UNKNOWN",
                        "the previous runner cannot be observed; confirm before resuming",
                        details={"kind": "runner_unknown"},
                    )
            reconciled["runtime"]["policy_snapshot"] = {
                **(reconciled["runtime"].get("policy_snapshot") or {"routes": {}}),
                "policy_revision": digest(config)[:16],
            }
            reconciled["runtime"]["draining"] = False
            # `begin` only accepts READY/PAUSED/BLOCKED; after a successful
            # preflight the only honest starting point is "not running".
            reconciled["state"] = "PAUSED"
            with store.tx():
                store.save_group(reconciled, "group_reconciled")
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
    start_req = dict(req)
    start_req["request_id"] = str(req.get("request_id")) + "-start"
    try:
        started = begin(store, group_id, start_req, spawn=spawn)
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
    merged = {
        **started,
        "operation_id": req.get("request_id"),
        "reconciled": True,
        "preflight": preflight,
        "resumed": True,
        "start_request_id": start_req["request_id"],
    }
    store.finish(key, merged)
    return merged


def cancel(store: Store, group_id: str, req: dict, stop: bool = False) -> dict:
    """Stop dispatching and close the group after confirming no active writer."""
    with store.lock():
        key, prior = store.operation(req, "group_cancel", group_id)
        if prior:
            return prior
        try:
            group = store.group(group_id)
            _groups.require_v3(group, "cancel")
            authorize(store.config(group["project_id"]), req, "execute")
            require(
                group["state"] not in ("DONE", "CANCELLED"),
                "STATE_CONFLICT",
                "group is already terminal",
            )
            active = _active_run_ids(store, group)
            stopped = []
            if stop:
                for task_id, run_id in active:
                    stopped.append(_stop_member(store, task_id, run_id))
                active = _active_run_ids(store, group)
            if active:
                result = {
                    "api_version": 2,
                    "operation_id": req.get("request_id"),
                    "status": "accepted",
                    "accepted": True,
                    "completed": False,
                    "group_id": group_id,
                    "state": group["state"],
                    **_groups.envelope(store, group),
                    "draining": True,
                    "active_run_ids": [run_id for _, run_id in active],
                    "stopped": stopped,
                    "note": "an agent run is still active; cancel does not pretend it ended",
                }
                store.finish(key, result)
                return result
            group["state"] = "CANCELLED"
            group["runtime"]["budget"]["finalized_at"] = time.time()
            group["runtime"]["cancel"] = {
                "reason": req.get("reason"),
                "actor": req.get("actor"),
                "at": time.time(),
            }
            result = {
                "api_version": 2,
                "operation_id": req.get("request_id"),
                "status": "succeeded",
                "accepted": True,
                "completed": True,
                "group_id": group_id,
                "state": "CANCELLED",
                **_groups.envelope(store, group),
                "stopped": stopped,
                "active_run_ids": [],
                "note": "cancelled inside Zoe; this is not an acceptance",
            }
            with store.tx():
                store.save_group(group, "group_cancelled")
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


def _active_run_ids(store: Store, group: dict) -> list[tuple[str, str]]:
    active = []
    # 修复单元也是真实的写者。漏掉它们时，``group cancel`` 会在 repair 仍在写
    # 工作区的情况下判定"无活跃写者"并直接置 CANCELLED。
    for runtime in _groups.all_runtimes(group):
        task_id = runtime.get("task_id")
        if not task_id:
            continue
        try:
            task = store.task(task_id)
        except Error:
            continue
        if task.get("state") not in ("RUNNING", "PAUSING"):
            continue
        run_id = task.get("current_run_id")
        if run_id:
            active.append((task_id, run_id))
    return active


def _stop_member(store: Store, task_id: str, run_id: str) -> dict:
    """Stop one member's process tree through the existing platform layer."""
    from src._platform.base import ProcessHandle

    run = store.get_run(run_id) or {}
    plat = get_platform()
    handle = ProcessHandle(
        pid=run.get("pid"),
        start_token=run.get("start_token") or run.get("identity"),
        group_token=run.get("group_token"),
        popen=None,
        platform=plat,
    )
    try:
        result = plat.stop_tree(handle, timeout=10)
    except Error as exc:
        return {"task_id": task_id, "run_id": run_id, "stopped": False, "error": str(exc)}
    return {
        "task_id": task_id,
        "run_id": run_id,
        "stopped": result.get("remaining") == 0,
        "remaining": result.get("remaining"),
    }


# --------------------------------------------------------------------------- #
# diagnostics
# --------------------------------------------------------------------------- #


def tick_snapshot(store: Store, group_id: str) -> dict:
    """Read-only view used by ``group plan GID`` and tests: never dispatches."""
    group = store.group(group_id)
    _groups.require_v3(group, "plan")
    gate = dispatch_gate(store, group)
    views = _groups.members_view(store, group)
    return {
        "group_id": group_id,
        "state": group["state"],
        "slots": slots_available(store, group),
        "gate": gate,
        "starts_billable": starts_billable(group),
        "max_starts": group["definition"]["budget_caps"]["max_starts"],
        "wall_seconds_inclusive": wall_seconds(group),
        "ready": ready_members(group),
        "members": [{"task_key": view["task_key"], "state": view["state"]} for view in views],
        "dispatchable_now": [{"item_id": key, "task_key": key} for key in ready_members(group)]
        if group["state"] in ("READY", "RUNNING") and gate["ok"]
        else [],
        "blocked": [
            {"item_id": view["task_key"], "code": view["reason"] or view["state"]}
            for view in views
            if view["state"] in ("BLOCKED", "WAITING", "INVALIDATED")
        ],
    }
