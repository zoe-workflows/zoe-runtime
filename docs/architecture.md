# Architecture

## Overview

Zoe Runtime is a single-machine, single-user controller. It does not run a
long-lived daemon: every command starts a short-lived process, and `task start`
forks a one-shot supervisor that lives only for the duration of that run.

```
                 ┌──────────────────────────────┐
   user / host   │  zoe (console entry point)    │
   ────────────▶ │  main(argv) -> int            │
                 └──────────────┬───────────────┘
                                │ fork supervisor
                                ▼
                 ┌──────────────────────────────┐
                 │  zoe internal (supervisor)     │
                 │  holds maintenance SH          │
                 │      + tasks/<id>/writer EX    │
                 └──────────────┬───────────────┘
                                │ resolve profile -> LaunchSpec
                                ▼
                 ┌──────────────────────────────┐
                 │  adapter registry              │
                 │  builtin + extended, gated     │
                 └──────────────┬───────────────┘
                                │ platform.spawn (own tree)
                                ▼
                 ┌──────────────────────────────┐
                 │  agent process tree            │
                 │  session (POSIX) / Job (Win)   │
                 └──────────────┬───────────────┘
                                │ bounded log + capture.json
                                ▼
                 ┌──────────────────────────────┐
                 │  adapter.parse_output          │
                 │  -> AgentResult (outcome)      │
                 └──────────────┬───────────────┘
                                │ verify -> evidence
                                ▼
                 ┌──────────────────────────────┐
                 │  publisher (host, gh)          │
                 │  commit / push / PR            │
                 └──────────────────────────────┘
```

The registry can hold N adapters, but the runtime still has **one** execution
slot. Adding an adapter never grants a second concurrent writer.

## Module map

