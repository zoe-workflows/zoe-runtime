# 受约束的跨 Agent 路由（Agent Routing）详细设计

基线：`zoe-runtime` v1.0.0（CLI 面 / 状态库 v3 / skill 契约 `api_version` 2 · `protocol_revision` 1 已冻结）
目标版本：v1.1.0（修订稿 R2）
配套 skill：`zoe-workflows`

---

## 0 文档定位与证据等级

### 0.0 版本关系

本文只定义 **profile 级受约束路由**：`routing` 段、过滤与偏好、`RouteDecision`、决策记录、错误码。

后续文档 `docs/execution-combination-routing-design.md`（v1.2）在本文之上增加执行组合标识、模型观测、三层事实、端点两档与失败归因。两版的边界：

| 本文（v1.1） | v1.2 |
|---|---|
| 选哪个 **profile** | 这个 profile 内部跑的是哪个**执行组合**（模型/端点/推理档） |
| 硬条件 = 权限、结构化结果、证据强度 | 硬条件 += `capabilities` 声明与端点要求 |
| `RouteDecision v1` | `RouteDecision v2`（增 `selected_execution`/`frozen_execution`） |

本文 R2 修正的六项（§8.3、§8.4、§5.2、§10.5、§15.3、§11），对 v1.2 同样适用，v1.2 已同步修订。

### 0.1 证据等级

| 标记 | 含义 |
|---|---|
| 【核】 | 已在代码中逐条核对，给出文件与行号 |
| 【外】 | 外部事实或产品判断 |
| 【新】 | 本文新增的契约、字段、错误码 |

本文不声称任何能力已实现。凡本文出现的 JSON、命令、错误码，除标【核】者外均为设计提案。

### 0.2 R2 修订摘要

本轮评审指出 6 个契约漏洞 + 2 项优化，全部采纳：

| # | 问题 | 状态 | 落点 |
|---|---|---|---|
| ① | 显式指定与默认 profile 绕过硬条件 | 已修正 | §8.3（统一过滤顺序）、§6.2（默认 profile 受范围约束） |
| ① b | 能力映射不严谨（权限≠非交互、network 缺失≠断网、三态被当质量） | 已修正 | §8.1（判定表重写 + "不作为硬条件"表） |
| ② | 规则命中但偏好全部不可用时，偷偷按批准列表选人 | 已修正 | §8.4（交集语义 + fallback） |
| ③ | "纯函数"却调用 `doctor`；`route_id` 被当完整性证明 | 已修正 | §5.2（快照/决策分离）、§9.3（用途边界） |
| ④ | "失败不落任何记录"与幂等登记冲突 | 已修正 | §10.5（区分操作记录与派发记录）、§9.2（崩溃一致性） |
| ⑤ | 策略收窄与换人形成操作死锁 | 已修正 | §15.3（rebind 不再拒绝，改为阻断任务） |
| ⑥ | `start` 失败被当作"旧执行一定不存在" | 已修正 | §11（三态判定表） |
| 优化 1 | 错误详情泄露本地路径到报告/PR | 已采纳 | §9.1（`detail` 脱敏，原文只进 `last_error`） |
| 优化 2 | 每个候选重复跑完整 doctor | 已采纳 | §8.2（项目级一次 + profile 级分开 + 总预算） |

落地顺序改为四步（§19），首版只交付 `route plan`（只读）。

---

## 1 决策摘要

| 决策 | 内容 |
|---|---|
| 实现的是 | **按任务要求与已配置偏好选择执行器** |
| **不**实现的是 | "自动证明哪个模型最擅长当前任务"（需要组合标识、模型观测与可比较历史，见 v1.2） |
| 匹配单元 | `profile_id`（adapter + backend + model + permission + credential 的组合） |
| 算法 | 确定性过滤 + 偏好交集，零评分、零模型裁决 |
| 默认值 | `routing.mode` 缺省 `explicit`：不写配置不改变现有派发行为 |
| 显式指定 | **不免检**：与自动路径走同一套硬条件与环境检查，只是不参与排序 |
| 无偏好命中 | 进入 `fallback`，**不得**按 `allowed_profiles` 顺序顶替 |
| 纯函数边界 | 环境探测产生**快照**，决策只读快照（可单测、可重放） |
| 状态 | 不新增表、不新增列、不升 `record_version` |
| 新错误码 | 3 个：`ROUTE_NO_ELIGIBLE`(4)、`ROUTE_AMBIGUOUS`(2)、`ROUTE_RECORD_INVALID`(3) |
| 不做 | 模型质量评分、DAG、消息队列、失败自动换人、group 批量路由 |

---

## 2 范围与非目标

### 2.1 范围内

1. 项目配置 `routing` 段：批准范围、任务类型偏好、无匹配时的行为。
2. 一个**两段式**路由器：环境快照（有副作用）+ 纯决策（可重放）。
3. 接入点：`task create`（首次派发）与 `task reassign`（换人，含 `target_profile: "auto"`）。
4. 只读预演 `zoe route plan`（首版唯一交付物，见 §19）。
5. 决策记录的数据契约、事件、报告增量、错误码。
6. 派发前二次校验的三态语义（未启动 / 已退出 / 状态未知）。

### 2.2 非目标

| 非目标 | 理由 |
|---|---|
| 模型质量评分 / 历史胜率加权 | 需要组合标识与可比较样本，属 v1.2+ |
| 由宿主大模型"凭印象"挑执行器 | 选择必须可复核、可重放 |
| DAG 调度 / 消息队列 / 常驻调度器 | v1.0.0 执行模型是"一次命令一个短命进程"【核】`docs/architecture.md` |
| 失败后自动换执行器 | 引入重试放大与证据污染 |
| `group dispatch` 批量路由 | 组内条目任务类型可能不同 |
| 跨项目复用 profile | `allowed_profiles ⊆ agent_profiles`，结构上无法跨界 |
| 自动写 `orchestration.pending_questions` | 询问通道由宿主实现（§14） |
| 会话跨工具迁移 | 不把 A 的 session id 交给 B |
| 宣称断网隔离 | `local-process` 不限制出口（§8.1） |

---

## 3 术语与模型

### 3.1 三个易混对象

| 对象 | 定义 | 落点 |
|---|---|---|
| **批准范围** | 用户允许本项目使用哪些执行组合 | `agent_profiles`【核】`src/_profiles.py:34` |
| **执行组合（profile）** | 具名的 `adapter + backend + expected_version + model + permission_profile + credential_refs` | `ProfileBinding`【核】`src/_profiles.py:62` |
| **路由决策** | 针对某个任务要求，在范围内选一个 profile 并说明为什么 | 本文 `RouteDecision`【新】 |

### 3.2 为什么匹配单元是 profile

`claude-code` 这个名字不构成执行能力。决定"能不能接这单"的是：

- `backend` 是否被支持（`ADAPTER_BACKEND_MISMATCH`）【核】`src/_profiles.py:354`
- `permission_profile` 是否落到 argv（`PERMISSION_PROFILE_UNBOUND`）【核】`src/_profiles.py:445`
- `expected_version` 是否有精确版本记录（`ADAPTER_VERSION_MISMATCH`）【核】`src/_profiles.py:416-430`
- `credential_refs` 在本机是否存在（`doctor.credential_present`）【核】`src/_config.py:268`

同一 adapter 的两种 profile（不同 model 或不同 permission）完全可能一个可用、一个不可用。所以路由对象是 `profile_id`。

### 3.3 决策链

```text
宿主提出任务
  └─ 项目配置声明批准范围 + 偏好规则        ← 用户授权，静态
       └─ 环境快照（有副作用）              ← 新增：与决策分离
            └─ 决策（纯函数）               ← 可单测、可重放
                 └─ create 冻结 profile     ← 现状【核】src/_pipeline.py:204
                      └─ start 二次校验     ← 现状【核】src/_pipeline.py:721
                           └─ 独立验证 → 发布 → 人工验收
```

---

## 4 现状核对

### 4.1 "选择"只发生在一个地方，且没有候选比较

