"""任务感知的执行组合路由（v1.2）。

三层事实严格分账（设计 §3.3、§8）：

* **L1 硬能力**：profile 声明的端到端能力，机械校验，只能排除；
* **L2 集成事实**：本机能否跑起来（版本/参数/输出），只能排除；
* **L3 任务表现**：历史统计，v1.2 **只记录，不参与选择**。

本模块提供三件事：

1. ``routing`` / ``endpoints`` 段的规范化与校验（§6.1、§6.4、§16.1）；
2. 快照与决策分离：:func:`collect_snapshot` 有副作用（doctor/解析/有界版本探测），
   :func:`decide` 是纯函数，只读 dict，可表驱动单测（§11.3）；
3. 组合标识 :func:`combination_id`、模型观测 :func:`model_identity`、
   失败归因 :func:`attribution`、费用口径与护栏（§7、§9、§10、§12.7）。

`routing` 段是否出现决定行为：顶层没有该键时本模块不产生任何默认值，
``policy_digest`` 与 v1.0.0 逐字节一致（§16.1）。
"""

from __future__ import annotations

import time
from pathlib import Path

from src._agents import base as agent_base
from src._agents import registry
from src._util import Error, atomic, digest, read, require, safe_id

# --------------------------------------------------------------------------- #
# 词表
# --------------------------------------------------------------------------- #

ROUTING_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "allowed_profiles",
        "rules",
        "fallback",
        "max_assignments",
        "cost_ceiling",
        "spend_guard",
        "use_performance",
    }
)
SPEND_GUARD_FIELDS = frozenset({"max_cost_per_task", "max_cost_per_day", "currency", "on_exceed"})
ENDPOINT_FIELDS = frozenset({"class", "attestation"})

MODES = ("explicit", "auto")
FALLBACKS = ("ask", "default_profile")
COST_CLASSES = ("low", "standard", "premium")
COST_ORDER = {"low": 0, "standard": 1, "premium": 2}
LATENCY_CLASSES = ("fast", "standard", "slow")
ENDPOINT_CLASSES = ("cloud-approved", "private", "local")
#: v1.2 只有 ``config-declared``：管理员声明，不是技术证明（§6.4、§17 E8）。
ATTESTATIONS = ("config-declared",)

TASK_TYPES = ("bugfix", "feature", "refactor", "test", "docs", "chore", "review", "unknown")
RULE_TASK_TYPES = TASK_TYPES + ("*",)

CAPABILITY_ORDER = {"IMPLEMENTED": 0, "UNVERIFIED": 1, "VERIFIED": 2}
#: ``alias_confirmed``：工具上报的值不逐字等于请求值，但每一个都落在 profile
#: 显式声明的 ``model_aliases`` 里（同一个模型的两种叫法）。它与 ``reported_match``
#: 是两个事实，不能合并成一个字符串——报告要能区分"逐字相等"和"按声明等价"。
MODEL_IDENTITIES = (
    "reported_match",
    "alias_confirmed",
    "reported_mismatch",
    "mixed",
    "unobserved",
)
#: ``request_echo``：``system``/``init`` 回显的**请求**模型，不参与身份判定。
OBSERVATION_SCOPES = ("final_result", "main_call", "request_echo", "mixed", "none")
RESIDENCY = ("preferred", "required")

FAILURE_CLASSES = (
    "environment",
    "infrastructure",
    "auth_permission",
    "spec",
    "executor",
    "verification",
)

#: 硬条件要求项（v1.1 全集 + v1.2 §8.1/§14.2 新增）。未知键 → INPUT_INCOMPLETE。
REQUIREMENT_FIELDS = frozenset(
    {
        "input_modalities",
        "context_budget",
        "needs_tool_use",
        "structured_result",
        "non_interactive",
        "data_residency",
        "session_resume",
        "pin_model",
        "min_capability",
        "structured_input",
        "permission_profile_in",
    }
)

#: 错误码 → 失败类别（§10.2）。不在表里的码一律 ``executor``：兜底必须保守，
#: 但绝不允许把环境/权限失败算进代码质量（C4）。
CODE_FAILURE_CLASS = {
    "ENVIRONMENT_ERROR": "environment",
    "SLOT_BUSY": "environment",
    "PROCESS_UNKNOWN": "environment",
    "INPUT_INCOMPLETE": "spec",
    "SPEC_CLARIFICATION_REQUIRED": "spec",
    "POLICY_DENIED": "spec",
    "POLICY_CHANGED": "spec",
    "RUN_TIMEOUT": "infrastructure",
    "EVIDENCE_INVALID": "infrastructure",
    "EVIDENCE_MISSING": "infrastructure",
    "WORKSPACE_CHANGED": "infrastructure",
    # 基线漂移是仓库历史被改写，不是这次改动的质量问题（v1.6）。
    "BASE_DRIFT": "infrastructure",
    "AGENT_AUTH_REQUIRED": "auth_permission",
    "AGENT_PERMISSION_DENIED": "auth_permission",
    "PERMISSION_PROFILE_UNBOUND": "auth_permission",
    "AGENT_RATE_LIMITED": "auth_permission",
    # 额度耗尽是账号/环境状态，不是权限也不是代码质量。归到 `environment`
    # 有两个直接后果：不进能力比较（本该如此），以及不会因为"auth 类失败"
    # 被导向 reassign——换 profile 修不好一个空了的额度池。
    "QUOTA_EXHAUSTED": "environment",
    "MODEL_MISMATCH": "executor",
    "AGENT_RESULT_INVALID": "executor",
    "AGENT_FAILED": "executor",
    "OUTPUT_LIMIT_EXCEEDED": "executor",
    "VALIDATION_FAILED": "verification",
    "CHECK_FAILED": "verification",
}

#: 不进质量指标的两类（环境/基础设施）；auth/spec 同样不计，但另计分项。
EXCLUDED_CLASSES = ("environment", "infrastructure", "auth_permission", "spec")


# --------------------------------------------------------------------------- #
# 规范化
# --------------------------------------------------------------------------- #


def _is_number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def normalize_endpoints(c: dict) -> None:
    """Validate the project-level ``endpoints`` section in place (§6.4).

    只做校验与规范化，不写回默认值；未声明时不产生该键（§16.1）。
    记录里只出现端点名字，不出现 base_url/密钥/账号。
    """
    endpoints = c.get("endpoints")
    if endpoints is None:
        return
    require(isinstance(endpoints, dict), "INPUT_INCOMPLETE", "endpoints must be an object")
    for name, value in endpoints.items():
        safe_id(name)
        require(isinstance(value, dict), "INPUT_INCOMPLETE", f"endpoint {name} must be an object")
        unknown = set(value) - ENDPOINT_FIELDS
        require(
            not unknown,
            "INPUT_INCOMPLETE",
            f"Unknown endpoint fields in {name}: " + ", ".join(sorted(unknown)),
        )
        require(
            value.get("class") in ENDPOINT_CLASSES,
            "INPUT_INCOMPLETE",
            f"endpoint {name} requires class in " + ", ".join(ENDPOINT_CLASSES),
        )
        attestation = value.get("attestation", "config-declared")
        require(
            attestation in ATTESTATIONS,
            "INPUT_INCOMPLETE",
            f"endpoint {name} attestation must be one of " + ", ".join(ATTESTATIONS),
        )
        value["attestation"] = attestation


