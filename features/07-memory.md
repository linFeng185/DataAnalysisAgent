# 7. 记忆系统

## 7. 记忆系统 (memory/) `[P0:10 P1:14 P2:4 P3:2]`

### 7.1 Checkpointer

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 7.1.1 | PostgresSaver 配置 | `src/memory/checkpointer.py` | 生产环境 PostgreSQL checkpointer 初始化 + setup()，Windows 自动切换 SelectorEventLoop | 单测完成 |
| 7.1.2 | MemorySaver 配置 | 同上 | 开发环境内存 checkpointer (用于测试) | 开发完成 |
| 7.1.3 | checkpointer 工厂函数 | 同上 | 自动选择 PostgresSaver/MemorySaver；自动建库时安全引用数据库标识符 | 单测完成 |

### 7.2 短期记忆

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 7.2.1 | SessionContext dataclass | `src/memory/models.py` | session_id / thread_id / user_id / created_at / conversation_history / current_datasource / current_tables / last_sql / last_result_summary | 开发完成 |
| 7.2.2 | ConversationTurn dataclass | 同上 | turn_id / user_query / generated_sql / execution_success / analysis_summary / chart_type / timestamp | 开发完成 |
| 7.2.3 | 会话恢复 | `src/memory/checkpointer.py`、`src/api/routes/session.py` | 通过 thread_id 恢复状态；存储故障向上报告，不伪装为空历史 | 单测完成 |
| 7.2.4 | 超时归档 (30分钟) | `src/memory/session_archive.py` | 超过 30 分钟未活动的会话 → 摘要后移入 sessions_archive 表 | 单测完成 |
| 7.2.5 | Token 上下文压缩 | 同上 | 按当前模型 `context_window` 动态计算输入预算；达到 80% 触发摘要，回落到 55%，92% 硬保护；50 轮仅作为无法估算旧 checkpoint 的兼容兜底 | 单测完成 |
| 7.2.6 | on_session_start() | 同上、`src/graph/nodes/prepare_turn.py` | 轮次启动按认证身份加载用户偏好和相关长期记忆 | 单测完成 |
| 7.2.7 | archive_sessions() | 同上 | 原子迁移超过 30 天的 inactive 会话到 sessions_archive | 单测完成 |
| 7.2.8 | summarize_session() | `src/memory/context_builder.py` | 规则拼接摘要文本用于归档 | 开发完成 |
| 7.2.9 | 逐轮结构化响应持久化 | `src/memory/history_store.py` | 工作流显式 await PG 写入，`final_result` JSONB 保存每轮 SQL、数据、分析、图表和推理 | 单测完成 |

### 7.3 长期记忆

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 7.3.1 | MemoryType Enum | `src/memory/models.py` | USER_PREFERENCE / SQL_TEMPLATE / LEARNED_PATTERN / CORRECTION / PROJECT_RULE | 开发完成 |
| 7.3.2 | LongTermMemory dataclass | 同上 | 增加 visibility / tenant_id / owner_user_id / UTC TTL 判定 | 单测完成 |
| 7.3.3 | LongTermMemoryStore 类 | `src/memory/long_term_store.py` | 封装 VectorStore + PostgreSQL 双写和请求身份事务 | 单测完成 |
| 7.3.4 | search() | 同上 | system + 当前 tenant + 当前 private 精确检索、越权复核、TTL 和置信度过滤 | 单测完成 |
| 7.3.5 | save_sql_template() | 同上、`src/graph/nodes/build_response.py` | 成功 SQL 默认写入当前用户 private 范围，180 天 TTL | 单测完成 |
| 7.3.6 | save_correction() | 同上 | 保存当前用户私有纠正记录: confidence=0.95 | 单测完成 |
| 7.3.7 | save_preference() | 同上 | 保存当前用户私有偏好: confidence=1.0 | 单测完成 |
| 7.3.8 | get_preferences() | 同上 | 按 tenant_id + owner_user_id 精确获取未过期偏好 | 单测完成 |
| 7.3.9 | _upsert() | 同上 | 带 RLS 身份事务幂等写入 VectorStore + PostgreSQL | 单测完成 |
| 7.3.10 | _to_memory() | 同上 | 兼容新旧 metadata 并恢复身份和生命周期字段 | 单测完成 |
| 7.3.11 | _upsert() 双写事务保证 | 同上 | 先 PG 后 VectorStore；失败写入 pending_vector_sync 补偿 | 单测完成 |
| 7.3.12 | 生产迁移与 RLS | `migrations/008_memory_runtime.sql` | 长期记忆、向量补偿、会话归档表及三级可见性 RLS | 单测完成 |

