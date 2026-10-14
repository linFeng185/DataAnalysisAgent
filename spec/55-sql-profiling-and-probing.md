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
[新增] 键列判断（结构规则兜底，可选轻量 LLM；按授权结构与模型缓存）
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
- **输入**：授权相关表结构（表名/列名/类型/注释/主键标记），不含用户问题或样例值。问题相关性由现有选表打分单独负责，避免问题参与判断却未参与缓存键。
- **输出**（Pydantic 结构化契约，例）：每表 `{ key_columns: 实体键/编码/唯一标识列, display_columns: 展示名类列, foreign_keys: 外键列, profile_columns: 画像候选列 }`，附一句理由。
- **三个用途**：
  1. **裁剪保护**：key_columns 并入 `_required_schema_columns` 的必保集合，列数超配额时不被挤掉；
  2. **画像候选列来源**：替代"命名启发式"决定画像探哪些列（原设计最弱环节）；
  3. **prompt 配对提示**：向 SQL 生成注入"`id/workshop_code` 是实体键、`workshop_name` 是展示名"的语义事实。
- **缓存**：按（tenant_id, user_id, datasource, 授权列/行策略指纹, 表结构哈希, 模型选择指纹, Prompt 版本）缓存，TTL 与容量有界；不跨身份复用。输出只接受输入结构中存在且获授权的列，未知列拒绝；编码/索引只能是语义候选，不能据此声称真实唯一。
- **降级**：调用不可用/超时/解析失败 → 回退现有启发式（`is_primary_key`、`is_indexed`、关系 `join_key`、`*_id`/`*_code` 命名），不阻塞主流程；此时画像候选列取"问题命中列（`_column_match_score`）+ 名称/编码/外键命名列"。
- **预算**：`schema_key_detection_enabled` 开关、`schema_key_detection_max_tokens` 和有限超时；模型统一走 `src/llm/client.py` 与 Prompt 注册调用。禁用知识工具，避免键列判断递归探查；无模型、失败或超时使用确定性规则，取消必须向上传播。

## 5. 数据画像（DataProfiler）

新模块 `src/datasource/profiling.py`，以授权后的有界样本为依据：

- 每表只做一次有界列投影查询，在行权限过滤之后取 `sample_rows + 1` 行，在 Python 中对已授权列统计空值、非空数、不同值数、最高频次和 TOP 8 值。不得先对整表 GROUP BY 再 LIMIT，也不得对每列重复扫描。
- 查询未触及样本上限时，统计范围为 `complete`（本次可见数据）；触及上限时为 `sample`，不是随机抽样，也不保证代表总体。估算行数仅作提示；0/负数/缺失都视为未知，不得触发全表扫描。
- **唯一性修正**：单组且频次等于行数表示常量列，不是唯一列。只有完整可见数据非空、无 NULL、`distinct_count == row_count` 才能标记 `unique`；发现重复可以标记 `duplicate`；样本中未见重复只能标记 `unknown`，不得提升为全表唯一。空表/全 NULL 不标唯一。
- 完整与样本统计均携带 `scope`、`sampled_rows`、`null_count`；TOP 8 的长度不等于 distinct_count。NULL 不当作实体候选，值文本有长度上限，原始样本不写日志或历史。
- 受 `sql_profiling_max_tables`、每表列数、每次请求查询数、样本行数、总耗时和单次超时共同限制。`sql_profiling_full_scan_row_limit` 只作为取样行数的额外硬上限，不再授权无界扫描。
- MySQL、ClickHouse、PostgreSQL 必测；SQLite 用于真实本地验收。Oracle/MSSQL 使用 sqlglot 对应方言渲染受限查询，无法安全执行时明确降级。
- **产出**（以下仅为完整可见数据的示意，不保证每个请求均取得完整统计）：

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

- **缓存**：进程内按（tenant_id, user_id, datasource, 结构/连接版本、授权列/行策略指纹、表、列集合和采样配置）TTL 缓存，命中零查询；容量默认 128 项，按需淘汰，单个异步锁合并同键并发填充。失败不作正常数据缓存，权限/结构变化自然换键，返回副本防止调用方污染。

## 6. 画像预取、裁剪保护与 prompt 注入

- **挂载点**：`generate_sql_node` 完成初步选表后、最终列裁剪前。仅当服务端聊天入口的 RunnableConfig 含 `enable_sql_intelligence=True` 且当前真实身份与请求上下文一致才启用；普通直接调用、报表和后台任务默认禁用。流式与非流式入口都由服务端写入此开关，不接受请求正文覆写。
- **生命周期**：画像、键列提示与探查范围只保存在本次调用局部变量/ContextVar 中，不保存到 checkpoint、会话历史、SSE 或业务数据库；并发调用及取消后必须恢复作用域。
- **裁剪保护**：键列判断结果并入 `_required_schema_columns` 必保集合；无判断结果时保留现有启发式。这修正了"主键/索引之外只剩配额"的现状风险（本次线上 schema 中 `workshop_code` 无索引标记，仅靠 32 列配额幸免）。
- **prompt 规则**（`sql.generate` 系统提示新增三条）：
  1. 分组/展示维度单列不唯一时，**SELECT 必须带出可区分的键列**（画像中标记唯一/编码的列），不得只输出展示名；
  2. 实体名过滤**优先使用画像中的精确值**；不存在精确匹配时，返回全部候选实体明细并说明歧义（禁止静默合并同类实体，与 spec/52 闸门一致）；
  3. 涉及具体实体、值域、时间边界而画像未覆盖时，先用 `probe_data` 确认，再生成正式 SQL。
