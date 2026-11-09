# 任务感知的执行组合路由（Execution-Combination Routing）详细设计

基线：`zoe-runtime` v1.0.0
目标版本：v1.2.0（修订稿 R2）
前序文档：`docs/agent-routing-design.md`（v1.1：受约束的 profile 级路由）

---

## 0 文档定位与本次修订

### 0.1 本文回答什么

前序文档回答"在批准范围内选哪个 profile"。本文回答三件前序文档没有回答的事：

1. 一个 profile 里**实际跑的是什么执行组合**，如何给它一个稳定标识；
2. 凭什么认为这个组合合适（三层事实，严格分账）；
3. 当事实不支持某个承诺时，如何如实降级而不是假装。

目标表述（与前序一致，不得改写为"选最强模型"）：

> **任务感知的执行组合路由**

### 0.2 R2 修订摘要

本稿基于两轮输入：一轮外部评审（6 个必改问题 + 4 组算法/实验/范围建议，全部采纳），以及一轮"费用是否会失控"的风险问询（补充 §6.1.1、§9.3.1、§11.1.1、§12.7）。逐条落点：

| # | 问题 | 状态 | 落点 |
|---|---|---|---|
| 1 | 缺少稳定的执行组合标识，统计会混桶 | 已修正 | §7（`combination_id`），`argv_sha256` 降级为审计摘要 |
| 2 | `confirmed` 语义过强；`pin_model + unknown` 漏洞 | 已修正 | §9（`reported_match` + `observation_scope` + unknown 阻断） |
| 3 | 端点限制靠事后模型对比保证（数据已外发） | 已修正 | §9.5（两档，强档要求启动前核实） |
| 4 | L3 分母错误（成功运行被排除，run/task 口径混用） | 已修正 | §10.3（两个指标、分项计数），§14.3 |
| 5 | 预检/统计/冻结生命周期无闭环 | 已修正 | §5.1、§12、§15.3 |
| 6 | 迁移与回滚退回"删字段即可" | 已修正 | §16（不补默认值、digest 影响清单、降级阻塞） |
| a | 偏好排序使成本/速度开关永不可达 | 已删 | §11.3（v1.2 只有 `prefer`） |
| b | 能力字段被当成硬保证 | 已改语义 | §6.2（标称值/档位/端到端） |
| c | 切换预算计数时点不清 | 已定义 | §13.3（四个计数点） |
| d | 实验设计有四处方法学问题 | 已重写 | §20 |
| e | v1.2 范围过大 | 已缩减 | §2.1–2.3、§21（L3 自动排除推迟） |
| f | "L3 只过滤不排序"被当作消除偏差 | 已修正表述 | §8.4（承认反馈回路） |
| g | 显式/默认绕过硬条件 | 已同步 | §11.1（统一过滤） |
| h | 偏好无交集时按批准列表顶替 | 已同步 | §11.2（交集 + fallback） |
| i | "纯函数"却调用探测；标识被当校验和 | 已同步 | §11.3、§14.2（`snapshot_digest` / `content_sha256`） |
| j | 幂等与失败记录冲突；崩溃无规则 | 已同步 | §15.5 |
| k | 策略收窄与换人死锁 | 已同步 | §16.5（五步路径） |
| l | `start` 失败被当作旧执行不存在 | 已同步 | §12.5（三态） |
| m | `model` 缺省时跑 CLI 默认贵模型（费用风险入口） | 已修正 | §6.1.1（`model` 必填 + 转义口） |
| n | premium 组合被静默替换仍继续 | 已修正 | §9.3.1（premium 默认 pin + `pin_waived` 留痕） |
| o | 无成本上限机制 | 已新增 | §11.1.1（`cost_ceiling` 排除层）、§12.7（`spend_guard`） |
| p | 费用不可观测时被当作"没问题" | 已修正 | §8.2 否决条件、§12.7.1（`SPEND_UNVERIFIABLE`） |
| q | "是否只用指定模型"缺少准确表述与证据层保障 | 已补齐 | §12.8（闸门顺序）、§16.7（`strict_model_enforcement_at_gateway`） |
| r | 闸门失败后改动仍留在工作区，可被误提交 | 已改为严格隔离 | §12.9（`worktree.patch` + 可还原性校验后才恢复干净） |
| s | 隔离后仍需明确"人怎么继续"，否则会留出洗白通道 | 已定案 A 方案 | §12.9.6（只有 `reassign` / `cancel`；不提供恢复与 override） |

### 0.3 与前序 v1.1 文档的兼容性（评审未获该文档，此处自包含）

| v1.1 章节 | 本文处置 | 影响现有实现 |
|---|---|---|
| §6 `routing` 段（mode/allowed_profiles/rules/fallback） | **原样保留** | 无 |
| §8.1 硬条件表 | 被 §8.1 取代（扩展为组合级） | 无（新增要求缺省不启用） |
| §8.3 排序（`prefer` 顺序） | 原样保留 | 无 |
| §9 `RouteDecision v1` | 升级 v2（新增字段，旧字段全保留） | 读旧文件用 `.get()` |
| §10 `route plan` / `route reassign auto` | 保留；`route plan` 去掉 `--probe-model`（§15.1） | 该开关在 v1.1 未发布，无迁移成本 |
| §14 `fallback: ask` | 原样保留 | 无 |
| §15 迁移 | 被本文 §16 取代（更严格） | 见 §16.3 降级阻塞 |

**v1.1 的 `routing` 段字段集不变；v1.2 只在 profile 侧新增字段。** 未声明 `routing` 段的项目在 v1.2 下行为不变（§16.2 有校验项）。

---

## 1 决策摘要

| 决策 | 内容 |
|---|---|
| 选择对象 | 执行组合：`CLI/backend + provider/model/变体 + 推理配置 + 工具权限 + 端点` |
| 组合标识 | `combination_id`（规范化有效配置摘要），统计与证据匹配的唯一键；`profile_id` 只用于人类配置与展示 |
| 三层事实 | L1 硬能力（声明）/ L2 集成事实（可执行）/ L3 任务表现（统计）。三者不合并 |
| **v1.2 的 L3** | **只做只读统计与报告，不影响选择**；影响选择推迟到 v1.3 |
| 排序 | 只有 `prefer` 顺序。价格/速度档只记录，不排序 |
| **成本** | `model` 必填（不写就是 CLI 默认，等于授权空白）；`cost_ceiling` 做**排除**不做排序；`spend_guard` 按实测累计越线即停 |
| 模型观测 | `requested_model` / `reported_model` / `observation_scope` / `model_identity`（`reported_match`/`reported_mismatch`/`mixed`/`unobserved`） |
| 固定模型 | `pin_model=true` 时，`mismatch` 与 `unobserved` **都阻断**；`premium` 组合默认 `true`；发现于运行后，阻止发布而非撤销已发生的事 |
| 端点 | 两档：`preferred`（声明+观测）/ `required`（启动前核实，无法确认则拒绝自动派发） |
| 失败归因 | 五类 + 基础设施单列；两个指标分别统计（尝试级 / 任务级） |
| 切换 | 每次尝试固定一个组合；次数、换人、金额三种预算相互独立；历史证据保留 |
| 新增错误码 | 5 个：`MODEL_UNAVAILABLE`(4)、`MODEL_MISMATCH`(5)、`ENDPOINT_UNVERIFIED`(5)、`SPEND_LIMIT_EXCEEDED`(5)、`SPEND_UNVERIFIABLE`(5) |
| 不做 | L3 自动排除（推迟）、成本/速度自动优化、复杂别名表、主动模型探测（推迟）、加权评分、**精确对账** |

---

## 2 范围

### 2.1 v1.2 必做

1. 显式组合配置：`provider` / `model`（必填）/ `model_variant` / `reasoning_effort` / `capabilities` / `endpoint_ref` / `unit_price`。
2. 适配器映射："声明必须能落实到受支持的有效配置"，不能则拒绝。
3. `combination_id`：构造规则、计算时机、落盘位置。
4. 硬条件过滤（L1）+ 集成可达过滤（L2）。
5. 固定偏好选择（只有 `prefer`）。
6. 启动冻结（`selected_execution` → `frozen_execution`）。
7. 模型观测（四值 + 覆盖范围）。
8. 恢复边界与预算计数时点。
9. **成本防线**：`cost_ceiling`（排除层）+ `spend_guard`（越线即停）+ `premium` 默认 pin。

### 2.2 v1.2 可做（可随版本交付，但独立可裁剪）

9. 只读统计 `route stats` 与 `PerformanceRecord`（不影响任何决策）。
10. 基础失败分类与归因标记。
11. 报告展示（三行：`capability` / `combination_verified` / `performance`）。

### 2.3 明确推迟

| 项 | 目标版本 | 理由 |
|---|---|---|
| L3 自动排除（`use_performance` 生效） | v1.3+ | 需要先有可信统计；v1.2 只记录 |
| 成本/速度自动优化 | v1.3+ | 有效优先级需先有分组语义（§11.3） |
| 复杂别名表（`provider` 别名映射） | v1.3+ | v1.2 只做严格相等，无法确认即 `unobserved` |
| 主动模型探测（`probe-model`） | v1.3+ | 需要独立预算、审计与费用提示；v1.2 只保留设计 |
| 加权评分 / 在线学习 | 不做 | §11.3、§10.5 |

`use_performance: true` 在 v1.2 里**被接受但拒绝执行**：`CAPABILITY_UNSUPPORTED`，message 指明"v1.2 只记录，不据此排除候选"。诚实拒绝优于接受后忽略。

### 2.4 永久非目标

| 非目标 | 理由 |
|---|---|
| 由榜单/厂商介绍生成能力分数 | 二手投影，与提示词/工具形态强相关 |
| 加权评分（成本×质量×速度） | 权重无法校准；"便宜"会抵消"不满足要求" |
| 单次运行内切换模型 | 破坏归因；一个 run 只能对应一个组合 |
| 声称精确控制工具内部路由 | 工具可能自带回退与子代理模型，只能观测报告值 |
| 模型级与 CLI 级 `VERIFIED` 合并 | 一个是效果证据，一个是可执行性证据 |
| **精确对账** | `spend_guard` 是"发现并停止"，不是账单；`estimated` 口径不用于对账（§12.7.1） |
| **预扣额度 / 硬性费用熔断** | 需要网关侧计量；Zoe 只能在 run 结束后累计（§12.7.3） |
| **闸门后的"洗白"通道**（恢复 patch 继续 verify、单次运行 override 模型检查） | 一旦可协商，`pin_model` 退化为建议；需要放宽必须改配置并 `rebind`（§12.9.6） |

---

## 3 术语

### 3.1 "内嵌模型"需要纠正

CodeBuddy / Claude Code / OpenCode 是执行工具（harness），模型是它们调用的推理后端。官方文档层面：OpenCode 支持配置 provider、model 与模型变体；Claude Code 支持模型配置并存在限制、别名与回退机制；CodeBuddy CLI 提供模型选择参数，可选范围受账号、版本与配置约束【外】。

Zoe 能做的是把模型选择转换为各 CLI 支持的启动参数，**不是**读取 Agent 内嵌的模型清单。前者是配置映射（可校验），后者是运行事实（只能观测）。

代码层已具备通道：`model` 是 typed option，内置适配器把它变成 `--model` 【核】

```76:76:src/_agents/base.py
TYPED_OPTIONS = ("model", "max_turns", "permission_profile", "approved_tools")
```

```41:42:src/_agents/claude_code.py
        if context.model or options.get("model"):
            argv += ["--model", str(context.model or options["model"])]
```

三者是三个事实，不能互相替代：**支持选择模型 ≠ 当前账号能用 ≠ Zoe 验证过这个组合。**

### 3.2 执行组合

```text
执行组合 = Agent CLI / backend / 实测 CLI 版本
         + provider / model / 变体 / 有效推理配置
         + 工具与权限配置
         + 端点（不含凭据的标识）
```

同一模型放进不同 harness，文件编辑工具、提示词、上下文压缩、权限与重试都可能不同。**组合能力才是准入依据。**

示例（**不代表能力排名**）：

| Profile | 配置特征 | 候选用途 |
|---|---|---|
| `fast-maintenance` | 低成本模型 + 已验证的编辑工具 | 文档、小范围修改 |
| `deep-debugging` | 较强推理配置 + 测试执行能力 | 跨文件故障定位 |
| `visual-implementation` | 支持图像输入 + 截图传递能力 | 按截图实现界面 |
| `private-development` | 批准的私有端点 + `endpoint_ref` | 有数据处理限制的任务 |

`visual-implementation` 说明关键点：模型支持图像，但 CLI 传不了截图，组合仍然不能接图像任务。L1 声明的是**组合的端到端能力**。

### 3.3 三层事实

| 层 | 回答 | 证据形态 | 作用 |
|---|---|---|---|
| **L1 硬能力** | 理论上能不能干这类活 | 配置声明（人写，机械校验） | 只能排除 |
| **L2 集成事实** | 本机能不能跑起来、参数是否生效、输出能否解析 | 版本证据 + 运行观测 | 只能排除 |
| **L3 任务表现** | 过去做得怎么样 | 分层统计 | v1.2 只记录；v1.3 起才可做门槛过滤 |

三条禁止：不能从 L1 推 L3；不能从 L2 推 L3；不能把三者合成一个 `VERIFIED`。

### 3.4 三个标识的分工

| 标识 | 计算 | 用途 | 不能用于 |
|---|---|---|---|
| `profile_id` | 配置键名 | 人类配置与展示 | 统计分桶（会被改内容） |
| `profile_hash` | `digest({profile_id, profile})`【核】`src/_profiles.py:120` | 检测"这个 profile 的定义变过" | 跨机器比较（含 `compatibility_evidence` 绝对路径） |
| `combination_id` | §7.1 | 证据匹配、统计分桶、run 归因 | 不宣称"远端权重相同" |

---

## 4 现状核对

### 4.1 已有

| 能力 | 证据 |
|---|---|
| `model` 作为 typed option 注入 argv | 【核】`src/_agents/base.py:76`、各 adapter 的 `launch_for` |
| 用量与成本字段 | 【核】`src/_agents/base.py:320-322`、`schemas/agent-result.v1.json` |
| 失败结果已分类 | 【核】`src/_agents/base.py:65-72` |
| 版本"实测 vs 声明"双轨 | 【核】`src/_pipeline.py:1650-1653` |
| 启动前 pin 二进制并复测版本 | 【核】`src/_pipeline.py:1600-1616` |
| run 记录冻结 launch（完整 argv） | 【核】`src/_pipeline.py:1634-1654` |
| `profile_hash` 存在 | 【核】`src/_profiles.py:120-121` |
| 有界探测（只跑 `--version`） | 【核】`src/_profiles.py:509` |

### 4.2 缺失

| 缺口 | 影响 |
|---|---|
| `AgentResult` 无 `model` 字段 | 无法回答"实际用了哪个模型" |
| run 记录无 `combination_id` / `reported_model` / `observation_scope` | 无法按组合归因 |
| profile 无 `provider` / `model_variant` / `reasoning_effort` / `capabilities` / `endpoint_ref` | 无法做 L1 过滤 |
| 无组合级 L2 记录 | L2 只能停在 `(adapter, backend, version)` |
| 无 `PerformanceRecord` | L3 不存在 |
| `AGENT_FAILED` 是兜底码 | 环境问题可能被算成执行器失败 |

### 4.3 契约漂移点（改动必须同一提交）

