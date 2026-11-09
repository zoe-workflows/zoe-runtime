# Changelog

All notable changes to Zoe Runtime are documented here. The project follows
semantic versioning for its public interfaces (CLI, on-disk state).

## [1.7.0] - unreleased

**第二层验收**。触发点是一次真实事故：7 个任务合并、全量测试 1164 条全绿，而 28 条需求
里真正验收闭环的是 0 条。根因不是"测试没跑"，是**验收被实现成一个整体布尔值**：
check 有逐条 exit code，`accept` 只有一个 `decision`；manual AC 在报告里只能写
「未记录逐项人工结论」——一句诚实的空白，也是一次货真价实的漏验。

本版不发明新概念：组级 `_review.py` 早就把"绑定版本 + 全覆盖 + 证据可解析 + 独立判者"
做完了，这里只是把它下沉到单任务，并给"验收的形状"补上三个可判定的位置。

### Added

- **spec v3（`schema_version: 3`）**。v2 仍然合法、行为不变；四个新字段只在 v3 被读取，
  写进 v2 会 `INPUT_INCOMPLETE`（静默忽略比拒绝更危险：作者会以为已经声明了门禁）。
  - `verification.judge`（manual 必填）：`independent` / `owner` / `human`；
  - `verification.supporting_checks`：机器能判的那一半，只能引用**项目已批准的 check**
    （不给 spec 内联 shell 命令的机会，那会绕开 checks 白名单与 `CHECK_ESCAPES_WORKSPACE`）；
  - `acceptance_review {required, mode, independent}`：声明这个任务必须被逐条判定才能 accept；
  - `decisions[]`：设计裁定。`status=open` 不阻断 create，**阻断 accept**（`DECISIONS_OPEN`）；
  - `progress_refs[]`：AC ↔ 需求文档复选框。`doc` 必须在 `allowed_paths` 内，verify 阶段
    检查该 marker 所在行已是 `- [x]`，否则 `PROGRESS_NOT_TICKED`。
- **`task review TASK [--request-file review.json]`**：逐条 AC 判定。契约
  `schemas/task-review.v1.json`——`subject_sha` 必须是当前 `validated_sha`、每条 AC 出现
  且只出现一次、证据引用必须解析到本候选（`check:` / `evidence:` / `doc:` / `decision:`）。
  非法输出留档 `status=invalid` 后拒绝，**不会**变成一次通过。
- **判者独立性**：`acceptance_review.independent=true` 时，除要求
  `reviewer.independent_context` 外，还机械比对 `profile_id` / `run_id` / `actor`——
  与产出该候选的执行者相同即 `EVIDENCE_INVALID`（`SELF_REVIEW`）。"worker 说自己复核完了"
  从来不是一次判定。
- **accept 三步门禁**：`DECISIONS_OPEN`（5）/ `ACCEPTANCE_UNREVIEWED`（3）/
  `ACCEPTANCE_REVIEW_FAILED`（5）/ `ACCEPTANCE_UNACKNOWLEDGED`（3）。`NOT_VERIFIED`
  不阻断，但必须逐条确认；通过后 `acceptance.json` 带 `review`（verdict / subject_sha /
  counts / reviewer / path）。
- **`task coverage --project P --markers-file F`**：需求覆盖账本。五桶
  （covered / pending / missing / duplicate / unmapped），`cleared` 是唯一可以被读成
  「清单清零」的字段。covered 的判据刻意很窄：`DONE` + `accepted` + 判定满足；
  `REVIEW_READY` 一律 pending——"代码写完"不是"需求闭环"。
- **`prepare` 的 `advice[]`**（不进 `diagnostics`、不影响 `prepared`）：`AC_SINGLE_ITEM`、
  `AC_SINGLE_SIGNAL`、`REVIEW_NOT_INDEPENDENT`、`DECISIONS_OPEN`；另加一条阻断诊断
  `PROGRESS_REF_OUT_OF_SCOPE`（文档不在 `allowed_paths` 内，写回必然失败）。
- **`depends_on`（上游 marker 列表）**：`start` / `resume` 前要求上游**已闭环**
  （`DONE` + `accepted` + 判定满足），否则 `DEPENDENCY_NOT_MET`（5）。判定与覆盖账本
  同源（`_coverage.dependency_state`），因此"图说不阻塞"等于"派得出去"。自依赖在
  契约层拒绝（`depends_on` 含本任务自己认领的 marker 即 `INPUT_INCOMPLETE`）。
  查在 `start` 而不是 `prepare`：上游任务通常在 spec 写完之后才建。
- **`task graph --project P [--format markdown]`**：依赖图 + 阻塞点 + 环。只读，
  输出 nodes / edges（逐条 `satisfied`）/ blocked / cycles / `ready`。
- **能力声明**：`task_acceptance_review` / `spec_v3_decisions` / `progress_refs_gate` /
  `task_coverage` / `task_dependency_gate`，以及 `features.spec_contract_version`、
  `acceptance.dependencies`。

### Changed

- `SPEC_CONTRACT_VERSION` 2 → 3；`validate_spec_text` 返回**这份 spec 实际使用的版本**
  （原先恒返回当前常量，v2 任务会被记成 3）。
- `task report` 的验收项表优先渲染 review 结论（`PASS/FAIL/NOT_VERIFIED` + 判者），
  没有判定时保持原文案；新增「未验证项」「待拍板」两节。
- `--emit` 草稿升级为 v3：两条 AC、两种判者、要求独立判定——模板比文档更能约束形状。

## [1.6.0] - unreleased

**可观测性与一致性**的批次。触发点是两次真实操作：一次是编排者只能报 "RUNNING /
REVIEW_READY"，报不出"它在写哪个文件、最后一句说了什么"——一版摘要要 N 次 `status` +
N 次 `logs`，成本把人逼成只看状态；一次是改了 `max_active_tasks` 后 5 个已可评的任务全部
`POLICY_CHANGED`。后者本次**不修**：拆分 `policy_hash` 是契约级变更，而"调度预算属于项目
政策"是已有记录的取舍（理由与重启条件见
`zoe-workflows/docs/2026-11-05-observability-and-policy-consistency-review.md` §4.2）。

### Added

- **`task digest --project P`**：一次调用拿到一批任务的事实聚合——`state` / `phase` /
  `elapsed_seconds` / `quiet_seconds` / `log_bytes` / `process_state` / `observations_stale`，
  加每任务最新 `text`（≤120 字符）与 `tool`（name + target，≤60），以及 `hot_paths`
  （同一路径被几个候选触及）。**只聚合事实**：不产出"完成/失败"，不推进状态，不写文件；
  `state` / `block_code` / `empty_candidate` 原样转述。`--no-content` 跳过日志读取。
  成本是 O(N) 次有界读（N 个目标），不是"恒定一次查询"。
- **`task list --fields A,B`**：字段投影。任务记录含冻结 `config` 副本，40 个任务时整包输出
  既慢又容易把配置当状态读。未知字段报 `INPUT_INCOMPLETE` 并列出可用字段——拼错必须报错，
  静默返回 `null` 是投影类接口最常见的误判来源。
- **`BASE_DRIFT`（退出码 4）**：`start` 首次派发前校验冻结基线是否仍是 `base_ref` 的祖先。
  rebase / force-push 之后再开 worktree，worker 会在一棵已被丢弃的树上干活，最后以
  `VALIDATION_FAILED` 收场——时间白花，现象还像代码问题。只在 workspace 尚未建立时校验：
  resume 继续的是已冻结的 worktree，此刻拦住它只会把正在推进的任务卡死。`git` 读不到时判
  `None` 并**放行**（这是提前失败的优化，不是安全边界；真正的校验仍在 verify 阶段）。
- **额度耗尽闸门（`project quota show` / `clear`）**：`QUOTA_EXHAUSTED` 此前只在
  `_EXIT_CODES` 登记、没有任何产生点。现在四件事一起补齐：
  ① `classify_failure` **先判配额再判限流**——中文横幅
  （`429 使用量已超出频率限制，将在 … 重置`）得到 `QUOTA_EXHAUSTED`，而不是可以退避重试的
  `AGENT_RATE_LIMITED`；
  ② 一次配额失败在**项目级**落闸门（`<home>/projects/<id>.quota.json`，不进项目配置，
  因此不改 `policy_digest`），`start`/`resume` 在 doctor 之前直接拒绝，**不产生 run**；
  ③ `reset_at` 过了也不自动放行（那个时刻可能来自另一时区或另一账号），解除只能
  `project quota clear --project P --why ...`，且保留事故记录；
  ④ 无 capture 元数据的降级路径改为用 `agent.log` 尾部重新分类，`agent_evidence` 保留原文
  一行——"额度用完"不再长成兜底码 `AGENT_FAILED`；
  ⑤ `handoff.json` 增加 `worktree_path` / `run_id` / `observation`（静默/字节/进程态）/
  `quota_gate`，收尾快照不必再抓 N 次日志。额度判定把 `402 Payment Required` /
  `insufficient balance` 与 `429` 归为同一类：两者都不是退避能修的。
- `task digest` 每任务新增 `run_id` / `candidate_sha` / `validated_sha`：增量视图
  （技能侧 `zoe-digest.sh --since`）需要看见"候选变了"，而不只是"状态变了"。纯加法，
  `api_version` 不变，忽略新字段的宿主行为不变。

### Changed

- `capabilities().observation` 新增 `task_digest` / `task_digest_fields` / `task_list_fields` /
  `base_drift_preflight`；`SUPPORTED_CAPABILITIES` 同步新增 `task_digest` /
  `task_list_fields` / `base_drift_preflight`，供技能的 `required_capabilities` 硬门禁使用。
- 错误码 → 失败类别：`BASE_DRIFT` → `infrastructure`（仓库历史被改写，不是这次改动的质量）。
- 错误码 → 失败类别：`QUOTA_EXHAUSTED` → `environment`。不是 `auth_permission`——换 profile
  修不好一个空额度池，归到权限类会让宿主把"等重置"误读成"换执行器"。
- `capabilities().features` 新增 `quota_gate`，技能的 `required_capabilities` 可以据此做硬门禁，
  而不是靠 `>=1.5.2` 的版本窗口猜这一版有没有闸门。
- **`concurrent_base`**：进入 `REVIEW_READY` 时写下"目标分支在任务基线之后做了什么"——
  `kind`（`none` / `base_advanced` / `overlap` / `reverts` / `unknown`）、`behind_by`、
  `overlapping_paths`、`reverts_merged_work`。**只写事实、不改状态**：并行批次里落后是常态，
  判成失败只会逼出 `merge --abort` + 重派；`unknown` 不等于 `none`。与 `BASE_DRIFT` 分工明确：
  那个回答"基线还在不在历史里"（拒绝），这个回答"基线之后发生了什么"（提示）。
  `capabilities().features` 新增 `base_drift_review`，但**不进**技能的 `required_capabilities`。
- **`doctor.config_source`**：`project register` 记下来源文件（`projects/<id>.source.json` 旁挂，
  **不进** `policy_digest`），`doctor` 回答"磁盘上的配置与注册时是否一致、改了哪些顶层键"。
  运行器只读注册副本，之前"改了 `project.json` 但没 register"完全静默，症状会被误读成
  "adapter 缓存了模型名"。`changed` 三态：`true` / `false` / `null`（无法判定，不是"没变化"）。
- **`block_details.next_action`**（`SCOPE_VIOLATION`）：错误自己说清唯一出路，
  spec 派生范围写"widen the spec + `task revise`（rebind 无效）"，项目派生范围写
  "widen project + register + rebind"。
- **`block_details.hint`**（`AGENT_FAILED`）：兜底码上的**未确认线索**
  （`model_not_found` / `gateway_error`），只用于分流"先修哪一头"。**不新增错误码**：
  可靠识别需要适配器 marker（契约变更），靠文本改码会把"无法归因"伪造成"已归因"。

### Fixed

- **`SCOPE_VIOLATION` 在 spec 派生范围下的恢复闸门**：`task_scope` 只在 create/revise 时重算，
  `rebind` 只更新 `policy_revision`。旧闸门只看 `policy_revision`，于是"改项目配置 + rebind"
  能放行、下一轮必然再撞同一个越界——白烧一次付费尝试，还给"已经修好了"的错觉。现在
  `scope_source=spec` 且 `basis=policy-too-narrow` 时只认 `spec_revision`。
- **`rebind` 丢掉归因字段**：重建 `last_block` 时只带 `code`/`run_id`/`policy_revision`，
  `basis` / `scope_source` / `spec_revision` 一起消失，恢复前的闸门随即认为"这不是策略类阻塞"
  而直接放行。现在按 `block_code` 相同则原样带过来。
