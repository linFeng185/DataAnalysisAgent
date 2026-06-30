# 11. LangGraph 集成细节

## 11. LangGraph 与 LangChain 集成细节

### 11.1 Node 内 LLM 调用模式

每个 Node 遵循统一的 LangChain 调用模式：

```python
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain_core.output_parsers import JsonOutputParser

# 每个 Node 的核心模式
async def generate_sql_node(state: AnalysisState) -> dict:
    llm = ChatOpenAI(model="gpt-4o", temperature=0)
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

### 11.5 LangSmith 可观测性

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
