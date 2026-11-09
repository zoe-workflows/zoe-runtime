"""v1.5.3：让"证据/冲突/空窗口"自己说清楚。

这批改动全部是**可观测性**修正，判据只有一条：宿主不必反推就能得到结论。

| 门槛 | 用例 |
|---|---|
| `REQUEST_CONFLICT` 要指出占用者 | `TestConflictDetails.*` |
| 零候选必须被记录，而不是靠两个 SHA 对比 | `TestEmptyCandidate.*` |
| `task logs` 的空窗口必须给原因 | `TestEmptyReason.*` |
| check 把 cwd 钉到别处要阻断 `prepare` | `TestCheckEscapes.*` |
| 目录级 `allowed_paths` 要给出粒度告警 | `TestScopeGranularity.*` |
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from skill_package import SKIP_REASON, skill_dir

from src import _evidence, _prepare
from src._util import Error
from src.cli import _task_logs
from src.core import Store

RUN = "run-1"


# --------------------------------------------------------------------------- #
# 1. REQUEST_CONFLICT 要能指向占用它的那一行
# --------------------------------------------------------------------------- #


class TestConflictDetails(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir(parents=True)
        self.store = Store(str(self.home))
        self.addCleanup(self.store.close)

    def _request(self, request_id, expected_version=1):
        return {
            "schema_version": 2,
            "request_id": request_id,
            "expected_version": expected_version,
            "authorization_ref": "auth-demo",
        }

    def test_conflict_quotes_the_owning_task_and_action(self):
        self.store.operation(self._request("req-shared"), "start", "task-a")
        with self.assertRaises(Error) as ctx:
            self.store.operation(self._request("req-shared"), "start", "task-b")
        self.assertEqual(ctx.exception.code, "REQUEST_CONFLICT")
        details = ctx.exception.details or {}
        self.assertEqual(details["request_id"], "req-shared")
        # 这是全部价值所在：不用去翻 sqlite 就知道 id 被谁占了。
        self.assertEqual(details["existing"]["task_id"], "task-a")
        self.assertEqual(details["existing"]["action"], "start")

    def test_conflict_distinguishes_actions_on_the_same_task(self):
        """同一个 id 跨 action 复用也是冲突——注释里写过，但没有可查的证据。"""
        self.store.operation(self._request("req-x"), "create", "task-a")
        with self.assertRaises(Error) as ctx:
            self.store.operation(self._request("req-x"), "start", "task-a")
        self.assertEqual(ctx.exception.details["existing"]["action"], "create")

    def test_identical_replay_is_not_a_conflict(self):
        first, _ = self.store.operation(self._request("req-same"), "start", "task-a")
        second, prior = self.store.operation(self._request("req-same"), "start", "task-a")
        self.assertEqual(first, second)
        self.assertIsNone(prior)

    def test_read_only_probe_reports_the_same_details(self):
        self.store.operation(self._request("req-probe"), "start", "task-a")
        with self.assertRaises(Error) as ctx:
            self.store.operation_prior(self._request("req-probe"), "start", "task-b")
        self.assertEqual(ctx.exception.details["existing"]["task_id"], "task-a")


# --------------------------------------------------------------------------- #
# 2. 零候选：写进证据，而不是让读者自己拿两个 SHA 比
# --------------------------------------------------------------------------- #


class TestEmptyCandidate(unittest.TestCase):
    def _task(self, **overrides):
        task = {
            "task_id": "task-1",
            "project_id": "p1",
            "spec_revision": 1,
            "spec_hash": "abc",
            "candidate_sha": "a" * 40,
            "empty_candidate": False,
        }
        task.update(overrides)
        return task

    def _build(self, task):
        return _evidence.build(
            task,
            [{"id": "unit", "status": "PASS", "exit_code": 0, "fingerprint": "f"}],
            "manifest",
            [{"id": "unit", "argv": ["true"]}],
        )

    def test_flag_is_copied_into_the_evidence(self):
        evidence = self._build(self._task(empty_candidate=True))
        self.assertTrue(evidence["empty_candidate"])

    def test_flag_defaults_to_false_when_the_task_predates_it(self):
        task = self._task()
        task.pop("empty_candidate")
        self.assertFalse(self._build(task)["empty_candidate"])

    def test_flag_does_not_change_evidence_identity(self):
        """标注不能改 id：否则同一候选重算会得到两个"不同"的证据。"""
        with_flag = self._build(self._task(empty_candidate=True))
        without = self._build(self._task(empty_candidate=False))
        self.assertEqual(with_flag["evidence_id"], without["evidence_id"])


# --------------------------------------------------------------------------- #
# 3. task logs 的空窗口：四种原因必须可区分
# --------------------------------------------------------------------------- #


class _FakeStore:
    def __init__(self, base, run=None):
        self._base = Path(base)
        self._task = {"task_id": "task-1", "project_id": "p1", "current_run_id": RUN}
        self._run = run or {"run_id": RUN, "status": "exited"}

    def path(self, _task):
        return self._base

    def task(self, _task_id):
        return self._task

    def get_run(self, _run_id):
        return self._run


class _Args:
    id = "task-1"
    run = None
    log = "agent"
    projection = "auto"
    follow = False
    cursor = None
    after = None
    tail_bytes = None


def _args(**overrides):
    args = _Args()
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


class TestEmptyReason(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.run_dir = self.root / "runs" / RUN
        self.run_dir.mkdir(parents=True)
        self.log = self.run_dir / "agent.log"
        self.store = _FakeStore(self.root)

    def _snapshot(self, data: bytes, **overrides) -> dict:
        self.log.write_bytes(data)
        return _task_logs(self.store, _args(**overrides))

    def test_reason_is_none_when_there_is_content(self):
        payload = self._snapshot(b'{"type":"assistant"}\n')
        self.assertIsNone(payload["empty_reason"])
        self.assertTrue(payload["events"])

    def test_rotated_is_reported_as_rotated(self):
        payload = self._snapshot(b"{}\n", after=f"{RUN}:1:999")
        self.assertTrue(payload["rotated"])
        self.assertEqual(payload["empty_reason"], "rotated")

    def test_window_inside_one_record_reports_no_newline(self):
        """尾部窗口整段落在同一条记录里：加窗口能解决，不是没有输出。"""
        payload = self._snapshot(b"x" * 200, tail_bytes=50)
        self.assertEqual(payload["text"], "")
        self.assertEqual(payload["empty_reason"], "no_newline_in_window")

    def test_tail_without_a_complete_record_reports_no_complete_line(self):
        payload = self._snapshot(b'{"type":"assistant"')
        self.assertNotEqual(payload["text"], "")
        self.assertEqual(payload["empty_reason"], "no_complete_line")

    def test_caught_up_cursor_reports_no_new_records(self):
        payload = self._snapshot(b"{}\n", after=f"{RUN}:1:3")
        self.assertEqual(payload["events"], [])
        self.assertFalse(payload.get("rotated"))
        self.assertEqual(payload["empty_reason"], "no_new_records")

    def test_blank_lines_only_is_a_complete_line_problem_not_a_read_problem(self):
        payload = self._snapshot(b"\n\n\n")
        self.assertEqual(payload["events"], [])
        self.assertEqual(payload["empty_reason"], "no_complete_line")


# --------------------------------------------------------------------------- #
# 4./5. prepare 的两条静态判据
# --------------------------------------------------------------------------- #

PROFILE = {
    "schema_version": 2,
    "project_id": "demo",
    "backend": "local-process",
    "network": "none",
    "default_agent_profile": "cb",
    "agent_profiles": {
        "cb": {
            "adapter": "codebuddy",
            "backend": "codebuddy-cli",
            "permission_profile": "isolated-edit",
            "require_permission_binding": False,
            "network_profile": "none",
            "model": "test-model",
        }
    },
    "authorization_ref": "auth-demo",
    "authorized_actions": ["create", "execute", "accept"],
    "allowed_paths": ["src", "README.md"],
    "protected_paths": [".env"],
    "checks": [{"id": "unit", "argv": ["true"]}],
}


def _spec(**overrides):
    document = {
        "schema_version": 2,
        "title": "make a.py print 2",
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "statement": "src/a.py prints 2",
                "verification": {"type": "check", "check_id": "unit"},
            }
        ],
    }
    document.update(overrides)
    return document


class _PrepareCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.repo = root / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        for argv in (
            ["git", "init", "-q", str(self.repo)],
            ["git", "-C", str(self.repo), "config", "user.email", "t@example.com"],
            ["git", "-C", str(self.repo), "config", "user.name", "t"],
            ["git", "-C", str(self.repo), "add", "-A"],
            ["git", "-C", str(self.repo), "commit", "-qm", "init"],
        ):
            subprocess.run(argv, check=True)
        self.profile = dict(PROFILE, repo=str(self.repo))

    def register(self, **overrides):
        from src import _config
        from src._util import atomic

        profile = dict(self.profile, **overrides)
        if "checks" in overrides:
            # AC 引用的 `unit` 必须在批准集合里，否则 prepared 会因为另一条
            # 原因（SPEC_CLARIFICATION_REQUIRED）失败，掩盖本用例要断言的东西。
            profile["checks"] = [{"id": "unit", "argv": ["true"]}] + list(overrides["checks"])
        self.home.mkdir(parents=True, exist_ok=True)
        config = _config.validate_config(profile, base_dir=Path(self._tmp.name))
        atomic(self.home / "projects" / f"{config['project_id']}.json", config)
        return config

    def write_spec(self, document) -> str:
        path = Path(self._tmp.name) / "spec.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def prepare(self, document=None):
        return _prepare.prepare(
            str(self.home), repo=str(self.repo), spec=self.write_spec(document or _spec())
        )


class TestCheckEscapes(_PrepareCase):
    def test_a_pinned_absolute_cwd_blocks_prepare(self):
        self.register(checks=[{"id": "test", "argv": ["bash", "-c", "cd /tmp && true"]}])
        report = self.prepare()
        codes = [d["code"] for d in report["diagnostics"]]
        self.assertIn("CHECK_ESCAPES_WORKSPACE", codes)
        self.assertFalse(report["prepared"])

    def test_git_dash_c_is_caught_too(self):
        self.register(
            checks=[{"id": "test", "argv": ["bash", "-c", "git -C /tmp log -1 --oneline"]}]
        )
        report = self.prepare()
        self.assertIn("CHECK_ESCAPES_WORKSPACE", [d["code"] for d in report["diagnostics"]])

    def test_relative_arguments_are_not_flagged(self):
        self.register(checks=[{"id": "test", "argv": ["bash", "-c", "./run-tests.sh"]}])
        report = self.prepare()
        self.assertNotIn(
            "CHECK_ESCAPES_WORKSPACE", [d["code"] for d in report["diagnostics"]]
        )
        self.assertTrue(report["prepared"])

    def test_shell_variable_cwd_is_not_flagged(self):
        """`cd "$WORKTREE"` 不是绝对路径：判据是"钉到绝对路径"，不是"出现 cd"。"""
        self.register(checks=[{"id": "test", "argv": ["bash", "-c", 'cd "$HOME" && true']}])
        report = self.prepare()
        self.assertNotIn(
            "CHECK_ESCAPES_WORKSPACE", [d["code"] for d in report["diagnostics"]]
        )


class TestScopeGranularity(_PrepareCase):
    def test_a_directory_scope_entry_is_reported_as_a_warning(self):
        self.register()
        report = self.prepare(_spec(allowed_paths=["src"]))
        warnings = report["allowed_paths"]["warnings"]
        self.assertIn("SCOPE_DIRECTORY_LEVEL", [w["code"] for w in warnings])
        # 只是告警：整包改动是合法需求，是否可接受由编排判断。
        self.assertTrue(report["prepared"])

    def test_file_level_scope_produces_no_granularity_warning(self):
        self.register()
        report = self.prepare(_spec(allowed_paths=["src/a.py", "README.md"]))
        warnings = report["allowed_paths"]["warnings"]
        self.assertNotIn("SCOPE_DIRECTORY_LEVEL", [w["code"] for w in warnings])

    def test_project_level_scope_is_not_reported(self):
        """项目级 `allowed_paths` 本来就是粗粒度，重复告警只会淹没真信号。"""
        self.register()
        report = self.prepare()
        warnings = report["allowed_paths"]["warnings"]
        self.assertEqual([w for w in warnings if w["code"] == "SCOPE_DIRECTORY_LEVEL"], [])


# --------------------------------------------------------------------------- #
# 6. 监控把零候选标出来（`zoe-monitor.sh`）
# --------------------------------------------------------------------------- #

MONITOR_FIELDS = "empty_diff=true"
SKILL = skill_dir()


@unittest.skipUnless(SKILL, SKIP_REASON)
@unittest.skipUnless(shutil.which("bash"), "bash is required")
@unittest.skipUnless(shutil.which("jq"), "the POSIX monitor depends on jq")
class TestMonitorEmptyCandidate(unittest.TestCase):
    """零候选的观感与"仍在写"完全一样；只有监控先说出来，人才能在验收前发现。"""

    FAKE = """#!/bin/sh
