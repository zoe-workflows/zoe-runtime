# Upgrading and uninstalling

Upgrading is not unconditionally recommended. The runtime and the skill commit to a
window once, in `runtime-requirements.json`, and an operator moves inside it.

## v1.5.0: the authorization rules got stricter

Three rules now refuse configurations that v1.4.x accepted. None of them rewrite your
file: they refuse and name the field, so the fix is visible instead of silent.

| Refused now | Message | Fix |
|---|---|---|
| a `routing` section without an explicit `allowed_profiles` | `INPUT_INCOMPLETE` | list the profiles you actually authorize (explicit and auto are treated the same) |
| `model` together with `allow_cli_default_model: true` | `INPUT_INCOMPLETE` | delete the flag to pin the model, or delete `model` to follow the CLI default |
| a `legacy: true` field inside `agent_profiles` (schema v2) | `POLICY_DENIED` | delete it and declare `model`; schema v1 configurations migrate as before |

`model` is also stricter about shape: empty, whitespace-only, surrounding whitespace and
non-string values are refused.

### Why reading still works

`store.config()` now validates with `strict=False`: structural problems (missing repo,
missing checks) still raise, but the new model/authorization rules are collected into
`policy_problems` instead of blocking the read. `status`, `report`, `events` and
`doctor` therefore stay usable on an old configuration — you can look before you fix.

The refusal happens at the dispatch entry (`task create` / `start` / `reassign`), where
it names the field. It is **not** reported as `POLICY_CHANGED`: that code means "the
policy drifted, rebind", and following it would land on the same error again.

### Confirm the allowlist once

A project that declares `routing` now needs one confirmation before its first dispatch:

```bash
zoe project attest --project P --dry-run   # show policy_hash, candidates and their models
zoe project attest --project P --expect-policy-hash <hash from the dry-run>   # write it
```

`--expect-policy-hash` (v1.5.1) closes a real window: the dry-run prints `H1`, the user agrees,
and a concurrent edit changes a model or endpoint so the write would record `H2`. With the
flag, the runtime re-reads the configuration **inside the same control lock** the record is
written under and compares digests (and, with `--expect-profiles`, the confirmed set):

- match → the record is written;
- mismatch → `ATTEST_PRECONDITION_FAILED`, `details` carries `field`, the
  `expected`/`observed` digests (plus 16-char `*_revision` short forms), the set difference
  (`added` / `removed`) and `next`, and **nothing is written** — the previous record, if any,
  stays untouched. (The `project` command family keeps the legacy exit code `4`; branch on
  `error.code`, not on the number — the frozen error→exit mapping only applies to the
  granular commands listed in the CLI contract.)

Without the flags the command behaves exactly as in 1.5.0: a mismatch can only be noticed
*afterwards*, from the `policy_hash` in the response, and the record is already on disk. On
`--dry-run` the flags are evaluated but never refuse: the response's `expectation` block
reports `matched` plus the same fields, so a host can rehearse the write.

The record lives in `$ZOE_HOME/projects/<id>.attest.json` and binds the whole
configuration digest. Any later change to an execution field — model, provider,
endpoint, permission profile, or the allowlist itself — invalidates it, and the next
dispatch returns `ALLOWLIST_UNCONFIRMED`. The exit code follows the command's own
mapping: `task create` / `start` keep the v0.2 `4`, the finer-grained actions
(`reassign`, and the v0.3+ commands) use `5` — the same split as `POLICY_DENIED`:

- `details.reason: "missing"` — never confirmed;
- `details.reason: "policy_changed_since_attestation"` — confirmed, then the
  configuration changed;
- `details.reason: "record_invalid"` — the record exists but is not trustworthy
  (missing `policy_hash`, `confirmed_profiles` not a non-empty id array, or the
  file is unreadable). A malformed record is treated as "not confirmed" rather
  than as a partial confirmation;
- `details.reason: "candidate_not_confirmed"` — the selected profile is inside
  `routing.allowed_profiles` but outside the set recorded by
  `project attest --profiles`.

