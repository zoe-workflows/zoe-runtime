"""Agent profiles: config normalization, ``(adapter, backend)`` binding, evidence.

The project configuration is normalized into an ``agent_profiles`` map. A v1
configuration (``"agent": "codebuddy"``) is migrated in memory into a
``legacy-codebuddy`` profile so existing installations keep working, but the
normalized form is what gets frozen into the task and hashed into the policy
revision.

Capability is decided per ``(adapter_id, backend)`` from a *verification record*:
either shipped in the registry (none in this build, which has no real e2e) or
declared by the operator through ``compatibility_evidence``. A record always
carries an exact version, so "close enough" can never unlock a backend.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src._agents import base as agent_base
from src._agents import registry
from src._agents.base import (
    CAPABILITY_VERIFIED,
    PROMPT_LIMIT_BYTES,
    LaunchSpec,
    validate_typed_options,
)
from src._util import Error, digest, is_integer, require, safe_id

#: Fields a profile may declare. Anything else is a typo or an attempt to pass
#: an untyped, controller-reserved parameter.
#:
#: v1.2 §4.3 drift point 1: this is now an alias of the canonical set that lives
#: next to ``validate_profile`` — the two lists used to be maintained by hand in
#: two files and could drift apart silently.
PROFILE_FIELDS = agent_base.PROFILE_FIELDS

#: Legacy ``agent_flags`` entries that may be migrated to typed options.
LEGACY_FLAG_MAP = {
    "--model": "model",
    "--max-turns": "max_turns",
}

#: F5 (TGD §5.5): how a profile authenticates. Absent means ``unknown`` — and
#: ``unknown`` may be dispatched, but the report must say "认证态未知", never
#: "已认证". Only ``api_key`` adds a hard precondition (``credential_refs``).
AUTH_MODES = ("cli_login", "api_key", "unknown")

#: What a pre-flight check may honestly claim about a CLI login. ``present``
#: requires one successful ``agent probe`` in this environment; nothing here
#: guesses it from the presence of a config directory.
LOGIN_STATES = ("unknown", "present", "absent")


def auth_mode(profile: dict) -> str:
    return profile.get("auth_mode") or "unknown"


@dataclass(frozen=True)
class ProfileBinding:
    """A validated ``(profile, adapter, backend)`` triple with its capability state."""

    profile_id: str
    profile: dict
    adapter: agent_base.AgentAdapter
    entry: registry.AdapterEntry
    backend: str
    capability: str
    capability_reason: str | None
    verification: registry.Verification | None
    profile_hash: str
    env_names: tuple[str, ...]
    options: dict
    #: Whether the named ``permission_profile`` reaches the command line at all.
    permission: dict = field(default_factory=dict)
    #: v1.2 §6.3：已声明的执行字段是否都能落到有效配置（不能则构造期就拒绝）。
    execution: dict = field(default_factory=dict)

    @property
    def adapter_id(self) -> str:
        return self.entry.adapter_id

    @property
    def unverified(self) -> bool:
        """该 (adapter, backend) 是否有真实 e2e 记录。

        默认策略下它不是闸门，而是标注：派发照常进行，但响应、run 记录和报告
        必须显式写明"未验证"。
        """
        return self.capability != registry.CAPABILITY_VERIFIED

    @property
    def trust_level(self) -> str:
        """``verified``（真实 e2e）/ ``declared``（管理员声明）/ ``unverified``。"""
        if self.capability != registry.CAPABILITY_VERIFIED:
            return "unverified"
        source = self.verification.source if self.verification else None
        return "verified" if source == "real_e2e" else "declared"

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "adapter_id": self.adapter_id,
            "adapter_kind": self.entry.kind,
            "backend": self.backend,
            "capability": self.capability,
            "capability_reason": self.capability_reason,
            "unverified": self.unverified,
            "trust_level": self.trust_level,
            "verification": self.verification.to_dict() if self.verification else None,
            "expected_version": self.profile.get("expected_version"),
            "profile_hash": self.profile_hash,
            "output_protocol": self.adapter.output_protocol,
            "permission": self.permission or {},
            "facts_status": getattr(self.adapter, "facts_status", "VERIFIED_FORM"),
            # v1.2: the execution combination is what actually runs, so it is the
            # first thing a reader needs — and "no model" must look unresolved,
            # not merely absent.
            "model": self.profile.get("model"),
            "provider": self.profile.get("provider"),
            "model_unresolved": model_unresolved(self.profile),
            "cost_class": self.profile.get("cost_class") or "standard",
            "endpoint_ref": self.profile.get("endpoint_ref"),
            "execution": self.execution or {},
        }


def model_unresolved(profile: dict) -> bool:
    """Whether "which model ran" is unknown for this profile (v1.2 §6.1.1).

    Two declared ways to reach this state: ``allow_cli_default_model`` (explicit
    escape hatch) and the migrated legacy v1 profile (no ``agent_profiles`` at
    all). Both mean the same thing for evidence: ``combination_verified`` is
    always ``false`` and the report must say "运行模型未解析".
    """
    return profile.get("model") is None and (
        profile.get("allow_cli_default_model") is True or profile.get("legacy") is True
    )


def profile_hash(profile_id: str, profile: dict) -> str:
    return digest({"profile_id": profile_id, "profile": profile})


def policy_digest(c: dict) -> str:
    """Digest of a *normalized* policy.

    Task records created before v0.3 froze a v1 configuration without profile
    fields; normalizing a copy here keeps the "did the policy change?" question
    about semantics rather than about when the record happened to be written.
    """
    copy = json.loads(json.dumps(c))
    try:
        return digest(normalize_config(copy))
    except Error:
        return digest(c)


def _absolutize(value: str, base_dir: Path | None) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = (Path(base_dir) / path).resolve()
    return str(path)


def migrate_legacy_flags(flags: list[str]) -> dict:
    """Map a v1 ``agent_flags`` array onto typed options, or refuse with advice."""
    options: dict[str, Any] = {}
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag not in LEGACY_FLAG_MAP:
            raise Error(
                "POLICY_DENIED",
                f"agent_flags entry {flag!r} cannot be migrated to a typed option. "
                "Remove it and use model / max_turns / permission_profile / "
                "approved_tools instead.",
            )
        require(
            index + 1 < len(flags),
            "INPUT_INCOMPLETE",
            f"agent_flags entry {flag!r} requires a value",
        )
        key, value = LEGACY_FLAG_MAP[flag], flags[index + 1]
        options[key] = int(value) if key == "max_turns" else value
        index += 2
    return options


def _legacy_profile(c: dict, base_dir: Path | None) -> dict[str, Any]:
    adapter_id = c.get("agent", "codebuddy")
    entry = registry.get(adapter_id)
    adapter = entry.factory() if entry else None
    options = migrate_legacy_flags(list(c.get("agent_flags") or []))
    profile = {
        "adapter": adapter_id,
        "backend": c.get("agent_backend") or (adapter.default_backend if adapter else None),
        "image": c.get("image"),
        "expected_version": c.get("agent_version") or None,
        "permission_profile": c.get("permission_profile", "isolated-edit"),
        "credential_refs": list(c.get("agent_env") or []),
        "network_profile": c.get("network_profile", c.get("network", "none")),
        "legacy": True,
        "compatibility_verified": c.get("compatibility_verified") is True,
        **options,
    }
    if c.get("compatibility_evidence"):
        profile["compatibility_evidence"] = _absolutize(c["compatibility_evidence"], base_dir)
    return profile


def _normalize_profile(
    c: dict[str, Any],
    profile_id: str,
    profile: Any,
    base: Path | None,
    *,
    supplied: bool,
    legacy_allowed: bool,
) -> dict[str, Any]:
    """Normalize and validate **one** ``agent_profiles`` entry.

    从 ``normalize_config`` 的循环体析出：读路径（``strict=False``）需要逐个
    profile 收集问题，而不是在第一个坏 profile 处停下——否则 ``doctor`` 一次
    只报一条，用户要修 N 轮才知道全部问题（v1.5.0 M1 的目标）。
    """
    safe_id(profile_id)
    require(isinstance(profile, dict), "INPUT_INCOMPLETE", "Profile must be an object")
    unknown = set(profile) - PROFILE_FIELDS
    require(
        not unknown,
        "INPUT_INCOMPLETE",
        "Unknown profile fields in {}: {}".format(profile_id, ", ".join(sorted(unknown))),
    )
    adapter_id = profile.get("adapter")
    require(
        isinstance(adapter_id, str) and adapter_id,
        "INPUT_INCOMPLETE",
        f"Profile {profile_id} requires an adapter",
    )
    entry = registry.get(adapter_id)
    require(
        entry is not None,
        "ADAPTER_UNVERIFIED",
        f"Profile {profile_id} references an unregistered adapter {adapter_id!r}",
    )
    adapter = entry.factory()
    value = dict(profile)
    if value.get("backend") is None:
        value["backend"] = adapter.default_backend
    value.setdefault("image", c.get("image"))
    if value.get("compatibility_evidence"):
        value["compatibility_evidence"] = _absolutize(value["compatibility_evidence"], base)
    # v0.5.0: no container backend, so no image-digest precondition. The
    # `image` field itself stays readable so old configurations still parse
    # and get an explicit refusal instead of a schema error (§19.4).
    network = value.get("network_profile", c.get("network", "none"))
    if network not in (None, "none"):
        require(
            c.get("network_policy_confirmed") is True,
            "POLICY_DENIED",
            f"Profile {profile_id} uses network {network!r}; administrator confirmation required",
        )
    value["network_profile"] = network
    # v1.5.0 M7：v2 配置里的 profile 一律不得声明 legacy。校验放在 model 必填
    # 之**前**，否则报错会变成"必须声明 model"，掩盖绕过意图。
    if supplied and not legacy_allowed:
        require(
            value.get("legacy") is not True,
            "POLICY_DENIED",
            f"Profile {profile_id}: 'legacy' is controller-reserved and may not be "
            "declared; it exists only for configurations migrated from schema v1. "
            "Declare model, or set allow_cli_default_model=true explicitly.",
        )
    # v1.2 §6.1.1：`model` 必填。缺省时适配器根本不传 `--model`，用户等于
    # 批准了一个他无法描述的组合（多数 CLI 的默认模型是旗舰档）。旧式
    # v1 配置（顶层 `agent`）是刻意保留的迁移路径：它在记录里显式标
    # `legacy`，因此 `model_unresolved` 恒为 true、`combination_verified`
    # 恒为 false，报告必须写"运行模型未解析"——不是静默放行。
    if not value.get("legacy"):
        model = value.get("model")
        # v1.5.0 M2：形态校验。空串与纯空白都必须拒绝——`bool("   ")` 为真，
        # 只靠"非空"会把空白模型放进 argv；前后空白也拒绝而不是静默 trim，
        # 否则"配置里写的"与"实际传的"会是两个不同的字符串。
        if model is not None:
            require(
                isinstance(model, str) and bool(model) and model.strip() == model,
                "INPUT_INCOMPLETE",
                f"Profile {profile_id}: model must be a non-empty string without "
                "leading or trailing whitespace",
            )
        # v1.5.0 M2：类型必须先校验。否则 ``"true"``（字符串）会被当成
        # ``False`` 处理——一个写错的配置看起来像"显式模型"，而且不报错。
        declared_flag = value.get("allow_cli_default_model")
        require(
            declared_flag is None or isinstance(declared_flag, bool),
            "INPUT_INCOMPLETE",
            f"Profile {profile_id}: allow_cli_default_model must be a boolean",
        )
        allow_default = declared_flag is True
        require(
            bool(model) or allow_default,
            "INPUT_INCOMPLETE",
            f"Profile {profile_id} must declare model, or explicitly set "
            "allow_cli_default_model=true to follow the CLI default",
        )
        # v1.5.0 M2 第 3 行：两个都写时该标志会被静默忽略。本项目的纪律是
        # "诚实拒绝优于接受后忽略"（见 _routing 里 use_performance 的处理）。
        require(
            not (model and allow_default),
            "INPUT_INCOMPLETE",
            f"Profile {profile_id} declares both model and "
            "allow_cli_default_model=true; the flag would be silently ignored. "
            "Remove the flag to pin the model, or remove model to follow the CLI "
            "default (model_unresolved=true).",
        )
    # F5：`auth_mode` 只在声明时校验，不回写默认值（否则未改动的配置
    # `policy_digest` 会变）。`api_key` 是唯一带硬前提的取值：没有
    # `credential_refs` 就是配置缺陷，注册期即拒绝（TGD §5.5）。
    declared_auth = value.get("auth_mode")
    if declared_auth is not None:
        require(
            declared_auth in AUTH_MODES,
            "INPUT_INCOMPLETE",
            "Profile {}: auth_mode must be one of {}".format(profile_id, ", ".join(AUTH_MODES)),
        )
        if declared_auth == "api_key":
            refs = value.get("credential_refs")
            require(
                isinstance(refs, list) and refs,
                "AUTH_REQUIRED",
                f"Profile {profile_id} declares auth_mode=api_key but no "
                "credential_refs; declare the environment variable names it needs",
            )
    return value


def _collect_section_problems(c: dict[str, Any], problems: list[dict]) -> None:
    """读路径专用：某个 profile 坏掉时，仍收集段级（routing/endpoints）问题。

    在副本上跑规范化，避免把半规范化结果写回调用方的配置——失败时 ``c`` 必须
    保持原样，否则摘要语义会变（"非法配置"不该表现为"策略漂移"）。
    """
    from src import _routing

    snapshot = json.loads(json.dumps(c))
    for step in (_routing.normalize_endpoints, _routing.normalize_routing):
        try:
            step(snapshot)
        except Error as exc:
            problems.append({"code": exc.code, "field": None, "message": str(exc)})


def normalize_config(
    c: dict[str, Any],
    base_dir: Path | str | None = None,
    *,
    problems: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalize a v1 or v2 project configuration in place and return it."""
    require(
        is_integer(c.get("schema_version")) and c.get("schema_version") in (1, 2),
        "INPUT_INCOMPLETE",
        f"Config schema_version must be 1 or 2, got {c.get('schema_version')!r}",
    )
    base = Path(base_dir) if base_dir is not None else None
    profiles = c.get("agent_profiles")
    # v1.5.0 M7：`legacy` 是控制器为 v1 配置注入的迁移标记（见 _legacy_profile），
    # 在 `agent_profiles` 里声明 `legacy: true` 会跳过下面的 model 必填校验，等于把
    # "配过"变成"授权跟随 CLI 默认模型"。
    #
    # 判据必须基于 ``schema_version``，而不是"agent_profiles 存不存在"：规范化会把
    # 构造出的 legacy profile 写回 ``agent_profiles``，于是二次规范化时后者恒为真，
    # 迁移产物会被误判成用户新写的声明。schema v1 本身是刻意保留的迁移路径（没有
    # model 必填的历史包袱，且 model_unresolved 恒为 true、报告必须写"运行模型
    # 未解析"），因此仍是合法入口；v2 配置没有任何合法来源带 legacy 标记。
    supplied = isinstance(profiles, dict) and bool(profiles)
    legacy_allowed = c.get("schema_version") == 1
    if not profiles:
        profiles = {"legacy-" + str(c.get("agent", "codebuddy")): _legacy_profile(c, base)}
    require(isinstance(profiles, dict) and profiles, "INPUT_INCOMPLETE", "agent_profiles required")
    normalized: dict[str, dict] = {}
    failed = False
    for profile_id, profile in profiles.items():
        try:
            normalized[profile_id] = _normalize_profile(
                c,
                profile_id,
                profile,
                base,
                supplied=supplied,
                legacy_allowed=legacy_allowed,
            )
        except Error as exc:
            if problems is None:
                raise
            failed = True
            problems.append(
                {
                    "code": exc.code,
                    "field": f"agent_profiles.{profile_id}",
                    "message": str(exc),
                }
            )
    if failed:
        # 读路径：保留原始 profile（摘要语义不变），只把段级问题一并收集。
        # 到这里 problems 必然不是 None：failed 只在收集模式下被置位。
        assert problems is not None
        _collect_section_problems(c, problems)
        return c
    c["agent_profiles"] = normalized
    default = c.get("default_agent_profile")
    if not default:
        default = next(iter(normalized))
    require(
        default in normalized,
        "INPUT_INCOMPLETE",
        f"default_agent_profile {default!r} is not defined",
    )
    c["default_agent_profile"] = default
    adapter_ids = {p["adapter"] for p in normalized.values()}
    prefixes = sorted(
        {
            p
            for adapter_id in adapter_ids
            for p in registry.require_entry(adapter_id).factory().credential_prefixes
        }
    )
    c["credential_env_prefixes"] = prefixes
    # Shared mutable resources (10 §8): a list of ``{kind, id}``, normalised here
    # (not only in ``validate_config``) so ``policy_digest`` and a re-read config
    # stay byte-identical.
    #
    # **This field is a declaration only — nothing serialises on it.** An earlier
    # version of this comment claimed "two tasks sharing any resource are
    # serialized"; no code implements that (`_pipeline`, `_store`,
    # `_group_runner` never read the field), so the claim was removed instead of
    # being turned into a half-built lock/lease engine. Do not document it as
    # mutual exclusion: tasks that really contend for one external resource must
    # be serialised by the host, and a future implementation is a separate
    # design (locks, crash recovery, deadlock policy).
    shared = c.get("shared_resources", [])
    require(isinstance(shared, list), "INPUT_INCOMPLETE", "shared_resources must be an array")
    normalized_shared = []
    for item in shared:
        require(
            isinstance(item, dict), "INPUT_INCOMPLETE", "shared_resources items must be objects"
        )
        require(
            isinstance(item.get("kind"), str)
            and item["kind"]
            and isinstance(item.get("id"), str)
            and item["id"],
            "INPUT_INCOMPLETE",
            "shared_resources items require non-empty kind and id",
        )
        normalized_shared.append({"kind": item["kind"], "id": item["id"]})
    c["shared_resources"] = normalized_shared
    # 准入策略的默认值必须在这里定，而不是只在 validate_config 里补：任务记录
    # 冻结的是规范化后的配置，两侧（任务与项目）要用同一套默认值算 policy_digest。
    c["require_real_e2e"] = c.get("require_real_e2e") is True
    # 证据与运行时的绑定强度：strict（默认）要求指纹完全一致；warn 允许"版本对、
    # 指纹变"，只在 reason 里留警告。
    binding_mode = c.get("runtime_binding") or "strict"
    c["runtime_binding"] = binding_mode if binding_mode in ("strict", "warn") else "strict"
    # v1.2 §16.1：`routing` / `endpoints` 的默认值只在**段存在时**写回。没有这两个
    # 键的配置规范化后仍然没有它们，`policy_digest` 与 v1.0.0 逐字节一致。
    from src import _routing

    _routing.normalize_endpoints(c)
    _routing.normalize_routing(c)
    return c


