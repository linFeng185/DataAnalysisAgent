"""
AnalysisState — LangGraph 状态图中流转的共享状态。

这是整个流水线的「数据契约」。每个节点函数接收 AnalysisState，
返回部分字段的 dict，LangGraph 自动合并回状态中。

total=False 意味着所有字段都是可选的（Optional），节点只填充自己负责的字段。
messages 字段使用 Annotated + add_messages 实现 append-only 语义。
"""

from __future__ import annotations

from typing import Annotated, Any, Sequence

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AnalysisState(TypedDict, total=False):
    """所有节点通过此状态共享输入输出。

    字段按数据流顺序组织，标注了「谁写 / 谁读」。
    """

    # ── 输入层（API 传入）──────────────────────────
    user_query: str
    """用户的自然语言查询，由 routes.py 注入。—— classify_intent / generate_sql 读"""

    datasource: str
    """目标数据源名称，由 routes.py 注入。—— retrieve_schema / execute_sql 读"""

    intent: str
    """意图分类结果（query/trend/aggregation/attribution/metadata/file_analysis/chat）。
    —— classify_intent 写，route_by_intent / analyze_result 读"""

    # ── 扩展层（技能/工具 注入，Phase 2）──────────
    activated_skills: list[str]
    skill_prompt_override: str
    skill_tools: list[Any]
    conversation_history: list[dict]
    selected_datasources: list[str]
    multi_source_results: list[dict]
    allowed_columns: list[str]
    row_filter_sql: str

    # ── Schema 层 ──────────────────────────────────
    dialect: str
    """数据源方言（clickhouse/mysql/postgres/oracle/mssql）。
    —— retrieve_schema 从 Registry 获取并写入，generate_sql 读"""

    resolved_schema: Any
    """数据源 Registry 返回的 Schema 对象。—— retrieve_schema 写"""

    relevant_tables: list[dict]
    """轻量表结构（name/description/columns），用于拼入 LLM Prompt。
    —— retrieve_schema 写，generate_sql 读"""

    # ── Prompt 增强层（Phase 2）────────────────────
    few_shot_examples: list[dict]
    business_rules_text: str
    """业务规则文本，从知识库检索后注入 Prompt。—— retrieve_schema 初始化为空"""

    long_term_memories_text: str
    """长期记忆文本，从记忆系统检索后注入 Prompt。—— retrieve_schema 初始化为空"""

    # ── SQL 生成层 ─────────────────────────────────
    generated_sql: str
    """LLM 生成的 SQL 语句。—— generate_sql 写，layer3_validate / execute_sql 读"""

    sql_reasoning_content: str
    """SQL 生成时的模型推理链（DeepSeek thinking）。—— generate_sql 写，build_response 读"""

    retry_count: int
    """当前重试次数（0-3）。—— generate_sql 递增写，条件路由读"""

    messages: Annotated[Sequence[BaseMessage], add_messages]
    """LangChain 消息历史（append-only），用于多轮对话。—— generate_sql / analyze_result 读写"""

    # ── SQL 校验层 ─────────────────────────────────
    sql_valid: bool
    """sqlglot 语法解析结果。—— layer3_validate 写"""

    validation_errors: list[dict]
    """校验错误列表（每项包含 type+message）。—— layer3_validate 写，
    after_layer3 / build_response 读"""

    validation_warnings: list[dict]
    """校验警告列表。—— layer3_validate 写"""

    transpiled_sql: str
    """方言转译后的 SQL（当前与 generated_sql 相同）。—— layer3_validate 写"""

    # ── EXPLAIN 层 ─────────────────────────────────
    explain_errors: list[dict]
    """EXPLAIN 执行错误列表。—— layer4_explain 写，after_layer4 读"""

    # ── 执行层 ─────────────────────────────────────
    execution_error: str
    """SQL 执行错误信息。—— execute_sql 写，should_retry 读"""

    query_result_sample: list[dict]
    """查询结果前 200 行（list[dict]）。—— execute_sql 写，analyze_result / build_response 读"""

    query_result_full_count: int
    """查询结果总行数。—— execute_sql 写"""

    query_result_statistics: dict
    """查询结果的基本统计（行数/数值列名）。—— execute_sql 写（当前未填充），analyze_result 读"""

    # ── 分析层 ─────────────────────────────────────
    analysis_result: dict
    """分析结果（summary/insights/recommended_chart_type/follow_up_questions/statistics）。
    —— analyze_result 写，build_response 读"""

    # ── 图表层 ─────────────────────────────────────
    chart_config: dict
    """ECharts 图表配置（type + echarts_option）。—— generate_chart 写，build_response 读"""

    # ── MCP 集成层（Phase 2）───────────────────────
    mcp_agent_output: str
    """MCP 子图输出（文件分析场景）。—— 仅 file_analysis 路径使用"""

    # ── 输出层 ─────────────────────────────────────
    final_response: dict
    """最终 API 响应体（success/session_id/user_query/sql/data/analysis/chart）。
    —— build_response 写，routes.py 读"""