def normalize_routing(c: dict) -> None:
    """Normalize/validate the ``routing`` section **only when it exists** (§16.1).

    段内默认值写回（``fallback`` / ``max_assignments`` / ``cost_ceiling`` /
    ``spend_guard``），段外默认值一律不写回：这样没配路由的项目不受本版影响。
    """
    routing = c.get("routing")
    if routing is None:
        # v1.5.0 修复：只有"根本没有 routing 键"才是历史配置（段不存在 = 旧语义）。
        # 显式 ``"routing": null`` 必须拒绝，否则它同时绕过两条守卫：白名单显式
        # 声明（本函数早退）与授权确认（``attestation_status`` 把 None 当历史语义）。
        require(
            "routing" not in c,
            "INPUT_INCOMPLETE",
            "routing must be an object; null is not accepted (omit the section entirely "
            "to keep the legacy 'configured means authorized' semantics)",
        )
        return
    require(isinstance(routing, dict), "INPUT_INCOMPLETE", "routing must be an object")
    unknown = set(routing) - ROUTING_FIELDS
    require(
        not unknown,
        "INPUT_INCOMPLETE",
        "Unknown routing fields: " + ", ".join(sorted(unknown)),
    )
    require(
        routing.get("schema_version", 1) == 1,
        "INPUT_INCOMPLETE",
        "routing.schema_version must be 1",
    )
    routing["schema_version"] = 1
    mode = routing.get("mode", "explicit")
    require(mode in MODES, "INPUT_INCOMPLETE", "routing.mode must be explicit or auto")
    routing["mode"] = mode

    profiles = list(c.get("agent_profiles") or {})
    allowed = routing.get("allowed_profiles")
    # v1.5.0 M3：声明了 routing 段就必须显式声明白名单。旧行为（缺省 = 全部
    # profile）会让"配过"等于"授权"：新加一个昂贵的 profile 就自动成为自动路由
    # 候选，而显式指定同样走这份白名单，所以 explicit 模式也挡不住。explicit 与
    # auto 一视同仁；只有"根本没有 routing 段"的历史配置保留旧语义（见
    # normalize_routing 开头：段不存在时直接 return）。
    require(
        allowed is not None,
        "INPUT_INCOMPLETE",
        "routing.allowed_profiles must be declared explicitly; an implicit "
        "'all profiles' allowlist is not accepted because it makes 'configured' "
        "equal to 'authorized'",
    )
    require(
        isinstance(allowed, list) and 1 <= len(allowed) <= 8,
        "INPUT_INCOMPLETE",
        "routing.allowed_profiles must contain 1..8 profile ids",
    )
    require(
        len(set(allowed)) == len(allowed),
        "INPUT_INCOMPLETE",
        "routing.allowed_profiles must not repeat an id",
    )
    for pid in allowed:
        require(
            pid in profiles,
            "INPUT_INCOMPLETE",
            f"routing.allowed_profiles references an undefined profile {pid!r}",
        )
        safe_id(pid)
    routing["allowed_profiles"] = list(allowed)

    rules = routing.get("rules", [])
    require(isinstance(rules, list), "INPUT_INCOMPLETE", "routing.rules must be an array")
    seen_types = set()
    normalized_rules = []
    for rule in rules:
        require(isinstance(rule, dict), "INPUT_INCOMPLETE", "routing.rules items must be objects")
        unknown = set(rule) - {"task_type", "prefer"}
        require(
            not unknown,
            "INPUT_INCOMPLETE",
            "Unknown rule fields: " + ", ".join(sorted(unknown)),
        )
        task_type = rule.get("task_type")
        require(
            task_type in RULE_TASK_TYPES,
            "INPUT_INCOMPLETE",
            "routing.rules[].task_type must be one of " + ", ".join(RULE_TASK_TYPES),
        )
        require(
            task_type not in seen_types,
            "INPUT_INCOMPLETE",
            f"routing.rules has two entries for task_type {task_type!r}",
        )
        seen_types.add(task_type)
        prefer = rule.get("prefer")
        require(
            isinstance(prefer, list) and prefer,
            "INPUT_INCOMPLETE",
            "routing.rules[].prefer must be a non-empty array",
        )
        require(
            len(set(prefer)) == len(prefer),
            "INPUT_INCOMPLETE",
            "routing.rules[].prefer must not repeat an id",
        )
        for pid in prefer:
            require(
                pid in allowed,
                "POLICY_DENIED",
                f"routing.rules[].prefer references {pid!r}, which is not in allowed_profiles",
            )
        normalized_rules.append({"task_type": task_type, "prefer": list(prefer)})
    routing["rules"] = normalized_rules

    fallback = routing.get("fallback", "ask")
    require(
        fallback in FALLBACKS, "INPUT_INCOMPLETE", "routing.fallback must be ask or default_profile"
    )
    routing["fallback"] = fallback

    # §6.2 第 7/8 条：默认 profile 必须在批准范围内，否则"批准范围"形同虚设，
    # 而 fallback=default_profile 会让越界者成为无条件胜出者。
    require(
        c.get("default_agent_profile") in allowed,
        "INPUT_INCOMPLETE",
        "default_agent_profile must be listed in routing.allowed_profiles",
    )

    max_assignments = routing.get("max_assignments", 2)
    require(
        isinstance(max_assignments, int)
        and not isinstance(max_assignments, bool)
        and 0 <= max_assignments <= 8,
        "INPUT_INCOMPLETE",
        "routing.max_assignments must be an integer 0..8",
    )
    routing["max_assignments"] = max_assignments

    ceiling = routing.get("cost_ceiling", "premium")
    require(
        ceiling in COST_CLASSES, "INPUT_INCOMPLETE", "routing.cost_ceiling must be a cost class"
    )
    routing["cost_ceiling"] = ceiling

    guard = routing.get("spend_guard", {})
    require(isinstance(guard, dict), "INPUT_INCOMPLETE", "routing.spend_guard must be an object")
    unknown = set(guard) - SPEND_GUARD_FIELDS
    require(
        not unknown,
        "INPUT_INCOMPLETE",
        "Unknown spend_guard fields: " + ", ".join(sorted(unknown)),
    )
    normalized_guard = {}
    for key in ("max_cost_per_task", "max_cost_per_day"):
        value = guard.get(key)
        require(
            value is None or (_is_number(value) and value >= 0),
            "INPUT_INCOMPLETE",
            f"spend_guard.{key} must be a non-negative number or null",
        )
        normalized_guard[key] = value
    currency = guard.get("currency")
    require(
        currency is None or (isinstance(currency, str) and currency),
        "INPUT_INCOMPLETE",
        "spend_guard.currency must be a string or null",
    )
    normalized_guard["currency"] = currency
    on_exceed = guard.get("on_exceed", "block")
    # v1.2 只实现 block：warn 会让"越线"退化成一条日志，属于未实现的能力。
    require(
        on_exceed == "block",
        "CAPABILITY_UNSUPPORTED",
        "spend_guard.on_exceed=warn is not implemented in v1.2; use block",
    )
    normalized_guard["on_exceed"] = on_exceed
    routing["spend_guard"] = normalized_guard

    use_performance = routing.get("use_performance", False)
    require(
        isinstance(use_performance, bool), "INPUT_INCOMPLETE", "use_performance must be a boolean"
    )
    # 诚实拒绝优于接受后忽略（§2.3、C9）。
    require(
        use_performance is False,
        "CAPABILITY_UNSUPPORTED",
        "use_performance=true is not implemented in v1.2: history is recorded only, "
        "never used to exclude candidates",
    )
    routing["use_performance"] = False

    c["routing"] = routing


def authorized_profiles(c: dict) -> list[str]:
    """The authorized candidate set for this project (v1.5.0 M3/M4).

    "没有 routing 段"是历史语义：所有已配置 profile 都算授权。**有 routing 段**
    时白名单必须显式且非空——这里不再用 ``or sorted(profiles)`` 兜底。兜底等于把
    "白名单没声明"静默翻译成"全部候选"，任何让路由段未被规范化（显式 null、
    读路径 strict=False）的路径都会因此退化成全量授权。决策期必须与规范化期给出
    同一个答案，否则守卫只在一条路径上存在。
    """
    profiles = c.get("agent_profiles") or {}
    if "routing" not in c:
        return sorted(profiles)
    routing = c.get("routing")
    require(
        isinstance(routing, dict),
        "INPUT_INCOMPLETE",
        "routing must be an object; null is not accepted",
    )
    allowed = routing.get("allowed_profiles")
    require(
        isinstance(allowed, list) and bool(allowed),
        "POLICY_DENIED",
        "routing.allowed_profiles is not declared; an implicit 'all profiles' "
        "allowlist is not accepted because it makes 'configured' equal to 'authorized'",
    )
    return list(allowed)


def routing_digest(c: dict) -> str | None:
    """Digest of the routing section alone, or ``None`` when absent."""
    routing = c.get("routing")
    return digest(routing) if routing is not None else None


def endpoint_of(c: dict, profile: dict) -> dict | None:
    """Resolve ``endpoint_ref`` into the recorded ``{id, class, attestation}`` form.

    A dangling reference is ``INPUT_INCOMPLETE`` at ``create`` time: it is a
    configuration defect, not a runtime unknown.
    """
    ref = profile.get("endpoint_ref")
    if not ref:
        return None
    endpoints = c.get("endpoints") or {}
    require(
        ref in endpoints, "INPUT_INCOMPLETE", f"endpoint_ref {ref!r} is not defined in endpoints"
    )
    value = endpoints[ref]
    return {"id": ref, "class": value.get("class"), "attestation": value.get("attestation")}


# --------------------------------------------------------------------------- #
# 组合标识
# --------------------------------------------------------------------------- #


def combination_id(
    *,
    adapter_id: str,
    backend: str,
    profile: dict,
    cli_version: str | None,
    endpoint: dict | None,
) -> str:
    """Stable identity of the *effective* execution combination (§7.1).

    三条纪律：不含绝对路径、不含凭据、缺省即不出现。因此同一个组合换一台机器
    仍得同一个 id，而改 model / 版本 / 端点必然不同。
    """
    payload: dict = {"adapter_id": adapter_id, "backend": backend}
    if cli_version:
        payload["cli_version"] = cli_version
    for key in ("provider", "model", "model_variant", "reasoning_effort"):
        value = profile.get(key)
        if value is not None:
            payload[key] = value
    if profile.get("permission_profile"):
        payload["permission_profile"] = profile["permission_profile"]
    tools = sorted(profile.get("approved_tools") or [])
    if tools:
        payload["approved_tools"] = tools
    if profile.get("max_turns") is not None:
        payload["max_turns"] = profile["max_turns"]
    if endpoint:
        payload["endpoint"] = {"id": endpoint.get("id"), "class": endpoint.get("class")}
    # 只含已声明的能力项：未声明的字段不出现，而不是填 null。
    declared = {
        key: value
        for key, value in (profile.get("capabilities") or {}).items()
        if value is not None
    }
    if declared:
        payload["attributes"] = declared
    return "cmb-" + digest(payload)[:16]


def selected_execution(
    *,
    binding,
    endpoint: dict | None,
    cli_version: str | None,
    policy_revision: str,
    routing: str | None,
) -> dict:
    """The ``selected_execution`` block frozen at ``create`` (§12.1).

    能确定的都冻结，不能确定的字段留 ``null``，不猜。
    """
    profile = binding.profile
    return {
        "profile_id": binding.profile_id,
        "combination_id": combination_id(
            adapter_id=binding.adapter_id,
            backend=binding.backend,
            profile=profile,
            cli_version=cli_version,
            endpoint=endpoint,
        ),
        "adapter_id": binding.adapter_id,
        "adapter_backend": binding.backend,
        "provider": profile.get("provider"),
        "model": profile.get("model"),
        "model_variant": profile.get("model_variant"),
        "reasoning_effort": profile.get("reasoning_effort"),
        "permission_profile": profile.get("permission_profile"),
        "endpoint": {"id": (endpoint or {}).get("id"), "class": (endpoint or {}).get("class")},
        "cli_version_pinned": profile.get("expected_version"),
        "cli_version_effective": None,
        "policy_revision": policy_revision,
        "routing_digest": routing,
        "model_unresolved": model_unresolved(profile),
    }


