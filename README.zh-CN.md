# Zoe Runtime

**本地 AI 编码任务的受控执行控制器 —— 交给 Agent 一份冻结的 Spec，把关键决策留在自己手里。**

`v1.5.0` ｜ Python ≥ 3.11 ｜ 仅标准库 ｜ macOS · Linux · Windows（`local-process`）

Zoe Runtime 把一份 Spec 变成任务，驱动它走完一条受控流水线：

```
create → route（冻结决策）→ implement（adapter）→ verify → publish → CI → accept
```

需要「顺序 + 集成 + 一次验收」的交付，用**任务组**冻结一份计划，成员串行执行：

```
group plan → group create（冻结）→ group start（推进器）→ integrate → group checks
           → 设计一致性审查（宿主执行）→ group accept → group report
```

执行器由**适配注册表**按 `(adapter, backend)` 选择。内置：`codebuddy`（`codebuddy-cli`）、
`opencode`（`opencode-cli`）、`claude-code`（`anthropic-api`）；扩展：`codex`
（`backend-pending`）。适配器只描述**怎么启动**与**怎么解析输出**——进程生命周期、
挂载、并发与发布始终由控制器独占。

四条不变量是被强制执行的，不是写在文档里的意愿：单一写者、独立验证、控制器独占发布、
诚实验收。Agent 说「我完成了」从不被当作证据。1.5 增加了第五条：**配过不等于授权**——
`agent_profiles` 里存在的 profile，不是用户允许派发的 profile。

> **1.5.0：接口已冻结，适配器证据仍未达成。** CLI 表面、磁盘状态（schema v4）与技能契约
> （`api_version` 2 / `protocol_revision` 1）从此保持稳定。尚未落定的是**证据**：本构建中
> 没有任何适配器达到 `VERIFIED`，真实本机 CLI 的端到端执行全部为 `NOT_RUN`。
> 能力仍是**三态**的 —— `IMPLEMENTED` / `UNVERIFIED` / `VERIFIED(version, backend)`。
> 能力态是**标签，不是入场券**：默认配置下未验证的 profile 仍可 `start`，但响应、run 记录、
> 事件与 `task report` 都必须带 `unverified` / `trust_level`；只有项目声明
> `require_real_e2e: true` 时才在 `start` 阶段返回 `ADAPTER_UNVERIFIED` 拒绝。
> 宿主集成同样未验证：`capabilities` 对 WorkBuddy 与 OpenClaw 都报 `real_e2e: NOT_RUN`。
> 把某个 backend 升到 `VERIFIED` 靠 `docs/compatibility/` 下的精确版本记录，不靠发布号。

```bash
zoe capabilities --json                                          # 注册表能力（无需项目）
zoe agent list --project example --json                          # 各 profile 的能力态与证据来源
zoe agent probe --project example --profile cb-approved --json   # 有界探测，不改变能力态
```

`codex` 以 `facts_status=FACT_PENDING` 登记：适配器契约已实现，但命令形态未冻结，
因此永远不会被描述为「已支持」。任何 backend 升到 `VERIFIED` 的唯一方式，
是在 `docs/compatibility/` 留一条精确版本的记录。

## 为什么用 Zoe Runtime

### 直接调用 Agent 不够吗

| 维度 | 直接调用 Agent | 通过 Zoe Runtime 编排 |
|---|---|---|
| **状态管理** | 无持久化，进程结束即丢失 | 完整任务生命周期（8 种状态、6 个阶段）；组还多一份冻结计划、集成账本与一次验收 |
| **受控范围** | Agent 可读写任意文件、自行提交 | 每任务 worktree + 每任务唯一写者 + 凭证白名单 + `allowed_paths` 事后审计（**问责，不是沙箱**） |
| **授权边界** | 谁写了配置谁说了算 | 显式 `routing.allowed_profiles`、一次 `project attest` 绑定配置摘要、首次派发前的 `ALLOWLIST_UNCONFIRMED` |
| **崩溃恢复** | 挂掉后靠人工排查 | 对账 supervisor + 进程树 + 工作区，从中断阶段续跑 |
| **发布控制** | Agent 自行 commit/push | 控制器独占 Git 写入，PR 带所有权标记 |
| **验收门禁** | 无机制 | SHA 匹配 + 工作区指纹未变 + 人工决策；组成员不能自验收 |

