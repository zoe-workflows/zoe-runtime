"""Shared primitives: error type, hashing, atomic writes, subprocess helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from src import __version__

#: Tool version. `src.__version__` is the single source of truth (hatch reads
#: it for packaging); this alias keeps the legacy `_util`/`core` import path.
VERSION = __version__

#: 预留：历史上曾设想把写者锁的 fd 交给 supervisor 继承。当前实现里**没有任何
#: 赋值点**，所以 ``lease_fds()`` 恒返回 ``()``，"锁随子进程继承"这件事并不存在，
#: 也不被任何安全说明依赖（`docs/threat-model.md`、`SECURITY.md` 均未提及 fd 继承；
#: 单一写者由 supervisor 自己持有的 ``writer`` EX 锁保证）。
#: 它被保留在 ``core.__all__`` 里只因为那是外部可见的导出面，删除要单独一轮变更。
LEASE_FD = None
TERMINAL = {"DONE", "CANCELLED"}

#: Non-secret environment names inherited by *name* so a child process can start.
#: Windows needs ``SYSTEMROOT`` (Python cannot initialise without it), ``TEMP`` and
#: ``COMSPEC`` for tool chains, and ``PATHEXT`` for ``.cmd`` resolution; POSIX
#: needs a locale so the CLI does not fall back to ASCII. Shared by the launch
#: path (``_backend``) and the CLI version probe (``_discovery``): a version
#: measured under a different environment is not the version that will be
#: launched (v0.5 §6.5).
PLATFORM_ENV_NAMES = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "TMPDIR",
    "LANG",
    "LC_ALL",
)


def lease_fds():
    from src._platform import is_windows

    # Windows does not support fd inheritance; the lock ownership record in the
    # run record is the cross-platform substitute (design §2.5, 01 §3.1).
    if is_windows() or LEASE_FD is None:
        return ()
    return (LEASE_FD,)


#: Recovery hints for the errors a host hits most often (v0.5.1 §11.3).
#: Deliberately a fixed map, not a heuristic: a wrong hint is worse than none.
_HINTS = {
    "INPUT_INCOMPLETE": "the message names the missing or invalid field; "
    "fix the request and retry (same request_id only for an identical payload)",
    "STATE_CONFLICT": "read `task status TASK --refresh`, then retry with the new expected_version",
    "SLOT_BUSY": "another task holds an active slot; read `slots` instead of retrying blindly",
    "POLICY_CHANGED": "review the policy change; `task rebind` accepts it explicitly",
    "ATTEST_PRECONDITION_FAILED": "nothing was written; run `project attest --dry-run` "
    "again, show the new execution[] and ask for confirmation before writing",
    "ADAPTER_UNVERIFIED": "this (adapter, backend) has no real-e2e record; "
    "see `require_real_e2e` and docs/adapter-qualification.md",
    "RUNTIME_INCOMPATIBLE": "the skill declares a version/API window this runtime is "
    "outside of; install a matching zoe-runtime release (`pipx install zoe-runtime==<version>`) "
    "or upgrade the skill",
}


class Error(Exception):
    """Business error carrying a stable ``code`` and optional structured details.

    ``details`` is an *optional* additive field (v0.5 §11.1). A host that only
    reads ``code`` keeps working; a host that needs to tell the three drift kinds
    apart reads ``details.drift_kind`` instead of parsing the message.

    ``hint`` (v0.5.1 §11.3) is filled in this one place for the five most common
    codes. It never changes a verdict and never supplies a default value.
    """

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = dict(details) if details else None

    def to_dict(self):
        value = {"code": self.code, "message": str(self)}
        if self.details:
            value["details"] = self.details
        hint = _HINTS.get(self.code)
        if hint:
            value["hint"] = hint
        return value


def require(ok, code, message):
    if not ok:
        raise Error(code, message)


def is_integer(value) -> bool:
    """True only for a real integer.

    ``bool`` subclasses ``int``, so ``True in (1, 2)`` is true and a JSON typo
    (``"schema_version": true``) would silently select schema v1 semantics —
    including the migration-only paths that skip the model requirement.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def digest(data):
    if not isinstance(data, bytes):
        data = json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2).encode()
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with open(tmp, "xb") as f:
        os.chmod(tmp, 0o600)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_text(path, text):
    """Write UTF-8 text atomically.

    ``atomic`` above always serialises JSON, so using it for a Markdown
    deliverable silently produced a JSON blob named ``*.md``. Text products
    (``report.md``) must go through this function instead.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with open(tmp, "xb") as f:
        os.chmod(tmp, 0o600)
        f.write(text.encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read(path):
    """Parse a JSON request/record file.

    ``-`` means stdin, so every command that accepts ``--request-file`` gets the
    same behaviour from this one function instead of growing its own branch.
    """
    if str(path) == "-":
        return json.loads(sys.stdin.read())
    return json.loads(Path(path).read_text())


def command(args, cwd=None, timeout=60, env=None):
    try:
        p = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                _kill_tree(p)
            except (OSError, AttributeError):
                try:
                    p.kill()
                except OSError:
                    pass
            # A surviving grandchild can hold the pipes open; waiting here without
            # a timeout turns "hard timeout" into an indefinite hang.
            try:
                p.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise Error(
                "ENVIRONMENT_ERROR", "Command timed out; reconcile external effects"
            ) from None
    except OSError as e:
        raise Error("ENVIRONMENT_ERROR", str(e)) from e
    require(p.returncode == 0, "COMMAND_FAILED", (stderr or stdout)[-4000:])
    return stdout.strip()


def _kill_tree(proc: subprocess.Popen) -> None:
    """Terminate a command tree, portable across POSIX and Windows."""
    from src._platform import is_windows

    if is_windows():
        # ``proc.kill()`` reaps only the direct child; a grandchild (git's
        # credential helper, a pager) keeps the stdout pipe open and the caller's
        # ``communicate()`` then waits for an EOF that never arrives.
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            proc.kill()
        return
    import signal

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


#: ``filter.*`` overrides, memoised per repository for this process.
#:
#: 探针本身是一次子进程：每个 ``git()`` 调用都要跑两次（探针 + 真命令），而
#: ``filter.*`` 的取值只在有人改配置时才会变。``manifest()`` 里连打两次、verify
#: 段连打四次、handoff 两次——同一进程内重复探同一个仓库是可省的固定开销。
#: 缓存的键是仓库绝对路径：不同仓库有各自的过滤器配置。
_FILTER_OVERRIDES: dict[str, tuple[str, ...]] = {}


def _filter_overrides(repo) -> tuple[str, ...]:
    key = os.path.abspath(str(repo))
    cached = _FILTER_OVERRIDES.get(key)
    if cached is not None:
        return cached
    # Disable configured filters, including globally installed LFS helpers, so host
    # checkout/add cannot execute project-selected programs as the publishing user.
    cfg = subprocess.run(
        ["git", "config", "--name-only", "--get-regexp", r"^filter\."],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=15,
        pass_fds=lease_fds(),
    )
    overrides: list[str] = []
    for line in cfg.stdout.splitlines():
        match = re.fullmatch(r"filter\.(.+)\.(clean|smudge|process|required)", line)
        if match:
            suffix = "=false" if match.group(2) == "required" else "="
            overrides += ["-c", match.group(0) + suffix]
    value = tuple(overrides)
    _FILTER_OVERRIDES[key] = value
    return value


def git(repo, *args):
    overrides = _filter_overrides(repo)
    return command(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.attributesFile=/dev/null",
            *overrides,
            *args,
        ],
        cwd=repo,
    )


_SECRET_PATTERNS = (
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{8,})"), "***"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{8,})"), "***"),
    (re.compile(r"\b(AKIA[0-9A-Z]{12,})"), "***"),
    (re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{8,})"), "***"),
    (
        re.compile(
            r"(?i)\b([A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*)\s*[:=]\s*\S+"
        ),
        r"\1=***",
    ),
)


def redact_secrets(text):
    """Best-effort masking for log tails.

    This is deliberately not a guarantee: it catches common token shapes and
    ``NAME=value`` assignments, nothing more. Reports only cite summaries; raw
    logs stay 0600 on disk.
    """
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def safe_id(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", value),
        "INPUT_INCOMPLETE",
        "Invalid ID",
    )
    return value


def check_disk_space(path, min_free_bytes=200 * 1024 * 1024):
    """Refuse to start new work when free space on ``path`` is below the floor.

    Read-only check: it never deletes or modifies anything. The default floor is
    200 MiB, matching the project's "stop new dispatch, keep existing work"
    policy (design doc section 22).
    """
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        # Unreadable filesystem: do not silently proceed.
        raise Error("ENVIRONMENT_ERROR", f"Cannot determine free disk space for {path}") from None
    if usage.free < min_free_bytes:
        raise Error(
            "ENVIRONMENT_ERROR",
            f"Insufficient free disk space ({usage.free} bytes < {min_free_bytes}); "
            "stop new dispatch",
        )
    return usage.free
