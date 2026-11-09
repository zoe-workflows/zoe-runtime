"""Path comparison shared by configuration, scope checks and preparation.

One module, one semantics. Every caller that asks "is this inside that" or
"does this entry cover that path" goes through here so that the answer cannot
differ between registration, ``create`` and the post-run diff check
(config-friction v5 §B0).

Rules:

* comparison form is POSIX-style with ``.``/``..`` folded, but the original text
  is never rewritten for the user;
* directory boundaries are component boundaries — ``src/a`` never matches
  ``src/abc``;
* case follows the platform's filesystem convention (:func:`os.path.normcase`)
  rather than lower-casing everything, which would be wrong on a
  case-sensitive macOS volume and on Linux;
* symlinks are honoured only where a real file exists (:func:`same_path`);
  planned paths that do not exist yet are compared textually, because resolving
  them would invent an answer.
"""

from __future__ import annotations

import os
import posixpath
from pathlib import Path

#: The repository root marker used by :func:`git_toplevel` callers.
_GIT_DIR = ".git"


def normalize(path: str | os.PathLike[str]) -> str:
    """Return the comparison form: separators unified, ``.``/``..`` folded.

    Empty input stays empty. A trailing slash is dropped (except for ``/``), so
    ``src/`` and ``src`` are the same scope entry.
    """
    text = str(path).strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    folded = posixpath.normpath(text)
    return folded


def _key(path: str | os.PathLike[str]) -> str:
    """Normalized, platform-case-folded key used for every comparison."""
    return os.path.normcase(normalize(path)).replace("\\", "/")


def is_within(path: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    """True when ``path`` equals ``parent`` or sits under it.

    Component-aware: ``src/abc`` is **not** within ``src/a``.

    An empty parent (or ``.``) is **not** a scope. It used to mean "anywhere",
    which turned an accidental ``allowed_paths: ["."]`` into whole-repository
    authorization on both the project and the spec side. Callers that really
    mean "unset" must say so explicitly instead of relying on a falsy key here.
    """
    child_key = _key(path)
    parent_key = _key(parent).rstrip("/")
    if not parent_key or parent_key == ".":
        return False
    if child_key == parent_key:
        return True
    return child_key.startswith(parent_key + "/")


def covers(path: str | os.PathLike[str], entries) -> bool:
    """True when any entry in ``entries`` is ``path`` or its ancestor."""
    return any(is_within(path, entry) for entry in entries or ())


def same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    """Equality that honours the real filesystem when both sides exist.

    ``os.path.samefile`` resolves symlinks and case differences the way the
    mounted volume does. When either side is missing (a path that will be
    created by the task), fall back to the textual key — never to a guess.
    """
    if _key(left) == _key(right):
        return True
    try:
        return os.path.samefile(str(left), str(right))
    except (OSError, ValueError):
        return False


def subtract(entries, excluded) -> list[str]:
    """Drop entries that are fully covered by ``excluded``.

    This is a **list-level** subtraction, not an expansion: ``["src"]`` minus
    ``["src/security"]`` stays ``["src"]`` because ``src`` is not inside
    ``src/security``. The protected list still wins at check time (deny-first in
    ``_backend.scope``), so the effective permission is the difference without
    the runtime having to enumerate every subdirectory.
    """
    remaining: list[str] = []
    for entry in entries or ():
        if any(is_within(entry, blocked) for blocked in excluded or ()):
            continue
        remaining.append(entry)
    return remaining


def declared_under(path: str | os.PathLike[str], protected) -> bool:
    """True when ``path`` explicitly names a protected entry or something in it.

    ``["src"]`` against ``["src/security"]`` is **not** a declaration: it asks
    for the parent directory, which is legitimate. ``["src/security"]`` against
    the same list is a declaration and must be refused before an agent starts.
    """
    return any(is_within(path, blocked) for blocked in protected or ())


def describe_missing(path: str | os.PathLike[str]) -> str | None:
    """Return a short reason when ``path`` looks wrong, else ``None``.

    Used for warnings only (never for a verdict): a path that does not exist may
    be created by the task, so this must not block anything.
    """
    text = normalize(path)
    if not text:
        return "empty path"
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        return None  # absolute paths are the project's business, not a typo
    if ".." in Path(text).parts:
        return "path escapes its own root with '..'"
    return None


def git_toplevel(start: str | os.PathLike[str] | None) -> str | None:
    """Return the repository root containing ``start``, or ``None``.

    Deliberately implemented by walking up for ``.git`` instead of shelling out:
    ``prepare --repo`` must answer this before any runtime state exists, and a
    git invocation would make the answer depend on the installed binary. A
    ``.git`` *file* (worktree/submodule) counts as a boundary too.
    """
    if not start:
        return None
    try:
        current = Path(start).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        marker = candidate / _GIT_DIR
        if marker.exists():
            return str(candidate)
    return None
