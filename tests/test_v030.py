"""v0.3.0 multi-agent behaviour: gating, handover, evidence, events, migration, CLI.

These tests use an in-process backend double, exactly like the v0.2 suite: they
never invoke a real agent, Docker or a remote. Real end-to-end status stays
``NOT_RUN`` in ``docs/capabilities.md``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from src import _groups, _migrations, _profiles, _store
from src.core import (
    Error,
    Store,
    accept,
    atomic,
    create,
    digest,
    git,
    read,
    reassign,
    rebind,
    run_pipeline,
    start,
)

SRC = Path(__file__).resolve().parents[1]


class FakeBackend:
    """In-process backend double; never an execution backend."""

    def __init__(self, fail=False, mutate=False):
        self.calls = []
        self.fail = fail
        self.mutate = mutate

    def inspect(self, name):
        return None

    def execute(self, store, t, r, argv, log_name, worker=False, prompt=None):
        self.calls.append(log_name)
        if worker:
            (Path(t["workspace"]) / "src.txt").write_text("implemented\n")
        if self.mutate and not worker:
            (Path(t["workspace"]) / "src.txt").write_text("changed by check\n")
        return 1 if self.fail and not worker else 0


class FakePublisher:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def publish(self, t, s):
        self.calls += 1
        if self.fail:
            raise Error("REMOTE_UNKNOWN", "lost response")
        return {"number": 1, "url": "https://example.invalid/pr/1"}

    def refresh(self, t):
        return {"ready": True, "pr": {"number": 1, "url": "https://example.invalid/pr/1"}}


class ProjectFixture(unittest.TestCase):
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
        self.spec = self.root / "spec.md"
        self.spec.write_text("# Test\nAC-001 implement")
        self.evidence = {
            ("codebuddy", "codebuddy-cli", "1.2.3"): self._evidence(
                "codebuddy", "codebuddy-cli", "1.2.3"
            ),
            ("claude-code", "anthropic-api", "2.0.0"): self._evidence(
                "claude-code", "anthropic-api", "2.0.0"
            ),
            # v1.6.0: claude-code 已绑定 --permission-mode，"标签存在但无法证明
            # 执行"的样例改用 opencode（没有权限开关，也不声明绑定）。
            ("opencode", "opencode-cli", "1.4.0"): self._evidence(
                "opencode", "opencode-cli", "1.4.0"
            ),
        }
        self.config = self.project_config()
        self.register()

    def tearDown(self):
        self.store.db.close()
        self._td.cleanup()

    def _evidence(self, adapter_id, backend, version):
        path = self.root / (f"compat-{adapter_id}-{backend}.json")
        path.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "adapter_id": adapter_id,
                            "backend": backend,
                            "version": version,
                            "source": "local_admin",
                            "platform": "linux",
                            "matrix": "builtin",
                            "recorded_at": "2026-10-27",
                            "evidence_ref": "fixture",
                        },
                    ]
                }
            )
        )
        return str(path)

    def project_config(self):
        return {
            "schema_version": 2,
            "project_id": "demo",
            "repo": str(self.repo),
            "backend": "local",
            "default_agent_profile": "p-cb",
            "agent_profiles": {
                "p-cb": {
                    "adapter": "codebuddy",
                    "model": "stub-model",
                    "backend": "codebuddy-cli",
                    "expected_version": "1.2.3",
                    "permission_profile": "isolated-edit",
                    "compatibility_evidence": self.evidence[
                        ("codebuddy", "codebuddy-cli", "1.2.3")
                    ],
                },
                "p-claude": {
                    "adapter": "claude-code",
                    "model": "stub-model",
                    "backend": "anthropic-api",
                    "expected_version": "2.0.0",
                    "permission_profile": "isolated-edit",
                    "compatibility_evidence": self.evidence[
                        ("claude-code", "anthropic-api", "2.0.0")
                    ],
                },
                "p-opencode": {
                    "adapter": "opencode",
                    "model": "stub-model",
                    "backend": "opencode-cli",
                    "expected_version": "1.4.0",
                    "permission_profile": "isolated-edit",
                    "compatibility_evidence": self.evidence[("opencode", "opencode-cli", "1.4.0")],
                },
                "p-old-version": {
                    "adapter": "codebuddy",
                    "model": "stub-model",
                    "backend": "codebuddy-cli",
                    "expected_version": "1.1.0",
                    "permission_profile": "isolated-edit",
                    "compatibility_evidence": self.evidence[
                        ("codebuddy", "codebuddy-cli", "1.2.3")
                    ],
                },
                "p-unverified": {
                    "adapter": "codex",
                    "model": "stub-model",
                    "backend": "backend-pending",
                    "expected_version": "0.9.0",
                    "permission_profile": "isolated-edit-codex",
                    "credential_refs": ["model-codex"],
                },
            },
            "checks": [{"id": "unit", "argv": ["true"]}],
            "allowed_paths": ["src.txt"],
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute", "publish", "accept"],
            "max_attempts": 3,
        }

    def register(self):
        atomic(self.store.home / "projects" / "demo.json", self.config)

    def strict(self):
        """Opt into ``require_real_e2e``: the default policy only labels.

        Without this flag an unverified adapter is dispatched and marked
        ``unverified`` in the run record instead of being refused.
        """
        self.config["require_real_e2e"] = True
        self.register()

    def change_policy(self, **overrides):
        self.config = {**self.config, **overrides}
        self.register()

    def req(self, **kwargs):
        value = dict(
            schema_version=2, request_id="req-" + uuid.uuid4().hex, authorization_ref="scope-1"
        )
        value.update(kwargs)
        return value

    def new(self, profile="p-cb", mode="local_artifact"):
        result = create(
            self.store,
            self.req(
                project_id="demo",
                title="Test",
                spec_file=str(self.spec),
                agent_profile=profile,
                delivery_mode=mode,
            ),
        )
        return self.store.task(result["task_id"])

    def run_task(self, t, backend=None, publisher=None):
        t = self.store.task(t["task_id"])
        rid = "run-" + uuid.uuid4().hex[:16]
        self.store.run(
            {"run_id": rid, "task_id": t["task_id"], "status": "running", "container": None}
        )
        t.update(current_run_id=rid, state="RUNNING")
        self.store.save(t)
        return run_pipeline(
            self.store, t["task_id"], backend or FakeBackend(), publisher or FakePublisher()
        )


class TestAdapterGate(ProjectFixture):
    def test_unverified_profile_blocks_start(self):
        self.strict()
        t = self.new("p-unverified")
        with self.assertRaises(Error) as ctx:
            start(self.store, t["task_id"], self.req(expected_version=t["row_version"]))
        self.assertEqual(ctx.exception.code, "ADAPTER_UNVERIFIED")
        self.assertEqual(self.store.task(t["task_id"])["state"], "READY")

    def test_version_mismatch_blocks_start(self):
        self.strict()
        t = self.new("p-old-version")
        with self.assertRaises(Error) as ctx:
            start(self.store, t["task_id"], self.req(expected_version=t["row_version"]))
        self.assertEqual(ctx.exception.code, "ADAPTER_VERSION_MISMATCH")

    def test_unverified_profile_blocks_pipeline(self):
        self.strict()
        t = self.run_task(self.new("p-unverified"))
        self.assertEqual(t["state"], "BLOCKED")
        self.assertEqual(t["block_code"], "ADAPTER_UNVERIFIED")

    def test_default_policy_labels_instead_of_blocking(self):
        """默认策略：未验证执行器照常派发，但绑定必须自报 unverified。"""
        t = self.new("p-unverified")
        binding = _profiles.resolve_binding(t["config"], "p-unverified")
        self.assertTrue(binding.unverified)
        self.assertEqual(binding.trust_level, "unverified")
        # 默认策略下 run_pipeline 不再因能力态拒绝：它要跑到真正的阶段才失败。
        self.assertNotEqual(t.get("block_code"), "ADAPTER_UNVERIFIED")

    def test_verified_profile_passes_the_gate(self):
        t = self.new("p-cb")
        binding = _profiles.require_runnable(_profiles.resolve_binding(t["config"], "p-cb"))
        self.assertEqual(binding.capability, "VERIFIED")
        self.assertEqual(binding.verification.source, "local_admin")


class TestEvidence(ProjectFixture):
    def test_pipeline_records_adapter_and_evidence(self):
        t = self.run_task(self.new())
        self.assertEqual(t["state"], "REVIEW_READY")
        self.assertTrue(t["current_evidence_id"].startswith("ev-"))
        evidence = read(self.store.path(t) / "evidence" / (t["current_evidence_id"] + ".json"))
        self.assertEqual(evidence["candidate_sha"], t["validated_sha"])
        self.assertEqual(evidence["adapter"]["adapter_id"], "codebuddy")
        self.assertEqual(evidence["adapter"]["adapter_backend"], "codebuddy-cli")
        self.assertEqual(evidence["check_policy_hash"], digest(t["config"]["checks"]))
        run = self.store.get_run(t["current_run_id"])
        self.assertEqual(run["adapter_id"], "codebuddy")
        self.assertEqual(run["adapter_backend"], "codebuddy-cli")
        self.assertEqual(run["profile_id"], "p-cb")
        self.assertIn("argv", run["launch"])
        self.assertFalse(any("AC-001" in arg for arg in run["launch"]["argv"]))

    def test_tampered_evidence_blocks_acceptance(self):
        t = self.run_task(self.new())
        path = self.store.path(t) / "evidence" / (t["current_evidence_id"] + ".json")
        evidence = read(path)
        evidence["candidate_sha"] = "0" * 40
        atomic(path, evidence)
        with self.assertRaises(Error) as ctx:
            accept(
                self.store,
                t["task_id"],
                self.req(
                    expected_version=t["row_version"],
                    subject_sha=t["validated_sha"],
                    actor="human",
                    decision="accepted",
                    evidence=["checked"],
                ),
            )
        self.assertEqual(ctx.exception.code, "EVIDENCE_INVALID")

    def test_missing_evidence_blocks_acceptance(self):
        t = self.run_task(self.new())
        (self.store.path(t) / "evidence" / (t["current_evidence_id"] + ".json")).unlink()
        with self.assertRaises(Error):
            accept(
                self.store,
                t["task_id"],
                self.req(
                    expected_version=t["row_version"],
                    subject_sha=t["validated_sha"],
                    actor="human",
                    decision="accepted",
                    evidence=["checked"],
                ),
            )

    def test_accept_succeeds_with_intact_evidence(self):
        t = self.run_task(self.new())
        result = accept(
            self.store,
            t["task_id"],
            self.req(
                expected_version=t["row_version"],
                subject_sha=t["validated_sha"],
                actor="human",
                decision="accepted",
                evidence=["checked"],
            ),
        )
        self.assertEqual(result["state"], "DONE")


class TestReassign(ProjectFixture):
    def blocked_task(self):
        t = self.run_task(self.new())
        # Force a verification failure at the implement phase.
        self.config["checks"] = [{"id": "unit", "argv": ["false"]}]
        self.register()
        # Rebind is not needed: the task keeps its frozen config, which failed.
        return t

    def test_handover_requires_a_verified_target(self):
        self.strict()
        t = self.new()
        with self.assertRaises(Error) as ctx:
            reassign(
                self.store,
                t["task_id"],
                self.req(
                    expected_version=t["row_version"],
                    target_profile="p-unverified",
                    reason="try codex",
                    authorization_ref="scope-1",
                ),
            )
        self.assertEqual(ctx.exception.code, "ADAPTER_UNVERIFIED")

    def test_handover_preserves_budget_and_clears_evidence(self):
        t = self.new()
        t = self.run_task(t, FakeBackend(fail=True))
        self.assertEqual(t["block_code"], "VALIDATION_FAILED")
        attempts = t["attempts"]
        result = reassign(
            self.store,
            t["task_id"],
            self.req(
                expected_version=t["row_version"],
                target_profile="p-claude",
                reason="codebuddy could not finish",
                authorization_ref="scope-1",
            ),
        )
        self.assertEqual(result["state"], "READY")
        self.assertEqual(result["assignment_revision"], 2)
        self.assertEqual(result["adapter_id"], "claude-code")
        t = self.store.task(t["task_id"])
        self.assertEqual(t["attempts"], attempts, "handover must not reset the retry budget")
        self.assertIsNone(t["validated_sha"])
        self.assertIsNone(t["current_evidence_id"])
        self.assertIsNone(t["acceptance"])
        self.assertEqual(t["phase"], "implement")
        archive = read(self.store.path(t) / "assignments" / "reassign-02.json")
        self.assertEqual(archive["previous"]["adapter_id"], "codebuddy")
        self.assertEqual(archive["next"]["adapter_id"], "claude-code")

    def test_handover_refuses_a_drifted_workspace(self):
        t = self.run_task(self.new(), FakeBackend(fail=True))
        (Path(t["workspace"]) / "src.txt").write_text("manually edited\n")
        with self.assertRaises(Error) as ctx:
            reassign(
                self.store,
                t["task_id"],
                self.req(
                    expected_version=self.store.task(t["task_id"])["row_version"],
                    target_profile="p-claude",
                    reason="handover",
                    authorization_ref="scope-1",
                ),
            )
        self.assertEqual(ctx.exception.code, "HANDOFF_STALE")

    def test_review_ready_cannot_switch_implementer(self):
        t = self.run_task(self.new())
        with self.assertRaises(Error) as ctx:
            reassign(
                self.store,
                t["task_id"],
                self.req(
                    expected_version=t["row_version"],
                    target_profile="p-claude",
                    reason="late switch",
                    authorization_ref="scope-1",
                ),
            )
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")


class TestRebind(ProjectFixture):
    def test_rebind_adopts_policy_and_invalidates_evidence(self):
        t = self.run_task(self.new())
        self.change_policy(max_attempts=5)
        current = self.store.config("demo")
        result = rebind(
            self.store,
            t["task_id"],
            self.req(
                expected_version=t["row_version"],
                target_policy_hash=digest(current),
                reason="approved new policy",
                authorization_ref="scope-1",
            ),
        )
        self.assertEqual(result["policy_revision"], digest(current)[:16])
        t = self.store.task(t["task_id"])
        self.assertIsNone(t["current_evidence_id"])
        self.assertIsNone(t["validated_sha"])
        self.assertIsNone(t["acceptance"])
        self.assertEqual(t["config"]["max_attempts"], 5)

    def test_rebind_refuses_a_policy_the_frozen_spec_violates(self):
        structured = {
            "schema_version": 2,
            "title": "Export",
            "open_questions": [],
            "acceptance_criteria": [
                {
                    "id": "AC-01",
                    "statement": "x",
                    "verification": {"type": "check", "check_id": "unit"},
                }
            ],
            "allowed_paths": ["src.txt", "tests/"],
        }
        self.spec.write_text(json.dumps(structured))
        self.change_policy(allowed_paths=["src.txt", "tests/"])
        t = self.new()
        # New policy narrows the paths the frozen spec still relies on.
        self.change_policy(allowed_paths=["src.txt"])
        current = self.store.config("demo")
        with self.assertRaises(Error) as ctx:
            rebind(
                self.store,
                t["task_id"],
                self.req(
                    expected_version=t["row_version"],
                    target_policy_hash=digest(current),
                    reason="narrow paths",
                    authorization_ref="scope-1",
                ),
            )
        self.assertEqual(ctx.exception.code, "POLICY_CHANGED")
        self.assertEqual(self.store.task(t["task_id"])["policy_revision"], t["policy_revision"])

    def test_rebind_requires_the_current_policy_hash(self):
        t = self.new()
        with self.assertRaises(Error) as ctx:
            rebind(
                self.store,
                t["task_id"],
                self.req(
                    expected_version=t["row_version"],
                    target_policy_hash="0" * 64,
                    reason="wrong hash",
                    authorization_ref="scope-1",
                ),
            )
        self.assertEqual(ctx.exception.code, "POLICY_CHANGED")


class TestGroups(ProjectFixture):
    def test_group_creation_is_a_record_not_a_scheduler(self):
        result = _groups.create(
            self.store,
            self.req(
                project_id="demo",
                title="Release train",
                authorization_ref="scope-1",
                ordered_items=[
                    {"item_id": "i1", "title": "first", "spec_ref": "specs/1.md"},
                    {"item_id": "i2", "title": "second", "spec_ref": "specs/2.md"},
                ],
            ),
        )
        status = _groups.status(self.store, result["group_id"])
        self.assertEqual([item["state"] for item in status["items"]], ["NOT_STARTED"] * 2)
        self.assertEqual(status["next_item"], "i1")
        self.assertEqual(len(self.store.tasks()), 0, "creating a group must start nothing")

    def test_task_binds_to_exactly_one_item(self):
        result = _groups.create(
            self.store,
            self.req(
                project_id="demo",
                title="g",
                authorization_ref="scope-1",
                ordered_items=[{"item_id": "i1", "title": "first", "spec_ref": "specs/1.md"}],
            ),
        )
        t = create(
            self.store,
            self.req(
                project_id="demo",
                title="one",
                spec_file=str(self.spec),
                delivery_mode="local_artifact",
                agent_profile="p-cb",
                group_id=result["group_id"],
                item_id="i1",
            ),
        )
        status = _groups.status(self.store, result["group_id"])
        self.assertEqual(status["items"][0]["task_id"], t["task_id"])
        self.assertEqual(status["items"][0]["state"], "READY")
        with self.assertRaises(Error) as ctx:
            create(
                self.store,
                self.req(
                    project_id="demo",
                    title="two",
                    spec_file=str(self.spec),
                    delivery_mode="local_artifact",
                    agent_profile="p-cb",
                    group_id=result["group_id"],
                    item_id="i1",
                ),
            )
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")

    def test_duplicate_item_ids_are_refused(self):
        with self.assertRaises(Error) as ctx:
            _groups.create(
                self.store,
                self.req(
                    project_id="demo",
                    title="g",
                    authorization_ref="scope-1",
                    ordered_items=[
                        {"item_id": "i1", "title": "a", "spec_ref": "s"},
                        {"item_id": "i1", "title": "b", "spec_ref": "s"},
                    ],
                ),
            )
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")


class TestStructuredSpec(ProjectFixture):
    def spec_json(self, **criteria):
        document = {
            "schema_version": 2,
            "title": "Export orders",
            "goal": "Authorized users export filtered orders",
            "open_questions": [],
            "acceptance_criteria": [
                {
                    "id": "AC-01",
                    "statement": "unauthorized rejected",
                    "verification": {"type": "check", "check_id": "unit"},
                },
            ],
            "allowed_paths": ["src.txt"],
        }
        document.update(criteria)
        self.spec.write_text(json.dumps(document))
        return document

    def test_structured_spec_is_accepted(self):
        self.spec_json()
        t = self.new()
        self.assertEqual(t["spec_contract_version"], 2)

    def test_unknown_check_reference_is_refused(self):
        self.spec_json(
            acceptance_criteria=[
                {
                    "id": "AC-01",
                    "statement": "x",
                    "verification": {"type": "check", "check_id": "not-approved"},
                }
            ]
        )
        with self.assertRaises(Error) as ctx:
            self.new()
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")

    def test_open_questions_block_start(self):
        self.spec_json(open_questions=["Which columns?"])
        with self.assertRaises(Error) as ctx:
            self.new()
        self.assertEqual(ctx.exception.code, "SPEC_CLARIFICATION_REQUIRED")

    def test_allowed_paths_cannot_widen_policy(self):
        self.spec_json(allowed_paths=["src.txt", "deploy/"])
        with self.assertRaises(Error) as ctx:
            self.new()
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")

    def test_duplicate_acceptance_ids_are_refused(self):
        self.spec_json(
            acceptance_criteria=[
                {
                    "id": "AC-01",
                    "statement": "a",
                    "verification": {"type": "manual", "instructions": "x"},
                },
                {
                    "id": "AC-01",
                    "statement": "b",
                    "verification": {"type": "manual", "instructions": "y"},
                },
            ]
        )
        with self.assertRaises(Error):
            self.new()


class TestEvents(ProjectFixture):
    def test_events_are_paged_by_monotonic_id(self):
        t = self.run_task(self.new())
        events, cursor, has_more = self.store.events(t["task_id"], 0, 2)
        self.assertEqual(len(events), 2)
        self.assertTrue(has_more)
        self.assertEqual(events[0]["type"], "created")
        following, next_cursor, _ = self.store.events(t["task_id"], cursor, 500)
        self.assertTrue(following)
        self.assertGreater(next_cursor, cursor)

    def test_event_payload_records_state_phase_and_run(self):
        t = self.run_task(self.new())
        events, _, _ = self.store.events(t["task_id"], 0, 500)
        kinds = {event["type"] for event in events}
        self.assertIn("created", kinds)
        self.assertIn("phase_update", kinds)
        self.assertIn("run_finalized", kinds)
        self.assertTrue(all("payload_version" in event for event in events))


class TestResponseEnvelope(ProjectFixture):
    """§12: every v2 mutation carries api_version / operation_id / evidence_refs."""

    def test_create_response_is_enveloped(self):
        req = self.req(
            project_id="demo",
            title="T",
            spec_file=str(self.spec),
            delivery_mode="local_artifact",
            agent_profile="p-cb",
        )
        result = create(self.store, req)
        self.assertEqual(result["api_version"], 2)
        self.assertEqual(result["operation_id"], req["request_id"])
        self.assertEqual(result["evidence_refs"], [])
        self.assertEqual(result["recommended_action"], "start")

    def test_reassign_and_rebind_responses_are_enveloped(self):
        t = self.run_task(self.new(), FakeBackend(fail=True))
        result = reassign(
            self.store,
            t["task_id"],
            self.req(
                expected_version=t["row_version"],
                target_profile="p-claude",
                reason="handover",
                authorization_ref="scope-1",
            ),
        )
        self.assertEqual(result["api_version"], 2)
        self.assertIn("operation_id", result)
        self.assertIn("evidence_refs", result)
        self.assertEqual(result["recommended_action"], "resume")


class TestPermissionGate(ProjectFixture):
    """§6/§10.2: an unenforced permission profile fails closed when demanded."""

    def test_pipeline_blocks_when_a_binding_is_required_but_missing(self):
        # v0.5.1: codebuddy 已绑定 --permission-mode，故改用 claude-code；
        # v1.6.0: claude-code 也已绑定，改由 opencode 演示"标签存在但无法证明
        # 执行"的 profile 在显式要求时被拒（它没有权限开关，也没有绑定）。
        self.config["agent_profiles"]["p-opencode"]["require_permission_binding"] = True
        self.register()
        t = self.run_task(self.new(profile="p-opencode"))
        self.assertEqual(t["state"], "BLOCKED")
        self.assertEqual(t["block_code"], "PERMISSION_PROFILE_UNBOUND")


class TestSessionDir(ProjectFixture):
    """§10.3/§10.4: diagnostic session dir isolated by task/run/adapter/backend."""

    def test_session_dir_is_isolated_per_adapter_and_backend(self):
        t = self.run_task(self.new())
        run = self.store.get_run(t["current_run_id"])
        expected = (
            self.store.path(t) / "runs" / run["run_id"] / "session" / "codebuddy" / "codebuddy-cli"
        )
        self.assertTrue(expected.is_dir())
        self.assertEqual(run["launch"]["session_dir"], str(expected))


class TestLegacyTaskResume(ProjectFixture):
    """§14: a task frozen under v0.2 (v1 config, no agent_profiles) still resumes."""

    def legacy_config(self):
        return {
            "schema_version": 1,
            "project_id": "demo",
            "repo": str(self.repo),
            "backend": "local",
            "network": "none",
            "agent": "codebuddy",
            "compatibility_verified": True,
            "agent_env": ["CODEBUDDY_API_KEY"],
            "checks": [{"id": "unit", "argv": ["true"]}],
            "allowed_paths": ["src.txt"],
            "authorization_ref": "scope-1",
            "authorized_actions": ["create", "execute", "publish", "accept"],
        }

    def test_frozen_v1_config_is_normalized_on_resume(self):
        t = self.new()
        # A v0.2 record has no (adapter, backend) assignment fields either.
        for key in (
            "agent_profile_id",
            "adapter_id",
            "adapter_backend",
            "assignment_revision",
            "assignments",
        ):
            t.pop(key, None)
        t["config"] = self.legacy_config()
        self.store.save(t)
        t = self.run_task(self.store.task(t["task_id"]))
        self.assertEqual(t["state"], "REVIEW_READY")
        self.assertTrue(t["current_evidence_id"])
        run = self.store.get_run(t["current_run_id"])
        self.assertEqual(run["adapter_id"], "codebuddy")
        self.assertEqual(run["adapter_backend"], "codebuddy-cli")
        # The legacy profile flowed through the same launch contract.
        self.assertIn("permission", run["launch"]["extra"])
        binding = _profiles.resolve_binding(_profiles.normalize_config(self.legacy_config()))
        self.assertEqual(binding.capability, "VERIFIED")
        self.assertEqual(binding.verification.source, "legacy_admin_declaration")


class TestMigration(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.home = Path(self._td.name) / "state"
        self.home.mkdir(parents=True)
        (self.home / "projects").mkdir()
        db = sqlite3.connect(self.home / "state.db")
        db.executescript(
            """
            CREATE TABLE tasks(id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE TABLE runs(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE operations(id TEXT PRIMARY KEY, hash TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE events(id INTEGER PRIMARY KEY, task_id TEXT, at REAL, data TEXT NOT NULL);
            PRAGMA user_version=1;
            """
        )
        legacy = {
            "task_id": "task-legacy",
            "project_id": "old",
            "state": "READY",
            "phase": "prepare",
            "row_version": 1,
            "config": {},
            "spec_revision": 1,
        }
        db.execute("INSERT INTO tasks VALUES(?,?,?)", ("task-legacy", 1, json.dumps(legacy)))
        db.commit()
        db.close()
        (self.home / "projects" / "old.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_id": "old",
                    "repo": str(self.home),
                    "backend": "local",
                    "agent": "unknown-agent",
                    "checks": [{"id": "unit", "argv": ["true"]}],
                    "allowed_paths": ["x"],
                    "authorization_ref": "s",
                }
            )
        )

    def tearDown(self):
        self._td.cleanup()

    def test_v1_state_requires_explicit_migration(self):
        with self.assertRaises(Error) as ctx:
            Store(self.home)
        self.assertEqual(ctx.exception.code, "MIGRATION_REQUIRED")

    def test_dry_run_changes_nothing(self):
        plan = _migrations.migrate(self.home, 3, dry_run=True)
        self.assertFalse(plan["changed"])
        self.assertTrue(plan["dry_run"])
        db = sqlite3.connect(self.home / "state.db")
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        db.close()

    def test_migration_backs_up_and_keeps_unknown_adapters_blocked(self):
        # v1.3 raises the schema to 4: a v3 database is refused by the store, so
        # the migration target must be the current schema, not the old one.
        result = _migrations.migrate(self.home, _store.SCHEMA_VERSION)
        self.assertTrue(result["changed"])
        self.assertEqual(result["integrity_check"], "ok")
        self.assertTrue(Path(result["backup"]).is_file())
        self.assertTrue(result["warnings"], "unregistered adapter must be reported, not rewritten")
        self.assertEqual(result["warnings"][0]["code"], "ADAPTER_UNVERIFIED")
        store = Store(self.home)
        try:
            self.assertEqual(store.task("task-legacy")["state"], "READY")
        finally:
            store.db.close()

    def test_migration_is_idempotent(self):
        _migrations.migrate(self.home, _store.SCHEMA_VERSION)
        again = _migrations.migrate(self.home, _store.SCHEMA_VERSION)
        self.assertFalse(again["changed"])


def run_cli(*argv, home):
    env = {**os.environ, "PYTHONPATH": str(SRC), "ZOE_HOME": str(home)}
    return subprocess.run(
        [sys.executable, "-m", "src", *argv], capture_output=True, text=True, env=env
    )


class TestCapabilitiesCli(unittest.TestCase):
    def test_capabilities_lists_registry_without_creating_state(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "state"
            p = run_cli("capabilities", "--json", home=home)
            self.assertEqual(p.returncode, 0, p.stderr)
            data = json.loads(p.stdout)
            adapters = {entry["adapter_id"]: entry for entry in data["adapters"]}
            self.assertEqual(adapters["codex"]["kind"], "extended")
            self.assertEqual(adapters["codebuddy"]["kind"], "builtin")
            for entry in adapters.values():
                for backend in entry["backends"]:
                    self.assertIn(backend["capability"], ("IMPLEMENTED", "UNVERIFIED", "VERIFIED"))
                    self.assertTrue(backend["reason"])
            self.assertIn("native_session_resume", data["not_supported"])
            self.assertFalse(home.exists(), "capabilities must not create a state directory")

    def test_unknown_adapter_does_not_appear_as_available(self):
        data = json.loads(
            run_cli("capabilities", "--json", home=Path(tempfile.gettempdir()) / "zoe-cap").stdout
        )
        ids = {entry["adapter_id"] for entry in data["adapters"]}
        self.assertEqual(ids, {"codebuddy", "opencode", "claude-code", "codex"})


class TestAgentCli(ProjectFixture):
    def test_agent_list_shows_capability_per_profile(self):
        p = run_cli("agent", "list", "--project", "demo", "--json", home=self.store.home)
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        profiles = {view["profile_id"]: view for view in data["profiles"]}
        self.assertEqual(profiles["p-cb"]["capability"], "VERIFIED")
        self.assertEqual(profiles["p-old-version"]["capability"], "UNVERIFIED")
        self.assertEqual(profiles["p-unverified"]["capability"], "IMPLEMENTED")
        self.assertNotIn("sk-", p.stdout)
        self.assertEqual(profiles["p-cb"]["verification"]["source"], "local_admin")

    def test_agent_probe_is_bounded_and_does_not_verify(self):
        # v0.5.0: the probe runs on this host against the same pinned binary the
        # launcher would spawn, so a stub CLI makes it deterministic.
        with tempfile.TemporaryDirectory() as td:
            stub_dir = Path(td) / "bin"
            stub_dir.mkdir()
            stub = stub_dir / "codebuddy"
            stub.write_text("#!/bin/sh\necho '1.2.3 (stub)'\n")
            stub.chmod(0o755)
            env = {
                **os.environ,
                "PYTHONPATH": str(SRC),
                "ZOE_HOME": str(self.store.home),
                "PATH": str(stub_dir) + os.pathsep + os.environ.get("PATH", ""),
            }
            p = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "src",
                    "agent",
                    "probe",
                    "--project",
                    "demo",
                    "--profile",
                    "p-cb",
                    "--json",
                ],
                capture_output=True,
                text=True,
                env=env,
            )
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["probe_status"], "PASSED")
        self.assertEqual(data["version"], "1.2.3")
        self.assertEqual(data["command"][0], str(stub), "argv[0] must be the resolved path")
        self.assertEqual(data["resolution"]["source"], "process-path")
        # A probe proves "this binary answers --version" and nothing more.
        self.assertEqual(data["capability"], "VERIFIED")
        self.assertTrue(data["notes"])

    def test_agent_probe_blocks_when_facts_are_pending(self):
        p = run_cli(
            "agent",
            "probe",
            "--project",
            "demo",
            "--profile",
            "p-unverified",
            "--json",
            home=self.store.home,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["probe_status"], "BLOCKED")


class TestTaskCli(ProjectFixture):
    def test_task_events_and_logs(self):
        t = self.run_task(self.new())
        p = run_cli("task", "events", t["task_id"], "--limit", "5", "--json", home=self.store.home)
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertTrue(data["events"])
        self.assertIn("next_cursor", data)

        log = self.store.path(t) / "runs" / t["current_run_id"] / "agent.log"
        log.write_text("CODEBUDDY_API_KEY=sk-abcdefgh12345678\nall good\n")
        p = run_cli(
            "task", "logs", t["task_id"], "--tail-bytes", "4096", "--json", home=self.store.home
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertIn("all good", data["text"])
        self.assertNotIn("sk-abcdefgh12345678", data["text"])


class TestStateCli(unittest.TestCase):
    def test_state_migrate_reports_missing_database(self):
        with tempfile.TemporaryDirectory() as td:
            p = run_cli("state", "migrate", "--to", "2", "--json", home=Path(td) / "nope")
            self.assertEqual(p.returncode, 2, p.stdout)
            self.assertEqual(json.loads(p.stdout)["error"]["code"], "INPUT_INCOMPLETE")


if __name__ == "__main__":
    unittest.main()
