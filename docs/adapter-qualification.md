# Adapter qualification: what "usable" means

An adapter in this repository is a *code* claim. Qualification is the separate
question of whether it can be trusted on *this* host, with *this* CLI version,
for *this* execution backend. Nothing in this document is a claim that any
adapter has been qualified — see `docs/capabilities.md` for the current state,
which is "no real end-to-end record".

Two different sets of letters are used below on purpose:

- **Q1–Q4** are the *adapter capability* gates from the design (§10.2). Their
  output is a column in the matrix, not a verdict.
- **S1–S4** are the *host qualification* steps. They run in order, and each one
  can veto everything after it.

## Q1–Q4: adapter capability gates (recorded facts)

| # | Gate | Question | Recorded where |
|---|---|---|---|
| Q1 | headless | is there a one-line, non-interactive invocation? | `CLI_FACTS[name].headless` (`AVAILABLE` / `ABSENT` / `FACT_PENDING`) |
| Q2 | terminal marker | can the end of a run be decided (final JSON object or exit-code semantics)? | adapter `output_protocol` + `result_schema` |
| Q3 | permission switch | is there a non-interactive permission control that is **not** "allow everything"? | adapter `permission_profiles` |
| Q4 | config sources | does the CLI load project/user settings (`settings.json`, `CLAUDE.md`, `.mcp.json`)? | `CLI_FACTS[name].config_sources` |

Q4 is **recorded, never isolated**. Zoe publishes no claim that a worker cannot
read the repository's own agent configuration: the reserved-parameter table
(`_agents/base.py`) blocks argv vectors, not the CLI's automatic discovery.

Q3 is not optional. A non-interactive turn has no approval channel, so a CLI
that offers a permission switch but is launched without one **cannot edit
files**: measured on `codebuddy` 2.111.0, a `-p` run without
`--permission-mode` burned 35 turns and reported `Permission denied` for
`Write`/`Bash`. Adapters therefore declare `permission_switch` and bind the
profile to real argv; `_profiles.require_runnable` fails closed with
`PERMISSION_PROFILE_UNBOUND` when a switch exists but is unbound. `-y` /
`bypassPermissions` are **not** the answer — that trades isolation for
availability and turns Q3 into "allow everything".

`FACT_PENDING` means "no frozen command form", which blocks S3 by construction.

## Capability matrix (v1.0.0)

| Adapter | Q1 headless | Q2 marker | Q3 permission switch | Q4 config sources | State |
|---|---|---|---|---|---|
| `claude-code` | yes | yes | yes (`--permission-mode acceptEdits`, `--allowedTools`) | loads user + project settings | `IMPLEMENTED` |
| `codebuddy` | yes | yes | yes (`--permission-mode acceptEdits`, `--allowedTools`) | unknown | `IMPLEMENTED` |
| `opencode` | yes | yes | none | unknown | `IMPLEMENTED` |
| `codex` | `FACT_PENDING` | `FACT_PENDING` | `FACT_PENDING` | unknown | `IMPLEMENTED` |

"Loads user + project settings" is listed because it is true, not because it is
harmless. Zoe does not isolate that, and no document here claims it does.

## Tested combinations (v1.0.0)

Two rows, kept apart because they answer different questions. The stub row is what
the test suite exercises on every run; the real row is what no host has recorded
yet. Merging them into one "codebuddy is tested" line is exactly the overclaim
this document exists to prevent.

| Combination | Runner | What it proves | Evidence | Level |
|---|---|---|---|---|
| `codebuddy` × `codebuddy-cli` × **stub CLI** | `tests/test_v041_qualification.py`, `tests/test_v030.py::TestAgentCli` | discovery, pinning, argv shape, output parsing, stop/resume bookkeeping | deterministic stub script | simulated-integration |
| `codebuddy` × `codebuddy-cli` × **real CLI 2.111.0** | manual smoke, 2026-10-28/29 | S3 *partly*: `build_launch` argv (`-p --output-format stream-json --permission-mode acceptEdits`) writes files, exits 0, and the capture parses to `completed`; an auth-less `HOME` yields plain text + exit 0, which the parser now classifies as `auth_error`; with `stream-json` the capture grows during the turn, so `task logs` exposes tool-level progress | local observation, **not** a qualification record | smoke (not S3) |

The real-CLI row is deliberately **not** a qualification record: S3 needs
`qualification run`'s six checks, including a confirmed stop/resume of a live
tree. The same applies to every other adapter: `opencode` and `claude-code` are
simulated-integration at best, `codex` cannot be launched at all
(`FACT_PENDING`). A `qualification run` against a stub, or a successful smoke
run, must never be reported as "codebuddy qualified".

## S1–S4: host qualification steps

| Step | Question | Evidence | Failure |
|---|---|---|---|
| S1 resolve | which binary would we launch? | `doctor.resolution` / `agent probe.resolution` (`path` / `source` / `version` / `notes`) | unresolved or several versions → the operator chooses, not the code |
| S2 answer | does it answer `--version` in the launch environment? | bounded probe (5 s, allowlisted env), `probe_status` | `BLOCKED` / `FAILED`; capability unchanged |
| S3 qualified run | does it complete a *real* task under the frozen command form? | `qualification run` six checks, two real runs | `INCOMPLETE_QUALIFICATION`; never promoted by a probe |
| S4 capability | may `start` use this `(adapter, backend, version)`? | compatibility record + registry state | `ADAPTER_UNVERIFIED` / `ADAPTER_VERSION_MISMATCH` |

A probe is S2 and only S2. It cannot promote a backend to `VERIFIED`, and a
passing `doctor` is not S3.

## What each step deliberately does *not* check

- **S1** does not consult a cached version across processes: a version measured
  yesterday must not masquerade as today's fact. The in-process cache only
  deduplicates one command's subprocesses, and reuses a measured version for the
  *same binary only*.
- **S2** does not fall back to "ask the model something" — that would be a turn,
  not a probe, and it would need credentials it is not allowed to demand.
- **S3** does not accept a fixture: synthetic captures are labelled
  `synthetic-from-documented-protocol` and never count as a real run.
- **S4** does not promote on the basis of `doctor`: readiness is environment-only.

## Windows

The platform layer (locks, identity, tree stop) has Windows code paths and no
hardware-verified record. An adapter on Windows is therefore `IMPLEMENTED, not
supported`: qualify it with a real run before relying on it, and expect
`.cmd`/`PATHEXT` resolution differences in S1 (the resolver uses `shutil.which`,
which honours `PATHEXT`; pinning to a `.cmd` path relies on the existing
`cmd.exe /c` launch path, which is itself unverified on hardware).

## Adding an adapter

1. Implement the adapter and register it — the registry is the only source of
   "what can be started".
2. Add its binary name to `_discovery.ADAPTER_CLI` and its detection facts to
   `_discovery.CLI_FACTS`. Launch shapes do not belong there.
3. Add a versioned fixture set **only** if the command form is frozen; a
   `FACT_PENDING` adapter ships no fixtures.
4. Record S3 evidence in `docs/compatibility/*.json` with a concrete version and,
   for `real_e2e`, an `evidence_ref`.
