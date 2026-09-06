# 平台动态配置、连接器热插拔与双语界面

落实本轮六项用户需求；现有功能不重新开发，新增能力登记于 features/25-runtime-platform-and-ui.md。

## 动态配置
- 环境变量/YAML 作为启动默认值；数据库平台覆盖优先。超管在平台管理中修改可热更新的标量配置，重置恢复启动默认值。
- 配置保存以全量 Settings 验证、版本号乐观锁、数据库事务和审计为边界；多 Worker 每两秒刷新，失败保留最后有效快照并记录异常。
- 额度、SQL/上传预算、上下文压缩、重试等请求级配置即时应用于后续请求。在途请求不强制改写。
- 主数据库、密钥、认证模式、网络监听、存储后端、进程级后台启停等启动必需项保留环境配置，界面明确只读；不暴露密码或完整连接串，不声称这些字段可热改。
- GET/PUT /admin/runtime-settings 使用 {revision,values}；恢复字段删除其覆盖，而非清空环境变量。

## 连接器插件
- 内置连接器和通过 Python entry point `data_analysis_agent.connectors` 安装的可信插件使用统一目录。插件提供 ConnectorBase 子类、SQLGlot 方言和异步 introspect_schema。
- 平台可发现、启用、停用插件，禁用状态持久化并跨 Worker 刷新；移除发行包后重新发现即移出目录。禁止通过 HTTP 上传或执行任意 Python、在线 pip 安装或卸载正在运行的驱动。
- 驱动依赖须由部署环境预安装。运行时热插拔指无需重启完成发现/注册/撤销，新查询不再进入停用插件，在途查询自然结束，关闭连接仍允许。
- 数据源表单使用插件目录；指标的数据源来自当前租户可见列表，方言由服务端数据源记录权威确定，不信任客户端任意方言。

## 应急授权选择
- GET /knowledge/emergency-grants/recipients?query= 提供可接受应急授权的平台账号，返回有限的 id/username 展示信息，最多 20 条。
- 前端使用可搜索下拉框，防抖并忽略旧请求响应；不再让用户手填数字 ID。

## 国际化
- 界面默认 zh-CN，支持 en-US；右上角切换并在本地保存选择，同步 Antd 和日期控件语言。
- 翻译静态界面文案，不翻译用户数据、SQL、代码、连接配置、知识原文或模型响应；不把界面语言切换冒充自然语言分析翻译功能（19.6）。
- 使用集中词典和 React 订阅更新，不直接改写 DOM。

## 表结构同步
- “刷新 Schema”统一更名“同步表结构”。每个租户/数据源只有一个活动同步任务。
- 新增启动、状态和终止 API；数据库记录 running/cancelling/succeeded/cancelled/failed。事务级租户/数据源互斥、心跳及 Worker 所有权防重复执行；旧 /schema/refresh 路径兼容同步完成响应。
- 终止请求先标记 cancelling，由拥有任务的 Worker 取消任务，完成清理后才显示 cancelled；不得在取消请求发出时立即释放互斥。
- 已完成的表结构缓存保持可用。取消只针对当前同步任务，不关闭其他请求共享的数据库连接池。

## 验收与部署
新增迁移不修改历史 checksum，不连接用户业务数据库。单元测试使用 Fake/Mock，浏览器拦截真实业务请求；真实多 Worker/PostgreSQL/外部插件环境另列部署验收。

## 接口与实现补充

下列路径统一加 `/api/v1` 前缀，复用已有登录认证。运行时配置及插件管理仅允许平台超管；同步写操作复用租户管理权限和数据源 ACL；平台账号搜索仅返回可接受应急授权的启用平台身份。

