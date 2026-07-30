# 架构整改设计

## 目标

在不改变 API 路径、响应契约和 LangGraph 条件路由业务语义的前提下，统一扩展注册、数据库连接、启动编排和依赖来源。

## 模块边界

- `src/api/routes/` 按 chat、datasource、schema、session、mcp、knowledge、skills、management 拆分；`__init__.py` 只组合 `APIRouter` 并保留旧导出。
- `src/bootstrap.py` 顺序执行迁移、工作流、演示数据源、知识库、LLM、存储、Skills、MCP 和外部数据源初始化。生产环境失败阻断，非生产环境按阶段记录并继续。
- `src/connectors/registry.py` 维护方言到 Connector 类的映射。执行超时、EXPLAIN、探针和 Engine 参数归 Connector 所有。
- `src/llm/provider_registry.py` 维护 Provider 工厂，OpenAI 与 Anthropic 通过同一路径创建；`get_llm()` 保持兼容入口。
- `src/graph/node_registry.py` 集中声明节点 handler 和进度文案；条件路由继续在 `workflow.py` 显式定义。
- `src/api/background_tasks.py` 是 API fire-and-forget 任务的唯一创建入口，持有任务强引用并在完成回调中消费和记录异常。
- `src/api/auth.py` 与 `src/api/security_headers.py` 使用纯 ASGI 中间件，避免 `BaseHTTPMiddleware` 截断流式响应上下文。

## PostgreSQL 连接契约

- `src/db/utils.py::to_asyncpg_url()` 是 SQLAlchemy PostgreSQL URL 到 asyncpg DSN 的唯一转换入口。
- 请求和运行时存储统一使用 `get_pg_pool()`；只有版本迁移和 Checkpointer 自动建库保留独立连接。
- RLS 身份必须在 `pg_connection()` 的事务内通过 `set_config(..., true)` 设置，禁止在共享池连接上使用会话级 `false`。
- 事务退出后再归还连接，保证租户、用户和角色不会跨请求残留。

## 兼容决策

- SPEC 已定义的 `SQLGeneratorTool`、`DBExecutorTool` 和 `SchemaExplorerTool` 不删除；同步 `_run()` 明确拒绝异步上下文，调用方使用 `_arun()`。
- 节点注册表不自动推导业务拓扑，只消除 handler 和进度文案重复。
- `pyproject.toml` 是人工维护的唯一依赖来源；`requirements.txt` 为生成物。Django、文档解析和嵌入模型分别属于可选依赖组。

## AppContext 与依赖注入

### 生命周期

- `AppContext` 是应用级依赖容器，每个 FastAPI 应用实例拥有独立 Context，禁止资源重新散落为模块级单例。
- `create_app()` 创建 Context 并保存到 `app.state.app_context`；lifespan 在该 Context 下执行 bootstrap，并在退出时逆序关闭已创建资源。
- `AppContextMiddleware` 为每个 HTTP/WebSocket 请求绑定当前 Context，保证 SSE、LangGraph 和后台任务读取同一应用实例。
- CLI、独立 Node 测试等无 FastAPI 场景允许创建进程级兼容 Context；测试必须能用 `use_app_context()` 临时覆盖并精确恢复。

### 接口

```python
@dataclass(slots=True)
class AppContext:
    settings: Settings
    tenant_policy: TenantPolicy

    def get_or_create(self, name, factory, *, closer=None): ...
    async def get_or_create_async(self, name, factory, *, closer=None): ...
    def set_resource(self, name, value, *, closer=None, replace=False): ...
    def get_resource(self, name, default=None): ...
    async def close_resource(self, name): ...
    async def close(self): ...
```

- 同一资源工厂在单个 Context 内最多执行一次；异步工厂必须用资源级 `asyncio.Lock` 防止并发重复初始化。
- close 顺序与初始化顺序相反，每个 closer 最多执行一次；关闭后的 Context 禁止继续创建资源。
- `get_request_app_context()` 是 FastAPI `Depends` 入口；Graph 编译时绑定显式 Context，节点兼容 getter 从当前绑定 Context 取依赖。
- 原 `get_registry()`、`get_vector_store()` 等函数在迁移期保留，但只委托 `AppContext`，不得继续维护模块级实例变量。

### 第一批集中资源