| Module | Responsibility |
|---|---|
| `src/cli.py` | argument parsing, dispatch, unified error → JSON, exit codes |
| `src/_util.py` | errors, hashing, atomic writes, subprocess/git helpers, redaction |
| `src/_proc.py` | process identity (PID-reuse guard), shim over the platform layer |
| `src/_platform/base.py` | platform layer interface (locks, identity, tree stop/spawn) |
| `src/_platform/posix.py` | macOS/Linux: `flock`, `ps`, process group tree stop |
| `src/_platform/windows.py` | Windows: `LockFileEx`, `GetProcessTimes`, Job Object |
| `src/_config.py` | project configuration, policy checks, `doctor` readiness |
| `src/_paths.py` | one path semantics for config, scope checks and prepare: normalization, component boundaries, platform case rules, symlink-aware equality, git root discovery |
| `src/_agents/base.py` | `AgentAdapter` contract, `LaunchSpec`, `RunContext`, `AgentResult` |
| `src/_agents/registry.py` | static registry, three-state capability per `(adapter, backend)` |
| `src/_agents/codebuddy.py` | built-in CodeBuddy adapter (final JSON) |
| `src/_agents/opencode.py` | built-in OpenCode adapter (event stream) |
| `src/_agents/claude_code.py` | built-in Claude Code adapter (final JSON) |
| `src/_agents/codex.py` | extended Codex adapter (`FACT_PENDING`, no command form) |
| `src/_profiles.py` | config normalization, profile binding, compatibility evidence |
| `src/_routing.py` | execution-combination routing: requirements, snapshot, pure `decide()`, `combination_id`, model identity, spend guard, attribution, read-only stats |
| `src/_diff.py` | configuration-change preview (`project diff`): field-level diff, both policy digests, which in-flight tasks a new policy would block, and `spec_valid_under_new_policy` validated through the same `validate_spec_text` that `rebind` calls; read-only |
| `src/_worktree.py` | workspace baseline at `start` and patch-first quarantine when a gate refuses a run |
| `src/_contracts.py` | request/spec validation (spec v1 text, spec v2/v3 JSON); v3 adds `acceptance_review`, `decisions`, `progress_refs`, and manual-AC `judge`/`supporting_checks` |
| `src/_groups.py` | task groups: frozen v3 plans (`resolve_plan_inputs` + pure `validate_plan` + `plan_digest`), legacy v2 ordered records, derived member/group state |
| `src/_group_runner.py` | group runner: lease handshake, `tick`, budget/slot dispatch gate, single group admission, pause/resume/cancel |
| `src/_integration.py` | serial integration (intent → prepared → done ledger), group checks bound to `subject_sha`, group acceptance, delivery report |
| `src/_review.py` | design-conformance review contract: subject/design binding, full AC coverage, resolvable evidence, verdict, and the `REVIEW_READY`/`accept` gate |
| `src/_task_review.py` | per-AC acceptance verdict (v1.7 second layer): `validated_sha` binding, full AC coverage, resolvable evidence (`check:`/`evidence:`/`doc:`/`decision:`), reviewer independence, and the `accept` gate |
| `src/_coverage.py` | requirement coverage ledger (`task coverage`): markers → tasks → state/acceptance/review, in five buckets; `cleared` is the only readable "list is empty" answer |
| `src/_evidence.py` | delivery evidence build / persist / re-check |
| `src/_store.py` | SQLite store (schema v4, events, groups, meta, orchestration); event ownership + filtered paging that advances over scanned rows |
| `src/_prepare.py` | read-only task preparation: "can this be created/started, and what would it freeze?", never constructs a `Store`, never touches the task library |
| `src/_logstream.py` | incremental, resumable log reading (`run:generation:offset` cursor), rotation/truncation detection, per-line `stream-json` projection |
| `src/_progress.py` | run observation facts (`progress.json`): `observed_at` / `last_output_at` / `quiet_seconds` / `process_state`; host-level thresholds, never project policy |
| `src/_quota.py` | quota exhaustion: banner classification, reset-time extraction, and the project-level dispatch gate (`project quota show` / `clear`); a local file, deliberately outside `policy_digest` |
| `src/_digest.py` | multi-task fact aggregation for `task digest`: per-task status + newest text/tool, shared-path counts; facts only — no verdict, no state mutation |
| `src/_migrations.py` | explicit v1 → v2 → v3 → v4 migration with backup and locks |
| `src/_backend.py` | LocalProcess lifecycle (the historical container class is kept but unreachable), scope, fingerprint |
| `src/_discovery.py` | CLI resolution order, bounded login-shell fallback, `argv[0]` pinning |
| `src/_drift.py` | pre-publication remote drift classification and its evidence |
| `src/_baseline.py` | pre-`REVIEW_READY` baseline facts for parallel batches: `behind_by`, `overlapping_paths`, `reverts_merged_work`; advisory only (no state change), never raises, distinct from the `BASE_DRIFT` refusal |
| `src/_config_source.py` | where the registered project config came from: sidecar `projects/<id>.source.json` (outside `policy_digest`) answering "the file on disk changed but was never re-registered", surfaced by `doctor.config_source` |
| `src/_publisher.py` | Git/PR publication |
| `src/_pipeline.py` | task lifecycle orchestration |
| `src/_orchestration.py` | orchestration record (goal/dispatch/decisions/ownership) |
| `src/_summaries.py` | versioned human-readable summaries and `report.md` |
| `src/_reports.py` | delivery report (`delivery-<rev>.md`): AC verdicts, check detail, boundaries |
| `src/_requirements.py` | skill compatibility gate (`capabilities --check-requirements`) |
| `src/_context.py` | project context bundle (project isolation) |
| `src/_qualification.py` | local CLI qualification (six checks, evidence) |
| `src/demo.py` | isolated simulated pipeline (test doubles only) |

`core.py` re-exports the public surface for backward compatibility only; new code
imports the focused module it needs.

## Execution-combination routing (v1.2)

```text
task create ──▶ requirements (route.requirements + pin default by cost_class)
            ──▶ snapshot      (project checks once, doctor per candidate, bounded ≤5s)
            ──▶ decide()      (PURE: same inputs → same output, table-testable)
            ──▶ freeze        (route-<rev>.json with content_sha256; task references it)
task start  ──▶ verify record (R9) ──▶ workspace_baseline ──▶ spawn supervisor
run end     ──▶ parse layer  (model identity / spend / failure class — always)
            ──▶ gate A       (pin_model ⇒ must be reported_match)
            ──▶ gate B       (spend guard, evaluated after recording)
            ──▶ verify ──▶ evidence
```

Three layers, strictly separated: **L1 declared capability** (mechanical, can only
exclude), **L2 integration fact** (can this host run it), **L3 task performance**
(recorded, never used to select — `layers` has no `l3_excluded` key by construction).

Two invariants matter more than any feature here:

1. **Not declared ≠ matched.** A tool that says nothing about its model yields
   `unobserved`; under `pin_model` that blocks, exactly like a mismatch.
