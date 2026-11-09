# Capabilities

Each capability has an **implementation status** and a **verification evidence**
level. They are deliberately separate: a feature can be implemented but not yet
proven end-to-end, and a simulated test never counts as real verification.

Implementation status: `implemented` / `unsupported`.
Evidence level: `unit` / `simulated-integration` / `real-e2e` / `not-run`.

## Agent adapters (v0.4.1)

Capability is decided per **`(adapter_id, backend)`**, never per adapter alone:

| State | Meaning | Default policy | `require_real_e2e: true` |
|---|---|---|---|
| `IMPLEMENTED` | adapter code exists, no real end-to-end record for this backend | runs, labelled `unverified` | `ADAPTER_UNVERIFIED` |
| `UNVERIFIED` | a record exists, but the current version/backend differs from it | runs, labelled `unverified` | `ADAPTER_VERSION_MISMATCH` |
| `VERIFIED(version, backend)` | this exact version completed a real e2e for this backend and the profile's evidence matches | runs, `trust_level=verified` | runs |

The state is a **label, not an admission ticket** (v0.4.1). `start` records
`capability`, `unverified`, `trust_level` and `verification_source` in the run
record and the response, emits `unverified_dispatch` as the first event when the
backend has no real-e2e record, and `task report` prints a `Trust:` line. A
project that must not tolerate that sets `require_real_e2e: true` and gets the
old refusal back. `doctor` separates `ready` (environment) from `trusted`
(real-e2e record).

Evidence binding is configurable via `runtime_binding` (default `strict`): a record
pinned to a `runtime_digest` only counts on the host/binary that produced it. With
`runtime_binding: "warn"` a version match is accepted across fingerprints and the
reason string says so.

`zoe capabilities --json` and `zoe agent list --project P --json` report the
state per backend. There is **no** shipped `VERIFIED` record in this build:

| Adapter | Kind | Backends | Shipped state | Evidence |
|---|---|---|---|---|
| `codebuddy` | builtin | `codebuddy-cli` | `IMPLEMENTED` | adapter/parser unit tests + `qualification run` with a stub CLI; **real-e2e: NOT_RUN** — see [adapter-qualification.md](adapter-qualification.md) |
| `opencode` | builtin | `opencode-cli` | `IMPLEMENTED` | event-stream parser unit tests; **real-e2e: NOT_RUN** |
| `claude-code` | builtin | `anthropic-api` | `IMPLEMENTED` | final-result parser unit tests; **real-e2e: NOT_RUN** |
| `codex` | extended | `backend-pending` (placeholder) | `IMPLEMENTED` (capped), `facts_status=FACT_PENDING` | contract tests + synthetic fixtures; no frozen command form |

`FACT_PENDING` is a hard cap, not a label: `registry.AdapterEntry.capability()`
returns `IMPLEMENTED` for every backend of such an adapter **even when an operator
supplies a `compatibility_evidence` record**, because no command form is frozen and
`VERIFIED` would advertise a backend that cannot launch. The backend identifier
`backend-pending` is a placeholder — M3x replaces it with the verified enum, and
this build deliberately names no unverified backend (design §9.2, §10.4).

### Permission profiles are audited, not assumed

A `permission_profile` name is validated as an enum *and* reported. An adapter that
cannot bind the name says so instead of implying it can:

```json
{"profile": "isolated-edit", "argv": [], "enforced": false}
```

`enforced: false` means the name is configuration only — this build ships no frozen
argv that proves the agent's tool approvals are configured. It appears in the run
record, `capture.json`, `zoe agent list` and `doctor`, so a "can answer but cannot
edit files" run is diagnosable instead of mysterious. A project that will not
tolerate that sets `require_permission_binding: true` on the profile and gets
`PERMISSION_PROFILE_UNBOUND` (exit 5) at `start` /`reassign` instead of a late
business failure.

`codebuddy` and `claude-code` both ship a frozen binding for `isolated-edit`
(`--permission-mode acceptEdits`), so their `enforced` is `true` from argv rather
than from configuration; `opencode` has no such switch and stays `enforced: false`.
An adapter that declares `permission_switch: true` must bind **every** profile it
declares — otherwise the declared profile is one that can only fail
(`tests/test_agents.py` asserts this). `approved_tools` for `claude-code` is an
additional enforcement path and is reported as `enforced: true` on its own.

An operator unlocks a backend by adding a `compatibility_evidence` file to the
profile (`docs/compatibility/`, schema `schemas/compat-evidence.v1.json`) whose
record matches the adapter, the backend and the **exact** `expected_version`.
`source` is preserved in `agent list`: `real_e2e` means a real run,
`local_admin` means an administrator declaration. A v0.2 configuration with
`"compatibility_verified": true` is migrated to a `legacy-<agent>` profile whose
record is labelled `legacy_admin_declaration`; it is not described as real e2e.