Note that `"routing": null` is **not** "no routing section": a key that exists with
a non-object value is `INPUT_INCOMPLETE`. Only omitting the key keeps the historical
"configured means authorized" semantics.

`project rebind` never writes this record. Re-registering an identical configuration
does not change the digest, so it cannot serve as a confirmation either — use
`project attest`.

The record proves "this configuration was confirmed once". It does not record who
wrote the original allowlist and it does not resist a program with write access to
the same home directory.

### A one-line migration check

```bash
zoe doctor --project P --json | python -c "import json,sys; print(json.load(sys.stdin).get('policy_problems', []))"
```

`policy_problems` lists every configuration issue found while reading, each with its
`code` and `field`. Empty means the configuration still satisfies v1.5.0.

## Check the window first

```bash
cat /abs/path/to/zoe-workflows/runtime-requirements.json
zoe --version
```

| Situation | Action |
|---|---|
| target version is inside `[min_version, max_version_exclusive)` | upgrade the runtime only |
| target version is at or above `max_version_exclusive` | upgrade the skill **and** the runtime together |
| target version is below `min_version` | do not install it; the wrapper will reject it |

An out-of-window pair fails closed: the wrapper exits before forwarding, so nothing
is dispatched under an unverified contract.

## Safe order

```text
1. read runtime-requirements.json (compatibility window)
2. stop dispatching new work: pause or finish active tasks
3. handle in-flight tasks: let them finish, or `task pause` and confirm the tree stopped
4. `zoe reconcile --project P --json` — close out runs whose supervisor is gone
5. upgrade: pipx upgrade zoe-runtime   (or pipx install "zoe-runtime==<version>")
6. verify: zoe --version && zoe capabilities --json
7. verify the pair: zoe capabilities --check-requirements <skill>/runtime-requirements.json
8. resume dispatch
```

There is **no hot upgrade**: a supervisor running from an old install keeps running
the old code until it exits. Upgrading under an active run is the one case where
"the upgrade succeeded" and "the task behaved as tested" are different statements.

## State is not the package

- On-disk state lives under `ZOE_HOME` (default `~/.local/share/zoe`), never inside
  the package. Upgrading or uninstalling the runtime does not touch tasks, runs,
  events or evidence.
- Schema migrations are explicit (`zoe state migrate --to 4`), take a backup, and
  refuse while work is in flight.
- Restoring: `zoe state restore --from <backup> --confirm`.

## v1.2 → v1.3 (task-group delivery, schema v4)

No new tables. The schema bump exists for one reason: a v1.2 binary that reads a
v1.3 group record would see `definition` instead of `ordered_items` and quietly
report "0 items". Raising `user_version` to 4 makes that binary answer
`SCHEMA_UNSUPPORTED` instead of misreading the record.

```text
1. stop dispatch; `zoe reconcile --project P` until no run is active
2. stop every group runner (`group pause GID --stop`, then confirm the process is gone)
3. zoe state migrate --to 4 --dry-run     # lists what would change
4. zoe state migrate --to 4               # takes home/backups/state-v3-<ts>.db first
5. zoe capabilities --json                # expect schema v4 / protocol "5"
6. re-register project configs if `capabilities --check-requirements` asks for it
```

What changes on disk:

| Item | Before (v1.2) | After (v1.3) |
|---|---|---|
| `groups` rows | `record_version: 2`, `ordered_items` | `record_version: 3`, `definition` + `runtime` |
| `group plan` / `dispatch` | single-step, no scheduler | unchanged in shape, admitted through one function |
| `group start/report/accept/pause/resume/cancel` | absent | present; **legacy v2 records are refused** with `kind=legacy_group` |
| `ZOE_HOME/groups/<gid>/` | absent | frozen plan, spec snapshots, route previews, integration ledgers |
| check rows | `status` + `exit_code` | plus `error_class` (`test` / `environment` / `timeout` / `policy`) |
| profile | — | optional `auth_mode` (`cli_login` / `api_key` / `unknown`) |

