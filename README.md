# Zoe Runtime

**A controlled execution controller for local AI coding tasks — hand a frozen spec to an agent, keep every decision that matters.**

`v1.5.0` ｜ Python ≥ 3.11 ｜ standard library only ｜ macOS · Linux · Windows (`local-process`)

Zoe Runtime turns one spec into one task and drives it through a single audited pipeline:

```
create → route (freeze) → implement (adapter) → verify → publish → CI → accept
```

For a delivery that needs ordering, integration and **one** acceptance, a task group freezes
a plan and runs its members serially:

```
group plan → group create (freeze) → group start (runner) → integrate → group checks
           → design review (host) → group accept → group report
```

Executors are selected from an **adapter registry** keyed by `(adapter, backend)`. Built-ins:
`codebuddy` (`codebuddy-cli`), `opencode` (`opencode-cli`), `claude-code` (`anthropic-api`);
extended: `codex` (`backend-pending`). An adapter only describes *how to launch* and
*how to parse output* — process lifetime, mounts, concurrency and publication stay with
the controller.

Four invariants are enforced, not merely documented: single writer, independent
verification, controller-owned publication, honest acceptance. The agent saying "I'm done"
is never treated as evidence. A fifth one arrived in 1.5: **configuration is not
authorization** — a profile that exists is not a profile you allowed.

> **1.5.0: the interfaces are frozen, adapter evidence is not.** The CLI surface, the
> on-disk state (schema v4) and the skill contract (`api_version` 2 /
> `protocol_revision` 1) are stable from here on. What is *not* settled is
> evidence: no adapter in this build reaches `VERIFIED`, and every real local-CLI
> end-to-end run is `NOT_RUN`. Capability stays tri-state — `IMPLEMENTED` /
> `UNVERIFIED` / `VERIFIED(version, backend)` — but the state is a *label, not an
> admission ticket*: an unverified profile still starts, and the run is recorded
> as `unverified` / `trust_level`; only a project that sets
> `require_real_e2e: true` is refused at `start` with `ADAPTER_UNVERIFIED`.
> Host integration is likewise unverified: `capabilities` reports
> `real_e2e: NOT_RUN` for both WorkBuddy and OpenClaw. A backend is promoted by a
> precise-version record under `docs/compatibility/`, never by a release number.

```bash
zoe capabilities --json                                          # registry view, no project needed
zoe agent list --project example --json                          # per-profile state + evidence source
zoe agent probe --project example --profile cb-approved --json   # bounded probe; does not promote state
```

`codex` is registered with `facts_status=FACT_PENDING`: the adapter contract exists, the
command shape is not frozen, so it can never be described as "supported". The only way to
promote any backend is a precise-version record under `docs/compatibility/`.

## Why

### Calling an agent directly is not enough

| Dimension | Direct agent call | Orchestrated by Zoe Runtime |
|---|---|---|
| **State** | none; lost when the process ends | full task lifecycle (8 states, 6 phases); groups add a frozen plan, an integration ledger and one acceptance |
| **Containment** | agent can read/write anything and commit | per-task worktree + one writer per task + credential allowlist + post-hoc `allowed_paths` audit (accountability, **not** a sandbox) |
| **Authorization** | whoever wrote the config decides what runs | explicit `routing.allowed_profiles`, one `project attest` bound to the config digest, `ALLOWLIST_UNCONFIRMED` before the first dispatch |
| **Recovery** | manual forensics after a crash | reconcile supervisor + process tree + worktree, resume from the interrupted phase |
| **Publication** | agent commits and pushes on its own | controller owns every Git write; PRs carry an ownership marker |
| **Acceptance** | no gate | SHA match + unchanged workspace fingerprint + human decision; group members cannot self-accept |

### What you actually get

