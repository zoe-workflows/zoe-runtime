"""CLI discovery: the binary ``doctor`` reports is the binary ``start`` launches.

The point of this module is *not* "can we find a CLI somewhere". It is: the
absolute path reported by ``doctor`` / ``agent probe`` and the ``argv[0]`` that
the controller actually spawns must be the same file, measured in the same
environment. Everything else (version strings, notes) is diagnostics.

Design rules (v0.5 §6):

* resolution order is fixed and not configurable:
  ``explicit`` → ``process-path`` → ``known-location`` → ``login-shell``;
* ``CLI_FACTS`` only carries *detection* facts. Launch shapes
  (``promptTransport`` / ``sendKeys*`` / permission flags) are deliberately
  absent: this controller has a single non-interactive launch form and the
  adapter registry is the only source of "what can be started";
* any user-supplied string never reaches a shell command line. The only name
  that reaches ``$SHELL -i -c "command -v <name>"`` comes from ``CLI_FACTS`` and
  is validated against ``_NAME_PATTERN`` first.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

from src._platform import is_windows
from src._util import PLATFORM_ENV_NAMES, Error

#: Hard upper bounds, not suggestions (§6.5).
#: 3.0s 对交互式 shell 的登录脚本太紧（实测 zsh + 框架 ~1.4s，机器一忙就超时，
#: 表现为"偶尔找不到 CLI"）。放宽到 5s 仍是有限等待，不改变"超时必须报错"的语义。
RESOLVE_TIMEOUT_SECONDS = 5.0
VERSION_TIMEOUT_SECONDS = 5.0
PROBE_OUTPUT_LIMIT_BYTES = 4096

SOURCE_EXPLICIT = "explicit"
SOURCE_PROCESS_PATH = "process-path"
SOURCE_KNOWN_LOCATION = "known-location"
SOURCE_LOGIN_SHELL = "login-shell"
SOURCE_UNRESOLVED = "unresolved"

SOURCES = (
    SOURCE_EXPLICIT,
    SOURCE_PROCESS_PATH,
    SOURCE_KNOWN_LOCATION,
    SOURCE_LOGIN_SHELL,
    SOURCE_UNRESOLVED,
)

_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

_HOME = str(Path.home())

#: Detection-only metadata for the CLIs the registry can launch (§6.3).
#: ``~`` is expanded when the table is built, so entries are absolute paths.
#: Multi-binary CLIs list every name in ``aliases``; the first resolvable one
#: wins and the choice is reported through ``resolution_source``/``notes``.
CLI_FACTS = {
    "codebuddy": {
        "aliases": ("codebuddy", "cbc"),
        "common_paths": (
            _HOME + "/.local/bin/codebuddy",
            _HOME + "/.codebuddy/bin/codebuddy",
            "/usr/local/bin/codebuddy",
            "/opt/homebrew/bin/codebuddy",
            _HOME + "/bin/codebuddy",
        ),
        "install_test": "command -v codebuddy",
        "version_argv": ("--version",),
        "headless": "AVAILABLE",
        # §10.2 Q4: Zoe never processed the CLI's own automatic discovery of
        # user/project settings. Recorded, never claimed to be isolated.
        "config_sources": "UNKNOWN",
    },
    "claude": {
        "aliases": ("claude",),
        "common_paths": (
            _HOME + "/.claude/local/claude",
            _HOME + "/.local/bin/claude",
            "/usr/local/bin/claude",
            "/opt/homebrew/bin/claude",
            "/usr/bin/claude",
            _HOME + "/bin/claude",
        ),
        "install_test": "command -v claude",
        "version_argv": ("--version",),
        "headless": "AVAILABLE",
        "config_sources": "LOADS_USER_AND_PROJECT_SETTINGS",
    },
    "opencode": {
        "aliases": ("opencode",),
        "common_paths": (
            _HOME + "/.local/bin/opencode",
            _HOME + "/.opencode/bin/opencode",
            "/usr/local/bin/opencode",
            "/opt/homebrew/bin/opencode",
            _HOME + "/bin/opencode",
        ),
        "install_test": "command -v opencode",
        "version_argv": ("--version",),
        "headless": "AVAILABLE",
        "config_sources": "UNKNOWN",
    },
    "codex": {
        "aliases": ("codex",),
        "common_paths": (
            _HOME + "/.local/bin/codex",
            "/usr/local/bin/codex",
            "/opt/homebrew/bin/codex",
            _HOME + "/bin/codex",
        ),
        "install_test": "command -v codex",
        "version_argv": ("--version",),
        # No frozen command form exists, so no headless claim is made.
        "headless": "FACT_PENDING",
        "config_sources": "UNKNOWN",
    },
}

#: ``adapter_id`` → the binary name that adapter launches. Kept next to
#: ``CLI_FACTS`` so a new adapter cannot silently skip discovery.
ADAPTER_CLI = {
    "codebuddy": "codebuddy",
    "claude-code": "claude",
    "opencode": "opencode",
    "codex": "codex",
}

#: In-process cache: path resolution and version probing are subprocess work, so
#: ``doctor``, ``agent list``, ``qualification run`` and ``start`` share one
#: result per (cli, explicit path, env names, probe mode). Never persisted:
#: a version measured yesterday must not masquerade as today's fact.
# Two caches, one truth per fact:
#   _PATH_CACHE   (name, explicit, env) -> which binary, from which source.
#   _VERSION_CACHE (path, env)          -> what that binary answered.
# Splitting them is what lets `doctor` (which probes the version) and `start`
# (which only needs the path) share one resolution instead of disagreeing, while
# still never reusing a version for a *different* binary (§6.5).
_PATH_CACHE: dict[tuple, Resolution] = {}
_VERSION_CACHE: dict[tuple, tuple[str | None, list[str]]] = {}


@dataclass(frozen=True)
class Resolution:
    """Where a CLI binary was found, and what it reported in the launch env."""

    path: str | None
    source: str
    version: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def resolved(self) -> bool:
        return bool(self.path)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "source": self.source,
            "version": self.version,
            "notes": list(self.notes),
        }


def normalize_backend(backend: str | None) -> str:
    """Return the canonical execution-backend name.

    ``local`` is the legacy alias of ``local-process`` (§6.6). Comparing the raw
    string would let ``local`` bypass launcher pinning.
    """
    if backend in (None, "", "local", "local-process"):
        return "local-process"
    return str(backend)


def cli_name_for_adapter(adapter_id: str) -> str | None:
    """The binary name an adapter launches, or ``None`` for an unknown adapter."""
    return ADAPTER_CLI.get(adapter_id)


def clear_cache() -> None:
    """Drop the in-process resolution cache (tests and long-lived hosts)."""
    _PATH_CACHE.clear()
    _VERSION_CACHE.clear()


def remembered_version(path: str | None, env_names=None) -> str | None:
    """The version already measured for ``path`` in this process, if any.

    Used by ``pin_launcher`` so a run inherits the probe that ``doctor`` /
    ``agent probe`` / ``qualification run`` already paid for. It never starts a
    subprocess: "not measured yet" stays ``None`` rather than becoming a guess.
    """
    if not path:
        return None
    entry = _VERSION_CACHE.get((path, tuple(sorted(env_names or ()))))
    return entry[0] if entry else None


def remember_version(path: str | None, env_names, version: str | None) -> None:
    """Record a version measured outside ``resolve_cli`` (e.g. ``agent probe``)."""
    if path and version:
        _VERSION_CACHE[(path, tuple(sorted(env_names or ())))] = (version, [])


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
        raise Error("INPUT_INCOMPLETE", f"Invalid CLI name {name!r}")
    return name


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def probe_env(env_names=None) -> dict:
    """Environment for discovery/version probing (§6.5).

    Same shape as the launch allowlist: OS plumbing by name, plus the profile's
    declared credential names. Never the full parent environment — a version
    measured under a different environment does not describe the launch.
    """
    env = {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", ""),
        "TERM": "dumb",
    }
    for key in PLATFORM_ENV_NAMES:
        if os.environ.get(key):
            env[key] = os.environ[key]
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    for name in env_names or ():
        if isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            if name in os.environ:
                env[name] = os.environ[name]
    return env


def run_bounded(argv, timeout: float, env=None) -> tuple[int | None, str, str]:
    """Run ``argv`` with a hard timeout. Returns ``(code, text, note)``.

    ``code`` is ``None`` when the process could not start or timed out; the note
    explains which. Output is truncated to ``PROBE_OUTPUT_LIMIT_BYTES`` so a
    chatty rc/profile cannot fill memory.
    """
    try:
        proc = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=not is_windows(),
        )
    except OSError as exc:
        return None, "", f"cannot start {argv[0]}: {exc}"
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return None, "", f"timed out after {timeout:g}s"
    text = (out or b"")[:PROBE_OUTPUT_LIMIT_BYTES].decode("utf-8", "replace")
    return proc.returncode, text, ""


def _kill_group(proc) -> None:
    """Kill the whole session group, not just the direct child (§6.4)."""
    if is_windows():
        try:
            proc.kill()
        except OSError:
            pass
        return
    import signal

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _probe_version(path: str, facts: dict, env_names, parse_version) -> tuple[str | None, list]:
    notes = []
    tail = tuple(facts.get("version_argv") or ("--version",))
    code, text, note = run_bounded(
        [path, *tail], timeout=VERSION_TIMEOUT_SECONDS, env=probe_env(env_names)
    )
    if note:
        notes.append(f"version probe: {note}")
        return None, notes
    version = parse_version(text) if parse_version else None
    if version is None:
        notes.append("version probe returned no parsable version")
    elif code not in (0, None):
        notes.append(f"version probe exited {code}")
    return version, notes


def _login_shell(name: str, timeout: float) -> tuple[str | None, list]:
    """POSIX-only bounded interactive-shell fallback (§6.4).

    ``$SHELL`` missing means *skip*, never fall back to ``/bin/sh``: ``sh -i``
    does not read the user's rc files, so falling back would make the notes lie
    about what was actually tried.
    """
    if is_windows():
        return None, ["login-shell lookup is POSIX only"]
    shell = os.environ.get("SHELL")
    if not shell or not _executable(shell):
        return None, ["no interactive shell"]
    code, text, note = run_bounded([shell, "-i", "-c", "command -v " + name], timeout=timeout)
    if note:
        return None, ["login-shell timeout" if "timed out" in note else f"login-shell: {note}"]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if not os.path.isabs(line):
            continue
        if os.path.isdir(line) or not _executable(line):
            return None, [f"login-shell returned a non-executable path: {line}"]
        return line, []
    if lines:
        return None, ["login-shell returned no absolute path (function or alias?)"]
    return None, ["login-shell returned nothing"]


def _resolve(
    name: str,
    explicit: str | None,
    env_names,
    parse_version,
    timeout: float,
) -> Resolution:
    facts = CLI_FACTS.get(name, {})
    names = tuple(facts.get("aliases") or (name,))
    notes: list[str] = []

    # 1. explicit path: must be absolute and executable, otherwise the claim is
    #    recorded and resolution continues with the next source.
    if explicit:
        candidate = str(Path(explicit).expanduser())
        if os.path.isabs(candidate) and _executable(candidate):
            version, more = (
                _probe_version(candidate, facts, env_names, parse_version)
                if parse_version
                else (None, [])
            )
            return Resolution(candidate, SOURCE_EXPLICIT, version, tuple(notes + more))
        notes.append(f"explicit path {explicit!r} is not an executable absolute path")

    # 2. + 3. process PATH and known locations. Both are cheap, so both are
    #    scanned: that is what lets `notes` list the versions that were *not*
    #    selected instead of silently picking one (§6.8).
    candidates: list[tuple[str, str]] = []
    for alias in names:
        found = shutil.which(alias)
        if found:
            candidates.append((SOURCE_PROCESS_PATH, str(found)))
    for raw in facts.get("common_paths") or ():
        path = str(Path(raw).expanduser())
        if _executable(path):
            candidates.append((SOURCE_KNOWN_LOCATION, path))
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source, path in candidates:
        key = os.path.realpath(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append((source, path))

    if unique:
        source, path = unique[0]
        if len(unique) > 1:
            others = ", ".join(item for _, item in unique[1:])
            notes.append(f"multiple versions found: {path} (selected), {others}")
        version, more = (
            _probe_version(path, facts, env_names, parse_version) if parse_version else (None, [])
        )
        return Resolution(path, source, version, tuple(notes + more))

    # 4. bounded login-shell fallback.
    path, more = _login_shell(name, timeout)
    notes.extend(more)
    if path:
        version, extra = (
            _probe_version(path, facts, env_names, parse_version) if parse_version else (None, [])
        )
        return Resolution(path, SOURCE_LOGIN_SHELL, version, tuple(notes + extra))
    return Resolution(None, SOURCE_UNRESOLVED, None, tuple(notes))


def resolve_cli(
    name: str,
    explicit: str | None = None,
    *,
    env_names=None,
    parse_version=None,
    timeout: float = RESOLVE_TIMEOUT_SECONDS,
) -> Resolution:
    """Resolve ``name`` to an absolute executable, memoised per process.

    ``parse_version`` is the adapter's own ``--version`` parser; passing ``None``
    skips the version subprocess entirely (``pin_launcher`` only needs a path).
    """
    _check_name(name)
    env_key = tuple(sorted(env_names or ()))
    path_key = (name, explicit, env_key)
    base = _PATH_CACHE.get(path_key)
    if base is None:
        # Path discovery always runs without a version probe: the version is an
        # *enrichment* of a path, never a reason to resolve that path twice.
        base = _resolve(name, explicit, env_key, None, timeout)
        _PATH_CACHE[path_key] = base
    if parse_version is None or not base.path:
        return base
    version_key = (base.path, env_key)
    probed = _VERSION_CACHE.get(version_key)
    if probed is None:
        probed = _probe_version(base.path, CLI_FACTS.get(name, {}), env_key, parse_version)
        _VERSION_CACHE[version_key] = probed
    version, notes = probed
    return Resolution(
        path=base.path,
        source=base.source,
        version=version,
        notes=tuple(list(base.notes) + list(notes)),
    )


def resolution_for_adapter(
    adapter, *, env_names=None, explicit=None, probe: bool = True
) -> Resolution | None:
    """Resolve the CLI of ``adapter`` with a version probe, never raising.

    Only adapters that have a frozen command form are probed: a
    ``FACT_PENDING`` adapter gets path discovery without a version claim.
    ``probe=False`` (``route plan --no-probe``) skips the ``--version``
    subprocess entirely and reports the version as unprobed — the absence must
    stay visible, not be filled in with a guess.
    """
    cli = cli_name_for_adapter(getattr(adapter, "id", ""))
    if cli is None:
        return None
    facts_pending = getattr(adapter, "facts_status", "VERIFIED_FORM") == "FACT_PENDING"
    try:
        return resolve_cli(
            cli,
            explicit=explicit,
            env_names=env_names,
            parse_version=None if (facts_pending or not probe) else adapter.parse_probe,
        )
    except Error:
        return None


def pin_launcher(
    launch,
    backend: str | None,
    *,
    env_names=None,
    explicit=None,
    probe=False,
    parse_version=None,
):
    """Pin ``launch.argv[0]`` to the resolved absolute path (§6.6).

    Only ``argv[0]`` is rewritten. The working directory and the environment are
    untouched: an agent that suddenly saw another directory would be a different
    execution, not a more precise one. ``docker`` is never pinned — an absolute
    host path would not exist inside the container's PATH.

    ``parse_version`` (v0.5.1): pass the adapter's own ``--version`` parser to
    measure the version **in the launch environment** and record it on the
    resolution. Without it the run record's ``cli_version`` was always ``null``
    and the profile's ``expected_version`` was never compared to reality; the
    subprocess is memoised per ``(path, env_names)`` so this costs one probe.
    """
    if normalize_backend(backend) != "local-process":
        return launch
    argv = list(launch.argv)
    if not argv:
        return launch
    raw = argv[0]
    # An adapter normally emits a bare name. If a profile already pinned an
    # absolute path, that operator choice wins over discovery: re-resolving it by
    # basename would silently replace it with another candidate.
    looks_like_path = os.sep in raw or bool(os.altsep and os.altsep in raw)
    explicit = raw if looks_like_path else explicit
    name = Path(raw).name
    try:
        resolution = resolve_cli(
            name, explicit=explicit, env_names=env_names, parse_version=parse_version
        )
    except Error:
        return launch
    extra = dict(launch.extra)
    measured = remembered_version(resolution.path, env_names)
    if measured:
        resolution = replace(resolution, version=measured)
    extra["resolution"] = resolution.to_dict()
    if probe:
        extra["probe"] = True
    if not resolution.path or resolution.path == argv[0]:
        # Unresolved: keep the bare name so the launch still fails with the
        # spawn error, but record that discovery was attempted and why it failed.
        return replace(launch, extra=extra)
    argv[0] = resolution.path
    return replace(launch, argv=tuple(argv), extra=extra)
