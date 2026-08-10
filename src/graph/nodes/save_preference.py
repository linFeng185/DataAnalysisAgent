"""显式用户偏好写入长期记忆的工作流节点。"""

from __future__ import annotations

import hashlib
import re

from src.graph.context import read_contexts
from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)

_CHART_TYPES = {
    "折线图": "line",
    "柱状图": "bar",
    "条形图": "bar",
    "饼图": "pie",
    "散点图": "scatter",
    "热力图": "heatmap",
    "表格": "table",
}


# 方法作用：从显式“记住”命令中提取受控偏好键和值。
# Args: query - 用户原始偏好命令。
# Returns: 偏好键和值；内容为空或不是显式记忆命令时返回 None。
def parse_preference_command(query: str) -> tuple[str, str] | None:
    """常见图表和语言偏好规范化，其他内容使用稳定私有键保存原文。"""
    normalized = str(query or "").strip()
    if "记住" not in normalized:
        return None
    content = re.sub(
        r"^.*?记住(?:我)?(?:的)?(?:偏好|喜欢|习惯|默认)?[：:\s]*",
        "",
        normalized,
        count=1,
    ).strip(" ，。；;：:")
    if not content:
        return None
    for label, value in _CHART_TYPES.items():
        if label in content:
            return "chart_type", value
    if "中文" in content:
        return "response_language", "zh-CN"
    if "英文" in content or "英语" in content:
        return "response_language", "en"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return f"custom_instruction_{digest}", content[:500]


# 方法作用：把当前用户显式声明的偏好持久化为私有长期记忆。
# Args: state - 包含用户问题和可信身份的 LangGraph 状态。
# Returns: 可由统一响应出口消费的偏好保存结果。
async def save_preference_node(state: AnalysisState) -> dict:
    """偏好写入不经过 LLM 推断，只处理用户明确要求记住的内容。"""
    contexts = read_contexts(state)
    parsed = parse_preference_command(contexts.request.user_query)
    if parsed is None:
        message = "请说明需要记住的具体偏好"
        logger.info("用户偏好写入等待补充", user_id=contexts.request.user_id)
        return _preference_response(contexts.request.user_query, message, success=False, status="needs_input")
    if contexts.request.tenant_id <= 0 or contexts.request.user_id <= 0:
        logger.warning("用户偏好写入拒绝", reason="缺少可信身份")
        return _preference_response(
            contexts.request.user_query,
            "当前身份不可用于保存长期偏好",
            success=False,
            status="failed",
        )

    preference, value = parsed
    try:
        from src.memory.long_term_store import get_long_term_memory_store
        from src.security.tenant_policy import RequestIdentity

        identity = RequestIdentity(
            tenant_id=contexts.request.tenant_id,
            user_id=contexts.request.user_id,
            role=contexts.request.user_role or "analyst",
        )
        store = await get_long_term_memory_store()
        await store.save_preference(
            contexts.request.user_id,
            preference,
            value,
            identity=identity,
        )
    except Exception as exc:
        logger.error(
            "用户偏好写入失败",
            tenant_id=contexts.request.tenant_id,
            user_id=contexts.request.user_id,
            preference=preference,
            error=str(exc),
            exc_info=True,
        )
        return _preference_response(
            contexts.request.user_query,
            "偏好保存失败，请稍后重试",
            success=False,
            status="failed",
        )
    logger.info(
        "用户偏好写入完成",
        tenant_id=contexts.request.tenant_id,
        user_id=contexts.request.user_id,
        preference=preference,
    )
    return _preference_response(
        contexts.request.user_query,
        "已记住该偏好，后续分析会优先参考",
        success=True,
        status="success",
    )


# 方法作用：构造偏好写入节点的统一最终响应。
# Args: query - 用户原始问题；message - 用户提示；success - 是否保存成功；status - 任务状态。
# Returns: 包含 final_response 和 analysis_result 的状态增量。
def _preference_response(query: str, message: str, *, success: bool, status: str) -> dict:
    """保持偏好命令与其他非 SQL 能力一致的响应字段。"""
    response = {
        "success": success,
        "status": status,
        "source": "long_term_memory",
        "user_query": query,
        "sql": "",
        "data": [],
        "analysis": {
            "summary": message,
            "insights": [],
            "recommended_chart_type": "table",
        },
        "chart": {"type": "table", "option": {}},
    }
    if not success:
        response["error_code"] = (
            "MEMORY_PREFERENCE_MISSING" if status == "needs_input" else "MEMORY_WRITE_FAILED"
        )
        response["error_message"] = message
    return {"final_response": response, "analysis_result": dict(response["analysis"])}
