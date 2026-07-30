"""市场研究独立能力子图。"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from src.graph.nodes.mcp_agent import mcp_agent_node
from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


async def market_research_node(state: AnalysisState) -> dict:
    """使用显式授权的搜索/MCP 工具生成带来源标识的研究简报。"""
    logger.info("市场研究子图执行开始")
    result = await mcp_agent_node(state)
    response = dict(result.get("final_response", {}) or {})
    response["source"] = "market_research"
    analysis = dict(response.get("analysis", {}) or {})
    limitations = list(analysis.get("limitations", []) or [])
    limitations.append("市场研究仅基于当前请求授权且实际返回的外部证据。")
    analysis["limitations"] = limitations
    response["analysis"] = analysis
    result["analysis_result"] = analysis
    result["final_response"] = response
    logger.info("市场研究子图执行完成", success=bool(response.get("success")))
    return result


def build_market_research_subgraph():
    """构建市场研究工具执行子图。"""
    graph = StateGraph(AnalysisState)
    graph.add_node("research", market_research_node)
    graph.set_entry_point("research")
    graph.add_edge("research", END)
    return graph.compile()