| 事实 | 证据 |
|---|---|
| 创建时确定执行器：`profile_id = req.get("agent_profile") or c["default_agent_profile"]` | 【核】`src/_pipeline.py:148` |
| 选中结果冻结进任务（`agent_profile_id`/`adapter_id`/`adapter_backend`/`assignment_revision`） | 【核】`src/_pipeline.py:204-207` |
| `start` 只按冻结值重新解析与校验，没有选择余地 | 【核】`src/_pipeline.py:32-43`、`:721` |
| 换执行器只有 `reassign`（stop-confirmed）与 `rebind`（采用新策略） | 【核】`src/_pipeline.py:1174`、`:1318` |

### 4.2 可复用的积木

| 积木 | 复用方式 | 位置 |
|---|---|---|
| `resolve_binding(c, pid)` | 候选解析，抛结构化错误、无副作用 | 【核】`src/_profiles.py:334` |
| `require_runnable(binding, require_real_e2e=...)` | 能力态闸门 + 权限绑定闸门 | 【核】`src/_profiles.py:408` |
| `binding.unverified` / `trust_level` | 可信度标注（不是闸门） | 【核】`src/_profiles.py:85-99` |
| `doctor(c, pid)` | 环境就绪、CLI 解析、`credential_present` | 【核】`src/_config.py:175` |
| `doctor(...)["profiles"]` | 一次调用即包含全部 profile 能力态视图 | 【核】`src/_config.py:274` |
| `probe_profile(...)` | 有界探测（只跑 `--version`，不改能力态） | 【核】`src/_profiles.py:509` |
| `atomic()` | 决策记录落盘 | 【核】`src/_pipeline.py:170` |
| `store.operation()` | 幂等重放（`request_id`） | 【核】`src/_pipeline.py:136-138` |
| `assignments[]` + `assignments/reassign-NN.json` | 换人记录格式 | 【核】`src/_pipeline.py:1230-1256` |
| `policy_digest(c)` | "策略是否变过"的判据 | 【核】`src/_profiles.py:124` |
| `Resolution.resolved` / `.version` | CLI 是否真的解析到 | 【核】`src/_discovery.py:156-166` |

### 4.3 缺失

1. 没有候选集概念：`agent_profiles` 只是"可被引用的字典"。
2. 没有任务类型：`create` 请求与 spec 契约都无 `task_type`【核】`src/_contracts.py:116-200`。
3. 没有排除原因：解析失败即抛错，调用方只看到一个 `code`。
4. 没有决策记录：`run` 记录有 `profile_id`/`capability`，但**没有"为什么是它"**【核】`src/_pipeline.py:749-786`。
5. 没有"无匹配"这一档。

### 4.4 两个容易被误读的现状事实（R2 新增）

这两条直接影响硬条件的写法，必须写清：

**(a) `create` 不调用 `require_runnable`。**

```text
create:  resolve_binding(...)                 → 只做"能否解析"（adapter/backend/版本/权限绑定）
start:   require_runnable(...)                → 才做能力态闸门【核】src/_pipeline.py:721
```

后果：显式指定一个 `UNVERIFIED` profile 时，`create` 会成功，直到 `start` 才被拒（除非项目 `require_real_e2e=false`，那是"放行并标注"策略）。因此**"显式指定"在现状下确实缺少准入检查**，这正是 §8.3 要统一的地方。

**(b) `local-process` 不限制网络出口。**

`doctor` 的输出明确写着 `network: host (local-process: not restricted by Zoe)`。项目配置的 `network: "none"` 是一个**声明**，不是强制隔离。因此：

- 不能用 `network_profile == "none"` 证明"该组合断网"；
- 不能在硬条件里把"网络"写成已落实的隔离能力（§8.1 已删该项）。

---

## 5 设计总览

### 5.1 五步流程（显式路径同样走完五步）

```text
① 范围   allowed_profiles ∩ agent_profiles（显式请求：把候选收窄为 1 个，但不免检）
   ↓
② 硬条件  任务要求 → 可从 binding 机械判定的项
   ↓
③ 环境   doctor 项目级一次 + profile 级分别（有总预算）
   ↓
④ 偏好   rules[task_type].prefer 与可用集合的**交集**
   ↓
⑤ 决策   唯一候选 / 交集首项 / fallback
```

第 ②③ 步只做**排除**，第 ④ 步只做**排序**。任何一步都不能被请求提升：请求只能收窄范围，不能扩大。

**显式指定不免检**（R2 修正）是这条流水线最重要的一句话：用户说"就用 A"，只表示"不要替我排序"，不表示"跳过对 A 的检查"。

### 5.2 快照与决策分离（R2 修正）

原稿把 `plan()` 描述为纯函数，伪代码却直接调用 `doctor()` —— 环境一变结果就变，"同输入同输出"不成立。

改为两段：

```python
snapshot = collect_environment(config, candidates)   # 有副作用：doctor / resolution
decision = decide(config, requirements, snapshot, requested_profile)   # 纯函数
```

| 段 | 性质 | 可测试性 |
|---|---|---|
| `collect_environment` | 有副作用（子进程、文件系统、平台探测） | 用真实/替身环境测试 |
| `decide` | 纯函数（只读 dict） | 构造 snapshot 即可表驱动测试 |

决策记录同时保存 `snapshot_digest`（快照内容的摘要），使"当时为什么这么判"可复核。**不承诺**同一请求在任何时刻重算都得同一结果 —— 只承诺"同一快照 + 同一配置 → 同一决策"。

### 5.3 与现有流水线的插入点

```text
task create
  ├─ store.operation(...)              幂等检查（先于路由）【核】
  ├─ authorize(c, req, "create")       【核】
  ├─ load_spec(...)                    【核】
  ├─ ★ snapshot = collect_environment(...)    ← 有副作用
  ├─ ★ decision = routing.decide(...)         ← 纯函数
  ├─ ★ 写 route-<rev>.json                     ← 原子写
  ├─ resolve_binding(c, selected)      【核】
  ├─ 冻结 agent_profile_id/...          【核】
  └─ store.save(..., "route_decided")  【核】
```

顺序约束（崩溃一致性，§9.2）：决策文件先落盘，任务记录在同一事务里带 `route_id` 与 `snapshot_digest`。任何一步失败都不留下"任务存在但决策缺失"的半成品。

### 5.4 不变量

| 编号 | 不变量 | 强制手段 |
|---|---|---|
| R1 | 选择不得越过批准范围 | `allowed_profiles ⊆ agent_profiles`；`default_agent_profile ∈ allowed_profiles` |
| R2 | 请求只能收窄，不能扩大 | 显式越界 → `POLICY_DENIED` |
| R3 | **显式指定不免检** | 显式与自动走同一 `evaluate()`（§8.3） |
| R4 | **批准范围不等于优先顺序** | 偏好取交集，无交集走 fallback（§8.4） |
| R5 | 无匹配如实报告，不降级 | `ROUTE_NO_ELIGIBLE` + 每候选一条缺口 |
| R6 | 决策可重放 | 快照/决策分离 + `snapshot_digest` |
| R7 | 选择通过 ≠ 启动成功 | `start` 的 `require_runnable` + `doctor` 二次校验 |
| R8 | 换人必须 stop-confirmed（含状态未知的情形） | §11 三态表 |
| R9 | 决策缺失或不一致时禁止启动 | `ROUTE_RECORD_INVALID`（§9.2） |

---

## 6 配置契约

### 6.1 `routing` 段

```json
{
  "routing": {
    "schema_version": 1,
    "mode": "auto",
    "allowed_profiles": ["claude-local", "opencode-local", "codebuddy-local"],
    "rules": [
      {"task_type": "bugfix", "prefer": ["claude-local", "opencode-local"]},
      {"task_type": "feature", "prefer": ["opencode-local", "codebuddy-local"]},
      {"task_type": "*", "prefer": ["codebuddy-local"]}
    ],
    "fallback": "ask"
  }
}
```

