# 任务组交付（Task Group Delivery, TGD）详细设计

- 文档版本：**1.1**（R2，含首轮外部评审处置）；日期：2026-10-30。
- 基线：`zoe-runtime` v1.2.1（`src/__init__.py`），state schema **v3**，`meta.protocol_version="4"`，
  skill 契约 `api_version=2` / `protocol_revision=1`。
  本次评审未独立复核"v1.2.1 已实现"的源码与测试声明，相关结论以基线源码为准，实施前须复跑。
- 目标版本：**v1.3.0**。契约冻结状态见 §0.5：**P0/P1 可开工，P3/P4 契约待冻结**。
- 状态：**待实施**。本文不是开发完成、平台验证或发布证明。

## 0.0 本版定位（对外必须这样描述）

> **v1.3 交付的是"静态任务组自动推进与检查"**：把一份已冻结的成员依赖图交给本机受管推进器，
> 串行执行、串行集成、按本地产物做集成级验收。
> **它不实现"自动检验代码是否符合设计文档"**——设计一致性审查推迟到 v1.4.0，
> 报告中该层固定写 `NOT_RUN`。

同时明确三件本版不做的事（本轮评审拍板）：

1. **不做 PR 组的正式收口**（仅 `local_artifact` 组可 `accept`）。
2. **不做已集成成员的原地修订**（要改就新建关联组）。
3. **不做共享路径隐式依赖边**（v1.3 全局串行，不需要）。

### 0.0.1 R2 修订摘要

| # | 评审问题 | 处置 | 落点 |
|---|---|---|---|
| 1 | `plan_digest` 混入运行状态，计划会不断变化；成员 Spec 只在创建时读路径 | 采纳。摘要改为**不可变投影**；创建组时冻结各成员 Spec 内容与 sha256 | §4.2、§4.4、§5.1 |
| 2 | "纯函数校验"实际含 IO；路径按字符串交集；隐式依赖边方向不明 | 采纳。拆 `resolve_plan_inputs`（IO）/`validate_plan`（纯）；路径按目录边界；删除隐式边；`expected_paths` 定性为规划提示 | §5.2 |
| 3 | `tick` 只保证"一轮一个"，不保证"全组一个"；预算门禁挡住最后一次交付；"预留"被称作"实际启动" | 采纳。组槽位与全局槽位取小；预算只门禁**新启动**；计数改为"计费启动额度 + 释放" | §7.2、§7.3 |
| 4 | runner 父子锁交接有空窗；`meta.epoch` 不能当接管代数；`dispatch`/`task start` 可绕过组准入 | 采纳。复用既有握手并引入**每次启动唯一 `lease_id`**；统一组级准入入口 | §7.1、§7.6 |
| 5 | Git 对账缺 `prepared` 时点，`result_sha` 可能根本没落盘 | 采纳。账本改为 `intent → prepared → done` 三段，恢复分态处理 | §8.2、§8.3 |
| 6 | 上游修订后 `invalidated` 没有恢复出口 | 采纳。v1.3 **禁止已集成成员原地变更**；未集成成员走既有 rebind/resume | §8.4、§6.4 |
| 7 | PR 组用"人工确认 + NOT_RUN"降低门禁 | 采纳。v1.3 **拒绝 `delivery_mode=pull_request` 的组**；自行合并只能记外部收口 | §9.4、§12.5 |
| 8 | 若干契约前后不一致 | 采纳 8 项逐条修正 | §0.6 |

---

## 0 对原方案（v1.0 输入）的裁定

### 0.1 裁定口径

1. **不重复开发**：基线已有能力只能作为回归项。
2. **不新增第二个真相源**：状态、证据、幂等、锁各只有一个机制承载。
3. **不夸大边界**：本机进程式、非沙箱、非无人值守、不含设计语义审查。

### 0.2 逐条裁定

| 原方案 | 内容 | 裁定 | 本文落点 | 理由 |
|---|---|---|---|---|
| §1.1–1.2 | 一次发起、持续推进、可中断恢复、按设计验收 | **调整采纳** | §1 | "按设计验收"在 v1.3 只能到集成级检查，定位见 §0.0 |
| §1.3 | 不做自我改写、提示词竞赛、跨仓库图、分布式调度、自动扩权 | **采纳** | §1.2 | 与既有边界一致 |
| §2 | 五角色职责边界 | **采纳（文档）** | §1.3 | 无代码成本 |
| §3 | 推进器不需要常驻 LLM；宿主退出后仍可推进 | **调整采纳** | §3 | 受管进程式 runner，不装 daemon；进程消失即停止推进 |
| §4 | 开发前 `baseline.json` 盘点 | **采纳** | §2、§5.1 | 落到 `groups/<gid>/baseline.json`，不写仓库文件 |
| §4.1 F8 | 版本对齐 | **部分已完成 → 补齐** | §2.3 | `tests/test_distribution.py` 与 `--check-requirements` 已存在 |
| §4.2 F1 | rebind 后只重验 | **已完成 → 回归** | §2.3 | `_pipeline._revalidation_product`、`tests/test_v121_rebind_revalidate.py` |
| §4.3 F2 | attempts 单调、豁免唯一、责任分派 | **已完成 → 回归** | §2.3 | 仅新增组级启动额度上限 |
| §4.4 F3 | PASS/FAIL/NOT_VERIFIED 三态 | **调整采纳** | §5.4 | 不新增第四态（证据契约冻结）；改为 `error_class` 分类 |
| §4.4 F4 | repair 建议 | **不采纳（机制）** | §10 | 原方案自己也限定"仅供解释"；有拼接 shell 的风险 |
| §4.4 F5 | `auth_mode` 属 profile，`unknown` 不算成功 | **采纳** | §5.5 | 基线无此字段 |
| §4.4 F6 | 按交付模式提供模板 | **采纳（skill 侧）** | §13 | 只改 `zoe-workflows/references/` |
| §4.4 F7 | 保护路径优先、越界全量、扩权与还原分道 | **已完成 → 回归** | §2.3 | v1.2.1 `scope()` |
| §5.2 | plan JSON 示例 | **调整采纳** | §5.1 | 删 `required_capabilities`；预算显式 |
| §5.3 | 依赖图、覆盖、路径、共享文件先串行 | **采纳** | §5.2 | 基线 `_groups.create` 现**拒绝** `depends_on`，本版是能力反转 |
| §6 | 执行组合路由 | **已完成 → 复用** | §2.1、§11.1 | v1.2 已交付纯函数 `decide()`/`combination_id`/观测/`fallback=ask`；组级只在 `group plan` 里用 `plan_route` 预演 |
| §7 | 四张新表 | **不采纳（表结构）** | §4 | 与本项目"单 JSON + 行版本 CAS"冲突；复用 `groups`/`operations`/`events` |
| §7 | 产物布局 `projects/<pid>/groups/` | **调整采纳** | §4.4 | 改为 `ZOE_HOME/groups/<gid>/`；**理由是与 `tasks/<id>/` 布局统一**（`<pid>.json` 与 `<pid>/` 本身并不冲突） |
| §7.1 | 先写 artifact 再提交 DB、孤立文件可对账 | **采纳** | §4.6 | 复用 `_util.atomic` 与 `store.operation_prior` |
| §8.1 | 组状态含 DRAFT | **调整采纳** | §6.1 | 无 DRAFT；`plan` 是无状态预览 |
| §8.2 | 成员状态五值、不提前改 DONE | **调整采纳** | §6.2–6.4 | 状态派生 + 4 个持久事实；新增 `delivery_mode=group_member` |
| §8.3 | 恢复先对账 | **采纳** | §7.5 | 与既有 reconcile 决策树一致 |
| §8.4 | 上游修复→下游失效 | **不采纳** | §8.4 | 无恢复出口，会造永久阻塞；改为"已集成成员冻结" |
| §9 | 组租约、epoch、预算、默认串行 | **调整采纳** | §7 | `lease_id` 取代 `epoch` 作为接管代数；预算只做组级且只门禁新启动 |
| §10 | 串行集成、意图→对账 | **采纳（加 prepared）** | §8 | 与 `publication.json` 意图模式同构，但需三段账本 |
| §10.2 | 候选可用 commit range 或 patch | **不采纳（双表达）** | §8.2 | 统一 `git merge --no-ff <candidate_sha>` |
| §11 | 三层验证、审查 Agent、DONE 条件 | **拆分采纳** | §9 | 集成级检查 v1.3；审查 Agent v1.4 |
| §12 | 修复闭环、组级 repair run | **部分采纳** | §10 | 组级 repair 执行推迟；v1.3 到"阻塞 + 最小决策" |
| §13 | 外部收口 `declared/verified` | **采纳（改词表）** | §9.5 | 沿用既有 `claim: verified|asserted` |
| §14 | 9 个 CLI 命令 | **调整采纳** | §11 | 保留 `dispatch`；`plan` 两种形式并存（§0.6-1） |
| §15 | Skill 瘦身 | **采纳** | §13 | 增 3 份 reference |
| §16 | ID 派生、审批是审计字段、argv 不拼 shell | **采纳** | §12 | 与现状一致 |
| §17 | W0–W7 | **调整采纳** | §14 | P0–P5；P3/P4 契约待冻结 |
| §18 | T01–T24 | **筛选采纳** | §15 | 保留 + 新增 7 条（T25–T31） |
| §19 | 迁移、双仓、回滚 | **调整采纳** | §12 | schema v3→v4 不新增表 |
| §20 | Harness² | **不采纳（本版）** | §16 | 与交付目标无关 |
| §21 | 完成定义 | **采纳并改写** | §17 | 分"开发现状/试用门槛/正式矩阵" |

### 0.3 与 v1.2 路由设计的版本冲突

`docs/execution-combination-routing-design.md` §2.3 把「L3 自动排除」排在 v1.3+。本设计占用
v1.3.0，因此：

- **v1.3.0 与 v1.4.0 均不含 L3 自动排除**，该能力**移出完成定义、不排期**；
- `capabilities.not_supported` 中 `performance_based_exclusion` 保持不宣称；
- CHANGELOG 必须写明这次顺延，不得让读者以为 v1.3 完成了统计反哺选择。

### 0.4 本轮评审处置（8 项）

