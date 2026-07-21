# 4. LangGraph 编排引擎

## 4. LangGraph 编排引擎 (graph/) `[P0:28 P1:16 P2:3 P3:1]`

### 4.1 状态定义与工作流组装

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.1.1 | AnalysisState TypedDict | `src/graph/state.py` | 28 个字段的完整状态定义 | 单测完成 | P0 |
| 4.1.2 | StateGraph 组装 + compile | `src/graph/workflow.py` | 注册 10 个 Node + 9 条边 + 4 组条件路由 | 单测完成 | P0 |
| 4.1.3 | after_layer3() | 同上 | security_block→终止 / syntax_error→重试 / ok→下一步 | 单测完成 | P0 |
| 4.1.4 | after_layer4() | 同上 | 失败且<3次→重试 / ≥3次→放弃 / ok→执行 | 单测完成 | P0 |
| 4.1.5 | should_retry() | 同上 | 执行错误且<3→generate_sql / 否则→build_response | 单测完成 | P0 |
| 4.1.6 | route_by_intent() | 同上 | file/meta/chat/metadata/多源分叉路由 | 开发完成 | P0 |
| 4.1.7 | MCP Agent Node 扩展 | 同上 | create_react_agent 动态工具调用，编译图真实经过 Agent 和统一出口 | 单测完成 |
| 4.1.8 | metadata/chat 路由 `[P2]` | `src/graph/workflow.py` | metadata/chat→llm_direct_answer | 开发完成 |
| 4.1.9 | multi_source 路由 `[P2]` | `src/graph/workflow.py` + `multi_source.py` | asyncio.gather 调度全部已选来源；单源连接失败快速隔离并在最终摘要列出 | 单测完成 |
| 4.1.10 | llm_direct_answer Node `[P2]` | `src/graph/nodes/llm_answer.py` | 跳过 SQL 流水线直接回答 | 开发完成 |
| 4.1.11 | meta 分析路由 `[P1]` | `src/graph/workflow.py` | 追问（规律/趋势/怎么看）→ restore_previous_result → analyze_result | 单测完成 |
| 4.1.12 | after_generate_sql() `[P1]` | 同上 | 生成 SQL 后条件路由：空 SQL+需要时间→build_response 提示 | 开发完成 |
| 4.1.13 | 时间提示中断 `[P1]` | 同上 + `build_response.py` | needs_time_range 跳过执行→前端时间标签 | 开发完成 |
| 4.1.14 | 轮次状态初始化 `[P0]` | `src/graph/nodes/prepare_turn.py` | 保留历史，清空 checkpoint 中上一轮 SQL/错误/结果/分析/图表/多源状态 | 单测完成 |
| 4.1.15 | 跨轮结果快照恢复 `[P0]` | `prepare_turn.py` + `restore_previous_result.py` | 固化上一轮结构化结果；仅 meta 且数据源一致时恢复，普通查询继续清空瞬态状态 | 单测完成 |

### 4.2 classify_intent Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.2.1 | classify_intent_node() | `src/graph/nodes/classify_intent.py` | 规则匹配 7 种意图 (Phase 2 切 LLM) | 单测完成 | P0 |
| 4.2.2 | INTENT_CLASSIFY_PROMPT | `src/llm/prompts.py` | 意图识别 Prompt 模板 | 单测完成 | P0 |
| 4.2.3 | Skill 匹配触发 | 同上 | 预留接口 (Phase 2 集成 SkillManager) | 开发完成 | P1 |
| 4.2.4 | 输出: intent / activated_skills / skill_prompt_override / skill_tools | 同上 | Skill 信息写入 state | 开发完成 | P0 |
| 4.2.5 | meta 意图识别 `[P1]` | 同上 | 有历史时匹配"规律/趋势/发现/怎么看"→meta，绕过 SQL 流水线 | 开发完成 |
| 4.2.6 | 关键词扩展 `[P1]` | 同上 | 新增"找出/消费/客户/平均/占比"等15个 query 关键词 | 开发完成 |
| 4.2.7 | 精确意图规则 `[P1]` | 同上 | 明确 Schema 问法才归 metadata，包含“字段”的统计问题继续走查询 | 单测完成 |
| 4.2.8 | 授权候选数据源自动路由 `[P0]` | 同上 + `src/security/permission_check.py` | 未选择数据源时模型仅从当前身份授权候选中选择；模型不可用时按名称/非空描述确定性回退 | 单测完成 |