2. **Declared ⇒ effective, or refuse.** A declared `provider`/`model_variant`/
   `reasoning_effort` that no adapter can turn into a real launch configuration is
   refused at registration (`CAPABILITY_UNSUPPORTED`) instead of being dropped
   from the command line.

When a gate refuses a run, the workspace is quarantined: the diff is archived as
`runs/<id>/worktree.patch` and only then is the tree restored — and only if
`git apply --check --reverse` proves the patch is restorable. A workspace that was
already dirty at `start` is never cleaned.

## Task-group delivery (v1.3; design review + bounded repair in v1.4)

A group is a **frozen plan plus one managed process**. It is not a scheduler
service: `group start` spawns a short-lived runner and returns, and when that
process exits, progress stops. There is no daemon, no timer and no service unit.

**Why the runner stops is a contract, not an implementation detail** (v1.4.1).
The loop exits on: a terminal state, an explicit pause, a real blocker, the
declared budget, a lost lease, or a round with nothing to advance. A tick count
is *not* an exit condition — `SAFETY_MAX_TICKS` is an emergency net for a wedged
tick, and hitting it is reported as `exit_reason=safety_cap` rather than ending
silently. Every stop is named:

| `exit_reason` | Meaning | Resumable |
|---|---|---|
| `completed` / `cancelled` / `paused` | terminal or explicitly stopped | no |
| `review_ready` | checks and review passed, waiting for `group accept` | yes (nothing to run) |
| `awaiting_design_review` / `review_failed` | the host has not submitted (or failed) the design review | yes, after the host acts |
| `budget_exhausted` | starts spent; reconcile/integrate/accept still work | yes, after a plan revision |
| `slots` | the global slot belongs to another task | yes |
| `blocked` | a real blocker needs a human decision (`stop.needs_human`) | no |
| `no_progress` | nothing advanced this round; work is **unfinished** | yes |
| `crashed` | the runner died on an unexpected error (`stop.needs_human`) | no — read the logs first |
| `lease_stale` | another runner took over; this process must not write | no |
| `safety_cap` | the emergency tick bound was hit; work is **unfinished** | yes |

The same value is persisted as `runtime.lease.exit_reason` and surfaced by
`group status`, so a host that restarts later can still explain the stop.

```text
host ──▶ group start GID ──▶ preflight ──▶ spawn `zoe internal-group GID LEASE_ID`
                                     └─▶ return accepted/completed=false

runner (one per group)                  # holds maintenance SH + groups/<gid>/.runner EX
  tick:  A reconcile  (members + integration ledgers)     always runs
         B integrate  (VALIDATED members, topological)    always runs
         C group checks (bound to integration_sha)        always runs
         D dispatch   (READY members)   ← the only budget/slot-gated step
         E derive state + write progress view
        │
        └─▶ `_pipeline.start` ──▶ task supervisor ──▶ agent process tree
                                  (still exactly one writer per task)
```

Three load-bearing invariants:

1. **A lease, not an epoch.** Each start mints a fresh `lease_id`; a runner whose
   `runtime.lease.lease_id` no longer matches its own refuses to write
   (`lease_stale`). `meta.epoch` only changes on migration/restore, so it cannot
   tell two take-overs in one database lifetime apart.
2. **Serial per group.** Dispatch takes `min(1 - group_busy, global_free)`, so a
   free global slot alone can never start a second member while the first is
   still executing.
3. **Budget gates dispatch only.** Reconcile, integration, group checks and human
   acceptance keep working after `max_starts` is reached — otherwise the last
   allowed start could never be integrated or accepted.

### Artefacts

`ZOE_HOME/groups/<gid>/` mirrors the `tasks/<id>/` layout (permissions, backup
and cleanup policies are reused as-is):

```text
plan-r001.json            frozen definition + plan_digest (immutable projection)
baseline.json             environment facts at creation
design-manifest.json      design files + per-file sha256
specs/<task_key>.json     frozen member spec snapshot (repo edits do not leak in)
routes/<task_key>-a001.json   route rehearsal; not an admission
integration/<op_id>.json  three-stage ledger: intent → prepared → done
integration/<op_id>/wt    one worktree per attempt
integration/<op_id>/conflict   conflict diagnostics (integration_sha unchanged)
checks/<subject_sha>.json group check results for exactly one SHA
reviews/<subject_sha>.json design-conformance review for exactly one SHA (v1.4)
reports/{progress,delivery}.md  regenerable views (DB/JSON is authoritative)
```

