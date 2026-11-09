"""AgentAdapter contract shared by built-in and extended adapters.

An adapter answers four questions and nothing else:

``validate_profile``  is this profile structurally usable for ``(adapter, backend)``?
``probe_plan``        what bounded, offline command reports the CLI version?
``build_launch``      which argv/env/mounts run one non-interactive turn?
``parse_output``      what did the fixed-version output protocol actually say?

It never starts a process, takes a lock, publishes, or writes the task database;
those remain controller duties (multi-agent design §9.3). Adapters also cannot
add host mounts: ``LaunchSpec`` only carries container-side writable paths, so a
buggy or hostile adapter cannot expose ``$HOME``, the Docker socket or the
publishing credentials to the worker.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

from src import _quota
from src._util import Error, digest, require

PROTOCOL_REVISION = 1

#: Conservative prompt ceiling from the design (§10.1). Larger specs must be
#: referenced as read-only context files instead of being pushed through argv.
PROMPT_LIMIT_BYTES = 64 * 1024

DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_CEILING = 64 * 1024 * 1024
PROBE_MAX_OUTPUT_BYTES = 16 * 1024

#: Container path the controller mounts the frozen prompt at. The launcher in
#: ``_backend`` redirects this file into the agent's stdin so prompt text never
#: becomes an argv element.
PROMPT_MOUNT = "/zoe-input.txt"

CAPABILITY_IMPLEMENTED = "IMPLEMENTED"
CAPABILITY_UNVERIFIED = "UNVERIFIED"
CAPABILITY_VERIFIED = "VERIFIED"
CAPABILITIES = (CAPABILITY_IMPLEMENTED, CAPABILITY_UNVERIFIED, CAPABILITY_VERIFIED)

OUTCOME_COMPLETED = "completed"
OUTCOME_NEEDS_INPUT = "needs_input"
OUTCOME_PERMISSION_DENIED = "permission_denied"
OUTCOME_AUTH_ERROR = "auth_error"
OUTCOME_RATE_LIMITED = "rate_limited"
#: The account/plan is out of budget. Deliberately **not** folded into
#: ``rate_limited``: backoff repairs a rate limit, while an exhausted quota only
#: waits for a reset or moves to another account. Sharing one outcome made
#: "resume" the documented recovery for both, which spends a real attempt on a
#: pool that is still empty.
OUTCOME_QUOTA_EXHAUSTED = "quota_exhausted"
OUTCOME_FAILED = "failed"
OUTCOME_INVALID_OUTPUT = "invalid_output"
OUTCOMES = (
    OUTCOME_COMPLETED,
    OUTCOME_NEEDS_INPUT,
    OUTCOME_PERMISSION_DENIED,
    OUTCOME_AUTH_ERROR,
    OUTCOME_RATE_LIMITED,
    OUTCOME_QUOTA_EXHAUSTED,
    OUTCOME_FAILED,
    OUTCOME_INVALID_OUTPUT,
)

#: Map a non-completed outcome onto the controller-facing error code (§10.4).
OUTCOME_ERRORS = {
    OUTCOME_NEEDS_INPUT: "SPEC_CLARIFICATION_REQUIRED",
    OUTCOME_PERMISSION_DENIED: "AGENT_PERMISSION_DENIED",
    OUTCOME_AUTH_ERROR: "AGENT_AUTH_REQUIRED",
    OUTCOME_RATE_LIMITED: "AGENT_RATE_LIMITED",
    #: ``QUOTA_EXHAUSTED`` is already registered in ``cli._EXIT_CODES`` (4);
    #: this is the missing producer, not a new code.
    OUTCOME_QUOTA_EXHAUSTED: "QUOTA_EXHAUSTED",
    OUTCOME_FAILED: "AGENT_FAILED",
    OUTCOME_INVALID_OUTPUT: "AGENT_RESULT_INVALID",
}

#: Typed options replace arbitrary ``agent_flags`` (§10.1). Anything else is a
#: controller-reserved parameter and must never be settable from a request.
TYPED_OPTIONS = ("model", "max_turns", "permission_profile", "approved_tools")

#: Canonical profile field set (v1.2 §4.3 drift point 1).
#:
#: This tuple lives here — next to ``validate_profile`` — and is imported by
#: ``_profiles`` so the registration whitelist and the adapter whitelist cannot
#: drift apart. Two hand-maintained copies were the original defect.
PROFILE_FIELDS = frozenset(
    {
        "adapter",
        "backend",
        "image",
        "expected_version",
        "permission_profile",
        "credential_refs",
        "network_profile",
        "compatibility_evidence",
        "model",
        # v1.5.1：CLI 上报的是**供应商侧**模型名，而 `--model` 收到的是本地别名
        # （实测 codebuddy：请求 `BC-DeepSeek-V4-Flash`，上报 `deepseek-v4.1-flash`）。
        # 让 profile 显式声明"这两个名字是同一个模型"，否则 pin_model 恒为 mismatch。
        # 它是观测口径的声明，不落到启动参数，所以不在 EXECUTION_FIELDS 里。
        "model_aliases",
        "max_turns",
        "approved_tools",
        "env_names",
        "max_output_bytes",
        "options",
        "legacy",
        "compatibility_verified",
        "description",
        "require_permission_binding",
        # v1.2 execution-combination fields (§6.1). Declared only; never written
        # back with a default value, so an untouched config keeps its digest.
        "provider",
        "allow_cli_default_model",
        "model_variant",
        "reasoning_effort",
        "endpoint_ref",
        "capabilities",
        "cost_class",
        "latency_class",
        # v1.3 (TGD §5.5, F5): how this profile authenticates. Declared, never
        # written back with a default, so an untouched config keeps its digest.
        "auth_mode",
        "unit_price",
    }
)

#: 占位串不是模型标识符。``unknown`` 实测出现在 codebuddy 的参数形态错误场景里；
#: 把它当作一个模型值会把"未观测"改写成"不匹配"——两者都阻断 pin，但报告口径不同。
MODEL_PLACEHOLDERS = ("unknown", "none", "null")


#: Execution fields that identify *what* actually runs (v1.2 §3.2, §7.1). Each
#: adapter must be able to turn a declared one into an effective launch
#: configuration or refuse (``CAPABILITY_UNSUPPORTED``), never ignore it (§6.3).
EXECUTION_FIELDS = ("provider", "model", "model_variant", "reasoning_effort")

REASONING_EFFORTS = ("low", "medium", "high")
COST_CLASSES = ("low", "standard", "premium")
LATENCY_CLASSES = ("fast", "standard", "slow")
INPUT_MODALITIES = ("text", "image", "audio", "video", "pdf")

#: Declared end-to-end capabilities (§6.2). Every field is a *claim* about the
#: combination (model + CLI), never about the model alone.
CAPABILITY_FIELDS = frozenset(
    {"input_modalities", "context_window", "tool_use", "structured_result", "max_output_tokens"}
)

UNIT_PRICE_FIELDS = frozenset({"input_per_1k", "output_per_1k", "currency"})
RESERVED_PROFILE_KEYS = (
    "agent_flags",
    "output_format",
    "output-format",
    "cwd",
    "resume",
    "continue",
    "add_dir",
    "add-dir",
    "mcp_config",
    "mcp-config",
    "plugin",
    "plugins",
    "sandbox_bypass",
    "skip_git_repo_check",
)


@dataclass(frozen=True)
class LaunchSpec:
    """Controller-validated description of one non-interactive agent launch."""

    argv: tuple[str, ...]
    stdin_file: str | None = None
    env_names: tuple[str, ...] = ()
    writable_dirs: tuple[str, ...] = ()
    output_protocol: str = "json-final"
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    non_interactive: bool = True
    session_dir: str | None = None
    agent_version: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "argv": list(self.argv),
            "stdin_file": self.stdin_file,
            "env_names": list(self.env_names),
            "writable_dirs": list(self.writable_dirs),
            "output_protocol": self.output_protocol,
            "max_output_bytes": self.max_output_bytes,
            "non_interactive": self.non_interactive,
            "session_dir": self.session_dir,
            "agent_version": self.agent_version,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, value: dict) -> LaunchSpec:
        require(
            isinstance(value, dict) and isinstance(value.get("argv"), list),
            "INPUT_INCOMPLETE",
            "Run is missing a valid launch specification",
        )
        return cls(
            argv=tuple(value["argv"]),
            stdin_file=value.get("stdin_file"),
            env_names=tuple(value.get("env_names") or ()),
            writable_dirs=tuple(value.get("writable_dirs") or ()),
            output_protocol=value.get("output_protocol", "json-final"),
            max_output_bytes=int(value.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)),
            non_interactive=bool(value.get("non_interactive", True)),
            session_dir=value.get("session_dir"),
            agent_version=value.get("agent_version"),
            extra=dict(value.get("extra") or {}),
        )


def validate_launch_spec(spec: LaunchSpec) -> None:
    """Backend-side validation. Adapters cannot widen these bounds (§9.2)."""
    require(isinstance(spec.argv, tuple) and spec.argv, "INPUT_INCOMPLETE", "Launch argv required")
    for arg in spec.argv:
        require(isinstance(arg, str) and arg, "INPUT_INCOMPLETE", "Launch argv must be strings")
        require(
            "\x00" not in arg and "\n" not in arg,
            "POLICY_DENIED",
            "Launch argv contains a control character",
        )
    require(spec.non_interactive is True, "POLICY_DENIED", "Interactive launches are not supported")
    require(
        0 < spec.max_output_bytes <= MAX_OUTPUT_CEILING,
        "INPUT_INCOMPLETE",
        "max_output_bytes outside supported range",
    )
    for name in spec.env_names:
        require(
            bool(re.fullmatch(r"[A-Z][A-Z0-9_]*", name)),
            "INPUT_INCOMPLETE",
            "Invalid environment name",
        )
    for directory in spec.writable_dirs:
        path = PurePosixPath(directory)
        require(
            path.is_absolute() and ".." not in path.parts,
            "POLICY_DENIED",
            "Writable path must be absolute and contained",
        )
        require(
            not str(path).startswith("/work"),
            "POLICY_DENIED",
            "The workspace must not be adapter-writable",
        )
        require(str(path) != "/", "POLICY_DENIED", "Root filesystem must stay read-only")
    require(
        spec.stdin_file is None or PurePosixPath(spec.stdin_file).is_absolute(),
        "INPUT_INCOMPLETE",
        "stdin_file must be an absolute host path",
    )
    if spec.session_dir is not None:
        # A session directory becomes a host bind mount, so an adapter must not be
        # able to point it at arbitrary host paths. The backend additionally
        # requires it to live inside this run's own directory.
        path = PurePosixPath(spec.session_dir)
        require(
            path.is_absolute() and ".." not in path.parts and str(path) != "/",
            "POLICY_DENIED",
            "session_dir must be an absolute, contained host path",
        )


@dataclass(frozen=True)
class RunContext:
    """Controller-constructed run facts. An adapter may read but never invent them."""

    task_id: str
    run_id: str
    adapter_id: str
    adapter_backend: str
    workspace: str
    prompt_file: str
    prompt_hash: str
    profile_id: str
    profile_hash: str
    spec_revision: int
    spec_hash: str
    image_digest: str | None = None
    expected_version: str | None = None
    model: str | None = None
    #: v1.2 execution-combination facts. They are controller-supplied (frozen in
    #: the profile and the route decision), never invented by an adapter.
    provider: str | None = None
    model_variant: str | None = None
    reasoning_effort: str | None = None
    endpoint: dict | None = None
    permission_profile: str | None = None
    env_names: tuple[str, ...] = ()
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    handoff_ref: str | None = None
    remaining_attempts: int = 0
    #: Controller-owned, per-``(task, run, adapter, backend)`` diagnostic directory.
    #: Adapters may hand it to ``LaunchSpec.session_dir``; they never choose it.
    session_dir: str | None = None
    options: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "adapter_id": self.adapter_id,
            "adapter_backend": self.adapter_backend,
            "workspace": self.workspace,
            "prompt_file": self.prompt_file,
            "prompt_hash": self.prompt_hash,
            "profile_id": self.profile_id,
            "profile_hash": self.profile_hash,
            "spec_revision": self.spec_revision,
            "spec_hash": self.spec_hash,
            "image_digest": self.image_digest,
            "expected_version": self.expected_version,
            "model": self.model,
            "provider": self.provider,
            "model_variant": self.model_variant,
            "reasoning_effort": self.reasoning_effort,
            "endpoint": dict(self.endpoint) if self.endpoint else None,
            "permission_profile": self.permission_profile,
            "env_names": list(self.env_names),
            "max_output_bytes": self.max_output_bytes,
            "handoff_ref": self.handoff_ref,
            "remaining_attempts": self.remaining_attempts,
            "session_dir": self.session_dir,
            "options": dict(self.options),
        }


@dataclass(frozen=True)
class ProcessCapture:
    """Bounded, decoded output of one launch plus its backend metadata."""

    adapter_id: str
    adapter_backend: str
    exit_code: int
    raw_text: str
    truncated: bool = False
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    expected_version: str | None = None
    log_name: str = "agent"

    def raw_sha256(self) -> str:
        return digest(self.raw_text.encode("utf-8", "replace"))

    def require_not_truncated(self) -> None:
        require(
            not self.truncated,
            "OUTPUT_LIMIT_EXCEEDED",
            f"Agent output exceeded {self.max_output_bytes} bytes",
        )

    @staticmethod
    def capture_present(run_dir) -> bool:
        return (Path(run_dir) / "capture.json").is_file()

    @classmethod
    def from_run_dir(
        cls,
        run_dir,
        log_name: str,
        adapter_id: str,
        adapter_backend: str,
        expected_version: str | None = None,
    ) -> ProcessCapture:
        """Rebuild the capture from the backend's bounded log plus its metadata."""
        run_dir = Path(run_dir)
        meta: dict = {}
        meta_path = run_dir / "capture.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text())
            except json.JSONDecodeError:
                meta = {}
        limit = int(meta.get("max_output_bytes") or DEFAULT_MAX_OUTPUT_BYTES)
        text, truncated_file = bounded_read(run_dir / (log_name + ".log"), limit)
        return cls(
            adapter_id=adapter_id,
            adapter_backend=adapter_backend,
            exit_code=int(meta.get("exit_code") or 0),
            raw_text=text,
            truncated=bool(meta.get("truncated")) or truncated_file,
            max_output_bytes=limit,
            expected_version=expected_version or meta.get("expected_version"),
            log_name=log_name,
        )


