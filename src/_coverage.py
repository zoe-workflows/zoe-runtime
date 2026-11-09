"""需求覆盖账本（v1.7）。

"清单已清零"必须是**命令输出**，不是一句判断。

2026-11-06 的事故里，编排者用自己推断的 ``WP-xx`` → ``R-xx`` 映射宣布覆盖
完成；实际 28 条需求里真正验收闭环的是 0 条。推断之所以危险，是因为它不可
反驳：没有一份记录说"R-10 由哪个任务负责、那条任务到哪一步了"。

这个模块只做**集合运算**：不执行命令、不读 worktree、不碰候选树。

* 输入：需求标记清单（``R-01`` / ``D-3`` / …）；
* 事实来源：每个任务冻结 spec 的 ``progress_refs[].marker``，加上任务自身的
  state / acceptance / 逐条判定结论；
* 输出：covered / pending / missing / duplicate / unmapped 五桶。

``covered`` 的定义刻意很窄：``state=DONE`` **且** 验收 accepted **且**（若 spec
要求逐条判定）判定满足。``REVIEW_READY`` 一律算 pending——"代码写完"不是
"需求闭环"。
"""

from __future__ import annotations

import time

from src import _contracts, _task_review
from src._store import Store
from src._util import require


def read_markers(path) -> list[str]:
    """每行一个 marker；忽略空行与 ``#`` 注释，保持首次出现顺序。"""
    found: list[str] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            if value not in found:
                found.append(value)
    require(found, "INPUT_INCOMPLETE", "标记文件里没有任何 marker")
    return found


def _accepted(store: Store, task: dict) -> tuple[bool, dict]:
    """(是否闭环, 判定视图)。"""
    acceptance = task.get("acceptance") or {}
    review = _task_review.summary(store, task)
    done = (
        task.get("state") == "DONE"
        and acceptance.get("decision") == "accepted"
        and bool(review.get("satisfied"))
    )
    return done, review


def _entry(store: Store, task: dict, marker: str) -> dict:
    done, review = _accepted(store, task)
    acceptance = task.get("acceptance") or {}
    return {
        "marker": marker,
        "task_id": task["task_id"],
        "title": task.get("title"),
        "state": task.get("state"),
        "acceptance": acceptance.get("decision"),
        "validated_sha": task.get("validated_sha"),
        "review_required": bool(review.get("required")),
        "review_state": review.get("state"),
        "review_verdict": review.get("verdict"),
        "covered": done,
    }


def _owners(store: Store, project_id: str) -> tuple[dict[str, list[dict]], list[dict]]:
    """``marker -> 认领它的任务``，外加无法进入账本的任务。只读。"""
    by_marker: dict[str, list[dict]] = {}
    unmapped: list[dict] = []
    for task in store.tasks():
        if task.get("project_id") != project_id:
            continue
        document = _task_review.frozen_document(store, task)
        refs = _contracts.progress_refs(document)
        if document is not None and not refs:
            # 结构化 spec 却没声明 progress_refs：这条任务无法进入账本。
            # 它不是错误（纯调研任务可以没有需求标记），但必须被看见。
            unmapped.append(
                {
                    "task_id": task["task_id"],
                    "title": task.get("title"),
                    "state": task.get("state"),
                }
            )
        for ref in refs:
            marker = ref.get("marker")
            if not isinstance(marker, str) or not marker:
                continue
            by_marker.setdefault(marker, []).append(task)
    return by_marker, unmapped


def ledger(store: Store, project_id: str, markers: list[str]) -> dict:
    """按 marker 汇总覆盖关系。只读。"""
    wanted = list(dict.fromkeys(markers))
    wanted_set = set(wanted)
    by_marker, unmapped = _owners(store, project_id)

    covered: list[dict] = []
    pending: list[dict] = []
    duplicate: list[dict] = []
    for marker in wanted:
        owners = by_marker.get(marker) or []
        if not owners:
            continue
        if len(owners) > 1:
            duplicate.append(
                {
                    "marker": marker,
                    "tasks": [task["task_id"] for task in owners],
                }
            )
        entries = [_entry(store, task, marker) for task in owners]
        if any(entry["covered"] for entry in entries):
            covered.append(next(entry for entry in entries if entry["covered"]))
        else:
            pending.extend(entries)

    covered_markers = {entry["marker"] for entry in covered}
    pending_markers = {entry["marker"] for entry in pending}
    missing = [
        marker
        for marker in wanted
        if marker not in by_marker
    ]
    # 声明了但不在本次清单里的 marker：说明任务与需求文档已经不同步。
    extra = sorted(set(by_marker) - wanted_set)

    return {
        "api_version": 2,
        "project_id": project_id,
        "generated_at": time.time(),
        "markers": wanted,
        "covered": covered,
        "pending": pending,
        "missing": missing,
        "duplicate": duplicate,
        "unmapped": unmapped,
        "extra": extra,
        "totals": {
            "declared": len(wanted),
            "covered": len(covered_markers),
            "pending": len(pending_markers),
            "missing": len(missing),
        },
        # 唯一可以被读成"清单清零"的判据。缺一条就是不清零。
        "cleared": not missing and not pending_markers,
    }


