"""Explicit, stop-the-world state migration (v1 -> v2 -> v3).

Migration is never implicit. It refuses to run while a control operation or an
execution lease is held, takes a consistent SQLite backup first (never a copy of
a WAL-active file), migrates in one transaction, verifies integrity afterwards,
and then re-validates every project configuration.

v3 changes the lock protocol (maintenance SH/EX barrier + short-operation locks)
and the dispatch protocol, so the migration takes the ``maintenance`` EX lock
*before* the ``control`` lock, and must refuse when any placeholder run is
present (a run whose status is reserved/starting/spawn_*/running/stopping/unknown).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from src._platform import get_platform
from src._store import _TABLES, PROTOCOL_VERSION, SCHEMA_VERSION, lock_path
from src._util import Error, require

SUPPORTED_TARGETS = (2, 3, 4)

#: Group record versions a v4 database may hold (TGD §12.2): legacy v2 records
#: stay readable but are never rewritten by the migration.
GROUP_RECORD_VERSIONS = (2, 3)

#: Run statuses that mean "a writer may still be active"; migration refuses on these.
_PLACEHOLDER_STATUSES = {
    "reserved",
    "starting",
    "spawn_intent",
    "spawn_observed",
    "acknowledged",
    "running",
    "stopping",
    "unknown",
}


def _lock(home: Path, name: str):
    # Same resolution as `Store.lock_path`: migrate and start must contend on the
    # same files, otherwise the maintenance barrier excludes nothing.
    path = Path(lock_path(home, name))
    path.parent.mkdir(parents=True, exist_ok=True)
    return get_platform().lock_exclusive(str(path), blocking=False)


def _unlock(handle) -> None:
    get_platform().unlock(handle)


def migrate(home, to: int = SCHEMA_VERSION, dry_run: bool = False) -> dict:
    home = Path(home).expanduser().resolve()
    require(
        to in SUPPORTED_TARGETS,
        "CAPABILITY_UNSUPPORTED",
        "Only state schema v2/v3/v4 are supported as migration targets",
    )
    db_path = home / "state.db"
    require(db_path.is_file(), "INPUT_INCOMPLETE", "No state.db in " + str(home))

    # Lock order per 01 §5.2: maintenance EX first (no control held), then control.
    maintenance = None
    control = None
    try:
        try:
            maintenance = _lock(home, "maintenance")
        except Error:
            raise Error(
                "MAINTENANCE_BUSY",
                "Cannot migrate while maintenance.lock is held; stop supervisors first",
            ) from None
        try:
            control = _lock(home, "control")
        except Error:
            raise Error(
                "SLOT_BUSY",
                "Cannot migrate while control.lock is held; stop supervisors first",
            ) from None

        db = sqlite3.connect(db_path, isolation_level=None)
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == to:
                return {
                    "status": "succeeded",
                    "from": version,
                    "to": to,
                    "changed": False,
                    "dry_run": dry_run,
                    "warnings": [],
                }
            require(
                version in (1, 2, 3) and version < to,
                "SCHEMA_UNSUPPORTED",
                f"Cannot migrate schema v{version} to v{to}",
            )

            if to >= 3:
                # Refuse to migrate while any placeholder run exists (04 §5.1).
                placeholders = _placeholder_runs(db)
                require(
                    not placeholders,
                    "MAINTENANCE_REQUIRED",
                    "Cannot migrate while placeholder runs exist: " + ", ".join(placeholders),
                )

            tasks = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            if dry_run:
                return {
                    "status": "succeeded",
                    "from": version,
                    "to": to,
                    "changed": False,
                    "dry_run": True,
                    "tasks": tasks,
                    "plan": [
                        "backup",
                        "create missing tables",
                        "write meta (epoch/protocol_version)",
                        "set user_version",
                        "integrity_check",
                    ],
                }
            backup = home / "backups" / f"state-v{version}-{int(time.time())}.db"
            backup.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(backup) as target:
                db.backup(target)
            db.execute("BEGIN IMMEDIATE")
            try:
                for statement in _TABLES.split(";"):
                    if statement.strip():
                        db.execute(statement)
                if to >= 3:
                    db.execute(
                        "INSERT OR IGNORE INTO meta(key,value) VALUES('epoch', ?)",
                        (uuid.uuid4().hex,),
                    )
                    # v3 -> v4 raises the protocol revision, so this is a REPLACE:
                    # an ``INSERT OR IGNORE`` would leave "4" behind and the new
                    # binary would then refuse the database it just migrated.
                    db.execute(
                        "INSERT OR REPLACE INTO meta(key,value) VALUES('protocol_version', ?)",
                        (PROTOCOL_VERSION,),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO meta(key,value) VALUES('migrated_at', ?)",
                        (str(int(time.time())),),
                    )
                if to >= 4:
                    # DDL is unchanged; the only content check is that every
                    # stored group record is a version this binary understands.
                    _require_group_records(db)
                db.execute(f"PRAGMA user_version={to}")
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            require(integrity == "ok", "SCHEMA_UNSUPPORTED", "Integrity check failed: " + integrity)
            warnings = _validate_projects(home)
            return {
                "status": "succeeded",
                "from": version,
                "to": to,
                "changed": True,
                "dry_run": False,
                "tasks": tasks,
                "backup": str(backup),
                "integrity_check": integrity,
                "warnings": warnings,
            }
        finally:
            db.close()
    finally:
        if control is not None:
            _unlock(control)
        if maintenance is not None:
            _unlock(maintenance)


def _require_group_records(db) -> None:
    """Refuse a v4 migration that would hold a group record it cannot read.

    The migration never rewrites a group record (TGD §12.3): guessing
    ``depends_on`` is not a migration, it is a fabrication. An unknown version is
    therefore a stop, not a conversion.
    """
    try:
        rows = db.execute("SELECT id, data FROM groups").fetchall()
    except sqlite3.OperationalError:
        return
    for row in rows:
        # The migration connection has no ``sqlite3.Row`` factory: index, do not key.
        record_id, data = row[0], row[1]
        try:
            record = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            raise Error(
                "SCHEMA_UNSUPPORTED", f"Group record {record_id} is not readable JSON"
            ) from None
        version = record.get("record_version", 1)
        require(
            version in GROUP_RECORD_VERSIONS,
            "SCHEMA_UNSUPPORTED",
            f"Group record {record_id} has record_version={version}; "
            "supported versions are " + ", ".join(str(v) for v in GROUP_RECORD_VERSIONS),
        )


def _placeholder_runs(db) -> list[str]:
    """返回「可能还有写者」的 run id；读不懂的记录一律阻断迁移。

    迁移连接没有 ``sqlite3.Row`` 工厂（见 ``_require_group_records``），所以
    **按下标取值**。曾经这里用 ``row["data"]``：tuple 取字符串键抛 ``TypeError``，
    又被本函数的 ``except`` 吞掉，守卫因此恒返回空列表 —— 静默失效比没有守卫更
    危险，所以无法解析的记录现在是错误，而不是「跳过」。
    """
    try:
        rows = db.execute("SELECT id, data FROM runs").fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    unreadable = []
    for row in rows:
        run_id, data = row[0], row[1]
        try:
            record = json.loads(data)
        except (json.JSONDecodeError, TypeError, ValueError):
            unreadable.append(str(run_id))
            continue
        if not isinstance(record, dict):
            unreadable.append(str(run_id))
            continue
        status = record.get("status")
        if status in _PLACEHOLDER_STATUSES:
            out.append(str(run_id))
    require(
        not unreadable,
        "SCHEMA_UNSUPPORTED",
        "Cannot migrate: run records are unreadable, so an active writer cannot be "
        "ruled out: " + ", ".join(unreadable),
    )
    return out


def _validate_projects(home: Path) -> list[dict]:
    from src._config import validate_config

    warnings = []
    directory = home / "projects"
    if not directory.is_dir():
        return warnings
    for path in sorted(directory.glob("*.json")):
        try:
            validate_config(json.loads(path.read_text()), base_dir=path.parent)
        except (Error, OSError, json.JSONDecodeError) as exc:
            warnings.append(
                {
                    "project": path.stem,
                    "code": getattr(exc, "code", "INPUT_INCOMPLETE"),
                    "message": str(exc),
                    "handling": "left unchanged; unknown adapters stay IMPLEMENTED and block start",
                }
            )
    return warnings


def restore(home, from_dir: str, confirm: bool = False) -> dict:
    """Restore a consistent backup and rotate the epoch (04 §5.2)."""
    require(confirm, "INPUT_INCOMPLETE", "--confirm required for restore")
    home = Path(home).expanduser().resolve()
    src = Path(from_dir).expanduser().resolve()
    require(src.is_dir(), "INPUT_INCOMPLETE", "Backup directory missing")

    maintenance = _lock(home, "maintenance")
    try:
        control = _lock(home, "control")
    except BaseException:
        # 拿不到 control 时 maintenance 必须当场归还：否则同一进程里后续的
        # 迁移/推进都会被一把没人持有的 EX 锁挡住，直到进程退出才恢复。
        _unlock(maintenance)
        raise
    try:
        db_path = home / "state.db"
        src_db = src / "state.db"
        require(src_db.is_file(), "INPUT_INCOMPLETE", "Backup has no state.db")

        # Back up the current DB before overwriting.
        pre = home / "backups" / f"pre-restore-{int(time.time())}.db"
        pre.parent.mkdir(parents=True, exist_ok=True)
        if db_path.is_file():
            with sqlite3.connect(db_path) as cur, sqlite3.connect(pre) as target:
                cur.backup(target)

        with sqlite3.connect(src_db) as cur, sqlite3.connect(db_path) as target:
            cur.backup(target)

        # Rotate the epoch so old cursors become invalid.
        db = sqlite3.connect(db_path, isolation_level=None)
        try:
            db.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('epoch', ?)",
                (uuid.uuid4().hex,),
            )
            # Clear stale lock ownership records so a restored DB does not claim
            # locks held by a now-dead process.
            for row in db.execute("SELECT id, data FROM runs").fetchall():
                data = json.loads(row["data"])
                if data.get("lock_owner") and data.get("status") not in ("exited",):
                    data["lock_owner"]["state"] = "released"
                    db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(data), row["id"]))
        finally:
            db.close()

        return {
            "status": "succeeded",
            "from": str(src),
            "epoch_rotated": True,
            "pre_restore_backup": str(pre),
            "note": "directly copying state.db does not rotate the epoch and cannot "
            "guarantee old cursors are recognised",
        }
    finally:
        _unlock(control)
        _unlock(maintenance)
