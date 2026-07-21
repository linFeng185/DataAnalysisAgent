# 附录

## 优先级分配总览

| 模块 | P0 | P1 | P2 | P3 | 小计 | 去重标注 |
|------|----|----|----|----|------|---------|
| 1. 项目基础设施 | 10 | 4 | — | — | 14 | — |
| 2. 数据源管理 | 20 | 12 | 3 | 1 | 36 | — |
| 3. 数据库连接器 | 8 | 2 | 4 | — | 14 | — |
| 4. LangGraph 编排引擎 | 28 | 16 | 3 | 1 | 48 | 4.10.4 → 见 7.3.5 |
| 5. 工具层 | 11 | 2 | — | — | 13 | — |
| 6. 知识库管理 | 14 | 10 | 4 | 1 | 29 | 6.3.4 与 6.6.3 协同 |
| 7. 记忆系统 | 10 | 14 | 4 | 2 | 30 | — |
| 8. MCP 集成 | 4 | 8 | 9 | 2 | 23 | — |
| 9. Skills 技能系统 | 4 | 6 | 5 | 3 | 18 | — |
| 10. LLM 管理层 | 10 | 4 | — | — | 14 | — |
| 11. API 层 | 10 | 10 | 2 | — | 22 | — |
| 12. 安全模块 | 8 | 4 | 2 | — | 14 | — |
| 13. 数据分析引擎 | — | 6 | — | — | 6 | — |
| 14. 可视化引擎 | — | 6 | 2 | — | 8 | — |
| 15. 评估与质量保障 | — | 2 | 4 | — | 6 | — |
| 16. 测试 | — | 5 | 10 | 2 | 17 | — |
| 17. 基础设施与运维 | 4 | 2 | 7 | 2 | 15 | 17.1.1 LangGraph 自动管理 |
| 18. 前端 | — | — | 13 | 5 | 18 | — |
| 19. 扩展能力 | — | — | 5 | 11 | 16 | — |
| **总计** | **141** | **113** | **72** | **29** | **355** | — |

> 优先级分配依据 SPEC.md §8 实现路线图的四个 Phase：
> - P0 = Phase 1 MVP（核心链路）
> - P1 = Phase 2 增强（正确性 + 体验）
> - P2 = Phase 3 生产化（多数据源/流式/容器化）
> - P3 = Phase 4 进阶（远期规划）

---

## 关键依赖关系图

以下列出跨模块的硬依赖。编号为 `模块.功能序号`。

```
Phase 1 核心链路:
  1.1.1 Poetry 项目 ──┬→ 2.1.1 DataSourceConfig
                      ├→ 10.1.1 ChatOpenAI 工厂
                      └→ 4.1.1 AnalysisState

  2.1.2 DataSourceRegistry ──→ 3.1.1 ConnectorBase
  2.5.x DB 内省 ────────────→ 3.1.1 ConnectorBase
  2.5.x DB 内省 ────────────→ 6.1.2 get_or_fetch_schema()
  2.6.1 SchemaSnapshot ──────→ 4.3 retrieve_schema Node

  4.1.1 AnalysisState ──────→ 4.2 ~ 4.10 所有 Node
  4.1.2 StateGraph 组装 ────→ 4.2 ~ 4.10 所有 Node
  4.2 classify_intent ──────→ 10.2.1 INTENT_CLASSIFY_PROMPT
  4.3 retrieve_schema ──────→ 6.1.2 get_or_fetch_schema()
  4.3 retrieve_schema ──────→ 6.3.3 search_business_rules()
  4.3 retrieve_schema ──────→ 7.3.4 search() 长期记忆
  4.4 generate_sql ─────────→ 10.2.2 SQL_GENERATION_SYSTEM_PROMPT
  4.4 generate_sql ─────────→ 7.5.1 build_llm_context()
  4.5 layer3_validate ──────→ 5.2.1 validate_with_sqlglot()
  4.6 layer4_explain ───────→ 3.x explain()
  4.7 execute_sql ──────────→ 2.1.2 DataSourceRegistry.resolve()
  4.7 execute_sql ──────────→ 12.2 限流控制
  4.8 analyze_result ───────→ 10.2.3 DATA_ANALYSIS_PROMPT
  4.9 generate_chart ───────→ 14.1 classify_chart_type()
  4.10 build_response ──────→ 4.4.10 SQL 模板保存 → 见 7.3.5

  3.1.1 ConnectorBase ──────→ 3.2/3.3/3.4 三个实现

  5.1.1 SchemaExplorerTool ─→ 6.1.2 get_or_fetch_schema()
  5.1.3 SQLglotValidator ───→ 5.2.1 validate_with_sqlglot()

  8.1.1 MCPClientManager ───→ config/mcp_servers.yaml (1.2.3)
  9.1.2 SkillManager ───────→ 4.2.3 Skill 匹配触发

Phase 2 关键依赖:
  11.1.2 流式查询 ──────────→ 4.1.2 StateGraph (astream_events)
  11.1.7~11.1.9 数据源API ──→ 2.3 ExternalProvider
  13.x 数据分析引擎 ─────────→ 4.8 analyze_result Node
  14.x 可视化引擎 ───────────→ 4.9 generate_chart Node
  7.2.8 summarize_session ──→ 10.1.4 cheap_llm 工厂
  7.5.1 build_llm_context ──→ 7.3.4 long_term_store.search()

Phase 3 关键依赖:
  3.3 MySQL 连接器 ──────────→ 3.1.1 ConnectorBase
  3.4 PostgreSQL 连接器 ─────→ 3.1.1 ConnectorBase
  17.3 Prometheus ───────────→ 11.1.10 GET /health
  18.x 前端全部 ─────────────→ 11.x 所有 API 完成
```