`DataSourceRegistry`、`SchemaManager`、`VectorStore`、`SessionStore`、`HistoryStore`、
`FileStore`、`UploadManager`、`KnowledgeTagStore`、`SkillManager`、`MCPClientManager`、
`DatasourceCache`、`ModelRegistry`、PostgreSQL Pool 和 LangGraph Checkpointer。

### 配置唯一来源

- `AppContext.settings` 是运行时配置的唯一实例来源；应用、请求、Graph、后台任务和资源工厂必须读取同一个对象。
- `get_settings()` 仅作为兼容入口，必须委托当前 `AppContext.settings`，禁止重新构造 `Settings()` 或维护第二份缓存。
- `create_app()` 是 FastAPI 启动边界，可在创建 `AppContext` 前构造一次 `Settings`；Context 建立后所有读取均受当前 Context 绑定。

## LLM 单一调用链

- 节点任务统一通过 `get_task_llm(task_name)` 进入任务路由，再由 `get_provider()` 解析并复用当前 Context 的 Provider。
- 多租户请求先按 `tenant_id + connection_id + model_id` 解析租户命名连接；未显式选择时使用
  当前租户默认连接和默认模型。解析结果通过请求级 ContextVar 进入统一调用链，禁止把凭证写入 Graph State。
- `get_llm()`、`get_openai_llm()` 和 `get_anthropic_llm()` 是兼容工厂，只能委托 `get_provider()`，不得直接创建厂商 ChatModel。
- 管理端模型连通性测试允许直接获取指定 Provider，但 Provider 创建、凭证、超时和 Adapter 注入仍走同一工厂。
- OpenAI-compatible 本地模型使用显式 `base_url/api_key/timeout` 覆盖创建 Provider，不得绕过 Provider 直接调用 Adapter。
- Provider Context 缓存键必须包含连接地址和 API Key 的不可逆摘要；同模型、同地址但不同凭证不得复用认证客户端。
- 平台厂商目录只声明 `openai_compatible/anthropic` 协议和模型能力；API Key 归租户命名连接所有，
  多租户模式禁止回退到平台 Settings 凭证。完整契约见 `spec/28-tenant-identity-and-llm-governance.md`。

## SQL 安全执行边界

- `src/security/sql_execution.py` 是 SQL 解析、只读校验、方言重写、EXPLAIN 和有界执行的唯一实现边界。
- `validate_sql()` 必须按数据源真实方言解析并在异常时 fail-closed；注释不参与危险关键字判断，AST 中的写操作和状态变更函数必须阻断。
- `validate_and_explain_sql()` 供 Layer 4 和 EXPLAIN Tool 使用；`validate_and_execute_sql()` 供主工作流、多数据源 worker 和 `DBExecutorTool` 使用。
- 调用方传入的方言仅作提示，数据源 Registry 返回的方言是执行权威；两者不一致时记录 warning 并使用 Registry 方言。
- 执行统一使用 Connector 的 `execute_bounded()`，禁止 Tool 绕过结果上限调用裸 `execute()`。

## 异常处理决策矩阵

| 领域 | 策略 | 允许的结果 |
|---|---|---|
| SQL 安全、表列白名单、权限注入 | fail-closed | 返回明确校验错误，禁止进入数据库 |
| 数据库连接、EXPLAIN、SQL 执行 | fail-closed | 返回明确配置或执行错误，禁止伪造成功 |
| LLM 调用 | fail-open | 降级到确定性规则、模板或不可用提示 |
| 知识库检索 | fail-open | 记录完整异常并返回空证据 |
| 数据处理器与图表生成 | fail-open | 回退通用分析或表格展示 |

`src/failure_policy.py` 集中声明上述策略。安全域不得由单个函数临时决定吞异常；所有回退必须记录原因，异常日志包含堆栈。

## VectorStore 所有权

- `VectorStore` 及具体实现拥有向量客户端、Collection、嵌入函数和关闭生命周期。
- `SchemaManager`、`BusinessRuleStore` 等调用方只依赖 `VectorStore` 公共接口，禁止保存或访问 ChromaDB Collection。
- Chroma 工厂直接从当前 Context 配置创建 `ChromaVectorStore`，不得通过 `SchemaManager._collection` 反向取资源。
- 切换 `VECTOR_STORE_TYPE` 后，Schema、业务规则、知识检索和上传写入必须使用同一个 Context 资源。
- 三种后端共享 metadata 过滤 DSL：普通键表示等值，`not:key` 与 `{"$ne": value}` 表示不等值；非法运算符失败关闭。
- Chroma、Milvus 和 pgvector 的 search/get/delete/count 必须保持相同过滤语义；`delete_by_filter({})` 必须返回 0，禁止解释为全表删除。
- pgvector 扩展、表或索引初始化失败必须 fail-closed，禁止返回延迟到首个请求才报错的半初始化 Store。