1. `src/_profiles.py:34-53` 的 `PROFILE_FIELDS`，**且** `src/_agents/base.py:520-539` 的 `validate_profile` 自带同义白名单 —— 两处必须同时改。
2. `schemas/project-profile.v2.json` 的 profile 属性表（新增 6 个字段 + `endpoints` 段）。
3. `src/_agents/base.py:311-360` 的 `AgentResult`（新增可选 `reported_model`）。
4. `schemas/agent-result.v1.json`（新增可选属性，`required` 不变，`protocol_revision` 不变）。
5. `src/_agents/base.py:240` 的 `RunContext.to_dict()`。
6. 各 adapter 的 `launch_for`（映射或拒绝）。
7. `src/_pipeline.py` 的 create / start / reassign 三处记录写入。

---

## 5 设计总览

### 5.1 生命周期六阶段（评审要求划清）

| 阶段 | 产出 | 是否可访问模型/网络 | 落盘 |
|---|---|---|---|
| `route plan` | 纯计算预览：候选、淘汰原因、选中项 | **否** | **不落盘** |
| 显式模型 probe | 该账号/版本是否接受该模型参数 | 是（外部调用） | run 目录下 `probe-model.json` + 事件（v1.3） |
| `create` | `RouteDecision`（选择依据 + 策略版本 + `combination_id` 预期） | 否 | `route-<rev>.json`（`selected_execution` 段） |
| `start` | 复查条件 + 冻结实际组合（CLI 路径/版本/argv）+ 记录 `workspace_baseline` | 否（只解析与复测版本） | run 记录 `frozen_execution` / `workspace_baseline` |
| 运行结束 | 模型身份判定 + 费用累计 + 归因标记（**两道闸，早于 verify**，§12.8） | 否 | run 记录 + `agent_result`，失败时不写 `candidate_sha` |
| `stats` | 只读聚合（固定截止点） | 否 | 不写盘；**缓存由 `route stats --write` 显式生成**（§15.3） |

三处此前冲突的处置：

- `route plan` **不再**提供 `--probe-model`（v1.1 稿的错误：把会调用模型的动作放进只读路径）。probe 是独立命令（§15.2），v1.3 交付。
- `route stats` 不写盘，因此**没有"缓存后的同一份数据"**；v1.2 的 `route plan` 不读统计（L3 不参与选择），这个问题在 v1.2 自然消失。
- `create` 时记录的 `combination_id` 使用**pin 值**（`expected_version`）；`start` 后用**实测值**重算，两者不同即 `combination_id_changed`，按配置漂移处理（§12.5）。

### 5.2 不变量

| 编号 | 不变量 | 强制手段 |
|---|---|---|
| C1 | 三层事实不合成单一状态 | 三个字段，报告分三行 |
| C2 | 观测不到模型身份时标 `unobserved`，不推断 | 四值 + `observation_scope` |
| C3 | 端点的强要求必须在启动前核实，不能靠事后 | §9.5 两档 |
| C4 | 环境/权限/规格/基础设施失败不计入质量指标 | §10.3 分项计数 |
| C5 | 单次尝试固定一个组合 | `frozen_execution` + 禁止运行内切换 |
| C6 | 预算按四个明确时点计数，不重复扣减 | §13.3 |
| C7 | 声明必须能落实到有效配置，否则拒绝 | adapter 映射校验 |
| C8 | 统计与证据按 `combination_id` 分桶 | §7 |
| C9 | v1.2 的统计不影响选择 | `use_performance: true` 直接拒绝 |

---

## 6 profile 契约：从"CLI 名字"到"执行组合"

### 6.1 新增字段

```json
{
  "deep-debugging": {
    "adapter": "claude-code",
    "backend": "anthropic-api",
    "expected_version": "2.111.0",
    "permission_profile": "isolated-edit-and-run",
    "model": "claude-sonnet-4-5",
    "model_variant": null,
    "provider": null,
    "reasoning_effort": "high",
    "endpoint_ref": null,
    "capabilities": {
      "input_modalities": ["text"],
      "context_window": 200000,
      "tool_use": true,
      "structured_result": true,
      "max_output_tokens": 8192
    },
    "cost_class": "standard",
    "latency_class": "slow"
  }
}
```

| 字段 | 类型 | 缺省 | 语义 |
|---|---|---|---|
| `provider` | string | `null` | 需要 `provider/model` 形态的 CLI 才有意义 |
| `model` | string | **无（必填）** | 请求使用的模型。缺失且未开启 `allow_cli_default_model` → `INPUT_INCOMPLETE`（§6.1.1） |
| `model_aliases` | string[] | `null` | v1.5.1：同一次请求在 CLI 上报时可能改用供应商名（实测 `BC-DeepSeek-V4-Flash` → `deepseek-v4.1-flash`）。声明等价名后，全部上报值落在该集合内即 `alias_confirmed`；未声明则按字面判 `reported_mismatch`。需要 `model` 存在，元素不重复 |
| `allow_cli_default_model` | bool | `false` | 显式允许"由 CLI 决定用哪个模型"的转义口，见 §6.1.1 |
| `model_variant` | string | `null` | 工具特有变体，透传给适配器 |
| `reasoning_effort` | enum `low`\|`medium`\|`high` | `null` | 推理档位 |
| `endpoint_ref` | string | `null` | 指向项目级 `endpoints` 段的名字（§6.4），**不含 URL/密钥** |
| `capabilities` | object | 全缺省 | 声明的端到端能力（§6.2） |
| `cost_class` | enum `low`\|`standard`\|`premium` | `standard` | 成本档，**不是金额**；不参与排序，但参与 `cost_ceiling` 上限过滤（§11.1） |
| `latency_class` | enum `fast`\|`standard`\|`slow` | `standard` | 速度档，仅记录 |
| `unit_price` | object | `null` | `{input_per_1k, output_per_1k, currency}`，仅在 `usage.cost` 缺失时用于**估算**；不参与对账（§12.7） |

#### 6.1.1 `model` 必须显式声明（费用风险的第一道闸）

现状事实：`model` 允许缺省，缺省时适配器**根本不传 `--model`** 【核】

```557:561:src/_agents/base.py
        if profile.get("model") is not None:
            require(
                isinstance(profile["model"], str) and profile["model"],
                "model must be a string",
```

```41:42:src/_agents/claude_code.py
        if context.model or options.get("model"):
            argv += ["--model", str(context.model or options["model"])]
```

后果不是"费用略高"，而是：**用户批准了一个他无法描述的组合**。多数编码 CLI 的默认模型是旗舰档，而默认值会随版本、账号档位与服务端配置变化。`combination_id` 把 `model: null` 当作稳定组合，看起来严谨，实际连"用的是什么"都不知道。

规则：

| 情形 | 行为 |
|---|---|
| `model` 已声明 | 正常 |
| `model` 缺失 + `allow_cli_default_model: false`（默认） | `INPUT_INCOMPLETE`，message 指明"必须声明 model，或显式开启 allow_cli_default_model" |
| `model` 缺失 + `allow_cli_default_model: true` | 允许，但该组合自动标记 `model_unresolved: true`：`combination_verified` 恒为 `false`；报告与 `route` 决策记录必须显示"运行模型未解析"；`pin_model: true` 与它互斥（`INPUT_INCOMPLETE`） |

`allow_cli_default_model` 是**转义口而非默认值**：它存在是为了让"确实想跟随 CLI 默认"的项目能表达意图，而不是让疏忽变成默认行为。

### 6.2 `capabilities` 的语义（评审要求逐项降级）

| 字段 | 语义 | 不可声称的事 |
|---|---|---|
| `input_modalities` | 该组合**端到端**能接收的输入类型（模型能力 + CLI 传递能力） | 不能声称"模型支持即可" |
| `context_window` | **标称**窗口（tokens） | 不是"可用输入预算"。可用预算需扣除系统提示、工具定义与输出空间 |
| `tool_use` | 该组合能真正调用工具修改文件/执行命令 | 不能声称"模型支持 function calling" |
| `structured_result` | **端到端**能产出结构化结果（模型能出 JSON **且** CLI 有结构化协议） | 不能声称其中任一方单独成立就够 |
| `max_output_tokens` | 单次输出上限 | 不是"模型最大生成长度" |

`context_window` 的粗筛语义必须写清：

```text
L1 只做粗筛：任务的 context_budget < context_window × 0.6（预留空间由适配器负责）
真正的硬约束仍是现有的 PROMPT_LIMIT_BYTES 与 max_output_bytes（运行期可验证）
```

也就是说 L1 是"明显放不下就排除"，不是"保证放得下"。写进 `detail` 的原因要区分：

```text
"context_budget 240000 exceeds nominal context_window 200000"        ← 明确超限
"context_budget 150000 within nominal window; usable budget unverified" ← 粗筛通过但不确定
```

`cost_class` 不承担预算上限：`cost_ceiling` 这类要求只能由 `max_attempts` + 事后成本观测核对，不能靠档位声明。v1.2 不做预算路由。

### 6.3 映射原则：不是"必须有独立参数"

v1.1 稿的伪代码用 `provider_flag is None` 判断不支持 —— 不充分：适配器也可能通过 `provider/model` 组合形态、环境变量或配置文件映射。

正确规则：

```text
适配器必须能回答"这个字段能否落实到受支持的有效配置"，
判定依据由适配器自己实现（argv / 组合形态 / 配置文件 / 环境变量名），
不能落到有效配置 → CAPABILITY_UNSUPPORTED（拒绝，不静默忽略）
```

理由：静默忽略一个已声明的字段，会让"配置里写了 provider"与"实际没传 provider"看起来完全一样。这与现有的诚实标记同源 —— `permission_profile` 无法落到 argv 时报 `enforced=False` 而不是假装生效【核】`src/_agents/base.py:594-605`。

### 6.4 项目级 `endpoints` 段（用于端点标识，不含凭据）

```json
{
  "endpoints": {
    "private-1": {"class": "private", "attestation": "config-declared"},
    "cloud-default": {"class": "cloud-approved", "attestation": "config-declared"}
  }
}
```

| 字段 | 语义 |
|---|---|
| `class` | `cloud-approved` / `private` / `local` |
| `attestation` | `config-declared`（v1.2 唯一取值；表示"由管理员声明"，不是技术证明） |

**记录里只出现端点名字**，不出现 base_url、密钥、账号标识。这样既能进统计与报告，又不扩大泄露面。

---

## 7 组合标识 `combination_id`

### 7.1 定义

```text
combination_id = "cmb-" + sha256(canonical_json({
    "adapter_id":          entry.adapter_id,
    "backend":             profile.backend,
    "cli_version":         effective_cli_version,      # create: expected_version；start: 实测值
    "provider":            profile.provider,
    "model":               profile.model,
    "model_variant":       profile.model_variant,
    "reasoning_effort":    profile.reasoning_effort,
    "permission_profile":  profile.permission_profile,
    "approved_tools":      sorted(profile.approved_tools or []),
    "max_turns":           profile.max_turns,
    "endpoint":            {id, class} if endpoint_ref else null,
    "attributes":          capabilities（只含已声明项）
}))[:16]
```

规范化的三条纪律：

1. **不含绝对路径**：`compatibility_evidence`、`repo`、`workspace`、`session_dir` 一律不进摘要（跨机器可比）。
2. **不含凭据**：`credential_refs` 的名称也不进（只进 `credential_presence` 布尔，且不进摘要）。
3. **缺省即不出现**：未声明的字段不出现在 canonical JSON 里（不是填 `null`）。这样"同一个组合换一台机器"仍同 id。

### 7.2 为什么不用 `profile_hash` 或 `argv_sha256`

| 候选 | 为什么不够 |
|---|---|
| `profile_id` | 内容会改（换模型/权限），历史混桶 |
| `profile_hash` | 含 `compatibility_evidence` 绝对路径 → 跨机器不同；不含实测 CLI 版本与端点 |
| `argv_sha256` | `launch.argv` 含 `prompt_file`、`workspace`、`session_dir` 等每次变化的路径（【核】`src/_pipeline.py:1634-1654`）；也可能漏掉环境变量与配置文件中的有效设置 |

`argv_sha256` 保留，但**只作为本次启动的审计摘要**（字段名 `argv_audit_sha256`），明确声明"不用于标识与分桶"。

### 7.3 计算时机与漂移

| 时机 | `cli_version` 取值 | 一致性检查 |
|---|---|---|
| `create` | `expected_version`（pin） | — |
| `start` | 实测 `cli_version`【核】`src/_pipeline.py:1650` | 重算 `combination_id`；与 create 记录不同 → 事件 `combination_changed`，按配置漂移处理（§12.5） |
| 运行结束 | 不变 | 把 `reported_model` 观测附在该 id 上 |

---

## 8 三层事实

### 8.1 L1 硬能力（声明 + 机械校验）

参与位置：**过滤**。绝不参与排序。

| 任务要求 | 判定 | 排除码 |
|---|---|---|
| `input_modalities` | 要求 ⊆ 声明 | `CAPABILITY_UNSUPPORTED` |
| `context_budget` | 粗筛（§6.2），超限即排除 | `CAPABILITY_UNSUPPORTED` |
| `needs_tool_use` | 声明为 true | `CAPABILITY_UNSUPPORTED` |
| `structured_result` | 声明为 true **且** `adapter.output_protocol == "json-final"` | `CAPABILITY_UNSUPPORTED` |
| `non_interactive` | `permission.enforced == true`（现状） | `PERMISSION_PROFILE_UNBOUND` |
| `data_residency` | 见 §9.5 的两档 | `ENDPOINT_UNVERIFIED` / `POLICY_DENIED` |
| `session_resume` | 恒不满足 | `CAPABILITY_UNSUPPORTED` |

`structured_result` 需要双重条件：模型能出 JSON 但 CLI 只输出自由文本，组合仍不合格。

### 8.2 L2 集成事实

| 档 | 内容 | 判据 |
|---|---|---|
| L2-a 版本 | CLI 版本与 pin 一致 | `expected_version` + 实测 `cli_version`【核】 |
| L2-b 参数 | 模型/ provider 参数被 CLI 接受 | 历史运行记录（`probe-model` 在 v1.3） |
| L2-c 输出 | 输出可解析为 `AgentResult` | `outcome != invalid_output` 的历史记录 |

`combination_verified` = 三者同时满足。说明：

- 这是**集成**证据，不说明"跑得好"。
- 粒度含 `model` 与 `endpoint`，但**不进 D9 二进制指纹**（§8.3）。
- 无证据的组合仍可派发（现状 `unverified` 语义不变），但必须标 `combination_verified: false`。

三个**否决条件**（任一成立则 `combination_verified` 恒为 `false`）：

| 否决条件 | 理由 |
|---|---|
| `model_unresolved == true`（§6.1.1） | 连"跑的是哪个模型"都不知道，谈不上"这个组合被验证过" |
| `usage` 的 `input_tokens`/`output_tokens`/`cost` **全为 `None`** | 无法核算成本，就不能声称"这个组合被完整观测过"。**"没有数据"不等于"没有问题"** |
| `model_identity == reported_mismatch` 或 `mixed`（§9.1） | 参数没生效，集成事实不成立 |

### 8.3 为什么模型不进 D9 指纹

D9 指纹绑定**本地可复算的产物**（CLI 二进制 + 平台 + 版本）。模型不在本地，也无法复算。因此：

