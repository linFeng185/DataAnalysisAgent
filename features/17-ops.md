# 17. 基础设施与运维

## 17. 基础设施与运维 `[P0:4 P1:2 P2:7 P3:2]`

### 17.1 数据库存储

| # | 功能 | 表名 | 描述 | 状态 |
|---|------|------|------|------|
| 17.1.1 | checkpointer 表 | `checkpoints` / `checkpoint_writes` / `checkpoint_blobs` | AsyncPostgresSaver.setup() 自动创建，连接失败回退 MemorySaver | 开发完成 |
| 17.1.2 | 会话表 | `sessions` | session_id / title / datasource / tenant_id / user_id / created_at / last_active_at | 单测完成 |
| 17.1.3 | 会话归档表 | `sessions_archive` | thread_id / summary / archived_at | 待开发 |
| 17.1.4 | 长期记忆表 | `long_term_memories` | id / memory_type / scope / content / payload / created_at / last_accessed_at / access_count / confidence | 待开发 |
| 17.1.5 | 数据源配置表 | `datasource_configs` | 外挂模式数据源配置持久化 (name/dialect/host/port/database/username/encrypted_password) | 待开发 |
| 17.1.6 | 查询审计日志表 | `query_audit_log` | tenant_id / user_id / datasource / sql_hash / row_count / duration_ms / success | 单测完成 |

### 17.2 数据库迁移

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 17.2.1 | 安全与租户迁移 | `migrations/001_batch1.sql` | 创建租户、用户、权限、审计、会话、历史和知识文件表，并启用身份 RLS | 单测完成 |
| 17.2.2 | 迁移工具 | `src/db/migrations.py` | 编号顺序、schema_migrations/checksum、advisory lock、单文件事务回滚和生产启动门禁 | 单测完成 |

### 17.3 监控与可观测性

| # | 功能 | 描述 | 状态 |
|---|------|------|------|
| 17.3.1 | LangSmith 全链路追踪 | 每个 Node 的输入/输出/延迟/LLM token 自动上报 | 待开发 |
| 17.3.2 | Prometheus metrics | 请求数 / 错误率 / P50/P95/P99 延迟 / LLM token 消耗 | 待开发 |
| 17.3.3 | Grafana Dashboard | 服务健康 / 查询性能 / 错误趋势 / 成本追踪 | 待开发 |
| 17.3.4 | 结构化日志 | structlog JSON/console 输出，文件每日轮转并保留 7 天 | 单测完成 |

### 17.4 容器化

| # | 功能 | 描述 | 状态 |
|---|------|------|------|
| 17.4.1 | Dockerfile | 多阶段构建 (builder + runtime) | 待开发 |
| 17.4.2 | docker-compose.yml | PostgreSQL 17 + ChromaDB + Redis 7 + App 开发环境编排 | 待开发 |
| 17.4.3 | .dockerignore | 排除 .venv / __pycache__ / .git / tests / .claude | 待开发 |

### 17.5 启动与供应链安全

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 17.5.1 | 生产启动配置门禁 | `src/config.py`、`src/main.py` | prod 校验认证、随机盐凭证主密钥、默认 DB 凭证和只读连接，并关闭 API 元数据 | 单测完成 |

### 模块收尾

模块功能点共 16 项，已完成 7 项，待开发 9 项。

| 功能点 | 不开发原因 | 可开发条件 | 预计开发时机 |
|--------|------------|------------|--------------|
| 17.1.3 会话归档表 | 迁移尚未创建 sessions_archive，现有维护代码仍引用旧 active_sessions 表，不能标记完成 | 新增幂等迁移，将归档查询改为 sessions，并补 PG 集成测试 | Phase 3，持久化迁移专项 |
| 17.1.4 长期记忆表 | LongTermMemoryStore 已有读写代码，但迁移和双写集成测试缺失 | 创建 long_term_memories / pending_vector_sync 表并验证 PG + VectorStore 补偿 | Phase 3，长期记忆持久化专项 |
| 17.1.5 数据源配置表 | 当前动态数据源仅驻留全局 Provider，服务重启后不会恢复 | 明确凭证加密字段和租户唯一键，增加持久化 Provider 与迁移 | Phase 3，多实例部署前 |
| 17.3.1 LangSmith 全链路追踪 | 仅提供环境变量示例，未验证 Node 追踪和敏感字段裁剪 | 配置 LangSmith 项目、脱敏回调和隔离环境验收 | Phase 3，可观测性批次 |
| 17.3.2 Prometheus metrics | 尚未引入 collector 和 `/metrics` 端点 | 确定指标命名、标签基数和认证策略 | Phase 3，可观测性批次 |
| 17.3.3 Grafana Dashboard | 缺少 Prometheus 数据源和稳定指标定义 | 17.3.2 完成并积累基线数据 | Phase 3，Prometheus 上线后 |
| 17.4.1 Dockerfile | 仓库当前没有应用镜像构建文件 | 明确前端静态资源交付方式和 Python 运行时依赖 | Phase 3，容器部署批次 |
| 17.4.2 docker-compose.yml | 当前已编排开发数据库、带认证持久化的 Redis 7 与 Milvus，但仍未包含 App、ChromaDB 和应用健康依赖 | 完成 Dockerfile，并拆分最小应用栈与可选数据库 profiles | Phase 3，Dockerfile 完成后 |
| 17.4.3 .dockerignore | 仓库当前没有 .dockerignore | Dockerfile 路径和构建上下文确定后补齐并验证镜像上下文 | Phase 3，Dockerfile 同批次 |

---