def frozen_execution(
    selected: dict,
    *,
    binding,
    endpoint: dict | None,
    measured_version: str | None,
    resolved_path: str | None = None,
    resolution_source: str | None = None,
) -> dict:
    """Complete ``selected_execution`` at ``start`` with the **measured** version (§12.2).

    ``create`` 只能用 pin 版本算组合；实测值必须由 ``start`` 补齐，并重算
    ``combination_id``——否则"pin 说 2.1、实测是 2.2"会被记成同一个组合。
    """
    frozen = dict(selected)
    effective = measured_version or selected.get("cli_version_pinned")
    frozen["cli_version_effective"] = effective
    frozen["combination_id"] = combination_id(
        adapter_id=binding.adapter_id,
        backend=binding.backend,
        profile=binding.profile,
        cli_version=effective,
        endpoint=endpoint,
    )
    frozen["resolved_path"] = resolved_path
    frozen["resolution_source"] = resolution_source
    frozen["argv_audit_sha256"] = None
    frozen["frozen_at"] = time.time()
    return frozen


def model_unresolved(profile: dict) -> bool:
    """``allow_cli_default_model`` 的效果：连"跑的是哪个模型"都不知道（§6.1.1）。

    与 ``_profiles.model_unresolved`` 同一判定（含迁移来的 legacy profile），
    避免"注册时认为已解析、报告里认为未解析"这种两份真相。
    """
    from src._profiles import model_unresolved as impl

    return impl(profile)


def combination_verified(
    *,
    run: dict,
    binding=None,
    usage: dict | None = None,
    model_identity_value: str | None = None,
) -> bool:
    """L2 集成事实：只有"确实观测到同一个模型 + 有用量 + 有版本"才算实测过（§8.2）。

    否决条件任一成立即恒为 ``false``：
    ``model_unresolved``、``usage`` 三项全空、``model_identity`` **不是**匹配态。

    最后一条必须是白名单而不是黑名单：``unobserved``（工具一个字都没报，或没有
    请求值可比对）与 ``reported_mismatch`` / ``mixed`` 一样，都不能支撑"该组合被
    实测过"。闸门侧 ``pin_blocks`` 早就这样判（"工具没说"不是"对上了"），这里
    若放行，同一个事实就会在 run 记录里写成 ``combination_verified=true``。
    """
    if run.get("model_unresolved"):
        return False
    if model_identity_value not in ("reported_match", "alias_confirmed"):
        return False
    usage = usage or (run.get("agent_result") or {}).get("usage") or {}
    if all(usage.get(k) is None for k in ("input_tokens", "output_tokens", "cost")):
        # "没有数据"不等于"没有问题"：无法核算成本就不能声称完整观测过。
        return False
    if binding is not None:
        pinned = binding.profile.get("expected_version")
        measured = run.get("cli_version")
        if pinned and measured and pinned != measured:
            return False
    return bool(run.get("cli_version")) and run.get("result_schema") == "agent-adapter"


# --------------------------------------------------------------------------- #
# 要求（requirements）
# --------------------------------------------------------------------------- #


def normalize_requirements(raw, profile: dict) -> dict:
    """Validate a ``route.requirements`` block and resolve the pin default (§9.3.1).

    未声明的项不出现（无约束），而不是填一个"看起来安全"的默认值。
    """
    require(
        raw is None or isinstance(raw, dict),
        "INPUT_INCOMPLETE",
        "route.requirements must be an object",
    )
    raw = dict(raw or {})
    # 内部标记（pin 默认值来自哪里）会随任务记录回传，不能被当成用户输入拒绝。
    raw.pop("pin_model_source", None)
    unknown = set(raw) - REQUIREMENT_FIELDS
    require(
        not unknown,
        "INPUT_INCOMPLETE",
        "Unknown requirement fields: " + ", ".join(sorted(unknown)),
    )
    out: dict = {}
    modalities = raw.get("input_modalities")
    if modalities is not None:
        require(
            isinstance(modalities, list)
            and modalities
            and all(m in agent_base.INPUT_MODALITIES for m in modalities),
            "INPUT_INCOMPLETE",
            "requirements.input_modalities must be a non-empty array of known modalities",
        )
        out["input_modalities"] = list(dict.fromkeys(modalities))
    budget = raw.get("context_budget")
    if budget is not None:
        require(
            isinstance(budget, int) and not isinstance(budget, bool) and budget > 0,
            "INPUT_INCOMPLETE",
            "requirements.context_budget must be a positive integer",
        )
        out["context_budget"] = budget
    for key in (
        "needs_tool_use",
        "structured_result",
        "non_interactive",
        "session_resume",
        "structured_input",
    ):
        if raw.get(key) is not None:
            require(
                isinstance(raw[key], bool),
                "INPUT_INCOMPLETE",
                f"requirements.{key} must be a boolean",
            )
            out[key] = raw[key]
    residency = raw.get("data_residency")
    if residency is not None:
        require(
            residency in RESIDENCY,
            "INPUT_INCOMPLETE",
            "requirements.data_residency must be preferred or required",
        )
        out["data_residency"] = residency
    minimum = raw.get("min_capability")
    if minimum is not None:
        require(
            minimum in CAPABILITY_ORDER,
            "INPUT_INCOMPLETE",
            "requirements.min_capability must be IMPLEMENTED, UNVERIFIED or VERIFIED",
        )
        out["min_capability"] = minimum
    permission_in = raw.get("permission_profile_in")
    if permission_in is not None:
        require(
            isinstance(permission_in, list)
            and permission_in
            and all(isinstance(x, str) and x for x in permission_in),
            "INPUT_INCOMPLETE",
            "requirements.permission_profile_in must be a non-empty string array",
        )
        out["permission_profile_in"] = list(permission_in)

    # pin_model 默认值按成本档：premium 组合默认要求确认（§9.3.1）。
    # 已知的粗略之处：决策前还不知道最终选中谁，所以这里用"典型 profile"（显式
    # 指定的那个，否则项目默认）的成本档。宁可从严（默认档通常是保守的一侧），
    # 也不要在选中之后再改需求——那会让决策与记录不一致。
    if raw.get("pin_model") is not None:
        require(
            isinstance(raw["pin_model"], bool),
            "INPUT_INCOMPLETE",
            "requirements.pin_model must be a boolean",
        )
        out["pin_model"] = raw["pin_model"]
        out["pin_model_source"] = "request"
    else:
        premium = (profile.get("cost_class") or "standard") == "premium"
        out["pin_model"] = premium
        out["pin_model_source"] = "cost_class_default" if premium else "default"
    # 转义口与固定模型互斥：允许 CLI 决定模型，就无法承诺"只用它"（§6.1.1）。
    require(
        not (model_unresolved(profile) and out["pin_model"]),
        "INPUT_INCOMPLETE",
        "allow_cli_default_model=true and pin_model=true are mutually exclusive",
    )
    return out


# --------------------------------------------------------------------------- #
# 快照（有副作用）
# --------------------------------------------------------------------------- #


def _sanitized(snapshot: dict) -> dict:
    """Drop absolute paths before digesting, so the digest is portable (§7.1)."""
    copy = {k: v for k, v in snapshot.items() if k != "budget"}
    candidates = {}
    for pid, value in (copy.get("candidates") or {}).items():
        candidates[pid] = {
            key: item for key, item in value.items() if key not in ("resolution_path",)
        }
    copy["candidates"] = candidates
    return copy


def snapshot_digest(snapshot: dict) -> str:
    return digest(_sanitized(snapshot))


def collect_snapshot(
    c: dict,
    profile_ids: list[str],
    *,
    probe: bool = True,
    time_budget: float = 5.0,
    doctor_fn=None,
) -> dict:
    """Collect the environment facts the decision needs (the only side-effecting step).

    项目级检查只算一次（``git``/平台层由 ``doctor`` 内部缓存），每候选一次
    ``doctor``，并受总时间预算约束：超预算的候选标 ``ENVIRONMENT_ERROR`` 而不是
    无限等待（宁可少选，不可卡死，§8.2）。
    """
    from src._config import doctor as default_doctor

    doctor_fn = doctor_fn or default_doctor
    started = time.monotonic()
    candidates: dict[str, dict] = {}
    probes = 0
    exhausted = False
    for pid in profile_ids:
        elapsed = time.monotonic() - started
        if elapsed > time_budget:
            exhausted = True
            candidates[pid] = {
                "profile_id": pid,
                "ready": False,
                "error": {"code": "ENVIRONMENT_ERROR", "message": "probe budget exhausted"},
            }
            continue
        try:
            report = (
                doctor_fn(c, pid, probe=probe) if _accepts_probe(doctor_fn) else doctor_fn(c, pid)
            )
        except Error as exc:
            candidates[pid] = {
                "profile_id": pid,
                "ready": False,
                "error": {"code": exc.code, "message": str(exc)},
            }
            continue
        checks = report.get("checks") or {}
        adapter = checks.get("adapter") or {}
        resolution = report.get("resolution") or {}
        if probe and resolution.get("path"):
            probes += 1
        candidates[pid] = {
            "profile_id": pid,
            "ready": bool(report.get("ready")),
            "adapter_id": adapter.get("adapter_id")
            or (report.get("profiles") or [{}])[0].get("adapter_id"),
            "backend": adapter.get("backend"),
            "capability": adapter.get("capability"),
            "trust_level": adapter.get("trust_level"),
            "permission_enforced": bool((adapter.get("permission") or {}).get("enforced")),
            "credential_present": report.get("credential_present"),
            "resolution_path": resolution.get("path"),
            "resolution_source": resolution.get("source"),
            "cli_version": resolution.get("version"),
            "output_protocol": adapter.get("output_protocol"),
            "error": report.get("adapter_error"),
        }
    return {
        "candidates": candidates,
        "budget": {
            "candidates": len(profile_ids),
            "probes": probes,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "exhausted": exhausted,
        },
    }


def _accepts_probe(doctor_fn) -> bool:
    """``doctor`` grew a keyword-only ``probe`` flag in v1.2."""
    try:
        import inspect

        return "probe" in inspect.signature(doctor_fn).parameters
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# 决策（纯函数）
# --------------------------------------------------------------------------- #