- **`revise` 无条件回到 `implement`**：只改 `allowed_paths` 时实现要求没变，却把工作区里已经
  写好的代码丢掉重跑一次 agent。现在这种修订直接进 `verify`（`checks` 不过会走
  `VALIDATION_FAILED` 再回 `implement`，代价只是一次本地检查）；读不到旧 spec 时保守回
  `implement`。
- **删除受控文件的候选必然 `WORKSPACE_CHANGED`**：`manifest` 把"索引里有、工作树里没有"记成
  `["name","absent"]`，提交前的指纹含这条、提交后的不含，`Commit changed candidate tree` 必然
  触发——删除类交付（清理死代码、删旧模块）从来不可能通过验证。`manifest` 现在只描述磁盘上
  存在的文件；删除由 `scope()` 与 `before`/`after` 条目差集检出。
- **`test_v160_quota` 的绝对时间炸弹**：`QUOTA_BANNER` 里写死 `2026-11-06 14:24:37`，当天
  14:24 起 `gate["expired"]` 变真、固定一条用例永久失败。改为相对当前时间生成。

## [1.5.3] - unreleased

**让证据和错误自己说清楚**的批次。触发点是两次真实误判：一次是把 `task logs` 的空窗口读成
"读取有 bug"（实际是 `rotated`），一次是把 worktree 里明明失败的 check 当成 PASS（check 自己
`cd` 到了主仓库，验证的是另一棵树）。

### Added

- **`task logs` 快照新增 `empty_reason`**：`text`/`events` 同时为空时给出四种原因之一——
  `rotated`（游标超出文件大小）、`no_newline_in_window`（尾部窗口整段落在同一记录里）、
  `no_complete_line`（只有半行/空行）、`no_new_records`（`--after` 已追平）。有内容时为
  `null`。此前这四种情况在响应里长得完全一样，`bytes` 很大而 `text` 为空更是自相矛盾。
- **`empty_candidate`（任务记录 / `task status` / `task report` / 证据）**：候选与 `base_sha`
  相同（零提交）时置真，并写一条 `empty_candidate` 事件；`task report` 顶部标
  `EMPTY：与 base_sha 相同`。**只标注，不阻断**——零改动是合法结果（纯验证任务），
  是否允许由验收判断。`revise` / `rebind` / 重新指派会复位。
- **`REQUEST_CONFLICT` 带 `details.existing`**（`task_id` / `action` / `status` /
  `created_at`）：这个码此前是唯一无法自查的失败——宿主只知道 id 被占，不知道被谁占。
  同一 `request_id` 换 `task` 或换 `action` 都会撞它（`hash` 覆盖 `action+task+payload`）。
- **`prepare` 新增 `CHECK_ESCAPES_WORKSPACE`（阻断）**：某 check 的 argv 里把 cwd 钉到绝对
  路径（`cd /abs`、`git -C /abs`、`--cwd=/abs`）时报告并令 `prepared=false`。checks 的 cwd
  已经是任务 worktree，再钉一次只会验证另一棵树，而 exit code 仍记在候选名下。
- **`prepare` 新增 `allowed_paths.warnings[]` 条目 `SCOPE_DIRECTORY_LEVEL`（告警）**：spec
  声明的范围含目录级条目时提示粒度。串行任务合法，并行批次里等于没有边界——判不判阻断由
  编排决定，运行时只报事实。

### Changed（对外行为，发布说明必读）

- 零候选不再需要调用方自己比两个 SHA（也避免"两个空 SHA 相等"被误判成没有改动）：
  `candidate_sha is None` 是"还没验证"。
- `REQUEST_CONFLICT` 的 `message` 未变，只新增 `details`；按 `code` 分支的宿主不受影响。

### Tests

- `tests/test_v153_candidate_truth.py`：冲突占用者、零候选标注与证据不变量、`empty_reason`
  四种取值、两条 `prepare` 静态判据各一组。
- `tests/test_v153_batch_start.py`：批量派发脚本的按码分流（`TASK_BUSY` 同 id 重放、
  `SLOT_BUSY` / `REQUEST_CONFLICT` 不重放、`ENVIRONMENT_ERROR` 不重试）与派发前闸门。

## [1.5.2] - unreleased

子进程进展的**可读化**批次。1.5.1 已让宿主能"接着读日志"，但读出来的东西有三处不可信：
快照先全量读文件再切片（500 MiB 日志 × 3 个目标 = 每轮 1.5 GiB I/O）、窗口起点落在多字节
字符或半行中间、`cursor` 指向 EOF 因而常常落在半行。这一版把读取面收敛成"有界窗口 + 字节级
完整记录"，并新增一条非阻塞增量读。

### Added

- **`task logs --after <cursor>`：非阻塞增量读。** 形状与 `--follow` 的 `cursor` /
  `next_cursor` 同构（`run:gen:offset`），可直接回填；`has_more` 表示还有下一页。
  与 `--follow` 同用返回 `INPUT_INCOMPLETE`（`details.field="after"`），**不静默忽略**：
  "以为在看历史、其实在看实时"是比报错更贵的错误。cursor 的 run 与本次读取的 run 不一致
  返回 `PROJECT_MISMATCH`；`offset > 文件大小`（截断/替换）不算错误，返回
  `rotated: true` + `reset_cursor` + 空 `events`。
- **快照响应新增字段**（全部为增量，旧字段保留）：`events`（投影后的事件，最多 200 条，
  **保留最近**）、`events_truncated`、`tail_start_offset`、`next_cursor`、`has_more`、
  `rotated` / `reset_cursor`、`projection`；单条内容被投影层截断时事件上带 `clipped: true`。
- **`--follow --tail-bytes N` 真正生效**：显式给窗口时先回看该窗口内的完整记录再跟随。
  `--cursor` 显式给出时 `--tail-bytes` 被忽略（cursor 优先）。
- **capabilities**：`observation.logs_snapshot=2`、`logs_after=1`、`logs_window`、
  `logs_truncation`；新增可声明能力 `logs_incremental_read`。

### Changed（对外行为，发布说明必读）

1. `--tail-bytes` 的**默认值从 `65536` 变为"未指定"**。快照仍然默认 65536 字节，
   `--follow` 不带该参数时仍从当前末尾开始——这一条是"显式参数从无效变有效"，不是默认回看。
2. 快照的 `cursor` 从"文件末尾"改为"最后一条完整记录之后"（可能小于 `bytes`），
   续读首行不再是半行残片。
3. **空行不再产出事件**：`project_line("")` 返回空列表，快照与 follow 同一规则
   （此前 follow 会为每个空行输出 `{"kind":"raw","text":""}`）。
4. 快照从"全量 `read_bytes()` 后切片"改为 `stat` + `seek`/`read` 的有界窗口；
   窗口起点**向后对齐**到完整记录（跳过不足一行的头部），不向前回溯。
5. 快照的 `text` 仍是窗口原文（含尾部半行，供排障），`bytes` 仍是文件总大小。

### Fixed

- `--tail-bytes` 的校验移到"解析后的有效值"上：`1..1048576` 同时约束快照与跟在
  `--follow` 后的显式窗口。

### 批量并行派发的摩擦点（B3 / B4 / B5）

- **`timeout_seconds` 进入校验面**（B3）。这个字段此前既无校验也无出参：写成
  `"3600"` 不会被注册拒绝，只会在 run 的监视循环里以 `TypeError` 结束（失败的
  时机还取决于走到哪个分支）。现在写入/执行路径以 `INPUT_INCOMPLETE`
  （`details.errors[0].field="timeout_seconds"`）拒绝非整数秒、布尔、`<60` 或
  `>86400` 的值；**读路径不拒绝**，只把问题记进 `problems`，否则一份写错的历史
  配置会让 `doctor` / `status` 一起打不开，用户连"为什么错"都看不到。生效值统一由
  `_config.run_timeout_seconds()` 给出（缺省 3600），执行期与回显读同一份定义；
  `doctor` 新增 `run_timeout_seconds`（生效值）、`run_timeout_declared`（原值）、
  `checks.run_timeout_valid`。`timeout_seconds` 仍是**项目级**，没有任务级覆盖。
- **`context.allowed_next` 补齐 `BLOCKED`**（B4）。此前只有 `INPUT_REQUIRED`
  有下一步，其余 `BLOCKED`（`RUN_TIMEOUT` / `RETRY_EXHAUSTED` / `RUN_INTERRUPTED` …）
  什么都不给——接管入口在最需要动作的状态上是空的。现在按 `block_code` 分流：
  可恢复 → `task resume`；`INPUT_REQUIRED` → 只给 `task answer`；`PROCESS_UNKNOWN`
  → 只给 `task recover`。后两条**不并列** `resume`：运行时会以 `STATE_CONFLICT`
  拒掉它，并列给出等于先骗一次再报错。
- **`shared_resources` 的注释不再承诺互斥**（B5）。字段只被规范化与保存，
  `_pipeline` / `_store` / `_group_runner` 都不读它；原注释"共享资源的两个任务会被
  串行化"没有任何实现。与其留一句错的承诺，不如写清"这是声明，不是锁"。

## [1.5.1] - unreleased

模型观测修补批次。起因是一次真实采集：`requested` 与 `reported` 不是同一个字符串时，
旧实现把"同一模型的两种叫法"读成了 `mixed`，于是 `pin_model` 在本机永远不可能满足，
只能靠降低 `cost_class` 绕过——那不是证据，是关掉要求。

### Added

- **`model_of` 多读两处结构位置**：`providerData.model` / `provider_data.model`（含
  `message` 内嵌套），并忽略 `unknown` 这类占位串。实测 codebuddy 2.111.0 的
  `--output-format json` 顶层没有 `model`，供应商模型只出现在 `providerData.model`；
  不读这一处会把"工具说了"记成"未观测"。
- **`model_aliases` 与 `alias_confirmed`**：profile 可以声明"请求名与上报名是同一个
  模型"。全部上报值落在 `{model} ∪ model_aliases` 内、但不是逐字相等时，
  `model_identity=alias_confirmed`、`identity_basis=declared_alias`。请求回显
  （`system`/`init` 行的 `model`）单独记为 `request_echo`：保留在 `reported_models`
  里供审计，但不参与判定。run 记录新增 `identity_basis` 与 `model_aliases`；
  `PerformanceRecord` 新增 `counts.runs_identity_alias`，与 `unobserved/mixed` 分列；
  `attribution` 仍不把"按声明等价"当成逐字一致，不进能力比较。
- **`watch --follow` 回传游标**：首行 `follow_start.cursor`、末行
  `follow_end.next_cursor`（`cursor_expired` 分支同样带）。宿主不必自己拼
  `<epoch>:<event_id>` 就能续读；事件行本身不带游标语义。
- **`task recover --run` 可省略**：缺省取 `current_run_id`，响应新增 `run_source`
  （`explicit` / `current_run`），并接受 `--json`。脚本收口不必先从 `status` 里抠 run id。
- **`project attest --expect-policy-hash` / `--expect-profiles`**：把"用户看过的"与
  "被记录的"钉在同一个受保护操作里。写路径在**同一把 control 锁内**重读配置、重算摘要并
  比对，不一致返回 `ATTEST_PRECONDITION_FAILED`（映射为 5；`project` 命令族沿用旧退出码
  `4`，宿主应按 `error.code` 分支）且**不写任何记录**。此前
  `--dry-run` 与正式写入之间的窗口只能靠"写入后比对响应里的 `policy_hash`"事后发现，而记录
  已经落盘（覆盖旧记录）。不带这两个参数时行为与 1.5.0 完全一致；`--dry-run` 带
  `--expect-*` 时不拒绝、不写入，只在 `expectation` 字段报出比对结果（可用于"预演一次，
  确认下一轮写入不会被拒"）。
- **`group start` 新增退出原因 `crashed`。** 推进器自身抛出未预期异常时不再记为
  `no_progress`（那属于 `_RESUMABLE`，会引导宿主重启一个必然再崩的推进器），而是
  `crashed` + `needs_human`，hint 指向 `group status --refresh`。同值写入
  `runtime.lease.exit_reason`。

### Changed

- **`pin_model` 的放行条件扩大为 `reported_match` / `alias_confirmed`。** 此前只有逐字
  相等才算满足，于是"请求名 ≠ 供应商名"的 CLI 上 pin 永远不可能满足。声明
  `model_aliases` 现在是一条可审计的路径（经 `attest` 覆盖）；未声明时结论仍是
  `reported_mismatch` / `mixed`，阻断行为不变。
