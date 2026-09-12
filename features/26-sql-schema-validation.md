# 26. SQL Schema 校验层

## 26. SQL Schema 校验层 (security/) `[P0:12 P1:2]`

> 设计依据：spec/39-sql-schema-validation.md
> 目标：建立唯一的 SQL ↔ Schema 校验实现，问答 / 多源 / 报表三条链路共用；校验基准按数据源取权威全量快照，消除基准差异与误杀。

### 26.1 校验器模块

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 26.1.1 | validate_sql_schema() | `src/security/sql_schema_validation.py` | 作用域感知（`traverse_scope`）校验 SQL 引用的表与列；显式多源列表 `datasources`，按 `(datasource, table)` 建索引，结构上不合并多源表名空间；不依赖 `src.graph` | 单测完成 | P0 |
| 26.1.2 | ValidationIssue 结构 | 同上 | `type / datasource / table / column / qualifier / provided_by / available_columns / confidence`，供结构化回喂重试 | 单测完成 | P0 |
| 26.1.3 | 置信度分层处置 | 同上 | high（限定符表不含该列、纯物理表 scope 零命中、歧义列、表不存在、解析失败）→ 拦截；low（`SELECT *`，产出低置信 issue，reason=`select_star`）→ 放行 + warning。另有只留 warning、不产出 issue 的"无法判定"分支：限定符无法判定（含 CTE 限定符与命名空间前缀，reason=`cte_qualifier`）、含 CTE 时零命中（reason=`cte_or_unresolved`）、空列集表（reason=`table_has_no_columns` / `tables_have_no_columns`）。命名空间不明没有独立 reason，走 `cte_qualifier` 分支，见 26.4.2 | 单测完成 | P0 |
| 26.1.4 | 表名/列名索引工具 | 同上 | 唯一导出「表名→列集合」（`build_table_index` / `resolve_table_owner`）；`execute_sql._columns_by_table` 与 `_alias_to_table` 已删除。`supplemental_query._schema_table_columns` 按设计**不收敛**（它额外纳入列注释、表描述、业务别名做自然语言匹配，语义不同，见 spec/39 §7.4） | 单测完成 | P0 |
| 26.1.5 | 方言归一统一 | 同上 | 内部走 `src/security/sql_execution.py:normalize_sql_dialect()`，删除 `execute_sql.py` 的内联方言映射副本 | 单测完成 | P1 |

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
| 26.4.2 | 命名空间（schema/database） | `TableSchema.name` 不含命名空间；`analytics.orders` 的 `analytics` 落在 `node.db` 被丢弃 | 当前仅按表名匹配；实测 `SELECT analytics.created_at FROM analytics.orders` 返回 `[]`，只留 reason=`cte_qualifier` 的 warning（无独立命名空间判定、不产出低置信 issue）；设计契约在 `tables` 入参上预留可选 `schema` 字段（校验器尚未读取） |
| 26.4.3 | 方言特有表函数 / `UNNEST` / `LATERAL` | 未纳入本设计承诺范围 | 后续按方言实测补充 |
| 26.4.4 | 快照与真实库漂移 | 基准来自各级缓存 | 独立议题 |
| 26.4.5 | `HAVING <未限定幻觉列>` | sqlglot 把 HAVING 未限定引用当输出列引用，不进 `scope.columns` | 实测零 issue 放行，只留 reason=`cte_or_unresolved` warning（spec/39 §10.6） |
| 26.4.6 | `UNION` 末尾 `ORDER BY <幻觉列>` | 外层 Union scope `sources={}`，走 `not resolved` 分支放行 | 实测零 issue **且零 warning，完全静默**（spec/39 §10.7）；不得当作已校验通过 |

### 26.5 依赖关系

- Depends on: 4.5.1（layer3_validate Node）、4.7.3（列名预检验证）、6.x（SchemaManager）、12.x（安全模块）
- 与 features/23-report-workbench.md 的关系：报表链路通过复用主图自动获得校验，**无独立功能点**

### 模块收尾

功能点共 14 项，已完成 14 项（14 项单测完成，含 26.3.2 降级留痕的 structlog capture_logs 断言），待开发 0 项。

残留项（显式记录，不在本计划范围）：

| 项 | 当前状态 | 可开发条件 | 预计时机 |
|----|----------|-----------|----------|
| 命名空间（schema/database）支持 | 仅按表名匹配，`tables` 入参预留 `schema` 字段未实现 | 需要 `TableSchema` 携带命名空间，属快照结构独立议题 | 待评估 |
| 方言特有表函数 / `UNNEST` / `LATERAL` | 未纳入判定，按盲区记录 | 按方言实测补充用例后 | 待评估 |
| 快照与真实库漂移 | 基准来自各级缓存 | 独立议题 | 待评估 |
| 跨源关联查询 | `datasources` 多源形状已预留 | 需先确认联邦方言策略（决策 D1 备选） | 待评估 |
| workflow 级集成测试 | 仅有节点级/模块级单测 | 需要覆盖主图 `layer3 → generate_sql` 重试回路的集成用例 | 待评估 |
