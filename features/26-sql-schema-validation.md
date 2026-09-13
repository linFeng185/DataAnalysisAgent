# 26. SQL Schema 校验层

## 26. SQL Schema 校验层 (security/) `[P0:16 P1:3]`

> 设计依据：spec/39-sql-schema-validation.md
> 目标：建立唯一的 SQL ↔ Schema 校验实现，问答 / 多源 / 报表三条链路共用；校验基准按数据源取权威全量快照，消除基准差异与误杀。

### 26.1 校验器模块

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 26.1.1 | validate_sql_schema() | `src/security/sql_schema_validation.py` | 作用域感知（`traverse_scope`）校验 SQL 引用的表与列；显式多源列表 `datasources`，按 `(datasource, table)` 建索引，结构上不合并多源表名空间；不依赖 `src.graph` | 单测完成 | P0 |
| 26.1.2 | ValidationIssue 结构 | 同上 | `type / datasource / table / column / qualifier / provided_by / available_columns / confidence`，供结构化回喂重试 | 单测完成 | P0 |
| 26.1.3 | 置信度分层处置 | 同上 | high（限定符表不含该列、纯物理表 scope 零命中、歧义列、表不存在、解析失败）→ 拦截；low（`SELECT *`，产出低置信 issue，reason=`select_star`）→ 放行 + warning。另有只留 warning、不产出 issue 的"无法判定"分支：限定符指向 CTE / 派生表（reason=`cte_qualifier`）、含 CTE 时零命中（reason=`cte_or_unresolved`）、空列集表（reason=`table_has_no_columns` / `tables_have_no_columns`） | 单测完成 | P0 |
| 26.1.4 | 表名/列名索引工具 | 同上 | 唯一导出「表名→列集合」（`build_table_index` / `resolve_table_owner`）；`execute_sql._columns_by_table` 与 `_alias_to_table` 已删除。`supplemental_query._schema_table_columns` 按设计**不收敛**（它额外纳入列注释、表描述、业务别名做自然语言匹配，语义不同，见 spec/39 §7.4） | 单测完成 | P0 |
| 26.1.5 | 方言归一统一 | 同上 | 内部走 `src/security/sql_execution.py:normalize_sql_dialect()`，删除 `execute_sql.py` 的内联方言映射副本 | 单测完成 | P1 |
| 26.1.6 | 无效列限定符拦截 | 同上 | 列限定符等于本 scope 某表的 schema 名时产出高置信 `invalid_qualifier`：无别名时表别名就是表名，用 schema 名限定列本身非法（DB 报 `missing FROM-clause entry`），旧实现按「限定符指向 CTE」静默放行，导致该列完全不参与校验。`describe()` 给出「改用表名限定，或先起别名再用该别名限定」的操作指引；表名限定、三段限定 `schema.table.col`、显式别名、CTE 限定均照常放行 | 单测完成 | P0 |

### 26.2 调用方接入

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 26.2.1 | layer3 两阶段取数 | `src/graph/nodes/layer3_validate.py` | 阶段一用 `state["relevant_tables"]` 零 IO 校验；出现 high 置信 issue 或阶段一无基准时才进入阶段二，用 `get_or_fetch_schema(ds, user_query="", tenant_id=...)` 全量快照复核（显式租户，与 `retrieve_schema` 调用约定一致，避免多租户下落到平台级缓存），消除单源问答链路的裁剪误杀；阶段一为空不再静默跳过；复核不可用回落阶段一结果 | 单测完成 | P0 |
| 26.2.2 | execute_sql 兼容包装 | `src/graph/nodes/execute_sql.py` | `_validate_column_references` 改为薄调用新模块，保留最后防线；不保留双实现 | 单测完成 | P0 |
| 26.2.3 | 表级校验收敛 | `src/graph/nodes/generate_sql.py` | `_check_table_hallucination` 的表名比对改为共用新模块实现，保留「生成后立即校验」的早失败位置 | 单测完成 | P1 |
| 26.2.4 | 重试回喂契约 | 同上（`build_retry_context`） | 新增 schema 幻觉分支：`column_reference` 类 issue **不回注失败 SQL**，改为按 issue 类型分派渲染（歧义列要求补表限定符并给出候选表，不得谎报"列不存在"；跨表错配说明真实归属；解析失败不产出空壳表名），并按表分组回喂实际可用列清单，「共 N 个」用 `available_columns_total` 真实总数；多源时按 `issue.datasource` 分组；issue 上限 3、每表列数上限 10，指令前置以抗 section 尾部截断 | 单测完成 | P0 |

