"""v1.5.1：`project attest` 的预览—写入一致性（TOCTOU 收口）。

问题的真实形状：`--dry-run` 展示 `policy_hash=H1`，用户同意；写入前另一进程改了
模型/端点，写入时算出 `H2` 并落盘——用户确认过 A，系统记录了 B。

v1.5.0 只能在**事后**从写入响应的 `policy_hash` 发现这件事，记录已经写进去了。
v1.5.1 增加 `--expect-policy-hash` / `--expect-profiles`：比对与写入在**同一把
control 锁内**完成，不一致则以 `ATTEST_PRECONDITION_FAILED` 拒绝写入。

两条不能越界的性质，各有一条用例：
  * 拒绝时**不落新记录**，也不改动已有记录；
  * 不带 `--expect-*` 时行为与 v1.5.0 一致（不新增失败路径）。
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path

from src import _profiles
from src._util import Error
from src.cli import _project_attest
from src.core import Store, atomic


def profile(**overrides):
    value = {
        "adapter": "codebuddy",
        "backend": "codebuddy-cli",
        "permission_profile": "isolated-edit",
        "model": "m-primary",
    }
    value.update(overrides)
    return value


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "src.txt").write_text("initial\n")
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.db.close)

    def config(self, **overrides):
        value = {
            "schema_version": 2,
            "project_id": "authz",
            "repo": str(self.repo),
            "backend": "local-process",
            "default_agent_profile": "cb",
            "agent_profiles": {"cb": profile()},
            "routing": {"mode": "explicit", "allowed_profiles": ["cb"]},
            "checks": [{"id": "unit", "argv": ["true"]}],
            "allowed_paths": ["src.txt"],
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute"],
        }
        value.update(overrides)
        return value

    def write_config(self, c):
        atomic(self.store.home / "projects" / "authz.json", c)

    def args(self, **overrides):
        values = {
            "project": "authz",
            "profiles": None,
            "dry_run": False,
            "expect_policy_hash": None,
            "expect_profiles": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def current_hash(self):
        return _profiles.policy_digest(self.store.config("authz"))

    def record_on_disk(self):
        return self.store.attestation("authz")


class TestWriteWithoutExpectations(Fixture):
    """不带 `--expect-*` 的行为必须与 v1.5.0 一致：不新增失败路径。"""

    def test_plain_attest_still_writes(self):
        self.write_config(self.config())
        result = _project_attest(self.store, self.args())
        self.assertTrue(result["wrote"])
        self.assertEqual(result["policy_hash"], self.current_hash())
        self.assertIsNotNone(self.record_on_disk())

    def test_plain_attest_records_the_configuration_that_is_on_disk(self):
        """写路径在锁内重读：记录绑定的是"写下时"的配置，不是更早那一刻的。"""
        self.write_config(self.config())
        _project_attest(self.store, self.args())
        before = self.current_hash()
        self.write_config(self.config(agent_profiles={"cb": profile(model="m-replaced")}))
        _project_attest(self.store, self.args())
        after = self.current_hash()
        self.assertNotEqual(before, after)
        self.assertEqual(self.record_on_disk()["policy_hash"], after)


class TestStalePreviewIsRefused(Fixture):
    def test_changed_config_refuses_the_write_and_keeps_the_old_record(self):
        self.write_config(self.config())
        preview = _project_attest(self.store, self.args(dry_run=True))
        self.assertFalse(preview["wrote"])
        # 预览之后配置被改：模型换了，摘要随之变化。
        self.write_config(self.config(agent_profiles={"cb": profile(model="m-expensive")}))
        with self.assertRaises(Error) as ctx:
            _project_attest(self.store, self.args(expect_policy_hash=preview["policy_hash"]))
        self.assertEqual(ctx.exception.code, "ATTEST_PRECONDITION_FAILED")
        details = ctx.exception.details
        self.assertFalse(details["wrote"])
        self.assertEqual(details["field"], "policy_hash")
        self.assertEqual(details["expected"], preview["policy_hash"])
        self.assertEqual(details["observed"], self.current_hash())
        self.assertIn("dry-run", details["next"])
        # 拒绝 = 什么都没写：磁盘上仍然没有确认记录。
        self.assertIsNone(self.record_on_disk())

    def test_a_refused_write_does_not_overwrite_an_existing_record(self):
        self.write_config(self.config())
        first = _project_attest(self.store, self.args())
        before = self.record_on_disk()
        self.write_config(self.config(agent_profiles={"cb": profile(model="m-expensive")}))
        with self.assertRaises(Error):
            _project_attest(self.store, self.args(expect_policy_hash=first["policy_hash"]))
        after = self.record_on_disk()
        self.assertEqual(after["policy_hash"], before["policy_hash"])
        self.assertEqual(after["confirmed_at"], before["confirmed_at"])

    def test_a_matching_preview_writes_the_same_hash(self):
        self.write_config(self.config())
        preview = _project_attest(self.store, self.args(dry_run=True))
        result = _project_attest(
            self.store,
            self.args(
                expect_policy_hash=preview["policy_hash"],
                expect_profiles="cb",
            ),
        )
        self.assertTrue(result["wrote"])
        self.assertEqual(result["policy_hash"], preview["policy_hash"])
        self.assertEqual(result["confirmed_profiles"], preview["confirmed_profiles"])
        self.assertEqual(self.record_on_disk()["policy_hash"], preview["policy_hash"])

    def test_unknown_profiles_are_reported_as_a_set_difference(self):
        """集合差异要能直接读出来：`added` / `removed`，而不是让人自己算。"""
        self.write_config(
            self.config(
                agent_profiles={"cb": profile(), "cb2": profile(model="m-2")},
                routing={"mode": "explicit", "allowed_profiles": ["cb", "cb2"]},
            )
        )
        with self.assertRaises(Error) as ctx:
            _project_attest(
                self.store,
                self.args(profiles="cb", expect_profiles="cb,cb2"),
            )
        details = ctx.exception.details
        self.assertEqual(details["field"], "confirmed_profiles")
        # `expected` 是"用户看过的"，`observed` 是"现在要写的"。
        self.assertEqual(details["expected"], ["cb", "cb2"])
        self.assertEqual(details["observed"], ["cb"])
        # 用户以为包含了 cb2，实际这次写入不含它 —— 那是 removed，不是 added。
        self.assertEqual(details["added"], [])
        self.assertEqual(details["removed"], ["cb2"])
        self.assertIsNone(self.record_on_disk())


class TestDryRunReportsTheExpectation(Fixture):
    """`--dry-run` 带 `--expect-*` 时不拒绝、不写入，但把比对结果报出来。

    这样宿主可以先用一次预览验证"我下一轮写入会不会被拒"，而不必真的写一次。
    """

    def test_dry_run_reports_a_match(self):
        self.write_config(self.config())
        preview = _project_attest(self.store, self.args(dry_run=True))
        again = _project_attest(
            self.store,
            self.args(dry_run=True, expect_policy_hash=preview["policy_hash"]),
        )
        self.assertFalse(again["wrote"])
        self.assertTrue(again["expectation"]["matched"])
        self.assertIsNone(self.record_on_disk())

    def test_dry_run_reports_a_mismatch_without_raising(self):
        self.write_config(self.config())
        stale = "0" * 64
        result = _project_attest(self.store, self.args(dry_run=True, expect_policy_hash=stale))
        self.assertFalse(result["wrote"])
        self.assertFalse(result["expectation"]["matched"])
        self.assertEqual(result["expectation"]["expected"], stale)
        self.assertEqual(result["expectation"]["observed"], self.current_hash())
        self.assertIsNone(self.record_on_disk())

    def test_dry_run_without_expectations_reports_nothing(self):
        self.write_config(self.config())
        result = _project_attest(self.store, self.args(dry_run=True))
        self.assertIsNone(result["expectation"])


class TestNarrowingStillWorks(Fixture):
    """收窄确认范围的老语义不能被这次改动带偏。"""

    def test_profiles_still_narrow_the_record(self):
        self.write_config(
            self.config(
                agent_profiles={"cb": profile(), "cb2": profile(model="m-2")},
                routing={"mode": "explicit", "allowed_profiles": ["cb", "cb2"]},
            )
        )
        result = _project_attest(self.store, self.args(profiles="cb"))
        self.assertEqual(result["confirmed_profiles"], ["cb"])
        self.assertEqual(self.record_on_disk()["confirmed_profiles"], ["cb"])

    def test_widening_is_still_policy_denied(self):
        self.write_config(self.config())
        with self.assertRaises(Error) as ctx:
            _project_attest(self.store, self.args(profiles="cb,ghost"))
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")


if __name__ == "__main__":
    unittest.main()
