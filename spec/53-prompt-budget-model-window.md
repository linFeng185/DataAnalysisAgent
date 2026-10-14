# 提示词预算跟随超管模型窗口（动态化设计）

日期：2026-10-14。方向已与用户确认（全注册表统一接窗口）。本设计修复 2026-10-13 测试环境事故（分析模型收不到结果数据），并把提示词层预算从"硬编码"升级为"按超管配置的模型窗口动态推导"。

## 一、背景与根因

2026-10-13 测试环境实证：可信租户（知识工具启用）下，`analysis.result` 调用中模型收到的结果样本仅 173 字符——恰好在第一个数值前被截断；模型据此报告"数据被截断、无法给出产量数字"，图表决策链（aggregation 判定）随之卡死。

本地探针复现（120 行 × 4 列车间数据，真实代码）：

| 环节 | 实测 |
|---|---|
| `analysis.result` 注册预算 | 7000 字符（硬编码） |
| 固定 System 渲染后 | 2673 字符 |
| 知识工具预留（schema + 6000 + 800），被 clamp 到预算一半 | 3500 字符 |
| 留给全部动态 section | ≈ 800 字符 |
| `result_sample` 实际分到 | **173 字符**（首个数值前截断） |
| `statistics`、`authorized_schema`、`business_context` 等 | 0 字符（全部裁空） |

深层问题是两层机制分裂：**会话历史层**已按超管窗口动态（`resolve_context_window` → `get_context_budget` → LLM 摘要压缩）；**提示词层**仍是硬编码字符预算 + 字符级腰斩。本次事故出在后者。同时 `spec/51` 已明确约束"保持完整 JSON，不能硬截断序列化字符串"，`result_sample` 的现实行为违背了该约束。

## 二、设计目标

1. 提示词预算跟随超管配置的模型窗口动态推导；注册值从"总预算"升级为"设计上限"。
2. 模型可见数据量由数据层采样单阀门决定；提示词层不再对序列化 JSON 做字符腰斩。
3. 供应商差异（窗口、tokenizer 效率、输出上限）全部由既有模型能力链吸收，字段缺失有多层兜底。
4. 大窗口不浪费、小窗口自动收敛、超小配置显式失败。

## 三、核心机制

预算取值公式（`build_budgeted_prompt` 内生效）：

```
实际预算 = min(注册上限, max(下限, 窗口推导值))
窗口推导值 = usable_input_tokens × 换算系数 K
```

1. **窗口与推导值**：复用会话层的 `get_context_budget()`（`src/memory/context_builder.py`）——同一条超管配置读取链（租户 LLM 选择 → 模型对象能力 → 模型注册表 → 供应商适配器声明 → 兜底 128K）。`usable_input_tokens = 窗口 − 固定预留（默认 12000）− 输出预留（max(默认 8192, max_tokens_limit)）`。
2. **换算系数 K**（token → 字符）：默认 `1.0`，即"1 token 按 1 字符折算"——对中文优化 tokenizer 是安全口径（实测依据：中文 ≈ 0.6 token/字符、英文 ≈ 0.3 token/字符，任何混合恒 < 1，1.0 下留有约 40% 安全余量），宁可装不满窗口、绝不超窗。支持按模型覆盖：读取顺序为模型 `capabilities.prompt_budget_char_factor` → 全局设置 `prompt_budget_token_char_factor`（默认 1.0）。**对中文费 token 的 tokenizer（1 个汉字需 1.5~2 token，部分国外供应商），必须把该系数调小（约 0.5），否则按 1.0 推导的字符预算换算回真实 token 仍可能超窗。**
3. **注册上限**：`PromptDefinition.context_budget` 的语义从"实际预算"改为"设计上限"——内容再多也不突破的封顶（防呆）。
4. **下限**：全局保底（默认 8000 字符），防超小窗口把预算推导成负数或装不下固定提示词。
5. **调用顺序调整**：`prepare_invocation` 先解析模型与窗口（调 `get_context_budget(model=...)`），再构建预算化 Prompt；解析结果加请求级缓存，避免每次 LLM 调用新增配置查询。
6. **工具预留保持现有语义**：`reserve_chars` 仍从预算内扣除、clamp 到一半预算——动态预算下预留占比很小，不再饿死内容。

## 四、注册值重审（设计上限表）

| Prompt ID | 现在 | 重审后上限 | 依据 |
|---|---|---|---|
| `analysis.result` | 7000 | **85000** | 分段上限之和 78400 + 固定 2673 + 余量 |
| `sql.generate` | 85000 | 85000（保留） | schema 60000 + grounding 10000 的既有设计 |
| `context.summary` | 3000 | 5000 | 输入限 2000 token + 摘要输出，留余量 |
| `attribution.select` / `analysis.polish` / 报表类等 | 各自写死 | 实施时按"现有值 × 内容构成 + 固定长度核对"逐项重审 | 换模型后不再有挤兑风险 |

效果（`analysis.result`）：128K 配置 → min(85000, ≈108000) = 85000；1M 配置 → min(85000, ≈980000) = 85000（被内容上限封顶，非浪费）；32K 配置 → 自动收缩到 ≈12576。

