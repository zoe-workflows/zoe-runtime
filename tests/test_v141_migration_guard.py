"""v1.4.1：迁移守卫的回归测试（真实入口，不是辅助函数的自证）。

v1.4.0 的 `_placeholder_runs` 用 `row["data"]` 读一条没有 `row_factory` 的
连接，抛出的 `TypeError` 被自己吞掉，守卫恒返回空列表 —— 迁移会在存在活跃
写者时照常放行，而且**测试全绿**。这里的用例因此刻意做三件事：

1. 用**默认连接**（无 row_factory）直接验证辅助函数，锁住下标取值；
2. 走 `migrate()` 真实入口，验证拒绝之后数据库**没有**被推进版本号；
3. 把「读不懂的 run 记录」当阻断而不是「当作没有活跃执行」。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src._migrations import _placeholder_runs, migrate
from src._store import _TABLES
from src._util import Error


class MigrationGuardTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.home = Path(self._td.name) / "state"
        self.home.mkdir()

    def tearDown(self):
        self._td.cleanup()

    # ---- fixture ---------------------------------------------------------
    def _make_home(self, version: int, runs=()) -> None:
        """写一个 v2 数据库：DDL 齐备，但 user_version 停在 2。"""
        db = sqlite3.connect(self.home / "state.db", isolation_level=None)
        try:
            db.executescript(_TABLES)
            db.execute(f"PRAGMA user_version={version}")
            for run_id, data in runs:
                db.execute(
                    "INSERT INTO runs(id, task_id, data) VALUES(?,?,?)",
                    (run_id, "task-1", data),
                )
        finally:
            db.close()

    def _version(self) -> int:
        db = sqlite3.connect(self.home / "state.db")
        try:
            return db.execute("PRAGMA user_version").fetchone()[0]
        finally:
            db.close()

    # ---- 1. 辅助函数：默认连接没有 row_factory ---------------------------
    def test_default_connection_still_finds_active_runs(self):
        """没有 row_factory 的连接也必须读出活跃 run（回归：曾恒返回 []）。"""
        self._make_home(
            2,
            [
                ("run-intent", json.dumps({"status": "spawn_intent"})),
                ("run-observed", json.dumps({"status": "spawn_observed"})),
                ("run-exited", json.dumps({"status": "exited"})),
            ],
        )
        db = sqlite3.connect(self.home / "state.db", isolation_level=None)
        try:
            self.assertIsNone(db.row_factory, "前提：迁移连接没有行工厂")
            found = _placeholder_runs(db)
        finally:
            db.close()
        self.assertEqual(sorted(found), ["run-intent", "run-observed"])

    # ---- 2. 真实入口：存在活跃 run 时拒绝迁移 ---------------------------
    def test_migration_refuses_when_a_writer_may_be_alive(self):
        self._make_home(2, [("run-1", json.dumps({"status": "spawn_observed"}))])
        with self.assertRaises(Error) as caught:
            migrate(self.home, to=3)
        self.assertEqual(caught.exception.code, "MAINTENANCE_REQUIRED")
        self.assertIn("run-1", str(caught.exception))
        self.assertEqual(self._version(), 2, "被拒绝的迁移不得推进版本号")

    def test_every_placeholder_status_is_recognised(self):
        """状态集合来自设计文档；漏一个就等于漏一类活跃写者。"""
        statuses = [
            "reserved",
            "starting",
            "spawn_intent",
            "spawn_observed",
            "acknowledged",
            "running",
            "stopping",
            "unknown",
        ]
        for status in statuses:
            with self.subTest(status=status):
                self._td.cleanup()
                self._td = tempfile.TemporaryDirectory()
                self.home = Path(self._td.name) / "state"
                self.home.mkdir()
                self._make_home(2, [("run-x", json.dumps({"status": status}))])
                with self.assertRaises(Error) as caught:
                    migrate(self.home, to=3)
                self.assertEqual(caught.exception.code, "MAINTENANCE_REQUIRED")

    # ---- 3. 读不懂的记录必须阻断，不能当成「没有活跃执行」 ---------------
    def test_unreadable_run_record_blocks_instead_of_passing(self):
        self._make_home(2, [("run-broken", "{not json")])
        with self.assertRaises(Error) as caught:
            migrate(self.home, to=3)
        self.assertEqual(caught.exception.code, "SCHEMA_UNSUPPORTED")
        self.assertIn("run-broken", str(caught.exception))
        self.assertEqual(self._version(), 2)

    def test_non_object_run_record_blocks(self):
        """合法 JSON 但不是对象，同样无法判定状态 —— 一律阻断。"""
        self._make_home(2, [("run-list", json.dumps([1, 2, 3]))])
        with self.assertRaises(Error) as caught:
            migrate(self.home, to=3)
        self.assertEqual(caught.exception.code, "SCHEMA_UNSUPPORTED")
        self.assertIn("run-list", str(caught.exception))

    # ---- 4. 对照：没有活跃 run 时迁移照常完成 ---------------------------
    def test_migration_proceeds_without_placeholders(self):
        self._make_home(
            2,
            [("run-done", json.dumps({"status": "exited"}))],
        )
        result = migrate(self.home, to=3)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self._version(), 3)
        self.assertTrue(result["backup"].endswith(".db"))


if __name__ == "__main__":
    unittest.main()