### Integration ledger

Git effects cannot join a SQLite transaction, so the bookkeeping is explicit:

```text
[1] intent    merge candidate in its own worktree (`--no-ff`, never a patch)
[2] prepared  result_sha + quick checks written to disk   ← MUST precede the ref
[3] ref       update-ref refs/heads/zoe/<gid> <result_sha> <base_sha>
[4] done      integration_sha, member.integrated_sha, op status, event
```

`prepared` before the ref is the whole point: a crash after the ref moves is
recoverable from the file, while the reverse order leaves a moved branch that no
record describes. Reconciliation follows the observed ref:

| ref | ledger | outcome |
|---|---|---|
| `== result_sha` | `prepared` | commit `done`, never merge again |
| `== result_sha` | `intent` | `INTEGRATION_UNKNOWN` (impossible by construction) |
| `== base_sha` | `prepared` | replay the idempotent `update-ref`, then `done` |
| `== base_sha` | `intent` | replay once in a fresh worktree, same `op_id` |
| other | any | `INTEGRATION_UNKNOWN` — a human decides |
| candidate reachable | `intent` | adopt the identifiable merge commit, else block |

Files win over rows: a file at `prepared` with a row at `intent` is repaired from
the file; a row at `done` with no file blocks instead of pretending.

An **integrated** member is frozen: `revise` / `reassign` / `rebind` are refused
(`kind=integrated_member`). Changing something already inside `integration_sha`
means a new linked group (`follow_up_of`), so the integration history stays true.

### Design-conformance review (v1.4)

`checks` answers "do the commands pass"; it cannot answer "was the design
implemented". The second layer is a host-produced artefact validated by
`_review`:

```text
group start … → integration_sha ──▶ host runs a reviewer in a separate context
                                    │
                                    └─▶ group review --request-file review.json
                                          ├─ subject_sha == integration_sha ?    no → invalid
                                          ├─ design_digest == frozen digest ?    no → invalid
                                          ├─ every required AC present once ?    no → invalid
                                          └─ every evidence ref resolves ?       no → invalid
                                                │
                        PASS / NOT_VERIFIED ────┴──── FAIL
                                │                        │
                        REVIEW_READY            back to RUNNING
                                             (+ `group repair`, capped)
```

The runtime never runs the reviewer, and an invalid submission is recorded as
`invalid` in `runtime.reviews[]` and rejected — it never becomes a partial pass.
`NOT_VERIFIED` may reach `REVIEW_READY` but is listed in the delivery report and
never counted as passing. Verdicts are bound to one `(subject_sha, design_digest)`
pair: a new integration invalidates both checks and review.

### Bounded repair (v1.4)

A repair is one unit that fixes **one** `FAIL` finding, capped by
`budget.max_group_repairs` (default `0` — no authorisation, not unlimited). It is
appended to `runtime.repairs`, never to `definition`: inserting a member into a
frozen plan would invalidate `plan_digest`. Repair units reuse the member code
path (`dispatch`/`integrate`/`members_view`) and are ordered after every frozen
member, because they fix what only integration could reveal. A change without a
finding is a new requirement, not a repair — it goes through plan revision.

## Adapter responsibilities

| Layer | Responsible for | Not responsible for |
|---|---|---|
| Registry | adapter id, kind, capability, `(adapter, backend)` granularity | starting processes, locks, publishing |
| AgentAdapter | profile validation, `LaunchSpec`, output parsing | process lifecycle, task DB, host mounts |
| Backend | hardening, mounts, network, resource limits, bounded output | vendor protocol parsing, business success |
| Controller | state machine, gating, evidence, Git/PR | deciding what the customer wants |

`LaunchSpec` cannot carry host mounts: only container-side `writable_dirs` are
accepted, and the workspace root stays read-only to the adapter. The frozen
prompt reaches the agent through a read-only mount consumed as stdin, so prompt
text is never an argv element.

## State machine

| State | Meaning |
|---|---|
| `READY` | frozen spec, not yet running |
| `RUNNING` | a supervised operation is active (`phase` says which) |
| `PAUSING` | stop requested, not yet confirmed |
| `PAUSED` | confirmed stopped, work retained |
| `BLOCKED` | environment / requirement / process / remote / adapter unknown |
| `REVIEW_READY` | evidence satisfies review policy, awaiting human |
| `DONE` | delivered and accepted |
| `CANCELLED` | confirmed no writer, no auto-resume |

