# SQL 探查与数据画像设计（键列判断 · 画像预取 · 结构化探查）

> 让大模型"先了解数据，再生成 SQL"：生成前的键列语义判断与数据画像预取（确定性），配合按需的结构化探查原语（模型自主），从源头减少"单列不唯一、组合唯一"造成的静默合并与模糊匹配；渲染闸门（spec/52 合并确认闸门）与分析追问作为兜底。

---

## 1. 背景与实证依据（2026-10-14 实机复现）

**复现 1（"今年各车间的产量是多少？"）**：t_workshop 约 120 行、8 个车间名称各 15 行；生成 SQL 为 `GROUP BY w.id, w.workshop_name` 但 SELECT 未输出任何区分列，结果 120 行同名；三轮分析模型对 `chart_plan.aggregation` 在 sum/none/sum 间摇摆，最终静默合并渲染成 8 根柱（首柱 8,021,610 = 15 个实体之和）。

**复现 2（"华东制造基地的各车间产量分别是多少？"）**：生成 SQL 用 `factory_name LIKE '%华东%'` 模糊匹配命中 3 家工厂 × 8 个同名车间 = 24 行；两轮补查在 `region='华东'`（0 行命中）与 `LIKE '%华东%'` 之间摇摆；同样出现模型自给的 sum 径行合并。

**可见性核验（关键结论）**：
- 线上 schema 中 t_workshop 共 6 列，含 `workshop_code`（车间编码，注释完整）；生成链路裁剪规则（`_PROMPT_COLUMNS_PER_TABLE=32`、`_INTERNAL_COLUMN_MARKERS` 不含 code 类词、`format_schema_for_prompt` 渲染列名+类型+注释）证明该列必在 SQL 生成 prompt 内；
- 模型在补查 SQL 中实际写过 `f.factory_code`，在分析 related_metrics 中引用过 `t_workshop.workshop_code`；
- **症结不是"取不到列"，而是"没有带上它的动机"**：schema 只回答"有哪些列"，不回答"数据长什么样"（唯一性、重复度、值域属于数据分布事实，不在表结构里）。模型按"问车间→按名称分组→输出名称"的直觉写 SQL，没有任何信号提示它名称会重复。

**结论**：需要三项能力——生成前的**键列语义判断**（哪些列是区分键）、**数据画像预取**（键列与展示列的真实分布）、以及按需的**结构化探查**（个案确认歧义值与边界）。

## 2. 目标与非目标

**目标**
1. 首轮 SQL 在存在同名实体的场景下保留可区分的键列（不指定具体列名，任何数据源成立）；
2. 实体名过滤基于真实值域（精确值优先，歧义时呈现全部候选而非静默合并）；
3. 画像与探查的数据库/模型开销全部有预算且可配置，缓存命中时零额外查询。

**非目标**
- 不做自由 SQL 探查（不接受模型自写探查语句，见 §7 安全边界）；
- 不新增"暂停提问"交互通道（歧义按 §8 在现有体系内处理）；
- 不改报表链路行为（画像/探查首期只服务对话 SQL 生成）。

## 3. 总体架构

```
retrieve_schema（选出相关表）
      ↓
[新增] 键列判断调用（轻量 LLM，结构化输出；按表结构哈希缓存）
      ↓
[新增] 画像预取 DataProfiler（只对判断选出的候选列采样，TTL 缓存）
      ↓
裁剪保护（键列强制保留，不被每表 32 列配额挤掉）
      ↓
generate_sql（prompt 注入画像段与键/展示配对提示；工具循环新增探查原语）
      ↓
渲染闸门（spec/52，已上线兜底）→ 分析追问（语义歧义交用户）
```

共享组件：**DataProfiler**（新模块 `src/datasource/profiling.py`）——列级数据画像采集；键列判断与探查工具分别位于 LLM 调用层与工具层，共用它的方言适配与执行通道。

## 4. 键列判断调用（schema_key_detection）

- **时机**：`retrieve_schema` 确定相关表之后、画像预取之前。
- **输入**：相关表结构（表名/列名/类型/注释/主键标记）+ 用户问题。
- **输出**（Pydantic 结构化契约，例）：每表 `{ key_columns: 实体键/编码/唯一标识列, display_columns: 展示名类列, foreign_keys: 外键列, profile_columns: 画像候选列 }`，附一句理由。
- **三个用途**：
  1. **裁剪保护**：key_columns 并入 `_required_schema_columns` 的必保集合，列数超配额时不被挤掉；
  2. **画像候选列来源**：替代"命名启发式"决定画像探哪些列（原设计最弱环节）；
  3. **prompt 配对提示**：向 SQL 生成注入"`id/workshop_code` 是实体键、`workshop_name` 是展示名"的语义事实。