### 你实际得到什么

1. **全流程受控** —— Spec → 任务 → Agent 执行 → 独立验证 → PR → CI → 人工验收。
2. **不信任 Agent 自述** —— 控制器在独立环境重跑检查，并比对执行前后的工作区指纹。
3. **崩溃可恢复** —— 进程身份 + 进程树对账 + 幸存工作区保留。
4. **发布权在你手里** —— 控制器独占文档化的发布路径（Agent 仍持有你的账户凭据，见威胁模型）。
5. **执行组合路由** —— 任务声明需求，运行时冻结一次决策（`route-<rev>.json`），执行后校验的是**实际报告的模型**，不是请求里写的。
6. **任务组交付** —— 冻结计划、串行成员、单一集成分支、绑定 `integration_sha` 的组级检查、设计一致性审查、有界修复、一次 `group accept`。

### 1.x 在「单个任务」之外加了什么

| 领域 | 命令面 | 被强制的是什么 |
|---|---|---|
| 授权边界（1.5） | `project attest`、`routing.allowed_profiles`、`project diff` | 白名单必须显式；任何执行字段变化都会让确认失效；改配置前可只读预览在途任务影响面 |
| 路由（1.2） | `route plan`、`route stats` | 纯函数 `decide()`、带 `content_sha256` 的冻结记录、执行后校验模型身份；表现只记录，从不参与选择 |
| 任务组（1.3） | `group plan/create/start/dispatch/status/pause/resume/cancel/accept/report` | 每组一个推进器（租约）、成员串行、预算只拦派发、成员停在 `REVIEW_READY` |
| 设计审查 + 修复（1.4） | `group review`、`group repair` | 宿主的结论绑定 `(integration_sha, design_digest)`；一次修复只针对一条 FAIL finding，受 `budget.max_group_repairs` 约束 |
| 观测（1.5） | `watch --follow`、`task events`、`task logs --follow`、`progress`、checks 预检 | 有界、可续的游标；「没有输出」是心跳，不是进展证明 |
| 只读准备（1.4/1.6） | `task prepare`、`project diff` | 「这次 create/start 会不会过、会冻结什么」，不写状态库 |

## 安装