def render(body: dict) -> str:
    totals = body["totals"]
    lines = [
        "# 需求覆盖账本",
        "",
        "- 项目：{}".format(body["project_id"]),
        "- 声明 {} 条；闭环 {} / 待验收 {} / 无任务 {}".format(
            totals["declared"], totals["covered"], totals["pending"], totals["missing"]
        ),
        "- 清单清零：{}".format("是" if body["cleared"] else "**否**"),
        "",
        "## 已闭环",
        "",
    ]
    if body["covered"]:
        for entry in body["covered"]:
            lines.append(
                "- {} → {}（{}）".format(entry["marker"], entry["task_id"], entry["validated_sha"])
            )
    else:
        lines.append("- 无")
    lines.extend(["", "## 待验收 / 未闭环", ""])
    if body["pending"]:
        for entry in body["pending"]:
            lines.append(
                "- {} → {}：state={} acceptance={} review={}".format(
                    entry["marker"],
                    entry["task_id"],
                    entry["state"],
                    entry["acceptance"] or "未验收",
                    entry["review_state"],
                )
            )
    else:
        lines.append("- 无")
    lines.extend(["", "## 无任何任务", ""])
    lines.append("- " + ", ".join(body["missing"]) if body["missing"] else "- 无")
    if body["duplicate"]:
        lines.extend(["", "## 同一需求被多个任务认领（口径不一致）", ""])
        for entry in body["duplicate"]:
            lines.append("- {}：{}".format(entry["marker"], ", ".join(entry["tasks"])))
    if body["extra"]:
        lines.extend(["", "## 任务声明了但清单里没有的 marker", ""])
        lines.append("- " + ", ".join(body["extra"]))
    if body["unmapped"]:
        lines.extend(["", "## 无法进入账本的任务（无 progress_refs）", ""])
        for entry in body["unmapped"][:20]:
            lines.append(
                "- {}：{}（state={}）".format(
                    entry["task_id"], entry["title"], entry["state"]
                )
            )
        if len(body["unmapped"]) > 20:
            lines.append("- …共 {} 条".format(len(body["unmapped"])))
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 依赖：`depends_on` 的上游必须**已闭环**，不是"任务存在"
# --------------------------------------------------------------------------- #
#
# 2026-11-06 的事故里，R-27（契约冻结复核）在它推翻的 R-10 还没跑通时写下了
# "已冻结"。当时 runtime 无从知道两者的关系——依赖从未声明，所以这不是"检查
# 缺失"，是"输入缺失"。把依赖写进 spec 之后，`start` 才有东西可查。


def dependency_state(store: Store, task: dict) -> dict:
    """这条任务声明的上游是否已闭环。只读，不执行任何命令。

    "已闭环"沿用 ``ledger`` 的 covered 定义（``DONE`` + ``accepted`` + 判定满足）：
    上游"代码写完了"不算，`REVIEW_READY` 也不算——那正是 R-27 当时的形状。
    """
    document = _task_review.frozen_document(store, task)
    declared = _contracts.depends_on(document)
    if not declared:
        return {"declared": [], "satisfied": True, "blocking": []}

    by_marker, _ = _owners(store, task.get("project_id"))
    blocking: list[dict] = []
    for marker in declared:
        owners = by_marker.get(marker) or []
        if not owners:
            blocking.append(
                {
                    "marker": marker,
                    "reason": "no_task_claims_marker",
                    "hint": (
                        "没有任何任务的 progress_refs 声明这个 marker；"
                        "先建负责它的任务，或在现有任务的 progress_refs 里补上"
                    ),
                }
            )
            continue
        entries = [_entry(store, owner, marker) for owner in owners]
        if any(entry["covered"] for entry in entries):
            continue
        # 多条任务认领同一 marker 时，取"最接近闭环"的一条做说明：
        # 否则报错里出现哪个 task_id 取决于遍历顺序，读起来像随机的。
        best = max(
            entries,
            key=lambda entry: (
                1 if entry["acceptance"] == "accepted" else 0,
                1 if entry["state"] == "DONE" else 0,
            ),
        )
        blocking.append(
            {
                "marker": marker,
                "task_id": best["task_id"],
                "state": best["state"],
                "acceptance": best["acceptance"],
                "review_state": best["review_state"],
                "reason": "upstream_not_closed",
            }
        )
    return {"declared": declared, "satisfied": not blocking, "blocking": blocking}


