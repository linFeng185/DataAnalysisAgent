# 17. 基础设施与运维

## 17. 基础设施与运维 `[P0:4 P1:2 P2:7 P3:2]`

### 17.1 数据库存储

| # | 功能 | 表名 | 描述 | 状态 |
|---|------|------|------|------|
| 17.1.1 | checkpointer 表 | `checkpoints` / `checkpoint_writes` / `checkpoint_blobs` | AsyncPostgresSaver.setup() 自动创建，连接失败回退 MemorySaver | 开发完成 |
| 17.1.2 | 会话表 | `sessions` | session_id / title / datasource / tenant_id / user_id / created_at / last_active_at | 单测完成 |
| 17.1.3 | 会话归档表 | `sessions_archive` | 会话身份、元数据、摘要、归档时间及原子迁移 | 单测完成 |
| 17.1.4 | 长期记忆表 | `long_term_memories` | 三级可见性、身份、TTL、置信度、RLS 与向量补偿 | 单测完成 |
| 17.1.5 | 数据源配置表 | `datasource_configs` | 外挂模式数据源配置持久化 (name/dialect/host/port/database/username/encrypted_password) | 单测完成 |
| 17.1.6 | 查询审计日志表 | `query_audit_log` | tenant_id / user_id / datasource / sql_hash / row_count / duration_ms / success | 单测完成 |

### 17.2 数据库迁移

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 17.2.1 | 安全与租户迁移 | `migrations/001_batch1.sql` | 创建租户、用户、权限、审计、会话、历史和知识文件表，并启用身份 RLS | 单测完成 |
| 17.2.2 | 迁移工具 | `src/db/migrations.py` | 编号顺序、schema_migrations/checksum、advisory lock、单文件事务回滚和生产启动门禁 | 单测完成 |

### 17.3 监控与可观测性

| # | 功能 | 描述 | 状态 |
|---|------|------|------|
| 17.3.1 | LangSmith 全链路追踪 | 显式启停、项目/端点/采样配置，默认隐藏 Node 输入输出 | 单测完成 |
| 17.3.2 | Prometheus metrics | `/api/v1/metrics` 提供请求数、状态、延迟、LLM 调用和 token 消耗 | 集成测试完成 |
| 17.3.3 | Grafana Dashboard | 自动配置 Prometheus 数据源及健康、吞吐、错误、P95、token 面板 | 单测完成 |
| 17.3.4 | 结构化日志 | structlog JSON/console 输出，文件每日轮转并保留 7 天 | 单测完成 |
| 17.3.5 | Prompt/能力追踪元数据 | 统一 LLM 调用携带 Prompt ID/版本、任务、能力和数据源标签，输入输出默认隐藏 | 单测完成 |

### 17.4 容器化

| # | 功能 | 描述 | 状态 |
|---|------|------|------|
| 17.4.1 | Dockerfile | 后端 Python builder/runtime + 前端 Node/Nginx 多阶段镜像，非 root 后端运行 | 单测完成 |
| 17.4.2 | `docker-compose.example.yml` | 根目录前后端分离模板 + PostgreSQL 17 + Redis 7 + 嵌入式 Chroma；实际 Compose 与配置忽略 | 单测完成 |
| 17.4.3 | `.dockerignore` | 后端/前端分别排除虚拟环境、依赖、缓存、测试、密钥和运行数据 | 单测完成 |

### 17.5 启动与供应链安全

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 17.5.1 | 生产启动配置门禁 | `src/config.py`、`src/main.py` | prod 校验认证、随机盐凭证主密钥和状态库凭证，并关闭 API 元数据 | 单测完成 |

### 模块收尾

模块功能点共 17 项，已完成 17 项，待开发 0 项。

当前模块无待开发项。

---