- **`claude-code` 的 `isolated-edit` 落到了真实 argv**（`--permission-mode acceptEdits`），
  `permission_switch=True`。此前该适配器声明了权限档位却没有绑定：无人值守的一轮没有
  审批通道，写文件必被拒，最终烧完 `timeout_seconds` 再伪装成 `AGENT_FAILED`，而
  `permission.enforced` 还是 `false`。现在 `enforced=true`（与 `codebuddy` 同形态，
  后者已实测）；`claude-code` 的 real-e2e 仍是 `NOT_RUN`，能力矩阵口径不变。
  `tests/test_agents.py` 新增不变量：声明 `permission_switch` 的适配器必须绑定它声明的
  **每一个**档位。
- **`task prepare` 的 `prepared` 纳入授权判定。** "能创建并启动"包含 `execute` 授权：
  未授权时 `prepared=false` 并给出 `AUTH_REQUIRED` 诊断（此前 `prepared=true` 而
  `start` 必然被拒，报告自相矛盾）。

### Fixed

- **请求回显被当成运行观测。** `system`/`init` 行的 `model` 是请求回显，旧实现按
  `main_call` 计入，与 `assistant` 行的供应商名一起构成两个不同模型 → `mixed` →
  `MODEL_MISMATCH` 隔离 worktree。现在回显单独作用域，不参与身份判定。
- **首次 `rebind` 会接纳被篡改的 worktree（证据缺陷）。** 产物基准取自**当前**工作树，
  而 `WORKSPACE_CHANGED` 只在上一次 `rebind` 留下 `revalidation.pending` 时触发：于是
  "先手工改文件、再 rebind"会把人改的内容当成 agent 产物重新签证据，`accept` 只校验到
  篡改后的树。现在首次基准取自 `validated_sha`/`validated_fingerprint`，基准存在但不一致
  即 `BLOCKED/WORKSPACE_CHANGED`；从未签过证据的任务仍只换策略（没有基准不等于基准丢失）。
- **v3 组的 `group_id` 可绑定非成员任务。** 冻结 spec 校验只对
  `delivery_mode=group_member` 生效，绑定却无条件执行，且 `task accept` 的自验收拦截按
  `delivery_mode` 判断——一个 `local_artifact` + `group_id` 的任务能绑成成员并自行验收。
  现在 `create` 对 v3 组前置拒绝；legacy ordered 组沿用旧语义。
- **`group start` 的 `stop.resume_hint` 在 `review_ready` 上是死命令。** `review_ready`
  同时在 `_RESUMABLE`/`_NEEDS_HUMAN`，hint 恒为 `group start`，但 `begin` 只接受
  READY/PAUSED/BLOCKED、`resume` 不接受 REVIEW_READY。现在两个集合互斥，hint 按原因
  给 `group review`/`group accept`/`group repair`/`group status --refresh`。
- **`authorized_actions` 可被子串匹配绕过授权。** 该字段此前没有任何类型校验，只做
  `action in c.get(...)`：写成字符串 `"no-publish"` 时 `"publish" in "no-publish"` 为真，
  作者以为拒绝了、`publish`/`accept` 却全部放行。现在注册期按数组 + 枚举校验，
  `authorized_actions()`/`is_authorized()` 统一解释（非数组一律视为"什么都不授权"）。
- **`protected_paths: []` / `null` 静默关闭默认保护。** `.env`/`.github` 的默认保护只在
  **键缺失**时生效，显式空数组让保护集归零且无告警。现在两者都被拒绝，并提示删掉该键以
  保留默认值；`schemas/project-profile.v2.json` 同步加 `minItems: 1`。
- **`schema_version: true` 等价于 v1。** `True in (1, 2)` 为真、`True == 1` 为真，而 v1 会
  打开迁移专用分支（跳过 model 必填、跳过 controller-reserved 拒绝）。现在 `is_integer()`
  统一判定，布尔值按类型错误拒绝。
- **`group dispatch` 绕过依赖图。** 依赖只在 tick 里检查，`admission` 不校验，于是宿主可以
  在 `A` 未集成时直接派发 `B(depends_on=[A])`，B 的产物随后被合并进不含 A 的
  `integration_sha`。现在 `admission` 与 `dispatch` 都用同一个 `dependencies_met()`，且
  `dispatch` 在 create **之前**拦截，不留"已绑定、从未启动"的半写入成员。
- **spawn 失败后任务卡在 RUNNING 并永久占用并发槽。** `start` 先写 RUNNING + 预留 run 再
  spawn，失败时只 finish operation：之后 `reconcile` 只能报 `PROCESS_UNKNOWN`（`start`
  会拒绝它），而 `slots()` 仍按 RUNNING 计数。现在确定失败即回滚为 `BLOCKED` + 真实错误码
  并关闭 run。
- **失败处理自身抛异常（`SCOPE_VIOLATION` 归因）。** spec 缺失/损坏时在 `except` 里再抛，
  导致 BLOCKED 只存在内存、`pipeline_stopped` 审计丢失、任务停在 RUNNING。现在"读不到
  冻结 spec"按"无法证明策略过窄"处理（不豁免），不再二次抛出。
- **`finally` 里的 git/fingerprint 覆盖真实失败原因。** workspace 被删或 git 元数据损坏时
  异常从 `finally` 冒出，业务码被换成 `ENVIRONMENT_ERROR`，且 `handoff.json` 完全不写
  （连带 `reassign` 的 HANDOFF_STALE 校验失效）。现在交接落盘独立于 git 读取。
- **超时/停止/超限路径不关日志句柄、不写 `capture.json`。** `_collect_logs`/`_write_capture`
  只在成功路径执行，`agent.log` 未 flush 的尾部（排障最需要的几 KB）丢失。现在失败路径也
  收尾，且诊断自身失败不顶替业务错误码。
- **`validation.json` 在中断时丢失。** `STOP_REQUESTED` 与非结构化异常直接从 `run_checks`
  逃逸，`handoff.json` 于是报"没有失败项"。现在落盘先于任何 re-raise。
- **`progress.json` 被验证命令覆盖。** check 命令复用同一个 run id 却新建 `Observer`
  （`started_at`/`bytes` 归零），`task status` 于是展示最后一条 check 的静默时间并声称观测
  新鲜。非 agent 执行改用 `NullObserver`。
- **`--allowedTools` 的 argv 形态错误（`claude-code`）。** 整串拼成**一个** argv 元素，而该
  flag 是 `nargs="+"`，结果一个工具都匹配不上，`permission.enforced` 却写着 `true`。改为
  逐工具传参（与 `codebuddy` 的实测结论一致）。
- **版本比较把 `rc9` 排到 `rc10` 之后。** 后缀按字符串比较，对 `min_version` 是 fail-open
  （放行更老的运行时）。改为自然序，并让解析失败指明是 `min_version` 还是
  `max_version_exclusive`；空版本窗口给出独立原因。
- **`command()` 超时在 Windows 上永久挂起。** 只杀直接子进程，孙进程持有管道，随后无超时的
  `communicate()` 永不返回。现在 `taskkill /F /T` + 有界等待。
- **`stop_tree` 的二次 `wait` 无保护。** SIGKILL 后 `wait(timeout=5)` 抛 `TimeoutExpired` 会把
  `RUN_TIMEOUT`/`STOP_REQUESTED` 覆盖成 `INTERNAL_ERROR`。
- **已集成成员的冻结点在组记录损坏时失效。** `_require_member_mutable` 读不到组就 `return`，
  等于"读不到 = 未集成"，`revise`/`reassign`/`rebind` 可以改写一次已经发生的集成。现在
  fail closed（`kind=group_unreadable`）。
- **`orchestration patch` 可写 `owner`。** 记录里的 owner 变成字符串后，每次任务级 mutation
  都在 `require_owner` 抛 `AttributeError`，且绕过 `claim --force` 的审计语义。现在 owner
  移出可 patch 集合、其余键做形状校验、脏记录按"未认领"处理。
- **组预算的 repair 单元不计入释放。** `release_unstarted`/`_active_run_ids` 只遍历
  `runtime.members`，从未 launch 的 repair 会一直占着计费名额；`cancel` 也可能在 repair 仍
  在写工作区时判定"无活跃写者"。两处改用 `all_runtimes`。

## [1.5.0] - unreleased

授权边界批次：把"配过什么"与"允许用什么"分开，并让执行准入不再依赖配置摘要是否恰好变化。
三条破坏性变更会在注册或首次派发时报错，迁移步骤见 `docs/upgrading.md`。

### Changed

- **`routing.allowed_profiles` 必须显式声明。** 只要声明了 `routing` 段，就必须显式给出非空
  白名单，`explicit` 与 `auto` 一视同仁。旧行为"缺省 = 全部 profile"让"配过"等于"授权"：
  新加一个昂贵 profile 就自动成为候选，显式指定也挡不住。原来那条 `mode=auto` 的非空校验因为
  `allowed` 已被补成全量而恒真，是不可达的死校验，已删除。
- **`model` 与 `allow_cli_default_model` 二选一。** 两者同时出现报 `INPUT_INCOMPLETE`（该标志
  会被静默忽略）。`model` 的类型与形态同时收紧：空串、纯空白、前后空白、非字符串一律拒绝——
  `bool("   ")` 为真，只校验"非空"会把空白模型放进 argv。
- **`legacy` 不再可由用户声明（P0）。** `legacy` 在 `PROFILE_FIELDS` 白名单内且豁免 `model`
  必填，用户在自己的 `agent_profiles` 里写 `legacy: true` 就能注册成功并跟随 CLI 默认模型。
  现在 `schema_version: 2` 的配置声明 `legacy` 一律 `POLICY_DENIED`；schema v1 的迁移路径不变。
- **执行准入与摘要比较分离。** 新增 `_config.check_execution_contract()`，在 create/start/
  reassign 创建新执行尝试前先回答"这份配置本身合不合法"。旧配置可能一字未变却不符合新规则
  （两侧摘要相等），此前只能靠 `POLICY_CHANGED` 兜底，而那个码会让人去 rebind，然后撞上同一个错。
- **读路径不再因新增策略规则失败。** `store.config()` 改为 `strict=False`：结构问题照旧抛，
  新增的模型/授权规则记进 `policy_problems`。否则旧配置升级后连 `status` / `doctor` 都打不开
  （`store.config` 有 40 处调用点，覆盖只读命令）。

### Added

- **`zoe project attest`**：授权确认记录（`$ZOE_HOME/projects/<id>.attest.json`），绑定
  `policy_hash` + 已确认候选集合 + 运行时版本。声明了 `routing` 段的项目在首次派发前需要确认
  一次；记录与配置分开存放，所以"配置内容没变也能确认一次"，而 `rebind` 不写它（不把未知来源
  洗成已确认）。缺失或失效时派发返回 `ALLOWLIST_UNCONFIRMED`，`details.reason` 区分
  `missing`、`policy_changed_since_attestation`，以及 `candidate_not_confirmed`
  （`--profiles` 收窄确认之后，超出确认范围的候选会被拒绝——收窄会真正生效）。
  `task create` 的响应新增 `allowlist_source` 与 `policy_hash`，报告不必再去读记录文件。
- **`attribution.model_capability_comparable`**：把"原因已确认"与"模型身份可归属"拆成两个
  条件，再加"结果可评价"。此前最外层限定 `failure_class == "executor"`，使身份未知的成功运行
  与环境失败都被判为可比。`AGENT_FAILED` 兜底码不再进"身份与归因明确样本"的通过率，但失败
  事实、调用次数、耗时与已观测费用照常保留。`PerformanceRecord` 同步新增
  `attempt.n_attributed` / `attempt.pass_rate_attributed`（分母为 0 时是 `null`）、
  `counts.runs_cause_unconfirmed` 与 `reliability.executor_failures_unconfirmed`；
  原有 `pass_rate` 保留为全量运维口径，两者并存、互不覆盖。
- **占位符前置拒绝**：`REPLACE_WITH_*` / `<FILL:*>` / `/absolute/path/to/*` 在注册与派发前被
  拒绝，`details.fields[]` 指向具体字段；`doctor` 新增 `placeholders` / `placeholders_ok` 并
  计入 `ready`，不再"显示 ready、随后启动失败"。`doctor` 另新增 `policy_problems[]` 与
  `policy_ready`：环境就绪与"这份配置还能不能被当前版本派发"分成两个维度。

### Fixed