@dataclass
class AgentResult:
    """Structured result. ``completed`` only ever advances to verification."""

    agent_id: str
    agent_version: str
    outcome: str
    exit_code: int
    session_id: str | None = None
    summary: str = ""
    usage: dict = field(
        default_factory=lambda: {"input_tokens": None, "output_tokens": None, "cost": None}
    )
    raw_log_sha256: str = ""
    protocol_revision: int = PROTOCOL_REVISION
    #: Structured input requests the agent reported. Only populated when the
    #: adapter advertises ``supports_structured_input`` (design 07 §4.0).
    input_requests: list = field(default_factory=list)
    #: What the *tool reported* about the model it used (v1.2 §9.1). ``None``
    #: means "the tool did not say", which is ``unobserved`` — never "matched".
    reported_model: str | None = None
    #: Every distinct ``{model, scope}`` observation, for the ``mixed`` verdict.
    reported_models: list = field(default_factory=list)

    def __post_init__(self) -> None:
        require(self.outcome in OUTCOMES, "AGENT_RESULT_INVALID", "Unknown agent outcome")

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
            "outcome": self.outcome,
            "exit_code": self.exit_code,
            "session_id": self.session_id,
            "summary": self.summary,
            "usage": dict(self.usage),
            "raw_log_sha256": self.raw_log_sha256,
            "protocol_revision": self.protocol_revision,
            "input_requests": list(self.input_requests),
            # Additive optional fields: `required` and `protocol_revision` are
            # unchanged, so a v1.1 reader keeps parsing this document.
            "reported_model": self.reported_model,
            "reported_models": [dict(item) for item in self.reported_models],
        }

    @classmethod
    def from_dict(cls, value: dict) -> AgentResult:
        return cls(
            agent_id=value.get("agent_id", ""),
            agent_version=value.get("agent_version", ""),
            outcome=value.get("outcome", OUTCOME_INVALID_OUTPUT),
            exit_code=int(value.get("exit_code", 1)),
            session_id=value.get("session_id"),
            summary=value.get("summary", ""),
            usage=dict(value.get("usage") or {}),
            raw_log_sha256=value.get("raw_log_sha256", ""),
            protocol_revision=int(value.get("protocol_revision", PROTOCOL_REVISION)),
            input_requests=list(value.get("input_requests") or []),
            reported_model=value.get("reported_model"),
            reported_models=list(value.get("reported_models") or []),
        )

    def require_completed(self) -> None:
        if self.outcome == OUTCOME_COMPLETED:
            return
        code = OUTCOME_ERRORS.get(self.outcome, "AGENT_FAILED")
        details = None
        if code == "AGENT_FAILED":
            # 兜底码上的**未确认线索**（v1.6）。刻意不新增错误码：`MODEL_UNAVAILABLE`
            # 的可靠识别需要适配器输出 marker（契约变更），靠文本猜出来直接改码会把
            # "无法确认"伪造成"已归因"，而 `_routing.attribution` 正是按这个区分在统计。
            # 线索放在 block_details.hint 里：宿主可以按它分流，结论仍是"未归因"。
            hint = failure_hint(self.summary)
            if hint:
                details = {"hint": hint, "outcome": self.outcome}
        raise Error(code, (self.summary or "Agent did not complete")[:4000], details=details)