Legacy v2 groups keep working for `status` and `dispatch`; they cannot be started,
because a dependency graph cannot be guessed from an ordered list. Re-create them
with `schema_version: 2` + `members` if you need the runner.

`state migrate` does **not** rewrite legacy records. Nothing is inferred.

## v1.3 → v1.4 (design review + bounded repair)

**No schema bump and no migration.** `user_version` stays 4: v1.4 only adds optional
keys inside records a v1.3 reader already tolerates.

```text
1. 升级二进制即可；`zoe state migrate` 无事可做，也不需要备份（没有结构变化）
2. 新计划的 `review` / `budget.max_group_repairs` 只在**显式声明**时生效
3. 想在既有项目上启用审查：新计划写 `"review": {"required": true}`，旧组不会被追溯
4. `zoe capabilities --json` → `task_groups.design_review` 与 `.group_repair` 出现
```

| Item | v1.3 | v1.4 |
|---|---|---|
| `group review` | 不存在 | 提交/查询设计一致性审查结论 |
| `group repair` | 不存在 | 一条 `FAIL` finding → 一次有界修复，受 `max_group_repairs` 约束 |
| `definition.review` | 不存在 | 可选 `{required, mode}`；缺失 = 该层不参与验收 |
| `budget.max_group_repairs` | 不存在 | 可选；缺失 = `0` = 不授权修复 |
| `runtime.reviews` / `runtime.repairs` | 不存在 | 提交历史（含 `invalid`）与修复单元 |
| `groups/<gid>/reviews/<sha>.json` | 不存在 | 审查结论，绑定一个集成版本 |
| 状态派生 | 检查全 PASS → `REVIEW_READY` | 计划要求审查时，还需要一份无 `FAIL` 的有效结论 |
| `plan_digest` 输入 | — | 仅当计划显式声明上述字段时才纳入；v1.3 计划摘要不变 |

旧组的 `runtime` 里没有 `reviews` / `repairs`，一律按"无审查、无修复"处理，行为与
1.3.0 完全一致。

### 从 v1.4 降级到 v1.3 的真实风险

不需要改数据库，但会**丢掉两道门**：

- v1.3 不认识 `runtime.reviews`，会忽略审查结论，可能让一个有 `FAIL` finding 的组
  进入 `REVIEW_READY` 并被 `group accept` 收口。
- v1.3 不认识 `runtime.repairs`，`members_view` 不会列出修复单元，`all_integrated`
  也不会计入它们——报告会显示"全部集成"而实际还有一个修复单元没跑。

所以降级前先确认：没有未解决的 `FAIL` finding，且没有任何未集成的 `repair-*` 单元。
`definition.review` 与 `budget.max_group_repairs` 会被 v1.3 当作未知键忽略（不报错，
因此也不会提醒你它被忽略了）。

## Downgrading

Downgrade is a version change like any other: check the window, stop dispatch, then
install the exact older version. A state schema written by a newer runtime may be
refused by an older one (`SCHEMA_UNSUPPORTED`) — that refusal is the safe outcome;
do not edit the database to work around it.

### 从 v1.3（任务组交付）降级

v1.2 及更早的读取器不认识 `record_version: 3` 的组、`groups/<gid>/` 产物、
`auth_mode` / `error_class` 两个字段，也没有 `group start/report/accept` 命令。回退步骤：

1. **关闭新入口**：停止所有 runner（`group pause GID --stop`，确认进程消失），
   不再调用 `group start`；需要配置级开关时把 `capabilities` 对应的能力从 skill 的
   `required_capabilities` 里去掉，让包装器在预检阶段就拒绝。
2. **停派发并收口在跑的 run**：`zoe reconcile --project P`，确认没有 `PROCESS_UNKNOWN`
   遗留；有就 `task recover --run R --confirm-stop`，不要带着未知进程回滚。