## Controller capabilities

| Capability | Implementation | Evidence |
|---|---|---|
| SQLite task records (WAL, busy timeout, schema v2) | implemented | real SQLite tests (`test_runtime.py`, `test_v030.py`) |
| Explicit v1 → v2 state migration (backup, locks, integrity check) | implemented | `test_v030.py::TestMigration` |
| Request dedup / idempotent create | implemented | `test_create_idempotency` |
| Dispatch replay / `dispatch_pending` / `TASK_BUSY` details | implemented | `test_v051_replay.py` (including a two-process barrier) |
| Optimistic concurrency (row version) | implemented | `test_optimistic_conflict` |
| Worktree + branch isolation | implemented | simulated-integration |
| Single writer per task (lock + identity) | implemented | `test_slot_lock`, `test_active_identity_not_restarted` |
| Cross-task concurrency limit (`max_active_tasks` 1..8, global count, project-scoped limit) | implemented | `test_v041_admission.py`, `test_v040_locks.py`, `test_v051_replay.py` |
| Concurrency snapshot (`capabilities.concurrency`, `slots` in `resume`/`group plan`) | implemented | `test_doc_contracts.py` — snapshot only, never an admission verdict |
| Crash reconciliation (orphan writer) | implemented | `test_orphan_container_blocks`, `test_crash_no_writer_reconciles` |
| Adapter registry + capability gating | implemented | `test_agents.py`, `test_v030.py::TestAdapterGate` |
| Adapter output protocols (final JSON, event stream, bounded output) | implemented | `test_agents.py` |
| Delivery evidence (spec + candidate + manifest + check policy) | implemented | `test_v030.py::TestEvidence` |
| Explicit verification scope (`verification_scope`, `NOT_RUN`, frozen semantics marker) | implemented | `test_v051_scope.py` |
| Scope binding in evidence (`selected`/`not_run`/`verification_semantics`) | implemented | `test_v051_scope.py::TestEvidenceBinding` |
| Delivery report (`task report --format markdown` / `--write`) | implemented | `test_v051_report.py` |
| Cross-adapter handover (`task reassign`) | implemented | `test_v030.py::TestReassign` |
| Policy rebind (`task rebind`) | implemented | `test_v030.py::TestRebind` |
| v0.2 frozen v1 task resumption (config normalized on read) | implemented | `test_v030.py::TestLegacyTaskResume` |
| Permission-profile binding audit (`enforced` flag, opt-in fail-closed) | implemented | `test_agents.py::TestPermissionBinding` |
| Per-`(task, run, adapter, backend)` session directory, containment-checked | implemented | `test_v030.py::TestSessionDir` |
| Adapter fixtures (`tests/fixtures/agents/`) | implemented | `test_contract_artifacts.py::TestAdapterFixtures` — **synthetic, not real captures** |
| `qualification run` six checks (two real runs: implement + stop/resume) | implemented | `test_v041_qualification.py` (stub CLI) — **real-e2e: NOT_RUN** on any target host |
| Real end-to-end harness (`tests/integration/`) | implemented (skipped) | **real-e2e: NOT_RUN** (`ZOE_REAL_E2E=1` to enable) |
| Ordered task groups (records only, no scheduler) | implemented | `test_v030.py::TestGroups` |
| Task-group delivery: frozen plan + dependency graph | implemented | T02/T06/T09/T21/T29/T31 in `test_v130_group_plan.py` — `plan_digest` covers the immutable projection only |
| Legacy ordered groups stay readable, never runnable | implemented | `test_v130_legacy_groups.py` — every runner command refuses with `kind=legacy_group` |
| Group member branch (`group_member`, frozen once integrated) | implemented | T28 in `test_v130_group_member.py` — `revise`/`reassign`/`rebind` all refuse |
| Group runner (single managed process, lease CAS, `tick`) | implemented | T01/T07/T08/T13/T19/T22/T25/T26 in `test_v130_runner.py` — process is short-lived, not a service |
| Drain pause (`group pause` stops the next dispatch, not the active one) | implemented | `test_v130_runner.py::TestPauseHonesty` — `draining` gates admission, `PAUSED` only at the safe point |
| Serial group integration + three-stage ledger | implemented | T10/T17/T27 in `test_v130_integration.py`; end-to-end in `test_v130_group_delivery.py::TestEndToEndDelivery` — real git, local only |
| Group checks bound to `subject_sha` + one-time `group accept` | implemented | T18/T20/T30 in `test_v130_group_accept.py`; `test_v130_group_delivery.py::TestAcceptanceGates` |
| `auth_mode` declaration (`cli_login` / `api_key` / `unknown`) | implemented | `test_v130_prereq.py::TestAuthMode*`, `test_requirements.py` — `login_state` needs observed evidence, never a guess |
| Check `error_class` (`test` / `environment` / `timeout` / `policy`) | implemented | T14 in `test_v130_prereq.py`; `test_v041_extras.py::TestHandoffFailedChecks` |
| Group delivery over `pull_request` | unsupported | refused at creation: `CAPABILITY_UNSUPPORTED` (`kind=group_pr_unsupported`) |
| Parallel members inside one group | unsupported | v1.3 runs one member at a time (`capabilities.task_groups.not_supported`) |
| Automatic retry of a failed member | unsupported | `group resume` / a new group is a human decision |
| Design-conformance review contract (`group review`) | implemented (opt-in) | `test_v140_design_review.py` — the runtime **validates** a host-produced review; it never produces one. Requires `review.required` in the plan; `NOT_VERIFIED` is recorded but never counted as passing |
| Bounded group repair (`group repair`, `budget.max_group_repairs`) | implemented | `test_v140_design_review.py::TestBoundedRepair` — one `FAIL` finding per run, hard cap, repair units live in `runtime.repairs` so `plan_digest` is unaffected |
| Unbounded dynamic task graph | unsupported | repairs are capped per plan; a new requirement needs a plan revision |
| Structured spec v2 (AC ids, verification refs, path tightening, `verification_scope`) | implemented | `test_v030.py::TestStructuredSpec`, `test_v051_scope.py` |
| Event paging + bounded, redacted log tails | implemented | `test_v030.py::TestEvents`, `TestTaskCli` |
| Streaming progress (`watch --follow --jsonl`) | implemented | `test_v041_extras.py::TestWatchFollow` |
| Handoff with `failed_checks` / `not_run_checks` / `remaining_work` | implemented | `test_v041_extras.py::TestHandoff*`, `test_v051_scope.py::TestConsumers` |
| Local process execution (`local-process`, the only backend) | implemented | **real-e2e: NOT_RUN** |
| CLI discovery (explicit → PATH → known locations → bounded login shell) | implemented | `test_discovery.py` |
| Launch pinning: `argv[0]` is the absolute path `doctor` reports | implemented | `test_v050_pipeline.py`, `test_v030.py::TestAgentCli` |
| `environment_drift` event when adjacent runs resolve to different binaries | implemented | `test_v050_pipeline.py` |
| Pre-publication remote drift (3 kinds + `remote_unknown`, `error.details`) | implemented | `test_drift.py`, `test_v050_pipeline.py` — built from real local bare repositories; **no live GitHub remote has been exercised** |
| Platform layer: SH/EX locks, process identity, tree stop/alive | implemented | `test_v040_locks.py` (POSIX); Windows code path **not exercised on hardware** |
| Lifecycle locks held for the whole run (`maintenance` SH + `writer` EX) | implemented | `test_v040_locks.py::TestLifecycleLocks` |
| Cross-process run observation after a crash (`pid`/`start_token`/`group_token`) | implemented | `test_v040_locks.py` |
| Docker container execution | **removed** | v0.5.0: out of the product scope; old configs are refused with `CAPABILITY_UNSUPPORTED`, historical runs stay readable but unobservable |
| Built-in agent worker execution (any adapter) | implemented | **real-e2e: NOT_RUN** |
| GitHub publication (gh) | implemented | simulated-integration (remote stub) |
| wheel / sdist installation | implemented | clean-venv install (CI) |
| Orchestration record (`orchestration show/resume/claim/release/update`, version CAS) | implemented | `test_v041_workflow.py::TestOrchestrationCas`, `::TestResumeSnapshot` |
| Owner gating on dispatch/answer/revise/reassign (§10.10) | implemented | `test_v041_workflow.py::TestAnswerOwnership` |
| Agent Q&A channel (`needs_input` → `task answer` → `resume`) | implemented | `test_v041_workflow.py::TestAnswerRestoresProgress` |
| Versioned readable summaries + `report.md` | implemented | `test_v041_workflow.py::TestSummariesRenderMarkdown` |
| Project context bundle (`project context`, 32 KiB cap) | implemented | `test_v041_workflow.py::TestContextBundle` |
| Autonomy budgets (fix rounds / retries / run count enforcement) | **partial** | counters are controller-owned and rejected from hosts; **limits are not yet applied at `start`** |
| Native session resume | unsupported | — |
| Runtime plugin loading / third-party adapters | unsupported | — (adding an adapter is a code change) |
| Parallel writers on the *same* task / DAG scheduling / auto merge | unsupported | — |
| Windows support | **implemented, unverified** | platform code exists; no real Windows E2E record → treat as `IMPLEMENTED`, not supported |

