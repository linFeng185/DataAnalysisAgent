"""通知渠道分发接缝：站内信必达，外部渠道按租户配置决定是否尝试。

设计边界（需求 1.9「先不发」）：
    1. 站内信由 notifications / notification_recipients 落库保证，本模块只回填
       success 状态，不做二次投递。
    2. email / feishu / slack 的**实际发送实现本次不提供**：默认 sender 返回
       None，状态记为 not_configured 并放行，不会让通知发布失败。
    3. 真实实现者按 src/automation/delivery.py 的 senders 注入模式，在
       `_default_sender` 里按租户 config_json（SMTP / Webhook）构造发送闭包，
       并把签名保持为 `async def sender(recipient: str, title: str, body: str)`。
"""

from __future__ import annotations

from typing import Any, Callable

from src.logging_config import get_logger
from src.notifications.store import get_notification_store

logger = get_logger(__name__)

# 需要外部发送实现的渠道；in_app 不走这里。
EXTERNAL_CHANNELS: tuple[str, ...] = ("email", "feishu", "slack")


class NotificationChannelDispatcher:
    """把一条已落库的通知分发到租户启用的外部渠道。"""

    # 方法作用：注入通知 Store 与可替换的渠道发送器。
    # Args: store - 可选 NotificationStore；senders - 可选渠道发送函数字典。
    # Returns: 无返回值。
    def __init__(
        self,
        store: Any | None = None,
        senders: dict[str, Callable[..., Any]] | None = None,
    ) -> None:
        self._store = store
        self._senders = dict(senders or {})
        logger.debug("通知渠道分发器初始化完成", injected_senders=sorted(self._senders))

    # 方法作用：按租户配置逐渠道尝试投递，单个渠道失败不影响其它渠道。
    # Args: self - 分发器；tenant_id - 租户 ID；notification - 通知摘要字典；
    #       user_ids - 收件人列表（用于日志与后续外部渠道解析）。
    # Returns: 渠道名到 success/disabled/not_configured/error 的状态映射。
    async def dispatch(
        self,
        tenant_id: int,
        notification: dict[str, Any],
        user_ids: list[int] | None = None,
    ) -> dict[str, str]:
        store = self._store or get_notification_store()
        statuses: dict[str, str] = {"in_app": "success"}
        try:
            channels = await store.get_channels(int(tenant_id))
        except Exception as exc:
            logger.error(
                "通知渠道配置读取失败，仅保留站内信",
                tenant_id=int(tenant_id),
                error_type=type(exc).__name__,
                exc_info=True,
            )
            return statuses

        title = str(notification.get("title", ""))
        body = str(notification.get("body", ""))
        reminder = list(dict.fromkeys(int(item) for item in (user_ids or [])))
        for channel_row in channels:
            channel = str(channel_row.get("channel", ""))
            if channel not in EXTERNAL_CHANNELS:
                continue
            config = dict(channel_row.get("config") or {})
            if not bool(channel_row.get("enabled", False)):
                statuses[channel] = "disabled"
                continue
            sender = self._senders.get(channel) or self._default_sender(channel, config)
            if sender is None:
                statuses[channel] = "not_configured"
                logger.warning(
                    "通知外部渠道未配置，已放行",
                    tenant_id=int(tenant_id),
                    channel=channel,
                    notification_id=notification.get("id"),
                )
                continue
            try:
                await sender(self._resolve_recipient(channel, config), title, body)
                statuses[channel] = "success"
            except Exception as exc:
                statuses[channel] = "error"
                logger.error(
                    "通知外部渠道投递失败",
                    tenant_id=int(tenant_id),
                    channel=channel,
                    notification_id=notification.get("id"),
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
        logger.info(
            "通知渠道分发完成",
            tenant_id=int(tenant_id),
            notification_id=notification.get("id"),
            recipient_count=len(reminder),
            statuses=statuses,
        )
        return statuses

    # 方法作用：按租户配置构造外部渠道发送闭包。
    # Args: self - 分发器；channel - email/feishu/slack；config - 租户渠道配置（敏感值已加密）。
    # Returns: 异步发送函数；本次统一返回 None（实现留待后续需求）。
    def _default_sender(self, channel: str, config: dict[str, Any]):
        """真实发送实现 TODO（需求 1.9 明确本次先不发送）。

        实现方向（保持与 src/automation/delivery.py 相同的结构）：
        * email：解密 smtp_password，按 smtp_host/port/username/use_tls 建
          SMTP/TLS 连接，向收件人发送纯文本邮件。
        * feishu / slack：解密 webhook_url，按各自 JSON 契约 POST。
        在补上实现前，这里必须返回 None，让状态保持 not_configured 而不是
        静默假装成功。
        """
        del channel, config
        return None

    # 方法作用：从渠道配置解析本次投递的收件目标。
    # Args: self - 分发器；channel - 渠道名；config - 渠道配置字典。
    # Returns: 收件人字符串（邮箱或 Webhook 地址）；缺失时为空字符串。
    def _resolve_recipient(self, channel: str, config: dict[str, Any]) -> str:
        if channel == "email":
            return str(config.get("recipient_email") or config.get("email_from") or "").strip()
        return self._decrypt(config.get("webhook_url") or "")

    # 方法作用：解密敏感配置值，解密失败按空值处理并记录错误。
    # Args: self - 分发器；value - 密文或明文配置值。
    # Returns: 明文；空输入或解密失败返回空字符串。
    @staticmethod
    def _decrypt(value: Any) -> str:
        text = str(value or "")
        if not text:
            return ""
        try:
            from src.datasource.credential_manager import CredentialManager

            return CredentialManager().decrypt(text)
        except Exception as exc:
            logger.error("通知渠道敏感配置解密失败", error_type=type(exc).__name__, exc_info=True)
            return ""


_dispatcher: NotificationChannelDispatcher | None = None


# 方法作用：获取进程级通知渠道分发器。
# Args: 无。
# Returns: 当前进程共享的 NotificationChannelDispatcher。
def get_notification_dispatcher() -> NotificationChannelDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = NotificationChannelDispatcher()
    return _dispatcher


# 方法作用：用注入的分发器替换进程级实例，供测试与运行时装配使用。
# Args: dispatcher - 新的分发器；None 表示恢复惰性创建。
# Returns: 无返回值。
def set_notification_dispatcher(dispatcher: NotificationChannelDispatcher | None) -> None:
    global _dispatcher
    _dispatcher = dispatcher
    logger.info("通知渠道分发器已替换", injected=dispatcher is not None)
