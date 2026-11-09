"""Built-in CodeBuddy adapter.

Command form is an interface sketch from the design (§10); the exact option set
must be confirmed against the pinned CLI version before a profile is marked
VERIFIED. Output protocol: one final JSON result object. It is never treated as
success unless a positive terminal structure is present (design D25).

Facts confirmed against ``codebuddy``/``cbc`` 2.111.0 on macOS (2026-10-28):

* ``-p --output-format stream-json`` emits one JSON object per line **while the
  turn runs**; ``--output-format json`` buffers the whole session and writes it
  only at exit (measured: 0 bytes for the entire 60 s turn, then 26 KB at once).
  Progress observation therefore requires ``stream-json`` — with ``json`` the
  implement phase is a black box between "started" and "finished". The terminal
  ``{"type":"result", ...}`` object is the last line, but it is located by
  structure rather than position;
* ``--permission-mode {acceptEdits,bypassPermissions,default,plan,dontAsk,auto}``
  and ``--allowedTools`` exist, so "isolated-edit" is bindable **without**
  ``-y`` (design Q3 forbids "allow everything" as the answer);
* without any permission switch, an unattended ``-p`` turn has no approval
  channel: ``Write``/``Bash`` are denied and the run ends with a plain-text
  "BLOCKED — 无法写入文件" self-report while ``is_error`` stays ``false``;
* when the CLI cannot find its interactive login state it exits **0** with a
  single plain-text line (``Authentication required. Please use /login ...``),
  so a non-JSON capture must be classified instead of being called invalid.
"""

from __future__ import annotations

import json

from src._agents.base import (
    OUTCOME_COMPLETED,
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
    without_flag,
)
from src._util import require