| # | 结论 | 具体改动 |
|---|---|---|
| 1 | 采纳 | `plan_digest` 只覆盖不可变投影（§4.3）；创建组时冻结成员 Spec 内容与 sha（§4.4、§5.1）；新增 T31 |
| 2 | 采纳 | 拆 IO 与纯校验（§5.2 两步）；路径按目录边界判定；删除隐式依赖边；`expected_paths` 明确定性地为**规划提示，不参与授权** |
| 3 | 采纳 | 组槽位 `1 - 本组活跃或已预留`，与全局槽位取小（§7.2）；预算只门禁新启动，不阻止对账/集成/检查/验收（§7.3）；计数改为"计费启动额度"（§7.3）；新增 T25/T26 |
| 4 | 采纳 | 复用既有握手 + 每次启动唯一 `lease_id`（§7.1）；空窗期以 `lease.state` 对账封堵；组级准入统一入口（§7.6）；新增 T07 断言"第二个 group start 被拒" |
| 5 | 采纳 | 账本 `intent → prepared → done`，ref 更新前必须落 `prepared`（§8.2、§8.3）；新增 T27 |
| 6 | 采纳 | 已集成成员禁止 `reassign/rebind/revise`，变更走新组（§6.4、§8.4）；新增 T28 |
| 7 | 采纳 | 组 `delivery_mode` 只允许 `local_artifact`；PR 组 create 直接拒绝（§9.4、§12.5）；`decision=rejected`、指纹、证据归属逐项补齐；新增 T29/T30 |
| 8 | 采纳 | 逐条修正见 §0.6 |

### 0.5 契约冻结状态（决定能否开工）

| 包 | 内容 | 契约状态 | 可开工 |
|---|---|---|---|
| P0 | 前置收尾与回归（F1/F2/F7 回归、F5、F3 分类、capabilities 同步） | **冻结** | 是 |
| P1 | 组计划契约 + 组记录 v3 + schema v4 + 迁移 | **冻结**（§4、§5、§12） | 是 |
| P2 | 成员交付分支 + 组级路由预演 | **冻结**（§6.3、§6.4） | 是 |
| P3 | 推进器（租约、tick、预算、暂停恢复取消、组级准入） | **待冻结**：需先通过 G1/G2 | 否 |
| P4 | 串行集成 + 组级检查/报告/验收 | **待冻结**：需先通过 G3/G4 | 否 |
| P5 | 设计审查 + 组级修复 + 有限并行 + PR 组收口 | **未设计**（仅列边界） | 否 |

进入 P3/P4 实现前必须闭合的四道门：

| 门 | 判据 |
|---|---|
| **G1 启动锁交接** | `lease_id` 握手在故障注入下证明：父子交接无第二个 runner；父在任意阶段崩溃后状态可对账 |
| **G2 组级准入** | runner、`group dispatch`、`task start/resume` 三者调用同一准入函数；无绕过路径（测试断言） |
| **G3 Git prepared 账本** | ref 更新前崩溃、ref 更新后崩溃、快速检查失败三种情况均可对账且不重复 merge |
| **G4 验收条件** | `local_artifact` 组的指纹/SHA/证据归属校验通过；PR 组被明确拒绝 |

### 0.6 契约一致性修正（评审第 8 项）

| # | 问题 | 修正 |
|---|---|---|
| 1 | `group plan` 是否接收 `--request-file` 前后矛盾 | **两种形式并存**：`group plan --project P --request-file plan.json`（预览未冻结计划，无副作用）与 `group plan GID`（既有：已存在组的只读准入视图）。同名不同形，按是否给 `GID` 分派，`--help` 明确 |
| 2 | `request_id` 只按组/动作/修订生成，第二次 pause 会重放第一次 | 加入操作序号：`grp-<gid>-<action>-r<revision>-s<seq>`，`seq` 来自组记录内单调计数器 `op_seq`（**不进入** `plan_digest`），在同一事务内 +1 |
| 3 | 成员派生状态未覆盖"无 task 且依赖已满足"、暂停、取消、失败 | §4.7 给出 9 个状态 + **判定优先级**（自上而下首个命中生效） |
| 4 | `auth_mode` 只有字段，没有凭据预检语义 | §5.5 明确 `credential_refs` 必需性、CLI 登录预检只能到 `unknown/present/absent` 且 `present` 需实测、`unknown` 可派发但报告不得称"已认证" |
| 5 | 计时口径"只算有推进动作的 tick"会漏掉长时间等待 Agent | §7.3 改为 `wall_seconds` 自首次 `RUNNING` 起**含休眠与等待**的墙钟时长；如实命名 `wall_seconds_inclusive`，不承诺排除休眠 |
| 6 | L3 一处推迟到 v1.4 之后、一处列入 v1.4 | 统一为"**移出完成定义、不排期**"（§0.3、§17） |
| 7 | 既要求至少一个真实 CLI 跑通，又固定声明无 `VERIFIED` | 拆成三档：开发现状（无 `VERIFIED`）、试用发布门槛（≥1 真实组合跑通组链路）、正式发布矩阵（跨 Agent ≥2 真实组合）（§17.3） |
| 8 | 声称 `<pid>.json` 与 `<pid>/` 路径冲突 | **该理由错误**，二者不冲突。保留 `ZOE_HOME/groups/<gid>/`，理由是**与 `tasks/<id>/` 布局统一**，不是避让 |
| 9 | 保留 `automatic_merge` 不支持声明，但本版会自动合并到集成分支 | 声明限定为"**目标主分支自动合并**"；集成分支自动合并属受控集成，不冲突（§2.2） |

---

## 1 范围

### 1.1 v1.3.0 必须交付

1. **组计划契约**：`members + depends_on` 静态依赖图；结构校验、需求/AC 覆盖校验、
   路径目录边界校验；不可变投影的 `plan_digest`；成员 Spec 内容冻结。
2. **组记录 v3**：复用 `groups` 表，`definition`（不可变）与 `runtime`（可变）分离；
   legacy v2 记录保持可读。
3. **成员交付分支**：`delivery_mode="group_member"`，验证通过即停 `REVIEW_READY`，
   不发布、不单独验收、集成后冻结。
4. **组推进器**：受管进程，`tick` 全组串行；`lease_id` 租约；组级启动额度。
5. **串行集成**：`intent → prepared → done` 三段账本；条件更新 ref；冲突隔离。
6. **组级检查与收口**：必需检查绑定 `integration_sha`；仅 `local_artifact` 组可 `accept`。
7. **前置收尾**：F5（`auth_mode`）、F3（`error_class`）、`capabilities` 同步。

### 1.2 明确不做（v1.3.0）

- 设计一致性审查 Agent、组级修复执行、`max_parallel > 1`（v1.4.0）。
- **PR 组的正式收口**（`delivery_mode=pull_request` 的组直接拒绝）。
- **已集成成员的原地修订**（新建关联组）。
- **共享路径隐式依赖边**（全局串行已足够）。
- 动态生成任务、跨仓库任务图、分布式调度、容器隔离、OS 服务/定时器。
- 自动放宽授权、降低验收标准、无人批准的主分支合并、L3 自动排除。

### 1.3 角色边界

| 角色 | 负责 | 不负责 |
|---|---|---|
| Skill / 宿主 | 需求消歧、拟定 plan、解释报告、必要时询问用户 | 把聊天记录当状态源；绕过 CLI 改 `state.db` |
| Runtime（控制器） | 校验、冻结、调度、归属、预算、恢复、证据、Git 集成与验收 | 猜业务要求；用 LLM 评分取消硬门禁 |
| 编码 Agent | 在成员 worktree 内实现、自检、输出结构化结果 | 改任务账本、主分支、授权与验收规则 |
| 用户 | 批准范围与变更、需要人工的验收 | 逐次批准已授权范围内的普通执行 |

---

## 2 基线盘点

### 2.1 直接复用

| 能力 | 位置 | 复用方式 |
|---|---|---|
| 任务状态机 7 态 + 6 阶段 | `_pipeline.py`、`docs/architecture.md` | 成员任务原样使用 |
| 唯一写者（writer EX + pid/start_token） | `_pipeline.lifecycle_locks`、`_platform/*` | 成员启动仍走 `start` |
| 派发握手与幂等重放 | `_pipeline.start`/`_start_locked`/`_replay` | `group start` **复用实现**，不复制形状 |
| reconcile 决策树 | `_pipeline.reconcile` | tick 每轮先对账 |
| 独立验证与证据 | `run_checks`/`judge_validation`、`_evidence.*` | 组级检查用同形结果结构 |
| 执行组合路由（纯函数） | `_routing.decide/plan_route/combination_id` | 组级路由预演直接调用 |
| 并发快照 | `_pipeline.slots` | 组派发取全局空位 |
| 预算豁免 | `_billable_attempts`/`_waived_run_ids` | 任务级口径不变 |
| 远端漂移分类 | `_drift.py` | 集成分支推送前复用 |
| 发布意图文件 | `_publisher.publish`（`publication.json`） | 集成账本同构但更严（§8） |
| 交付报告 | `_reports.snapshot/render` | `group report` 新增组级渲染器 |
| 兼容门禁 | `_requirements.check` | 不变 |

### 2.2 语义不同，必须区分

| 项 | 基线事实 | 本设计差异 |
|---|---|---|
| `group` | 有序记录，**显式拒绝** `depends_on`（`CAPABILITY_UNSUPPORTED`） | 升级为静态依赖图（能力反转） |
| `group dispatch` | 单步派发，不推进 | 保留；**同受组级准入约束**（§7.6） |
| `group plan` | 只读准入视图，`dispatchable_now` **恒为空**（实现缺陷） | 修正；新增"预览未冻结计划"形式 |
| `capabilities.not_supported` | 含 `automatic_dag_scheduling`、`parallel_writers`、`automatic_merge` | 移除 `automatic_dag_scheduling`；`automatic_merge` 改写为"目标主分支自动合并" |
| 预算 | 文档称"recorded, not enforced" | **仅组级生效**；任务级维持现状，不得改写为"已全部强制执行" |

### 2.3 前置修复/回归清单（P0）

