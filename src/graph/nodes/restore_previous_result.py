"""跨轮结构化结果恢复节点。"""

from __future__ import annotations

from copy import deepcopy

from src.graph.context import read_contexts
from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


# 方法作用：校验当前数据源上下文并恢复上一轮结构化查询结果。
# Args: state - 包含当前数据源和 previous_turn_snapshot 的 LangGraph 状态。
# Returns: 可供 analyze_result 使用的当前轮结果字段，或明确的不可恢复说明。
async def restore_previous_result_node(state: AnalysisState) -> dict:
    """只为明确的 meta 追问恢复同一数据源、同一会话中的上一轮结果。"""
    contexts = read_contexts(state)
    logger.debug(
        "上一轮结果恢复入口",
        session_id=contexts.request.session_id[:20],
        datasource=contexts.routing.datasource,
    )
    snapshot = state.get("previous_turn_snapshot", {}) or {}
    current_sources = set(contexts.routing.selected_datasources)
    if not current_sources and contexts.routing.datasource:
        current_sources = {contexts.routing.datasource}
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

    rich_result: dict = {}
    session_id = contexts.request.session_id
    if session_id:
        try:
            from src.memory.history_store import get_history_store

            history = await get_history_store().list_session(session_id, limit=1)
            if history:
                candidate = history[-1].get("final_result", {}) or {}
                if isinstance(candidate, dict):
                    rich_result = candidate
            logger.info(
                "上一轮富结果读取完成",
                session_id=session_id[:20],
                found=bool(rich_result),
            )
        except Exception as exc:
            logger.error(
                "上一轮富结果读取失败",
                session_id=session_id[:20],
                error=str(exc),
                exc_info=True,
            )

    # 兼容升级前已经保存富结果的旧 checkpoint。
    rows = rich_result.get("data", snapshot.get("query_result_sample", [])) or []
    statements = rich_result.get("sql_statements", []) or []
    generated_sql = (
        rich_result.get("sql", "")
        or snapshot.get("generated_sql", "")
        or ""
    )
    if not rich_result and not rows and not snapshot.get("query_result_sample"):
        logger.warning("上一轮结果恢复跳过", reason="持久化富结果不存在")
        return _unavailable_result("上一轮结果明细已不可用，请重新执行数据查询。")

    result = {
        "previous_result_restored": True,
        "generated_sql": generated_sql,
        "query_result_sample": deepcopy(rows),
        "query_result_full_count": int(
            rich_result.get(
                "row_count", snapshot.get("query_result_full_count", len(rows)),
            ) or 0
        ),
        "query_result_truncated": bool(
            rich_result.get(
                "truncated", snapshot.get("query_result_truncated", False),
            )
        ),
        "query_result_statistics": deepcopy(snapshot.get("query_result_statistics", {}) or {}),
        "multi_source_results": [
            {
                "success": True,
                "datasource": statement.get("datasource", ""),
                "dialect": statement.get("dialect", ""),
                "sql": statement.get("sql", ""),
            }
            for statement in statements
            if isinstance(statement, dict) and statement.get("sql")
        ],
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
