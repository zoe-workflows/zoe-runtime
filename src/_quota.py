"""Quota exhaustion: evidence extraction and the project-level dispatch gate.

两类问题必须分开回答，因为它们的作用域不同：

* **一次 run 的证据**：这次失败是"额度用完了"，还是"请求太快了"？
  处置方向相反——限流可以退避，额度只能等重置或换账号。
* **跨任务的闸门**：这个项目现在还能不能派发？六个并发撞的是同一个账号
  额度池，按任务熔断等于让第 2~6 个继续试，把一次事故变成六次。

闸门是**本地文件**（``<home>/projects/<id>.quota.json``），不进项目配置：
写进配置会改变 ``policy_digest``，让在途任务全部 ``POLICY_CHANGED``——一次
额度事故不该顺带触发一次策略迁移。

闸门**不自动过期**。服务端给的"重置时间"可能来自另一个时区、另一个账号或
根本没有给（只给 ``retry after``）；拿它自动放行等于用一个猜测去授权花费。
``reset_at_epoch`` 只用来提示"看起来已经过了重置时间"，解除仍要显式
``project quota clear``。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path

from src._util import atomic, read, safe_id

QUOTA_CODE = "QUOTA_EXHAUSTED"

#: Payload version of ``<home>/projects/<id>.quota.json``.
SCHEMA_VERSION = 1

#: How much of the agent log is passed to the classifier when a run produced no
#: capture metadata. Enough for a CLI banner plus a stack of retries; bounded so
#: a 8 MB log does not become a per-run scan.
EVIDENCE_SCAN_BYTES = 16 * 1024

#: One-line excerpt length stored in the block details and the gate file.
EVIDENCE_LIMIT = 400

#: "The account/plan is out of budget" — these words mean waiting helps only
#: until the reset, and retrying the same action spends nothing but time.
#: ``使用量`` is what CodeBuddy prints ("使用量已超出频率限制"); ``quota`` /
#: ``insufficient_quota`` is the provider-side name for the same condition.
_QUOTA_TOKENS = (
    "quota",
    "usage limit",
    "usage_limit",
    "insufficient_quota",
    "payment required",
    "insufficient balance",
    "no credit",
    "credit balance",
    "out of credits",
    "额度",
    "配额",
    "使用量",
    "余额不足",
)

#: A reset timestamp turns a plain "too many requests" into "come back later".
#: Without one, a 429 is a rate limit and backoff is the correct response.
_RESET_TOKENS = ("reset", "重置")

_ABSOLUTE_RE = re.compile(
    r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})[T ](?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})"
    r"(?:\.\d+)?(?P<tz>Z|[+-]\d{2}:?\d{2})?"
)
_RELATIVE_RE = re.compile(
    r"(?:retry|try again|重试)[^\d]{0,24}(?P<n>\d+)\s*"
    r"(?P<unit>seconds?|secs?|秒|minutes?|mins?|分钟|hours?|hrs?|小时)",
    re.IGNORECASE,
)
_RELATIVE_SECONDS = {
    "second": 1,
    "seconds": 1,
    "sec": 1,
    "secs": 1,
    "秒": 1,
    "minute": 60,
    "minutes": 60,
    "min": 60,
    "mins": 60,
    "分钟": 60,
    "hour": 3600,
    "hours": 3600,
    "hr": 3600,
    "hrs": 3600,
    "小时": 3600,
}


def excerpt(text: str, limit: int = EVIDENCE_LIMIT) -> str:
    """Last non-empty line, whitespace-collapsed, bounded.

    The useful line in a CLI banner is usually the last one (the error), and the
    first one is the progress noise that preceded it.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return " ".join(lines[-1].split())[:limit]


def looks_exhausted(text: str) -> bool:
    """Whether this failure is "out of budget" rather than "too fast".

    Deliberately token-based: the CLIs do not expose a machine-readable quota
    code, and inventing a structured field they never send would be a fiction.
    A bare ``429`` stays a rate limit — only a quota word or a reset timestamp
    promotes it.
    """
    lowered = (text or "").lower()
    if not lowered:
        return False
    if any(token in lowered for token in _QUOTA_TOKENS):
        return True
    return "429" in lowered and any(token in lowered for token in _RESET_TOKENS)


def reset_at(text: str, *, now: float | None = None) -> dict | None:
    """Extract the reset moment from the CLI banner.

    Returns ``None`` when the text carries no usable moment. An absolute stamp
    without an offset is interpreted in **local** time and labelled as such:
    dropping the assumption would either fabricate UTC or refuse to parse the
    one form CodeBuddy actually prints.
    """
    value = text or ""
    match = _ABSOLUTE_RE.search(value)
    if match:
        try:
            dt = datetime(
                int(match.group("y")),
                int(match.group("mo")),
                int(match.group("d")),
                int(match.group("h")),
                int(match.group("mi")),
                int(match.group("s")),
            )
        except ValueError:
            dt = None
        if dt is not None:
            tz = match.group("tz")
            if tz is None:
                epoch = dt.timestamp()  # naive datetime.timestamp() == local time
                label = "local"
            elif tz == "Z":
                epoch = dt.replace(tzinfo=timezone.utc).timestamp()
                label = "utc"
            else:
                sign = 1 if tz[0] == "+" else -1
                digits = tz[1:].replace(":", "")
                offset = sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
                epoch = dt.replace(tzinfo=timezone.utc).timestamp() - offset
                label = "offset"
            return {
                "value": match.group(0),
                "epoch": epoch,
                "source": "absolute",
                "timezone": label,
            }
    relative = _RELATIVE_RE.search(value)
    if relative:
        unit = relative.group("unit").lower()
        factor = _RELATIVE_SECONDS.get(unit) or _RELATIVE_SECONDS.get(unit.rstrip("s"))
        if factor:
            base = time.time() if now is None else float(now)
            return {
                "value": relative.group(0),
                "epoch": base + int(relative.group("n")) * factor,
                "source": "relative",
                "timezone": "relative",
            }
    return None


