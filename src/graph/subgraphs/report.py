"""自定义报告独立能力子图。"""

from __future__ import annotations

from decimal import Decimal

from langgraph.graph import END, StateGraph

from src.graph.context import read_contexts
from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


async def report_node(state: AnalysisState) -> dict:
    """确保 custom-report 真实执行并把报告写入分析结果。"""
    contexts = read_contexts(state)
    analysis = dict(state.get("analysis_result", {}) or {})
    if analysis.get("rendered_report"):
        logger.info("报告子图复用分析后 Skill 输出")
        return {"analysis_result": analysis}
    tools = [tool for tool in (state.get("skill_tools", []) or []) if getattr(tool, "name", "") == "render_report"]
    if not tools:
        from src.skill_manager import get_skill_manager

        manager = get_skill_manager()
        skill = manager.get_skill(
            "custom-report",
            tenant_id=contexts.request.tenant_id,
            user_id=contexts.request.user_id,
        )
        if skill is not None:
            tools = manager.get_active_tools([skill])
    if not tools:
        logger.error("报告子图缺少 custom-report 工具")
        return {
            "final_response": {
                "success": False,
                "status": "failed",
                "source": "report",
                "user_query": contexts.request.user_query,
                "error_code": "REPORT_TOOL_UNAVAILABLE",
                "error_message": "报告渲染工具当前不可用",
                "sql": state.get("generated_sql", ""),
                "data": state.get("query_result_sample", []) or [],
                "analysis": {"summary": "报告渲染工具当前不可用"},
                "chart": {"type": "table", "option": {}},
            }
        }
    rows = list(state.get("query_result_sample", []) or [])
    metrics = {
        key: value
        for key, value in (rows[0] if rows else {}).items()
        if not isinstance(value, bool) and isinstance(value, (int, float, Decimal))
    }
    template = "monthly_report" if "月报" in contexts.request.user_query else "weekly_report"
    report = await tools[0].ainvoke({
        "template": template,
        "data": {
            "title": contexts.request.user_query or "数据分析报告",
            "summary": str(analysis.get("summary", "") or ""),
            "insights": list(analysis.get("insights", []) or []),
            "metrics": metrics,
        },
    })
    analysis["rendered_report"] = str(report)
    logger.info("报告子图执行完成", template=template, report_chars=len(str(report)))
    return {"analysis_result": analysis}


def build_report_subgraph():
    """构建报告渲染子图。"""
    graph = StateGraph(AnalysisState)
    graph.add_node("render", report_node)
    graph.set_entry_point("render")
    graph.add_edge("render", END)
    return graph.compile()