`phase` is one of `prepare / implement / verify / publish / ci / acceptance`.
A task is never created in an ad-hoc `DRAFT` state; incomplete input is rejected
with a structured error instead.

A group reuses the same vocabulary, and has no `DRAFT` either: `group plan` is a
stateless preview.

| Group state | Meaning | Allowed next |
|---|---|---|
| `READY` | plan frozen, no runner yet | `RUNNING` / `CANCELLED` |
| `RUNNING` | a runner exists and there is something to advance | `BLOCKED` / `PAUSED` / `REVIEW_READY` / `CANCELLED` |
| `BLOCKED` | nothing can advance and a blocker is named | `RUNNING` / `PAUSED` / `CANCELLED` |
| `PAUSED` | no new dispatch; nothing is executing | `RUNNING` / `CANCELLED` |
| `REVIEW_READY` | every member integrated and the required checks passed on that SHA | `DONE` / `RUNNING` / `CANCELLED` |
| `DONE` | accepted on the final `integration_sha` (`local_artifact` only) | terminal |
| `CANCELLED` | terminated, optionally via external close | terminal |

A pause **drains**: `group pause` writes `runtime.draining`, dispatch is refused
(`admission`) while the active member runs to a safe point, and the group becomes
`PAUSED` only when nothing is executing. `draining` is cleared by `group resume`
and by nothing else — a pause that quietly resumed would be worse than no pause.
The flag is read by `dispatch_gate` and by the admission gate, so the host cannot
start the next member in the window between "the last run exited" and "the next
tick completed".

A partially blocked group is **not** blocked: as long as one independent member
can advance, it stays `RUNNING` and `progress.blocked` carries the count. Member
state is derived, never stored: only `task_id`, `validated_sha`,
`integrated_sha`, `invalidated` and `last_block` persist. A `CANCELLED` member
does not satisfy a dependency, so its downstream stays `WAITING` and the group
eventually reports `BLOCKED` instead of silently completing.

## Readiness (doctor)

`zoe doctor --project P [--agent-profile X]` reports independent dimensions:

| Field | Meaning |
|---|---|
| `runtime_ready` | git + the platform layer (lock/identity/tree); docker is never an input |
| `policy_ready` | configuration validates and profiles resolve |
| `adapter_approved` | selected `(adapter, backend, version)` is `VERIFIED` |
| `adapter_probe_passed` | always `null` here — probing is a separate explicit command |
| `real_e2e_status` | `PASS` / `DECLARED` / `NOT_RUN` from the verification source |
| `credential_present` | whether the profile's env names exist (values never shown) |
| `resolution` | the CLI binary this project would launch (`path`/`source`/`version`/`notes`) |
| `resolution_notes` | why that path was chosen, and which other versions were found |

`ready` requires `runtime_ready` (including `adapter_approved`) and process
identity, so a host check passing never implies the adapter is usable.

## Reconcile decision tree

On `status --refresh` or `reconcile`, the controller resolves an unknown
writer state before allowing any new launch:

```
run.status == exited?
 ├─ yes → (stop_requested? → PAUSED/CANCELLED : keep)
 └─ no  → supervisor alive with matching identity?
          ├─ yes → keep RUNNING, observe
          ├─ no, dispatch_state unconfirmed (spawn_intent/spawn_observed)
          │       → BLOCKED/PROCESS_UNKNOWN
          ├─ no, process tree still alive → BLOCKED/PROCESS_UNKNOWN
          ├─ identity/tree unverifiable → BLOCKED/PROCESS_UNKNOWN
          ├─ run has a container label (historical docker run)
          │       → BLOCKED/PROCESS_UNKNOWN (never auto-closed)
          └─ tree gone AND writer lock acquirable → mark exited (interrupted)
```

A run's `container` field predates the local backend: local runs write
`local-<run>-<log>` into it too. Only a non-`local-` label denotes container
execution (`_is_container_run`). Since v0.5.0 the container backend is out of the
product scope, so such a run can no longer be observed at all — it is reported
as uncertain and is closed out only by `task recover --confirm-stop`.

## Publication drift decision tree

