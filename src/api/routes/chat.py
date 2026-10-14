"""聊天与 SSE 入口路由。"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, File, HTTPException, UploadFile

from src.api.routes._helpers import _call_with_optional_tenant
from src.api.schemas import ChatRequest, ChatResponse
from src.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()
_started_at = time.time()
_CHAT_ATTACHMENT_EXTENSIONS = frozenset({
    ".csv", ".xlsx", ".xls", ".parquet", ".pdf", ".docx", ".txt", ".md", ".markdown",
    ".png", ".jpg", ".jpeg", ".webp",
})

if TYPE_CHECKING:
    from src.llm.tenant_config import TenantLLMSelection


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
async def _enforce_chat_request_quota(req: ChatRequest) -> None:
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
    if not await check_rate_limit(user_id=get_current_user_id()):
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

        registry = routes_package._registry()
        available = await _call_with_optional_tenant(
            registry.list_all,
            tenant_id=get_current_tenant_id(),
        )
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


# 方法作用：在进入工作流前校验用户显式选择的 Skill 可见性和启用状态。
# Args: req - 聊天请求模型。
# Returns: 去重后保持顺序的 Skill 复合资源 ID。
def _validated_skill_ids(req: ChatRequest) -> list[str]:
    requested = list(dict.fromkeys(
        str(resource_id or "").strip()
        for resource_id in req.enabled_skill_ids
        if str(resource_id or "").strip()
    ))
    if not requested:
        return []
    from src.api.auth import get_current_tenant_id, get_current_user_id
    from src.skill_manager import get_skill_manager

    try:
        selected = get_skill_manager().resolve_requested_skills(
            requested,
            tenant_id=get_current_tenant_id(),
            user_id=get_current_user_id(),
        )
    except PermissionError as exc:
        logger.warning("聊天显式 Skill 授权拒绝", requested_count=len(requested))
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        logger.warning("聊天显式 Skill 状态无效", requested_count=len(requested))
        raise HTTPException(409, str(exc)) from exc
    result = [skill.resource_id for skill in selected]
    logger.info("聊天显式 Skill 校验完成", selected_count=len(result))
    return result


# 方法作用：为文件和结构化动作请求跳过不相关的数据源授权解析。
# Args: req - 已通过 Pydantic 校验的聊天请求。
# Returns: 文件/动作专用请求返回空快照，数据查询请求返回真实授权快照。
async def _resolve_chat_access_for_request(req: ChatRequest) -> dict[str, dict]:
    """没有显式数据源时，附件和外部动作应先进入各自的独立能力子图。"""
    requested = _requested_chat_datasources(req)
    if not requested and (req.file_ids or req.action_request is not None):
        logger.info(
            "Chat 跳过数据源解析",
            file_count=len(req.file_ids),
            has_action_request=req.action_request is not None,
        )
        return {}
    import src.api.routes as routes_package

    return await routes_package._resolve_chat_access(req)


# 方法作用：解析当前租户的显式或默认 LLM 选择，并执行多租户失败关闭策略。
# Args: req - 包含可选连接和模型的聊天请求。
# Returns: 已授权租户选择；未配置时抛出冲突异常。
async def _resolve_chat_llm_selection(req: ChatRequest) -> TenantLLMSelection:
    from src.api.auth import get_current_tenant_id
    from src.llm.tenant_config import resolve_tenant_llm_selection

    tenant_id = get_current_tenant_id()
    explicit = req.llm_connection_id is not None
    logger.debug(
        "解析 Chat LLM 选择入口",
        tenant_id=tenant_id,
        explicit=explicit,
        connection_id=req.llm_connection_id or 0,
        model_id=req.model_id,
    )
    try:
        selection = await resolve_tenant_llm_selection(
            tenant_id=tenant_id,
            connection_id=req.llm_connection_id,
            model_id=req.model_id,
            reasoning_enabled=req.reasoning_enabled,
            reasoning_effort=req.reasoning_effort,
        )
    except (LookupError, ValueError) as exc:
        logger.warning(
            "解析 Chat LLM 选择拒绝",
            tenant_id=tenant_id,
            explicit=explicit,
            reason=str(exc),
        )
        raise HTTPException(409, "当前租户未配置可用的 LLM，请先在管理后台配置并启用模型") from exc
    logger.info(
        "解析 Chat LLM 选择完成",
        tenant_id=tenant_id,
        connection_id=selection.connection_id,
        model_id=selection.model_id,
    )
    return selection


# ---- Chat (11.1.1-2) ----


# 方法作用：读取并校验聊天附件的格式、单文件大小和请求累计大小。
# Args: files - 当前请求上传的附件；max_bytes - 单文件上限；max_total_bytes - 累计大小上限。
# Returns: 已通过校验的文件名、内容和字节数列表。
async def _read_chat_uploads(
    files: list[UploadFile],
    *,
    max_bytes: int,
    max_total_bytes: int,
) -> list[tuple[str, bytes, int]]:
    """在持久化前完成全部可逆校验，避免越限请求产生部分文件。"""
    logger.debug(
        "读取聊天附件入口",
        file_count=len(files),
        max_bytes=max_bytes,
        max_total_bytes=max_total_bytes,
    )
    prepared: list[tuple[str, bytes, int]] = []
    image_count = 0
    total_bytes = 0
    for uploaded_file in files:
        filename = os.path.basename(str(uploaded_file.filename or "").strip())
        if not filename:
            raise HTTPException(400, "附件文件名不能为空")
        extension = os.path.splitext(filename)[1].lower()
        if extension not in _CHAT_ATTACHMENT_EXTENSIONS:
            logger.warning("聊天附件格式拒绝", filename=filename, extension=extension)
            raise HTTPException(400, f"不支持的附件格式: {extension or filename}")
        content = await uploaded_file.read(max_bytes + 1)
        if len(content) > max_bytes:
            logger.warning("聊天附件单文件超限", filename=filename, size=len(content), limit=max_bytes)
            raise HTTPException(413, f"文件 '{filename}' 超过大小限制")
        total_bytes += len(content)
        if total_bytes > max_total_bytes:
            logger.warning("聊天附件累计大小超限", total_bytes=total_bytes, limit=max_total_bytes)
            raise HTTPException(413, "上传附件累计大小超过限制")
        from src.knowledge.image_assets import (
            IMAGE_EXTENSIONS,
            MAX_REQUEST_IMAGES,
            ImageAssetError,
            prepare_image_bytes,
        )

        if extension in IMAGE_EXTENSIONS:
            import asyncio

            image_count += 1
            if image_count > MAX_REQUEST_IMAGES:
                raise HTTPException(413, f"单次最多上传 {MAX_REQUEST_IMAGES} 张图片")
            try:
                await asyncio.to_thread(prepare_image_bytes, content)
            except ImageAssetError as exc:
                raise HTTPException(400, str(exc)) from None
        prepared.append((filename, content, len(content)))
    logger.info("读取聊天附件完成", accepted=len(prepared), total_bytes=total_bytes)
    return prepared


# 方法作用：逆序删除当前批次已经保存的聊天附件，补偿部分提交。
# Args: store - 当前 FileStore；saved_files - 已返回 file_id 的附件摘要。
# Returns: 无返回值，单项回滚失败时记录日志并继续其余项。
async def _rollback_chat_files(store, saved_files: list[dict[str, int | str]]) -> None:
    """回滚必须按精确 ID 执行，不能按文件名误删历史同名附件。"""
    logger.info("聊天附件回滚入口", saved_count=len(saved_files))
    for saved_file in reversed(saved_files):
        file_id = int(saved_file["file_id"])
        try:
            deleted = await store.delete_by_id(file_id, knowledge_scope="private")
            if not deleted:
                logger.error("聊天附件回滚未删除记录", file_id=file_id)
        except Exception as exc:
            logger.error(
                "聊天附件回滚失败",
                file_id=file_id,
                error_type=type(exc).__name__,
                exc_info=True,
            )
    logger.info("聊天附件回滚完成", attempted=len(saved_files))


@router.post("/chat/files")
# 方法作用：把聊天附件保存为当前用户私有文件并返回结构化 file_id。
# Args: files - multipart/form-data 中的附件列表。
# Returns: 可直接传入 ChatRequest.file_ids 的文件摘要列表。
async def upload_chat_files(files: list[UploadFile] = File(...)) -> dict:
    """附件不进入知识库索引，后续读取仍由 FileStore 身份过滤。"""
    from src.config import get_settings
    from src.knowledge.file_store import get_file_store

    settings = get_settings()
    max_upload_bytes = max(1, int(settings.max_upload_bytes))
    max_upload_files = min(10, max(1, int(getattr(settings, "max_upload_files", 10))))
    max_upload_total_bytes = max(
        max_upload_bytes,
        int(getattr(settings, "max_upload_total_bytes", 100 * 1024 * 1024)),
    )
    logger.info(
        "聊天附件上传入口",
        file_count=len(files),
        max_upload_files=max_upload_files,
    )
    if not files:
        raise HTTPException(400, "请至少选择一个附件")
    if len(files) > max_upload_files:
        logger.warning("聊天附件数量超限", file_count=len(files), limit=max_upload_files)
        raise HTTPException(413, f"单次最多上传 {max_upload_files} 个附件")

    prepared = await _read_chat_uploads(
        files,
        max_bytes=max_upload_bytes,
        max_total_bytes=max_upload_total_bytes,
    )
    store = get_file_store()
    saved_files: list[dict[str, int | str]] = []
    current_filename = ""
    try:
        for filename, content, size in prepared:
            current_filename = filename
            file_id = await store.save(filename, content, knowledge_scope="private")
            if file_id is None:
                raise RuntimeError("FileStore.save 未返回文件 ID")
            saved_files.append({"file_id": int(file_id), "filename": filename, "size": size})
    except Exception as exc:
        logger.error(
            "聊天附件批量持久化失败",
            filename=current_filename,
            saved_count=len(saved_files),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        await _rollback_chat_files(store, saved_files)
        raise HTTPException(503, "附件存储暂不可用") from exc
    logger.info("聊天附件上传完成", saved_count=len(saved_files))
    return {"files": saved_files, "total": len(saved_files)}

# 按请求模式执行对话并保留最终节点生成的公开展示元数据。
# Args: req - 已校验的对话请求，包含问题、数据源和流式选项。
# Returns: 非流式公开响应模型或 SSE 流式响应。
@router.post("/chat")
async def chat(req: ChatRequest):
    """统一 chat 端点：stream=False 返回 JSON，stream=True 返回 SSE 流式。"""
    await _enforce_chat_request_quota(req)
    enabled_skill_ids = _validated_skill_ids(req)
    selected_datasources = _requested_chat_datasources(req)
    import src.api.routes as routes_package

    datasource_access = await _resolve_chat_access_for_request(req)
    llm_selection = await _resolve_chat_llm_selection(req)
    primary_access = datasource_access.get(req.datasource, {}) if req.datasource else {}
    logger.debug(
        "Chat 请求入口",
        datasource=req.datasource,
        selected_count=len(selected_datasources),
        file_count=len(req.file_ids),
        has_action_request=req.action_request is not None,
        stream=req.stream,
    )
    if req.stream:
        from fastapi.responses import StreamingResponse

        from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
        from src.api.streaming import stream_analysis
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
                enabled_skill_ids=enabled_skill_ids,
                request_rate_limit_checked=True,
                llm_selection=llm_selection,
                file_ids=req.file_ids,
                action_request=req.action_request.model_dump() if req.action_request else None,
                display_language=req.display_language,
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
    # SQL 智能标记只由服务端聊天入口写入，请求正文无法覆写；报表入口不带该标记。
    from src.api.auth import (
        get_current_role,
        get_current_tenant_id,
        get_current_user_id,
        scope_thread_id,
    )
    from src.llm.schema_keys import SQL_INTELLIGENCE_CONFIG_KEY

    cfg = {
        "configurable": {
            "thread_id": scope_thread_id(sid),
            SQL_INTELLIGENCE_CONFIG_KEY: True,
        },
    }
    # 非流式入口与 SSE 使用同一套知识检索范围，显式选源时收窄白名单。
    from src.llm.knowledge_loop import use_request_knowledge_scope
    from src.llm.tenant_config import use_tenant_llm_selection

    with use_tenant_llm_selection(llm_selection), use_request_knowledge_scope(
        datasource=req.datasource,
        allowed_datasources=(
            frozenset(datasource_access) if selected_datasources else None
        ),
    ):
        result = await routes_package._app().ainvoke({
            "user_query": req.query,
            "display_language": req.display_language,
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
            "enabled_skill_ids": enabled_skill_ids,
            "file_ids": list(req.file_ids),
            "action_request": req.action_request.model_dump() if req.action_request else {},
        }, cfg)
    from src.graph.outcome import sanitize_public_output

    f = sanitize_public_output(result.get("final_response", {}))
    return ChatResponse(
        success=f.get("success", True), status=f.get("status", "success"),
        source=f.get("source", "sql_query"), session_id=sid,
        user_query=req.query,
        sql=f.get("sql", result.get("generated_sql", "")),
        sql_statements=f.get("sql_statements", []),
        data=f.get("data", result.get("query_result_sample", [])),
        row_count=f.get("row_count", result.get("query_result_full_count", 0)),
        truncated=bool(f.get("truncated", result.get("query_result_truncated", False))),
        analysis=f.get("analysis", result.get("analysis_result", {})),
        chart=f.get("chart", result.get("chart_config", {})),
        display_language=str(f.get("display_language", req.display_language) or req.display_language),
        column_labels=f.get("column_labels", {}) or {},
        process_summary_enabled=bool(f.get("process_summary_enabled", False)),
        decision_summary=str(f.get("decision_summary", "") or ""),
        artifact=f.get("artifact", {}),
        activated_skills=list(f.get("activated_skills", result.get("activated_skills", [])) or []),
        activated_knowledge=f.get("activated_knowledge", result.get("activated_knowledge", [])) or [],
        error_code=f.get("error_code", ""),
        error_message=f.get("error_message", ""),
    )


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """（保留向后兼容）独立流式端点，等价于 /chat + stream=True。"""
    await _enforce_chat_request_quota(req)
    enabled_skill_ids = _validated_skill_ids(req)
    logger.debug(
        "兼容流式 Chat 入口",
        datasource=req.datasource,
        selected_count=len(req.datasources or [req.datasource]),
    )
    from fastapi.responses import StreamingResponse

    from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
    from src.api.streaming import stream_analysis

    datasource_access = await _resolve_chat_access_for_request(req)
    llm_selection = await _resolve_chat_llm_selection(req)
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
            enabled_skill_ids=enabled_skill_ids,
            request_rate_limit_checked=True,
            llm_selection=llm_selection,
            file_ids=req.file_ids,
            action_request=req.action_request.model_dump() if req.action_request else None,
            display_language=req.display_language,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