### 26.3 观测与测试

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 26.3.1 | 指标计数 | `src/observability.py` | 新增 Counter `data_agent_sql_schema_validation_total`，标签 `issue_type / confidence / dialect / stage`；**不使用数据源名做标签**（高基数） | 单测完成 | P0 |
| 26.3.2 | 结构化日志 | `src/security/sql_schema_validation.py`、`layer3_validate.py` | 入口 debug、拦截 warning、无法判定 warning（含 reason）、出口 info（high/low 计数）；降级留痕由 `TestDegradationWarnings` 用 `structlog.testing.capture_logs` 断言（reason=`table_has_no_columns` / `tables_have_no_columns`）——caplog 看不到 structlog 输出，故不用 caplog | 单测完成 | P0 |
| 26.3.3 | 单元测试 | `tests/test_security/test_sql_schema_validation.py` | 覆盖正常路径、高置信拦截、跨表错配、歧义列、低置信放行、多源隔离、跨源同名表、方言兼容、边界、回退路径 | 单测完成 | P0 |
| 26.3.4 | 回喂契约测试 | `tests/test_graph/test_generate_sql_retry_context.py` | 验证 schema 幻觉分支不回注失败 SQL、包含可用列清单、截断报真实总数，以及歧义列（state 经真实 layer3 节点产出）必须渲染成"补表限定符"而非"列不存在" | 单测完成 | P0 |
| 26.3.5 | 兼容回归 | `tests/test_graph/test_execute_column_validation.py` | 现有 7 个用例必须继续通过（包装层） | 单测完成 | P0 |

### 26.4 已知盲区（显式记录，不得静默当作校验通过）

| # | 盲区 | 原因 | 处置 |
|---|------|------|------|
| 26.4.1 | `SELECT *` / `SELECT t.*` | sqlglot AST 不展开星号（实测 `columns=[]`） | 低置信放行 + warning（reason=`select_star`） |
| 26.4.2 | 命名空间（schema/database） | `TableSchema.name` 不含命名空间，校验基准只能按表名匹配 | 结构性缺口：需 `TableSchema` 携带命名空间（属快照结构独立议题，见「模块收尾」的残留项表）。**两个相邻面已修**：① 列限定符误用 schema 名的形态已拦为高置信 `invalid_qualifier`（26.1.6）；② 内省侧命名空间已可按方言解析与配置（26.5）。故 `SELECT analytics.created_at FROM analytics.orders` 现产出 `invalid_qualifier` 而非静默放行 |
| 26.4.3 | 方言特有表函数 / `UNNEST` / `LATERAL` | 未纳入本设计承诺范围 | 后续按方言实测补充 |
| 26.4.4 | 快照与真实库漂移 | 基准来自各级缓存 | 独立议题 |
| 26.4.5 | `HAVING <未限定幻觉列>` | sqlglot 把 HAVING 未限定引用当输出列引用，不进 `scope.columns`，连 warning 分支都到不了 | 实测零 issue **且零相关 warning，完全静默**（spec/39 §10.6）；日志无任何线索，不得当作已校验通过 |
| 26.4.6 | `UNION` 末尾 `ORDER BY <幻觉列>` | 外层 Union scope `sources={}`，走 `not resolved` 分支放行（该分支留痕） | 实测零 issue，但留 reason=`cte_or_unresolved` warning（`resolved_count=0 total_table_count=0`，spec/39 §10.7）；日志可查，不得当作已校验通过 |

### 26.5 内省命名空间（跨模块：`src/datasource/introspection.py`）

> 来源：命名空间支持排查中发现内省侧普遍缺少命名空间限定——Postgres 写死 `public`、MSSQL 误用 `TABLE_CATALOG`（库名）、Postgres/MSSQL/ClickHouse 的列与外键查询完全不过滤命名空间，导致跨 schema 同名表的元数据被混在一份快照里。

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 26.5.1 | resolve_namespace() | `src/datasource/introspection.py` | 按方言语义统一解析命名空间：MySQL / ClickHouse 用连接的 `database`；Postgres / MSSQL 取 `extra_params.schema`，未显式配置时回落方言默认值 `public` / `dbo`；SQLite 返回空串。默认值集中在 `DEFAULT_NAMESPACE` | 单测完成 | P0 |
| 26.5.2 | 元数据查询命名空间限定 | 同上 | 表名、列、外键、行数四类查询全部改为 `:schema` 绑定：Postgres 不再写死 `schemaname = 'public'`，MSSQL 由 `TABLE_CATALOG` 改用 `TABLE_SCHEMA`，ClickHouse 列查询补 `database` 过滤；执行器在四处调用点统一传 `schema` 参数 | 单测完成 | P0 |
| 26.5.3 | TABLE_NAME_QUERY 常量 | 同上 | 表名查询从 `_list_tables` 的局部 `sql_map` 提为模块常量，使各方言 SQL 可被断言、可复用 | 单测完成 | P1 |

### 26.6 依赖关系

- Depends on: 4.5.1（layer3_validate Node）、4.7.3（列名预检验证）、6.x（SchemaManager）、12.x（安全模块）
- 与 features/23-report-workbench.md 的关系：报表链路通过复用主图自动获得校验，**无独立功能点**

### 26.7 Oracle 集合运算字符集修复

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 26.7.1 | 权威 Schema 驱动的字符集统一 | `src/tools/sql_rewriter.py`、`src/security/sql_execution.py` | 追踪 CTE/派生表输出，仅对集合运算中已确认的字符集冲突补 TO_NCHAR；保留指标、别名与过滤，未知类型回退原校验；EXPLAIN/执行共用修正结果 | 集成测试完成 | P0 |

