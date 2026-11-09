"""多任务事实聚合（v1.6）：一次调用拿全一批任务的 status + 最近内容。

为什么需要它
------------
一版"给人看的摘要"原本是 N 次 ``task status`` + N 次 ``task logs``。宿主只能一条条发、
一条条读，于是汇报退化成"只报状态、不报内容"——不是执行者偷懒，是成本结构决定的。
聚合把这件事变成一次调用。

三条边界（改这里之前先读 SKILL「检查点与汇报」）
------------------------------------------------
1. **只聚合事实，不产生结论**：本模块不输出"完成 / 失败 / 通过"，也不推断"卡住"。
   ``state`` / ``block_code`` / ``empty_candidate`` 原样转述，解释权在宿主。
   这与 S8 的决定一致：语义归纳不是投影层的职责。
2. **只读**：不推进状态、不写文件、不签证据。副作用只有把日志尾部读进内存（有界）。
3. **不新增事实来源**：状态走 ``_progress.view``（与 ``task status`` 同一套），
   内容走 ``_logstream``（与 ``task logs`` 同一套）。同一件事不允许有两个答案。

成本要如实说：它仍然是有界的 O(N) 次文件读取（N 个目标），不是"恒定一次查询"。
``content=False`` 时省掉日志读取，只剩状态。
"""

from __future__ import annotations

import time
from typing import Any

from src import _logstream, _progress
from src._util import Error, redact_secrets, require

#: 不参与默认目标集合的状态：终态任务进 digest 只会稀释"现在谁在干什么"。
TERMINAL_STATES = ("DONE", "CANCELLED")

#: 内容行里文本与工具目标的截断长度（按字符，不是字节：中文按字节切会出半个字）。
TEXT_LIMIT = 120
TARGET_LIMIT = 60


