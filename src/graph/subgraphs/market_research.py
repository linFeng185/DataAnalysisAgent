"""市场研究独立能力子图。"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from src.graph.nodes.mcp_agent import mcp_agent_node
from src.graph.outcome import public_error_message
from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


# 方法作用：执行市场研究并强制要求结果包含真实工具证据。
# Args: state - 当前 LangGraph 分析状态。
# Returns: 带来源、限制说明和证据校验状态的响应增量。
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
    citations = [
        citation
        for citation in list(analysis.get("citations", []) or [])
        if isinstance(citation, dict)
        and str(citation.get("source_id", "") or "").strip()
        and isinstance(citation.get("locator"), dict)
        and bool(citation.get("locator"))
    ]
    analysis["citations"] = citations
    if response.get("success") and not citations:
        logger.warning("市场研究证据校验失败", reason="工具执行结果不含可追溯引用")
        error_code = "MARKET_RESEARCH_EVIDENCE_MISSING"
        response["success"] = False
        response["status"] = "failed"
        response["error_code"] = error_code
        response["error_message"] = public_error_message(error_code)
        analysis["summary"] = public_error_message(error_code)
    response["analysis"] = analysis
    result["analysis_result"] = analysis
    result["final_response"] = response
    logger.info("市场研究子图执行完成", success=bool(response.get("success")))
    return result


# 方法作用：构建只包含市场研究执行节点的独立能力子图。
# Args: 无参数。
# Returns: 编译后的 LangGraph 子图。
def build_market_research_subgraph():
    """构建市场研究工具执行子图。"""
    graph = StateGraph(AnalysisState)
    graph.add_node("research", market_research_node)
    graph.set_entry_point("research")
    graph.add_edge("research", END)
    return graph.compile()