Run at the start of the `publish` phase, before anything is pushed (§7). The
probe never raises: a classification is always available, so "persist the
evidence, then block" cannot be skipped by an exception.

```
fetch <remote> refs/heads/<target>            (bounded, read through FETCH_HEAD)
 ├─ fetch failed / unparsable               → remote_unknown  (REMOTE_UNKNOWN)
 └─ FETCH_HEAD == base_sha                  → continue
     ├─ base_sha is ancestor of FETCH_HEAD  → base_advanced   (REMOTE_DRIFT)
     ├─ exit 1 (not an ancestor)            → base_rewritten  (REMOTE_DRIFT)
     └─ any other exit                      → remote_unknown  (REMOTE_UNKNOWN)
ls-remote <remote> refs/heads/<task branch>
 ├─ no ref and no publication history       → none
 ├─ no ref but history exists               → candidate_diverged + task_ref_missing
 ├─ ref == candidate_sha or published_sha   → none
 └─ ref is anything else                    → candidate_diverged (REMOTE_DRIFT)
```

`remote_unknown` is deliberately separate from the three confirmed drifts:
"could not check" is neither "clean" nor "changed". `local_artifact` tasks never
reach this stage (verify sends them straight to acceptance), so the check cannot
turn a local delivery into a network-dependent one.

## Terminology

One concept, one word, in code and in the documents that describe it:

| Concept | Canonical term | Also seen as | Notes |
|---|---|---|---|
| Work item | task | — | the unit a human accepts |
| One supervised execution of a task | run | attempt | `attempt_id`/`attempts` remain for retry accounting only |
| The OS process doing the work | process | worker | supervisor + agent tree, never a "session" |
| Bound agent CLI profile | profile | binding | `profile_id` is the stable key |
| Where the process runs | execution backend | backend | v0.5.0: `local-process` only |

The words "attempt" and "backend" survive where they are persisted field names;
they do not introduce a second concept.

The process tree is identified by a persisted token, never by in-memory state:
the pgid on POSIX, the **job name** on Windows (a HANDLE would only be valid in
the creating process, so a reconciling process could never observe it).

The invariant: **never start a second writer while the first is alive or
unaccounted for.** It is enforced by the OS, not by a convention: the supervisor
holds `tasks/<id>/writer` EX for the run's whole lifetime, and `reconcile` only
closes a run out once that lock is acquirable.

## Launch handshake

1. `start` resolves the profile and resolves a capability for
   `(adapter, backend, execution_backend, platform, version, runtime_digest)`.
   A capability state is a **label, not an admission ticket**: an `IMPLEMENTED`
   backend may still start, and the run is recorded as `unverified` /
   `trust_level`; only a project that declares `require_real_e2e: true` turns
   "no real e2e record" into a refusal (`ADAPTER_UNVERIFIED`).
   `ADAPTER_VERSION_MISMATCH` / `ADAPTER_BACKEND_MISMATCH` always refuse and
   write nothing. Records that pin a `runtime_digest` are additionally rejected
   when the platform or the CLI fingerprint changed, or when the qualification
   checks are incomplete.
2. `start` takes `maintenance` SH, then the task's `writer` EX (non-blocking;
   `TASK_BUSY` if held), then the `control` lock — the 01 §5.2 order, so it
   cannot deadlock against `state migrate` (maintenance EX → control).
3. Under `control`, it reserves the run (`dispatch_state=spawn_intent`, recording
   adapter, backend, profile hash, assignment revision, `attempt_id`,
   `lock_owner`) and writes `state=RUNNING` in one transaction.
4. It spawns the supervisor and re-reads the record to confirm the pid + identity
   the child must adopt (`dispatch_state=spawn_observed`), then releases all three
   locks. The supervisor is spawned *without* its own tree/job so it can outlive
   `start`.
5. The supervisor re-acquires `maintenance` SH + `writer` EX for the whole run,
   runs the CAS handshake (`attempt_id` + `lock_owner` + pid identity), then
   launches the agent into its own tree and persists `group_token`. On exit it
   stops the tree *before* releasing the locks.

## Handover

`task reassign` is the only way to change the implementing agent. It requires a
confirmed stop, a `VERIFIED` target profile, an unchanged policy digest and a
matching handoff fingerprint; it archives an assignment record, preserves the
retry budget, and invalidates candidate/validation/acceptance evidence. There is
no automatic failover.