def _match_rule(rules: list[dict], task_type: str) -> dict | None:
    for rule in rules or []:
        if rule.get("task_type") == task_type:
            return rule
    for rule in rules or []:
        if rule.get("task_type") == "*":
            return rule
    return None


def _l1_exclusion(
    requirements: dict, *, profile: dict, binding, adapter, endpoint
) -> tuple[str, str] | None:
    """Return ``(code, detail)`` for the first unmet hard requirement, else ``None``.

    L1 只做粗筛：所有细节都在 ``detail`` 里写清"明确超限"还是"通过但不确定"。
    """
    capabilities = profile.get("capabilities") or {}
    requested = requirements.get("input_modalities")
    if requested:
        declared = capabilities.get("input_modalities") or []
        missing = [m for m in requested if m not in declared]
        if missing:
            return (
                "CAPABILITY_UNSUPPORTED",
                "input_modalities missing " + ", ".join(repr(m) for m in missing),
            )
    budget = requirements.get("context_budget")
    if budget is not None:
        window = capabilities.get("context_window")
        if not window:
            return ("CAPABILITY_UNSUPPORTED", "capabilities.context_window not declared")
        if budget > window:
            return (
                "CAPABILITY_UNSUPPORTED",
                f"context_budget {budget} exceeds nominal context_window {window}",
            )
        if budget > window * 0.6:
            # 粗筛通过，但可用预算未验证：这句话必须能被读者看到。
            return None
    if requirements.get("needs_tool_use") is True and capabilities.get("tool_use") is not True:
        return ("CAPABILITY_UNSUPPORTED", "capabilities.tool_use is not declared true")
    if requirements.get("structured_result") is True:
        if capabilities.get("structured_result") is not True:
            return ("CAPABILITY_UNSUPPORTED", "capabilities.structured_result is not declared true")
        if getattr(adapter, "output_protocol", None) != "json-final":
            # 模型能出 JSON 但 CLI 只输出自由文本，组合仍不合格（§8.1）。
            return (
                "CAPABILITY_UNSUPPORTED",
                "adapter output_protocol is {!r}, not 'json-final'".format(
                    getattr(adapter, "output_protocol", None)
                ),
            )
    if requirements.get("non_interactive") is True:
        if not (binding.permission or {}).get("enforced"):
            return (
                "PERMISSION_PROFILE_UNBOUND",
                "permission argv not bound for {!r}".format(profile.get("permission_profile")),
            )
    if requirements.get("session_resume") is True:
        return ("CAPABILITY_UNSUPPORTED", "native session resume is not implemented")
    if requirements.get("structured_input") is True and not getattr(
        adapter, "supports_structured_input", False
    ):
        return ("CAPABILITY_UNSUPPORTED", "adapter cannot report structured input requests")
    permission_in = requirements.get("permission_profile_in")
    if permission_in and profile.get("permission_profile") not in permission_in:
        return (
            "PERMISSION_PROFILE_UNBOUND",
            "permission_profile {!r} is not in the requested set".format(
                profile.get("permission_profile")
            ),
        )
    minimum = requirements.get("min_capability")
    if minimum:
        actual = binding.capability
        if CAPABILITY_ORDER.get(actual, 0) < CAPABILITY_ORDER.get(minimum, 0):
            code = "ADAPTER_VERSION_MISMATCH" if actual == "UNVERIFIED" else "ADAPTER_UNVERIFIED"
            return (code, f"capability {actual} is below the requested minimum {minimum}")
    residency = requirements.get("data_residency")
    if residency == "required":
        if endpoint is None:
            return (
                "ENDPOINT_UNVERIFIED",
                "data_residency=required but the profile declares no endpoint_ref",
            )
        if endpoint.get("class") not in ("private", "local"):
            return (
                "ENDPOINT_UNVERIFIED",
                "data_residency=required but endpoint {!r} is class {!r}".format(
                    endpoint.get("id"), endpoint.get("class")
                ),
            )
        if not getattr(adapter, "supports_endpoint_ref", False):
            # 无法把端点落到启动配置 = 无法确认数据去向 = 拒绝派发，而不是派发后祈祷。
            return (
                "ENDPOINT_UNVERIFIED",
                f"{getattr(adapter, 'id', 'adapter')} cannot bind endpoint_ref "
                "to its launch configuration",
            )
    return None


def decide(
    c: dict,
    requirements: dict,
    snapshot: dict,
    *,
    requested_profile: str | None = None,
    task_type: str = "unknown",
    explicit_profile: str | None = None,
) -> dict:
    """Pure decision over one snapshot. Same inputs → byte-identical output (§11.3).

    显式指定把候选**收窄为 1 个**，不跳过 L0/L1/L2；失败时返回该候选的具体排除码，
    绝不回落到别的 profile（否则等于未经批准的替换）。
    """
    profiles = c.get("agent_profiles") or {}
    routing = c.get("routing")
    mode = (routing or {}).get("mode", "explicit")
    allowed = authorized_profiles(c)
    ceiling = (routing or {}).get("cost_ceiling", "premium")
    require_real_e2e = bool(c.get("require_real_e2e"))

    if explicit_profile is not None or requested_profile is not None:
        pid = explicit_profile if explicit_profile is not None else requested_profile
        require(
            pid in allowed,
            "POLICY_DENIED",
            f"profile {pid!r} is not in routing.allowed_profiles",
        )
        candidate_ids = [pid]
        source = "explicit"
    elif mode == "auto":
        candidate_ids = [pid for pid in allowed if pid in profiles]
        source = "auto"
    else:
        default = c.get("default_agent_profile")
        # 默认 profile 同样受白名单约束（SKILL.md：「同时约束显式指定与自动选择」）。
        # 规范化期确实会查 `default in allowed`，但读路径是 `strict=False`，且
        # profile 规范化失败时配置会**原样返回**——那条路径上默认 profile 可能不在
        # 白名单里，只有这里再判一次才能保证"白名单是白名单"。
        require(
            default in allowed,
            "POLICY_DENIED",
            f"default_agent_profile {default!r} is not in routing.allowed_profiles; "
            "fix the configuration (default_agent_profile must be an allowed profile)",
        )
        candidate_ids = [default]
        source = "project_default"

    candidates: list[dict] = []
    eligible: list[str] = []
    l1_excluded: list[dict] = []
    l2_excluded: list[dict] = []
    for pid in candidate_ids:
        profile = profiles.get(pid) or {}
        entry = registry.get(profile.get("adapter") or "")
        adapter = entry.factory() if entry else None
        facts = (snapshot.get("candidates") or {}).get(pid) or {}
        record = {"profile_id": pid, "status": "eligible", "code": None, "detail": None}

        def exclude(code: str, detail: str, layer: str, record=record, pid=pid) -> None:
            # 绑定 `record`/`pid`：闭包在本轮内同步调用，但不绑定会随循环变量
            # 漂移，将来异步化就是静默的错记。
            record.update(status="excluded", code=code, detail=detail)
            (l1_excluded if layer == "l1" else l2_excluded).append(
                {"profile_id": pid, "code": code, "detail": detail}
            )

        if adapter is None:
            exclude("ADAPTER_UNVERIFIED", "profile references an unregistered adapter", "l2")
            candidates.append(record)
            continue
        # L0：成本上限是**排除**，不是排序（§11.1.1）。
        cost_class = profile.get("cost_class") or "standard"
        if COST_ORDER.get(cost_class, 1) > COST_ORDER.get(ceiling, 2):
            exclude(
                "COST_CEILING_EXCEEDED",
                f"cost_class {cost_class} exceeds cost_ceiling {ceiling}",
                "l1",
            )
            candidates.append(record)
            continue
        # L1：声明 + 机械校验。
        try:
            binding = _binding_from(c, pid)
        except Error as exc:
            exclude(exc.code, _sanitize_detail(str(exc)), "l1")
            candidates.append(record)
            continue
        endpoint = None
        try:
            endpoint = endpoint_of(c, profile)
        except Error as exc:
            exclude(exc.code, _sanitize_detail(str(exc)), "l1")
            candidates.append(record)
            continue
        unmet = _l1_exclusion(
            requirements,
            profile=profile,
            binding=binding,
            adapter=adapter,
            endpoint=endpoint,
        )
        if unmet:
            exclude(unmet[0], unmet[1], "l1")
            candidates.append(record)
            continue
        # L2：集成事实/能力态闸门。
        if require_real_e2e and binding.capability != registry.CAPABILITY_VERIFIED:
            subject = "require_real_e2e=true but capability is " + str(binding.capability)
            detail = binding.capability_reason or subject
            exclude(
                "ADAPTER_VERSION_MISMATCH"
                if binding.capability == "UNVERIFIED"
                else "ADAPTER_UNVERIFIED",
                _sanitize_detail(detail),
                "l2",
            )
            candidates.append(record)
            continue
        # L2-c：可执行环境（doctor 就绪 + CLI 已解析）。
        if not facts.get("ready"):
            error = facts.get("error") or {}
            exclude(
                error.get("code") or "ENVIRONMENT_ERROR",
                _sanitize_detail(error.get("message") or "doctor reports not ready"),
                "l2",
            )
            candidates.append(record)
            continue
        if facts.get("resolution_path") is None:
            exclude("ENVIRONMENT_ERROR", "agent CLI not resolved on this host", "l2")
            candidates.append(record)
            continue
        record["detail"] = _pass_note(requirements, profile)
        eligible.append(pid)
        candidates.append(record)

    # ``mode`` 描述的是**这次决策走的路**，不是项目配置的字面值：显式指定与默认
    # profile 都是显式路径，只有 router 真正参与时才是 auto。
    effective_mode = "auto" if source == "auto" else "explicit"
    order: dict = {"source": source, "sequence": []}
    if not eligible:
        return {
            "mode": effective_mode,
            "decision": "no_eligible",
            "reason": "no_eligible",
            "selected_profile": None,
            "candidate_ids": candidate_ids,
            "candidates": candidates,
            "layers": {"l1_excluded": l1_excluded, "l2_excluded": l2_excluded},
            "order": order,
            "fallback_used": None,
            "gap_codes": [
                {"profile_id": c_["profile_id"], "code": c_["code"]} for c_ in candidates
            ],
        }

    rule = _match_rule((routing or {}).get("rules") or [], task_type)
    prefer = rule["prefer"] if rule else []
    hits = [pid for pid in prefer if pid in eligible]
    if len(eligible) == 1:
        # 只有一个候选时"偏好"没有起作用，声称 `rule_preference` 会把一次没有
        # 选择的决策说成一次有依据的选择。
        selected = eligible[0]
        reason = "only_eligible"
        order = {"source": "only_eligible", "sequence": list(eligible)}
        fallback_used = None
    elif hits:
        selected = hits[0]
        reason = (
            "rule_preference" if rule and rule.get("task_type") != "*" else "catch_all_preference"
        )
        order = {
            "source": "user_preference",
            "sequence": hits + [p for p in eligible if p not in hits],
        }
        fallback_used = None
    else:
        fallback = (routing or {}).get("fallback", "ask")
        empty_reason = "prefer_empty" if rule else "no_rule"
        default = c.get("default_agent_profile")
        if fallback == "default_profile" and default in eligible:
            selected = default
            reason = "fallback_default"
            order = {
                "source": "fallback",
                "sequence": [default] + [p for p in eligible if p != default],
            }
            fallback_used = "default_profile"
        else:
            return {
                "mode": effective_mode,
                "decision": "ambiguous",
                "reason": empty_reason,
                "selected_profile": None,
                "candidate_ids": candidate_ids,
                "candidates": candidates,
                "layers": {"l1_excluded": l1_excluded, "l2_excluded": l2_excluded},
                "order": {"source": "fallback", "sequence": list(eligible)},
                "fallback_used": fallback,
                "options": [
                    {"profile_id": pid, "capability": _capability_of(c, pid)} for pid in eligible
                ],
            }

    for record in candidates:
        if record["profile_id"] == selected:
            record["status"] = "selected"
    return {
        "mode": effective_mode,
        "decision": "selected",
        "reason": reason,
        "selected_profile": selected,
        "candidate_ids": candidate_ids,
        "candidates": candidates,
        "layers": {"l1_excluded": l1_excluded, "l2_excluded": l2_excluded},
        "order": order,
        "fallback_used": fallback_used,
    }