def load_evidence(profile: dict, adapter_id: str, backend: str) -> registry.Verification | None:
    """Load the operator-declared verification record for this exact granularity."""
    ref = profile.get("compatibility_evidence")
    if not ref:
        return None
    path = Path(ref)
    require(
        path.is_file(), "ADAPTER_VERSION_MISMATCH", f"compatibility_evidence {ref} does not exist"
    )
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise Error(
            "ADAPTER_VERSION_MISMATCH", f"compatibility_evidence unreadable: {exc}"
        ) from exc
    records = data.get("records") if isinstance(data, dict) else data
    require(
        isinstance(records, list) and records,
        "INPUT_INCOMPLETE",
        "compatibility_evidence must contain a non-empty records array",
    )
    parsed = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("adapter_id") not in (None, adapter_id):
            raise Error(
                "ADAPTER_BACKEND_MISMATCH",
                "compatibility_evidence declares adapter {!r}, profile uses {!r}".format(
                    record.get("adapter_id"), adapter_id
                ),
            )
        parsed.append(registry.Verification.from_dict({**record, "evidence_ref": str(path)}))
    matching = [v for v in parsed if v.backend == backend]
    require(
        matching,
        "ADAPTER_BACKEND_MISMATCH",
        f"compatibility_evidence has no record for backend {backend!r}",
    )
    return matching[-1]


