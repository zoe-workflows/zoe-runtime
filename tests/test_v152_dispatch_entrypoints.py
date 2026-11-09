"""v1.5.2 两段式入口（技能侧 S2 / S7）的契约用例。

对应 ``zoe-workflows/docs/2026-11-04-batch-parallel-dispatch-friction.md`` §5.1 / §5.2。
两个脚本都是**薄入口**（不自实现状态校验/错误码映射/授权逻辑），所以断言只针对
它们真正承担的三件事：

1. 在**生成/提交之前**拦住前置条件不满足的情形，并且不留下"看起来能提交"的文件；
2. 提交的是**同一个文件**（不是重建的请求），因此重放是幂等的；
3. 预览与写入之间的漂移必须被拒绝，而不是被"自动修正"成一次没人看过的授权。
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

SKILL = skill_dir()
RESUME = SKILL / "scripts" / "zoe-resume.sh" if SKILL else None
ATTEST = SKILL / "scripts" / "zoe-attest.sh" if SKILL else None

REGISTRY = []

FAKE_ZOE = """#!/bin/sh
# 最小 zoe 替身：记录每次调用，并按环境变量返回预设结果。
echo "$*" >>"$FAKE_LOG"
case "$1 $2" in
  "task status")
    printf '%s\\n' "$FAKE_STATUS"
    exit 0
    ;;
  "task resume")
    # 把**被提交的文件**原样留一份：断言"重放提交的是同一份字节"才有意义。
    prev=""
    for arg in "$@"; do
      [ "$prev" = "--request-file" ] && cp "$arg" "$FAKE_ECHO" 2>/dev/null
      prev=$arg
    done
    printf '%s\\n' "$FAKE_RESUME_OUT"
    exit "${FAKE_RESUME_EXIT:-0}"
    ;;
  "project attest")
    COUNTER="$FAKE_LOG.counter"
    n=$(cat "$COUNTER" 2>/dev/null || echo 0)
    n=$((n + 1))
    echo "$n" >"$COUNTER"
    # 运行时按 `--profiles` 收窄候选，`execution[]` 也随之收窄：替身必须同构，
    # 否则"收窄"与"漂移"在测试里无法区分。
    profs=""
    prev=""
    for arg in "$@"; do
      [ "$prev" = "--profiles" ] && profs=$arg
      prev=$arg
    done
    case " $* " in
      *" --dry-run "*)
        if [ "$n" -ge 2 ] && [ -n "$FAKE_DRY_JSON_2" ]; then
          doc=$FAKE_DRY_JSON_2
        else
          doc=$FAKE_DRY_JSON
        fi
        if [ -n "$profs" ]; then
          doc=$(printf '%s' "$doc" | jq -c --arg p "$profs" '
            ($p | split(",")) as $want
            | .execution |= map(select(.profile_id as $x | $want | index($x)))
            | .confirmed_profiles |= map(select(. as $x | $want | index($x)))')
        fi
        printf '%s\\n' "$doc"
        exit 0
        ;;
      *)
        printf '%s\\n' "$FAKE_WRITE_JSON"
        exit "${FAKE_WRITE_EXIT:-0}"
        ;;
    esac
    ;;
