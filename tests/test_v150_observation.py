"""v1.5 observation layer: event filtering, log following, run observation, check preflight.

These tests are written against the *behaviour* the host depends on, not against
the implementation shape:

* a project filter must not leak another project's events, and must not return
  an empty page as if nothing had happened (v2 §3-1);
* a log follower must emit complete records only, resume without duplicates,
  and survive rotation/truncation (v2 §3-3);
* ``progress.json`` reports observations, never a verdict about the agent
  (v2 §3-2);
* a check command is resolved under the execution environment, and a missing
  tool is an environment failure rather than a mysteriously failing test.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from src import _config, _logstream, _pipeline, _progress, _store


def _make_store(home) -> _store.Store:
    return _store.Store(str(home))


class _FakeStore:
    """Minimum surface the observation helpers touch."""

    def __init__(self, base: Path, task=None, run=None):
        self._base = Path(base)
        self._task = task or {"task_id": "task-1", "project_id": "p1"}
        self._run = run or {"run_id": "run-1", "status": "running"}

    def path(self, t):
        return self._base

    def task(self, tid):
        return self._task

    def get_run(self, rid):
        return self._run


# --------------------------------------------------------------------------- #
# 1. event filtering                                                          #
# --------------------------------------------------------------------------- #
class TestEventFiltering(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self._tmp.name)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _append(self, project, event="phase_update", task_id=None, **extra):
        with self.store.tx():
            self.store.event(
                task_id, {"event": event, "type": event, "project_id": project, **extra}
            )

    def test_project_filter_does_not_leak_other_projects(self):
        self._append("p1")
        self._append("p2")
        self._append("p1")
        events, cursor, has_more, unattributed = self.store.events_filtered(0, 10, project_id="p1")
        self.assertEqual([e["project_id"] for e in events], ["p1", "p1"])
        self.assertFalse(has_more)
        self.assertEqual(unattributed, 0)
        self.assertEqual(cursor, 3)

    def test_cursor_advances_over_scanned_rows_even_with_no_match(self):
        for _ in range(5):
            self._append("p2")
        events, cursor, has_more, _ = self.store.events_filtered(0, 10, project_id="p1")
        self.assertEqual(events, [])
        # Nothing matched, but the cursor must move past what was scanned,
        # otherwise a host re-reads the same unrelated tail forever.
        self.assertEqual(cursor, 5)
        self.assertFalse(has_more)

    def test_matches_are_not_dropped_when_a_page_fills(self):
        for _ in range(3):
            self._append("p1")
        first, cursor, has_more, _ = self.store.events_filtered(0, 2, project_id="p1")
        self.assertEqual(len(first), 2)
        self.assertTrue(has_more)
        second, _, has_more2, _ = self.store.events_filtered(cursor, 2, project_id="p1")
        self.assertEqual(len(second), 1)
        self.assertFalse(has_more2)

    def test_unattributed_events_are_counted_not_guessed(self):
        with self.store.tx():
            self.store.event(
                None, {"event": "orchestration_update", "type": "orchestration_update"}
            )
        events, _, _, unattributed = self.store.events_filtered(0, 10, project_id="p1")
        self.assertEqual(events, [])
        self.assertEqual(unattributed, 1)

    def test_task_and_project_filters_intersect(self):
        self._append("p1", task_id="task-a")
        self._append("p1", task_id="task-b")
        events, _, _, _ = self.store.events_filtered(0, 10, project_id="p1", task_id="task-a")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], 1)


class TestEventOwnership(unittest.TestCase):
    """Legacy rows carry no ``project_id``; attribution must be explainable."""

    def test_task_row_fills_missing_project(self):
        tasks = {"task-1": ("p1", "g1")}
        project, group = _store._ownership({"event": "x"}, "task-1", tasks, {"g1": "p1"})
        self.assertEqual((project, group), ("p1", "g1"))

    def test_group_row_fills_missing_project(self):
        project, group = _store._ownership(
            {"event": "group_created", "group_id": "g1"}, None, {}, {"g1": "p2"}
        )
        self.assertEqual((project, group), ("p2", "g1"))

    def test_unknown_owner_stays_none(self):
        project, group = _store._ownership({"event": "x"}, None, {}, {})
        self.assertIsNone(project)
        self.assertIsNone(group)

    def test_explicit_field_wins(self):
        tasks = {"task-1": ("p-old", None)}
        project, _ = _store._ownership({"event": "x", "project_id": "p-new"}, "task-1", tasks, {})
        self.assertEqual(project, "p-new")


class TestFilterValidation(unittest.TestCase):
    def test_task_of_another_project_is_refused(self):
        from src.cli import _validate_event_filters

        store = _FakeStore(tempfile.gettempdir(), task={"task_id": "t1", "project_id": "p2"})
        with self.assertRaises(_store.Error) as caught:
            _validate_event_filters(store, {"project_id": "p1", "group_id": None, "task_id": "t1"})
        self.assertEqual(caught.exception.code, "PROJECT_MISMATCH")


# --------------------------------------------------------------------------- #
# 2. log following                                                            #
# --------------------------------------------------------------------------- #
class TestLogFollower(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "agent.log"
        self.path.write_bytes(b"")

    def tearDown(self):
        self._tmp.cleanup()

    def _append(self, data: bytes):
        with open(self.path, "ab") as handle:
            handle.write(data)

    def test_only_complete_lines_are_emitted(self):
        stream = _logstream.LogFollower(self.path)
        self._append(b'{"type":"result"')
        self.assertEqual(stream.poll(), [])
        self._append(b',"subtype":"success"}\n')
        events = stream.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0]["text"])["subtype"], "success")

    def test_resume_from_cursor_neither_duplicates_nor_loses(self):
        self._append(b"one\ntwo\n")
        first = _logstream.LogFollower(self.path)
        self.assertEqual([e["text"] for e in first.poll()], ["one", "two"])
        cursor = first.cursor("run-1")
        self.assertEqual(cursor["offset"], len(b"one\ntwo\n"))

        self._append(b"three\n")
        resumed = _logstream.LogFollower(
            self.path, offset=cursor["offset"], generation=cursor["log_generation"]
        )
        self.assertEqual([e["text"] for e in resumed.poll()], ["three"])

    def test_partial_line_is_not_consumed_by_the_cursor(self):
        self._append(b"complete\npart")
        stream = _logstream.LogFollower(self.path)
        self.assertEqual([e["text"] for e in stream.poll()], ["complete"])
        self.assertEqual(stream.cursor("run-1")["offset"], len(b"complete\n"))
        self._append(b"ial\n")
        self.assertEqual([e["text"] for e in stream.poll()], ["partial"])

    def test_rotated_file_starts_a_new_generation(self):
        self._append(b"old\n")
        stream = _logstream.LogFollower(self.path)
        stream.poll()
        replacement = Path(self._tmp.name) / "agent.log.new"
        replacement.write_bytes(b"new\n")
        replacement.replace(self.path)
        events = stream.poll()
        self.assertEqual(events[0]["kind"], "rotated")
        self.assertEqual(events[0]["reason"], "replaced")
        self.assertEqual(events[0]["log_generation"], 2)
        self.assertEqual([e["text"] for e in stream.poll()], ["new"])

    def test_in_place_truncation_is_detected(self):
        self._append(b"aaaa\nbbbb\n")
        stream = _logstream.LogFollower(self.path)
        stream.poll()
        self.path.write_bytes(b"c\n")
        events = stream.poll()
        self.assertEqual(events[0]["kind"], "rotated")
        self.assertEqual(events[0]["reason"], "truncated")

    def test_utf8_split_across_reads_is_decoded(self):
        stream = _logstream.LogFollower(self.path)
        payload = "中文\n".encode()
        self._append(payload[:2])
        self.assertEqual(stream.poll(), [])
        self._append(payload[2:])
        self.assertEqual([e["text"] for e in stream.poll()], ["中文"])

    def test_oversized_line_is_emitted_marked_and_bounded(self):
        stream = _logstream.LogFollower(self.path, max_line_bytes=16)
        self._append(b"x" * 64)
        events = stream.poll()
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["truncated"])
        self.assertEqual(events[0]["kind"], "line")


class TestLogProjection(unittest.TestCase):
    def test_tool_use_becomes_a_compact_action(self):
        record = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "editing the file"},
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {"file_path": "/repo/internal/app/publish.go"},
                    },
                ]
            },
        }
        events = _logstream.project_line(json.dumps(record))
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds, ["text", "tool"])
        self.assertEqual(events[1]["name"], "Edit")
        self.assertEqual(events[1]["target"], "/repo/internal/app/publish.go")

    def test_plain_text_is_wrapped_not_dropped(self):
        events = _logstream.project_line("Authentication required. Please use /login")
        self.assertEqual(events[0]["kind"], "raw")
        self.assertIn("Authentication", events[0]["text"])

    def test_events_projection_drops_text_fallback(self):
        self.assertEqual(_logstream.project_line("banner text", "events"), [])

    def test_events_projection_drops_empty_envelopes(self):
        """An assistant turn without text/tools is a breadcrumb, not an action."""
        envelope = json.dumps({"type": "assistant", "message": {"content": []}})
        self.assertEqual(_logstream.project_line(envelope, "events"), [])
        self.assertEqual([e["kind"] for e in _logstream.project_line(envelope, "auto")], ["record"])

    def test_raw_projection_still_redacts(self):
        events = _logstream.project_line("token sk-abcdef0123456789", "raw")
        self.assertNotIn("sk-abcdef0123456789", events[0]["text"])

    def test_terminal_result_is_recognised(self):
        events = _logstream.project_line(
            json.dumps({"type": "result", "subtype": "success", "is_error": False})
        )
        self.assertEqual(events[0]["kind"], "result")
        self.assertFalse(events[0]["is_error"])

    def test_cursor_roundtrip(self):
        text = _logstream.format_cursor("run-1", 2, 4096)
        self.assertEqual(_logstream.parse_cursor(text), ("run-1", 2, 4096))
        self.assertIsNone(_logstream.parse_cursor("4096"))
        self.assertIsNone(_logstream.parse_cursor(None))


class TestLogsFollowCommand(unittest.TestCase):
    def test_follow_reads_from_cursor_and_emits_json_lines(self):
        from src.cli import _logs_follow

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            (run_dir / "agent.log").write_text(
                json.dumps({"type": "result", "subtype": "success"}) + "\n", encoding="utf-8"
            )
            store = _FakeStore(td)

            class Args:
                id = "task-1"
                run = None
                log = "agent"
                cursor = "run-1:1:0"
                projection = "auto"
                timeout = 0.3
                max_bytes = 0

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                _logs_follow(store, Args())
            lines = [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]
        self.assertEqual(lines[0]["kind"], "meta")
        self.assertEqual(lines[0]["run_id"], "run-1")
        self.assertEqual(lines[-1]["kind"], "result")


# --------------------------------------------------------------------------- #
# 3. run observation                                                          #
# --------------------------------------------------------------------------- #
class TestRunObservation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        (self.base / "runs" / "run-1").mkdir(parents=True)
        self.log = self.base / "runs" / "run-1" / "agent.log"
        self.log.write_bytes(b"")
        self.store = _FakeStore(self.base)

    def tearDown(self):
        self._tmp.cleanup()

    def _observer(self, **kwargs):
        return _progress.Observer(
            self.store, {"task_id": "task-1"}, "run-1", self.log, "agent", **kwargs
        )

    def test_writes_are_throttled_by_interval(self):
        observer = self._observer(min_interval=60, min_bytes=1)
        self.assertTrue(observer.tick())
        with open(self.log, "ab") as handle:
            handle.write(b"x" * 32)
        self.assertFalse(observer.tick(), "a second write inside the interval is not allowed")

    def test_silence_never_invents_output(self):
        observer = self._observer(min_interval=0, min_bytes=1)
        observer.tick()
        payload = _progress.view(
            self.store, {"task_id": "task-1"}, {"run_id": "run-1"}, stale_after=60
        )
        self.assertIsNone(payload["last_output_at"])
        self.assertGreaterEqual(payload["quiet_seconds"], 0)
        self.assertEqual(payload["process_state"], "alive")

    def test_exit_is_stated_not_implied(self):
        observer = self._observer(min_interval=0, min_bytes=1)
        observer.finish()
        payload = _progress.view(self.store, {"task_id": "task-1"}, {"run_id": "run-1"})
        self.assertEqual(payload["process_state"], "exited")

    def test_observation_age_marks_staleness(self):
        observer = self._observer(min_interval=0, min_bytes=1)
        observer.tick()
        path = _progress.path(self.store, {"task_id": "task-1"}, "run-1")
        data = json.loads(path.read_text())
        data["observed_at"] = time.time() - 3600
        path.write_text(json.dumps(data))
        payload = _progress.view(self.store, {"task_id": "task-1"}, {"run_id": "run-1"})
        self.assertTrue(payload["observations_stale"])
        self.assertGreater(payload["observed_age_seconds"], 3000)

    def test_missing_progress_is_none_not_zero(self):
        payload = _progress.view(self.store, {"task_id": "task-1"}, {"run_id": "run-1"})
        self.assertIsNone(payload)


# --------------------------------------------------------------------------- #
# 4. check command preflight                                                  #
# --------------------------------------------------------------------------- #
class TestChecksPreflight(unittest.TestCase):
    def test_bare_name_on_path_is_ok(self):
        row = _config.resolve_check(["true"])
        self.assertEqual(row["status"], "ok")
        self.assertTrue(row["resolved_path"])

    def test_missing_bare_name_is_unresolved(self):
        row = _config.resolve_check(["zoe-definitely-not-a-binary"])
        self.assertEqual(row["status"], "unresolved")
        self.assertIsNone(row["resolved_path"])

    def test_generated_tool_is_deferred_not_broken(self):
        with tempfile.TemporaryDirectory() as td:
            row = _config.resolve_check(["./bin/tool"], cwd=td)
        self.assertEqual(row["status"], "deferred")

    def test_shell_inner_command_is_not_parsed(self):
        row = _config.resolve_check(["bash", "-c", "go build ./..."])
        self.assertIn(row["status"], ("shell", "unresolved"))
        if row["status"] == "shell":
            self.assertEqual(row["inner_command"], "unverified")

    def test_preflight_rows_are_per_check(self):
        c = {
            "repo": tempfile.gettempdir(),
            "checks": [
                {"id": "unit", "argv": ["true"]},
                {"id": "lint", "argv": ["zoe-definitely-not-a-binary"]},
            ],
        }
        rows = _config.checks_preflight(c)
        self.assertEqual([row["check_id"] for row in rows], ["unit", "lint"])
        self.assertEqual(rows[1]["status"], "unresolved")

    def test_doctor_ready_tracks_only_unresolved_tools(self):
        from src import _profiles

        base = {
            "schema_version": 2,
            "project_id": "demo",
            "repo": tempfile.gettempdir(),
            "backend": "local",
            "default_agent_profile": "p-cb",
            "agent_profiles": {
                "p-cb": {"adapter": "codebuddy", "model": "stub", "backend": "codebuddy-cli"}
            },
            "allowed_paths": ["src"],
            "authorization_ref": "scope-1",
            "authorized_actions": ["create"],
        }
        broken = _profiles.normalize_config(
            {**base, "checks": [{"id": "unit", "argv": ["zoe-definitely-not-a-binary"]}]}
        )
        report = _config.doctor(broken, probe=False)
        self.assertFalse(report["checks"]["ready"])
        self.assertEqual(report["checks_preflight"][0]["status"], "unresolved")

        deferred = _profiles.normalize_config(
            {**base, "checks": [{"id": "unit", "argv": ["./bin/generated-tool"]}]}
        )
        report = _config.doctor(deferred, probe=False)
        self.assertEqual(report["checks_preflight"][0]["status"], "deferred")
        # deferred 不阻断：缺的是任务自己会装出来的工具。
        self.assertTrue(report["checks"]["checks_preflight_ok"])

    def test_unresolved_check_fails_as_environment_without_running(self):
        calls = []

        class Backend:
            def execute(self, *args, **kwargs):
                calls.append(args)
                return 0

        results, errors = _pipeline.run_checks(
            Backend(),
            None,
            None,
            None,
            [{"id": "unit", "argv": ["zoe-definitely-not-a-binary"]}],
            None,
            "marker",
        )
        self.assertEqual(calls, [], "an unresolvable command must not be launched")
        self.assertEqual(results[0]["status"], "FAIL")
        self.assertEqual(results[0]["error_class"], "environment")
        self.assertEqual(results[0]["error"]["code"], "ENVIRONMENT_ERROR")
        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
