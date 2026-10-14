# 11. API 层

## 11. API 层 (api/)

### 11.1 核心接口

| # | 功能 | 文件 | 路由 | 描述 | 状态 |
|---|------|------|------|------|------|
| 11.1.1 | POST /chat | `src/api/routes.py` | `POST /api/v1/chat` | NL 查询 → 完整分析结果 | 单测完成 | P0 |
| 11.1.2 | POST /chat/stream | `src/api/routes.py` | `POST /api/v1/chat/stream` | SSE 流式 (astream_events) | 单测完成 | P0 |
| 11.1.3 | GET /schema/tables | `src/api/routes.py` | `GET /api/v1/schema/tables` | 表列表 + 分页 + 搜索 | 单测完成 | P0 |
| 11.1.4 | GET /schema/tables/{name} | `src/api/routes.py` | `GET /api/v1/schema/tables/{table_name}` | 指定表结构 | 单测完成 | P0 |
| 11.1.5 | POST /schema/refresh | `src/api/routes.py` | `POST /api/v1/schema/refresh` | 手动刷新 Schema | 单测完成 | P0 |
| 11.1.6 | GET /history | `src/api/routes.py` | `GET /api/v1/history` | 查询历史列表，支持 datasource + search 过滤 | 开发完成 | P1 |
| 11.1.7 | POST /datasources | `src/api/routes.py` | 注册数据源 | 单测完成 (2.3.7) | P1 |
| 11.1.8 | DELETE /datasources/{name} | `src/api/routes.py` | 删除数据源 | 单测完成 (2.3.8) | P1 |
| 11.1.9 | GET /datasources | `src/api/routes.py` | 按当前身份授权过滤后列出数据源 (分页) | 单测完成 (2.3.9) | P1 |
| 11.1.10 | GET /health | `src/api/routes/management.py` | 仅返回应用存活、版本和运行时长，不执行无租户 LLM 探测 | 单测完成 | P0 |
| 11.1.11 | PUT /schema/.../comment | `src/api/routes.py` | 手动标注字段 | 单测完成 | P1 |
| 11.1.12 | POST /mcp/{name}/reset | `src/api/routes/mcp.py` | `POST /api/v1/mcp/{name}/reset` | 手动重置 degraded/unavailable MCP Server：身份可见性 + 管理授权（system 仅平台超管、内置 YAML 仅平台超管），scope 可选消歧；未知/不可见 404、无权/停用/安全阻断 403（且不建连）、存储不可用 503、超时 504、其他脱敏 500；授权校验先于任何连接、只准备并重建已授权目标（支持数据库中尚未加载的合法目标，不用整租户连接加载实现 reset） | 集成测试完成 | P1 |
| 11.1.13 | 业务指标目录 API | `src/api/routes/metric_catalog.py` | 独立 /metric-catalog；租户隔离、分页、不可变版本、审核、生效和固定版本引用，不占用 Prometheus /metrics | 单测完成 | P2 |
| 11.1.14 | GET /skills | `src/api/routes.py` | `GET /api/v1/skills` | 列出所有 Skill，含 is_builtin/triggers/tools/deps | 开发完成 | P2 |
| 11.1.15 | POST /skills/upload | `src/api/routes.py` | `POST /api/v1/skills/upload` | 批量上传、验签、失败回滚和受控错误分类 | 单测完成 | P2 |
| 11.1.16 | POST /skills/refresh | `src/api/routes.py` | `POST /api/v1/skills/refresh` | 全量重扫并恢复持久启停状态 | 单测完成 | P2 |
| 11.1.17 | GET /skills/{name}/content | `src/api/routes.py` | `GET /api/v1/skills/{name}/content` | 返回 SKILL.md 原始内容 | 开发完成 | P2 |
| 11.1.18 | PUT /skills/{name}/toggle | `src/api/routes.py` | 原子持久化启用/禁用状态，异常响应脱敏 | 单测完成 | P2 |
| 11.1.19 | DELETE /skills/{name} | `src/api/routes.py` | 删除 Skill（内置禁止） | 开发完成 | P2 |
| 11.1.20 | GET /knowledge | `src/api/routes.py` | `GET /api/v1/knowledge` | 列出知识条目，支持 category + search | 开发完成 | P2 |
| 11.1.21 | GET /knowledge/docs | `src/api/routes.py` | `GET /api/v1/knowledge/docs` | 列出已索引文档（全格式） | 开发完成 | P2 |
| 11.1.22 | POST /knowledge/docs/upload | `src/api/routes.py` | 上传文档，参数 strategy/chunk_size/category，返回 task_id | 开发完成 | P2 |
| 11.1.23 | GET /knowledge/upload/status | `src/api/routes.py` | 查询异步处理任务进度 | 开发完成 | P2 |
| 11.1.24 | GET /knowledge/docs/{name}/content | `src/api/routes.py` | 获取文档内容（PDF 返回 raw_url, Word 转 HTML, TXT 纯文本） | 开发完成 | P2 |
| 11.1.25 | GET /knowledge/docs/{name}/raw | `src/api/routes.py` | 返回原始文件（PDF iframe 渲染） | 开发完成 | P2 |
| 11.1.26 | DELETE /knowledge/{id} | `src/api/routes.py` | 删除知识条目（系统条目禁止） | 开发完成 | P2 |
| 11.1.27 | DELETE /knowledge/docs/{name} | `src/api/routes.py` | 删除文档及其所有关联条目（内置禁止） | 开发完成 | P2 |
| 11.1.28 | POST /auth/login | `src/api/auth.py` | 登录成功后设置 HttpOnly access_token Cookie，不返回明文令牌 | 单测完成 | P0 |
| 11.1.29 | POST /auth/logout | `src/api/auth.py` | 清除访问 Cookie | 单测完成 | P0 |
| 11.1.30 | GET /auth/me | `src/api/auth.py` | 返回当前身份、认证状态和认证开关 | 单测完成 | P0 |
| 11.1.31 | 数据源管理生命周期 | `src/api/routes.py` | 注册后可见、删除后不可见、重复删除返回 404 | 集成测试完成 | P1 |
| 11.1.32 | 知识上传安全边界 | `src/api/routes.py` | 写入前限制文件数、单文件和累计字节，Word 文本安全转义 | 单测完成 | P1 |
| 11.1.33 | POST /assets/profile | `src/api/routes.py` | CSV/Excel/Parquet 列级 profile 和质量摘要 | 单测完成 | P1 |
| 11.1.34 | POST /assets/query | `src/api/routes.py` | 上传结构化文件执行 DuckDB 只读 SQL；单文件固定表名为 `data`，多 Sheet 表名从返回体 `tables` 获取，返回结果和截断状态 | 单测完成 | P1 |
| 11.1.35 | 知识范围 API | `src/api/routes.py` | 知识列表/文档上传/删除支持 system、tenant、private 范围与角色授权 | 集成测试完成 | P1 |
| 11.1.36 | 知识标签 API | `src/api/routes.py` | 标签搜索、个人标签创建、超级管理员维护与提升全局标签 | 集成测试完成 | P1 |
| 11.1.37 | POST /auth/register 注册保护 | `src/api/auth.py` | 公开注册按客户端地址限流，bcrypt 在线程池执行，多租户创建在事务内完成 | 单测完成 | P0 |
| 11.1.38 | 授权数据源发现 `[P0]` | `src/api/routes.py` | 显式选择先鉴权；未选择时把当前身份授权候选交给工作流 | 单测完成 | P0 |
| 11.1.39 | 会话完整删除 `[P0]` | `src/api/routes.py`、`src/memory/history_store.py` | 返回完整 session_id；删除同步清理历史和新旧 Checkpointer 线程 | 单测完成 | P0 |
| 11.1.40 | Grafana SimpleJSON 查询协议 | `src/api/routes/grafana.py` | `/grafana/search`、`/grafana/query`、`/grafana/annotations` 复用 JWT、租户、数据源 ACL 和只读有界执行 | 单测完成 | P2 |
| 11.1.41 | SQL 自动优化建议 | `src/api/routes/optimization.py` | `/optimization/advice` 返回确定性问题、候选索引和重写证据，明确不执行 DDL | 单测完成 | P3 |
| 11.1.42 | POST /chat/files | `src/api/routes/chat.py`、`src/knowledge/file_store.py` | 私有附件批量预校验；部分保存失败按精确 file_id 逆序补偿，不误删同名历史文件 | 单测完成 | P1 |