esac
printf '{"error": {"code": "UNEXPECTED", "message": "fake zoe got: %s"}}\\n' "$*"
exit 2
"""

STATUS_OK = json.dumps(
    {
        "task_id": "T1",
        "project_id": "P1",
        "state": "BLOCKED",
        "block_code": "RUN_TIMEOUT",
        "row_version": 7,
        "config": {"authorization_ref": "scope-1"},
    }
)

DRY_OK = json.dumps(
    {
        "api_version": 2,
        "project_id": "P1",
        "dry_run": True,
        "policy_hash": "HASH-A",
        "policy_revision": "HASH-A",
        "scope": "project",
        "confirmed_profiles": ["a"],
        "execution": [
            {
                "profile_id": "a",
                "adapter_id": "codebuddy",
                "backend": "codebuddy-cli",
                "model": "m1",
                "permission_profile": "isolated-edit",
                "cost_class": "standard",
                "model_unresolved": False,
            }
        ],
        "previous": None,
        "wrote": False,
    }
)

DRY_AB = json.dumps(
    {
        "api_version": 2,
        "project_id": "P1",
        "dry_run": True,
        "policy_hash": "HASH-A",
        "policy_revision": "HASH-A",
        "scope": "project",
        "confirmed_profiles": ["a", "b"],
        "execution": [
            {
                "profile_id": "a",
                "adapter_id": "codebuddy",
                "backend": "codebuddy-cli",
                "model": "m1",
                "permission_profile": "isolated-edit",
                "cost_class": "standard",
                "model_unresolved": False,
            },
            {
                "profile_id": "b",
                "adapter_id": "codebuddy",
                "backend": "codebuddy-cli",
                "model": "m1",
                "permission_profile": "isolated-edit",
                "cost_class": "standard",
                "model_unresolved": False,
            },
        ],
        "previous": None,
        "wrote": False,
    }
)

WRITE_OK = json.dumps(
    {
        "api_version": 2,
        "project_id": "P1",
        "dry_run": False,
        "policy_hash": "HASH-A",
        "policy_revision": "HASH-A",
        "scope": "project",
        "confirmed_profiles": ["a"],
        "execution": [],
        "previous": None,
        "wrote": True,
        "path": "/tmp/attest.json",
    }
)


@unittest.skipUnless(SKILL, SKIP_REASON)
@unittest.skipUnless(shutil.which("bash"), "bash is required")
@unittest.skipUnless(shutil.which("jq"), "these entry points depend on jq")
class FakeZoe(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.log = self.root / "invocations.log"
        self.zoe = self.root / "zoe"
        self.zoe.write_text(FAKE_ZOE)
        self.zoe.chmod(0o755)
        self.echo = self.root / "submitted.json"

    def env(self, **overrides):
        env = dict(os.environ)
        env.update(
            {
                "ZOE_BIN": str(self.zoe),
                "FAKE_LOG": str(self.log),
                "FAKE_STATUS": STATUS_OK,
                "FAKE_RESUME_OUT": json.dumps(
                    {"task_id": "T1", "state": "READY", "row_version": 8}
                ),
                "FAKE_RESUME_EXIT": "0",
                "FAKE_LAST_FILE": str(self.echo),
                "FAKE_ECHO": str(self.echo),
                "FAKE_DRY_JSON": DRY_OK,
                "FAKE_DRY_JSON_2": "",
                "FAKE_WRITE_JSON": WRITE_OK,
                "FAKE_WRITE_EXIT": "0",
            }
        )
        env.update({key: str(value) for key, value in overrides.items()})
        return env

    def run_script(self, script, *args, **env):
        return subprocess.run(
            ["bash", str(script), *args],
            capture_output=True,
            text=True,
            env=self.env(**env),
            cwd=str(self.root),
        )

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


@unittest.skipUnless(SKILL, SKIP_REASON)
class TestPackagingGate(unittest.TestCase):
    """§5.4：两个新脚本必须进 `REQUIRED_FILES`，且带可执行位。

    `build.py --check` 只审计 `REQUIRED_FILES` 里**已列**的文件；把它从清单里删掉，
    检查仍然通过。所以"发布了"这件事需要单独一条断言，否则脚本会在某次重构里
    静默从分发包消失，而所有门禁都是绿的。
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util

        spec = importlib.util.spec_from_file_location("skill_build_v152", SKILL / "build.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.build = module

    def test_both_entry_points_are_required_files(self):
        for relative in ("scripts/zoe-resume.sh", "scripts/zoe-attest.sh"):
            with self.subTest(script=relative):
                self.assertIn(relative, self.build.REQUIRED_FILES)

    def test_both_entry_points_are_executable(self):
        for relative in ("scripts/zoe-resume.sh", "scripts/zoe-attest.sh"):
            with self.subTest(script=relative):
                path = SKILL / relative
                self.assertTrue(path.is_file())
                self.assertTrue(path.stat().st_mode & 0o111, f"{relative} must be executable")

    def test_the_skill_tree_passes_its_own_check(self):
        self.assertEqual(self.build.check(), [])


class TestResumePreview(FakeZoe):
    def preview(self, out, **env):
        return self.run_script(
            RESUME, "preview", "--project", "P1", "--task", "T1", "--out", str(out), **env
        )

    def test_expected_version_comes_from_the_refreshed_status(self):
        out = self.root / "requests"
        result = self.preview(out)
        self.assertEqual(result.returncode, 0, result.stderr)
        files = list(out.glob("*.json"))
        self.assertEqual(len(files), 1)
        request = json.loads(files[0].read_text())
        self.assertEqual(request["expected_version"], 7)
        self.assertEqual(request["authorization_ref"], "scope-1")
        self.assertEqual(request["authorized_actions"], ["execute"])
        self.assertEqual(request["task_id"], "T1")
        self.assertIn("--refresh", self.calls()[0])

    def test_input_required_is_refused_before_a_file_exists(self):
        out = self.root / "requests"
        status = dict(json.loads(STATUS_OK), block_code="INPUT_REQUIRED")
        result = self.preview(out, FAKE_STATUS=json.dumps(status))
        self.assertEqual(result.returncode, 3)
        self.assertIn("INPUT_REQUIRED", result.stderr)
        self.assertEqual(list(out.glob("*.json")), [])

    def test_process_unknown_is_refused_before_a_file_exists(self):
        out = self.root / "requests"
        status = dict(json.loads(STATUS_OK), block_code="PROCESS_UNKNOWN")
        result = self.preview(out, FAKE_STATUS=json.dumps(status))
        self.assertEqual(result.returncode, 3)
        self.assertIn("PROCESS_UNKNOWN", result.stderr)
        self.assertEqual(list(out.glob("*.json")), [])

    def test_non_resumable_state_is_refused(self):
        out = self.root / "requests"
        status = dict(json.loads(STATUS_OK), state="REVIEW_READY", block_code=None)
        result = self.preview(out, FAKE_STATUS=json.dumps(status))
        self.assertEqual(result.returncode, 3)
        self.assertEqual(list(out.glob("*.json")), [])

    def test_preview_does_not_touch_logs_or_submit(self):
        out = self.root / "requests"
        self.preview(out)
        verbs = [line.split()[:2] for line in self.calls()]
        self.assertEqual(verbs, [["task", "status"]])


class TestResumeApply(FakeZoe):
    def make_request(self):
        out = self.root / "requests"
        result = self.run_script(
            RESUME, "preview", "--project", "P1", "--task", "T1", "--out", str(out)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return next(out.glob("*.json"))

    def test_replay_submits_the_same_bytes(self):
        request = self.make_request()
        first = self.run_script(RESUME, "apply", "--request-file", str(request))
        self.assertEqual(first.returncode, 0, first.stderr)
        submitted = self.echo.read_text()
        second = self.run_script(RESUME, "apply", "--request-file", str(request))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.echo.read_text(), submitted)
        # 同一个 request_id + 同一份载荷 = 幂等重放，不是新意图。
        self.assertEqual(
            json.loads(submitted)["request_id"], json.loads(request.read_text())["request_id"]
        )

    def test_state_conflict_is_distinguishable_and_asks_for_a_new_preview(self):
        request = self.make_request()
        result = self.run_script(
            RESUME,
            "apply",
            "--request-file",
            str(request),
            FAKE_RESUME_EXIT="3",
            FAKE_RESUME_OUT=json.dumps(
                {"error": {"code": "STATE_CONFLICT", "message": "expected_version is stale"}}
            ),
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("STATE_CONFLICT", result.stderr)
        self.assertIn("preview", result.stderr)

    def test_a_file_without_task_id_is_refused(self):
        bogus = self.root / "bogus.json"
        bogus.write_text(json.dumps({"schema_version": 1}))
        result = self.run_script(RESUME, "apply", "--request-file", str(bogus))
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.calls(), [])

    def test_a_hand_edited_expected_version_is_refused(self):
        """手改 `expected_version` 是最危险的一处改动：它绕过"版本已前进"的保护。"""
        request = self.make_request()
        document = json.loads(request.read_text())
        self.assertTrue(document["self_sha256"])
        document["expected_version"] = 3
        request.write_text(json.dumps(document, indent=2, sort_keys=True))
        result = self.run_script(RESUME, "apply", "--request-file", str(request))
        self.assertEqual(result.returncode, 3)
        self.assertIn("self_sha256", result.stderr)
        self.assertFalse(any("task resume" in line for line in self.calls()))


class TestAttestPreview(FakeZoe):
    def preview(self, **env):
        out = self.root / "attest.json"
        result = self.run_script(
            ATTEST, "preview", "--project", "P1", "--profiles", "a", "--out", str(out), **env
        )
        return result, out

    def test_preview_records_a_self_checksum_and_does_not_write(self):
        result, out = self.preview()
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(out.read_text())
        self.assertEqual(document["kind"], "zoe-attest-preview")
        self.assertEqual(document["policy_hash"], "HASH-A")
        self.assertEqual(document["requested_profiles"], ["a"])
        self.assertTrue(document["self_sha256"])
        self.assertIn("execution", document)
        self.assertTrue(all("--dry-run" in line for line in self.calls()))


class TestAttestConfirm(FakeZoe):
    def preview(self):
        out = self.root / "attest.json"
        result = self.run_script(
            ATTEST, "preview", "--project", "P1", "--profiles", "a", "--out", str(out)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return out

    def confirm(self, preview, *args, **env):
        return self.run_script(ATTEST, "confirm", "--preview", str(preview), *args, **env)

    def test_a_tampered_preview_is_refused(self):
        preview = self.preview()
        document = json.loads(preview.read_text())
        document["execution"][0]["model"] = "something-else"
        preview.write_text(json.dumps(document, indent=2, sort_keys=True))
        result = self.confirm(preview, "--policy-hash", "HASH-A")
        self.assertEqual(result.returncode, 3)
        self.assertIn("self_sha256", result.stderr)
        self.assertFalse(any("--expect-policy-hash" in line for line in self.calls()))

    def test_the_hash_is_mandatory(self):
        preview = self.preview()
        result = self.confirm(preview)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--policy-hash", result.stderr)

    def test_a_wrong_hash_is_refused(self):
        preview = self.preview()
        result = self.confirm(preview, "--policy-hash", "HASH-B")
        self.assertEqual(result.returncode, 3)
        self.assertFalse(any("--expect-policy-hash" in line for line in self.calls()))

    def test_narrowing_the_profile_set_is_allowed(self):
        """收窄是**允许**的动作：`execution[]` 会随之变短，不能被读成"配置漂移"。"""
        preview = self.root / "attest-ab.json"
        made = self.run_script(
            ATTEST,
            "preview",
            "--project",
            "P1",
            "--profiles",
            "a,b",
            "--out",
            str(preview),
            FAKE_DRY_JSON=DRY_AB,
        )
        self.assertEqual(made.returncode, 0, made.stderr)
        result = self.confirm(
            preview,
            "--policy-hash",
            "HASH-A",
            "--confirm-profiles",
            "a",
            FAKE_DRY_JSON=DRY_AB,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        write = [line for line in self.calls() if "--expect-policy-hash" in line]
        self.assertEqual(len(write), 1)
        self.assertIn("--profiles a ", write[0] + " ")
        self.assertNotIn("a,b", write[0])

    def test_widening_the_profile_set_is_refused(self):
        preview = self.preview()
        result = self.confirm(
            preview, "--policy-hash", "HASH-A", "--confirm-profiles", "a,b"
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("扩张", result.stderr)
        self.assertFalse(any("--expect-policy-hash" in line for line in self.calls()))

    def test_drift_between_preview_and_write_is_refused(self):
        preview = self.preview()
        drifted = json.loads(DRY_OK)
        drifted["policy_hash"] = "HASH-DRIFTED"
        # 第一次 dry-run 由 preview 消费，confirm 的复核拿到漂移后的摘要。
        result = self.confirm(
            preview, "--policy-hash", "HASH-A", FAKE_DRY_JSON_2=json.dumps(drifted)
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("重新 preview", result.stderr)
        self.assertFalse(any("--expect-policy-hash" in line for line in self.calls()))

    def test_success_passes_the_preview_hash_back_to_the_runtime(self):
        preview = self.preview()
        result = self.confirm(preview, "--policy-hash", "HASH-A")
        self.assertEqual(result.returncode, 0, result.stderr)
        write = [line for line in self.calls() if "--expect-policy-hash" in line]
        self.assertEqual(len(write), 1)
        self.assertIn("--expect-policy-hash HASH-A", write[0])
        self.assertIn("--profiles a", write[0])
        self.assertIn("[confirm]", result.stdout)

    def test_an_inconsistent_write_response_is_not_reported_as_confirmed(self):
        preview = self.preview()
        mismatched = json.loads(WRITE_OK)
        mismatched["policy_hash"] = "HASH-OTHER"
        result = self.confirm(
            preview, "--policy-hash", "HASH-A", FAKE_WRITE_JSON=json.dumps(mismatched)
        )
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("[confirm]", result.stdout)


if __name__ == "__main__":
    unittest.main()