| 编号 | 项 | 类型 | 验收 |
|---|---|---|---|
| P0-1 | F1 rebind 重验 | 回归 | `pytest tests/test_v121_rebind_revalidate.py` |
| P0-2 | F2 预算豁免与责任分派 | 回归 | `pytest tests/test_v121_budget.py` |
| P0-3 | F7 越界全量 + 保护路径优先 | 回归 | 现有 `scope()` 测试 |
| P0-4 | F8 安装同源 | 补齐 | `tests/test_distribution.py` + 安装文档三面核对 |
| P0-5 | F5 `auth_mode` | 新增 | §5.5 语义测试 |
| P0-6 | F3 `error_class` | 新增 | 测试失败 vs 检查无法执行可分辨 |
| P0-7 | `capabilities` 同步 | 新增 | `tests/test_contract_artifacts.py` |

**F1/F2/F7 不重写**：把它们列为"待开发"是基于旧摘要的误判。

---

## 3 总体架构与进程模型

```
宿主 / 用户
   │  zoe group start GID        （冻结已完成；本命令只做预检 + 启动 runner + 返回）
   ▼
group runner（受管进程 zoe internal-group <gid> <lease_id>）
   │  持有 maintenance SH + groups/<gid>/.runner EX
   │  tick（无 LLM）：对账 → 集成 → 组级检查 → 派发（受预算门禁）→ 派生状态 → 落进度
   ▼
成员任务 supervisor（既有路径，每任务一个 writer）
   ▼
编码 Agent 进程树（既有 local-process）
```

三条不可让渡：

1. **runner 不是 daemon**：无 `--daemon`，不写 systemd/launchd/计划任务，不装定时器。
   进程退出＝推进停止。宿主若声称"会话结束后继续推进"，必须有真实存活的 runner，
   否则只能写"未在推进"并交出 `event_cursor`。
2. **runner 不持有第二个写者**：成员实现永远由 `_pipeline.start` 派发给任务 supervisor。
3. **runner 不常驻 LLM**：语义判断要么在任务内，要么在 v1.4.0 的显式审查阶段。

---

## 4 数据模型

### 4.1 schema v4：不新增表

`groups`、`operations`、`events`、`meta` 均复用，DDL 不变，只做语义版本闸门：

```python
SCHEMA_VERSION = 4          # 原 3
PROTOCOL_VERSION = "5"      # 原 "4"
```

理由：组记录 v3 的 `definition` 让 v1.2 二进制把 `ordered_items` 读成空列表并显示"0 项"，
属**静默误读**。抬到 `user_version=4` 后，v1.2 命中既有分支返回 `SCHEMA_UNSUPPORTED`。

### 4.2 组记录 v3：不可变定义与运行时事实分离

```json
{
  "record_version": 3,
  "group_id": "group-<16hex>",
  "project_id": "demo",
  "state": "READY",
  "revision": 1,
  "op_seq": 0,
  "plan_digest": "<sha256>",
  "design_digest": "<sha256>",
  "base_sha": "<40hex>",
  "delivery_mode": "local_artifact",

  "definition": {
    "title": "导出能力交付",
    "integration_branch": "zoe/group-<16hex>",
    "requirements": [{"id": "REQ-01", "statement": "...", "source": "export.md#3.2"}],
    "members": [
      {
        "task_key": "export-contract",
        "title": "导出接口",
        "spec_sha256": "<sha256>",
        "spec_snapshot": "specs/export-contract.json",
        "depends_on": [],
        "requirement_ids": ["REQ-01"],
        "acceptance_ids": ["AC-01"],
        "expected_paths": ["src/export", "tests/export"],
        "task_type": "api",
        "requested_profile": null,
        "check_ids": ["export-contract-test"]
      }
    ],
    "group_checks": [{"check_id": "integration", "command_digest": "<sha256>", "quick": true}],
    "budget_caps": {"max_starts": 12, "max_wall_seconds": 14400}
  },

  "runtime": {
    "integration_sha": "<40hex>",
    "lease": null,
    "policy_snapshot": {"policy_revision": "<16hex>", "routes": {"export-contract": {"combination_id": "cmb-..."}}},
    "budget": {"starts_reserved": 0, "starts_released": 0, "release_basis": [], "first_running_at": null, "finalized_at": null},
    "members": [{"task_key": "export-contract", "task_id": null, "validated_sha": null,
                 "integrated_sha": null, "invalidated": false, "last_block": null}],
    "checks": [{"check_id": "integration", "subject_sha": null, "status": null, "evidence_ref": null, "at": null}],
    "blockers": []
  },
  "created_at": 0.0,
  "updated_at": 0.0
}
```

### 4.3 `plan_digest` 只覆盖不可变投影

```text
plan_digest = sha256(canonical({
  schema_version, project_id, base_sha, design_digest, delivery_mode,
  requirements[],                                  # id/statement/source
  members[].{task_key,title,spec_sha256,depends_on,requirement_ids,
             acceptance_ids,expected_paths,task_type,requested_profile,check_ids},
  group_checks[].{check_id,command_digest,quick},
  budget_caps.{max_starts,max_wall_seconds}
}))
```

**不进入摘要**：`runtime.*` 全部（task_id / validated_sha / integrated_sha / invalidated /
lease / integration_sha / checks 结果 / 预算计数）、`op_seq`、`revision`、时间戳。

因此运行中计数变化**不会**改变 `plan_digest`；只有"计划本身变了"才 `revision+1`。
`revision` 与 `plan_digest` 的一致性由 `group status` 校验：
`revision` 递增必须伴随 `plan_digest` 变化，否则视为实现缺陷（测试 T31）。

### 4.4 成员 Spec 必须在创建组时冻结

只记 `spec_file` 路径不够：下游任务延迟创建时可能读到被修改的规格。

- 创建组时逐成员读取 `spec_file`，经 `_contracts.load_spec` 校验后，
  以**相同结构**写入 `ZOE_HOME/groups/<gid>/specs/<task_key>.json`
  （`{"text","contract_version","document","verification_semantics"}`），
  并记录 `spec_sha256 = sha256(text)`。
- 成员任务创建时把该**快照路径**作为 `spec_file` 传入 `task create`：
  `_contracts.load_spec` 会对快照重新校验，行为与直接传仓内 spec 一致，
  但内容已被冻结，仓库后续改动不影响本组。
- 创建成员任务时若 `sha256(快照 text) != definition.spec_sha256` ⇒ `SPEC_CHANGED` 阻塞。

### 4.5 产物布局

```text
ZOE_HOME/
  tasks/<task_id>/                     # 既有，不改
  groups/<group_id>/
    plan-r001.json                     # 冻结计划（definition + digest）
    baseline.json                      # 创建时的运行时/环境事实
    design-manifest.json               # 设计文件清单 + 每文件 sha256
    specs/<task_key>.json              # 成员 Spec 冻结快照（§4.4）
    routes/<task_key>-a001.json        # 组级路由预演（RouteDecision v2 同构）
    integration/<op_id>.json           # 集成账本（intent/prepared/done）
    integration/<op_id>/wt/            # 每次集成独立 worktree
    integration/<op_id>/conflict/      # 冲突诊断（不破坏 integration_sha）
    checks/<subject_sha>.json          # 组级检查结果
    reports/progress.md                # 可再生视图
    reports/delivery.md                # 可再生视图
```

`groups/<gid>/` 与 `tasks/<id>/` 平级：权限、备份、清理策略可直接复用（**不是**为了避让
`projects/<pid>.json`，二者本不冲突）。

### 4.6 原子性与幂等

- 不可变产物：`_util.atomic()`（临时文件 + fsync + 原子更名）。
- **每个阶段先写产物文件，再在事务里提交引用与事件**；恢复时以文件补 DB，
  两者不一致即阻塞（不猜）。
- request_id：`"grp-<gid>-<action>-r<revision>-s<seq>"`，`seq` 取自组记录 `op_seq`
  （同事务 +1）。同 ID 同输入返回原结果；不同输入 `REQUEST_CONFLICT`。
  这样第二次 `pause`/`resume` 不会重放第一次的结果。
- Git 副作用不能与 SQLite 同事务：一律「写意图→执行→对账→完成」（§8）。
  **不得**因为代码写在同一函数里就声称原子提交。

### 4.7 成员状态是派生值

**持久事实只有 5 个**：`task_id`、`validated_sha`、`integrated_sha`、`invalidated`、`last_block`。
`member_state` 是派生视图，不落盘。判定**自上而下，首个命中生效**：

| 优先级 | 状态 | 判定 |
|---|---|---|
| 1 | `INTEGRATED` | `integrated_sha is not None and not invalidated` |
| 2 | `INVALIDATED` | `invalidated`（需重验，出口见 §6.4） |
| 3 | `BLOCKED` | 有 task 且 `task.state == BLOCKED`；或 `validated_sha` 存在但证据校验失败 |
| 4 | `CANCELLED` | 有 task 且 `task.state == CANCELLED` |
| 5 | `PAUSED` | 有 task 且 `task.state in (PAUSED, PAUSING)` |
| 6 | `VALIDATED` | `validated_sha == task.candidate_sha` 且 `task.state == REVIEW_READY` 且证据有效 |
| 7 | `EXECUTING` | 有 task 且 `task.state == RUNNING` |
| 8 | `READY` | 无 task，且全部依赖已 `INTEGRATED` 且未失效 |
| 9 | `WAITING` | 无 task，且存在未满足依赖 |

`CANCELLED` 的成员**不满足**下游依赖（下游保持 `WAITING`，最终由组 `BLOCKED` 暴露）。

---

## 5 计划契约与校验

### 5.1 `group create` 请求（`schema_version: 2`）

```json
{
  "schema_version": 2,
  "request_id": "req-1",
  "authorization_ref": "owner:me@2026-10-30",
  "project_id": "demo",
  "title": "导出能力交付",
  "design_files": ["docs/design/export.md"],
  "delivery_mode": "local_artifact",
  "base_ref": "HEAD",
  "budget": {"max_starts": 12, "max_wall_seconds": 14400},
  "requirements": [{"id": "REQ-01", "statement": "支持按条件导出", "source": "export.md#3.2"}],
  "members": [
    {
      "task_key": "export-contract",
      "title": "导出接口",
      "spec_file": "specs/export-contract.json",
      "depends_on": [],
      "requirement_ids": ["REQ-01"],
      "acceptance_ids": ["AC-01"],
      "expected_paths": ["src/export", "tests/export"],
      "task_type": "api",
      "requested_profile": null,
      "check_ids": ["export-contract-test"]
    }
  ],
  "group_checks": ["integration"]
}
```

