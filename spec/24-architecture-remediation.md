# 架构整改设计

## 目标

在不改变 API 路径、响应契约和 LangGraph 条件路由业务语义的前提下，统一扩展注册、数据库连接、启动编排和依赖来源。

## 模块边界

- `src/api/routes/` 按 chat、datasource、schema、session、mcp、knowledge、skills、management 拆分；`__init__.py` 只组合 `APIRouter` 并保留旧导出。
- `src/bootstrap.py` 顺序执行迁移、工作流、演示数据源、知识库、LLM、存储、Skills、MCP 和外部数据源初始化。生产环境失败阻断，非生产环境按阶段记录并继续。
- `src/connectors/registry.py` 维护方言到 Connector 类的映射。执行超时、EXPLAIN、探针和 Engine 参数归 Connector 所有。
- `src/llm/provider_registry.py` 维护 Provider 工厂，OpenAI 与 Anthropic 通过同一路径创建；`get_llm()` 保持兼容入口。
- `src/graph/node_registry.py` 集中声明节点 handler 和进度文案；条件路由继续在 `workflow.py` 显式定义。

## PostgreSQL 连接契约

- `src/db/utils.py::to_asyncpg_url()` 是 SQLAlchemy PostgreSQL URL 到 asyncpg DSN 的唯一转换入口。
- 请求和运行时存储统一使用 `get_pg_pool()`；只有版本迁移和 Checkpointer 自动建库保留独立连接。
- RLS 身份必须在 `pg_connection()` 的事务内通过 `set_config(..., true)` 设置，禁止在共享池连接上使用会话级 `false`。
- 事务退出后再归还连接，保证租户、用户和角色不会跨请求残留。

## 兼容决策

- SPEC 已定义的 `SQLGeneratorTool`、`DBExecutorTool` 和 `SchemaExplorerTool` 不删除；同步 `_run()` 明确拒绝异步上下文，调用方使用 `_arun()`。
- 节点注册表不自动推导业务拓扑，只消除 handler 和进度文案重复。
- `pyproject.toml` 是人工维护的唯一依赖来源；`requirements.txt` 为生成物。Django、文档解析和嵌入模型分别属于可选依赖组。

## 仓库历史契约

- `src/test_data.sql` 和根目录 `test_data.sql` 禁止进入任何 Git ref；历史清理后必须用 `git rev-list --objects --all` 验证零引用。
- 本地测试数据可以保留为 `.gitignore` 排除的工作区文件，不参与提交、bundle 或发布产物。
- 发布重写后的历史必须先备份仓库并通知协作者，再使用带 lease 的 force-push；不得在常规代码提交中静默改写远端历史。

## 验证

- AST 回归禁止仅含 `pass` 的异常处理和遗留 `asyncio.run()`。
- Provider、Connector、节点目录、路由拆包、bootstrap、连接池和依赖来源均有 pytest 契约测试。
- 默认测试禁止调用远程 LLM。