@runtime_checkable
class AgentAdapter(Protocol):
    """Structural contract from multi-agent design §9.2."""

    id: str
    kind: str
    protocol_revision: int
    default_backend: str
    supported_backends: tuple[str, ...]
    output_protocol: str
    supports_structured_input: bool

    def validate_profile(self, profile: dict) -> None: ...

    def probe_plan(self, profile: dict) -> LaunchSpec: ...

    def build_launch(self, context: RunContext) -> LaunchSpec: ...

    def parse_output(self, capture: ProcessCapture) -> AgentResult: ...


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def json_objects(text: str) -> tuple[list, int]:
    """Return ``(objects, unparsable_lines)`` for a single JSON document or a stream."""
    stripped = text.strip()
    if not stripped:
        return [], 0
    try:
        whole = json.loads(stripped)
    except json.JSONDecodeError:
        whole = None
    if whole is not None:
        return (whole if isinstance(whole, list) else [whole]), 0
    objects, bad = [], 0
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            objects.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    return objects, bad


def classify_failure(text: str) -> str:
    """Map free-text failure evidence onto the outcome enum (never success).

    Quota is checked **before** rate limiting: the real banner
    (``429 使用量已超出频率限制, 将在 … 重置``) contains both signals, and the
    order decides the recovery advice. Classifying it as a rate limit tells the
    host to back off — which spends the next attempt on the same empty pool.
    """
    lowered = (text or "").lower()
    if _quota.looks_exhausted(text):
        return OUTCOME_QUOTA_EXHAUSTED
    if any(token in lowered for token in ("rate limit", "rate_limit", "429", "overloaded")):
        return OUTCOME_RATE_LIMITED
    if any(
        token in lowered
        for token in (
            "unauthorized",
            "invalid api key",
            "authentication",
            "not logged in",
            "login required",
            "401",
            "auth error",
        )
    ):
        return OUTCOME_AUTH_ERROR
    if any(
        token in lowered
        for token in (
            "permission denied",
            "not permitted",
            "approval required",
            "tool not allowed",
            "forbidden",
            "403",
        )
    ):
        return OUTCOME_PERMISSION_DENIED
    if any(
        token in lowered for token in ("clarification", "need_spec", "needs_input", "ambiguous")
    ):
        return OUTCOME_NEEDS_INPUT
    return OUTCOME_FAILED


