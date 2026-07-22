# 12. 安全模块

## 12. 安全模块 `[P0:9 P1:4 P2:2]`

### 12.1 SQL 安全

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 12.1.1 | DDL/DML AST 拦截 | `src/graph/nodes/layer3_validate.py` | 拒绝写/管理语句、SELECT INTO 和已知状态变更函数 | 单测完成 |
| 12.1.2 | 危险语句拦截 | 同上 | 拒绝 CALL / VACUUM / SET ROLE 及 EXPLAIN 包裹的写语句 | 单测完成 |
| 12.1.3 | 只读白名单模式 | 同上 | 默认只允许 SELECT / SHOW / DESCRIBE / EXPLAIN SELECT | 单测完成 |
| 12.1.4 | 只读数据库账号 | 各 Connector | 所有数据源连接使用只读账号（运维层面，代码已支持） | 开发完成 |
| 12.1.5 | SQL 注入防护 | 同上 | LLM 输出的 SQL 已结构化，不拼接用户输入 | 开发完成 |
| 12.1.6 | LLM 输出二次校验 | `src/graph/nodes/generate_sql.py` | sqlglot 提取表引用 → 比对 relevant_tables，拦截幻觉 | 开发完成 |

### 12.2 限流控制

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 12.2.1 | 单用户每小时查询上限 | `src/api/routes.py`、`src/security/data_masker.py` | 工作流前执行内存滑动窗口且每请求只计一次，主动回收过期用户 key（生产环境应切 Redis） | 单测完成 |
| 12.2.2 | 单次查询最大扫描行数 | `src/config.py` | 配置项 MAX_SCAN_ROWS (默认 1000 万) | 开发完成 |
| 12.2.3 | 单次查询最大执行时间 | `src/config.py` | 配置项 MAX_EXECUTION_TIME (默认 30 秒)，execute_sql 按方言 SET timeout | 开发完成 |
| 12.2.4 | 结果集最大返回行数 | `src/config.py` | 配置项 MAX_RESULT_ROWS (默认 10 万)，execute_sql 有界读取并报告截断 | 单测完成 |
| 12.2.5 | 公开注册接口限流 | `src/api/auth.py`、`src/config.py` | 按客户端地址限制注册频率，默认每小时 10 次 | 单测完成 | P0 |
| 12.2.6 | 登录接口限流 | `src/api/auth.py`、`src/config.py` | 按客户端地址与规范化用户名组合限制登录尝试 | 单测完成 | P0 |
| 12.2.7 | 请求与上传资源预算 | `src/api/routes.py`、`src/api/schemas.py` | 限制 query 字符、数据源数、上传文件数、单文件和累计字节 | 单测完成 | P0 |

### 12.3 数据安全

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 12.3.1 | 查询结果脱敏 | `src/security/data_masker.py` | 自动脱敏手机号(138****1234)、身份证号、邮箱 | 单测完成 |
| 12.3.2 | 敏感表/字段白名单 | 同上 | _SENSITIVE_COLS + 列名关键词匹配 | 单测完成 |
| 12.3.3 | 查询审计日志 | 同上 | 成功/失败均同步写 PG query_audit_log；SQL 与错误仅保存 hash 和执行摘要 | 单测完成 |

### 12.4 身份、租户与输入安全

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 12.4.1 | Cookie 认证与上下文清理 | `src/api/auth.py` | HttpOnly Cookie / Bearer 双入口，请求结束重置 ContextVar | 单测完成 |
| 12.4.2 | 租户资源隔离 | `src/memory/`、`src/knowledge/file_store.py` | Session、History、FileStore 显式过滤 tenant_id + user_id，并启用 PG RLS | 单测完成 |
| 12.4.3 | 生产启动安全校验 | `src/config.py`、`src/api/auth.py`、`src/main.py` | prod 拒绝匿名/弱密钥/默认 DB 凭证/临时 JWT，并关闭 Docs、Redoc、OpenAPI | 单测完成 |
| 12.4.4 | 上传与 Word XSS 防护 | `src/api/routes.py`、`src/knowledge/doc_parser.py` | 限制上传字节数并转义 Word 段落和单元格文本 | 单测完成 |
| 12.4.5 | 字段权限失败关闭 | `src/security/permission_check.py` | 列解析失败、SELECT * 绕过与行过滤解析失败均阻断 | 单测完成 |
| 12.4.6 | 平台与租户管理员分权 | `src/api/auth.py`、`src/knowledge/governance.py` | super_admin 管全局；tenant_admin 仅管理本租户知识 | 单测完成 | P0 |
| 12.4.7 | 数据源访问失败关闭 | `src/security/permission_check.py`、`src/api/routes.py` | 自动发现、显式选择、列表和多源执行统一按 tenant/user/role 授权 | 单测完成 | P0 |
| 12.4.8 | 受管 MCP RCE/SSRF 防护 | `src/api/routes.py`、`src/mcp_client/client_manager.py` | 禁止数据库 stdio 配置，SSE 主机必须在部署方 allowlist | 单测完成 | P0 |
| 12.4.9 | 凭证主密钥失败关闭 | `src/config.py`、`src/datasource/credential_manager.py` | 源码无固定默认密钥；生产缺少或弱主密钥拒绝启动 | 单测完成 | P0 |
| 12.4.10 | ClickHouse 出站 SSRF 防护 | `src/security/network.py`、`src/connectors/clickhouse.py` | 私网默认拒绝，部署 allowlist 放行；探针与客户端固定使用已校验 IP | 单测完成 | P0 |
| 12.4.11 | API 浏览器安全中间件 | `src/api/security_headers.py`、`src/main.py` | CSP/HSTS/防嵌入/nosniff 与显式 CORS allowlist | 单测完成 | P0 |
| 12.4.12 | 纯 ASGI 身份上下文 | `src/api/auth.py` | SSE 最后分块发送前保持 JWT 身份，请求结束精确清理 ContextVar | 单测完成 | P0 |

### 模块收尾

模块功能点共 28 项，已完成 28 项，待开发 0 项。

---
