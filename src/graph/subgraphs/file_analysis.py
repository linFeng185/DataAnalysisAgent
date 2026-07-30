"""文件分析独立能力子图。"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from src.graph.nodes.mcp_agent import mcp_agent_node
from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


async def file_analysis_node(state: AnalysisState) -> dict:
    """通过授权的文件/MCP 工具执行分析并标记能力来源。"""
    logger.info("文件分析子图执行开始")
    result = await mcp_agent_node(state)
    response = dict(result.get("final_response", {}) or {})
    response["source"] = "file_analysis"
    result["final_response"] = response
    logger.info("文件分析子图执行完成", success=bool(response.get("success")))
    return result


def build_file_analysis_subgraph():
    """构建只包含授权工具执行的文件分析子图。"""
    graph = StateGraph(AnalysisState)
    graph.add_node("execute", file_analysis_node)
    graph.set_entry_point("execute")
    graph.add_edge("execute", END)
    return graph.compile()
