# 16. 测试

## 16. 测试 `[P1:5 P2:10 P3:2]`

### 16.1 单元测试

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 16.1.1 | KnowledgeEntry / SchemaSnapshot 序列化测试 | `tests/test_knowledge/test_models.py`、`tests/test_datasource/test_schema.py` | 测试字典往返与 to_prompt_text() 格式 | 单测完成 |
| 16.1.2 | DataSourceConfig 验证测试 | `tests/test_datasource/test_schema.py`、`test_schema_edge_cases.py` | 测试默认值、完整配置与边界字段 | 单测完成 |
| 16.1.3 | sqlglot validator 测试 | `tests/test_tools/test_sqlglot_validator.py` | 测试多方言、SQL 错误拦截和函数建议 | 单测完成 |
| 16.1.4 | SQL 安全拦截测试 | `tests/test_security/test_sql_security.py` | 非只读语句、解析失败、权限失败关闭与审计 hash | 单测完成 |
| 16.1.5 | compute_statistics() 测试 | `tests/test_tools/test_analyzer.py` | 正常、空值、空输入和非数值列统计 | 单测完成 |
| 16.1.6 | classify_chart_type() 测试 | `tests/test_tools/test_chart_generator.py` | 各种列组合的选图正确性 | 单测完成 |
| 16.1.7 | LongTermMemoryStore 测试 | `tests/test_memory/test_long_term_store.py` | CRUD + 置信度过滤 + 语义检索 | 单测完成 |
| 16.1.8 | build_llm_context() 测试 | `tests/test_memory/test_context_builder.py` | 三层裁剪逻辑验证 | 单测完成 |
| 16.1.9 | SkillManager 测试 | `tests/test_skill_manager_v2.py` | discover / match_skills / manifest v2 / 权限预算 | 单测完成 |
| 16.1.10 | MCPClientManager 测试 | `tests/test_mcp/test_client_manager.py` | 作用域隔离、连接转换、重连和生命周期 | 单测完成 |
| 16.1.11 | LLM 输出二次校验测试 | `tests/test_graph/test_workflow_integration.py` | Fake LLM 生成危险 SQL → 验证 Layer 3 短路且执行器零调用 | 集成测试完成 | P1 |

### 16.2 Mock 工具与测试基础设施

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 16.2.1 | FakeListChatModel 工厂 | `tests/fixtures/mock_llm.py` | 按顺序返回预设 AIMessage，支持 ainvoke/astream | 单测完成 | P1 |
| 16.2.2 | SQLite MemoryDB Connector | `tests/fixtures/mock_db.py` | aiosqlite + SQLAlchemy StaticPool，真实建表、seed、EXPLAIN 和执行 | 单测完成 | P1 |
| 16.2.3 | StaticMCPTestServer | `tests/fixtures/mock_mcp.py` | 注册固定 tools 的内存 MCP Server，不依赖外部进程 | 单测完成 | P1 |
| 16.2.4 | ChromaDB EphemeralClient | `tests/conftest.py` | 临时 Collection + 确定性 embedding，fixture 自动创建销毁 | 单测完成 | P1 |

### 16.3 集成测试

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 16.3.1 | 完整 LangGraph 工作流测试 | `tests/test_graph/test_workflow_integration.py` | Fake LLM + SQLite 真实校验、EXPLAIN、执行、分析和响应 | 集成测试完成 | P1 |
| 16.3.2 | SQL 错误重试集成测试 | 同上 | 首轮缺失字段 EXPLAIN 失败 → 条件边重生成 → 最终成功 | 集成测试完成 | P1 |
| 16.3.3 | 安全拦截终止流程测试 | 同上 | Fake LLM 生成 DELETE → Layer 3 终止且 execute_bounded 未调用 | 集成测试完成 | P1 |
| 16.3.4 | 三级 Schema 回退集成测试 | `tests/test_knowledge/test_schema_manager.py` | Mock 空缓存 → 验证走到 DB 内省兜底 | 待开发 | P1 |
| 16.3.5 | API 集成测试 | `tests/test_api/` | httpx AsyncClient + ASGITransport 覆盖 health、chat、认证和数据源生命周期 | 集成测试完成 | P1 |
| 16.3.6 | 条件边路由测试 | `tests/test_graph/test_workflow.py`、`tests/test_graph_routing.py` | 构造 AnalysisState 验证 after_layer3 / after_layer4 / should_retry / route_by_intent | 集成测试完成 | P1 |

### 16.4 缺陷整改回归测试

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 16.4.1 | 生产配置与日志安全 | `tests/test_config_security.py` | 生产启动拒绝、七天日志轮转、禁用 MCP 和 Compose 凭证回归 | 单测完成 |
| 16.4.2 | SQL 执行安全 | `tests/test_security/test_sql_security.py`、`tests/test_graph/test_execute_security.py` | AST 白名单、权限关闭、有界读取、脱敏与截断响应 | 单测完成 |
| 16.4.3 | 认证与租户隔离 | `tests/test_api/test_auth_security.py`、`tests/test_memory/test_tenant_isolation.py` | Cookie、ContextVar、Session/History/FileStore 与 RLS 回归 | 单测完成 |
| 16.4.4 | 管理 API 与上传安全 | `tests/test_api/test_management_routes.py` | 数据源生命周期、Schema 管理、XSS 转义和大小限制 | 集成测试完成 |
| 16.4.5 | 正确性回归 | `tests/test_graph/test_correctness_regressions.py` | 无 LLM 回退、SQLite 内省和分析采样崩溃回归 | 单测完成 |
| 16.4.6 | 工作流与模型路由整改 | `tests/test_graph/test_workflow_remediation.py`、`tests/test_llm/test_task_routing.py`、`tests/test_mcp/test_client_manager.py` | 编译图、状态清理、EXPLAIN、错误分流、本地/远程任务和 MCP 租户边界 | 单测完成 |
| 16.4.7 | 安全扫描关键盲区补测 | `tests/test_graph/test_mcp_agent.py`、`tests/test_api/test_middleware.py`、`tests/test_connectors/test_mssql.py`、`tests/test_connectors/test_sqlite.py` | MCP 授权降级、异常响应脱敏、SHOWPLAN 清理和 SQLite Engine 契约 | 单测完成 |
| 16.4.8 | 其余零覆盖模块补盲 | `tests/test_data_generation.py`、`tests/test_datasource/test_setup.py`、`tests/test_mcp/test_server.py`、`tests/test_memory/`、`tests/test_tools/` | coverage 基线、67% 门禁及全部真实零覆盖生产模块的公共行为测试 | 单测完成 | P1 |

### 模块收尾

模块功能点共 29 项，已完成 28 项，待开发 1 项。

| 功能点 | 不开发原因 | 可开发条件 | 预计开发时机 |
|--------|------------|------------|--------------|
| 16.3.4 三级 Schema 回退集成测试 | 已覆盖缓存读写和真实 refresh 委托，未覆盖空缓存到 DB 内省的完整回退 | 使用 SQLite MemoryDB 构造空 Chroma 缓存和真实表结构 | Phase 2，16.2.2/16.2.4 就绪后 |

---
