"""Static adapter registry: built-in and extended adapters, with capability states.

Design rules enforced here (multi-agent design §9.1):

* registration is a **code change**, not a runtime plugin; nothing is imported
  from a repository, a host or a request at runtime;
* a capability state is ``IMPLEMENTED`` (code exists, never verified),
  ``UNVERIFIED`` (verified once, but the current version/backend differs) or
  ``VERIFIED(version, backend)``. Only the last one may run a task;
* verification is granular in ``(adapter_id, backend)``. Evidence for one
  backend never unlocks another one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src._agents.base import (
    CAPABILITY_IMPLEMENTED,
    CAPABILITY_UNVERIFIED,
    CAPABILITY_VERIFIED,
    AgentAdapter,
)
from src._agents.base import PROTOCOL_REVISION as _PROTOCOL_REVISION
from src._agents.claude_code import ClaudeCodeAdapter
from src._agents.codebuddy import CodeBuddyAdapter
from src._agents.codex import CodexAdapter
from src._agents.opencode import OpenCodeAdapter
from src._util import Error, require

BUILTIN_IDS = ("codebuddy", "opencode", "claude-code")
EXTENDED_IDS = ("codex",)

#: Re-exported for `zoe capabilities`; the protocol itself lives in `base`.
PROTOCOL_REVISION = _PROTOCOL_REVISION


@dataclass(frozen=True)
class Verification:
    """One recorded compatibility result for a single ``(adapter, backend)``.

    The capability key is ``(adapter_id, backend, execution_backend, platform,
    agent_version, runtime_digest)`` (design D9 / §0.3). A record that carries a
    ``runtime_digest`` is held to that exact key: changing platform or swapping
    the CLI binary invalidates it. Records without a digest predate the platform
    layer and keep the legacy version-only semantics, so an operator's existing
    hand-written record is not silently invalidated.
    """

    backend: str
    version: str
    source: str = "local_admin"
    evidence_ref: str = ""
    platform: str = "any"
    matrix: str = "builtin"
    recorded_at: str = ""
    execution_backend: str = "local-process"
    runtime_digest: str = ""
    stopped_and_resumed: bool = False
    checks: tuple = ()

    @property
    def identity_bound(self) -> bool:
        """Whether this record pins the platform + runtime digest (D9 key)."""
        return bool(self.runtime_digest)

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "version": self.version,
            "source": self.source,
            "evidence_ref": self.evidence_ref,
            "platform": self.platform,
            "matrix": self.matrix,
            "recorded_at": self.recorded_at,
            "execution_backend": self.execution_backend,
            "runtime_digest": self.runtime_digest,
            "stopped_and_resumed": self.stopped_and_resumed,
            "checks": [dict(item) for item in self.checks],
        }

    @classmethod
    def from_dict(cls, value: dict) -> Verification:
        """Parse a record, accepting the qualification evidence field names.

        ``qualification run`` emits design-style names (``adapter_backend`` /
        ``agent_version``); the registry uses ``backend`` / ``version``. Both are
        accepted so the evidence file the controller produces is directly
        consumable as ``compatibility_evidence`` — otherwise the qualification
        loop can never be closed.
        """
        require(
            isinstance(value, dict), "INPUT_INCOMPLETE", "Verification record must be an object"
        )
        version = value.get("version") or value.get("agent_version")
        backend = value.get("backend") or value.get("adapter_backend")
        require(
            isinstance(version, str) and version,
            "INPUT_INCOMPLETE",
            "Verification record requires an exact version (version or agent_version)",
        )
        require(
            isinstance(backend, str) and backend,
            "INPUT_INCOMPLETE",
            "Verification record requires a backend (backend or adapter_backend)",
        )
        checks = value.get("checks")
        return cls(
            backend=backend,
            version=version,
            source=value.get("source", "local_admin"),
            evidence_ref=value.get("evidence_ref", ""),
            platform=value.get("platform", "any"),
            matrix=value.get("matrix", "builtin"),
            recorded_at=value.get("recorded_at", ""),
            execution_backend=value.get("execution_backend") or "local-process",
            runtime_digest=value.get("runtime_digest") or "",
            stopped_and_resumed=value.get("stopped_and_resumed") is True,
            checks=tuple(checks) if isinstance(checks, list) else (),
        )


@dataclass(frozen=True)
class AdapterEntry:
    adapter_id: str
    kind: str
    protocol_revision: int
    factory: Callable[[], AgentAdapter]
    #: Shipped verification records. Empty on purpose: this build has not run
    #: real e2e for any agent, so everything starts IMPLEMENTED.
    verified: tuple[Verification, ...] = ()

    @property
    def adapter(self) -> AgentAdapter:
        return self.factory()

    def _records(self, backend: str, extra: Verification | None = None) -> list[Verification]:
        records = [v for v in self.verified if v.backend == backend]
        if extra is not None and extra.backend == backend:
            records.append(extra)
        return records

    def capability(
        self,
        backend: str,
        expected_version: str | None,
        extra: Verification | None = None,
        *,
        platform: str | None = None,
        runtime_digest: str | None = None,
        allow_runtime_mismatch: bool = False,
    ) -> tuple[str, str | None, Verification | None]:
        """Return ``(capability, reason, verification)`` for one granularity.

        An adapter whose ``facts_status`` is ``FACT_PENDING`` is capped at
        ``IMPLEMENTED`` no matter what evidence an operator supplies: its command
        form was never frozen, so ``VERIFIED`` would advertise a runnable backend
        that cannot launch (multi-agent design §9.1, §10.4).

        For records that pin a ``runtime_digest`` the full D9 key applies: the
        platform and the current CLI fingerprint must both match, and the record
        must show all six qualification checks PASS plus a confirmed
        stop-and-resume. A record without a digest keeps the legacy
        version-only semantics.
        """
        adapter = self.adapter
        if getattr(adapter, "facts_status", "VERIFIED_FORM") == "FACT_PENDING":
            return (
                CAPABILITY_IMPLEMENTED,
                f"{self.adapter_id} ships with facts_status=FACT_PENDING; no command "
                "form is frozen, so no backend of this adapter may be reported as VERIFIED",
                None,
            )
        records = self._records(backend, extra)
        if not records:
            return (
                CAPABILITY_IMPLEMENTED,
                f"adapter code exists but no real end-to-end record for backend {backend!r}",
                None,
            )
        if not expected_version:
            return (
                CAPABILITY_UNVERIFIED,
                "profile does not pin expected_version; exact version required",
                None,
            )
        exact = [r for r in records if r.version == expected_version]
        if not exact:
            known = ", ".join(sorted({r.version for r in records}))
            return (
                CAPABILITY_UNVERIFIED,
                f"expected_version {expected_version!r} is not the verified version ({known})",
                None,
            )

        bound = [r for r in exact if r.identity_bound]
        if not bound:
            # Legacy operator record: no platform/digest to compare against.
            return CAPABILITY_VERIFIED, None, exact[0]

        matched = [
            r
            for r in bound
            if r.runtime_digest == runtime_digest and (platform is None or r.platform == platform)
        ]
        if not matched:
            reason = (
                "evidence is bound to a different platform or CLI fingerprint "
                f"(recorded platform={bound[0].platform!r}, "
                f"current platform={platform!r})"
            )
            if allow_runtime_mismatch:
                # 显式选择 runtime_binding=warn 的项目：版本仍精确匹配，指纹变了
                # 只记警告。默认 strict，不做这个让步。
                return (
                    CAPABILITY_VERIFIED,
                    reason + "; accepted because runtime_binding=warn",
                    bound[0],
                )
            return (
                CAPABILITY_IMPLEMENTED,
                reason + "; re-run qualification on this host",
                None,
            )
        # Six checks PASS + stop/resume are required for an identity-bound record.
        for record in matched:
            failed = [
                str(item.get("id"))
                for item in record.checks
                if isinstance(item, dict) and item.get("status") != "PASS"
            ]
            if failed or not record.stopped_and_resumed:
                missing = failed or ["stop-resume"]
                return (
                    CAPABILITY_UNVERIFIED,
                    "qualification is incomplete for this host: " + ", ".join(sorted(missing)),
                    None,
                )
            return CAPABILITY_VERIFIED, None, record
        return CAPABILITY_IMPLEMENTED, "no usable qualification record", None

    def to_dict(
        self,
        expected_version: str | None = None,
        extra: Verification | None = None,
        *,
        platform: str | None = None,
        runtime_digest: str | None = None,
    ) -> dict:
        adapter = self.adapter
        backends = []
        for backend in adapter.supported_backends:
            state, reason, verification = self.capability(
                backend,
                expected_version,
                extra,
                platform=platform,
                runtime_digest=runtime_digest,
            )
            backends.append(
                {
                    "backend": backend,
                    "capability": state,
                    "reason": reason,
                    "verified": verification.to_dict() if verification else None,
                }
            )
        return {
            "adapter_id": self.adapter_id,
            "kind": self.kind,
            "protocol_revision": self.protocol_revision,
            "output_protocol": adapter.output_protocol,
            "facts_status": getattr(adapter, "facts_status", "VERIFIED_FORM"),
            "backends": backends,
        }


_ENTRIES: tuple[AdapterEntry, ...] = (
    AdapterEntry("codebuddy", "builtin", 1, CodeBuddyAdapter),
    AdapterEntry("opencode", "builtin", 1, OpenCodeAdapter),
    AdapterEntry("claude-code", "builtin", 1, ClaudeCodeAdapter),
    AdapterEntry("codex", "extended", 1, CodexAdapter),
)

_BY_ID = {entry.adapter_id: entry for entry in _ENTRIES}


def entries() -> tuple[AdapterEntry, ...]:
    return _ENTRIES


def get(adapter_id: str) -> AdapterEntry | None:
    return _BY_ID.get(adapter_id)


def require_entry(adapter_id: str) -> AdapterEntry:
    entry = get(adapter_id)
    require(
        entry is not None,
        "ADAPTER_UNVERIFIED",
        f"Adapter {adapter_id!r} is not registered; adding one is a code change, "
        "not a runtime setting",
    )
    return entry


def adapter(adapter_id: str) -> AgentAdapter:
    return require_entry(adapter_id).factory()


def catalog(
    expected_versions: dict[str, str] | None = None, extras: dict[str, Verification] | None = None
) -> list[dict]:
    """Capability listing used by ``zoe capabilities`` and ``zoe agent list``."""
    expected_versions = expected_versions or {}
    extras = extras or {}
    return [
        entry.to_dict(
            expected_version=expected_versions.get(entry.adapter_id),
            extra=extras.get(entry.adapter_id),
        )
        for entry in _ENTRIES
    ]


def resolve(
    adapter_id: str,
    backend: str,
    expected_version: str | None,
    extra: Verification | None = None,
    *,
    platform: str | None = None,
    runtime_digest: str | None = None,
    allow_runtime_mismatch: bool = False,
) -> tuple[str, str | None, Verification | None]:
    """Registry-level capability decision for one ``(adapter, backend)``."""
    entry = require_entry(adapter_id)
    adapter_obj = entry.factory()
    if backend not in adapter_obj.supported_backends:
        raise Error(
            "ADAPTER_BACKEND_MISMATCH", f"{adapter_id} does not support backend {backend!r}"
        )
    return entry.capability(
        backend,
        expected_version,
        extra,
        platform=platform,
        runtime_digest=runtime_digest,
        allow_runtime_mismatch=allow_runtime_mismatch,
    )


def require_runnable(
    adapter_id: str,
    backend: str,
    expected_version: str | None,
    extra: Verification | None = None,
    *,
    platform: str | None = None,
    runtime_digest: str | None = None,
) -> Verification | None:
    """Gate used at ``start``/``reassign``; never falls back to "close enough"."""
    state, reason, verification = resolve(
        adapter_id,
        backend,
        expected_version,
        extra,
        platform=platform,
        runtime_digest=runtime_digest,
    )
    if state == CAPABILITY_VERIFIED:
        return verification
    code = "ADAPTER_UNVERIFIED" if state == CAPABILITY_IMPLEMENTED else "ADAPTER_VERSION_MISMATCH"
    raise Error(
        code, "{}/{} is {}: {}".format(adapter_id, backend, state, reason or "not verified")
    )


def capability_summary() -> dict:
    return {
        "capabilities": [CAPABILITY_IMPLEMENTED, CAPABILITY_UNVERIFIED, CAPABILITY_VERIFIED],
        "builtin": list(BUILTIN_IDS),
        "extended": list(EXTENDED_IDS),
        "note": (
            "Capability is per (adapter_id, backend). An unverified backend is blocked at "
            "start; evidence for one backend never unlocks another."
        ),
    }