| 字段 | 类型 | 缺省 | 语义 |
|---|---|---|---|
| `schema_version` | int | 1 | 本段自版本，与项目配置 `schema_version` 独立 |
| `mode` | `explicit`\|`auto` | `explicit` | `explicit` 只允许显式或默认 profile |
| `allowed_profiles` | string[] | 全部 `agent_profiles` 键（排序后） | 允许被选择的子集，1..8 项 |
| `rules` | object[] | `[]` | 按 `task_type` 的偏好；同类型只能一条 |
| `rules[].task_type` | enum + `*` | — | 见 §7.3 |
| `rules[].prefer` | string[] | — | 非空、去重、⊆ `allowed_profiles`；顺序即优先级 |
| `fallback` | `ask`\|`default_profile` | `ask` | 偏好无交集时的行为 |

### 6.2 规范化与校验（R2 增补两条）

写入 `src/_profiles.py:normalize_config()` 末尾（与 `require_real_e2e`、`runtime_binding` 同处）【核】`src/_profiles.py:284-288`。

1. 顶层**没有** `routing` 键 → 规范化后也**不产生**该键（零迁移，§15.1）。
2. 有 `routing` → 未知字段、类型错误、越界一律 `INPUT_INCOMPLETE`（与 profile 字段同等纪律）。
3. `allowed_profiles` 中每个 id 必须存在于 `agent_profiles`，否则 `INPUT_INCOMPLETE`。
4. `rules[].prefer` 出现 `allowed_profiles` 之外的 id → `POLICY_DENIED`。
5. `mode == "auto"` 且 `allowed_profiles` 为空 → `INPUT_INCOMPLETE`。
6. `rules[].task_type` 重复 → `INPUT_INCOMPLETE`。
7. **【R2 新增】`default_agent_profile` 必须在 `allowed_profiles` 内**，否则 `INPUT_INCOMPLETE`。
   理由：否则 `mode=explicit` 的默认派发会派给一个"未被批准用于路由"的 profile，批准范围形同虚设。若确实想保留一个只在人工指定时使用的 profile，应把它从 `allowed_profiles` 移除**并**改 `default_agent_profile` 指向范围内的键。
8. **【R2 新增】`fallback == "default_profile"` 时同样要求第 7 条成立**（否则回退目标可能越界）。

### 6.3 `policy_digest` 影响与零迁移

| 场景 | 后果 |
|---|---|
| 项目配置**不含** `routing` | 规范化不新增键 → `policy_digest` 与 v1.0.0 逐字节一致 → 在途任务不受影响 |
| 项目配置**新增** `routing` | digest 改变 → 在途任务 `start` 返回 `POLICY_CHANGED`，需 `task rebind` |

运维顺序（进 `docs/upgrading.md`）：

```text
1. 升级运行时
2. 确认无在途任务（或先 finish / pause）
3. 加入 routing 段并重新 register
4. 对残留的在途任务执行 task rebind（见 §15.3）
5. 用 route plan 只读预演，确认决策符合预期后再切 mode=auto
```

### 6.4 schema 变更

`schemas/project-profile.v2.json` 增加可选 `routing` 对象（顶层未声明 `additionalProperties: false`，不升 `schema_version`）。`routing` 及其 `rules[]` 元素声明 `additionalProperties: false`。

新增 `schemas/route-decision.v1.json`，登记进 `tests/test_contract_artifacts.py`。

---

## 7 任务侧输入契约

### 7.1 `task create` 请求新增可选 `route`

```json
{
  "schema_version": 2,
  "request_id": "req-...",
  "project_id": "example",
  "spec_file": "/abs/spec.json",
  "title": "修复登录超时",
  "delivery_mode": "pull_request",
  "agent_profile": null,
  "route": {
    "task_type": "bugfix",
    "requirements": {
      "non_interactive": true,
      "structured_result": true,
      "session_resume": false,
      "min_capability": "UNVERIFIED"
    }
  }
}
```

| 字段 | 类型 | 语义 |
|---|---|---|
| `task_type` | enum | 见 §7.3 |
| `requirements.non_interactive` | bool | 需要真实权限 argv（**必要条件，非充分条件**，§8.1） |
| `requirements.structured_result` | bool | 需要 `output_protocol == "json-final"`【核】`src/_agents/base.py:478` |
| `requirements.structured_input` | bool | 需要 `supports_structured_input`【核】`src/_agents/base.py:484` |
| `requirements.session_resume` | bool | 当前恒不满足 |
| `requirements.min_capability` | 三态 | **最低证据强度**，不是质量下限（§8.1） |
| `requirements.permission_profile_in` | string[] | `profile.permission_profile` 必须命中 |

`requirements` 的未知键 → `INPUT_INCOMPLETE`。

### 7.2 `task_type` 来源优先级

```text
route.task_type  >  spec.document.task_type  >  "unknown"
```

spec 顶层新增可选 `task_type`（`src/_contracts.py:validate_spec_text` 校验枚举，缺省不校验）。`task_type_source` 必须记录（`request`/`spec`/`default`）。

### 7.3 `task_type` 词表

```text
bugfix | feature | refactor | test | docs | chore | review | unknown | *
```

`*` 只能出现在规则里。

### 7.4 判定矩阵

| 请求 `agent_profile` | 请求 `route` | 配置 `routing.mode` | 行为 |
|---|---|---|---|
| 给定 | 任意 | 任意 | **显式**：收窄候选为 1 个，**仍走硬条件与环境检查** |
| 未给 | 给定 | `auto` | **自动**：走路由器 |
| 未给 | 给定 | `explicit` | `INPUT_INCOMPLETE`（声明了路由需求但未启用，不静默忽略） |
| 未给 | 未给 | `auto` | **自动**：`task_type="unknown"`，走 `*` 规则或 fallback |
| 未给 | 未给 | `explicit` | **默认**：`default_agent_profile`（受 §6.2 第 7 条约束） |
| 未给 | 未给 | 无 `routing` 段 | **默认**：同上（v1.0.0 行为） |

`agent_profile: "auto"` 这个字面量不接受：它与真实 profile id 无法区分。

---

## 8 匹配算法

### 8.1 硬条件判定表（R2 重写）

| 要求 | 判定表达式 | 不满足时的排除码 | 强度 |
|---|---|---|---|
| `non_interactive` | `binding.permission.get("enforced") is True` | `PERMISSION_PROFILE_UNBOUND` | **必要非充分**：只证明权限 argv 已绑定，不证明该 CLI 在该模式下不会弹审批 |
| `structured_result` | `binding.adapter.output_protocol == "json-final"` | `CAPABILITY_UNSUPPORTED` | 充分（由适配器契约保证） |
| `structured_input` | `binding.adapter.supports_structured_input` | `CAPABILITY_UNSUPPORTED` | 充分 |
| `session_resume` | `False` | `CAPABILITY_UNSUPPORTED` | 恒不满足（无原生恢复） |
| `min_capability` | 三态序 `IMPLEMENTED < UNVERIFIED < VERIFIED` | `ADAPTER_UNVERIFIED` / `ADAPTER_VERSION_MISMATCH` | **只是证据强度下限**（§8.1.1） |
| `permission_profile_in` | `profile.permission_profile in 列表` | `PERMISSION_PROFILE_UNBOUND` | 充分 |
| 项目 `require_real_e2e == true` | `binding.capability == "VERIFIED"` | `ADAPTER_UNVERIFIED` | 充分（项目策略） |

#### 8.1.1 三态是证据强度，不是质量等级

`IMPLEMENTED / UNVERIFIED / VERIFIED` 的含义是"该 `(adapter, backend)` 有没有真实 e2e 记录"【核】`src/_profiles.py:408-430`。它**不表示**"哪个更好"：

- `VERIFIED` 只说明"这条路被走过一次并留下了记录"；
- **禁止**用它排序（例如"优先选 VERIFIED"）；
- `min_capability` 的语义是"我要求的最低证据强度"，不是"我要求的最低质量"。

若未来要做质量比较，那是 v1.2 的 L3（任务表现），与三态无关。

#### 8.1.2 明确**不作为**硬条件的项

