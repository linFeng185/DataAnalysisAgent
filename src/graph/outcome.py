"""工作流统一结果状态和公开错误文本。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal


TaskStatus = Literal["success", "failed", "needs_input", "partial"]


_ERROR_MESSAGES = {
    "VALIDATION_FAILED": "查询未通过安全校验",
    "EXPLAIN_FAILED": "查询未通过执行计划校验",
    "SQL_EXECUTION_FAILED": "查询执行失败，请检查数据源、权限或查询条件",
    "MCP_AGENT_FAILED": "工具分析失败，请检查输入和已授权工具",
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


# 方法作用：从新旧响应字段中解析统一任务状态。
# Args: response - 最终响应或历史恢复响应。
# Returns: success、failed、needs_input 或 partial 状态。
def status_from_response(response: dict[str, Any]) -> TaskStatus:
    """从统一响应读取状态，并兼容旧响应中的 success 字段。"""
    status = str(response.get("status", "") or "").strip()
    if status == "needs_input" or response.get("needs_time_range"):
        return "needs_input"
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
