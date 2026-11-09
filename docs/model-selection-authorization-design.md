# 用户授权范围内的模型选择（详细实现设计）

| 项 | 内容 |
|---|---|
| 基线 | `zoe-runtime` v1.4.1（未发布批次）工作区 |
| 目标版本 | **v1.5.0**（授权边界 + 执行准入 + 统计口径）；模型可用性精确识别推迟 **v1.6.0** |
| 前序文档 | `docs/agent-routing-design.md`（v1.1）、`docs/execution-combination-routing-design.md`（v1.2） |
| 配套 skill | `zoe-workflows`（`SKILL.md` + `references/`） |
| 输入 | 外部建议《用户授权范围内的模型选择方案》；一审、二审、三审意见（2026-10-30） |
| 状态 | **通过开发准入，未通过发布准入**：三处核心逻辑已在本文冻结为可测试条件，实现与回归证据待补 |

## 修订记录

| 版本 | 变化 |
|---|---|
| v1 | 首次评估：复用 `routing`、拒绝 `agent_selection`、五处缺口，2.5 人日 |
| v2 | 采纳二审：M3 扩展到 explicit、授权对象是配置内容、示例分档、M2 四情形、M4 拆分、兼容迁移核验、11 条测试，版本更正为 v1.5.0 |
| **v3** | 采纳三审三处核心修正，并因核实推翻 v2 两处结论：**(a) 更正 §6.1——`store.config()` 本身调用 `validate_config()`，是严格校验入口，共 40 处调用点，旧配置会导致读取即失败（新增 M0 读路径分层）；(b) 实测发现 `legacy: true` 可被用户声明并绕过 `model` 必填（新增 M7）**；新增"执行契约校验"与"配置摘要比较"的分离（§4）、授权确认记录 `attest`（§5，不放进用户配置）、统计三条件组合（§6，修正 v2 伪代码逻辑错误）、占位符检查前移与 `ready` 统一（§7.1）、真实 CLI 验收的证明范围限定（§10.3）、四条发布阻断测试（§10.4）；人日重估为编码 4.5 / 全量 9.0 |

---

## 0 结论

### 0.1 三处核心修正的结论

| 三审指出的问题 | 本文处置 |
|---|---|
| 1 配置摘要相等 ≠ 符合新版本规则 | **成立，且比指出的更严重**：`store.config()` 直接 `validate_config()`（§2.1），所以阻断其实会发生——但发生在**读配置阶段**，会连带打挂 `status`/`doctor`/`orchestration`。修正为"读路径分层 + 执行入口统一校验"（§4） |
| 2 授权来源迁移自相矛盾 | **成立**：`allowlist_source` 不放进用户配置；改为独立确认记录 `projects/<id>.attest.json`（§5），记录 runtime 版本 + 完整配置摘要 + 已确认候选；无记录报 `unknown`；`rebind` 不自动补 |
| 3 M4 统计判定逻辑错误 | **成立**：v2 伪代码最外层限定 `failure_class == "executor"`，会让"身份未知的成功运行"与"环境失败"被判为可比较。改为三条件组合（§6），并改名 `pass_rate_attributed` |

### 0.2 证据等级

【核】已在本工作区代码逐条核对（给行号）；【测】已在本机执行命令验证；【新】本文新增；【不】不采纳；【待验】需真实 CLI 才能证明。

### 0.3 逐条判定总表

| 来源 | 主张 | 判定 |
|---|---|---|
| 一审 一~十二 | 原则、术语、职责、验收 | 采纳（见 v2 记录） |
| 二审 二 | 白名单扩展到 explicit | 采纳（M3） |
| 二审 三 | 授权对象是配置内容 | 采纳（§4.3） |
| 二审 四 | 模板分档 + 占位符 | 采纳（M1，位置按三审前移） |
| 二审 五 | M2 四情形 + 输入形态 | 采纳（§7.2，空白冻结为拒绝） |
| 二审 六 | M4 拆两半 | 采纳（§6，v3 修正判定逻辑） |
| 二审 七 | 兼容影响面 | 采纳并**修正**（v2 结论错误，见 §2） |
| 二审 八 | 宿主评审约束 | 采纳（§8.4，按三审改为不静默降级） |
| 二审 九 | 任务特征来源 / 成本表述 | 部分采纳（`task_type_source` 已有；成本表述简化） |
| 二审 十 | 新增测试 | 采纳（§10.2） |
| 三审 1 | 执行准入与摘要分离 | 采纳（§2、§4） |
| 三审 2 | 授权确认记录 | 采纳（§5） |
| 三审 3 | 统计三条件 | 采纳（§6） |
| 三审 4 | 占位符前移 + `ready` 统一 | 采纳（§7.1） |
| 三审 5 | 五处小问题 | 采纳（§7.3） |
| 三审 6 | 真实 CLI 验收范围 | 采纳（§10.3） |
| 三审 建议 | 四条发布阻断测试 | 采纳（§10.4） |

### 0.5 实现状态（v1.5.0 工作区，已落地）

| 项 | 状态 | 落点 |
|---|---|---|
| M7 `legacy` 声明拒绝（P0） | 已实现 | `_profiles.normalize_config` |
| M2 四情形 + 形态 + 布尔类型 | 已实现 | 同上 |
| M3 白名单必须显式 | 已实现 | `_routing.normalize_routing` |
| M0 读路径分层 | 已实现 | `_config.validate_config(strict=…)`、`Store.config` / `config_problems` |
| M8 执行契约 + 三个调用点 | 已实现 | `_config.check_execution_contract`；`create` / `_start_locked` / `reassign` |
| attest 确认记录 | 已实现 | `Store.attestation/write_attestation`、`zoe project attest`、`ALLOWLIST_UNCONFIRMED` |
| M4b 统计三条件 | 已实现 | `_routing.attribution` 新增 `model_capability_comparable` / `identity_attributable` / `cause_attributed`；`performance_record` 新增 `attempt.n_attributed` / `pass_rate_attributed`、`counts.runs_cause_unconfirmed`、`reliability.executor_failures_unconfirmed` |
| M1 三档示例 + 占位符 | 已实现 | `references/project-profile{,.multi-model,.advanced}.example.json`、`_config.find_placeholders`、`doctor.placeholders_ok` |
| M4a `MODEL_UNAVAILABLE` 识别 | 未实现（v1.6.0） | — |
| M6 `project diff` 执行配置归类 | 未实现 | 依赖既有 B4 的 `changed_fields` 扩展 |

实现时发现两处设计需要修正，已按事实约束落地：