| 项 | 为什么不能用 |
|---|---|
| `network_profile` / 项目 `network: "none"` | `local-process` 不限制出口，`doctor` 明确输出"not restricted by Zoe"【核】`src/_config.py:281`。配置值是**声明**，不是隔离能力。只能记录，不能当作"该组合断网"的证据 |
| 成本档 / 价格 | v1.1 的 profile 没有 model 与 cost 维度，因此不在 v1.1 判定。成本上限（`cost_ceiling`，排除语义）与金额护栏（`spend_guard`）属执行组合属性，见 v1.2 §11.1.1 与 §12.7。**不要**在这里用"价格低"做任何偏好判断 |
| "模型擅长某类任务" | 无可迁移的度量（v1.2 §8.4） |
| `credential_present` | 只表示环境变量存在，不表示有效【核】`src/_config.py:268`。作为门槛会把"密钥过期"误报成"没有执行器" |

### 8.2 环境条件与成本（R2 增补预算）

| 档 | 内容 | 默认 | 执行方式 |
|---|---|---|---|
| P 项目级 | `git`、`platform_lock`、`platform_tree`、`disk` | 开 | **一次**（不随候选数增长） |
| E1 profile 级就绪 | `doctor(c, pid)["ready"]` | 开 | 每候选一次，但复用同一份 `profiles` 视图【核】`src/_config.py:274` |
| E1b CLI 解析 | `resolution.path` 非空【核】`src/_discovery.py:156` | 开 | 复用 E1 的结果 |
| E2 有界探测 | `probe_profile`（spawn `--version`） | **关** | 默认不执行 |
| — 凭据存在 | `credential_present` | 记录，不作门槛 | 复用 E1 |

三条纪律（R2）：

1. 候选上限 8，使最坏情况有界。
2. **项目级检查只跑一次**，`decide()` 读快照，不重复调用 `doctor`。
3. **总时间预算**（默认 5s）：逐候选跑 E1 时若累计超预算，剩余候选标 `ENVIRONMENT_ERROR` + `detail: "probe budget exhausted"`，而不是无限等待。超预算的候选**不进** eligible（宁可少选，不可卡死）。

### 8.3 统一过滤顺序（R2 修正 ①）

原稿的伪代码在 `requested_profile` 与"默认 profile"两个分支直接 `return`，绕过了硬条件与环境检查。结合 §4.4(a)（`create` 本就不调用 `require_runnable`），这会让"显式指定"成为准入后门。

修正：**三条路径共用同一个 `evaluate()`**。

```python
def decide(config, requirements, snapshot, requested_profile=None):
    routing = config.get("routing")

    # ① 范围（显式路径只是收窄候选，不免检）
    if requested_profile:
        require(requested_profile in config["agent_profiles"], "INPUT_INCOMPLETE", ...)
        scope = routing["allowed_profiles"] if routing else list(config["agent_profiles"])
        require(requested_profile in scope, "POLICY_DENIED", ...)
        candidates = [requested_profile]
    elif routing and routing["mode"] == "auto":
        candidates = routing["allowed_profiles"]
    else:
        candidates = [config["default_agent_profile"]]

    # ②③ 逐候选评估（显式与自动一致）
    entries = [evaluate(config, pid, requirements, snapshot.get(pid, {}))
               for pid in candidates]

    # ④ 判定
    eligible = [e for e in entries if e.status == "eligible"]

    if requested_profile:
        if not eligible:
            # 不回落、不换人：把该候选真实的排除原因报出来
            raise Error(entries[0].code, entries[0].detail, details={
                "profile_id": requested_profile, "excluded": entries[0].to_dict()})
        return Decision(mode="explicit", selected=requested_profile, reason="requested",
                        candidates=entries)

    if not eligible:
        raise Error("ROUTE_NO_ELIGIBLE", "no profile satisfies the task",
                    details={"gaps": [e.to_dict() for e in entries]})

    if len(eligible) == 1:
        return Decision(selected=eligible[0], reason="only_eligible", candidates=entries)

    return apply_preference(config, routing, eligible, entries)


def evaluate(config, pid, requirements, snapshot_entry):
    try:
        binding = resolve_binding(config, pid)                      # 无副作用
        require_runnable(binding, require_real_e2e=config.get("require_real_e2e"))
        for req in requirements:
            check_hard(binding, req)                                # §8.1
        check_environment(snapshot_entry)                           # 只读快照
        return eligible(binding)
    except Error as exc:
        return excluded(pid, exc.code, exc.message)
```

关键性质：

- 显式路径与自动路径的 `evaluate` **完全相同**；
- 显式路径失败时**报具体原因**（而不是笼统的 `NO_ELIGIBLE`），因为它只有一个候选；
- 显式路径**永不回落**到其他 profile（那会违背用户意图，且属于未经批准的替换）。

### 8.4 偏好交集与 fallback（R2 修正 ②）

原稿把"不在 `prefer` 中的 eligible 追加到末尾"，于是只要规则存在就一定返回第一项 —— 当 `prefer` 全部不可用时，会**偷偷**从批准列表里挑一个，而 reason 仍写 `rule_preference`。这既误导记录，也违背"批准范围不等于优先顺序"。

修正：

```python
def apply_preference(config, routing, eligible, entries):
    rule = match_rule(routing["rules"], task_type)      # 精确优先，其次 "*"
    eligible_ids = [e.profile_id for e in eligible]
    prefer = rule["prefer"] if rule else []

    hits = [pid for pid in prefer if pid in eligible_ids]
    if hits:
        return Decision(selected=hits[0],
                        reason="rule_preference" if rule["task_type"] != "*" else "catch_all_preference",
                        candidates=entries)

    # 交集为空：规则命中但一个都不可用 → 不得按允许顺序顶替
    return fallback(config, routing, eligible, entries,
                    reason="prefer_empty" if rule else "no_rule")
```

`fallback()`：

| `fallback` | 条件 | 结果 |
|---|---|---|
| `default_profile` | `default_agent_profile ∈ eligible_ids` | 选它，`reason="fallback_default"` |
| `default_profile` | 不在 eligible 内 | `ROUTE_AMBIGUOUS`（默认 profile 不得成为无条件胜出者） |
| `ask` | — | `ROUTE_AMBIGUOUS` + `options[]` |

三条边界语义变化（R2）：

| 情形 | 语义 |
|---|---|
| 唯一候选（`len(eligible)==1`） | **独立规则**：直接选，`reason="only_eligible"`，与偏好无关 |
| `prefer` 与 eligible 有交集 | 选交集首项 |
| `prefer` 与 eligible **无**交集 | 走 `fallback`，**绝不**按 `allowed_profiles` 顺序顶替 |

`reason` 必须能区分 `prefer_empty`（规则命中但没命中人）与 `no_rule`（压根没有规则）—— 这两者的运维含义完全不同：前者说明规则需要更新，后者说明还没配偏好。

### 8.5 为什么不做评分

评分需要"同一任务在同一执行器上的可比结果"。当前同一任务只跑一次，且失败原因大量来自环境与权限（`PERMISSION_PROFILE_UNBOUND`、`ADAPTER_VERSION_MISMATCH`）。用这种数据训练权重，会把"环境没配好"学成"工具不行"。

第一版的可测量目标不是"选得更聪明"，而是**减少人工选型耗时、避免错误派发**（§18.6）。

---

## 9 数据结构

### 9.1 字段（R2：`detail` 脱敏）

```json
{
  "schema_version": 1,
  "route_id": "rt-3f9a1c22b7e0",
  "task_id": "task-...",
  "project_id": "example",
  "assignment_revision": 1,
  "at": 1761700000.0,
  "trigger": "create",
  "mode": "auto",
  "decision": "selected",
  "task_type": "bugfix",
  "task_type_source": "request",
  "requested_profile": null,
  "requirements": {"non_interactive": true, "structured_result": true},
  "requested_scope": {
    "allowed_profiles": ["claude-local", "opencode-local"],
    "routing_digest": "9c1d...",
    "policy_revision": "a3f0...",
    "snapshot_digest": "5b7c..."
  },
  "selected": {
    "profile_id": "claude-local",
    "adapter_id": "claude-code",
    "adapter_backend": "anthropic-api",
    "profile_hash": "…",
    "capability": "UNVERIFIED",
    "trust_level": "unverified",
    "permission_profile": "isolated-edit",
    "permission_enforced": true,
    "credential_presence": true,
    "resolution_source": "path_alias",
    "evidence_strength": "UNVERIFIED",
    "rank_reason": "rule_preference"
  },
  "candidates": [
    {"profile_id": "claude-local", "status": "selected", "code": null, "detail": null},
    {"profile_id": "opencode-local", "status": "eligible", "code": null, "detail": "rank 2"},
    {"profile_id": "codebuddy-local", "status": "excluded",
     "code": "PERMISSION_PROFILE_UNBOUND",
     "detail": "permission argv not bound for 'isolated-edit'"}
  ],
  "fallback_used": null,
  "reason": "rule_preference",
  "rules_version": "9c1d…"
}
```