## Expected contract change: `argv[0]` is an absolute path (v0.5.0)

`local-process` launches now record `argv[0]` as the **absolute binary path**
(`/opt/homebrew/bin/codebuddy`, `%LOCALAPPDATA%\...\codebuddy.cmd`), not the bare
name the adapter emits. This is deliberate: §6.6 pins the launched binary to the
one `doctor` reports, so "the CLI is installed" and "this run measured that CLI"
cannot describe two different executables.

Consequence for readers of evidence: a `capture.json` / launch record whose
`argv[0]` is a bare name predates v0.5.0 (or was produced by a non-`local-process`
path) and is **not** evidence of the frozen command form. Do not read it as
`FACT_PENDING` — that status is reserved for adapters without a frozen command
shape (`codex`).

## Execution combinations (v1.2)

一个"组合"不是 profile 名，而是**实际跑起来的那套东西**：适配器 + backend + 实测 CLI
版本 + provider/model/variant/reasoning_effort + 权限/工具 + 端点。它有一个稳定标识
`combination_id`（`cmb-<16 hex>`），可复现、不含绝对路径与凭据。

### 声明即生效，否则拒绝

`provider` / `model` / `model_variant` / `reasoning_effort` 一旦声明，适配器必须能把它落到
**受支持的有效配置**，否则注册期就 `CAPABILITY_UNSUPPORTED`。按适配器的实际形态：

