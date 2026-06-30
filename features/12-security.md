# 12. 安全模块

## 12. 安全模块 `[P0:8 P1:4 P2:2]`

### 12.1 SQL 安全

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 12.1.1 | DDL/DML 正则黑名单 | `src/graph/nodes/layer3_validate.py` | 拦截 INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/TRUNCATE/RENAME/GRANT/REVOKE/MERGE/REPLACE | 开发完成 |
| 12.1.2 | 危险函数拦截 | 同上 | 拦截 sleep() / benchmark() / 存储过程调用 | 开发完成 |
| 12.1.3 | 白名单模式 | 同上 | 默认只允许 SELECT / SHOW / DESCRIBE / EXPLAIN | 开发完成 |
| 12.1.4 | 只读数据库账号 | 各 Connector | 所有数据源连接使用只读账号（运维层面，代码已支持） | 开发完成 |
| 12.1.5 | SQL 注入防护 | 同上 | LLM 输出的 SQL 已结构化，不拼接用户输入 | 开发完成 |
| 12.1.6 | LLM 输出二次校验 | `src/graph/nodes/generate_sql.py` | sqlglot 提取表引用 → 比对 relevant_tables，拦截幻觉 | 开发完成 |

### 12.2 限流控制

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 12.2.1 | 单用户每小时查询上限 | `src/security/data_masker.py` | 内存滑动窗口限流 (生产环境应切 Redis) | 开发完成 |
| 12.2.2 | 单次查询最大扫描行数 | `src/config.py` | 配置项 MAX_SCAN_ROWS (默认 1000 万) | 开发完成 |
| 12.2.3 | 单次查询最大执行时间 | `src/config.py` | 配置项 MAX_EXECUTION_TIME (默认 30 秒)，execute_sql 按方言 SET timeout | 开发完成 |
| 12.2.4 | 结果集最大返回行数 | `src/config.py` | 配置项 MAX_RESULT_ROWS (默认 10 万)，execute_sql 限制 200 行 | 开发完成 |

### 12.3 数据安全

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 12.3.1 | 查询结果脱敏 | `src/security/data_masker.py` | 自动脱敏手机号(138****1234)、身份证号、邮箱 | 开发完成 |
| 12.3.2 | 敏感表/字段白名单 | 同上 | _SENSITIVE_COLS + 列名关键词匹配 | 开发完成 |
| 12.3.3 | 查询审计日志 | 同上 | structlog 记录 + PG query_audit_log 表写入 | 开发完成 |

---
