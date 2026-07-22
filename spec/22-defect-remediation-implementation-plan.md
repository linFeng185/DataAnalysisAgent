# 缺陷系统整改实施计划

> **执行要求：** 使用 `superpowers:executing-plans` 在当前工作树逐项执行；每项遵循测试先行，完成后立即运行定向回归。

**目标：** 消除 `spec/21-defect-remediation-design.md` 列出的安全、正确性、API 生命周期与运维缺陷，并恢复完整测试基线。

**架构：** 保留 FastAPI、LangGraph、DataSourceRegistry 和 VectorStore 边界，在边界层增加生产配置验证、认证上下文、租户过滤、只读 SQL AST 校验和有界结果处理。管理 API 复用全局 Provider，不引入新重量级依赖。

**技术栈：** Python 3.12+、FastAPI、Pydantic、sqlglot、SQLAlchemy、structlog、pytest、React、TypeScript。

## 全局约束

- `dev/test + MULTI_TENANT=false` 保留匿名演示；`prod` 强制认证与安全配置。
- 每个新增或修改的公开函数包含中文说明、参数和返回值说明，并记录入口、出口与异常日志。
- 不修改根目录 `SPEC.md` / `FEATURES.md`；设计与状态只写 `spec/` / `features/`。
- 不覆盖当前工作树的用户改动，不引入 SPEC 未声明的重量级依赖。

---

### 任务 1：生产配置、日志与启动供应链

**文件：**
- 修改：`src/config.py`
- 修改：`src/main.py`
- 修改：`src/logging_config.py`
- 修改：`src/mcp_client/client_manager.py`
- 修改：`config/mcp_servers.yaml`
- 修改：`docker-compose.yml`
- 测试：`tests/test_config_security.py`

**接口：**
- 新增 `validate_production_settings(settings: Settings) -> None`
- `setup_logging()` 同时配置控制台与 7 天文件轮转
- `MCPClientManager.connect_all()` 跳过 `enabled=false` 的服务

- [x] 编写生产配置拒绝弱密钥、日志轮转和禁用 MCP 的失败测试。
- [x] 运行 `pytest tests/test_config_security.py -q`，确认因缺少实现失败。
- [x] 实现生产配置验证、日志轮转、Docker 环境变量和禁用默认远程 MCP。
- [x] 运行定向测试并确认通过（5 passed）。

### 任务 2：SQL 只读白名单、权限关闭、结果上限与脱敏审计

**文件：**
- 修改：`src/graph/nodes/layer3_validate.py`
- 修改：`src/security/permission_check.py`
- 修改：`src/security/data_masker.py`
- 修改：`src/graph/nodes/execute_sql.py`
- 修改：`src/graph/nodes/build_response.py`
- 修改：`src/graph/state.py`
- 修改：`src/api/schemas.py`
- 修改：`src/api/routes.py`
- 修改：`frontend/src/types/index.ts`
- 测试：`tests/test_security/test_sql_security.py`
- 测试：`tests/test_graph/test_execute_security.py`
- 测试：`tests/test_api/test_routes.py`

**接口：**
- 新增 `validate_readonly_sql(sql: str, dialect: str) -> list[dict]`
- `inject_row_filter()` 失败时抛出 `SQLSecurityError`
- `mask_sensitive_data()` 在结果写入 state 前执行
- SQLAlchemy 结果最多读取 `max_result_rows + 1` 行并报告截断

- [x] 编写 `CALL`、`VACUUM`、`SET ROLE`、坏 SQL、坏行过滤、PII 和超限结果测试。
- [x] 运行定向测试，确认旧实现失败（11 failed）。
- [x] 实现 AST 白名单、失败关闭、有界读取、用户级限流、脱敏和 hash 审计。
- [x] 运行任务 2 全部测试并确认通过（16 passed，前端 build 通过）。

### 任务 3：Cookie 认证与租户资源隔离