1. **M7 的判据是 `schema_version`，不是"有没有 `agent_profiles` 段"。** 规范化会把构造出的
   legacy profile 写回 `agent_profiles`，于是二次规范化时"有段"恒为真——迁移产物会被误判成
   用户新写的声明（首次实现时 136 个既有用例当场失败）。改为按 schema 版本区分：v2 配置声明
   `legacy` 一律 `POLICY_DENIED`；v1 配置保留迁移语义。代价是用户仍可退回 `schema_version: 1`
   走旧路径，但那条路径本身要求 `model_unresolved=true`、报告必须写"运行模型未解析"，不属于
   静默绕过。
2. **`doctor` 新增 `policy_problems`。** 读路径把问题收进局部变量，而 `doctor` 拿到的已经是
   "读处理完"的配置，所以它自己用 `strict=False` 再算一次（幂等）。没有这个字段，"旧配置哪里
   不合法"就没有可读的落点，§9.3 的迁移检测也就不成立。

另外，`model` 的布尔类型校验比设计多一条：`allow_cli_default_model: "true"`（字符串）此前会被
当成 `False`——一个写错的配置看起来像"显式模型"，且不报错。

**实现复核（第二轮）修掉四处"接了一半"**：

| # | 问题 | 影响 | 修复 |
|---|---|---|---|
| R1 | `create` 响应没有 `allowlist_source` / `policy_hash` | §5.3 要求报告写"授权：confirmed"，宿主拿不到这个事实，只能去读 attest 文件 | `_require_attested` 的返回值写进 create 响应 |
| R2 | `attestation_status` 完全不看 `confirmed_profiles` | `project attest --profiles cb` 收窄之后，cb2 仍能被派发——收窄只是装饰 | 新增 `selected_profile` 参数；`create`（候选确定后）/`start`/`reassign`（目标确定后）都传实际候选；新 reason `candidate_not_confirmed` |
| R3 | `doctor.policy_ready` 恒为 `true` | 有 `policy_problems` 时仍宣称策略就绪 | 改为 `not policy_problems`（`ready` 不变：环境归环境、策略归策略） |
| R4 | `not_comparable` 只按组合合并 | 同一组合的"身份未观测"与"原因未确认"被压成一条，只留先到的 reason | 按 `(combination_id, reason)` 分组 |

R2 的顺序调整值得记一笔：`reassign` 的授权确认从"目标确定之前"移到"目标确定之后"，
因为只有那时才知道"实际要用哪个候选"。`create` 则判两次——先判配置与记录存在，候选
由路由决定后再判"这个候选被确认过"。

测试：`tests/test_v150_authorization.py`（41 个用例）覆盖以上各项；
`tests/test_contract_artifacts.py` 新增三个示例测试（schema / 占位符被拒 / 替换后可注册）。
全量 `pytest tests` 结果：**870 passed, 6 skipped**。

### 0.4 本文覆盖的旧条款

| 被覆盖 | 位置 | 覆盖为 |
|---|---|---|
| `allowed_profiles` 缺省 = 全部 | `_routing.py:191-194`、`agent-routing-design.md:287` | 声明 `routing` 即必须显式非空（M3） |
| "`mode=auto` 且白名单为空 → `INPUT_INCOMPLETE`" | `_routing.py:213-217` | 该条恒真（死校验），删除并替换 |
| `allowed_profiles` = "自动派发白名单" | `agent-routing-design.md:287` | "经 Zoe 派发时允许使用的集合，同时约束显式与自动" |
| `store.config()` 视为"已规范化、不重新校验" | 本文 v2 §6.1 | **错误**：它调用 `validate_config`（§2.1） |
| 旧配置无效时靠 `POLICY_CHANGED` 阻断 | 本文 v2 §6.2 | 改为执行契约校验先行（§4） |
| `routing.allowlist_source` 作为配置字段 | 本文 v2 §2.5 拍板项 P-1 | 取消，改为独立 attest 记录（§5） |
| 成本"只能禁止不能偏好" | v1.2 §11.4 | "不自动推断价格；用户通过 `prefer` 明确表达顺序" |
| `MODEL_UNAVAILABLE` 登记即可 | `cli.py:118-121` | 识别延期 v1.6.0；统计本期排除未归属样本（§6） |
| "凡冲突以前序文档为准" | 本文 v1 §0.4 | 本节清单 |

不变：R1–R9 不变量、三层事实、闸门顺序（模型身份早于 verify）、`detail` 脱敏、三个预算计数点、`session_mode: "new"`、`not_supported` 四条、`use_performance: false` 禁用边界。

---

## 1 新发现：`store.config()` 是严格校验入口（推翻 v2 结论）

### 1.1 事实【核】

```551:553:src/_store.py
    def config(self, project):
        path = self.home / "projects" / (safe_id(project) + ".json")
        return validate_config(read(path), base_dir=path.parent)
```

而 `validate_config` 末尾调用 `_profiles.normalize_config(c, base_dir)` 并对每个 profile 跑 `resolve_binding`：

```455:462:src/_config.py
    _profiles.normalize_config(c, base_dir)
    for profile_id in c["agent_profiles"]:
        _profiles.resolve_binding(c, profile_id)
    return c
```

### 1.2 后果

| 后果 | 说明 |
|---|---|
| 旧配置在**任何**读配置的命令上失败 | `store.config()` 有 40 处调用点【核】，覆盖 `doctor`（`cli.py:1577`）、`status`（`cli.py:811/821`）、`orchestration`（`_context.py:39`）、group 全流程、`review`、`qualification` |
| 连带打挂只读命令 | `status` / `report` / `events` 只要经过 `store.config()` 就失败——违反三审"只读命令仍可用"的要求 |
| 摘要比对不是唯一防线 | 三审担心的"摘要相等所以不阻断"在 `_start_locked` 路径上成立，但因为 `store.config()` 先抛错，实际阻断发生在更早处——**代价是错误类型不对、范围过大** |
| v2 §6.1 的表格错误 | v2 写"`store.config()` 不调用 normalize、`doctor` 不调用 normalize"，均与代码不符 |

### 1.3 这不是本次引入的缺陷，但本次会放大它

现状：`compatibility_evidence` 文件被删，连 `status` 都读不了项目（`resolve_binding` → `load_evidence` 报 `ADAPTER_VERSION_MISMATCH`）。M2/M3 会让**更多配置组合**触发同类失败。因此必须先把"读"与"执行"的严格性分开，再加新校验；否则新校验的第一次生效就是"用户什么都看不到"。

---

## 2 新发现：`legacy: true` 可绕过 `model` 必填（P0 漏洞）

### 2.1 事实【测】

`legacy` 在 `PROFILE_FIELDS` 白名单内【核】`_agents/base.py:99`，而 `model` 必填校验对 legacy 豁免【核】`_profiles.py:273`：

```python
if not value.get("legacy"):
    require(bool(value.get("model")) or value.get("allow_cli_default_model") is True, ...)
```

实测（用户在自己的 `agent_profiles` 里写 `legacy: true`）：

