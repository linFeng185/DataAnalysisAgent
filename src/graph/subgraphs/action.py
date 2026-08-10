"""外部动作独立能力子图。"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from src.actions.contracts import ActionRequest, get_action_registry
from src.graph.context import read_contexts
from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


# 方法作用：校验并分派结构化外部动作，未确认请求停留在人工确认状态。
# Args: state - 当前 LangGraph 分析状态及可选 action_request。
# Returns: 包含动作状态、执行结果和用户提示的统一响应。
async def action_node(state: AnalysisState) -> dict:
    """仅执行结构化且已确认的动作；自然语言请求停在人工确认。"""
    contexts = read_contexts(state)
    raw_request = dict(state.get("action_request", {}) or {})
    logger.info(
        "外部动作请求边界",
        has_request=bool(raw_request),
        action_name=str(raw_request.get("action_name", "") or ""),
        confirmed=bool(raw_request.get("confirmed", False)),
    )
    if not raw_request:
        status = "confirmation_required"
        message = "外部动作需要明确动作参数、幂等键和人工确认"
        result_data = None
    else:
        try:
            request = ActionRequest(
                action_name=str(raw_request.get("action_name", "")),
                payload=dict(raw_request.get("payload", {}) or {}),
                idempotency_key=str(raw_request.get("idempotency_key", "")),
                confirmed=bool(raw_request.get("confirmed", False)),
                metadata=dict(raw_request.get("metadata", {}) or {}),
            )
            dispatched = await get_action_registry().dispatch(request)
            status = dispatched.status
            message = dispatched.message or "外部动作处理完成"
            result_data = dispatched.result
        except (TypeError, ValueError) as exc:
            logger.warning("外部动作请求格式无效", error=str(exc))
            status = "rejected"
            message = "外部动作请求格式无效"
            result_data = None
    success = status in {"executed", "already_executed"}
    response_status = "needs_confirmation" if status == "confirmation_required" else (
        "success" if success else "failed"
    )
    logger.info("外部动作子图执行完成", action_status=status)
    response_data = {"action_status": status, "result": result_data}
    if status == "confirmation_required" and raw_request:
        response_data["action_request"] = raw_request
    return {
        "final_response": {
            "success": success,
            "status": response_status,
            "source": "external_action",
            "user_query": contexts.request.user_query,
            "sql": "",
            "data": response_data,
            "analysis": {"summary": message, "recommended_chart_type": "table"},
            "chart": {"type": "table", "option": {}},
        }
    }


# 方法作用：构建人工确认和幂等约束下的外部动作子图。
# Args: 无参数。
# Returns: 编译后的 LangGraph 子图。
def build_action_subgraph():
    """构建人工确认和幂等约束下的外部动作子图。"""
    graph = StateGraph(AnalysisState)
    graph.add_node("dispatch", action_node)
    graph.set_entry_point("dispatch")
    graph.add_edge("dispatch", END)
    return graph.compile()