> 窗口示例按输出预留 8192 口径；实际可用输入取决于模型适配器声明的 `max_tokens_limit`（如声明 16384 时，
> 128K 窗口 ≈ 99616、32K 窗口 ≈ 4384——后者由全局下限 8000 兜底）。

## 五、数据层单阀门（含标签语义）

**问题**：现状数据层固定按 `analysis_data_max_chars=50000` 准备样本，提示词层再字符腰斩——两道关卡失配，产生残片 JSON。

**设计**：
1. 数据层采样量跟随本调用的动态预算：`数据段可用字符 = min(设置上限, max(2000, 实际预算 − 固定 System − 其余段上限之和))`（设置上限封顶最强，尊重操作员的数据量配置），`_to_compact` 参数化接收该值。
2. `result_sample` 段上限同步重审为 **55000**（覆盖数据层上限 50000 + 标题余量）；提示词层不再对它做字符腰斩（字符截断仅保留为异常防线）。
3. 标签如实：数据全量进入 → "输入全量 N 行"；被采样 → "输入分源均匀采样 X/N 行"（现有 `_to_compact` 标签逻辑保留）。
4. 实施顺序：`analyze_result` 节点先解析动态预算（与 `prepare_invocation` 共用缓存结果，可通过可选参数传入避免重复解析）→ 计算数据段可用量 → 采样 → 组装 sections。

效果：128K/1M 配置下 120 行（1.19 万字符）全量进入且 JSON 完整；32K 配置下数据层均匀采样出约 20 行（标签如实），JSON 依然完整、不再出现残片。

## 六、分段上限重审（`analysis.result`）

| 分段 | 现在 | 重审后 | 说明 |
|---|---|---|---|
| `result_sample` | 4500 | **55000** | 见第五节 |
| `statistics` | 2200 | 8000 | 多列统计摘要空间 |
| `query` / `completeness` / `sample_scope` / `sql` / `supplemental_evidence` / `deterministic_findings` / `authorized_schema` / `business_context` / `history` | 现状 | 保留 | 各段现状数值即合理设计上限 |

## 七、供应商差异处理

1. **窗口不同**：per 模型配置（租户连接 → 模型目录 `capabilities.context_window`），运行时按当次请求模型解析，天然自适应。
2. **tokenizer 效率不同**：由换算系数 K 按模型覆盖（见第三节第 2 条；中文优化口径默认 1.0，中文费 token 的模型调小约 0.5）。
3. **`max_tokens_limit` 字段**：纯系统内部字段，不传大模型；仅用于输出预留计算。读取链为租户配置 → 模型对象 → 内置注册表 → 适配器声明；全部缺失时输出预留按设置默认 8192 兜底，且该字段在超管界面为必填（默认 8192），实际很难缺失。本设计不新增该字段逻辑。
4. **输出预留边界**：各调用点请求的输出上限（`max_tokens`，如分析任务 2048）应始终 ≤ 输出预留（现状 8192，富余 4 倍）；将来若放开长输出任务，需同步核对本条。

## 八、边界行为

1. **超小窗口配置**（如 16K）：预算收缩到下限 8000；若固定 System 仍超限，维持现有 `ValueError` 显式失败（配置错误应暴露，不静默砍废）。
2. **工具预留 clamp**：保留 `min(reserve, 预算 − 固定, 预算//2)` 现有逻辑。
3. **解析缓存**：窗口/预算解析加请求级缓存；重复解析不产生额外数据库查询。
4. **无租户配置环境**（本地开发、单测）：全部走兜底 128K，行为与今天兼容。
5. **LLM 摘要调用**（`context.summary`）同样走动态预算；其上限 5000 在任意窗口下自然满足。

## 九、测试与验收

1. **守线（防回归）**：128K 配置下 `analysis.result` 实际预算 ≥ 85000，且 120 行样本（约 1.19 万字符）完整进入 Human 消息、JSON 可解析。
2. **窗口阶梯**：1M 配置 → 预算放大且被上限封顶；32K 配置 → 收缩到 ≈12576、样本被行级采样、标签如实；16K 配置 → 下限 8000 生效。
3. **字段缺失**：`max_tokens_limit` / `prompt_budget_char_factor` 全部缺失时走默认值，无异常。
4. **换算系数覆盖**：模型 capabilities 提供系数时优先于全局设置。
5. **更新现有测试**：`test_prompt_budget.py`、`test_prompt_registry.py`、`test_sample_policy.py` 中按旧语义（"context_budget 即实际预算"）断言的用例适配新语义。
6. **真实环境验收**：测试环境重跑"各车间产量"问题——模型能看到全量 120 行、`aggregation` 决策恢复、图表按规则成功或明确失败。

## 十、对既有 SPEC 的修订（实施时同步）

1. **修订 `spec/13-prompt-templates.md` §12.5 第 4 条**：`context_budget` 语义从"统一总预算"修订为"设计上限 + 按超管模型窗口动态推导（推导公式与换算系数见本文件）"。
2. **修订 §12.5 第 5 条**：追加"数据段不得对序列化 JSON 做字符级腰斩；模型可见数据量由数据层行级采样单阀门决定"（与 `spec/51` 的既有约束对齐）。
3. 实施完成后按项目规则更新 `features/` 对应功能点状态。