- `delivery_mode` 必须为 `local_artifact`；`pull_request` 直接
  `CAPABILITY_UNSUPPORTED`，`details.kind="group_pr_unsupported"`（§9.4）。
- `expected_paths` **不是授权**（见 §5.2-4）。
- `budget.max_starts` 必填（不接受自动推算）；`max_wall_seconds` 可为 `null`，须在响应 `notes` 写明。
- `schema_version: 1`（`ordered_items`）保持可用：不能带 `depends_on`，不能 `start`。

### 5.2 两步校验：先固定输入，再做纯校验

原稿把"读 Git/读设计文件/解析 Spec/解析符号链接"称作纯函数，是错的。拆成两步：

```text
resolve_plan_inputs(req, config) -> snapshot      # IO：读、校验、固定
    - base_sha = git rev-parse --verify <base_ref>^{commit}
    - design_manifest = [{path, sha256(内容)}]，按 path 排序
    - 逐成员读 spec_file，_contracts.load_spec 校验，得到 spec_text/spec_sha256
    - 逐成员包 resolve() 规范化路径（含符号链接）
    - 项目 allowed_paths 规范化
    - 环境快照：版本/schema/protocol/平台/适配器能力/路由候选（只读）

validate_plan(snapshot) -> plan | Error            # 纯函数，无 IO，可表驱动测试
```

`validate_plan` 规则：

| # | 规则 | 失败码 |
|---|---|---|
| 1 | `task_key` 唯一且 `safe_id`；`title`/`spec_sha256` 非空 | `INPUT_INCOMPLETE` |
| 2 | `depends_on ⊆ 全部 task_key`；无自环；Kahn 拓扑排序成功 | `INPUT_INCOMPLETE` + `details.kind=cycle` |
| 3 | 每个 `REQ` 至少被一个成员覆盖 | `INPUT_INCOMPLETE` + `details.kind=coverage_missing` |
| 4 | 每成员 spec 的 AC 非空；`check` 型 AC 的 `check_id` 属于 `member.check_ids ∪ group_checks`；`manual` 型 AC 必须显式列出 | `INPUT_INCOMPLETE` |
| 5 | 每成员 spec 的 `allowed_paths` 只能收紧项目 `allowed_paths`（复用 `_contracts._prefix_covered`） | `POLICY_DENIED` |
| 6 | `expected_paths`：仓内相对路径；拒绝绝对路径/盘符/父目录逃逸；解析符号链接后仍须落在**某个**项目 `allowed_paths` 目录边界内 | `INPUT_INCOMPLETE` / `POLICY_DENIED` |
| 7 | Windows：路径比较前按大小写折叠；报告里保留原始写法 | — |
| 8 | `budget.max_starts` **必填**正整数；`max_wall_seconds` 可为 `null`（不限，报告须写"未设时长上限"）。**不接受按规模自动推算** | `INPUT_INCOMPLETE` |
| 9 | `plan_digest` 按 §4.3 投影计算 | — |

三条与本轮评审对应的明确决定：

1. **路径包含按目录边界判定**，不做字符串集合交集。`src/export` 不包含 `src/exported`；
   实现统一走 `Path.resolve()` + `is_relative_to()`，不用 `startswith`。
2. **不生成共享路径隐式依赖边**。v1.3 全局串行，隐式加边方向不明且可能造环。
   并行版（v1.4+）改用显式资源互斥约束，届时另立契约。
3. **`expected_paths` 的定性：规划提示，不参与授权执行**。它在 `group plan` 中用于
   提示"这些成员会碰同一片目录"，在报告中用于说明预期范围。
   真正生效的强制范围仍由既有机制决定：项目 `allowed_paths` ∩ 成员 spec 的
   `allowed_paths`（只能收紧），事后由 `scope()` 出越界清单。
   —— 二者不混用，避免出现"规划路径即授权"的隐性扩权。

### 5.3 结构覆盖 ≠ 语义实现

`coverage` 校验只证明"每个 REQ 都有责任成员、每个必需 AC 都有验证方法与责任成员"。
它**不证明**实现符合设计语义。因此：

- `group status` / `group report` 必须写"结构覆盖已通过"，不得写"需求已满足"；
- 语义判断只有两个合法来源：任务的 `check` 型 AC 实际执行通过，或 v1.4.0 的设计审查；
- v1.3 报告中"设计一致性"一行固定为 `NOT_RUN`。

### 5.4 检查结果分类（F3 落地）

`validation.json` 行结构不变，新增可选字段：

```json
{"id": "unit", "status": "FAIL", "exit_code": null,
 "error_class": "environment", "error": {"code": "ENVIRONMENT_ERROR", "message": "..."}}
```

| `error_class` | 含义 | 处理 |
|---|---|---|
| `test` | 检查执行了并失败 | 计入修复预算，走实现修复 |
| `environment` | 检查无法执行（缺二进制/PATH/认证） | 预检类，不计修复预算 |
| `timeout` | 执行超时 | 记为 `FAIL` 并计入预算（不得当作 PASS） |
| `policy` | 策略拒绝（保护路径、授权） | 阻塞，需人决策 |

`status` 仍只有 `PASS/FAIL/NOT_RUN`。**不新增 `NOT_VERIFIED`**：证据契约已冻结，
第四态会让所有既有消费者需要升级。

### 5.5 `auth_mode`（F5 落地）

profile 新增可选字段 `auth_mode ∈ {cli_login, api_key, unknown}`，缺省 `unknown`。

| 取值 | `credential_refs` | 预检 | 可派发 | 报告措辞 |
|---|---|---|---|---|
| `cli_login` | 可空 | `doctor` 只能给出 `login_state ∈ {unknown, present, absent}`；`present` 必须由一次真实 `agent probe` 成功支撑，否则写 `unknown` | 是 | "CLI 登录态：unknown/present" |
| `api_key` | **非空**；缺失即 `AUTH_REQUIRED`（注册期与 `start` 前各判一次） | `credential_present` 只回答环境变量在不在 | 是（缺失时否） | "凭据已注入（值不展示）" |
| `unknown` | 任意 | 无 | 是 | **不得**写"已认证"，只能写"认证态未知" |

- 显式声明 `required_credential_refs` 时不得静默降级为可选。
- `auth_mode` 属于 profile，不属于任务；`agent reassign` 后按新 profile 重新判定。
- `AGENT_AUTH_REQUIRED` 的恢复路径不变：修环境 → `resume`，不重写代码。

---

## 6 状态机

### 6.1 组状态（与任务同名同义）

| 状态 | 含义 | 允许后续 |
|---|---|---|
| `READY` | 计划已冻结，无 runner 或待启动 | `RUNNING`/`CANCELLED` |
| `RUNNING` | 有 runner 且存在可推进工作 | `BLOCKED`/`PAUSED`/`REVIEW_READY`/`CANCELLED` |
| `BLOCKED` | 无可推进工作且存在明确阻塞 | `RUNNING`/`PAUSED`/`CANCELLED` |
| `PAUSED` | 不再派发；活跃成员按 drain 策略处理 | `RUNNING`/`CANCELLED` |
| `REVIEW_READY` | 全部成员已集成，必需组级检查 PASS | `DONE`/`RUNNING`/`CANCELLED` |
| `DONE` | 对最终 `integration_sha` 完成全部必要验收 | 终态 |
| `CANCELLED` | 流程终止（可含 external close 原因） | 终态 |

无 `DRAFT`：`group plan` 是无状态预览。**部分阻塞不算组阻塞**：只要还有独立成员可推进，
组保持 `RUNNING`，`progress.blocked` 列出数量与原因。

### 6.2 迁移合法性

| 从 | 到 | 前置条件 |
|---|---|---|
| `READY` | `RUNNING` | 租约获取成功；预检通过（策略/能力/工作区/`.gitattributes` 过滤器） |
| `RUNNING` | `REVIEW_READY` | 全部成员 `INTEGRATED` 未失效 + 必需组级检查在最终 `integration_sha` 上 `PASS` |
| `RUNNING` | `BLOCKED` | 无可推进成员且存在 `blockers` |
| `RUNNING` | `PAUSED` | 显式 `pause`（含 drain 确认） |
| `PAUSED` | `RUNNING` | 显式 `resume` + 重新预检 + 重新对账 |
| `REVIEW_READY` | `RUNNING` | 证据失效（`stale` 检查 / 成员失效），需重新集成或重跑检查 |
| `REVIEW_READY` | `DONE` | `accept` 通过 §9.4 全部门禁 |
| 任意非终态 | `CANCELLED` | `cancel`，且已确认无活跃执行 |

`DONE`/`CANCELLED` 不得原地重开；继续工作新建组，`follow_up_of` 记录旧组并以其
`integration_sha` 为 `base_ref`。

### 6.3 成员交付分支（`delivery_mode="group_member"`）

- 只有带 `group_id`/`item_id` 且该成员存在于组 `definition` 时才接受。
- 流转：`prepare → implement → verify → (REVIEW_READY, phase=acceptance)`；
  **跳过** `publish`/`ci`，不推送、不建 PR。
- `task accept` 对成员任务返回 `STATE_CONFLICT`，`details.kind="group_member"`，
  `hint="由 group accept 统一收口"`。
- 成员创建时冻结 `consumed_input_sha = 组当时 integration_sha`（首成员为 `base_sha`），
  并断言该 SHA 是全部上游 `integrated_sha` 的后代（§5.2 依赖序已保证）。

### 6.4 已集成成员冻结（取代原"失效传播"）

v1.3 的硬规则：

| 成员状态 | `reassign` / `rebind` / `revise` | 说明 |
|---|---|---|
| `INTEGRATED` | **拒绝**：`STATE_CONFLICT` + `details.kind="integrated_member"` | 产物已进入 `integration_sha`，原地变更会让集成历史与证据失真。需要修改 ⇒ 新建关联组 |
| `VALIDATED` / `INVALIDATED` | 允许（既有机制） | `rebind` → `resume` → 重跑检查；通过后清除 `invalidated` |
| `WAITING` / `READY` / 无 task | 允许（改 plan 需 `revision+1`） | — |