3. **恢复数据库**：`zoe state restore --from <backups/state-v3-*.db> --confirm`
   （会自动轮换 epoch，旧游标全部失效，宿主必须用 `next_cursor` 重来）。
4. **对账 Git 副作用**（数据库回滚**不会**回滚它）：
   - `groups/<gid>/integration/<op_id>.json` 逐条比对真实 ref：
     `git rev-parse refs/heads/zoe/<gid>`；
   - `status=prepared` 而 ref 未更新的项 = 未完成的集成，人工决定推还是弃；
   - `status=done` 的集成已经在分支上，删库不等于撤销；
   - 集成分支 `zoe/group-*` 与已推送的分支不会被回滚，需要人工删除。
5. **恢复后**：schema 回到 v3，`protocol_version` 回到 `"4"`，旧二进制可以打开。
   不要手改 `PRAGMA user_version` 绕过闸门。

⚠️ 只恢复数据库就宣称"回滚完成"是错的：v1.3 的交付物一半在 SQLite，一半在 Git。

### 从 v1.2（执行组合路由）降级

v1.1/v1.0 的读取器不认识 `routing` / `endpoints` / profile 的执行组合字段、`route-*.json`
决策文件，以及 run 记录上的 `frozen_execution` / `combination_id` / `model_identity` /
`spend` / `attribution`。它们的安全处理方式是：

1. **先停派发**：`zoe reconcile --project P` 确认没有在跑的 run；
2. 备份 `ZOE_HOME`；
3. 从项目配置里删掉 `routing` 与 `endpoints` 两段（旧版本对这两段会 `INPUT_INCOMPLETE`）；
4. profile 里新增的 `provider` / `model_variant` / `reasoning_effort` / `endpoint_ref` /
   `capabilities` / `cost_class` / `latency_class` / `unit_price` 一并删除，只保留
   `model`（旧版本认识它）；
5. 旧版本对任务记录里多出的字段是**容忍**的（它只读自己认识的键），所以不需要改任务；
   但 `route-*.json` 与 v1.2 的统计文件会被忽略——它们不是缓释，留着即可，不要手改。

⚠️ 有一点**不可逆**：v1.2 的闸门（`MODEL_MISMATCH` / `SPEND_*`）拒绝过的 run 会把改动
移到 `runs/<id>/worktree.patch`。回退到旧版本不会自动还原工作区，patch 仍可
`git apply --stat` 检查后手工使用。

### 从 v1.0/v1.1 升级到 v1.2 的唯一破坏性变更

**profile 必须声明 `model`**（or 显式 `allow_cli_default_model: true`）。缺省时适配器根本
不传 `--model`，等于批准了一个描述不清的组合。两种迁移方式：

- 路径 A（推荐）：给每个 profile 写上真实模型名，例如 `"model": "claude-sonnet-4.5"`；
- 路径 B（应急）：`"allow_cli_default_model": true` —— 此时记录里 `model_unresolved=true`，
  `combination_verified` 恒为 `false`，报告必须写"运行模型未解析"，pin 闸门也不可能通过。

旧式 v1 配置（顶层 `agent` 而非 `agent_profiles`）不需要改：它被自动迁移为 legacy
profile，效果等同路径 B，且规范化后的 `policy_digest` 与 v1.0.0 逐字节一致。

### 没配 `routing` 的项目

行为与 v1.0.0 完全相同：不写决策文件、不新增响应字段、不做路由决策、不启用费用护栏。
`task create` / `task start` 的响应字段逐字节一致（`policy_digest` 也一致）。

## Uninstalling

```bash
pipx uninstall zoe-runtime
```

The task state and the evidence remain on disk. Remove `ZOE_HOME` only after you
have exported what you need — evidence is the audit trail, not a cache.

Uninstalling the **skill** is a separate action: remove its directory from the
host's skills folder. Keep one version of the previous install for a rollback
window (see the migration steps in the split plan, §4).
