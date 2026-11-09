"""Built-in Claude Code adapter.

``claude -p --output-format json`` returns one final result object. The session
id is recorded for diagnosis only: Zoe does not promise native session resume
(multi-agent design §10). A resume request is a controller-reserved parameter
and is rejected before launch.
"""

from __future__ import annotations

from src._agents.base import (
    OUTCOME_COMPLETED,
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


class ClaudeCodeAdapter(AgentAdapterBase):
    id = "claude-code"
    kind = "builtin"
    default_backend = "anthropic-api"
    supported_backends = ("anthropic-api",)
    output_protocol = "json-final"
    credential_prefixes = ("ANTHROPIC_", "CLAUDE_")
    permission_profiles = ("isolated-edit",)
    #: ``claude -p`` 同样支持 ``--permission-mode`` / ``--allowedTools``（与
    #: codebuddy 2.111.0 同源的开关形态；codebuddy 侧已实测，见
    #: docs/adapter-qualification.md）。此前这里是 ``False`` 且无绑定，于是
    #: 声明了 ``permission_profile`` 却没有审批通道：无人值守的一轮要么在写文件
    #: 时被拒、要么烧完 ``timeout_seconds`` 再以 ``AGENT_FAILED`` 收场，而
    #: ``permission.enforced`` 还写着 false——配置缺陷被伪装成业务失败。
    permission_switch = True
    #: 只绑 ``isolated-edit``：``acceptEdits`` 自动接受写文件，Bash 仍需审批
    #: （在 ``-p`` 下等于被拒），与 codebuddy 的同名档位语义一致。
    #: ``isolated-edit-and-run`` 需要 ``default`` + 白名单的实测结论，本适配器
    #: real-e2e 仍是 NOT_RUN，所以不声明该档位，也不假装它可用。
    permission_bindings = {
        "isolated-edit": ("--permission-mode", "acceptEdits"),
    }

    def probe_argv(self) -> tuple[str, ...]:
        return ("claude", "--version")

    def build_launch(self, context: RunContext) -> LaunchSpec:
        options = validate_typed_options(context.options)
        argv = ["claude", "-p", "--output-format", "json"]
        if context.model or options.get("model"):
            argv += ["--model", str(context.model or options["model"])]
        if options.get("max_turns"):
            argv += ["--max-turns", str(int(options["max_turns"]))]
        if options.get("approved_tools"):
            tools = options["approved_tools"]
            require(
                isinstance(tools, list) and all(isinstance(t, str) and t for t in tools),
                "INPUT_INCOMPLETE",
                "approved_tools must be a string array",
            )
            # ``--allowedTools`` 是 ``nargs="+"``：整串拼成一个 argv 元素会被当作
            # **单个**工具名，结果一个都不匹配，而 ``permission["enforced"]``
            # 仍写着 True（与 codebuddy 的实测结论相同）。必须逐工具传参。
            argv += ["--allowedTools", *tools]
        argv += list(self.permission_argv(context.permission_profile))
        permission = self.permission_state(context.permission_profile)
        # `approved_tools` is the one enforcement path this build really has.
        permission["approved_tools"] = bool(options.get("approved_tools"))
        permission["enforced"] = bool(permission["argv"]) or permission["approved_tools"]
        return LaunchSpec(
            argv=tuple(argv),
            stdin_file=context.prompt_file,
            env_names=context.env_names,
            output_protocol=self.output_protocol,
            max_output_bytes=context.max_output_bytes,
            session_dir=context.session_dir,
            agent_version=context.expected_version,
            extra={"permission": permission},
        )

    def parse_output(self, capture: ProcessCapture) -> AgentResult:
        capture.require_not_truncated()
        objects, _ = json_objects(capture.raw_text)
        require(
            objects and isinstance(objects[-1], dict),
            "AGENT_RESULT_INVALID",
            "No structured Claude Code result",
        )
        data = objects[-1]
        require(
            data.get("type") in ("result", "error") or "is_error" in data,
            "AGENT_RESULT_INVALID",
            "Claude Code output lacks a terminal result object",
        )
        reported_model, reported_models = self.observe_models(objects, data)
        failed = (
            capture.exit_code != 0
            or bool(data.get("is_error"))
            or data.get("type") == "error"
            or str(data.get("subtype", "")).startswith("error")
        )
        if failed:
            summary = summarize(str(data.get("result") or data.get("error") or data))
            return self.result(
                capture,
                classify_failure(summary),
                summary,
                session_id=self._session(data),
                usage=self._usage(data),
                reported_model=reported_model,
                reported_models=reported_models,
            )
        summary = summarize(str(data.get("result") or "completed"))
        return self.result(
            capture,
            OUTCOME_COMPLETED,
            summary,
            session_id=self._session(data),
            usage=self._usage(data),
            reported_model=reported_model,
            reported_models=reported_models,
        )

    @staticmethod
    def _session(data: dict):
        value = data.get("session_id") or data.get("sessionId")
        return value if isinstance(value, str) else None

    @staticmethod
    def _usage(data: dict) -> dict:
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cost": data.get("total_cost_usd"),
        }