`invalidated` 的**唯一置位来源**（v1.3）：证据失效（`evidence.json` 缺失/被改）、
策略变更（`policy_revision` 不匹配）、重验失败。三者都有明确出口（§6.4 第 2 行）。
因此 v1.3 **不存在**"标记失效后无路可走"的状态。

---

## 7 组推进器

### 7.1 启动握手（复用既有实现，不复制形状）

`group start` 与 `task start` 共用同一序列（`_pipeline._start_locked` 的组版本）：

```text
1  maintenance SH（与 state migrate 互斥）
2  groups/<gid>/.runner EX, blocking=False
   └─ 失败 ⇒ SLOT_BUSY, details.kind="group_runner", 附现有 lease 快照
3  tx: lease = {lease_id: uuid4, owner, state:"spawn_intent", pid:null, start_token:null}
       group.state = RUNNING；op_seq 不变
4  spawn `zoe internal-group <gid> <lease_id>`（不建自有进程树）
5  重读记录：子进程已 ACK ⇒ lease.state="spawn_observed" + pid/start_token 落盘
6  父释放 runner EX 与 maintenance SH
7  子重新获取 maintenance SH + runner EX
8  子 CAS 校验 lease_id + pid identity 三方一致，才进入 tick
```

四个必须写清的时点（对应评审第 4 项）：

| 问题 | 答案 |
|---|---|
| 父何时释放 | 步骤 6：`spawn_observed` 落盘之后 |
| 子何时确认接管 | 步骤 7–8：重取锁 + CAS 通过之后 |
| 第二个 `group start` 如何被挡 | 步骤 2 抢不到 runner EX ⇒ `SLOT_BUSY`；若父已释放而子未取（空窗），记录中 `lease.state == "spawn_observed"` 且 `lease.pid` 存在 ⇒ 新的 `group start` **必须先对账**：pid 可观测 ⇒ 拒绝（`SLOT_BUSY`）；不可观测 ⇒ `BLOCKED/RUNNER_UNKNOWN`。空窗因此不可被第二个 runner 利用 |
| 父在各阶段崩溃如何恢复 | `spawn_intent` 且无 pid ⇒ 视为未启动，可重放一次；`spawn_observed` 且 pid 不可观测 ⇒ `BLOCKED/RUNNER_UNKNOWN`，交人；`spawn_observed` 且 pid 可观测 ⇒ 只读观察，不抢锁 |

**`lease_id` 是接管代数**，不是 `meta.epoch`。`meta.epoch` 只在 `state migrate/restore` 时变化，
无法区分同一数据库生命周期内的两次接管。runner 每次写前校验
`group.runtime.lease.lease_id == 自己`，不等即 `STATE_CONFLICT`
（`details.kind="lease_stale"`）并退出——旧 runner 的写入一律拒绝。

实现备注：`_store.lock_path()` 目前只特判 `tasks/` 前缀，需增加 `groups/<gid>/<role>` 分支
（落到 `ZOE_HOME/groups/<gid>/.<role>.lock`），并让 `_migrations._lock` 用同一解析，
否则 `state migrate` 与 runner 不会真正互斥。

### 7.2 tick（全组串行 + 预算只门禁派发）

```python
def tick(store, gid):
    g = load(store, gid)
    assert g.runtime.lease.lease_id == my_lease and lease_is_current(g)

    # A 对账：永远执行，不受预算/暂停影响
    reconcile_members(store, g)                 # 既有 reconcile
    reconcile_integrations(store, g)            # §8.3

    # B 集成已完成候选：永远执行（收尾不受派发预算限制）
    for m in members_in_topological_order(g):
        if member_state(m) == "VALIDATED": integrate(store, g, m)

    # C 组级检查：全部集成后执行
    if all_integrated(g): run_group_checks(store, g)

    # D 派发新启动：唯一受预算与槽位门禁的步骤
    gate = dispatch_gate(store, g)              # 见下
    if gate.ok:
        for m in ready_members(g)[:gate.slots]: dispatch(store, g, m)

    # E 派生状态与进度
    g.state = derive_group_state(g)
    save_group(g); write_progress(g)
    return CONTINUE if has_dispatchable_or_active(g) else STOP
```

**串行必须算两个量**（评审第 3 项）：

```python
group_busy = count(m for m in members(g)
                   if member_state(m) in ("EXECUTING",) or is_reserved(m))
group_free = max(0, 1 - group_busy)             # 本组最多一个活跃或已预留
global_free = slots(store, config)["free"]      # 既有快照
slots_available = min(group_free, global_free)  # 两者取小
```

只算 `min(1, global_free)` 只能保证"一轮最多一个"，上一轮成员仍在跑时下一轮会再启一个。
`is_reserved(m)` 覆盖"已写派发意图但 run 未确认"的窗口，防止重复派发；
它的判据是成员任务的 `dispatch_state ∈ {spawn_intent, spawn_observed}`（既有字段，不新增）。

**预算门禁只作用于 D**（评审第 3 项的核心）：

```python
def dispatch_gate(store, g):
    if starts_billable(g) >= g.definition.budget_caps.max_starts:
        return Blocked("GROUP_BUDGET_EXHAUSTED", kind="starts")
    cap = g.definition.budget_caps.max_wall_seconds      # 可为 None
    if cap is not None and wall_seconds(g) >= cap:
        return Blocked("GROUP_BUDGET_EXHAUSTED", kind="wall_seconds")
    if policy_changed(g):  return Blocked("POLICY_CHANGED")
    if slots_available(g) == 0: return Deferred()      # 不是阻塞，下一 tick 再来
    return Ok(slots_available(g))
```

**预算耗尽不阻止对账、集成、组级检查与验收。** 反例（原稿的漏洞）：`max_starts=3`，
第三次成功启动后计数正好为 3，下一 tick 若在 A/B/C 之前拦下，就永远无法集成和验收。

### 7.3 预算计量（可实现的诚实口径）

| 字段 | 计量 | 语义 |
|---|---|---|
| `starts_reserved` | 每次成功预留启动额度 +1（与 run 预留同事务），**单调不减**：释放不回退它 | **计费启动额度**，不是"实际 Agent 进程启动数"；单调才可作为审计事实 |
| `starts_released` | 启动在产生 Agent 进程前失败（run 无 `launch_stage`）时 +1，附 `release_basis`（run_id + 证据引用） | 与 `budget_waivers` 同构：必须有持久化证据才释放。一次释放只把 billable 减一——同时 `reserved -= 1` 会把名额退两次，`max_starts` 因此可超发 |
| `starts_billable` | `starts_reserved - starts_released` | `max_starts` 的比较对象 |
| `wall_seconds` | `(finalized_at or now) - first_running_at` | **含机器休眠与等待**的墙钟时长 |

三点必须如实写进文档与报告：

1. `starts_billable` ≠ 进程实际启动数；后者以每个 run 的 `pid`/`launch_stage` 为证据，
   报告分列"计费额度"与"实际进程启动"。
2. 时间预算是**墙钟**时长，`wall_seconds_inclusive`；**不承诺排除休眠**。
   原稿"累计活动时长、排除休眠"无法可靠实现，故撤回该承诺。
3. 时间预算耗尽：停止启动新成员，仍允许集成已完成的候选、组级检查，
   以及人工触发的 `group accept`（人工命令不受 runner 预算约束）；
   若还有未启动成员 ⇒ `BLOCKED/GROUP_BUDGET_EXHAUSTED`，`kind="wall_seconds"`。

### 7.4 暂停 / 恢复 / 取消

| 命令 | 行为 | 响应必须携带 |
|---|---|---|
| `group pause ID [--stop]` | 默认 drain：不再派发，活跃成员跑到安全点 | `active_run_ids`、`draining=true`、`state` |
| `group pause ID --stop` | 对活跃成员调用既有 `stop`，确认进程树消失后才置 `PAUSED` | 每成员停止确认 |
| `group resume ID` | 重新预检 → 重新对账 → 接管 `lease_id` | `preflight`、`reconciled`、`lease_id` |
| `group cancel ID` | drain/stop 后确认无活跃写者，置 `CANCELLED` | 停止确认列表 |
| `group cancel ID --external` | 复用 `close-external` 语义 | `claim: verified\|asserted`、`external_ref`、`actor` |

"暂停确认" ≠ "所有子进程已停止"，两者分别报告。

### 7.5 恢复

```text
1 lease.lease_id 的进程身份是否可观测（pid + start_token）
2 成员任务是否有活跃 run
3 是否有未完成集成（intent / prepared）        ← §8.3
4 证据引用是否齐全（文件在盘、subject_sha 匹配）
5 策略是否变化（policy_revision）
6 全部确定 ⇒ 计算可推进集合；任一 unknown ⇒ BLOCKED/RUNNER_UNKNOWN
```

身份未知不得靠心跳过期重启。

### 7.6 组级准入是唯一入口（评审第 4 项）

三条路径都必须经过同一函数 `group_admission(store, task)`：

| 入口 | 行为 |
|---|---|
| runner `tick` | 直接调用 `group_admission` + `dispatch_gate` |
| `group dispatch ID --item K` | **手动推进**：同样调用 `group_admission`，同样受串行槽位、预算、策略检查约束；不满足即拒绝，没有豁免 |
| `task start` / `task resume`（成员任务） | 由 `_pipeline.start` 调用 `group_admission`；组为 `PAUSED`/`CANCELLED`/`BLOCKED` 时拒绝（`STATE_CONFLICT`, `details.kind="group_state"`） |

`group_admission` 判定：组成员身份 → 组 `state` 允许派发 → 该成员处于
`READY`/`PAUSED`（可恢复） → 串行槽位空闲 → `starts_billable` 未超 → 策略未变。

`group dispatch` 是"人来代替 tick 按下派发按钮"，**不是约束绕过入口**。
需要绕过约束时只有一条合法路径：改 plan（`revision+1`）或改 profile（`rebind`），
两者都留痕。

---

## 8 串行集成

### 8.1 初始条件