def _int_or_none(value: Any) -> int | None:
    """把 progress.json 里的计数转成 int；坏值一律 None，不当成 0。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _content_items(
    store, t: dict, run_id: str, tail_bytes: int, lines: int
) -> tuple[list[dict], str | None]:
    """读日志尾部，返回 (内容行, note)。

    内容行只保留 ``text`` / ``tool``：``raw`` 是认不出信封的原始行，``record`` 是
    认出了信封但认不出内容——两者都不是"可读进展"，塞进汇报只会制造噪声。

    读取失败一律降级成 note，不抛错：digest 的职责是"尽可能给出当前视图"，
    一个任务的日志不可读不该让整批汇总失败（退出码与 note 由调用方决定）。
    """
    path = store.path(t) / "runs" / run_id / "agent.log"
    try:
        if not path.is_file():
            return [], "no_agent_log"
        size = path.stat().st_size
        if size <= 0:
            return [], "empty_log"
        start = _logstream.window_start(path, size, tail_bytes)
        data = _logstream.read_window(path, start, min(tail_bytes, size - start))
        events, _ = _logstream.project_complete_lines(data, start, "auto")
    except OSError:
        return [], "log_unreadable"

    items: list[dict] = []
    seen: set[tuple] = set()
    for event in events:
        kind = event.get("kind")
        if kind == "text":
            text = redact_secrets(str(event.get("text") or ""))[:TEXT_LIMIT]
            if not text:
                continue
            key: tuple = ("text", text)
            item = {"kind": "text", "text": text}
        elif kind == "tool":
            name = str(event.get("name") or "")
            target = redact_secrets(str(event.get("target") or ""))[:TARGET_LIMIT]
            key = ("tool", name, target)
            item = {"kind": "tool", "name": name, "target": target}
        else:
            continue
        # 去重放在截断之后：同一条工具调用连续出现多次会制造"一直在动"的假象，
        # 而那正是 `--with-log` 曾经踩过的坑。
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    if not items:
        return [], "no_readable_progress"
    return items[-lines:], None


def _task_row(store, t: dict, now: float, tail_bytes: int, lines: int, content: bool) -> dict:
    run_id = t.get("current_run_id")
    run = store.get_run(run_id) if run_id else None
    progress = _progress.view(store, t, run) if run else None

    created = t.get("created_at")
    elapsed: int | None = None
    if created:
        try:
            elapsed = max(0, int(now - float(created)))
        except (TypeError, ValueError):
            elapsed = None

    items: list[dict] = []
    note: str | None = None
    if content:
        if not run_id:
            note = "no_run"
        else:
            items, note = _content_items(store, t, run_id, tail_bytes, lines)
    else:
        note = "content_skipped"

    return {
        "task_id": t["task_id"],
        "title": t.get("title") or "",
        "state": t.get("state"),
        "phase": t.get("phase"),
        "block_code": t.get("block_code"),
        # v1.6：增量视图要能看见"候选变了"，不只是"状态变了"。
        # 三个字段都是原样转述，不推断；没有 run 时 run_id 为 null。
        "run_id": run_id,
        "candidate_sha": t.get("candidate_sha"),
        "validated_sha": t.get("validated_sha"),
        "elapsed_seconds": elapsed,
        "quiet_seconds": None if not progress else progress.get("quiet_seconds"),
        "log_bytes": None if not progress else _int_or_none(progress.get("bytes")),
        # 观测事实，不是健康度：`unknown` 表示本版无法判断进程，不是"已退出"。
        "process_state": None if not progress else progress.get("process_state"),
        "observations_stale": None if not progress else progress.get("observations_stale"),
        "empty_candidate": bool(t.get("empty_candidate")),
        # v1.6：并行批次的基线事实（目标分支前进了多少、重叠在哪）。原样转述，
        # 不判断"该不该合"——那是交付策略，不是聚合层的结论。
        "concurrent_base": t.get("concurrent_base"),
        "scope": t.get("task_scope"),
        "scope_source": t.get("scope_source"),
        "content": items,
        "content_note": note,
    }


def _hot_paths(rows: list[dict]) -> list[dict]:
    """同一路径被几个候选触及。

    ``task_scope`` 为空的候选不参与统计：那表示"沿用项目范围"，把它算进来会让
    所有任务看起来都重叠（假信号比没有信号更糟）。计数是**当下**的快照，两次调用
    之间状态会变，它不提供互斥。
    """
    buckets: dict[str, list[str]] = {}
    for row in rows:
        scope = row.get("scope")
        if not scope:
            continue
        for item in scope:
            buckets.setdefault(str(item), []).append(str(row["task_id"]))
    hot = []
    for path in buckets:
        tasks = sorted(set(buckets[path]))
        if len(tasks) >= 2:
            hot.append({"path": path, "tasks": tasks, "count": len(tasks)})
    hot.sort(key=lambda item: (-item["count"], item["path"]))
    return hot


def digest(
    store,
    *,
    project_id: str,
    targets: list[str] | None = None,
    tail_bytes: int | None = None,
    lines: int = 2,
    content: bool = True,
) -> dict:
    """聚合 ``project_id`` 下若干任务的状态与最近内容。

    ``targets`` 缺省取该项目所有非终态任务；显式给出时逐个校验归属，跨项目直接报
    ``PROJECT_MISMATCH``（与 ``task logs --project`` 同语义：静默混入别人的任务会让
    汇报指向错误的项目）。
    """
    window = _logstream.DEFAULT_TAIL_BYTES if tail_bytes is None else int(tail_bytes)
    require(
        1 <= window <= _logstream.MAX_TAIL_BYTES,
        "INPUT_INCOMPLETE",
        f"tail-bytes must be 1..{_logstream.MAX_TAIL_BYTES}",
    )
    require(1 <= lines <= 10, "INPUT_INCOMPLETE", "lines must be 1..10")

    if targets:
        rows = []
        for raw in targets:
            task = store.task(raw)
            if task["project_id"] != project_id:
                raise Error(
                    "PROJECT_MISMATCH",
                    f"task {raw} belongs to {task['project_id']!r}, not {project_id!r}",
                    details={
                        "task_id": raw,
                        "task_project": task["project_id"],
                        "project_id": project_id,
                    },
                )
            rows.append(task)
    else:
        rows = [
            t
            for t in store.tasks()
            if t["project_id"] == project_id and t["state"] not in TERMINAL_STATES
        ]

    now = time.time()
    task_rows = [_task_row(store, t, now, window, lines, content) for t in rows]
    return {
        "api_version": 1,
        "project_id": project_id,
        "generated_at": int(now),
        "tail_bytes": window,
        "lines": lines,
        "content": bool(content),
        "tasks": task_rows,
        "hot_paths": _hot_paths(task_rows),
        "note": (
            "facts only: state/block_code are verbatim; content is a process observation, "
            "not evidence; no output does not prove a hang"
        ),
    }