- **`"routing": null` 绕过两条守卫（P0）。** 规范化与 `attestation_status` 都按值判空，
  "键存在但为 null"因此被当成"没有 routing 段"：既跳过"白名单必须显式声明"，又拿到
  `legacy_config_is_authorization / confirmed: true`，无需 `project attest`。改为按键存在性
  判断，`null` 一律 `INPUT_INCOMPLETE`；决策期的 `allowed_profiles or sorted(profiles)`
  兜底也改为显式拒绝（`_routing.authorized_profiles`），避免同一条规则在规范化期强校验、
  在决策期默认放开。
- **确认记录不合规时 fail-open。** 旧实现用 `isinstance(confirmed_profiles, list)` 保护收窄
  比较：记录缺字段或类型不对时直接放行未确认的候选。现在读取侧按最小 schema 校验
  （`policy_hash` 非空字符串 + `confirmed_profiles` 非空 id 数组），不合规报
  `ALLOWLIST_UNCONFIRMED` 且 `details.reason: "record_invalid"`；写入侧同步拒绝坏记录；
  `Store.attestation` 区分"缺失"与"损坏"（损坏返回 `_corrupt` 记录，不再静默当作没有）。
- **组启动预算双扣，`max_starts` 可超发。** 释放一次未启动的预留同时做
  `starts_reserved -= 1` 与 `starts_released += 1`，而 billable 是两者之差——一次释放退还两个
  名额。现在 `starts_reserved` 单调，释放只增加 `starts_released`（审计账本不再回退）。
- **`policy_problems` 一次只报一条。** `normalize_config` 在第一个坏 profile 处抛出，
  `doctor` 因此要用户修 N 轮。改为逐 profile 收集（带 `agent_profiles.<id>` 定位），并把
  `Store.config(problems=...)` 接通——旧实现读路径根本没传列表，收集到的问题被直接丢弃。
  失败时配置保持原样，不再写回半规范化结果（那会让"配置非法"表现为"策略漂移"）。
- **源码模式的 supervisor 预检不可靠。** `supervisor_env()` 只绝对化已有 `PYTHONPATH`，
  不补包根；未 `pip install` 的仓库里 `import src` 必然失败，4 个准入/资格测试在本地红。
  现在从 `src/_pipeline.py` 位置推出包根并幂等追加。
- **`create` 用未校验的配置继续执行。** `_require_executable` 返回的规范化副本被丢弃，
  后续绑定用的是读路径原件；同时 `authorize` 排在配置合法性之前，占位符配置会先报
  `AUTH_DENIED` 再报 `INPUT_INCOMPLETE`。现在先校验（并使用返回值），再比对凭据。
- **CI 门禁转绿。** `ruff check`（13 项）与 `ruff format --check`（8 个文件）全绿；
  `_prepare.py` 未使用导入、`_config.py` UP038、`Store.tx()` 关闭后二次抛错、
  `validate_spec_text` 对非对象 `checks` 的裸 `TypeError`、`policy_problems` 结构不一致
  （占位符项缺 `code`）一并修掉；删除无调用点的 `_groups.release_member`。

### Docs

- 三档示例：默认（一个模型 + 显式白名单）、多模型、高级。旧示例缺少 `model`，照它复制出来的
  配置注册必然失败。
- `SKILL.md` 新增「执行配置与模型授权」；`runtime-contract.md` 新增同名节；
  `decomposition-patterns.md` 改为"角色只决定候选，不决定授权"；`report-template.md` 新增
  「执行配置」段。
- `docs/architecture.md` 补上漏记的 `src/_diff.py`。

### Renamed

- **产品显示名统一为 `Zoe Runtime`**，与发行名 `zoe-runtime`、命令 `zoe` 对齐。README
  （中英）、`docs/`、`--help` 描述、`zoe doctor` 人读输出、`SECURITY.md`、
  `CONTRIBUTING.md` 与 `NOTICE` 同步更新。发行名、命令、导入名 `src`、任务 ID 与状态目录
  均不变。
- 历史记录保持原样：0.5.1 之前的发行名 `zoe-lite` 与已删除设计稿的真实文件名
  `docs/Zoe-Lite-*` 是当时的事实，不改写。

## [1.4.1] - unreleased

发布阻断项的修复批次。**不加功能**：这一版只做「真实任务能完成、异常后能恢复、装完
能用」三件事所依赖的正确性与契约修补。

### Fixed

- **迁移守卫静默失效（P0）。** `_placeholder_runs` 用 `row["data"]` 读一个没有
  `sqlite3.Row` 工厂的连接，抛出的 `TypeError` 被同一个函数吞掉，返回值恒为 `[]`
  —— 存在 `spawn_intent`/`spawn_observed`/`running` 等活跃 run 时，`MAINTENANCE_REQUIRED`
  永不触发，停机迁移的前提被打破。改为按下标取值；**读不懂或非对象的 run 记录现在是
  `SCHEMA_UNSUPPORTED` 阻断**，而不是「当作没有活跃执行」。
  回归测试走 `migrate()` 真实入口，并断言被拒绝后 `user_version` 未被推进。
- **`Store` 没有关闭路径。** 新增 `close()` 与上下文管理器，`main()` 在 `finally`
  里关闭连接；`watch --follow`、`internal-group` 这类长驻路径不再依赖进程退出回收。
- **错误通道不一致。** 运行器的结构化错误走 stdout（同一个 `output()`），而三个包装器
  写 stderr，宿主必须合并两路流。现在包装器也写 **stdout**，退出码语义不变。
- **`zoe.cmd` 的固定临时文件竞态。** 版本探测与预检日志改用带随机段的一次性文件名：
  并发调用不再可能读到彼此的运行器版本。

### Changed

- **runner 退出条件是公开契约。** 生产循环按终态 / 显式暂停 / 真实阻塞 / 已声明预算 /
  租约被接管 / 本轮无可推进项退出；`MAX_TICKS` 降级为兜底（`SAFETY_MAX_TICKS`），
  命中时 `exit_reason=safety_cap` 并说明「推进停止、任务仍未完成」。旧行为是
  `2000 × 0.2s ≈ 400s` 后静默结束，宿主的「一次启动持续推进」在预算之外被截断。
  返回值新增 `exit_reason` / `completed` / `stop{reason,explain,resumable,needs_human,
  resume_hint}`，并持久化到 `runtime.lease.exit_reason`；`group status` 的 `lease`
  视图同步暴露 `exit_reason`/`ticks`/`exited_at`。
- **设计一致性审查的执行方是本版显式选择：宿主在独立上下文执行**，Runtime 只校验。
  没有结论时组的退出原因是 `awaiting_design_review`（不是"运行中"，也不是"完成"）。
- **文档口径统一。** `README.md`、`README.zh-CN.md`、`docs/architecture.md` 曾声称
  "未验证 profile 在 `start` 阶段直接返回 `ADAPTER_UNVERIFIED`"，与
  `docs/capabilities.md`、`SKILL.md` 及 `_profiles.require_runnable` 的实际行为
  （默认放行并标 `unverified`，`require_real_e2e: true` 才拒绝）相反。以后者为准。

### CI

- `ci.yml` 增加 `ruff format --check .`（此前只在本地 `make fmt` 里，36 个文件已漂移）。
- `contract.yml` 明确两种方向：**已发布兼容下限**（本构建 × 最低支持的已发布 skill，
  阻断）与**前沿漂移**（本构建 × skill `main`，`continue-on-error` 不阻断）。

## [1.4.0] - unreleased

补齐 TGD §11（与设计文档一致的验收）第二层与 §12（修复闭环），把
「任务局部通过 → 组级自动检查 → **设计一致性审查** → 一次验收」做成一条链。
**不含**并行成员、PR 组、自动重试；审查由宿主在独立上下文执行，Runtime 只校验。

### Added

- **`group review`（设计一致性审查，§11.1/§11.2）**：宿主提交审查结论，Runtime 校验
  三条硬规则——`subject_sha` 必须等于当前 `integration_sha`、`design_digest` 必须等于
  冻结设计摘要、计划里每个必需 AC 必须出现且只出现一次。`evidence_refs` 只接受
  `check:<id>`（在当前版本真的 PASS）/ `group:<sha>` / `task:<id>`（本组已集成成员），
  编造的引用与缺失的引用一样被拒。`locations` 只接受仓库相对路径。
- **无效输出留档并报错**：`EVIDENCE_INVALID`（`details.kind=review_invalid`，附
  `errors` 全集），同时记 `runtime.reviews[]` 的一条 `invalid` 记录。**它不会变成一次
  通过**，也不能被静默丢弃。
- **`review` 计划块与门禁**：`{"required": true, "mode": "agent|human|either"}`。要求时，
  没有有效结论就不能进入 `REVIEW_READY`（组退回 `RUNNING`，阻塞项为
  `DESIGN_REVIEW_REQUIRED` / `DESIGN_REVIEW_FAILED`），`group accept` 一并拒绝。
  结论落盘在 `groups/<gid>/reviews/<subject_sha>.json`，`items_digest` 被改写即作废。
- **`group repair`（有界修复，§12）**：必须绑定当前版本审查里的一条 `FAIL` finding，
  一次执行只修一条，受 `budget.max_group_repairs` 约束（缺省 **0**，即不授权）。
  没有 finding 的改动返回 `kind=repair_without_finding`（新增需求走计划修订）。
  修复单元存在 `runtime.repairs` 而不是 `definition`：往冻结计划里插成员等于篡改
  `plan_digest`。修复与成员走**同一条** `dispatch`/`integrate` 路径，排在所有成员之后。
- **`schemas/group-review.v1.json`** 与 `capabilities.task_groups.design_review` /
  `.group_repair`；`features` 新增 `group_design_review`、`group_review_contract`、
  `group_bounded_repair`。

### Changed

- `group status` 增加 `review`（含 `state`/`reason`/`unresolved`/`not_verified`/
  `required_acceptance_ids` 由 CLI 补充）与 `repairs`；不再声称"设计审查恒为 NOT_RUN"。
- `group report` 的设计审查段落如实渲染七种状态（`NOT_REQUIRED` / `NOT_RUN` /
  `STALE` / `INVALID` / `PASS` / `NOT_VERIFIED` / `FAIL`），并新增逐条偏差表、
  修复单元清单与"组级修复轮数 x/y"。
- `plan_digest` 只在计划**显式声明** `review` / `budget.max_group_repairs` 时纳入它们，
  因此 v1.3 计划的摘要逐字节不变（§19.3：新能力字段默认缺失，不自动注入）。
- `capabilities.task_groups.not_supported` 移除 `design_conformance_review`（已交付），
  改为 `unbounded_dynamic_task_graph`。
- 未声明 `review` 块的计划会在 `group create` 的 `warnings` 里被点名——"这一层没参与
  验收"必须是可见事实，而不是默认值带来的静默。

### Migration

- 旧组无需迁移：缺 `runtime.reviews` / `runtime.repairs` 即"无审查、无修复"，行为与
  1.3.0 一致。schema 仍为 v4，无 DDL 变更。
- 已冻结的计划不会被追加重试审查门禁：只有新计划声明 `review.required` 才生效。

## [1.3.0] - unreleased

任务组交付（TGD）：把「计划冻结 → 单一推进器 → 串行集成 → 组级检查 → 一次验收」做成一条可
审计的链。**不含**并行成员、PR 组、自动重试与设计一致性审查。

### Breaking

- **状态库 schema 升至 v4**：DDL 未变，v4 是**语义闸门**——v3 组记录把成员放在
  `definition` 下，v1.2 二进制会把它读成"0 个 item"。打开 v1/v2/v3 库返回
  `MIGRATION_REQUIRED`，需 `zoe state migrate --to 4`（该命令的 `--to` 默认值现在是当前
  schema）。迁移**不改写**组记录，遇到无法识别的 `record_version` 直接
  `SCHEMA_UNSUPPORTED`（猜测 `depends_on` 不是迁移，是编造）。
- `capabilities --json` 新增 `task_groups` 与 `features`；`runtime-requirements.json` 新增
  `required_capabilities`（按名声明所需能力，未支持即 `RUNTIME_INCOMPATIBLE`）。
- **`task accept` 拒绝组成员**：`delivery_mode=group_member` 的任务返回
  `STATE_CONFLICT`（`details.kind=group_member`），由 `group accept` 统一收口。
- **已集成成员被冻结**：`task revise/reassign/rebind` 在成员已合入集成分支后返回
  `STATE_CONFLICT`（`details.kind=integrated_member`），修正走新组。
- 退出码新增 `INTEGRATION_CONFLICT`(5) / `INTEGRATION_CHECK_FAILED`(5) /
  `INTEGRATION_UNKNOWN`(5) / `GROUP_BUDGET_EXHAUSTED`(5)。四个集成失败码同为 5：
  \"交付因人工必读的原因停下\"是一个退出状态，不拆两种。

