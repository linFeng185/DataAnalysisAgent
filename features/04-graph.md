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
| 4.1.6 | route_by_intent() | 同上 | file_analysis→mcp_agent / 其他→retrieve_schema | 单测完成 | P0 |
| 4.1.7 | MCP Agent Node 扩展 | 同上 | 使用 create_react_agent 为文件分析场景创建动态工具调用 Node | 待开发 |
| 4.1.8 | route_by_intent 扩展 `[P2]` | `src/graph/workflow.py` | metadata→llm_direct_answer / chat→llm_direct_answer | 开发完成 |
| 4.1.9 | multi_source 路由 `[P2]` | `src/graph/workflow.py` + `multi_source.py` | asyncio.gather 并行调度 + merge_results | 开发完成 |
| 4.1.10 | llm_direct_answer Node `[P2]` | `src/graph/nodes/llm_answer.py` | 跳过 SQL 流水线，知识库+对话直接回答 | 开发完成 |

### 4.2 classify_intent Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.2.1 | classify_intent_node() | `src/graph/nodes/classify_intent.py` | 规则匹配 7 种意图 (Phase 2 切 LLM) | 单测完成 | P0 |
| 4.2.2 | INTENT_CLASSIFY_PROMPT | `src/llm/prompts.py` | 意图识别 Prompt 模板 | 单测完成 | P0 |
| 4.2.3 | Skill 匹配触发 | 同上 | 预留接口 (Phase 2 集成 SkillManager) | 开发完成 | P1 |
| 4.2.4 | 输出: intent / activated_skills / skill_prompt_override / skill_tools | 同上 | Skill 信息写入 state | 开发完成 | P0 |

### 4.3 retrieve_schema Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.3.1 | retrieve_schema_node() | `src/graph/nodes/retrieve_schema.py` | 从 injected schema 提取表结构 (Phase 2 向量检索) | 单测完成 | P0 |
| 4.3.2 | 关键词提取 + 向量检索 | 同上 | Phase 2: ChromaDB 语义检索 (依赖模块 6) | 待开发[^5] | P1 |
| 4.3.5 | 检索业务规则 | 同上 | Phase 2: BusinessRuleStore (依赖模块 6) | 待开发[^5] | P1 |
| 4.3.6 | 检索历史 SQL 模板 | 同上 | Phase 2: LongTermMemoryStore (依赖模块 7) | 待开发[^5] | P1 |

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

### 4.5 layer3_validate Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.5.1 | layer3_validate_node() | `src/graph/nodes/layer3_validate.py` | sqlglot 语法 + 14 项安全拦截正则 | 单测完成 | P0 |
| 4.5.2 | SQL 安全拦截 | 同上 | 14 正则黑名单: INSERT/DELETE/DROP/ALTER/... | 单测完成 | P0 |
| 4.5.3 | sqlglot 语法解析校验 | 同上 | sqlglot.parse(sql, dialect) | 单测完成 | P0 |
| 4.5.7 | 输出: sql_valid / errors / transpiled | 同上 | | 单测完成 | P0 |

### 4.6 layer4_explain Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.6.1 | layer4_explain_node() | `src/graph/nodes/layer4_explain.py` | Phase 2 对接 Connector | 开发完成 | P1 |

### 4.7 execute_sql Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.7.1 | execute_sql_node() | `src/graph/nodes/execute_sql.py` | Phase 2 对接 Registry (Phase 1 mock) | 单测完成 | P0 |
| 4.7.2 | 错误信息简洁化 | `src/graph/nodes/execute_sql.py:82-91` | 从 DB 原始错误提取错误码和消息 | 开发完成 | P1 |
| 4.7.3 | 列名预检验证 (Layer 2) | `src/graph/nodes/execute_sql.py:_validate_column_references()` | sqlglot 解析列引用 → 对照 schema → 触发 retry | 开发完成 | P0 |

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

### 4.9 generate_chart Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.9.1 | generate_chart_node() | `src/graph/nodes/generate_chart.py` | Phase 2 ECharts 生成 (Phase 1 占位) | 单测完成 | P1 |
| 4.9.4 | CHART_RECOMMEND_PROMPT | `src/llm/prompts.py` | 图表推荐 Prompt | 单测完成 | P0 |

### 4.10 build_response Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.10.1 | build_response_node() | `src/graph/nodes/build_response.py` | 组装 success/error 两种响应 | 单测完成 | P0 |
| 4.10.2 | 正常响应 | 同上 | user_query+sql+data+analysis+chart | 单测完成 | P0 |
| 4.10.3 | 错误响应 | 同上 | error_code + error_message | 单测完成 | P0 |

---
