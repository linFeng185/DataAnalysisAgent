# 16. 测试

## 16. 测试 `[P1:5 P2:10 P3:2]`

### 16.1 单元测试

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 16.1.1 | KnowledgeEntry / SchemaSnapshot 序列化测试 | `tests/test_datasource/test_schema_snapshot.py` | 测试 to_prompt_text() 格式正确 | 待开发 |
| 16.1.2 | DataSourceConfig 验证测试 | `tests/test_datasource/test_config.py` | 测试必填字段校验 | 待开发 |
| 16.1.3 | sqlglot validator 测试 | `tests/test_tools/test_sqlglot_validator.py` | 测试各种 SQL 错误的拦截和函数建议 | 待开发 |
| 16.1.4 | SQL 安全拦截测试 | `tests/test_tools/test_sql_security.py` | Drop/Delete/Insert 语句拦截验证 | 待开发 |
| 16.1.5 | compute_statistics() 测试 | `tests/test_tools/test_analyzer.py` | pandas 统计计算正确性 | 待开发 |
| 16.1.6 | classify_chart_type() 测试 | `tests/test_tools/test_chart_generator.py` | 各种列组合的选图正确性 | 待开发 |
| 16.1.7 | LongTermMemoryStore 测试 | `tests/test_memory/test_long_term_store.py` | CRUD + 置信度过滤 + 语义检索 | 待开发 |
| 16.1.8 | build_llm_context() 测试 | `tests/test_memory/test_context_builder.py` | 三层裁剪逻辑验证 | 待开发 |
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
| 16.3.5 | API 集成测试 | `tests/test_api/test_routes.py` | httpx AsyncClient + FastAPI TestClient 测试所有接口 | 待开发 | P1 |
| 16.3.6 | 条件边路由测试 | `tests/test_graph/test_routing.py` | 构造特定 AnalysisState，验证 after_layer3 / should_retry 返回值 | 待开发 | P1 |

---