### Added

- **组计划契约（`group plan --project P --request-file plan.json`）**：校验 + 路由预演 +
  基线快照，**不写任何东西**。校验全有或全无：`task_key` 唯一、依赖图无环、每条需求至少被
  一个成员覆盖（`details.kind=coverage_missing`）、每个 AC 被完整映射且 check 型 AC 的
  `check_id` 可解析、成员 spec 的 `allowed_paths` 只能收紧、`expected_paths` 落在项目目录
  边界内、`budget.max_starts` 必须显式正整数。
- **计划与运行分离**：`definition`（不可变）与 `runtime`（任务绑定、集成 SHA、租约、预算计数、
  检查结果）分账；`plan_digest` 只覆盖不可变投影，记一次启动或集成一个成员都**不会**让计划
  失效。冻结产物：`groups/<gid>/plan-r001.json`、`design-manifest.json`、`baseline.json`、
  `specs/<task_key>.json`。
- **组成员任务**：`delivery_mode=group_member`，`spec_file` 指向冻结快照（被改动即
  `SPEC_CHANGED`），记录 `consumed_input_sha`（建任务时上游已集成到哪），停在
  `REVIEW_READY`。
- **推进器**：`group start` 起一个短命进程，先写入租约（`lease_id`/`pid`/`start_token`）
  再派生；进程退出即停止推进（不是守护服务）。`tick` 顺序固定：对账 → 串行集成 → 组级检查 →
  派发 → 派生状态。派发门禁取 `min(组内空闲, 全局空闲)`，一个组同时只有一个成员在跑；
  `group dispatch` 与 `tick` 走**同一个**准入函数。
- **租约 CAS**：被接管的推进器写入返回 `STATE_CONFLICT`（`kind=lease_stale`）；进程不可
  观测时返回 `PROCESS_UNKNOWN`（`kind=runner_unknown`），不猜"大概没在跑"。
- **预算**：`max_starts`（计费启动额度，未产生 Agent 进程的启动会释放并记
  `release_basis`）与 `max_wall_seconds`（含机器休眠的墙钟时长）。预算只门禁新派发；
  对账、集成、组级检查、人工验收不受影响。
- **暂停/恢复/取消**：`group pause [--stop]` 默认 drain 并给出 `active_run_ids`；
  `group resume` 重新预检 → 重新对账 → 换新租约（`policy_revision` 变化即显式接纳）；
  `group cancel [--stop]` 在有活跃 run 时返回 `accepted` + `draining:true`；
  `group cancel --external` 记录 `external_close`，固定 `accepted:false`。
- **串行集成 + 三段账本**：`intent → prepared → done`（失败分支 `conflict`/`failed`）。
  `prepared` **先于** `update-ref` 落盘，所以"引用已移动但记录未提交"可恢复；
  "引用移动了却没有 `prepared` 记录"返回 `INTEGRATION_UNKNOWN`（`kind=intent_ref_moved`），
  不猜。合并冲突保留 `conflict/` 现场，快检失败保留 worktree。
- **组级检查绑定 `subject_sha`**：`NOT_RUN` 与另一个 SHA 的通过记录都不算通过；验收会重算
  集成工作区指纹，指纹不符即拒绝。
- **`group accept`（一次验收）**：要求 `REVIEW_READY`、集成分支引用 == 记录的
  `integration_sha`、工作区 HEAD 与指纹一致、当前 SHA 检查全 PASS、无阻塞、非空
  `evidence_refs`（`group:<sha>` 或本组成员 `task:<id>`）与 `actor`/`authorization_ref`；
  未满足时一次性列出全部条件。`rejected` 只记录，组保持 `REVIEW_READY`。
- **`group report`**：固定七段（需求覆盖 / 成员与执行组合 / SHA 链 / 组级检查明细 /
  阻塞历史 / 预算与时间 / 交付边界声明），设计一致性审查恒为 `NOT_RUN`。
- **drain 是真的门禁**：`group pause`（不带 `--stop`）写 `runtime.draining`，`dispatch_gate`
  与组级准入都会读它——先前它只出现在响应里，活跃 run 一结束下一个成员照跑。drain 期间
  槽位空闲也拒绝新成员（`STATE_CONFLICT` / `kind=group_state`），最后一个成员到安全点后
  组才落 `PAUSED`，只有 `group resume` 能离开。
- **§11.2 信封统一**：`envelope()` 一次性给出 `revision`/`plan_digest`/`blockers`/
  `progress`/`next_actions`/`event_cursor`；`pause`/`resume`/`cancel`/`accept`/
  `close_external` 不再各缺几项（`close_external` 之前连 `plan_digest` 都没有）。
- **成员证据按祖先关系归属**：串行集成给每个成员各自的 `integrated_sha`（只有最后一个等于
  `integration_sha`），验收要求\"等于`\"会让多成员组的任何 `task:<id>` 证据都不可引用；改为
  \"等于或祖先\"，仍拒绝别的 SHA、别的组的任务和缺失的证据文件。
- **`accept` 二次校验 `delivery_mode`**：创建期拒绝 `pull_request` 之外，验收也拒绝非
  `local_artifact` 记录（防 legacy 记录或人工改库绕过）。
- **检查分类（F3）**：每条检查结果带 `error_class`（`test` / `environment` / `timeout` /
  `policy`），`handoff.failed_checks` 同步携带。
- **认证声明（F5）**：profile 可声明 `auth_mode`（`cli_login` / `api_key` / `unknown`），
  只在声明时校验、不回写默认值；`api_key` 缺 `credential_refs` 即 `AUTH_REQUIRED`；
  `login_state` 只有成功的 `agent probe` 才能给 `present`。
- **任务组 CLI**：`group plan|create|start|dispatch|status|report|pause|resume|cancel|accept`
  与内部入口 `internal-group`。旧有序组（`ordered_items`）保持可读、可查询、可 dispatch，
  但拒绝 `depends_on` 且不能 `start`。

## [1.2.1] - unreleased

修复"策略变更后任务既不能重验也不能验收"与"配置类失败吃掉实现预算"两个阻塞，
来自 Wave 0 实战复盘。**不含** F3（check 三态）、F4 修复路径建议、F5 认证声明。

### Added

- **`rebind` 的显式重验（F1）**：`delivery_mode=local_artifact`、无活跃写者、
  `HEAD`/`fingerprint` 与已记录产物一致时，响应 `revalidate.pending=true`，任务
  置为 `READY/verify`。`resume` 只重跑 checks 并重签证据，**不重启 Agent**。
  产物基准写在 `revalidation.product`，**第一次** rebind 落盘、后续 rebind 复用
  ——否则第一次 rebind 清空 `validated_fingerprint` 后第二次就失去判据。
  不可重验时返回 `revalidate.blockers`（已发布交付 / 写者未收口 / 产物漂移），
  状态不变；已发布/已合并的交付仍按真实远端状态对账，不凭 `phase` 回退。
- **`task close-external`（F1-b）**：对账"交付在 Zoe 之外发生"的任务。字段
  `external_ref`/`actor`/`claim(verified|asserted)`/可选 `merged_sha`；进入
  `CANCELLED` + `block_code=CLOSED_EXTERNAL`，响应固定 `accepted: false`，
  报告渲染"外部收口（未通过 Zoe 验收）"。仅在没有活跃 run、`block_code` 不是
  `PROCESS_UNKNOWN` 时可用；**不是验收**，不计入 `DONE`。
- **预算豁免（F2）**：`budget_waivers[{run_id, code, basis, at}]`。门禁与
  `remaining_attempts` 改用 `attempts - 有效豁免数`；同一 `run_id` 最多豁免一次，
  重放/崩溃恢复不重复调整。
- **越界全量上报**：`scope()` 一次性列出全部越权路径，`details` 带
  `violations`/`missing_paths`/`allowed_paths`；`protected_paths` 优先判定且
  永不进入 `missing_paths`。

### Changed

- **恢复前判定按责任分派**：策略类阻塞（`SCOPE_VIOLATION` 归因为策略过窄、
  `VERIFICATION_NOT_APPLICABLE`）在扣预算前要求策略已变；环境/认证类
  （`AGENT_AUTH_REQUIRED`、`ENVIRONMENT_ERROR`）**不要求**策略变化——重新登录、
  修 PATH 之后必须能继续。归因按证据：spec 是否显式要求该路径、check 是否被 AC
  引用；无法确定则不豁免。
- `rebind` 不再无条件清空 `block_code`，并把上一轮阻塞留档到 `last_block`
  （`code`/`basis`/`run_id`/`policy_revision`），供恢复前判定使用。
- `revise` 与成功的 `verify` 清空 `last_block`：新 spec 或已验证通过都意味着
  上一轮阻塞已处理。
- 报告中 `attempts` 旁显示豁免次数；`billable_attempts`、`waived_runs`、
  `external_close` 进入快照。

### Migration

- 旧任务无需迁移：新字段缺省即"无豁免、无重验基准"，行为与 1.2.0 一致。
- `attempts` 仍是单调的真实启动次数，历史统计口径不变；`max_attempts` 的判定
  从 `attempts` 改为 `attempts - 豁免数`，因此**曾经被配置错误烧掉的预算不会
  自动返还**（豁免只对发生在新版本的 run 生效）。

## [1.2.0] - unreleased

执行组合路由（execution-combination routing）。核心不是"支持多模型"，而是把
**实际跑起来的那套东西**（适配器 + backend + 实测 CLI 版本 + provider/model/
variant/effort + 权限/工具 + 端点）变成一个可标识、可冻结、可复核的对象。

### Added

- **`combination_id`（`cmb-<16 hex>`）**：组合的稳定标识，可复现、无绝对路径、无凭据；
  缺省字段不出现，因此同一组合换机器仍是同一个 id，而改 model/版本/端点必然不同。
- **`routing` / `endpoints` 两段配置**：`mode`、`allowed_profiles`、`rules`、`fallback`、
  `max_assignments`、`cost_ceiling`、`spend_guard`、`reject on use_performance=true`。
  两段的默认值**只在段存在时**写回，没有这两段的配置 `policy_digest` 与 v1.0.0 逐字节一致。
- **`RouteDecision v2`**：`route-<rev>.json` 记录 `decision`/`reason`/三层排除明细/
  `order`/`snapshot_digest`/`content_sha256`。`layers` 只有 `l1_excluded`/`l2_excluded`，
  **没有** `l3_excluded`——历史统计本版不参与选择。
- **`route plan` / `route stats`**：预览决策（不建任务、不写文件、不发事件）与只读聚合
  （`combination_id × task_type × spec_scale`，`used_for_selection=false`）。
- **模型观测**：`AgentResult.reported_model(s)`；`model_identity` ∈
  `reported_match|reported_mismatch|mixed|unobserved`，`observation_scope` ∈
  `final_result|main_call|mixed|none`。"工具没说"是 `unobserved`，永不算匹配。
- **闸门 A（模型身份）**：`pin_model` 下只有 `reported_match` 进入验证；`unobserved`
  同样阻断（`MODEL_MISMATCH`），且不产生 `candidate_sha`。
- **闸门 B（费用）**：`reported` / `estimated` / `unavailable` 三分口径；配了上限而成本
  不可观测 → `SPEND_UNVERIFIABLE`，越线 → `SPEND_LIMIT_EXCEEDED`；币种不可比不比较。
- **工作区隔离**：`start` 落盘 `workspace_baseline`；闸门拒绝时把改动归档为
  `runs/<id>/worktree.patch`（`add -A` + `diff --cached --binary`），**可还原性校验通过**
  才恢复干净；启动时本就有未提交改动则不清理，只报告。
- **归因**：`attribution.{attempt_metrics,delivery_metrics,reason}`；probe 运行、
  环境/基础设施/权限/规格失败不进指标；未观测模型的 run 只进可靠性，不进质量结论。
- 报告新增五行：执行组合 / 集成事实 / 模型观测 / 选择依据 / 质量数据。

### Changed

- **profile 必须声明 `model`**（或显式 `allow_cli_default_model: true`）。这是本版唯一
  破坏性变更：缺省时适配器根本不传 `--model`，等于批准了一个描述不清的组合。
  旧式 v1 配置（顶层 `agent`）自动迁移为 legacy profile，行为等同于显式开启逃生口，
  并**恒为** `model_unresolved=true` / `combination_verified=false`。
- **声明的执行字段必须能落到有效配置**：`provider`/`model_variant`/`reasoning_effort`
  一旦声明而适配器无法映射，注册期即 `CAPABILITY_UNSUPPORTED`（拒绝，不静默忽略）。
  `opencode` 支持 `provider`（`--model provider/model`）；`codex` 目前不支持任何一项。