## 公共生命周期与抽象边界

- 启动编排只调用资源公开生命周期接口：`FileStore.initialize()`、`get_vector_store()` 和 `AppContext.close()`。
- API 路由不得读取管理器私有状态；MCP Server 选择、Skill 清单解析必须由管理器公开方法完成。
- 结构化查询只能使用 `StructuredAssetAdapter.load_tables()` 与 `serialize_frame()`，不得调用格式识别、DataFrame 加载或 JSON 转换私有方法。
- AST 架构门禁持续检查上述跨模块私有访问，测试内部白盒断言不视为生产边界泄漏。

## AnalysisState 持久化边界

- 会话级字段由 Checkpointer 保存：`user_query`、`datasource`、`session_id`、`intent`、精简 `conversation_history`、`messages` 和轻量上一轮引用。
- 请求级字段使用 LangGraph `UntrackedValue`：身份/权限快照、Schema、SQL 校验与执行中间态、结果样本、多源结果、分析、图表和 `final_response`。
- `conversation_history` 只保存查询、SQL、成功状态和摘要，不保存 `final_result/data/chart` 大对象。
- 每轮完整富结果以 `query_history.final_result` 为权威存储；会话 UI 和“分析刚才的数据”按 `session_id` 从 HistoryStore 恢复，checkpoint 只保存轻量元数据。
- `previous_turn_snapshot` 禁止包含 `query_result_sample`、`multi_source_results`、`analysis_result` 或 `chart_config`。

## 路由导入边界

- 每个领域路由只显式导入自身使用的标准库、FastAPI 类型和 Schema。
- 禁止 `from ._helpers import *`；共享 helper 只收敛真正共享的行为，不作为符号转发层。
- AST 契约测试阻止复制整块 Schema 导入和模块级未使用导入回归。

## TenantPolicy 租户策略

### 常量与身份

```python
SYSTEM_TENANT_ID = 0
DEFAULT_TENANT_ID = 1
ANONYMOUS_USER_ID = 0
ANONYMOUS_ROLE = "anonymous"
```

- `RequestIdentity` 统一保存 `tenant_id/user_id/role`，请求身份仍由 ContextVar 隔离，不作为 AppContext 的可变字段。
- 单租户匿名身份固定为 `(tenant_id=1, user_id=0, role=anonymous)`；系统后台身份固定使用 `tenant_id=0`，禁止混用。

### 策略职责

- `requires_authentication(is_probe)`：决定当前部署是否强制 JWT。
- `tenant_filter(tenant_id, explicit=False)`：统一生成向量库/知识库租户过滤条件；单租户兼容数据默认不附加过滤，显式租户查询除外。
- `validate_identity(identity)`：多租户请求必须具备正数 tenant/user ID，失败关闭。
- `can_write_scope(scope, identity)`：统一 system/tenant/private 写权限和匿名开发兼容。
- `datasource_isolation_enabled`、`knowledge_isolation_enabled`：替代业务模块直接读取 `settings.multi_tenant`。

业务模块允许调用策略方法，但禁止新增 `if get_settings().multi_tenant`。RLS、VectorStore metadata、
数据源授权和知识作用域的单/多租户差异必须由 `TenantPolicy` 的表驱动测试覆盖。

## 仓库历史契约

- `src/test_data.sql` 和根目录 `test_data.sql` 禁止进入任何 Git ref；历史清理后必须用 `git rev-list --objects --all` 验证零引用。
- 本地测试数据可以保留为 `.gitignore` 排除的工作区文件，不参与提交、bundle 或发布产物。
- 发布重写后的历史必须先备份仓库并通知协作者，再使用带 lease 的 force-push；不得在常规代码提交中静默改写远端历史。

## 验证

- AST 回归禁止仅含 `pass` 的异常处理和遗留 `asyncio.run()`。
- Provider、Connector、节点目录、路由拆包、bootstrap、连接池和依赖来源均有 pytest 契约测试。
- 默认测试禁止调用远程 LLM。
