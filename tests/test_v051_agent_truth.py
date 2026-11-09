"""回归测试：让"跑过了"和"真的干成了"不再被同一句话掩盖（v0.5.1 修复批）。

每一条都对应一次真实的失败现场：

1. codebuddy 无人值守时没有权限开关 → 写入类工具必被拒，任务不可能完成；
2. codebuddy 找不到登录态时 **exit 0 + 一行纯文本**，退出码不可信；
3. ``run_pipeline`` 的 finally 用 PROCESS_UNKNOWN 顶替真实失败码；
4. 进程组观测依赖外部 ``ps``，失败一次就把每次 run 判成"未知";
5. ``doctor`` 只测进程身份，不测进程组观测，于是"自检通过"是假阳性；
6. profile 的 ``expected_version`` 从未和启动环境里测到的版本比对。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

from src import _discovery, _pipeline, _profiles
from src._agents import base, registry
from src._agents.codebuddy import CodeBuddyAdapter
from src._platform import get_platform
from src._platform.base import ProcessHandle
from src._util import Error


def capture(raw, exit_code=0):
    return base.ProcessCapture(
        adapter_id="codebuddy",
        adapter_backend="codebuddy-cli",
        exit_code=exit_code,
        raw_text=raw,
    )


class UnboundCodeBuddy(CodeBuddyAdapter):
    """声称有权限开关、却没有任何绑定的适配器（回归用）。"""

    permission_bindings = {}


def binding(adapter, permission=None):
    return _profiles.ProfileBinding(
        profile_id="p1",
        profile={"adapter": adapter.id, "permission_profile": "isolated-edit"},
        adapter=adapter,
        entry=registry.AdapterEntry(adapter.id, "builtin", 1, type(adapter)),
        backend=adapter.default_backend,
        capability=registry.CAPABILITY_VERIFIED,
        capability_reason=None,
        verification=None,
        profile_hash="0" * 64,
        env_names=(),
        options={},
        permission=permission
        if permission is not None
        else adapter.permission_state("isolated-edit"),
    )


class TestCodeBuddyPermissionBinding(unittest.TestCase):
    """1. 没有审批通道 = 结构性不可用，必须体现在 argv 上。"""

    def test_isolated_edit_reaches_the_command_line(self):
        spec = CodeBuddyAdapter().build_launch(_context())
        self.assertEqual(spec.extra["permission"]["enforced"], True)
        self.assertIn("--permission-mode", spec.argv)
        self.assertIn("acceptEdits", spec.argv)

    def test_allow_everything_is_never_the_answer(self):
        # 设计 §10.2 Q3：答案不能是"全放行"。--dangerously-skip-permissions/-y
        # 会连同 Bash 一起放开，等于用隔离性换可用性。
        argv = CodeBuddyAdapter().build_launch(_context()).argv
        self.assertNotIn("-y", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("bypassPermissions", argv)

    def test_isolated_edit_and_run_can_execute_commands(self):
        # worker-prompt 要求"执行批准的自测命令"：只有 default + 白名单能让
        # Bash 真的跑起来（acceptEdits/dontAsk/auto 下 allowedTools 对 Bash 无效）。
        spec = CodeBuddyAdapter().build_launch(_context(permission_profile="isolated-edit-and-run"))
        argv = spec.argv
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")
        self.assertIn("--allowedTools", argv)
        self.assertIn("Bash", argv)
        self.assertIn("Write", argv)
        self.assertTrue(spec.extra["permission"]["enforced"])
        self.assertNotIn("-y", argv)
        self.assertNotIn("bypassPermissions", argv)

    def test_approved_tools_are_separate_argv_tokens(self):
        # `--allowedTools` 是 nargs="+"：逗号串会被当成一个工具名，一个都不匹配。
        spec = CodeBuddyAdapter().build_launch(
            _context(options={"approved_tools": ["Read", "Edit"]})
        )
        i = spec.argv.index("--allowedTools")
        self.assertEqual(list(spec.argv[i + 1 : i + 3]), ["Read", "Edit"])
        self.assertNotIn("Read,Edit", spec.argv)
        self.assertTrue(spec.extra["permission"]["enforced"])

    def test_approved_tools_replace_the_profile_default_not_duplicate(self):
        # 同一 flag 传两次时后者静默胜出，覆盖会让"显式指定"变成谎话。
        spec = CodeBuddyAdapter().build_launch(
            _context(
                permission_profile="isolated-edit-and-run",
                options={"approved_tools": ["Read", "Edit"]},
            )
        )
        self.assertEqual(list(spec.argv).count("--allowedTools"), 1)
        i = spec.argv.index("--allowedTools")
        self.assertEqual(list(spec.argv[i + 1 : i + 3]), ["Read", "Edit"])
        # 档位里的 Bash 必须随默认值一起被替换掉，否则它仍会被放行。
        self.assertNotIn("Bash", spec.argv)

    def test_adapter_that_claims_a_switch_must_bind_it(self):
        with self.assertRaises(Error) as ctx:
            _profiles.require_runnable(binding(UnboundCodeBuddy()))
        self.assertEqual(ctx.exception.code, "PERMISSION_PROFILE_UNBOUND")

    def test_adapter_without_a_switch_still_runs(self):
        # opencode 没有这个开关：enforced=False 是诚实的说法，不是拒绝理由。
        from src._agents.opencode import OpenCodeAdapter

        self.assertIsNotNone(_profiles.require_runnable(binding(OpenCodeAdapter())))


class TestCodeBuddyPlainTextFailure(unittest.TestCase):
    """2. 非 JSON 捕获必须被分类，不能当成"输出格式错误"。"""

    AUTH = "Authentication required. Please use /login command to sign in to your account"

    def test_auth_text_is_classified_as_auth_error(self):
        result = CodeBuddyAdapter().parse_output(capture(self.AUTH))
        self.assertEqual(result.outcome, "auth_error")
        with self.assertRaises(Error) as ctx:
            result.require_completed()
        self.assertEqual(ctx.exception.code, "AGENT_AUTH_REQUIRED")

    def test_empty_capture_never_becomes_success(self):
        result = CodeBuddyAdapter().parse_output(capture(""))
        self.assertNotEqual(result.outcome, "completed")

    def test_json_array_terminal_is_found_by_structure(self):
        raw = (
            '[{"type":"message","role":"user","content":[]},'
            '{"type":"result","subtype":"success","is_error":false,"result":"done"}]'
        )
        result = CodeBuddyAdapter().parse_output(capture(raw))
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.summary, "done")

    def test_stream_json_jsonl_is_parsed(self):
        # stream-json 的真实形态：逐行 JSON 对象，末行是 result。
        raw = "\n".join(
            [
                '{"type":"system","subtype":"init","session_id":"s-1"}',
                '{"type":"file-history-snapshot","messageId":"m1"}',
                '{"type":"assistant","message":{"role":"assistant","content":[]}}',
                '{"type":"user","message":{"role":"user","content":[]}}',
                '{"type":"result","subtype":"success","is_error":false,'
                '"result":"Done: three files created"}',
            ]
        )
        result = CodeBuddyAdapter().parse_output(capture(raw))
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.summary, "Done: three files created")

    def test_output_format_streams_so_progress_is_observable(self):
        # json 模式在退出前一个字节都不写（实测 60s 恒为 0），宿主的
        # `task logs` 因此在整个 implement 阶段看不到任何东西。
        argv = CodeBuddyAdapter().build_launch(_context()).argv
        self.assertIn("--output-format", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")

    def test_array_without_terminal_object_is_still_invalid(self):
        with self.assertRaises(Error) as ctx:
            CodeBuddyAdapter().parse_output(
                capture('[{"type":"message","role":"user","content":[]}]')
            )
        self.assertEqual(ctx.exception.code, "AGENT_RESULT_INVALID")


class TestProcessGroupObservation(unittest.TestCase):
    """4/5. 进程组观测必须不依赖外部 ps，且自检必须真的测它。"""

    def setUp(self):
        self.plat = get_platform()

    def test_group_is_observed_after_leader_exits(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        handle = ProcessHandle(
            pid=proc.pid,
            start_token=None,
            group_token=str(proc.pid),
            popen=proc,
            platform=self.plat,
        )
        try:
            self.assertIs(self.plat.tree_alive(handle), True)
        finally:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.plat.tree_alive(handle) is False:
                break
            time.sleep(0.05)
        self.assertIs(self.plat.tree_alive(handle), False)

    def test_tree_remaining_returns_zero_for_reaped_group(self):
        # 纯 syscall 实现：被回收的组必须报 0，而不是"无法判定"。
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        proc.wait(timeout=10)
        deadline = time.monotonic() + 5
        remaining = -1
        while time.monotonic() < deadline:
            remaining = self.plat._tree_remaining(proc.pid)
            if remaining == 0:
                break
            time.sleep(0.05)
        self.assertEqual(remaining, 0)

    def test_doctor_tree_check_is_not_a_false_positive(self):
        from src import _config

        self.assertTrue(_config._platform_tree_ok())

    def test_doctor_tree_check_fails_when_observation_breaks(self):
        # 观测能力真的坏掉时自检必须说 False，否则 ready=true 又成了空话。
        from unittest import mock

        from src import _config
        from src._platform import posix as posix_platform

        with mock.patch.object(posix_platform.PosixPlatform, "_tree_remaining", return_value=-1):
            self.assertFalse(_config._platform_tree_ok())

    def test_process_identity_does_not_shell_out(self):
        # 身份查询是 PID 复用保护的基础：本机 ps 被禁用时也必须能回答。
        from unittest import mock

        from src._platform import posix as posix_platform

        with mock.patch.object(posix_platform, "command", side_effect=AssertionError("ps used")):
            identity = self.plat.process_identity(os.getpid())
        self.assertTrue(identity)
        self.assertTrue(self.plat.alive(os.getpid(), identity))
        self.assertFalse(self.plat.alive(os.getpid(), "not-the-identity"))


class TestFailureCodeIsNotOverwritten(unittest.TestCase):
    """3. PROCESS_UNKNOWN 描述"不可观测"，不得顶替业务失败原因。"""

    def test_pre_launch_stage_is_not_uncertain(self):
        # create_requested 只说明"打算启动"：此时没有进程树可观测。
        for stage in (None, "create_requested", "start_requested"):
            self.assertFalse(_pipeline._run_uncertain({"launch_stage": stage}, None))

    def test_started_without_group_token_is_left_to_dispatch_state(self):
        self.assertFalse(
            _pipeline._run_uncertain(
                {"launch_stage": "started", "dispatch_state": "acknowledged"}, None
            )
        )


class TestPinLauncherMeasuresVersion(unittest.TestCase):
    """6. expected_version 必须和启动环境里测到的版本比对。"""

    def test_parse_version_is_optional_and_does_not_raise(self):
        launch = base.LaunchSpec(argv=("definitely-not-a-real-cli-zoe", "-p"))
        pinned = _discovery.pin_launcher(launch, "local-process", parse_version=lambda t: None)
        # 解析不到就如实留空，不编造版本。
        self.assertEqual(pinned.extra["resolution"]["path"], None)


def _context(**overrides):
    values = dict(
        task_id="task-1",
        run_id="run-1",
        adapter_id="codebuddy",
        adapter_backend="codebuddy-cli",
        workspace="/work",
        prompt_file="/state/prompt.txt",
        prompt_hash="0" * 64,
        profile_id="p1",
        profile_hash="0" * 64,
        spec_revision=1,
        spec_hash="1" * 64,
        expected_version="2.111.0",
        permission_profile="isolated-edit",
    )
    values.update(overrides)
    return base.RunContext(**values)


if __name__ == "__main__":
    unittest.main()