本仓库**只有运行器**。宿主 skill 在另一个仓库
[`zoe-workflows`](https://github.com/zoe-runtime/zoe-workflows)，它带
`runtime-requirements.json`——两个仓库之间的唯一锚点。

### 一、作为 CLI

```bash
pipx install zoe-runtime        # 隔离环境，把 `zoe` 暴露到 PATH
pipx ensurepath
zoe --version
zoe doctor                   # 宿主机诊断，不创建状态目录

pipx install "zoe-runtime==1.5.0"   # 生产/复现
```

五种安装路径（pipx、venv + pip、开发者 checkout、离线 wheel、升级与卸载）见
[docs/installation.md](docs/installation.md)；不打断在途任务的升级顺序见
[docs/upgrading.md](docs/upgrading.md)。

### 二、从本仓库直接运行（不安装）

```bash
PYTHONPATH=. python3 -m src --version
PYTHONPATH=. python3 -m src doctor
```

`python -m src` 会把当前目录放在 `sys.path` 首位，请在仓库根目录执行。由于包名就是
`src`，如果所在项目自身也有一个可导入的 `src/` 包，会遮蔽运行器——那种场景请用已安装的
`zoe` 命令（控制台脚本从 `site-packages` 导入 `src.cli`，不看 cwd）。

### 三、skill 单独安装

```bash
git clone https://github.com/zoe-runtime/zoe-workflows.git ~/.workbuddy/skills/zoe-workflows
bash ~/.workbuddy/skills/zoe-workflows/scripts/zoe.sh --which
```

包装器按 `ZOE_BIN` → `ZOE_SRC` → PATH 上的 `zoe` 定位，然后先跑
`capabilities --check-requirements` 再转发。`RUNTIME_NOT_FOUND`（exit 126）与
`RUNTIME_INCOMPATIBLE`（exit 5）都表示**请求没有被转发**；`--which` 与 `--version`
不受预检限制，因为它们是排障通道。GUI 宿主有两个坑：`doctor.resolution.path` 是
**编码 Agent** 的路径，不是 Zoe 的；终端里 `command -v zoe` 有结果不代表
WorkBuddy/OpenClaw 能看到它——请配置显式 `ZOE_BIN` 并重启宿主。

兼容策略见 [docs/compatibility.md](docs/compatibility.md)。

## 快速开始

### 仿真模式 —— 无需 Agent、不落真实状态

```bash
zoe demo    # 在临时目录用测试替身跑完整流水线
```

`zoe demo` 不写远端、不调用真实 Agent，是了解控制器做了什么的最快方式。

### 真实执行 —— 需要本机 CLI、其认证与项目配置

```bash
# 1. 写项目配置（schema v2；参考 skill 仓库的 references/project-profile.example.json）
zoe --home /private/zoe-state project register --config project.json
zoe --home /private/zoe-state doctor --project my-project --json

# 2. 只读预检：「create 会不会过、会冻结什么」
zoe --home /private/zoe-state task prepare --project my-project --spec spec.json

# 3. 把 Spec 冻结成任务
zoe --home /private/zoe-state task create --request-file create.json

# 4. 项目声明了 routing 时，先确认一次候选集合
zoe --home /private/zoe-state project attest --project my-project --dry-run
zoe --home /private/zoe-state project attest --project my-project

# 5. 启动（未验证 profile 仍可跑，记录带 unverified；require_real_e2e 才拒绝）
zoe --home /private/zoe-state task start TASK_ID --request-file start.json

# 6. 观察、对账、恢复
zoe --home /private/zoe-state task status TASK_ID --refresh --json
zoe --home /private/zoe-state task events TASK_ID --json
zoe --home /private/zoe-state task logs TASK_ID --follow --timeout 60
zoe --home /private/zoe-state watch --project my-project --follow --jsonl --timeout 60

# 7. 人工验收
zoe --home /private/zoe-state task accept TASK_ID --request-file accept.json
```

`project.json` 使用 schema v2 字段（`repo`、`image`、`agent_profiles`、`checks`、
`allowed_paths`、`routing` 等）。请从 skill 仓库的
`references/project-profile.example.json` 复制，不要手写旧的 v1 结构。skill 提供三档示例：
默认（一个模型 + 显式白名单）、多模型、全字段高级。

`zoe project diff --config candidate.json` 只读预览改配置对在途任务的影响，复用
`task rebind` 的同一套 spec 判定。

### 任务组交付 —— 一份计划、一次验收

```bash
zoe group plan   --project my-project --request-file plan.json   # 校验 + 路由预演，不写任何东西
zoe group create --request-file plan.json                        # 冻结计划（仍不派发）
zoe group start  GROUP_ID --request-file start.json              # 短命推进器，不是守护服务
zoe group status GROUP_ID --refresh --json
zoe group review GROUP_ID --request-file review.json             # plan.review.required 时必需
zoe group accept GROUP_ID --request-file accept.json
zoe group report GROUP_ID --format markdown --write
```

`group start` 返回 `accepted: true` / `completed: false`：**受理不是推进**。推进器退出时，
`stop.exit_reason` 说明原因（`completed`、`review_ready`、`awaiting_design_review`、
`budget_exhausted`、`slots`、`blocked`、`no_progress`、`lease_stale`、`safety_cap`），
`stop.resumable` 说明「再 start 一次」是否有意义。成员停在 `REVIEW_READY`，
对成员调 `task accept` 会被拒（`kind=group_member`）。

## 仓库结构

```
zoe-runtime/
├── src/                  # 运行器包；导入名就是 `src`（`src/cli.py`）
├── schemas/              # CLI 输出的 JSON Schema（capabilities v2、组计划/审查等）
├── tests/                # 单测 + 契约漂移闸门 + 发行产物验证
│   └── contract/         # skill ↔ 运行器命令契约（需要 ZOE_SKILL_DIR）
├── docs/                 # 安装/升级/发布文档、架构、威胁模型、设计记录
└── .github/workflows/    # ci.yml、contract.yml、release.yml
```

包目录就是 `src/`，所以导入名是 `src`（`import src.cli`、`python -m src`、
入口点 `src.cli:main`）。分发包名（`zoe-runtime`）、`zoe` 命令、任务 ID 与状态目录
都不变。状态在 `ZOE_HOME`（缺省 `~/.local/share/zoe`）：SQLite schema v4，加上
`tasks/<id>/`、`groups/<gid>/`、`projects/` 下的产物。

宿主 skill 是**另一个**仓库（`zoe-workflows`）：`SKILL.md`、`references/`、`scripts/`
与 `runtime-requirements.json` 都在那边，本仓库不留副本。跨仓测试通过
`ZOE_SKILL_DIR` 定位它。

## 工作原理

### 运行时形态

```
                 ┌──────────────────────────────┐
   用户 / 宿主   │  zoe (控制台入口)             │
   ────────────▶ │  main(argv) -> int            │
                 └──────────────┬───────────────┘
                                │ task create：路由决策 + 冻结
                                │ task start：fork 监管进程
                                ▼
                 ┌──────────────────────────────┐
                 │  zoe internal (监管进程)       │
                 │  maintenance SH + writer EX    │
                 └──────────────┬───────────────┘
                                │ 平台层 spawn（独立进程树）
                                ▼
                 ┌──────────────────────────────┐
                 │  Agent CLI（受管进程树）        │
                 │  POSIX pgid / Windows Job      │
                 └──────────────┬───────────────┘
                                │ 退出码 + 有界日志 + capture.json
                                ▼
                 ┌──────────────────────────────┐
                 │  publisher (宿主机, gh)        │
                 │  commit / push / PR            │
                 └──────────────────────────────┘
```

任务组多一层：`group start` 派生一个短命推进器（`zoe internal-group`），它持有组级租约、
对账成员、把已校验成员集成到 `refs/heads/zoe/<gid>`、在该 SHA 上跑组级检查，
并且只在预算与全局槽位都允许时派发下一个成员。

### 六个阶段

| 阶段 | 做什么 | 谁执行 |
|---|---|---|
| **prepare** | 创建隔离的 git worktree，记录工作区基线 | 控制器 |
| **implement** | 在独立进程树中启动 Agent CLI 执行编码 | Agent（`local-process`） |
| **verify** | 重跑检查、比对工作区指纹 | 控制器 |
| **publish** | 推送代码、创建或更新 PR | 控制器（`gh`） |
| **ci** | 查询 CI，等待必需检查通过 | 控制器 |
| **acceptance** | 人工决策 | 你 |

### 任务状态机

```
READY → RUNNING → [PAUSING → PAUSED] → BLOCKED → REVIEW_READY → DONE
           ↓
       CANCELLED
```

| 状态 | 含义 |
|---|---|
| `READY` | Spec 已冻结，尚未运行 |
| `RUNNING` | 有活跃的受监管操作（`phase` 指示当前阶段） |
| `PAUSING` | 已请求停止，尚未确认 |
| `PAUSED` | 已确认停止，工作区保留 |
| `BLOCKED` | 环境/需求/进程/远端状态未知，或 Agent 正在提问 |
| `REVIEW_READY` | 证据满足审核策略，等待人工 |
| `DONE` | 已交付并验收 |
| `CANCELLED` | 已确认无写者，不自动恢复 |

没有 `DRAFT`：输入不完整会被结构化错误拒绝，不会存成半个任务。组复用同一套词汇
（`READY`、`RUNNING`、`BLOCKED`、`PAUSED`、`REVIEW_READY`、`DONE`、`CANCELLED`），
成员状态是派生出来的（`WAITING`、`READY`、`EXECUTING`、`VALIDATED`、`INTEGRATED`、
`INVALIDATED`、`PAUSED`、`BLOCKED`、`CANCELLED`）。

更多见 [docs/architecture.md](docs/architecture.md)。

## 安全保证

### 1. 单一写者（Single Writer）

`maintenance.lock`（共享）+ `tasks/<id>/.writer.lock`（排他），由 supervisor 在整个
Run 期间持有，再叠加进程身份（PID + 启动令牌）：

- 已有写者存活时，绝不启动第二个，且由操作系统而非数据库约定保证；
- supervisor **或它的进程树**无法判定时进入 `BLOCKED`，而不是盲目重启；
- 检测僵尸进程与 PID 重用（Linux `/proc`、macOS `proc_pidinfo`——不依赖外部 `ps`，
  仅在都不可用时退回 `ps -o lstart`；Windows `GetProcessTimes`）。

组在构造上就是串行的：派发取 `min(1 - group_busy, global_free)`，仅有空闲全局槽位
不会让第二个成员启动。

### 2. 独立验证（Independent Verification）

- 控制器在**独立环境**重跑检查脚本；
- 比对执行前后的工作区**指纹**（文件列表 + 权限 + 内容哈希）；
- Agent 的「我完成了」**从不被当作证据**；
- 验证失败回退到 `implement` 阶段；
- 门禁拒绝一次运行时，先把差异归档到 `runs/<id>/worktree.patch` 再恢复工作树，
  且仅当 `git apply --check --reverse` 证明可还原；`start` 前就已脏的工作区永不被清理。

### 3. 控制器独占发布（Controller-owned Publication）

- Agent **不能** commit / push / 创建 PR；
- 所有 Git 写操作由控制器完成；
- PR 带所有权标记：`<!-- zoe-task: TASK_ID -->`；
- 超时先对账，不盲目重试、不建重复 PR；
- 组集成是三段账本（`intent` → `prepared` → `done`），`prepared` 必须在 ref 移动**之前**落盘；
  恢复时文件优先于数据库行。

### 4. 诚实验收（Honest Acceptance）

以下条件必须同时满足：真实的用户决策（`accepted`/`rejected`）、真实的 actor、
非空证据列表、`subject_sha == validated_sha`、工作区指纹未变。组成员不能自验收，
唯一路径是 `group accept`，且校验最终 `integration_sha`。

### 5. 配过不等于授权（1.5）

- 只要声明了 `routing` 段，`routing.allowed_profiles` 必须显式给出——往
  `agent_profiles` 里加一个 profile 只是「配过」，不是「授权」；
- `zoe project attest` 把 `policy_hash` + 已确认候选集合 + 运行时版本写入
  `$ZOE_HOME/projects/<id>.attest.json`；任何执行字段变化都会让它失效，下一次派发返回
  `ALLOWLIST_UNCONFIRMED`（`details.reason` 为 `missing` /
  `policy_changed_since_attestation` / `candidate_not_confirmed` / `record_invalid`）；
- `task rebind` 不会替你确认，重新注册相同配置也不会（摘要没变）；
- `zoe project diff` 复用 `rebind` 的判定，只读展示新策略会挡住哪些在途任务。

### 默认后端能保证什么、不能保证什么

`local-process` 以本机账户运行 Agent，放在独立进程树中：

| 机制 | 能保证 | **不能**保证 |
|---|---|---|
| git worktree + 分支 | 文件组织、任务隔离、可审计的 diff 范围 | 阻止 Agent 读写工作区之外的目录 |
| `allowed_paths` + 事后 diff | **问责**（越界即拒绝交付） | **预防**（写已经发生） |
| 环境变量白名单 | 只注入声明的凭据名 | Agent 读不到 `~/.ssh`、git helper 等 |
| 控制器独占发布 | 发布记录可追溯 | Agent 用自己的账户凭据自行 push |

进程树管理是真实保证：POSIX `start_new_session` + `killpg`，Windows 具名 Job Object
（`KILL_ON_JOB_CLOSE`），停一次带整棵子树（含 node/python 子进程），而不是只杀父进程。

本版**没有可依赖的容器边界**：`docker` 后端已在 v0.5.0 退出产品支持范围。
配置里写 `backend: docker` 会被显式拒绝（`CAPABILITY_UNSUPPORTED` + 迁移提示），
不会被降级成本机执行继续跑；历史容器任务只读保留，执行状态按 `PROCESS_UNKNOWN` 报告，
因为它们已无法被观测。

### 凭证

Worker 进程只拿到 `HOME`、`PATH`、`TERM`、启动必需的系统变量（如 Windows 的
`SYSTEMROOT`、`TEMP`、`PATHEXT`）、`PYTHONUTF8=1`，以及所选 adapter profile 明确声明的凭据，
此外什么都拿不到。每个适配器解析自己的引用（`model-codebuddy` → `CODEBUDDY_API_KEY`、
`model-anthropic` → `ANTHROPIC_API_KEY`、`model-openai` → `OPENAI_API_KEY`、
`model-codex` → `CODEX_API_KEY`），可接受的前缀按适配器隔离
（`CODEBUDDY_`/`ANTHROPIC_`、`OPENCODE_`/`OPENAI_`/`ANTHROPIC_`、
`ANTHROPIC_`/`CLAUDE_`、`CODEX_`）；一个适配器无法借用另一个厂商的凭据。

威胁模型见 [docs/threat-model.md](docs/threat-model.md)。

## 已知限制

- **没有任何适配器达到 `VERIFIED`。** 真实本机 CLI 端到端全部为 `NOT_RUN`，因此 run 一律标 `unverified`；只有项目声明 `require_real_e2e: true` 时才在 `start` 阶段返回 `ADAPTER_UNVERIFIED` 拒绝——默认行为是「能跑，但绝不称已验证」。`qualification run` 目前只真正验证「CLI 可发现」与「版本匹配」；认证、非交互改文件、结果解析、停止续跑仍为 `INCOMPLETE`，需真实执行一轮才能升级。
- **宿主集成未验证。** WorkBuddy 与 OpenClaw 均报 `real_e2e: NOT_RUN`；目前只验证过本机 CLI 路径。
- **`codex` 为 `FACT_PENDING`。** 契约已实现、命令形态未冻结，不可选择，也不得在文档或汇报中声称已支持。
- **Windows 平台层未在真机验证。** Job Object / `LockFileEx` / `GetProcessTimes` 已实现且有构造性测试，但没有任何真实 Windows 端到端记录 —— 按 `IMPLEMENTED` 对待，不宣称「支持」。
- **没有容器隔离。** 只有本机执行；Worker 没有 `--cap-drop`/`--read-only`/`--network none` 这类保证。
- **写者粒度是任务，不是项目。** 不同任务可在 `max_active_tasks`（1..8，默认 1）上限内并行；活跃数按 `ZOE_HOME` 下的所有项目**全局**统计，限额取自派发项目自身的配置。同一项目内两个任务的 `allowed_paths` 若有重叠，并行并不安全。
- **任务组刻意做窄。** 仅支持 `local_artifact` 交付；没有组级 PR、没有成员并行、没有自动重试、没有无界动态任务图，也不自动合入目标分支——集成到 `zoe/<gid>`，合并仍由人决定。
- **组预算只拦派发。** `max_starts` 与 `max_wall_seconds` 只阻止新启动；对账、集成、检查与验收在预算耗尽后照常可用。任务级自主推进预算仍只记录，未在 `start` 生效。
- **设计审查由宿主执行。** `group review` 只校验提交（`subject_sha`、`design_digest`、必需 AC 是否覆盖、证据是否可解析），从不自己跑评审；`NOT_VERIFIED` 可以进入 `REVIEW_READY`，但会逐条列入报告，绝不算通过。
- **观测是有界查询，不是守护服务。** `watch --follow` 与 `task logs --follow` 到 `--timeout` 就退出，续接靠显式 `--after` / `--cursor`。`progress.process_state=alive` 与 `observations_stale=true` 是两件事，「没有事件」是心跳而不是进展证明。
- **没有日志轮转策略。** 轮转与截断能被**识别**（游标跨两者有效），但不会自动清理文件；磁盘需自行管理。
- **漂移检查未对真实远端跑过。** 三种漂移用本地裸仓覆盖，尚未在带真实 GitHub 远端的机器上执行。
- **无原生 session 恢复。** 只做工作区接续，不保留 Agent 对话上下文。
- **无定时调度/守护进程模式。** CI 通过 `status --refresh` 查询；`group start` 是短命推进器；不安装定时器。
- **不支持热升级。** 需停止监管进程、备份状态后再升级；schema 变更走 `zoe state migrate`（v1 → v2 → v3 → v4，含备份）。

完整能力矩阵见 [docs/capabilities.md](docs/capabilities.md)。

## CLI 速查

| 领域 | 命令 |
|---|---|
| 宿主与项目 | `doctor`、`project register`、`project context`、`project attest`、`project diff`、`capabilities`、`reconcile` |
| 兼容预检 | `capabilities --check-requirements FILE`（skill 包装器在转发前执行） |
| Agent 与路由 | `agent list`、`agent probe`、`route plan`、`route stats` |
| 任务 | `task prepare/create/list/status/report/events/logs/start/resume/pause/cancel/revise/accept` |
| Agent 问答 | `task answer`（记录回答；`resume` 继续，改变验收标准则走 `revise`） |
| 执行交接 | `task reassign`、`task rebind`、`task recover`、`task close-external`、`task handoff show` |
| 记录 | `task summary submit/list`、`evidence record`、`qualification run`、`state migrate/restore` |
| 任务组 | `group plan/create/start/dispatch/status/pause/resume/cancel/accept/review/repair/report` |
| 编排 | `orchestration show/resume/claim/release/update` |
| 事件流与其他 | `watch`、`demo` |
| 内部入口 | `internal`、`internal-group`（由 `task start` / `group start` 派生，宿主不得直接调用） |

退出码：`0` 成功；`2` 输入不完整/需澄清；`3` 状态或请求冲突（含 `SLOT_BUSY`、
`TASK_BUSY`、游标失效）；`4` 环境或能力问题（`ADAPTER_UNVERIFIED`、
`CAPABILITY_UNSUPPORTED`、`PROCESS_UNKNOWN`）；`5` 被闸门拒绝（`POLICY_DENIED`、
`ALLOWLIST_UNCONFIRMED`、`PERMISSION_PROFILE_UNBOUND`、集成类错误码）；`6` 远端未知。
`task create/start/revise/accept` 对策略拒绝保留历史的 `4`，0.3+ 命令使用 `5`。

完整命令面、请求/响应格式与错误码随 skill 发布：
[`zoe-workflows/references/cli-contract.md`](https://github.com/zoe-runtime/zoe-workflows/blob/main/references/cli-contract.md)。

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/installation.md](docs/installation.md) | 五种安装路径、GUI 宿主 PATH 陷阱、离线安装 |
| [docs/upgrading.md](docs/upgrading.md) | 兼容区间、安全升级顺序、1.5 授权规则迁移、卸载 |
| [docs/releasing.md](docs/releasing.md) | TestPyPI → PyPI 两阶段、可信发布、版本规则 |
| [docs/compatibility.md](docs/compatibility.md) | 运行时查什么、CI 查什么、支持矩阵 |
| [docs/architecture.md](docs/architecture.md) | 状态机、模块图、路由、组推进器、集成账本、审查闸门 |
| [docs/threat-model.md](docs/threat-model.md) | 信任边界、缓解措施、未缓解风险 |
| [docs/capabilities.md](docs/capabilities.md) | 能力 × 证据矩阵 |
| [docs/rollback.md](docs/rollback.md) | 回滚前置条件、状态恢复与远端对账 |
| [docs/adapter-qualification.md](docs/adapter-qualification.md) | 四道准入闸门，以及每道闸门**不**证明什么 |
| [docs/agent-routing-design.md](docs/agent-routing-design.md) | profile 路由与授权模型 |
| [docs/execution-combination-routing-design.md](docs/execution-combination-routing-design.md) | L1/L2/L3 分层、`decide()`、模型身份与费用闸门 |
| [docs/model-selection-authorization-design.md](docs/model-selection-authorization-design.md) | 白名单、确认记录与人工确认链 |
| [docs/task-group-delivery-design.md](docs/task-group-delivery-design.md) | 组计划、推进器、集成与验收语义 |
| [docs/Zoe-Task-Group-Release-Design-v1.md](docs/Zoe-Task-Group-Release-Design-v1.md) | 任务组发布设计记录（冻结版） |
| [docs/compatibility/README.md](docs/compatibility/README.md) | `VERIFIED` 背后的精确版本证据记录 |

## 安全声明

`authorization_ref` 是自声明字符串，**不是**身份认证系统。`project attest` 只证明
「这份配置被确认过一次」：它不记录白名单是谁写的，也不能抵抗对同一 home 目录有写权限的进程。
详见 [docs/threat-model.md](docs/threat-model.md) 与 [SECURITY.md](SECURITY.md)。

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。

---

*English version: [README.md](README.md).*
