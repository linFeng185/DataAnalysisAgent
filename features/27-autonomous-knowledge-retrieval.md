# 27. 自主知识检索

设计：`spec/51-autonomous-knowledge-retrieval.md`。依赖：6.8.2、6.8.3、6.8.7、6.8.8、6.9.4、10.2.9、10.2.10（均已具备实现）。

实现：`src/tools/knowledge_search.py`（只读工具）、`src/llm/knowledge_loop.py`（统一有界循环）；接入 `src/llm/invocation.py`、`src/graph/nodes/generate_sql.py`、`src/graph/nodes/analyze_result.py`、`src/graph/nodes/mcp_agent.py`、`src/knowledge/image_assets.py`、`src/api/streaming.py`、`src/reporting/plan_worker.py`、`src/knowledge/retrieval.py`、`src/knowledge/upload_manager.py`、`src/memory/vector_store_chroma.py`、`src/api/auth.py`、`src/llm/prompt_budget.py`。

| # | 功能 | 主实现 | 状态 | 优先级 |
|---|---|---|---|---|
| 27.1.1 | 只读StructuredTool、参数契约、证据引用 | `src/tools/knowledge_search.py` | 单测完成 | P1 |
| 27.1.2 | 身份复核、数据源授权、未绑定知识过滤 | `src/tools/knowledge_search.py`、`src/knowledge/retrieval.py` | 单测完成 | P1 |
| 27.1.3 | 共用有界循环、缓存、超时、消息预算 | `src/llm/knowledge_loop.py` | 单测完成 | P1 |
| 27.1.4 | 全业务LLM接入与裸调用审计 | `src/llm/invocation.py`、相关调用点 | 单测完成 | P1 |
| 27.1.5 | SQL流式、SSE与超时回退兼容 | `src/graph/nodes/generate_sql.py`、`src/api/streaming.py` | 单测完成 | P1 |
| 27.1.6 | MCP单循环、限额、失败关闭与引用 | `src/graph/nodes/mcp_agent.py` | 单测完成 | P1 |
| 27.1.7 | 后台可信身份隔离及模型观测 | `src/reporting/plan_worker.py`、`src/api/auth.py` | 单测完成 | P1 |
| 27.1.8 | 跨方言离线链路与真实环境验收记录 | `tests/test_graph/test_knowledge_sql_cross_dialect.py`、`test_knowledge_chroma_review.py` | 集成测试完成 | P1 |

## 覆盖说明

- 27.1.1/27.1.2：`tests/test_tools/test_knowledge_search.py`、`test_knowledge_search_review.py`、`test_knowledge_search_logging.py`、`test_knowledge_search_deadline.py`；覆盖参数契约、身份漂移、请求白名单与数据源授权、未绑定下推、超时/不可用/预算耗尽、缓存、脱敏、日志分级。
- 27.1.3：`tests/test_llm/test_knowledge_loop.py`、`test_knowledge_loop_review.py`、`test_knowledge_loop_message_conversion.py`；覆盖零检索单次调用、ID 闭合（含 `invalid_tool_calls`）、回合/次数上限、缓存、`bind_tools` 回退与追踪配置、取消传播、消息预算、分片合并与 provider 序列化。
- 27.1.4：统一接入点为 `invocation.py` 的 `invoke_structured`/`invoke_text`/`stream_prompt`；裸调用审计见下表。改动未新增依赖，Prompt 注册、预算与模型工厂保持不变。工具交互空间在同一 `context_budget` 内预留（`knowledge_tool_reserve_chars` = 工具定义 + 6000 字符单次结果 + 800 字符协议开销），大 Schema 场景下证据仍能进入续轮；预算装不下一条结果时按 `_prepare_tool_budget` 不挂工具，回退为一次普通调用。回归见 `test_knowledge_loop_review.py::test_large_sql_context_reserves_space_for_knowledge_tool`。
- 27.1.5：`tests/test_api/test_streaming_tool_round.py` 以回放 `astream_events` 事件序列验证 SSE 行为；`generate_sql` 与 `analyze_result` 通过 `KnowledgeRoundReset` 丢弃中间回合正文。
- 27.1.8（离线）：`tests/test_graph/test_knowledge_sql_cross_dialect.py` 以 Fake 模型 → 真实 StructuredTool 与真实检索层（隔离内存向量库）→ 续轮 SQL → `validate_with_sqlglot` + `validate_sql_schema`，参数化 ClickHouse/MySQL/PostgreSQL，断言模型调用次数、ToolMessage 闭合、引用与 SQL 条件。审核者另以 `test_knowledge_chroma_review.py` 验证真实内存Chroma的租户/私有范围过滤及三方言链路。真实在线验收未执行；本行状态只表示离线模块集成测试完成。
- 27.1.6：`tests/test_graph/test_knowledge_mcp_review.py` 验证外部 MCP 故障时本地知识工具仍可继续，市场研究护栏只看外部工具；知识引用沿用现有 `_extract_tool_citations` 并入 `citations`。
- 27.1.7：`tests/test_llm/test_knowledge_loop.py::TestKnowledgeLoopConfig::test_background_identity_binding_is_reset`、`tests/test_api/test_streaming_tool_round.py::test_knowledge_scope_is_reset_after_stream`、`tests/test_reporting/test_backend_planning.py`。