- `PROFILE_FIELDS` 现在是 `_agents.base` 中的唯一真相（此前 `_profiles` 与
  `validate_profile` 各维护一份，会静默漂移）。
- `run_pipeline` 在真正冻结时重算 `combination_id`（实测版本 ≠ pin 版本 → 两个组合）
  并记录 `combination_changed`。

### Not supported（逐条列出，而不是含糊地说"支持路由"）

`history_based_preference`、`hard_endpoint_residency_enforcement`、
`automatic_l3_model_attribution`。

## [1.0.0] - unreleased

The version jumps from 0.5.2 to 1.0.0: the CLI surface, the on-disk state (v3)
and the skill contract (`api_version` 2 / `protocol_revision` 1) are declared
frozen from here on. 0.5.1 and 0.5.2 were never released: this section carries
their content plus the fixes made while validating the host skill against the
real `codebuddy` CLI (2.111.0). Nothing here changes the writer model: still one
writer per task.

### Added

- **Explicit verification scope (`verification_scope`).** A structured spec may
  declare `{"checks": ["lint", "slice_a"]}`. Checks outside the declaration are
  recorded as `NOT_RUN` — neither pass nor fail — and are *not executed*.
  Omitted key = full run, unchanged. The declared list must be non-empty, must
  reference registered check ids, and must cover every
  `acceptance_criteria[].verification.check_id`; otherwise `INPUT_INCOMPLETE` at
  `create`/`revise`. The verdict is strict: the declared set must be covered
  exactly once and every row must be `PASS` — "at least one PASS" never counts.
- **Frozen semantics marker.** `spec-<rev>.json` gains a fourth key,
  `verification_semantics` (1 = full, 2 = scope-aware), written at `create` and
  `revise`. A revision frozen by v0.5.0 or earlier has no marker and is therefore
  full: a scope written into its text is ignored, not silently honoured. Unknown
  values are `SPEC_CHANGED`, not guesses.
- **Evidence carries the scope.** `evidence.json` gains `selected_check_ids`,
  `not_run_check_ids` and `verification_semantics`. `verify` recomputes the
  expected set from the frozen spec text and refuses evidence whose selection
  disagrees with it, whose ids overlap or do not cover the configured checks, or
  whose selected rows include a failure. `check_policy_hash` still describes the
  full project policy, so old and new evidence stay comparable.
- **`handoff.json` gains `not_run_checks`** (with the declaration reason) and
  `failed_checks` now means *failed*: `NOT_RUN` is no longer filed as a failure.
- **Delivery report** (renderer in `_reports.py`, surfaced by `task report`).
  `task report --format markdown` renders AC verdicts
  (`PASS` / `FAIL` / `INVALID` / `无本期证据` / `未记录逐项人工结论`), check detail,
  blockers and an explicit boundary statement; `--write` persists
  `tasks/<task>/delivery-<spec_revision>.md`. `--format json` without `--write`
  is still the task record, unchanged.
- **Concurrency snapshot.** `capabilities.concurrency` reports
  `max_supported` / `default` / `count_scope: global` / `limit_source:
  project_config`; `orchestration resume` and `group plan` gain `slots`
  (`limit` / `active_global` / `free` / `active_tasks`). These are snapshots, not
  admission: `start` re-checks inside its critical section.
- **`error.hint`** for the five most common codes (`INPUT_INCOMPLETE`,
  `STATE_CONFLICT`, `SLOT_BUSY`, `POLICY_CHANGED`, `ADAPTER_UNVERIFIED`).
- **`--request-file -`** reads the request from stdin, in one place, for every
  command that accepts the flag.

### Fixed

- **Dispatch replay.** `task start` / `task resume` now probe idempotency before
  taking the writer lock and again (bounded, 1 s) if the lock is held. A repeated
  request returns the finished record byte-for-byte, or
  `{"dispatch_pending": true, "run_id": null}` when the intent is still open —
  never `TASK_BUSY`, and never `row_version` at the top level. A replay writes
  nothing: no new run, no event, no state change.
- **`TASK_BUSY` carries `details`** (`task_id` / `state` / `phase` / `run_id` /
  `dispatch_state` / `hint`). A crashed intent is no longer reported as
  "already running": the snapshot says what the task actually is.
- **Abandoned dispatch intents are collected by `reconcile`.** An intent older
  than `INTENT_GRACE_SECONDS` (2 × `SUPERVISOR_LOCK_WAIT`) on a task that is not
  `RUNNING`/`PAUSING` is closed out as `PROCESS_UNKNOWN`; nothing is re-spawned,
  and a late `finish()` from the original process may still overwrite it.
- **Check execution errors keep their diagnostics.** A timeout (or any other
  error) while running a check is recorded as `FAIL` with `error.code` and
  `validation.json` is written before the error is re-raised, so the task-level
  code (`RUN_TIMEOUT`, …) and the per-check detail both survive. `STOP_REQUESTED`
  still aborts immediately.
- **`codebuddy` can edit files unattended.** The adapter bound `isolated-edit` to
  nothing, so a `-p` turn had no approval channel: measured on 2.111.0, 35 turns
  ended with `Permission denied` for `Write`/`Bash` and no file created. It now
  passes `--permission-mode acceptEdits` (and `--allowedTools` when
  `approved_tools` is set) — deliberately **not** `-y`/`bypassPermissions`, which
  would turn Q3 into "allow everything". Adapters declare `permission_switch`;
  `require_runnable` fails closed with `PERMISSION_PROFILE_UNBOUND` when a switch
  exists but is unbound. `opencode` (no switch) keeps `enforced=false` and stays
  dispatchable.
- **`codebuddy` turns are observable while they run.** The capture used
  `--output-format json`, which buffers the whole session and writes it only at
  exit — measured 0 bytes for the entire turn, then 26 KB at once. `task logs`
  was therefore blind for the whole implement phase (one run showed a 145 s gap
  in the event stream with no bytes at all). It now uses
  `--output-format stream-json` (one JSON object per line, realtime): the log
  grows in step with each tool call, `parse_output` reads the trailing
  `type=result` line, and the same turn produced 10.7 KB instead of 26.7 KB.
- **A plain-text agent failure is classified, not called invalid.** `codebuddy`
  exits **0** with one line (`Authentication required. Please use /login ...`)
  when its login state is missing. The parser classified that as
  `AGENT_RESULT_INVALID`; it now runs `classify_failure` on a non-JSON capture and
  reports `auth_error` → `AGENT_AUTH_REQUIRED`. Terminal objects in the
  `--output-format json` session array are located by structure, and "a message
  with a `type` field" is no longer mistaken for a terminal object.
- **`PROCESS_UNKNOWN` no longer overwrites the real failure code.**
  `run_pipeline`'s finalizer wrote `block_code=PROCESS_UNKNOWN` whenever the tree
  looked uncertain, burying `AGENT_AUTH_REQUIRED` / `CHECK_FAILED` / … A specific
  code is kept and the uncertainty is recorded separately as
  `execution_domain=unknown` + `run.status=unknown`.
- **Process-group observation no longer depends on `ps`.** `_tree_remaining`
  parsed `ps -A -o pgid=`; on a host without `ps` it returned "unknown" for every
  run, so every task ended `PROCESS_UNKNOWN` (and `stop`/`qualification
  stop-resume` could never be confirmed). It is now `killpg(pgid, 0)`, a pure
  syscall with the same semantics, including "reaped leader, surviving child".
- **`doctor` really probes the tree.** `platform_tree` only tested
  `process_identity` + `alive`, so a host with broken tree observation still
  reported `ready: true`. It now spawns a short-lived child and requires both
  "alive" and "empty" to be answered correctly.
- **Process identity no longer needs `ps` on macOS.** `process_identity` shelled
  out to `ps -p … -o lstart=`, so a host where `ps` is restricted required a
  hand-written shim before the controller would start at all. macOS now asks
  `libproc` (`proc_pidinfo`) directly, in the same `sec.usec` format a shim
  produced (existing run records still reconcile); `ps` remains the fallback for
  other POSIX hosts and its absence is reported as an empty identity, never as a
  fabricated one.
- **`task recover` no longer treats "cannot tell" as "already dead".** A
  `tree_alive() is None` answer is reported as `unknown` and refuses to close the
  run — observation failure is not a release condition.
- **Events carry the blocking reason.** `pipeline_stopped` / `run_finalized` and
  every other state event now include `block_code` and `last_error`, so
  `task events` is usable as the diagnostic record the skill claims it is.
- **The measured CLI version is recorded and compared.** `pin_launcher` never
  passed `parse_version`, so `cli_version` was always `null` and
  `expected_version` was never checked against the binary actually launched.
  Mismatches are recorded (`cli_version_mismatch`, `version_drift` event) and are
  a hard error only when a project opts in with `verify_cli_version: true`.
- **Interactive-shell discovery waits long enough.** `RESOLVE_TIMEOUT_SECONDS`
  3 s → 5 s: a login shell with a framework takes ~1.4 s, and the 3 s budget
  produced intermittent "CLI not found" under load.

### Documentation

- Replaced the "single execution slot / serial only / no parallel writers" claims
  in `SKILL.md`, both READMEs, `references/runtime-contract.md` and
  `references/decomposition-patterns.md` with the real model: one writer per
  task, global active count, project-scoped limit.
- `references/cli-contract.md`: concurrency table (`1..8` / `global` /
  `project_config`), replay semantics, `TASK_BUSY.details`, report surface.
- `references/delivery-policy.md` / `report-template.md`: `NOT_RUN` is not a
  pass, project-wide verification must be stated separately, replay is not a new
  dispatch.
- `docs/adapter-qualification.md`: tested-combination table that keeps the stub
  CLI row apart from the (still empty) real CLI row.
- `docs/overview.zh-CN.md`: marked `SUPERSEDED` (the file was later removed with
  the other pre-split documents — see "Docs cleaned up" below).
- `tests/test_doc_contracts.py` now gates the above: banned phrases, required
  scope/verdict wording, and the concurrency table cells are parsed and compared
  against `MAX_ACTIVE_TASKS`.

### Split — runtime and skill are separate repositories

The repository used to contain both the runtime and the host skill. It now contains
only the runtime; the skill lives in `zoe-workflows` (v5.0 of the split plan).

- **Added `zoe capabilities --check-requirements FILE`.** The runtime parses the
  skill's `runtime-requirements.json` (structured fields, no PEP 440, stdlib only)
  and exits `0` / `5` / `2`. This is the only command-level compatibility check at
  run time; the command list is a CI concern.
- **Exit code `5` for `RUNTIME_INCOMPATIBLE`** — an environment-class rejection,
  not bad input.
- **Removed from the runtime package:** `SKILL.md`, `references/`, `scripts/`,
  `agents/`, `assets/`, `build.py`, and the skill-install code path
  (`build.py --install`, the `.build/openclaw` variant). The sdist asserts their
  absence, so a release cannot ship a second copy of the skill.
- **Cross-repo tests.** `tests/contract/test_skill_cli_contract.py` validates every
  command line the skill documents against the parser (four expected outcomes:
  red/red/green/green); `tests/test_doc_contracts.py` and the skill-tree checks in
  `tests/test_contract_artifacts.py` resolve the skill checkout through
  `ZOE_SKILL_DIR` and **skip** when it is absent.
- **`tests/test_distribution.py`** encodes the release verification: an installed
  wheel, outside the source tree, with a scrubbed `PYTHONPATH`, must pass
  `--version`, `capabilities --json`, the requirements gate (accept and reject) and
  the `demo` guardrails.
- **CI:** `ci.yml` (ubuntu/macos/windows, wheel-from-sdist rebuild, artifact
  install on three hosts), `contract.yml` (this build × a fixed skill tag),
  `release.yml` (TestPyPI stage, then PyPI stage, same artifacts).
- **Docs:** `installation.md`, `upgrading.md`, `releasing.md`, `compatibility.md`.

### Changed — package layout

The package directory is now `src/` itself, so the **import name is `src`**:

- `from src.cli import main`; entry point `zoe = "src.cli:main"`; module entry point
  `python -m src`. The `zoe` command, task ids and the state directory are unchanged.
- Hatchling reads the version from `src/__init__.py`, the wheel ships `packages = ["src"]`,
  and the sdist includes `src/**`.
- `PYTHONPATH` must now point at the **repository root** (the parent of `src/`), not at
  `src/`; every test harness, the supervisor spawn (`-m src`) and `ZOE_SRC` were updated
  accordingly.