```text
输入: {"adapter":"codebuddy","backend":"codebuddy-cli","permission_profile":"isolated-edit","legacy":true}
输出: BYPASS_OK  model=None  legacy=True  model_unresolved=True
```

即：**任何用户（或 Skill）写一个 `legacy: true` 就能跳过"必须声明模型"这条最核心的费用闸门**，直接跟随 CLI 默认模型。`legacy` 原本是 `_legacy_profile()` 在无 `agent_profiles` 段时由系统注入的迁移标记【核】`_profiles.py:193-212`，不该出现在用户可写的字段里。

### 2.2 修法（M7）

区分"系统注入"与"用户声明"，**不改 `PROFILE_FIELDS`**（否则 legacy 迁移本身失效）：

```python
# normalize_config：循环之前记录来源
supplied = bool(c.get("agent_profiles"))     # 用户是否提供了 agent_profiles 段
...
for profile_id, profile in profiles.items():
    ...
    if supplied:
        require(
            profile.get("legacy") is not True,
            "POLICY_DENIED",
            f"Profile {profile_id}: 'legacy' is controller-reserved and may not be "
            "declared; it exists only for configurations migrated from schema v1",
        )
```

- `supplied == False` 时 profile 由 `_legacy_profile()` 构造，豁免照旧；
- `supplied == True` 时任何 `legacy: true` → `POLICY_DENIED`；
- 该判断必须在 `model` 必填校验**之前**，否则错误仍是"必须声明 model"，掩盖了绕过意图。

**验收**：上面那段实测输入必须返回 `POLICY_DENIED`；`{"agent": "codebuddy"}` 这种 v1 配置迁移后仍能注册并保持 `legacy: true`。

---

## 3 冻结的三条原则

| 编号 | 原则 | 理由 |
|---|---|---|
| E1 | **摘要相等**只回答"配置有没有变"，**不回答**"这份配置允不允许执行" | 三审第 1 条；两者失败条件不同 |
| E2 | **执行准入**必须在创建新执行尝试前用纯校验函数集中判定，错误必须指向具体字段 | 不依赖偶然的摘要变化，不一律包装为 `POLICY_CHANGED` |
| E3 | 只读命令必须能在配置非法时读取历史记录 | 否则用户既不能执行，也无法排障 |

---

## 4 执行准入设计（M0 + M8）

### 4.1 M0 读路径分层（前置项，必须最先做）

`validate_config` 增加 `strict` 参数，只影响**策略性**校验，不影响结构校验与路径规范化：

| 校验 | `strict=True`（注册 / 执行） | `strict=False`（读） |
|---|---|---|
| JSON 解析、`schema_version`、`project_id`、`repo`、`backend` | 抛 | 抛 |
| `compatibility_evidence` 相对路径 absolutize | 执行 | 执行（否则后续路径全错） |
| `model` 必填 / M2 组合 / M7 legacy 伪造 | 抛 | **跳过并记录** |
| `routing` 段白名单（M3） | 抛 | **跳过并记录** |
| `auth_mode=api_key` 凭据声明 | 抛 | **跳过并记录** |
| `resolve_binding`（含证据文件存在、适配器映射） | 执行 | **跳过并记录** |

```python
def validate_config(c, base_dir=None, *, strict=True, problems=None):
```

- `store.config(project)` → `validate_config(..., strict=False, problems=[])`；
- 新增 `store.config_problems(project)` 返回该列表（供 `doctor` / `status` 显示"配置有问题但可读"）；
- `project register` → `strict=True`；
- 执行入口 → `strict=True`（见 §4.2）。

`problems` 只存在于内存，**绝不写回 `c`**（写回会改 `policy_digest`）。返回的 `c` 在 `strict=False` 下可能缺少新规则要求的字段——这不影响执行，因为执行入口一定会用 `strict=True` 重新校验。

### 4.2 M8 执行契约校验（统一入口）

新增纯函数（无 spawn、无写盘）：

```python
# src/_profiles.py
def check_execution_contract(c: dict, profile_id: str | None = None) -> None:
    """Answer: may this exact configuration be dispatched by *this* runtime?

    Raises a specific error carrying `details.field`. Never returns a
    POLICY_CHANGED: that code means 'the config changed', not 'the config is bad'.
    """
```

内部顺序**固定**（三审第 4 条的顺序）：

```text
1. 结构       JSON 已解析；schema_version / project_id / repo / backend 类型与取值
2. 占位符     扫描配置字段（不是任意正文）：model / expected_version / provider /
              endpoint_ref / compatibility_evidence / credential_refs / authorization_ref
3. 模型与授权 model 必填、M2 四情形、M7 legacy 伪造、routing 白名单（M3）、
              attest 确认记录（§5）
4. 绑定       resolve_binding（适配器映射、证据文件、权限绑定、凭据存在）
5. 环境       由 start 的既有 doctor 负责，不在这里重复
6. 派发       由调用方继续
```

调用点（全部是"创建新执行尝试"的入口）：

| 入口 | 位置 | 说明 |
|---|---|---|
| `task create` | `_pipeline.py:336` 之前 | 新任务不得用非法配置起跑 |
| `task start` | `_pipeline.py:1034` 的 `POLICY_CHANGED` 比对**之前** | 先报具体配置问题，再谈漂移 |
| `task reassign` | `_pipeline.py:1685` 之前 | 同上 |
| `group start` / 成员派发 | 经 `_group_runner` 调用 `_pipeline.create` / `start` | 组内成员没有独立派发路径，因此自动继承前三条；`group start` 自身不再重复校验（重复只会让同一个错误在两处各报一次） |
| `task resume` / `recover` | 复用 `start` 路径 | 不得绕过 |

**顺序纪律**：契约校验失败 → 直接返回具体错误；通过后才做摘要比对（`POLICY_CHANGED`）。这样"摘要相等但配置非法"不可能放行，也不会把配置缺陷说成"策略变了"。

### 4.3 摘要与授权对象（二审第 3 条，v2 已冻结，此处只补落点）

| 机制 | 回答 | 位置 |
|---|---|---|
| `policy_hash` / `policy_digest` | 配置是否与冻结/确认时一致 | 【核】`_profiles.py:148-159` |
| `combination_id` | **解析后**的执行组合（含 endpoint 的 `{id, class}`） | 【核】`_routing.py:371-408` |
| attest（§5） | 用户是否确认过这份配置的候选集合 | 【新】 |

端点：`combination_id` 已包含 `endpoint_of()` 解析后的 `{id, class}`【核】`_routing.py:398-399`，因此"`endpoint_ref` 名字不变但 `class` 变了"会被识别为不同组合；`endpoints` 段其他字段（目前只有 `attestation`，唯一取值 `config-declared`）不改变解析身份。**仍需在 `start` 记录 `combination_changed` 事件**（已实现【核】`_pipeline.py:2496-2511`），但该事件**不阻断**——阻断由"组合变化 → `policy_hash` 变化 → attest 失效"承担（§5.3）。