- 版本可以指纹化；"模型参数生效"只能靠运行观测；
- `compat-evidence` 可加 `model` / `combination_id` 作为**标签**，但不得塞进指纹匹配，否则会出现"指纹匹配、模型早已不同"的假精确。

配置摘要证明的是"请求配置相同"，**不声称**远端权重相同。

### 8.4 L3 任务表现（v1.2 只记录）

v1.2 的 L3 只有两个用途：

1. 生成 `PerformanceRecord` 报告；
2. 供人工判断"这个组合是否值得继续用"。

**不参与选择。** `use_performance: true` → `CAPABILITY_UNSUPPORTED`（§2.3）。

关于反馈偏差，必须如实写：

```text
"只过滤不排序"能降低错误决策的暴露面，
但不能消除反馈偏差：被过滤掉的组合同样失去获得新样本的机会，
于是更容易持续被过滤。这是保守实现选择，不是统计可靠性的证明。
```

因此 v1.3 若启用 L3 自动排除，必须同时提供：定期人工复核、抽样强制分派（exploration quota）、以及"排除原因可解释"。

### 8.5 三个字段，不是一个状态机

| 字段 | 取值 | 含义 |
|---|---|---|
| `capability` | `IMPLEMENTED`/`UNVERIFIED`/`VERIFIED` | 现状：`(adapter, backend)` 的 e2e 证据【核】 |
| `combination_verified` | `true`/`false` | L2：该组合的参数与输出是否被实测过 |
| `performance` | 统计对象或 `null` | L3：分层统计，可能为 `null` |

报告必须分三行。把三者压成一个 `VERIFIED` 的具体后果：一次"能启动"的成功会让读者以为"这个模型修 bug 很强"。

---

## 9 模型观测

### 9.1 四个字段与四值

| 字段 | 来源 |
|---|---|
| `requested_model` | profile 声明（`provider` / `model` / `model_variant`） |
| `reported_model` | 适配器从 CLI 输出解析，解析不到即 `null` |
| `observation_scope` | `final_result` / `main_call` / `request_echo` / `mixed` / `none` |
| `model_identity` | 判定结果 |

`model_identity` 五值（v1.5.1 增加 `alias_confirmed`；v1.2 曾按评审建议不做别名表，实测
"请求名 ≠ 供应商名"的 CLI 上 pin 因此不可能满足，故补回，且只接受**显式声明**的等价名）：

```text
reported_match      单一 reported 与 requested 严格相等，scope ∈ {final_result, main_call}
alias_confirmed     全部 reported 都落在 {model} ∪ model_aliases 内，但不逐字相等
reported_mismatch   单一 reported 既不等于 requested 也不在声明别名内
mixed               观测到多个不同的 reported，且不能全部归入同一模型集合
unobserved          reported 为 null，或只有请求回显（scope == request_echo / none）
```

`identity_basis` 记录结论依据（`exact` / `declared_alias` / `mismatch` / `mixed` /
`unresolved_request` / `none`），报告不得把 `declared_alias` 写成"逐字一致"。

`request_echo` 的来源是 `system`/`init` 行：实测 codebuddy 2.111.0 在该行回显
`custom-local:BC-DeepSeek-V4-Flash`（**请求**），而 `assistant` 行的 `message.model` 是
`deepseek-v4.1-flash`（**供应商**）。把回显算作 `main_call` 会让一次正常运行变成 `mixed`
并阻断 pin；把它单独记录则保留审计价值而不污染判定。`model_of` 同时读
`providerData.model`，因为 `--output-format json` 的顶层可能没有 `model`。

关键语义修正（评审 2）：`reported_match` **只证明**

> 工具报告的模型标识与请求匹配

它**不证明**：整次运行所有子代理都用同一模型；服务端实际权重版本；工具内部没有中间回退。因此：

- 字段名不再叫 `confirmed`（暗示"确认"），而叫 `reported_match`（报告匹配）；
- 必须同时记录 `observation_scope`，让读者知道覆盖范围；
- 报告文案固定为"报告匹配（scope=final_result）"，不得简写为"已确认"。

### 9.2 观测点

| 位置 | 增量 |
|---|---|
| `AgentResult` | 可选 `reported_model`（`to_dict`/`from_dict` 同步；`schema_version` 仍为 1，`required` 不变） |
| `AgentResult` | 可选 `reported_models: list`（用于 `mixed`，元素为 `{model, scope}`） |
| run 记录 | `requested_model`、`reported_model`、`observation_scope`、`model_identity`、`identity_basis`、`model_aliases` |
| 事件 | `model_drift`（payload：`requested`/`reported`/`scope`/`identity`），与 `version_drift` 同位置【核】`src/_pipeline.py:1656-1668` |
| 报告 | 「执行器组合」行显示四值，`unobserved` 不等于失败但必须显示 |

### 9.3 `unobserved` 的处置（补齐评审指出的漏洞）

| 任务要求 | `model_identity` | 处置 |
|---|---|---|
| 未指定固定模型 | 任意 | 记录并继续 |
| `pin_model: true` | `reported_match` | 继续 |
| `pin_model: true` | `alias_confirmed` | 继续（依据是 profile 声明的 `model_aliases`，被 `attest` 覆盖；`identity_basis=declared_alias`） |
| `pin_model: true` | `reported_mismatch` / `mixed` | `MODEL_MISMATCH`(exit 5) |
| **`pin_model: true`** | **`unobserved`** | **`MODEL_MISMATCH`(exit 5)，message 说明"当前适配器无法提供所需的确认程度"** |

v1.1 稿遗漏了最后一行。固定模型是强承诺，**"看不到"不能被当作"符合"**。

同样地，历史上一轮 `reported_match` **不能**证明本次仍相同 —— 因此 pin 的校验是**每次运行的观测**，不是一次性认定。

#### 9.3.1 `premium` 组合默认开启 pin（贵的必须确认）

`pin_model` 的默认值按成本档区分：

| `cost_class` | `pin_model` 缺省值 | 理由 |
|---|---|---|
| `low` / `standard` | `false` | 模型跑错了通常也不会造成显著费用意外 |
| **`premium`** | **`true`** | 一旦工具回退到别的贵模型，单次成本可能翻数倍；这是最需要确认的一档 |

显式写 `pin_model: false` 于 `premium` 组合是允许的，但必须留痕：

```text
注册时 → events 记 pin_waived=true，警告"premium 组合关闭了模型身份确认"
报告中 → 该组合显示「模型确认已关闭（pin waived）」
```

不允许"静默关闭"：`premium` + `pin_model: false` 而没有任何提示，会让读者以为这个组合是受约束的。

注意 pin 的能力边界：`pin_model` 依赖工具**上报**模型。若工具不上报，pin 只能给出 `unobserved` → 阻断（而不是"证明没用错"）。因此它是"拒绝在不确定时继续"，不是"保证用对了"。

### 9.4 `mismatch` 的处置时机（不能称"启动前防护"）

`reported_model` 只能在**运行结束后**获得。因此：

| 事实 | 正确表述 | 错误表述 |
|---|---|---|
| 运行后观测到 `mismatch` | "本次运行未满足固定模型要求，已阻止进入发布阶段" | "启动前阻止了错误模型" |

发现 mismatch 后的动作顺序（**检查点必须早于 verify**）：

```text
run 结束（进程已退出，agent_result 已解析）
  ↓
① 模型身份判定 —— 必须在此刻做，早于任何验证动作
     reported_model 解析 → observation_scope → model_identity
  ↓
② 若 pin_model 且 identity ∉ {reported_match, alias_confirmed}：
     该 run 作废：
       - candidate_sha / validated_sha 保持 null（不写）
       - 不生成 evidence.json，不进入 verify
       - 工作区隔离：归档为 patch 并恢复干净（§12.9，前置满足时才执行）
       - 保留 runs/<id>/agent.log
       - 任务 BLOCKED，block_code = MODEL_MISMATCH，details 带四值
       - 不自动换模型
  ↓
③ 否则进入 verify —— 这一阶段才允许写 candidate_sha = HEAD
```

为什么顺序不能反：`candidate_sha` 是**跑完检查之后**才写入的【核】`src/_pipeline.py:1739`、`:1758`。若 pin 检查被放到"阻止进入 publish"，那么 verify 已经基于错误模型的产物生成了候选与证据 —— 发布虽然被挡，但证据链里会留下"这个提交通过过验证"的记录，而这条记录足以让人手动发布。

三个对象处置不同，不能都说成"清空"或"保留"：

| 对象 | pin 失败时 | 理由 |
|---|---|---|
| 工作区文件 | **归档为 `runs/<id>/worktree.patch` 并恢复干净**（可还原，前置不满足时退回"保留"，§12.9） | 既保留证据，又不让它被误当作可交付产物 |
| `runs/<id>/agent.log` | **保留** | 排障与事后复核的唯一来源 |
| `candidate_sha` / `validated_sha` / `current_evidence_id` | **不写或清除** | 它们表示"可交付"，而这次交付不可信 |

如果因为流程缺陷已经写入了 `candidate_sha`，必须用现有的清空机制把它置 `null`【核】`src/_pipeline.py:1257-1271`，并记 `evidence_invalidated` 事件说明原因。

### 9.5 端点的两档（评审 3：不能靠事后保证）

v1.1 稿写"私有端点回落到云端 → `MODEL_MISMATCH` 阻断"是错的：**数据已经发出去了，事后阻断无法撤销外发**；而且 `reported_model` 根本不携带端点身份。

改为两档：

| 需求 | 语义 | 可接受实现 | 不满足时 |
|---|---|---|---|
| `data_residency: preferred` | 优先用私有端点，允许回退 | 配置声明 + 运行观测报告 | 记录 `endpoint_observed: unknown`，继续运行，报告标注 |
| `data_residency: required` | **只允许**发送到批准端点 | **启动前**核实有效端点与回退配置；无法强制或无法确认 → 拒绝自动派发 | `ENDPOINT_UNVERIFIED`(exit 5)，不派发 |

`required` 档的现实边界（必须写进文档与 skill）：

```text
Zoe 能核实：endpoint_ref 指向的端点配置存在、class == private、适配器能把它落实到启动配置
Zoe 不能核实：CLI 内部是否在限流/失败时静默回退到云端、服务端是否记录了数据去向
因此 required 档要求"显式禁止回退的配置"，配置达不到就拒绝派发，而不是派发后祈祷
```

同时：`endpoint_class: private` 是**人为标签**，不是技术保证。`attestation: config-declared` 已在 §6.4 写死，避免读者误读。

---

## 10 失败分类与归因

### 10.1 六类失败（新增"基础设施"单列）

| 类 | 例子 | 进质量指标 |
|---|---|---|
| `environment` | 缺依赖、磁盘满、CLI 未装、网络不通 | 否 |
| `infrastructure` | 测试基础设施错误、超时（网络/CI 侧）、证据被外部修改 | **否**（v1.1 稿把它混进 `verification`，已修正） |
| `auth_permission` | 认证失败、权限未绑定、审批被拒、限流 | 否 |
| `spec` | 验收标准矛盾、开放问题未解决 | 否 |
| `executor` | 输出不可解析、进程崩溃、明确能力不足 | 是（组合可靠性） |
| `verification` | 代码跑不通检查、AC 未通过 | 是（实现质量） |

### 10.2 码表映射

| 现有码 | 分类 | 进尝试级指标 | 进任务级指标 |
|---|---|---|---|
| `ENVIRONMENT_ERROR` / `INPUT_INCOMPLETE` / `POLICY_DENIED` / `POLICY_CHANGED` / `SLOT_BUSY` / `PROCESS_UNKNOWN` | environment / spec | 否 | 否（另计） |
| `RUN_TIMEOUT` / 测试基础设施错误 | **infrastructure** | 否 | 否（另计） |
| `EVIDENCE_INVALID` / `EVIDENCE_MISSING` | **infrastructure** | 否 | 否（另计） |
| `AGENT_AUTH_REQUIRED` / `AGENT_PERMISSION_DENIED` / `PERMISSION_PROFILE_UNBOUND` / `AGENT_RATE_LIMITED` | auth_permission | 否 | 否（另计） |
| `SPEC_CLARIFICATION_REQUIRED` | spec | 否 | 否（另计） |
| `AGENT_RESULT_INVALID` / `AGENT_FAILED` | executor | 是 | 视任务结局 |
| 检查失败 | verification | 是 | 视任务结局 |
| `MODEL_MISMATCH` | executor（**组合可靠性**） | 是（可靠性） | 否（**不计入代码质量**） |
| `MODEL_UNAVAILABLE` | 未产生 run | 否（无 run） | 否 |
| `ENDPOINT_UNVERIFIED` | 未产生 run | 否 | 否 |

评审指出的三条收紧，已落表：

- `EVIDENCE_INVALID` 可能来自控制器、过期证据或外部修改 → 归 `infrastructure`，不归实现失败。
- 超时可能来自网络或测试基础设施 → 归 `infrastructure`。
- `MODEL_MISMATCH` 计入"组合可靠性"，**不得**计入"请求模型的代码质量"（两者是两个不同的质量维度）。

### 10.3 两个指标与分母（评审 4：修正真实错误）

v1.1 稿把"失败类别 ∈ {executor, verification}"写成进入统计的条件，导致**成功运行被排除**，随后又用它算通过率。这是直接错误，此处重写。

**指标 A：尝试验证通过率（run 级）**

```text
分母 = 所有"可评价运行" = 成功运行 + executor 失败 + verification 失败
分子 = 其中"独立验证通过"的运行

排除（不进分母，单独计数）：
  environment / infrastructure / auth_permission / spec / 未结束 / 已取消 / purpose=probe
```

**指标 B：任务交付率（task 级）**

```text
分母 = 观察窗口内已分配（create 成功）的 task
分子 = 其中获得人工验收通过的 task

排除（单独计数）：待人工验收 / 已取消 / 未结束
```

**禁止的回填**：一个任务先由 A 失败、后由 B 完成，最终 `accepted` **只**记给 B 的 run 与 B 的组合；A 的 run 记为 `executor` 失败。不得把"任务最终通过"回填给 A。

**必须分项报告的计数**（不允许合并成一个百分比）：

| 计数字段 | 含义 |
|---|---|
| `runs_evaluable` | 指标 A 分母 |
| `runs_passed` | 指标 A 分子 |
| `runs_excluded_environment` / `_infrastructure` / `_auth` / `_spec` | 排除分项 |
| `runs_unfinished` / `_cancelled` | 排除分项 |
| `runs_identity_unobserved` | 模型身份未知（不影响 A，但影响"按模型"的结论） |
| `tasks_assigned` / `tasks_accepted` / `tasks_pending_acceptance` / `tasks_cancelled` | 指标 B 与其排除项 |

### 10.4 不可归因清单

以下运行**永远**不进按组合的质量结论（可以进可靠性结论）：

1. `model_identity ∈ {unobserved, mixed}`；
2. `purpose == "probe"`；
3. 失败类 ∈ {environment, infrastructure, auth_permission, spec}；
4. 该 run 之后任务被 `cancel`（人为放弃，不是执行结果）。

### 10.5 probe 必须在事前标记（评审 4 最后一条）

"切换前试跑"不能事后按结果剔除，否则失败样本会被洗掉。规则：

- probe 运行在创建时即写 `purpose: "probe"`，**不可修改**；
- probe 使用独立预算（§13.3），不占任务 `attempts`；
- probe 结果只进"集成事实"（L2-b），不进质量指标。

---

## 11 选择算法

### 11.1 过滤（唯一会排除候选的地方，显式路径不免检）