- **注入位置**：新增 PromptSection `data_profile`（优先级 95，限 6000 字符，受既有动态预算器裁剪；优先保留 scope、键列与重复度结论）。禁用本功能时不改变原 Prompt、Schema 或工具集合。

## 7. 结构化探查工具（probe_data）

- **挂载**：统一工具循环（`src/llm/knowledge_loop.py`），仅当前已授权聊天调用的 `sql.generate` 可见；次数与超时预算独立计数，不复用知识检索配额。知识检索关闭也不能意外禁用已启用探查，反之亦然；统一消息/轮次上限仍有效。
- **五个固定原语**（模型只填参数，**不接收自由 SQL**）：

| 原语 | 参数 | 返回 |
|------|------|------|
| `entity_lookup` | table, column, pattern | 在有界授权样本内按字面包含查找（精确匹配优先排前；%/_ 无通配语义） |
| `value_domain` | table, column | 列值域+频次（TOP N） |
| `column_repeat` | table, column | 唯一性判定与最高重复值（与 value_domain 共用同一查询，仅返回字段窄化） |
| `time_range` | table, column | 类型兼容样本的 MIN/MAX，不把未知文本格式声称为真实时间范围 |
| `table_sample` | table, columns | 样例行（≤ limit） |

- **安全边界**：
  - 表名必须命中本次请求的 `relevant_tables` 白名单；列名必须在表结构内；
  - 标识符必须逐项等于授权 Schema 名称，再经 sqlglot AST 引用；拒绝未知表列和额外字段，不拼接模型 SQL 或表达式；
  - `allowed_columns` 和 `row_filter_sql` 在取样前生效，不能认为统一执行器已经替调用方做完行列授权；策略解析失败直接拒绝该次探查；
  - 当前请求身份、datasource_access 与数据源作用域必须在执行前复核；工具参数不包含身份、权限或数据源切换；
  - LIMIT 强制 ≤ `sql_probe_limit`；行级结果截断返回；
  - 执行通道复用 `src/security/sql_execution.py` 的 `validate_and_execute_sql`（只读校验+限行）；
  - 调用次数 ≤ `sql_probe_max_calls`、单次超时 `sql_probe_timeout_seconds`，耗尽后工具返回明确的"预算耗尽"结果，不抛异常。
- **返回契约**：`{success, columns, rows, row_count, truncated, scope, error, error_code}`；样本未命中不能宣称全表无结果，工具结果作为不可信证据，拒绝其中的指令。结果总字符数和单个值长度有硬上限，日志只记计数、耗时、错误类型，不回显原始异常或 SQL。
- **超时**：外层 asyncio.timeout 与既有执行器/数据库超时共同约束；CancelledError 原样传播。对没有可控超时的同步驱动不宣称已停止服务端 SQL，首期降级并记录验收限制。

## 8. 歧义处理（已确认产品行为）

探查/画像发现实体名不唯一或不存在精确匹配时：**SQL 返回全部候选实体明细且不合并**，分析正文说明歧义并生成澄清追问（`follow_up_questions`）。不新增"暂停提问"通道，与现有追问体系、spec/52 渲染闸门同构。

## 9. 配置项（全部可配置，`src/config.py`）

| 配置 | 默认 | 说明 |
|------|------|------|
| `sql_profiling_enabled` | true | 画像预取总开关 |
| `sql_profiling_max_tables` | 6 | 每次请求画像表数上限 |
| `sql_profiling_max_columns_per_table` | 6 | 每表画像列数上限（取键列判断结果前 N） |
| `sql_profiling_full_scan_row_limit` | 50000 | 取样行数的额外硬上限，不授权无界全表扫描 |
| `sql_profiling_sample_rows` | 2000 | 采样行数（非随机样本；另取 1 行判断截断） |
| `sql_profiling_ttl_seconds` | 300 | 画像缓存有效期 |
| `sql_profiling_cache_entries` | 128 | 画像与键列缓存容量上限 |
| `sql_profiling_max_queries` | 6 | 每次生成调用的预取查询上限 |
| `sql_profiling_timeout_seconds` | 3.0 | 预取单次查询超时 |
| `sql_profiling_total_seconds` | 8.0 | 全部预取总耗时 |
| `schema_key_detection_enabled` | true | 键列判断开关 |
| `schema_key_detection_max_tokens` | 800 | 键列判断输出上限 |
| `schema_key_detection_timeout_seconds` | 3.0 | 可选模型判断超时 |
| `sql_probe_max_calls` | 3 | 探查工具每请求调用次数上限 |
| `sql_probe_timeout_seconds` | 3.0 | 单次探查询超时 |
| `sql_probe_limit` | 50 | 探查结果行上限 |