1. **End-to-end control** — spec → task → agent execution → independent verification → PR → CI → human acceptance.
2. **Distrust of self-reports** — the controller re-runs checks in a separate environment and compares workspace fingerprints before and after.
3. **Crash recovery** — process identity + process-tree observation, surviving worktrees preserved.
4. **Publication stays with you** — the controller owns the documented publish path (the agent still holds your account credentials; see the threat model).
5. **Execution-combination routing** — a task declares requirements, the runtime freezes a decision (`route-<rev>.json`) and later verifies the reported model, not the requested one.
6. **Task-group delivery** — a frozen plan, serial members, one integration branch, group-level checks bound to `integration_sha`, design-conformance review, bounded repair, one `group accept`.

### What 1.x added on top of a single task

| Area | Surface | What is enforced |
|---|---|---|
| Authorization (1.5) | `project attest`, `routing.allowed_profiles`, `project diff` | the allowlist must be explicit; changing any execution field invalidates the attestation; a config change can be previewed before it strands in-flight tasks |
| Routing (1.2) | `route plan`, `route stats` | a pure `decide()`, a frozen record with `content_sha256`, model identity checked after the run; performance is recorded, never used to select |
| Task groups (1.3) | `group plan/create/start/dispatch/status/pause/resume/cancel/accept/report` | one runner per group via lease, serial members, budget gates dispatch only, members stop at `REVIEW_READY` |
| Design review + repair (1.4) | `group review`, `group repair` | a host-produced verdict bound to `(integration_sha, design_digest)`; one FAIL finding per repair, capped by `budget.max_group_repairs` |
| Observation (1.5) | `watch --follow`, `task events`, `task logs --follow`, `progress`, `checks` preflight | bounded, resumable cursors; "no output" is a heartbeat, not proof of progress |
| Preparation (1.4/1.6) | `task prepare`, `project diff` | read-only: "would this create/start succeed, and what would it freeze?" without writing state |

## Install

