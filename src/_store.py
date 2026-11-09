"""SQLite-backed store: tasks, runs, operations, groups, events, meta,
orchestration (schema v3)."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from src._config import validate_config
from src._platform import get_platform
from src._util import Error, digest, is_integer, read, require, safe_id

#: v3 adds the ``meta`` and ``orchestration`` tables and changes the dispatch
#: protocol (``dispatch_state``/``attempt_id``) and lock semantics. The version
#: is a protocol gate: a v0.3 binary must refuse to open a v3 database rather
#: than operate it under the old lock/dispatch protocol.
#:
#: v4 (task-group delivery §4.1) adds **no table and no DDL**: it is purely a
#: semantics gate. A v3 group record (``record_version=3``) stores its members
#: under ``definition``, which a v1.2 binary would silently read as
#: ``ordered_items == []`` and then display "0 items". Raising ``user_version``
#: turns that silent misread into the existing ``SCHEMA_UNSUPPORTED`` refusal.
SCHEMA_VERSION = 4

#: Protocol version written to ``meta``; supervisors compare against it before
#: touching a run, so a stale supervisor cannot mis-drive a new database.
PROTOCOL_VERSION = "5"


def lock_path(home, name: str) -> str:
    """Canonical lock path (01 §5.1), shared by the store and the migrator.

    ``control``/``maintenance`` sit at the home root; a task's writer lock lives
    next to the task's own state as ``tasks/<id>/.<role>.lock``, and a group's
    ``runner`` lock lives next to the group's own artefacts as
    ``groups/<gid>/.<role>.lock`` (TGD §7.1). Both modules must resolve the
    *same* file, or ``state migrate`` and ``task start`` (or ``group start``)
    would exclude each other with nothing.
    """
    for prefix, folder in (("tasks/", "tasks"), ("groups/", "groups")):
        if name.startswith(prefix):
            subject, _, role = name[len(prefix) :].partition("/")
            return str(Path(home) / folder / subject / f".{role}.lock")
    return str(Path(home) / (name + ".lock"))


_TABLES = """
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations(
  id TEXT PRIMARY KEY, hash TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, task_id TEXT, at REAL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS groups(
  id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orchestration(
  project_id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL,
  updated_at REAL NOT NULL);
"""


class Store:
    def __init__(self, home):
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.home, 0o700)
        self.db = sqlite3.connect(self.home / "state.db", timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=15000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self.db.executescript(_TABLES + f"\nPRAGMA user_version={SCHEMA_VERSION};")
            self._init_meta()
        elif version in (1, 2, 3):
            self.db.close()
            raise Error(
                "MIGRATION_REQUIRED",
                f"state.db uses schema v{version}; run `zoe state migrate --to {SCHEMA_VERSION}` "
                "while no task is running",
            )
        elif version != SCHEMA_VERSION:
            self.db.close()
            raise Error("SCHEMA_UNSUPPORTED", str(version))
        else:
            self._check_protocol()

    # -- lifecycle --------------------------------------------------------- #
    def close(self) -> None:
        """关闭连接；可重复调用。

        短命命令里这是卫生问题，``watch --follow`` 与 ``internal-group`` 这类
        长驻路径里则是必须显式归还的资源。``close()`` 之后的 ``self.db`` 是
        ``None``：继续使用会立刻失败，而不是悄悄用一个已关闭的连接。
        """
        db = getattr(self, "db", None)
        if db is not None:
            self.db = None
            db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # -- transactions ------------------------------------------------------ #
    @contextlib.contextmanager
    def tx(self):
        # 先取本地引用并挡掉"已关闭"：``close()`` 之后 self.db 是 None，旧实现对
        # None 调 execute 会在 except 分支里再抛一次，把原始异常掩盖掉。
        require(self.db is not None, "STATE_CONFLICT", "the state store is closed")
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            yield
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    @contextlib.contextmanager
    def lock(self, name="control", blocking=True):
        plat = get_platform()
        path = self.lock_path(name)
        handle = plat.lock_exclusive(path, blocking=blocking)
        try:
            yield handle
        finally:
            plat.unlock(handle)

    def lock_path(self, name: str) -> str:
        """Return the platform lock file path for ``name``."""
        return lock_path(self.home, name)

    def _init_meta(self) -> None:
        """Write the epoch and protocol version into ``meta`` on first create."""
        self.db.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('epoch', ?)", (uuid.uuid4().hex,)
        )
        self.db.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('protocol_version', ?)",
            (PROTOCOL_VERSION,),
        )

    def _check_protocol(self) -> None:
        row = self.db.execute("SELECT value FROM meta WHERE key='protocol_version'").fetchone()
        require(
            row is not None and row["value"] == PROTOCOL_VERSION,
            "PROTOCOL_UNSUPPORTED",
            "meta.protocol_version mismatch; this binary cannot operate this database",
        )

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))

    def epoch(self) -> str:
        return self.meta("epoch") or ""

    # -- tasks ------------------------------------------------------------- #
    def task(self, id):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (safe_id(id),)).fetchone()
        require(row, "TASK_NOT_FOUND", id)
        t = json.loads(row["data"])
        t["row_version"] = row["version"]
        return t

    def tasks(self):
        """Every task record, in one query.

        旧实现是 ``SELECT id`` 再逐行 ``self.task(id)``：T 条任务要发 1+T 次查询。
        ``slots()`` / ``spend_totals()`` / 组推进器每轮都会走这里，任务表一旦累积
        到几百条就是可观测的固定开销。
        """
        rows = self.db.execute("SELECT id, version, data FROM tasks").fetchall()
        values = []
        for row in rows:
            value = json.loads(row["data"])
            value["row_version"] = row["version"]
            values.append(value)
        return values

    def save(
        self,
        t,
        event="updated",
        operation=None,
        run=None,
        payload=None,
        run_id=None,
        actor_kind="controller",
        request_id=None,
    ):
        old = t["row_version"]
        # 先在局部变量里算出新版本：CAS 失败时调用方的 dict 必须保持原样。
        # 旧实现在事务外就地改写 ``row_version``，一旦 UPDATE 撞上版本冲突，
        # 内存对象已经比库里多 1——调用方若在同一对象上重试（``release_unstarted``
        # 就是这么循环的），每次都会再失败一次，且永远不会自愈。
        new = old + 1
        stamp = time.time()
        with self.tx():
            body = json.dumps({**t, "row_version": new, "updated_at": stamp})
            cur = self.db.execute(
                "UPDATE tasks SET version=?,data=? WHERE id=? AND version=?",
                (new, body, t["task_id"], old),
            )
            require(cur.rowcount == 1, "STATE_CONFLICT", "Refresh task version")
            t["row_version"] = new
            t["updated_at"] = stamp
            if run is not None:
                self.run(run)
            if operation is not None:
                self.finish(operation[0], operation[1])
            self.event(
                t["task_id"],
                {
                    "event": event,
                    "type": event,
                    "payload_version": 2,
                    # 归属在写入时确定，读侧不必靠"任务集合反查"猜项目：
                    # 组事件与项目级事件没有 task_id，靠反查会漏（v2 §3-1）。
                    "project_id": t.get("project_id"),
                    "state": t["state"],
                    "phase": t["phase"],
                    "state_phase": [t["state"], t["phase"]],
                    "run_id": run_id,
                    "actor_kind": actor_kind,
                    "request_id": request_id,
                    # Stamped centrally so every state change carries which
                    # (adapter, backend) produced it (design §14: Event.payload
                    # may carry `adapter_backend`).
                    "adapter_id": t.get("adapter_id"),
                    "adapter_backend": t.get("adapter_backend"),
                    # v0.5.1：阻塞原因随事件一起落盘。此前只能读 SQLite 的
                    # tasks 行才知道"为什么 BLOCKED"，而 skill 层把 task events
                    # 当作排障证据——证据里没有结论等于没有证据。
                    "block_code": t.get("block_code"),
                    "last_error": t.get("last_error"),
                    **(payload or {}),
                },
            )
        return t

    # -- runs -------------------------------------------------------------- #
    def run(self, r):
        self.db.execute(
            "INSERT OR REPLACE INTO runs VALUES(?,?,?)", (r["run_id"], r["task_id"], json.dumps(r))
        )

    def get_run(self, id):
        row = self.db.execute("SELECT data FROM runs WHERE id=?", (id,)).fetchone()
        return json.loads(row[0]) if row else None

    def runs(self, task_id):
        rows = self.db.execute(
            "SELECT data FROM runs WHERE task_id=? ORDER BY rowid", (task_id,)
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    # -- operations -------------------------------------------------------- #
    def _operation_conflict(self, key: str, row) -> Error:
        """``REQUEST_CONFLICT`` quoting the row that already owns ``key``.

        The code alone is not actionable: "Request ID reused with different
        payload" does not say *which* task or action holds the id, so a host can
        only guess or go read the database. The owning row's identity is not a
        secret — it is the caller's own earlier request — and it is what turns
        the error into "this file belongs to T9d's start, not T9a's".
        """
        try:
            prior = json.loads(row["data"]) if row is not None else {}
        except (TypeError, ValueError):  # pragma: no cover - corrupted row
            prior = {}
        return Error(
            "REQUEST_CONFLICT",
            "Request ID reused with different payload",
            details={
                "request_id": key,
                "existing": {
                    "task_id": prior.get("task_id"),
                    "action": prior.get("action"),
                    "status": prior.get("status"),
                    "created_at": prior.get("created_at"),
                },
            },
        )

    def _operation_key(self, request, action, task=None):
        """Derive ``(storage key, request hash)`` exactly once, for both paths.

        The key is the ``request_id`` alone; ``action`` and the payload go into
        the hash. One id must therefore never be reused across actions: that is
        ``REQUEST_CONFLICT``, not "each action registers separately" (§7.3).
        """
        require(
            is_integer(request.get("schema_version")) and request.get("schema_version") in (1, 2),
            "INPUT_INCOMPLETE",
            f"schema_version must be 1 or 2, got {request.get('schema_version')!r}",
        )
        key = safe_id(request.get("request_id"))
        return key, digest([action, task, request])

    def operation(self, request, action, task=None):
        key, h = self._operation_key(request, action, task)
        row = self.db.execute("SELECT * FROM operations WHERE id=?", (key,)).fetchone()
        if row:
            if row["hash"] != h:
                raise self._operation_conflict(key, row)
            prior = json.loads(row["data"])
            return key, None if prior.get("status") == "intent" else prior
        value = {
            "status": "intent",
            "task_id": task,
            "action": action,
            "request_id": key,
            # Recorded so an abandoned dispatch intent can be aged out by
            # `reconcile` instead of masquerading as an active run (§7.4-4).
            "created_at": time.time(),
        }
        # 单条 INSERT 本身就是原子的（autocommit），不需要再开事务；但唯一键冲突
        # 抛的是 ``sqlite3.IntegrityError``，调用方按 ``Error.code`` 分支的 except
        # 会漏接，用户看到的是一个 traceback 而不是 REQUEST_CONFLICT。
        try:
            self.db.execute("INSERT INTO operations VALUES(?,?,?)", (key, h, json.dumps(value)))
        except sqlite3.IntegrityError:
            row = self.db.execute("SELECT * FROM operations WHERE id=?", (key,)).fetchone()
            if row is None or row["hash"] != h:
                raise self._operation_conflict(key, row) from None
            prior = json.loads(row["data"])
            return key, None if prior.get("status") == "intent" else prior
        return key, None

    def operation_prior(self, request, action, task=None):
        """Read-only idempotency probe. Returns ``(key, prior_or_None, status)``.

        Same key derivation and hash conflict rule as :meth:`operation`, but it
        never inserts an intent row: a replay must not mutate state. ``status``
        is the raw row status, so ``intent`` is distinguishable from "no row" —
        the writing variant collapses both to ``None`` (§7.4).
        """
        key, h = self._operation_key(request, action, task)
        row = self.db.execute("SELECT * FROM operations WHERE id=?", (key,)).fetchone()
        if row is None:
            return key, None, None
        if row["hash"] != h:
            raise self._operation_conflict(key, row)
        data = json.loads(row["data"])
        status = data.get("status")
        if status == "intent":
            return key, None, "intent"
        return key, data, status

    def operation_intents(self, task_id: str) -> list[tuple[str, dict]]:
        """Every still-unfinished operation row recorded for ``task_id``.

        Used by ``reconcile`` to close out dispatch intents that were persisted
        but never turned into a run (§7.4-4). Read-only.
        """
        query = (
            "SELECT id, data FROM operations "
            "WHERE json_extract(data,'$.status')='intent' "
            "AND json_extract(data,'$.task_id')=?"
        )
        try:
            rows = self.db.execute(query, (task_id,)).fetchall()
        except sqlite3.OperationalError:
            # 没有 JSON1 扩展的构建：退回全表扫描。``operations`` 只增不减，
            # 这条路径在 reconcile 的每个成员上都会走，所以只在兜底时才用。
            rows = self.db.execute("SELECT id, data FROM operations").fetchall()
        found = []
        for row in rows:
            try:
                data = json.loads(row["data"])
            except ValueError:
                continue
            if data.get("status") == "intent" and data.get("task_id") == task_id:
                found.append((row["id"], data))
        return found

    def finish(self, key, result):
        self.db.execute("UPDATE operations SET data=? WHERE id=?", (json.dumps(result), key))

    # -- groups ------------------------------------------------------------ #
    def group(self, id):
        row = self.db.execute("SELECT * FROM groups WHERE id=?", (safe_id(id),)).fetchone()
        require(row, "GROUP_NOT_FOUND", id)
        g = json.loads(row["data"])
        g["row_version"] = row["version"]
        return g

    def groups(self):
        rows = self.db.execute("SELECT id, version, data FROM groups").fetchall()
        values = []
        for row in rows:
            value = json.loads(row["data"])
            value["row_version"] = row["version"]
            values.append(value)
        return values

    def save_group(self, g, event="group_updated"):
        old = g.get("row_version", 0)
        # 同 `save()`：新版本先留在局部变量，CAS 成功后才写回调用方的 dict，
        # 否则 ``release_unstarted`` 那种"在同一对象上循环保存"的路径一旦撞一次
        # 冲突就会永久偏移，本轮写入全部丢失。
        new = old + 1
        if old == 0:
            value = json.dumps({**g, "row_version": 1})
            self.db.execute("INSERT INTO groups VALUES(?,?,?)", (g["group_id"], 1, value))
            g["row_version"] = 1
        else:
            value = json.dumps({**g, "row_version": new})
            cur = self.db.execute(
                "UPDATE groups SET version=?,data=? WHERE id=? AND version=?",
                (new, value, g["group_id"], old),
            )
            require(cur.rowcount == 1, "STATE_CONFLICT", "Refresh group version")
            g["row_version"] = new
        self.event(
            None,
            {
                "event": event,
                "type": event,
                "payload_version": 2,
                "group_id": g["group_id"],
                "project_id": g.get("project_id"),
            },
        )
        return g

    # -- events ------------------------------------------------------------ #
    def event(self, task_id, record):
        """Append one event row. Called inside an existing transaction."""
        self.db.execute(
            "INSERT INTO events(task_id,at,data) VALUES(?,?,?)",
            (task_id, time.time(), json.dumps(record)),
        )

    def _event(self, task_id, record):
        self.event(task_id, record)

    def max_event_id(self, task_id=None) -> int:
        if task_id is None:
            row = self.db.execute("SELECT MAX(id) AS m FROM events").fetchone()
        else:
            row = self.db.execute(
                "SELECT MAX(id) AS m FROM events WHERE task_id=?", (task_id,)
            ).fetchone()
        return int(row["m"] or 0)

    def events(self, task_id, after=0, limit=100):
        """Page events by monotonic id. It is a pull interface, not a push one.

        ``task_id=None`` pages across all tasks (used by ``watch``); otherwise the
        query is scoped to one task.
        """
        require(1 <= int(limit) <= 500, "INPUT_INCOMPLETE", "limit must be 1..500")
        if task_id is None:
            rows = self.db.execute(
                "SELECT id, at, data FROM events WHERE id>? ORDER BY id LIMIT ?",
                (int(after), int(limit) + 1),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT id, at, data FROM events WHERE task_id=? AND id>? ORDER BY id LIMIT ?",
                (task_id, int(after), int(limit) + 1),
            ).fetchall()
        has_more = len(rows) > int(limit)
        rows = rows[: int(limit)]
        events = [{"id": r["id"], "at": r["at"], **json.loads(r["data"])} for r in rows]
        return events, (events[-1]["id"] if events else int(after)), has_more

    def _owners_signature(self) -> tuple[int, int, int, int]:
        """Cheap invalidation key for :meth:`_owners`.

        只扫整数列（COUNT/SUM(version)），不碰 ``data``：**任何**一次
        ``save``/``save_group`` 都会让版本前进，所以摘要变了就说明映射可能变了。
        """
        t = self.db.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(version),0) AS s FROM tasks"
        ).fetchone()
        g = self.db.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(version),0) AS s FROM groups"
        ).fetchone()
        return (int(t["c"]), int(t["s"]), int(g["c"]), int(g["s"]))

    def _owners(self) -> tuple[dict, dict[str, str]]:
        """Legacy attribution: ``task_id -> (project_id, group_id)`` and group's project.

        Events written before the explicit ``project_id`` field must still be
        attributable, otherwise a project filter would silently drop history.

        ``watch --follow`` 每次 poll 都会带着 project/group/task 过滤调用
        ``events_filtered``，而旧实现每次调用都把 tasks 与 groups **全表**拉出来
        逐行 ``json.loads``——即使这一轮一个新事件都没有。这里按
        :meth:`_owners_signature` 做进程内缓存：任务只在状态迁移时版本前进，
        所以同一条监控循环里绝大多数轮次都能直接命中。
        """
        signature = self._owners_signature()
        cached = getattr(self, "_owners_cache", None)
        if cached is not None and cached[0] == signature:
            return cached[1]
        tasks: dict[str, tuple[str | None, str | None]] = {}
        for row in self.db.execute("SELECT id, data FROM tasks").fetchall():
            try:
                data = json.loads(row["data"])
            except ValueError:
                continue
            tasks[row["id"]] = (data.get("project_id"), data.get("group_id"))
        groups: dict[str, str] = {}
        for row in self.db.execute("SELECT id, data FROM groups").fetchall():
            try:
                data = json.loads(row["data"])
            except ValueError:
                continue
            groups[row["id"]] = data.get("project_id")
        self._owners_cache = (signature, (tasks, groups))
        return tasks, groups

    def events_filtered(
        self,
        after=0,
        limit=100,
        *,
        project_id: str | None = None,
        group_id: str | None = None,
        task_id: str | None = None,
    ):
        """Page events with optional project / group / task filters.

        Returns ``(events, next_cursor_id, has_more, unattributed)``. Semantics
        that callers depend on (v2 §3-1):

        * filters are combined with **and**;
        * ``next_cursor_id`` advances over **scanned** rows, never over matched
          rows only — a page with zero matches must still move forward, or a
          host would rescan the same unrelated tail forever;
        * rows after a full page of matches are left for the next call, so no
          matching event is skipped;
        * ``unattributed`` counts scanned events that carry no project at all;
          they are never guessed into a project.
        """
        require(1 <= int(limit) <= 500, "INPUT_INCOMPLETE", "limit must be 1..500")
        if project_id is None and group_id is None and task_id is None:
            events, cursor, has_more = self.events(None, after, limit)
            return events, cursor, has_more, 0
        tasks, groups = self._owners()
        scanned = int(after)
        matched: list[dict] = []
        unattributed = 0
        page = min(500, max(int(limit), 100))
        stopped_early = False
        while not stopped_early:
            rows = self.db.execute(
                "SELECT id, at, task_id, data FROM events WHERE id>? ORDER BY id LIMIT ?",
                (scanned, page),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                scanned = int(row["id"])
                try:
                    record = json.loads(row["data"])
                except ValueError:
                    record = {}
                owner_project, owner_group = _ownership(record, row["task_id"], tasks, groups)
                if owner_project is None:
                    unattributed += 1
                if not _matches(
                    record,
                    row["task_id"],
                    owner_project,
                    owner_group,
                    project_id=project_id,
                    group_id=group_id,
                    task_id=task_id,
                ):
                    continue
                matched.append({"id": row["id"], "at": row["at"], **record})
                if len(matched) >= int(limit):
                    stopped_early = True
                    break
            if len(rows) < page:
                break
        if stopped_early:
            has_more = True
        else:
            has_more = bool(
                self.db.execute("SELECT 1 FROM events WHERE id>? LIMIT 1", (scanned,)).fetchone()
            )
        return matched, scanned, has_more, unattributed

    # -- orchestration ----------------------------------------------------- #
    def orchestration(self, project_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM orchestration WHERE project_id=?", (safe_id(project_id),)
        ).fetchone()
        if not row:
            return None
        data = json.loads(row["data"])
        data["version"] = row["version"]
        data["updated_at"] = row["updated_at"]
        return data

    def upsert_orchestration(self, project_id: str, expected_version: int, data: dict) -> dict:
        """Decision-level CAS: bump version and write, returning the new record.

        Raises ``DECISION_CONFLICT`` when ``expected_version`` does not match the
        current stored version. Uses a single ``UPDATE ... WHERE version=?`` to
        guarantee atomicity (07 §2.1).
        """
        current = self.orchestration(project_id)
        current_version = current["version"] if current else 0
        require(
            current_version == expected_version,
            "DECISION_CONFLICT",
            "Orchestration version conflict; refresh expected_version",
        )
        new_version = current_version + 1
        payload = dict(data)
        payload["version"] = new_version
        payload["updated_at"] = time.time()
        stored = json.dumps(
            {k: v for k, v in payload.items() if k not in ("version", "updated_at")}
        )
        if current is None:
            # 两个并发"首次规划"都可能读到 None：单条 INSERT 是原子的，后到的
            # 那次会撞唯一键。翻译成 DECISION_CONFLICT，而不是把
            # ``sqlite3.IntegrityError`` 漏给调用方。
            try:
                self.db.execute(
                    "INSERT INTO orchestration(project_id,version,data,updated_at) VALUES(?,?,?,?)",
                    (safe_id(project_id), new_version, stored, payload["updated_at"]),
                )
            except sqlite3.IntegrityError:
                raise Error(
                    "DECISION_CONFLICT",
                    "Orchestration was created concurrently; refresh expected_version",
                ) from None
        else:
            cur = self.db.execute(
                "UPDATE orchestration SET version=?, data=?, updated_at=? "
                "WHERE project_id=? AND version=?",
                (new_version, stored, payload["updated_at"], safe_id(project_id), current_version),
            )
            require(cur.rowcount == 1, "DECISION_CONFLICT", "Orchestration version conflict")
        return self.orchestration(project_id)

    # -- paths ------------------------------------------------------------- #
    def path(self, t):
        return self.home / "tasks" / t["task_id"]

    def config(self, project, *, problems=None):
        path = self.home / "projects" / (safe_id(project) + ".json")
        # v1.5.0 M0：读路径不因新增的策略规则失败。结构问题（没有 repo、没有
        # checks）照旧抛——那种配置确实不可用；但"模型字段组合不合法""白名单
        # 没显式声明"只记进 problems，否则旧配置升级后连 status/doctor 都打不开。
        # 真正的拦截点在执行入口（_config.check_execution_contract）。
        #
        # ``problems`` 必须由调用方传入：不传就等于把这些诊断丢掉（旧实现就是
        # 这样，`config_problems` 只能自己再读一遍）。诊断调用方传一个 list 即可。
        #
        # 缓存只在 (mtime_ns, size) 未变时命中：配置是用 ``atomic()`` 整体替换写的，
        # 内容一变这两个值必变。缓存的是**校验后的 JSON 文本**而不是对象本身——
        # 调用方历史上拿到的是一份独立副本，直接复用同一个 dict 会让某处的就地
        # 修改污染后续读取（而 ``validate_config`` 里还有 ``Path.resolve()``、
        # ``is_dir()`` 这类系统调用，正是要省掉的部分）。
        stat = path.stat() if path.is_file() else None
        # inode 也要进 key：配置是用 ``atomic()``（临时文件 + replace）整体替换的，
        # 每次写入都换 inode。只靠 mtime/size 在时间戳精度粗的文件系统上（或同尺寸
        # 覆盖写）会把"改过的配置"读成旧值，而配置摘要正是 POLICY_CHANGED 的依据。
        key = (
            (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if stat
            else (str(path), None, None, None)
        )
        cache = getattr(self, "_config_cache", None)
        if cache is None:
            cache = {}
            self._config_cache = cache
        cached = cache.get(key)
        if cached is not None:
            text, cached_problems = cached
            if problems is not None:
                problems.extend(dict(item) for item in cached_problems)
            return json.loads(text)
        found: list[dict] = []
        value = validate_config(read(path), base_dir=path.parent, strict=False, problems=found)
        if problems is not None:
            problems.extend(dict(item) for item in found)
        text = json.dumps(value)
        cache[key] = (text, found)
        # 有界：一个 Store 生命周期里通常只碰一两个项目，但 `status --refresh`
        # 之类会跨项目读；超过上限就淘汰最早写入的那一条。
        if len(cache) > 8:
            cache.pop(next(iter(cache)))
        return json.loads(text)

    def config_problems(self, project):
        """v1.5.0 M0：读路径发现的策略问题（不抛错，供 doctor/status 展示）。"""
        problems: list[dict] = []
        self.config(project, problems=problems)
        return problems

    def attestation_path(self, project):
        return self.home / "projects" / (safe_id(project) + ".attest.json")

    def attestation(self, project):
        """读取授权确认记录（v1.5.0 §5）。

        缺失返回 ``None``；**读不懂**返回带 ``_corrupt`` 的记录。"没有记录"与
        "记录坏了"必须可区分：两者都是未确认，但只有后者说明磁盘上有一份坏数据，
        否则授权被默默降级成一个"再确认一次"的动作，没有任何可观测信号。
        """
        path = self.attestation_path(project)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return {"_corrupt": True, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(data, dict):
            return {"_corrupt": True, "error": "attestation record is not an object"}
        return data

    def write_attestation(self, project, payload):
        """写入授权确认记录。与项目配置分开存放：配置内容不变也能确认一次。

        写入侧校验最小 schema：读取侧按 fail-closed 处理坏记录，写侧若允许写出
        坏记录，等于把"记录不可信"变成常态。
        """
        from src._util import atomic

        require(
            isinstance(payload, dict), "INPUT_INCOMPLETE", "attestation payload must be an object"
        )
        require(
            isinstance(payload.get("policy_hash"), str) and bool(payload["policy_hash"]),
            "INPUT_INCOMPLETE",
            "attestation requires a non-empty policy_hash",
        )
        profiles = payload.get("confirmed_profiles")
        require(
            isinstance(profiles, list)
            and bool(profiles)
            and all(isinstance(item, str) and item for item in profiles),
            "INPUT_INCOMPLETE",
            "attestation requires non-empty confirmed_profiles",
        )
        path = self.attestation_path(project)
        atomic(path, payload)
        return payload


def _ownership(record: dict, row_task_id, tasks: dict, groups: dict):
    """Resolve ``(project_id, group_id)`` for one stored event row.

    Order matters: the explicit field wins (new events), then the task row, then
    the group row. Anything else stays ``None`` and is counted as unattributed
    instead of being assigned to whichever project happens to be obvious.
    """
    project = record.get("project_id")
    group = record.get("group_id")
    owned = tasks.get(row_task_id) if row_task_id else None
    if owned is not None:
        project = project or owned[0]
        group = group or owned[1]
    if project is None and group:
        project = groups.get(group)
    return project, group


def _matches(record, row_task_id, project, group, *, project_id, group_id, task_id) -> bool:
    """Whether one event satisfies every declared filter (logical and)."""
    if project_id is not None and project != project_id:
        return False
    if group_id is not None and group != group_id:
        return False
    if task_id is not None and row_task_id != task_id:
        return False
    return True