### 跨模块协同功能标注

以下功能在不同模块中被提及，但本质是同一实现：

| 功能 | 主实现位置 | 引用位置 | 说明 |
|------|-----------|---------|------|
| SQL 模板保存 | 7.3.5 save_sql_template() | 4.10.4 build_response | 4.10.4 调用 7.3.5，不独立实现 |
| Markdown 文档切片 | 6.3.5 _split_by_headings() | 6.6.3 按标题切片 | 6.3.4 是主实现，6.6.3 是 doc_loader 的复用 |
| 用户偏好存储 | 7.3.7 save_preference() | 7.2.6 on_session_start() | 7.2.6 读取 7.3.8，不独立存储 |

---

## 统计摘要

| 模块 | 功能点数量 | P0 | P1 | P2 | P3 |
|------|-----------|----|----|----|----|
| 1. 项目基础设施 | 14 | 10 | 4 | — | — |
| 2. 数据源管理 | 37 | 20 | 13 | 3 | 1 |
| 3. 数据库连接器 | 14 | 8 | 2 | 4 | — |
| 4. LangGraph 编排引擎 | 48 | 28 | 16 | 3 | 1 |
| 5. 工具层 | 13 | 11 | 2 | — | — |
| 6. 知识库管理 | 30 | 14 | 11 | 4 | 1 |
| 7. 记忆系统 | 32 | 10 | 16 | 4 | 2 |
| 8. MCP 集成 | 24 | 4 | 9 | 9 | 2 |
| 9. Skills 技能系统 | 18 | 4 | 6 | 5 | 3 |
| 10. LLM 管理层 | 14 | 10 | 4 | — | — |
| 11. API 层 | 28 | 10 | 16 | 2 | — |
| 12. 安全模块 | 15 | 8 | 5 | 2 | — |
| 13. 数据分析引擎 | 6 | — | 6 | — | — |
| 14. 可视化引擎 | 8 | — | 6 | 2 | — |
| 15. 评估与质量保障 | 6 | — | 2 | 4 | — |
| 16. 测试 | 23 | — | 11 | 10 | 2 |
| 17. 基础设施与运维 | 15 | 4 | 2 | 7 | 2 |
| 18. 前端 | 10 | — | — | 10 | — |
| 19. 扩展能力 | 20 | — | — | 9 | 11 |
| **总计** | **375** | **141** | **131** | **78** | **25** |

---

## 待开发项台账

每个待开发项均标注原因、触发条件和预计时机。条件满足时主动提醒开发。

[^1]: **1.2.3 MCP Server 注册表** — 原因: MCP Client 模块(8)未实现，配置文件结构需与 `MCPClientManager.connect_all()` 同步定义。条件: MCP Client Manager 开发时一并创建。时机: Phase 1 后续(模块 8)。

[^2]: **1.3.7 全局异常中间件 / 2.3.7~9 外挂 API 路由** — 原因: 依赖 FastAPI 路由体系，当前 `api/` 仅占位。条件: `api/routes.py` + `api/schemas.py` 创建时。时机: Phase 2(模块 11)。

[^3]: **2.1.4 DataSourceConfigStore** — 原因: 通用迁移执行器已完成，但 `datasource_configs` 的凭证加密字段和租户唯一键设计尚未落地。条件: 完成 17.1.5 表结构设计与迁移后。时机: Phase 3，多实例部署前。

[^4]: **2.4.3 KMS 集成** — 原因: 需 Vault/AWS KMS/Azure Key Vault 等外部基础设施，当前 AES 本地加密已覆盖 MVP。条件: 生产环境 KMS 就绪。时机: Phase 4。

[^5]: **4.3.2/4.3.5/4.3.6 Schema 关键词+向量检索/业务规则/历史模板** — 原因: 依赖模块 6 (知识库 ChromaDB) 和模块 7 (长期记忆 Core) 未实现。条件: SchemaManager + BusinessRuleStore + LongTermMemoryStore 就绪。时机: Phase 2 (模块 6/7 完成后)。

[^6]: **10.2.8 Prompt 版本号管理** — 原因: 需 LangSmith A/B 测试基础设施 + CI 集成。当前 Phase 1 无此需求。条件: Phase 3 生产化评估。时机: Phase 3。

[^7]: **4.8.5 归因分析** — 原因: 归因分析需 LLM 理解业务语义（"为什么销售额下降"），纯统计只能提供维度下钻数据，无法生成归因解释。条件: LLM 客户端(10.1)就绪 + ChatOpenAI 可用。时机: Phase 2 (模块 10 完成后)。

[^8]: **11.1.6/11.1.12/11.1.13/11.2.2 会话历史/MCP重置/指标列表/历史分页** — 原因: 依赖 Checkpointer(模块7)/MCPClientManager(模块8)/BusinessRuleStore(模块6) 未实现。条件: 对应模块就绪后立即实现。时机: Phase 2。
