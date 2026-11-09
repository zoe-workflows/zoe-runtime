"""Execution backend (Docker/LocalProcess) and workspace scope/fingerprint checks.

The backend only knows how to start, observe and stop a process with a hardened
mount/network policy. It deliberately does **not** parse vendor output: the agent
adapter owns that (multi-agent design §6). It does own the *bounds*: every mount
is built here from controller state, the prompt never becomes an argv element,
and output is captured with a hard byte limit so a runaway stream cannot be
mistaken for success.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path

# 运行观测（progress.json）与状态库分离：观测是宿主可见的事实，不是状态迁移。
from src import _paths, _progress
from src._agents.base import (
    DEFAULT_MAX_OUTPUT_BYTES,
    PROMPT_MOUNT,
    LaunchSpec,
    validate_launch_spec,
)
from src._agents.codebuddy import legacy_parse_result
from src._platform import get_platform, is_windows
from src._proc import alive
from src._util import (
    PLATFORM_ENV_NAMES,
    Error,
    atomic,
    command,
    digest,
    git,
    lease_fds,
    require,
)

# Platform detection (kept for the Docker backend's user-id branch).
IS_WINDOWS = is_windows()

#: Non-secret environment names inherited by *name* so a child process can start.
#: Values are never persisted — only copied from the parent's environment into
#: the child's. The list lives in ``_util`` because the CLI version probe must
#: measure under the same allowlist (§6.5).
_PLATFORM_ENV_NAMES = PLATFORM_ENV_NAMES


def parse_agent_output(raw):
    """Deprecated v0.2 helper: final CodeBuddy JSON as a dict.

    New code parses through an :class:`~src._agents.base.AgentAdapter` so the
    protocol belongs to the adapter, not the backend.
    """
    return legacy_parse_result(raw)


class BackendBase(ABC):
    """Abstract base class for execution backends.

    Provides the common execution pipeline framework using the Template Method pattern.
    Subclasses implement backend-specific logic for launching, stopping, inspecting,
    and log collection.

    Security model differences between backends are documented in each subclass.
    """

    def __init__(self, config):
        self.c = config

    @abstractmethod
    def inspect(self, name) -> dict | None:
        """Query execution state.

        Returns:
            {"Running": bool, "ExitCode": int} or None if not found.
        Raises:
            Error("PROCESS_UNKNOWN") if state cannot be determined.
        """
        ...

    @abstractmethod
    def stop(self, name) -> None:
        """Stop the execution gracefully (SIGTERM) then forcefully (SIGKILL) if needed."""
        ...

    @abstractmethod
    def _do_launch(self, store, t, r, name, argv, log_name, worker, prompt, env, launch) -> None:
        """Backend-specific launch logic (Docker create/start or subprocess.Popen).

        Must update r["launch_stage"] to "started" after successful launch.
        """
        ...

    @abstractmethod
    def _collect_logs(self, store, t, r, log_name, state, worker, launch) -> bool:
        """Collect bounded logs. Returns ``True`` when the capture was truncated."""
        ...

    def execute(self, store, t, r, argv, log_name, worker=False, prompt=None) -> int:
        """Execute a command through the backend with full lifecycle management.

        This is the Template Method that defines the common execution pipeline:
        1. Resolve the controller-validated LaunchSpec (if the caller supplied one)
        2. Generate unique name and prepare run directory
        3. Build environment variables with whitelist filtering
        4. Write prompt file if provided
        5. Call _do_launch() for backend-specific startup
        6. Monitor loop: stop request, timeout, output budget, exit
        7. Collect bounded logs and backend capture metadata

        Returns:
            Exit code of the executed command.
        """
        # The frozen LaunchSpec applies to the agent turn only. Verification
        # checks are controller commands and must never inherit agent argv,
        # env names or output budgets.
        launch = LaunchSpec.from_dict(r["launch"]) if (worker and r.get("launch")) else None
        if launch is not None:
            validate_launch_spec(launch)
            require(
                list(launch.argv) == list(argv),
                "POLICY_DENIED",
                "argv does not match the frozen launch specification",
            )

        # 1. Generate unique name and prepare
        prefix = "zoe-" if isinstance(self, Docker) else "local-"
        name = prefix + r["run_id"] + "-" + log_name
        r["container"] = name
        r["launch_stage"] = "create_requested"
        store.run(r)

        d = store.path(t) / "runs" / r["run_id"]
        d.mkdir(parents=True, exist_ok=True)
        log_path = d / (log_name + ".log")

        # 2. Build environment variables with whitelist
        env = self._build_env(worker, launch.env_names if launch else None)

        # 3. Write prompt file if provided
        if prompt is not None:
            f = d / "prompt.txt"
            # 显式 UTF-8：``write_text`` 默认用 locale 编码（Windows 中文主机是
            # cp936），含中文标点或 emoji 的规格会抛 UnicodeEncodeError（不是
            # ``Error``，最终落成 INTERNAL_ERROR），或让 agent 读到乱码。
            f.write_bytes(prompt.encode("utf-8"))
            # 该文件内嵌完整 spec 与上一轮的 last_error。容器后端需要挂载可读，
            # 本地后端按 ``atomic()`` 的口径收紧到 0600。
            f.chmod(0o644 if isinstance(self, Docker) else 0o600)
            env["ZOE_PROMPT"] = str(f)

        # 4. Backend-specific launch
        self._do_launch(store, t, r, name, argv, log_name, worker, prompt, env, launch)

        # 5. Monitoring loop (common to all backends)
        limit = launch.max_output_bytes if launch else DEFAULT_MAX_OUTPUT_BYTES
        # 墙钟上限走 `_config.run_timeout_seconds`：校验期、执行期与 doctor 回显
        # 必须读同一份定义。此前这里直接 `self.c.get("timeout_seconds", 3600)`，
        # 类型写错时在监视循环里抛 TypeError，而 `doctor` 什么都不说。
        from src._config import run_timeout_seconds

        deadline = time.monotonic() + run_timeout_seconds(self.c)
        state = None
        # 观测事实（v2 §3-2）：循环本来就在 stat 日志大小，这里顺手把"有没有新输出"
        # 落到 runs/<run_id>/progress.json。它不进状态库、不发事件，也不推断卡死。
        # 一个 run 只有一份 progress.json，描述的是 **agent** 进程。验证命令复用
        # 同一个 run_id 但各自新建 Observer（started_at/bytes 归零），照旧写入会
        # 让 `task status` 展示最后一条 check 的大小与静默时间，并声称观测新鲜。
        observer = (
            _progress.Observer(store, t, r["run_id"], log_path, log_name)
            if log_name == "agent"
            else _progress.NullObserver()
        )
        try:
            observer.tick(_progress.ALIVE, force=True)
            while True:
                state = self.inspect(name)
                require(state is not None, "PROCESS_UNKNOWN", "Execution disappeared")

                current = store.task(t["task_id"])
                if current.get("stop_requested"):
                    self.stop(name)
                    raise Error("STOP_REQUESTED", current["stop_requested"])
                if not state["Running"]:
                    break
                if time.monotonic() > deadline:
                    self.stop(name)
                    raise Error("RUN_TIMEOUT", "Execution exceeded deadline")
                if log_path.exists() and log_path.stat().st_size > limit:
                    self.stop(name)
                    raise Error(
                        "OUTPUT_LIMIT_EXCEEDED",
                        f"Output exceeded {limit} bytes; refusing to treat a truncated "
                        "stream as a result",
                    )
                observer.tick(_progress.ALIVE)
                time.sleep(0.3)
        except Error:
            # 失败路径（停止 / 超时 / 超限）同样要关闭日志句柄并落盘 capture。
            # ``_collect_logs`` / ``_write_capture`` 原本只在成功路径执行，于是
            # agent.log 未 flush 的尾部（正是排障最需要的那几 KB）丢失，句柄也
            # 会泄漏到下一次 attempt 被覆盖。诊断本身的失败不得顶替业务错误码。
            try:
                diagnostic_state = state or {"ExitCode": None, "Running": False}
                truncated = self._collect_logs(
                    store, t, r, log_name, diagnostic_state, worker, launch
                )
                self._write_capture(
                    store, t, r, log_name, diagnostic_state, launch, truncated, log_path
                )
            except (Error, OSError) as exc:
                r["diagnostics_error"] = str(exc)
                store.run(r)
            raise
        finally:
            # 观测必须自己说"进程已结束"，否则最后一次写入会永远停在 alive。
            observer.finish(_progress.EXITED)

        # 6. Collect logs and results
        r["launch_stage"] = "finished"
        store.run(r)
        truncated = self._collect_logs(store, t, r, log_name, state, worker, launch)
        self._write_capture(store, t, r, log_name, state, launch, truncated, log_path)

        # Legacy direct-backend call paths (no LaunchSpec) keep the v0.2 result
        # artifact so existing integrations do not silently lose it. The controller
        # pipeline always parses through the adapter instead.
        if worker and state["ExitCode"] == 0 and launch is None:
            result = parse_agent_output(log_path.read_text(errors="replace"))
            atomic(d / "agent-result.json", result)

        return state["ExitCode"]

    def _session_dir(self, store, t, r, launch):
        """Resolve the controller-owned diagnostic directory for this run.

        The adapter may *name* a session directory, but the backend decides
        whether it is inside this run's own material directory; anything else
        would turn an adapter field into an arbitrary host bind mount.
        """
        if launch is None or not launch.session_dir:
            return None
        allowed = (store.path(t) / "runs" / r["run_id"]).resolve()
        path = Path(launch.session_dir).resolve()
        require(
            path.is_relative_to(allowed),
            "POLICY_DENIED",
            "session_dir must stay inside the run directory",
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_capture(self, store, t, r, log_name, state, launch, truncated, log_path) -> None:
        size = log_path.stat().st_size if log_path.exists() else 0
        # Only the agent turn owns capture.json; check runs keep their own file so
        # the adapter result can never be overwritten by a verification command.
        name = "capture.json" if launch is not None else f"capture-{log_name}.json"
        atomic(
            store.path(t) / "runs" / r["run_id"] / name,
            {
                "schema_version": 1,
                "adapter_id": r.get("adapter_id"),
                "adapter_backend": r.get("adapter_backend"),
                "backend": "docker" if isinstance(self, Docker) else "local",
                "exit_code": state["ExitCode"],
                "log": log_name + ".log",
                "bytes": size,
                "truncated": bool(truncated),
                "max_output_bytes": (
                    launch.max_output_bytes if launch else DEFAULT_MAX_OUTPUT_BYTES
                ),
                "output_protocol": launch.output_protocol if launch else "legacy",
                "expected_version": launch.agent_version if launch else None,
                "session_dir": launch.session_dir if launch else None,
                "permission": (launch.extra.get("permission") if launch else None),
                "finished_at": time.time(),
            },
        )

    def _build_env(self, worker, env_names=None):
        """Build environment variables with whitelist filtering.

        Only names already authorized by the selected adapter profile are passed
        through, and they must match the credential prefixes the normalized
        project configuration allows. This prevents credential leakage to the
        agent and stops one adapter from borrowing another vendor's credentials.

        The allowlist also carries the OS plumbing a child needs to start at all
        (design §2.6, 09 §2): on Windows a process without ``SYSTEMROOT`` fails
        before running, because Python cannot initialise its filesystem encoding.
        Those names are not credentials, so they are read from the parent by name
        and never persisted.
        """
        env = {
            "HOME": str(Path.home()),
            "PATH": os.environ.get("PATH", ""),
            "TERM": "dumb",
        }
        # Name-only inheritance of non-secret OS plumbing.
        for key in _PLATFORM_ENV_NAMES:
            if os.environ.get(key):
                env[key] = os.environ[key]
        # Force UTF-8 for Python-based CLIs so a non-UTF-8 console code page cannot
        # corrupt the captured protocol (design §5). Other CLIs ignore these.
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if worker:
            names = env_names if env_names is not None else (self.c.get("agent_env") or [])
            prefixes = tuple(self.c.get("credential_env_prefixes") or ("CODEBUDDY_", "ANTHROPIC_"))
            for key in names:
                require(
                    re.fullmatch(r"[A-Z][A-Z0-9_]*", key),
                    "INPUT_INCOMPLETE",
                    "Invalid environment key",
                )
                require(
                    key.startswith(prefixes),
                    "POLICY_DENIED",
                    "Only adapter-approved credentials allowed",
                )
                require(key in os.environ, "AUTH_REQUIRED", "Missing configured model credential")
                env[key] = os.environ[key]
        return env


class Docker(BackendBase):
    """Docker container execution backend.

    **Out of the product scope since v0.5.0.** Configuration validation refuses
    ``backend: docker`` and frozen docker tasks are refused by ``start`` /
    ``run_pipeline``, so no product path constructs this class; only historical
    run records still mention containers, and those are treated as unobservable.
    The class is kept for one release so old state stays readable, and is deleted
    in its own batch.

    Security guarantees it *would* provide:
    - Filesystem isolation: --read-only root, worktree bind mount
    - Permission restriction: --cap-drop=ALL, no-new-privileges
    - Resource limits: cgroups (memory, CPU, PIDs)
    - Network isolation: --network none (default)
    - Git protection: .git mounted read-only
    - Credential isolation: only adapter-approved env names
    - Prompt isolation: prompt is a read-only mount consumed via stdin, never argv
    """

    def inspect(self, name):
        try:
            p = subprocess.run(
                ["docker", "inspect", name],
                capture_output=True,
                text=True,
                timeout=15,
                pass_fds=lease_fds(),
            )
        except (OSError, subprocess.TimeoutExpired):
            raise Error("PROCESS_UNKNOWN", "Docker inspection unavailable or timed out") from None
        if p.returncode:
            if "No such" in p.stderr:
                return None
            raise Error("PROCESS_UNKNOWN", "Cannot query Docker state")
        return json.loads(p.stdout)[0]["State"]

    def stop(self, name):
        if self.inspect(name):
            command(["docker", "stop", "-t", "10", name], timeout=25)
        state = self.inspect(name)
        require(not state or not state["Running"], "PROCESS_UNKNOWN", "Container still running")

    def _do_launch(self, store, t, r, name, argv, log_name, worker, prompt, env, launch):
        """Launch Docker container with security hardening."""
        d = store.path(t) / "runs" / r["run_id"]
        network = self.c.get("network", "none") if worker else "none"
        args = [
            "docker",
            "create",
            "--name",
            name,
            "--label",
            "zoe.run=" + r["run_id"],
            "--network",
            network,
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--read-only",
            "--pids-limit",
            "256",
            "--memory",
            self.c.get("memory", "2g"),
            "--cpus",
            str(self.c.get("cpus", 2)),
            "--tmpfs",
            "/tmp:rw,nosuid,size=256m",
            "--mount",
            f"type=bind,src={t['workspace']},dst=/work",
            "--mount",
            f"type=bind,src={t['workspace']}/.git,dst=/work/.git,readonly",
            "--workdir",
            "/work",
            "--env",
            "HOME=/tmp",
        ]
        if not IS_WINDOWS:
            args += ["--user", f"{os.getuid()}:{os.getgid()}"]
        stdin_file = launch.stdin_file if launch else None
        if prompt is not None:
            f = d / "prompt.txt"
            args += ["--mount", f"type=bind,src={f},dst={PROMPT_MOUNT},readonly"]
        if stdin_file:
            # Trusted in-image launcher: the frozen prompt reaches the agent on
            # stdin, so its text never appears in argv (design D07).
            argv = ["/bin/sh", "-c", f'exec "$@" < {PROMPT_MOUNT}', "zoe", *argv]
        session = self._session_dir(store, t, r, launch)
        if session is not None:
            args += ["--mount", f"type=bind,src={session},dst=/zoe-session"]
        for directory in launch.writable_dirs if launch else ():
            args += ["--tmpfs", f"{directory}:rw,nosuid,size=64m"]
        # Add whitelisted env vars for worker mode
        if worker:
            for key in launch.env_names if launch else (self.c.get("agent_env") or []):
                if key in env:
                    args += ["--env", key]
        command(args + [self.c["image"], *argv], env=env)
        # start may succeed remotely even if client dies; name remains available for reconciliation.
        r["launch_stage"] = "start_requested"
        store.run(r)
        command(["docker", "start", name])
        r["launch_stage"] = "started"
        store.run(r)

    def _collect_logs(self, store, t, r, log_name, state, worker, launch):
        """Collect Docker logs with a hard byte budget."""
        d = store.path(t) / "runs" / r["run_id"]
        limit = launch.max_output_bytes if launch else DEFAULT_MAX_OUTPUT_BYTES
        path = d / (log_name + ".log")
        truncated = False
        with open(path, "wb") as out:
            proc = subprocess.Popen(
                ["docker", "logs", r["container"]],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                pass_fds=lease_fds(),
            )
            written = 0
            try:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    if written + len(chunk) > limit:
                        out.write(chunk[: limit - written])
                        truncated = True
                        proc.kill()
                        break
                    out.write(chunk)
                    written += len(chunk)
            finally:
                proc.stdout.close()
                proc.wait(timeout=30)
        return truncated


class LocalProcess(BackendBase):
    """Local process backend (no Docker required).

    Security model (design §0.2, 09): this is **controlled execution in a
    trusted personal environment**, not a sandbox. The agent runs as the current
    user with host network and permissions. What Zoe guarantees is the *call
    surface*: which CLI, which cwd, which env names, what bounds, and what
    evidence. It cannot prevent the agent from reading outside the worktree.

    The process tree is managed by the platform layer (POSIX process group /
    Windows Job Object). The run record persists ``pid/start_token/group_token``
    so a *new* process can re-observe the tree after a crash — the previous
    implementation kept only an in-memory ``self._process`` and returned ``None``
    from ``inspect`` whenever reconcile ran in a fresh process.
    """

    def __init__(self, config):
        super().__init__(config)
        self._process: subprocess.Popen | None = None
        self._handle = None  # platform ProcessHandle for the current launch
        self._logfile = None  # Track logfile for safe cleanup

    def _handle_from_run(self, r):
        """Rebuild a platform ProcessHandle from the persisted run record."""
        from src._platform.base import ProcessHandle

        pid = r.get("pid")
        if not pid:
            return None
        return ProcessHandle(
            pid=pid,
            start_token=r.get("start_token"),
            group_token=r.get("group_token"),
            popen=None,
            platform=get_platform(),
        )

    def inspect(self, name):
        """Observe the run's process tree using persisted tokens, not memory.

        ``name`` is ignored (kept for the backend interface); the current run's
        persisted ``pid/start_token/group_token`` are the source of truth so a
        fresh-process reconcile can still observe the tree.
        """
        # In-process fast path: if this backend instance launched the process,
        # poll() is the most reliable signal.
        if self._process is not None:
            poll_result = self._process.poll()
            if poll_result is not None:
                return {"Running": False, "ExitCode": poll_result}
            if self._handle is not None:
                if alive(self._handle.pid, self._handle.start_token):
                    return {"Running": True, "ExitCode": None}
                return {"Running": False, "ExitCode": self._process.poll() or 0}

        # Cross-process path: only persisted tokens are available.
        handle = getattr(self, "_persisted_handle", None)
        if handle is None:
            return None
        plat = get_platform()
        try:
            if plat.alive(handle.pid, handle.start_token):
                return {"Running": True, "ExitCode": None}
        except Error:
            raise Error("PROCESS_UNKNOWN", "Cannot determine process state") from None
        # Parent dead: check whether the tree still has survivors.
        tree = plat.tree_alive(handle)
        if tree is None:
            raise Error("PROCESS_UNKNOWN", "Cannot determine process tree state")
        if tree:
            return {"Running": True, "ExitCode": None}
        return {"Running": False, "ExitCode": None}

    def stop(self, name):
        """Stop the tree via the platform layer (SIGTERM then SIGKILL / Job)."""
        handle = getattr(self, "_handle", None)
        if handle is None:
            handle = getattr(self, "_persisted_handle", None)
        if handle is None:
            return
        plat = get_platform()
        result = plat.stop_tree(handle, timeout=10)
        require(result["remaining"] == 0, "PROCESS_UNKNOWN", "Process tree still running")
        if self._process is not None and self._process.poll() is None:
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise Error("PROCESS_UNKNOWN", "Process still running after stop") from None

    def bind_persisted(self, r):
        """Bind persisted run tokens so cross-process inspect/stop works."""
        self._persisted_handle = self._handle_from_run(r)

    def _do_launch(self, store, t, r, name, argv, log_name, worker, prompt, env, launch):
        """Launch subprocess through the platform layer, persisting tree tokens."""
        d = store.path(t) / "runs" / r["run_id"]
        stdin_path = launch.stdin_file if launch else None
        session = self._session_dir(store, t, r, launch)
        if session is not None:
            env["ZOE_SESSION_DIR"] = str(session)
        self._logfile = open(d / (log_name + ".log"), "wb")
        plat = get_platform()
        try:
            stdin = None
            if stdin_path:
                stdin = open(stdin_path, "rb")
            try:
                self._handle = plat.spawn(
                    argv, cwd=t["workspace"], env=env, stdout=self._logfile, stdin=stdin
                )
            finally:
                if stdin is not None:
                    stdin.close()
            self._process = self._handle._popen
        except Error:
            self._close_logfile()
            raise
        except Exception as e:
            self._close_logfile()
            raise Error("ENVIRONMENT_ERROR", f"Failed to start process: {e}") from e

        # Persist the tree token so a future process can re-observe the tree.
        # `pid`/`start_token` keep identifying the *supervisor* (set by `start`):
        # reconcile, `stop` and `task recover` use them to decide whether the
        # supervisor is still alive, and the agent's identity must not shadow that.
        r["agent_pid"] = self._handle.pid
        r["agent_start_token"] = self._handle.start_token
        r["group_token"] = self._handle.group_token
        r["launch_stage"] = "started"
        store.run(r)

    def _collect_logs(self, store, t, r, log_name, state, worker, launch):
        """Close the logfile and report truncation against the output budget."""
        self._close_logfile()
        d = store.path(t) / "runs" / r["run_id"]
        limit = launch.max_output_bytes if launch else DEFAULT_MAX_OUTPUT_BYTES
        path = d / (log_name + ".log")
        size = path.stat().st_size if path.exists() else 0
        if size > limit:
            path.write_bytes(path.read_bytes()[:limit])
            return True
        return False

    def _close_logfile(self):
        """Safely close the logfile handle (idempotent)."""
        if hasattr(self, "_logfile") and self._logfile and not self._logfile.closed:
            try:
                self._logfile.close()
            except Exception:
                pass  # Best-effort close
            self._logfile = None


def create_backend(config):
    """Factory function: create backend instance based on configuration.

    Args:
        config: Project configuration dict, must contain 'backend' field.

    Returns:
        Docker or LocalProcess instance.

    Raises:
        Error("CAPABILITY_UNSUPPORTED") if backend type is unknown.
    """
    backend_type = config.get("backend", "local-process")
    if backend_type == "docker":
        return Docker(config)
    elif backend_type in ("local", "local-process"):
        return LocalProcess(config)
    else:
        raise Error("CAPABILITY_UNSUPPORTED", f"Unknown backend type: {backend_type}")


def scope(t):
    w = Path(t["workspace"])
    c = t["config"]
    # 任务级范围优先；``None`` 才表示"沿用项目范围"（v5 §B0-a）。绝不能用
    # ``or``：空列表是"显式声明为空"，在 create 期已被拒绝，这里不得回退。
    task_scope = t.get("task_scope")
    allowed_paths = c["allowed_paths"] if task_scope is None else task_scope
    protected_paths = c.get("protected_paths", [".env", ".github"])
    names = git(w, "diff", "--name-only", "-z", t["base_sha"]).split("\x00")
    names += git(w, "ls-files", "--others", "--exclude-standard", "-z").split("\x00")
    # 一次报全部越界：逐条 require 会让"一个路径一轮"变成常态，而每轮都要
    # 重新启动 agent。保护路径先判，且**永不**进入 missing_paths——那会让
    # 宿主把"禁止改动"读成"建议扩权"。
    violations: list[dict] = []
    missing: set[str] = set()
    for name in filter(None, names):
        allowed = _paths.covers(name, allowed_paths)
        denied = _paths.covers(name, protected_paths)
        if denied:
            violations.append({"path": name, "reason": "protected"})
        elif not allowed:
            violations.append({"path": name, "reason": "outside"})
            missing.add(name.split("/")[0])
    if violations:
        shown = ", ".join(item["path"] for item in violations[:5])
        more = "" if len(violations) <= 5 else f" (+{len(violations) - 5} more)"
        spec_scoped = task_scope is not None
        # 范围来自哪里决定**唯一**可用的解法，两者不可替换（v1.6）：spec 派生的
        # `task_scope` 只在 create/revise 时重算，`rebind` 只更新 policy_revision。
        # 这一步必须写在 details 里而不是靠宿主记忆——现场的表现正是"resume/rebind
        # 都无效，于是 cancel 重派"，把已经写好的代码当成废料。
        next_action = (
            "spec-scoped: widen `allowed_paths` in the spec, then `task revise` "
            "(rebind does NOT recompute task_scope; the worktree is kept)"
            if spec_scoped
            else "project-scoped: widen `allowed_paths` in the project config, "
            "`project register`, then `task rebind`"
        )
        raise Error(
            "SCOPE_VIOLATION",
            f"{len(violations)} path(s) violate the frozen policy: {shown}{more}",
            details={
                "violations": violations[:50],
                "missing_paths": sorted(missing),
                # 报告实际生效的范围，而不是项目范围：两者不同时，宿主需要
                # 知道是"任务声明太窄"还是"项目授权不足"。
                "allowed_paths": [_paths.normalize(p) for p in allowed_paths],
                "scope_source": "spec" if spec_scoped else "project",
                "protected_paths": list(protected_paths),
                "next_action": next_action,
            },
        )
    require(
        git(w, "symbolic-ref", "--short", "HEAD") == t["branch"],
        "WORKSPACE_CHANGED",
        "Task branch changed",
    )


def manifest(t):
    """Byte-safe deliverable manifest; caches and ignored files are excluded.

    只描述**磁盘上存在**的文件（v1.6）。早先的写法给"索引里有、工作树里没有"的条目
    记一个 ``["name", "absent"]``，于是同一个工作树会在 ``git add`` 前后得到两个不同的
    指纹：删除一个受控文件时，``after``（提交前，删除还没进索引）含 ``absent``，提交后
    （索引里也没有了）不含 —— ``require(fingerprint(t) == after)`` 必然失败，**任何
    删除文件的候选都会被报成 ``WORKSPACE_CHANGED``**，与它实际有没有动别的文件无关。

    "不存在" 不是一种内容，不该出现在内容清单里：删除由 ``scope()``（``git diff``）
    和 ``before``/``after`` 的条目差集检出，两者都不依赖索引状态。
    """
    w = Path(t["workspace"])
    result = []
    names = git(w, "ls-files", "-z").split("\x00") + git(
        w, "ls-files", "--others", "--exclude-standard", "-z"
    ).split("\x00")
    for name in sorted(set(filter(None, names))):
        p = w / name
        if p.is_symlink():
            result.append([name, "symlink", os.readlink(p)])
        elif p.is_file():
            result.append([name, oct(p.stat().st_mode & 0o777), digest(p.read_bytes())])
    return result


def fingerprint(t):
    return digest(manifest(t))