def combination_allowed(c: dict, profile_id: str, requirements: dict) -> tuple[str, str] | None:
    """Policy-only re-check of an **existing** combination after a rebind (§16.5).

    只做 L0 + L1：环境（L2-c）是 ``start`` 的职责，在 rebind 时重测环境会把
    "策略变宽了"误报成"机器坏了"。也**不**做 L2 能力闸门——那会在策略没变时
    因为证据过期而阻断正在进行的任务。
    """
    routing = c.get("routing")
    profiles = c.get("agent_profiles") or {}
    allowed = authorized_profiles(c)
    if profile_id not in allowed:
        return ("POLICY_DENIED", f"profile {profile_id!r} is no longer in routing.allowed_profiles")
    profile = profiles.get(profile_id) or {}
    ceiling = (routing or {}).get("cost_ceiling", "premium")
    cost_class = profile.get("cost_class") or "standard"
    if COST_ORDER.get(cost_class, 1) > COST_ORDER.get(ceiling, 2):
        return ("POLICY_DENIED", f"cost_class {cost_class} exceeds cost_ceiling {ceiling}")
    entry = registry.get(profile.get("adapter") or "")
    if entry is None:
        return ("POLICY_DENIED", "profile references an unregistered adapter")
    try:
        binding = _binding_from(c, profile_id)
        endpoint = endpoint_of(c, profile)
    except Error as exc:
        return ("POLICY_DENIED", _sanitize_detail(str(exc)))
    unmet = _l1_exclusion(
        requirements or {},
        profile=profile,
        binding=binding,
        adapter=entry.factory(),
        endpoint=endpoint,
    )
    return ("POLICY_DENIED", unmet[1]) if unmet else None


def _pass_note(requirements: dict, profile: dict) -> str | None:
    budget = requirements.get("context_budget")
    window = (profile.get("capabilities") or {}).get("context_window")
    if budget is not None and window:
        return f"context_budget {budget} within nominal window; usable budget unverified"
    return None


def _capability_of(c: dict, pid: str) -> str | None:
    try:
        return _binding_from(c, pid).capability
    except Error:
        return None


def _binding_from(c: dict, pid: str):
    """Resolve one profile without importing ``_profiles`` at module import time."""
    from src import _profiles

    return _profiles.resolve_binding(c, pid)


def _sanitize_detail(text: str) -> str:
    """Redact absolute host paths from a decision ``detail`` (§9.1 R2).

    The record goes into reports; the raw message stays in ``last_error`` and the
    task events, which are local diagnostics.
    """
    out = str(text or "")
    for token in ("/Users/", "/home/", "/private/", "/tmp/", "/var/"):
        index = out.find(token)
        while index >= 0:
            end = out.find(" ", index)
            end = len(out) if end < 0 else end
            out = out[:index] + "<redacted>" + out[end:]
            index = out.find(token)
    return out[:400]


# --------------------------------------------------------------------------- #
# 决策文档
# --------------------------------------------------------------------------- #


def route_id_for(
    task_id, assignment_revision, task_type, requirements, profile_id, routing_d, policy
) -> str:
    """关联标识（可复现），**不**承担防篡改职责（§15.5）。"""
    return (
        "rt-"
        + digest(
            {
                "task_id": task_id,
                "assignment_revision": assignment_revision,
                "task_type": task_type,
                "requirements": requirements,
                "selected_profile": profile_id,
                "routing_digest": routing_d,
                "policy_revision": policy,
            }
        )[:12]
    )


def build_decision(
    *,
    c: dict,
    plan: dict,
    requirements: dict,
    task_type: str,
    task_type_source: str,
    task_id: str,
    assignment_revision: int,
    trigger: str,
    requested_profile: str | None,
    execution: dict | None,
    project_id: str,
) -> dict:
    """Assemble ``RouteDecision v2`` (§14.2). ``layers`` has no ``l3_excluded``."""
    policy_revision = digest(c)[:16]
    routing_d = routing_digest(c)
    document = {
        "schema_version": 2,
        "route_id": route_id_for(
            task_id,
            assignment_revision,
            task_type,
            requirements,
            (execution or {}).get("profile_id"),
            routing_d,
            policy_revision,
        ),
        "task_id": task_id,
        "project_id": project_id,
        "assignment_revision": assignment_revision,
        "at": time.time(),
        "trigger": trigger,
        "mode": plan["mode"],
        "decision": plan["decision"],
        # `reason` 与 `decision` 必须分开读：`selected` 可以是偏好命中、唯一候选、
        # 默认回退三种完全不同的事，合成一个字段会让报告撒谎。
        "reason": plan["reason"],
        "task_type": task_type,
        "task_type_source": task_type_source,
        "requested_profile": requested_profile,
        "selected_execution": execution,
        "requirements": requirements,
        "candidates": plan["candidates"],
        "layers": plan["layers"],
        "order": plan["order"],
        "fallback_used": plan.get("fallback_used"),
        "policy_revision": policy_revision,
        "routing_digest": routing_d,
        "snapshot_digest": plan.get("snapshot_digest"),
    }
    document["content_sha256"] = decision_content_sha256(document)
    return document


def decision_content_sha256(document: dict) -> str:
    """全文完整性摘要（覆盖排除原因与环境观测），与 ``route_id`` 职责分开。"""
    body = {key: value for key, value in document.items() if key != "content_sha256"}
    return digest(body)


def decision_path(store, t: dict, revision: int | None = None) -> Path:
    revision = int(revision if revision is not None else t.get("assignment_revision", 1))
    return store.path(t) / f"route-{revision:02d}.json"


def write_decision(store, t: dict, document: dict) -> str:
    """Write ``route-<rev>.json`` and return its ``content_sha256``.

    Called inside the same transaction that writes the task record, so the
    "decision file first, task second" order of §15.5 is preserved by the caller.
    """
    require(
        decision_content_sha256(document) == document.get("content_sha256"),
        "ROUTE_RECORD_INVALID",
        "Route decision content digest does not match its body",
    )
    atomic(decision_path(store, t), document)
    return document["content_sha256"]


def read_decision(store, t: dict) -> dict:
    """Read and verify the current decision record, or refuse (§15.5 R9)."""
    path = decision_path(store, t)
    if not path.is_file():
        raise Error(
            "ROUTE_RECORD_INVALID",
            "route decision file is missing; recreate the task or restore the record",
            details={
                "task_id": t.get("task_id"),
                "route_id": t.get("route_id"),
                "problem": "missing",
            },
        )
    try:
        document = read(path)
    except (OSError, ValueError) as exc:
        raise Error(
            "ROUTE_RECORD_INVALID",
            f"route decision file is unreadable: {exc}",
            details={
                "task_id": t.get("task_id"),
                "route_id": t.get("route_id"),
                "problem": "unreadable",
            },
        ) from exc
    expected = t.get("route_content_sha256")
    actual = decision_content_sha256(document)
    if expected and expected != actual:
        raise Error(
            "ROUTE_RECORD_INVALID",
            "route decision content_sha256 does not match the task record",
            details={
                "task_id": t.get("task_id"),
                "route_id": t.get("route_id"),
                "problem": "digest_mismatch",
            },
        )
    if t.get("route_id") and document.get("route_id") != t["route_id"]:
        raise Error(
            "ROUTE_RECORD_INVALID",
            "route decision belongs to a different route_id",
            details={
                "task_id": t.get("task_id"),
                "route_id": t.get("route_id"),
                "problem": "route_id_mismatch",
            },
        )
    return document