### 4.3 retrieve_schema Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.3.1 | retrieve_schema_node() | `src/graph/nodes/retrieve_schema.py` | 从 injected schema 提取表结构 (Phase 2 向量检索) | 单测完成 | P0 |
| 4.3.2 | 向量语义检索 | `retrieve_schema.py` | VectorStore.search 语义检索 | 开发完成 | P1 |
| 4.3.5 | 检索业务规则 | `business_rules.py` | VectorStore 统一接口 | 开发完成 | P1 |
| 4.3.6 | 检索历史 SQL 模板 | `long_term_store.py` | VectorStore 统一接口 | 开发完成 | P1 |
| 4.3.7 | 数据库分析上下文补全 | `retrieve_schema.py` | 输出 PK/FK/可空性/行数/枚举并区分知识上下文 | 单测完成 | P1 |

### 4.4 generate_sql Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.4.1 | generate_sql_node() | `src/graph/nodes/generate_sql.py` | 模板拼接 + 错误回注 (Phase 2 ChatOpenAI) | 单测完成 | P0 |
| 4.4.2 | SQL_GENERATION_SYSTEM_PROMPT | `src/llm/prompts.py` | SQL 生成 Prompt + 方言速查表 | 单测完成 | P0 |
| 4.4.3 | 方言 Prompt 注入 | 同上 | get_dialect_cheatsheet() — 3 种方言速查 | 单测完成 | P0 |
| 4.4.6 | 错误回注处理 | 同上 | retry_count>0 时返回修复占位 | 单测完成 | P0 |
| 4.4.10 | format_schema_for_prompt() | 同上 | 表结构 → Markdown 格式化 | 单测完成 | P0 |
| 4.4.11 | Schema 增强格式 | 同上 | 列名白名单 + 样本值 + "禁止编造"约束 (Layer 1) | 开发完成 | P0 |
| 4.4.12 | 重试错误上下文增强 | 同上 | 含执行错误 + 列名提示 + 第N次警告 (Layer 3) | 开发完成 | P0 |
| 4.4.13 | 跨轮 retry 重置 `[P1]` | 同上 | 新问题≠上轮提问时 retry_count 复位为 0，避免跨轮重试上下文 | 开发完成 |
| 4.4.14 | 时间过滤拦截 `[P1]` | 同上 `_missing_time_filter()` | SQL 模式匹配：有聚合+日期列+无WHERE过滤→拦截，不依赖具体表名 | 开发完成 |
| 4.4.15 | SQL Prompt 优化 `[P1]` | `src/llm/prompts.py` | ROUND默认两位小数、时间过滤依据聚合/统计意图 | 开发完成 |
| 4.4.16 | 证据约束 SQL Prompt `[P1]` | `src/llm/prompts.py` + `generate_sql.py` | 注入业务知识/枚举/示例，增加粒度、JOIN 膨胀、NULL/除零和方言约束 | 单测完成 |

### 4.5 layer3_validate Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.5.1 | layer3_validate_node() | `src/graph/nodes/layer3_validate.py` | sqlglot AST 语法校验与只读语句白名单 | 单测完成 | P0 |
| 4.5.2 | SQL 安全拦截 | 同上 | 仅允许 SELECT / SHOW / DESCRIBE / EXPLAIN，拒绝嵌套写语句 | 单测完成 | P0 |
| 4.5.3 | sqlglot 语法解析校验 | 同上 | 解析失败时关闭执行路径 | 单测完成 | P0 |
| 4.5.7 | 输出: sql_valid / errors / transpiled | 同上 | | 单测完成 | P0 |