#: 兜底码 ``AGENT_FAILED`` 上的线索词表（v1.6）。**不是**错误码表：
#: 命中只写进 ``block_details.hint``，``block_code`` 仍是 ``AGENT_FAILED``，
#: 归因仍是"未确认"。这样监控可以按线索分流，而统计不会被猜测污染。
_HINT_TOKENS = {
    "model_not_found": (
        "unknown model",
        "model not found",
        "model_not_found",
        "invalid model",
        "model is not available",
        "no such model",
        "unsupported model",
        "模型不存在",
        "模型不可用",
        "不支持的模型",
    ),
    "gateway_error": (
        "bad gateway",
        "gateway timeout",
        "502",
        "503",
        "504",
        "upstream",
        "socket hang up",
        "econnreset",
        "connection reset",
    ),
}


def failure_hint(text: str) -> str | None:
    """Best-effort *hint* for an unattributed failure, or ``None``.

    Only consulted when the outcome already collapsed to ``AGENT_FAILED``. The
    result is advice for the next step, never an attribution: a CLI that rejects a
    model name and a CLI that is momentarily unreachable still share one code.
    """
    lowered = (text or "").lower()
    for hint, tokens in _HINT_TOKENS.items():
        if any(token in lowered for token in tokens):
            return hint
    return None


