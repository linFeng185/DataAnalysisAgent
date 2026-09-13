"""通用通知服务：发布、收件箱、已读、删除与租户渠道配置。

对外关键契约（其它模块直接 import）：
    async def notify_user(tenant_id, user_id, kind, title, body, link="") -> None

平台通知跨租户方案：
    超管发布 platform 通知时，为**每个启用租户各写一条** notifications 行（tenant_id
    各不相同），而不是写一条跨租户可见的行。好处是 RLS 策略可以保持「行属于单一
    租户」的不变式，收件箱查询与渠道配置都无需特判；代价是超管发布一次会产生
    N 条记录（N=租户数），审计时按 audience_json + created_at 聚合即可。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from src.logging_config import get_logger
from src.notifications.dispatch import get_notification_dispatcher
from src.notifications.hub import get_notification_hub
from src.notifications.models import (
    MAX_PAGE_SIZE,
    NOTIFICATION_KINDS,
    PUBLISHABLE_KINDS,
    SENSITIVE_CHANNEL_FIELDS,
)
from src.notifications.store import get_notification_store

logger = get_logger(__name__)


# 方法作用：把请求对象或字典统一转换为投递范围字典。
# Args: audience - NotificationAudience、字典或 None。
# Returns: {"scope", "user_ids", "roles"} 规范化字典。
def _normalize_audience(audience: Any) -> dict[str, Any]:
    if audience is None:
        return {"scope": "all", "user_ids": [], "roles": []}
    if hasattr(audience, "model_dump"):
        data = audience.model_dump()
    else:
        data = dict(audience)
    scope = str(data.get("scope") or "all")
    user_ids = list(dict.fromkeys(int(item) for item in (data.get("user_ids") or []) if int(item) > 0))
    roles = list(dict.fromkeys(str(item).strip().lower() for item in (data.get("roles") or []) if str(item).strip()))
    return {"scope": scope, "user_ids": user_ids, "roles": roles}


# 方法作用：校验发布者角色与通知类型的匹配关系。
# Args: identity - 当前身份；kind - 通知类型。
# Returns: 无返回值；越权时抛出 PermissionError。
def _authorize_publish(identity: Any, kind: str) -> None:
    role = str(getattr(identity, "role", "") or "").strip().lower()
    logger.debug("通知发布授权入口", kind=kind, role=role, user_id=getattr(identity, "user_id", 0))
    if kind not in PUBLISHABLE_KINDS:
        raise ValueError(f"不支持从发布接口创建的通知类型: {kind}")
    if kind == "platform":
        if role != "super_admin":
            logger.warning("平台通知授权拒绝", role=role)
            raise PermissionError("平台通知仅超级管理员可发布")
        return
    if role != "tenant_admin":
        logger.warning("租户通知授权拒绝", kind=kind, role=role)
        raise PermissionError("租户通知与用户群通知仅租户管理员可发布")
    logger.debug("通知发布授权通过", kind=kind, role=role)


# 方法作用：按投递范围解析目标租户内的收件人 ID。
# Args: store - NotificationStore；tenant_id - 目标租户；audience - 规范化范围字典。
# Returns: 收件人 ID 列表；指定用户范围里的跨租户 ID 会被剔除。
async def _resolve_recipients(store: Any, tenant_id: int, audience: dict[str, Any]) -> list[int]:
    scope = str(audience.get("scope") or "all")
    logger.debug("通知收件人解析入口", tenant_id=int(tenant_id), scope=scope)
    if scope == "all":
        users = await store.list_users(int(tenant_id))
        recipients = [int(user["id"]) for user in users]
    elif scope == "role":
        users = await store.list_users(int(tenant_id), audience.get("roles") or [])
        recipients = [int(user["id"]) for user in users]
    else:
        requested = [int(item) for item in (audience.get("user_ids") or [])]
        recipients = await store.filter_tenant_users(int(tenant_id), requested)
    logger.info("通知收件人解析完成", tenant_id=int(tenant_id), scope=scope, recipient_count=len(recipients))
    return recipients


# 方法作用：构造只含标识与摘要的 WebSocket 推送负载，不含正文与业务数据。
# Args: notification - 通知摘要；unread_count - 该收件人的未读数。
# Returns: 可直接 JSON 序列化的推送字典。
def _ws_payload(notification: dict[str, Any], unread_count: int) -> dict[str, Any]:
    return {
        "type": "notification",
        "id": notification.get("id"),
        "kind": notification.get("kind"),
        "title": notification.get("title"),
        "link": notification.get("link"),
        "created_at": notification.get("created_at"),
        "unread_count": int(unread_count),
    }


# 方法作用：尽力把新通知推送给在线连接，失败只记 warning 不影响落库。
# Args: tenant_id - 租户 ID；notification - 通知摘要；user_ids - 收件人列表。
# Returns: 实际推送成功的连接数。
async def _push_to_recipients(tenant_id: int, notification: dict[str, Any], user_ids: list[int]) -> int:
    hub = get_notification_hub()
    store = get_notification_store()
    try:
        online_users = await hub.online_user_ids(int(tenant_id))
    except Exception as exc:
        logger.warning("通知在线连接查询失败", tenant_id=int(tenant_id), error_type=type(exc).__name__, exc_info=True)
        return 0
    targets = [int(user_id) for user_id in user_ids if int(user_id) in set(online_users)]
    sent = 0
    for user_id in targets:
        try:
            unread = await store.unread_count(
                SimpleNamespace(tenant_id=int(tenant_id), user_id=int(user_id), role="")
            )
            sent += await hub.broadcast_to_user(
                int(tenant_id),
                int(user_id),
                _ws_payload(notification, unread),
            )
        except Exception as exc:
            # 推送是「尽力而为」：任何异常都不得让通知发布失败。
            logger.warning(
                "通知推送降级",
                tenant_id=int(tenant_id),
                user_id=int(user_id),
                notification_id=notification.get("id"),
                error_type=type(exc).__name__,
            )
    logger.info(
        "通知推送结果",
        tenant_id=int(tenant_id),
        notification_id=notification.get("id"),
        online_targets=len(targets),
        sent=sent,
    )
    return sent


# 方法作用：调用渠道分发接缝，失败只记 warning 不影响落库。
# Args: tenant_id - 租户 ID；notification - 通知摘要；user_ids - 收件人列表。
# Returns: 渠道状态映射；分发异常时返回空字典。
async def _dispatch_channels(tenant_id: int, notification: dict[str, Any], user_ids: list[int]) -> dict[str, str]:
    try:
        return await get_notification_dispatcher().dispatch(int(tenant_id), notification, user_ids)
    except Exception as exc:
        logger.warning(
            "通知渠道分发降级",
            tenant_id=int(tenant_id),
            notification_id=notification.get("id"),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return {}


# 方法作用：写入一条通知并完成推送与渠道分发（站内信必达）。
# Args: tenant_id - 租户 ID；kind - 通知类型；title - 标题；body - 正文；link - 链接；
#       created_by - 发布人；audience - 投递范围字典；recipients - 收件人列表。
# Returns: {"id", "created_at", "recipient_count"}。
async def _publish_to_tenant(
    *,
    tenant_id: int,
    kind: str,
    title: str,
    body: str,
    link: str,
    created_by: int,
    audience: dict[str, Any],
    recipients: list[int],
) -> dict[str, Any]:
    store = get_notification_store()
    notification = await store.insert_notification(
        tenant_id=int(tenant_id),
        kind=kind,
        title=title,
        body=body,
        link=link,
        created_by=int(created_by),
        audience=audience,
        recipients=recipients,
    )
    # 推送与渠道分发需要完整摘要（标题/链接/正文），而落库只返回 id 与时间。
    summary = {**notification, "kind": kind, "title": title, "body": body, "link": link}
    await _push_to_recipients(int(tenant_id), summary, recipients)
    await _dispatch_channels(int(tenant_id), summary, recipients)
    return {**notification, "recipient_count": len(recipients)}


# 方法作用：业务代码产生个人通知的入口：落库 + 尽力推送 + 渠道分发。
# Args: tenant_id - 租户 ID；user_id - 收件人；kind - 通知类型；title - 标题；
#       body - 正文；link - 可选跳转链接。
# Returns: 无返回值；推送与外部渠道失败都不抛异常。
async def notify_user(tenant_id: int, user_id: int, kind: str, title: str, body: str, link: str = "") -> None:
    """个人通知入口：站内信必达，WebSocket 推送与外部渠道尽力而为。"""
    logger.debug(
        "个人通知入口",
        tenant_id=int(tenant_id),
        user_id=int(user_id),
        kind=kind,
        title_length=len(title or ""),
    )
    resolved_kind = str(kind or "personal")
    if resolved_kind not in NOTIFICATION_KINDS:
        logger.warning("个人通知类型无效，回退为 personal", kind=kind)
        resolved_kind = "personal"
    try:
        audience = {"scope": "users", "user_ids": [int(user_id)], "roles": []}
        await _publish_to_tenant(
            tenant_id=int(tenant_id),
            kind=resolved_kind,
            title=str(title or ""),
            body=str(body or ""),
            link=str(link or ""),
            created_by=0,
            audience=audience,
            recipients=[int(user_id)],
        )
    except Exception as exc:
        # 通知是业务后置动作：落库失败也不能让报表等主流程崩溃，只记 error。
        logger.error(
            "个人通知写入失败",
            tenant_id=int(tenant_id),
            user_id=int(user_id),
            kind=resolved_kind,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return
    logger.info("个人通知完成", tenant_id=int(tenant_id), user_id=int(user_id), kind=resolved_kind)


# 方法作用：按投递范围发布通知，平台通知为每个启用租户各写一条。
# Args: identity - 当前身份；kind - 通知类型；title - 标题；body - 正文；
#       link - 链接；audience - 投递范围。
# Returns: (通知 ID, 实际投递人数)；无收件人时 ID 为 None。
async def create_notification(
    identity: Any,
    kind: str,
    title: str,
    body: str,
    link: str = "",
    audience: Any = None,
) -> tuple[str | None, int]:
    """发布通知并返回主通知 ID 与投递人数。"""
    _authorize_publish(identity, kind)
    audience_dict = _normalize_audience(audience)
    if not str(title or "").strip():
        raise ValueError("title 不能为空")
    store = get_notification_store()
    if kind == "platform":
        tenant_ids = await store.list_tenants()
    else:
        tenant_ids = [int(getattr(identity, "tenant_id", 0) or 0)]
    logger.info(
        "通知发布入口",
        kind=kind,
        scope=audience_dict["scope"],
        tenant_count=len(tenant_ids),
        created_by=int(getattr(identity, "user_id", 0) or 0),
    )
    primary_id: str | None = None
    delivered = 0
    for tenant_id in tenant_ids:
        recipients = await _resolve_recipients(store, int(tenant_id), audience_dict)
        if not recipients:
            logger.info("通知发布跳过空投递租户", tenant_id=int(tenant_id), kind=kind)
            continue
        result = await _publish_to_tenant(
            tenant_id=int(tenant_id),
            kind=kind,
            title=str(title).strip(),
            body=str(body or ""),
            link=str(link or ""),
            created_by=int(getattr(identity, "user_id", 0) or 0),
            audience=audience_dict,
            recipients=recipients,
        )
        if primary_id is None:
            primary_id = str(result["id"])
        delivered += int(result["recipient_count"])
    logger.info(
        "通知发布完成",
        kind=kind,
        primary_notification_id=primary_id,
        delivered=delivered,
    )
    return primary_id, delivered


# 方法作用：读取当前身份收件箱，分页与每页条数在服务层夹紧。
# Args: identity - 当前身份；unread_only - 只看未读；page - 页码；page_size - 每页条数。
# Returns: {"items", "total", "page", "page_size"}。
async def list_inbox(
    identity: Any,
    *,
    unread_only: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """收件箱按收件时间倒序，软删行不出现。"""
    # 分页参数一律夹紧而不是报错：0/负数取 1，超出上限取 100。
    safe_page = max(1, int(page)) if page is not None else 1
    safe_size = max(1, min(int(page_size), MAX_PAGE_SIZE)) if page_size is not None else 20
    result = await get_notification_store().list_inbox(
        identity,
        unread_only=bool(unread_only),
        page=safe_page,
        page_size=safe_size,
    )
    logger.info(
        "收件箱读取完成",
        tenant_id=getattr(identity, "tenant_id", 0),
        user_id=getattr(identity, "user_id", 0),
        unread_only=bool(unread_only),
        total=result.get("total", 0),
    )
    return result


# 方法作用：返回当前身份未读数。
# Args: identity - 当前身份。
# Returns: 未读条数。
async def unread_count(identity: Any) -> int:
    count = await get_notification_store().unread_count(identity)
    logger.info("未读数读取完成", user_id=getattr(identity, "user_id", 0), count=count)
    return count


# 方法作用：标记一条通知为已读。
# Args: identity - 当前身份；notification_id - 通知 ID。
# Returns: 命中返回 True，否则 False（路由层转 404）。
async def mark_read(identity: Any, notification_id: str) -> bool:
    matched = await get_notification_store().mark_read(identity, str(notification_id))
    logger.info("标记已读", user_id=getattr(identity, "user_id", 0), notification_id=notification_id, matched=matched)
    return matched


# 方法作用：把当前身份的全部未读标记为已读。
# Args: identity - 当前身份。
# Returns: 实际更新条数。
async def mark_all_read(identity: Any) -> int:
    updated = await get_notification_store().mark_all_read(identity)
    logger.info("全部已读", user_id=getattr(identity, "user_id", 0), updated=updated)
    return updated


# 方法作用：软删一条通知，删后不再出现在收件箱且未读数下降。
# Args: identity - 当前身份；notification_id - 通知 ID。
# Returns: 命中返回 True，否则 False（路由层转 404）。
async def delete_notification(identity: Any, notification_id: str) -> bool:
    matched = await get_notification_store().soft_delete(identity, str(notification_id))
    logger.info("通知删除", user_id=getattr(identity, "user_id", 0), notification_id=notification_id, matched=matched)
    return matched


# 方法作用：把渠道配置里的敏感值替换为「是否已配置」，绝不回显明文或密文。
# Args: config - 渠道配置字典。
# Returns: 脱敏后的配置字典。
def _mask_channel_config(config: dict[str, Any]) -> dict[str, Any]:
    masked: dict[str, Any] = {}
    for key, value in (config or {}).items():
        if key in SENSITIVE_CHANNEL_FIELDS:
            masked[f"{key}_configured"] = bool(str(value or "").strip())
        else:
            masked[key] = value
    return masked


# 方法作用：读取租户渠道配置（敏感值只回显是否已配置）。
# Args: tenant_id - 租户 ID。
# Returns: {"channels": [{channel, enabled, config, updated_by, updated_at}]}。
async def get_channels(tenant_id: int) -> dict[str, Any]:
    rows = await get_notification_store().get_channels(int(tenant_id))
    channels = [
        {
            "channel": row["channel"],
            "enabled": bool(row["enabled"]),
            "config": _mask_channel_config(dict(row.get("config") or {})),
            "updated_by": int(row.get("updated_by") or 0),
            "updated_at": row.get("updated_at"),
        }
        for row in rows
    ]
    logger.info("渠道配置读取完成", tenant_id=int(tenant_id), channel_count=len(channels))
    return {"channels": channels}


# 方法作用：保存租户渠道配置；敏感值加密入库，空字符串表示清除、缺键表示保留原值。
# Args: identity - 当前身份；payload - ChannelsPayload 或等效字典。
# Returns: 保存后的脱敏配置。
async def save_channels(identity: Any, payload: Any) -> dict[str, Any]:
    role = str(getattr(identity, "role", "") or "").strip().lower()
    tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
    if role != "tenant_admin":
        logger.warning("渠道配置保存拒绝", role=role, user_id=getattr(identity, "user_id", 0))
        raise PermissionError("通知渠道配置由当前租户管理员维护")
    if tenant_id <= 0:
        raise PermissionError("当前身份没有租户")
    raw = payload.model_dump() if hasattr(payload, "model_dump") else dict(payload or {})
    requested = dict(raw.get("channels") or {})
    current = {
        row["channel"]: dict(row.get("config") or {})
        for row in await get_notification_store().get_channels(tenant_id)
    }
    prepared: dict[str, dict[str, Any]] = {}
    for channel, setting in requested.items():
        config = dict((setting or {}).get("config") or {})
        merged = dict(current.get(channel) or {})
        for key in SENSITIVE_CHANNEL_FIELDS:
            if key not in config:
                continue
            value = str(config[key] or "").strip()
            if not value:
                merged.pop(key, None)
                continue
            merged[key] = _encrypt_secret(value)
        for key, value in config.items():
            if key in SENSITIVE_CHANNEL_FIELDS:
                continue
            merged[key] = value
        prepared[channel] = {
            "enabled": bool((setting or {}).get("enabled", False)),
            "config": merged,
        }
    await get_notification_store().save_channels(tenant_id, prepared, int(getattr(identity, "user_id", 0) or 0))
    logger.info(
        "渠道配置保存完成",
        tenant_id=tenant_id,
        user_id=getattr(identity, "user_id", 0),
        channels=sorted(prepared),
    )
    return await get_channels(tenant_id)


# 方法作用：用与数据源凭证相同的加密能力保护渠道敏感值。
# Args: value - 明文敏感值。
# Returns: 版本化密文；加密失败时抛出异常由调用方决定是否中断保存。
def _encrypt_secret(value: str) -> str:
    from src.datasource.credential_manager import CredentialManager

    encrypted = CredentialManager().encrypt(value)
    logger.debug("渠道敏感配置加密完成", cipher_length=len(encrypted))
    return encrypted
