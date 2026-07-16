# 缺陷修复设计

## 目标

修复当前项目审计发现的安全、正确性、API 生命周期和可运维性缺陷，同时保持
`dev/test + MULTI_TENANT=false` 的匿名演示能力。生产环境必须在启动阶段拒绝不完整的
认证、凭证和数据库安全配置。

## 兼容边界

| 环境 | 认证行为 | 说明 |
|------|----------|------|
| `dev` / `test` 且 `MULTI_TENANT=false` | 匿名可用 | 仅用于本地演示和测试 |
| 任意环境 `MULTI_TENANT=true` | JWT 必须有效 | 所有租户资源按用户和租户隔离 |
| `prod` | JWT、`JWT_SECRET`、凭证密钥、只读数据库配置必须存在 | 配置缺失时启动失败 |

## 安全设计

1. JWT 通过 HttpOnly Cookie 传输，前端请求使用 `credentials: include`，不再把令牌写入
   `localStorage`。认证中间件在请求结束时重置 `ContextVar`。
2. PostgreSQL 会话、历史、知识文件和审计记录包含 `tenant_id` / `user_id`，应用查询使用
   显式过滤；连接层设置并在归还连接前重置 RLS 参数。
3. SQL 校验采用 sqlglot AST 只读白名单，仅允许 `SELECT`、`SHOW`、`DESCRIBE`、`EXPLAIN`。
   解析失败、列权限校验失败和行过滤注入失败均拒绝执行。
4. 查询结果在进入分析和响应前统一脱敏；执行层按 `MAX_RESULT_ROWS` 有界读取，审计日志
   保存 SQL hash，不保存明文 SQL。
5. Word 内容转 HTML 时对文本进行转义，上传文件限制大小；默认 MCP 配置不执行未锁版本
   的远程 `npx` 包。

## 核心正确性

1. 修正 SQLite 方言的 Schema 内省路由。
2. 修正 LLM 分析路径的长度判断和处理器输出契约，始终返回 `statistics`。
3. 无 LLM 时仅提供可证明的确定性回退（例如数量查询使用 `COUNT(*)`）；无法确定语义时
   返回明确错误，不执行固定的 `SELECT *`。
4. 数据源注册使用全局 Provider/Registry，删除时释放引擎并清除缓存；Schema 刷新和字段
   备注执行真实的缓存/元数据更新。
5. 健康检查区分应用存活与依赖可用状态，避免依赖不可用时返回假健康。

## 运维设计

1. 所有数据库密码来自环境变量，Docker Compose 不保存明文密码。
2. 日志使用 `TimedRotatingFileHandler`，每天轮转并保留 7 天，同时保留控制台输出。
3. 生产配置验证在 `create_app`/启动生命周期中执行，测试环境可通过显式配置关闭外部依赖。

## 测试与验收

先为每个修复增加回归测试并确认测试在旧实现上失败，再实现最小修复。覆盖：

- SQL 非只读语句、解析失败和权限失败关闭；
- Cookie 认证、生产配置拒绝启动、匿名开发模式；
- 会话/历史/知识文件租户过滤和 ContextVar 清理；
- 结果上限、PII 脱敏、审计字段；
- LLM 回退、统计字段、SQLite 内省；
- 数据源注册/删除/刷新真实状态；
- Word/XSS 文本转义和上传大小限制。

验收命令：

```text
.venv\\Scripts\\python.exe -m pytest -q
npm run build
python -m compileall -q src tests
```

另外使用隔离的测试配置执行 `/health`、`/chat`、数据源注册/列表和认证 curl 回归，确认
没有外部数据库或远程 MCP 依赖时仍能完成本地验证。
