"""4.1.1 AnalysisState — LangGraph 状态图中流转的共享状态。"""

from __future__ import annotations

from typing import Annotated, Any, Sequence

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AnalysisState(TypedDict, total=False):
    """所有 Node 通过此状态共享输入输出。"""

    messages: Annotated[Sequence[BaseMessage], add_messages]
    user_query: str
    datasource: str
    intent: str
    activated_skills: list[str]
    skill_prompt_override: str
    skill_tools: list[Any]
    resolved_schema: Any
    relevant_tables: list[dict]
    few_shot_examples: list[dict]
    business_rules_text: str
    long_term_memories_text: str
    generated_sql: str
    retry_count: int
    sql_valid: bool
    validation_errors: list[dict]
    validation_warnings: list[dict]
    transpiled_sql: str
    explain_errors: list[dict]
    execution_error: str
    query_result_sample: list[dict]
    query_result_full_count: int
    query_result_statistics: dict
    analysis_result: dict
    chart_config: dict
    mcp_agent_output: str
    final_response: dict
