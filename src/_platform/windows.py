"""Windows platform backend.

Implements the platform contract with ``ctypes`` over ``kernel32``:

* file locks: ``LockFileEx``/``UnlockFileEx`` byte-range lock at offset 0. A
  shared lock omits ``LOCKFILE_EXCLUSIVE_LOCK``; exclusive adds it. Byte-range
  locking is used because ``msvcrt.locking`` has no shared semantics.
* process identity: ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`` +
  ``GetProcessTimes`` creation time — a reuse-safe token.
* liveness probe: ``OpenProcess`` + creation-time comparison. **Never**
  ``os.kill``: on Windows a non-console signal is ``TerminateProcess``, so
  probing with it would kill the process being probed.
* process tree: a **named** Job Object with
  ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. The job is stored as a *name* in the
  run record, not as a HANDLE: a HANDLE is only valid inside the process that
  created it, so a reconciling process could never observe it. A reconciling
  process reopens the job with ``OpenJobObjectW`` by name.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
import uuid
from ctypes import wintypes
from pathlib import Path

from src._platform.base import ProcessHandle
from src._util import Error

# --------------------------------------------------------------------------- #
# kernel32 declarations
# --------------------------------------------------------------------------- #

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001

_JOB_OBJECT_QUERY = 0x0004
_JOB_OBJECT_TERMINATE = 0x0008
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

_JobObjectBasicAccountingInformation = 1
_JobObjectExtendedLimitInformation = 9

_ERROR_FILE_NOT_FOUND = 2
_ERROR_ACCESS_DENIED = 5
_ERROR_LOCK_VIOLATION = 33
_ERROR_INVALID_PARAMETER = 87

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    #: Layout must match the Windows definition: two LARGE_INTEGERs, then DWORD,
    #: then two SIZE_T, then DWORD, ULONG_PTR and two DWORDs (64 bytes on x64).
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_kernel32.CreateFileW.restype = wintypes.HANDLE

_kernel32.LockFileEx.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(_OVERLAPPED),
]
_kernel32.LockFileEx.restype = wintypes.BOOL
_kernel32.UnlockFileEx.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(_OVERLAPPED),
]
_kernel32.UnlockFileEx.restype = wintypes.BOOL

_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_FILETIME),
    ctypes.POINTER(_FILETIME),
    ctypes.POINTER(_FILETIME),
    ctypes.POINTER(_FILETIME),
]
_kernel32.GetProcessTimes.restype = wintypes.BOOL

_kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenJobObjectW.restype = wintypes.HANDLE
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.TerminateJobObject.restype = wintypes.BOOL
_kernel32.QueryInformationJobObject.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_void_p,
]
_kernel32.QueryInformationJobObject.restype = wintypes.BOOL
_kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL


class WindowsPlatform:
    name = "windows"

    # -- locks ------------------------------------------------------------- #
    def _open_lock(self, path: str):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Share-all, so exclusion is expressed by LockFileEx rather than by the
        # file handle's sharing mode.
        handle = _kernel32.CreateFileW(
            str(p),
            _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_ALWAYS,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if not handle or handle == _INVALID_HANDLE_VALUE:
            raise Error("PROCESS_UNKNOWN", f"Cannot open lock file {path}")
        return _LockHandle(handle)

    def _try_lock(self, handle, exclusive: bool) -> int:
        """Try to take the byte-range lock; return 0 on success or the Win32 error."""
        flags = _LOCKFILE_FAIL_IMMEDIATELY
        if exclusive:
            flags |= _LOCKFILE_EXCLUSIVE_LOCK
        ov = _OVERLAPPED()
        if _kernel32.LockFileEx(handle.handle, flags, 0, 1, 0, ctypes.byref(ov)):
            return 0
        return ctypes.get_last_error()

    def lock_shared(self, path: str):
        handle = self._open_lock(path)
        ov = _OVERLAPPED()
        if not _kernel32.LockFileEx(handle.handle, 0, 0, 1, 0, ctypes.byref(ov)):
            err = ctypes.get_last_error()
            handle.close()
            raise Error("PROCESS_UNKNOWN", f"Cannot acquire shared lock (error {err})")
        return handle

    def lock_exclusive(self, path: str, blocking: bool = True, timeout: float | None = None):
        handle = self._open_lock(path)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            err = self._try_lock(handle, exclusive=True)
            if err == 0:
                return handle
            if not blocking:
                handle.close()
                raise Error("SLOT_BUSY", "Another operation is active") from None
            if err not in (_ERROR_LOCK_VIOLATION,):
                handle.close()
                raise Error("PROCESS_UNKNOWN", f"Cannot acquire lock (error {err})") from None
            if deadline is not None and time.monotonic() >= deadline:
                handle.close()
                raise Error("SLOT_BUSY", f"Lock not acquired within {timeout}s") from None
            time.sleep(0.02)

    def unlock(self, handle) -> None:
        handle.close()

    # -- process identity -------------------------------------------------- #
    def _open_process(self, pid: int, access: int):
        handle = _kernel32.OpenProcess(access, False, pid)
        if handle:
            return handle, 0
        return None, ctypes.get_last_error()

    @staticmethod
    def _creation_token(handle) -> str | None:
        creation = _FILETIME()
        exit_t = _FILETIME()
        kernel = _FILETIME()
        user = _FILETIME()
        if not _kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_t),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        return f"{creation.dwHighDateTime:x}:{creation.dwLowDateTime:x}"

    def process_identity(self, pid: int) -> str | None:
        if not pid:
            return None
        handle, _ = self._open_process(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
        if not handle:
            return None
        try:
            return self._creation_token(handle)
        finally:
            _kernel32.CloseHandle(handle)

    def alive(self, pid: int, identity: str | None) -> bool:
        """Observe only — never signals the target.

        An access-denied or otherwise failing ``OpenProcess`` means the answer
        cannot be determined, which is reported as ``PROCESS_UNKNOWN`` rather than
        "dead": claiming a live writer is dead would release its slot and let a
        second writer into the workspace.
        """
        if not pid or not identity:
            return False
        handle, err = self._open_process(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
        if not handle:
            if err == _ERROR_INVALID_PARAMETER:
                return False  # no such process
            reason = "access denied" if err == _ERROR_ACCESS_DENIED else f"error {err}"
            raise Error("PROCESS_UNKNOWN", f"Cannot probe process {pid} ({reason})") from None
        try:
            token = self._creation_token(handle)
        finally:
            _kernel32.CloseHandle(handle)
        if token is None:
            raise Error("PROCESS_UNKNOWN", f"Cannot read creation time of process {pid}") from None
        return token == identity

    # -- jobs -------------------------------------------------------------- #
    @staticmethod
    def _open_job(name: str, access: int):
        """Reopen a named job. Returns ``(handle, error)``."""
        if not name:
            return None, _ERROR_FILE_NOT_FOUND
        handle = _kernel32.OpenJobObjectW(access, False, name)
        if handle:
            return handle, 0
        return None, ctypes.get_last_error()

    @staticmethod
    def _job_active(handle) -> int:
        acct = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not _kernel32.QueryInformationJobObject(
            handle,
            _JobObjectBasicAccountingInformation,
            ctypes.byref(acct),
            ctypes.sizeof(acct),
            None,
        ):
            return -1
        return int(acct.ActiveProcesses)

    def _active_by_name(self, name: str) -> int:
        """Live process count in a job, re-opened by name from any process."""
        handle, err = self._open_job(name, _JOB_OBJECT_QUERY)
        if not handle:
            # A destroyed job means no surviving member: the tree is gone.
            return 0 if err == _ERROR_FILE_NOT_FOUND else -1
        try:
            return self._job_active(handle)
        finally:
            _kernel32.CloseHandle(handle)

    # -- spawn / tree ------------------------------------------------------ #
    @staticmethod
    def wrap_batch(argv):
        """Route ``.cmd``/``.bat`` through ``cmd.exe /c``.

        ``CreateProcess`` cannot execute a batch file directly, so an npm-style
        ``codebuddy.cmd`` (the normal install shape on Windows) would fail with
        ``WinError 193`` even though ``shutil.which`` resolved it. The wrapper is
        still inside the caller's Job Object, so the tree is stopped as one.
        """
        if not argv:
            return argv
        first = str(argv[0]).lower()
        if first.endswith((".cmd", ".bat")):
            comspec = os.environ.get("COMSPEC") or "cmd.exe"
            return [comspec, "/c", *argv]
        return argv

    def spawn(self, argv, cwd, env, stdout, stdin=None, isolate_tree: bool = True):
        argv = self.wrap_batch(list(argv))
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        job = None
        job_name = None
        if isolate_tree:
            # A *named* job so another process can reopen it; "Local\" keeps it in
            # the session namespace.
            job_name = f"Local\\zoe-job-{uuid.uuid4().hex}"
            job = _kernel32.CreateJobObjectW(None, job_name)
            if not job:
                raise Error("ENVIRONMENT_ERROR", "CreateJobObjectW failed")
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            if not _kernel32.QueryInformationJobObject(
                job,
                _JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
                None,
            ):
                err = ctypes.get_last_error()
                _kernel32.CloseHandle(job)
                # A wrong struct layout surfaces here as ERROR_BAD_LENGTH (24),
                # so include the size to make that diagnosable.
                raise Error(
                    "ENVIRONMENT_ERROR",
                    "QueryInformationJobObject failed "
                    f"(error {err}, struct size {ctypes.sizeof(info)})",
                )
            # Arm kill-on-close: if this controller dies, the whole tree dies with
            # it, so no orphan writer can keep modifying the workspace.
            info.BasicLimitInformation.LimitFlags |= _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _kernel32.SetInformationJobObject(
                job,
                _JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                _kernel32.CloseHandle(job)
                raise Error("ENVIRONMENT_ERROR", "SetInformationJobObject failed")

        try:
            popen = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                stdin=stdin if stdin is not None else subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except BaseException:
            if job:
                _kernel32.CloseHandle(job)
            raise

        if job is not None:
            # Ownership must be transferred before SPAWN_OBSERVED is written
            # (design §4); a failure here is treated like an unconfirmed spawn.
            if not _kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(popen._handle)):
                err = ctypes.get_last_error()
                _kernel32.CloseHandle(job)
                try:
                    popen.kill()
                except OSError:
                    pass
                raise Error(
                    "ENVIRONMENT_ERROR",
                    f"AssignProcessToJobObject failed (error {err})",
                ) from None

        return ProcessHandle(
            pid=popen.pid,
            start_token=self.process_identity(popen.pid),
            group_token=job_name,
            popen=popen,
            platform=self,
            job=job,
        )

    def stop_tree(self, handle, timeout: float) -> dict:
        name = handle.group_token
        popen = getattr(handle, "_popen", None)
        if not name:
            # Not an isolated tree (e.g. the supervisor itself): stop the child.
            if popen is not None and popen.poll() is None:
                popen.terminate()
                try:
                    popen.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    popen.kill()
            return {"stopped": True, "remaining": 0}

        job, err = self._open_job(name, _JOB_OBJECT_TERMINATE | _JOB_OBJECT_QUERY)
        if not job:
            if err == _ERROR_FILE_NOT_FOUND:
                return {"stopped": True, "remaining": 0}
            return {"stopped": False, "remaining": -1}
        try:
            _kernel32.TerminateJobObject(job, 1)
            deadline = time.monotonic() + max(timeout, 0)
            while True:
                remaining = self._job_active(job)
                if remaining == 0:
                    return {"stopped": True, "remaining": 0}
                if remaining < 0:
                    return {"stopped": False, "remaining": -1}
                if time.monotonic() >= deadline:
                    return {"stopped": False, "remaining": remaining}
                time.sleep(0.05)
        finally:
            _kernel32.CloseHandle(job)

    def tree_alive(self, handle) -> bool | None:
        name = handle.group_token
        if not name:
            return None
        remaining = self._active_by_name(name)
        if remaining < 0:
            return None
        return remaining > 0


class _LockHandle:
    def __init__(self, handle):
        self._handle = handle

    @property
    def handle(self):
        return self._handle

    def close(self):
        if self._handle:
            ov = _OVERLAPPED()
            _kernel32.UnlockFileEx(self._handle, 0, 1, 0, ctypes.byref(ov))
            _kernel32.CloseHandle(self._handle)
            self._handle = None


def platform() -> WindowsPlatform:
    return WindowsPlatform()