def summarize(text: str, limit: int = 400) -> str:
    return " ".join((text or "").split())[:limit]


def bounded_read(path: Path, limit: int) -> tuple[str, bool]:
    """Read at most ``limit`` bytes; the boolean reports truncation."""
    path = Path(path)
    if not path.exists():
        return "", False
    data = path.read_bytes()
    if len(data) > limit:
        return data[:limit].decode("utf-8", "replace"), True
    return data.decode("utf-8", "replace"), False


def without_flag(argv: list[str], flag: str) -> list[str]:
    """Drop ``flag`` and the variadic values that follow it.

    ``--allowedTools`` is ``nargs="+"`` on both the codebuddy and claude CLIs: it
    consumes tokens until the next one that starts with ``-``, so a naive remove
    would leave the values behind and turn them into positional arguments the CLI
    rejects.
    """
    out: list[str] = []
    dropping = False
    for token in argv:
        if dropping and not token.startswith("-"):
            continue
        dropping = False
        if token == flag:
            dropping = True
            continue
        out.append(token)
    return out


class AgentAdapterBase:
    """Default plumbing shared by the built-in and extended adapters."""

    id = ""
    kind = "builtin"
    protocol_revision = PROTOCOL_REVISION
    default_backend = ""
    supported_backends: tuple[str, ...] = ()
    output_protocol = "json-final"
    credential_prefixes: tuple[str, ...] = ()
    permission_profiles: tuple[str, ...] = ()
    #: Whether this adapter can report structured ``needs_input`` requests through
    #: its normalized result (design 07 §4.0). Default ``False``: an adapter that
    #: cannot do this must not have the Q&A flow enabled for its profiles.
    supports_structured_input: bool = False
    #: ``permission_profile`` name -> the argv that actually enforces it. An empty
    #: map means this build validates the name but cannot prove enforcement: the
    #: adapter reports ``enforced=False``, and a profile may demand a real binding
    #: with ``require_permission_binding``.
    permission_bindings: dict[str, tuple[str, ...]] = {}
    #: 该 CLI 是否存在"非交互、且不是全放行"的权限开关（设计 §10.2 Q3）。
    #:
    #: ``True`` 表示"它确实有这个开关"。此时空绑定不再是诚实记录，而是缺陷：
    #: 无人值守启动会缺审批通道，写入类工具必被拒，任务不可能完成——所以
    #: ``_profiles.require_runnable`` 对它 fail closed（PERMISSION_PROFILE_UNBOUND）。
    #: ``False`` 表示"这个 CLI 没有该开关"（例如 opencode），``enforced=False``
    #: 仍是唯一诚实的说法，不构成拒绝。
    permission_switch: bool = False
    #: v1.2 §6.3：本适配器能把哪些执行字段落实到**受支持的有效配置**。
    #: 未列出的字段一旦被声明就 ``CAPABILITY_UNSUPPORTED``（拒绝，不静默忽略）。
    #: 所有适配器都通过 ``--model`` 映射 ``model``；其余字段按 CLI 真实形态声明。
    execution_fields: tuple[str, ...] = ("model",)
    #: v1.2 §9.5：本适配器能否把 ``endpoint_ref`` 落到启动配置（必须显式禁止回退）。
    #: ``False`` 时 ``data_residency: required`` 判为 ``ENDPOINT_UNVERIFIED``。
    supports_endpoint_ref: bool = False
    version_pattern = r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?)"
    #: Extra facts that must be frozen before a profile may run. Empty means the
    #: command form is part of the shipped adapter contract.
    facts_status = "VERIFIED_FORM"

    def backend_of(self, profile: dict) -> str:
        return profile.get("backend") or self.default_backend

    def validate_profile(self, profile: dict) -> None:
        require(isinstance(profile, dict), "INPUT_INCOMPLETE", "Agent profile must be an object")
        backend = self.backend_of(profile)
        require(
            backend in self.supported_backends,
            "ADAPTER_BACKEND_MISMATCH",
            f"{self.id} does not support backend {backend!r}",
        )
        for key in RESERVED_PROFILE_KEYS:
            require(
                key not in profile,
                "POLICY_DENIED",
                f"{key} is controller-reserved; use typed options instead of {key!r}",
            )
        unknown = set(profile) - PROFILE_FIELDS
        require(
            not unknown, "INPUT_INCOMPLETE", "Unknown profile fields: " + ", ".join(sorted(unknown))
        )
        if self.permission_profiles:
            require(
                profile.get("permission_profile") in self.permission_profiles,
                "POLICY_DENIED",
                "{} requires one of the approved permission profiles: {}".format(
                    self.id, ", ".join(self.permission_profiles)
                ),
            )
        expected = profile.get("expected_version")
        require(
            expected is None or (isinstance(expected, str) and expected),
            "INPUT_INCOMPLETE",
            "expected_version must be a non-empty string when declared",
        )
        if profile.get("model") is not None:
            require(
                isinstance(profile["model"], str) and profile["model"],
                "INPUT_INCOMPLETE",
                "model must be a string",
            )
        self._validate_execution_fields(profile)
        if profile.get("max_turns") is not None:
            require(
                isinstance(profile["max_turns"], int) and 0 < profile["max_turns"] <= 200,
                "INPUT_INCOMPLETE",
                "max_turns must be 1..200",
            )
        refs = profile.get("credential_refs") or []
        require(
            isinstance(refs, list) and all(isinstance(x, str) for x in refs),
            "INPUT_INCOMPLETE",
            "credential_refs must be a string array",
        )
        require(
            isinstance(profile.get("require_permission_binding", False), bool),
            "INPUT_INCOMPLETE",
            "require_permission_binding must be a boolean",
        )
        for name in self.env_names(profile):
            require(
                name.startswith(self.credential_prefixes),
                "POLICY_DENIED",
                f"{self.id} may only receive credentials with its own prefixes",
            )

    # -- execution combination (v1.2) --------------------------------------- #
    def _validate_execution_fields(self, profile: dict) -> None:
        """Type/enum checks for the declared execution fields (§6.1, §6.2)."""
        for key in ("provider", "model_variant", "endpoint_ref"):
            value = profile.get(key)
            require(
                value is None or (isinstance(value, str) and value),
                "INPUT_INCOMPLETE",
                f"{key} must be a non-empty string when declared",
            )
        effort = profile.get("reasoning_effort")
        require(
            effort is None or effort in REASONING_EFFORTS,
            "INPUT_INCOMPLETE",
            "reasoning_effort must be one of " + ", ".join(REASONING_EFFORTS),
        )
        require(
            isinstance(profile.get("allow_cli_default_model", False), bool),
            "INPUT_INCOMPLETE",
            "allow_cli_default_model must be a boolean",
        )
        aliases = profile.get("model_aliases")
        if aliases is not None:
            require(
                isinstance(aliases, list)
                and bool(aliases)
                and all(isinstance(item, str) and item.strip() for item in aliases),
                "INPUT_INCOMPLETE",
                "model_aliases must be a non-empty array of non-empty strings",
            )
            require(
                profile.get("model") is not None,
                "INPUT_INCOMPLETE",
                "model_aliases requires model: an alias only means something next to a "
                "requested model id",
            )
            require(
                len(aliases) == len({item.strip() for item in aliases}),
                "INPUT_INCOMPLETE",
                "model_aliases must not repeat an entry",
            )
        cost_class = profile.get("cost_class")
        require(
            cost_class is None or cost_class in COST_CLASSES,
            "INPUT_INCOMPLETE",
            "cost_class must be one of " + ", ".join(COST_CLASSES),
        )
        latency_class = profile.get("latency_class")
        require(
            latency_class is None or latency_class in LATENCY_CLASSES,
            "INPUT_INCOMPLETE",
            "latency_class must be one of " + ", ".join(LATENCY_CLASSES),
        )
        capabilities = profile.get("capabilities")
        if capabilities is not None:
            require(
                isinstance(capabilities, dict),
                "INPUT_INCOMPLETE",
                "capabilities must be an object",
            )
            unknown = set(capabilities) - CAPABILITY_FIELDS
            require(
                not unknown,
                "INPUT_INCOMPLETE",
                "Unknown capability fields: " + ", ".join(sorted(unknown)),
            )
            modalities = capabilities.get("input_modalities")
            if modalities is not None:
                require(
                    isinstance(modalities, list)
                    and modalities
                    and all(m in INPUT_MODALITIES for m in modalities),
                    "INPUT_INCOMPLETE",
                    "input_modalities must be a non-empty array of " + ", ".join(INPUT_MODALITIES),
                )
            for key in ("context_window", "max_output_tokens"):
                value = capabilities.get(key)
                require(
                    value is None
                    or (isinstance(value, int) and not isinstance(value, bool) and value > 0),
                    "INPUT_INCOMPLETE",
                    f"capabilities.{key} must be a positive integer",
                )
            for key in ("tool_use", "structured_result"):
                require(
                    capabilities.get(key) is None or isinstance(capabilities[key], bool),
                    "INPUT_INCOMPLETE",
                    f"capabilities.{key} must be a boolean",
                )
        unit_price = profile.get("unit_price")
        if unit_price is not None:
            require(
                isinstance(unit_price, dict) and not (set(unit_price) - UNIT_PRICE_FIELDS),
                "INPUT_INCOMPLETE",
                "unit_price accepts only " + ", ".join(sorted(UNIT_PRICE_FIELDS)),
            )
            for key in ("input_per_1k", "output_per_1k"):
                value = unit_price.get(key)
                require(
                    value is None
                    or (
                        isinstance(value, int | float)
                        and not isinstance(value, bool)
                        and value >= 0
                    ),
                    "INPUT_INCOMPLETE",
                    f"unit_price.{key} must be a non-negative number",
                )
            require(
                isinstance(unit_price.get("currency"), str) and unit_price["currency"],
                "INPUT_INCOMPLETE",
                "unit_price.currency is required",
            )

    def execution_mapping(self, profile: dict) -> dict:
        """Decide whether every declared execution field reaches a real config.

        v1.2 §6.3: the question is *not* "is there a dedicated flag" but "can this
        adapter turn the declared value into an effective, supported launch
        configuration". Refusing is mandatory; silently ignoring a declared field
        would make "provider is configured" and "provider is not passed"
        indistinguishable. This mirrors the existing honesty for
        ``permission_profile`` (``enforced=False`` instead of pretending).
        """
        declared = [f for f in EXECUTION_FIELDS if profile.get(f) is not None]
        refused = sorted(set(declared) - set(self.execution_fields))
        require(
            not refused,
            "CAPABILITY_UNSUPPORTED",
            "{} cannot map {} onto a supported launch configuration".format(
                self.id, ", ".join(refused)
            ),
        )
        return {
            "adapter_id": self.id,
            "declared": declared,
            "mapped": {field: profile[field] for field in declared},
            # Endpoint mapping is decided separately: `required` residency needs a
            # pre-launch verification, not a post-hoc comparison (§9.5).
            "endpoint_mapped": bool(profile.get("endpoint_ref")) and self.supports_endpoint_ref,
        }

    # -- permissions ------------------------------------------------------- #
    def permission_argv(self, profile_name: str | None) -> tuple[str, ...]:
        """Argv that enforces ``profile_name``, or ``()`` when it is unbound."""
        if not profile_name:
            return ()
        return tuple(self.permission_bindings.get(profile_name, ()))

    def permission_state(self, profile_name: str | None) -> dict:
        """Auditable view: does the named permission profile reach the command line?

        ``enforced=False`` is deliberate honesty, not a bug workaround: a named
        profile with no frozen argv would otherwise suggest that tool approvals
        are configured when the agent may still block on them (design §6, §10.2).
        """
        argv = self.permission_argv(profile_name)
        return {"profile": profile_name, "argv": list(argv), "enforced": bool(argv)}

    # -- credentials ------------------------------------------------------- #
    def env_names(self, profile: dict) -> tuple[str, ...]:
        """Resolve credential references to environment names for this adapter only."""
        names = list(profile.get("env_names") or [])
        for ref in profile.get("credential_refs") or []:
            names.append(CREDENTIAL_ALIASES.get(ref, ref))
        resolved = tuple(dict.fromkeys(names))
        for name in resolved:
            require(
                bool(re.fullmatch(r"[A-Z][A-Z0-9_]*", name)),
                "INPUT_INCOMPLETE",
                f"Invalid credential reference {name!r}",
            )
        return resolved

    # -- probing ----------------------------------------------------------- #
    def probe_argv(self) -> tuple[str, ...]:
        raise Error("CAPABILITY_UNSUPPORTED", f"{self.id} has no probe command")

    def probe_plan(self, profile: dict) -> LaunchSpec:
        self.validate_profile(profile)
        return LaunchSpec(
            argv=self.probe_argv(),
            output_protocol="text",
            max_output_bytes=PROBE_MAX_OUTPUT_BYTES,
        )

    def parse_probe(self, text: str) -> str | None:
        import re as _re

        match = _re.search(self.version_pattern, text or "")
        return match.group(1) if match else None

    # -- launch ------------------------------------------------------------ #
    def build_launch(self, context: RunContext) -> LaunchSpec:
        raise NotImplementedError

    def parse_output(self, capture: ProcessCapture) -> AgentResult:
        raise NotImplementedError

    # -- model observation (v1.2 §9.2) ------------------------------------- #
    @staticmethod
    def model_of(obj) -> str | None:
        """The model identifier a stream object claims, if it claims one.

        Only structural places are read: ``model`` / ``model_id`` / ``modelId``,
        the same keys inside a nested ``message``, and the provider block
        (``providerData`` / ``provider_data``) that carries the upstream id.
        Nothing is inferred from prose.

        为什么要读 ``providerData``：实测 codebuddy 2.111.0 的
        ``--output-format json`` 里顶层没有 ``model``，供应商模型只出现在
        ``providerData.model``；不读它就只剩 ``unobserved``，读者会以为"工具
        什么都没说"，而实际上工具说了，只是放在另一个结构位置。
        """
        if not isinstance(obj, dict):
            return None
        candidates = [obj]
        for key in ("message", "providerData", "provider_data"):
            nested = obj.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
        message = obj.get("message")
        if isinstance(message, dict):
            for key in ("providerData", "provider_data"):
                nested = message.get(key)
                if isinstance(nested, dict):
                    candidates.append(nested)
        for candidate in candidates:
            for key in ("model", "model_id", "modelId"):
                value = candidate.get(key)
                if isinstance(value, str):
                    value = value.strip()
                    if value and value.lower() not in MODEL_PLACEHOLDERS:
                        return value
        return None

    @staticmethod
    def observation_scope(obj, terminal) -> str:
        """Where a claim about the model came from (§9.1).

        ``request_echo`` 是 ``system``/``init`` 那一条：它回显的是**请求**的
        模型（实测 ``model = "custom-local:BC-DeepSeek-V4-Flash"``），不是运行时
        实际使用的模型。把它算作 ``main_call`` 会让一次正常运行看起来像"两个
        模型混用"（``mixed``），于是 pin_model 在没有证据的地方阻断。因此它单独
        一个作用域：保留在 ``reported_models`` 里可审计，但不参与身份判定。
        """
        if obj is terminal:
            return "final_result"
        if isinstance(obj, dict) and obj.get("type") == "system" and obj.get("subtype") == "init":
            return "request_echo"
        return "main_call"

    def observe_models(self, objects: list, terminal) -> tuple[str | None, list]:
        """Return ``(reported_model, reported_models)`` from a parsed stream.

        ``reported_model`` is the **terminal** observation (what the final result
        says); ``reported_models`` lists every distinct ``{model, scope}`` pair so
        a mixed stream can be reported as ``mixed`` instead of being flattened to
        one value (§9.1). An empty result is ``unobserved`` — never "matched".
        """
        entries: list[dict] = []
        seen = set()
        for obj in objects:
            model = self.model_of(obj)
            if not model:
                continue
            scope = self.observation_scope(obj, terminal)
            key = (model, scope)
            if key in seen:
                continue
            seen.add(key)
            entries.append({"model": model, "scope": scope})
        # 终态观测只从"运行事实"里取：``request_echo`` 是请求回显，不能当结果。
        usage = [e for e in entries if e["scope"] != "request_echo"]
        final = next(
            (e["model"] for e in usage if e["scope"] == "final_result"),
            usage[-1]["model"] if usage else None,
        )
        return final, entries

    # -- shared result plumbing -------------------------------------------- #
    def result(
        self,
        capture: ProcessCapture,
        outcome: str,
        summary: str,
        session_id: str | None = None,
        usage: dict | None = None,
        input_requests: list | None = None,
        reported_model: str | None = None,
        reported_models: list | None = None,
    ) -> AgentResult:
        return AgentResult(
            agent_id=self.id,
            agent_version=capture.expected_version or "",
            outcome=outcome,
            exit_code=capture.exit_code,
            session_id=session_id,
            summary=summary,
            usage=usage or {"input_tokens": None, "output_tokens": None, "cost": None},
            raw_log_sha256=capture.raw_sha256(),
            input_requests=input_requests or [],
            reported_model=reported_model,
            reported_models=reported_models or [],
        )


#: Logical credential names -> environment variable names. Only names on this
#: list (or an explicit uppercase env name with an approved prefix) resolve.
CREDENTIAL_ALIASES = {
    "model-codebuddy": "CODEBUDDY_API_KEY",
    "model-anthropic": "ANTHROPIC_API_KEY",
    "model-openai": "OPENAI_API_KEY",
    # Adapter-owned namespace. It says nothing about how codex authenticates.
    "model-codex": "CODEX_API_KEY",
}


def validate_typed_options(profile: dict) -> dict:
    """Return the typed options a profile may pass; reject anything else."""
    options = {}
    for key in TYPED_OPTIONS:
        if key in profile and profile[key] is not None:
            options[key] = profile[key]
    for key in profile.get("options") or {}:
        require(key in TYPED_OPTIONS, "POLICY_DENIED", f"Unknown typed option {key!r}")
        options[key] = profile["options"][key]
    return options
