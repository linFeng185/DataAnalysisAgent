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

## 2026-09-07 数据源管理补充契约

本节按用户确认的应用内管理要求替代旧 §3.1 中的 YAML/环境变量数据源发现方案。

- 业务数据源只能由应用管理 API 创建或导入，启动仅从 PostgreSQL `datasource_configs` 恢复。不读取 `config/datasources.yaml`、`DATASOURCE_NAME`、`DATASOURCE_0_*` 等变量，不再自动注册演示库或扫描 ORM 连接。
- 保留 `DATABASE_URL`、密钥托管、数据源缓存和网络安全策略等应用基础设施配置；它们不是业务数据源定义。本地忽略的旧配置文件不主动删除，但不再生效。
- `GET /api/v1/datasources/export` 只允许租户管理身份，导出当前租户配置，不带密码、归属/权限、同步状态或运行时对象。插件扩展参数递归剔除凭证字段和连接 URL；导入后须补充敏感连接信息。可选重复查询参数 `names` 指定导出范围：不传或为空即导出全部（保持既有行为）；传入时先去重、再按 Provider 顺序过滤，其中任一名称在当前租户内不存在则返回 `404` 且不产生文件。「不存在」与「属于其他租户」返回一致，不泄露跨租户信息。文件名固定 `datasources.json`，不因子集而改名。导出始终由服务端生成，不在前端拼装，避免把敏感字段判定权交给客户端。
- `POST /api/v1/datasources/import` 接受 multipart 的 `file` JSON 文件，最大 1 MiB、1—100 条配置，固定 `version=1`。逐条按当前启用的连接器插件校验，拒绝未知字段、无效端口、重复名称和来自文件的归属覆盖。
- 导入为仅新增操作：全部配置及权限在单个状态库事务提交后才进入 Provider。文件内名称重复仍整批拒绝；与当前租户已有名称冲突时，自动追加 `_` 加 6 位 base36 随机后缀（如 `sales_7f3a2b`）后重试，至多 3 次仍冲突才返回 `409`。不覆盖、不产生部分导入；格式错误返回 `422`，超限 `413`，持久化错误 `500`。不在导入过程中访问业务数据库。采用随机后缀而非递增序号：序号需先查询现有名称求最大值，并发导入会选中同一序号；随机后缀配合唯一约束与有界重试在并发下才成立。
- 改名映射必须随导入响应回传并在界面提示。若只报告导入条数，用户无法定位被改名后的数据源，等同于静默丢失。
- 数据源列表按 `updated_at` 倒序返回并透出该字段：新增由数据库默认 `NOW()` 填充，编辑时刷新。`updated_at` 属运行时元数据，与同步状态一样不进入导出文件。
- 数据源页提供导入、导出、同步表结构和终止按钮，并轮询列表中的 `schema_sync` 共享状态。导出支持单选与多选：勾选一个或多个即导出所选，一个都不勾时导出全部并保持按钮文案不变；导出完成后清空勾选，避免下一轮误导出上一轮的子集。运行中和终止中不能重复同步，发布阶段不允许取消；状态不可读显示 `unknown`，而非假装空闲。表结构菜单仅供浏览，不保留同步或字段编辑按钮；`GET /schema/tables?cached_only=true` 只读取已发布快照，不创建业务连接或触发内省。旧 API 不传该参数时保留延迟加载兼容。
- 前端轮询必须有不依赖用户操作即可达的停止条件：仅当存在进行中的任务时续期，任务全部进入终态即停止。禁止用「组件是否挂载」这类恒真条件续期，也不得只判断集合非空而忽略成员已从响应中消失的情况。数据源页空闲时仍每 1.5 秒请求列表、知识上传页在任务从响应中消失后永不停止，都属于此类缺陷。
- 普通 JSON API 及配置上传下载提供 30 秒网络和响应读取超时；认证探测及登录为 10 秒，聊天流不套用该总超时。认证区分已认证、明确未认证与暂时无法验证：等待有可见状态，超时/网络错误/5xx 留在原路由展示重试，不清理 Cookie 或把已有身份设为未登录；只有明确的 401 或未认证响应才进入登录页。重试和刷新都重新探测，不永久复用失败请求。

