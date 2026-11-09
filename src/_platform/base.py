"""Platform abstraction layer interface (v0.4.0).

The platform layer isolates every OS assumption that previously leaked into
``_store``, ``_util``, ``_proc``, ``_pipeline``, ``_migrations`` and ``_backend``:
file locks (shared/exclusive), process identity (PID-reuse guard), tree-alive
probing, tree stop, and spawn-with-tree. macOS (POSIX) and Windows provide the
two concrete implementations.

The contract is deliberately small and never raises platform-specific signals on
the probing path: ``alive`` must only *observe*, never signal. This is the highest
correctness risk on Windows, where ``os.kill(pid, 0)`` terminates the target.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Platform(Protocol):
    """Capabilities a concrete platform backend must provide."""

    name: str

    def lock_shared(self, path: str) -> object:
        """Acquire a shared (read) lock on ``path`` (blocking)."""

    def lock_exclusive(
        self, path: str, blocking: bool = True, timeout: float | None = None
    ) -> object:
        """Acquire an exclusive lock on ``path``.

        ``blocking=False`` must raise :class:`src._util.Error` with code
        ``SLOT_BUSY`` rather than blocking. ``timeout`` bounds a blocking wait.
        """

    def unlock(self, handle: object) -> None:
        """Release a lock previously returned by lock_shared/lock_exclusive."""

    def process_identity(self, pid: int) -> str | None:
        """Return an opaque, reuse-safe identity token for ``pid`` or ``None``.

        ``None`` means the process does not exist or its identity cannot be
        determined. Must never signal the target.
        """

    def alive(self, pid: int, identity: str | None) -> bool:
        """Return whether ``pid`` is alive and matches ``identity``.

        Must only observe, never signal. Raises
        :class:`src._util.Error` with code ``PROCESS_UNKNOWN`` when the
        answer cannot be determined.
        """

    def spawn(self, argv, cwd, env, stdout, stdin=None, isolate_tree: bool = True):
        """Spawn ``argv`` and return a handle exposing ``pid``/``start_token``/
        ``group_token``.

        ``isolate_tree=True`` puts the child in its own tree (session/process
        group on POSIX, a named Job Object on Windows) so the whole subtree can
        be observed and stopped together. ``isolate_tree=False`` is for long-lived
        controllers that must outlive the spawning process: on Windows such a
        child must **not** be placed in a kill-on-close job, or it dies the
        moment the spawner exits.
        """

    def stop_tree(self, handle, timeout: float) -> dict:
        """Stop a process tree and return ``{"stopped": bool, "remaining": int}``."""

    def tree_alive(self, handle) -> bool | None:
        """Return whether any process in the tree is still alive, or ``None`` if
        undeterminable."""


class ProcessHandle:
    """Process-tree handle shared by POSIX and Windows spawns.

    ``start_token`` and ``group_token`` are the persistent tokens written to the
    run record so a *new* process can re-observe the tree after a crash (the
    ``LocalProcess`` recovery defect described in the design §2.4).
    """

    #: ``group_token`` is the pgid (POSIX) or the *job name* (Windows). A job name
    #: is used instead of a HANDLE precisely because a HANDLE is only valid in the
    #: process that created it, so a reconciling process could never observe it.
    #: ``_job`` keeps the creating process's own handle open so that
    #: ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` takes the whole tree down if this
    #: process dies.
    __slots__ = ("pid", "start_token", "group_token", "_popen", "_platform", "_job")

    def __init__(self, pid, start_token, group_token, popen=None, platform=None, job=None):
        self.pid = pid
        self.start_token = start_token
        self.group_token = group_token
        self._popen = popen
        self._platform = platform
        self._job = job
