"""跨轮结构化结果恢复节点。"""

from __future__ import annotations

from copy import deepcopy

from src.graph.state import AnalysisState
from src.logging_config import get_logger


logger = get_logger(__name__)


# 方法作用：校验当前数据源上下文并恢复上一轮结构化查询结果。
# Args: state - 包含当前数据源和 previous_turn_snapshot 的 LangGraph 状态。
# Returns: 可供 analyze_result 使用的当前轮结果字段，或明确的不可恢复说明。
async def restore_previous_result_node(state: AnalysisState) -> dict:
    """只为明确的 meta 追问恢复同一数据源、同一会话中的上一轮结果。"""
    snapshot = state.get("previous_turn_snapshot", {}) or {}
    current_sources = set(state.get("selected_datasources", []) or [])
    if not current_sources and state.get("datasource"):
        current_sources = {state.get("datasource", "")}
    snapshot_sources = set(snapshot.get("selected_datasources", []) or [])
    if not snapshot_sources and snapshot.get("datasource"):
        snapshot_sources = {snapshot.get("datasource", "")}
    logger.info(
        "上一轮结果恢复边界输入",
        current_sources=sorted(current_sources),
        snapshot_sources=sorted(snapshot_sources),
        result_available=bool(snapshot.get("result_available")),
        snapshot_rows=len(snapshot.get("query_result_sample", []) or []),
    )

    if not snapshot.get("result_available"):
        logger.warning("上一轮结果恢复跳过", reason="上一轮没有可复用的查询结果")
        return _unavailable_result("上一轮没有可复用的查询结果，请先完成一次数据查询。")
    if current_sources != snapshot_sources:
        logger.warning(
            "上一轮结果恢复跳过",
            reason="数据源已切换",
            current_sources=sorted(current_sources),
            snapshot_sources=sorted(snapshot_sources),
        )
        return _unavailable_result("数据源已切换，不能复用上一数据源的查询结果，请重新发起查询。")

    result = {
        "previous_result_restored": True,
        "generated_sql": snapshot.get("generated_sql", "") or "",
        "query_result_sample": deepcopy(snapshot.get("query_result_sample", []) or []),
        "query_result_full_count": int(snapshot.get("query_result_full_count", 0) or 0),
        "query_result_truncated": bool(snapshot.get("query_result_truncated", False)),
        "query_result_statistics": deepcopy(snapshot.get("query_result_statistics", {}) or {}),
        "multi_source_results": deepcopy(snapshot.get("multi_source_results", []) or []),
    }
    logger.info(
        "上一轮结果恢复完成",
        rows=len(result["query_result_sample"]),
        full_count=result["query_result_full_count"],
        has_sql=bool(result["generated_sql"]),
    )
    return result


# 方法作用：构造无法恢复上一轮结果时的统一分析响应。
# Args: message - 面向用户的不可恢复原因。
# Returns: 供 build_response 直接消费的空结果状态。
def _unavailable_result(message: str) -> dict:
    """返回成功结束但不携带旧数据的澄清响应。"""
    logger.debug("构造上一轮结果不可用响应入口", message=message)
    result = {
        "previous_result_restored": False,
        "generated_sql": "",
        "query_result_sample": [],
        "query_result_full_count": 0,
        "query_result_truncated": False,
        "query_result_statistics": {},
        "analysis_result": {
            "summary": message,
            "insights": [],
            "recommended_chart_type": "table",
            "follow_up_questions": [],
        },
        "chart_config": {"type": "table", "option": {}},
    }
    logger.info("构造上一轮结果不可用响应完成")
    return result