```text
候选 =
  请求显式指定时    :  [requested_profile]        （须 ∈ allowed_profiles，否则 POLICY_DENIED）
  mode == auto 时   :  allowed_profiles ∩ agent_profiles
  其余（显式/默认）  :  [default_agent_profile]   （须 ∈ allowed_profiles，注册期已校验）

for pid in 候选:
    binding = resolve_binding(c, pid)                → 失败即 excluded
    L0: 成本上限（§11.1.1）                           → excluded(COST_CEILING_EXCEEDED)
    L1: requirements 逐项判定                         → excluded(CAPABILITY_UNSUPPORTED/POLICY_DENIED/ENDPOINT_UNVERIFIED)
    L2: require_runnable(...)                        → 能力态闸门（现状）
    L2-c: doctor 就绪 + CLI 已解析                    → excluded(ENVIRONMENT_ERROR)
```

显式指定只是把候选**收窄为 1 个**，不跳过 L0/L1/L2。失败时返回该候选的具体排除码（如 `PERMISSION_PROFILE_UNBOUND`），**不回落**到其他 profile。

#### 11.1.1 成本上限 `cost_ceiling`（排除，不是排序）

```json
"routing": { "cost_ceiling": "standard" }
```

| 值 | 含义 |
|---|---|
| `low` | 只允许 `cost_class == low` |
| `standard` | 允许 `low` / `standard` |
| `premium` | 不限制（默认） |

判定：`order(cost_class) > order(cost_ceiling)` → `excluded(code="COST_CEILING_EXCEEDED")`。

三点必须写清：

1. **这是排除，不是排序。** 它位于字典序的第 0 层（先于硬条件之前或同级），因此不可能出现"便宜但不符合要求"被选中——那正是上一轮评审反对的加权语义。
2. **不新增顶层错误码。** 它只作为 `excluded` 条目出现在 `candidates[]` / `layers.l1_excluded`；若所有候选都因此被排除，最终仍报 `ROUTE_NO_ELIGIBLE`，`gaps` 里能看到每条的具体原因。
3. **默认 `premium` = 不限制**，保持 v1.1 行为；收窄必须显式声明。这样"费用上限"是一个可审计的配置决定，而不是运行时推断。

理由是现状事实：`create` 本就不调用 `require_runnable`（v1.1 §4.4），显式路径若无过滤就成了准入后门。

淘汰即记录，`candidates[]` 覆盖全部候选。

### 11.2 偏好交集与 fallback（批准范围不等于优先顺序）

```text
hits = [pid for pid in rule.prefer if pid in eligible_ids]

if hits:                     → 选 hits[0]（rule_preference / catch_all_preference）
elif len(eligible) == 1:     → only_eligible（独立规则，与偏好无关）
else:                        → fallback
```

**不得**把"不在 `prefer` 中的 eligible"追加到末尾来凑一个结果。那会在 `prefer` 全部不可用时**偷偷**按批准列表选人，而 `reason` 仍写 `rule_preference` —— 记录会撒谎，且违背"批准范围决定谁可以执行，不决定优先选谁"。

`fallback` 语义：

| `fallback` | 条件 | 结果 |
|---|---|---|
| `default_profile` | 默认 profile 在 `eligible_ids` 内 | `fallback_default` |
| `default_profile` | 不在 | `ROUTE_AMBIGUOUS`（默认不得无条件胜出） |
| `ask` | — | `ROUTE_AMBIGUOUS` + `options[]`，`reason` 区分 `prefer_empty` / `no_rule` |

### 11.3 快照与决策分离（纯函数边界）

```python
snapshot = collect_environment(config, candidates)   # 有副作用：doctor / resolution，产出 snapshot_digest
decision = decide(config, requirements, snapshot, requested_profile)   # 纯函数，只读 dict
```

`decide()` 可表驱动单测（构造 snapshot 即可）；`RouteDecision v2` 因此新增 `snapshot_digest`（§14.2）。幂等重放命中操作记录时**直接返回**，不再探测、不再选择。

### 11.4 为什么删掉成本/速度排序（评审 a）

v1.1 稿的字典序是 `prefer → cost_class → latency_class`。但如果 `prefer` 列出了全部候选（常见写法），第一层已经产出唯一顺序，**后两层永远不可达** —— 是两个死开关。

试图绕过（改成"同优先级组内比较"）需要先定义"组"，而组语义本身会变成排序规则的一部分，等于用配置语法重写权重。

因此 v1.2：

- 删除 `prefer_cheaper` / `prefer_faster` 开关；
- 保留 `cost_class` / `latency_class` 作为**记录与展示**字段（报告里可见，便于人判断）；
- 成本/速度优化明确推迟到 v1.3，且届时必须先回答"是组内比较，还是用户主动选择成本优先模式"（§2.3）。

**"删除排序"与"保留上限"不矛盾**（区分要写清，否则会被读成又引入了评分）：

| 语义 | 作用 | 是否保留 |
|---|---|---|
| **排序**（贵的排前/排后） | 在多候选里挑一个 | **删除**：`prefer` 已决定顺序，加排序层是死开关，且会让"便宜"影响选择 |
| **上限**（超过 `cost_ceiling` 就排除） | 把不该出现的候选挡在门外 | **保留**（§11.1.1）：它只做排除，不参与挑谁，因此不会抵消"不满足要求" |

一句话：**成本只能用来"禁止"，不能用来"偏好"。**

### 11.5 无数据 / 无规则的默认

```text
无规则 + 多候选 + fallback=ask            → ROUTE_AMBIGUOUS（前序文档 §14）
无规则 + 多候选 + fallback=default_profile → 默认 profile（须通过过滤）
单候选                                    → only_eligible
```

不生成"看起来精确的能力分数"。

---

## 12 启动前确认与冻结

### 12.1 `selected_execution`（create 阶段）

`create` 时能确定的都冻结，**不能确定的字段留 `null`**，不猜：

```json
"selected_execution": {
  "profile_id": "deep-debugging",
  "combination_id": "cmb-9c1d3f0a77b2",       // 用 pin 版本计算
  "adapter_id": "claude-code",
  "adapter_backend": "anthropic-api",
  "provider": null,
  "model": "claude-sonnet-4-5",
  "model_variant": null,
  "reasoning_effort": "high",
  "permission_profile": "isolated-edit-and-run",
  "endpoint": {"id": null, "class": null},
  "cli_version_pinned": "2.111.0",
  "cli_version_effective": null,               // 待 start 填
  "policy_revision": "a3f0…",
  "routing_digest": "9c1d…"
}
```

### 12.2 `frozen_execution`（start 阶段）

`start` 复查后补全实际值：

```json
"frozen_execution": {
  ...selected_execution,
  "combination_id": "cmb-…",                   // 用实测版本重算
  "cli_version_effective": "2.111.0",
  "resolved_path": "/abs/bin/codebuddy",
  "resolution_source": "path_alias",
  "argv_audit_sha256": "…",                    // 仅审计，不用于标识（§7.2）
  "frozen_at": 1761700000.0
}
```

`combination_id` 在两个阶段不同 → `combination_changed` 事件，按 §12.5 处理。

### 12.3 模型可用性三档

| 档 | 手段 | 能证明 | v1.2 |
|---|---|---|---|
| A 形态 | `validate_profile` + 映射校验 | 参数形态合法、字段能落实 | 总是执行 |
| B 主动探测 | `probe-model`（真实调用一次） | 该账号/版本是否接受该参数 | **不做**（v1.3） |
| C 事后观测 | `reported_model` 比对 | 实际报告了什么 | 总是执行 |

现有 `probe_profile` 只跑 `--version`【核】`src/_profiles.py:509`，**不能**验证模型可用性。这一点必须写进 skill 文档，避免读者把 `agent probe` 当成"模型可用"的证据。

### 12.4 probe 的独立预算（v1.3 交付，此处定规则）

- 独立计数，不占任务 `attempts`；
- 有超时与单项目每日上限；
- 写审计事件（`model_probe`），含 `profile_id`、`combination_id`、结果、可能的费用提示；
- 不改 `combination_verified`（一次探测不是一次 e2e 任务）。

### 12.5 `start` 失败不等于旧执行不存在

不能因为一次 `start` 失败就假设"没有进程在跑"。启动握手失败、控制器崩溃或进程状态未知时，Agent 仍可能存活。三态判定（与 v1.1 §11 一致）：

| 观测 | 判断依据 | 允许换人 |
|---|---|---|
| **明确未启动** | 无 run 记录，或 `dispatch_state == "spawn_intent"` 且从未观测到进程 | 是 |
| **明确已退出** | `run.status == "exited"` | 是 |
| **状态未知** | `block_code == PROCESS_UNKNOWN`，或 `spawn_observed` 而 `status != exited` | **否**：先 `reconcile` |

现状实现已有对应保护【核】`src/_pipeline.py:1211-1213`（`not r or r["status"] == "exited"`）与 `:1200-1203`（`block_code != PROCESS_UNKNOWN`），文档不得比实现更宽松。

失败后给出的下一步提示必须是可执行的具体文件路径，不使用 `<( )` 进程替换（不是合法 JSON，且 Windows 不可用）：

```text
zoe task reassign <id> --request-file /abs/path/reassign.json
```

### 12.6 选择后配置变化 → 不静默换

现状已有 `POLICY_CHANGED`（项目配置与冻结配置不一致）【核】`src/_pipeline.py:690`。补充两条：

| 变化 | 处置 |
|---|---|
| `routing` 段或 profile 定义变化 | `POLICY_CHANGED`（现状），要求 `rebind` |
| 实测 `cli_version` ≠ pin | 现状会记 `version_drift` 并在 `verify_cli_version` 时拒绝【核】`src/_pipeline.py:1600-1616`；`combination_id` 相应变化，写 `combination_changed` |

**不得**在 `start` 时悄悄换成另一个候选（即使它"现在可用"）。换人必须是显式的 `reassign`，因为它要 stop-confirmed 与预算扣减。

### 12.7 费用护栏（`spend_guard`）

`cost_ceiling` 管的是"允许哪一档"，管不住"这一档跑了多少次、每次都超预期"。因此需要第二道闸：**按实测用量累计，越线即停**。

```json
"routing": {
  "spend_guard": {
    "max_cost_per_task": 5.0,
    "max_cost_per_day": 30.0,
    "currency": "USD",
    "on_exceed": "block"
  }
}
```

| 字段 | 类型 | 缺省 | 语义 |
|---|---|---|---|
| `max_cost_per_task` | number | `null`（不限） | 单个任务累计（含所有 assignment 与 attempt）上限 |
| `max_cost_per_day` | number | `null`（不限） | 单项目自然日累计上限 |
| `currency` | string | `null` | 与 `unit_price.currency` 一致才可比较 |
| `on_exceed` | `block` \| `warn` | `block` | 越线行为；v1.2 只实现 `block`，`warn` 拒绝（`CAPABILITY_UNSUPPORTED`） |

#### 12.7.1 数据来源与三种成本口径

| `cost_source` | 来源 | 能不能用于对账 | 能不能用于护栏 |
|---|---|---|---|
| `reported` | `AgentResult.usage.cost`（工具上报）【核】`src/_agents/base.py:320-322` | 可以（与账单口径接近） | 可以 |
| `estimated` | `(input_tokens × unit_price.input + output_tokens × unit_price.output)`，单价由 profile 的 `unit_price` 声明 | **不可以** | 可以（判"是否越线"，不判"花了多少"） |
| `unavailable` | 三字段全 `None` | 不可以 | **不可以**：此时护栏无法工作，必须显式降级 |

`unavailable` 的处置（关键）：护栏**不静默放行**，而是：

```text
若 max_cost_per_task 或 max_cost_per_day 已配置，而本轮 cost_source == unavailable:
    任务在运行结束后置 BLOCKED，block_code = SPEND_UNVERIFIABLE
    message: "费用不可观测，无法核实是否越过护栏；请为该组合声明 unit_price 或换用上报成本的工具"
    → 不自动继续、不自动换人
若未配置任何上限 → 只记录 cost_source，不阻断
```

理由：配了护栏却因为"没有数据"而默认放行，等于护栏只在有数据时才存在，而无数据恰恰是最需要护栏的场景。

#### 12.7.2 越线时的动作（`block`）

```text
1. 不进入下一阶段（publish / ci / accept）
2. 保留工作区与 runs/<id>/agent.log（与 §9.4 一致）
3. 任务置 BLOCKED，block_code = SPEND_LIMIT_EXCEEDED，details 带
   {limit, scope: task|day, observed, cost_source}
4. 不自动换模型、不自动重试
5. 由人决定：调高上限（改配置 + register + rebind）或放弃
```

调高上限本身是一次配置变更，会改变 `policy_digest` → 在途任务 `POLICY_CHANGED` → 需 `rebind`（§16.5 五步）。这条路径必须写清，否则"越线后改配置继续跑"会变成一个隐蔽的后门。

#### 12.7.3 护栏能防什么、不能防什么

| 能 | 不能 |
|---|---|
| 单任务跑飞（死循环、反复重试、子代理扩张）导致的费用堆积 | 工具在**单次调用内**就已经花掉的钱（护栏是**事中/事后**判定，不是预扣额度） |
| `unavailable` 场景下的静默放行 | 服务端价格变动（`unit_price` 是声明值，会过期） |
| 多任务并发的日累计（配合 `max_active_tasks ≤ 8`） | 工具内部子代理的**归属**（只能看总量异常，看不到是谁） |

**一句话边界：护栏是"发现并停止"，不是"预先冻结额度"。** 它在第一次越线后必然已经产生了超额费用，只是不再放大。这一点必须在用户文档中直说。

#### 12.7.4 与 `attempts` / `max_assignments` 的关系

| 预算 | 维度 | 计数点 |
|---|---|---|
| `max_attempts` | 次数 | `start` 创建执行尝试（§13.3） |
| `max_assignments` | 换人次数 | `reassign` 成功（§13.3） |
| `max_cost_per_task` | **金额** | 每次 run 结束后累加（含失败 run 的用量） |

三者独立、互不替代：换人一次可能花掉 3 次尝试的钱，也可能一次都不花（环境失败无用量）。因此**不能**用"次数还没用完"推断"费用还安全"。

### 12.8 运行结束后的闸门顺序（决定"证据是否可信"）

run 退出到 verify 之间有两道闸，顺序固定：

```text
run 退出（dispatch_state 已收口，agent_result 已解析）
   │
   ├─ 解析层（总是执行，无副作用）
   │    reported_model / reported_models → observation_scope → model_identity
   │    usage（input/output tokens、cost）→ cost_source 判定
   │    failure_class 判定（六类）
   │    spend 累计（**即使后面失败也要记，钱已经花了**）
   │
   ├─ 闸门 A：模型身份（§9.4）
   │    pin_model 且 identity ∉ {reported_match, alias_confirmed}
   │      → 该 run 作废：candidate_sha / validated_sha 保持 null
   │      → 不写 evidence，不进 verify
   │      → 工作区隔离：归档 patch + 恢复干净（§12.9）
   │      → BLOCKED + MODEL_MISMATCH
   │
   ├─ 闸门 B：费用（§12.7）
   │    cost_source == unavailable 且配了上限 → SPEND_UNVERIFIABLE
   │    累计 cost > max_cost_per_task / max_cost_per_day → SPEND_LIMIT_EXCEEDED
   │      → 同样不写 candidate_sha、不进 verify
   │
   └─ 都通过 → verify（此阶段才写 candidate_sha = HEAD，随后 validated_sha）
```

