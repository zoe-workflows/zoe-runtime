"""Project configuration validation and policy checks."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from src import _discovery, _paths, _profiles
from src._proc import process_identity
from src._util import VERSION, Error, git, is_integer, require, safe_id

#: 跨任务并发上限。单一写者不变量按任务生效，这里限制的是同时活跃的 Run 数量。
MAX_ACTIVE_TASKS = 8

#: 单次 run 的墙钟上限（``timeout_seconds``），**项目级**、不是任务级也不是
#: profile 级。三个常量放在这里是因为：校验（注册期）、生效值（执行期）、回显
#: （doctor）必须读同一份定义，否则"配的"和"跑的"会悄悄分叉。
MIN_RUN_TIMEOUT_SECONDS = 60
MAX_RUN_TIMEOUT_SECONDS = 86400
DEFAULT_RUN_TIMEOUT_SECONDS = 3600

#: v1.5.0 M1：配置模板里的占位符。必须在**派发之前**被发现：如果等到 Agent 启动
#: 后才发现，用户看到的是 AGENT_FAILED（或"证据文件不存在"），而不是"这个字段
#: 还没填"——那正是要消灭的配置摩擦。
PLACEHOLDER_MARKERS = ("REPLACE_WITH_", "<FILL:", "/absolute/path/to/")

#: 需要扫描占位符的字段。只扫会进入执行/授权的字段，不扫 `description` 这类自由
#: 文本：说明性文字里出现 "REPLACE_WITH_" 不该阻断。
_PLACEHOLDER_PROFILE_FIELDS = (
    "model",
    "provider",
    "expected_version",
    "endpoint_ref",
    "compatibility_evidence",
    "image",
    "credential_refs",
)
_PLACEHOLDER_TOP_FIELDS = ("authorization_ref", "repo")

#: v0.5.0 起 Docker 退出产品支持范围（§18）。拒绝是**显式**的：既不改写配置，
#: 也不降级成 local-process 继续跑，否则等于悄悄换了隔离边界。
SUPPORTED_BACKENDS = ("local-process",)

#: 授权动作的完整取值集合（与 ``schemas/project-profile.v2.json`` 的 enum 一致）。
#: 未列出的动作永远不会被授权，拼错的动作名必须是注册期错误而不是"静默拒绝"。
AUTHORIZED_ACTIONS = ("create", "execute", "publish", "accept")

_DOCKER_MIGRATION = (
    '把配置里的 "backend" 改为 "local-process"，并删除 "image"；'
    "历史 docker 任务只读保留（status/events/logs/report），需新建 task 重新冻结基线。"
)

#: Shells whose inner command cannot be resolved statically. We verify only the
#: shell itself: variable assignments, pipes, conditionals and quoting make
#: "look at the first inner word" wrong more often than right (v2 §4-3).
SHELL_BASENAMES = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "ksh",
        "fish",
        "csh",
        "tcsh",
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
    }
)

#: Directory names that mark a tool produced by an install/build step. Missing at
#: ``doctor`` time is expected, so they are reported as ``deferred``, not broken.
#: Only the **relative** form the author wrote is inspected: matching the
#: resolved absolute path would also classify ``/usr/bin/go`` as generated.
GENERATED_DIRS = frozenset(
    {"bin", "node_modules", "target", "dist", "build", ".venv", "venv", "vendor", "obj", "out"}
)


def _which(program: str, search_path: str | None = None) -> str | None:
    """Resolve a bare name under the *execution* PATH, never a guessed one."""
    return shutil.which(program, path=search_path) if search_path else shutil.which(program)


def resolve_check(argv, *, cwd=None, search_path: str | None = None) -> dict:
    """Resolve ``argv[0]`` the way execution would, without running anything.

    Status values, and what each one means for a gate:

    ``ok``         resolvable now — safe to require.
    ``shell``      the program is a shell; the inner command is opaque by design
                   and is **not** parsed. Execution may still fail, and the
                   report must say the check could not be verified statically.
    ``deferred``   a tool produced by an install/build step (``node_modules/.bin``,
                   ``bin/`` …). Absent now is expected: never a config error.
    ``unresolved`` a bare name that is not on ``PATH`` — this *is* an environment
                   problem and is worth stopping before an agent spends a run.
    ``invalid``    empty argv (should have been rejected at registration).

    ``cwd`` is the directory the check would run in; relative paths resolve
    against it, not against the controller's own working directory.
    """
    if not isinstance(argv, list | tuple) or not argv or not isinstance(argv[0], str):
        return {
            "argv0": None,
            "resolved_path": None,
            "status": "invalid",
            "hint": "check argv is empty or malformed",
        }
    program = argv[0]
    basename = os.path.basename(program).lower()
    if basename in SHELL_BASENAMES:
        resolved = None
        if os.path.sep in program:
            candidate = Path(program)
            if not candidate.is_absolute() and cwd:
                candidate = Path(cwd) / candidate
            resolved = str(candidate) if candidate.exists() else None
        else:
            resolved = _which(program, search_path)
        return {
            "argv0": program,
            "resolved_path": resolved,
            "status": "shell" if resolved else "unresolved",
            "inner_command": "unverified",
            "hint": (
                "shell form: only the shell itself is verified; the inner command "
                "cannot be resolved statically"
            ),
        }
    has_separator = os.path.sep in program or (os.altsep is not None and os.altsep in program)
    if has_separator:
        relative = Path(program)
        absolute = relative if relative.is_absolute() else Path(cwd or os.getcwd()) / relative
        if absolute.exists():
            return {"argv0": program, "resolved_path": str(absolute), "status": "ok", "hint": None}
        generated = not relative.is_absolute() and any(
            part in GENERATED_DIRS for part in relative.parts
        )
        if generated:
            return {
                "argv0": program,
                "resolved_path": str(absolute),
                "status": "deferred",
                "hint": "tool is expected to be produced by an install/build step",
            }
        return {
            "argv0": program,
            "resolved_path": str(absolute),
            "status": "unresolved",
            "hint": "path does not exist",
        }
    found = _which(program, search_path)
    if found:
        return {"argv0": program, "resolved_path": found, "status": "ok", "hint": None}
    return {
        "argv0": program,
        "resolved_path": None,
        "status": "unresolved",
        "hint": "bare name is not on PATH; declare an installed tool or run it via a shell",
    }


def checks_preflight(c: dict[str, Any]) -> list[dict]:
    """Resolve every declared check's ``argv[0]`` (read-only, no execution)."""
    rows = []
    for check in c.get("checks") or []:
        if not isinstance(check, dict):
            continue
        row = resolve_check(check.get("argv"), cwd=c.get("repo"))
        row["check_id"] = check.get("id")
        rows.append(row)
    return rows