def resolve_binding(c: dict[str, Any], profile_id: str | None = None) -> ProfileBinding:
    """Resolve one profile into a runnable-or-blocked binding, without side effects."""
    require(
        isinstance(c.get("agent_profiles"), dict),
        "INPUT_INCOMPLETE",
        "Project configuration is not normalized",
    )
    pid = profile_id or c.get("default_agent_profile")
    profiles = c["agent_profiles"]
    require(
        isinstance(pid, str) and pid in profiles,
        "INPUT_INCOMPLETE",
        f"Unknown agent profile {pid!r}",
    )
    profile = profiles[pid]
    entry = registry.require_entry(profile["adapter"])
    adapter = entry.factory()
    backend = profile.get("backend") or adapter.default_backend
    require(
        backend in adapter.supported_backends,
        "ADAPTER_BACKEND_MISMATCH",
        f"{entry.adapter_id} does not support backend {backend!r}",
    )
    adapter.validate_profile(profile)
    # v1.2 §6.3：声明必须能落到受支持的有效配置，否则拒绝（不静默忽略）。
    # 放在这里而不是只在启动时：注册期就失败，才不会让"配置里写了 provider"
    # 与"实际没传 provider"看起来一样。
    mapping = adapter.execution_mapping(profile)
    extra = None
    expected_version = profile.get("expected_version")
    if profile.get("legacy") and profile.get("compatibility_verified") is True:
        # Explicit operator declaration carried over from v0.2. Labelled as such
        # in `agent list`; it is not a real-e2e record.
        expected_version = expected_version or "admin-declared"
        extra = registry.Verification(
            backend=backend,
            version=expected_version,
            source="legacy_admin_declaration",
            evidence_ref="project config: compatibility_verified=true",
        )
    evidence = load_evidence(profile, entry.adapter_id, backend)
    # D9: an identity-bound record is only valid on the platform and CLI
    # fingerprint it was produced on. The digest is computed lazily (and cached)
    # because it shells out to the CLI's ``--version``.
    current_platform = None
    current_digest = None
    if evidence is not None and evidence.identity_bound:
        from src import _qualification

        current_platform = _qualification.platform_id()
        current_digest = _qualification.current_runtime_digest(entry.adapter_id)
    # A real compatibility record always wins over a legacy admin declaration.
    capability, reason, verification = registry.resolve(
        entry.adapter_id,
        backend,
        expected_version,
        evidence or extra,
        platform=current_platform,
        runtime_digest=current_digest,
        # 默认 strict：只有显式 runtime_binding=warn 的项目才容忍"版本对、指纹变"。
        allow_runtime_mismatch=c.get("runtime_binding") == "warn",
    )
    return ProfileBinding(
        profile_id=pid,
        profile=profile,
        adapter=adapter,
        entry=entry,
        backend=backend,
        capability=capability,
        capability_reason=reason,
        verification=verification,
        profile_hash=profile_hash(pid, profile),
        env_names=adapter.env_names(profile),
        options=validate_typed_options(profile),
        permission=adapter.permission_state(profile.get("permission_profile")),
        execution=mapping,
    )