字段纪律：

| 规则 | 理由 |
|---|---|
| **`detail` 必须脱敏**（R2） | 该记录会进报告与 PR 描述。绝对路径替换为 `<repo>`、环境变量值替换为 `<redacted>`；原始 `Error.message` 只进 `tasks.last_error` 与 `task events`（本地诊断） |
| 不含 `session_id` / token / 环境变量值 | 同上 |
| `credential_refs` 只记 `credential_presence: bool` | 名称本身也是泄露面 |
| `candidates` 覆盖**进入循环的每一个** profile | 否则无法复核排除是否合理 |
| `evidence_strength` 与 `trust_level` 并列，不用 `quality` 之类词 | 防止读者把证据强度读成质量 |

### 9.2 落盘与崩溃一致性（R2 新增）

```text
ZOE_HOME/tasks/<task_id>/
├── route-01.json                     ← 首次派发
├── route-02.json                     ← reassign 后
└── assignments/reassign-02.json      ← 现有【核】
```

写入顺序与恢复规则：

| 顺序 | 动作 | 失败后果 |
|---|---|---|
| 1 | `atomic()` 写 `route-<rev>.json` | 任务目录存在但无决策文件 → 后续 `create` 重试会覆盖；`start` 拒绝 |
| 2 | 同一事务写任务记录（含 `route_id`、`snapshot_digest`）+ 事件 | 决策文件在、任务不在 → 孤儿文件，无害（无任务引用） |
| 3 | `store.operation` 记成功 | 重放命中操作记录 |

**启动前的硬检查**（R9）：

```text
start:
  若任务有 route_id:
      决策文件必须存在，且 route_id 与 snapshot_digest 与任务记录一致
      否则 → ROUTE_RECORD_INVALID（exit 3），不启动
  若任务无 route_id（v1.0.0 旧任务）:
      按现状处理，不新增门槛
```

单个 JSON 原子写不等于多个对象一起提交。这条规则把"部分提交"变成可检测状态，而不是静默继续。

### 9.3 `route_id` 的用途边界（R2 修正 ③）

```text
route_id = "rt-" + sha256(canonical_json({
    task_id, assignment_revision, task_type, requirements,
    selected.profile_id, routing_digest, policy_revision
}))[:12]
```

| 它能做 | 它不能做 |
|---|---|
| 关联决策与任务、幂等比对、报告引用 | **不能**证明"整个决策文件未被修改"（不覆盖排除原因全文与环境观测） |

文件完整性由独立摘要保障：

```text
route-<rev>.json 写入时计算 content_sha256（覆盖全文），存于任务记录
读取时校验；不一致 → ROUTE_RECORD_INVALID
```

理由：`route_id` 是**关联标识**，`content_sha256` 是**完整性校验**，两者职责不同。混用会让"改了排除原因但 route_id 不变"成为可能。

### 9.4 事件与报告

| 位置 | 增量 |
|---|---|
| 事件 | `route_decided`（`route_id`、`decision`、`profile_id`、`reason`、`candidate_count`、`excluded_count`） |
| `assignments[0].reason` | `route:rule_preference` / `route:project_default` / `explicit` / `route:only_eligible` / `route:fallback_default` |
| `_reports.snapshot()` | `route`（决策三元组 + `routing_digest` + 排除数） |
| `delivery-<rev>.md` | 「执行器选择」一节，含选中理由与排除原因（脱敏后） |
| `task status --json` | `route_id`、`route_reason` |

旧任务无这些字段时显示 `无路由记录（v1.0.0 之前创建）`，不显示 `null`。

---

## 10 接口设计

### 10.1 `zoe route plan`（只读预演，首版唯一交付物）

```bash
zoe route plan --project example --request-file route-request.json --json
```

```json
{
  "api_version": 2,
  "project_id": "example",
  "mode": "auto",
  "task_type": "bugfix",
  "task_type_source": "request",
  "decision": "selected",
  "selected": {"profile_id": "claude-local", "reason": "rule_preference"},
  "candidates": [ "…同 §9.1…" ],
  "routing_digest": "9c1d…",
  "policy_revision": "a3f0…",
  "snapshot_digest": "5b7c…",
  "snapshot_budget": {"candidates": 3, "probes": 3, "elapsed_ms": 412, "exhausted": false},
  "wrote": false
}
```

约束（R2 修正：不再声称"不 spawn 子进程"）：

- **不写盘、不改状态、不加锁**：不创建任务目录、不写 `state.db`、不产生 `route-*.json`。
- **允许有界版本探测**：`doctor()` 内部会调用 `_discovery.resolution_for_adapter`，后者传入 `adapter.parse_probe`，因此**每候选会 spawn 一次 `--version`**（`FACT_PENDING` 适配器除外）【核】`src/_discovery.py:441-459`、`src/_config.py:220-224`。这是预演的必要成本，必须在输出里报出来，不能假装零成本。
- `--no-probe` 可关闭探测：此时版本档记 `unverified`，能力态不变，`snapshot_budget.probes = 0`。
- 无匹配时**不抛错退出**，而是 `decision: "no_eligible"` + `gaps`（预演语义），但退出码仍为 4 便于脚本判断。
- `create` 走同一 `decide()` 但必须抛错（避免创建出无法派发的任务）。
- 输出 `snapshot_digest`，使"这次预演基于什么环境"可复核。

### 10.2 `task create`

在 `src/_pipeline.py:148` 之前插入快照与决策，其余冻结逻辑不变。响应新增 `route_id`、`route_decision`、`route_reason`、`routing_digest`。已发布字段语义不变。

### 10.3 `task reassign` 支持 `target_profile: "auto"`

前置校验之后调用路由器（携带 `route.task_type`/`route.requirements`，缺省沿用任务记录的 `task_type`），结果写 `route-<new_rev>.json`，`trigger: "reassign"`。

不允许：`phase` 为 `publish`/`ci`/`acceptance` 时换人（现状已拒【核】`src/_pipeline.py:1206-1210`）。

### 10.4 `zoe route show`

读取 `route-*.json`（最新 revision）+ 任务记录，**不重算**。文件缺失或摘要不符 → `ROUTE_RECORD_INVALID`。

### 10.5 幂等、失败记录与崩溃（R2 修正 ④）

原稿两处承诺互相冲突：`store.operation()` 在路由之前登记，却又说 `ROUTE_AMBIGUOUS` 时"不落任何记录"。统一为：

| 承诺 | 准确表述 |
|---|---|
| 路由失败 | **不创建任务、不启动执行、不生成成功派发记录**；**允许保留操作失败记录**（`operations` 表里的 `status: failed`，这是排障依据） |
| 重放 | 命中 `store.operation` → **直接返回已保存结果，不再探测、不再选择** |
| 用户补充 profile 后重试 | 请求内容已变 → 必须使用**新的 `request_id`**（旧 `request_id` 的重放只会返回旧的失败） |
| 崩溃 | 按 §9.2 的顺序写入；`start` 发现决策缺失或不一致 → `ROUTE_RECORD_INVALID` |

这里有一个容易忽略的细节：**幂等重放不能重新探测环境**。若重放路径重新跑 `doctor`，那么"同一 request_id 两次调用"可能返回不同结果，幂等名义就消失了。因此重放路径必须在路由函数**之前**返回。

### 10.6 错误码

新增 3 个：

| 码 | exit | 语义 | `details` |
|---|---|---|---|
| `ROUTE_NO_ELIGIBLE` | 4 | 所有候选被排除 | `task_type`、`gaps[]`（每候选 `profile_id`/`code`/`detail`）、`allowed_profiles` |
| `ROUTE_AMBIGUOUS` | 2 | 多候选且无规则/偏好无交集 | `task_type`、`options[]`、`fallback`、`reason`（`prefer_empty`/`no_rule`） |
| `ROUTE_RECORD_INVALID` | 3 | 决策记录缺失/摘要不符/与任务不一致 | `task_id`、`route_id`、`problem` |

