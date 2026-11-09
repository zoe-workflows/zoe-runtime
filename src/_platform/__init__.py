"""Platform layer entry point: pick the concrete implementation for this host.

``get_platform()`` returns a singleton for the current OS. Everything that needs
a lock, process identity, tree probing or tree stop goes through this, so no
other module imports ``fcntl`` or calls ``os.kill``/``killpg`` directly.
"""

from __future__ import annotations

import sys

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    from src._platform.windows import WindowsPlatform as _Impl
else:
    from src._platform.posix import PosixPlatform as _Impl

_instance = None


def get_platform():
    """Return the process-wide platform backend singleton."""
    global _instance
    if _instance is None:
        _instance = _Impl()
    return _instance


def is_windows() -> bool:
    return _IS_WINDOWS


__all__ = ["get_platform", "is_windows"]