def require_runnable(binding: ProfileBinding, *, require_real_e2e: bool = True) -> ProfileBinding:
    """返回可派发的绑定；``require_real_e2e`` 决定"未验证"是否构成拒绝。

    默认值 ``True`` 保留"低层守卫"语义（直接调用者必须显式要求放行），项目级
    策略由 ``start`` / ``run_pipeline`` / ``reassign`` 从配置的
    ``require_real_e2e``（默认 false）传入。放行不等于声称已验证：调用方必须
    把 ``binding.unverified`` / ``binding.trust_level`` 写进响应、run 记录与报告。
    """
    if binding.capability != CAPABILITY_VERIFIED and require_real_e2e:
        code = (
            "ADAPTER_UNVERIFIED"
            if binding.capability == registry.CAPABILITY_IMPLEMENTED
            else "ADAPTER_VERSION_MISMATCH"
        )
        raise Error(
            code,
            "{}/{} is {}: {}".format(
                binding.adapter_id,
                binding.backend,
                binding.capability,
                binding.capability_reason or "not verified",
            ),
        )
    # A profile that demands a bound permission profile may not run with one that
    # only exists as a label: the agent would hit its own approval prompt and be
    # reported as a business failure instead of a configuration gap (design §10.2).
    #
    # v0.5.1 起对"确实有权限开关"的适配器（``permission_switch``）默认 fail
    # closed：无人值守的 ``-p`` 模式没有审批通道，缺绑定等于"写文件必被拒"，
    # 让这种派发跑 900 秒再以 AGENT_FAILED 收场只是把配置缺陷伪装成业务失败。
    # 没有该开关的适配器（如 opencode）仍然放行，``enforced=False`` 保持诚实。
    unbound = not binding.permission.get("enforced")
    if unbound and (
        binding.profile.get("require_permission_binding")
        or getattr(binding.adapter, "permission_switch", False)
    ):
        raise Error(
            "PERMISSION_PROFILE_UNBOUND",
            "profile {} requires an enforced permission_profile, but {!r} has no "
            "verified argv binding in this build".format(
                binding.profile_id, binding.profile.get("permission_profile")
            ),
        )
    # F5：`api_key` 的凭据缺失是启动前就能判定的配置缺陷。这里只检查"环境变量
    # 是否存在"，绝不读取、记录或回显值（TGD §5.5）。
    state = auth_state(binding)
    if state["auth_mode"] == "api_key" and not state["credential_present"]:
        raise Error(
            "AUTH_REQUIRED",
            "profile {} uses api_key but {} is not set in this environment".format(
                binding.profile_id, ", ".join(state["credential_refs"])
            ),
            details={
                "profile_id": binding.profile_id,
                "auth_mode": "api_key",
                "missing": [name for name in state["credential_refs"] if not os.environ.get(name)],
            },
        )
    return binding