# --------------------------------------------------------------------------- #
# 路由入口（plan / create / reassign 共用）
# --------------------------------------------------------------------------- #


def task_type_of(request: dict, spec_document: dict | None) -> tuple[str, str]:
    """``route.task_type`` > ``spec.document.task_type`` > ``unknown`` (§7.2)."""
    route = (request or {}).get("route") or {}
    declared = route.get("task_type")
    if declared:
        return declared, "request"
    found = (spec_document or {}).get("task_type")
    if found:
        return found, "spec"
    return "unknown", "default"


def plan_route(
    c: dict,
    request: dict | None = None,
    *,
    spec_document: dict | None = None,
    task_id: str = "-",
    assignment_revision: int = 1,
    trigger: str = "create",
    probe: bool = True,
    explicit_profile: str | None = None,
) -> dict:
    """Snapshot + decide, with no writes (§5.1, §15.1).

    ``route plan`` calls this directly; ``create``/``reassign`` call it and then
    write the decision document. Both paths run the *same* ``decide()``.
    """
    request = request or {}
    routing = c.get("routing")
    route = request.get("route")
    if route is not None:
        require(
            (routing or {}).get("mode") == "auto",
            "INPUT_INCOMPLETE",
            "route was declared but the project has no routing.mode=auto; "
            "enable routing or drop the route block instead of having it ignored",
        )
    task_type, task_type_source = task_type_of(request, spec_document)
    require(
        task_type in RULE_TASK_TYPES,
        "INPUT_INCOMPLETE",
        "task_type must be one of " + ", ".join(RULE_TASK_TYPES),
    )
    api_profiles = c.get("agent_profiles") or {}
    # ``explicit_profile`` 是调用方（``group plan`` 的路由预演）传进来的成员级
    # 指定：派发时 ``_group_runner`` 会把它写进 ``agent_profile``，预演必须用同一
    # 个候选，否则 routes/*.json 与真实派发描述的是两个 profile。
    requested = request.get("agent_profile") or explicit_profile
    # 显式 profile 也要有一个"典型 profile"来决定 pin 默认值：它就是被指定的那个。
    pin_subject = requested or c.get("default_agent_profile")
    requirements = normalize_requirements(
        (route or {}).get("requirements"), api_profiles.get(pin_subject, {})
    )
    mode = (routing or {}).get("mode", "explicit")
    allowed = authorized_profiles(c)
    if requested:
        # 显式指定把候选收窄为 1 个；它是否在批准范围内由 `decide()` 报
        # POLICY_DENIED，这里只负责把该候选放进快照。
        candidate_ids = [requested]
    elif mode == "auto":
        candidate_ids = [pid for pid in allowed if pid in api_profiles]
    else:
        candidate_ids = [c.get("default_agent_profile")]
    snapshot = collect_snapshot(c, [pid for pid in candidate_ids], probe=probe)
    plan = decide(
        c,
        requirements,
        snapshot,
        requested_profile=None,
        task_type=task_type,
        explicit_profile=requested,
    )
    plan.update(
        requirements=requirements,
        task_type=task_type,
        task_type_source=task_type_source,
        snapshot=snapshot,
        snapshot_digest=snapshot_digest(snapshot),
        routing_digest=routing_digest(c),
        policy_revision=digest(c)[:16],
        trigger=trigger,
        task_id=task_id,
        assignment_revision=assignment_revision,
    )
    return plan


def raise_for_plan(plan: dict) -> None:
    """Turn a preview plan into a hard refusal (``create`` must not half-succeed).

    显式指定时报**该候选的具体排除码**（§7.4）：宿主需要知道"是因为端点无法确认"
    还是"因为能力未声明"，而不是一个笼统的"没有可用候选"。自动模式才用
    ``ROUTE_NO_ELIGIBLE`` + ``gaps`` 汇总。
    """
    if plan["decision"] == "selected":
        return
    if plan["decision"] == "no_eligible":
        if plan.get("mode") == "explicit" and len(plan.get("candidates") or []) == 1:
            candidate = plan["candidates"][0]
            if candidate.get("code"):
                raise Error(
                    candidate["code"],
                    candidate.get("detail") or "the requested profile is not eligible",
                    details={
                        "profile_id": candidate["profile_id"],
                        "reason": candidate.get("detail"),
                        "task_type": plan.get("task_type"),
                        "gaps": plan.get("gap_codes"),
                    },
                )
        # 数据去向无法确认是"拒绝派发"的独立理由，不是"没有候选"。只要每个已考虑
        # 的候选都因为端点被排除，就要报 `ENDPOINT_UNVERIFIED`（§19-6）；而成本
        # 上限这类"配置收窄"仍应保持 `ROUTE_NO_ELIGIBLE`（§19-22）。
        if plan.get("candidates") and all(
            candidate.get("code") == "ENDPOINT_UNVERIFIED" for candidate in plan["candidates"]
        ):
            raise Error(
                "ENDPOINT_UNVERIFIED",
                "data_residency is required but no candidate can prove its endpoint",
                details={
                    "task_type": plan.get("task_type"),
                    "profiles": [item["profile_id"] for item in plan["candidates"]],
                    "gaps": plan.get("gap_codes"),
                },
            )
        raise Error(
            "ROUTE_NO_ELIGIBLE",
            "no candidate execution combination satisfies the requirements",
            details={
                "task_type": plan.get("task_type"),
                "gaps": plan.get("gap_codes"),
                "allowed_profiles": plan.get("candidate_ids"),
            },
        )
    raise Error(
        "ROUTE_AMBIGUOUS",
        "several eligible combinations and no usable preference",
        details={
            "task_type": plan.get("task_type"),
            "options": plan.get("options"),
            "fallback": plan.get("fallback_used"),
            "reason": plan.get("reason"),
        },
    )


# --------------------------------------------------------------------------- #
# 模型观测（§9）
# --------------------------------------------------------------------------- #


def model_identity(
    requested: str | None,
    reported_model,
    reported_models: list | None = None,
    aliases=None,
) -> dict:
    """Judge identity from *reported* facts only; never infer a match (§9.1).

    三类观测分开处理，因为它们证明的事情不同：

    * ``request_echo``（``system``/``init`` 回显）只证明"请求了什么"，不参与判定；
    * ``main_call`` / ``final_result`` 才是"用了什么"的观测；
    * ``aliases`` 是 profile 显式声明的等价名（请求名与供应商名的映射）。全部
      观测都落在 ``{requested} ∪ aliases`` 里但并非逐字相等时，结论是
      ``alias_confirmed`` —— 一个**可审计的声明**，不是一次测量。
    """
    entries = [e for e in (reported_models or []) if isinstance(e, dict) and e.get("model")]
    usage = [e for e in entries if e.get("scope") != "request_echo"]
    models = []
    for entry in usage:
        if entry["model"] not in models:
            models.append(entry["model"])
    if not models and not entries and reported_model:
        # 老适配器只填 ``reported_model``，没有作用域信息：当作终态观测。
        models.append(reported_model)
    if not models:
        return {
            "requested_model": requested,
            "reported_model": None,
            "observation_scope": "none",
            "model_identity": "unobserved",
            "identity_basis": "none",
            "reported_models": entries,
            "request_echoes": [e["model"] for e in entries],
            "accepted_models": [],
            "unmatched_models": [],
        }
    # 去重保序：别名集合里重复写一遍请求名不应该出现在 ``accepted_models`` 里两次。
    accepted = list(dict.fromkeys([requested, *(aliases or [])])) if requested else []
    scopes = {e.get("scope") for e in usage if e.get("scope")}
    scope = "mixed" if len(scopes) > 1 else (next(iter(scopes)) if scopes else "final_result")
    if requested is None:
        # 没有请求值可比对：报告"未解析"，不能因为"只看到一个模型"就称匹配。
        identity, basis = "unobserved", "unresolved_request"
    elif all(model in accepted for model in models):
        if models == [requested]:
            identity, basis = "reported_match", "exact"
        else:
            # 上报值与请求值不同，但都在声明的等价集合里。
            identity, basis = "alias_confirmed", "declared_alias"
    elif len(models) > 1:
        identity, basis = "mixed", "mixed"
        # 多个模型就是"混合观测"，scope 也必须这样说，否则读者会以为这只是
        # 某一次调用的观测。
        scope = "mixed"
    else:
        identity, basis = "reported_mismatch", "mismatch"
    return {
        "requested_model": requested,
        # 终态观测只取"运行事实"；请求回显不能冒充结果。
        "reported_model": reported_model if reported_model in models else models[0],
        "observation_scope": scope,
        "model_identity": identity,
        "identity_basis": basis,
        "reported_models": entries,
        "request_echoes": [e["model"] for e in entries if e.get("scope") == "request_echo"],
        "accepted_models": accepted,
        "unmatched_models": [model for model in models if model not in accepted],
    }


def pin_blocks(requirements: dict, identity: str) -> bool:
    """``pin_model`` 只在 ``reported_match`` / ``alias_confirmed`` 时放行 (§9.3)。

    ``alias_confirmed`` 的前提是 profile 显式声明了 ``model_aliases``：它证明
    "工具报的每个值都在我声明的同一个模型集合里"。它与逐字相等不是一回事，所以
    结论、``identity_basis`` 与报告措辞都分开；但两者都能满足 pin——否则在
    "请求名 ≠ 供应商名"的 CLI 上，pin 永远不可能满足，只能被关闭。
    """
    if not requirements.get("pin_model"):
        return False
    return identity not in ("reported_match", "alias_confirmed")


