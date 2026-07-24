# 11. LangGraph 集成细节

## 11. LangGraph 与 LangChain 集成细节

### 11.1 Node 内 LLM 调用模式

每个 Node 遵循任务级模型路由，轻量任务优先本地模型，只有显式授权任务可调用远程模型：

```python
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from src.llm.client import get_task_llm, is_task_llm_available

# 每个 Node 的核心模式
async def generate_sql_node(state: AnalysisState) -> dict:
    if not is_task_llm_available("generate_sql"):
        return deterministic_sql_fallback(state)
    llm = get_task_llm("generate_sql", temperature=0, reasoning=False)
    prompt = ChatPromptTemplate.from_messages([
        ("system", SQL_GENERATION_SYSTEM_PROMPT),
        ("user", "{user_query}")
    ])
    parser = JsonOutputParser(pydantic_object=SQLOutput)
    chain = prompt | llm | parser
    result = await chain.ainvoke({
        "user_query": state["user_query"],
        "schemas": state["relevant_tables"],
        "datasource_type": state["datasource"],
        "few_shot_examples": state.get("few_shot_examples", [])
    })
    return {"generated_sql": result["sql"]}
```

### 11.2 ChatPromptTemplate 示例（SQL 生成）

```python
from langchain_core.prompts import ChatPromptTemplate

SQL_GENERATION_SYSTEM_PROMPT = """你是一个 {datasource_type} SQL 专家。根据表结构和用户问题生成正确的 SQL。

## 数据库表结构
{schemas}

## 参考示例
{few_shot_examples}

## 规则
1. 只生成 SELECT 语句
2. 大表查询必须包含时间范围过滤
3. 结果集默认限制 1000 行
4. 使用 {datasource_type} 正确的日期/字符串函数
5. 字段名和表名必须来自 Schema，禁止编造

## 输出格式
{format_instructions}
"""

sql_prompt = ChatPromptTemplate.from_messages([
    ("system", SQL_GENERATION_SYSTEM_PROMPT),
    ("placeholder", "{history}"),          # 对话历史自动注入
    ("user", "{user_query}")
])
```

### 11.3 流式输出 (Streaming)

利用 LangGraph 的 `astream_events` 实现 SSE 流式推送：

```python
async def stream_analysis(user_query: str, config: dict):
    """FastAPI SSE endpoint: 逐步推送每个 Node 的执行状态"""
    async for event in app.astream_events(
        {"user_query": user_query, "datasource": "clickhouse_prod"},
        config=config,
        version="v2"
    ):
        kind = event["event"]
        if kind == "on_chat_model_stream":
            # LLM 输出 token 级流式
            yield f"data: {json.dumps({'type': 'token', 'content': event['data']['chunk'].content})}\n\n"
        elif kind == "on_chain_start":
            # Node 开始执行
            yield f"data: {json.dumps({'type': 'node_start', 'node': event['name']})}\n\n"
        elif kind == "on_chain_end":
            # Node 执行完成
            yield f"data: {json.dumps({'type': 'node_end', 'node': event['name'], 'output': event['data'].get('output')})}\n\n"
```

### 11.4 会话持久化 (Checkpointer)

```python
from langgraph.checkpoint.postgres import PostgresSaver

# 生产环境使用 PostgreSQL 存储会话状态
checkpointer = PostgresSaver.from_conn_string(POSTGRES_URL)
checkpointer.setup()

app = workflow.compile(checkpointer=checkpointer)

# 每个用户会话通过 thread_id 隔离
config = {"configurable": {"thread_id": "user_session_abc123"}}
result = await app.ainvoke(
    {"user_query": "上个月销售额Top10品类"},
    config=config
)
# 会话自动持久化，追问时复用上下文

# 恢复历史会话
history = await app.aget_state(config)
```

