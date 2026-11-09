"""POSIX (macOS/Linux) platform backend.

Uses ``fcntl.flock`` for shared/exclusive locks, ``ps -o lstart`` for process
identity, ``os.kill(pid, 0)`` for probing (safe on POSIX), ``setsid`` via
``start_new_session=True`` for the process group, and ``killpg`` for tree stop.
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from src._platform.base import ProcessHandle
from src._util import Error, command

#: ``proc_pidinfo`` request flavour: ``PROC_PIDTBSDINFO``.
_PROC_PIDTBSDINFO = 3


def _darwin_identity(pid: int) -> str | None:
    """macOS process start time via ``proc_pidinfo`` (no external binary).

    Returns ``None`` when libproc is unavailable or reports nothing, so the
    caller falls back to ``ps`` instead of inventing an identity. The identity
    is opaque: only equality against a previously recorded value matters, and
    PID reuse changes the start time.
    """
    import ctypes
    import ctypes.util

    class _ProcBsdInfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16),
            ("pbi_name", ctypes.c_char * 32),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32),
            ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32),
            ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    try:
        libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
        info = _ProcBsdInfo()
        written = libproc.proc_pidinfo(
            pid, _PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
    except (OSError, AttributeError, ValueError):
        return None
    if written <= 0 or not info.pbi_start_tvsec:
        return None
    return f"{info.pbi_start_tvsec}.{info.pbi_start_tvusec:06d}"


class PosixPlatform:
    name = "posix"

    # -- locks ------------------------------------------------------------- #
    def _open_lock(self, path: str):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return open(p, "a+")

    def lock_shared(self, path: str):
        f = self._open_lock(path)
        try:
            fcntl.flock(f, fcntl.LOCK_SH)
        except OSError as exc:
            f.close()
            raise Error("PROCESS_UNKNOWN", f"Cannot acquire shared lock: {exc}") from None
        return f

    def lock_exclusive(self, path: str, blocking: bool = True, timeout: float | None = None):
        f = self._open_lock(path)
        # A bounded wait is implemented as a non-blocking poll loop, because
        # flock's blocking form has no timeout.
        bounded = blocking and timeout is not None
        flags = fcntl.LOCK_EX | (0 if (blocking and not bounded) else fcntl.LOCK_NB)
        deadline = None if not bounded else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f, flags)
                return f
            except BlockingIOError:
                if not bounded:
                    f.close()
                    raise Error("SLOT_BUSY", "Another operation is active") from None
                if time.monotonic() >= deadline:
                    f.close()
                    raise Error("SLOT_BUSY", f"Lock not acquired within {timeout}s") from None
                time.sleep(0.02)
            except OSError as exc:
                f.close()
                raise Error("PROCESS_UNKNOWN", f"Cannot acquire exclusive lock: {exc}") from None

    def unlock(self, handle) -> None:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()

    # -- process identity -------------------------------------------------- #
    def process_identity(self, pid: int) -> str | None:
        # Linux: /proc gives the start time with no external binary.
        proc = Path(f"/proc/{pid}/stat")
        if proc.exists():
            fields = proc.read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                return None
            return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + fields[19]
        # macOS: ask libproc directly. Depending on ``ps`` made the whole
        # controller unusable on hosts where the binary is restricted (that is
        # the situation the operator had to paper over with a hand-written
        # shim). libproc is a syscall, and its start-time format is the same
        # "%sec.%usec" the shim produced, so records written with a shim still
        # reconcile.
        if sys.platform == "darwin":
            identity = _darwin_identity(pid)
            if identity:
                return identity
        # Anything else: the external binary remains the fallback, and a
        # missing ``ps`` surfaces as an empty identity (never as a fake one).
        return command(["ps", "-p", str(pid), "-o", "lstart="]) or None

    def alive(self, pid: int, identity: str | None) -> bool:
        if not pid or not identity:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            raise Error("PROCESS_UNKNOWN", "Cannot inspect process identity") from None
        try:
            return self.process_identity(pid) == identity
        except (OSError, Error):
            raise Error(
                "PROCESS_UNKNOWN", "Process exists but identity cannot be verified"
            ) from None

    # -- spawn / tree ------------------------------------------------------ #
    def spawn(self, argv, cwd, env, stdout, stdin=None, isolate_tree: bool = True):
        # `isolate_tree` is a Windows-only distinction: a new session is harmless
        # for the supervisor and required for the agent tree, so it is always set.
        popen = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            stdin=stdin if stdin is not None else subprocess.DEVNULL,
            start_new_session=True,
        )
        # start_new_session=True makes the child its own session/pgroup leader,
        # so pgid == pid.
        start_token = self.process_identity(popen.pid)
        return ProcessHandle(
            pid=popen.pid,
            start_token=start_token,
            group_token=str(popen.pid),
            popen=popen,
            platform=self,
        )

    def stop_tree(self, handle, timeout: float) -> dict:
        pgid = handle.group_token
        try:
            pgid_int = int(pgid)
        except (TypeError, ValueError):
            return {"stopped": False, "remaining": -1}
        # Reap the direct child if present (SIGTERM then SIGKILL).
        popen = getattr(handle, "_popen", None)
        if popen is not None and popen.poll() is None:
            try:
                os.killpg(pgid_int, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                popen.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(pgid_int, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    popen.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # 不可中断状态（D state / NFS）下 SIGKILL 也可能迟迟不回收。
                    # 这里抛出未分类异常会把真正的 RUN_TIMEOUT / STOP_REQUESTED
                    # 覆盖成 INTERNAL_ERROR，所以交给调用方的 remaining 判定。
                    return {"stopped": False, "remaining": self._tree_remaining(pgid_int)}
        else:
            try:
                os.killpg(pgid_int, signal.SIGTERM)
            except ProcessLookupError:
                return {"stopped": True, "remaining": 0}
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                remaining = self._tree_remaining(pgid_int)
                if remaining == 0:
                    return {"stopped": True, "remaining": 0}
                time.sleep(0.05)
            try:
                os.killpg(pgid_int, signal.SIGKILL)
            except ProcessLookupError:
                pass
        remaining = self._tree_remaining(pgid_int)
        return {"stopped": remaining == 0, "remaining": remaining}

    def _tree_remaining(self, pgid: int) -> int:
        """Report whether the process group still has members.

        v0.5.1: this used to parse ``ps -A -o pgid=``. That made the *whole*
        tree observation depend on an external binary which is absent or
        sandboxed on some hosts — and when it failed the answer was ``-1``
        ("unknown"), which every caller turned into ``PROCESS_UNKNOWN`` for
        **every** run. ``killpg(pgid, 0)`` is a pure syscall with exactly the
        semantics needed here: it succeeds while any member of the group
        exists, and raises ``ESRCH`` once the group is empty (including the
        "reaped leader, surviving child" case).

        Return value keeps the old contract: ``0`` empty, ``1`` members
        present, ``-1`` genuinely undeterminable.
        """
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return 0
        except PermissionError:
            # 组里还有进程，但不是我们能观测的；这仍不是"空"。
            return 1
        except OSError:
            return -1
        return 1

    def tree_alive(self, handle) -> bool | None:
        pgid = handle.group_token
        try:
            pgid_int = int(pgid)
        except (TypeError, ValueError):
            return None
        remaining = self._tree_remaining(pgid_int)
        if remaining < 0:
            return None
        return remaining > 0


def platform() -> PosixPlatform:
    return PosixPlatform()
