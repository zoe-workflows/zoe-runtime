"""Extended adapter: Codex CLI.

**Facts status: FACT_PENDING (multi-agent design §3, §9.2, §10.4).** The official
Codex interface was not verified when this build was written, so this module
deliberately asserts **no** unverified fact:

* no command form (``build_launch`` / ``probe_argv`` refuse);
* no backend identity — the single registered backend is the placeholder
  ``backend-pending``, replaced by M3x with the verified enum;
* no authentication mode — only the adapter-owned ``CODEX_`` credential
  namespace is accepted, which says nothing about *how* codex authenticates;
* no output field names — the parser matches structural keywords
  (``result`` / ``done`` / ``finish`` / ``complete``) instead of hard-coding a
  vendor event name such as ``turn.completed``.

What *is* implemented is the contract surface: profile validation per
``(adapter_id, backend)``, credential scoping, a tolerant parser for both
candidate protocols, and a launch gate. None of that makes the adapter runnable:
the registry caps ``FACT_PENDING`` adapters at ``IMPLEMENTED``, ``start`` blocks
with ``ADAPTER_UNVERIFIED``, and ``build_launch`` blocks again if someone
bypasses the registry.
"""

from __future__ import annotations

from src._agents.base import (
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_INVALID_OUTPUT,
    AgentAdapterBase,
    AgentResult,
    LaunchSpec,
    ProcessCapture,
    RunContext,
    classify_failure,
    json_objects,
    summarize,
    validate_typed_options,
)
from src._util import Error, require

#: The five items M3x must verify before any command form is written down here.
FACT_PENDING_ITEMS = (
    "non_interactive_entrypoint_and_arguments",
    "output_protocol_and_termination",
    "backend_authentication_and_credential_injection",
    "session_resume_semantics_and_repo_check_switch",
    "distribution_variance",
)

FACTS_REFERENCE = "compat/codex-<backend>.json"

#: Placeholder backend. A real backend identifier is a *verified fact*, so this
#: build refuses to name one (design §9.2: "本文不预设任何后端标识").
BACKEND_PLACEHOLDER = "backend-pending"

#: Structural terminal keywords. These are protocol shape words, not vendor
#: field names; a stream must still end with one before it counts as finished.
_TERMINAL_KEYWORDS = ("result", "done", "finished", "finish", "complete", "completed", "end")
_ERROR_KEYWORDS = ("error", "fatal", "failed", "blocked")


class CodexAdapter(AgentAdapterBase):
    id = "codex"
    kind = "extended"
    default_backend = BACKEND_PLACEHOLDER
    supported_backends = (BACKEND_PLACEHOLDER,)
    output_protocol = "unverified-final-or-events"
    #: Adapter-owned credential namespace only. This is not a statement about the
    #: real auth mode (design §10.4 marks it FACT_PENDING).
    credential_prefixes = ("CODEX_",)
    permission_profiles = ("isolated-edit-codex",)
    facts_status = "FACT_PENDING"

    # -- profile ----------------------------------------------------------- #
    def validate_profile(self, profile: dict) -> None:
        # A multi-backend adapter must declare its backend explicitly (D27);
        # the backend may never hide in a free-form profile field.
        require(
            bool(profile.get("backend")),
            "ADAPTER_BACKEND_MISMATCH",
            "codex supports multiple backends; profile must declare backend explicitly",
        )
        super().validate_profile(profile)

    # -- launch ------------------------------------------------------------ #
    def probe_argv(self) -> tuple[str, ...]:
        raise Error(
            "ADAPTER_VERSION_MISMATCH",
            "codex CLI facts are FACT_PENDING ({}); record them in {} before probing".format(
                ", ".join(FACT_PENDING_ITEMS), FACTS_REFERENCE
            ),
        )

    def build_launch(self, context: RunContext) -> LaunchSpec:
        validate_typed_options(context.options)
        raise Error(
            "ADAPTER_VERSION_MISMATCH",
            "codex has no frozen command form in this build (FACT_PENDING: {}). "
            "Record the verified form in {} before enabling the adapter.".format(
                ", ".join(FACT_PENDING_ITEMS), FACTS_REFERENCE
            ),
        )

    # -- parsing ----------------------------------------------------------- #
    def parse_output(self, capture: ProcessCapture) -> AgentResult:
        """Tolerant parser for either candidate protocol (version-pinned later).

        It may only ever *refuse* to report success: an unrecognised stream, a
        missing terminal marker or a non-zero exit is never ``completed``.
        """
        capture.require_not_truncated()
        objects, unparsable = json_objects(capture.raw_text)
        require(objects, "AGENT_RESULT_INVALID", "No Codex result captured")
        terminal, error, session, usage = None, None, None, {}
        for event in objects:
            if not isinstance(event, dict):
                continue
            session = session or self._session(event)
            usage = usage or self._usage(event)
            kind = str(event.get("type") or event.get("event") or "").lower()
            if event.get("error") or self._matches(kind, _ERROR_KEYWORDS):
                error = event
            elif self._matches(kind, _TERMINAL_KEYWORDS):
                terminal = event
            elif kind == "" and {"result", "status", "output"} & set(event):
                terminal = event
        if capture.exit_code != 0 or error is not None:
            summary = summarize(
                str((error or {}).get("message") or f"Codex exited {capture.exit_code}")
            )
            return self.result(
                capture, classify_failure(summary), summary, session_id=session, usage=usage
            )
        if terminal is None:
            return self.result(
                capture,
                OUTCOME_INVALID_OUTPUT,
                f"Codex stream ended without a recognised terminal event "
                f"({len(objects)} objects, {unparsable} unparsable)",
                session_id=session,
                usage=usage,
            )
        if terminal.get("status") in ("failed", "blocked"):
            return self.result(
                capture,
                OUTCOME_FAILED,
                summarize(str(terminal.get("status"))),
                session_id=session,
                usage=usage,
            )
        return self.result(
            capture,
            OUTCOME_COMPLETED,
            summarize(str(terminal.get("result") or terminal.get("output") or "completed")),
            session_id=session,
            usage=usage,
        )

    @staticmethod
    def _matches(kind: str, keywords: tuple[str, ...]) -> bool:
        """Keyword-shape match on an event kind, never an exact vendor name."""
        if not kind:
            return False
        tokens = kind.replace(".", "_").replace("-", "_").split("_")
        return any(token in keywords for token in tokens)

    @staticmethod
    def _session(event: dict):
        """Opportunistic diagnostic only: it never influences success."""
        value = event.get("session_id") or event.get("sessionId") or event.get("session")
        return value if isinstance(value, str) else None

    @staticmethod
    def _usage(event: dict) -> dict:
        """Opportunistic diagnostic only: unknown shapes stay ``None``."""
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        return {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cost": event.get("cost"),
        }
