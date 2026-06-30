# 10. 目录结构建议

## 10. 目录结构建议

```
dataAnalysisAgent/
├── src/
│   ├── graph/                       # LangGraph 状态图
│   │   ├── state.py                 # AnalysisState TypedDict 定义
│   │   ├── workflow.py              # StateGraph 组装 + compile
│   │   └── nodes/                   # 各 Node 实现
│   │       ├── classify_intent.py   # 意图识别 Node
│   │       ├── retrieve_schema.py   # Schema 检索 Node
│   │       ├── generate_sql.py      # SQL 生成 Node（含方言 Prompt 注入）
│   │       ├── layer3_validate.py   # ★ sqlglot 语法校验 Node
│   │       ├── layer4_explain.py    # ★ EXPLAIN 空跑校验 Node
│   │       ├── execute_sql.py       # SQL 执行 Node
│   │       ├── analyze_result.py    # 数据分析 Node
│   │       ├── generate_chart.py    # 图表生成 Node
│   │       └── build_response.py    # 响应组装 Node
│   ├── tools/                       # LangChain BaseTool 封装
│   │   ├── schema_explorer.py       # SchemaExplorerTool
│   │   ├── sql_generator.py         # SQLGeneratorTool
│   │   ├── sqlglot_validator.py     # ★ sqlglot 方言校验 Tool
│   │   ├── db_executor.py           # DBExecutorTool
│   │   ├── db_explain.py            # ★ EXPLAIN 空跑校验 Tool
│   │   ├── analyzer.py              # DataAnalyzerTool
│   │   └── chart_generator.py       # ChartGeneratorTool
│   ├── connectors/                  # 数据库连接器
│   │   ├── base.py                  # 连接器基类
│   │   ├── clickhouse.py
│   │   ├── mysql.py
│   │   └── postgres.py
│   ├── datasource/                   # ★ 数据源注册与管理
│   │   ├── registry.py              # DataSourceRegistry 统一入口
│   │   ├── config.py                # DataSourceConfig 定义
│   │   ├── providers/
│   │   │   ├── base.py              # DataSourceProvider 抽象基类
│   │   │   ├── embedded.py          # EmbeddedProvider (内置模式)
│   │   │   └── external.py          # ExternalProvider (外挂模式)
│   │   ├── schema_snapshot.py       # SchemaSnapshot 统一数据结构
│   │   ├── introspection.py         # DB 内省 (INFORMATION_SCHEMA 查询)
│   │   └── credential_manager.py   # 凭证加解密管理
│   ├── memory/
│   │   ├── checkpointer.py          # PostgresSaver / MemorySaver 配置
│   │   ├── long_term_store.py       # 长期记忆 (ChromaDB + PostgreSQL)
│   │   └── session_archive.py       # 会话归档与摘要
│   ├── mcp/                          # ★ MCP 集成
│   │   ├── client_manager.py        # MCPClientManager (连接/转换/健康检查)
│   │   ├── server.py                # FastMCP Server (对外暴露分析能力)
│   │   └── tool_adapter.py          # MCP Tool → LangChain BaseTool 适配器
│   ├── skills/                       # ★ Skills 技能包
│   │   ├── data_quality_check/
│   │   │   ├── SKILL.md             # Skill 清单 (YAML frontmatter)
│   │   │   ├── tools.py             # 工具实现
│   │   │   └── prompts.py           # 专属 Prompt
│   │   └── custom_report/
│   │       ├── SKILL.md
│   │       └── templates/
│   │           └── weekly_report.jinja2
│   ├── skill_manager.py             # ★ Skill 发现/加载/激活引擎
│   ├── config/                       # 配置文件
│   │   └── mcp_servers.yaml         # MCP Server 注册表
│   ├── knowledge/                    # 知识库管理
│   │   ├── schema_manager.py        # Schema 缓存管理 (文档 → 自动拉取 三级回退)
│   │   ├── business_rules.py        # 业务规则存储与检索
│   │   ├── cache_refresher.py       # AUTO 来源过期刷新 + DDL 监听
│   │   ├── enum_discovery.py        # 枚举值自动发现
│   │   └── doc_loader.py            # Markdown 文档解析与索引
│   ├── docs/                         # ★ 手工维护的业务文档
│   │   └── metrics/                 # 指标口径文档 (*.md)
│   │       ├── gmv.md
│   │       ├── user_definitions.md
│   │       └── order_status.md
│   ├── api/
│   │   ├── routes.py                # FastAPI 路由
│   │   ├── schemas.py               # Pydantic 请求/响应模型
│   │   └── streaming.py             # SSE 流式输出 (astream_events)
│   ├── llm/
│   │   ├── client.py                # ChatOpenAI / ChatAnthropic 工厂
│   │   └── prompts.py               # ChatPromptTemplate 集中管理
│   └── config.py                    # Settings (pydantic-settings)
├── tests/
│   ├── test_graph/                  # LangGraph 集成测试
│   │   ├── test_workflow.py
│   │   └── test_nodes/
│   ├── test_tools/                  # Tool 单元测试
│   └── fixtures/                    # 测试用的 schema + 数据 fixtures
├── tests/
├── frontend/                    # 前端项目 (Phase 3)
├── docker-compose.yml
├── requirements.txt
└── SPEC.md
```

---