Depends on: 26.1.1（作用域校验）、3.5.1（Oracle 连接器）、12.x（统一 SQL 安全执行）；前置实现均已完成。

验收（2026-09-13）：
- 新增 55 项回归：`tests/test_tools/test_sql_rewriter_oracle_charset.py`、`tests/test_security/test_sql_execution_charset.py`，覆盖 AST 口径保持、字符类型、幂等、降级日志、跨方言、租户及 Layer4 状态写回。
- 两组相关回归合计 574 通过、1 跳过；包括安全、连接器、DB Tools、SQL 子图、重试与报表模块；Python 语法检查与限定文件 Ruff 检查通过。
- 真实 Oracle：原始完整报表 SQL 再现 ORA-12704；使用已发布权威快照后只增加 2 处 TO_NCHAR，EXPLAIN 通过、有界执行返回 4 行，保留实际金额指标；重复 EXPLAIN 和健康检查通过。
- SQL 执行期间并发访问共享页面、共享接口及刷新均为 HTTP 200。验收使用加载新代码的独立进程；现有后端未启用热更新，需要重启后生效。未调用真实 LLM 重生成整张报表，未覆盖已发布版本；公共分享不涉及登录态刷新验收。

### 2026-09-13 阶段二类型证据修复

- 对应 26.2.1、26.2.4，保持既有已完成状态，补充节点集成及真实 Oracle 表达式验收。
- 阶段二原先只序列化列名，丢弃 `type` 与 `value_format`，使阶段一已确认的文本日期错误被清除。现在完整传递属性；仅对全量快照中确实存在的同名表列补充阶段一已知的缺失属性，权威新类型/格式仍优先；空快照和异常仍回落阶段一。
- `ValidationIssue.describe()` 随结构化 issue 传入生成重试，`type_mismatch` 使用准确类型与显式转换建议，不再退化为“未知字段引用错误”。重试仍不回注失败 SQL。
- 真实 Oracle：读取租户 3 已发布快照，确认 `orders.order_date` 为 `VARCHAR2`、格式 `YYYY-MM-DD`；与故障同构的 `COALESCE(TRUNC(文本日期), TRUNC(SYSDATE))` 在 EXPLAIN 中复现 `ORA-00932`，新 layer3 在打库前拦截，重试提示包含 `VARCHAR2` 与 `TO_DATE`。按快照格式显式转换后，layer3、统一 EXPLAIN/有界执行通过，返回 2 行，后续健康检查成功。
- 本轮两模块共新增 31 项回归，相关套件 505 通过、1 跳过；覆盖类型/格式保留、未知属性回退、权威新证据纠正旧基准、租户传递、条件路由、重试提示、重复渲染无噪声及异常堆栈保留。未用远程模型重新生成完整故障 SQL；独立进程的真实表达式验收不替代整报表验收。

### 2026-09-13 深度报表执行回归

- 26.1 单测回归完成：聚合输入列与其输出别名同名时，不允许输入借自身 alias 获准；合法 ORDER BY/GROUP BY 输出别名保持可用，覆盖五种数据库方言。
- 第七轮真实 ClickHouse 在第二个 CTE 内引用不存在的 `avg_temp_c`，日志出现 code 47。根因是 `scope.sources` 包含声明过但本查询未引用的 CTE，误触发“存在不可判定来源”的放行分支。现在物理表、命名空间和是否存在派生来源均只统计 `selected_sources`，与已有 CTE 输出索引保持一致。
- 新增前置无关 CTE/星号 CTE 与五方言组合测试，确认缺列在执行前拦截，不通过猜列名或忽略异常解决。第八轮真实原任务 17 次业务 SQL 全成功，日志没有数据库执行 ERROR；安全校验的拦截重试 WARNING 保留。
- 扩大回归 1566 通过、1 跳过、1 排除，详细真实任务、日志与部署边界见 `features/23-report-workbench.md` 的续修记录；不将传输成功计数当作模型语义正确率。

### 模块收尾

功能点共 19 项，已完成 19 项（18 项单测完成，26.7.1 集成测试完成），待开发 0 项。

残留项（显式记录，不在本计划范围）：

| 项 | 当前状态 | 可开发条件 | 预计时机 |
|----|----------|-----------|----------|
| 命名空间（schema/database）支持 | 校验基准仍仅按表名匹配，`tables` 入参的 `schema` 字段未实现。**相邻两处已修**：列限定符误用 schema 名的形态已拦为 `invalid_qualifier`（26.1.6）；内省侧已可按方言解析命名空间（26.5） | 需要 `TableSchema` 携带命名空间，属快照结构独立议题 | 待评估 |
| 方言特有表函数 / `UNNEST` / `LATERAL` | 未纳入判定，按盲区记录 | 按方言实测补充用例后 | 待评估 |
| 快照与真实库漂移 | 基准来自各级缓存 | 独立议题 | 待评估 |
| 跨源关联查询 | `datasources` 多源形状已预留 | 需先确认联邦方言策略（决策 D1 备选） | 待评估 |
| workflow 级集成测试 | 仅有节点级/模块级单测 | 需要覆盖主图 `layer3 → generate_sql` 重试回路的集成用例 | 待评估 |