def _find_cycles(edges: list[dict]) -> list[list[str]]:
    """任务级依赖环。自依赖已在契约层拒绝，这里抓"A 等 B、B 等 A"。"""
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        target = edge.get("to_task_id")
        if target:
            adjacency.setdefault(edge["task_id"], []).append(target)

    found: list[list[str]] = []
    done: set[str] = set()
    on_path: set[str] = set()

    def walk(node: str, path: list[str]) -> None:
        if node in on_path:
            start = path.index(node)
            cycle = path[start:] + [node]
            if cycle not in found:
                found.append(cycle)
            return
        if node in done:
            return
        on_path.add(node)
        path.append(node)
        for nxt in adjacency.get(node, []):
            walk(nxt, path)
        path.pop()
        on_path.discard(node)
        done.add(node)

    for node in list(adjacency):
        walk(node, [])
    return found


def graph(store: Store, project_id: str) -> dict:
    """依赖图 + 阻塞点。只读；用于"一眼看出谁堵了谁"。"""
    by_marker, unmapped = _owners(store, project_id)
    nodes: list[dict] = []
    edges: list[dict] = []
    blocked: list[dict] = []

    for task in store.tasks():
        if task.get("project_id") != project_id:
            continue
        document = _task_review.frozen_document(store, task)
        declared = _contracts.depends_on(document)
        refs = [
            ref.get("marker")
            for ref in _contracts.progress_refs(document)
            if isinstance(ref.get("marker"), str) and ref.get("marker")
        ]
        acceptance = task.get("acceptance") or {}
        nodes.append(
            {
                "task_id": task["task_id"],
                "title": task.get("title"),
                "state": task.get("state"),
                "acceptance": acceptance.get("decision"),
                "markers": refs,
                "depends_on": declared,
            }
        )
        state = dependency_state(store, task)
        blocking_markers = set()
        for item in state["blocking"]:
            blocking_markers.add(item["marker"])
            blocked.append(
                {
                    "task_id": task["task_id"],
                    "marker": item["marker"],
                    "upstream_task_id": item.get("task_id"),
                    "upstream_state": item.get("state"),
                    "upstream_acceptance": item.get("acceptance"),
                    "reason": item["reason"],
                }
            )
        for marker in declared:
            owners = by_marker.get(marker) or []
            edges.append(
                {
                    "task_id": task["task_id"],
                    "marker": marker,
                    "to_task_id": owners[0]["task_id"] if owners else None,
                    "satisfied": marker not in blocking_markers,
                }
            )

    # `edges` 里同一 (task, marker) 只出现一次；环检测用 task 级邻接。
    unique: dict[tuple, dict] = {}
    for edge in edges:
        unique[(edge["task_id"], edge["marker"])] = edge

    return {
        "api_version": 2,
        "project_id": project_id,
        "generated_at": time.time(),
        "nodes": nodes,
        "edges": list(unique.values()),
        "blocked": blocked,
        "cycles": _find_cycles(list(unique.values())),
        "unmapped": unmapped,
        "ready": not blocked,
    }


def render_graph(body: dict) -> str:
    lines = [
        "# 依赖图",
        "",
        "- 项目：{}".format(body["project_id"]),
        "- 节点 {} / 依赖边 {} / 阻塞 {} / 环 {}".format(
            len(body["nodes"]),
            len(body["edges"]),
            len(body["blocked"]),
            len(body["cycles"]),
        ),
        "",
        "## 阻塞点",
        "",
    ]
    if body["blocked"]:
        for item in body["blocked"]:
            upstream = item.get("upstream_task_id") or "（无任务认领）"
            lines.append(
                "- {} ⇠ {}：{}（上游 {} state={} acceptance={}）".format(
                    item["task_id"],
                    item["marker"],
                    item["reason"],
                    upstream,
                    item.get("upstream_state") or "-",
                    item.get("upstream_acceptance") or "未验收",
                )
            )
    else:
        lines.append("- 无")
    if body["cycles"]:
        lines.extend(["", "## 依赖环（必须人工打破）", ""])
        for cycle in body["cycles"]:
            lines.append("- " + " → ".join(cycle))
    lines.extend(["", "## 节点", ""])
    for node in body["nodes"]:
        deps = ", ".join(node["depends_on"]) or "无"
        lines.append(
            "- {}：state={} acceptance={} markers=[{}] depends_on=[{}]".format(
                node["task_id"],
                node["state"],
                node["acceptance"] or "未验收",
                ", ".join(node["markers"]) or "无",
                deps,
            )
        )
    lines.append("")
    return "\n".join(lines)