## 裸调用覆盖审计

| 位置 | 判定 | 依据 |
|---|---|---|
| `src/graph/nodes/analyze_result.py:1002` | 已接入 | 改走 `invoke_with_knowledge`，保留 `analysis_llm_timeout` 整体预算 |
| `src/graph/nodes/generate_sql.py:864` | 已接入 | 改走 `stream_with_knowledge`；首块超时回退也走统一循环 |
| `src/knowledge/image_assets.py:112` | 已接入 | 显式助手，保留多模态消息块、`callbacks=[]` 与 30s 超时 |
| `src/graph/nodes/mcp_agent.py:394` | 已接入 | 知识工具并入同一份工具列表，仍是单循环 |
| `src/graph/nodes/mcp_agent.py:410` | 排除 | Agent 无法构建时的降级直接回答，无工具语义 |
| `src/reporting/planner.py:562` | 排除 | 测试注入 Fake planner 的兼容分支，非业务路径 |
| `src/api/routes/chat.py:459`、`src/api/streaming.py:265` | 排除 | 图入口与事件流消费，非模型调用 |
| `src/graph/nodes/multi_source.py:232,933`、`src/reporting/runner.py:558,791`、`src/mcp_client/server.py:33` | 排除 | 子图与图入口，非模型调用 |
| `src/tools/*`、`src/skill_runtime.py:163`、`src/skill_worker.py:106`、`src/graph/nodes/analyze_result.py:745`、`src/graph/nodes/mcp_agent.py:233`、`src/graph/subgraphs/report.py:60` | 排除 | LangChain Tool 调用，无模型语义 |
| `src/llm/provider_openai.py`、`src/llm/provider_anthropic.py` | 排除 | 传输层实现，由 provider 契约覆盖 |

## 已知缺口与残余风险

| 项 | 说明 |
|---|---|
| 27.1.8 真实验收 | 本机 8000 端口无已授权会话（`curl` 均 401），无法完成真实服务链路验收；本轮仅完成离线与 Fake/回放测试，**不等于线上验收通过** |
| 在途任务收尾 | Milvus 只读路径已拆分为可放弃取消策略：`_run_on_collection_read`/`_run_abandonable_read` 取消后立即返回，在途线程受 SDK `_RPC_TIMEOUT_SECONDS` 约束并由 `_consume_task_result` 收束异常；写入路径仍用 `_run_sync` 等线程退出后才释放写锁。回归见 `tests/test_memory/test_milvus_read_cancellation.py`、`tests/test_tools/test_knowledge_search_deadline.py` |
| 流式展示 | 挂工具的回合由 SSE 缓冲，在 `on_chat_model_end` 判定后释放或丢弃；工具交互期间不逐 token 展示（设计允许），最终回合仍实时输出 |
| 单租户行为 | 保持现有部署策略：单租户模式不启用数据源权限表及多租户过滤，不得据此宣称任意配置都有强租户隔离；多租户隔离已用明确的多租户策略和真实内存Chroma验证 |
| 小预算模型节点 | 全业务入口已接入；不支持工具或预算放不下工具交互时退回原调用，部分既有小预算Prompt会触发此分支，不擅自提高其总预算 |

## 模块收尾

共8项，代码已实现8项，待开发0项；其中单测完成7项、离线集成测试完成1项。Codex最终独立定向回归1022项通过，Python编译通过；静态检查仅保留基线已有的Milvus未使用logging导入。详细审核见 `spec/51-autonomous-knowledge-retrieval.md` 第7节。

下表是独立的真实服务验收待办，不把离线通过等同于生产验收通过。

| 功能点 | 不开发原因 | 可开发条件 | 预计开发时机 |
|---|---|---|---|
| 27.1.8 真实服务验收 | 无可用授权会话、未配置本地模型；未擅自调用远程业务模型 | 在本机配置可用授权会话及本地模型，或显式授权远程模型测试；不在对话中粘贴凭证 | 授权环境具备后的部署验收阶段，复测SQL重试、异常分析、报表、多源隔离与失败恢复 |