- 固定 `base_sha`；创建 `zoe/group-<16hex>` 集成分支（**不写 main**）。
- 每次集成使用**独立** worktree：`groups/<gid>/integration/<op_id>/wt`。
- 推送前复用 `_drift`：目标分支漂移 ⇒ `REMOTE_DRIFT` + `details.drift_kind`；不 force push。

### 8.2 三段账本（评审第 5 项）

集成前必须同时满足（任一不满足则不发起集成，组 `BLOCKED`）：

1. `m.validated_sha == task.validated_sha == task.candidate_sha`；
2. `_evidence.verify(...)` 通过，且没有任何 `FAIL`（诊断证据不可集成）；
3. `task.policy_revision == 当前 policy_revision`——策略已变则证据不再是"当前策略下的证据"；
4. `task.state == REVIEW_READY`，`task.group_id == group_id`。

原稿在更新 ref **之后**才写 `result_sha`，崩溃后记录里根本没有 `result_sha`，
恢复规则"`ref == result_sha` 且 `op=intent`"永远无法命中。改为：

```text
[1] intent    持久化：{op_id, group_id, member, base_sha: g.integration_sha,
                        candidate_sha, seq}
              写文件 integration/<op_id>.json，再提交 operations 行（status="intent"）

[2] prepared  在隔离 worktree 完成 merge 与快速检查后，先落盘再记账：
              {result_sha, merged_candidate_sha, quick_checks[], tree_fingerprint}
              写文件（原子）→ 提交 operations 行（status="prepared"）
              ★ 必须发生在更新 ref 之前

[3] ref        git update-ref refs/heads/<integration_branch> <result_sha> <base_sha>
               非 fast-forward / 旧值不符 ⇒ 不重试、不强制，转 §8.3 对账

[4] done       DB 事务：g.runtime.integration_sha = result_sha
                       member.integrated_sha = result_sha
                       operations.status = "done"
                       写 group 事件（含 op_id、result_sha）
```

账本状态机：`intent → prepared → done`，旁支 `intent|prepared → failed`（快速检查失败）
与 `prepared → conflict`（merge 冲突）。**任何阶段失败都保留 worktree 与文件**。

强制规则：

| 规则 | 说明 |
|---|---|
| 每 op 独立 worktree | 不复用上一个 op 的目录，避免半成品污染 |
| 快速检查失败 | 保留 `prepared` 与 worktree；op=`failed`；组 `BLOCKED/INTEGRATION_CHECK_FAILED`；`integration_sha` 不变；重试需新 `op_id`（`op_seq` +1） |
| 重试前探可达性 | `git merge-base --is-ancestor <candidate_sha> <ref>` 为真 ⇒ 已合并，不再 merge，直接走 `prepared`→`done`（若 `result_sha` 未知则阻塞） |
| 清理失败 | 只记 `cleanup_pending`，**不改变交付结论**，但报告中必须列出 |
| 不允许 patch 兜底 | 统一 `git merge --no-ff`，保留 `candidate_sha` 可达性 |

### 8.3 崩溃对账

| ref 观察 | 账本 | 判定 | 动作 |
|---|---|---|---|
| `== result_sha` | `prepared` | 已执行 ref、未记账 | 补记 `done`，**不重复 merge** |
| `== result_sha` | `intent` | 不可能（prepared 必先于 ref） | `INTEGRATION_UNKNOWN`，交人 |
| `== base_sha` | `prepared` | 未更新 ref | 允许重试 `update-ref`（幂等），再转 `done` |
| `== base_sha` | `intent` | 未执行 | 允许重放一次（新 worktree，同 op_id） |
| 第三种值 | 任意 | 不可判定 | `INTEGRATION_UNKNOWN`，交人 |
| `candidate 已可达` | `intent` | 已被合并但未记账 | 转 `prepared`（需能从文件补 `result_sha`，否则阻塞） |

文件与 DB 冲突时以**文件**为准：文件存在 `prepared` 而 DB 是 `intent` ⇒ 补 DB；
DB 是 `done` 而文件缺失 ⇒ 阻塞（不得假装无集成）。

### 8.4 冻结与恢复出口（取代失效传播）

原稿的"上游修复 → 下游 `invalidated`"没有定义如何重新验证成员、何时清除标记，
会造永久阻塞。v1.3 直接取消该路径：

- **已集成成员禁止原地变更**（§6.4）：上游不可能在集成后"再修复"。
- **未集成成员**的 `invalidated` 只有三个来源（证据失效 / 策略变更 / 重验失败），
  出口统一为 `task rebind` → `resume` → 重跑检查 → 清除标记。
- 因此 v1.3 **没有"下游失效解除"这个流程**，也不需要保守全量重跑成员：
  组级检查只在 `integration_sha` 变化时重跑。
- 需要"改已经交付的东西"⇒ 新建关联组（`follow_up_of`），
  以当前 `integration_sha` 为 `base_ref`，重新走一遍完整链路。

---

## 9 组级检查与收口

### 9.1 三层验证（v1.3 落地两层）

| 层 | 输入 | 输出 | 版本 |
|---|---|---|---|
| 任务检查 | 冻结 spec 的 AC、候选 SHA、范围与检查配置 | 单任务 `PASS/FAIL/NOT_RUN` | 已有 |
| 集成验收 | 最终 `integration_sha`、必需组级检查 | 组级交付证据 | v1.3.0 |
| 设计一致性审查 | 冻结设计、最终 diff、需求映射 | 逐条偏差 | **v1.4.0** |

v1.3 的 `group report` **必须**固定输出一行：
`设计一致性审查：NOT_RUN（v1.3 不包含该层，交付不等于符合设计语义）`。

### 9.2 组级检查

```json
{
  "schema_version": 1,
  "group_id": "group-...",
  "subject_sha": "<integration_sha>",
  "design_digest": "<sha256>",
  "plan_revision": 1,
  "policy_revision": "<16hex>",
  "checks": [
    {"check_id": "integration", "status": "PASS", "exit_code": 0,
     "command_digest": "<sha256 of argv>", "log_ref": "runs/.../log", "at": 0.0}
  ],
  "workspace_fingerprint": {"manifest_hash": "<sha256>", "head_sha": "<40hex>"}
}
```

- `status` 三值同任务检查；`NOT_RUN` 不计通过。
- 只在 `subject_sha` 上有效；`integration_sha` 一变，旧结果 `stale`。
- `workspace_fingerprint` 是验收时要复核的对象（§9.4），不是 commit SHA。
- 不改 `_evidence` 的 `schema_version: 1` 结构（已冻结）；组检查用独立文件、同名字段。

### 9.3 `REVIEW_READY` 条件

1. 全部成员 `INTEGRATED` 且未失效；
2. 全部必需组级检查在最终 `integration_sha` 上 `PASS`（`NOT_RUN`/`stale` 均不算）；
3. 无 `PROCESS_UNKNOWN`、无失效证据引用；
4. 人工验收项逐条列出（未记录即未通过）。

### 9.4 `DONE` 条件（仅 `local_artifact`）

| # | 条件 | 校验方式 |
|---|---|---|
| 1 | 组处于 `REVIEW_READY` | 状态检查 |
| 2 | `git rev-parse refs/heads/<integration_branch>` == `integration_sha` | 读实际 ref |
| 3 | 集成 worktree `HEAD` == `integration_sha` | `git rev-parse HEAD` |
| 4 | 集成 worktree 当前指纹 == 组级检查记录的 `manifest_hash` | 重新计算文件清单+内容哈希 |
| 5 | `evidence_refs` 全部存在、属于本组或本组成员、`subject_sha == integration_sha`、未被标 `stale` | 逐条校验归属与版本 |
| 6 | 请求含真实 `actor`、`decision`、非空 `evidence_refs`、`authorization_ref` | 请求必填校验 |

第 3–4 条纠正原稿的错误表述：**不能拿指纹去和 commit SHA 比较**；
要比的是"实际工作区身份（HEAD + 文件指纹）与验证时记录的身份"。

`decision` 两个取值的明确后果：

| `decision` | 结果 | 后续 |
|---|---|---|
| `accepted` | 上述 6 条全过 ⇒ `DONE`；任一不过 ⇒ 拒绝并返回未满足项 | 终态 |
| `rejected` | **不进入 `DONE`**，写 `acceptance` 记录（actor/原因/证据），组**保持 `REVIEW_READY`** | `next_actions`：修复（新组/成员 rebind）后重新集成与验收，或 `cancel` |

**PR 组不支持**：`group create` 对 `delivery_mode=pull_request` 返回
`CAPABILITY_UNSUPPORTED`（`details.kind="group_pr_unsupported"`，
`note="v1.3 仅支持 local_artifact 组验收"`）。用户若已在外部合并，
只能 `group cancel --external` 记录事实，**不得**伪装成 Zoe 已验收。

组 `DONE` 后，成员任务的显示不新增终态：`task status`/`group status` 派生
`closed_by_group: {group_id, integration_sha, accepted_at}`，
并以"已由任务组 GID 验收（abc1234）"呈现；成员自身仍是 `REVIEW_READY`，
`task accept` 继续拒绝（避免两套验收语义）。

### 9.5 外部收口

- `group cancel --external`：记录 `close_reason`、`external_ref`、`actor`、
  `claim: verified|asserted`、可选 `merged_sha`；`CANCELLED` + `block_code=CLOSED_EXTERNAL`。
- 响应固定 `accepted: false`；报告渲染"外部收口（未通过 Zoe 验收）"。
- 不计入成功率、不满足下游依赖、不允许 `resume`。

### 9.6 报告

`zoe group report ID --format markdown`（`--write` 才落 `reports/delivery.md`）：

1. 需求覆盖表（REQ → 成员 → AC → 结论），标注"结构覆盖，非语义验证"；
2. 成员与执行组合（`profile_id`/`combination_id`/`cli_version`/模型观测/`auth_mode`）；
3. `base_sha` → 各 `integrated_sha` → 最终 `integration_sha`；
4. 组级检查明细（含 `NOT_RUN`、`stale`、`cleanup_pending`）；
5. 阻塞历史与最小必要决策；
6. 计费启动额度 / 实际进程启动数 / 释放数 / 墙钟时长（含休眠）；
7. 边界声明：无沙箱、设计一致性审查 `NOT_RUN`、PR 组不支持、未运行项清单。

Markdown 是可再生视图；JSON/DB 是权威记录。

