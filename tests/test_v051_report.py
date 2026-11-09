"""v0.5.1 W4: the delivery report must be pasteable and honest.

The three things that can go wrong here are all absences: a verdict that was
never recorded, a verification that never ran, and an end time that was never
observed. Each must be printed as itself, not as PASS, "green" or a duration.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from test_runtime import FakePublisher

from src import _reports, cli
from src._util import atomic, git
from src.core import Store, create, run_pipeline

CHECKS = [
    {"id": "lint", "argv": ["true"]},
    {"id": "security", "argv": ["true"]},
    {"id": "slice_a", "argv": ["true"]},
    {"id": "slice_b", "argv": ["true"]},
]


class Backend:
    def __init__(self):
        self.calls = []

    def inspect(self, name):
        return None

    def execute(self, store, t, r, argv, log_name, worker=False, prompt=None):
        self.calls.append(log_name)
        if worker:
            (Path(t["workspace"]) / "src.txt").write_text("implemented\n")
        return 0


class ReportFixture(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "test")
        git(self.repo, "config", "user.email", "test@localhost")
        (self.repo / "src.txt").write_text("initial\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        self.store = Store(self.root / "state")
        self.spec = self.root / "spec.json"
        self.config = {
            "schema_version": 1,
            "project_id": "demo",
            "repo": str(self.repo),
            "backend": "local-process",
            "network": "none",
            "compatibility_verified": True,
            "agent": "codebuddy",
            "checks": [dict(check) for check in CHECKS],
            "allowed_paths": ["src.txt"],
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute", "publish", "accept"],
            "ci_required": False,
            "max_attempts": 3,
        }
        atomic(self.store.home / "projects/demo.json", self.config)

    def tearDown(self):
        self.store.db.close()
        self._td.cleanup()

    def write_spec(self, scope):
        acs = list(scope) if scope is not None else [check["id"] for check in CHECKS]
        document = {
            "schema_version": 2,
            "title": "Slice A",
            "acceptance_criteria": [
                {
                    "id": "AC-" + check_id,
                    "statement": "implement " + check_id,
                    "verification": {"type": "check", "check_id": check_id},
                }
                for check_id in acs
            ]
            + [
                {
                    "id": "AC-manual",
                    "statement": "error text reviewed",
                    "verification": {"type": "manual", "instructions": "人工确认错误文案"},
                }
            ],
        }
        if scope is not None:
            document["verification_scope"] = {"checks": list(scope)}
        self.spec.write_text(json.dumps(document))

    def new(self, scope=("lint", "slice_a")):
        self.write_spec(None if scope is None else list(scope))
        result = create(
            self.store,
            {
                "schema_version": 1,
                "request_id": "req-" + uuid.uuid4().hex,
                "authorization_ref": "scope-1",
                "project_id": "demo",
                "title": "Slice A",
                "spec_file": str(self.spec),
                "delivery_mode": "local_artifact",
            },
        )
        return self.store.task(result["task_id"])

    def run_task(self, t):
        t = self.store.task(t["task_id"])
        rid = "run-" + uuid.uuid4().hex[:16]
        self.store.run(
            {
                "run_id": rid,
                "task_id": t["task_id"],
                "status": "running",
                "started_at": time.time(),
            }
        )
        t.update(current_run_id=rid, state="RUNNING")
        self.store.save(t)
        return run_pipeline(self.store, t["task_id"], Backend(), FakePublisher())

    def render(self, t):
        return _reports.render(_reports.snapshot(self.store, t))


class TestDeliveryReport(ReportFixture):
    def test_two_check_acs_one_manual_and_two_not_run(self):
        t = self.run_task(self.new(scope=("lint", "slice_a")))
        self.assertEqual(t["state"], "REVIEW_READY")
        text = self.render(t)

        self.assertIn("| AC-lint | PASS | check `lint` exit 0 |", text)
        self.assertIn("| AC-slice_a | PASS | check `slice_a` exit 0 |", text)
        self.assertIn("| AC-manual | 未记录逐项人工结论 | manual：人工确认错误文案 |", text)
        # Manual criteria never inherit the task-level verdict as PASS.
        self.assertNotIn("| AC-manual | PASS |", text)
        self.assertIn("任务整体验收：未验收", text)

        # Checks that no AC references live in the detail table only.
        self.assertIn("| security | NOT_RUN | — | 未纳入本任务声明范围 |", text)
        self.assertIn("| slice_b | NOT_RUN | — | 未纳入本任务声明范围 |", text)
        self.assertNotIn("| AC-security |", text)

        self.assertIn("Verification scope: 显式范围 2/4（未执行: security, slice_b）", text)
        self.assertIn("Project-wide verification: **NOT_RUN**", text)
        self.assertIn("来源: observed", text)
        self.assertIn("交付边界声明", text)

    def test_no_declaration_reads_as_a_full_run(self):
        text = self.render(self.run_task(self.new(scope=None)))
        self.assertIn("Verification scope: 全量 4/4", text)
        self.assertIn("PASS（本 run 执行了项目全部 check）", text)
        self.assertNotIn("NOT_RUN", text)

    def test_a_declaration_covering_everything_still_reads_as_a_declaration(self):
        text = self.render(
            self.run_task(self.new(scope=("lint", "slice_a", "security", "slice_b")))
        )
        self.assertIn("显式范围 4/4（未执行: 无）", text)

    def test_a_reconciled_run_has_no_duration(self):
        t = self.run_task(self.new())
        run = self.store.get_run(t["current_run_id"])
        run.pop("ended_at", None)
        run["reconciled_at"] = time.time()
        self.store.run(run)
        text = self.render(t)
        self.assertRegex(text, r"Started / Ended: [^\n]*/ unknown[^\n]*")
        self.assertRegex(text, r"- Reconciled at: [^\n]*非真实退出时间[^\n]*")
        self.assertNotRegex(text, r"（\d+s，来源: observed）")

    def test_the_file_is_a_view_of_the_latest_run(self):
        t = self.run_task(self.new())
        first = self.store.get_run(t["current_run_id"])["run_id"]
        receipt = _reports.persist(self.store, t, _reports.snapshot(self.store, t), self.render(t))
        path = Path(receipt["report_path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "delivery-1.md")
        self.assertIn("Run: " + first, path.read_text())

        # A second attempt overwrites the same view and points at the new run.
        t = self.store.task(t["task_id"])
        t.update(state="READY", phase="verify", validated_sha=None)
        self.store.save(t)
        t = self.run_task(t)
        second = self.store.get_run(t["current_run_id"])["run_id"]
        _reports.persist(self.store, t, _reports.snapshot(self.store, t), self.render(t))
        text = path.read_text()
        self.assertIn("Run: " + second, text)
        self.assertNotIn("Run: " + first, text)
        self.assertFalse((self.store.path(t) / "report.md").exists())

    def test_the_report_never_borrows_another_runs_evidence(self):
        t = self.run_task(self.new())
        # Move the task to a new run that has no evidence of its own yet.
        rid = "run-" + uuid.uuid4().hex[:16]
        self.store.run(
            {
                "run_id": rid,
                "task_id": t["task_id"],
                "status": "running",
                "started_at": time.time(),
            }
        )
        t.update(current_run_id=rid, state="RUNNING", phase="implement")
        self.store.save(t)
        text = self.render(self.store.task(t["task_id"]))
        self.assertIn("Evidence: none (this run)", text)
        self.assertIn("Verification scope: NOT_RUN", text)
        self.assertIn("| AC-lint | 无本期证据 |", text)


class TestTruncation(ReportFixture):
    def test_blockers_and_boundaries_survive_a_16k_cut(self):
        t = self.run_task(self.new())
        snap = _reports.snapshot(self.store, t)
        snap["rows"] = [
            {
                "id": f"check-{index:04d}-{'x' * 60}",
                "status": "NOT_RUN",
                "exit_code": None,
                "reason": "not in declared verification_scope",
            }
            for index in range(2000)
        ]
        text = _reports.render(snap)
        self.assertLessEqual(len(text.encode("utf-8")), _reports.MAX_REPORT_BYTES)
        self.assertIn("truncated: true", text)
        self.assertIn("## 交付边界声明", text)
        self.assertIn("## 阻断与未完成", text)


class TestCliSurface(ReportFixture):
    def call(self, **kwargs):
        args = SimpleNamespace(
            id=self.task_id, refresh=False, format="json", action="report", write=False
        )
        for key, value in kwargs.items():
            setattr(args, key, value)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli._status_or_report(self.store, args)
        return buffer.getvalue()

    def setUp(self):
        super().setUp()
        self.task = self.run_task(self.new())
        self.task_id = self.task["task_id"]

    def test_json_without_write_is_still_the_task_record(self):
        payload = json.loads(self.call())
        self.assertEqual(payload, self.store.task(self.task_id))

    def test_write_returns_only_the_report_path(self):
        payload = json.loads(self.call(write=True))
        self.assertEqual(
            set(payload),
            {"api_version", "task_id", "spec_revision", "run_id", "report_path", "generated_at"},
        )
        self.assertTrue(Path(payload["report_path"]).is_file())
        self.assertEqual(payload["spec_revision"], self.task["spec_revision"])

    def test_markdown_is_printed_and_report_md_is_untouched(self):
        report_md = self.store.path(self.task) / "report.md"
        atomic(report_md, {"summary": "progress view"})
        before = report_md.read_text()

        text = self.call(format="markdown")
        self.assertIn("# Slice A", text)
        self.assertIn("## 验收项", text)
        self.assertEqual(report_md.read_text(), before, "progress summary is a different file")


if __name__ == "__main__":
    unittest.main()