def cost_observation(usage: dict, profile: dict) -> dict:
    """Three cost sources, never mixed (§12.7.1).

    ``reported`` / ``estimated`` / ``unavailable``；前两者可用于护栏，
    ``unavailable`` **不可**（配了上限时必须显式降级，不得静默放行）。
    """
    usage = usage or {}
    reported = usage.get("cost")
    if isinstance(reported, int | float) and not isinstance(reported, bool):
        return {"amount": float(reported), "cost_source": "reported"}
    price = profile.get("unit_price") or {}
    inputs, outputs = usage.get("input_tokens"), usage.get("output_tokens")
    if (
        isinstance(inputs, int | float)
        and isinstance(outputs, int | float)
        and (price.get("input_per_1k") is not None or price.get("output_per_1k") is not None)
    ):
        amount = float(inputs) / 1000 * float(price.get("input_per_1k") or 0) + float(
            outputs
        ) / 1000 * float(price.get("output_per_1k") or 0)
        return {"amount": amount, "cost_source": "estimated"}
    return {"amount": None, "cost_source": "unavailable"}


def spend_limits(c: dict) -> dict:
    guard = (c.get("routing") or {}).get("spend_guard") or {}
    return {
        "max_cost_per_task": guard.get("max_cost_per_task"),
        "max_cost_per_day": guard.get("max_cost_per_day"),
        "currency": guard.get("currency"),
    }


def spend_totals(store, t: dict) -> dict:
    """Sum observed spend for this task and for this project's natural day.

    Read-only. Only runs that recorded a spend amount participate; ``unavailable``
    runs are counted separately so "no data" is visible (§12.7.1).
    """
    day_start = _day_start(time.time())
    task_total = 0.0
    day_total = 0.0
    unavailable = 0
    for task in store.tasks():
        if task["project_id"] != t["project_id"]:
            continue
        for run in store.runs(task["task_id"]):
            spend = run.get("spend") or {}
            amount = spend.get("amount")
            if amount is None:
                if run.get("spend"):
                    unavailable += 1
                continue
            if task["task_id"] == t["task_id"]:
                task_total += float(amount)
            if float(run.get("started_at") or 0) >= day_start:
                day_total += float(amount)
    return {
        "task_total": task_total,
        "day_total": day_total,
        "unavailable_runs": unavailable,
    }


def _day_start(now: float) -> float:
    import datetime

    dt = datetime.datetime.fromtimestamp(now, datetime.UTC)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def spend_gate(store, t: dict, c: dict, observation: dict) -> dict:
    """Decide whether this run may continue, **after** the spend was recorded.

    配了上限而本轮 ``cost_source == unavailable`` → ``SPEND_UNVERIFIABLE``：
    护栏不能只在有数据时才存在，而无数据恰恰是最需要护栏的场景。
    """
    limits = spend_limits(c)
    configured = [k for k in ("max_cost_per_task", "max_cost_per_day") if limits[k] is not None]
    totals = spend_totals(store, t)
    if not configured:
        return {"blocked": False, "limits": limits, **totals}
    if observation.get("cost_source") == "unavailable":
        raise Error(
            "SPEND_UNVERIFIABLE",
            "cost is not observable, so the spend guard cannot be evaluated; declare "
            "unit_price for this combination or use a tool that reports cost",
            details={
                "limit": {k: limits[k] for k in configured},
                "scope": "task",
                "reason": "cost_source=unavailable",
            },
        )
    currency = limits.get("currency")
    profile_currency = ((c.get("agent_profiles") or {}).get(t.get("agent_profile_id")) or {}).get(
        "unit_price", {}
    ) or {}
    if currency and profile_currency.get("currency") and currency != profile_currency["currency"]:
        # 币种不可比时不能用"数字大小"冒充比较。
        raise Error(
            "SPEND_UNVERIFIABLE",
            "spend_guard.currency is {!r} but unit_price.currency is {!r}; the amounts are not "
            "comparable".format(currency, profile_currency["currency"]),
            details={
                "limit": {k: limits[k] for k in configured},
                "scope": "task",
                "reason": "currency_mismatch",
            },
        )
    per_task = limits.get("max_cost_per_task")
    if per_task is not None and totals["task_total"] > float(per_task):
        raise Error(
            "SPEND_LIMIT_EXCEEDED",
            f"task spend {totals['task_total']:.4f} exceeded max_cost_per_task {per_task}",
            details={
                "limit": per_task,
                "scope": "task",
                "observed": round(totals["task_total"], 6),
                "cost_source": observation.get("cost_source"),
            },
        )
    per_day = limits.get("max_cost_per_day")
    if per_day is not None and totals["day_total"] > float(per_day):
        raise Error(
            "SPEND_LIMIT_EXCEEDED",
            f"daily spend {totals['day_total']:.4f} exceeded max_cost_per_day {per_day}",
            details={
                "limit": per_day,
                "scope": "day",
                "observed": round(totals["day_total"], 6),
                "cost_source": observation.get("cost_source"),
            },
        )
    return {"blocked": False, "limits": limits, **totals}


# --------------------------------------------------------------------------- #
# 归因（§10）
# --------------------------------------------------------------------------- #


def failure_class_of(code: str | None) -> str | None:
    if not code:
        return None
    return CODE_FAILURE_CLASS.get(code, "executor")


def attribution(
    *,
    outcome: str | None,
    failure_class: str | None,
    model_identity_value: str | None,
    purpose: str,
    block_code: str | None = None,
    model_unresolved: bool | None = None,
) -> dict:
    """Record *why* a run does or does not enter a metric (§14.1).

    v1.5.0：新增 ``model_capability_comparable``。此前"原因已确认"与"模型身份可
    归属"被合成一个布尔，于是 (a) 身份未知的**成功**运行、(b) 环境失败 都被判
    成可比。这里拆成三个独立条件，任一不成立就不进"身份与归因明确样本"口径：

    - ``identity_attributable``：工具报告了模型且与请求**逐字**一致，且启动前已固定模型
      （``alias_confirmed`` 只说明"落在声明等价集合内"，不算逐字一致）；
    - ``result_evaluable``：有明确结局，且失败类别不是环境/基础设施/权限/规格；
    - ``cause_attributed``：失败原因能归到执行器或验收，而不是兜底码。

    ``AGENT_FAILED`` 是兜底码：模型 ID 写错、CLI 拒绝、实现失败在输出上一样。
    归因未确认就不进能力比较，但失败事实、调用次数、耗时与已观测费用照常保留。
    """
    identity_attributable = (
        model_identity_value == "reported_match" and model_unresolved is not True
    )
    result_evaluable = outcome is not None and failure_class not in EXCLUDED_CLASSES
    # 成功运行没有"失败原因"要归因，这一条自动成立；失败时必须能指到执行器或
    # 验收，兜底码 ``AGENT_FAILED`` 不算已归因。
    cause_attributed = failure_class is None or (
        failure_class in ("executor", "verification") and block_code != "AGENT_FAILED"
    )
    comparable = (
        purpose != "probe" and identity_attributable and result_evaluable and cause_attributed
    )

    def record(attempt: str, delivery: str, reason: str) -> dict:
        return {
            "attempt_metrics": attempt,
            "delivery_metrics": delivery,
            "reason": reason,
            "model_capability_comparable": comparable,
            "identity_attributable": identity_attributable,
            "cause_attributed": cause_attributed,
        }

    if purpose == "probe":
        return record(
            "excluded", "excluded", "purpose=probe; probe results are integration facts only"
        )
    if failure_class in EXCLUDED_CLASSES:
        return record(
            "excluded", "excluded", f"failure_class={failure_class} is not the executor's result"
        )
    if model_identity_value in ("unobserved", "mixed", "reported_mismatch"):
        return record(
            "counted",
            "excluded",
            f"model_identity={model_identity_value}; no quality conclusion by combination",
        )
    if block_code == "AGENT_FAILED":
        # 失败事实保留（attempt 仍然计数），但不进能力比较：这个码不说明是模型
        # 能力不足，还是模型/端点/凭据写错。
        return record(
            "counted",
            "excluded",
            "AGENT_FAILED is a catch-all code; cause unconfirmed, "
            "excluded from the attributed-sample pass rate",
        )
    delivered = outcome == "completed" and failure_class is None
    if model_identity_value == "alias_confirmed":
        # 观测值落在声明等价集合内：能证明"没跑到集合外"，不能证明逐字一致。
        # 交付事实照常计数，但不进"身份与归因明确"的能力比较——否则一次声明
        # 就会被读成一次测量。
        return record(
            "counted",
            "counted" if delivered else "excluded",
            "model_identity=alias_confirmed; reported ids match declared model_aliases, "
            "exact identity unconfirmed",
        )
    return record(
        "counted",
        "counted" if delivered else "excluded",
        "evaluable run" if delivered else f"failure_class={failure_class}",
    )


def spec_scale(ac_count: int) -> str:
    """规格规模桶，**不是难度**（§20.1）。"""
    if ac_count <= 3:
        return "S"
    if ac_count <= 8:
        return "M"
    return "L"


# --------------------------------------------------------------------------- #
# 只读统计（PerformanceRecord v2，§14.3）
# --------------------------------------------------------------------------- #


