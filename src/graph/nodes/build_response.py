"""4.10 build_response Node — 组装最终响应。"""

from __future__ import annotations

import time

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


async def build_response_node(state: AnalysisState) -> dict:
    _start = time.monotonic()
    logger.info("节点开始", node="build_response")
    if state.get("validation_errors"):
        logger.info("节点完成", node="build_response", elapsed_ms=round((time.monotonic() - _start) * 1000))
        return {"final_response": {
            "success": False,
            "error_code": "VALIDATION_FAILED",
            "error_message": str(state["validation_errors"]),
            "user_query": state.get("user_query", ""),
        }}
    result = {
        "success": True,
        "session_id": "",
        "user_query": state.get("user_query", ""),
        "sql": state.get("generated_sql", ""),
        "data": state.get("query_result_sample", []),
        "analysis": state.get("analysis_result", {}),
        "chart": state.get("chart_config", {}),
    }
    reasoning = state.get("sql_reasoning_content", "")
    if reasoning:
        result["sql_reasoning_content"] = reasoning
    logger.info("节点完成", node="build_response", elapsed_ms=round((time.monotonic() - _start) * 1000))
    return {"final_response": result}