复用：

| 场景 | 复用码 | exit |
|---|---|---|
| 显式 profile 不在批准范围 | `POLICY_DENIED` | 5 |
| 显式 profile 未通过硬条件 | 具体排除码（`PERMISSION_PROFILE_UNBOUND` 等） | 4/5 |
| `routing` 段字段非法 | `INPUT_INCOMPLETE` | 2 |
| 声明了 `route` 但项目未启用 | `INPUT_INCOMPLETE` | 2 |
| 策略变化 | `POLICY_CHANGED` | 5 |
| 同 revision 重复写决策 | `STATE_CONFLICT` | 3 |

`src/cli.py:51` 的 `_EXIT_CODES` 新增三行；`src/_util.py:62` 的 `_HINTS` 为三码补可执行建议。

---

## 11 派发前二次校验（R2：三态）

原稿写"`reassign` 的前置条件在 `start` 失败后天然满足"，过于绝对：启动握手失败、控制器崩溃或进程状态未知时，Agent 仍可能在运行。

现状代码已有对应保护【核】`src/_pipeline.py:1211-1213`（`not r or r["status"] == "exited"`）与 `:1200-1203`（`block_code != PROCESS_UNKNOWN`）。文档必须与实现同样严格：

| 观测到的状态 | 判断依据 | 允许换人 |
|---|---|---|
| **明确未启动** | 无 run 记录，或 `dispatch_state == "spawn_intent"` 且从未观测到进程 | 是 |
| **明确已退出** | `run.status == "exited"` | 是 |
| **状态未知** | `block_code == PROCESS_UNKNOWN`，或 `dispatch_state == "spawn_observed"` 而 `status != exited` | **否**：先 `zoe reconcile`，再判断 |

`start` 的既有校验链保持不变：

```text
reconcile → POLICY_CHANGED → require_runnable → doctor → disk → slots
【核】:681        :690            :721          :726   :731   :739
```

失败时不自动换人；若任务带 `route_id` 且失败码属于"执行器不可用"族（`ENVIRONMENT_ERROR`、`ADAPTER_*`、`PERMISSION_PROFILE_UNBOUND`），`hint` 追加：

```text
zoe task reassign <id> --request-file /abs/path/reassign.json
```

（R2：不使用 `<(...)` 进程替换语法 —— 它不是合法 JSON，且在 Windows 上不可用。）

---

## 12 失败切换边界

| 情形 | 允许的动作 | 强制校验 |
|---|---|---|
| 未启动（明确） | 换执行器 | 无 run，或 `spawn_intent` 且未观测 |
| 已开始执行（明确退出） | 必须先停止并确认 | `state ∈ READY/PAUSED/BLOCKED`；`block_code != PROCESS_UNKNOWN`；run 已退出 |
| **状态未知** | **不允许** | 先 `reconcile`（§11） |
| 已产生候选提交 | 换人即作废旧证据 | `reassign` 清空 `validated_sha`/`candidate_sha`/`current_evidence_id`/`acceptance`【核】`:1257-1271` |
| 工作区已漂移 | 换人前必须登记变更 | `handoff.fingerprint == fingerprint(t)`，否则 `HANDOFF_STALE`【核】`:1221-1226` |

注意区分两种"run 结束"：

| 场景 | 工作区 |
|---|---|
| 人主动换人（上表） | **保留**，作为交接材料 |
| v1.2 的模型闸门失败（本次不可信） | **隔离**：归档 `worktree.patch` 并从工作区撤出，见 v1.2 §12.9 |

v1.1 没有模型观测，因此不触发隔离路径。

## 12.1 `on_failure: auto_unstarted` 为什么只登记不实现

自动换人需要回答"换谁、换几次、失败算谁的责任"，而 `attempts` 是任务级预算、不是执行器级预算。在预算模型扩展前实现它，会让"换人"变成绕开预算的后门。v1.1 拒绝该值（`CAPABILITY_UNSUPPORTED`）。

---

## 13 上下文隔离

| 要求 | 实现 |
|---|---|
| 不把 A 的 session id 交给 B | run 记录固定 `session_mode: "new"`【核】`src/_pipeline.py:768`；`native_session_resume` 不在支持列表【核】`src/cli.py:428-431` |
| 交接材料由控制器生成 | 复用 `latest_handoff()` + `handoff.json`；路由不生成自由文本上下文 |
| 不混入其他项目 | `allowed_profiles ⊆ 本项目 agent_profiles` |
| 决策记录不含秘密 | §9.1 字段纪律 + `detail` 脱敏 |
| 换人后证据链断开 | `reassign` 作废当前引用并抬 `assignment_revision` |

---

## 14 无匹配与询问闭环

```text
decide
 ├─ 0 个通过          → ROUTE_NO_ELIGIBLE（exit 4）+ gaps[]
 ├─ 1 个通过          → only_eligible（独立规则）
 ├─ 多个通过 + 偏好有交集      → 交集首项
 ├─ 多个通过 + 偏好无交集/无规则
 │    ├─ fallback=ask             → ROUTE_AMBIGUOUS（exit 2）+ options[]
 │    └─ fallback=default_profile → 默认在 eligible 内则选它，否则 AMBIGUOUS
 └─ 显式指定未通过    → 该候选的具体排除码（不回落）
```

`fallback=ask` 的闭环由**宿主**完成：宿主收到 `options[]` → 向用户提问 → 用户选择 → 用**新的 `request_id`** 重发 `task create` 并带显式 `agent_profile`。

运行时**不写** `orchestration.pending_questions`：`create` 未产生 task_id，无处分摊这笔记录。

---

## 15 迁移与兼容

### 15.1 零迁移

| 维度 | 处理 |
|---|---|
| 项目配置 | 无 `routing` 键 → 规范化不新增键 → `policy_digest` 不变 |
| 状态库 | 不新增表/列；新字段只进 `tasks.data` 与任务目录；`record_version` 保持 2 |
| 旧任务 | 无 `route_id` → 报告占位；`start` 不因缺记录而拒绝（R9 只对"有 route_id 但记录不符"生效） |
| CLI 面 | 只增不改 |
| skill 契约 | `api_version` 2 / `protocol_revision` 1 不变 |
| `capabilities.not_supported` | 新增 `model_quality_routing` |

### 15.2 文档同步清单（同一提交）

`docs/capabilities.md`、`docs/architecture.md`、`docs/upgrading.md`、`schemas/*`、`zoe-workflows/references/cli-contract.md`、`references/runtime-contract.md`、`references/project-profile.example.json`。

### 15.3 策略收窄与换人：修复操作死锁（R2 修正 ⑤）

原稿给出"新策略不再允许当前 profile → `rebind` 拒绝 → 提示先 `reassign`"，但现状 `reassign` 要求"项目配置与冻结配置的 digest 一致"【核】`src/_pipeline.py:1184-1188`，即**要求先 rebind**。两条要求互为前置，形成死锁：

```text
rebind  →  需要先 reassign
reassign → 需要先 rebind
```

修正：`rebind` 不再拒绝，改为"采用新策略 + 必要时阻断任务"。

| 步骤 | 命令 | 结果 |
|---|---|---|
| 1 | `task pause`（或已停止） | 确认进程树停止 |
| 2 | `task rebind` | **无条件**采用当前策略（更新 `t.config`、重算 `policy_revision`） |
| 3a | 若当前 `agent_profile_id` 仍在新 `allowed_profiles` 内 | 任务恢复 `READY`，可 `start` |
| 3b | 若不在范围内 | 任务置 `BLOCKED` + `block_code: POLICY_DENIED` + `details.reason: "profile no longer allowed"`；**不清空工作区与历史证据** |
| 4 | `task reassign`（`target_profile: "auto"` 或显式） | 此时 digest 已一致，`POLICY_CHANGED` 不再触发；换人成功 |
| 5 | `task start` | 继续执行 |

这样"策略收窄不得静默生效"（3b 确实阻断了）与"必须有可执行路径"（4 可达）同时成立，且不需要新命令或新错误码。

