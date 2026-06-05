"""4.1.2-7 LangGraph 工作流组装 + 5 个条件路由函数。"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from src.graph.nodes.analyze_result import analyze_result_node
from src.graph.nodes.build_response import build_response_node
from src.graph.nodes.classify_intent import classify_intent_node
from src.graph.nodes.execute_sql import execute_sql_node
from src.graph.nodes.generate_chart import generate_chart_node
from src.graph.nodes.generate_sql import generate_sql_node
from src.graph.nodes.layer3_validate import layer3_validate_node
from src.graph.nodes.layer4_explain import layer4_explain_node
from src.graph.nodes.retrieve_schema import retrieve_schema_node
from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


# ================================================================
# 条件路由 (4.1.3-6)
# ================================================================

def after_layer3(state: AnalysisState) -> str:
    """4.1.3 sqlglot 校验后路由: security_block→终止 / syntax_error→重试 / ok→下一步。"""
    for e in state.get("validation_errors", []):
        if e.get("type") == "security_block":
            return "build_response"
    if state.get("validation_errors"):
        return "generate_sql"
    return "layer4_explain"


def after_layer4(state: AnalysisState) -> str:
    """4.1.4 EXPLAIN 后路由。"""
    if state.get("explain_errors"):
        return "generate_sql" if state.get("retry_count", 0) < 3 else "build_response"
    return "execute_sql"


def should_retry(state: AnalysisState) -> str:
    """4.1.5 执行失败后路由: 瞬态错误重试(最多3次)，配置错误直接终止。"""
    err = state.get("execution_error", "")
    retry = state.get("retry_count", 0)
    if not err:
        return "build_response"
    # 数据源缺失/配置错误 → 不重试，直接返回
    if "未配置" in err or "未找到" in err or "not found" in err.lower():
        return "build_response"
    if retry < 3:
        return "generate_sql"
    return "build_response"


def route_by_intent(state: AnalysisState) -> str:
    """4.1.6 意图路由: file_analysis→mcp_agent (Phase 2), 其他→标准流水线。"""
    if state.get("intent") == "file_analysis":
        return "mcp_agent"
    return "retrieve_schema"


# ================================================================
# 4.1.2 StateGraph 组装
# ================================================================

def build_workflow() -> StateGraph:
    workflow = StateGraph(AnalysisState)

    workflow.add_node("classify_intent", classify_intent_node)
    workflow.add_node("retrieve_schema", retrieve_schema_node)
    workflow.add_node("generate_sql", generate_sql_node)
    workflow.add_node("layer3_validate", layer3_validate_node)
    workflow.add_node("layer4_explain", layer4_explain_node)
    workflow.add_node("execute_sql", execute_sql_node)
    workflow.add_node("analyze_result", analyze_result_node)
    workflow.add_node("generate_chart", generate_chart_node)
    workflow.add_node("build_response", build_response_node)

    workflow.set_entry_point("classify_intent")
    workflow.add_conditional_edges(
        "classify_intent", route_by_intent,
        {"retrieve_schema": "retrieve_schema", "mcp_agent": END}
    )
    workflow.add_edge("retrieve_schema", "generate_sql")
    workflow.add_edge("generate_sql", "layer3_validate")
    workflow.add_conditional_edges(
        "layer3_validate", after_layer3,
        {"generate_sql": "generate_sql", "layer4_explain": "layer4_explain", "build_response": "build_response"}
    )
    workflow.add_conditional_edges(
        "layer4_explain", after_layer4,
        {"generate_sql": "generate_sql", "execute_sql": "execute_sql", "build_response": "build_response"}
    )
    workflow.add_conditional_edges(
        "execute_sql", should_retry,
        {"generate_sql": "generate_sql", "build_response": "build_response"}
    )
    workflow.add_edge("execute_sql", "analyze_result")
    workflow.add_edge("analyze_result", "generate_chart")
    workflow.add_edge("generate_chart", "build_response")
    workflow.add_edge("build_response", END)

    return workflow.compile()


app = build_workflow()
