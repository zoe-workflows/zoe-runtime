"""项目配置来源："改了 project.json 但没重新 register"的可观测出口（v1.6）。

问题
----
运行器**只**读 ``<home>/projects/<id>.json``（``project register`` 的落盘副本）。
磁盘上那份 ``project.json`` 从此不再被读取，于是"改了配置文件、没重新注册"是完全
静默的：``doctor`` 显示 ready、``start`` 照常派发，只是用的仍然是旧策略、旧
``allowed_paths``、旧 model 名。排查时它会被误读成"adapter 缓存了模型"——真正的
原因在更前面一层，而链路里没有任何地方能回答"上次注册读的是哪个文件、之后改过没有"。

为什么是 sidecar 而不是项目配置里的一个字段
--------------------------------------------
项目配置的**任何**字段变化都会改变 ``policy_digest``，在途任务随即
``POLICY_CHANGED``。"上次注册读的是哪个文件"是审计信息，不是策略；写进配置等于
让一次纯记录动作触发一次策略迁移，正是 ``_quota.py`` 刻意避开的那类副作用。

所以它落在 ``<home>/projects/<id>.source.json``，与 ``.attest.json`` / ``.quota.json``
同一层：都可以删，删掉只丢"可比对的基准"，不影响任务能不能跑。

只回答"文件变没变"
------------------
:func:`state` 不判断旧配置对错，也不推进任何状态。``changed=true`` 只是一个提示：
**磁盘上的配置与注册时不同，重新 register 才会生效。**
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from src._util import atomic, digest, read, safe_id

#: sidecar 载荷版本；结构变化时升它，读旧版本按"未记录"处理而不是猜。
SCHEMA_VERSION = 1

#: ``changed_fields`` 的上限：这是提示，不是配置 diff。
FIELD_LIMIT = 20


def path(store, project_id: str) -> Path:
    return store.home / "projects" / (safe_id(project_id) + ".source.json")


def _field_hashes(config: dict) -> dict[str, str]:
    """每个顶层键一个短摘要。

    只比顶层键：``allowed_paths`` / ``agent_profiles[cb].model`` 都落在顶层键的值里，
    任意深度的修改都会改变该键的摘要，而读者不需要一份逐行的 diff。
    """
    hashes: dict[str, str] = {}
    for key in sorted(config):
        try:
            payload = json.dumps(config[key], sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = repr(config[key])
        hashes[str(key)] = digest(payload.encode())[:16]
    return hashes


def remember(store, project_id: str, source_path, config: dict) -> dict:
    """记录本次注册的来源文件与字段摘要。返回写入的记录。

    写入失败不抛错：注册本身已经成功，丢一个审计基准不该把它变成失败——
    否则"配置注册不上"和"记不了来源"会变成同一个错误码。
    """
    # 基准取**源文件原文**，不取校验后的配置：`validate_config` 会补默认值、
    # 规范化路径，拿它当基准会让"文件一个字没改"也显示成 changed（假阳性比没有
    # 提示更糟：它会让这条提示立刻被忽略）。
    try:
        recorded_from = read(source_path)
        if not isinstance(recorded_from, dict):
            recorded_from = config
    except (OSError, ValueError):
        recorded_from = config
    record = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "path": str(Path(source_path).resolve()),
        "field_hashes": _field_hashes(recorded_from),
        "registered_at": time.time(),
    }
    try:
        target = path(store, project_id)
        # `projects/` 由 `register` 的写入顺带创建；这里不能假设它已存在，否则
        # 首次注册时侧记录会静默丢失——而"没有记录"会被读成"没有变化"。
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic(target, record)
    except OSError:
        return {**record, "written": False}
    return {**record, "written": True}


def state(store, project_id: str, config: dict | None = None) -> dict:
    """Compare the source file on disk with what was registered. Never raises.

    ``changed`` 三态：``True``（文件变了，需要重新 register）、``False``（一致）、
    ``None``（无法判定：没有记录 / 文件没了 / 读不懂）。“无法判定”绝不等同于
    “没有变化”。
    """
    try:
        return _state(store, project_id, config)
    except Exception:  # noqa: BLE001 - 提示性事实不得让 doctor 失败
        return {
            "recorded": False,
            "source": None,
            "registered_at": None,
            "source_exists": None,
            "changed": None,
            "changed_fields": [],
            "reason": "unreadable",
        }


def _state(store, project_id: str, config: dict | None) -> dict:
    base = {
        "recorded": False,
        "source": None,
        "registered_at": None,
        "source_exists": None,
        "changed": None,
        "changed_fields": [],
        "reason": "not_recorded",
    }
    p = path(store, project_id)
    if not p.is_file():
        return base
    try:
        record = read(p)
    except (OSError, ValueError):
        return {**base, "recorded": True, "reason": "record_unreadable"}
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
        return {**base, "recorded": True, "reason": "record_unreadable"}

    source = record.get("path")
    recorded = record.get("field_hashes") or {}
    base.update(recorded=True, source=source, registered_at=record.get("registered_at"))
    if not source:
        return {**base, "reason": "record_unreadable"}

    file_path = Path(source)
    if not file_path.is_file():
        # 源文件被移走/删除：不是"没变化"，而是"无从比对"。
        return {**base, "source_exists": False, "reason": "source_missing"}

    try:
        current = read(file_path)
    except (OSError, ValueError, UnicodeDecodeError):
        return {**base, "source_exists": True, "reason": "source_unreadable"}
    if not isinstance(current, dict):
        return {**base, "source_exists": True, "reason": "source_unreadable"}

    current_hashes = _field_hashes(current)
    keys = set(recorded) | set(current_hashes)
    changed = sorted(key for key in keys if recorded.get(key) != current_hashes.get(key))
    return {
        **base,
        "source_exists": True,
        "changed": bool(changed),
        "changed_fields": changed[:FIELD_LIMIT],
        "reason": "changed" if changed else "same",
    }


def hint(value: dict) -> str | None:
    """人话提示；``None`` 表示无需提示（一致或无法判定）。"""
    if not value.get("changed"):
        return None
    fields = ", ".join(value.get("changed_fields") or []) or "（无法定位具体字段）"
    return (
        f"磁盘上的项目配置与注册时不同（{fields}），运行器仍在用注册副本："
        f"`project register --config {value.get('source')}` 之后才会生效；"
        "在途任务随后需要 `rebind`。"
    )
