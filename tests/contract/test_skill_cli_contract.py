"""Skill ↔ runtime CLI contract, checked in CI rather than at runtime (§2.5).

The runtime gate (§2.2) only checks version, ``api_version`` and
``protocol_revision`` — a machine-readable command list is deliberately *not*
part of it. Commands and parameters are a CI concern, and this module is that CI:

| scenario                                        | expected |
|-------------------------------------------------|----------|
| the skill claims a command or option we reject   | red      |
| the runtime drops an interface the skill uses    | red      |
| the runtime adds a command the skill never uses  | green    |
| the skill drops a command it no longer uses      | green    |

The asymmetry is the point: only the skill's *documented* calls are asserted.
Nothing here requires the runtime to be documented completely, so removing an
unused line from the skill can never turn this gate red.

Source of truth for the skill side: the fenced ``text`` blocks of
``references/cli-contract.md`` (commands are documented as callable lines).
"""

from __future__ import annotations

import argparse
import re
import unittest

from skill_package import SKIP_REASON, skill_dir

from src.cli import _build_parser

SKILL = skill_dir()
CONTRACT = "references/cli-contract.md"
_FENCE = re.compile(r"```text\n(.*?)```", re.S)


def _subparsers(parser: argparse.ArgumentParser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def documented_calls() -> list[str]:
    """Return one normalised call per documented command line."""
    text = (SKILL / CONTRACT).read_text(encoding="utf-8")
    calls = []
    for block in _FENCE.findall(text):
        pending = ""
        for raw in block.splitlines():
            line = raw.split("#", 1)[0].strip() if raw.lstrip().startswith("#") else raw
            line = line.split("   #", 1)[0].strip()
            if not line:
                continue
            if line.endswith("\\"):
                pending += line[:-1].strip() + " "
                continue
            calls.append((pending + line).strip())
            pending = ""
    return calls


def resolve(call: str) -> tuple[list[argparse.ArgumentParser], list[str], str | None]:
    """Walk the parser down the documented command path.

    Returns the parser chain, the remaining tokens and an error string when a
    documented sub-command does not exist.
    """
    tokens = [token for token in re.sub(r"[\[\]]", " ", call).split() if token]
    parser = _build_parser()
    chain = [parser]
    index = 0
    while index < len(tokens):
        sub = _subparsers(chain[-1])
        if sub is None or tokens[index].startswith("-"):
            break
        name = tokens[index]
        if name not in sub.choices:
            return chain, tokens[index:], name
        chain.append(sub.choices[name])
        index += 1
    return chain, tokens[index:], None


def accepted_options(chain) -> set[str]:
    names = set()
    for parser in chain:
        for action in parser._actions:
            names.update(action.option_strings)
    return names


@unittest.skipUnless(SKILL, SKIP_REASON)
class TestDocumentedCalls(unittest.TestCase):
    def test_the_contract_documents_at_least_one_call(self):
        # A silently empty block would make every assertion below vacuous.
        self.assertGreater(len(documented_calls()), 10)

    def test_every_documented_command_exists(self):
        for call in documented_calls():
            with self.subTest(call=call):
                _chain, _rest, missing = resolve(call)
                self.assertIsNone(missing, f"skill documents unknown command {missing!r}: {call}")

    def test_every_documented_option_is_accepted(self):
        for call in documented_calls():
            chain, rest, missing = resolve(call)
            if missing:
                continue  # reported by the command test, not twice
            allowed = accepted_options(chain)
            for token in rest:
                if not token.startswith("--"):
                    continue
                name = token.split("=", 1)[0]
                with self.subTest(call=call, option=name):
                    self.assertIn(name, allowed, f"{name} is not accepted by: {call}")

    def test_the_gate_rejects_a_command_the_runtime_lost(self):
        """Self-check of the gate itself: the red path must stay red."""
        chain, rest, missing = resolve("definitely-not-a-command --json")
        self.assertEqual(missing, "definitely-not-a-command")
        self.assertNotIn("--definitely-not-an-option", accepted_options(chain))


#: 观测面的负向文案（v2.2 §5）：这些说法会直接导致误判，出现即红。
BANNED_CLAIMS = (
    "--follow 能看到之前的进展",
    "kind=record\" = 没有输出",
    "task logs --project",
    "task logs --jsonl",
    "日志静默说明卡住",
)


@unittest.skipUnless(SKILL, SKIP_REASON)
class TestLogsObservationContract(unittest.TestCase):
    """v1.5.2：`task logs --after` 与窗口语义必须在两侧都自洽。

    文档说了一个选项，parser 就必须接受；parser 的 help 说了互斥关系，文档也必须
    写到同一件事——否则宿主会按文档传参、按文档误读结果。
    """

    def _logs_chain(self):
        chain, _rest, missing = resolve("task logs TASK --after RUN:1:0")
        self.assertIsNone(missing)
        return chain

    def test_after_and_tail_bytes_are_accepted(self):
        allowed = accepted_options(self._logs_chain())
        self.assertIn("--after", allowed)
        self.assertIn("--tail-bytes", allowed)
        self.assertIn("--follow", allowed)

    def test_help_states_the_follow_exclusion(self):
        parser = resolve("task logs TASK")[0][-1]
        action = next(a for a in parser._actions if "--after" in a.option_strings)
        self.assertIn("不能与 --follow 同用", action.help)

    def test_the_skill_documents_the_option_matrix(self):
        text = (SKILL / CONTRACT).read_text(encoding="utf-8")
        for phrase in ("--after C", "task logs T", "--follow --tail-bytes N"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        # 两种 `--after` 同名不同物，必须点明。
        self.assertIn("**不是** `task events --after` 的整数事件 id", text)

    def test_the_skill_does_not_teach_the_misreadings(self):
        for name in ("SKILL.md", CONTRACT):
            text = (SKILL / name).read_text(encoding="utf-8")
            for phrase in BANNED_CLAIMS:
                with self.subTest(file=name, phrase=phrase):
                    self.assertNotIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