字段风格沿用现有配置（布尔开关 + `Field(ge/le)` 边界），进入运行期配置白名单并补充 app.example.yaml；按实际租户/数据源路由而非全局同名缓存生效。禁用开关必须带来零新增模型调用、数据库查询和工具定义。

## 10. 失败降级与安全边界（汇总）

- 键列判断失败 → 启发式兜底；画像失败 → 无画像照常生成；探查失败 → 返回脱敏结构化结果；取消不降级。仅处理异常的边界记一次堆栈，预期禁用/额度耗尽不打印逐列 INFO，正常路径只保留聚合摘要。
- 探查结果作为**证据文本**进入 prompt，与知识检索结果同级：只作参考，不改变权限、工具与安全边界。
- 所有新增 SQL 一律走既有只读校验与租户隔离通道，不新增绕过路径。
- 权限以执行期真实身份为准；快照与当前策略取交集，列权限交集为空必须拒绝，不能将其解释为没有列限制。表列身份保留大小写，显式数据源缺少权限时禁止回退到另一数据源。
- 画像与探查对外值沿用 `src/security/data_masker.py` 的结果脱敏策略。先在授权范围内统计，再对样例、最高频值和值域等输出脱敏，避免不同原值被同一掩码合并而改变基数与唯一性。脱敏后的实体值不得被表述为可直接用于精确过滤的原值；原始样本和敏感原值不进入模型、缓存输出、历史或 SSE。

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

**开发登记**：`features/29-sql-profiling.md`（采集/探查服务）、`features/30-sql-key-detection.md`（键列判断/生成接入）、`features/31-sql-probe-loop.md`（统一工具循环）。按依赖分离写集并行开发；真实外部数据库/远程模型验收单列，不得把离线通过改写为生产验收完成。

## 13. 与既有体系的关系

- **复用不新建**：行数估算与方言工具（`src/datasource/introspection.py`）、只读校验执行（`src/security/sql_execution.py`）、工具循环预算（`src/llm/knowledge_loop.py`）、prompt 预算器（`src/llm/prompt_budget.py`）、schema 裁剪（`src/graph/nodes/generate_sql.py`）；
- **不引入新依赖**：sqlglot 与现有连接器足够；
- **与闸门的关系**：画像/探查减少首轮出错（预防），spec/52 合并确认闸门是出错时的兜底（拦截），两者不互斥、不可互相替代。

## 14. 实施接口约定（2026-10-14 审查修订）

`src/datasource/profiling.py` 对外提供 Pydantic `ProfilingContext`、`ProfileSnapshot`、`ProbeRequest`、`ProbeResult`、`DataProfiler`、`get_data_profiler()` 和 `render_profile(snapshot)`：

- `ProfilingContext(tenant_id, user_id, datasource, dialect, tables, allowed_columns=[], row_filter_sql="", schema_fingerprint="")`；tables 为完整授权表结构字典，身份必须为真实认证身份；调用方复制上下文，不接受模型提供的权限字段。
- `await DataProfiler.profile(context, columns_by_table: dict[str, list[str]]) -> ProfileSnapshot`；`ProfileSnapshot.tables` 为结构化各表统计，`query_count` 为本次真实新增查询数，`errors` 为脱敏代码列表。
- `await DataProfiler.probe(context, request: ProbeRequest) -> ProbeResult`；ProbeRequest 的原语字段名为 `operation`，其余字段按 §7；`extra="forbid"`，结果由 §7 约束。
- `render_profile(snapshot) -> str` 输出含 complete/sample 语义的紧凑证据文本，禁止把样本唯一写成总体唯一。

`src/tools/data_probe.py` 提供 `use_data_probe_context(context)` 同步上下文管理器和 `build_data_probe_tools(prepared=None)`；无已授权上下文、非 sql.generate 或关闭开关时返回空列表。一次上下文绑定共享 3 次探查预算，重复构建工具不得重置；禁止递归绑定键列判断工具。

`src/llm/schema_keys.py` 提供 `await detect_schema_keys(context) -> dict[str, dict[str, list[str]]]`，内层固定键为 `key_columns/display_columns/foreign_keys/profile_columns`；以及 `apply_key_hints(tables, hints) -> list[dict]`，只返回复制后的表结构，键候选放 `_profile_key_columns`。所有返回列均在授权输入中。

工具循环必须按实际工具的 args_schema 校验，不再把每个工具都强制解析为 KnowledgeSearchArgs；schema/result 开销统一进入动态总预算，知识与探查各自的调用额度不能相互消耗。任务关闭、预算不足、不支持 bind_tools 或后端报错时保持既有安全降级与流式结束契约。
