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
