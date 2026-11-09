"""Contract artifacts: docs, schemas, the skill tree and the runtime must agree.

A document that claims a command the CLI rejects (or an adapter the registry does
not know) is a bug, not a typo. These tests are the drift gate from the design
§16/§18.

Since the split (§1) the skill is a **separate repository**. The parts of this
module that read ``SKILL.md``, ``references/`` and the skill's ``build.py``
resolve that checkout through ``ZOE_SKILL_DIR`` and skip when it is absent; the
runner-side artifacts (``schemas/``, ``docs/``, READMEs) always run here.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import unittest
from pathlib import Path

from skill_package import SKIP_REASON, skill_dir

from src import _requirements
from src._agents import registry
from src._agents.base import ProcessCapture
from src._agents.codebuddy import CodeBuddyAdapter
from src._util import Error
from src.cli import _build_parser, capabilities

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
#: The skill is a separate repository (§1); tests that read its files resolve it
#: via ``ZOE_SKILL_DIR`` and skip (not pass) when it is absent.
SKILL = skill_dir()
REFERENCES = SKILL / "references" if SKILL else None


def load_build_module():
    """Load ``build.py`` from the skill checkout, not from this repository."""
    spec = importlib.util.spec_from_file_location("skill_build", SKILL / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate(instance, schema, path="$"):
    """Minimal validator for the schema subset used in ``schemas/``."""
    errors = []
    if "const" in schema and instance != schema["const"]:
        errors.append("{}: expected const {!r}".format(path, schema["const"]))
    if "enum" in schema and instance not in schema["enum"]:
        errors.append("{}: {!r} not in {!r}".format(path, instance, schema["enum"]))
    expected = schema.get("type")
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        mapping = {
            "object": dict,
            "array": list,
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "null": type(None),
        }
        if not any(
            isinstance(instance, mapping[t])
            and not (t in ("integer", "number") and isinstance(instance, bool))
            for t in types
        ):
            errors.append(f"{path}: {instance!r} is not {expected!r}")
            return errors
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required {key!r}")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                errors.extend(validate(value, properties[key], path + "." + key))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property {key!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors.extend(validate(value, schema["additionalProperties"], path + "." + key))
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(instance):
                errors.extend(validate(item, schema["items"], f"{path}[{index}]"))
    if isinstance(instance, str):
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append("{}: {!r} does not match {!r}".format(path, instance, schema["pattern"]))
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
    return errors


def load_schema(name):
    return json.loads((SCHEMAS / name).read_text())


def command_names(parser, found=None):
    found = found if found is not None else set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                found.add(name)
                command_names(sub, found)
    return found


class TestSchemaArtifacts(unittest.TestCase):
    def test_all_schemas_parse_and_are_draft_2020_12(self):
        files = sorted(SCHEMAS.glob("*.json"))
        self.assertGreaterEqual(len(files), 4)
        for path in files:
            schema = json.loads(path.read_text())
            self.assertIn("$schema", schema, path.name)
            self.assertIn("2020-12", schema["$schema"], path.name)
            self.assertIn("$id", schema, path.name)

    def test_capabilities_output_matches_its_schema(self):
        errors = validate(capabilities(), load_schema("capabilities.v2.json"))
        self.assertEqual(errors, [])

    def test_agent_result_matches_its_schema(self):
        result = CodeBuddyAdapter().parse_output(
            ProcessCapture(
                adapter_id="codebuddy",
                adapter_backend="codebuddy-cli",
                exit_code=0,
                raw_text='{"type":"result","is_error":false,"result":"done"}',
            )
        )
        errors = validate(result.to_dict(), load_schema("agent-result.v1.json"))
        self.assertEqual(errors, [])

    @unittest.skipUnless(SKILL, SKIP_REASON)
    def test_project_profile_examples_match_their_schema(self):
        """v1.5.0 M1：三档示例都要过 schema（默认 / 多模型 / 高级）。"""
        for name in (
            "project-profile.example.json",
            "project-profile.multi-model.example.json",
            "project-profile.advanced.example.json",
        ):
            with self.subTest(example=name):
                example = json.loads((REFERENCES / name).read_text())
                self.assertEqual(validate(example, load_schema("project-profile.v2.json")), [])

    @unittest.skipUnless(SKILL, SKIP_REASON)
    def test_project_profile_examples_are_refused_until_placeholders_are_filled(self):
        """占位符必须在注册期就被指出，而不是等到 Agent 启动后 AGENT_FAILED。"""
        from src import _config

        for name in (
            "project-profile.example.json",
            "project-profile.multi-model.example.json",
            "project-profile.advanced.example.json",
        ):
            with self.subTest(example=name):
                example = json.loads((REFERENCES / name).read_text())
                with self.assertRaises(Error) as ctx:
                    _config.validate_config(example)
                self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
                fields = {hit["field"] for hit in ctx.exception.details["fields"]}
                self.assertIn("repo", fields)
                # 至少有一个 profile 的 model 还没填
                self.assertTrue(any(field.endswith(".model") for field in fields), sorted(fields))

    @unittest.skipUnless(SKILL, SKIP_REASON)
    def test_project_profile_example_is_runnable_after_placeholders_are_replaced(self):
        """Schema 通过不等于能注册：替换占位符后必须真的过规范化和绑定。"""
        import tempfile

        from src import _config

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            for name in (
                "project-profile.example.json",
                "project-profile.multi-model.example.json",
            ):
                with self.subTest(example=name):
                    example = json.loads((REFERENCES / name).read_text())
                    example["repo"] = str(repo)
                    example["authorization_ref"] = "scope-1"
                    for profile in example["agent_profiles"].values():
                        profile["expected_version"] = "1.2.3"
                        profile["model"] = "stub-model"
                    _config.validate_config(example, base_dir=Path(tmp))

    def test_compatibility_record_shape_matches_runtime_parser(self):
        schema = load_schema("compat-evidence.v1.json")
        record = {
            "adapter_id": "codex",
            "backend": "backend-pending",
            "version": "0.42.0",
            "source": "local_admin",
            "platform": "linux-x86_64",
            "matrix": "extended",
            "recorded_at": "2026-10-27",
        }
        errors = validate({"records": [record]}, schema)
        self.assertEqual(errors, [])
        verification = registry.Verification.from_dict(record)
        self.assertEqual(verification.version, "0.42.0")
        # Version ranges and empty versions are refused by the runtime, so the
        # schema must require a concrete string too.
        with self.assertRaises(Error):
            registry.Verification.from_dict({**record, "version": ""})


class TestDocumentationMatchesRuntime(unittest.TestCase):
    @unittest.skipUnless(SKILL, SKIP_REASON)
    def test_cli_contract_documents_every_command(self):
        contract = (REFERENCES / "cli-contract.md").read_text()
        for name in sorted(command_names(_build_parser())):
            self.assertIn(name, contract, f"CLI command {name!r} is not documented")

    def test_capabilities_doc_covers_every_adapter_and_backend(self):
        document = (ROOT / "docs" / "capabilities.md").read_text()
        self.assertIn("ADAPTER_UNVERIFIED", document)
        self.assertIn("real-e2e", document)
        for entry in registry.entries():
            adapter = entry.adapter
            self.assertIn(f"`{entry.adapter_id}`", document)
            for backend in adapter.supported_backends:
                self.assertIn(
                    f"`{backend}`",
                    document,
                    f"backend {backend!r} missing from docs/capabilities.md",
                )
        doc_ids = set(re.findall(r"\| `([a-z-]+)` \|", document))
        self.assertTrue({e.adapter_id for e in registry.entries()} <= doc_ids)

    def test_compatibility_readme_documents_the_evidence_rules(self):
        document = (ROOT / "docs" / "compatibility" / "README.md").read_text()
        for token in (
            "real_e2e",
            "local_admin",
            "expected_version",
            "FACT_PENDING",
            "ADAPTER_BACKEND_MISMATCH",
        ):
            self.assertIn(token, document)

    def test_readme_states_the_unverified_boundary(self):
        for name in ("README.md", "README.zh-CN.md"):
            text = (ROOT / name).read_text()
            self.assertIn("ADAPTER_UNVERIFIED", text)
            self.assertIn("VERIFIED", text)
            self.assertIn("capabilities", text)

    def test_architecture_doc_lists_every_module(self):
        document = (ROOT / "docs" / "architecture.md").read_text()
        modules = sorted((ROOT / "src").glob("*.py")) + sorted(
            (ROOT / "src" / "_agents").glob("*.py")
        )
        for module in modules:
            if module.name in ("__init__.py", "__main__.py", "_version.py"):
                continue
            self.assertIn(module.name, document, f"{module.name} is not in the module map")


class TestExitCodeContract(unittest.TestCase):
    """§13: the exit-code table is frozen for v0.5.0.

    Changing a value silently breaks every host that branches on it, so a change
    must be a deliberate edit here *and* in ``references/cli-contract.md``.
    """

    EXPECTED = {
        "INPUT_INCOMPLETE": 2,
        "SPEC_CLARIFICATION_REQUIRED": 2,
        "STATE_CONFLICT": 3,
        "REQUEST_CONFLICT": 3,
        "SLOT_BUSY": 3,
        "HANDOFF_STALE": 3,
        "EVIDENCE_INVALID": 3,
        "EVIDENCE_MISSING": 3,
        "CURSOR_EXPIRED": 3,
        "PROTOCOL_UNSUPPORTED": 3,
        "MAINTENANCE_REQUIRED": 3,
        "ENVIRONMENT_ERROR": 4,
        "CAPABILITY_UNSUPPORTED": 4,
        "ADAPTER_UNVERIFIED": 4,
        "ADAPTER_VERSION_MISMATCH": 4,
        "ADAPTER_BACKEND_MISMATCH": 4,
        "MIGRATION_REQUIRED": 4,
        "SCHEMA_UNSUPPORTED": 4,
        "PROCESS_UNKNOWN": 4,
        "PROMPT_TOO_LARGE": 4,
        "OUTPUT_LIMIT_EXCEEDED": 4,
        "RUN_TIMEOUT": 4,
        "QUOTA_EXHAUSTED": 4,
        "TASK_BUSY": 4,
        "REPO_BUSY": 4,
        "PUBLISH_BUSY": 4,
        "MAINTENANCE_BUSY": 4,
        "DEPENDENCY_BLOCKED": 4,
        "DEPENDENCY_STALE": 4,
        "PATH_CONFLICT": 4,
        "QUALIFICATION_REQUIRED": 4,
        "INCOMPLETE_QUALIFICATION": 4,
        "DECISION_CONFLICT": 4,
        "NOT_OWNER": 4,
        "BUDGET_EXHAUSTED": 4,
        "SCOPE_VIOLATION": 4,
        "EXTRA_COMMIT": 4,
        "PROJECT_MISMATCH": 4,
        "TASK_RUN_MISMATCH": 4,
        "SESSION_SCOPE_MISMATCH": 4,
        "MISSING_CONTEXT": 4,
        "RESOURCE_CONFLICT": 4,
        "REMOTE_DRIFT": 4,
        # v1.6：冻结基线已不在目标分支历史里（rebase / force-push）。与
        # REMOTE_DRIFT 同族：不是"重试即可"，也不是 POLICY_CHANGED 的 rebind 路径。
        "BASE_DRIFT": 4,
        # The skill gate: "wrong runtime", not "bad input" (§2.2).
        "RUNTIME_INCOMPATIBLE": 5,
        # v1.5 attestation: the configuration digest changed after the allowed
        # profile set was confirmed, so dispatch is refused until re-attested.
        "ALLOWLIST_UNCONFIRMED": 5,
        # v1.5.1 attest preconditions: `--expect-policy-hash` / `--expect-profiles`
        # no longer match the configuration read inside the write lock, so the
        # record was NOT written. 5 = a gate refused, not "retry the same call".
        "ATTEST_PRECONDITION_FAILED": 5,
        "AUTH_REQUIRED": 5,
        "POLICY_DENIED": 5,
        "POLICY_CHANGED": 5,
        "PERMISSION_PROFILE_UNBOUND": 5,
        "PLATFORM_UNSUPPORTED": 5,
        "REMOTE_UNKNOWN": 6,
        # v1.2 execution-combination routing (§15.4). Frozen here so the mapping
        # cannot drift silently: 4 = the host cannot do it, 5 = a gate refused,
        # 3 = the record/decision itself is not trustworthy.
        "MODEL_UNAVAILABLE": 4,
        "MODEL_MISMATCH": 5,
        "ENDPOINT_UNVERIFIED": 5,
        "SPEND_LIMIT_EXCEEDED": 5,
        "SPEND_UNVERIFIABLE": 5,
        "ROUTE_NO_ELIGIBLE": 4,
        "ROUTE_AMBIGUOUS": 3,
        "ROUTE_RECORD_INVALID": 3,
        # v1.3 task-group delivery (TGD §11.3), transcribed from the frozen
        # table: all four integration codes are 5, so "delivery stopped for a
        # reason a human must read" is one exit status, not two.
        "INTEGRATION_CONFLICT": 5,
        "INTEGRATION_CHECK_FAILED": 5,
        "INTEGRATION_UNKNOWN": 5,
        "GROUP_BUDGET_EXHAUSTED": 5,
        # v1.7 per-AC acceptance verdict. 3 = the record is missing or not
        # acknowledged (fix: produce/confirm a review); 5 = the verdict itself
        # failed (fix: the implementation, not the paperwork).
        "ACCEPTANCE_UNREVIEWED": 3,
        "ACCEPTANCE_UNACKNOWLEDGED": 3,
        "ACCEPTANCE_REVIEW_FAILED": 5,
        "DECISIONS_OPEN": 5,
        "PROGRESS_NOT_TICKED": 5,
        "DEPENDENCY_NOT_MET": 5,
    }

    def test_exit_codes_are_frozen(self):
        from src.cli import _EXIT_CODES

        self.assertEqual(_EXIT_CODES, self.EXPECTED)

    def test_drift_kinds_do_not_invent_new_codes(self):
        """Three kinds share ``REMOTE_DRIFT``; the difference lives in details."""
        from src import _drift

        for kind in _drift.DRIFT_KINDS:
            self.assertEqual(_drift.error_code({"kind": kind}), "REMOTE_DRIFT")
        self.assertEqual(_drift.error_code({"kind": _drift.DRIFT_REMOTE_UNKNOWN}), "REMOTE_UNKNOWN")
        # Fail-closed default: nothing that is not a confirmed drift may map to
        # a code that a host would read as "clean".
        self.assertEqual(_drift.error_code({"kind": _drift.DRIFT_NONE}), "REMOTE_UNKNOWN")

    def test_error_details_are_additive(self):
        plain = Error("POLICY_DENIED", "no").to_dict()
        self.assertEqual(plain, {"code": "POLICY_DENIED", "message": "no"})
        detailed = Error("REMOTE_DRIFT", "drift", details={"drift_kind": "base_advanced"}).to_dict()
        self.assertEqual(detailed["details"]["drift_kind"], "base_advanced")


class TestAdapterFixtures(unittest.TestCase):
    """§10.4/§18: adapters ship versioned fixtures, and synthetic never pretends real."""

    directory = ROOT / "tests" / "fixtures" / "agents"
    required_cases = {"normal", "auth_failure", "rate_limited", "permission_denied", "truncated"}

    def files(self):
        return sorted(self.directory.glob("*.json"))

    def test_fixtures_cover_every_released_adapter(self):
        covered = {json.loads(path.read_text())["adapter_id"] for path in self.files()}
        expected = {
            entry.adapter_id
            for entry in registry.entries()
            if getattr(entry.adapter, "facts_status", "VERIFIED_FORM") != "FACT_PENDING"
        }
        self.assertEqual(covered, expected)

    def test_fact_pending_adapter_has_no_fixture(self):
        for path in self.files():
            record = json.loads(path.read_text())
            entry = registry.require_entry(record["adapter_id"])
            self.assertNotEqual(
                getattr(entry.adapter, "facts_status", ""),
                "FACT_PENDING",
                f"{path.name} must not ship fixtures while its facts are pending",
            )

    def test_each_fixture_declares_provenance_and_the_five_classes(self):
        for path in self.files():
            record = json.loads(path.read_text())
            self.assertIn("source", record, path.name)
            self.assertIn(record["source"], ("real-e2e", "synthetic-from-documented-protocol"))
            self.assertIn("cli_version", record, path.name)
            self.assertTrue(record.get("note"), path.name)
            names = {case["name"] for case in record["cases"]}
            self.assertTrue(
                self.required_cases <= names,
                f"{path.name} is missing {self.required_cases - names}",
            )

    def test_fixtures_are_actually_parsed_by_their_adapter(self):
        for path in self.files():
            record = json.loads(path.read_text())
            entry = registry.require_entry(record["adapter_id"])
            adapter = entry.factory()
            self.assertIn(record["adapter_backend"], adapter.supported_backends, path.name)
            for case in record["cases"]:
                capture = ProcessCapture(
                    adapter_id=record["adapter_id"],
                    adapter_backend=record["adapter_backend"],
                    exit_code=case.get("exit_code", 0),
                    raw_text=case["raw"],
                    truncated=bool(case.get("truncated")),
                )
                with self.subTest(file=path.name, case=case["name"]):
                    expectation = case["expect"]
                    if "error" in expectation:
                        with self.assertRaises(Exception) as ctx:
                            adapter.parse_output(capture)
                        self.assertEqual(getattr(ctx.exception, "code", None), expectation["error"])
                        continue
                    result = adapter.parse_output(capture)
                    self.assertEqual(result.outcome, expectation["outcome"])
                    if "session_id" in expectation:
                        self.assertEqual(result.session_id, expectation["session_id"])


class TestCompatibilityRecords(unittest.TestCase):
    """§10.4: records, docs and the registry must agree, or the build fails."""

    directory = ROOT / "docs" / "compatibility"

    def records(self):
        found = []
        for path in sorted(self.directory.glob("*.json")):
            schema = load_schema("compat-evidence.v1.json")
            errors = validate(json.loads(path.read_text()), schema, str(path.name))
            self.assertEqual(errors, [], errors)
            for record in json.loads(path.read_text())["records"]:
                found.append((path, record))
        return found

    def test_every_record_matches_the_registry(self):
        for path, record in self.records():
            entry = registry.require_entry(record["adapter_id"])
            self.assertIn(
                record["backend"],
                entry.adapter.supported_backends,
                "{}: {!r} is not a registered backend".format(path.name, record["backend"]),
            )

    def test_docs_mention_every_recorded_pair(self):
        document = (ROOT / "docs" / "capabilities.md").read_text()
        for path, record in self.records():
            self.assertIn("`{}`".format(record["adapter_id"]), document, path.name)
            self.assertIn("`{}`".format(record["backend"]), document, path.name)

    def test_a_real_e2e_record_must_cite_evidence(self):
        for path, record in self.records():
            if record["source"] == "real_e2e":
                self.assertTrue(
                    record.get("evidence_ref"), f"{path.name} claims real_e2e without evidence_ref"
                )

    def test_fact_pending_records_never_promote_the_adapter(self):
        """A record may exist; the adapter still stays IMPLEMENTED (§10.4)."""
        for _, record in self.records():
            entry = registry.require_entry(record["adapter_id"])
            if getattr(entry.adapter, "facts_status", "VERIFIED_FORM") != "FACT_PENDING":
                continue
            verification = registry.Verification.from_dict(record)
            state, reason, promoted = entry.capability(
                record["backend"], record["version"], verification
            )
            self.assertEqual(state, "IMPLEMENTED")
            self.assertIsNone(promoted)
            self.assertIn("FACT_PENDING", reason)

    def test_no_shipped_record_is_described_as_verified_in_the_docs(self):
        """With zero records, the capability table must keep saying NOT_RUN."""
        document = (ROOT / "docs" / "capabilities.md").read_text()
        if self.records():
            self.skipTest("records exist; the per-pair tests above apply")
        for entry in registry.entries():
            for backend in entry.adapter.supported_backends:
                self.assertEqual(entry.capability(backend, "1.0.0")[0], "IMPLEMENTED")
        self.assertIn("NOT_RUN", document)


@unittest.skipUnless(SKILL, SKIP_REASON)
class TestSkillTree(unittest.TestCase):
    """The skill repository is separate; these guard its boundary and integrity."""

    def setUp(self):
        self.build = load_build_module()

    def test_skill_tree_is_shippable(self):
        self.assertEqual(self.build.check(), [], "run `python3 build.py --check`")

    def test_whitelist_excludes_runner_and_scaffolding(self):
        files = self.build.skill_files()
        self.assertIn("SKILL.md", files)
        self.assertIn("runtime-requirements.json", files)
        self.assertIn("scripts/zoe.sh", files)
        for relative in files:
            self.assertFalse(
                relative.startswith(("src/", "tests/", "docs/", "schemas/")),
                f"runner code leaked into the skill package: {relative}",
            )
            self.assertNotIn(".pyc", relative)
            self.assertFalse(relative.endswith(".py"), f"python file in the package: {relative}")

    def test_whitelist_covers_every_required_file(self):
        files = set(self.build.skill_files())
        for relative in self.build.REQUIRED_FILES:
            self.assertIn(relative, files)

    def test_no_link_points_to_a_missing_reference(self):
        skill = (SKILL / "SKILL.md").read_text()
        for link in re.findall(r"\]\((references/[^)]+)\)", skill):
            self.assertTrue((SKILL / link).is_file(), f"SKILL.md links to missing {link}")

    def test_frontmatter_declares_the_distributed_name(self):
        frontmatter = (SKILL / "SKILL.md").read_text().split("---\n")[1]
        name = re.search(r"^name:\s*(\S+)", frontmatter, re.M).group(1)
        self.assertEqual(name, "zoe-workflows")
        # The description must not re-sell the runner's responsibilities (§2.6).
        self.assertNotIn("控制与证据层", frontmatter)

    def test_requirements_file_satisfies_this_runtime(self):
        """The shipped declaration must pass the parser it is written for."""
        result = _requirements.check(SKILL / "runtime-requirements.json")
        self.assertTrue(result["ok"])
        self.assertEqual(result["requirements"]["api_version"], 2)
        self.assertEqual(result["requirements"]["protocol_revision"], 1)

    def test_wrapper_is_executable(self):
        wrapper = SKILL / "scripts" / "zoe.sh"
        self.assertTrue(wrapper.is_file())
        self.assertTrue(wrapper.stat().st_mode & 0o111, "scripts/zoe.sh must be executable")

    def test_shell_wrapper_enforces_the_gate(self):
        """The gate must be inside the call path, not a documented prerequisite."""
        script = (SKILL / "scripts" / "zoe.sh").read_text()
        for token in (
            "ZOE_BIN",
            "ZOE_SRC",
            "command -v zoe",
            "pipx install zoe-runtime",
            "capabilities --check-requirements",
            "runtime-requirements.json",
            "RUNTIME_INCOMPATIBLE",
            "--which",
        ):
            self.assertIn(token, script)

    def test_windows_wrappers_run_the_same_check(self):
        cmd = (SKILL / "scripts" / "zoe.cmd").read_text()
        for token in (
            "capabilities --check-requirements",
            "RUNTIME_INCOMPATIBLE",
            "runtime-requirements.json",
        ):
            self.assertIn(token, cmd)
        ps1 = (SKILL / "scripts" / "zoe.ps1").read_text()
        self.assertIn("zoe.cmd", ps1)
        self.assertIn("$LASTEXITCODE", ps1)

    def test_windows_monitor_keeps_the_same_protocol(self):
        """B2：Windows 是**宿主侧实现**，不是"照抄一段 sh"。

        两个实现可以换语言，但不能换判断：同一组退出码、同样的游标推进方式
        （`watch --after` + `next_cursor`）、同样的权威状态来源（`task status`）。
        少了这一条，Windows 宿主只能"看屏幕"或把窗口到期当完成。
        """
        bash = (SKILL / "scripts" / "zoe-monitor.sh").read_text()
        ps1 = (SKILL / "scripts" / "zoe-monitor.ps1").read_text()
        # 退出码是**协议**，写法可以不同（.sh 是 `exit 10`，.ps1 走 `Exit-Monitor -Code 10`
        # 以便把结束原因一起写出去）。断言的是同一组数字，不是同一行源码。
        for code in ("10", "11"):
            with self.subTest(code=code):
                self.assertIn(f"exit {code}", bash)
                self.assertRegex(ps1, r"-Code\s+%s\b" % code)
        for token in ("--after", "--max-events", "next_cursor", "--jsonl", "task"):
            with self.subTest(token=token):
                self.assertIn(token, ps1)
        # 事件解析不能要求 Windows 先装 jq（注释里提到 jq 是说明，不算调用）。
        self.assertNotIn("| jq", ps1)
        self.assertIn("ConvertFrom-Json", ps1)

    def test_both_monitors_share_the_progress_and_stop_protocol(self):
        """v1.5.2：可读进展与结束原因必须两边同语义，否则 Windows 宿主只能看屏幕。"""
        bash = (SKILL / "scripts" / "zoe-monitor.sh").read_text()
        ps1 = (SKILL / "scripts" / "zoe-monitor.ps1").read_text()
        for token in (
            "monitor_stop_reason",
            "[stop] reason=",
            "ALL_TERMINAL",
            "BUDGET_EXHAUSTED",
            "HUMAN_REQUIRED",
            "WATCH_ERROR",
            "DEPENDENCY_ERROR",
            "phases=",
            "runs=",
            # v1.5.3：零候选必须两边都报，否则 Windows 宿主会把"没改动"当"等验收"。
            "empty_diff=true",
            "empty_candidates",
        ):
            with self.subTest(token=token):
                self.assertIn(token, bash)
                self.assertIn(token, ps1)
        for token in ("[log]", "[log-error]", "[log-off]", "--after", "reset_cursor"):
            with self.subTest(token=token):
                self.assertIn(token, bash)
                self.assertIn(token, ps1)


if __name__ == "__main__":
    unittest.main()