---

## 5 授权确认记录（attest）

### 5.1 为什么不能放进配置

三审第 2 条正确：`routing.allowlist_source` 若放进 `routing` 段，等于让**被授权方**填写授权来源，且用户手写 `"explicit"` 即可伪造。同时用户配置本身会被 `policy_digest` 覆盖，"确认"与"配置内容"耦合在一起，会导致"重新注册相同内容不改摘要 → 无法产生确认事件"。

### 5.2 落点与形状【新】

```text
$ZOE_HOME/projects/<project_id>.attest.json     （与 <project_id>.json 同级）
```

```json
{
  "schema_version": 1,
  "project_id": "example",
  "policy_hash": "<64 hex of the normalized config>",
  "confirmed_profiles": ["cb-primary", "cb-secondary"],
  "runtime_version": "1.5.0",
  "confirmed_at": 1793500042.0,
  "confirmed_by": "project attest",
  "note": "Confirms the allowlist for this exact policy_hash. Not a record of who wrote the original allowlist."
}
```

不写状态库（不新增表、不升 `SCHEMA_VERSION`），用 `atomic()` 落盘，与 `projects/<id>.json` 同级。

### 5.3 语义冻结

| 场景 | 行为 |
|---|---|
| 无 `routing` 段（历史配置） | **不要求 attest**；报告固定写"历史配置即授权"，`allowlist_source: "legacy_config_is_authorization"` |
| `"routing": null` | 视为配置缺陷（`INPUT_INCOMPLETE`），不算"无 routing 段"。按值判空会让 null 同时跳过白名单与 attest |
| 有 attest，但记录缺 `policy_hash` / `confirmed_profiles` 不合规 | 报 `ALLOWLIST_UNCONFIRMED`，`details.reason: "record_invalid"`。损坏的记录按未确认处理，不能因为字段缺失而放行 |
| 有 `routing` 段，无 attest | 执行入口报 `ALLOWLIST_UNCONFIRMED`，`details` 给 `policy_hash` + 候选列表 + 下一步命令 |
| 有 attest，`policy_hash` 一致 | 放行；`allowlist_source: "confirmed"` |
| 有 attest，`policy_hash` 不一致 | 报 `ALLOWLIST_UNCONFIRMED`，`details.reason: "policy_changed_since_attestation"`（**配置改动使确认失效**，包括"白名单没变但 profile 换了模型"） |
| `project rebind` | **不写 attest**（不得自动把未知来源洗成已确认） |
| `project attest` 重新执行（配置内容未变） | 允许；更新 `confirmed_at` 与 `runtime_version`，`policy_hash` 不变 |
| 拥有同机写权限的程序伪造 attest | **不防御**。attest 证明"这份配置被确认过"，不是防篡改凭证；文档不得声称它能防伪造 |

### 5.4 新命令（最小面）

```text
zoe project attest --project P [--profiles a,b] [--json]
```

- 读 `store.config(P)`（`strict=True`，配置非法则拒绝确认）；
- 默认确认**当前 `allowed_profiles`**（无 `routing` 段时确认全部 `agent_profiles` 键，用于一次性显式确认历史项目）；
- `--profiles` 只允许**收窄**（⊆ `allowed_profiles`），扩大必须改配置并重新 `register`；
- 输出 `policy_hash` / `confirmed_profiles` / `would_write`；`--dry-run` 不落盘。

Skill 的代劳方式：Skill 展示候选与执行配置字段，取得用户确认后调用该命令；**不要求用户手敲命令**，但 Skill 不得在用户未确认时调用。

### 5.5 为什么这条能兑现"不会用到我不想用的模型"

```text
新 profile 加入 agent_profiles
  → 若在 allowed_profiles 内：policy_hash 变化 → attest 失效 → 下一次执行前必须重新确认
  → 若不在 allowed_profiles 内：显式指定也会 POLICY_DENIED
两种情况都不会静默用上新模型。
```

---

## 6 统计口径（M4，修正 v2 逻辑错误）

### 6.1 v2 伪代码错在哪

```python
# v2（错误）
cause_confirmed = not (
    failure_class == "executor"
    and (block_code == "AGENT_FAILED" or model_unresolved or identity in ("unobserved","mixed"))
)
```

最外层 `failure_class == "executor"` 使条件短路：

| 情况 | v2 判定 | 应为 |
|---|---|---|
| 身份未知的**成功**运行（`failure_class=None`） | `not(False) = True` → 可比较 | 否 |
| 环境失败（`environment`） | `True` → 可比较 | 否 |
| `reported_mismatch` | 不在排除列表 → `True` | 否 |
| "原因已确认"与"身份可归属" | 合并成一个布尔 | 两个独立条件 |

### 6.2 冻结为三条件组合（M4b）

```text
可纳入「身份与归因明确样本的通过率」 =
    identity_attributable        # 模型身份可归属
    且 result_evaluable          # 结果可评价（有明确结局，非未结束/取消）
    且 cause_attributed          # 失败时原因已明确归因到执行器
```

| 情况 | 纳入 |
|---|---|
| `reported_match` + 独立验收通过 | 是 |
| `reported_match` + 有证据的实现失败（非兜底码） | 是 |
| `AGENT_FAILED`（兜底码） | 否（原因未确认） |
| `model_identity ∈ {unobserved, mixed, reported_mismatch}` | 否 |
| `model_unresolved == true` | 否 |
| 环境 / 认证 / 策略 / 基础设施 / 人工取消 / 未结束 | 否 |
| `purpose == "probe"` | 否（既有） |

实现（`_routing.attribution` 增加参数，返回新增字段）：

```python
identity_attributable = (
    model_identity_value == "reported_match"
    and run.get("model_unresolved") is not True
)
result_evaluable = outcome is not None and failure_class not in EXCLUDED_CLASSES
cause_attributed = failure_class in ("executor", "verification") and (
    run.get("block_code") != "AGENT_FAILED"
)
```

### 6.3 字段与显示纪律

| 纪律 | 说明 |
|---|---|
| 原名改称 | `pass_rate_confirmed` → **`pass_rate_attributed`**；报告文案"身份与归因明确样本的通过率"。不得写成"模型能力"或"模型更好" |
| 分母为 0 | `n_attributed == 0` → 比例为 **`null`**，不是 `0%` 或 `100%` |
| 老记录缺字段 | 按**未知**处理（`None`），不默认 `confirmed`；`runs_pre_v12` 的既有口径不变 |
| 双口径并存 | `attempt.pass_rate`（全量，运维指标）与 `attempt.pass_rate_attributed`（能力比较）同时输出，不合并 |
| 展示排除 | `PerformanceRecord` 新增 `counts.runs_cause_unconfirmed` 与 `not_comparable[].reason`；报告展示排除数量与原因 |
| 保留事实 | 启动次数、耗时、`spend`、`block_code`、`failure_class` 全部保留（解析层无条件执行，v1.2 §12.8 不变） |
| `use_performance` | 保持 `false` 拒绝（`CAPABILITY_UNSUPPORTED`）——本期仍不用历史决定候选 |

