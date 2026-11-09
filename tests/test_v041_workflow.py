"""v0.4.1 workflow closure: answer → resume, ownership, summaries, resume snapshot.

These tests exist because the v0.4.0 review found the orchestrator loop could not
actually be driven end to end:

- ``needs_input`` used to write ``phase="execute"``, a value no pipeline stage
  consumes, so ``task answer`` cleared the block and the next ``start`` silently
  did nothing. The answer path now restores the stage, and legacy records are
  normalized when the pipeline opens them.
- ``task answer`` bypassed the operation/idempotency table and never checked the
  project owner (§10.10).
- ``report.md`` was written through the JSON helper, producing a ``.md`` file that
  was a JSON object.
- ``orchestration resume`` returned hardcoded empty ``dispatchable_now`` /
  ``stale_summaries``, so a fresh host session could not decide anything.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src import _context, _summaries
from src import _orchestration as orch
from src._pipeline import run_pipeline, start
from src._store import Store
from src._util import Error
from src.cli import _task_answer

CONFIG = {
    "authorization_ref": "scope-1",
    "authorized_actions": ["create", "execute", "publish", "accept"],
}

REQUEST = {
    "schema_version": 1,
    "request_id": "req-1",
    "authorization_ref": "scope-1",
    "expected_version": 1,
}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.home = self.root / "home"
        self.store = Store(self.home)
        (self.home / "projects").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.store.db.close()
        self.td.cleanup()

    # -- fixtures ---------------------------------------------------------- #
    def project(self, project_id="demo", allowed=None):
        config = {
            "schema_version": 2,
            "project_id": project_id,
            "repo": str(self.repo),
            "backend": "local-process",
            "default_agent_profile": "p1",
            "agent_profiles": {
                "p1": {
                    "adapter": "codebuddy",
                    "model": "stub-model",
                    "backend": "codebuddy-cli",
                    "expected_version": "1.2.3",
                    "permission_profile": "isolated-edit",
                }
            },
            "checks": [{"id": "unit", "argv": ["true"]}],
            "allowed_paths": allowed or ["src.txt"],
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute", "publish", "accept"],
        }
        (self.home / "projects" / f"{project_id}.json").write_text(json.dumps(config))
        return config

    def task(self, task_id="task-1", project_id="demo", **over):
        t = {
            "task_id": task_id,
            "project_id": project_id,
            "title": "Demo task",
            "row_version": 1,
            "record_version": 2,
            "state": "READY",
            "phase": "implement",
            "spec_revision": 1,
            "spec_hash": "abc",
            "workspace": str(self.root / "worktree"),
            "config": dict(CONFIG),
            "attempts": 0,
            "current_run_id": None,
            "block_code": None,
            "candidate_sha": None,
            "validated_sha": None,
            "current_evidence_id": None,
            "acceptance": None,
            "pr": None,
            "created_at": 0,
            "last_progress_at": 0,
        }
        t.update(over)
        self.store.db.execute("INSERT INTO tasks VALUES(?,?,?)", (task_id, 1, json.dumps(t)))
        return self.store.task(task_id)

    def question(self, task_id="task-1", question_id="q1"):
        return self.task(
            task_id,
            state="BLOCKED",
            phase="execute",  # what v0.4.0 wrote
            block_code="INPUT_REQUIRED",
            needs_input=True,
            pending_questions=[{"question_id": question_id, "text": "which database?"}],
        )


class TestOrchestrationPatchContract(Fixture):
    """归属是 claim 协议，不是普通字段；形状错误必须在写盘前拒绝。

    ``{"patch": {"owner": "someone"}}`` 曾让记录里的 ``owner`` 变成字符串，此后
    每次任务级 mutation 都在 ``require_owner`` 里抛 AttributeError（INTERNAL_ERROR，
    无法归类），并且绕过了 ``claim --force`` 的审计语义。
    """

    def test_owner_is_not_patchable(self):
        with self.assertRaises(Error) as ctx:
            orch.update(
                self.store,
                {"project_id": "demo", "expected_version": 0, "patch": {"owner": {"host": "h"}}},
            )
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")

    def test_patch_value_shapes_are_validated(self):
        with self.assertRaises(Error) as ctx:
            orch.update(
                self.store,
                {"project_id": "demo", "expected_version": 0, "patch": {"goal": ["not", "text"]}},
            )
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("goal", str(ctx.exception))

    def test_a_hand_edited_owner_does_not_break_mutations(self):
        base = orch._empty("demo")
        base["owner"] = "broken-string"
        base.pop("version", None)
        base.pop("updated_at", None)
        self.store.upsert_orchestration("demo", 0, base)
        # 读不成对象就按"未认领"处理，而不是让每个 mutation 变成内部错误。
        self.assertIsNotNone(orch.require_owner(self.store, "demo", {"by": "anyone"}))


class TestAnswerRestoresProgress(Fixture):
    def test_answer_clears_block_and_restores_phase(self):
        self.question()
        result = _task_answer(
            self.store,
            "task-1",
            {**REQUEST, "question_id": "q1", "answer": "postgres", "answered_by": "user"},
        )

        self.assertEqual(result["state"], "READY")
        self.assertEqual(result["phase"], "implement")
        task = self.store.task("task-1")
        self.assertEqual(task["state"], "READY")
        self.assertEqual(task["phase"], "implement")
        self.assertIsNone(task["block_code"])
        self.assertFalse(task["needs_input"])
        self.assertEqual(task["pending_questions"][0]["answer"], "postgres")

    def test_answer_is_idempotent_by_request_id(self):
        self.question()
        request = {**REQUEST, "question_id": "q1", "answer": "postgres", "answered_by": "user"}
        first = _task_answer(self.store, "task-1", request)
        second = _task_answer(self.store, "task-1", request)

        self.assertEqual(first, second)
        # One operation row, not two: a retried request must not re-run the save.
        rows = self.store.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertEqual(self.store.task("task-1")["row_version"], 2)

    def test_answer_requires_current_version(self):
        self.question()
        with self.assertRaises(Error) as ctx:
            _task_answer(
                self.store,
                "task-1",
                {
                    **REQUEST,
                    "expected_version": 99,
                    "question_id": "q1",
                    "answer": "postgres",
                },
            )
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")

    def test_answer_rejects_unknown_question(self):
        self.question()
        with self.assertRaises(Error) as ctx:
            _task_answer(
                self.store,
                "task-1",
                {**REQUEST, "question_id": "q-other", "answer": "postgres"},
            )
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")

    def test_answer_rejects_a_task_that_is_not_asking(self):
        self.task()
        with self.assertRaises(Error) as ctx:
            _task_answer(
                self.store, "task-1", {**REQUEST, "question_id": "q1", "answer": "postgres"}
            )
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")

    def test_resume_before_answering_is_refused(self):
        self.project()
        self.task(
            state="BLOCKED",
            block_code="INPUT_REQUIRED",
            config=self.store.config("demo"),
            pending_questions=[{"question_id": "q1", "text": "which database?"}],
        )
        with self.assertRaises(Error) as ctx:
            start(
                self.store,
                "task-1",
                {
                    "schema_version": 1,
                    "request_id": "start-1",
                    "authorization_ref": "scope-1",
                    "expected_version": 1,
                },
            )
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")
        self.assertIsNone(self.store.task("task-1")["current_run_id"])

    def test_legacy_phase_is_normalized_when_the_pipeline_runs(self):
        # A config without profiles makes the pipeline stop early, which is
        # enough: normalization happens before any stage is selected.
        self.task(phase="execute", current_run_id="run-1")
        self.store.run({"run_id": "run-1", "task_id": "task-1", "status": "exited"})
        run_pipeline(self.store, "task-1")
        self.assertEqual(self.store.task("task-1")["phase"], "implement")


class TestAnswerOwnership(Fixture):
    def test_non_owner_cannot_answer(self):
        self.question()
        orch.claim(self.store, "demo", "hostA")

        with self.assertRaises(Error) as ctx:
            _task_answer(
                self.store,
                "task-1",
                {**REQUEST, "question_id": "q1", "answer": "postgres", "by": "hostB"},
            )
        self.assertEqual(ctx.exception.code, "NOT_OWNER")
        self.assertEqual(self.store.task("task-1")["state"], "BLOCKED")

    def test_owner_can_answer(self):
        self.question()
        orch.claim(self.store, "demo", "hostA")
        result = _task_answer(
            self.store,
            "task-1",
            {**REQUEST, "question_id": "q1", "answer": "postgres", "by": "hostA"},
        )
        self.assertEqual(result["state"], "READY")

    def test_unclaimed_project_is_not_blocked(self):
        self.question()
        result = _task_answer(
            self.store, "task-1", {**REQUEST, "question_id": "q1", "answer": "postgres"}
        )
        self.assertEqual(result["state"], "READY")


class TestOrchestrationCas(Fixture):
    def update(self, host, version, **extra):
        return orch.update(
            self.store,
            {
                "schema_version": 1,
                "request_id": f"upd-{host}-{version}",
                "project_id": "demo",
                "expected_version": version,
                "owner": {"host": host},
                "patch": {"goal": "ship it"},
                "append_decisions": [{"action": "dispatch", "reason": "spec ready"}],
                **extra,
            },
        )

    def test_decision_requires_the_recorded_owner(self):
        orch.claim(self.store, "demo", "hostA")
        with self.assertRaises(Error) as ctx:
            self.update("hostB", 1)
        self.assertEqual(ctx.exception.code, "NOT_OWNER")
        self.assertEqual(orch.show(self.store, "demo")["decisions"], [])

    def test_stale_version_is_a_decision_conflict(self):
        orch.claim(self.store, "demo", "hostA")
        updated = self.update("hostA", 1)
        self.assertEqual(updated["version"], 2)
        self.assertEqual(len(updated["decisions"]), 1)

        with self.assertRaises(Error) as ctx:
            self.update("hostA", 1)
        self.assertEqual(ctx.exception.code, "DECISION_CONFLICT")
        # The refused update must not append a second decision.
        self.assertEqual(len(orch.show(self.store, "demo")["decisions"]), 1)

    def test_budget_counters_are_controller_owned(self):
        orch.claim(self.store, "demo", "hostA")
        with self.assertRaises(Error) as ctx:
            self.update("hostA", 1, patch={"budget": {"fix_rounds_used": 0}})
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")


class TestSummariesRenderMarkdown(Fixture):
    def submit(self, **over):
        req = {
            "schema_version": 1,
            "request_id": "sum-1",
            "project_id": "demo",
            "spec_revision": 1,
            "through_event_id": 1,
            "generated_by": "workbuddy",
            "summary": "实现完成，日期边界测试失败",
            **over,
        }
        return _summaries.submit(self.store, "task-1", req)

    def test_report_md_is_markdown_not_json(self):
        self.task()
        self.store.db.execute(
            "INSERT INTO events(task_id,at,data) VALUES(?,?,?)",
            ("task-1", 0, json.dumps({"event": "created"})),
        )
        self.submit()
        report = (self.store.path(self.store.task("task-1")) / "report.md").read_text()

        self.assertTrue(report.startswith("# Demo task"), report[:80])
        self.assertIn("实现完成", report)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(report)

    def test_summary_for_an_old_spec_revision_is_stale(self):
        self.task(spec_revision=2)
        self.store.db.execute(
            "INSERT INTO events(task_id,at,data) VALUES(?,?,?)",
            ("task-1", 0, json.dumps({"event": "created"})),
        )
        result = self.submit(spec_revision=1)
        self.assertTrue(result["stale"])

        index = _summaries.list_summaries(self.store, "task-1")
        self.assertEqual(index["entries"][0]["status"], "stale")
        # A stale summary never renders the report: there is no active summary.
        self.assertFalse((self.store.path(self.store.task("task-1")) / "report.md").exists())


class TestResumeSnapshot(Fixture):
    def test_dispatchable_and_waiting_are_derived_from_records(self):
        self.task("task-ready")
        self.task(
            "task-asking",
            state="BLOCKED",
            block_code="INPUT_REQUIRED",
            pending_questions=[{"question_id": "q1", "text": "which database?"}],
        )
        self.task("task-unknown", state="BLOCKED", block_code="PROCESS_UNKNOWN")

        snapshot = orch.resume(self.store, "demo")
        dispatchable = {d["task_id"]: d["action"] for d in snapshot["dispatchable_now"]}
        self.assertEqual(dispatchable["task-ready"], "task resume")
        self.assertEqual(dispatchable["task-asking"], "task answer")
        self.assertNotIn("task-unknown", dispatchable)

        waiting = {q["question_id"] for q in snapshot["pending_questions"]}
        self.assertIn("q1", waiting)

    def test_summary_lagging_the_event_log_is_reported(self):
        self.task()
        self.store.db.execute(
            "INSERT INTO events(task_id,at,data) VALUES(?,?,?)",
            ("task-1", 0, json.dumps({"event": "created"})),
        )
        _summaries.submit(
            self.store,
            "task-1",
            {
                "schema_version": 1,
                "request_id": "sum-1",
                "project_id": "demo",
                "spec_revision": 1,
                "through_event_id": 1,
                "generated_by": "workbuddy",
                "summary": "halfway",
            },
        )
        self.assertEqual(orch.resume(self.store, "demo")["stale_summaries"], [])

        self.store.db.execute(
            "INSERT INTO events(task_id,at,data) VALUES(?,?,?)",
            ("task-1", 0, json.dumps({"event": "phase_update"})),
        )
        stale = orch.resume(self.store, "demo")["stale_summaries"]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["summary_version"], 1)
        self.assertGreater(stale[0]["current_event_id"], stale[0]["through_event_id"])


class TestContextBundle(Fixture):
    def test_bundle_includes_the_agents_pending_question(self):
        self.project()
        self.task(
            state="BLOCKED",
            block_code="INPUT_REQUIRED",
            pending_questions=[{"question_id": "q1", "text": "which database?"}],
        )
        bundle = _context.build(self.store, "demo", "task-1")

        self.assertEqual(bundle["project_id"], "demo")
        # The bundle reports the resolved path (macOS temp dirs are symlinked).
        self.assertEqual(Path(bundle["repo"]["root"]), self.repo.resolve())
        questions = {q["question_id"] for q in bundle["pending_questions"]}
        self.assertIn("q1", questions)
        self.assertEqual([a["action"] for a in bundle["allowed_next"]][0], "task answer")

    def test_task_from_another_project_is_refused(self):
        self.project("demo")
        self.project("other")
        self.task()
        with self.assertRaises(Error) as ctx:
            _context.build(self.store, "other", "task-1")
        self.assertEqual(ctx.exception.code, "PROJECT_MISMATCH")

    def test_bundle_is_bounded(self):
        self.project()
        self.task(
            pending_questions=[{"question_id": f"q{i}", "text": "x" * 512} for i in range(64)]
        )
        bundle = _context.build(self.store, "demo", "task-1")
        self.assertLessEqual(len(json.dumps(bundle, ensure_ascii=False).encode()), 32 * 1024)
        self.assertTrue(bundle["truncated"])


class TestAnswerCli(Fixture):
    """The CLI path matters: argparse wiring and exit codes are part of the contract."""

    SRC = Path(__file__).resolve().parents[1]

    def zoe(self, *argv):
        env = {**os.environ, "PYTHONPATH": str(self.SRC), "ZOE_HOME": str(self.home)}
        return subprocess.run(
            [sys.executable, "-m", "src", *argv],
            capture_output=True,
            text=True,
            env=env,
        )

    def write_request(self, **over):
        path = self.root / "answer.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "request_id": "req-cli-1",
                    "authorization_ref": "scope-1",
                    "expected_version": 1,
                    "question_id": "q1",
                    "answer": "postgres",
                    "answered_by": "user",
                    **over,
                }
            )
        )
        return str(path)

    def test_cli_answer_succeeds(self):
        self.question()
        result = self.zoe("task", "answer", "task-1", "--request-file", self.write_request())
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["state"], "READY")

    def test_cli_answer_rejects_a_non_owner(self):
        self.question()
        orch.claim(self.store, "demo", "hostA")
        result = self.zoe(
            "task", "answer", "task-1", "--request-file", self.write_request(by="hostB")
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "NOT_OWNER")
        self.assertEqual(self.store.task("task-1")["state"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
