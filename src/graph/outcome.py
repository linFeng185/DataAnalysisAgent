"""工作流统一结果状态和公开错误文本。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

TaskStatus = Literal["success", "failed", "needs_input", "needs_confirmation", "partial"]


_ERROR_MESSAGES = {
    "VALIDATION_FAILED": "查询未通过安全校验",
    "EXPLAIN_FAILED": "查询未通过执行计划校验",
    "LLM_UNAVAILABLE": "当前租户未配置可用的 LLM，请先在管理后台配置并启用模型",
    "SQL_EXECUTION_FAILED": "查询执行失败，请检查数据源、权限或查询条件",
    "MCP_AGENT_FAILED": "工具分析失败，请检查输入和已授权工具",
    "MARKET_RESEARCH_EVIDENCE_MISSING": "市场研究未获得可追溯证据，无法生成可靠结论",
    "MULTI_SOURCE_FAILED": "所有数据源查询均失败，请检查数据源状态和访问权限",
    "MULTI_SOURCE_PARTIAL": "部分数据源查询失败，当前结果仅包含成功的数据源",
    "STREAM_FAILED": "流式处理失败，请稍后重试",
}


# 方法作用：把内部错误码转换为稳定且脱敏的用户提示。
# Args: error_code - 内部错误码；fallback - 未知错误码的默认提示。
# Returns: 可公开返回给客户端的错误文本。
def public_error_message(error_code: str, *, fallback: str = "处理失败") -> str:
    """把内部错误转换为稳定的用户可见文本，详细信息只进入服务端日志。"""
    return _ERROR_MESSAGES.get(error_code, fallback)


# 方法作用：把内部执行错误类型转换成可写入历史的脱敏失败原因。
# Args: error_code - 统一错误码；error_type - 执行阶段分类。
# Returns: 不包含连接串、凭证和堆栈的用户可见原因。
def public_failure_reason(error_code: str, error_type: str = "") -> str:
    reasons = {
        "rate_limit": "请求频率超过限制",
        "security": "查询涉及未授权字段或被安全策略拦截",
        "sql_semantic": "SQL 与当前 Schema 或字段语义不匹配",
        "transient": "数据源暂时不可用或查询超时",
        "configuration": "数据源配置、认证或连接失败",
        "llm_unavailable": _ERROR_MESSAGES["LLM_UNAVAILABLE"],
    }
    return reasons.get(str(error_type or "").strip(), public_error_message(error_code))


# 方法作用：从新旧响应字段中解析统一任务状态。
# Args: response - 最终响应或历史恢复响应。
# Returns: success、failed、needs_input、needs_confirmation 或 partial 状态。
def status_from_response(response: dict[str, Any]) -> TaskStatus:
    """从统一响应读取状态，并兼容旧响应中的 success 字段。"""
    status = str(response.get("status", "") or "").strip()
    if status == "needs_input" or response.get("needs_time_range"):
        return "needs_input"
    if status == "needs_confirmation":
        return "needs_confirmation"
    # 旧数据可能同时存在互相矛盾的 status/success；显式失败必须优先，避免历史伪成功。
    if "success" in response and not bool(response.get("success")):
        return "failed"
    if status in {"success", "failed", "partial"}:
        return status  # type: ignore[return-value]
    return "success" if bool(response.get("success", False)) else "failed"


# 方法作用：判断响应是否可作为成功或部分成功结果处理。
# Args: response - 待判断的统一响应。
# Returns: success/partial 返回 True，其余状态返回 False。
def is_successful_response(response: dict[str, Any]) -> bool:
    """统一判断历史和审计使用的结果成败。"""
    return status_from_response(response) in {"success", "partial"}


def build_decision_summary(
    state: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
    *,
    max_chars: int = 320,
) -> str:
    """基于受控状态字段生成简短处理摘要，不暴露模型原始推理。"""
    state = state or {}
    response = response or {}
    source = str(response.get("source") or "").strip()
    status = status_from_response(response)
    labels = {
        "sql_query": "数据查询",
        "multi_source_query": "多数据源查询",
        "llm_direct": "直接回答",
        "mcp_agent": "工具分析",
        "prompt": "参数补充",
    }
    parts: list[str] = []
    if source in labels:
        parts.append(f"已完成{labels[source]}处理")
    elif source:
        parts.append("已完成当前任务处理")
    if status == "needs_input":
        parts.append("等待补充必要参数")
    elif status == "needs_confirmation":
        parts.append("等待人工确认")
    elif status == "failed":
        parts.append("处理未完成")
    elif status == "partial":
        parts.append("部分数据源完成")
    row_count = response.get("row_count", state.get("query_result_full_count", 0))
    if isinstance(row_count, int) and row_count > 0:
        parts.append(f"返回{row_count}行结果")
    generation_retries = max(int(state.get("retry_count", 0) or 0) - 1, 0)
    execution_retries = int(state.get("execution_retry_count", 0) or 0)
    retry_count = max(generation_retries, execution_retries)
    if retry_count > 0:
        parts.append(f"已自动重试{min(retry_count, 9)}次")
    skills = state.get("activated_skills") or response.get("activated_skills") or []
    if skills:
        parts.append(f"已应用{min(len(skills), 5)}项业务能力")
    if not parts:
        parts.append("已完成请求处理")
    result = "；".join(parts)
    return result if len(result) <= max_chars else result[: max_chars - 1] + "…"


_PRIVATE_OUTPUT_KEYS = frozenset({
    "sql_reasoning_content",
    "analysis_reasoning_content",
    "reasoning_content",
})


# 方法作用：递归复制公开输出并移除内部推理字段。
# Args: value - 任意响应、列表或标量值。
# Returns: 已脱敏的深拷贝值。
def sanitize_public_output(value: Any) -> Any:
    """递归移除历史响应中的内部推理字段，兼容旧数据恢复。"""
    if isinstance(value, dict):
        return {
            key: sanitize_public_output(item)
            for key, item in value.items()
            if key not in _PRIVATE_OUTPUT_KEYS
        }
    if isinstance(value, list):
        return [sanitize_public_output(item) for item in value]
    return deepcopy(value)
