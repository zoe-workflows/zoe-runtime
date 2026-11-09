"""config-friction v5: path semantics, aggregated diagnostics, prepared tasks.

The invariants under test are the six the review blocked on:

1. an empty ``task_scope`` is refused, never widened back to the project scope;
2. declaring a protected path is refused, while declaring its **parent** is not;
3. diagnostics aggregate only independently-judgeable problems;
4. ``prepare`` writes no runtime state and its drafts cannot be mistaken for a
   finished spec;
5. ``--out``/``--emit`` keep existing files;
6. the post-run scope check reads the task scope, not the project scope.

No agent, no network: every check command is ``true``.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from src import _backend, _config, _contracts, _paths, _pipeline, _prepare
from src._util import Error, atomic
from src.core import Store

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
    "protected_paths": [".env", "src/.secret"],
    "checks": [{"id": "unit", "argv": ["true"]}],
}


def _spec(**overrides) -> dict:
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


class Fixture(unittest.TestCase):
    """A registered project on a real (throwaway) git repository."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.repo = root / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "t@example.com"], check=True
        )
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)
        self.head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.profile = dict(PROFILE, repo=str(self.repo))
        self.profile_path = root / "profile.json"
        self.profile_path.write_text(json.dumps(self.profile), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def register(self):
        self.home.mkdir(parents=True, exist_ok=True)
        config = _config.validate_config(dict(self.profile), base_dir=self.profile_path.parent)
        atomic(self.home / "projects" / f"{config['project_id']}.json", config)
        return config

    def write_spec(self, document) -> str:
        path = Path(self._tmp.name) / f"spec-{uuid.uuid4().hex[:6]}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)


class PathSemantics(unittest.TestCase):
    def test_component_boundary(self):
        self.assertTrue(_paths.is_within("src/a", "src"))
        self.assertFalse(_paths.is_within("src/abc", "src/a"))

    def test_normalisation_folds_dots_and_separators(self):
        self.assertEqual(_paths.normalize("src/./a/../b"), "src/b")
        self.assertEqual(_paths.normalize("src\\a"), "src/a")

    def test_subtract_is_list_level_not_expansion(self):
        self.assertEqual(_paths.subtract(["src"], ["src/security"]), ["src"])
        self.assertEqual(_paths.subtract(["src/security"], ["src/security"]), [])

    def test_declared_under_distinguishes_declaration_from_ancestor(self):
        self.assertTrue(_paths.declared_under("src/security", ["src/security"]))
        self.assertFalse(_paths.declared_under("src", ["src/security"]))

    def test_trailing_slash_and_dotdot_fold(self):
        self.assertEqual(_paths.normalize("src/"), "src")
        self.assertEqual(_paths.normalize("src/../src/a"), "src/a")
        self.assertEqual(_paths.normalize(""), "")

    def test_empty_parent_is_not_a_scope(self):
        # ``""`` / ``"."`` used to mean "anywhere", which turned an accidental
        # entry into whole-repository authorization.
        self.assertFalse(_paths.is_within("anything/at/all", "."))
        self.assertFalse(_paths.is_within("anything/at/all", ""))
        self.assertTrue(_paths.is_within("src", "src"))

    def test_covers_accepts_any_ancestor(self):
        self.assertTrue(_paths.covers("src/a/b.py", ["tests", "src"]))
        self.assertFalse(_paths.covers("srcx/b.py", ["src"]))

    def test_same_path_resolves_symlinks_when_they_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._assert_symlink_case(Path(tmp))

    def _assert_symlink_case(self, root: Path):
        real = root / "real"
        real.mkdir(exist_ok=True)
        link = root / "link"
        if not link.exists():
            try:
                link.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable on this host")
        self.assertTrue(_paths.same_path(link, real))

    def test_same_path_falls_back_to_text_for_planned_paths(self):
        self.assertTrue(_paths.same_path("src/a.py", "./src/a.py"))
        self.assertFalse(_paths.same_path("src/a.py", "src/b.py"))

    def test_describe_missing_flags_escaping_paths_only(self):
        self.assertEqual(
            _paths.describe_missing("../outside"), "path escapes its own root with '..'"
        )
        self.assertIsNone(_paths.describe_missing("src/new/dir"))
        self.assertIsNone(_paths.describe_missing("/abs/path"))

    def test_git_toplevel_treats_a_git_file_as_a_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "wt"
            (worktree / "sub").mkdir(parents=True)
            (worktree / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
            self.assertEqual(_paths.git_toplevel(worktree / "sub"), str(worktree.resolve()))


class AggregatedDiagnostics(Fixture):
    def test_independent_errors_are_reported_together(self):
        broken = dict(
            self.profile, max_active_tasks=99, allowed_paths=[], checks=[{"id": "u", "argv": []}]
        )
        with self.assertRaises(Error) as ctx:
            _config.validate_config(broken, base_dir=self.profile_path.parent)
        details = ctx.exception.details
        self.assertEqual(details["count"], 3)
        self.assertEqual(
            [item["field"] for item in details["errors"]],
            ["max_active_tasks", "checks[0].argv", "allowed_paths"],
        )

    def test_top_level_error_still_names_the_first_problem(self):
        broken = dict(self.profile, max_active_tasks=99)
        with self.assertRaises(Error) as ctx:
            _config.validate_config(broken, base_dir=self.profile_path.parent)
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("max_active_tasks", str(ctx.exception))

    def test_structural_gate_does_not_manufacture_cascade(self):
        broken = dict(self.profile, repo=str(self.repo / "missing"), allowed_paths=[])
        with self.assertRaises(Error) as ctx:
            _config.validate_config(broken, base_dir=self.profile_path.parent)
        self.assertIsNone(ctx.exception.details)


class ScopeEntries(Fixture):
    """``""`` / ``"."`` / absolute entries must never widen a scope silently."""

    def test_project_scope_refuses_non_relative_entries(self):
        for entries in (["."], [""], ["   "], ["/etc"]):
            broken = dict(self.profile, allowed_paths=entries)
            with self.assertRaises(Error) as ctx:
                _config.validate_config(broken, base_dir=self.profile_path.parent)
            self.assertIn("repository-relative", str(ctx.exception))

    def test_protected_entries_get_the_same_treatment(self):
        broken = dict(self.profile, protected_paths=["/etc"])
        with self.assertRaises(Error) as ctx:
            _config.validate_config(broken, base_dir=self.profile_path.parent)
        self.assertIn("repository-relative", str(ctx.exception))

    def test_spec_scope_refuses_non_relative_entries(self):
        config = _config.validate_config(dict(self.profile))
        import json as _json

        path = Path(self._tmp.name) / "dot-spec.json"
        path.write_text(_json.dumps(_spec(allowed_paths=["."])), encoding="utf-8")
        with self.assertRaises(Error) as ctx:
            _contracts.load_spec(str(path), config)
        self.assertIn("repository-relative", str(ctx.exception))


class AuthorizationShape(Fixture):
    """授权字段必须在注册期按形状校验，不能交给 ``in`` 去解释。

    ``"publish" in "no-publish"`` 为真：把允许列表写成字符串，作者以为拒绝了，
    运行时却放行。``protected_paths: []`` 同理——它会把 ``.env`` / ``.github``
    的默认保护静默关掉（默认值只在键缺失时生效）。
    """

    def test_a_string_allow_list_is_refused(self):
        broken = dict(self.profile, authorized_actions="no-publish, no-accept")
        with self.assertRaises(Error) as ctx:
            _config.validate_config(broken, base_dir=self.profile_path.parent)
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("authorized_actions", str(ctx.exception))

    def test_an_unknown_action_is_refused(self):
        broken = dict(self.profile, authorized_actions=["create", "execute", "exec"])
        with self.assertRaises(Error) as ctx:
            _config.validate_config(broken, base_dir=self.profile_path.parent)
        self.assertIn("authorized_actions", str(ctx.exception))

    def test_an_empty_protected_list_is_refused(self):
        broken = dict(self.profile, protected_paths=[])
        with self.assertRaises(Error) as ctx:
            _config.validate_config(broken, base_dir=self.profile_path.parent)
        self.assertIn("protected_paths", str(ctx.exception))

    def test_a_null_protected_list_is_refused(self):
        broken = dict(self.profile, protected_paths=None)
        with self.assertRaises(Error) as ctx:
            _config.validate_config(broken, base_dir=self.profile_path.parent)
        self.assertIn("protected_paths", str(ctx.exception))

    def test_a_hand_edited_string_allow_list_authorizes_nothing(self):
        """防深：注册期拒绝之外，访问器自己也不做子串匹配。"""
        self.assertFalse(_config.is_authorized({"authorized_actions": "no-publish"}, "publish"))
        self.assertTrue(_config.is_authorized({"authorized_actions": ["publish"]}, "publish"))


class ConfigFieldShape(Fixture):
    """字段类型错误必须给出结构化原因，而不是 AttributeError / bool 混淆。"""

    def test_null_network_is_a_structured_error(self):
        with self.assertRaises(Error) as ctx:
            _config.validate_config(
                dict(self.profile, network=None), base_dir=self.profile_path.parent
            )
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("network", str(ctx.exception))

    def test_a_boolean_schema_version_is_not_v1(self):
        # ``True in (1, 2)`` 为真，而 ``schema_version == 1`` 会打开迁移专用的
        # legacy 分支（跳过 model 必填）。
        with self.assertRaises(Error) as ctx:
            _config.validate_config(
                dict(self.profile, schema_version=True), base_dir=self.profile_path.parent
            )
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("schema_version", str(ctx.exception))

    def test_deny_prefixes_must_be_an_array(self):
        with self.assertRaises(Error) as ctx:
            _config.validate_config(
                dict(self.profile, deny_repo_prefixes="/tmp/elsewhere"),
                base_dir=self.profile_path.parent,
            )
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")


class RegisterDryRun(Fixture):
    def test_dry_run_writes_nothing(self):
        from src import cli

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(
                [
                    "--home",
                    str(self.home),
                    "project",
                    "register",
                    "--config",
                    str(self.profile_path),
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0)
        report = json.loads(buffer.getvalue())
        self.assertTrue(report["valid"])
        self.assertFalse(report["will_overwrite"])
        self.assertFalse(self.home.exists())

    def test_dry_run_reports_path_warnings_without_blocking(self):
        with_missing = dict(self.profile, allowed_paths=["src", "svc/typo"])
        path = Path(self._tmp.name) / "warn.json"
        path.write_text(json.dumps(with_missing), encoding="utf-8")
        from src import cli

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.main(
                [
                    "--home",
                    str(self.home),
                    "project",
                    "register",
                    "--config",
                    str(path),
                    "--dry-run",
                ]
            )
        report = json.loads(buffer.getvalue())
        self.assertTrue(report["valid"])
        self.assertEqual([w["code"] for w in report["warnings"]], ["SCOPE_MISSING"])


class TaskScope(Fixture):
    def test_declared_scope_is_frozen(self):
        self.register()
        with Store(str(self.home)) as store:
            result = _pipeline.create(
                store,
                {
                    "schema_version": 2,
                    "request_id": "req-" + uuid.uuid4().hex,
                    "project_id": "demo",
                    "title": "tighten",
                    "spec_file": self.write_spec(_spec(allowed_paths=["src"])),
                    "delivery_mode": "local_artifact",
                    "authorization_ref": "auth-demo",
                },
            )
            task = store.task(result["task_id"])
        self.assertEqual(task["task_scope"], ["src"])
        self.assertEqual(task["scope_source"], "spec")

    def test_absent_scope_inherits_project(self):
        self.register()
        with Store(str(self.home)) as store:
            result = _pipeline.create(
                store,
                {
                    "schema_version": 2,
                    "request_id": "req-" + uuid.uuid4().hex,
                    "project_id": "demo",
                    "title": "inherit",
                    "spec_file": self.write_spec(_spec()),
                    "delivery_mode": "local_artifact",
                    "authorization_ref": "auth-demo",
                },
            )
            task = store.task(result["task_id"])
        self.assertIsNone(task["task_scope"])
        self.assertEqual(task["scope_source"], "project")

    def test_empty_scope_is_refused_not_widened(self):
        self.register()
        with Store(str(self.home)) as store:
            with self.assertRaises(Error) as ctx:
                _pipeline.create(
                    store,
                    {
                        "schema_version": 2,
                        "request_id": "req-" + uuid.uuid4().hex,
                        "project_id": "demo",
                        "title": "empty",
                        "spec_file": self.write_spec(_spec(allowed_paths=[])),
                        "delivery_mode": "local_artifact",
                        "authorization_ref": "auth-demo",
                    },
                )
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")
        self.assertIn("empty scope", str(ctx.exception))

    def test_declaring_a_protected_path_is_refused(self):
        self.register()
        with Store(str(self.home)) as store:
            with self.assertRaises(Error) as ctx:
                _pipeline.create(
                    store,
                    {
                        "schema_version": 2,
                        "request_id": "req-" + uuid.uuid4().hex,
                        "project_id": "demo",
                        "title": "secret",
                        "spec_file": self.write_spec(_spec(allowed_paths=["src/.secret"])),
                        "delivery_mode": "local_artifact",
                        "authorization_ref": "auth-demo",
                    },
                )
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")
        self.assertIn("protected", str(ctx.exception))

    def test_declaring_a_parent_of_a_protected_path_is_allowed(self):
        self.register()
        loaded = _contracts.load_spec(
            self.write_spec(_spec(allowed_paths=["src"])),
            _config.validate_config(dict(self.profile)),
        )
        self.assertEqual(_contracts.spec_scope(loaded["document"]), ["src"])

    def test_scope_check_reads_task_scope(self):
        self.register()
        (self.repo / "README.md").write_text("changed\n", encoding="utf-8")
        task = {
            "workspace": str(self.repo),
            "base_sha": self.head,
            "config": {"allowed_paths": ["src", "README.md"], "protected_paths": []},
            "task_scope": ["src"],
        }
        with self.assertRaises(Error) as ctx:
            _backend.scope(task)
        self.assertEqual(ctx.exception.code, "SCOPE_VIOLATION")
        self.assertEqual(ctx.exception.details["scope_source"], "spec")
        self.assertIn("README.md", str(ctx.exception))


class Placeholders(Fixture):
    def test_spec_placeholder_is_refused_everywhere(self):
        config = _config.validate_config(dict(self.profile))
        for document in (
            _spec(title="<FILL:目标>"),
            _spec(
                acceptance_criteria=[
                    {
                        "id": "AC-1",
                        "statement": "<FILL:条件>",
                        "verification": {"type": "manual", "instructions": "ask a human"},
                    }
                ]
            ),
            _spec(allowed_paths=["<FILL:路径>"]),
        ):
            with self.assertRaises(Error) as ctx:
                _contracts.load_spec(self.write_spec(document), config)
            self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
            self.assertIn("placeholder", str(ctx.exception))

    def test_request_placeholder_fails_before_authorization(self):
        self.register()
        with Store(str(self.home)) as store:
            with self.assertRaises(Error) as ctx:
                _pipeline.create(
                    store,
                    {
                        "schema_version": 2,
                        "request_id": "req-" + uuid.uuid4().hex,
                        "project_id": "demo",
                        "title": "<FILL:目标>",
                        "spec_file": self.write_spec(_spec()),
                        "delivery_mode": "local_artifact",
                        "authorization_ref": "auth-demo",
                    },
                )
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")
        self.assertIn("title", str(ctx.exception))


class Prepare(Fixture):
    def test_prepare_is_read_only_and_ready(self):
        self.register()
        spec = self.write_spec(_spec(allowed_paths=["src"]))
        before = sorted(p.name for p in self.home.iterdir())
        report = _prepare.prepare(str(self.home), repo=str(self.repo), spec=spec)
        after = sorted(p.name for p in self.home.iterdir())
        self.assertEqual(before, after)
        self.assertTrue(report["prepared"])
        self.assertEqual(report["project"]["project_id"], "demo")
        self.assertEqual(report["spec"]["scope_source"], "spec")
        self.assertEqual(report["diagnostics"], [])

    def test_prepare_reports_every_independent_problem(self):
        self.register()
        spec = self.write_spec(_spec(allowed_paths=["src/.secret"]))
        report = _prepare.prepare(str(self.home), project="demo", spec=spec)
        self.assertFalse(report["prepared"])
        self.assertEqual([d["code"] for d in report["diagnostics"]], ["POLICY_DENIED"])

    def test_an_unauthorized_project_is_never_reported_as_prepared(self):
        """``prepared`` 的语义是"能创建并启动"；未授权 execute 时 start 必被拒。"""
        self.profile["authorized_actions"] = ["create"]
        self.profile_path.write_text(json.dumps(self.profile), encoding="utf-8")
        self.register()
        report = _prepare.prepare(
            str(self.home), project="demo", spec=self.write_spec(_spec(allowed_paths=["src"]))
        )
        self.assertFalse(report["prepared"])
        self.assertFalse(report["profile"]["authorized"])
        self.assertIn("AUTH_REQUIRED", [d["code"] for d in report["diagnostics"]])

    def test_repo_matching_uses_the_git_root(self):
        self.register()
        nested = self.repo / "src"
        report = _prepare.prepare(str(self.home), repo=str(nested), spec=self.write_spec(_spec()))
        self.assertEqual(report["project"]["project_id"], "demo")

    def test_unregistered_repo_yields_a_draft_requirement(self):
        self.home.mkdir(parents=True, exist_ok=True)
        report = _prepare.prepare(
            str(self.home), repo=str(self.repo), spec=self.write_spec(_spec())
        )
        self.assertFalse(report["prepared"])
        self.assertTrue(report["unregistered"])
        self.assertIn("PROJECT_UNREGISTERED", [d["code"] for d in report["diagnostics"]])

    def test_emit_writes_drafts_and_keeps_them(self):
        self.register()
        target = Path(self._tmp.name) / "draft"
        first = _prepare.prepare(str(self.home), project="demo", emit=str(target))
        self.assertEqual(len(first["emitted"]["created"]), 2)
        create_request = json.loads((target / "create.json").read_text())
        self.assertTrue(create_request["request_id"])
        second = _prepare.prepare(str(self.home), project="demo", emit=str(target))
        self.assertEqual(second["emitted"]["created"], [])
        self.assertEqual(len(second["emitted"]["kept"]), 2)
        self.assertEqual(
            json.loads((target / "create.json").read_text())["request_id"],
            create_request["request_id"],
        )

    def test_out_refuses_to_overwrite(self):
        self.register()
        target = Path(self._tmp.name) / "report.json"
        target.write_text("{}", encoding="utf-8")
        with self.assertRaises(Error) as ctx:
            _prepare.prepare(
                str(self.home), project="demo", spec=self.write_spec(_spec()), out=str(target)
            )
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")
        _prepare.prepare(
            str(self.home),
            project="demo",
            spec=self.write_spec(_spec()),
            out=str(target),
            force=True,
        )
        self.assertEqual(json.loads(target.read_text())["api_version"], 2)

    def test_out_and_emit_are_mutually_exclusive(self):
        with self.assertRaises(Error) as ctx:
            _prepare.prepare(str(self.home), project="demo", out="a.json", emit="drafts")
        self.assertEqual(ctx.exception.code, "INPUT_INCOMPLETE")

    def test_prepare_without_spec_or_emit_asks_for_one(self):
        self.register()
        report = _prepare.prepare(str(self.home), project="demo")
        self.assertFalse(report["prepared"])
        self.assertIn("INPUT_INCOMPLETE", [d["code"] for d in report["diagnostics"]])

    def test_prepare_surfaces_the_profile_and_its_model(self):
        self.register()
        report = _prepare.prepare(str(self.home), project="demo", spec=self.write_spec(_spec()))
        self.assertEqual(report["profile"]["selected"], "cb")
        self.assertEqual(report["profile"]["model_source"], "profile")
        self.assertTrue(report["profile"]["authorized"])
        self.assertEqual(report["profile"]["conflicts"], [])


if __name__ == "__main__":
    unittest.main()