四条纪律：

1. **解析层无条件执行**：即使闸门 A 失败，也要把 `usage` 记账。否则"用错模型的那一次花了多少"永远查不到。
2. **闸门失败一律不产生候选**：`candidate_sha` 为 `null` 是"本次不可交付"的唯一可信信号。若只阻断 publish 而不阻断 verify，证据链会留下一条不该存在的"通过"记录。
3. **两道闸互不替代**：模型对了也可能超支，费用合规也可能跑错模型。
4. **闸门不改变 `phase`**：`phase` 停在 `implement` 或 `verify` 之前，不前进、不回退；状态由 `state=BLOCKED` + `block_code` 表达。

这一节是"是否仅仅使用指定模型"这个问题在**证据层**的答案：Zoe 不能阻止工具用别的模型，但能保证**用错的那一次不会留下可交付的产物**。

### 12.9 工作区隔离（`worktree.patch` + 恢复干净）

闸门 A 失败时，仅仅"不写 `candidate_sha`"是不够的：改动还躺在工作区里，一次 `git add -A && git commit` 就能把它变成提交。因此需要把改动**移出工作区**，但**不能丢**。

#### 12.9.1 前置条件：`workspace_baseline`

现状 `start` **不要求工作区干净**，`handoff.json` 只记录运行后的 `git status --porcelain`【核】`src/_pipeline.py:1856`，没有启动前基线。没有基线就无法区分"本次 run 的改动"与"用户原有的未提交改动"——盲目清理会删掉用户的工作。

因此 `start` 在 spawn 之前必须落盘：

```json
"workspace_baseline": {
  "head": "abc1234",
  "dirty_paths": [],            // 启动时已有的未提交改动（含新增文件）
  "porcelain_sha256": "…",
  "captured_at": 1761700000.0
}
```

#### 12.9.2 隔离流程（全部步骤成功才丢弃）

```text
0. 条件检查（任一不满足 → 不做清理，落 cleanup="skipped" 并记原因）
     - task.state == BLOCKED 且 block_code == MODEL_MISMATCH
     - baseline.dirty_paths == []                 # 启动时工作区必须干净
     - workspace 存在且 git 可用
1. git add -A                                     # 把新增文件也纳入，否则 patch 会漏
2. git diff --cached --binary > runs/<id>/worktree.patch
3. git reset                                      # index 回 HEAD，工作区不动
4. 可还原性校验（**最关键的一步**）
     - patch 非空（或确认工作区本就无改动）
     - shell: git apply --check --reverse runs/<id>/worktree.patch
     - 记录 patch_sha256
   ← 任一项失败 → 立即停止，保留工作区原样（清理=aborted）
5. git checkout -- .                              # 只回退已跟踪文件
6. git clean -fd -- <allowed_paths...>             # 仅限 allowed_paths；不加 -x，保住 ignored 文件
7. 复核：git status --porcelain 应为空
     ← 不为空 → cleanup="partial"，如实记录残留路径
```

三个刻意的选择：

| 选择 | 原因 |
|---|---|
| 先 `add -A` 再 `diff --cached` | 只用 `git diff` 会**漏掉新增文件**，而新增文件恰恰是最常见的交付物 |
| 必须 `git apply --check --reverse` 通过才丢弃 | 这是"能还原"的**可执行证明**，不是靠主观判断。校验不过就不清理——**丢数据的风险远大于误提交的风险** |
| `clean -fd` 不加 `-x`，且限定 `allowed_paths` | 不加 `-x` 才不会删 `node_modules`/`venv` 这类 ignored 目录；限定路径避免清到仓库外的临时产物 |

#### 12.9.3 不满足前置时的降级

```text
baseline.dirty_paths 非空（启动时就有用户改动）
  → 不清理，只标 BLOCKED + candidate_sha=null
  → details.cleanup = "skipped: workspace was dirty at start"
  → 记录该情形：这说明"启动前工作区不干净"本身就是一个该被治理的问题
```

这条降级规则同时给出了一个产品建议：**长期应要求 `start` 前工作区干净**（或把 dirty baseline 也算作 `ENVIRONMENT_ERROR`），否则隔离机制只能退化为"保留 + 警告"。

#### 12.9.4 还原与保留期

```text
还原：git apply runs/<id>/worktree.patch
```

- patch 属于任务证据的一部分，**不得自动删除**（与 `evidence.json` 同级保留）；
- 事件 `worktree_quarantined` 记录：`patch_sha256`、`patch_bytes`、`checkout_paths`、`cleanup`（`done`/`partial`/`skipped`/`aborted`）、`reason`；
- 报告里显示一行「工作区已隔离（patch 可还原）」，避免读者误以为改动丢失。

#### 12.9.5 与 `reassign` 的区别

| 场景 | 工作区 |
|---|---|
| 闸门 A/B 失败（本次不可信） | **隔离**：归档 patch + 恢复干净 |
| 人主动 `reassign`（正常换人） | **保留**：工作区就是交接材料，配合 `handoff.json` 指纹交接 |
| `stop` / `pause` | **保留**：任务可恢复继续 |

只有"该 run 被判定不可交付"才隔离；"人要换人继续干"绝不能清理工作区。这两件事看起来都是"run 结束"，但语义相反。

#### 12.9.6 隔离之后如何继续（只有两条路）

| 决策 | 操作 | 结果 |
|---|---|---|
| **换组合重做** | `task reassign --request-file <abs>.json`（`target_profile: "auto"` 或显式） | 工作区已干净，新 run 从零开始；新 attempt 计入预算；新 run 记录自己的 `workspace_baseline` |
| **放弃** | `task cancel`，或保持 `BLOCKED` 归档 | 任务终止，patch 与日志作为事故证据保留 |

**不提供第三条路**（这是刻意的，不是遗漏）：

| 不提供的 | 原因 |
|---|---|
| `task apply-worktree` / `--restore-patch` 一类命令 | 它会把"作废的改动"直接变成"下一次的基线"，闸门就此变成建议 |
| `--override-model-check` / `approve and continue` 开关 | 一旦可协商，`pin_model` 就失去意义；需要放宽时应改配置（会触发 `POLICY_CHANGED` + `rebind`），而不是对单次运行开后门 |
| 事后把作废 run 的 `candidate_sha` 补回 | 证据链不允许回填 |

人仍然可以做的事（系统不阻止、但不背书）：

```text
1. 阅读 patch：git apply --stat runs/<id>/worktree.patch
2. 把结论写进新任务的 spec（人是自由文本的作者）
3. 自行复制其中有价值的部分
```

第 3 项是人的机器操作，系统不记录、不背书。**它不会让旧 run 变成"通过"**：旧 run 永远是 `candidate_sha=null` + `MODEL_MISMATCH`，新 run 的观测只描述新组合。这是 A 方案的准确边界——**不提供洗白通道，而不是物理阻止人复用代码**。

运维提示：同一任务连续两次因 `MODEL_MISMATCH` 被隔离时，`hint` 应指向"检查 `pin_model` 与模型配置"，而不是"再换一个组合"——连续触发通常说明配置错了，不是运气不好。

---

## 13 恢复与切换

### 13.1 未启动失败 → 可换

条件（现状）：无 run 或 `run.status == "exited"`【核】`src/_pipeline.py:1211-1213`，`phase ∈ prepare/implement/verify`【核】`:1206-1210`。

### 13.2 已改代码 → 必须走恢复流程

```text
1. task pause / stop，确认进程树停止
2. 核对工作区：handoff.fingerprint == fingerprint(t)，否则 HANDOFF_STALE
3. 生成/更新 handoff
4. reassign：清空"当前有效引用"，保留历史证据（§13.4）
5. 新组合从 phase=implement 重新开始，session_mode=new
```

### 13.3 预算计数时点（评审 c）

| 计数点 | 何时 +1 | 幂等重放 | 说明 |
|---|---|---|---|
| `assignments` | `reassign` **成功变更归属**时 | 不增 | 记录换人次数，用来校验 `max_assignments` |
| `attempts` | `start` **创建实际执行尝试**（写 run 记录）时 | 不增 | 现状语义【核】`src/_pipeline.py:788-794` 附近 |
| probe | probe 独立计数 | 不增 | 不占 `attempts` |
| 失败的路由尝试（`create` 未产生 task） | 不计数 | 不增 | 没有任务就没有预算 |

这样避免 v1.1 稿描述的"reassign 计一次、下次 start 又计一次"的双重扣减：`assignments` 与 `attempts` 是两个不同维度的预算，各自单调，互不替代。

### 13.4 历史证据保留，只清当前引用

`reassign` 现状会把任务上的 `validated_sha` / `candidate_sha` / `current_evidence_id` / `acceptance` 置 `null`【核】`src/_pipeline.py:1257-1271`。

措辞修正：这是**清除当前有效引用**，不是删除证据。`runs/<run_id>/evidence.json` 与 `evidence/<id>.json` 文件仍在，`handoff.json` 记录旧指纹。文档与报告必须用"当前有效证据已作废（历史仍可查）"，不能写"证据已清空"。

### 13.5 单次尝试固定一个组合

```text
一个 run → 一个 combination_id → 一个观测结果
```

禁止运行中换模型/端点/推理档位。工具内部自行回退不可控，只能靠 `observation_scope` 与 `mixed` 如实记录。

---

## 14 数据契约

### 14.1 run 记录增量

| 字段 | 类型 | 说明 |
|---|---|---|
| `combination_id` | string | §7.1 |
| `requested_model` / `reported_model` | string\|null | §9.1 |
| `observation_scope` | enum | `final_result`/`main_call`/`mixed`/`none` |
| `model_identity` | enum | 四值 |
| `endpoint` | object\|null | `{id, class, attestation}`，不含 URL/密钥 |
| `purpose` | enum | `task` / `probe`（创建时写入，不可改） |
| `failure_class` | string\|null | 六类之一 |
| `attribution` | object | `{"attempt_metrics": "counted"\|"excluded", "delivery_metrics": "counted"\|"excluded", "reason": "..."}` |
| `frozen_execution` | object | §12.2 |
| `argv_audit_sha256` | string | 仅审计 |

`attribution` 是刻意设计的：**归因决策本身也要留痕**，否则三个月后无法解释"为什么这次失败没算进统计"。

### 14.2 `RouteDecision v2`

```json
{
  "schema_version": 2,
  "route_id": "rt-…",
  "task_id": "task-…",
  "assignment_revision": 1,
  "trigger": "create",
  "mode": "auto",
  "decision": "selected",
  "task_type": "bugfix",
  "task_type_source": "request",
  "selected_execution": {"...": "§12.1"},
  "requirements": {
    "input_modalities": ["text"],
    "needs_tool_use": true,
    "context_budget": 120000,
    "structured_result": true,
    "pin_model": false,
    "data_residency": "preferred"
  },
  "layers": {
    "l1_excluded": [{"profile_id": "…", "code": "CAPABILITY_UNSUPPORTED", "detail": "input_modalities missing 'image'"}],
    "l2_excluded": [{"profile_id": "…", "code": "ENVIRONMENT_ERROR", "detail": "CLI not resolved"}]
  },
  "order": {"source": "user_preference", "sequence": ["deep-debugging", "fast-maintenance"]},
  "policy_revision": "a3f0…",
  "routing_digest": "9c1d…",
  "snapshot_digest": "5b7c…",
  "content_sha256": "…"
}
```

变化说明：

- 新增 `selected_execution`（v1.1 只有 profile 三元组）；
- `layers` 只有两组：v1.2 没有 L3 淘汰，因此**不出现** `l3_excluded`（v1.3 再加）；
- 新增 `requirements` 完整快照，便于复核"当时是按什么要求选的"；
- 新增 `snapshot_digest`：决策基于的环境快照摘要（§11.3），使"同一快照 + 同一配置 → 同一决策"可复核；
- 新增 `content_sha256`：文件全文完整性校验（§15.5）。`route_id` / `combination_id` 只是**关联标识**，不承担防篡改职责。

### 14.3 `PerformanceRecord v2`

```json
{
  "schema_version": 2,
  "project_id": "example",
  "generated_at": 1761700000.0,
  "as_of": 1761700000.0,
  "window": {"since": 1759000000.0, "assignments_considered": 62},
  "counts": {
    "runs_evaluable": 41, "runs_passed": 31,
    "runs_excluded_environment": 6, "runs_excluded_infrastructure": 4,
    "runs_excluded_auth": 3, "runs_excluded_spec": 2,
    "runs_unfinished": 4, "runs_cancelled": 2,
    "runs_identity_unobserved": 5,
    "tasks_assigned": 40, "tasks_accepted": 24,
    "tasks_pending_acceptance": 9, "tasks_cancelled": 3
  },
  "buckets": [
    {
      "combination_id": "cmb-9c1d3f0a77b2",
      "profile_id": "deep-debugging",
      "execution": {"adapter_id": "claude-code", "backend": "anthropic-api",
                    "cli_version": "2.111.0", "model": "claude-sonnet-4-5",
                    "reasoning_effort": "high", "endpoint": {"id": null, "class": null}},
      "task_type": "bugfix",
      "spec_scale": "M",
      "attempt": {"n": 12, "passed": 9, "pass_rate": 0.75},
      "delivery": {"n": 10, "accepted": 7, "delivery_rate": 0.70,
                   "pending_acceptance": 2},
      "rework": {"assignments_per_task": 1.25, "extra_attempts": 3},
      "wall_clock": {"median_seconds": 1840, "p95_seconds": 5200},
      "usage": {"input_tokens": 412000, "output_tokens": 51000,
                "cost": 4.82, "cost_source": "reported"},
      "spend": {"reported": 4.82, "estimated": null,
                "unavailable_runs": 0, "breaches": 0},
      "reliability": {"model_mismatch_runs": 1, "executor_failures": 3}
    }
  ],
  "not_comparable": [
    {"combination_id": "cmb-…", "n": 5, "reason": "model_identity=unobserved; excluded from quality comparison"}
  ]
}
```

口径（每条都影响结论，必须固定）：

| 项 | 规则 |
|---|---|
| 分桶键 | `combination_id × task_type × spec_scale` |
| `spec_scale` | 按 AC 数量：`S` ≤ 3，`M` 4–8，`L` > 8。**只是规格规模，不是难度**（§20.1） |
| 指标 A 分母 | `runs_evaluable`（成功 + executor + verification） |
| 指标 B 分母 | `tasks_assigned`（窗口内 create 成功） |
| 成本来源 | `reported` / `estimated` / `unavailable`，**不得混算**；三者在 `spend` 里分列，不合成一个 `cost` |
| `spend.breaches` | 该桶内触发 `SPEND_LIMIT_EXCEEDED` 的运行数，用于判断"这个组合是否经常跑飞" |
| `as_of` | 固定截止时间戳；`route stats` 不读未结束 run |
| `not_comparable` | 身份未知或样本不足的桶，只列不比较 |

### 14.4 报告增量

```text
执行器组合    claude-code / anthropic-api / claude-sonnet-4-5 (effort=high)  [cmb-9c1d3f0a77b2]
集成事实      combination_verified: false（无该组合的 model 参数实测记录）
模型观测      requested=claude-sonnet-4-5  reported=claude-sonnet-4-5  scope=final_result
              身份=报告匹配（仅证明工具报告值与请求值一致）
选择依据      user_preference（prefer[0]）
质量数据      未用于选择（v1.2 只记录）
```