- **缓存**：按（datasource, 表结构哈希）缓存——实体键是结构属性，与具体问题弱相关；问题相关列仍走现有确定性打分 `_column_match_score`，不重复消耗。
- **降级**：调用不可用/超时/解析失败 → 回退现有启发式（`is_primary_key`、`is_indexed`、关系 `join_key`、`*_id`/`*_code` 命名），不阻塞主流程；此时画像候选列取"问题命中列（`_column_match_score`）+ 名称/编码/外键命名列"。
- **预算**：`schema_key_detection_enabled` 开关 + `schema_key_detection_max_tokens` 上限。

## 5. 数据画像（DataProfiler）

新模块 `src/datasource/profiling.py`，与 `introspection.py` 并列，复用其行数估算与方言工具：

- **行数**：复用 `introspection.estimate_row_count`（多方言）。
- **列画像**：对候选列逐列执行 `SELECT 列, COUNT(*) AS c FROM 表 GROUP BY 列 ORDER BY c DESC LIMIT 8`，判定：
  - 返回 1 行且频次≈行数 → 唯一（实体键候选）；
  - 多个值 → "N 个值，最高重复 ×M"（单列不唯一）；
  - 实现：任一列失败只跳过该列，任何失败都以 warning 记录并降级，绝不阻塞主流程（同 `sample_value_formats` 既有模式）。
- **大表降级链**：估算行数 ≤ `sql_profiling_full_scan_row_limit`（默认 5 万）→ 直接 GROUP BY；超限 → 先采样（子查询 LIMIT 后 GROUP BY）；方言不支持或仍失败 → 该列不画像。
- **方言适配**：标识符引用与采样查询沿用 introspection 的方言工具（`_quote_sampling_identifier` 等），覆盖 MySQL/ClickHouse/PostgreSQL 及既有方言清单。
- **产出**（注入 prompt 的紧凑文本示例）：

```
## 数据画像（生成 SQL 前必读）
t_workshop（约 120 行）
  - id: 唯一（120 个值）【实体键】
  - workshop_code: 唯一（120 个值）【编码】
  - workshop_name: 8 个值，最高重复 ×15 —— 单列不唯一，必须与 id/workshop_code 组合【展示名】
  - factory_id: 15 个值【外键】
t_factory（约 5 行）
  - factory_name: 5 个值（含"华东"字样的 3 个）；region: 华东区、华北区…【展示名/分类】
```

- **缓存**：进程内按（datasource, table, 列集合）TTL（`sql_profiling_ttl_seconds`，默认 300 秒）；命中零查询。

## 6. 画像预取、裁剪保护与 prompt 注入

- **挂载点**：`retrieve_schema` 内（相关表确定后调用，产出随 schema 上下文流转），失败静默降级。
- **裁剪保护**：键列判断结果并入 `_required_schema_columns` 必保集合；无判断结果时保留现有启发式。这修正了"主键/索引之外只剩配额"的现状风险（本次线上 schema 中 `workshop_code` 无索引标记，仅靠 32 列配额幸免）。
- **prompt 规则**（`sql.generate` 系统提示新增三条）：
  1. 分组/展示维度单列不唯一时，**SELECT 必须带出可区分的键列**（画像中标记唯一/编码的列），不得只输出展示名；
  2. 实体名过滤**优先使用画像中的精确值**；不存在精确匹配时，返回全部候选实体明细并说明歧义（禁止静默合并同类实体，与 spec/52 闸门一致）；
  3. 涉及具体实体、值域、时间边界而画像未覆盖时，先用 `probe_data` 确认，再生成正式 SQL。
- **注入位置**：新增 PromptSection `data_profile`（高优先级，与 schema 段同级，受既有预算器裁剪；超预算时按重要性保留键列与重复度结论）。

## 7. 结构化探查工具（probe_data）

- **挂载**：统一工具循环（`src/llm/knowledge_loop.py`），`generate_sql` 任务可见；次数与超时预算独立计数，不复用知识检索配额。
- **五个固定原语**（模型只填参数，**不接收自由 SQL**）：

| 原语 | 参数 | 返回 |
|------|------|------|
| `entity_lookup` | table, column, pattern | 按字面/模糊查找实体候选清单（精确匹配优先排前） |
| `value_domain` | table, column | 列值域+频次（TOP N） |
| `column_repeat` | table, column | 唯一性判定与最高重复值（与 value_domain 共用同一查询，仅返回字段窄化） |
| `time_range` | table, column | MIN/MAX 时间边界 |
| `table_sample` | table, columns | 样例行（≤ limit） |

- **安全边界**：
  - 表名必须命中本次请求的 `relevant_tables` 白名单；列名必须在表结构内；
  - 标识符引用走方言 quoting，拒绝非法标识符（防注入）；
  - LIMIT 强制 ≤ `sql_probe_limit`；行级结果截断返回；
  - 执行通道复用 `src/security/sql_execution.py` 的 `validate_and_execute_sql`（只读校验+限行）；
  - 调用次数 ≤ `sql_probe_max_calls`、单次超时 `sql_probe_timeout_seconds`，耗尽后工具返回明确的"预算耗尽"结果，不抛异常。