def docker_backend_error(where: str = "project") -> Error:
    """The single refusal for a Docker backend, configuration or frozen task."""
    message = (
        "Docker 后端已在 v0.5.0 退出产品支持范围；本版只支持 local-process 执行。"
        if where == "project"
        else "该任务冻结的执行后端是 docker，已在 v0.5.0 退出支持；历史记录只读，"
        "继续执行需新建 task。"
    )
    return Error(
        "CAPABILITY_UNSUPPORTED",
        message,
        details={
            "configured_backend": "docker",
            "supported_backends": list(SUPPORTED_BACKENDS),
            "title": "Docker execution backend removed in v0.5.0",
            "migration": _DOCKER_MIGRATION,
            "scope": where,
        },
    )


def refuse_docker_backend(c: dict[str, Any], *, where: str = "project") -> None:
    """Reject a Docker backend without rewriting it to ``local-process``.

    Called from configuration validation *and* from ``start``/``run_pipeline`` so
    a task frozen before the change cannot silently continue on another
    execution backend (§18.2 B1, §18.4).
    """
    if c.get("backend", "local-process") == "docker":
        raise docker_backend_error(where)


def _default_deny_prefixes():
    """Paths the controller must never execute in, independent of operator config.

    These are the controller's own runtime locations, resolved to real paths.
    ``deny_repo_prefixes`` in config only *adds* to this list; it cannot relax it.
    """
    prefixes = []
    here = Path(__file__).resolve()
    for candidate in (here, here.parent, here.parent.parent):
        if candidate.name in ("src",) or candidate.name.endswith("site-packages"):
            prefixes.append(str(candidate))
            break
    prefixes.append(str(here.parent))  # package root
    return prefixes


def _is_within(path: Path, prefixes):
    p = path.resolve()
    for prefix in prefixes:
        try:
            if p == Path(prefix).resolve() or p.is_relative_to(Path(prefix).resolve()):
                return True
        except (ValueError, OSError):
            continue
    return False


def _git_tracked(repo: Path, path: str) -> bool | None:
    """Whether ``git ls-files`` reports anything under ``path``.

    ``None`` means "cannot judge" (not a git repository, git unavailable) and
    must never be turned into a warning: the absence of an answer is not a
    finding.
    """
    try:
        out = git(str(repo), "ls-files", "--", path)
    except (Error, OSError):
        return None
    return bool(out.strip())


def path_warnings(c: dict[str, Any]) -> list[dict[str, Any]]:
    """Advisory findings about declared paths — never a verdict (v5 §B5).

    A missing directory may be created by the task, so nothing here blocks a
    registration. It exists because a typo in ``allowed_paths`` is otherwise
    only discovered after an agent has finished an entire run.
    """
    warnings: list[dict[str, Any]] = []
    repo = Path(c["repo"])
    try:
        repo_resolved = repo.resolve()
    except OSError:
        repo_resolved = repo
    for index, entry in enumerate(c.get("allowed_paths") or []):
        field = f"allowed_paths[{index}]"
        normalized = _paths.normalize(entry)
        reason = _paths.describe_missing(entry)
        if reason:
            warnings.append(
                {"field": field, "code": "SCOPE_INVALID", "message": f"'{entry}': {reason}"}
            )
            continue
        if normalized in ("", "."):
            warnings.append(
                {
                    "field": field,
                    "code": "SCOPE_COVERS_REPO",
                    "message": f"'{entry}' covers the whole repository",
                }
            )
            continue
        candidate = Path(normalized)
        if not candidate.is_absolute():
            candidate = repo / normalized
        if not candidate.exists():
            warnings.append(
                {
                    "field": field,
                    "code": "SCOPE_MISSING",
                    "message": f"'{entry}' does not exist under the repository",
                }
            )
            continue
        try:
            if candidate.resolve() == repo_resolved:
                warnings.append(
                    {
                        "field": field,
                        "code": "SCOPE_COVERS_REPO",
                        "message": f"'{entry}' resolves to the repository root",
                    }
                )
                continue
        except OSError:
            pass
        if _git_tracked(repo, str(candidate)) is False:
            warnings.append(
                {
                    "field": field,
                    "code": "SCOPE_UNTRACKED",
                    "message": f"'{entry}' has no git-tracked files",
                }
            )
    return warnings


