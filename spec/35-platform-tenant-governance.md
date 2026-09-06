# 平台与租户治理收敛

本设计落实已批准的 G24.1—G24.5，依赖既有认证、PostgreSQL、租户 LLM 连接及报表模块。不引入新服务，不开发联网搜索。

## 身份边界

- `super_admin` 是平台操作身份，不是租户业务身份。报表能力始终为空，存量覆盖不能恢复授权；存量报表保留但不自动转移给任何租户。
- 后台无请求身份使用 `tenant_id=0,user_id=0` 的系统连接；报表实际执行继续恢复业务拥有者。数据库后台例外不得接受用户 JWT 直接触发。
- 知识 system 管理归平台，tenant 管理归同租户管理员，private 管理归拥有者。普通检索不接收应急授权。

## 查询额度

- `max_queries_per_hour` 保持平台每用户硬上限，同时作为租户聚合硬上限；租户可以配置 `tenant_per_hour` 和 `user_per_hour`，满足 `0 <= user_per_hour <= tenant_per_hour <= hard_limit`。零表示停用查询。
- 使用数据库 UTC 自然小时窗口；POST 聊天、流式聊天及报表实时钻取在执行前原子占用一次。SQL 节点非 API 调用复用相同服务；入口已经检查时不重复扣减。
- PostgreSQL 事务锁定租户计数，再同时更新租户与用户计数；拒绝请求不扣次数。数据库失败返回 503，禁止回退进程内放行。
- `GET/PUT /api/v1/admin/query-quota` 仅当前租户管理员可调用；请求示例 `{"tenant_per_hour":60,"user_per_hour":20}`，响应包含硬上限、软额度与窗口说明。无权 403，非法额度 422，超限 429。

## 知识应急读取

- 授权对象是一个 PostgreSQL 知识文件 ID；tenant 文件由同租户管理员授权，private 文件只能由拥有者授权。授权指定在用超管用户、理由和最长 60 分钟有效期，可提前撤销。
- `POST /api/v1/knowledge/emergency-grants` 请求示例 `{"file_id":12,"grantee_user_id":1,"reason":"排查工单 ABC","minutes":15}`；返回授权 ID 和到期时间。
- `DELETE /api/v1/knowledge/emergency-grants/{id}` 由原授权者撤销。
- `GET /api/v1/knowledge/emergency-grants/{id}/content` 仅指定超管可读，事务内锁定授权、检查当前文件归属和授权者身份、写入读取审计后返回文件；过期/撤销/越权均不返回正文。授权不扩散至检索、标签、向量库或其他文件。
- 授权、撤销和读取使用数据库审计记录，不记录文件正文或凭证。迁移删除级联仅用于租户生命周期。

## MCP 安全停用

- 平台仅日常管理 system；租户管理员管理 tenant，私有连接仍遵守拥有者边界及受管 SSE/主机白名单约束。
- `POST /api/v1/mcp/security-blocks` 仅超管可设置指定租户/范围/名称的安全阻断，必须给出原因；不读取或更改租户连接 URL、凭证、enabled。
- 每次受管工具调用和连接测试重新读取共享阻断状态与当前可见连接，避免缓存跨 Worker 绕过。阻断独立持久化，删除并同名重建不能绕过。
- 解除安全阻断不启用租户连接；租户仍决定日常 enabled。平台操作写入独立审计。

## 旧模型入口

- `POST /api/v1/models/test` 保留路径并返回 410 与迁移提示，不再创建 Provider 或调用全局凭证。
- 唯一租户测试入口为 `/api/v1/admin/llm/connections/{connection_id}/test`，沿用现有租户归属校验，不重复实现。

## 验收

权限矩阵、存量覆盖、跨租户/私有边界、额度并发/零值/异常、应急过期撤销和安全阻断均使用确定性测试。真实 PostgreSQL 多 Worker/RLS 验收单独报告，不以 Mock 结果冒充生产验收。