`capability` / `combination_verified` / `performance` 三行必须分开，任一为 `null`/`false` 时不得显示为"已验证"。

---

## 15 接口

### 15.1 `route plan`（纯计算）

```bash
zoe route plan --project example --request-file route-request.json --json
```

- 不访问模型/网络、不写任何文件、不加锁；
- **允许有界版本探测**：`doctor()` 经 `resolution_for_adapter` 传入 `parse_probe`，每候选 spawn 一次 `--version`【核】`src/_discovery.py:441-459`；输出 `snapshot_budget.probes` 如实报告，`--no-probe` 可关闭（关闭时版本档记 unverified）；
- **不提供 `--probe-model`**（v1.1 稿的错误）；
- 输出 `layers`（两组淘汰）、`selected_execution` 预览（`cli_version_effective: null`）、`combination_id` 预期值。

`combination_id` 预览用 pin 值计算，并在输出里标注 `basis: "pinned_version"`，避免读者把它当作最终值。

### 15.2 `agent probe-model`（v1.3 交付，规则先定）

```bash
zoe agent probe-model --project example --profile deep-debugging --json
```

- 外部调用，有超时、独立预算、审计事件、费用提示；
- 失败 → `MODEL_UNAVAILABLE`(4)，`details.source="probe"`；
- 不改变 `combination_verified`，不进质量指标（`purpose="probe"`）。

### 15.3 `route stats`（只读聚合）

```bash
zoe route stats --project example --window 40 --json
zoe route stats --project example --window 40 --write     # 显式生成缓存
```

关于"缓存"的闭环（评审 5）：v1.1 稿说"在线决策读缓存后的同一份数据"，却没定义谁写缓存。v1.2 的处置：

| 事实 | 说明 |
|---|---|
| `route stats` 默认只读、不写盘 | 输出 `as_of` 截止时间戳 |
| `--write` 显式生成 `stats-<as_of>.json` | 供人工复核与跨次比较，**不被 `route plan` 读取** |
| v1.2 的 `route plan` 不读任何统计 | 因为 L3 不参与选择（§8.4），"缓存"问题在 v1.2 不存在 |
| v1.3 若启用 L3 | 必须先引入"策略快照 = 统计 as_of + routing_digest"，并写进 `RouteDecision` |

这条是刻意的：先不做缓存，比做一个语义不明的缓存安全。

### 15.4 错误码

新增 5 个：

| 码 | exit | 语义 | `details` |
|---|---|---|---|
| `MODEL_UNAVAILABLE` | 4 | 参数被拒 / probe 失败 | `model`、`provider`、`source`（`probe`/`config`） |
| `MODEL_MISMATCH` | 5 | 要求固定模型，实际 `reported` 不符或 `unobserved` | `requested`、`reported`、`scope`、`identity`、`pin_model` |
| `ENDPOINT_UNVERIFIED` | 5 | `data_residency: required` 但无法确认端点 | `endpoint_ref`、`class`、`reason` |
| `SPEND_LIMIT_EXCEEDED` | 5 | 累计费用越过 `spend_guard` | `limit`、`scope`（`task`/`day`）、`observed`、`cost_source` |
| `SPEND_UNVERIFIABLE` | 5 | 配了护栏但本轮 `cost_source == unavailable` | `limit`、`scope`、`reason` |

复用（不新增）：

| 场景 | 码 |
|---|---|
| 声明能力不满足 | `CAPABILITY_UNSUPPORTED` |
| 数据去向不允许（声明 class 不在允许集合） | `POLICY_DENIED` |
| 组合集成未验证 | 不改码，走 `combination_verified: false` |
| 切换超预算 | `BUDGET_EXHAUSTED`（现状） |
| v1.2 启用 L3 | `CAPABILITY_UNSUPPORTED`（明确"只记录"） |
| 未声明 `model` 且未开启转义口 | `INPUT_INCOMPLETE`（§6.1.1） |
| 成本档超过 `cost_ceiling` | 不作为顶层错误码，只进 `candidates[].code`（§11.1.1） |

### 15.5 幂等、失败记录与崩溃一致性

| 承诺 | 准确表述 |
|---|---|
| 重放 | 命中 `store.operation` → **直接返回已保存结果，不再探测、不再选择**。因此 `route_id` 与 `combination_id` 在重放时不会因环境变化而改变 |
| 路由失败 | **不创建任务、不启动执行、不生成成功派发记录**；**允许保留操作失败记录**（`operations.status: failed`，这是排障依据）。"失败不落任何记录"是错的表述 |
| 补充 profile 后重试 | 请求内容已变 → 必须使用**新的 `request_id`**；旧 id 的重放只会返回旧的失败 |
| 并发 | `route plan` / `route stats` 只读无锁；`route stats` 不读未结束 run |
| 计数 | `reassign` 成功后 `assignments` +1（§13.3），重放不增 |

崩溃一致性（决策文件 → 任务记录 → 操作记录，顺序写入）：

```text
1. atomic() 写 route-<rev>.json（含 content_sha256）
2. 同一事务写任务记录（含 route_id / snapshot_digest / route_content_sha256）+ 事件
3. store.operation 记成功

start 前硬检查（R9）：
  任务有 route_id → 决策文件必须存在且 content_sha256 与任务记录一致
                    否则 ROUTE_RECORD_INVALID（exit 3），不启动
  任务无 route_id（v1.0.0 旧任务）→ 按现状处理
```

两个标识的职责必须分开：`route_id` / `combination_id` 是**关联标识**（可复现，用于幂等与报告引用），`content_sha256` 是**完整性校验**（覆盖文件全文，含排除原因与环境观测）。混用会让"改了排除原因而关联标识不变"成为可能。

---

## 16 迁移与降级

### 16.1 新字段不做默认值回填（关键）

`policy_digest` 对**规范化后的整份配置**取摘要【核】`src/_profiles.py:124-135`。因此：

```text
normalize_config 不得为 profile 新字段写入任何默认值。
未声明的字段 = 不出现在 profile 字典里 = 不进入 digest。
```

允许的默认值只有两类：

| 默认值 | 是否写回配置 | 理由 |
|---|---|---|
| `routing.*` 段内部的缺省（如 `fallback: ask`） | **仅当 `routing` 键存在时**写回（前序文档 §6.3 已定） | 保证同段内语义确定 |
| profile 新字段的缺省 | **不写回** | 保证旧配置 digest 不变 |

### 16.2 digest 影响清单（必须逐项验证）

| 变更 | 是否改变 `policy_digest` | 在途任务影响 |
|---|---|---|
| 无 `routing` 段、无新 profile 字段 | 否 | 无 |
| 新增 `routing` 段 | 是 | `start` → `POLICY_CHANGED`，需 `rebind` |
| profile 新增 `capabilities` / `model` / `provider` 等**显式字段** | 是（该 profile 的定义变了，`profile_hash` 与 digest 都变） | 同上 |
| 新增 `endpoints` 段 | 是 | 同上（仅在引用它的项目里） |
| 新增 `max_assignments` | 是 | 同上 |
| 仅升级运行时、不改配置 | 否 | 无 |

对 `max_assignments` 的处置：为避开"只加一个预算参数就触发 `POLICY_CHANGED`"，v1.2 把它放在 **`routing` 段内**（`routing.max_assignments`），而不是项目顶层。这样"没用路由的项目"完全不受影响。

### 16.2.1 `model` 必填是**破坏性校验**（升级前必须处理）

`model` 从"可缺省"改为"必填"（§6.1.1），这是本版唯一的破坏性变更：**现有 profile 若有未声明 `model` 的，升级后会注册失败**。

| 升级路径 | 操作 | 适用场景 |
|---|---|---|
| A 补声明 | 给每个 profile 写 `model`，并在 `compatibility_evidence` 里补一次 e2e 记录 | 推荐；顺带把"实际用什么"固定下来 |
| B 显式转义 | 写 `allow_cli_default_model: true` | 确实要跟随 CLI 默认（该组合 `combination_verified` 恒 false） |

不接受"先升运行时、配置以后再说"：注册会直接失败（fail-closed）。这是刻意的 —— 费用风险正是从"没人知道自己用了什么"开始的。

迁移检查清单（进 `docs/upgrading.md`）：

```text
1. 列出所有 agent_profiles，标注哪些没有 model
2. 对每一个：补 model 或加 allow_cli_default_model: true
3. 若不补，register 会报 INPUT_INCOMPLETE（明确列出缺 model 的 profile 名）
4. 补完后 register → 必然触发 policy_digest 变化 → 对在途任务 rebind（§16.5）
5. 用 route plan --json 核对：每个候选的 combination_id 是否与预期一致
```

第 4 步的副作用需要提前告知：**即使迁移本身只补了 `model`，在途任务也会 `POLICY_CHANGED`**。建议在无在途任务时升级（§16.3 的降级流程同样要求先清空在途任务）。

### 16.3 降级是阻塞，不是"恢复执行"（评审 6）

v1.1 稿写"删 `routing` 段即可回到显式派发"，这对**路由行为**成立，但对**安全约束**不成立：

| 降级动作 | 实际结果 |
|---|---|
| 删除 `routing` 段 | 回到默认 profile 派发；*若默认 profile 带 `capabilities` / `endpoint_ref`，约束仍在（好）* |
| 删除 profile 的 `capabilities` | L1 相关要求全部判为不满足 → **更严格**，不是"恢复执行" |
| 删除 `endpoint_ref` 而任务仍要求 `data_residency: required` | `ENDPOINT_UNVERIFIED` → 拒绝派发（fail-closed） |
| 降级到 v1.0.0（不认识新字段） | `validate_profile` 报未知字段 → `INPUT_INCOMPLETE`，**整个项目无法注册** → 无法派发任何任务 |

最后一行必须写进 `docs/upgrading.md`：

```text
降级到 v1.0.0 / v1.1.x 之前：
1. 结束或暂停所有在途任务
2. 从项目配置中移除 profile 新字段（capabilities / endpoint_ref / provider / ...）
   与 routing 段
3. 重新 register
4. 再降级
不能靠"删掉约束让我们跑起来"绕过：约束删除只会让要求更严格或直接拒绝派发。
```

### 16.4 旧任务的冻结配置

规则：**旧任务保留冻结配置**，新配置只用于新任务或显式 `rebind`。

| 场景 | 行为 |
|---|---|
| 旧任务（有 `record_version: 2`，无 `combination_id`） | 报告显示"无组合标识（v1.1 之前创建）"；不参与统计分桶 |
| 旧任务 `start` | 用冻结配置解析；若项目配置已变 → `POLICY_CHANGED`（现状） |
| `rebind` | 采用当前策略；重算 `combination_id` 并写 `combination_rebound` 事件（失准处置见 §16.5） |
| `revise` | 只改 spec，不换组合、不重算 `combination_id` |

### 16.5 策略收窄与换人：修复操作死锁

现状 `reassign` 要求"项目配置与冻结配置一致"【核】`src/_pipeline.py:1184-1188`；若 `rebind` 又拒绝"当前 profile 已不在新 `allowed_profiles` 内"，两者互为前置，形成死锁：

```text
rebind  →  需要先 reassign
reassign → 需要先 rebind
```

修正：`rebind` **不拒绝**，改为"采用新策略 + 必要时阻断任务"。完整可走通路径：

| 步骤 | 命令 | 结果 |
|---|---|---|
| 1 | `task pause`（或已停止） | 确认进程树停止（§12.5 三态） |
| 2 | `task rebind` | 无条件采用当前策略（更新 `t.config`、重算 `policy_revision`、重算 `combination_id`） |
| 3a | 当前组合仍在新范围内 | 任务恢复 `READY` |
| 3b | 当前组合已不在范围内 | 任务 `BLOCKED` + `block_code: POLICY_DENIED` + `details.reason: "combination no longer allowed"`；**不清空工作区与历史证据** |
| 4 | `task reassign`（`target_profile: "auto"` 或显式） | digest 已一致，`POLICY_CHANGED` 不再触发；换人成功 |
| 5 | `task start` | 继续执行 |

"策略收窄不得静默生效"（3b 确实阻断了）与"必须有可执行路径"（步骤 4 可达）同时成立，且不新增命令或错误码。

若步骤 4 仍无候选可用，明确要求**新建任务**，并在 `hint` 里直说，不要给出走不通的提示。

### 16.6 "删除 routing 即回滚"的准确表述

| 动作 | 实际后果 |
|---|---|
| 删除 `routing` 段 | **改变 `policy_digest`** → 在途任务 `POLICY_CHANGED`；同时**移除批准范围**。这不是无影响回退 |
| 只想回到人工选型 | 保留 `routing`、把 `mode` 改为 `explicit`（批准范围保留，可逆） |

正确顺序：停派发 → `mode: explicit` → `register` → `rebind`（§16.5）→ 确认无在途任务后再考虑删除整段。

### 16.7 `not_supported` 声明清单

`capabilities --json` 的 `not_supported` 必须新增四条。这不是免责声明，而是防止用户把"调度器能力"误读成"执行层保证"：

| 条目 | 说明 |
|---|---|
| `model_quality_scoring` | 不做模型质量评分与自动择优 |
| `internal_routing_control` | 不能控制工具内部的回退、别名解析与子代理模型选择 |
| `performance_based_exclusion` | v1.2 不用历史表现排除候选（v1.3 起才可能） |
| **`strict_model_enforcement_at_gateway`** | **不提供网关级模型白名单**。"只允许指定模型"的**事前强制**只能在 API 网关或单模型私有端点上实现；Zoe 只能做到"请求指定 + 事后作废"（§12.8） |

最后一条尤其重要：它是"是否仅仅使用用户指定的模型"这个问题的准确答案。任何文档或 skill 不得把它写成"Zoe 保证只用指定模型"。

---

## 17 安全与威胁

| 威胁 | 缓解 |
|---|---|
| 声明与实况不符（声明 image，CLI 传不了） | L1 只排除；L2 需运行记录；报告三行 |
| 端点声明为 `private` 但实际回退云端 | `data_residency` 两档：`required` 档要求启动前核实，达不到则拒绝派发（§9.5） |
| 固定模型被静默替换 | 四值 + `unobserved` 也阻断；mismatch 阻止发布 |
| 用"换个模型"绕过验收 | 换人不重置 `attempts`；旧引用作废；`max_assignments` |
| 统计被环境失败污染 | 六类分账 + 分项计数 |
| 归因被事后调整（洗样本） | `purpose` 创建时写死；`attribution` 留痕且不可回改 |
| 记录泄露端点/凭据 | 只记 `endpoint.id` + `class`；不含 base_url、密钥、账号 |
| 用统计反向操纵选择 | v1.2 明确 `use_performance: true` 直接拒绝 |
| **`model` 未声明导致跑 CLI 默认贵模型** | `model` 必填；转义口 `allow_cli_default_model` 需显式开启且 `combination_verified` 恒 false（§6.1.1） |
| **工具回退到更贵模型** | `premium` 组合默认 `pin_model: true`；`unobserved` 也阻断（§9.3.1） |
| **重试/换人把费用放大** | `max_attempts` / `max_assignments` / `spend_guard` 三维独立（§12.7.4） |
| **费用不可观测时护栏失效** | `cost_source == unavailable` 且配了上限 → `SPEND_UNVERIFIABLE` 阻断（§12.7.1） |
| 越线后"改配置继续跑"成为后门 | 调高上限会改 `policy_digest` → `POLICY_CHANGED` → `rebind`（§12.7.2） |
| **不可信改动被误提交**（工作区还留着） | 闸门失败即隔离：归档 patch + 恢复干净（§12.9），且必须通过 `git apply --check --reverse` 才丢弃 |
| **隔离动作本身丢数据** | 可还原性校验不过 → `cleanup=aborted`，工作区原样；baseline 脏 → 不清理（§12.9.3） |