class Diagnostics:
    """Collect independent configuration problems instead of stopping at the first.

    Only checks that do not depend on an earlier field being valid are collected
    here. Structural gates (``schema_version`` / ``project_id`` / ``repo`` /
    backend) still raise immediately: without a repository a path check has no
    meaning, and reporting its failure would manufacture a second error out of
    the first one (config-friction v5 §B1).

    The published contract is preserved: the top-level ``code`` and ``message``
    are the **first** problem — exactly what sequential validation reported —
    and the full list is additive in ``details.errors``.
    """

    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []
        self.skipped: list[dict[str, Any]] = []

    def check(
        self,
        ok,
        code: str,
        message: str,
        field: str,
        *,
        reason: str | None = None,
        suggested_change: str | None = None,
        requires_approval: bool = False,
    ) -> bool:
        if ok:
            return True
        self.errors.append(
            {
                "code": code,
                "field": field,
                "message": message,
                "reason": reason or message,
                "suggested_change": suggested_change,
                "requires_approval": bool(requires_approval),
            }
        )
        return False

    def skip(self, field: str, reason: str) -> None:
        """Record a check that could not run because an earlier field was invalid."""
        self.skipped.append({"field": field, "reason": reason})

    def raise_if_any(self) -> None:
        if not self.errors:
            return
        first = self.errors[0]
        raise Error(
            first["code"],
            first["message"],
            details={
                "count": len(self.errors),
                "errors": list(self.errors),
                "skipped": list(self.skipped),
            },
        )


