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
| 16.1.9 | SkillManager 测试 | `tests/test_skills/test_skill_manager.py` | discover / match_skills / build_skill_prompt | 待开发 |
| 16.1.10 | MCPClientManager 测试 | `tests/test_mcp/test_client_manager.py` | 使用 mock MCP Server 测试连接/转换/重连 | 待开发 |
| 16.1.11 | LLM 输出二次校验测试 | `tests/test_tools/test_sql_security.py` | 模拟 LLM 幻觉编造表名/字段名 → 验证拦截 | 待开发 | P1 |

### 16.2 Mock 工具与测试基础设施

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 16.2.1 | FakeListChatModel 工厂 | `tests/fixtures/mock_llm.py` | 按顺序返回预设 AIMessage，用于所有 LLM Node 测试 | 待开发 | P1 |
| 16.2.2 | SQLite MemoryDB Connector | `tests/fixtures/mock_db.py` | 基于 `aiosqlite` + `sqlalchemy` 的内存数据库连接器，用于 mock 真实 DB | 待开发 | P1 |
| 16.2.3 | StaticMCPTestServer | `tests/fixtures/mock_mcp.py` | 注册固定 tools 的内存 MCP Server，不依赖外部进程 | 待开发 | P1 |
| 16.2.4 | ChromaDB EphemeralClient | `tests/conftest.py` | 测试用临时向量库，`pytest.fixture` 自动创建销毁 | 待开发 | P1 |

### 16.3 集成测试

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 16.3.1 | 完整 LangGraph 工作流测试 | `tests/test_graph/test_workflow.py` | Mock LLM + SQLite 内存 DB，端到端走通 | 待开发 | P1 |
| 16.3.2 | SQL 错误重试集成测试 | 同上 | 模拟执行失败 → 验证回到 generate_sql 并最终成功 | 待开发 | P1 |
| 16.3.3 | 安全拦截终止流程测试 | 同上 | 模拟生成 Delete SQL → 验证直接返回错误不进入执行 | 待开发 | P1 |
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

模块功能点共 29 项，已完成 18 项，待开发 11 项。

| 功能点 | 不开发原因 | 可开发条件 | 预计开发时机 |
|--------|------------|------------|--------------|
| 16.1.9 SkillManager 测试 | 仅有导入冒烟，未覆盖目录发现、匹配和依赖解析 | 建立临时内置/租户 Skill 目录 fixture 和冲突优先级断言 | Phase 2，租户 Skill 扫描完成后 |
| 16.1.10 MCPClientManager 测试 | 本轮只覆盖 enabled=false，连接、工具转换和重连仍依赖外部进程 | 完成 StaticMCPTestServer，提供确定性的连接中断与恢复场景 | Phase 2，MCP 测试服务就绪后 |
| 16.1.11 LLM 输出二次校验测试 | 现有测试覆盖 SQL AST 与列预检，但未模拟 LLM 编造表/字段的完整生成链路 | 使用 FakeListChatModel 返回幻觉 SQL，并断言生成节点拒绝输出 | Phase 2，FakeListChatModel fixture 完成后 |
| 16.2.1 FakeListChatModel 工厂 | 当前 Node 测试使用 monkeypatch，缺少统一消息序列工厂 | 固化项目 LLM adapter 接口后实现共享 fixture | Phase 2，LLM 测试基建批次 |
| 16.2.2 SQLite MemoryDB Connector | SQLite 仅在单个安全测试中临时创建，尚未抽成共享连接器 fixture | 提取可复用 schema/seed/cleanup fixture | Phase 2，数据库集成测试批次 |
| 16.2.3 StaticMCPTestServer | 当前测试没有协议级内存 MCP Server | 确定 MCP SDK 测试 transport 并实现固定 tools 服务 | Phase 2，MCP 集成测试批次 |
| 16.2.4 ChromaDB EphemeralClient | 当前向量测试使用 FakeVectorStore，Schema 测试使用 mock collection | 提供临时目录、关闭钩子与 collection 清理 fixture | Phase 2，向量库集成测试批次 |
| 16.3.1 完整 LangGraph 工作流测试 | 已覆盖编译和无数据源错误链路，但未贯通 Fake LLM + SQLite 成功查询 | 先完成 16.2.1 与 16.2.2，再验证 SQL 生成到响应的成功链路 | Phase 2，测试 fixture 就绪后 |
| 16.3.2 SQL 错误重试集成测试 | 现有测试只确认最终响应存在，未证明条件边发生重试并最终成功 | 增加 Node 调用计数和先失败后成功的 SQLite 执行器 | Phase 2，完整工作流测试同期 |
| 16.3.3 安全拦截终止流程测试 | 已有 Node 级拦截，尚未断言 workflow 中 execute_sql 从未被调用 | 注入可观测执行器并验证安全分支短路 | Phase 2，完整工作流测试同期 |
| 16.3.4 三级 Schema 回退集成测试 | 已覆盖缓存读写和真实 refresh 委托，未覆盖空缓存到 DB 内省的完整回退 | 使用 SQLite MemoryDB 构造空 Chroma 缓存和真实表结构 | Phase 2，16.2.2/16.2.4 就绪后 |

---
