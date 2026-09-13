"""通用通知 API：收件箱、已读、删除、发布、租户渠道配置与 WebSocket 推送。

WebSocket 鉴权说明：
    AuthMiddleware 与 ApiAccessPolicyMiddleware 都只处理 HTTP 作用域
    （`scope["type"] != "http"` 直接放行），因此 WS 握手必须在 handler 内
    自行取令牌并复用 auth 模块的解码与身份复核函数；推送负载只含通知标识与
    摘要，不含正文、报表数据或任何敏感字段。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from src.api.auth import (
    ACCESS_TOKEN_COOKIE,
    AuthIdentity,
    decode_access_token,
    get_current_identity,
    load_current_identity,
)
from src.config import get_settings
from src.logging_config import get_logger
from src.notifications.hub import get_notification_hub
from src.notifications.models import ChannelsPayload, NotificationPublishRequest
from src.notifications.service import (
    create_notification,
    delete_notification,
    get_channels,
    list_inbox,
    mark_all_read,
    mark_read,
    save_channels,
    unread_count,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/notifications", tags=["notifications"])

# 未通过鉴权时使用的应用级关闭码（4000-4999 为用户自定义区间）。
_WS_UNAUTHORIZED_CODE = 4401


# 方法作用：校验字符串是否为合法 UUID，避免非法标识触发数据库转型错误。
# Args: value - 待校验的标识。
# Returns: 合法 UUID 返回 True。
def _is_uuid(value: Any) -> bool:
    try:
        UUID(str(value))
        return True
    except (TypeError, ValueError, AttributeError):
        return False


# 方法作用：把服务层权限异常映射为 403、参数异常映射为 422。
# Args: exc - 服务层抛出的异常。
# Returns: 无返回值；始终抛出 HTTPException。
def _raise_service_error(exc: Exception) -> None:
    if isinstance(exc, PermissionError):
        raise HTTPException(403, str(exc)) from exc
    raise HTTPException(422, str(exc)) from exc


# 方法作用：从 WebSocket 握手中取出访问令牌（Cookie 优先，其次 Bearer 头）。
# Args: websocket - 当前 WebSocket 连接。
# Returns: 令牌字符串；缺失时返回空字符串。
def _extract_websocket_token(websocket: WebSocket) -> str:
    authorization = str(websocket.headers.get("authorization", "") or "")
    bearer = (
        authorization[7:].strip()
        if authorization.lower().startswith("bearer ")
        else ""
    )
    cookie_token = str(websocket.cookies.get(ACCESS_TOKEN_COOKIE, "") or "")
    token = bearer or cookie_token
    logger.debug(
        "通知 WebSocket 令牌来源",
        source="bearer" if bearer else "cookie" if cookie_token else "missing",
    )
    return token


# 方法作用：在 WS 握手内完成身份校验，复用 HTTP 侧的解码、策略与数据库复核。
# Args: websocket - 当前 WebSocket 连接。
# Returns: 五元组身份；未通过校验返回 None。
async def authenticate_websocket(websocket: WebSocket) -> AuthIdentity | None:
    """AuthMiddleware 不覆盖 WS 作用域，这里必须自行强制校验身份。"""
    token = _extract_websocket_token(websocket)
    if not token:
        logger.warning("通知 WebSocket 鉴权失败", reason="缺少令牌")
        return None
    try:
        identity = decode_access_token(token)
    except Exception as exc:
        # 令牌解码异常正文可能回显令牌片段，只记异常类型与堆栈，不把原始消息写进日志。
        safe_error = RuntimeError("通知 WebSocket 令牌解码失败")
        logger.warning("通知 WebSocket 鉴权失败", reason="令牌无效", error_type=type(exc).__name__, exc_info=(type(safe_error), safe_error, exc.__traceback__))
        return None
    try:
        from src.app_context import get_tenant_policy
        from src.security.tenant_policy import RequestIdentity

        get_tenant_policy().validate_identity(
            RequestIdentity(user_id=identity[0], tenant_id=identity[1], role=identity[2])
        )
    except Exception as exc:
        logger.warning("通知 WebSocket 鉴权失败", reason="身份不满足租户策略", error_type=type(exc).__name__, exc_info=True)
        return None
    settings = get_settings()
    if str(getattr(settings, "database_url", "") or "").strip():
        try:
            current_identity = await load_current_identity(identity[0])
        except Exception as exc:
            logger.error(
                "通知 WebSocket 身份复核失败",
                user_id=identity[0],
                error_type=type(exc).__name__,
                exc_info=True,
            )
            if getattr(settings, "env", "prod") == "prod":
                return None
        else:
            if current_identity is None or current_identity != identity:
                logger.warning("通知 WebSocket 身份已失效", user_id=identity[0])
                return None
    logger.info(
        "通知 WebSocket 鉴权完成",
        user_id=identity[0],
        tenant_id=identity[1],
        role=identity[2],
    )
    return identity


@router.get("/inbox")
# 方法作用：读取当前身份收件箱。
# Args: unread_only - 只看未读；page - 页码；page_size - 每页条数（服务层夹紧到 100）。
# Returns: 收件箱分页结果。
async def get_inbox(
    unread_only: bool = Query(default=False),
    page: int = Query(default=1),
    page_size: int = Query(default=20),
) -> dict[str, Any]:
    """软删的通知不会出现在收件箱中。"""
    identity = get_current_identity()
    return await list_inbox(
        identity,
        unread_only=unread_only,
        page=page,
        page_size=page_size,
    )


@router.get("/unread-count")
# 方法作用：读取当前身份未读数，用于未读角标。
# Args: 无。
# Returns: {"count": N}。
async def get_unread_count() -> dict[str, int]:
    """未读计数只统计未软删的收件人行。"""
    identity = get_current_identity()
    return {"count": await unread_count(identity)}


@router.post("/read-all")
# 方法作用：把当前身份全部未读通知标记为已读。
# Args: 无。
# Returns: {"updated": N}。
async def post_read_all() -> dict[str, int]:
    """批量已读只影响当前用户的收件人行。"""
    identity = get_current_identity()
    return {"updated": await mark_all_read(identity)}


@router.post("/{notification_id}/read")
# 方法作用：标记一条通知为已读。
# Args: notification_id - 通知 ID。
# Returns: 更新结果；不存在或不属于当前用户时 404。
async def post_mark_read(notification_id: str) -> dict[str, Any]:
    """非法 UUID 直接按不存在处理，避免数据库转型错误。"""
    identity = get_current_identity()
    if not _is_uuid(notification_id):
        raise HTTPException(404, "通知不存在")
    if not await mark_read(identity, notification_id):
        raise HTTPException(404, "通知不存在")
    return {"notification_id": notification_id, "is_read": True}


@router.delete("/{notification_id}")
# 方法作用：软删一条通知。
# Args: notification_id - 通知 ID。
# Returns: 删除结果；不存在或不属于当前用户时 404。
async def delete_one(notification_id: str) -> dict[str, Any]:
    """软删后未读数下降，但保留审计痕迹。"""
    identity = get_current_identity()
    if not _is_uuid(notification_id):
        raise HTTPException(404, "通知不存在")
    if not await delete_notification(identity, notification_id):
        raise HTTPException(404, "通知不存在")
    return {"notification_id": notification_id, "deleted": True}


@router.post("/publish")
# 方法作用：发布平台通知、租户通知或用户群通知。
# Args: payload - 发布请求（类型、标题、正文、链接、投递范围）。
# Returns: {"notification_id", "delivered", "kind"}。
async def publish_notification(payload: NotificationPublishRequest) -> dict[str, Any]:
    """platform 仅 super_admin，tenant/group 仅 tenant_admin；personal 不走本接口。"""
    identity = get_current_identity()
    try:
        notification_id, delivered = await create_notification(
            identity,
            payload.kind,
            payload.title,
            payload.body,
            payload.link,
            payload.audience,
        )
    except (PermissionError, ValueError) as exc:
        _raise_service_error(exc)
        raise
    return {"notification_id": notification_id, "delivered": delivered, "kind": payload.kind}


@router.get("/admin/channels")
# 方法作用：读取当前租户的通知渠道配置。
# Args: 无。
# Returns: 渠道列表；敏感值只回显是否已配置。
async def get_admin_channels() -> dict[str, Any]:
    """仅租户管理员可读，超管 403（与报表额度口径一致）。"""
    identity = get_current_identity()
    if str(getattr(identity, "role", "") or "") != "tenant_admin":
        raise HTTPException(403, "通知渠道配置由当前租户管理员维护")
    return await get_channels(int(identity.tenant_id))


@router.put("/admin/channels")
# 方法作用：保存当前租户的通知渠道配置。
# Args: payload - 渠道启用状态与配置项。
# Returns: 保存后的脱敏配置。
async def put_admin_channels(payload: ChannelsPayload) -> dict[str, Any]:
    """敏感值加密入库；读取接口只回显 xxx_configured。"""
    identity = get_current_identity()
    try:
        return await save_channels(identity, payload)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@router.websocket("/stream")
# 方法作用：建立通知推送长连接，握手内强制校验身份后加入 Hub。
# Args: websocket - WebSocket 连接。
# Returns: 无返回值；连接关闭时自动从 Hub 注销。
async def notifications_stream(websocket: WebSocket) -> None:
    """推送消息只含通知标识、标题、链接与未读数，客户端消息只用于心跳。"""
    identity = await authenticate_websocket(websocket)
    if identity is None:
        await websocket.close(code=_WS_UNAUTHORIZED_CODE)
        return
    tenant_id, user_id = int(identity[1]), int(identity[0])
    await websocket.accept()
    hub = get_notification_hub()
    await hub.register(tenant_id, user_id, websocket)
    logger.info("通知 WebSocket 连接建立", tenant_id=tenant_id, user_id=user_id)
    try:
        while True:
            message = await websocket.receive_text()
            if str(message).strip().lower() == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        logger.info("通知 WebSocket 客户端断开", tenant_id=tenant_id, user_id=user_id)
    except Exception as exc:
        logger.warning(
            "通知 WebSocket 连接异常关闭",
            tenant_id=tenant_id,
            user_id=user_id,
            error_type=type(exc).__name__,
        )
    finally:
        await hub.unregister(tenant_id, user_id, websocket)