def performance_record(store, project_id: str, *, window_days: int = 30, clock=None) -> dict:
    """Aggregate runs/tasks by ``combination_id × task_type × spec_scale``.

    只读、固定截止点、不读未结束的 run；不影响任何决策（C9）。
    """
    now = float(clock if clock is not None else time.time())
    since = now - float(window_days) * 86400
    counts = {
        "runs_evaluable": 0,
        "runs_passed": 0,
        "runs_excluded_environment": 0,
        "runs_excluded_infrastructure": 0,
        "runs_excluded_auth": 0,
        "runs_excluded_spec": 0,
        "runs_unfinished": 0,
        "runs_cancelled": 0,
        "runs_identity_unobserved": 0,
        # 声明等价（alias_confirmed）：观测到了，但不是逐字一致。单列，避免被
        # 读成"身份已确认"。
        "runs_identity_alias": 0,
        # v1.5.0：归因未确认的 run（兜底码 ``AGENT_FAILED``）。它们是真实失败，
        # 只是不足以说明是模型能力不足，所以保留事实、不进能力比较。
        "runs_cause_unconfirmed": 0,
        # Runs recorded before v1.2 have neither attribution nor model
        # observation; they are counted here instead of propping up the buckets.
        "runs_pre_v12": 0,
        "tasks_assigned": 0,
        "tasks_accepted": 0,
        "tasks_pending_acceptance": 0,
        "tasks_cancelled": 0,
    }
    buckets: dict[tuple, dict] = {}
    not_comparable: list[dict] = []
    assignments_considered = 0

    for task in store.tasks():
        if task["project_id"] != project_id:
            continue
        created = float(task.get("created_at") or 0)
        if created < since:
            continue
        counts["tasks_assigned"] += 1
        if task.get("state") == "DONE":
            counts["tasks_accepted"] += 1
        elif task.get("state") == "CANCELLED":
            counts["tasks_cancelled"] += 1
        elif task.get("state") == "REVIEW_READY":
            counts["tasks_pending_acceptance"] += 1
        for run in store.runs(task["task_id"]):
            # 任务本身已在窗口内，缺时间戳的 run 不应被静默丢掉（那会让统计
            # 只留下"恰好写了 started_at 的那部分样本"）。只有**确知**更早才排除。
            started = run.get("started_at") or run.get("created_at") or run.get("at")
            if started is not None and float(started) < since:
                continue
            if run.get("status") in ("reserved", "starting", "running", "unknown"):
                counts["runs_unfinished"] += 1
                continue
            attribution = run.get("attribution") or {}
            if not attribution:
                # v1.2 之前的 run 没有归因记录，也没有模型观测。把它们算进质量
                # 结论等于用"没测过"支撑"看起来不错"，所以单列并排除。
                counts["runs_pre_v12"] += 1
                continue
            if attribution.get("attempt_metrics") == "excluded":
                failure = run.get("failure_class")
                if failure == "environment":
                    counts["runs_excluded_environment"] += 1
                elif failure == "infrastructure":
                    counts["runs_excluded_infrastructure"] += 1
                elif failure == "auth_permission":
                    counts["runs_excluded_auth"] += 1
                elif failure == "spec":
                    counts["runs_excluded_spec"] += 1
                continue
            if run.get("model_identity") in ("unobserved", "mixed"):
                counts["runs_identity_unobserved"] += 1
            elif run.get("model_identity") == "alias_confirmed":
                counts["runs_identity_alias"] += 1
            counts["runs_evaluable"] += 1
            assignments_considered += 1
            # 指标 A 的分子是"独立验证通过"，而不是"agent 退出码为 0"：
            # 退出码 0 但被闸门拦下 / check 失败的 run 不算通过。
            verified = bool((run.get("verification") or {}).get("passed"))
            if verified:
                counts["runs_passed"] += 1
            key = (
                run.get("combination_id") or "cmb-unknown",
                run.get("task_type") or "unknown",
                run.get("spec_scale") or "S",
            )
            bucket = buckets.setdefault(
                key,
                {
                    "combination_id": key[0],
                    "profile_id": run.get("profile_id"),
                    "execution": {
                        "adapter_id": run.get("adapter_id"),
                        "backend": run.get("adapter_backend"),
                        "cli_version": run.get("cli_version"),
                        "model": run.get("requested_model"),
                        "reasoning_effort": (run.get("frozen_execution") or {}).get(
                            "reasoning_effort"
                        ),
                        "endpoint": (run.get("endpoint") or {}).get("id"),
                    },
                    "task_type": key[1],
                    "spec_scale": key[2],
                    "attempt": {
                        "n": 0,
                        "passed": 0,
                        "failed": 0,
                        "pass_rate": None,
                        # v1.5.0：身份与归因明确样本的口径。`pass_rate` 是全量
                        # 运维指标，`pass_rate_attributed` 才用于能力比较；分母为
                        # 0 时是 null，不是 0% 或 100%。
                        "n_attributed": 0,
                        "passed_attributed": 0,
                        "pass_rate_attributed": None,
                    },
                    "delivery": {
                        "n": 0,
                        "accepted": 0,
                        "delivery_rate": None,
                        "pending_acceptance": 0,
                    },
                    "rework": {"assignments_per_task": None, "extra_attempts": 0},
                    "wall_clock": {"median_seconds": None, "p95_seconds": None},
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost": None,
                        "cost_source": None,
                    },
                    "spend": {
                        "reported": 0.0,
                        "estimated": 0.0,
                        "unavailable_runs": 0,
                        "breaches": 0,
                    },
                    "reliability": {
                        "model_mismatch_runs": 0,
                        "executor_failures": 0,
                        "executor_failures_unconfirmed": 0,
                    },
                    "_seconds": [],
                },
            )
            bucket["attempt"]["n"] += 1
            if verified:
                bucket["attempt"]["passed"] += 1
            # v1.5.0：只有身份与归因都明确的 run 才进能力比较口径。老记录没有
            # 这个字段时按"未知"处理，绝不默认 confirmed。
            if attribution.get("model_capability_comparable") is True:
                bucket["attempt"]["n_attributed"] += 1
                if verified:
                    bucket["attempt"]["passed_attributed"] += 1
            usage = (run.get("agent_result") or {}).get("usage") or {}
            for field in ("input_tokens", "output_tokens"):
                if isinstance(usage.get(field), int | float):
                    bucket["usage"][field] += usage[field]
            spend = run.get("spend") or {}
            source = spend.get("cost_source")
            if source == "reported":
                bucket["spend"]["reported"] += float(spend.get("amount") or 0)
                bucket["usage"]["cost"] = bucket["spend"]["reported"]
                bucket["usage"]["cost_source"] = "reported"
            elif source == "estimated":
                bucket["spend"]["estimated"] += float(spend.get("amount") or 0)
                if bucket["usage"]["cost_source"] is None:
                    bucket["usage"]["cost_source"] = "estimated"
            elif source == "unavailable":
                bucket["spend"]["unavailable_runs"] += 1
            if run.get("block_code") in ("SPEND_LIMIT_EXCEEDED", "SPEND_UNVERIFIABLE"):
                bucket["spend"]["breaches"] += 1
            if run.get("model_identity") == "reported_mismatch":
                bucket["reliability"]["model_mismatch_runs"] += 1
            if run.get("failure_class") == "executor":
                bucket["reliability"]["executor_failures"] += 1
                if attribution.get("cause_attributed") is not True:
                    bucket["reliability"]["executor_failures_unconfirmed"] += 1
                    counts["runs_cause_unconfirmed"] += 1
                    not_comparable.append(
                        {
                            "combination_id": key[0],
                            "n": 1,
                            "reason": "failure_class=executor but the cause is unconfirmed "
                            "(catch-all code); excluded from the attributed-sample rate",
                        }
                    )
            started, ended = run.get("started_at"), run.get("ended_at")
            if started and ended:
                bucket["_seconds"].append(float(ended) - float(started))
            if attribution.get("delivery_metrics") == "counted":
                bucket["delivery"]["n"] += 1
                if (task.get("acceptance") or {}).get("decision") == "accepted":
                    bucket["delivery"]["accepted"] += 1
            if task.get("state") == "REVIEW_READY":
                bucket["delivery"]["pending_acceptance"] += 1
            if run.get("model_identity") in ("unobserved", "mixed"):
                not_comparable.append(
                    {
                        "combination_id": key[0],
                        "n": 1,
                        "reason": f"model_identity={run.get('model_identity')}; "
                        "excluded from quality comparison",
                    }
                )
            elif run.get("model_identity") == "alias_confirmed":
                # 与 unobserved 分开记：原因不同，读者不该看到两句一样的话。
                not_comparable.append(
                    {
                        "combination_id": key[0],
                        "n": 1,
                        "reason": "model_identity=alias_confirmed; reported ids match "
                        "declared model_aliases, exact identity unconfirmed",
                    }
                )

    for bucket in buckets.values():
        seconds = sorted(bucket.pop("_seconds"))
        attempt = bucket["attempt"]
        # `failed` 与 `passed` 并列写出：手工复算 pass_rate 时不必再猜
        # "n - passed 里有多少是失败、多少是没跑完"。
        attempt["failed"] = attempt["n"] - attempt["passed"]
        attempt["pass_rate"] = round(attempt["passed"] / attempt["n"], 4) if attempt["n"] else None
        # v1.5.0：能力比较只用这一条。分母为 0 时写 null——0% 会被读成"全失败"，
        # 100% 会被读成"全通过"，两者都不是"没有可比样本"。
        attempt["pass_rate_attributed"] = (
            round(attempt["passed_attributed"] / attempt["n_attributed"], 4)
            if attempt["n_attributed"]
            else None
        )
        delivery = bucket["delivery"]
        delivery["delivery_rate"] = (
            round(delivery["accepted"] / delivery["n"], 4) if delivery["n"] else None
        )
        if seconds:
            bucket["wall_clock"]["median_seconds"] = int(seconds[len(seconds) // 2])
            index = min(len(seconds) - 1, int(len(seconds) * 0.95))
            bucket["wall_clock"]["p95_seconds"] = int(seconds[index])
    merged: dict[tuple, dict] = {}
    for item in not_comparable:
        # v1.5.0：按 (组合, 原因) 分组。只按组合合并会把"身份未观测"和"原因未确认"
        # 压成一条、只留下先到的那句 reason——排除数量还在，原因却没了。
        key = (item["combination_id"], item["reason"])
        merged.setdefault(key, {"combination_id": key[0], "reason": key[1], "n": 0})["n"] += 1
    return {
        "schema_version": 2,
        "project_id": project_id,
        "generated_at": now,
        "as_of": now,
        "window": {"since": since, "assignments_considered": assignments_considered},
        "counts": counts,
        "buckets": sorted(buckets.values(), key=lambda b: (b["combination_id"], b["task_type"])),
        "not_comparable": sorted(
            merged.values(), key=lambda item: (item["combination_id"], item["reason"])
        ),
        "used_for_selection": False,
        "note": "read-only aggregates; v1.2 never uses them to exclude or order candidates",
    }
