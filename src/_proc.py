"""Process identity helpers. Guards against PID reuse when reconciling writers.

This module keeps the POSIX implementation (matching v0.3 semantics, so existing
callers and their patches keep working) and delegates to the platform layer for
Windows, where ``os.kill(pid, 0)`` would *terminate* the target. The platform
layer owns the canonical implementation; this shim only exists for the
``process_identity`` patch point used by tests and legacy callers.
"""

from __future__ import annotations

import os

from src._platform import get_platform, is_windows
from src._util import Error


def process_identity(pid):
    return get_platform().process_identity(pid)


def alive(pid, identity):
    if not pid or not identity:
        return False
    if is_windows():
        return get_platform().alive(pid, identity)
    # POSIX: probe is a no-op signal (safe here), identity via the patchable
    # process_identity above.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        raise Error("PROCESS_UNKNOWN", "Cannot inspect process identity") from None
    try:
        return process_identity(pid) == identity
    except (OSError, Error):
        raise Error("PROCESS_UNKNOWN", "Process exists but identity cannot be verified") from None