def _placeholder_hits(value: Any, field: str) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    if isinstance(value, str):
        for marker in PLACEHOLDER_MARKERS:
            if marker in value:
                hits.append({"field": field, "marker": marker})
                break
    elif isinstance(value, list):
        for index, item in enumerate(value):
            hits.extend(_placeholder_hits(item, f"{field}[{index}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            hits.extend(_placeholder_hits(item, f"{field}.{key}"))
    return hits


def find_placeholders(c: dict[str, Any]) -> list[dict[str, str]]:
    """Return unresolved template placeholders as ``{field, marker}`` (v1.5.0 M1)."""
    hits: list[dict[str, str]] = []
    profiles = c.get("agent_profiles")
    if isinstance(profiles, dict):
        for profile_id, profile in profiles.items():
            if not isinstance(profile, dict):
                continue
            for field in _PLACEHOLDER_PROFILE_FIELDS:
                hits.extend(
                    _placeholder_hits(profile.get(field), f"agent_profiles.{profile_id}.{field}")
                )
    for field in _PLACEHOLDER_TOP_FIELDS:
        hits.extend(_placeholder_hits(c.get(field), field))
    return hits


def require_no_placeholders(c: dict[str, Any]) -> None:
    """Refuse a configuration that still carries template placeholders."""
    hits = find_placeholders(c)
    if not hits:
        return
    fields = sorted({hit["field"] for hit in hits})
    raise Error(
        "INPUT_INCOMPLETE",
        "Configuration still contains template placeholders: " + ", ".join(fields),
        details={"fields": hits},
    )


def run_timeout_seconds(c: dict[str, Any]) -> int:
    """Return the effective wall-clock ceiling for one run (seconds).

    **One** definition for three readers: registration-time validation, the
    execution monitoring loop and ``doctor``'s echo. ``timeout_seconds`` is a
    project-level field; there is deliberately no per-task override (a task-level
    knob would make ``policy_digest`` describe half a policy). A missing field
    means the default; an unusable value also means the default, because
    ``validate_config`` refuses it on the write path — here we must never raise
    from inside the monitoring loop.
    """
    value = c.get("timeout_seconds", DEFAULT_RUN_TIMEOUT_SECONDS)
    if not is_integer(value):
        return DEFAULT_RUN_TIMEOUT_SECONDS
    if not MIN_RUN_TIMEOUT_SECONDS <= value <= MAX_RUN_TIMEOUT_SECONDS:
        return DEFAULT_RUN_TIMEOUT_SECONDS
    return int(value)


def validate_config(
    c: dict[str, Any],
    base_dir: Path | str | None = None,
    *,
    strict: bool = True,
    problems: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a project profile and return it (normalized, ``repo`` resolved).

    ``base_dir`` anchors relative paths such as ``compatibility_evidence``; it is
    the directory of the configuration file being registered (or the project
    store directory when re-read from disk).

    ``strict`` 决定**策略性**校验是否抛错（v1.5.0 M0）。读路径用 ``strict=False``：
    结构问题照旧抛（没有 repo、没有 checks 的配置确实不可用），但 v1.5.0 新增的
    模型/授权规则只记进 ``problems``，不阻断读取——否则旧配置升级后连
    ``status`` / ``doctor`` 都会打不开，用户既不能执行也无法排障。写路径
    （``project register``）与执行入口（``check_execution_contract``）仍然严格。
    """
    require(
        is_integer(c.get("schema_version")) and c.get("schema_version") in (1, 2),
        "INPUT_INCOMPLETE",
        f"Config schema_version must be 1 or 2, got {c.get('schema_version')!r}",
    )
    # 占位符必须先于路径解析与绑定：否则 repo=/absolute/path/to/repo 会先报
    # "Repository missing"，compatibility_evidence 的占位符会先报"证据不存在"，
    # 用户看不到"请填写这个字段"。
    if strict:
        require_no_placeholders(c)
    elif problems is not None:
        # 与其它 policy_problems 同构（{code, field, message}）：消费方按
        # ``problem["code"]`` 取值，结构不一致会 KeyError。
        problems.extend(
            {
                "code": "INPUT_INCOMPLETE",
                "field": hit["field"],
                "message": f"placeholder {hit['marker']} is unresolved",
            }
            for hit in find_placeholders(c)
        )
    safe_id(c.get("project_id"))
    repo = Path(c.get("repo", "")).expanduser().resolve()
    require(repo.is_dir(), "INPUT_INCOMPLETE", "Repository missing")
    # Refuse to execute inside the controller's own runtime locations. Operator
    # deny_repo_prefixes only appends; it cannot override these defaults.
    declared_deny = c.get("deny_repo_prefixes", [])
    require(
        isinstance(declared_deny, list) and all(isinstance(p, str) for p in declared_deny),
        "INPUT_INCOMPLETE",
        "deny_repo_prefixes must be an array of paths",
    )
    deny = _default_deny_prefixes() + [str(Path(p).expanduser().resolve()) for p in declared_deny]
    require(
        not _is_within(repo, deny),
        "POLICY_DENIED",
        "Do not execute inside the controller or denied paths",
    )
    c["repo"] = str(repo)
    # v0.5.0 只支持本地进程执行。旧 Docker 配置必须显式报错，不得在内存里改写成
    # local-process（§18.2-B1）。``local`` 是 ``local-process`` 的遗留别名，
    # 归一化它属于既有语义，不是后端切换。
    refuse_docker_backend(c)
    backend = c.get("backend", "local-process")
    if backend == "local":
        backend = "local-process"
        c["backend"] = backend
    require(
        backend in SUPPORTED_BACKENDS,
        "INPUT_INCOMPLETE",
        "backend must be 'local-process'",
    )
    # local-process 沿用本机网络配置，不做出口限制；host/container 命名空间
    # 只对已移除的容器后端有意义，仍然拒绝以免旧配置被当成有效授权。
    network = c.get("network", "none")
    # 类型先行：``network: null`` 曾让 ``.startswith`` 抛 AttributeError，
    # 用户看到的是 INTERNAL_ERROR 而不是"这个字段填错了"。
    require(
        isinstance(network, str),
        "INPUT_INCOMPLETE",
        f"network must be a string, got {network!r}",
    )
    require(
        network != "host" and not network.startswith("container:"),
        "POLICY_DENIED",
        "Host/shared container networking is prohibited",
    )
    # 并发上限：单一写者不变量仍按任务生效（每个任务独占自己的 writer 锁与
    # worktree），这里放开的是"跨任务并行"。默认仍为 1，保持既有部署行为。
    # 只做校验，不改写配置：规范化（含默认值）由 normalize_config 统一负责，
    # 否则任务冻结配置与项目配置的 policy_digest 会不一致。
    diagnostics = Diagnostics()
    limit = c.get("max_active_tasks", 1)
    diagnostics.check(
        isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= MAX_ACTIVE_TASKS,
        "INPUT_INCOMPLETE",
        f"max_active_tasks must be an integer between 1 and {MAX_ACTIVE_TASKS}",
        "max_active_tasks",
        suggested_change=f"use an integer between 1 and {MAX_ACTIVE_TASKS}",
    )
    # v1.5.2 B3：``timeout_seconds`` 是项目级墙钟上限，此前**完全未校验**——写成
    # ``"3600"`` 之类的形态不会被注册/预检拒绝，只会在 run 的监视循环里以
    # ``TypeError`` 的形式炸掉（失败的时机还取决于走到哪个分支）。配置类型错误
    # 必须在这一层暴露，而不是等到执行期。
    #
    # 只校验、不改写：生效值由 ``run_timeout_seconds()`` 统一给出。在这里补默认值
    # 会改变 ``policy_digest``，让一次纯读取变成策略变更。
    #
    # 写入/执行路径（``strict``）拒绝；读路径（``doctor`` / ``status``）只记进
    # ``problems``：否则一份历史配置里写错一个字段，会连"看一眼为什么错"都做不到
    # ——doctor 本身就打不开了。
    if "timeout_seconds" in c:
        declared_timeout = c["timeout_seconds"]
        timeout_ok = (
            is_integer(declared_timeout)
            and MIN_RUN_TIMEOUT_SECONDS <= declared_timeout <= MAX_RUN_TIMEOUT_SECONDS
        )
        timeout_message = (
            f"timeout_seconds must be an integer number of seconds between "
            f"{MIN_RUN_TIMEOUT_SECONDS} and {MAX_RUN_TIMEOUT_SECONDS}, "
            f"got {declared_timeout!r}"
        )
        if strict:
            diagnostics.check(
                timeout_ok,
                "INPUT_INCOMPLETE",
                timeout_message,
                "timeout_seconds",
                suggested_change=(
                    f"use an integer between {MIN_RUN_TIMEOUT_SECONDS} and "
                    f"{MAX_RUN_TIMEOUT_SECONDS} (seconds)"
                ),
            )
        elif problems is not None and not timeout_ok:
            problems.append(
                {
                    "code": "INPUT_INCOMPLETE",
                    "field": "timeout_seconds",
                    "message": timeout_message,
                }
            )
    checks_declared = isinstance(c.get("checks"), list) and bool(c.get("checks"))
    diagnostics.check(
        checks_declared,
        "INPUT_INCOMPLETE",
        "At least one independent check is required",
        "checks",
        suggested_change='add {"id": "<name>", "argv": ["<tool>", ...]}',
    )
    if checks_declared:
        for index, check in enumerate(c["checks"]):
            field = f"checks[{index}]"
            if not isinstance(check, dict):
                diagnostics.check(False, "INPUT_INCOMPLETE", "Check must be an object", field)
                continue
            diagnostics.check(
                isinstance(check.get("id"), str) and bool(check.get("id")),
                "INPUT_INCOMPLETE",
                "Check requires an id",
                field + ".id",
            )
            argv_ok = (
                isinstance(check.get("argv"), list)
                and check["argv"]
                and all(isinstance(x, str) for x in check["argv"])
            )
            diagnostics.check(
                argv_ok,
                "INPUT_INCOMPLETE",
                "Checks require argv arrays",
                field + ".argv",
                suggested_change='use an argv array, e.g. ["go", "build", "./..."]',
            )
    else:
        diagnostics.skip(
            "checks[*]", "checks must be a non-empty array before per-check fields can be judged"
        )
    diagnostics.check(
        isinstance(c.get("allowed_paths"), list) and bool(c.get("allowed_paths")),
        "INPUT_INCOMPLETE",
        "allowed_paths required",
        "allowed_paths",
        suggested_change='list the writable directories, e.g. ["src", "tests"]',
    )
    # 路径条目必须能安全地当作"仓库内相对路径"解释。``""`` / ``"."`` 会让
    # ``_paths.is_within`` 退化成"任意位置"，等于把项目授权悄悄放大成全仓
    # （spec 侧再写 ``["."]`` 就能通过 widening 校验）；绝对路径在
    # ``_backend.scope`` 的 git 相对路径比较里永远匹配不上，等于静默失效。
    for key in ("allowed_paths", "protected_paths"):
        entries = c.get(key)
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            normalized = _paths.normalize(entry) if isinstance(entry, str) else ""
            diagnostics.check(
                isinstance(entry, str)
                and normalized not in ("", ".")
                and not Path(normalized).is_absolute(),
                "INPUT_INCOMPLETE",
                f"{key}[{index}] must be a repository-relative path, got {entry!r}",
                f"{key}[{index}]",
                suggested_change='use a path inside the repository, e.g. "src"',
            )
    # 授权动作是**允许列表**，但 ``in`` 对字符串做的是子串匹配：写成
    # ``"no-publish"`` 时 ``"publish" in "no-publish"`` 为真，作者以为拒绝了，
    # 运行时却放行。非数组形态必须在注册期拒绝，而不是留给 authorize()。
    if "authorized_actions" in c:
        actions = c["authorized_actions"]
        diagnostics.check(
            isinstance(actions, list)
            and all(isinstance(a, str) and a in AUTHORIZED_ACTIONS for a in actions),
            "INPUT_INCOMPLETE",
            "authorized_actions must be an array of " + ", ".join(AUTHORIZED_ACTIONS),
            "authorized_actions",
            suggested_change='["create", "execute"]',
        )
    # 默认保护路径只在**键缺失**时生效（``_backend`` 的默认值）。显式的 ``[]``
    # 或 ``null`` 会把 ``.env`` / ``.github`` 的保护关掉，且注册期零告警——这与
    # "默认最小权限"相反，所以空数组也要报错，让用户删掉这个键。
    if "protected_paths" in c:
        protected = c["protected_paths"]
        diagnostics.check(
            isinstance(protected, list) and bool(protected),
            "INPUT_INCOMPLETE",
            "protected_paths must be a non-empty array; "
            "omit the key to keep the defaults ['.env', '.github']",
            "protected_paths",
            suggested_change='[".env", ".github"]',
        )
    diagnostics.check(
        bool(c.get("authorization_ref")),
        "AUTH_REQUIRED",
        "Register an authorization scope",
        "authorization_ref",
        requires_approval=True,
    )
    base_ref = c.get("base_ref", "HEAD")
    diagnostics.check(
        bool(base_ref) and not str(base_ref).startswith("-"),
        "INPUT_INCOMPLETE",
        "Invalid base ref",
        "base_ref",
    )
    diagnostics.raise_if_any()
    # ``safe_id`` raises instead of returning a verdict, so it stays outside the
    # collector: the shape above already guaranteed a non-empty string.
    for check in c["checks"]:
        safe_id(check.get("id"))
    # Profiles are normalized and validated here so an unknown adapter, an
    # undeclared backend or a reserved option fails at registration rather than
    # at the first execution. ``normalize_config`` also registers the shared
    # resources so the policy digest and re-read config stay byte-identical.
    if strict:
        _profiles.normalize_config(c, base_dir)
        for profile_id in c["agent_profiles"]:
            _profiles.resolve_binding(c, profile_id)
    else:
        # 读路径：策略问题只记录。`normalize_config` 是幂等的（注册时写盘的
        # 就是规范化结果），所以这里失败时 c 保持原样，摘要不受影响。
        # 传 problems 让它**逐 profile** 收集：不传的话第一个坏 profile 就抛出，
        # doctor 一次只报一条，用户要修 N 轮（v1.5.0 M1 要消灭的体验）。
        try:
            _profiles.normalize_config(c, base_dir, problems=problems)
        except Error as exc:
            if problems is not None:
                problems.append({"code": exc.code, "field": None, "message": str(exc)})
        for profile_id in list(c.get("agent_profiles") or {}):
            try:
                _profiles.resolve_binding(c, profile_id)
            except Error as exc:
                if problems is not None:
                    problems.append(
                        {
                            "code": exc.code,
                            "field": f"agent_profiles.{profile_id}",
                            "message": str(exc),
                        }
                    )
    return c


def check_execution_contract(
    c: dict[str, Any],
    profile_id: str | None = None,
    *,
    base_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Answer: **may this exact configuration be dispatched by this runtime?**

    v1.5.0 M8。与 ``policy_digest`` 的比较是两件事：

    - 摘要比较回答"配置变了没有"；
    - 这里回答"这份配置本身合不合法"。

    两者不能互相替代。旧配置可能一字未变却已不符合新版本规则（例如同时写了
    ``model`` 与 ``allow_cli_default_model``），此时两侧摘要相等，摘要比较什么
    都发现不了。反过来，配置非法时也不应该报 ``POLICY_CHANGED``——那个码的意思
    是"策略漂移，请 rebind"，会把配置缺陷伪装成流程事件，用户照着做还会再撞一次。

    返回**校验后**的副本（``repo`` 已解析、默认值已写回），调用方可以直接用它
    构造绑定，而不必重新规范化。
    """
    checked = json.loads(json.dumps(c))
    validate_config(checked, base_dir, strict=True)
    if profile_id is not None:
        _profiles.resolve_binding(checked, profile_id)
    return checked


def doctor(
    c: dict[str, Any], profile_id: str | None = None, *, probe: bool = True
) -> dict[str, Any]:
    """Return a project readiness report.

    Readiness is environment-only: git, process identity and the platform layer
    (lock/identity/tree). ``ready`` never depends on ``docker`` — the container
    backend is out of the product scope as of v0.5.0 — and it never implies that
    the adapter was verified end to end (§18.3).

    ``resolution`` reports the CLI binary this project would actually launch,
    together with the source that produced it (§6.5/§6.8). Only the adapter bound
    to the current profile is probed for its version: probing every registered
    CLI would make one ``doctor`` call spawn a dozen subprocesses.
    """
    refuse_docker_backend(c)
    # v1.5.0 M0：读路径把策略问题收集起来而不是抛错，doctor 是把它们呈现出来的地方。
    # 自己再算一次（`strict=False` 是幂等的），这样即便调用方传进来的是已读好的
    # 配置，也仍然能看到"这份配置在 v1.5.0 下哪里不合法"。
    policy_problems: list[dict[str, Any]] = []
    try:
        validate_config(
            json.loads(json.dumps(c)), base_dir=None, strict=False, problems=policy_problems
        )
    except Error as exc:
        policy_problems.append({"code": exc.code, "field": None, "message": str(exc)})
    checks: dict[str, Any] = {"git": bool(shutil.which("git")), "gh": bool(shutil.which("gh"))}
    backend = c.get("backend", "local-process")
    if backend == "local":
        backend = "local-process"
    checks["backend"] = backend
    try:
        checks["process_identity"] = bool(process_identity(os.getpid()))
    except (Error, OSError):
        checks["process_identity"] = False

    # Platform capability probe (design §2.3): lock, identity, tree stop/alive.
    # A failed platform probe is a hard gate for local-process execution and
    # qualification.
    checks["platform_lock"] = _platform_lock_ok()
    checks["platform_tree"] = _platform_tree_ok()

    # 检查命令预检（v2 §4-3）：在**与执行相同的 PATH 规则**下解析 argv[0]。
    # 只有 `unresolved`（裸名不在 PATH）才算环境不就绪；`deferred`（安装/构建
    # 之后才产生的工具）与 `shell`（内部命令不可静态解析）都不阻断。
    preflight = checks_preflight(c)
    checks["checks_preflight"] = preflight
    checks["checks_preflight_ok"] = all(
        row["status"] in ("ok", "shell", "deferred") for row in preflight
    )
    # v1.5.0 M1：占位符是"配置没填完"，与 checks_preflight 同层并入 ready。
    # 否则会重现"显示 ready、随后启动失败"——那正是本次要修掉的体验。
    placeholders = find_placeholders(c)
    checks["placeholders"] = placeholders
    checks["placeholders_ok"] = not placeholders

    binding = None
    adapter_error = None
    try:
        binding = _profiles.resolve_binding(c, profile_id)
    except Error as exc:
        adapter_error = {"code": exc.code, "message": str(exc)}
    checks["adapter_profile"] = binding.profile_id if binding else profile_id
    checks["adapter"] = binding.to_dict() if binding else None
    checks["adapter_block_code"] = adapter_error["code"] if adapter_error else None
    # ``adapter_approved`` 只表示"该 backend 有真实 e2e 记录"。它默认不再是
    # 就绪条件（见 require_real_e2e），只用于报告可信度。
    checks["adapter_approved"] = bool(binding and binding.capability == "VERIFIED")
    checks["adapter_unverified"] = bool(binding and binding.capability != "VERIFIED")

    # CLI discovery (§6.5): the binary ``doctor`` prints must be the binary
    # ``start`` spawns. Version probing is limited to the bound profile.
    resolution = (
        _discovery.resolution_for_adapter(binding.adapter, env_names=binding.env_names, probe=probe)
        if binding is not None
        else None
    )
    resolution_notes = list(resolution.notes) if resolution else []
    if not probe:
        resolution_notes.append("version not probed (probe disabled); version evidence unverified")

    # B3：把"实际会用多少"回显出来。`timeout_seconds` 此前两仓文档都没写、也没有
    # 任何可观测出口，用户只能看着 RUN_TIMEOUT 猜；`declared` 与 `effective` 并列
    # 是为了让"写了但被忽略"（类型错/越界）在预检期就可见。
    checks["run_timeout_seconds"] = run_timeout_seconds(c)
    checks["run_timeout_declared"] = c.get("timeout_seconds")
    checks["run_timeout_valid"] = (
        "timeout_seconds" not in c
        or (
            is_integer(c.get("timeout_seconds"))
            and MIN_RUN_TIMEOUT_SECONDS <= c["timeout_seconds"] <= MAX_RUN_TIMEOUT_SECONDS
        )
    )

    checks["ready"] = (
        checks["git"]
        and checks["process_identity"]
        and checks["platform_lock"]
        and checks["platform_tree"]
        and checks["checks_preflight_ok"]
        and checks["placeholders_ok"]
    )
    try:
        free = shutil.disk_usage(c["repo"]).free
        checks["disk_free"] = free
        checks["disk_ok"] = free >= 200 * 1024 * 1024
    except OSError:
        checks["disk_free"] = None
        checks["disk_ok"] = False

    # 严格项目（require_real_e2e=true）仍把"无真实 e2e 记录"当作不就绪。
    trusted = bool(checks["adapter_approved"])
    if c.get("require_real_e2e") and not trusted:
        checks["ready"] = False

    verification = binding.verification if binding else None
    return {
        "version": VERSION,
        "project_id": c["project_id"],
        "backend": backend,
        "checks": checks,
        "ready": checks["ready"],
        "trusted": trusted,
        "require_real_e2e": bool(c.get("require_real_e2e")),
        # Explicit readiness dimensions (design §11): a passing host check never
        # implies that the adapter was probed or verified end to end.
        "runtime_ready": bool(
            checks["git"] and checks["platform_lock"] and checks["platform_tree"]
        ),
        # v1.5.0 M0：有策略问题时这里为 False，但 `ready` 不变——`ready` 回答
        # "环境能不能跑"，`policy_ready` 回答"这份配置还能不能被当前版本派发"。
        # 两个维度分开写，宿主才能说清"环境就绪但配置要改"。
        "policy_ready": not policy_problems,
        "adapter_probe_passed": None,
        "real_e2e_status": (
            "PASS"
            if verification and verification.source == "real_e2e"
            else ("DECLARED" if verification else "NOT_RUN")
        ),
        "credential_present": _profiles.credentials_present(binding),
        "network_mode": (
            binding.profile.get("network_profile") if binding else c.get("network", "none")
        ),
        "verification_ready": bool(c.get("checks")),
        # 与 `checks.checks_preflight` 同源，提到顶层是因为宿主最常读这一层：
        # 「配置里写的命令本机到底能不能跑」比一个笼统的 ready 更能定位问题。
        "checks_preflight": checks["checks_preflight"],
        # v1.5.0 M1：未填写的模板占位符。`ready` 已并入这一项，顶层再暴露一次，
        # 与 checks_preflight 同理——宿主最常读这一层，比笼统的 ready 更好定位。
        "placeholders": checks["placeholders"],
        "placeholders_ok": checks["placeholders_ok"],
        # 与 `checks.run_timeout_seconds` 同源，提到顶层是同 `checks_preflight`
        # 的理由：宿主最常读这一层，「这次 run 最多跑多久」比笼统的 ready 更能
        # 解释一次 RUN_TIMEOUT。
        "run_timeout_seconds": checks["run_timeout_seconds"],
        "run_timeout_declared": checks["run_timeout_declared"],
        "adapter_error": adapter_error,
        # v1.5.0 M0：读路径收集到的策略问题（空数组表示配置仍满足当前版本）。
        # 有内容时 `ready` 不因此变 false——它们不影响"环境能不能跑"，只说明
        # "这份配置还能不能被当前版本派发"，拦截点在执行入口。
        "policy_problems": policy_problems,
        "profiles": _profiles.profile_views(c),
        # Which binary would actually be launched, and why that one (§6.8).
        "resolution": resolution.to_dict() if resolution else None,
        "resolution_notes": resolution_notes,
        # v0.6 (M3) owns `native_resume`/`resume_mode`; v0.5 deliberately keeps
        # the conservative values instead of pre-announcing a capability.
        "native_resume": False,
        "resume_mode": "workspace",
        "network": c.get("network", "none"),
    }


def _attestation_record_problem(attestation: dict[str, Any]) -> str | None:
    """Return why a stored attestation record cannot be trusted, or ``None``.

    读取侧的最小 schema：``policy_hash`` 必须是非空字符串，``confirmed_profiles``
    必须是非空 id 数组。写侧（``Store.write_attestation``）拒绝不合规记录，这里
    负责"磁盘上的记录已经坏了"的情况——两者都要 fail-closed，否则收窄检查会被
    一个类型不符的字段绕过。
    """
    if attestation.get("_corrupt"):
        return str(attestation.get("error") or "unreadable record")
    if not isinstance(attestation.get("policy_hash"), str) or not attestation["policy_hash"]:
        return "policy_hash missing or not a non-empty string"
    profiles = attestation.get("confirmed_profiles")
    if not isinstance(profiles, list) or not profiles:
        return "confirmed_profiles must be a non-empty array"
    if not all(isinstance(item, str) and item for item in profiles):
        return "confirmed_profiles must contain non-empty profile ids"
    return None


def attestation_status(
    c: dict[str, Any],
    attestation: dict[str, Any] | None,
    selected_profile: str | None = None,
) -> dict[str, Any]:
    """这份配置的候选集合是否被用户确认过（v1.5.0 §5）。

    三种口径，报告里必须区分，不能混用：

    - ``legacy_config_is_authorization``：没有 ``routing`` 段的历史项目。
      ``agent_profiles`` 的每一项都算已授权——这是**历史语义**，不是严格白名单。
    - ``confirmed``：有可信的确认记录，且记录里的摘要与当前配置一致。
    - ``unknown``：声明了 ``routing`` 段但没有记录（``missing``）、记录已因配置变化
      失效（``policy_changed_since_attestation``）、记录本身不合规
      （``record_invalid``/``record_corrupt``），或候选不在确认范围内
      （``candidate_not_confirmed``）。

    记录只证明"这份配置被确认过"，不证明"当初是谁写的白名单"，也不防同机写权限
    的伪造——文档不得把它说成审批或防篡改机制。
    """
    if "routing" not in c:
        return {
            "allowlist_source": "legacy_config_is_authorization",
            "confirmed": True,
        }
    policy_hash = _profiles.policy_digest(c)
    if not attestation:
        return {
            "allowlist_source": "unknown",
            "confirmed": False,
            "reason": "missing",
            "policy_hash": policy_hash,
        }
    # v1.5.0 修复：记录本身不可信时一律拒绝。旧实现在 `confirmed_profiles` 不是
    # list 时跳过收窄比较——一份缺字段或被手工改过的记录于是被判为"已确认"，
    # 把"收窄必须生效"从守卫降级成装饰。记录格式由 `write_attestation` 写入侧
    # 保证，这里按 fail-closed 处理读取侧。
    record_problem = _attestation_record_problem(attestation)
    if record_problem is not None:
        return {
            "allowlist_source": "unknown",
            "confirmed": False,
            "reason": "record_invalid",
            "detail": record_problem,
            "policy_hash": policy_hash,
        }
    if attestation.get("policy_hash") != policy_hash:
        return {
            "allowlist_source": "unknown",
            "confirmed": False,
            "reason": "policy_changed_since_attestation",
            "policy_hash": policy_hash,
            "attested_policy_hash": attestation.get("policy_hash"),
        }
    confirmed_profiles = attestation.get("confirmed_profiles")
    # v1.5.0：`project attest --profiles` 可以收窄确认范围，收窄必须**生效**——
    # 否则"只确认了 cb"的项目仍会把任务派给 cb2，确认记录就只是装饰。
    # 摘要一致只说明"配置没变"，不说明"这个候选被确认过"。
    # `confirmed_profiles` 的形态已由 `_attestation_record_problem` 保证是 list，
    # 这里不再用 isinstance 兜底（那正是旧实现的 fail-open 入口）。
    if selected_profile is not None and selected_profile not in confirmed_profiles:
        return {
            "allowlist_source": "unknown",
            "confirmed": False,
            "reason": "candidate_not_confirmed",
            "policy_hash": policy_hash,
            "confirmed_profiles": confirmed_profiles,
            "selected_profile": selected_profile,
        }
    return {
        "allowlist_source": "confirmed",
        "confirmed": True,
        "policy_hash": policy_hash,
        "confirmed_at": attestation.get("confirmed_at"),
        "confirmed_profiles": confirmed_profiles,
    }


def _platform_lock_ok() -> bool:
    """Probe that shared/exclusive locking works on this host."""
    from src._platform import get_platform

    plat = get_platform()
    import tempfile

    probe = Path(tempfile.gettempdir()) / f"zoe-lock-probe-{os.getpid()}"
    try:
        h1 = plat.lock_shared(str(probe))
        h2 = plat.lock_shared(str(probe))
        plat.unlock(h2)
        plat.unlock(h1)
        h = plat.lock_exclusive(str(probe), blocking=False)
        plat.unlock(h)
        return True
    except (Error, OSError):
        return False
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass


def _platform_tree_ok() -> bool:
    """Probe that process identity **and tree observation** work on this host.

    v0.5.1：旧实现只测 ``process_identity`` + ``alive``，从未调用
    ``tree_alive``。于是一台 ``ps`` 不可用、进程组观测全线失败的机器仍然
    得到 ``platform_tree: true / ready: true``，而每个 run 最后都以
    ``PROCESS_UNKNOWN`` 收场——自检说过的话比它实际验证的多。这里真的起一个
    短命子进程，要求"组内有进程"和"组已清空"两种答案都判对。
    """
    import subprocess
    import sys

    from src._platform import get_platform
    from src._platform.base import ProcessHandle

    plat = get_platform()
    pid = os.getpid()
    try:
        identity = plat.process_identity(pid)
        if not (identity and plat.alive(pid, identity)):
            return False
    except (Error, OSError):
        return False
    if getattr(plat, "name", "") != "posix":
        # Windows 的树观测建立在命名 Job Object 上，与"新会话进程组"不是同一
        # 机制；这里起裸子进程去问 job 名只会得到假阴性。保持原有判定，等
        # Windows 专项再补等价探测。
        return True
    proc = None
    try:
        # 子进程阻塞在 stdin 上：先观测"活着"，再放它退出观测"已清空"，
        # 避免用 sleep 猜时序。
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.PIPE,
            start_new_session=True,
        )
        token = str(proc.pid)
        live = plat.tree_alive(
            ProcessHandle(
                pid=proc.pid, start_token=None, group_token=token, popen=proc, platform=plat
            )
        )
        if proc.stdin is not None:
            proc.stdin.close()
        proc.wait(timeout=10)
        # 进程被回收后，组内不应再有成员；给内核一点时间清理进程组。
        dead = None
        for _ in range(20):
            dead = plat.tree_alive(
                ProcessHandle(
                    pid=proc.pid, start_token=None, group_token=token, popen=None, platform=plat
                )
            )
            if dead is False:
                break
            time.sleep(0.05)
        return live is True and dead is False
    except (Error, OSError, subprocess.SubprocessError):
        return False
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass


def authorized_actions(c: dict[str, Any]) -> list[str]:
    """Return the configured action allow-list; a non-array value authorizes nothing.

    ``validate_config`` rejects the shape at registration. This accessor keeps a
    hand-edited or externally written store from re-opening the same hole, where
    ``"publish" in "no-publish"`` would read as authorized.
    """
    actions = c.get("authorized_actions")
    if not isinstance(actions, list):
        return []
    return [action for action in actions if isinstance(action, str)]


def is_authorized(c: dict[str, Any], action: str) -> bool:
    """True when ``action`` is explicitly listed in the project's allow-list."""
    return action in authorized_actions(c)


def authorize(c: dict[str, Any], req: dict[str, Any], action: str) -> None:
    """Enforce that ``req`` carries the configured authorization ref and that
    ``action`` is within the project's authorized actions."""
    require(
        req.get("authorization_ref") == c["authorization_ref"],
        "AUTH_REQUIRED",
        "Authorization reference mismatch",
    )
    require(
        is_authorized(c, action),
        "POLICY_DENIED",
        f"Action {action} not authorized",
    )
