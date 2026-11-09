# Real end-to-end harness (design §19.2)

This directory holds the tests that **must** be skipped in a normal run: they
need a real Docker daemon, a pinned image, real agent credentials and real host
packages. Their presence is the deliverable; their execution is the evidence.

## Current status: NOT_RUN

| Host | CodeBuddy | OpenCode | Claude Code |
|---|---|---|---|
| WorkBuddy (exact version) | NOT_RUN | NOT_RUN | NOT_RUN |
| OpenClaw (exact version) | NOT_RUN | NOT_RUN | NOT_RUN |

Extended matrix (`codex`) is separate and also NOT_RUN. Nothing here promotes an
adapter to `VERIFIED`; only a recorded run plus a `docs/compatibility/` record
does that, and `zoe agent list` is the single source of truth.

## How to run

```bash
export ZOE_REAL_E2E=1
export ZOE_E2E_IMAGE='registry/zoe-codebuddy@sha256:<64-hex>'
export ZOE_E2E_REPO=/path/to/scratch/repo          # throwaway clone
export CODEBUDDY_API_KEY=...                        # or the adapter's own key
python -m pytest tests/integration -q
```

Each cell of the matrix runs the same loop:

natural-language spec → `task create` → `task start` → real file change →
controller verification → `local_artifact` `REVIEW_READY` → human acceptance,
plus one stop/resume cycle.

Without `ZOE_REAL_E2E=1` every test in this directory is skipped, and skipping is
not evidence. Do not describe a skipped run as a passing one.