| 接口 | 请求示例 | 响应或边界 |
|---|---|---|
| `GET /admin/runtime-settings` | 无请求体 | `{ "revision": 4, "fields": [{ "key": "max_queries_per_hour", "value": 100, "default": 100, "overridden": false, "apply_mode": "live", "schema": { "type": "integer" } }], "read_only": ["database_url", "jwt_secret"] }`；示例仅展示目录子集，只读项不暴露值 |
| `PUT /admin/runtime-settings` | `{ "revision": 4, "values": { "max_queries_per_hour": 120 } }` | 新目录及递增版本；旧版本 `409`、非法字段/范围 `422`；设置字段为 `null` 恢复启动默认值 |
| `GET /knowledge/emergency-grants/recipients?query=admin` | 查询串 | `{ "items": [{ "id": 1, "username": "admin" }] }`；最多 20 条，不返回凭证 |
| `GET /datasource-types` | 无请求体 | `{ "items": [{ "dialect": "postgres", "label": "postgres", "sqlglot_dialect": "postgres", "enabled": true, "external": false, "fields": ["host", "port", "database", "schema", "username", "password"], "error": "" }] }` |
| `GET /admin/connector-plugins` | 无请求体 | 相同目录结构，包含停用和加载失败的插件 |
| `POST /admin/connector-plugins/discover` | `{}` | 刷新目录并推进共享版本，使其他 Worker 重新发现 |
| `PUT /admin/connector-plugins/postgres` | `{ "enabled": false, "reason": "维护窗口暂停新查询" }` | 保存插件策略并审计，新调用被拒绝；已开始的查询可以完成 |
| `POST /schema/sync?datasource=sales` | 无请求体 | `202` 与任务状态；重复同步 `409`，Worker 并发预算不足 `429` |
| `GET /schema/sync?datasource=sales` | 无请求体 | `{ "datasource": "sales", "status": "running", "phase": "collecting", "run_id": "任务 UUID", "table_count": 0, "error": "" }`，实际还包含时间戳 |
| `POST /schema/sync/cancel?datasource=sales` | 无请求体 | 返回 `cancelling`，真实清理完后才转 `cancelled`；发布中 `409` |
| `POST /schema/refresh?datasource=sales` | 无请求体 | 兼容入口等待相同任务机制，成功返回 `status=ok`，不绕过共享互斥 |

### 运行时与插件边界

- `src/runtime/settings.py::LIVE_FIELDS` 是唯一可热改白名单，`describe()` 向前端暴露实际可改范围。密钥、认证模式、元数据库、文件目录、存储后端和后台任务初始化配置仍为启动项，不能通过任意 JSON 绕过。
- 配置修改保留环境/YAML 默认值，不重写 `.env`。两秒是健康服务下的轮询间隔，并非数据库故障时的生效保证；故障保留最后有效快照并持续重试。
- Python 发行包声明 `[project.entry-points."data_analysis_agent.connectors"]`，入口名作为数据源 `dialect`，入口值指向 `ConnectorBase` 的具体子类。禁止覆盖内置方言。
- 插件必须声明 `sqlglot_dialect`，提供 `async introspect_schema(self, *, executor, table_names=None)` 返回 `SchemaSnapshot`；可声明 `display_name`、`connection_fields`。普通扩展连接字段保存在 `extra_params`，不得把密钥类字段当作普通扩展参数明文托管。
- 运行时发现、启停与撤销不重启服务；驱动依赖需先由部署侧在全部 Worker 安装。替换已加载的 Python 模块或原生驱动版本不承诺在线卸载，应滚动部署，不能把“发现插件”当作安装器。

### 同步发布边界

- `schema_sync_jobs` 保存任务与最后成功快照。拥有者持有会话级 advisory lock 覆盖内省、取消清理和发布全生命周期；状态查询使用事务级锁确认拥有者已退出后才回收失败任务，不单凭心跳超时抢占。
- 取消信号与进入发布的条件更新互斥；清理期间状态保持 `cancelling`。同步驱动放入工作线程，取消需要等待底层调用真正退出，不承诺毫秒级终止，也不关闭共享连接池。
- PostgreSQL 规范快照先提交，向量/连接缓存是可重建的派生索引。周期任务复用同步服务，成功发布用 `refresh_from_snapshot()` 清理旧自动条目，不再独立运行一次数据库内省。
- `SchemaManager` 命中规范快照后仍合并文档备注，仅作用于当前请求，不把文档覆盖写回共享规范快照；数据源连接指纹变化后不复用旧快照。
- 首次部署需应用 `025_runtime_controls.sql`、`026_schema_sync.sql`；现有启动迁移器负责顺序执行。迁移、真实多 Worker 和真实外部驱动验收情况记录在 `features/25-runtime-platform-and-ui.md`。