- **返回契约**：`{success, columns, rows, row_count, truncated, error}`；任何失败以结构化错误返回，由模型自行决定继续或放弃。

## 8. 歧义处理（已确认产品行为）

探查/画像发现实体名不唯一或不存在精确匹配时：**SQL 返回全部候选实体明细且不合并**，分析正文说明歧义并生成澄清追问（`follow_up_questions`）。不新增"暂停提问"通道，与现有追问体系、spec/52 渲染闸门同构。

## 9. 配置项（全部可配置，`src/config.py`）

| 配置 | 默认 | 说明 |
|------|------|------|
| `sql_profiling_enabled` | true | 画像预取总开关 |
| `sql_profiling_max_tables` | 6 | 每次请求画像表数上限 |
| `sql_profiling_max_columns_per_table` | 6 | 每表画像列数上限（取键列判断结果前 N） |
| `sql_profiling_full_scan_row_limit` | 50000 | 估算行数超过则改用采样 |
| `sql_profiling_sample_rows` | 50000 | 采样行数 |
| `sql_profiling_ttl_seconds` | 300 | 画像缓存有效期 |
| `schema_key_detection_enabled` | true | 键列判断开关 |
| `schema_key_detection_max_tokens` | 800 | 键列判断输出上限 |
| `sql_probe_max_calls` | 3 | 探查工具每请求调用次数上限 |
| `sql_probe_timeout_seconds` | 3.0 | 单次探查询超时 |
| `sql_probe_limit` | 50 | 探查结果行上限 |

字段风格沿用现有配置（布尔开关 + `Field(ge/le)` 边界），随数据源/租户路由生效。

## 10. 失败降级与安全边界（汇总）

- 键列判断失败 → 启发式兜底；画像失败 → 无画像照常生成；探查失败 → 模型自行重试或放弃；三层任一失败都不阻塞主流程，但都不静默（warning 级日志 + 结构化结果）。
- 探查结果作为**证据文本**进入 prompt，与知识检索结果同级：只作参考，不改变权限、工具与安全边界。
- 所有新增 SQL 一律走既有只读校验与租户隔离通道，不新增绕过路径。

## 11. 验收标准（数据源无关）

| 验收项 | 标准 | 验证方式 |
|--------|------|---------|
| **重复坐标不变量**（主标准） | 对任何存在"同名实体"形态的数据源，首轮查询结果要么展示维度单列唯一（模型自行选出了某种区分列），要么走"呈现明细+追问"；不允许"单列不唯一、无区分维度、又不追问"的静默形态 | 在工业库与 retail 等至少两套异构测试数据源跑同类场景，检查结果行形态与图表坐标唯一性 |
| **画像质量** | 画像的"唯一性/重复度"结论与直查数据抽样一致 | 抽样维度列，画像结论与直跑 GROUP BY 比对 |
| **闸门兜底** | 画像/探查未命中时，重复坐标的图表方案仍转入待确认 | spec/52 既有测试 + 回归样本 |
| **性能预算** | 画像 ≤ 上限次数/请求（缓存命中为 0 查询）；探查 ≤ 上限次数/请求；单次均在超时内 | 请求级计数日志与耗时断言 |

## 12. 实施拆分与测试策略

**Phase A（键列判断 + 画像预取，先行）**：DataProfiler 模块 → 键列判断调用与缓存 → 裁剪保护接入 `_required_schema_columns` → 画像段注入 generate_sql prompt → 配置项。

**Phase B（探查工具）**：五原语实现 → knowledge_loop 工具挂载与专属预算 → sql.generate 提示引导 → 安全测试。

**测试**（遵循项目测试规范）：
- 单测：画像查询的方言适配（MySQL/ClickHouse/PostgreSQL）、大表降级链、逐列失败降级、缓存 TTL、键列判断结构化契约与缓存命中、探查原语参数校验/白名单/LIMIT 强制/预算耗尽；
- 集成（Mock LLM 或 Fake LLM）：画像段确实进入生成 prompt、键列进入必保集合、探查工具调用后生成 SQL 的形态；
- 回归样本：§1 两个实机场景（含跨数据源版本）；远程模型测试默认不跑（`RUN_LIVE_LLM_TESTS=1` 显式开启）。

**开发时**：在 `features/` 对应模块（建议 04-graph / 05-tools / 10-llm）登记功能点后开始实现；本文档为设计基线。

## 13. 与既有体系的关系

- **复用不新建**：行数估算与方言工具（`src/datasource/introspection.py`）、只读校验执行（`src/security/sql_execution.py`）、工具循环预算（`src/llm/knowledge_loop.py`）、prompt 预算器（`src/llm/prompt_budget.py`）、schema 裁剪（`src/graph/nodes/generate_sql.py`）；
- **不引入新依赖**：sqlglot 与现有连接器足够；
- **与闸门的关系**：画像/探查减少首轮出错（预防），spec/52 合并确认闸门是出错时的兜底（拦截），两者不互斥、不可互相替代。