def auth_state(binding: ProfileBinding, probe: dict | None = None) -> dict:
    """Honest authentication state for one profile (F5, TGD §5.5).

    Three states, and every non-``unknown`` one needs evidence:

    * ``api_key`` — ``present`` when every declared credential ref is in the
      environment (the value is never read, stored or echoed), ``absent`` when
      one is missing;
    * ``cli_login`` — ``absent`` when a real probe's output carries the adapter's
      authentication failure marker; otherwise ``unknown``, because answering
      ``--version`` does not prove a session. A config directory is not proof.
    """
    from src._agents.base import OUTCOME_AUTH_ERROR, classify_failure

    mode = auth_mode(binding.profile or {})
    refs = list((binding.profile or {}).get("credential_refs") or [])
    present = bool(refs) and credentials_present(binding) is True
    login_state = "unknown"
    basis = (
        "login_state=unknown means the authentication state was not proven in this "
        "environment; a version probe is not a login and a config directory is not "
        "evidence"
    )
    if mode == "api_key":
        if present:
            login_state, basis = "present", "every declared credential ref is present"
        elif refs:
            login_state, basis = "absent", "a declared credential ref is missing"
    elif probe is not None:
        text = probe.get("text") or ""
        # The failure marker is checked on *any* probe status: a CLI can report
        # "Authentication required" with exit 0 (observed with codebuddy), so
        # trusting the status alone would turn a definite `absent` into `unknown`.
        if classify_failure(text) == OUTCOME_AUTH_ERROR:
            login_state = "absent"
            basis = "the CLI reported an authentication failure"
        elif probe.get("probe_status") == "PASSED":
            basis = "the CLI answered --version; a login session was not proven"
    return {
        "profile_id": binding.profile_id,
        "auth_mode": mode,
        "credential_refs": refs,
        "credential_present": present,
        "login_state": login_state,
        "login_basis": basis,
        "note": (
            "login_state=unknown means the authentication state was not proven; "
            "only observed evidence may report present/absent"
        ),
    }