### 6.4 M4a 精确识别：延期 v1.6.0

`MODEL_UNAVAILABLE` 退出码已冻结但无代码路径【核】`cli.py:118-121`。准确识别需要适配器输出 marker，属契约变更，不在本轮。本轮只做 §6.2 的统计隔离 + 文档写明"显式模型被拒当前表现为 `AGENT_FAILED`"。

---

## 7 实现项

### 7.1 M1 示例分档 + 占位符前移（P0）

**现状【测】**：`references/project-profile.example.json` 两个 profile 无 `model`【核】`:10-31`，注册必然 `INPUT_INCOMPLETE`。漏测根因：`tests/test_contract_artifacts.py:139-143` 只校验 JSON Schema，而 `model` 不是 schema required【核】`schemas/project-profile.v2.json:24`。

**三档示例**（`build.py` 的 `WHITELIST` 含 `references` 整目录，新文件自动分发；**必须加入 `REQUIRED_FILES`**【核】`build.py:45-55`）：

| 档 | 文件 | 内容 | 验证 |
|---|---|---|---|
| 默认 | `project-profile.example.json`（改造） | 1 profile + `model` + 显式白名单，只留必需字段 | 结构校验 + 夹具替换后可注册 |
| 多模型 | `project-profile.multi-model.example.json` | 2 profile（不同 `model`）+ `routing.rules` | 同上 |
| 高级 | `project-profile.advanced.example.json` | `capabilities` / `endpoint_ref` / `unit_price` / `compatibility_evidence` | 同上 |

**占位符检查前移（三审第 4 条）**：

- 约定：`REPLACE_WITH_*`、`/absolute/path/to/*`、`<FILL:*>`；
- 位置：`check_execution_contract` 的**第 2 步**（早于 `resolve_binding`），并在 `project register` 的 `validate_config` 之前先跑一次；
- 只扫描**配置字段值**（`model` / `expected_version` / `provider` / `endpoint_ref` / `compatibility_evidence` / `credential_refs` / `authorization_ref`），不扫描 `description` 等自由文本；
- 错误信息只输出**字段路径 + 占位符种类**，不回显引用内容（避免泄露端点/凭据命名）；
- **不要求模板能通过 `normalize_config`**：模板只需结构可解析；替换后的夹具必须通过 `normalize_config` + `resolve_binding`。

**`doctor` 与 `ready` 统一（三审要求）**：现状 `ready` 已包含 `checks_preflight_ok`【核】`_config.py:347-353`，占位符按同一方式并入：

| 字段 | 语义 |
|---|---|
| `checks.placeholders_ok` | 布尔；`false` 时 `details.fields[]` 列出未填字段 |
| `ready` | **纳入** `placeholders_ok`（与 `checks_preflight_ok` 同层），避免"显示 ready，随后不能启动" |
| 顶层 `placeholders_ok` | 与 `checks_preflight` 一样顶层重复暴露【核】`_config.py:393-395`，宿主最常读这层 |

### 7.2 M2 模型字段四情形 + 输入形态（P1）

| `model` | `allow_cli_default_model` | 结果 | 现状 |
|---|---|---|---|
| 非空有效 | 缺省 / `false` | 显式模型 | 满足 |
| 缺失 | `true` | 允许，`model_unresolved=true` | 满足 |
| 非空 | `true` | **拒绝** | 未实现【测】 |
| 缺失 | 缺省 / `false` | **拒绝** | 满足 |

输入形态（**空白冻结为拒绝**，删除 v2 的"或 strip"二选一）：

| 输入 | 冻结结果 |
|---|---|
| `""` | 拒绝 |
| `"   "` | **拒绝**（`strip()` 后为空；现状通过【核】`_agents/base.py:557-561`） |
| `" m-a "` | **拒绝**（要求 `model.strip() == model`；不静默 trim，避免"配置写的与 argv 不同"） |
| `allow_cli_default_model: "true"` | 拒绝（非布尔） |
| `allow_cli_default_model: 1` | 拒绝（`isinstance(..., bool)` 检查，`1` 不是 bool） |

**legacy 豁免的准确边界**（三审第 5 条）：完整的判定顺序写进代码注释与本文，避免"分支外/分支内"歧义：

```text
normalize_config(c):
  supplied = bool(c.get("agent_profiles"))
  profiles = c["agent_profiles"] or {"legacy-<adapter>": _legacy_profile(c, base_dir)}
  for pid, profile in profiles.items():
      safe_id(pid); 类型检查; 字段白名单检查
      if supplied:                       # ← 用户提供的段：无任何豁免
          M7: 拒绝 legacy: true
          M2: 拒绝 model + allow=true
          输入形态：拒绝空/空白/非字符串 model
          model 必填（allow=true 可替代）
      # supplied == False：系统构造的 legacy profile，仅此一处豁免
      M3: routing 白名单（与 supplied 无关，只看 routing 段是否存在）
```

一句话：**豁免只针对"配置里根本没有 `agent_profiles` 段"这一种历史形态；用户无法通过提交任何字段获得豁免。**

### 7.3 M3 授权边界统一（P1）

见 §4.2 第 3 步与 §5。要点复述：

- 声明 `routing` 段 → `allowed_profiles` 必须显式、非空、⊆ `agent_profiles`、含 `default_agent_profile`；
- explicit 与 auto 一视同仁（显式指定同样受白名单约束【测】已复现 `POLICY_DENIED`）；
- 删除死校验 `require(mode != "auto" or bool(allowed))`；
- schema 层不强制（避免两处规则漂移），注册期/执行期校验是唯一权威。

### 7.4 M5 skill 文本（P1）

见 §8。三审第 5 条的四点修正已并入：成本表述、独立评审不得静默降级、本期不依赖 `project diff`、迁移检测不靠 `grep`。

### 7.5 M6 执行配置可见性（P2，本期只做展示）

三审第 5 条：`project diff` 延期，不能作为本期人工确认链的必需步骤。本期改为**在确认现场直接展示**：

| 位置 | 展示内容 |
|---|---|
| `agent list --project P --json` | 每个 profile 的 `model` / `provider` / `endpoint_ref` / `permission_profile` / `cost_class`（已有字段的显式呈现） |
| `project attest --json` | 确认前的 `policy_hash` + `confirmed_profiles[]` + 每个候选的执行配置摘要 |
| `doctor` | `policy_problems[]`（`strict=False` 收集到的问题）+ `placeholders_ok` |