### 11.2 分页增强

| # | 功能 | 文件 | 路由 | 描述 | 状态 |
|---|------|------|------|------|------|
| 11.2.1 | 表列表分页 | `src/api/routes.py` | ?page=1&page_size=20&search=xxx | 单测完成 | P1 |
| 11.2.2 | 会话历史分页 | `src/api/routes.py` | Checkpointer + HistoryStore 持久化回退，支持前端逐页浏览 | 单测完成 | P1 |
| 11.2.3 | 数据源列表分页 | `src/api/routes.py` | ?page=1&page_size=20 | 单测完成 | P1 |
| 11.2.4 | 会话逐轮富数据恢复 | `src/api/routes.py` | 每轮返回独立 final_result；首次加载最新 20 轮，before 游标向前分页 | 集成测试完成 | P1 |

### 11.3 请求/响应 Schema (重新编号为 11.3)

### 11.2 请求/响应 Schema

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 11.2.1 | ChatRequest | `src/api/schemas.py` | query / session_id / datasource | 单测完成 | P0 |
| 11.2.2 | ChatResponse | 同上 | success / sql / sql_statements / data / analysis / chart / row_count / truncated，并在非流式与 SSE 最终结果一致透出 activated_skills / activated_knowledge 来源摘要 | 单测完成 | P0 |
| 11.2.5 | DataSourceCreateRequest | 同上 | name / dialect / host / ... | 单测完成 | P0 |
| 11.2.7 | HealthResponse | 同上 | status / version / uptime_seconds | 单测完成 | P0 |
| 11.2.8 | ModelTestRequest | 同上 | model_id 长度与字符集校验，模型测试固定单 token | 单测完成 | P0 |
| 11.2.9 | Schema 可变默认工厂 | 同上 | list/dict 字段统一使用 Field(default_factory=...) | 单测完成 | P1 |
| 11.2.10 | OptimizationRequest | 同上 | SQL、方言、可选 Schema/索引/统计信息和耗时证据校验 | 单测完成 | P3 |
| 11.2.11 | MCPServerResetResponse | 同上 | reset 成功响应契约 status / name / scope / tools | 单测完成 | P1 |