def profile_views(c: dict[str, Any]) -> list[dict]:
    """Read-only profile listing for ``zoe agent list`` (never leaks credentials)."""
    views = []
    for pid in sorted(c.get("agent_profiles") or {}):
        profile = c["agent_profiles"][pid]
        try:
            binding = resolve_binding(c, pid)
        except Error as exc:
            views.append(
                {
                    "profile_id": pid,
                    "adapter_id": profile.get("adapter"),
                    "backend": profile.get("backend"),
                    "capability": "UNVERIFIED",
                    "error": {"code": exc.code, "message": str(exc)},
                }
            )
            continue
        state = auth_state(binding)
        views.append(
            {
                **binding.to_dict(),
                "default": pid == c.get("default_agent_profile"),
                "image": profile.get("image"),
                "permission_profile": profile.get("permission_profile"),
                "network_profile": profile.get("network_profile"),
                "credential_refs": list(profile.get("credential_refs") or []),
                "credential_present": credentials_present(binding),
                # F5：认证态单独列出，`unknown` 不是"已认证"，且必须带判据。
                "auth_mode": state["auth_mode"],
                "login_state": state["login_state"],
                "login_basis": state["login_basis"],
                "compatibility_evidence": profile.get("compatibility_evidence"),
            }
        )
    return views