`project diff` 落地后（另一份方案的 B4）再补 `changed_fields` 与 `affected_tasks[]`，不作为本期前置。

### 7.6 M7 legacy 绕过（P0，新发现）

见 §2。这是本轮唯一一个**安全漏洞级**修复，必须与 M3 同批或更早发布。

### 7.7 M0/M8 读路径分层与执行契约（P0）

见 §4。工作量最大、影响面最广（40 处 `store.config()` 调用点）。

---

## 8 文本落地

### 8.1 SKILL.md 新增小节（放在"能力与边界"之后）

```markdown
### 执行配置与模型授权

派发单位是 **profile**，不是一个模型 ID。`routing.allowed_profiles` 是**经 Zoe 派发时允许
使用的 profile 集合**，同时约束显式指定与自动选择；`routing.mode` 缺省 `explicit`。

没有 `routing` 段的历史项目，`agent_profiles` 每一项都视为已授权（"历史配置即授权"），
这与"严格白名单"不是一回事，汇报时不要混用两种说法。

**新配置确认**：声明了 `routing` 段但还没有确认记录时，执行会被 `ALLOWLIST_UNCONFIRMED`
拒绝。此时把候选与每个候选的执行配置（模型、端点、权限档）展示给用户，用户确认后执行
`project attest --project P`；**不要在用户未确认时调用它**。确认记录绑定的是当时的整份
配置摘要，所以之后任何执行配置变化（换模型、换端点、换权限档）都会让确认失效，需要重新
确认一次——这包括"白名单一个键都没变，但 profile 内容变了"的情况。

纪律：

- 不凭 profile 名或模型名推断能力与成本。
- 不自动推断价格：用户通过 `rules[].prefer` 明确表达顺序，或用 `cost_ceiling` 直接排除；
  Zoe 不维护价格表。
- 工具支持或新出现的模型不自动获得授权。**不得**自行修改 `agent_profiles` / `routing` 后
  `register`，把自己的选择变成"用户授权"。
- 模型不可用或任务失败时**不得静默切换**：固定模式暂停，自动模式也不在 `start` 阶段重选；
  换配置只能 `task reassign`（要求 stop-confirmed）。
- `allow_cli_default_model: true` 一律视为"运行模型未解析"，报告写"未解析"；工具事后报告了
  模型也不能倒推为启动前已固定。
- 配置有问题导致 `status` / `doctor` 报 `policy_problems` 时，先把问题原样转述给用户，
  不要用"环境问题"概括；这些命令仍应能读出历史记录，读不出来就是缺陷，要报缺陷。
```

### 8.2 第 5 步"派发"处

```markdown
需要路由时在请求里给 `route.task_type` 与 `route.requirements`（`context_budget` /
`needs_tool_use` / `structured_result` / `input_modalities` / `data_residency`），由 runtime
决策；只有用户已明确指定执行配置时才传 `agent_profile`。角色（实现 / 评审 / 调研）不降低
准入门槛，也不构成自动授权的理由。
```

### 8.3 派发摘要

```text
执行配置：cb-primary
选择方式：项目默认（routing.mode=explicit）
模型：显式指定 <model-id>；报告值 <reported or 未上报>
授权：confirmed（policy_hash 前三后四）
```

### 8.4 宿主评审约束（三审第 5 条修正）

```markdown
用户提出模型或费用限制时，宿主直接发起的额外评审也必须遵守该限制。

若任务或项目要求**独立评审**，则必须取得符合要求的独立评审：经 Zoe 派发一个已授权
profile 的评审任务，或使用具备独立上下文的审查方。**无法确认模型时不要自行启动额外
评审 Agent，也不要用当前会话内的结论替代独立评审**——报告写"独立评审未能进行"并说明
原因，不得静默降级为"已评审"。
```

### 8.5 `network_profile: none`（沿用 v2，未变）

```markdown
`network_profile` / 顶层 `network` 在 `local-process` 后端下是**声明，不是隔离**：doctor
输出 `network: host (local-process: not restricted by Zoe)`。它不表示"禁止 CLI 联网"，也不
表示"禁止执行工具访问网络"。若代表"禁止远程模型调用"，当前后端无法强制；不得把它当作
"数据不出本机"的证据。
```

### 8.6 `decomposition-patterns.md:73` 补充

```markdown
角色只决定**候选**，不决定授权：候选必须落在 `routing.allowed_profiles` 内，且要过同一套
硬条件与环境检查。多个角色共用一个 profile 完全正常；为了"看起来分工"给每个角色配一个
模型，只会增加配置面与未验证组合。
```

---

## 9 兼容与迁移（修正 v2 结论）

### 9.1 旧配置的三层影响（修正后）

| 路径 | v2 的错误结论 | 冻结结论 |
|---|---|---|
| `store.config()` | "不调用 normalize" | **调用 `validate_config`**，40 处受影响 → M0 分层后改为 `strict=False`，只记录问题 |
| `doctor` | "不调用 normalize" | 经 `store.config()` 间接调用 → 同 M0 |
| `policy_digest()` | "不会抛错" | 正确（`except Error` 回退），但两侧口径可能不一致 → 契约校验先行（§4.2） |
| `_binding_for`（legacy 任务） | "唯一风险点" | 仍是风险点，但 M2/M7 的豁免已限定在"无 `agent_profiles` 段"形态，legacy 任务不受影响 |
| `status` / `report` / `events` | "已成立" | **不成立**，经 `store.config()` 会失败 → M0 修复后才成立 |

### 9.2 迁移步骤（替换 v2 的"重新 register"）

```text
1. 升级 runtime
2. 跑 doctor（M0 之后可用）：读 policy_problems[] 与 placeholders_ok
3. 按 problems 逐条修：补 model、删除重复的 allow_cli_default_model、
   删除伪造的 legacy、补 allowed_profiles
4. project register --config <修改后的文件>
5. project attest：确认候选集合（配置内容未变也允许）
6. 对在途任务 rebind（policy_hash 变化时）
```

关键修正（三审第 2 条）：**不能**用"重新注册相同内容"来证明历史来源，因为

- 已注册配置可能已被旧版补全了白名单，重新注册不会缺 `allowed_profiles`；
- 相同内容重新注册**未必改变摘要**，因此不保证触发 `POLICY_CHANGED`。

正确的确认动作是 `project attest`：它读当前配置、计算 `policy_hash`、写入确认记录，**不依赖摘要变化**。

### 9.3 迁移检测不靠 `grep`

三审第 5 条正确：`grep` 无法判断嵌套 JSON、字段组合与来源。改用**同一个纯校验函数**输出诊断：

- `doctor` → `policy_problems[]`（每条含 `field` / `code` / `reason`）；
- `project attest --dry-run` → 同样的诊断 + `would_write`；
- 不新增迁移框架、不新增命令（`attest` 本身已必需）。

