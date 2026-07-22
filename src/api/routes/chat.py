"""聊天与 SSE 入口路由。"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException

from src.api.schemas import ChatRequest, ChatResponse
from src.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()
_started_at = time.time()


# 方法作用：规范化聊天请求中用户显式选择的数据源顺序并去重。
# Args: req - 聊天请求模型。
# Returns: 用户显式选择的数据源列表；空列表表示进入授权候选自动发现。
def _requested_chat_datasources(req: ChatRequest) -> list[str]:
    logger.debug(
        "规范化聊天数据源入口",
        datasource=req.datasource,
        datasource_count=len(req.datasources),
    )
    raw = req.datasources if req.datasources else ([req.datasource] if req.datasource else [])
    result = list(dict.fromkeys(str(name).strip() for name in raw if str(name).strip()))
    logger.info("规范化聊天数据源完成", selected_count=len(result), discovery=not result)
    return result


# 方法作用：在任何权限、Schema 或 LLM 工作前校验聊天资源预算并计入用户配额。
# Args: req - 聊天请求模型。
# Returns: 校验通过时返回 None；超限时抛出 HTTP 413/429。
def _enforce_chat_request_quota(req: ChatRequest) -> None:
    from src.api.auth import get_current_user_id
    from src.config import get_settings
    from src.security.data_masker import check_rate_limit

    settings = get_settings()
    requested = _requested_chat_datasources(req)
    query_length = len(req.query)
    max_query_chars = max(1, int(getattr(settings, "max_query_chars", 8_000)))
    max_datasources = max(1, int(getattr(settings, "max_datasources_per_query", 5)))
    logger.debug(
        "校验聊天请求配额入口",
        user_id=get_current_user_id(),
        query_length=query_length,
        datasource_count=len(requested),
    )
    if query_length > max_query_chars:
        logger.warning(
            "聊天请求字符数超限",
            query_length=query_length,
            limit=max_query_chars,
        )
        raise HTTPException(413, f"查询内容不能超过 {max_query_chars} 个字符")
    if len(requested) > max_datasources:
        logger.warning(
            "聊天请求数据源数超限",
            datasource_count=len(requested),
            limit=max_datasources,
        )
        raise HTTPException(413, f"单次查询最多选择 {max_datasources} 个数据源")
    if not check_rate_limit(user_id=get_current_user_id()):
        logger.warning("聊天请求频率超限", user_id=get_current_user_id())
        raise HTTPException(429, "请求频率超限，请稍后重试")
    logger.info(
        "校验聊天请求配额完成",
        user_id=get_current_user_id(),
        query_length=query_length,
        datasource_count=len(requested),
    )


# 方法作用：在进入 LangGraph 前解析当前身份可访问的数据源和行列权限。
# Args: req - 聊天请求模型。
# Returns: 以数据源名为键的授权快照；空候选或越权时抛出 HTTP 403。
async def _resolve_chat_access(req: ChatRequest) -> dict[str, dict]:
    """显式选择和自动发现共用同一授权边界。

    Args:
        req: 当前聊天请求。

    Returns:
        当前用户可以交给模型和 SQL 工作流的数据源权限映射。
    """
    from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
    from src.app_context import get_tenant_policy
    from src.security.permission_check import resolve_datasource_access

    policy = get_tenant_policy()
    isolation_enabled = policy.datasource_isolation_enabled
    requested = _requested_chat_datasources(req)
    logger.debug(
        "解析聊天数据源权限入口",
        tenant_id=get_current_tenant_id(),
        user_id=get_current_user_id(),
        requested_count=len(requested),
        tenant_isolation=isolation_enabled,
    )
    if not isolation_enabled and requested:
        result = {
            name: {
                "name": name,
                "description": "",
                "allowed_columns": [],
                "row_filter_sql": "",
                "access_level": "read",
            }
            for name in requested
        }
        logger.info("解析聊天数据源权限完成", authorized_count=len(result), mode="single_tenant")
        return result
    try:
        import src.api.routes as routes_package

        available = await routes_package._registry().list_all()
        result = await resolve_datasource_access(
            available,
            requested,
            tenant_id=get_current_tenant_id(),
            user_id=get_current_user_id(),
            role=get_current_role(),
            tenant_policy=policy,
        )
        logger.info(
            "解析聊天数据源权限完成",
            authorized_count=len(result),
            discovery=not requested,
        )
        return result
    except PermissionError as exc:
        logger.warning("解析聊天数据源权限拒绝", error=str(exc))
        raise HTTPException(403, str(exc)) from exc


# ---- Chat (11.1.1-2) ----

@router.post("/chat")
async def chat(req: ChatRequest):
    """统一 chat 端点：stream=False 返回 JSON，stream=True 返回 SSE 流式。"""
    _enforce_chat_request_quota(req)
    selected_datasources = _requested_chat_datasources(req)
    import src.api.routes as routes_package

    datasource_access = await routes_package._resolve_chat_access(req)
    primary_access = datasource_access.get(req.datasource, {}) if req.datasource else {}
    logger.debug(
        "Chat 请求入口",
        datasource=req.datasource,
        selected_count=len(selected_datasources),
        stream=req.stream,
    )
    if req.stream:
        from fastapi.responses import StreamingResponse
        from src.api.streaming import stream_analysis
        from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
        logger.info(
            "Chat 流式响应已创建",
            datasource=req.datasource,
            selected_count=len(selected_datasources),
        )
        return StreamingResponse(
            stream_analysis(
                req.query, req.datasource, req.session_id or "", req.datasources,
                tenant_id=get_current_tenant_id(),
                user_id=get_current_user_id(),
                user_role=get_current_role(),
                datasource_access=datasource_access,
                request_rate_limit_checked=True,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    import uuid as _uuid
    sid = req.session_id or str(_uuid.uuid4())
    # 非流式也保存会话元数据
    from src.api.background_tasks import create_background_task
    try:
        from src.memory.session_store import get_session_store
        if req.session_id:
            create_background_task(
                get_session_store().touch(sid, req.datasource, req.query),
                name="touch-chat-session",
                context={"session_id": sid[:20]},
            )
        else:
            create_background_task(
                get_session_store().create(sid, req.datasource, req.query),
                name="create-chat-session",
                context={"session_id": sid[:20]},
            )
    except Exception as exc:
        logger.error(
            "非流式会话元数据任务创建失败",
            session_id=sid[:20],
            error=str(exc),
            exc_info=True,
        )
    from src.api.auth import scope_thread_id
    from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
    cfg = {"configurable": {"thread_id": scope_thread_id(sid)}}
    result = await routes_package._app().ainvoke({
        "user_query": req.query,
        "datasource": req.datasource,
        "session_id": sid,
        "selected_datasources": selected_datasources,
        "datasource_access": datasource_access,
        "allowed_columns": list(primary_access.get("allowed_columns", []) or []),
        "row_filter_sql": str(primary_access.get("row_filter_sql", "") or ""),
        "tenant_id": get_current_tenant_id(),
        "user_id": get_current_user_id(),
        "user_role": get_current_role(),
        "request_rate_limit_checked": True,
    }, cfg)
    f = result.get("final_response", {})
    return ChatResponse(
        success=f.get("success", True), session_id=sid,
        user_query=req.query,
        sql=f.get("sql", result.get("generated_sql", "")),
        sql_statements=f.get("sql_statements", []),
        data=f.get("data", result.get("query_result_sample", [])),
        row_count=f.get("row_count", result.get("query_result_full_count", 0)),
        truncated=bool(f.get("truncated", result.get("query_result_truncated", False))),
        analysis=f.get("analysis", result.get("analysis_result", {})),
        chart=f.get("chart", result.get("chart_config", {})),
    )


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """（保留向后兼容）独立流式端点，等价于 /chat + stream=True。"""
    _enforce_chat_request_quota(req)
    logger.debug(
        "兼容流式 Chat 入口",
        datasource=req.datasource,
        selected_count=len(req.datasources or [req.datasource]),
    )
    from fastapi.responses import StreamingResponse
    from src.api.streaming import stream_analysis
    from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
    import src.api.routes as routes_package

    datasource_access = await routes_package._resolve_chat_access(req)
    logger.info("兼容流式 Chat 响应已创建", datasource=req.datasource)
    return StreamingResponse(
        stream_analysis(
            req.query,
            req.datasource,
            req.session_id or "",
            req.datasources,
            tenant_id=get_current_tenant_id(),
            user_id=get_current_user_id(),
            user_role=get_current_role(),
            datasource_access=datasource_access,
            request_rate_limit_checked=True,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