---

## 10 修复闭环与人工边界（v1.3.0）

| 问题 | 动作 | 启动 Agent |
|---|---|---|
| 登录 / PATH / 工具缺失 | `BLOCKED/ENVIRONMENT_ERROR`，修环境后 `group resume` 重新预检 | 否 |
| 策略变化 | 显式 `rebind`/`revise`，产物重验 | 按阶段 |
| 实现末尾越界 | 汇总全部越界路径；选"批准范围"或"撤销越界"后增量执行 | 是（不 skip） |
| 必需检查失败（`error_class=test`） | 附失败证据，计任务修复预算 | 是 |
| 检查无法执行（`error_class=environment`） | 归环境类，不计修复预算 | 否 |
| 集成冲突 | 保留隔离诊断；v1.3 **暂停等人工** | 否 |
| 集成快速检查失败 | `prepared` + worktree 保留，`INTEGRATION_CHECK_FAILED` | 否 |
| 已集成成员需修改 | 拒绝；新建关联组 | 否（新组内是） |
| `PROCESS_UNKNOWN` | 对账原执行 | 否 |
| 组预算耗尽 | 保存现场，`BLOCKED` | 否 |
| 设计重大歧义 | 生成一个聚合问题 | 否 |

组级修复在 v1.4.0 落地时必须是同组内的有界 repair run（`parent_finding`、受影响成员、
范围与预算），不开放无限动态任务图。新增需求必须 plan revise。

---

## 11 CLI 契约

### 11.1 命令

| 命令 | 作用 | 副作用 | 状态 |
|---|---|---|---|
| `zoe group plan --project P --request-file plan.json` | 预览未冻结计划：校验 + 路由预演 + 基线快照 | **无**（不建记录/任务/事件） | 新增形式 |
| `zoe group plan GID` | 已存在组的只读准入视图（`dispatchable_now` 修正后不再恒空） | 无 | 保留 |
| `zoe group create --request-file create.json` | 冻结计划（v3） | 写组记录、`plan-r001.json`、`specs/*` | 扩展 |
| `zoe group start ID --request-file start.json` | 启动 runner | 有界派发 | 新增（P3 门禁后） |
| `zoe group status ID [--json] [--refresh]` | 查询；`--refresh` 显式对账 | 默认只读 | 扩展 |
| `zoe group report ID [--format markdown\|json] [--write]` | 渲染交付/进度 | `--write` 写可再生报告 | 新增 |
| `zoe group dispatch ID --item K --request-file r.json` | 手动推进一个成员（同准入） | 建任务 + 启动 | 保留 |
| `zoe group pause ID --request-file pause.json [--stop]` | 停止新派发 | drain 或停止 | 新增（P3） |
| `zoe group resume ID --request-file resume.json` | 预检 + 接管 + 恢复 | 对账后派发 | 新增（P3） |
| `zoe group accept ID --request-file accept.json` | 授权验收（仅 local_artifact） | 门禁通过后 `DONE` | 新增（P4） |
| `zoe group cancel ID --request-file cancel.json [--external]` | 停止并关闭/外部收口 | 终态 | 新增（P3/P4） |
| `zoe internal-group GID LEASE_ID` | runner 入口 | 隐藏，不进 capabilities | 新增（P3） |

### 11.2 响应信封

```json
{
  "api_version": 2,
  "operation_id": "grp-<gid>-status-r3-s7",
  "evidence_refs": ["ev-..."],
  "group_id": "group-...",
  "revision": 3,
  "state": "RUNNING",
  "plan_digest": "<sha256>",
  "progress": {
    "members_total": 6, "integrated": 3, "executing": 1, "waiting": 1, "blocked": 1,
    "integration_sha": "abc1234", "starts_billable": 5, "max_starts": 12,
    "wall_seconds_inclusive": 3600, "max_wall_seconds": 14400
  },
  "blockers": [{"member": "export-ui", "code": "AGENT_AUTH_REQUIRED", "next_action": "resume"}],
  "next_actions": ["group status GID --refresh"],
  "event_cursor": "<meta.epoch>:<max_event_id>"
}
```

`event_cursor` 与 `watch --after` 同格式，宿主可直接续接。

### 11.3 错误码

复用为主：`INPUT_INCOMPLETE`(2)、`STATE_CONFLICT`(3)、`SLOT_BUSY`(3)、`POLICY_CHANGED`(5)、
`EVIDENCE_INVALID`(3)、`EVIDENCE_MISSING`(3)、`PROCESS_UNKNOWN`(6)、`REMOTE_DRIFT`(5)。

**新增 4 个**：

| 码 | 退出码 | 场景 |
|---|---|---|
| `INTEGRATION_CONFLICT` | 5 | 合并冲突（`details.conflict_dir`） |
| `INTEGRATION_CHECK_FAILED` | 5 | 集成快速检查失败（`prepared` 保留） |
| `INTEGRATION_UNKNOWN` | 5 | ref 处于第三种状态，不可判定 |
| `GROUP_BUDGET_EXHAUSTED` | 5 | `details.kind ∈ {starts, wall_seconds}` |

`details.kind` 取值：`cycle`、`coverage_missing`、`dependency_failed`、`group_runner`、
`lease_stale`、`group_member`、`integrated_member`、`group_pr_unsupported`、`legacy_group`、
`group_state`、`stale_checks`。

---

## 12 兼容、迁移与回滚

### 12.1 双仓版本

| 侧 | 变更 |
|---|---|
| `zoe-runtime` | v1.3.0：组命令、schema v4、`group_member`、`auth_mode`、`error_class` |
| `zoe-workflows` | 声明最低兼容版本与 required capabilities；新增 3 份 references |

`runtime-requirements.json` 的 `api_version=2` 不变（组命令是新增能力），
但 `group start/report/accept` 必须进入 required capability 清单，
否则旧 skill 会调用不存在的命令。

### 12.2 schema v3 → v4

```text
1  maintenance EX + control EX；存在 placeholder run 则拒绝
2  sqlite backup（既有 db.backup，不复制 WAL）
3  BEGIN IMMEDIATE
   - DDL 不变（无新表）
   - 校验既有 groups 记录：record_version ∈ {2,3}，不合法则中止
   - PRAGMA user_version = 4
   - meta.protocol_version = "5"
   COMMIT
4  integrity_check
5  重新校验全部项目配置
```

`SUPPORTED_TARGETS` 增加 `4`；v1.2.1 打开 v4 库返回 `SCHEMA_UNSUPPORTED`（预期）。

### 12.3 legacy group（record_version 2）

| 命令 | 行为 |
|---|---|
| `group status` / `group dispatch` | 正常（`ordered_items` 语义） |
| `group start/report/accept/pause/resume/cancel` | `CAPABILITY_UNSUPPORTED` + `details.kind="legacy_group"`，提示以 `schema_version: 2` 重建 |
| `state migrate` | 不改写 legacy 记录（不猜 `depends_on`） |

### 12.4 回滚

1. 关闭新入口（`capabilities` 移除组推进能力；或配置 `group_runner: false`）。
2. 停止所有 runner 与成员 supervisor，确认进程树消失。
3. `state restore --confirm` 恢复 v3 备份（自动轮换 epoch）。
4. **对账 Git 副作用**：集成分支与 PR 不受数据库回滚影响，逐条比对
   `integration/<op_id>.json` 与真实 ref（含 `prepared` 未完成项）。
5. 不得只恢复数据库就声称回滚完成。

### 12.5 PR 组

`delivery_mode=pull_request` 的组在 v1.3 **不支持**：创建即拒绝，无降级路径。
PR 发布、合并、合并后复验全部留到后续版本（v1.4+），届时按既有"合并后验收"契约实现。

---

## 13 Skill 侧改造（`zoe-workflows`）

| 新增文件 | 内容 |
|---|---|
| `references/group-plan-schema.md` | plan 请求形状、`expected_paths` 只是提示、常见拒绝原因 |
| `references/group-delivery.md` | runner 语义、暂停/恢复/取消、组级验收口径、`event_cursor` 续接 |
| `references/group-report-template.md` | 交付报告七段格式与"未验证"措辞 |

`SKILL.md` 最小改动：

1. 第 5 步「派发」：改为"用 `group create` 冻结依赖图；`group start` 交 runner 推进
   （runner 未启动则如实说明）"。
2. 第 6 步「观察」：组级用 `group status --refresh` + `event_cursor`，
   不用"我在关注"替代查询。
3. 第 9 步「收口」：区分任务级 `accept` 与 `group accept`；成员任务不接受单独验收；
   说明 PR 组不支持。
4. 「保持边界」：增加"会话结束后无 runner 即无推进"、
   "组级依赖图与组级检查不等于设计语义正确"。

---

## 14 工作包与实施顺序

| 包 | 内容 | 主要文件 | 依赖 | 契约 |
|---|---|---|---|---|
| **P0** | 前置收尾与回归（§2.3） | `_profiles.py`（`auth_mode`）、`_pipeline.py`（`error_class`）、`cli.py`（capabilities）、`tests/test_v130_prereq.py` | 无 | 冻结 |
| **P1** | 组计划契约 + 组记录 v3 + schema v4 + 迁移 | `_groups.py`、`_store.py`、`_migrations.py`、`schemas/group-plan.v1.json`、`cli.py`、`tests/test_v130_group_plan.py`、`tests/test_v130_legacy_groups.py` | P0 | 冻结 |
| **P2** | 成员交付分支 + 冻结成员 + 组级路由预演 | `_pipeline.py`（`create`/`run_pipeline`/`accept`/`reassign` 拒绝）、`_routing.plan_route` 复用、`tests/test_v130_group_member.py` | P1 | 冻结 |
| **P3** | 推进器（`lease_id` 握手、tick、预算、暂停恢复取消、组级准入） | 新 `_group_runner.py`、`_store.lock_path`、`cli.py`、`tests/test_v130_runner.py` | P1/P2 + **G1/G2** | **待冻结** |
| **P4** | 串行集成（三段账本）+ 组级检查/报告/验收 | 新 `_integration.py`、`_reports.py`、`_groups.py`、`tests/test_v130_integration.py`、`tests/test_v130_group_accept.py` | P3 + **G3/G4** | **待冻结** |
| **P5** | 设计一致性审查、组级有界修复、有限并行、PR 组收口 | 新 `_group_review.py` 等 | P4 | 未设计 |