# --------------------------------------------------------------------------- #
# Project-level gate
# --------------------------------------------------------------------------- #


def gate_path(store, project_id: str) -> Path:
    return store.home / "projects" / (safe_id(project_id) + ".quota.json")


def read_gate(store, project_id: str) -> dict | None:
    """Read the gate record; a damaged record stays readable but is reported.

    ``None`` means "no quota incident was ever recorded". A corrupt file is
    returned with ``_corrupt`` so a caller can tell "never tripped" from
    "trip record is unreadable" — the latter is not a reason to let dispatch
    through silently.
    """
    path = gate_path(store, project_id)
    if not path.is_file():
        return None
    try:
        data = read(path)
    except (OSError, ValueError) as exc:
        return {"_corrupt": True, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(data, dict):
        return {"_corrupt": True, "error": "quota gate record is not an object"}
    return data


def active_gate(store, project_id: str, *, now: float | None = None) -> dict | None:
    """Return the gate that currently blocks dispatch, or ``None``.

    An expired reset time does **not** unlock by itself (module docstring); it
    only changes the message. A corrupt record stays blocking: "unreadable"
    must not read as "no incident".
    """
    gate = read_gate(store, project_id)
    if gate is None:
        return None
    if gate.get("_corrupt"):
        return gate
    if gate.get("cleared_at"):
        return None
    moment = time.time() if now is None else float(now)
    reset_epoch = gate.get("reset_at_epoch")
    gate["expired"] = bool(reset_epoch and moment >= float(reset_epoch))
    return gate


def trip(
    store,
    project_id: str,
    *,
    task_id: str | None,
    run_id: str | None,
    text: str,
    source: str = "agent-output",
) -> dict:
    """Record one quota incident for the whole project.

    Overwriting is intentional: every writer observed the same pool running
    out, so the newest evidence is as true as the oldest, and a
    read-modify-write would only add a race. The gate's job is a boolean plus
    one reset moment, not an incident ledger (events are the ledger).
    """
    moment = time.time()
    reset = reset_at(text, now=moment)
    gate = {
        "schema_version": SCHEMA_VERSION,
        "project_id": safe_id(project_id),
        "block_code": QUOTA_CODE,
        "tripped_at": moment,
        "task_id": task_id,
        "run_id": run_id,
        "source": source,
        "evidence": excerpt(text),
        "reset_at": (reset or {}).get("value"),
        "reset_at_epoch": (reset or {}).get("epoch"),
        "reset_at_source": (reset or {}).get("source"),
        "reset_at_timezone": (reset or {}).get("timezone"),
        "cleared_at": None,
        "cleared_by": None,
        "cleared_reason": None,
    }
    atomic(gate_path(store, project_id), gate)
    gate["expired"] = bool(
        gate["reset_at_epoch"] and moment >= float(gate["reset_at_epoch"])
    )
    return gate


def clear(store, project_id: str, *, why: str, actor: str | None = None) -> dict:
    """Explicitly release the gate, keeping the incident on disk.

    Clearing is a human statement ("the budget is back / the account changed"),
    not an inference from a clock. The record keeps who said so and why,
    because the next incident should be comparable to this one.
    """
    gate = read_gate(store, project_id) or {}
    moment = time.time()
    payload = {
        **gate,
        "schema_version": gate.get("schema_version") or SCHEMA_VERSION,
        "project_id": safe_id(project_id),
        "block_code": gate.get("block_code") or QUOTA_CODE,
        "cleared_at": moment,
        "cleared_by": actor or "human",
        "cleared_reason": why,
    }
    atomic(gate_path(store, project_id), payload)
    return payload


def details(gate: dict | None) -> dict:
    """Controller-facing ``Error.details`` for a gated dispatch.

    A host that branches on ``code`` is unaffected; one that wants to show
    "when can I retry" reads this instead of parsing ``last_error``.
    """
    if not gate:
        return {"kind": "quota_gate"}
    if gate.get("_corrupt"):
        return {
            "kind": "quota_gate_corrupt",
            "error": gate.get("error"),
            "hint": "the gate record is unreadable; inspect the file before clearing it",
        }
    return {
        "kind": "quota_gate",
        "quota_reset_at": gate.get("reset_at"),
        "quota_reset_epoch": gate.get("reset_at_epoch"),
        "quota_reset_source": gate.get("reset_at_source"),
        "quota_reset_timezone": gate.get("reset_at_timezone"),
        "expired": bool(gate.get("expired")),
        "tripped_at": gate.get("tripped_at"),
        "task_id": gate.get("task_id"),
        "run_id": gate.get("run_id"),
        "evidence": gate.get("evidence"),
        "gate": "project quota show",
        "hint": (
            "wait for the reset, then `project quota clear --project <id> --why ...`; "
            "resume before that spends the next attempt on the same exhausted pool"
        ),
    }