**文件：**
- 修改：`src/api/auth.py`
- 修改：`src/api/routes.py`
- 修改：`src/api/streaming.py`
- 修改：`src/memory/session_store.py`
- 修改：`src/memory/history_store.py`
- 修改：`src/knowledge/file_store.py`
- 修改：`migrations/001_batch1.sql`
- 修改：`frontend/src/api/client.ts`
- 修改：`frontend/src/hooks/AuthContext.tsx`
- 修改：`frontend/src/App.tsx`
- 测试：`tests/test_api/test_auth_security.py`
- 测试：`tests/test_memory/test_tenant_isolation.py`

**接口：**
- 登录响应设置 `access_token` HttpOnly Cookie，登出清除 Cookie
- `AuthMiddleware` 从 Cookie 或 Bearer 读取令牌，并在 `finally` 重置 ContextVar
- Session/History/FileStore 的读写使用当前 `tenant_id` / `user_id`
- 前端请求统一 `credentials: 'include'`

- [x] 编写 Cookie、ContextVar 清理和跨租户不可见测试。
- [x] 运行定向测试，确认旧实现失败（8 failed）。
- [x] 实现认证与租户字段、查询过滤、迁移顺序修正和前端 Cookie 客户端。
- [x] 运行后端定向测试（11 passed）与前端 TypeScript 构建（通过）。

### 任务 4：管理 API、上传与 XSS

**文件：**
- 修改：`src/datasource/registry.py`
- 修改：`src/datasource/providers/external.py`
- 修改：`src/knowledge/schema_manager.py`
- 修改：`src/knowledge/models.py`
- 修改：`src/knowledge/upload_manager.py`
- 修改：`src/memory/vector_store_chroma.py`
- 修改：`src/api/routes.py`
- 修改：`src/config.py`
- 修改：`frontend/src/pages/KnowledgePage.tsx`
- 测试：`tests/test_api/test_management_routes.py`

**接口：**
- Registry 提供 `get_provider()`、`invalidate()` 和 `unregister()`
- SchemaManager 提供 `refresh(datasource_name: str)` 和字段备注更新
- 上传在读取前后校验 `max_upload_bytes`
- `_docx_to_html()` 对段落和单元格内容进行 HTML 转义

- [x] 编写注册后可见、删除后不可见、不存在数据源刷新 404、XSS 转义和超限上传测试。
- [x] 运行定向测试，确认旧实现失败（6 failed）。
- [x] 实现全局 Provider 生命周期、真实刷新/备注、上传限制和 HTML 转义。
- [x] 运行任务 4 测试并使用 ASGI 客户端复核状态变化（7 passed；组合回归 23 passed）。

### 任务 5：分析、方言、workflow 与旧测试契约

**文件：**
- 修改：`src/datasource/introspection.py`
- 修改：`src/graph/nodes/generate_sql.py`
- 修改：`src/graph/nodes/analyze_result.py`
- 修改：`src/graph/workflow.py`
- 修改：现有失败测试文件
- 测试：`tests/test_graph/test_correctness_regressions.py`

**接口：**
- SQLite 使用 `sqlite_master` 和 `PRAGMA table_info`
- 无 LLM 数量查询生成 `COUNT(*)`，不确定查询返回明确不可用错误
- `_llm_analyze()` 正确计算样本是否完整
- 处理器路径始终返回 `statistics`
- workflow 测试通过显式 `await build_workflow()` 或应用 lifespan 初始化

- [x] 编写数量回退、SQLite 内省、LLM 分析长度和统计契约测试。
- [x] 运行定向测试，确认旧实现失败（5 failed）。
- [x] 实现正确性修复并更新已经漂移的旧测试契约。
- [x] 运行全部 graph、knowledge、API 测试并消除 coroutine 警告（阶段基线 358 passed，最终全量 369 passed）。

### 任务 6：状态同步与完整验收

**文件：**
- 修改：`src/datasource/providers/external.py`
- 修改：`src/datasource/registry.py`
- 修改：`src/connectors/oracle.py`
- 修改：`src/connectors/clickhouse.py`
- 修改：`config/datasources.yaml`
- 测试：`tests/test_datasource/test_clickhouse_registry.py`
- 测试：`tests/test_connectors/test_base.py`
- 测试：`tests/test_config_security.py`
- 修改：`features/02-datasource.md`
- 修改：`features/03-connectors.md`
- 修改：`features/README.md`
- 修改：`features/04-graph.md`
- 修改：`features/11-api.md`
- 修改：`features/12-security.md`
- 修改：`features/16-testing.md`
- 修改：`features/17-ops.md`
- 视目录/数据流变化决定是否修改：`CODE_GUIDE.md`