同一 `thread_id` 只恢复会话级字段：`user_query/datasource/session_id/intent`、精简后的
`conversation_history`、`messages` 和轻量 `previous_turn_snapshot`。身份权限、Schema、SQL 校验与执行中间态、
结果样本、多源结果、分析、图表和 `final_response` 使用 `UntrackedValue`，不写入 checkpoint。
API 每轮必须重新注入当前身份与 `datasource_access/allowed_columns/row_filter_sql`，禁止恢复旧权限。

`prepare_turn` 仍负责清空当前轮瞬态状态，并在升级后首次请求中删除旧 checkpoint 内
`conversation_history[].final_result`。`previous_turn_snapshot` 只保存来源问题、意图、数据源集合、最终 SQL 和
`result_available` 标记，不保存结果、统计、分析、图表或多源数据。

为支持“分析刚才的数据”类明确追问，`restore_previous_result` 先校验当前数据源集合与轻量快照一致，再按
`session_id` 从 `HistoryStore.list_session(limit=1)` 读取上一轮 `query_history.final_result`。普通查询仍重新生成并
执行 SQL；历史存储不可用或富结果缺失时返回明确不可恢复说明，不从旧瞬态字段猜测结果。

会话 UI 恢复不能依赖最新 checkpoint。`build_response` 只把查询、最终 SQL、成功状态、摘要和图表类型写入
`conversation_history`，并把每轮完整 `final_response` 写入 `query_history.final_result` JSONB。

`query_history.final_result` 保存该轮 `sql/sql_statements/data/row_count/truncated/analysis/chart/`
`sql_reasoning_content/success/error_message`。历史 API 以持久化逐轮响应为权威数据，checkpoint 用于补充
轻量摘要和消息；禁止使用顶层 `generated_sql` 是否为空来判断富结果是否有效，因为多源查询的顶层
`generated_sql` 合法地为空。历史数据分页首次返回最新一页，再按 `turn_id` 向前加载。

### 11.5 多数据源结果与流式展示契约

1. 每个多数据源 worker 必须保存 `execute_sql` 完成方言重写、权限注入后的最终 SQL，
   不得继续向最终响应返回 LLM 原始 SQL。
2. 最终响应使用 `sql_statements` 返回多条 SQL，元素包含 `datasource`、`dialect`、`sql`；
   顶层 `sql` 保留为兼容展示字段，单源为最终 SQL，多源为带数据源注释的 SQL 合集。
3. 跨源列对齐先基于每个来源全部结果行生成列画像，将列划分为 `dimension` 和 `metric`：
   - 所有非空值均为数值且不是布尔值时为 `metric`；
   - 其他列为 `dimension`；
   - 多个来源的列宽、角色序列一致时，分别按维度序号和指标序号对齐，因此支持任意数量的数值列；
   - 规范列名按同位置别名的出现频次、可读性和稳定前缀确定，并记录原始别名映射；
   - 列宽或角色序列不一致时禁止强制语义对齐，保留原始字段并记录告警。
4. 前端表格必须使用所有结果行字段的有序并集生成列，作为不兼容结果的展示兜底；
   前端字段并集不承担指标语义合并职责。
5. `thinking`、`token`、`llm_start`、`llm_end` 事件必须携带稳定 `stream_id` 和 `node`。
   前端按 `stream_id` 独立缓冲并行 LLM 内容，禁止把多个数据源或多个 LLM 阶段直接拼接为一个字符串。
6. `chart.type=table` 表示数据表本身就是展示结果，前端不得再渲染“图表配置未生成”的空图表面板。

### 11.6 LangSmith 可观测性

```python
# .env 配置
# LANGCHAIN_TRACING_V2=true
# LANGCHAIN_API_KEY=ls__xxx
# LANGCHAIN_PROJECT=data-analysis-agent

# 每个 Node 的执行自动上报到 LangSmith:
# - Node 级延迟、输入/输出
# - LLM 调用 token 消耗、Prompt 快照
# - Tool 调用链完整追溯
# - 失败 Node 的错误堆栈

# 在 LangSmith UI 中可:
# - 对比不同 Prompt 版本的 SQL 正确率
# - 定位耗时最长的 Node 做优化
# - 查看完整调用链排查生产问题
```

---
