"""Locate the separately-distributed ``zoe-workflows`` skill checkout.

The runner and the skill are two repositories (design §1). Tests that assert on
the *skill* surface therefore resolve it instead of assuming it is next to
``src/``:

1. ``ZOE_SKILL_DIR`` — explicit, what CI sets;
2. ``../zoe-workflows`` — the sibling checkout developers usually have.

When neither exists the cross-repo tests **skip**, they do not pass: "the other
repository was absent" is not evidence that the contract holds.
"""

from __future__ import annotations

import os
from pathlib import Path

SKIP_REASON = "zoe-workflows checkout not found; set ZOE_SKILL_DIR=/abs/path/to/zoe-workflows"

ROOT = Path(__file__).resolve().parents[1]


def skill_dir() -> Path | None:
    """Return the skill checkout, or ``None`` when it is not available."""
    explicit = os.environ.get("ZOE_SKILL_DIR")
    if explicit:
        candidate = Path(explicit).expanduser()
        if not (candidate / "SKILL.md").is_file():
            # An explicit path that does not hold the skill is a CI wiring bug,
            # not an absent repository: fail instead of silently skipping.
            raise AssertionError(f"ZOE_SKILL_DIR={candidate} has no SKILL.md")
        return candidate
    sibling = ROOT.parent / "zoe-workflows"
    return sibling if (sibling / "SKILL.md").is_file() else None
