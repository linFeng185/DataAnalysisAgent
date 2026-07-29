"""多源合并复用的结果分析与图表展示子图。"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph

from src.graph.state import AnalysisState
from src.logging_config import get_logger


logger = get_logger(__name__)


# 方法作用：记录结果展示子图入口并保持父级状态不变。
# Args: state - 多源合并后的展示状态。
# Returns: 空状态增量。
async def _presentation_start(state: AnalysisState) -> dict:
    """记录结果展示子图入口，并保持父级状态不变。"""
    logger.info(
        "结果展示子图入口",
        row_count=len(state.get("query_result_sample", []) or []),
        analysis_precomputed=bool(state.get("multi_source_analysis_precomputed")),
    )
    return {}


# 方法作用：根据是否已有精确分析选择展示子图入口节点。
# Args: state - 包含预计算分析标记的展示状态。
# Returns: analyze_result 或 generate_chart 节点名。
def _route_presentation(state: AnalysisState) -> str:
    """精确分析已完成时直接生成图表，否则先执行统一分析节点。"""
    target = (
        "generate_chart"
        if state.get("multi_source_analysis_precomputed")
        else "analyze_result"
    )
    logger.info("结果展示子图路由", target=target)
    return target


# 方法作用：构建分析与图表固定顺序的结果展示子图。
# Args: 无。
# Returns: 已编译且不持久化中间态的 LangGraph 子图。
def build_result_presentation_subgraph() -> Any:
    """构建带固定分析、图表边的结果展示子图。"""
    from src.graph.nodes import analyze_result as analyze_module
    from src.graph.nodes import generate_chart as chart_module

    workflow = StateGraph(AnalysisState)
    workflow.add_node("presentation_start", _presentation_start)
    workflow.add_node("analyze_result", analyze_module.analyze_result_node)
    workflow.add_node("generate_chart", chart_module.generate_chart_node)
    workflow.set_entry_point("presentation_start")
    workflow.add_conditional_edges(
        "presentation_start",
        _route_presentation,
        {
            "analyze_result": "analyze_result",
            "generate_chart": "generate_chart",
        },
    )
    workflow.add_edge("analyze_result", "generate_chart")
    workflow.add_edge("generate_chart", END)
    logger.info("结果展示子图构建完成")
    return workflow.compile()