- Known consequence, accepted deliberately: the wheel installs a top-level `src` package
  into `site-packages`, which does not follow the usual "distribution name == import name"
  convention and could collide with another project doing the same.

### Renamed — distribution name

The PyPI name `zoe` was already taken (an unrelated OpenGL rendering package), so the
distribution is published as **`zoe-runtime`**. Per the split plan only the distribution
name changes:

- `pipx install zoe-runtime` / `pip install "zoe-runtime==0.5.1"`; artifact filenames
  become `zoe_runtime-0.5.1-*`.
- The **command stays `zoe`**, the import name stays `src`, task ids and the state
  directory are unchanged.
- The skill's `runtime-requirements.json` now declares `"package": "zoe-runtime"`.
- Entries above predate the rename and were written when the name was `zoe-lite`;
  nothing was ever published under that name, so no user-visible migration is needed.

### Docs cleaned up

`docs/` now holds only current reference material (10 files, ~76 KB). Six
pre-split documents were removed because each described a state that no longer
exists — the old `src/zoe_lite` layout and the in-repo `references/`:

| Removed | Why |
|---|---|
| `docs/overview.zh-CN.md` | marked `SUPERSEDED`; v0.2.0 baseline snapshot |
| `docs/release-v0.2.0.md` | v0.2.0 *plan*; referenced `scripts/zoe_runtime/core.py` |
| `docs/specs/v0.4/**` (10 files) | v0.4 contracts, superseded by `architecture.md` + the skill's `references/`; upstream links already dangling |
| `docs/Zoe-Lite-v0.3.0-*.md` (2 files) | design drafts for a superseded architecture |
| `docs/Zoe-Lite-v0.5.0-研发详细设计.md` | design draft, superseded by v0.5.1 and by the code |
| `docs/Zoe-Lite-v0.5.1-优化修复详细设计.md` | design draft; its actionable content is in `architecture.md`, `capabilities.md` and this file |

They were archived **outside** the repository before deletion. `test_doc_contracts`
now fails if a live doc links to any of them.

### Tooling

- **Added `Makefile`** — self-documenting targets (`make help`) that mirror the CI
  steps: `setup`/`install`, `test`, `contract` (cross-repo, `SKILL_DIR=...`),
  `lint`/`fmt`/`check`, `build`/`sdist-check`/`verify-dist`/`release-check`,
  `install-cli`/`uninstall-cli`, `demo`/`version`/`clean`/`distclean`.
  `make` prefers `.venv` when it exists and falls back to the ambient `python3`.
  `fmt` is deliberately not part of `check`: the tree is not `ruff format` clean
  yet (~24 files), so formatting stays a separate, reviewable change.
- **Fixed a latent CI failure** in `tests/test_v041_admission.py`: the precheck
  test simulated a broken environment with a non-existent `PYTHONPATH`, which only
  fails while the package is *not* installed. Under `pip install -e .` (CI, any
  `.venv`) `import src` resolved from site-packages and the assertion never fired.
  The test now shadows the package with a broken `src.py`, so it is
  environment-independent.

## [0.5.0] - unreleased (Preview)

Two honesty fixes and one capability: the product stops advertising an execution
backend it does not exercise, the binary `doctor` reports becomes the binary
`start` launches, and pre-publication remote drift detection finally exists (it
was in the error-code table since v0.2 but never implemented).

Scope note: native session resume (M3) and the single-execution-slot reset (M4)
are **not** in this release — they are v0.6 work. Deleting the container backend
*class* is also deferred to its own batch; this release only closes the product
surface around it.

### Added

- **Pre-publication drift detection (`REMOTE_DRIFT`, finally real).** Before the
  `publish` phase pushes anything, the target branch is fetched (scoped, bounded,
  read through `FETCH_HEAD`) and compared with the frozen baseline, then the task
  branch is checked with `ls-remote`. Four outcomes, no more: `base_advanced`,
  `base_rewritten`, `candidate_diverged`, `remote_unknown`. The first three share
  the existing `REMOTE_DRIFT` code and are distinguished by
  `error.details.drift_kind`; `remote_unknown` stays `REMOTE_UNKNOWN` because
  "could not check" is neither "clean" nor "changed". `git merge-base` exit 1 is
  the only signal for "rewritten" — any other failure is `remote_unknown`, so a
  git error can never be reported as a history rewrite. Nothing is force-pushed,
  overwritten or auto-rebased; `publication.json` records the classification
  (`status: blocked_by_drift`) and a `remote_drift` event is emitted.
  `local_artifact` never reaches this stage.
- **CLI discovery with pinning (§6).** `_discovery.py` resolves a CLI in a fixed
  order — explicit path → process `PATH` → known locations → bounded (3 s,
  process-group-killed) login shell — and `pin_launcher` rewrites `argv[0]` to that
  absolute path for `local-process`. The run record now stores `resolved_path`,
  `resolution_source`, `resolution_notes` and `cli_version` (filled when a probe
  already measured that exact binary in this process — never a second guess); `doctor` and `agent probe` report the
  same `resolution`/`resolution_notes`, and `agent probe` runs the same pinned
  `argv`. Adjacent runs of one task resolving to different binaries emit an
  `environment_drift` event (observation only, never a verdict). Version probing
  uses the launch environment allowlist, never the full parent environment.
- **`error.details`** — an optional object alongside `code`/`message`. Additive:
  a host that only reads `code` is unaffected. The same object is stored on the
  task as `block_details` (and cleared by the next successful start), so a
  supervisor-side failure is readable through `task status` without parsing
  `last_error`.
- **`docs/adapter-qualification.md`** — the four gates (resolve / answer /
  qualified run / capability), the capability matrix with `FACT_PENDING`, and an
  explicit list of what each gate does *not* prove.
- Tests: `tests/test_discovery.py` (order, multi-version notes, shell timeout,
  name validation, pinning), `tests/test_drift.py` (three kinds + unknown, built
  from real local bare repositories), `tests/test_v050_pipeline.py` (pinned
  launch, drift gate, drift evidence, branch hardening),
  `tests/test_v050_docker_exit.py`, plus a frozen `_EXIT_CODES` contract test and
  a `CLI_FACTS` ↔ registry consistency test.

### Changed

- **Docker backend left the product scope (stage one).** `schemas/` no longer
  offers `docker` as a `backend` value, `doctor` no longer checks `docker` /
  `docker_ready`, and `runtime_ready` no longer depends on an image. An old
  configuration is refused with `CAPABILITY_UNSUPPORTED` and
  `error.details.migration` — it is **never** rewritten to `local-process` in
  memory or on disk. A task frozen on the container backend cannot be resumed, and
  a historical container run is `PROCESS_UNKNOWN` (unobservable, not "exited") and
  is closed only by `task recover --confirm-stop`.
- **`agent probe` runs on this host** instead of requiring the container backend
  and a digest-pinned image; it now reports `BLOCKED` when the CLI cannot be
  resolved rather than `NOT_RUN` with a misleading note.
- **Task branches are validated with `check-ref-format`** before the worktree is
  created, and a leading `-` is rejected explicitly (a valid ref that `git push`
  would read as an option).
- **Docs tell the truth about isolation**: README (both languages), `SKILL.md`,
  `references/runtime-contract.md`, `references/cli-contract.md`,
  `docs/architecture.md`, `docs/threat-model.md` and `docs/capabilities.md` no
  longer describe a container sandbox that is not there, document the new
  `REMOTE_DRIFT`/`REMOTE_UNKNOWN` recovery, and mark the Windows platform layer as
  code without hardware evidence.
- Local runs also persist a `container` label (`local-<run>-<log>`); only a
  non-`local-` label now means container execution, so a local run is no longer
  mistaken for an unobservable container run.
- `capabilities --json` lists `container_execution` in `not_supported` instead of
  `docker_socket_mount`: "no socket mount" implied container support with one
  caveat, which is no longer true.

### Not in this release

- `EXTRA_COMMIT` detection is still unimplemented (only `REMOTE_DRIFT` was in
  scope).
- Native session resume, the single-execution-slot reset, and the removal of the
  container backend class are v0.6 / a separate batch.
- No `real_e2e` record is shipped. Drift checks are proven against local bare
  repositories, not a live GitHub remote; no adapter is `VERIFIED`.

## [0.4.1] - unreleased (Preview)

Closes the orchestration loop that the v0.4.0 review found unusable, and syncs the
documented positioning with the code (local `local-process` execution, not a
container sandbox).

### Fixed

- **`task answer` no longer dead-ends a task.** Asking a question used to write
  `phase="execute"`, a value no pipeline stage consumes, so answering cleared the
  block and the next `resume` silently did nothing. The question now stays in the
  stage that raised it, and legacy `phase="execute"` records are normalized when
  the pipeline opens them.
- **`task answer` is a real mutation.** It records an idempotency operation row,
  enforces `expected_version`, `authorization_ref` and the project owner, and
  clears `needs_input` (previously it wrote to a non-existent operation row).
- **`resume` before answering is refused** with `STATE_CONFLICT` instead of
  re-running the agent without the answer it asked for.
- **`report.md` is Markdown.** It was written through the JSON helper, producing a
  `.md` file whose content was a JSON object. New `_util.atomic_text` writes text
  atomically.
- **Budget attempts are rejected, not ignored.** A host patch containing `budget`
  or a budget counter now returns `POLICY_DENIED` before the unknown-field check.

### Added

- **Owner gating (§10.10)** — `dispatch`/`start`, `revise`, `reassign` and `answer`
  refuse a host that is not the recorded orchestration owner (`NOT_OWNER`); an
  unclaimed project stays open to any host.
- **`orchestration resume` derives its snapshot** — `dispatchable_now`,
  `stale_summaries` and unanswered Agent questions are computed from records
  instead of being hardcoded empty.
- **Context bundle carries Agent questions** — outstanding `needs_input` items on
  the task record are merged into `project context`, and truncation now halves the
  largest lists until the 32 KiB cap is met (previously only decisions/summary were
  dropped, so a task with many questions still overshot).
- **Windows wrappers** — `scripts/zoe.cmd` and `scripts/zoe.ps1` mirror the
  `zoe.sh` locate-and-forward contract; `build.py` requires them in the skill tree.
- **Windows child-process prerequisites** — the env allowlist now inherits
  `SYSTEMROOT`/`TEMP`/`PATHEXT`/`COMSPEC`/… by name (without them a Python child
  cannot start) and forces `PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8`; `.cmd`/`.bat`
  CLIs are launched through `cmd.exe /c` inside the same Job Object.
- **Tests** — `tests/test_v041_workflow.py` covers the answer→resume path,
  ownership, decision CAS, Markdown rendering, resume snapshot and context bounds.

### Changed

- Docs now describe **controlled execution in a trusted personal environment**
  instead of a sandbox: README, `references/runtime-contract.md`,
  `docs/threat-model.md` and `docs/capabilities.md` state what worktrees,
  `allowed_paths` and the env allowlist do **not** guarantee.
- `schemas/project-profile.v2.json` accepts `local-process` as the documented
  default backend (it previously only allowed `docker`/`local`).
- **Admission is a policy, not a wall.** `require_real_e2e` (default `false`)
  decides whether a missing real-e2e record blocks dispatch. Default: an unverified
  backend starts, and `unverified` / `trust_level` are written into the `start`
  response, the run record, the first event (`unverified_dispatch`) and
  `task report`. `require_real_e2e: true` restores `ADAPTER_UNVERIFIED`.
  This removes the deadlock where `qualification run` could only ever emit
  `INCOMPLETE`, so no backend could become `VERIFIED` and nothing could start.
- **`doctor` splits `ready` from `trusted`.** `ready` is environment-only
  (git, process identity, platform lock/tree, optional docker). `trusted` reports
  whether the selected backend has a real-e2e record; `checks.adapter_approved`
  keeps that meaning. A strict project (`require_real_e2e: true`) still reports
  `ready: false` without evidence.
- **Cross-task concurrency (`max_active_tasks`, 1..8, default 1).** The
  single-writer invariant stays per task (own writer lock and worktree); what the
  limit governs is how many runs may be active at once, and `SLOT_BUSY` now names
  the tasks holding the slots.
- **Supervisor spawn is pre-checked.** `start` absolutizes `PYTHONPATH` for the
  supervisor (relative entries broke under `cwd=<home>` in source mode), verifies
  `import zoe_runtime` with the same interpreter/cwd/env, and fails with an actionable
  `ENVIRONMENT_ERROR` instead of letting the task decay into `PROCESS_UNKNOWN`.
- Tests: `tests/test_v041_admission.py` covers the policy switch, `ready`/`trusted`
  split, the concurrency range and the supervisor pre-check; the gate tests in
  `tests/test_v030.py` now opt into strict mode explicitly.
