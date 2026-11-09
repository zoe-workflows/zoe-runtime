"""Runtime requirements gate: compare this runtime with a skill's declaration.

The skill ships ``runtime-requirements.json``; the *runtime* parses it. Parsing on
the skill side would mean ``jq`` or a ``python -c`` one-liner — undeclared
dependencies that a future non-Python runtime cannot rely on.

The file is structured, not a PEP 440 range string: the runtime has
``dependencies = []``, so there is no ``packaging`` to parse specifiers. Version
comparison is a tuple compare with one deliberate rule — a prerelease sorts
*before* the release with the same numbers, so ``min_version: 0.5.1`` rejects
``0.5.1rc1``. A prerelease is never a reason to relax a published skill.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src import __version__
from src._agents.base import PROTOCOL_REVISION
from src._util import Error, require

#: ``capabilities().api_version``. Not imported from ``cli`` to keep this module
#: free of the CLI import cycle.
API_VERSION = 2

#: Feature names a skill may demand through ``required_capabilities`` (TGD §5.6).
#: A name is listed here only when this build actually honours it; "not_supported"
#: entries in ``capabilities()`` are deliberately absent.
SUPPORTED_CAPABILITIES = frozenset(
    {
        "task_groups",
        "group_plan_freeze",
        "group_dependency_graph",
        "group_serial_execution",
        "group_runner_lease",
        "group_budget",
        "group_checks",
        "group_acceptance",
        "check_error_class",
        "auth_mode",
        "design_manifest",
        # v1.4：设计一致性审查与有界修复。
        "group_design_review",
        "group_review_contract",
        "group_bounded_repair",
        # v1.5 观测面：事件过滤、日志增量流、运行观测、检查预检。
        "event_project_filter",
        "logs_follow",
        "run_observation",
        "checks_preflight",
        # v1.5.2：快照也按游标增量读（`task logs --after`），不只 `--follow`。
        "logs_incremental_read",
        # v1.6：跨任务事实聚合、任务记录投影、派发前基线校验。
        # 名字只在本版真的兑现时才出现：skill 靠它决定"能不能少发 N 条命令"。
        "task_digest",
        "task_list_fields",
        "base_drift_preflight",
        # v1.6：额度耗尽的项目级闸门（`project quota show` / `clear`）。
        # 技能只有能探测到它，才谈得上"恢复前不要 resume"。
        "quota_gate",
        # v1.6：进入 REVIEW_READY 时附带的基础事实（`concurrent_base`）。
        # 刻意**不**进技能的 required_capabilities：它是提示字段，缺失时宿主只是
        # 少一条信息，不该让整包在旧 1.6.0 构建上直接 RUNTIME_INCOMPATIBLE。
        "base_drift_review",
        # v1.7：第二层验收。名字出现即表示 accept 真的会被这些门禁拦住，
        # 而不只是"CLI 里有这个子命令"。
        "task_acceptance_review",
        "spec_v3_decisions",
        "progress_refs_gate",
        "task_coverage",
        # v1.7：`depends_on` 在 start 阶段被判定（上游必须 DONE + accepted + 判定满足）。
        "task_dependency_gate",
    }
)

_VERSION = re.compile(r"^(\d+(?:\.\d+)*)([A-Za-z0-9.+_-]*)$")

#: 预发布后缀里的"数字段"与"文字段"。``rc10`` 必须排在 ``rc9`` 之后，纯粹的
#: 字符串比较会反过来，而方向错误的后果不对称：对 ``min_version`` 是放行一个
#: 更老的运行时。
_SUFFIX_PART = re.compile(r"(\d+|[^\d]+)")


def _suffix_key(suffix: str) -> tuple[tuple[int, object], ...]:
    """Order a prerelease suffix naturally (``rc2`` < ``rc9`` < ``rc10``)."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part) for part in _SUFFIX_PART.findall(suffix)
    )


def parse_version(text: str) -> tuple[tuple[int, ...], str]:
    """Split ``"0.5.1rc1"`` into ``((0, 5, 1), "rc1")``.

    Anything that is not ``<digits>[.<digits>...]<suffix>`` is refused instead of
    guessed: a requirements file the runtime cannot read must fail closed.
    """
    match = _VERSION.match(str(text).strip())
    require(match, "INPUT_INCOMPLETE", f"unparseable version {text!r}")
    numbers = tuple(int(part) for part in match.group(1).split("."))
    return numbers, match.group(2)


