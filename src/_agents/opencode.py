"""Built-in OpenCode adapter.

``opencode run --format json`` emits a **JSON event stream** (design S4), not a
single trailing document, so "take the last JSON line" is explicitly wrong. The
parser walks every event, tolerates unknown non-terminal events (counted, not
guessed), and requires a recognised terminal event before it may report
``completed``.
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
from src._util import require

#: Event types that terminate a run. Unknown types are never terminal: a stream
#: that ends without one of these is invalid output, not success.
TERMINAL_TYPES = ("result", "done", "finished", "complete", "completed", "step_finish")
ERROR_TYPES = ("error", "fatal")


class OpenCodeAdapter(AgentAdapterBase):
    id = "opencode"
    kind = "builtin"
    default_backend = "opencode-cli"
    supported_backends = ("opencode-cli",)
    output_protocol = "json-events"
    credential_prefixes = ("OPENCODE_", "OPENAI_", "ANTHROPIC_")
    permission_profiles = ("isolated-edit",)
    #: opencode 的 ``--model`` 接受 ``provider/model`` 组合形态，因此 ``provider``
    #: 能被落实到有效配置（v1.2 §6.3：判定依据由适配器自己实现）。
    execution_fields = ("model", "provider")

    def probe_argv(self) -> tuple[str, ...]:
        return ("opencode", "--version")

    def build_launch(self, context: RunContext) -> LaunchSpec:
        options = validate_typed_options(context.options)
        argv = ["opencode", "run", "--format", "json"]
        model = context.model or options.get("model")
        if model and context.provider and "/" not in str(model):
            # `provider/model` 是 opencode 的有效配置形态；不拼前缀等于丢掉
            # 已声明的 provider，而"声明了却没生效"正是本版要拒绝的静默忽略。
            model = f"{context.provider}/{model}"
        if model:
            argv += ["--model", str(model)]
        argv += list(self.permission_argv(context.permission_profile))
        return LaunchSpec(
            argv=tuple(argv),
            stdin_file=context.prompt_file,
            env_names=context.env_names,
            output_protocol=self.output_protocol,
            max_output_bytes=context.max_output_bytes,
            session_dir=context.session_dir,
            agent_version=context.expected_version,
            extra={"permission": self.permission_state(context.permission_profile)},
        )

    def parse_output(self, capture: ProcessCapture) -> AgentResult:
        capture.require_not_truncated()
        events, unparsable = json_objects(capture.raw_text)
        require(events, "AGENT_RESULT_INVALID", "No OpenCode events captured")
        terminal, error, session, usage, unknown = None, None, None, {}, 0
        for event in events:
            if not isinstance(event, dict):
                unknown += 1
                continue
            kind = str(event.get("type") or event.get("event") or "")
            session = session or self._session(event)
            usage = usage or self._usage(event)
            if kind in ERROR_TYPES:
                error = event
            elif kind in TERMINAL_TYPES:
                if not event.get("error"):
                    terminal = event
        reported_model, reported_models = self.observe_models(events, terminal)
        if capture.exit_code != 0 or error is not None:
            summary = summarize(
                str(
                    (error or {}).get("message")
                    or (error or {}).get("error")
                    or f"OpenCode exited {capture.exit_code}"
                )
            )
            return self.result(
                capture,
                classify_failure(summary),
                summary,
                session_id=session,
                usage=usage,
                reported_model=reported_model,
                reported_models=reported_models,
            )
        if terminal is None:
            return self.result(
                capture,
                OUTCOME_INVALID_OUTPUT,
                f"OpenCode stream ended without a terminal event "
                f"({len(events)} events, {unparsable} unparsable)",
                session_id=session,
                usage=usage,
                reported_model=reported_model,
                reported_models=reported_models,
            )
        if terminal.get("error") or terminal.get("status") in ("failed", "blocked"):
            summary = summarize(str(terminal.get("error") or terminal.get("status")))
            return self.result(
                capture,
                OUTCOME_FAILED,
                summary,
                session_id=session,
                usage=usage,
                reported_model=reported_model,
                reported_models=reported_models,
            )
        summary = summarize(str(terminal.get("result") or terminal.get("message") or "completed"))
        return self.result(
            capture,
            OUTCOME_COMPLETED,
            summary,
            session_id=session,
            usage=usage,
            reported_model=reported_model,
            reported_models=reported_models,
        )

    @staticmethod
    def _session(event: dict):
        value = event.get("session_id") or event.get("sessionID")
        return value if isinstance(value, str) else None

    @staticmethod
    def _usage(event: dict) -> dict:
        tokens = event.get("tokens") if isinstance(event.get("tokens"), dict) else {}
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        return {
            "input_tokens": usage.get("input_tokens", tokens.get("input")),
            "output_tokens": usage.get("output_tokens", tokens.get("output")),
            "cost": event.get("cost"),
        }