---

## 18 测试矩阵

新增 `tests/test_v120_combination.py`。

| 类别 | 用例 | 断言 |
|---|---|---|
| 决策 | 显式 profile 未满足 `capabilities` | 返回该候选具体排除码，**不回落**到别的组合 |
| 决策 | `allowed=[A,B,C]`、`prefer=[A]`、A 不可用、`fallback=ask` | `ROUTE_AMBIGUOUS`（不得选 B）；`reason=prefer_empty` |
| 决策 | 唯一候选 | `only_eligible`，与偏好无关 |
| 决策 | 同一 snapshot 两次 `decide` | 逐字段相同（纯函数） |
| 幂等 | 同 `request_id` 重放 | 不探测环境（桩计数 0）、`combination_id` 不变 |
| 崩溃 | 决策文件在、任务记录不在 | 孤儿文件无害；重试可覆盖 |
| 崩溃 | 任务有 `route_id`、决策文件缺失或 `content_sha256` 不符 | `start` → `ROUTE_RECORD_INVALID` |
| 三态 | `block_code=PROCESS_UNKNOWN` 时 `reassign` | 拒绝；`reconcile` 后按三态表判断 |
| 死锁 | 策略收窄 → rebind（组合失准 → BLOCKED）→ reassign(auto) → start | 五步全部可走通 |
| 费用 | `model` 缺失且未开启 `allow_cli_default_model` | `INPUT_INCOMPLETE`（注册期） |
| 费用 | `model` 缺失 + 转义口开启 | 允许，但 `combination_verified=false` 且报告显示"运行模型未解析" |
| 费用 | 转义口开启 + `pin_model: true` | `INPUT_INCOMPLETE`（互斥） |
| 费用 | `cost_class: premium` 未写 `pin_model` | 默认 `true`；显式 `false` 时 `pin_waived=true` 留痕 |
| 费用 | `cost_ceiling: standard` + `premium` 候选 | `excluded(COST_CEILING_EXCEEDED)`，且全部被排除时仍报 `ROUTE_NO_ELIGIBLE` |
| 费用 | 单任务累计 cost 越过 `max_cost_per_task` | `SPEND_LIMIT_EXCEEDED`，不进入 publish，不自动换人 |
| 费用 | `usage` 三项全 `None` + 已配上限 | `SPEND_UNVERIFIABLE`，任务 `BLOCKED`（不得静默放行） |
| 费用 | `usage` 三项全 `None` + 未配上限 | 只记录 `cost_source=unavailable`，不阻断 |
| 费用 | `estimated` 成本越线 | 同样阻断（`cost_source` 随记录落盘，便于分辨口径） |
| 费用 | 失败 run 的用量 | 计入 `max_cost_per_task`（失败也花钱） |
| 闸门 | `pin_model` 且 `reported_mismatch` | `candidate_sha` / `validated_sha` 均为 `null`，无 `evidence.json`，`phase` 未前进 |
| 闸门 | `pin_model` 失败但 `usage` 有值 | 该次 `cost` 仍计入累计（解析层无条件执行） |
| 闸门 | 闸门 A 失败、闸门 B 也会越线 | 两者都记录；`block_code` 报 A（顺序固定），B 的原因进 details |
| 闸门 | 已错误写入 `candidate_sha` 后补判 | 置 `null` 并记 `evidence_invalidated` |
| 隔离 | 闸门 A 失败 + baseline 干净 | `worktree.patch` 存在且非空；`git status --porcelain` 为空；`git apply --check --reverse` 通过 |
| 隔离 | 闸门 A 失败 + 有**新增文件** | patch 包含该新增文件（`add -A` 生效，未漏） |
| 隔离 | `git apply --check --reverse` 失败（构造损坏 patch） | `cleanup=aborted`，工作区**原样保留**，不丢改动 |
| 隔离 | baseline 有未提交改动 | `cleanup=skipped`，工作区保留，`details.reason` 说明 |
| 隔离 | 清理后复核仍有残留 | `cleanup=partial`，残留路径进 details，任务仍 BLOCKED |
| 隔离 | `allowed_paths` 之外有改动 | 不被 `clean` 删除；记录残留 |
| 隔离 | ignored 目录（node_modules） | 不被删除（未加 `-x`） |
| 隔离 | reassign（正常换人） | 工作区**保留**，不触发隔离 |
| 隔离 | 重复触发隔离 | 幂等：已隔离则不再执行，不二次破坏 |
| 隔离 | CLI 命令面 | **不存在** `apply-worktree` / `--restore-patch` / `--override-model-check`（断言参数解析器里没有这些名字） |
| 隔离 | 隔离后 `reassign` | 新 run 的 `workspace_baseline.dirty_paths == []`，`worktree.patch` 属于旧 run 目录 |
| 隔离 | 同一任务连续两次 `MODEL_MISMATCH` | `hint` 指向配置检查，而不是"再换组合" |
| 配置 | `default_agent_profile ∉ allowed_profiles` | `INPUT_INCOMPLETE`（注册期） |
| 配置 | `capabilities` 类型/枚举非法 | `INPUT_INCOMPLETE` |
| 配置 | 声明 `provider` 而适配器无法落实 | `CAPABILITY_UNSUPPORTED` |
| 配置 | 两处 profile 白名单一致 | 注册与 `validate_profile` 同结果 |
| 配置 | 新增字段缺省时不写回 profile | 规范化前后 dict 无该键 |
| 组合标识 | 同一组合在不同机器 | `combination_id` 相同（不含绝对路径） |
| 组合标识 | 改 `model` | `combination_id` 变化 |
| 组合标识 | 改 `expected_version` | `combination_id` 变化 |
| 组合标识 | create（pin）与 start（实测同版本） | 两值相同；不同版本则 `combination_changed` |
| 组合标识 | `argv_audit_sha256` 变化 | `combination_id` **不变**（证明未混用） |
| L1 | 要求 image、声明 text | `excluded` + detail |
| L1 | `context_budget` 超标称窗口 | `excluded`；未超但接近时 detail 说明"可用预算未验证" |
| L1 | 要求 `structured_result`，声明 true 但 `output_protocol != json-final` | `excluded` |
| 端点 | `data_residency: preferred` + 无观测 | 运行，报告 `endpoint_observed: unknown` |
| 端点 | `data_residency: required` + 无法核实 | `ENDPOINT_UNVERIFIED`，未创建 task |
| 观测 | 单值相等 | `reported_match` + `scope` |
| 观测 | 多值不同 | `mixed` |
| 观测 | 无上报 | `unobserved` |
| 观测 | `pin_model=true` + `unobserved` | **`MODEL_MISMATCH`**（v1.1 漏项） |
| 观测 | `pin_model=true` + mismatch | 阻止发布、保留工作区、`BLOCKED` |
| 归因 | 成功运行 | `attempt_metrics: counted`（v1.1 会排除，回归用例） |
| 归因 | `RUN_TIMEOUT` | `infrastructure`，不进质量指标 |
| 归因 | `EVIDENCE_INVALID` | `infrastructure`，不进实现失败 |
| 归因 | `MODEL_MISMATCH` | 进可靠性，不进代码质量 |
| 归因 | `purpose=probe` | 一律不进质量指标 |
| 指标 | A 失败、B 完成 | 最终 accepted 只记 B；A 记 executor 失败 |
| 指标 | 任务待验收 | 计入 `tasks_pending_acceptance`，不进分子 |
| 指标 | 任务取消 | 计入 `tasks_cancelled`，不进分母 |
| 预算 | reassign 后 start | `assignments` +1、`attempts` +1；重放不加 |
| 预算 | `max_assignments` 用尽 | `BUDGET_EXHAUSTED` |
| 预算 | 幂等重放 create | 两个计数都不变 |
| 生命周期 | `route plan` | 无文件、无 DB 写、无子进程 |
| 生命周期 | `route stats` | 不写盘；未结束 run 不计入 |
| 生命周期 | `use_performance: true` | `CAPABILITY_UNSUPPORTED` |
| 迁移 | v1.0.0/v1.1 黄金配置 | 规范化后 digest 与基线一致 |
| 迁移 | 旧任务无 `combination_id` | 报告占位，不参与分桶 |
| 文档契约 | `not_supported` 增 `model_quality_scoring` / `internal_routing_control` / `performance_based_exclusion` | `test_doc_contracts.py` 断言 |

---

## 19 验收标准

1. 无 `routing` 段、无新字段的旧配置：`policy_digest` 与升级前一致；`task create` / `start` 响应字段与 v1.0.0 一致。
2. 同一组合在两台机器（不同绝对路径）上产生相同 `combination_id`；修改 `model` 或 `expected_version` 后必然不同。
3. `argv_audit_sha256` 因任务路径变化而变化时，`combination_id` 不变。
4. `pin_model=true` + `unobserved` → `MODEL_MISMATCH`(exit 5)；这是 v1.1 的漏项，必须有回归用例。
5. `pin_model=true` + mismatch 发生在运行后：`phase` 不前进到 publish，工作区保留，`runs/<id>/agent.log` 保留，任务 `BLOCKED`。
6. `data_residency: required` 且端点无法核实 → `ENDPOINT_UNVERIFIED`(exit 5)，**不创建任务、不发送任何任务内容**。
7. 成功运行计入 `runs_evaluable`；`attempt.pass_rate` 的分子分母关系可手工复算（给一份固定样本数据）。
8. 一个任务 A 失败、B 完成：`accepted` 只出现在 B 的组合桶；A 的桶 `attempt` 计一次失败、`delivery` 不计。
9. `route stats` 调用前后文件系统与 DB mtime 不变（不带 `--write`）。
10. `route plan` 不写盘（`--json` 输出可截获）；版本探测次数 == 候选数，`--no-probe` 时为 0。
11. `use_performance: true` 被拒绝，且 message 明确"v1.2 只记录"。
12. 降级到 v1.1.x：删除新字段后可注册；不删除则 `INPUT_INCOMPLETE`（证明降级是阻塞而非静默放行）。
13. 报告三行分离：`capability` / `combination_verified` / `performance` 各自独立，任一 `null`/`false` 不显示为"已验证"。
14. 显式指定的组合未满足 L1/L2 时，返回该组合的具体排除码，**不切换到其他组合**。
15. `prefer` 与可用集合无交集时进入 fallback：`fallback=ask` → `ROUTE_AMBIGUOUS`；`default_profile` 仅当默认在可用集合内才生效。
16. 同 `request_id` 重放：不触发任何环境探测（桩计数为 0），`combination_id` 不变。
17. 任务有 `route_id` 而决策文件缺失或 `content_sha256` 不符 → `start` 返回 `ROUTE_RECORD_INVALID`(exit 3)，不生成 run。
18. 策略收窄后五步（pause → rebind → BLOCKED/READY → reassign → start）全部可执行，且 3b 不清空工作区与历史证据。
19. `block_code=PROCESS_UNKNOWN` 时 `reassign` 被拒；`reconcile` 后按三态表判断。
20. 未声明 `model` 的 profile 注册失败（`INPUT_INCOMPLETE`）；开启 `allow_cli_default_model` 后可注册，但报告与 `route` 记录均显示"运行模型未解析"。
21. `cost_class: premium` 且未写 `pin_model` 时，默认按 `pin_model: true` 执行：`unobserved` → `MODEL_MISMATCH`。
22. `routing.cost_ceiling: standard` 时，`premium` 候选出现在 `candidates[]` 且 `code=COST_CEILING_EXCEEDED`；若全部被排除，最终码仍是 `ROUTE_NO_ELIGIBLE`。
23. 单任务累计 cost 超过 `max_cost_per_task` → `SPEND_LIMIT_EXCEEDED`，不进入 publish，`assignments` 不增（不自动换人）。
24. 已配 `max_cost_per_task` 而 `usage` 三项全 `None` → `SPEND_UNVERIFIABLE`，任务 `BLOCKED`；未配上限时不阻断，只记 `cost_source=unavailable`。
25. `pin_model=true` 且观测到 `mismatch` 时：`candidate_sha` 与 `validated_sha` 均为 `null`，`runs/<id>/` 下无 `evidence.json`，`phase` 未进入 `publish`；`agent.log` 仍在。
26. 闸门顺序固定：模型身份判定先于 verify；`candidate_sha` 只能在两道闸都通过后写入（用桩记录写入次序断言）。
27. `capabilities --json` 的 `not_supported` 含 `strict_model_enforcement_at_gateway`，且 `docs/capabilities.md` 中不出现"保证只用指定模型"一类表述（文档契约测试）。
28. 闸门 A 失败后：工作区 `git status --porcelain` 为空，`runs/<id>/worktree.patch` 存在且非空，`git apply --check --reverse` 通过 —— 即"改动可完整还原，但不留可提交内容"。
29. 隔离流程在**可还原性校验失败**时不执行任何丢弃动作（工作区原样），并记 `cleanup=aborted`。
30. 启动时工作区已脏（`baseline.dirty_paths` 非空）→ 不清理，`cleanup=skipped`，任务仍 `BLOCKED`。
31. `reassign` 正常换人不触发隔离：工作区保留，`worktree.patch` 不生成。
32. 隔离后只有两条可执行路径（`reassign` / `cancel`）；CLI 命令面不存在 `apply-worktree`、`--restore-patch`、`--override-model-check`（对参数解析器做否定断言）。
33. 隔离后 `reassign` 产生的新 run：`workspace_baseline.dirty_paths == []`，且旧 run 的 `candidate_sha` 仍为 `null`（历史不被洗白）。

---

## 20 实验设计（评审 d 的重写）

### 20.1 关于"难度"

`AC 数量 ＝ 难度` 不成立：三条 AC 的并发缺陷可能比十条 AC 的 CRUD 难得多。

因此：

- 改名为 **`spec_scale`（规格规模桶）**，只作辅助描述与分层键；
- 承认同桶内仍有难度方差，报告中展示桶样本量与方差；
- 不引入人工难度打分（引入主观分后，任何比较都失去可复核性）。

### 20.2 三种方式

| 方式 | 用途 |
|---|---|
| A 固定一个可靠组合 | 基准 |
| B 人工选择组合 | 当前人工经验 |
| C 规则选择（`prefer`） | 待验证方案 |

### 20.3 分配方式（修正"按周轮换"）

按周轮换无法避免时间差异，反而会混入模型版本、服务状态与项目阶段变化。

改为：

```text
同一时间窗口内，按 task_type 分组后交错分配（A/B/C 轮转），
使每个任务类别在各 arm 上的时间分布接近；
离线基准任务使用相同仓库基线与验收条件。
```

### 20.4 指标（必须包含"没完成"的）

| 指标 | 定义 | 采集 |
|---|---|---|
| 尝试通过率 | 指标 A（§10.3） | run 记录 |
| 任务交付率 | 指标 B | 任务记录 |
| **卡住/放弃任务** | `BLOCKED` 超时未处理 / `cancelled` | 任务记录（**不得只统计完成的任务**） |
| 总耗时 | `create` → `accept` 墙钟，含返工 | 事件时间戳 |
| 总成本 | 所有 run 的 `usage.cost`（含返工），缺失时记 tokens | run 记录 |
| 人工介入 | `answer` / `revise` / `reassign` / 手工修复 | 事件 |
| 错误派发 | 首次 `start` 失败且 `failure_class ∈ {environment, auth_permission}` 且可通过路由避免 | `failure_class` |
| 不必要切换 | 换人后仍同因失败 | `assignments` + `failure_class` |