JSON 文件示例（默认不含凭证）：

```json
{"version":1,"datasources":[{"name":"sales","dialect":"postgres","host":"db.internal","port":5432,"database":"sales","username":"reader","extra_params":{"schema":"public"}}]}
```

导入响应示例：`201 {"imported":["sales_7f3a2b"],"renamed":[{"from":"sales","to":"sales_7f3a2b"}]}`（无改名时 `renamed` 为空数组）；导出示例：`GET /api/v1/datasources/export?names=sales&names=orders` 仅返回这两个数据源，未知名称返回 `404`。同步列表示例：`{"datasources":[{"name":"sales","schema_sync":{"status":"running","phase":"collecting","table_count":0}}],"total":1}`。

验收使用事务替身、真实 HTTP 路由及隔离浏览器；不连接用户业务数据库，不把替身事务验证描述为真实 PostgreSQL 多 Worker 部署验收。

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
- Milvus 的集合加载、查询/迭代、写入/flush、计数和健康探针统一在线程执行，RPC 显式传入 10 秒超时、查询迭代设置 30 秒预算；重量级嵌入库的导入和模型构造也不在事件循环执行，并发首次请求合并初始化。
- `Collection.load()` 只在未确认加载时执行：确认加载后复用本地标记，不再每次操作重发重量级 `LoadCollection`。加载失败立即进入 60 秒熔断窗口，窗口内直接抛 `MilvusCollectionUnavailable` 而不发起 RPC，避免集合不可用时每个请求各等满一次 10 秒超时；熔断期内健康检查直接判不健康。操作返回「集合未加载」类错误时失效本地标记，使服务端驱逐或人工 release 后能自行重建，不被本地假象永久锁死。
- 取消不能使后台同步写入脱离互斥：等待当前工作线程实际退出后再释放写锁。派生索引刷新另有 30 秒取消预算，超时记录失败并完成必要清理后将已发布规范快照的任务收束为成功，不保证中断同步驱动或模型导入的瞬间即返回。
- `SchemaManager` 命中规范快照后仍合并文档备注，仅作用于当前请求，不把文档覆盖写回共享规范快照；数据源连接指纹变化后不复用旧快照。
- 首次部署需应用 `025_runtime_controls.sql`、`026_schema_sync.sql`；现有启动迁移器负责顺序执行。迁移、真实多 Worker 和真实外部驱动验收情况记录在 `features/25-runtime-platform-and-ui.md`。

## 日志语义与频率约束

- 在调用点判断日志价值，不按模块屏蔽 `INFO`。启动/关闭、实际持久化、外部调用结果和关键状态变更保留有运维价值的摘要；安全阻断、回退和异常不因降噪被过滤，异常在处理边界保留堆栈。
- 数据源列表、授权成功、缓存命中、指纹计算、SQL/提示词拼装、Skill 参数规范化及图节点机械开始/完成使用 `DEBUG`。ClickHouse 逐行属性读取和结果适配删除日志，避免日志量随结果行数增长。
- 周期缓存检查没有变化、没有过期数据或其他 Worker 正常持锁时只记 `DEBUG`；实际清理和变更保留聚合 `INFO`。每个 HTTP 请求保留现有访问中间件摘要，不另开 Uvicorn 重复访问日志。
- 前端正常渲染、表单操作和请求成功不写控制台日志；保留异常提示及必要的 `warn`/`error`，普通表单必填校验不当作系统异常打印。
- 默认沿用 `LOG_LEVEL=INFO`，需要排障时临时改为 `DEBUG`。structlog 在脱敏、序列化和渲染前按级别过滤，避免未输出的调试明细仍消耗处理资源。禁止为降噪引入整模块过滤开关。
- 敏感字段及异常堆栈脱敏保持有效，日志文件继续通过 `TimedRotatingFileHandler` 按日轮换、`backupCount=7` 保留七天。测试同时验证控制台与文件输出，不以丢弃异常实现降噪。
- 诊断优先复用已有日志、网络记录、进程堆栈和测试捕获，只对缺失证据添加临时 DEBUG。项目规则和调试 skill 不得强制逐层 INFO，修复收尾检查临时探针；不得把 Mock 测试通过描述为真实故障路径验收。