def compare(left: str, right: str) -> int:
    """Return -1, 0 or 1 for ``left`` versus ``right`` (prerelease sorts lower)."""
    left_numbers, left_suffix = parse_version(left)
    right_numbers, right_suffix = parse_version(right)
    width = max(len(left_numbers), len(right_numbers))
    left_padded = left_numbers + (0,) * (width - len(left_numbers))
    right_padded = right_numbers + (0,) * (width - len(right_numbers))
    if left_padded != right_padded:
        return -1 if left_padded < right_padded else 1
    if left_suffix == right_suffix:
        return 0
    if not left_suffix:
        return 1
    if not right_suffix:
        return -1
    # 后缀按自然序比较：`rc9` 低于 `rc10`。任何后缀都排在正式版之下（保守：
    # `1.5.1.post1` 视为低于 `1.5.1`，不会因 post 版本绕过上限）。
    left_key = _suffix_key(left_suffix)
    right_key = _suffix_key(right_suffix)
    if left_key == right_key:
        return 0
    return -1 if left_key < right_key else 1


def _field(declared: dict, name: str, kind: type):
    value = declared.get(name)
    require(
        isinstance(value, kind) and not isinstance(value, bool),
        "INPUT_INCOMPLETE",
        f"runtime-requirements.json: {name!r} must be {kind.__name__}",
    )
    return value


def check(path: str | Path) -> dict:
    """Validate ``path`` against this runtime and return the comparison result.

    Raises :class:`Error` with ``RUNTIME_INCOMPATIBLE`` when the runtime is outside
    the declared window, and ``INPUT_INCOMPLETE`` when the file itself is unusable.
    """
    location = Path(path)
    require(location.is_file(), "INPUT_INCOMPLETE", f"requirements file not found: {location}")
    try:
        declared = json.loads(location.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise Error("INPUT_INCOMPLETE", f"requirements file is not JSON: {exc}") from exc
    require(isinstance(declared, dict), "INPUT_INCOMPLETE", "requirements file must be an object")

    minimum = _field(declared, "min_version", str)
    maximum = _field(declared, "max_version_exclusive", str)
    # 解析实际发生在 ``compare`` 里，那里已经不知道出错的是哪个字段。先按字段名
    # 各解析一次，诊断才能指出是 min 还是 max 写坏了（``"2.0"`` 少一位是最常见
    # 的笔误）。
    for name, value in (("min_version", minimum), ("max_version_exclusive", maximum)):
        try:
            parse_version(value)
        except Error as exc:
            raise Error(
                "INPUT_INCOMPLETE",
                f"runtime-requirements.json: {name!r} is not a version: {value!r}",
                details={"requirements_path": str(location), "field": name},
            ) from exc
    api_version = _field(declared, "api_version", int)
    revision = _field(declared, "protocol_revision", int)
    # Optional: a skill that needs a v1.3 surface says so by name instead of by
    # version guesswork. Names this build does not honour are refusals, not
    # warnings — otherwise the skill would drive a command that always fails.
    required = declared.get("required_capabilities", [])
    require(
        isinstance(required, list) and all(isinstance(item, str) for item in required),
        "INPUT_INCOMPLETE",
        "runtime-requirements.json: 'required_capabilities' must be an array of strings",
    )

    runtime = {
        "version": __version__,
        "api_version": API_VERSION,
        "protocol_revision": PROTOCOL_REVISION,
    }
    reasons = []
    if compare(__version__, minimum) < 0:
        reasons.append(f"version {__version__} < min_version {minimum}")
    if compare(__version__, maximum) >= 0:
        reasons.append(f"version {__version__} >= max_version_exclusive {maximum}")
    # 空窗口（min >= max）没有任何运行时可满足，但上面的两条原因各自看起来都
    # 像"改一个字段就好"。作为附加原因报出来，用户才知道是窗口写错了。
    if compare(minimum, maximum) >= 0:
        reasons.append(
            f"empty version window: min_version {minimum} >= max_version_exclusive {maximum}"
        )
    if api_version != API_VERSION:
        reasons.append(f"api_version {api_version} != {API_VERSION}")
    if revision != PROTOCOL_REVISION:
        reasons.append(f"protocol_revision {revision} != {PROTOCOL_REVISION}")
    missing_capabilities = sorted(set(required) - SUPPORTED_CAPABILITIES)
    if missing_capabilities:
        reasons.append("required capability not supported: " + ", ".join(missing_capabilities))

    if reasons:
        raise Error(
            "RUNTIME_INCOMPATIBLE",
            "runtime does not satisfy the skill's requirements: " + "; ".join(reasons),
            details={
                "runtime": runtime,
                "requirements": {
                    "min_version": minimum,
                    "max_version_exclusive": maximum,
                    "api_version": api_version,
                    "protocol_revision": revision,
                    "required_capabilities": list(required),
                },
                "requirements_path": str(location),
                "failures": reasons,
            },
        )
    return {
        "ok": True,
        "runtime": runtime,
        "requirements": {
            "min_version": minimum,
            "max_version_exclusive": maximum,
            "api_version": api_version,
            "protocol_revision": revision,
            "required_capabilities": list(required),
        },
        "requirements_path": str(location),
    }
