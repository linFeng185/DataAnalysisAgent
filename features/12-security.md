# 12. 安全模块

## 12. 安全模块 `[P0:8 P1:4 P2:2]`

### 12.1 SQL 安全

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 12.1.1 | DDL/DML AST 拦截 | `src/graph/nodes/layer3_validate.py` | 解析语句树并拒绝所有写操作和管理语句 | 单测完成 |
| 12.1.2 | 危险语句拦截 | 同上 | 拒绝 CALL / VACUUM / SET ROLE 及 EXPLAIN 包裹的写语句 | 单测完成 |
| 12.1.3 | 只读白名单模式 | 同上 | 默认只允许 SELECT / SHOW / DESCRIBE / EXPLAIN SELECT | 单测完成 |
| 12.1.4 | 只读数据库账号 | 各 Connector | 所有数据源连接使用只读账号（运维层面，代码已支持） | 开发完成 |
| 12.1.5 | SQL 注入防护 | 同上 | LLM 输出的 SQL 已结构化，不拼接用户输入 | 开发完成 |
| 12.1.6 | LLM 输出二次校验 | `src/graph/nodes/generate_sql.py` | sqlglot 提取表引用 → 比对 relevant_tables，拦截幻觉 | 开发完成 |

### 12.2 限流控制

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 12.2.1 | 单用户每小时查询上限 | `src/security/data_masker.py` | 内存滑动窗口限流 (生产环境应切 Redis) | 开发完成 |
| 12.2.2 | 单次查询最大扫描行数 | `src/config.py` | 配置项 MAX_SCAN_ROWS (默认 1000 万) | 开发完成 |
| 12.2.3 | 单次查询最大执行时间 | `src/config.py` | 配置项 MAX_EXECUTION_TIME (默认 30 秒)，execute_sql 按方言 SET timeout | 开发完成 |
| 12.2.4 | 结果集最大返回行数 | `src/config.py` | 配置项 MAX_RESULT_ROWS (默认 10 万)，execute_sql 有界读取并报告截断 | 单测完成 |

### 12.3 数据安全

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 12.3.1 | 查询结果脱敏 | `src/security/data_masker.py` | 自动脱敏手机号(138****1234)、身份证号、邮箱 | 单测完成 |
| 12.3.2 | 敏感表/字段白名单 | 同上 | _SENSITIVE_COLS + 列名关键词匹配 | 单测完成 |
| 12.3.3 | 查询审计日志 | 同上 | structlog + PG query_audit_log，仅保存 SQL hash 和执行摘要 | 单测完成 |

### 12.4 身份、租户与输入安全

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 12.4.1 | Cookie 认证与上下文清理 | `src/api/auth.py` | HttpOnly Cookie / Bearer 双入口，请求结束重置 ContextVar | 单测完成 |
| 12.4.2 | 租户资源隔离 | `src/memory/`、`src/knowledge/file_store.py` | Session、History、FileStore 显式过滤 tenant_id + user_id，并启用 PG RLS | 单测完成 |
| 12.4.3 | 生产启动安全校验 | `src/config.py` | prod 拒绝匿名模式、弱密钥、缺失凭证密钥和只读数据库配置 | 单测完成 |
| 12.4.4 | 上传与 Word XSS 防护 | `src/api/routes.py`、`src/knowledge/doc_parser.py` | 限制上传字节数并转义 Word 段落和单元格文本 | 单测完成 |
| 12.4.5 | 字段权限失败关闭 | `src/security/permission_check.py` | 列解析失败、SELECT * 绕过与行过滤解析失败均阻断 | 单测完成 |
| 12.4.6 | 平台与租户管理员分权 | `src/api/auth.py`、`src/knowledge/governance.py` | super_admin 管全局；tenant_admin 仅管理本租户知识 | 单测完成 | P0 |

### 模块收尾

模块功能点共 19 项，已完成 19 项，待开发 0 项。

---