class CodeBuddyAdapter(AgentAdapterBase):
    id = "codebuddy"
    kind = "builtin"
    default_backend = "codebuddy-cli"
    supported_backends = ("codebuddy-cli",)
    output_protocol = "json-final"
    credential_prefixes = ("CODEBUDDY_", "ANTHROPIC_")
    permission_profiles = ("isolated-edit", "isolated-edit-and-run")
    supports_structured_input = True
    #: 2.111.0 提供 --permission-mode / --allowedTools（可限定，非全放行）。
    permission_switch = True
    #: ``-p`` 模式没有 TTY，任何"需要审批"的工具调用都等于被拒。两个档位：
    #:
    #: ``isolated-edit``      只有文件编辑：``acceptEdits`` 自动接受写文件，Bash
    #:                        等仍需审批 → 实际被拒。适合"只改代码、自测由控制器
    #:                        的 checks 执行"的任务。
    #: ``isolated-edit-and-run`` 编辑 + 命令：worker-prompt 要求"执行批准的自测
    #:                        命令"，这必须有 Bash。实测 ``acceptEdits`` /
    #:                        ``dontAsk`` / ``auto`` 下 ``--allowedTools`` 对 Bash
    #:                        一律无效，只有 ``default`` + 白名单生效。
    #:
    #: 两个档位都不是 ``-y``/``bypassPermissions``：放行集合仍是枚举出来的，
    #: 只是 Bash 本身没有参数级约束（详见 docs/adapter-qualification.md）。
    #: ``--allowedTools`` 是变长参数（``nargs="+"``），会一路吞到下一个以 ``-``
    #: 开头的 token 或行尾。**提示词必须走 stdin**：一旦把提示词作为位置参数写在
    #: 它后面，提示词会被当成工具名吃掉，CLI 空转一回合即退出（实测 2.111.0：
    #: ``tools=[]``、``model="unknown"``、``duration_ms≈26``、``result=""``），而
    #: ``is_error`` 仍是 ``false``、``subtype`` 仍是 ``success``、退出码 0 —— 外观
    #: 上像"被宿主静默拦截"，实际是参数形态错误。逗号或空格分隔本身都不是失效原因
    #: （提示词走 stdin 时两种写法皆可）。手工命令行若必须用位置参数，应写成
    #: ``--allowedTools A B -- "提示词"``。
    permission_bindings = {
        "isolated-edit": ("--permission-mode", "acceptEdits"),
        "isolated-edit-and-run": (
            "--permission-mode",
            "default",
            "--allowedTools",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "Bash",
        ),
    }

    def probe_argv(self) -> tuple[str, ...]:
        return ("codebuddy", "--version")

    def build_launch(self, context: RunContext) -> LaunchSpec:
        options = validate_typed_options(context.options)
        # stream-json：逐行输出，运行期间 host 就能通过 `task logs` 看到进展；
        # `json` 会把整段会话憋到退出时一次性写出（实测 60s 内恒为 0 字节）。
        argv = ["codebuddy", "-p", "--output-format", "stream-json"]
        if context.model or options.get("model"):
            argv += ["--model", str(context.model or options["model"])]
        if options.get("max_turns"):
            argv += ["--max-turns", str(int(options["max_turns"]))]
        bound = list(self.permission_argv(context.permission_profile))
        if options.get("approved_tools"):
            tools = options["approved_tools"]
            require(
                isinstance(tools, list) and all(isinstance(t, str) and t for t in tools),
                "INPUT_INCOMPLETE",
                "approved_tools must be a string array",
            )
            # 显式白名单**覆盖**档位默认值：同一个 flag 传两次时后者静默胜出，
            # 覆盖会让"我显式指定了工具"变成谎话。
            bound = without_flag(bound, "--allowedTools")
            # 每个工具一个 argv：`--allowedTools` 是 nargs="+"，逗号串会被当成
            # 单个工具名从而一个都不匹配（实测：逗号串 → Write/Bash 全拒；
            # 空格分隔 → 两者皆过）。
            argv += ["--allowedTools", *tools]
        argv += bound
        permission = self.permission_state(context.permission_profile)
        # approved_tools 是第二条真实的收窄路径，和 claude-code 保持同一语义。
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
        if not objects:
            # 该 CLI 在"未登录 / 被策略拒绝 / 限流"时会以 exit 0 只吐一行纯文本
            # （2.111.0 实测：``Authentication required. Please use /login ...``）。
            # 退出码在这里不可信：先按关键字分类，绝不报成"输出格式错误"。
            summary = summarize(capture.raw_text) or "CodeBuddy produced no output"
            return self.result(capture, classify_failure(summary), summary)
        require(
            all(isinstance(item, dict) for item in objects) or isinstance(objects[-1], dict),
            "AGENT_RESULT_INVALID",
            "No structured CodeBuddy result",
        )
        # 两种输出形态都要吃：``stream-json`` 是逐行 JSONL（末行是 result），
        # ``json`` 是整段会话数组（末元素通常是 result）。都不保证"最后一个",
        # 所以按结构倒序找终态对象，找不到再退回末元素（仍会被下面的 terminal
        # 检查拦下，而不是当成成功）。
        data = next(
            (
                item
                for item in reversed(objects)
                if isinstance(item, dict) and item.get("type") == "result"
            ),
            objects[-1],
        )
        require(
            isinstance(data, dict),
            "AGENT_RESULT_INVALID",
            "No structured CodeBuddy result",
        )
        # 终态结构不能只看"有没有 type"：会话里的普通 message 也有 type，
        # 那样会把"数组里只有对话、没有终态"误判成可解析。
        terminal = {"subtype", "is_error", "result", "status"} & set(data)
        require(
            data.get("type") == "result" or terminal,
            "AGENT_RESULT_INVALID",
            "CodeBuddy output lacks a recognised terminal structure",
        )
        reported_model, reported_models = self.observe_models(objects, data)
        failed = (
            capture.exit_code != 0
            or bool(data.get("is_error"))
            or data.get("status") in ("failed", "blocked", "error")
            or str(data.get("subtype", "")).startswith("error")
            or data.get("type") == "error"
        )
        if failed:
            summary = (
                summarize(json.dumps(data, ensure_ascii=False)) or "CodeBuddy reported a blocker"
            )
            return self.result(
                capture,
                classify_failure(summary),
                summary,
                session_id=self._session(data),
                usage=self._usage(data),
                reported_model=reported_model,
                reported_models=reported_models,
            )
        if data.get("type") not in (None, "result"):
            return self.result(
                capture,
                OUTCOME_INVALID_OUTPUT,
                "Unexpected terminal type {!r}".format(data.get("type")),
                reported_model=reported_model,
                reported_models=reported_models,
            )
        summary = summarize(str(data.get("result") or data.get("summary") or "completed"))
        return self.result(
            capture,
            OUTCOME_COMPLETED,
            summary,
            session_id=self._session(data),
            usage=self._usage(data),
            input_requests=self._input_requests(data),
            reported_model=reported_model,
            reported_models=reported_models,
        )

    @staticmethod
    def _input_requests(data: dict) -> list:
        """Extract structured input requests from the final JSON (07 §4.0)."""
        value = data.get("input_requests") or data.get("needs_input")
        if not isinstance(value, list):
            return []
        out = []
        for i, item in enumerate(value):
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "question_id": item.get("question_id") or f"q-{i}",
                    "question": item.get("question") or item.get("prompt"),
                    "context": item.get("context"),
                    "options": item.get("options"),
                    "default": item.get("default"),
                }
            )
        return out

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


def legacy_parse_result(raw: str) -> dict:
    """v0.2 dict-shaped parse, kept only for direct backend callers.

    The controller pipeline uses :meth:`CodeBuddyAdapter.parse_output`, which
    produces an ``AgentResult`` with an explicit outcome instead of a bare dict.
    """
    import json as _json

    try:
        data = _json.loads(raw)
    except _json.JSONDecodeError:
        objects = []
        for line in raw.splitlines():
            try:
                objects.append(_json.loads(line))
            except _json.JSONDecodeError:
                pass
        data = objects[-1] if objects else None
    if isinstance(data, list):
        data = data[-1] if data else None
    require(isinstance(data, dict), "AGENT_RESULT_INVALID", "No structured CodeBuddy result")
    require(
        not data.get("is_error")
        and data.get("status") not in ("failed", "blocked")
        and not str(data.get("subtype", "")).startswith("error"),
        "AGENT_FAILED",
        "CodeBuddy reported an error/blocker",
    )
    return data