- [x] 更新本轮功能状态和缺陷修复说明，纠正桩实现或失败测试对应的虚假完成状态。
- [x] 修复 Oracle 21c 的 DUAL 连通性探针、Oracle 同步 Engine 异步适配和 service_name URL。
- [x] 修复 ClickHouse 缺失 SQLAlchemy 方言、HTTP 端口映射和工具 Connector 路径。
- [x] 移除数据源 YAML 与导入脚本中的明文数据库密码，统一使用环境变量。
- [x] 运行 `.venv\\Scripts\\python.exe -m pytest -q`（369 passed，0 warning）。
- [x] 运行 `.venv\\Scripts\\python.exe -m compileall -q src tests`。
- [x] 运行 `npm run build`（构建成功；Vite 提示主包超过 500 kB 的性能建议）。
- [x] 受控启动服务并用 curl 验证 health、chat、认证、五种数据库 Schema 和数据源注册/删除。
- [x] 检查 `git diff --check`、高风险密钥格式扫描和最终工作树差异。

> 验收备注：当前工作区未运行 Docker；数据库服务通过 `192.168.195.133` 提供。ClickHouse 使用已声明的 `clickhouse-connect` HTTP 客户端，配置 `CLICKHOUSE_PASSWORD` 后可用；生产环境仍需按 `features/16-testing.md` 与 `features/17-ops.md` 中的条件完成后续专项。

### 任务 7：权限、入口资源预算、审计与迁移闭环

**范围：**
- Chat 在进入工作流前解析当前身份可访问的数据源；未选择时只允许模型在授权候选中路由。
- 数据源列表按身份过滤；多源 worker 使用各自列权限和行过滤条件。
- 受管 MCP 禁止 stdio 和任意远程主机，ADMIN_API_KEY 仅保护平台基础设施写操作。
- Chat/登录/上传增加入口限流与资源预算；查询审计同步写入 PostgreSQL，日志统一脱敏。
- 新增 `src/db/migrations.py`，提供顺序、checksum、advisory lock 和事务回滚。
- 会话响应保留完整 ID，删除时清理 SessionStore、HistoryStore 和新旧 Checkpointer 线程。
- API 拒绝尚无连接器的 Presto/Hive，前端升级 ECharts 并正确报告 SSE 读取失败。

- [x] 先增加权限绕过、MCP RCE/SSRF、限流、上传、审计、迁移、会话、方言和 SSE 失败测试。
- [x] 按边界日志定位数据消失层并实施最小修复。
- [x] 定向组合回归通过（162 passed），前端依赖审计 0 vulnerabilities。
- [x] 全量非 live pytest（662 passed, 1 skipped）、前端 build、compileall、pip check 与差异检查通过。

### 任务 8：生产安全审查返修

**范围：**
- 凭证加密改为每次生成随机 salt 的版本化密文，并兼容历史固定 salt token。
- 生产环境拒绝默认数据库凭证、默认凭证主密钥和临时 JWT 密钥。
- SQL 副作用、表名幻觉、列引用、自动建库和枚举采样全部失败关闭或安全引用。
- Milvus 候选表达式转义后追加 metadata 精确过滤，删除与计数使用相同语义。
- 上传任务、外部动作审计和幂等记录有界；查询历史写入由工作流等待完成。
- 管理模型请求、MCP/会话错误、知识路径与错误响应、生产 OpenAPI 暴露完成收口。
- Connector URL、SQLite 方言、本地嵌入模型路径、首行空值图表和 cheap LLM Provider 修复。

- [x] 先编写随机盐、生产配置、SQL、Milvus、任务生命周期和故障可见性失败测试。
- [x] 根据探针日志定位各边界并实施最小修复。
- [x] 第一批安全测试通过（68 passed），第二批知识与执行测试通过（57 passed）。
- [x] 剩余 Critical/High/Medium 定向组合测试通过（93 passed）。
- [x] 运行全量非 live pytest（721 passed）、compileall 与差异检查。
