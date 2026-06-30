# 17. 基础设施与运维

## 17. 基础设施与运维 `[P0:4 P1:2 P2:7 P3:2]`

### 17.1 数据库存储

| # | 功能 | 表名 | 描述 | 状态 |
|---|------|------|------|------|
| 17.1.1 | checkpointer 表 | `checkpoints` / `checkpoint_writes` / `checkpoint_blobs` | LangGraph PostgresSaver 自动创建 | 待开发 |
| 17.1.2 | 会话表 | `active_sessions` | session_id / thread_id / user_id / created_at / last_active_at | 待开发 |
| 17.1.3 | 会话归档表 | `sessions_archive` | thread_id / summary / archived_at | 待开发 |
| 17.1.4 | 长期记忆表 | `long_term_memories` | id / memory_type / scope / content / payload / created_at / last_accessed_at / access_count / confidence | 待开发 |
| 17.1.5 | 数据源配置表 | `datasource_configs` | 外挂模式数据源配置持久化 (name/dialect/host/port/database/username/encrypted_password) | 待开发 |
| 17.1.6 | 查询审计日志表 | `query_audit_log` | user_id / datasource / sql / row_count / execution_time_ms / created_at | 待开发 |

### 17.2 数据库迁移

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 17.2.1 | 初始迁移 | `migrations/001_initial.py` | 创建所有业务表 (sessions / long_term_memories / datasource_configs / audit_log) | 待开发 |
| 17.2.2 | 迁移工具 | `src/db/migrations.py` | Alembic 或自研轻量迁移执行器 | 待开发 |

### 17.3 监控与可观测性

| # | 功能 | 描述 | 状态 |
|---|------|------|------|
| 17.3.1 | LangSmith 全链路追踪 | 每个 Node 的输入/输出/延迟/LLM token 自动上报 | 待开发 |
| 17.3.2 | Prometheus metrics | 请求数 / 错误率 / P50/P95/P99 延迟 / LLM token 消耗 | 待开发 |
| 17.3.3 | Grafana Dashboard | 服务健康 / 查询性能 / 错误趋势 / 成本追踪 | 待开发 |
| 17.3.4 | 结构化日志 | structlog JSON 格式输出，支持 ELK / Loki 采集 | 待开发 |

### 17.4 容器化

| # | 功能 | 描述 | 状态 |
|---|------|------|------|
| 17.4.1 | Dockerfile | 多阶段构建 (builder + runtime) | 待开发 |
| 17.4.2 | docker-compose.yml | PostgreSQL 17 + ChromaDB + Redis 7 + App 开发环境编排 | 待开发 |
| 17.4.3 | .dockerignore | 排除 .venv / __pycache__ / .git / tests / .claude | 待开发 |

---