### 20.5 判据（修正：必须有明确改善）

v1.1 稿的判据允许"质量略降、成本略增"，却没有任何指标必须改善 —— 那无法证明自动路由值得维护。重写：

```text
预先指定主要收益（二选一，实验前写定）：
  P1 = 人工选择与排障时间减少 ≥ 30%
  P2 = 错误派发次数减少 ≥ 50%（相对 B 组）

同时必须满足（不可接受的退化，任一违反即不通过）：
  通过率：      C ≥ B − 5 个百分点
  卡住/放弃率： C ≤ B + 2 个百分点
  总成本：      C ≤ A × 1.2 且 ≤ B × 1.1
  人工介入次数：C ≤ B × 1.1
```

即：**至少要有一项明确改善，且没有任何一项越过退化上限。**

### 20.6 样本量与结论强度

| 情况 | 结论 |
|---|---|
| 每 arm 每 `task_type` ≥ 10 个完成 + 全部卡住/放弃任务 | 可判断方向 |
| 差异 < 10 个百分点 | **数据不足**，不判定"更好"；保持人工选择 |
| 某 `spec_scale` 桶 < 5 | 合并到 `task_type` 层并标注 |
| 总量不足 | 只能结论"数据不足"，不得用"趋势看起来不错"代替 |

10 个样本适合初步试验，**不足以稳定判断几个百分点的差异**。这一点必须写进实验说明，避免把噪声当结论。

---

## 21 里程碑（评审范围缩减 + 风险重标）

### 21.1 v1.2 必做

| 里程碑 | 内容 | 风险标注（修正） |
|---|---|---|
| **M1 组合配置与映射** | profile 新字段 + 两处白名单 + schema + `endpoints` 段 + 适配器映射（含拒绝） | **中**：改变配置校验与**启动参数**；写错字段会改变实际执行的模型 |
| **M2 组合标识** | `combination_id` 构造、create/start 两处计算、`combination_changed` 事件 | 低：纯记录 |
| **M3 模型观测** | `AgentResult.reported_model` + 四值 + `scope` + `model_drift` + pin 阻断 | **中**：`unobserved` 会阻断 `pin_model` 任务；mismatch 会阻止发布 |
| **M4 过滤与冻结** | L1/L2 过滤 + 固定偏好 + `selected_execution` / `frozen_execution` + 恢复边界 + 预算时点 | **中高**：唯一改变"派发给谁"的里程碑 |

### 21.2 v1.2 可做（独立可裁剪）

| 里程碑 | 内容 | 风险 |
|---|---|---|
| M5 只读统计 | `PerformanceRecord v2` + `route stats` + 双指标 | 低：不影响决策 |
| M6 报告与文档 | 三行报告 + skill 文档 + `upgrading.md` 降级章节 | 低 |

### 21.3 推迟

| 项 | 版本 |
|---|---|
| `agent probe-model` | v1.3 |
| L3 自动排除（`use_performance` 生效） | v1.3（需先有 M5 数据 + 抽样强制分派） |
| 成本/速度优化 | v1.3（需先定义"组"语义） |
| 别名表 | v1.3 |

### 21.4 必须修正的说明

`use_performance=false` **只关闭 L3**，不关闭 M1/M3/M4 引入的过滤与阻断。因此：

- 不能把 M1–M4 统一标为"低风险、纯观测"；
- M1 会改变实际传给 CLI 的参数；
- M3 的 `unobserved` 会阻断使用 `pin_model` 的任务；
- M4 会改变派发结果。

---

## 22 回滚

| 触发 | 动作 | 影响 |
|---|---|---|
| 过滤误排除 | 从请求中删除该 `requirements` 项，或从 profile 删除对应声明 | 立即恢复；不改变已派发任务 |
| 模型观测误阻断 | 关闭该任务的 `pin_model`（需 `revise` 或新建任务） | 不影响其他任务 |
| 端点核实阻塞交付 | 把 `data_residency` 降为 `preferred`，或补齐端点配置 | 降档后回到"声明+观测" |
| 统计口径争议 | 只影响报告，不影响执行 | 重跑 `route stats`，历史记录不重算 |
| 完全回退到 v1.1 | 删除 profile 新字段 + `endpoints` + `routing.max_assignments` | 回到 v1.1 行为；**不得**靠删除约束恢复执行（§16.3） |
| 降级到 v1.0.0 | 先结束/暂停任务 → 清理新字段 → 重新 register | 不清理则 `INPUT_INCOMPLETE`（阻塞） |

`route-*.json`（v2）与 `stats-*.json` 是增量产物，旧版本不读，可保留。

---

## 23 风险与未决

| 编号 | 风险 | 影响 | 缓解 |
|---|---|---|---|
| E1 | `capabilities` 由人声明，可能与实况不符 | 错误纳入/排除 | 声明即"待验证"；L2 提供反证；报告分三行 |
| E2 | 工具不上报模型 | 大量 `unobserved` | 接受：宁可不用统计，也不用错归属 |
| E3 | `reported_match` 只覆盖报告值 | 被误读为"已确认" | 字段名 + `scope` + 固定报告文案 |
| E4 | **L3 只过滤会引入"被排除者无新样本"的回路** | 错误长期固化 | v1.2 不启用；v1.3 需抽样强制分派 + 定期复核 |
| E5 | `spec_scale` 不等于难度 | 分层后仍有方差 | 展示桶样本量与方差；不宣称消除了难度差异 |
| E6 | 成本数据缺失 | 成本维度失效 | `cost_source` 标注；不据此下结论 |
| E7 | 降级阻塞（`INPUT_INCOMPLETE`） | 紧急回退时卡住 | `upgrading.md` 给四步流程；建议先暂停而非立即降级 |
| E8 | `endpoint_class` 是人为标签 | 被当作技术保证 | `attestation: config-declared` 写死在配置里；`required` 档拒绝无法核实的派发 |

未决问题：

1. `max_assignments` 放 `routing` 段内（§16.2 的选择）是否需要独立于路由开关？即"不用路由但想限制换人次数"的项目怎么办 → 可能需要一个顶层 `limits.max_assignments`，但那会再动 digest。v1.2 先只支持 `routing` 内。
2. `endpoint_ref` 指向的端点是否需要与 provider 一致性校验（避免"声明 private 但 provider 是云端服务"）→ 倾向需要，M1 定。
3. `spec_scale` 是否加入 `len(allowed_paths)` 作为次级键 → 倾向加，但需先确认不会让桶过细。
4. `reported_models` 的 `mixed` 是否需要记录每个模型的调用次数 → 若工具不提供，只能记"出现过"，如实标注。

---

## 24 附录

### 24.1 与评审意见的对应

| 评审意见 | 落点 |
|---|---|
| 增加 `combination_id`，覆盖 CLI 版本/模型/推理/权限/端点 | §7.1 |
| `argv_sha256` 不能代替组合标识 | §7.2、§12.2 |
| `confirmed` 语义改为"报告匹配" + 记录覆盖范围 | §9.1 |
| `pin_model=true` + `unknown` 必须阻断 | §9.3 |
| mismatch 只能运行后发现，不能称"启动前防护" | §9.4 |
| 端点不能靠事后对比，分两档 | §9.5 |
| L3 分母错误，成功运行必须计入 | §10.3（重写） |
| run 与 task 口径拆开，禁止回填 | §10.3 |
| `EVIDENCE_INVALID`/超时/mismatch 归因收紧 | §10.2 |
| probe 必须事前标记 | §10.5 |
| `route plan` 不能调模型 | §5.1、§15.1 |
| `route stats` 缓存由谁生成 | §15.3 |
| create/start 两阶段冻结 | §12.1、§12.2 |
| 配置变化不得静默换组合 | §12.5 |
| 迁移不能"删字段即可"，需验证 digest 与降级 | §16 |
| 偏好排序使成本/速度开关失效 | §11.3 |
| 能力字段区分标称/档位/端到端 | §6.2 |
| provider 不能靠 `provider_flag is None` 判断 | §6.3 |
| 切换预算计数时点 | §13.3 |
| 历史证据保留，只清当前引用 | §13.4 |
| `spec_scale` 代替难度 | §14.3、§20.1 |
| 交错分配代替按周轮换 | §20.3 |
| 必须统计卡住/放弃任务 | §20.4 |
| 10 样本不足以判断几个百分点 | §20.6 |
| 判据必须要求明确改善 | §20.5 |
| v1.2 范围缩减 | §2.1–2.3、§21 |
| `use_performance=false` 只关 L3 | §21.4 |
| "L3 只过滤"不等于消除偏差 | §8.4 |
| 费用是否会因"用了范围外模型"而飙升 | §6.1.1（`model` 必填）、§9.3.1（premium 默认 pin）、§11.1.1（`cost_ceiling`）、§12.7（`spend_guard` 与三种成本口径） |
| 是否"仅仅使用用户指定的模型" | §12.8（闸门顺序：用错即不产生候选）、§16.7（`strict_model_enforcement_at_gateway`：事前强制不在调度器能力内） |
| 闸门失败后改动是否还会被提交 | §12.9（工作区隔离：patch 归档 + 可还原性校验后恢复干净） |
| 隔离后如何继续（是否留绕过口子） | §12.9.6（只有 `reassign` / `cancel`；明确不提供恢复与 override 通道） |

### 24.2 最小配置示例

```json
{
  "endpoints": {
    "cloud-default": {"class": "cloud-approved", "attestation": "config-declared"}
  },
  "routing": {
    "schema_version": 1,
    "mode": "auto",
    "allowed_profiles": ["deep-debugging", "fast-maintenance"],
    "rules": [{"task_type": "bugfix", "prefer": ["deep-debugging", "fast-maintenance"]}],
    "fallback": "ask",
    "max_assignments": 2,
    "cost_ceiling": "standard",
    "spend_guard": {
      "max_cost_per_task": 5.0,
      "max_cost_per_day": 30.0,
      "currency": "USD",
      "on_exceed": "block"
    }
  },
  "agent_profiles": {
    "deep-debugging": {
      "adapter": "claude-code",
      "backend": "anthropic-api",
      "expected_version": "2.111.0",
      "permission_profile": "isolated-edit-and-run",
      "model": "claude-sonnet-4-5",
      "reasoning_effort": "high",
      "endpoint_ref": "cloud-default",
      "capabilities": {
        "input_modalities": ["text"],
        "context_window": 200000,
        "tool_use": true,
        "structured_result": true,
        "max_output_tokens": 8192
      },
      "cost_class": "premium",
      "latency_class": "slow",
      "unit_price": {"input_per_1k": 0.003, "output_per_1k": 0.015, "currency": "USD"}
    },
    "fast-maintenance": {
      "adapter": "opencode",
      "backend": "opencode-cli",
      "expected_version": "REPLACE_WITH_EXACT_CLI_VERSION",
      "permission_profile": "isolated-edit",
      "provider": "anthropic",
      "model": "claude-haiku-4",
      "endpoint_ref": "cloud-default",
      "capabilities": {
        "input_modalities": ["text"],
        "context_window": 200000,
        "tool_use": true,
        "structured_result": true,
        "max_output_tokens": 8192
      },
      "cost_class": "low",
      "latency_class": "fast"
    }
  }
}
```

`cost_class` / `latency_class` 在 v1.2 只作记录；模型与档位**不代表能力排名**。

### 24.3 字段总表（新增/变更）

| 字段 | 位置 | 类型 | 默认 | v1.2 作用 |
|---|---|---|---|---|
| `provider` | profile | string | 不写回 | 映射到启动配置 |
| **`model`** | profile | string | **必填** | 缺失即 `INPUT_INCOMPLETE`（§6.1.1） |
| `allow_cli_default_model` | profile | bool | `false` | 允许跟随 CLI 默认模型（转义口，代价是 `combination_verified=false`） |
| `model_variant` | profile | string | 不写回 | 映射到启动配置 |
| `reasoning_effort` | profile | enum | 不写回 | 映射到启动配置 |
| `endpoint_ref` | profile | string | 不写回 | 引用 `endpoints` |
| `capabilities.*` | profile | mixed | 不写回 | L1 过滤 |
| `cost_class` / `latency_class` | profile | enum | 不写回 | `cost_class` 参与上限过滤，`latency_class` 仅记录 |
| `unit_price` | profile | object | 不写回 | 仅在 `cost` 缺失时用于估算 |
| `endpoints` | 项目 | object | 不写回 | 端点标识 |
| `routing.max_assignments` | 项目 | int | 2（段内写回） | 换人预算 |
| `routing.cost_ceiling` | 项目 | enum | `premium`（段内写回） | 成本档上限（排除） |
| `routing.spend_guard.*` | 项目 | mixed | 全 `null`（段内写回） | 金额护栏 |
| `model_unresolved` | run / decision | bool | `false` | 模型未解析（否决 `combination_verified`） |
| `spend` | `PerformanceRecord` | object | — | 三分口径成本汇总 + 越线次数 |
| `combination_id` | run / decision | string | — | 分桶与归因 |
| `requested_model` / `reported_model` / `observation_scope` / `model_identity` | run | mixed | null / `none` / `unobserved` | 观测 |
| `endpoint` | run | object | null | 端点观测 |
| `purpose` | run | enum | `task` | probe 隔离 |
| `failure_class` | run | enum | null | 归因 |
| `attribution` | run | object | — | 归因留痕 |
| `argv_audit_sha256` | run | string | null | 仅审计 |
| `workspace_baseline` | run | object | — | 启动前工作区基线（§12.9.1），隔离判定依据 |
| `worktree.patch` | 任务目录文件 | binary | — | 闸门失败时的改动归档，可 `git apply` 还原 |
| `cleanup` | run / 事件 | enum | — | `done`/`partial`/`skipped`/`aborted` |
| `counts.*` / `buckets[]` / `not_comparable[]` | `PerformanceRecord v2` | mixed | — | 只读统计 |

---

## 25 一句话结论

**v1.2 的目标不是"选得更好"，而是"选得可解释、传得对、记得准、换得安全、花得可控"。**

L3 自动排除推迟到有可信统计之后；在此之前，宁可让人按 `prefer` 决定，也不要让一个口径不明、分母错误的统计去替人做决定。

费用方面同样要给出一条准确的话：**这套机制能让"跑错模型"变成可见、可阻断、有上限的事件，但不能保证费用一定不超支。** 它没有预扣额度，也不掌握服务端计价；它只能保证三件事：

1. 不声明模型就无法通过注册（消除最大的一处授权空白）；
2. 贵的组合默认要求确认，看不到模型身份就阻断；
3. 越线即停、不自动重试、不自动换人 —— 超额只发生一次，不会复利。

"是否只用指定模型"要给一条同样准确的话：

> **Zoe 保证"请求的是它"，并保证"不是就作废"；不保证"实际只有它"。**
> 事前强制只能在网关白名单或单模型私有端点上实现（`not_supported: strict_model_enforcement_at_gateway`）。
> 调度器能做的是：让用错模型的那一次**不留下可交付的产物**——候选标记不写（§12.8），改动归档为 `worktree.patch` 并从工作区撤出（§12.9）。