### 9.4 五条迁移要求的落点（二审第七节）

| 要求 | 落点 |
|---|---|
| 升级前检测旧配置 | `doctor` 的 `policy_problems[]`（M0 后可用） |
| 只读命令仍可用 | M0 的 `strict=False`（这是本轮必做项，不是"已成立"） |
| 新执行与恢复明确阻断 | `check_execution_contract` 在执行入口抛具体错误 + `ALLOWLIST_UNCONFIRMED` |
| 修复后摘要与在途任务 | `register` → `policy_hash` 变化 → `rebind` 五步【核】`agent-routing-design.md:879-901` |
| 历史决策记录不改写 | `route-<rev>.json` 有 `content_sha256`，`start` 只校验不重写【核】`_routing.py:1231-1278`；补断言"旧文件逐字节不变" |

---

## 10 验收

### 10.1 方案十二场景

| # | 场景 | 现状 |
|---|---|---|
| 1 | 十几个模型只授权两个 | 满足；候选 = 交集，只对候选跑 `doctor`【核】`_routing.py:1330-1348` |
| 2 | 新增模型不自动授权 | M3 + attest 后满足（新增 profile 会使 `policy_hash` 变化） |
| 3 | 无指定 → 固定默认 | 满足【核】`_routing.py:874-877` |
| 4 | 自动模式只在授权内选 | 满足 |
| 5 | 昂贵模型未授权不被调用 | 满足；`cost_ceiling` 是排除层 |
| 6 | 显式模型不可用 → 不静默退回 | **部分满足**：argv 固定；识别延期 v1.6.0，统计不污染（§6） |
| 7 | 身份未知 → 记录未知 | 满足【核】`_profiles.py:131-141` |
| 8 | 额外参数覆盖模型 → 拒绝 | 满足【核】`base.py:137-145`、`:922-931` |
| 9 | 单 profile 项目正常执行 | 满足（`only_eligible`） |
| 10 | 切换不复用不兼容 session | 满足（`session_mode: new`） |
| 11 | 授权撤销后恢复被阻止 | 满足（`rebind` 3b） |
| 12 | 升级不自动开启路由 | 满足【测】 |

### 10.2 二审十条 + 三审补充

| 场景 | 应有结果 |
|---|---|
| explicit 声明 `routing` 未声明白名单 | `INPUT_INCOMPLETE`（M3） |
| 新增昂贵 profile 后显式请求 | 未授权 `POLICY_DENIED`；在白名单内则 attest 失效 → `ALLOWLIST_UNCONFIRMED` |
| 已授权 profile 原地改 model / endpoint | `policy_hash` 变化 → attest 失效；在途任务 `POLICY_CHANGED` |
| `model` + `allow=false` | 通过（不误伤） |
| `model` + `allow=true` | `INPUT_INCOMPLETE`，message 含两种修法 |
| `model` 为 `""` / `"   "` / `" m-a "` / 非字符串 | 四种都拒绝 |
| 未替换占位符 | 启动前拒绝，**spawn 次数 0** |
| 旧配置升级后读取与恢复 | `status`/`report` 可读（M0）；`create`/`start` 报具体字段；legacy 任务不受影响 |
| 模型拒绝 vs 实现失败 | 事实与费用保留；不进 `pass_rate_attributed` 分母 |
| 失败后重新 `start` | 不隐式换模型 |
| `reassign` 时 `PROCESS_UNKNOWN` | 拒绝（已有【核】`_pipeline.py:1025-1028`，补回归） |
| 白名单撤销后恢复 | 不继续使用已撤销候选 |
| **用户声明 `legacy: true`** | `POLICY_DENIED`（M7） |
| **端点 `class` 变化（`endpoint_ref` 名不变）** | `combination_id` 变化 + attest 失效 |

### 10.3 真实 CLI 验收（限定证明范围）

三审第 6 条正确：模拟测试只能证明 Zoe 构造了什么，不能证明工具内部用了什么。

| # | 验证 | 能证明 | **不能**证明 |
|---|---|---|---|
| R1 | 显式 `model` 传到 CLI | 脱敏 argv 记录 + `argv_audit_sha256`（**仅证明该记录的完整性**）+ 适配器单测 | CLI 实际用了该模型 |
| R2 | 无效模型 ID 后无第二次 Zoe 尝试 | **Zoe 没有重派** | CLI 内部是否回退 |
| R3 | `reported_mismatch` 可被识别 | 结构化输出夹具下 `model_identity=reported_mismatch` 的判定正确 | 真实工具能否被制造成不一致 |
| R4 | `allow_cli_default_model` 运行标"未解析" | 报告与 run 记录一致 | 实际用了哪个模型 |

执行纪律：

- R3 **不要求**用真实工具制造不一致，用夹具验证判定逻辑即可；
- 双 adapter 是**"宣布这两种组合受支持"**的条件，不阻塞已完成验证的单 adapter 发布；
- **不得**为了凑数量使用用户未授权的模型。

### 10.4 四条发布阻断测试（三审建议，全部采纳）

| # | 场景 | 断言 |
|---|---|---|
| P1 | 旧 M2 非法配置**保持不变**、摘要相等时启动 | `check_execution_contract` 抛 `INPUT_INCOMPLETE`，**不是** `POLICY_CHANGED`，`spawn` 次数 0 |
| P2 | 重新注册**完全相同**的配置后完成确认 | `project attest` 成功写入；`policy_hash` 允许不变；不依赖摘要变化 |
| P3 | 身份未知的成功运行 / 环境失败 / `reported_mismatch` | 三者均不进 `n_attributed` 分母；`pass_rate_attributed` 分母为 0 时为 `null` |
| P4 | 占位符在任何 Agent 启动前被发现 | 错误 `details.fields[]` 指向具体字段；`spawn` 次数 0；`doctor.ready=false` 且 `placeholders_ok=false` |

---

## 11 实施顺序与估时

| 序 | 项 | 编码 | 含迁移/联测 |
|---|---|---|---|
| 1 | M7 legacy 绕过（P0 漏洞，改动最小，先发） | 0.2 | 0.3 |
| 2 | M0 读路径分层（`strict` 参数 + `store.config` 改造 + 40 处回归） | 1.0 | 1.5 |
| 3 | M8 执行契约校验（统一入口 + 五个调用点 + 错误码） | 0.8 | 1.2 |
| 4 | M3 白名单显式 | 0.4 | 0.7 |
| 5 | M2 四情形 + 输入形态 | 0.4 | 0.6 |
| 6 | M1 三档示例 + 占位符前移 + `doctor` 统一 | 0.6 | 0.9 |
| 7 | §5 attest（命令 + 文件 + 执行期检查） | 0.7 | 1.0 |
| 8 | §6 统计三条件 + `PerformanceRecord` 增量 | 0.6 | 0.8 |
| 9 | M5 skill 文本 + `build.py` REQUIRED_FILES | 0.5 | 0.5 |
| 10 | 文档同步（runtime-contract / cli-contract / upgrading / capabilities） | 0.5 | 0.7 |
| 11 | 跨仓联测 + 版本兼容测试 | — | 0.5 |
| 12 | 真实 CLI 验证 R1–R4 | — | 1.0 |