| 适配器 | 能落实的执行字段 | 说明 |
|---|---|---|
| `codebuddy` | `model` | 通过 `--model` |
| `claude-code` | `model` | 通过 `--model` |
| `opencode` | `model`, `provider` | `--model provider/model` 是其有效形态 |
| `codex` | 无（`FACT_PENDING`） | 没有冻结的命令形态，声明即拒绝 |

`reasoning_effort` 目前**没有任何适配器能落实**，所以声明它会得到明确的拒绝，而不是被
静默忽略。端点（`endpoint_ref`）同理：没有适配器能把它落到启动配置，因此
`data_residency: required` 会判 `ENDPOINT_UNVERIFIED`。

### 三条不承诺

`capabilities --json` 的 `not_supported` 逐条列出，避免用"支持路由"掩盖：

- `model_quality_scoring` —— 不做模型质量评分与自动择优；证据来自任务验证，
  不是榜单投影；
- `internal_routing_control` —— 不能控制工具内部的回退、别名解析与子代理模型选择，
  只能观测工具自报的值；
- `performance_based_exclusion` —— v1.2 不用历史表现排除候选（统计只记录；
  `routing.use_performance=true` 会被拒绝）；
- `strict_model_enforcement_at_gateway` —— **不提供网关级模型白名单**。"只用指定模型"
  的事前强制只能在 API 网关或单模型私有端点上实现；Zoe 能做到的是"请求指定 +
  事后作废"（观测不符则 `MODEL_MISMATCH`，不产出证据、不进发布）。因此任何文档
  都不得据此声称已获得模型级的事前强制保证。

端点方面同样只做**启动前**声明校验：`attestation` 只有 `config-declared`，不做运行中
数据流向的技术证明。

另外：本版不做**主动模型探测**（`route probe-model` 推迟），因此
`MODEL_UNAVAILABLE`(4) 虽然在错误码表里冻结，当前没有任何代码路径会产出它；
模型"不可用"只能表现为 CLI 未解析（`ENVIRONMENT_ERROR`）、能力未声明
（`CAPABILITY_UNSUPPORTED`）或观测不一致（`MODEL_MISMATCH`）。

### 模型观测与闸门

`pin_model=true` 时只有 `reported_match` 才能进入验证；`unobserved` 同样阻断——"工具没说"
不是"对上了"。被阻断的 run 不产生 `candidate_sha`，工作区改动归档为
`runs/<id>/worktree.patch` 后恢复干净（可还原性校验通过才动手）。

## Evidence levels

- **unit** — deterministic, in-process, no external service.
- **simulated-integration** — external service replaced by a named test double
  (e.g. `FakeBackend`, `FakePublisher`).
- **real-e2e** — exercised against the real local CLI, a real repository and a
  real remote. **None are currently `real-e2e`.** The container backend no longer
  exists, so there is no "real docker daemon" level to claim either.
- **not-run** — implemented but never exercised in the target environment.

## Rule

Any promotion in evidence level must cite the evidence (test name or run log).
A simulated-integration result must never be described as real end-to-end. A
`--version` probe is a probe: it never promotes a backend to `VERIFIED`.