### 7.4 记忆维护

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 7.4.1 | MemoryMaintenance 类 | `src/memory/session_archive.py`、`src/bootstrap.py` | 应用启动后立即运行并按配置周期调度，关闭时停止 | 单测完成 |
| 7.4.2 | decay_old_templates() | `src/memory/long_term_store.py` | 30 天未使用的 SQL 模板置信度 * 0.5 并同步向量 metadata | 单测完成 |
| 7.4.3 | prune_low_confidence() | 同上 | 双后端删除 confidence < 0.3 且 access_count = 0 的自动模板 | 单测完成 |
| 7.4.4 | prune_expired() | 同上 | 按 ttl_days 双后端清理过期长期记忆 | 单测完成 |
| 7.4.5 | reconcile_pending_sync() | 同上 | 周期消费 PG 补偿队列并恢复 VectorStore 一致性 | 单测完成 |

### 7.5 上下文裁剪

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 7.5.1 | build_llm_context() | `src/memory/context_builder.py` | 统一按模型 Token 预算裁剪：热数据完整保留 → 温数据摘要 → 冷数据检索；支持租户模型上下文窗口和最新轮次最低保留 | 单测完成 |
| 7.5.2 | _summarize_turns() | 同上 | 受 Token 上限保护的 LLM 摘要优先，失败时规则拼接回退 | 单测完成 |
| 7.5.3 | 各 Node 上下文裁剪集成 | 各 Node 文件 | generate_sql / analyze_result Node 调用 build_llm_context()（generate_chart 为桩实现无需集成） | 开发完成 |
| 7.5.4 | 动态 Prompt token 预算检查 | `src/memory/context_builder.py` | 扣除 Prompt/输出预留后按模型窗口计算 80% 触发、55% 目标和 92% 硬保护；显式上限仅能收紧预算 | 单测完成 |
| 7.5.5 | 异步预计算摘要 | `src/memory/context_builder.py` | LLM 优先生成摘要 (_summarize_turns_llm)，失败回退规则拼接；摘要输入自身受 Token 上限保护，session_archive 同步支持 | 单测完成 |

### 7.6 长期记忆管理（spec/44）

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 7.6.1 | list_memories() | `src/memory/long_term_store.py` | PostgreSQL 权威表分页查询，归属恒为当前用户 private 范围 | 单测完成 | P1 |
| 7.6.2 | get_memory() | 同上 | 按 ID 直查并校验归属，失败返回 None | 单测完成 | P1 |
| 7.6.3 | delete_memory() | 同上 | PG 删除后经 `_delete_vector_or_queue` 补偿队列同步向量库 | 单测完成 | P1 |
| 7.6.4 | 记忆管理 API | `src/api/routes/memory.py` | GET /memory、GET/DELETE /memory/{id}；越权返回 404；删除记 audit 日志 | 单测完成 | P1 |

### 7.7 嵌入后端（spec/46）

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 7.7.1 | ONNX 嵌入后端 | `src/memory/embedding_backend.py` | onnxruntime + tokenizers 加载 onnx/model.onnx；mean pooling + L2 归一化；启动导入 5.42s→0.12s | 单测完成 | P1 |
| 7.7.2 | 双后端共用 | `src/memory/vector_store_milvus.py`、`vector_store_pg.py` | Milvus 与 PG 共用同一嵌入实现，编码契约统一为 list[str]→list[list[float]] | 单测完成 | P1 |
| 7.7.3 | 数值一致性验证 | `spec/46` §3 | 与 SentenceTransformer 输出余弦 1.000000，最大绝对差 1.71e-07 | 单测完成 | P1 |

---