### 11.3 流式输出

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 11.3.1 | stream_analysis() | `src/api/streaming.py` | FastAPI SSE endpoint: astream_events 循环推送 | 开发完成 |
| 11.3.2 | on_chat_model_stream 事件处理 | 同上 | LLM token 级别流式推送 | 开发完成 |
| 11.3.3 | on_chain_start 事件处理 | 同上 | Node 开始执行通知 | 开发完成 |
| 11.3.4 | on_chain_end 事件处理 | 同上 | Node 执行完成通知 (含 output) | 开发完成 |
| 11.3.5 | SSE 格式化 | 同上 | `data: {json}\n\n` 格式包装 | 开发完成 |
| 11.3.6 | Decimal JSON 精度 `[P1]` | 同上 `_json_serialize()` | Decimal 归一化→int或精确float | 开发完成 |
| 11.3.7 | 全局 PrecisionResponse `[P1]` | `src/main.py` | FastAPI 默认响应类替换，float→Decimal | 开发完成 |
| 11.3.8 | 并行 LLM 流隔离 `[P1]` | `src/api/streaming.py` | token 携带 run_id 派生的 stream_id 并按调用实例隔离；reasoning_content 仅服务端计数，不生成 thinking 事件 | 单测完成 |
| 11.3.9 | 后台任务异常跟踪 | `src/api/background_tasks.py`、`src/api/routes/` | API 后台任务持有强引用并通过 done callback 记录异常 | 单测完成 | P0 |

### 模块收尾

模块功能点共 61 项，已完成 61 项，待开发 0 项。

> 11.1.12 已完成实现与定向回归：`tests/test_mcp/test_owner_task.py`、`test_reset_lifecycle.py`（含并发/竞态/超时/取消/总预算覆盖锁等待与拆旧/取消后重试）、`test_reset_stdio_real.py`（真实本地 stdio 子进程，含无孤儿进程回收断言）、`tests/test_api/test_mcp_reset.py`（授权与状态码契约、拒绝目标零连接、未加载目标单独准备）。
> 11.1.12 路由总 deadline 补充（2026-10-14）：`reset_mcp_server` 以路由级 `asyncio.timeout` 覆盖目标准备（`prepare_authorized_server`）与重置全程，剩余预算传给 `reset_server`；准备阶段也受 504 约束，准备失败与重置失败同套脱敏映射（404/403/500），取消继续向上传播、服务锁不被误释放。回归：`tests/test_api/test_mcp_reset.py::TestMcpResetRouteDeadline`（冷目标/既有目标持锁 504、准备耗时扣除、准备失败映射、取消后锁可重试、未授权零连接）。
> 11.1.12 异常日志单次归属补充（2026-10-14）：同一连接失败穿过 owner→manager→reset 路由时只落一份脱敏堆栈——owner 边界记录后打标（`mark_error_logged`），路由遇已记录异常只做状态映射、不再重复落盘；owner 未介入的准备/构建直接异常仍由路由单次记录；取消/超时路径不变。回归：`tests/test_api/test_mcp_reset.py::TestMcpResetLoggingBoundary`（owner+manager+route 三层联合日志捕获，单次计数、无凭证、保留原堆栈抛错位置）。
> 验收边界：真实本地进程验收（stdio 子进程）不等于生产远程 MCP 服务验收；远程 SSE 连通性、allowlist 主机解析与生产网络边界仍需部署环境复核。

---
