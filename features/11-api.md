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
| 11.1.9 | GET /datasources | `src/api/routes.py` | 列出数据源 (分页) | 单测完成 (2.3.9) | P1 |
| 11.1.10 | GET /health | `src/api/routes.py` | 健康检查 | 单测完成 | P0 |
| 11.1.11 | PUT /schema/.../comment | `src/api/routes.py` | 手动标注字段 | 单测完成 | P1 |
| 11.1.12 | POST /mcp/{name}/reset | `src/api/routes.py` | Phase 2: MCP reset (依赖模块 8) | 待开发[^8] | P1 |
| 11.1.13 | GET /metrics | `src/api/routes.py` | Phase 2: 指标列表 (依赖模块 6) | 待开发 | P2 |
| 11.1.14 | GET /skills | `src/api/routes.py` | `GET /api/v1/skills` | 列出所有 Skill，含 is_builtin/triggers/tools/deps | 开发完成 | P2 |
| 11.1.15 | POST /skills/upload | `src/api/routes.py` | `POST /api/v1/skills/upload` | 批量上传 SKILL.md，递归匹配 + YAML 解析 | 开发完成 | P2 |
| 11.1.16 | POST /skills/refresh | `src/api/routes.py` | `POST /api/v1/skills/refresh` | 全量重扫所有 Skill 目录 | 开发完成 | P2 |
| 11.1.17 | GET /skills/{name}/content | `src/api/routes.py` | `GET /api/v1/skills/{name}/content` | 返回 SKILL.md 原始内容 | 开发完成 | P2 |
| 11.1.18 | PUT /skills/{name}/toggle | `src/api/routes.py` | 启用/禁用 Skill | 开发完成 | P2 |
| 11.1.19 | DELETE /skills/{name} | `src/api/routes.py` | 删除 Skill（内置禁止） | 开发完成 | P2 |
| 11.1.20 | GET /knowledge | `src/api/routes.py` | `GET /api/v1/knowledge` | 列出知识条目，支持 category + search | 开发完成 | P2 |
| 11.1.21 | GET /knowledge/docs | `src/api/routes.py` | `GET /api/v1/knowledge/docs` | 列出已索引文档（全格式） | 开发完成 | P2 |
| 11.1.22 | POST /knowledge/docs/upload | `src/api/routes.py` | 上传文档，参数 strategy/chunk_size/category，返回 task_id | 开发完成 | P2 |
| 11.1.23 | GET /knowledge/upload/status | `src/api/routes.py` | 查询异步处理任务进度 | 开发完成 | P2 |
| 11.1.24 | GET /knowledge/docs/{name}/content | `src/api/routes.py` | 获取文档内容（PDF 返回 raw_url, Word 转 HTML, TXT 纯文本） | 开发完成 | P2 |
| 11.1.25 | GET /knowledge/docs/{name}/raw | `src/api/routes.py` | 返回原始文件（PDF iframe 渲染） | 开发完成 | P2 |
| 11.1.26 | DELETE /knowledge/{id} | `src/api/routes.py` | 删除知识条目（系统条目禁止） | 开发完成 | P2 |
| 11.1.27 | DELETE /knowledge/docs/{name} | `src/api/routes.py` | 删除文档及其所有关联条目（内置禁止） | 开发完成 | P2 |

### 11.2 分页增强

| # | 功能 | 文件 | 路由 | 描述 | 状态 |
|---|------|------|------|------|------|
| 11.2.1 | 表列表分页 | `src/api/routes.py` | ?page=1&page_size=20&search=xxx | 单测完成 | P1 |
| 11.2.2 | 会话历史分页 | `src/api/routes.py` | HistoryStore 内存环形缓冲区 → 前端逐页浏览 | 开发完成 | P1 |
| 11.2.3 | 数据源列表分页 | `src/api/routes.py` | ?page=1&page_size=20 | 单测完成 | P1 |

### 11.3 请求/响应 Schema (重新编号为 11.3)

### 11.2 请求/响应 Schema

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 11.2.1 | ChatRequest | `src/api/schemas.py` | query / session_id / datasource | 单测完成 | P0 |
| 11.2.2 | ChatResponse | 同上 | success / sql / data / analysis / chart | 单测完成 | P0 |
| 11.2.5 | DataSourceCreateRequest | 同上 | name / dialect / host / ... | 单测完成 | P0 |
| 11.2.7 | HealthResponse | 同上 | status / llm_available / uptime | 单测完成 | P0 |

### 11.3 流式输出

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 11.3.1 | stream_analysis() | `src/api/streaming.py` | FastAPI SSE endpoint: astream_events 循环推送 | 开发完成 |
| 11.3.2 | on_chat_model_stream 事件处理 | 同上 | LLM token 级别流式推送 | 开发完成 |
| 11.3.3 | on_chain_start 事件处理 | 同上 | Node 开始执行通知 | 开发完成 |
| 11.3.4 | on_chain_end 事件处理 | 同上 | Node 执行完成通知 (含 output) | 开发完成 |
| 11.3.5 | SSE 格式化 | 同上 | `data: {json}\n\n` 格式包装 | 开发完成 |

---