若 3b 之后仍无法换人（例如新 `allowed_profiles` 里没有任何可用组合），明确要求**新建任务**，并在错误 `hint` 里直说，不要给出走不通的提示。

### 15.4 "删除 routing 即回滚"的准确表述（R2 修正）

| 动作 | 实际后果 |
|---|---|
| 删除 `routing` 段 | **改变 `policy_digest`** → 在途任务 `POLICY_CHANGED`；同时**移除批准范围**，路由器不再可用。这不是"无影响回退" |
| 若只想回到人工选型 | 保留 `routing` 段、把 `mode` 改为 `explicit` —— 不改变 digest 语义（digest 仍变一次，但批准范围保留，可逆） |

正确的回退顺序：

```text
1. 停派发（pause / finish）
2. 把 mode 改为 explicit（而不是删除整段）
3. register
4. 对在途任务 rebind（§15.3）
5. 确认无任务后再考虑删除整段
```

---

## 16 安全与威胁增量

| 威胁 | 缓解 |
|---|---|
| 请求绕过批准范围 | `POLICY_DENIED`（R2） |
| **显式指定绕过硬条件** | 显式与自动共用 `evaluate()`（R3，§8.3） |
| **偏好全失效时偷偷换人** | 交集语义 + fallback（R4，§8.4） |
| 规则引用未批准的 profile | 注册期 `POLICY_DENIED` |
| 用路由掩盖环境缺陷 | 排除原因全量记录；`start` 失败不自动换人 |
| **决策记录泄露本地路径到 PR** | `detail` 脱敏，原文只进本地诊断（§9.1） |
| 记录被篡改 | `content_sha256` 完整性校验（§9.3），不复用 `route_id` |
| 策略静默漂移 | `policy_revision` 冻结 + `POLICY_CHANGED` + `snapshot_digest` |
| 宿主大模型左右选择 | 路由器只读配置与 `ProfileBinding`，不接受自由文本"建议名" |
| **错误地宣称断网隔离** | §8.1.2：`network` 不作为硬条件，`local-process` 不限制出口 |

不在本设计的防护范围：Agent 持有用户账户凭据（见 `docs/threat-model.md`）、`allowed_paths` 是事后审计而非沙箱。

---

## 17 测试矩阵

新增 `tests/test_v110_routing.py`。

| 类别 | 用例 | 断言 |
|---|---|---|
| 纯函数 | `decide()` 的 16 个分支（表驱动，构造 snapshot） | 决策 + reason 与预期一致 |
| 纯函数 | 同一 snapshot + 同一配置两次调用 | 逐字段相同（R6） |
| **反例 1** | 显式 profile 未满足 `non_interactive` | 返回 `PERMISSION_PROFILE_UNBOUND`，**不回落**别的 profile |
| **反例 2** | `mode=explicit` + 默认 profile 不在 `allowed_profiles` | 注册期 `INPUT_INCOMPLETE`（不等到运行时） |
| **反例 3** | `allowed=[A,B,C]`、`prefer=[A]`、A 不可用、B/C 可用、`fallback=ask` | **`ROUTE_AMBIGUOUS`**（不得选 B） |
| **反例 3b** | 同上但 `fallback=default_profile` 且默认在 eligible 内 | 选默认，`reason=fallback_default` |
| **反例 3c** | 同上但默认不在 eligible 内 | `ROUTE_AMBIGUOUS` |
| **反例 4** | 同 `request_id` 重放（两次） | 同 `route_id`；**第二次不探测环境**（用桩计数） |
| **反例 5** | 写决策文件后、写任务记录前崩溃 | 重试可覆盖孤儿文件；无任务引用 |
| **反例 6** | 有 `route_id` 但决策文件缺失/摘要不符 | `start` → `ROUTE_RECORD_INVALID`，不启动 |
| **反例 7** | `block_code=PROCESS_UNKNOWN` 时 `reassign` | `PROCESS_UNKNOWN` 拒绝（三态表） |
| 配置 | `allowed_profiles` 含未定义 profile / `prefer` 越界 / `task_type` 重复 | 对应码 |
| 配置 | `default_agent_profile ∉ allowed_profiles` | `INPUT_INCOMPLETE` |
| 环境 | 候选数 8 + 预算耗尽 | 剩余候选标 `ENVIRONMENT_ERROR`，不进 eligible，总耗时受控 |
| 环境 | 项目级检查次数 | 与候选数无关（一次） |
| 生命周期 | `route plan` | 无文件、无 DB 写；版本探测次数 == 候选数（`--no-probe` 时为 0） |
| 负例 | 0 候选 | `ROUTE_NO_ELIGIBLE`，exit 4，**无任务、无 route 文件、无成功派发记录**；有 `operations` 失败记录 |
| 负例 | 显式越界 profile | `POLICY_DENIED`，exit 5 |
| 迁移 | v1.0.0 黄金配置（无 routing） | 无 `routing` 键，`policy_digest` 与基线一致 |
| 迁移 | 加入 routing 后的在途任务 | `start` → `POLICY_CHANGED`；按 §15.3 五步可恢复 |
| 迁移 | 3b 场景（rebind 后 profile 失准） | 任务 `BLOCKED` + `POLICY_DENIED`，随后 `reassign` 成功 |
| 记录 | `detail` 脱敏 | 不含绝对路径、不含环境变量值 |
| 记录 | `content_sha256` 校验 | 改动 `candidates[].detail` 一字节 → `ROUTE_RECORD_INVALID` |
| 幂等/记录 | `reassign(target_profile="auto")` | 新 `route-02.json`，`trigger=reassign`，旧引用作废、历史文件保留 |
| 契约漂移 | `routing` 段与 schema 一致 | schema 校验通过/拒绝同构 |
| 文档契约 | `capabilities.not_supported` 含 `model_quality_routing` | `test_doc_contracts.py` |

---

## 18 验收标准

1. 未声明 `routing` 的项目：`task create`/`start` 响应字段与 v1.0.0 逐字段一致。
2. **显式指定一个不满足硬条件的 profile 时，返回该硬条件的排除码，且不切换到其他 profile。**
3. 无匹配时返回 `ROUTE_NO_ELIGIBLE`，`gaps` 长度等于候选数，**未创建任务目录、未写 route 文件、未产生成功派发记录**（允许存在 `operations` 失败记录）。
4. `allowed=[A,B,C]`、`prefer=[A]`、A 不可用时，`fallback=ask` 必须返回 `ROUTE_AMBIGUOUS` 并列出 B/C，**不得**自动选 B。
5. 显式请求越界 profile → `POLICY_DENIED`(exit 5)，不产生任何文件。
6. 同 `request_id` 重放两次：`route_id` 相同、route 文件数不变、**第二次调用不触发环境探测**（桩计数为 0）。
7. `route plan` 调用前后文件系统与 DB mtime 不变；输出含 `snapshot_digest` 与 `snapshot_budget`。
8. 有 `route_id` 而决策文件缺失或 `content_sha256` 不符 → `start` 返回 `ROUTE_RECORD_INVALID`(exit 3)，不生成 run。
9. `block_code=PROCESS_UNKNOWN` 时 `reassign` 被拒；`reconcile` 后按三态表判断。
10. 策略收窄后：`rebind` 不报错；当前 profile 失准时任务 `BLOCKED`；随后 `reassign` 成功（五步路径可走通）。
11. v1.0.0 黄金配置（无 `routing`）规范化后 `policy_digest` 与升级前一致。
12. `route-<rev>.json` 的 `detail` 不含绝对路径与环境变量值。

### 验证实验（产品假设，不是代码验收）

| 指标 | 采集方式 |
|---|---|
| 选型耗时 | 人工：从收到任务到 `task create` 的间隔；自动：`create` 耗时 |
| 错误派发次数 | 首次 `start` 失败且失败码属"执行器不可用"族 |
| 返工次数 | `assignments` 长度 > 1 的任务占比 |
| 排障成本 | 从 `start` 失败到下一次成功 `start` 的间隔 |

判据（建议，非承诺）：若"错误派发"与"排障成本"没有下降，自动匹配不值得开；单执行器项目保持 `explicit`。