每包必须同时交付：实现 + 测试 + CLI/schema 文档 + 迁移说明。不得"代码先行、契约后补"。

里程碑：

- **M1（P0+P1）**：`group plan/create/status` 在 fake CLI 上跑通；legacy group 行为不变；
  `plan_digest` 不随运行时事实变化（T31）。
- **M2（P1+P2）**：单成员 `group_member` 任务停在 `REVIEW_READY`、不产生 PR、
  已集成后 `reassign` 被拒（T28）。
- **M3（P3，G1+G2 之后）**：三成员链自动推进；第二个 `group start` 与 `task start` 被
  统一准入挡住；预算耗尽仍能集成与验收（T25/T26）。
- **M4（P4，G3+G4 之后）**：端到端 `DONE`，报告绑定同一 `integration_sha`；
  ref 更新前后崩溃均可对账（T27）；PR 组被拒（T29）；证据归属校验（T30）。

---

## 15 测试矩阵

| ID | 场景 | 必须断言 | 归属 | 状态 |
|---|---|---|---|---|
| T01 | 三成员正常链 | 一次 `start` 推进到 `REVIEW_READY`；全部指向同一 `integration_sha` | `test_v130_runner.py` | 新增 |
| T02 | 依赖环 / 缺 AC 映射 | 派发前拒绝（`kind=cycle`/`coverage_missing`），零 Agent 启动 | `test_v130_group_plan.py` | 新增 |
| T03 | 显式 profile 越权 | 与自动选择同样拒绝，不回落 | `test_v120_combination.py` | 已覆盖 |
| T04 | prefer 全失效、两候选 | `fallback=ask` 不猜选 | 现有 | 已覆盖 |
| T05 | 模型观测未知 | 不伪报固定模型 | 现有 | 已覆盖 |
| T06 | 重复 `create`/`start` 请求 | 同输入重放，无重复任务与 run；不同动作不串号（`op_seq`） | `test_v130_group_plan.py` | 新增 |
| T07 | runner 崩溃 / 第二个 `group start` | 接管对账；空窗期第二个 start 被拒 | `test_v130_runner.py` | 新增 |
| T08 | 启动握手未知 | 保持 `PROCESS_UNKNOWN` | `test_v130_runner.py` | 新增 |
| T09 | artifact 写后 DB 前崩溃 | 孤立文件可对账，无假完成 | `test_v130_group_plan.py` | 新增 |
| T10 | ref 更新前后崩溃 | `prepared` 可补记 `done`；不重复 merge | `test_v130_integration.py` | 新增 |
| T11 | 连续两次 rebind | 产物基准仍在，只重验 | `test_v121_rebind_revalidate.py` | 已覆盖 |
| T12 | waiver 前后崩溃 | 每 run 最多一项有效豁免 | `test_v121_budget.py` | 已覆盖 |
| T13 | 认证修复、策略未变 | 可重新预检恢复 | `test_v130_runner.py` | 新增 |
| T14 | Agent 删除测试 | 不 PASS，且 `error_class` 可解释 | `test_v130_prereq.py` | 新增 |
| T15 | 保护路径与范围同时命中 | 保护优先，不建议扩权 | 现有 | 已覆盖 |
| T16 | 集成分支在两次集成间变化 | 组级检查 `stale`，组回 `RUNNING` 重跑 | `test_v130_integration.py` | 新增 |
| T17 | 两候选各自通过但集成冲突 | 不进入 `REVIEW_READY`，`INTEGRATION_CONFLICT` | `test_v130_integration.py` | 新增 |
| T18 | 组级检查缺证据/状态过期 | 不得进入 `REVIEW_READY` | `test_v130_group_accept.py` | 新增 |
| T19 | pause/cancel 时有活跃执行 | 不抢先终态，返回 `active_run_ids` + `draining` | `test_v130_runner.py` | 新增 |
| T20 | external close | `CANCELLED`，不计成功，不满足依赖 | `test_v130_group_accept.py` | 新增 |
| T21 | 两项目同 `task_key` | 路径、会话、证据不串用 | `test_v130_group_plan.py` | 新增 |
| T22 | 组预算耗尽 | 新启动被拒，**但集成与验收仍可完成** | `test_v130_runner.py` | 新增 |
| T23 | PR 合并树与候选不同 | 对实际产物重新验证 | — | v1.4.0 |
| T24 | wheel/Skill 干净安装 | 无源码目录仍可调用 | `test_distribution.py` | 已覆盖 |
| **T25** | 上一成员仍在执行 | tick 不启动第二个成员（组槽位，不只是全局槽位） | `test_v130_runner.py` | 新增 |
| **T26** | 预算刚好用尽 | 最后一次启动后可集成、检查、验收 | `test_v130_runner.py` | 新增 |
| **T27** | `prepared` 已落盘、ref 未更新 | 补 `update-ref` 或阻塞，不重复 merge | `test_v130_integration.py` | 新增 |
| **T28** | 已集成成员 `reassign/rebind/revise` | `STATE_CONFLICT` + `integrated_member` | `test_v130_group_member.py` | 新增 |
| **T29** | `delivery_mode=pull_request` 的组 | create 即拒（`group_pr_unsupported`） | `test_v130_group_plan.py` | 新增 |
| **T30** | `evidence_refs` 归属/版本不合规 | accept 拒绝，列出未满足项 | `test_v130_group_accept.py` | 新增 |
| **T31** | 运行时事实变化 | `plan_digest` 不变、`revision` 不变 | `test_v130_group_plan.py` | 新增 |

分层：纯算法单测 → fake CLI 进程集成 → 精确故障点注入（杀 runner/杀 git/断在 ref 前后）
→ macOS/Windows 平台测试 → 真实 Agent 端到端。**fake CLI 通过不等于 VERIFIED。**

---

## 16 风险与已知限制（发布时原样写出）

| 风险 | 说明 | 缓解 |
|---|---|---|
| runner 被误当无人值守 | 进程退出即停止推进 | 响应与文档固定写"未在推进"，交出 `event_cursor` |
| 依赖图是能力反转 | v1.2 显式拒绝 `depends_on` | CHANGELOG 单列；`capabilities` 同步移除 `automatic_dag_scheduling` |
| 组记录单行 CAS 争用 | runner 与宿主同写一条记录 | 宿主只读；写全部走 `expected_version`；组记录不存大日志 |
| merge 冲突需人工 | v1.3 无组级修复执行 | 隔离 + `INTEGRATION_CONFLICT` + 明确 `next_action` |
| 集成 worktree 残留 | 清理失败不影响结论 | `cleanup_pending` 进报告，人工清理 |
| 时间口径含休眠 | 墙钟时长包含机器休眠与等待 | 命名 `wall_seconds_inclusive`，不承诺排除 |
| 任务级预算仍未全部生效 | 组级生效≠任务级生效 | README「Known limitations」保留原文 |
| 无设计语义审查 | v1.3 只做集成级检查 | 报告固定 `NOT_RUN` 行 |
| PR 组不支持 | 自行合并无法验收 | 只能外部收口 |
| Windows 无硬件证据 | Job Object/LockFileEx 未实测 | 标 `IMPLEMENTED`，矩阵写 `NOT_RUN` |
| schema v4 不可降级打开 | v1.2 拒绝 v4 库 | 迁移前备份；回滚含 Git 对账 |
| 无真实 e2e | 无 adapter `VERIFIED` | 见 §17.3 三档口径 |

---

## 17 完成定义

### 17.1 v1.3.0

- [ ] P0 七项前置全部通过，F1/F2/F7 回归无退化。
- [ ] `plan_digest` 只覆盖不可变投影；运行时事实变化不改变摘要与 revision（T31）。
- [ ] 成员 Spec 在创建组时冻结，仓库后续改动不影响本组。
- [ ] 静态依赖图由 runner 全组串行推进；上一个成员未结束时不会启动第二个（T25）。
- [ ] 启动预算只门禁新启动；预算用尽仍可集成、检查与验收（T26）。
- [ ] runner 接管无第二个写者；空窗期第二个 `group start` 被拒（T07）。
- [ ] `task start/resume`、`group dispatch`、runner 三者经同一准入函数，无绕过路径。
- [ ] 集成三段账本：ref 更新前后崩溃均可对账，不重复 merge（T10/T27）。
- [ ] 已集成成员无法原地变更（T28）；未集成成员的失效有明确出口。
- [ ] 组级检查绑定 `integration_sha`；`NOT_RUN`/`stale` 不进入成功统计。
- [ ] 仅 `local_artifact` 组可 `accept`；PR 组创建即拒（T29）。
- [ ] `accept` 校验 ref、worktree HEAD、指纹、证据归属与版本（T30）；`rejected` 不进入 `DONE`。
- [ ] schema v3→v4 迁移有备份与回滚说明；legacy group 行为不变。
- [ ] 双仓安装与兼容流程通过；Skill 在无 Runtime 源码的机器上可用。
- [ ] 已知限制原样列出（§16），设计一致性审查标 `NOT_RUN`。

### 17.2 v1.4.0（不在本文交付范围，不承诺排期）

- 设计一致性审查 Agent + 结构化 findings 契约。
- 组级有界修复 run。
- `max_parallel > 1`（含资源互斥显式约束）。
- PR 组的发布、合并后对实际产物复验。

### 17.3 发布口径（三档，不得混用）

| 档位 | 判据 | 可以说的话 |
|---|---|---|
| 开发现状 | 无任何真实 CLI 组链路记录 | "实现完成，未验证"；`capabilities` 不声称支持 |
| 试用发布 | **≥1** 真实 CLI 组合跑通组链路（记录 OS/架构、CLI 版本、`auth_mode`、模型配置、测试 commit、证据） | "在 <组合> 上试用通过" |
| 正式发布矩阵 | **≥2** 不同 Agent 的真实组合通过 | "跨 Agent 自动派发已验证" |

- 无论哪一档，**没有精确版本证据记录就不能写 `VERIFIED`**；
  macOS 成功不能推出 Windows 成功。
- L3 自动排除不在任何档位的承诺里（移出完成定义、不排期）。