### 4.6 layer4_explain Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.6.1 | layer4_explain_node() | `src/graph/nodes/layer4_explain.py` | 复用 Registry 引擎执行六种方言 EXPLAIN，错误关闭执行路径 | 单测完成 | P1 |
| 4.6.2 | 多源 EXPLAIN 一致性 | `src/graph/nodes/multi_source.py` | 每个多源 worker 在真实执行前经过 Layer 4 | 单测完成 | P1 |

### 4.7 execute_sql Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.7.1 | execute_sql_node() | `src/graph/nodes/execute_sql.py` | 对接全局 Registry 并执行真实数据源查询 | 单测完成 | P0 |
| 4.7.2 | 错误信息简洁化 | `src/graph/nodes/execute_sql.py:82-91` | 从 DB 原始错误提取错误码和消息 | 开发完成 | P1 |
| 4.7.3 | 列名预检验证 (Layer 2) | `src/graph/nodes/execute_sql.py:_validate_column_references()` | sqlglot 解析列引用 → 对照 schema → 触发 retry | 开发完成 | P0 |
| 4.7.4 | float→Decimal 精度 `[P1]` | 同上 `_row_to_dict()` | 查询结果 float 自动转 Decimal，从源头消除 IEEE 754 | 开发完成 |
| 4.7.5 | sync/async 引擎兼容 `[P1]` | 同上 | 检测 AsyncEngine→async with，否则在线程池中执行 sync Engine（Oracle/MSSQL支持） | 单测完成 |

### 4.8 analyze_result Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.8.1 | analyze_result_node() | `src/graph/nodes/analyze_result.py` | 分析引擎集成: 统计+趋势+异常+占比 | 单测完成 | P0 |
| 4.8.2 | DATA_ANALYSIS_PROMPT | `src/llm/prompts.py` | 数据分析 Prompt | 单测完成 | P0 |
| 4.8.3 | 描述性统计 | `src/tools/analyzer.py` | 均值/中位数/标准差/分位数/空值率 | 单测完成 | P0 |
| 4.8.4 | 趋势分析 | 同上 | 环比/方向/移动平均 | 单测完成 | P0 |
| 4.8.5 | 归因分析 | 同上 | Phase 2 LLM 归因 (数据统计已就绪) | 待开发[^7] | P1 |
| 4.8.6 | 异常检测 | 同上 | Z-Score + IQR 两种方法 | 单测完成 | P0 |
| 4.8.7 | 占比分析 | 同上 | 集中度/分类聚合 | 单测完成 | P0 |
| 4.8.9 | 输出: analysis_result | 同上 | summary+insights+chart+followups | 单测完成 | P0 |
| 4.8.10 | 证据化分析 Prompt `[P1]` | `src/llm/prompts.py` + `analyze_result.py` | 注入原问题和完整性标签，输出事实/限制/置信度/行动建议 | 单测完成 |

### 4.9 generate_chart Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.9.1 | generate_chart_node() | `src/graph/nodes/generate_chart.py` | Phase 2 ECharts 生成 (Phase 1 占位) | 单测完成 | P1 |
| 4.9.4 | CHART_RECOMMEND_PROMPT | `src/llm/prompts.py` | 图表推荐 Prompt | 单测完成 | P0 |
| 4.9.5 | 交叉透视智能降维 `[P1]` | `src/graph/nodes/generate_chart.py` | X 列重复 → 按第三列拆多系列；>30行截断 | 开发完成 |
| 4.9.6 | 伪数值列过滤 `[P1]` | 同上 | phone/id/no 类列不参与图表 | 开发完成 |