---

## 19 实施顺序（R2：四步，首版只做只读）

| 阶段 | 交付范围 | 通过标准 |
|---|---|---|
| **第一步** | `routing` 配置校验 + `decide()` 纯函数 + 快照收集 + `route plan`（只读） | 显式 / 默认 / 自动三条路径均不绕过授权与硬条件；偏好无交集不顶替 |
| **第二步** | `create` 接入 + 决策记录 + 幂等重放 + 崩溃一致性 | 重放不重复探测；崩溃不产生双任务、不覆盖决策 |
| **第三步** | `reassign(auto)` + 策略变更五步路径 + 启动前三态 | 状态未知不换人；策略收窄有可走通的路径 |
| **第四步** | 宿主（WorkBuddy/OpenClaw）调用 + 真实运行验证 | 宿主能展示选择理由、处理歧义、记录结果 |

真实验证目标仍是 **macOS / Windows 本地 CLI**，不引入 Docker。若要宣称"跨 Agent 自动派发已支持"，至少完成两个不同 Agent 的真实派发，并分别声明经过验证的平台范围。

验收测试优先覆盖六类反例（§17 的反例 1、2、3、4、5、7）。

发布纪律：第一步单独可发布（无写入路径，风险最低）；第二步起每次必须同提交更新 §15.2 文档清单。

---

## 20 回滚

| 触发 | 动作 | 影响 |
|---|---|---|
| 自动匹配误导派发 | `mode: explicit` | 回到默认 profile 行为；**不改变 digest 语义之外的范围**，批准范围保留 |
| 规则错误 | 删除 `rules`，保留 `allowed_profiles` | 变成"只有唯一候选时才自动" |
| 完全回退 | 见 §15.4 五步（先 `explicit`，再考虑删段） | 删除 `routing` 会改 digest 且移除范围，需 `rebind` |
| 降级运行时 | 删除 `routing` 段后降级 | 旧版读到未知顶层键不报错（v2 顶层未禁止额外键），但 digest 语义不同，在途任务需 `rebind` |

---

## 21 风险与未决

| 编号 | 风险 | 影响 | 缓解 |
|---|---|---|---|
| K1 | 规则顺序无证据支撑，只是偏好 | 选错执行器 | 记录全部排除原因；用 §18.6 实验证伪假设 |
| K2 | 每候选一次 `doctor` | 路由变慢 | 候选上限 8；项目级检查一次；总预算（§8.2） |
| K3 | 自动路由掩盖环境缺陷 | 用户看不到"CLI 没装" | 排除原因进 `details` 与报告 |
| K4 | `non_interactive` 只是必要条件 | 任务仍可能卡在审批 | `permission_state.enforced` 的语义在报告里注明"必要非充分" |
| K5 | 加入 `routing` 触发 `POLICY_CHANGED` | 在途任务被阻断 | §6.3 运维顺序 + §15.3 五步 |
| K6 | `fallback=ask` 在无宿主时等于硬失败 | CLI 用户被卡住 | 文档明确"`ask` 需要交互宿主"；纯 CLI 用 `default_profile` |
| K7 | 快照过期（预演与派发间隔长） | 预演结论与派发不一致 | 记录 `snapshot_digest`；`create` 重新取快照；`start` 二次校验 |

未决问题：

1. `route plan` 的 `--spec-file` 是否允许（需要 spec 校验但不创建任务）→ 倾向允许，走 `_contracts.load_spec` 的只读分支。
2. `task_type` 是否允许项目自定义词表 → 倾向不允许（v1.1），否则拼写错误会静默降级。
3. 决策记录是否进 `evidence.json` → 不进：证据回答"候选提交是否被独立验证过"，路由回答"为什么是这个执行器"。

---

## 22 附录

### 22.1 决策原因枚举

| `reason` | 含义 |
|---|---|
| `requested` | 请求显式指定（仍已通过全部检查） |
| `project_default` | 项目默认 profile（未启用路由） |
| `only_eligible` | 只有一个候选通过 |
| `rule_preference` | 精确 `task_type` 规则与可用集合的交集首项 |
| `catch_all_preference` | `*` 规则与可用集合的交集首项 |
| `fallback_default` | 偏好无交集/无规则时回落到默认（且它在 eligible 内） |
| `prefer_empty` | 规则命中但可用集合与 `prefer` 无交集（进入 fallback） |
| `no_rule` | 无匹配规则（进入 fallback） |

`decision`：`selected` / `no_eligible` / `ambiguous`。
候选 `status`：`selected` / `eligible` / `excluded`。

### 22.2 最小可用配置示例

```json
{
  "schema_version": 2,
  "project_id": "example",
  "repo": "/abs/path/to/repo",
  "backend": "local-process",
  "network": "none",
  "default_agent_profile": "codebuddy-local",
  "agent_profiles": {
    "codebuddy-local": {
      "adapter": "codebuddy",
      "backend": "codebuddy-cli",
      "expected_version": "2.111.0",
      "permission_profile": "isolated-edit",
      "require_permission_binding": true,
      "credential_refs": ["model-codebuddy"],
      "compatibility_evidence": "compat/codebuddy-local.json"
    },
    "opencode-local": {
      "adapter": "opencode",
      "backend": "opencode-cli",
      "expected_version": "REPLACE_WITH_EXACT_CLI_VERSION",
      "permission_profile": "isolated-edit",
      "credential_refs": ["model-opencode"]
    }
  },
  "routing": {
    "schema_version": 1,
    "mode": "auto",
    "allowed_profiles": ["codebuddy-local", "opencode-local"],
    "rules": [
      {"task_type": "bugfix", "prefer": ["codebuddy-local", "opencode-local"]},
      {"task_type": "feature", "prefer": ["opencode-local", "codebuddy-local"]}
    ],
    "fallback": "default_profile"
  },
  "authorization_ref": "REPLACE_WITH_REAL_SCOPE",
  "allowed_paths": ["src", "tests"],
  "checks": [{"id": "unit", "argv": ["pnpm", "exec", "vitest", "run"]}]
}
```

注意：`default_agent_profile` 必须在 `allowed_profiles` 内（§6.2 第 7 条）。示例中的优先顺序**只是写法示例**，不代表工具能力排名。

### 22.3 与评审意见的对应

| 评审意见 | 落点 |
|---|---|
| 只在批准范围内选择 | §6、§8.3 |
| 先过滤硬条件，再按偏好 | §5.1、§8.1、§8.4 |
| create 冻结、start 再检查 | §5.3、§11 |
| 启动失败不自动换 | §11、§12.1 |
| reassign 显式换人并作废旧证据 | §12 |
| 无法裁决时返回选项 | §14 |
| 不引入队列/DAG/自动评分 | §2.2、§8.5 |
| ① 显式与默认不能绕过硬条件 | §8.3（统一 `evaluate`）、§6.2 第 7 条 |
| ① 权限绑定 ≠ 非交互已落实 | §8.1（"必要非充分"）、§8.1.2 |
| ① network 缺失 ≠ 断网已落实 | §8.1.2 |
| ① 三态不宜作质量等级 | §8.1.1（改名 `evidence_strength`） |
| ② 偏好全失效不得按批准列表选人 | §8.4（交集 + fallback） |
| ③ 决策与环境探测分离 | §5.2、§10.1（`snapshot_digest`） |
| ③ `route_id` 不是完整性证明 | §9.3（`content_sha256`） |
| ④ 失败可保留操作记录 | §10.5 |
| ④ 补充 profile 后换新 `request_id` | §10.5 |
| ④ 崩溃恢复规则 | §9.2（R9） |
| ⑤ 策略收窄与换人死锁 | §15.3（五步路径） |
| ⑤ "删除 routing 即回滚"表述错误 | §15.4 |
| ⑥ start 失败 ≠ 旧执行不存在 | §11（三态表） |
| ⑥ `<( )` 不合法 | §11 |
| 优化 1 错误详情分内部/对外 | §9.1（`detail` 脱敏） |
| 优化 2 不重复跑 doctor | §8.2（项目级一次 + 总预算） |
| 四步落地顺序 | §19 |
| 六类反例 | §17、§18 |
