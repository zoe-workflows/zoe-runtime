"""Doc gates: the claims we removed must not creep back (v0.5.1 W3).

Docs are part of the product surface here — a host reads `SKILL.md` and decides
whether to dispatch. Since the split (§1) the surface spans two repositories:
`SKILL.md` and `references/` ship with **zoe-workflows**, while the READMEs and
`docs/` stay with **zoe-runtime**. The gate covers both, skipping the skill half when
that checkout is absent (``ZOE_SKILL_DIR``).

The gate is deliberately narrow:

1. phrases that assert a model we do not have ("single execution slot",
   "serial only", "no parallel writers") may not appear in what a host reads
   (`SKILL.md`, `references/`, both READMEs). `docs/` is **not** gated as a whole —
   it is written for humans and reworded freely;
2. the concurrency table in `references/cli-contract.md` must agree with
   `MAX_ACTIVE_TASKS`, the count scope and the limit source — parsed cell by
   cell, not matched as "the file contains an 8 somewhere";
3. `capabilities()` must still refuse to advertise what it does not do, and must
   report the same constants the table does.

Plus two point checks: the `concurrency` object is documented where readers look,
and no live doc points at the removed v0.2.0 overview, the v0.2.0 release plan or
the v0.4 contract specs — those described the pre-split layout and were archived.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from skill_package import SKIP_REASON, skill_dir

from src._config import MAX_ACTIVE_TASKS

ROOT = Path(__file__).resolve().parents[1]
SKILL = skill_dir()

#: Runner-owned files a host may show next to the skill documentation.
RUNNER_GATED = ["README.md", "README.zh-CN.md"]

#: Skill-owned files, i.e. the installed skill surface.
SKILL_GATED = [
    "SKILL.md",
    "README.md",
    "references/cli-contract.md",
    "references/runtime-contract.md",
    "references/decomposition-patterns.md",
    "references/delivery-policy.md",
    "references/report-template.md",
    "references/spec-template.md",
]

#: Claims that are no longer true, or were never true.
RUNNER_BANNED = {
    "README.md": ["single execution slot", "Single execution slot", "no parallel tasks"],
    "README.zh-CN.md": ["单执行槽"],
}
SKILL_BANNED = {
    "SKILL.md": ["单执行槽", "仍然串行", "不支持并行写者"],
    "README.md": ["单执行槽"],
    "references/cli-contract.md": ["单执行槽"],
    "references/decomposition-patterns.md": ["默认单执行槽", "仍然串行", "真并行执行"],
    "references/runtime-contract.md": ["全局单执行槽"],
}

#: What must be said, and where (all skill-owned).
REQUIRED = {
    "SKILL.md": ["max_active_tasks", "verification_scope", "NOT_RUN", "dispatch_pending"],
    "references/cli-contract.md": ["dispatch_pending", "TASK_BUSY", "verification_scope"],
    "references/delivery-policy.md": ["NOT_RUN", "Project-wide verification", "重放"],
    "references/spec-template.md": ["verification_scope"],
}

#: Capabilities we must keep refusing to advertise.
#:
#: v1.3 (TGD §2.2) reverses one of them: a *static* member dependency graph is
#: now scheduled, so `automatic_dag_scheduling` must disappear from the list
#: instead of lingering as a stale refusal. `automatic_merge` becomes narrower
#: and is renamed accordingly: only merging into the **target** branch is
#: refused; controlled integration into `zoe/group-<id>` is a delivered feature.
NOT_SUPPORTED = ("parallel_writers", "automatic_merge_to_target_branch")

#: v1.3 capabilities that are now *supported* and must not be refused above.
NOW_SUPPORTED = (
    "automatic_dag_scheduling",
    "task_groups",
    "group_dependency_graph",
    "group_serial_execution",
    "group_acceptance",
)


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def read_skill(name: str) -> str:
    return (SKILL / name).read_text(encoding="utf-8")


class TestRunnerClaims(unittest.TestCase):
    def test_the_removed_execution_model_is_gone(self):
        for name, phrases in RUNNER_BANNED.items():
            text = read(name)
            for phrase in phrases:
                with self.subTest(file=name, phrase=phrase):
                    self.assertNotIn(phrase, text)


@unittest.skipUnless(SKILL, SKIP_REASON)
class TestSkillClaims(unittest.TestCase):
    def test_the_removed_execution_model_is_gone(self):
        for name, phrases in SKILL_BANNED.items():
            text = read_skill(name)
            for phrase in phrases:
                with self.subTest(file=name, phrase=phrase):
                    self.assertNotIn(phrase, text)

    def test_no_gated_doc_reserves_a_project_slot(self):
        for name in SKILL_GATED:
            with self.subTest(file=name):
                self.assertNotIn("项目单执行槽", read_skill(name))

    def test_scope_and_replay_are_documented_where_hosts_look(self):
        for name, phrases in REQUIRED.items():
            text = read_skill(name)
            for phrase in phrases:
                with self.subTest(file=name, phrase=phrase):
                    self.assertIn(phrase, text)


@unittest.skipUnless(SKILL, SKIP_REASON)
class TestConcurrencyTable(unittest.TestCase):
    """Parse the table cells instead of grepping the whole file (§8.3-2)."""

    @classmethod
    def setUpClass(cls):
        cls.rows = {}
        for line in read_skill("references/cli-contract.md").splitlines():
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 2:
                cls.rows.setdefault(cells[0], cells[1])

    def find(self, needle: str) -> str:
        for key, value in self.rows.items():
            if needle in key:
                return value
        self.fail(f"no row mentioning {needle!r} in references/cli-contract.md")

    def test_the_range_matches_the_constant(self):
        cell = self.find("max_active_tasks")
        match = re.search(r"(\d+)\.\.(\d+)", cell)
        self.assertIsNotNone(match, cell)
        self.assertEqual(int(match.group(1)), 1)
        self.assertEqual(int(match.group(2)), MAX_ACTIVE_TASKS)

    def test_the_count_scope_is_global(self):
        self.assertIn("global", self.find("活跃计数作用域"))

    def test_the_limit_comes_from_the_project(self):
        self.assertIn("project_config", self.find("限额来源"))


class TestCapabilitiesConstants(unittest.TestCase):
    def setUp(self):
        from src.cli import capabilities

        self.capabilities = capabilities()

    def test_the_snapshot_matches_the_table(self):
        concurrency = self.capabilities["concurrency"]
        self.assertEqual(concurrency["max_supported"], MAX_ACTIVE_TASKS)
        self.assertEqual(concurrency["default"], 1)
        self.assertEqual(concurrency["count_scope"], "global")
        self.assertEqual(concurrency["limit_source"], "project_config")

    def test_unsupported_capabilities_are_still_declared(self):
        for name in NOT_SUPPORTED:
            with self.subTest(name):
                self.assertIn(name, self.capabilities["not_supported"])

    def test_the_dependency_graph_is_no_longer_refused(self):
        """A capability reversal must retire the old refusal, not double-book it."""
        for name in NOW_SUPPORTED:
            with self.subTest(name):
                self.assertNotIn(name, self.capabilities["not_supported"])
        for name in ("task_groups", "group_dependency_graph"):
            with self.subTest(supported=name):
                self.assertIn(name, self.capabilities["features"])


class TestPointObligations(unittest.TestCase):
    """§14 items that are literal strings rather than prose."""

    def test_capabilities_doc_points_at_the_single_source(self):
        self.assertIn("capabilities.concurrency", read("docs/capabilities.md"))

    def test_no_doc_claims_a_superseded_baseline_as_current(self):
        """The v0.2.0 overview and the version design drafts were removed.

        They described the pre-split layout (`src/zoe_lite`, in-repo
        `references/`), so a live `docs/` file must not point at them either.
        """
        for path in sorted((ROOT / "docs").rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            for stale in ("overview.zh-CN.md", "release-v0.2.0.md", "specs/v0.4"):
                with self.subTest(file=path.name, stale=stale):
                    self.assertNotIn(stale, text)


if __name__ == "__main__":
    unittest.main()