if [ "$1" = "watch" ]; then
  case " $* " in
    *" --jsonl "*)
      echo '{"id": 7, "event": "state_changed"}'
      echo '{"kind": "meta", "event": "query_end", "next_cursor": "epoch:200"}'
      ;;
    *) echo '{"next_cursor": "epoch:100"}' ;;
  esac
  exit 0
fi
if [ "$1" = "task" ] && [ "$2" = "status" ]; then
  echo '{"state": "REVIEW_READY", "phase": "acceptance", "current_run_id": "run-1",
    "base_sha": "abc123", "candidate_sha": "%s"}'
  exit 0
fi
if [ "$1" = "task" ] && [ "$2" = "logs" ]; then
  echo '{}'
  exit 0
fi
echo '{"error": {"code": "UNEXPECTED", "message": "fake zoe got: $*"}}'
exit 2
"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.script = SKILL / "scripts" / "zoe-monitor.sh"
        self.stop = self.root / "stop.json"

    def _run(self, candidate_sha: str):
        zoe = self.root / "zoe"
        zoe.write_text(self.FAKE % candidate_sha)
        zoe.chmod(0o755)
        env = dict(os.environ, ZOE_BIN=str(zoe))
        return subprocess.run(
            [
                "bash",
                str(self.script),
                "--project",
                "demo",
                "--targets",
                "T1",
                "--max-rounds",
                "1",
                "--interval",
                "0",
                "--cursor-file",
                str(self.root / "cursor.txt"),
                "--events",
                str(self.root / "events.jsonl"),
                "--stop-file",
                str(self.stop),
            ],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_equal_candidate_and_base_is_reported_and_listed(self):
        result = self._run("abc123")
        self.assertIn(MONITOR_FIELDS, result.stdout)
        self.assertEqual(result.returncode, 10)
        payload = json.loads(self.stop.read_text())
        self.assertEqual(payload["empty_candidates"], ["T1"])

    def test_a_real_candidate_is_not_flagged(self):
        result = self._run("def456")
        self.assertNotIn(MONITOR_FIELDS, result.stdout)
        self.assertIn("[needs-human] T1 REVIEW_READY 等人工验收", result.stdout)
        payload = json.loads(self.stop.read_text())
        self.assertEqual(payload["empty_candidates"], [])


if __name__ == "__main__":
    unittest.main()