## 十一、不在本次范围

1. 会话注入层温摘要每轮重算的缓存/持久化优化（另一条优化线，后续单独排期）。
2. 预算器从"字符 + 折算系数"迁移到纯 token 计量（本次保留迁移空间：K 的读取链即为接口）。
3. `sql.generate` 等其余 Prompt 的固定 System 长度核对（实施第 4 节重审时顺带记录，如有超限另行处置）。
4. PDF/DOCX 上传路径（`src/knowledge/document_assets.py` 的 `chunk_document`）存在同族切分缺口：
   无重叠实现、丢弃式过滤残留、块级 strip——本轮切分修复仅覆盖 TXT/MD 路径；建议下一轮以同一
   "出口收口"思路修复。

## 十二、实施影响面（文件级）

| 文件 | 改动 |
|---|---|
| `src/llm/prompt_budget.py` | 预算公式接入动态推导与下限；预算单位说明更新 |
| `src/llm/invocation.py` | `prepare_invocation` 调整顺序（先解析窗口）；支持传入已解析预算避免重复计算 |
| `src/llm/prompts.py` | 注册值重审（上限表）；第 3 节系数读取链 |
| `src/llm/capability_schema.py` | 可选字段 `prompt_budget_char_factor`（非必填，默认读全局设置） |
| `src/config.py` | 新增全局设置：换算系数（默认 1.0）、预算下限（默认 8000） |
| `src/graph/nodes/analyze_result.py` | 数据采样先于提示词组装（跟随动态预算）；分段上限重审 |
| `tests/`（test_llm / test_memory / test_graph） | 九节所列新增与更新用例 |

---

## 验收记录（2026-10-14）

- 全量回归：`pytest tests/ -m "not live_llm"` 修复 `config/app.example.yaml` 未登记新增预算字段后复跑，
  3482 passed / 30 skipped；唯一失败为既有无关项（`test_architecture_remediation.py` 的异常回退日志
  测试，指向 `src/llm/knowledge_loop.py:520` 与 `src/tools/knowledge_search.py:450`，两文件不在本改造
  改动面且工作区未触碰，属 HEAD 既有）；ruff 检查：本改造文件 0 告警，其余 13 项为既有告警。
- 端到端探针（128K 兜底窗口）：`analysis.result` 动态预算 = 85000；数据段预算（生产函数）= 50000；
  120 行样本约 1.2 万字符完整进入提示词、无截断标记——修复前同场景样本仅 173 字符。
- 与设计的偏差（如实记录）：请求级缓存未新增（核实解析链为纯内存，无数据库查询）；换算系数 1.0 补充
  实测注记（中文 ≈0.6 / 英文 ≈0.3 token/字符，见第三节）；数据段预算与设置上限的优先级裁定为
  "设置上限封顶最强"（第五节）。
- 注册值重审与全量安全核对：Task 4 审查枚举全部 22 个注册 Prompt 的"固定长度 vs 有效预算下限"对照表，
  全部安全（最紧余量 475 字符）；全注册表守线断言已加入 `tests/test_llm/test_prompt_registry.py`。

## 十三、审计修订（2026-10-14，审计副本追加）

审计发现：注册值重审（第四节）把 `analysis.result` 的上限从 7000 升到 85000，但知识工具循环的
消息预算 `_resolve_message_budget`（`src/llm/knowledge_loop.py`）仍直接读取
`PromptDefinition.context_budget`（注册上限）。在本设计的语义下注册值只是"设计上限"，因此小窗口
租户（如 32K）下工具循环会放行最多 85000 字符的消息总量（远超窗口推导值 12576，按中文
≈0.6 token/字符折算约 5.1 万 token），工具证据可把整轮请求推过模型窗口；`sql.generate`
（上限 85000）在本次修订前同样受影响。

修订（最小修复）：

1. `PreparedInvocation` 新增 `budget_chars` 字段（`src/llm/invocation.py`），由
   `prepare_invocation` 写入本次调用的动态推导预算（`budgeted.budget`）。
2. `_resolve_message_budget`（`src/llm/knowledge_loop.py`）读取顺序修订为：显式
   `config.message_budget_chars` → 调用快照 `prepared.budget_chars` → 注册上限回退。
   回退保证直接构造调用快照的既有调用方与测试行为不变。
3. 与提示词层预留对齐：预算器按 `schema + 6000 + 800` 预留的空间（第八节第 2 条）与循环的
   消息总量判定现在同源，小窗口下工具能否挂载由同一次调用的动态预算裁决。

回归测试：`tests/test_llm/test_dynamic_prompt_budget.py::TestToolLoopBudgetPropagation`
（动态预算跟随 + 注册上限回退两条路径）。

影响面补充（第十二节表格）：`src/llm/invocation.py` 增加预算快照字段；
`src/llm/knowledge_loop.py` 调整消息预算读取顺序。