**编码 5.7 人日；含迁移、联测、真实 CLI 验证 9.7 人日。** 真实 CLI 验证耗时单独记录，不计入计划估算（三审要求）。

发布纪律：

- 第 1 项（M7）可独立先发：只拒绝用户伪造的 `legacy`，不改变任何合法配置；
- 第 2 项（M0）是其他项的前提，必须先合并：否则新校验第一次生效就是"连 `status` 都打不开"；
- 第 3 项起每次提交必须更新 `docs/upgrading.md` 与 `cli-contract.md` 的错误码表；
- 两仓配套提交：skill 侧在 `SKILL.md` 声明"`ALLOWLIST_UNCONFIRMED` / 占位符预检需要 `zoe-runtime >= 1.5.0`"，CI 跑兼容测试。

---

## 12 回滚

| 触发 | 动作 | 影响 |
|---|---|---|
| M0 分层引发意外放宽 | 把 `store.config()` 改回 `strict=True` | 回到"配置非法则全命令失败"的旧行为；不引入新风险 |
| attest 阻断过严 | `project attest` 重新确认即可；不改代码 | 只影响需要确认的项目 |
| M3 挡住注册 | 补 `allowed_profiles`，再 `attest` | `policy_hash` 变化 → 在途任务 `rebind` |
| 统计口径被质疑 | `pass_rate` 与 `pass_rate_attributed` 并存，可回看全量 | 历史不重算 |
| 想退回"不路由" | `routing.mode: explicit`（不要删整段） | 白名单与 attest 保留可审计 |
| 完整删 `routing` 段 | 先结束/暂停在途任务 | 删段改 digest 并移除白名单，不是无影响回退 |

---

## 13 附录

### 13.1 现状锚点（2026-10-30，v1.4.1 未发布）

```text
zoe-runtime/src/_store.py:551-553           store.config() 调用 validate_config（v2 结论错误来源）
zoe-runtime/src/_config.py:333-462          validate_config；459 调 normalize_config，460-461 调 resolve_binding
zoe-runtime/src/_config.py:347-353          ready 已含 checks_preflight_ok（占位符按同层并入）
zoe-runtime/src/_config.py:393-395          顶层重复暴露 checks_preflight（占位符同位置）
zoe-runtime/src/_profiles.py:131-141        model_unresolved：只看启动前配置
zoe-runtime/src/_profiles.py:148-159        policy_digest：normalize 副本，异常回退 digest(c)
zoe-runtime/src/_profiles.py:193-212        _legacy_profile()：legacy 标记的系统来源
zoe-runtime/src/_profiles.py:273-279        model 必填校验（M2/M7 插入点）
zoe-runtime/src/_routing.py:191-217         allowed_profiles 缺省全量 + 死校验（M3）
zoe-runtime/src/_routing.py:371-408         combination_id：含 endpoint {id, class}
zoe-runtime/src/_routing.py:1462-1464       requested=None → model_identity=unobserved（不可倒推）
zoe-runtime/src/_routing.py:1632-1665       attribution()：M4b 修改点
zoe-runtime/src/_agents/base.py:83-118      PROFILE_FIELDS（含 legacy，第 99 行——M7 来源）
zoe-runtime/src/_agents/base.py:137-145     RESERVED_PROFILE_KEYS
zoe-runtime/src/_agents/base.py:557-561     model 只校验"非空字符串"（空白缺口）
zoe-runtime/src/_agents/base.py:673-677     allow_cli_default_model 仅校验布尔
zoe-runtime/src/_pipeline.py:56-60          _binding_for：仅 legacy 配置才 normalize
zoe-runtime/src/_pipeline.py:336            create 读 store.config
zoe-runtime/src/_pipeline.py:1034-1038      start：policy_digest vs digest(store.config())（M8 插在其前）
zoe-runtime/src/_pipeline.py:1682-1687      reassign：digest 比对（M8 插在其前）
zoe-runtime/src/_pipeline.py:2496-2511      combination_changed 事件（不阻断）
zoe-runtime/src/cli.py:115-125              退出码表（新增 ALLOWLIST_UNCONFIRMED 的位置）
zoe-runtime/src/cli.py:811/821/1577         status / doctor 经 store.config（M0 影响面）
zoe-runtime/src/_context.py:39              orchestration 经 store.config（M0 影响面）
zoe-runtime/tests/test_contract_artifacts.py:139-143  示例只过 JSON Schema（M1 漏测根因）
zoe-workflows/build.py:31-38 / 45-55        WHITELIST / REQUIRED_FILES
zoe-workflows/references/project-profile.example.json:10-31  两 profile 无 model（P0）
zoe-workflows/SKILL.md:174-177 / 283-285    第 5 步 / 会话内审查 Agent
zoe-runtime/CHANGELOG.md:6                  当前未发布版本 1.4.1
```

### 13.2 新增错误码

| 码 | exit | 语义 | `details` |
|---|---|---|---|
| `ALLOWLIST_UNCONFIRMED` | 5（`task create`/`start` 沿用 v0.2 的 4） | 项目声明了 `routing` 但没有可信的确认记录，或其 `policy_hash` 已失效，或记录本身不合规 | `policy_hash`、`candidates[]`、`reason`（`missing` / `policy_changed_since_attestation` / `candidate_not_confirmed` / `record_invalid`）、`next`（命令） |

不新增其他错误码：配置字段问题复用 `INPUT_INCOMPLETE`，授权越界复用 `POLICY_DENIED`，漂移仍是 `POLICY_CHANGED`。

### 13.3 三版结论对照

| 问题 | v1 | v2 | v3 |
|---|---|---|---|
| 剩余缺口 | 五处 | 五处（含兼容） | 八项（M0–M8），其中 M0/M7 是新发现 |
| `store.config()` 是否重新校验 | 未讨论 | 错误地认为"不校验" | 确认调用 `validate_config`，需分层 |
| 授权来源 | `routing.allowlist_source` 字段（拍板） | 同上 | 独立 `attest` 文件，不进用户配置 |
| 统计判定 | `cause_confirmed` 单布尔 | 同上 | 三条件组合 + `pass_rate_attributed` |
| 人日 | 2.5（仅编码） | 3.3 编码 / 6.9 全量 | 5.7 编码 / 9.7 全量（真实验证另计） |
