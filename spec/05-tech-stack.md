# 4. 技术栈选型

## 4. 技术栈选型

| 层次 | 技术 | 说明 |
|-----|------|------|
| 后端框架 | Python FastAPI | 异步支持、生态完善 |
| LLM 接入 | OpenAI API / Claude API | 支持多模型切换 |
| LLM 框架 | LangChain + LangGraph | StateGraph 管理复杂流水线；BaseTool 封装工具复用；ChatPromptTemplate 统一 Prompt 管理；checkpointer 内置会话持久化 |
| MCP 协议 | mcp (Python SDK) + FastMCP | MCP Client 导入外部工具；MCP Server 对外暴露能力；stdio + SSE 双传输 |
| 数据库连接 | SQLAlchemy 2.0 / clickhouse-connect | 异步 + 连接池 |
| SQL 解析校验 | sqlglot | 多方言解析、转译、函数白名单 |
| 数值计算 | pandas + numpy | 统计分析 |
| 可视化 | Plotly + ECharts | 服务端渲染 + 前端渲染 |
| 向量检索 | ChromaDB | Schema 语义检索、字段级索引、业务文档检索、历史 SQL 检索 |
| Agent UI 调试 | LangSmith | 全链路追踪、Node 级耗时、Prompt 版本管理 |
| 前端 | React + TypeScript | 如果选 Web UI |
| Agent UI 调试 | LangSmith | 全链路追踪、Node 级耗时、Prompt 版本管理 |
| 缓存 | Redis | 查询结果缓存、限流计数 |
| 任务队列 | Celery / ARQ | 长查询异步执行 |

---
