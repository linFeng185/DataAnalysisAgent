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

`AnalysisState` 在兼容扁平字段之外提供四个请求级轻量分组：

- `request_context`：查询、会话 ID 和可信身份；
- `permission_context`：数据源权限快照、列白名单、行过滤和显式 Skill 授权；
- `routing_context`：意图、任务计划、数据源选择及 Skill 激活阶段；
- `execution_context`：方言、真实表名、校验/重试/行数摘要。

四组上下文全部使用 `UntrackedValue`，不得保存 SQL 文本、Schema 对象、结果行、分析、图表或模型推理。
迁移期间节点优先写入分组并保留旧扁平字段；适配器读取时优先使用明确存在的扁平字段，合法的 `0`、`False`
和空集合也必须覆盖旧分组值，禁止按真值回退造成跨轮污染。`prepare_turn` 每轮根据当前 API 输入和清空后的
执行字段重新构造四组上下文。

`prepare_turn` 仍负责清空当前轮瞬态状态，并在升级后首次请求中删除旧 checkpoint 内
`conversation_history[].final_result`。`previous_turn_snapshot` 只保存来源问题、意图、数据源集合、最终 SQL 和
`result_available` 标记，不保存结果、统计、分析、图表或多源数据。

为支持“分析刚才的数据”类明确追问，`restore_previous_result` 先校验当前数据源集合与轻量快照一致，再按
`session_id` 从 `HistoryStore.list_session(limit=1)` 读取上一轮 `query_history.final_result`。普通查询仍重新生成并
执行 SQL；历史存储不可用或富结果缺失时返回明确不可恢复说明，不从旧瞬态字段猜测结果。

会话 UI 恢复不能依赖最新 checkpoint。`build_response` 只把查询、最终 SQL、成功状态、摘要和图表类型写入
`conversation_history`，并把每轮完整 `final_response` 写入 `query_history.final_result` JSONB。

`query_history.final_result` 保存该轮 `sql/sql_statements/data/row_count/truncated/analysis/chart/`
`success/status/source/error_code/error_message`。原始模型推理不得进入响应、历史或会话恢复。历史 API 以持久化逐轮响应为权威数据，checkpoint 用于补充
轻量摘要和消息；禁止使用顶层 `generated_sql` 是否为空来判断富结果是否有效，因为多源查询的顶层
`generated_sql` 合法地为空。历史数据分页首次返回最新一页，再按 `turn_id` 向前加载。

### 11.5 多数据源结果与流式展示契约

单源主图和多源 worker 子图必须调用同一个 SQL 流程装配函数注册
`retrieve_schema/decompose_query/generate_sql/layer3_validate/layer4_explain/execute_sql` 及其条件边。
两者可配置不同终点，但禁止分别维护重试、安全校验、EXPLAIN 和执行拓扑。

调度边界使用三个 Pydantic 契约：`SourceQueryRequest` 只从父状态提取身份、权限、路由、历史和偏好白名单，
禁止使用 `dict(state)` 复制上一轮 SQL、错误、结果或图表；`SourceQueryResult` 统一成功、SQL、数据、阶段错误和
截断信息；`MultiSourceResult` 校验全部已选来源均有且仅有一个结果，并根据明细计算成功/失败数量。
兼容期继续写入 `multi_source_results`，同时把完整契约写入 `multi_source_result`。

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
5. `token`、`llm_start`、`llm_end` 事件必须携带稳定 `stream_id` 和 `node`。
   前端按 `stream_id` 独立缓冲并行 LLM 内容，禁止把多个数据源或多个 LLM 阶段直接拼接为一个字符串；
   原始 `reasoning_content` 只允许在服务端做长度计数和受控诊断，不得生成 `thinking` SSE 事件。
6. `chart.type=table` 表示数据表本身就是展示结果，前端不得再渲染“图表配置未生成”的空图表面板。

Skill 激活分为两个明确阶段：`classify_intent` 根据关键词、意图和可见表触发声明形成候选集，记录
`skill_candidate_ids`；`retrieve_schema` 获取真实表名后重新匹配，记录最终 `activated_skill_ids` 和
`skill_activation_stage=schema`。仅声明表触发器的 Skill 在 Schema 到达前只能进入候选集，不能提前激活。
显式选择的 Skill 在两个阶段都按复合资源 ID、租户和用户重新校验，不得混入自动匹配项。

### 11.6 通用能力子图

`TaskPlan.capability` 是能力路由的唯一稳定字段。文件分析、市场研究和外部动作在意图分类后直接进入
`file_analysis_subgraph`、`market_research_subgraph`、`action_subgraph`；预测与报告必须先复用完整 SQL
分析链，在 `analyze_result` 后进入 `forecast_subgraph` 或 `report_subgraph`。五类子图全部回到
`build_response`，禁止自行写历史或建立第二套响应出口。

预测子图只接受包含时间列、数值列且按时间有序的真实查询结果，输出必须包含回测、预测区间、模型卡和
限制说明。报告子图必须真实执行 `custom-report` 工具，经过 Skill Runtime 输入输出契约后生成 report
Artifact。外部动作子图默认返回人工确认状态，只有结构化请求同时具备动作名、幂等键和确认标志时才可
交给 `ExternalActionRegistry`。

所有子图输出继续由 `AnalysisArtifact` 归一化为 table/chart/report/forecast/recommendation，必须携带证据、
限制、置信度和复现信息。文件与市场研究只能使用当前请求授权的 MCP/Skill 工具，工具不可用时保留真实失败。

### 11.7 LangSmith 可观测性

```python
# .env 配置
# LANGCHAIN_TRACING_V2=true
# LANGCHAIN_API_KEY=ls__xxx
# LANGCHAIN_PROJECT=data-analysis-agent

# 每个 Node 的执行自动上报到 LangSmith:
# - Node 级延迟，输入/输出默认隐藏
# - LLM 调用 token 消耗、Prompt ID/版本
# - Tool 调用链完整追溯
# - 失败 Node 的错误堆栈

# 在 LangSmith UI 中可:
# - 对比不同 Prompt 版本的 SQL 正确率
# - 定位耗时最长的 Node 做优化
# - 查看完整调用链排查生产问题
```

业务 LLM 调用统一通过 `src/llm/invocation.py`，调用 metadata 至少包含 `prompt_id`、`prompt_version` 和
`llm_task`，能力节点按需附加 capability/datasource。PromptRegistry 保存多版本、当前激活版本和回滚历史；
生产回归通过 `tests/evaluators/run_eval.py` 的显式 `RUN_LANGSMITH_EVALS=1` 开关调用 LangSmith Dataset，
日常测试只运行固定离线评测集。

---