- **`qualification run` performs all six checks** (03 §1). Checks 3-6 are no longer
  hardcoded `INCOMPLETE`: the command registers an ephemeral `qual-*` project
  (`require_real_e2e: false` — the single exemption), runs one full implement pass
  through the normal controller path, then a second pass that is stopped and
  resumed. `auth` / `noninteractive-edit` / `parse` are derived from the run's
  `agent_result`, the `base_sha..worktree` diff and `result_schema`;
  `stop-resume` only passes when the tree was observed live, stop was confirmed
  (`remaining == 0`) and the resume reached a terminal stage. New flags
  `--timeout` and `--stop-after`; the disposable-repo constraints of 03 §5 are
  enforced (`POLICY_DENIED` on a non-local remote). A record is written in both
  cases: all-PASS becomes `source: real_e2e`, anything else `source: local_admin`.
- Tests: `tests/test_v041_qualification.py` proves the six-check path with a stub
  CLI on `PATH` (pass, auth-failure, remote-repo refusal, missing-fixture), and
  that qualification does not unlock its own source project.
- **Diagnostics no longer mask the real failure.**
  `reconcile` used to overwrite an existing `block_code` with `RUN_INTERRUPTED`
  when the supervisor died; it now keeps the pipeline's code. `_run_uncertain`
  used to report `PROCESS_UNKNOWN` for a run that failed *before* launching the
  agent (e.g. `RETRY_EXHAUSTED`), because the run's tree token still pointed at
  the supervisor itself; a run with no `launch_stage` is now treated as
  deterministically finished.
- **Resume needs attempt budget.** Pausing consumes one implementation attempt,
  so a project with `max_attempts: 1` cannot be resumed (`RETRY_EXHAUSTED`,
  previously surfaced as `PROCESS_UNKNOWN`). The qualification project therefore
  registers with three attempts; `--stop-after` fixtures must outlive the stop
  window or the check reports `INCOMPLETE` instead of a silent pass.
- Slot accounting counts tasks in `RUNNING`/`PAUSING` rather than stale
  `run.status`, so a crashed or failed resume can no longer occupy a slot forever.
- **Handoff carries the remaining work** (06 §8). `handoff.json` now includes
  `block_code`, `failed_checks` (from `validation.json`) and `remaining_work`
  (derived from phase/state only — never from agent self-report), so a handover
  does not require replaying the run to learn where it stopped. `HANDOFF_STALE`
  on `reassign` already compared workspace fingerprints; it now has the fields it
  was specified with.
- **`watch --follow`** streams events as one JSON object per line (bounded by
  `--timeout`), so a host can `spawn` + `readline` instead of polling. The
  non-follow response is unchanged.
- **`runtime_binding` (default `strict`).** Evidence bound to a precise
  `runtime_digest` stays strict by default: a changed binary/platform drops the
  backend to `IMPLEMENTED` with a "re-run qualification" reason. Projects that
  prefer a warning over a refusal set `runtime_binding: "warn"`, which keeps
  `VERIFIED` and records why in `capability_reason`. Defaulting this to `warn`
  would have made `VERIFIED` stop meaning "this exact binary, on this host".
- Tests: `tests/test_v041_extras.py` (handoff fields, runtime binding, watch
  streaming), plus `tests/qualification/basic/` as a ready-to-use fixture.

### Known gaps (unchanged in this release)

- No `real_e2e` record is **shipped** for any `(platform, adapter)` cell — that
  still requires running `qualification run` on the target host with a real CLI.
  What changed is that the command can now actually produce one: it drives two real
  runs (implement + stop/resume) instead of hardcoding four `INCOMPLETE` checks.
  Anything short of all six PASS is written as `source: local_admin` and cannot
  lift a backend above `IMPLEMENTED`, so a report must keep saying "unverified"
  until a PASS record exists.
- Autonomy budgets are recorded but not enforced at `start`.
- `REMOTE_DRIFT` detection and shared-resource serialization were not implemented
  yet at 0.4.1; drift is implemented in 0.5.0, `EXTRA_COMMIT` and shared-resource
  serialization still are not.

## [0.3.0] - unreleased (Preview)

### Preview scope

Extensible multi-agent adapters on top of the v0.2.0 controller. The single
writer, hardened container, independent verification and controller-owned
publication are unchanged. **No adapter is `VERIFIED` in this build**: real
end-to-end runs remain `NOT_RUN` and `start` blocks unverified adapters.

#### Added

- `_agents/` package with the `AgentAdapter` contract (`LaunchSpec`,
  `RunContext`, `AgentResult`) and a **static** registry. Built-in adapters:
  `codebuddy`, `opencode`, `claude-code`; extended adapter: `codex`.
- Three-state capability per `(adapter_id, backend)`: `IMPLEMENTED` /
  `UNVERIFIED` / `VERIFIED(version, backend)`. Unverified adapters are blocked
  at `start` with `ADAPTER_UNVERIFIED`; version drift yields
  `ADAPTER_VERSION_MISMATCH`; a backend that the adapter does not support (or
  that the evidence does not cover) yields `ADAPTER_BACKEND_MISMATCH`.
- Agent profiles in the project configuration (`agent_profiles`,
  `default_agent_profile`, per-profile `backend`, typed options
  `model` / `max_turns` / `permission_profile` / `approved_tools`), replacing
  arbitrary `agent_flags`; legacy arrays are migrated through a whitelist or
  refused with advice.
- Per-adapter output protocols: final JSON (`codebuddy`, `claude-code`), JSON
  event streams (`opencode`), and a provisional dual parser for `codex`. Errors,
  auth failures, permission blocks, rate limits, truncation and missing
  terminators can never become `completed`.
- Bounded agent output (per-profile `max_output_bytes`) with `capture.json`
  metadata; a stream over budget is `OUTPUT_LIMIT_EXCEEDED`, never success.
- Prompt delivery over a read-only mount consumed as stdin, removing prompt text
  from argv; 64 KiB prompt ceiling (`PROMPT_TOO_LARGE`).
- `zoe capabilities --json`, `zoe agent list --json`, `zoe agent probe --json`
  (bounded, offline, never a verification), `zoe task events`,
  `zoe task logs --tail-bytes`, `zoe task reassign`, `zoe task rebind`,
  `zoe group create|status`, `zoe state migrate --to 2`.
- Cross-adapter handover with assignment records, preserved retry budget,
  handoff-fingerprint check (`HANDOFF_STALE`) and evidence invalidation.
- Delivery evidence (spec hash + candidate + manifest + check policy + verifier
  image) persisted per run and re-checked at publish and acceptance.
- SQLite schema v2 (`groups` table, record/event payload fields) with an
  explicit, locked, backed-up migration; v1 databases are refused until
  migrated. Unknown adapter ids are reported as warnings, not rewritten.
- Structured spec contract (`schema_version: 2`) with unique acceptance ids,
  verification references, blocking open questions and path tightening.
- `schemas/` (capabilities, project profile, compatibility evidence, agent
  result) and `docs/compatibility/`, plus a contract test that keeps docs,
  schemas, CLI and the registry in agreement.
- `tests/fixtures/agents/` — versioned adapter fixtures covering normal, auth
  failure, rate limit, permission block and truncation. They are labelled
  `synthetic-from-documented-protocol` until a real capture replaces them.
- `tests/integration/` — the real end-to-end harness for the §19.2 matrix. It is
  skipped unless `ZOE_REAL_E2E=1`; **`real-e2e` stays `NOT_RUN`**.
- `docs/rollback.md` — how to go back to v0.2.0 without stranding state.
- Permission-profile **binding audit**: `permission_profile` is validated as an
  enum *and* reported as `enforced: true|false` in the run record, `capture.json`,
  `zoe agent list` and `doctor`. `require_permission_binding: true` fails closed
  with `PERMISSION_PROFILE_UNBOUND` instead of pretending the profile has effect.
- Per-`(task, run, adapter, backend)` session directory, containment-checked by
  the backend before it becomes a mount, for diagnostics only.

#### Fixed

- A `facts_status=FACT_PENDING` adapter can no longer be promoted to `VERIFIED`
  by an operator `compatibility_evidence` record: the registry caps it at
  `IMPLEMENTED`, so `zoe agent list` and `doctor` never advertise a backend that
  `build_launch` refuses (§9.1, §10.4).
- A v0.2 task whose frozen configuration predates `agent_profiles` resumes
  instead of failing with "Project configuration is not normalized".
- The capability gate is applied in one place: `run_pipeline` no longer exempts
  legacy profiles.
- `task reassign` / `rebind` / `events` / `logs` use the granular exit codes while
  `task create/start/stop/revise/accept` keep the published `4`.

#### Changed

- `codex` is no longer absent: it is the first **extended** adapter. Its facts
  are `FACT_PENDING`, so no command form is shipped and `build_launch` refuses
  with `ADAPTER_VERSION_MISMATCH` until the M3x verification is recorded. It also
  names **no** unverified fact: the backend is the placeholder `backend-pending`,
  the credential namespace is `CODEX_`, and the parser matches structural
  keywords instead of hard-coding a vendor event name.
- Unified v2 response envelope: every mutation carries `api_version`,
  `operation_id` and `evidence_refs`.
- Docker backend no longer parses vendor output and no longer decides credential
  prefixes; adapters own protocol parsing, profiles own credential scope.
- Console exit codes for the new v0.3 commands are granular (2/3/4/5/6) and every
  response carries `error.code`; legacy commands keep exit code `4`.

## [0.2.0] - unreleased

### Preview release

First installable, open-source preview. Focus is packaging, auditability, and
contributability — not new features.

#### Added

- `zoe doctor` without `--project`: host diagnosis that does not create a state
  database. `--project P` retains the previous project check; `--require-ready`
  makes readiness machine-checkable via exit code.
- `zoe demo`: an isolated, simulated pipeline using test doubles only. Never
  invokes a real agent or writes a remote.
- Module entry point `python -m zoe_runtime`; console and background supervisor
  now share the same controlled `main(argv) -> int` entry, fixing the
  previously file-path-based background launch.
- `LICENSE` (Apache-2.0), `NOTICE`, `CONTRIBUTING.md` (DCO), `SECURITY.md`.
- `docs/architecture.md`, `docs/threat-model.md`, `docs/capabilities.md`.
- Packaged as `zoe-runtime` with `pyproject.toml`; runtime has no third-party
  dependencies.

#### Changed

- Source moved to a `src/zoe_runtime/` layout.
- `core.VERSION` removed; version is sourced from `zoe_runtime.__version__`.
- Runtime tests migrated from `zoe_runtime` to `zoe_runtime` (import/mock/entry
  paths only; scenarios and assertions preserved).
- `core.py` split into focused modules (`_util`, `_proc`, `_config`, `_store`,
  `_backend`, `_publisher`, `_pipeline`); `core.py` remains a re-export
  aggregator for backward compatibility.
- Public entry points annotated with types and docstrings (`create`, `start`,
  `stop`, `revise`, `accept`, `validate_config`, `doctor`, `authorize`).
- Disk-space guard: `start` refuses new dispatch below a free-space floor, and
  `doctor` reports free space (`check_disk_space`, read-only).
- Host integration (`SKILL.md`, `references/`, `agents/`, `assets/`,
  `scripts/zoe.sh`) moved under `integrations/workbuddy-skill/` as a
  self-contained skill package.

#### Compatibility

- Upgrade fixture (`tests/test_upgrade.py`) proves 0.1.0 `state.db` data stays
  readable and recoverable across the 0.2.0 change. It writes rows directly
  (bypassing 0.2.0 code paths) and pins the 0.1.0 field set, then covers:
  `READY` (resumable), `PAUSED` (work and retry budget retained), `BLOCKED`
  (reason retained), interrupted `publish` (reconciled as `RUN_INTERRUPTED`
  without restarting implementation), `REVIEW_READY` (verification evidence
  still linked, no invented acceptance), and `DONE`/`CANCELLED` (never
  implicitly restarted). Legacy `request_id` values are replayed instead of
  re-executed, and changed payloads raise `REQUEST_CONFLICT`.

#### Upgrade notes

1. Stop any active supervisor before upgrading.
2. Back up your `~/.local/share/zoe` directory (database and associated
   worktrees/specs).
3. Install the new version. `state.db` remains compatible (`user_version=1`).

Live cross-version upgrade is **not** supported.

## [0.1.0] - reference implementation

- Local runtime: SQLite store, single execution slot, Docker backend, workspace
  resume, independent verification, PR reconciliation, acceptance gate.
- 40 runtime test scenarios.