### 4.10 build_response Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.10.1 | build_response_node() | `src/graph/nodes/build_response.py` | 组装 success/error 两种响应 | 单测完成 | P0 |
| 4.10.2 | 正常响应 | 同上 | user_query+sql+data+analysis+chart | 单测完成 | P0 |
| 4.10.3 | 错误响应 | 同上 | error_code + error_message | 单测完成 | P0 |
| 4.10.4 | 时间提示响应 `[P1]` | 同上 | needs_time_range→返回时间标签，含 conversation_history 追加 | 开发完成 |
| 4.10.5 | 统一出口重构 `[P1]` | 同上 | 三条路径（正常/校验失败/时间提示）共用历史追加逻辑 | 开发完成 |
| 4.10.6 | 直接回答统一出口 `[P1]` | 同上 | llm_direct/MCP 保留 source，并统一追加 conversation_history/messages | 单测完成 |

### 4.11 decompose_query Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.11.1 | decompose_query_node() | `src/graph/nodes/decompose_query.py` | LLM判断多步分解；简单查询直接跳过 | 单测完成 | P0 |
| 4.11.2 | 快径短路 `[P1]` | 同上 | 有上下文或无多步关键词（然后/先查/再查）→ 秒级跳过LLM | 开发完成 |
| 4.11.3 | 分解步骤消费 `[P1]` | `decompose_query.py` + `generate_sql.py` | 使用真实 Schema 规划，步骤写入 SQL grounding context 并要求单条 CTE | 单测完成 |

### 4.12 analyze_result Node (续)

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.12.1 | 全量数据投喂 `[P1]` | `src/graph/nodes/analyze_result.py` | 紧凑JSON优先全量，超限均匀抽取，上限可通过 ANALYSIS_DATA_MAX_CHARS 配置 | 开发完成 |
| 4.12.2 | 处理器 Decimal 运算与专用路由 `[P1]` | `src/tools/data_processor.py`、`analyze_result.py`、`classify_intent.py` | 专用业务关键词选择 22 个处理器，按处理器契约构造列参数；确定性计算保留 Decimal 精度 | 单测完成 |

### 4.13 缺陷整改回归

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.13.1 | 权限失败关闭 | `src/security/permission_check.py` | 列白名单解析失败、通配符绕过和行过滤注入失败均拒绝执行 | 单测完成 |
| 4.13.2 | 有界执行与结果脱敏 | `src/graph/nodes/execute_sql.py` | 最多读取 MAX_RESULT_ROWS + 1 行，写入 state 前脱敏并标记 truncated | 单测完成 |
| 4.13.3 | 确定性无 LLM 回退 | `src/graph/nodes/generate_sql.py` | 数量查询生成 COUNT(*)，无法确定语义时返回明确错误 | 单测完成 |
| 4.13.4 | 方言与分析契约修复 | `src/datasource/introspection.py`、`src/graph/nodes/analyze_result.py` | SQLite 内省、LLM 样本长度和 statistics 输出契约回归 | 单测完成 |
| 4.13.5 | 编排与状态契约修复 | `src/graph/workflow.py`、`prepare_turn.py` | 修复 MCP 死边、metadata 无 Schema、幻觉错误覆盖和瞬态错误误回 LLM | 单测完成 |
| 4.13.6 | 跨源列契约与最终 SQL | `src/graph/nodes/multi_source.py`、`build_response.py` | 按维度/指标角色序列对齐任意数量列；冲突时保留原字段；返回每个来源重写后的最终 SQL | 单测完成 |

### 模块收尾

模块功能点共 80 项，已完成 79 项，待开发 1 项。

| 功能点 | 不开发原因 | 可开发条件 | 预计开发时机 |
|--------|------------|------------|--------------|
| 4.8.5 归因分析 | 规则统计无法可靠生成因果解释，当前隔离验收环境也没有真实 LLM 凭证和归因质量评测集 | 配置可用 LLM，并建立可重复的归因基准数据与人工验收标准 | Phase 2 增强阶段，归因评测集就绪后 |

---
