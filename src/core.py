"""Zoe Runtime — compatibility aggregator.

The implementation lives in focused submodules:

- ``_util``       — errors, hashing, atomic writes, subprocess/git helpers
- ``_proc``       — process identity (PID-reuse guard)
- ``_config``     — project configuration, profiles and policy
- ``_profiles``   — agent profile binding, capability and compatibility evidence
- ``_agents``     — AgentAdapter contract plus the (built-in + extended) registry
- ``_contracts``  — explicit request/spec validation
- ``_groups``     — ordered task groups (records only, no scheduler)
- ``_evidence``   — delivery evidence and its validity
- ``_platform``   — locks, process identity, process-tree stop/alive (POSIX/Windows)
- ``_store``      — SQLite store (schema v3: meta + orchestration)
- ``_logstream``  — incremental log cursor + per-line stream-json projection
- ``_progress``   — run observation facts (``progress.json``), host-level thresholds
- ``_migrations`` — explicit v1 → v2 → v3 migration
- ``_summaries``  — versioned readable summaries and ``report.md``
- ``_reports``    — delivery report (``delivery-<rev>.md``) for ``task report``
- ``_orchestration`` — orchestration record, decision CAS, ownership
- ``_context``    — project context bundle and identity checks
- ``_qualification`` — local CLI qualification evidence
- ``_backend``    — Docker/LocalProcess lifecycle and workspace scope/fingerprint
- ``_publisher``  — Git/PR publication
- ``_routing``    — execution-combination routing (snapshot, pure decide, gates, stats)
- ``_worktree``   — workspace baseline and patch-first quarantine
- ``_pipeline``   — task lifecycle orchestration

This module re-exports every public symbol for backward compatibility, so
``from src.core import *`` and ``patch('src.core.<name>')`` keep
working while the code itself is split for auditability.
"""
from __future__ import annotations

from src import _context as context_mod
from src import _orchestration as orchestration_mod
from src import _qualification as qualification_mod
from src import _summaries as summaries_mod
from src._agents import (
    AgentAdapter,
    AgentResult,
    LaunchSpec,
    ProcessCapture,
    RunContext,
)
from src._agents import (
    catalog as adapter_catalog,
)
from src._agents import (
    entries as adapter_entries,
)
from src._backend import (
    BackendBase,
    Docker,
    LocalProcess,
    create_backend,
    fingerprint,
    parse_agent_output,
    scope,
)
from src._config import (
    _default_deny_prefixes,
    _is_within,
    authorize,
    doctor,
    validate_config,
)
from src._groups import create as group_create
from src._groups import status as group_status
from src._migrations import migrate
from src._pipeline import (
    accept,
    close_external,
    create,
    lifecycle_locks,
    reassign,
    rebind,
    reconcile,
    release_run_tree,
    revise,
    run_pipeline,
    slots,
    start,
    stop,
)
from src._proc import alive, process_identity
from src._profiles import (
    ProfileBinding,
    normalize_config,
    profile_views,
    require_runnable,
    resolve_binding,
)
from src._publisher import Publisher
from src._reports import (
    render as render_report,
)
from src._reports import (
    snapshot as report_snapshot,
)
from src._store import SCHEMA_VERSION, Store
from src._util import (
    LEASE_FD,
    TERMINAL,
    VERSION,
    Error,
    atomic,
    command,
    digest,
    git,
    lease_fds,
    read,
    require,
    safe_id,
)

__all__ = [
    "Error", "LEASE_FD", "TERMINAL", "VERSION",
    "atomic", "command", "digest", "git", "lease_fds", "read", "require", "safe_id",
    "alive", "process_identity",
    "authorize", "doctor", "validate_config",
    "normalize_config", "profile_views", "require_runnable", "resolve_binding", "ProfileBinding",
    "AgentAdapter", "AgentResult", "LaunchSpec", "ProcessCapture", "RunContext",
    "adapter_catalog", "adapter_entries",
    "SCHEMA_VERSION", "Store", "migrate",
    "BackendBase", "Docker", "LocalProcess", "create_backend", "fingerprint", "parse_agent_output", "scope",
    "Publisher",
    "accept", "close_external", "create", "reconcile", "reassign", "rebind", "revise", "run_pipeline",
    "start", "stop",
    "lifecycle_locks", "release_run_tree", "slots",
    "render_report", "report_snapshot",
    "group_create", "group_status",
]