def credentials_present(binding: ProfileBinding | None) -> bool | None:
    """Whether every credential env name of ``binding`` is present in this process.

    ``None`` means "not applicable" (no binding, or no declared names) and never
    "absent": a caller must not turn an unanswered question into a denial. One
    implementation, because ``doctor``, ``auth_state`` and ``profile_views`` all
    report this fact and two copies drift the moment one of them is tightened.
    """
    if binding is None or not binding.env_names:
        return None
    return all(name in os.environ for name in binding.env_names)


def check_prompt_size(prompt: bytes) -> None:
    require(
        len(prompt) <= PROMPT_LIMIT_BYTES,
        "PROMPT_TOO_LARGE",
        f"Prompt exceeds {PROMPT_LIMIT_BYTES} bytes; move long context into read-only files",
    )


def launch_for(binding: ProfileBinding, context: agent_base.RunContext) -> LaunchSpec:
    spec = binding.adapter.build_launch(context)
    agent_base.validate_launch_spec(spec)
    return spec


def probe_profile(c: dict[str, Any], profile_id: str, timeout: float | None = None) -> dict:
    """Bounded, offline adapter probe on *this host*.

    The probe runs the same ``argv`` the launcher pins and would spawn (§6.6):
    resolving a path and then measuring a different binary would make
    ``agent probe`` and ``start`` describe two different CLIs. It proves "this
    binary answers ``--version``" and nothing else: capability is never touched
    and no model turn runs.
    """
    from src import _discovery
    from src._agents.base import PROBE_MAX_OUTPUT_BYTES

    binding = resolve_binding(c, profile_id)
    probe_timeout = _discovery.VERSION_TIMEOUT_SECONDS if timeout is None else float(timeout)
    result = {
        "api_version": 2,
        "project_id": c["project_id"],
        "profile_id": binding.profile_id,
        "adapter_id": binding.adapter_id,
        "backend": binding.backend,
        "capability": binding.capability,
        "probe_status": "NOT_RUN",
        "command": None,
        "version": None,
        "resolution": None,
        "resolution_notes": [],
        "notes": ["A probe is not end-to-end verification; capability is unchanged."],
        "bounds": {"timeout_seconds": probe_timeout, "max_output_bytes": PROBE_MAX_OUTPUT_BYTES},
        # F5: the probe is the only place that can *observe* an authentication
        # failure, so it must report the auth state instead of leaving callers to
        # infer it. Without a probe it stays `unknown`.
        "auth": auth_state(binding),
    }
    try:
        plan = binding.adapter.probe_plan(binding.profile)
    except Error as exc:
        result.update(probe_status="BLOCKED", notes=result["notes"] + [f"{exc.code}: {exc}"])
        return result
    # Same pinning call as the pipeline: a probe may never measure a different
    # binary from the one `start` will spawn.
    plan = _discovery.pin_launcher(plan, c.get("backend"), env_names=binding.env_names, probe=True)
    resolution = plan.extra.get("resolution") or {}
    argv = list(plan.argv)
    result["command"] = argv
    result["resolution"] = resolution or None
    result["resolution_notes"] = list(resolution.get("notes") or [])

    if _discovery.normalize_backend(c.get("backend")) != "local-process":
        result["notes"].append("probe runs only against the local-process execution backend")
        return result
    if not resolution.get("path"):
        result.update(probe_status="BLOCKED")
        result["notes"].append(
            "agent CLI not found; install it, put it on PATH, or bind an absolute path"
        )
        return result

    code, text, note = _discovery.run_bounded(
        argv, timeout=probe_timeout, env=_discovery.probe_env(binding.env_names)
    )
    if note:
        result.update(probe_status="FAILED")
        result["notes"].append(f"probe did not complete: {note}")
        return result
    version = binding.adapter.parse_probe(text)
    result.update(
        probe_status="PASSED" if code == 0 and version else "FAILED",
        version=version,
        exit_code=code,
    )
    if version:
        # The probe just measured this binary; publish the fact so the rest of
        # this process (and the run record) uses it instead of re-measuring or
        # leaving `resolution.version` empty while `version` is filled (§6.5).
        _discovery.remember_version(resolution.get("path"), binding.env_names, version)
        result["resolution"] = {**resolution, "version": version}
    if code not in (0, None):
        result["notes"].append(f"probe exited {code}")
    result["auth"] = auth_state(
        binding, {"probe_status": result["probe_status"], "text": text, "exit_code": code}
    )
    return result