This repository is the **runner** only. The host skill lives in
[`zoe-workflows`](https://github.com/zoe-runtime/zoe-workflows) and ships
`runtime-requirements.json` — the single anchor between the two.

### 1. As a CLI

```bash
pipx install zoe-runtime        # isolated env, exposes `zoe` on PATH
pipx ensurepath
zoe --version
zoe doctor                   # host diagnosis; creates no state directory

pipx install "zoe-runtime==1.5.0"   # production / reproducible
```

Five supported install paths (pipx, venv + pip, developer checkout, offline wheel,
upgrade/uninstall) are in [docs/installation.md](docs/installation.md); the upgrade
order that does not strand in-flight tasks is in
[docs/upgrading.md](docs/upgrading.md).

### 2. From this checkout (no install)

```bash
PYTHONPATH=. python3 -m src --version
PYTHONPATH=. python3 -m src doctor
```

`python -m src` puts the current directory first on `sys.path`, so run it from the
repository root. Because the package is named `src`, a project that itself contains
an importable `src/` package would shadow it: use the installed `zoe` command there.
The console script imports `src.cli` from `site-packages`, not from the cwd.

### 3. The skill is installed separately

```bash
git clone https://github.com/zoe-runtime/zoe-workflows.git ~/.workbuddy/skills/zoe-workflows
bash ~/.workbuddy/skills/zoe-workflows/scripts/zoe.sh --which
```

The wrapper resolves `ZOE_BIN` → `ZOE_SRC` → `zoe` on PATH, then runs:
`capabilities --check-requirements` before forwarding. `RUNTIME_NOT_FOUND` (exit 126)
and `RUNTIME_INCOMPATIBLE` (exit 5) both mean **the request was not forwarded**. `--which`
and `--version` skip the gate because they are the diagnosis path. Two rules that bite
GUI hosts: `doctor.resolution.path` is the **coding agent's** path, not Zoe's; and a
terminal `command -v zoe` does not prove WorkBuddy/OpenClaw can see it — set an explicit
`ZOE_BIN` and restart the host.

Compatibility policy: [docs/compatibility.md](docs/compatibility.md).

## Quick start

### Simulation — no agent, no state

```bash
zoe demo    # isolated pipeline on test doubles, in a temporary directory
```

`zoe demo` writes nothing to a remote and calls no real agent. It is the fastest way to see
what the controller does.

### Real execution — needs a local CLI, its auth, and a project profile

```bash
# 1. write a project profile (schema v2; see the skill's references/project-profile.example.json)
zoe --home /private/zoe-state project register --config project.json
zoe --home /private/zoe-state doctor --project my-project --json

# 2. dry-run the input: "would this create succeed, and what would it freeze?"
zoe --home /private/zoe-state task prepare --project my-project --spec spec.json

# 3. freeze a spec into a task
zoe --home /private/zoe-state task create --request-file create.json

# 4. if the project declares `routing`, confirm the allowlist once
zoe --home /private/zoe-state project attest --project my-project --dry-run
zoe --home /private/zoe-state project attest --project my-project

# 5. start it (an unverified profile still runs, recorded as unverified unless
#    the project sets require_real_e2e: true)
zoe --home /private/zoe-state task start TASK_ID --request-file start.json

# 6. observe, reconcile, recover
zoe --home /private/zoe-state task status TASK_ID --refresh --json
zoe --home /private/zoe-state task events TASK_ID --json
zoe --home /private/zoe-state task logs TASK_ID --follow --timeout 60
zoe --home /private/zoe-state watch --project my-project --follow --jsonl --timeout 60

# 7. human acceptance
zoe --home /private/zoe-state task accept TASK_ID --request-file accept.json
```

`project.json` uses schema v2 fields (`repo`, `image`, `agent_profiles`, `checks`,
`allowed_paths`, `routing`, …). Copy the skill's `references/project-profile.example.json`;
do not hand-write the legacy v1 shape. Three examples ship with the skill: default (one
model + explicit allowlist), multi-model, and the full advanced field set.

`zoe project diff --config candidate.json` previews what a policy change would do to
in-flight tasks without writing anything; it uses the same spec predicate as `task rebind`.

### Task-group delivery — one plan, one acceptance

```bash
zoe group plan   --project my-project --request-file plan.json   # validate + route preview, writes nothing
zoe group create --request-file plan.json                        # freeze the plan (still dispatches nothing)
zoe group start  GROUP_ID --request-file start.json              # short-lived runner, not a daemon
zoe group status GROUP_ID --refresh --json
zoe group review GROUP_ID --request-file review.json             # required when plan.review.required
zoe group accept GROUP_ID --request-file accept.json
zoe group report GROUP_ID --format markdown --write
```

`group start` returns `accepted: true` / `completed: false`: acceptance is not progress.
When the runner exits, `stop.exit_reason` says why (`completed`, `review_ready`,
`awaiting_design_review`, `budget_exhausted`, `slots`, `blocked`, `no_progress`,
`crashed`, `lease_stale`, `safety_cap`) and `stop.resumable` says whether polling `start` again is
meaningful. Members stop at `REVIEW_READY`; `task accept` on a member is refused
(`kind=group_member`).

## Repository layout

```
zoe-runtime/
├── src/                  # the runtime package; import name is `src` (`src/cli.py`)
├── schemas/              # JSON Schemas for CLI output (capabilities v2, group plan/review, …)
├── tests/                # unit tests + contract drift gate + distribution proof
│   └── contract/         # skill ↔ runtime CLI contract (needs ZOE_SKILL_DIR)
├── docs/                 # install/upgrade/release docs, architecture, threat model, designs
└── .github/workflows/    # ci.yml, contract.yml, release.yml
```

The package directory is `src/` itself, so the import name is `src`
(`import src.cli`, `python -m src`, entry point `src.cli:main`). The distribution
name (`zoe-runtime`), the `zoe` command, task ids and the state directory are
unchanged. State lives under `ZOE_HOME` (default `~/.local/share/zoe`): SQLite
schema v4 plus `tasks/<id>/`, `groups/<gid>/` and `projects/` artefacts.

The host skill is a **different** repository (`zoe-workflows`): `SKILL.md`,
`references/`, `scripts/` and `runtime-requirements.json` live there, and this
repository never carries a copy. Cross-repo tests locate it via `ZOE_SKILL_DIR`.

## How it works

### Runtime shape

```
                 ┌──────────────────────────────┐
   user / host   │  zoe (console entry point)    │
   ────────────▶ │  main(argv) -> int            │
                 └──────────────┬───────────────┘
                                │ task create: route decide + freeze
                                │ task start: fork supervisor
                                ▼
                 ┌──────────────────────────────┐
                 │  zoe internal (supervisor)    │
                 │  maintenance SH + writer EX   │
                 └──────────────┬───────────────┘
                                │ platform spawn (own process tree)
                                ▼
                 ┌──────────────────────────────┐
                 │  agent CLI (worker tree)      │
                 │  POSIX pgid / Windows Job     │
                 └──────────────┬───────────────┘
                                │ exit code + bounded logs + capture.json
                                ▼
                 ┌──────────────────────────────┐
                 │  publisher (host, gh)         │
                 │  commit / push / PR           │
                 └──────────────────────────────┘
```

Groups add one more layer: `group start` spawns a short-lived runner
(`zoe internal-group`) that holds a per-group lease, reconciles members, integrates
validated ones into `refs/heads/zoe/<gid>`, runs group checks on that SHA, and
dispatches the next member only when the budget and the global slot allow it.

### Six phases

| Phase | What happens | Owner |
|---|---|---|
| **prepare** | create an isolated git worktree, record the workspace baseline | controller |
| **implement** | launch the agent CLI in its own process tree | agent (`local-process`) |
| **verify** | re-run checks, compare fingerprints | controller |
| **publish** | push, create or update the PR | controller (`gh`) |
| **ci** | query CI, wait for required checks | controller |
| **acceptance** | human decision | you |

### Task states

```
READY → RUNNING → [PAUSING → PAUSED] → BLOCKED → REVIEW_READY → DONE
           ↓
       CANCELLED
```

| State | Meaning |
|---|---|
| `READY` | spec frozen, not started |
| `RUNNING` | a supervised operation is live (`phase` says which) |
| `PAUSING` | stop requested, not yet confirmed |
| `PAUSED` | stop confirmed, workspace kept |
| `BLOCKED` | environment/spec/process/remote state unknown, or the agent is asking a question |
| `REVIEW_READY` | evidence satisfies the review policy, waiting on a human |
| `DONE` | delivered and accepted |
| `CANCELLED` | no writer confirmed, never auto-resumed |

There is no `DRAFT`: incomplete input is refused with a structured error instead of
being stored as a half-task. Groups reuse the same vocabulary (`READY`, `RUNNING`,
`BLOCKED`, `PAUSED`, `REVIEW_READY`, `DONE`, `CANCELLED`) and add derived member
states (`WAITING`, `READY`, `EXECUTING`, `VALIDATED`, `INTEGRATED`, `INVALIDATED`,
`PAUSED`, `BLOCKED`, `CANCELLED`).

More: [docs/architecture.md](docs/architecture.md).

## Safety invariants

### 1. Single writer

`maintenance.lock` (shared) + `tasks/<id>/.writer.lock` (exclusive), held by the
supervisor for the whole run, plus process identity (PID + start token):

- never starts a second writer while one is alive, and the OS — not a database
  convention — is what enforces it;
- enters `BLOCKED` when the supervisor **or its process tree** cannot be observed,
  instead of restarting blindly;
- detects zombie processes and PID reuse (`/proc` on Linux, `proc_pidinfo` on macOS
  — no external `ps` needed — with `ps -o lstart` as the last-resort fallback;
  `GetProcessTimes` on Windows).

A group is serial by construction: dispatch takes `min(1 - group_busy, global_free)`,
so a free global slot alone never starts a second member.

### 2. Independent verification

- checks are re-run by the controller in a separate environment;
- workspace fingerprints (file list + modes + content hash) are compared before and after;
- the agent's "I'm done" is never evidence;
- a failed verification returns to `implement`;
- when a gate refuses a run, the diff is archived as `runs/<id>/worktree.patch`
  before the tree is restored, and only if `git apply --check --reverse` proves it
  restorable; a workspace already dirty at `start` is never cleaned.

### 3. Controller-owned publication

- the agent cannot commit, push or open a PR;
- every Git write is performed by the controller;
- PRs carry `<!-- zoe-task: TASK_ID -->`;
- timeouts reconcile first — no blind retries, no duplicate PRs;
- group integration is a three-stage ledger (`intent` → `prepared` → `done`) where
  `prepared` is written **before** the ref moves, and files win over rows on recovery.

### 4. Honest acceptance

All of these must hold: a real user decision (`accepted`/`rejected`), a real actor,
a non-empty evidence list, `subject_sha == validated_sha`, and an unchanged workspace
fingerprint. A group member cannot accept itself; `group accept` is the only path, and
it validates the final `integration_sha`.

### 5. Configuration is not authorization (1.5)

- `routing.allowed_profiles` must be explicit whenever a `routing` section exists —
  new profiles in `agent_profiles` are *configured*, not *authorized*;
- `zoe project attest` records `policy_hash` + the confirmed candidate set + runtime
  version in `$ZOE_HOME/projects/<id>.attest.json`; any execution-field change
  invalidates it and the next dispatch returns `ALLOWLIST_UNCONFIRMED`
  (`details.reason` = `missing` / `policy_changed_since_attestation` /
  `candidate_not_confirmed` / `record_invalid`);
- `task rebind` never writes the attestation and neither does re-registering an
  identical config (the digest would not change);
- `zoe project diff` shows which in-flight tasks a new policy would block, read-only,
  through the same spec predicate `rebind` uses.

### What the default backend does and does not contain

`local-process` runs the agent as your user account, in its own process tree:

| Mechanism | Guarantees | Does **not** guarantee |
|---|---|---|
| git worktree + branch | file organisation, task isolation, auditable diff scope | that the agent cannot read/write outside the workspace |
| `allowed_paths` + post-hoc diff | **accountability** (out-of-scope writes block delivery) | **prevention** (the write already happened) |
| env allowlist | which credential *names* are injected | that `~/.ssh`, git helpers or other env are unreadable |
| controller-owned publish | traceable publish record | that the agent cannot push with your credentials |

Process-tree containment is real: POSIX `start_new_session` + `killpg`, Windows Job
Object with `KILL_ON_JOB_CLOSE`, so stopping a run takes the whole subtree (node/python
children included) rather than just the parent.

There is **no container boundary** to rely on: the `docker` backend left the product
scope in v0.5.0. A configuration that still says `backend: docker` is refused with
`CAPABILITY_UNSUPPORTED` (and a migration hint) rather than being silently downgraded
to local execution; historical container runs stay readable but are reported as
`PROCESS_UNKNOWN`, because they can no longer be observed.

### Credentials

The worker process receives `HOME`, `PATH`, `TERM`, the OS plumbing a child needs to
start (e.g. `SYSTEMROOT`, `TEMP`, `PATHEXT` on Windows), `PYTHONUTF8=1`, and exactly the
credentials the selected adapter profile declares — nothing else. Each adapter resolves
its own references
(`model-codebuddy` → `CODEBUDDY_API_KEY`, `model-anthropic` → `ANTHROPIC_API_KEY`,
`model-openai` → `OPENAI_API_KEY`, `model-codex` → `CODEX_API_KEY`), and the accepted
prefixes are per adapter (`CODEBUDDY_`/`ANTHROPIC_`, `OPENCODE_`/`OPENAI_`/`ANTHROPIC_`,
`ANTHROPIC_`/`CLAUDE_`, `CODEX_`); one adapter cannot borrow another vendor's
credentials.

Threat model: [docs/threat-model.md](docs/threat-model.md).

## Known limitations

- **No adapter is `VERIFIED`.** All real local-CLI end-to-end runs are `NOT_RUN`. Runs are therefore labelled `unverified`, and only a project with `require_real_e2e: true` is refused at `start` (`ADAPTER_UNVERIFIED`) — the default is "run, but never call it verified". `qualification run` currently proves only *CLI discoverable* and *version*; auth, non-interactive edit, parse and stop-resume stay `INCOMPLETE` until a real turn runs.
- **Host integration is `NOT_RUN`.** WorkBuddy and OpenClaw both report `real_e2e: NOT_RUN`; only the local CLI path is exercised.
- **`codex` is `FACT_PENDING`.** Contract implemented, command shape not frozen; it cannot be selected and must not be reported as supported.
- **Windows platform layer is unverified on hardware.** The Job Object / `LockFileEx` / `GetProcessTimes` implementation exists and is unit-tested by construction, but no real Windows E2E run has been recorded — treat Windows as `IMPLEMENTED`, not supported.
- **No container isolation.** Local execution only; there is no `--cap-drop`/`--read-only`/`--network none` guarantee for a worker.
- **One writer per task, not per project.** Different tasks may run in parallel up to `max_active_tasks` (1..8, default 1); active runs are counted globally across all projects under one `ZOE_HOME`, while the limit comes from the dispatching project's own configuration. Two tasks on the same project are not parallel-safe if their `allowed_paths` overlap.
- **One wall-clock ceiling per run, project-level only.** `timeout_seconds` is an integer in `60..86400` (default `3600`) and is validated on the write and execution paths (`INPUT_INCOMPLETE`, `field=timeout_seconds`); the read path only records the problem so a stale config can still be inspected with `doctor`, which echoes `run_timeout_seconds` / `run_timeout_declared` / `checks.run_timeout_valid`. There is no per-task override: to give a task longer, split it, or change the project (which changes `policy_digest` — in-flight tasks then need `rebind`).
- **Groups are deliberately narrow.** `local_artifact` delivery only; no group pull requests, no parallel members, no automatic retry, no unbounded dynamic task graph, and no automatic merge into the target branch. Integration goes to `zoe/<gid>` and merging stays a human decision.
- **Group budgets gate dispatch only.** `max_starts` and `max_wall_seconds` stop new starts; reconcile, integration, checks and acceptance keep working afterwards. Task-level autonomy budgets are still recorded, not enforced at `start`.
- **Design review is executed by the host.** `group review` validates a submission (`subject_sha`, `design_digest`, full AC coverage, resolvable evidence) but never runs the reviewer; a `NOT_VERIFIED` verdict reaches `REVIEW_READY` and is listed in the report, never counted as passing.
- **Observation is bounded, not a daemon.** `watch --follow` and `task logs --follow` exit at `--timeout`; continuation is an explicit `--after` / `--cursor`. `progress.process_state=alive` and `observations_stale=true` mean different things, and "no events" is a heartbeat, not proof of progress.
- **No log rotation policy.** Rotation and truncation are *detected* (cursor survives both), but nothing trims files; disk is your responsibility.
- **Drift checks are not exercised against a real remote.** The three drift kinds are covered with local bare repositories; no host with a live GitHub remote has been run.
- **No native session resume.** Workspace-level continuation only; agent conversation context is not preserved.
- **No scheduler or daemon mode.** CI is polled through `status --refresh`; `group start` is a short-lived runner; nothing installs a timer.
- **No hot upgrade.** Stop the supervisor, back up state, then upgrade. Schema changes go through `zoe state migrate` (v1 → v2 → v3 → v4, with a backup).

Capability matrix: [docs/capabilities.md](docs/capabilities.md).

## CLI at a glance

| Area | Commands |
|---|---|
| Host & project | `doctor`, `project register`, `project context`, `project attest`, `project diff`, `capabilities`, `reconcile` |
| Compatibility | `capabilities --check-requirements FILE` (what the skill wrapper runs before forwarding) |
| Agents & routing | `agent list`, `agent probe`, `route plan`, `route stats` |
| Tasks | `task prepare/create/list/status/report/events/logs/start/resume/pause/cancel/revise/accept` |
| Agent Q&A | `task answer` (records the answer; `resume` restarts, `revise` if the AC changed) |
| Execution handover | `task reassign`, `task rebind`, `task recover`, `task close-external`, `task handoff show` |
| Records | `task summary submit/list`, `evidence record`, `qualification run`, `state migrate/restore` |
| Groups | `group plan/create/start/dispatch/status/pause/resume/cancel/accept/review/repair/report` |
| Orchestration | `orchestration show/resume/claim/release/update` |
| Streams & misc | `watch`, `demo` |
| Internal | `internal`, `internal-group` (spawned by `task start` / `group start`; hosts must not call them) |

Exit codes: `0` success, `2` incomplete input / clarification required, `3` state or
request conflict (including `SLOT_BUSY`, `TASK_BUSY`, stale cursors), `4` environment or
capability (`ADAPTER_UNVERIFIED`, `CAPABILITY_UNSUPPORTED`, `PROCESS_UNKNOWN`), `5` a
gate refused (`POLICY_DENIED`, `ALLOWLIST_UNCONFIRMED`, `PERMISSION_PROFILE_UNBOUND`,
integration codes), `6` remote unknown. `task create/start/revise/accept` keep the
historical `4` for policy refusals; the 0.3+ commands use `5`.

The full command surface, request/response shapes and error codes are published with
the skill: [`zoe-workflows/references/cli-contract.md`](https://github.com/zoe-runtime/zoe-workflows/blob/main/references/cli-contract.md).

## Documentation

| Document | Contents |
|---|---|
| [docs/installation.md](docs/installation.md) | five install paths, GUI-host PATH trap, offline install |
| [docs/upgrading.md](docs/upgrading.md) | compatibility window, safe upgrade order, 1.5 authorization migration, uninstall |
| [docs/releasing.md](docs/releasing.md) | TestPyPI → PyPI stages, trusted publishing, version rules |
| [docs/compatibility.md](docs/compatibility.md) | what the runtime gate checks, what CI checks, support matrix |
| [docs/architecture.md](docs/architecture.md) | state machines, module map, routing, group runner, integration ledger, review gates |
| [docs/threat-model.md](docs/threat-model.md) | trust boundaries, mitigations, unmitigated risks |
| [docs/capabilities.md](docs/capabilities.md) | capability × evidence matrix |
| [docs/rollback.md](docs/rollback.md) | rollback preconditions, state recovery, remote reconciliation |
| [docs/adapter-qualification.md](docs/adapter-qualification.md) | the four qualification gates and what each one does **not** prove |
| [docs/agent-routing-design.md](docs/agent-routing-design.md) | profile routing and the authorization model |
| [docs/execution-combination-routing-design.md](docs/execution-combination-routing-design.md) | L1/L2/L3 layers, `decide()`, model identity, spend guard |
| [docs/model-selection-authorization-design.md](docs/model-selection-authorization-design.md) | allowlist, attestation and the confirmation chain |
| [docs/task-group-delivery-design.md](docs/task-group-delivery-design.md) | group plan, runner, integration and acceptance semantics |
| [docs/Zoe-Task-Group-Release-Design-v1.md](docs/Zoe-Task-Group-Release-Design-v1.md) | the frozen group release design record |
| [docs/compatibility/README.md](docs/compatibility/README.md) | the precise-version evidence records behind `VERIFIED` |

## Security statement

`authorization_ref` is a self-declared string, **not** an authentication system. The
`project attest` record proves "this configuration was confirmed once"; it does not
record who wrote the allowlist and does not resist a process with write access to the
same home directory. See [docs/threat-model.md](docs/threat-model.md) and
[SECURITY.md](SECURITY.md).

## License

Apache-2.0 — see [LICENSE](LICENSE).

---

*Chinese version: [README.zh-CN.md](README.zh-CN.md).*
