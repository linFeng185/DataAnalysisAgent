"""定时报告与主动洞察的受控通知渠道。"""

from __future__ import annotations

import asyncio
import json
import smtplib
import urllib.request
from email.message import EmailMessage
from typing import Any

from src.automation.models import ScheduleDefinition
from src.logging_config import get_logger

logger = get_logger(__name__)


class NotificationDispatcher:
    """把通知投递到站内、SMTP、飞书或 Slack 固定服务端配置。"""

    # 方法作用：注入服务器配置和可测试发送器，禁止任务自带外部 URL。
    # Args: self - 分发器；settings - 可选 Settings；senders - 可选渠道发送函数。
    # Returns: 无返回值。
    def __init__(self, settings: Any | None = None, senders: dict[str, Any] | None = None) -> None:
        if settings is None:
            from src.config import get_settings

            settings = get_settings()
        self._settings = settings
        self._senders = dict(senders or {})

    # 方法作用：逐渠道投递并把每个失败限制在当前渠道。
    # Args: self - 分发器；schedule - 任务定义；title - 通知标题；body - Markdown 正文。
    # Returns: channel 到 success/not_configured/error/unsupported 的状态映射。
    async def deliver(
        self,
        schedule: ScheduleDefinition,
        title: str,
        body: str,
    ) -> dict[str, str]:
        statuses: dict[str, str] = {}
        for channel in dict.fromkeys(schedule.channels):
            if channel == "in_app":
                statuses[channel] = "success"
                continue
            sender = self._senders.get(channel)
            if sender is None:
                sender = self._default_sender(channel, schedule)
            if sender is None:
                statuses[channel] = "not_configured" if channel in {"email", "feishu", "slack"} else "unsupported"
                logger.warning(
                    "自动化通知渠道不可用",
                    schedule_id=schedule.id,
                    channel=channel,
                    status=statuses[channel],
                )
                continue
            try:
                await sender(title, body)
                statuses[channel] = "success"
            except Exception as exc:
                statuses[channel] = "error"
                logger.error(
                    "自动化通知投递失败",
                    schedule_id=schedule.id,
                    channel=channel,
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
        logger.info(
            "自动化通知投递完成",
            schedule_id=schedule.id,
            channel_count=len(statuses),
            success_count=sum(status == "success" for status in statuses.values()),
        )
        return statuses

    # 方法作用：按服务器配置创建指定渠道发送闭包。
    # Args: self - 分发器；channel - 渠道；schedule - 任务定义。
    # Returns: 异步发送函数；配置缺失返回 None。
    def _default_sender(self, channel: str, schedule: ScheduleDefinition):
        if channel == "email":
            host = str(getattr(self._settings, "automation_smtp_host", "") or "").strip()
            sender = str(getattr(self._settings, "automation_email_from", "") or "").strip()
            if not host or not sender or not schedule.recipient_email:
                return None

            # 方法作用：通过服务器 SMTP 配置发送当前任务邮件。
            # Args: title - 邮件标题；body - Markdown 正文。
            # Returns: 无返回值。
            async def send_email(title: str, body: str) -> None:
                await asyncio.to_thread(
                    self._send_email_sync,
                    schedule.recipient_email,
                    title,
                    body,
                )

            return send_email
        if channel in {"feishu", "slack"}:
            setting_name = f"automation_{channel}_webhook_url"
            webhook_url = str(getattr(self._settings, setting_name, "") or "").strip()
            if not webhook_url:
                return None

            # 方法作用：向服务器预配置的协作平台 Webhook 发送通知。
            # Args: title - 通知标题；body - Markdown 正文。
            # Returns: 无返回值。
            async def send_webhook(title: str, body: str) -> None:
                await asyncio.to_thread(
                    self._send_webhook_sync,
                    channel,
                    webhook_url,
                    title,
                    body,
                )

            return send_webhook
        return None

    # 方法作用：使用 SMTP/TLS 和可选认证发送纯文本报告邮件。
    # Args: self - 分发器；recipient - 收件人；title - 标题；body - 正文。
    # Returns: 无返回值。
    def _send_email_sync(self, recipient: str, title: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = title
        message["From"] = str(self._settings.automation_email_from)
        message["To"] = recipient
        message.set_content(body)
        host = str(self._settings.automation_smtp_host)
        port = int(getattr(self._settings, "automation_smtp_port", 587))
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            if bool(getattr(self._settings, "automation_smtp_starttls", True)):
                smtp.starttls()
            username = str(getattr(self._settings, "automation_smtp_username", "") or "")
            password = str(getattr(self._settings, "automation_smtp_password", "") or "")
            if username:
                smtp.login(username, password)
            smtp.send_message(message)

    # 方法作用：按飞书或 Slack 固定 JSON 契约调用服务端配置 Webhook。
    # Args: channel - feishu/slack；url - 服务端 URL；title - 标题；body - 正文。
    # Returns: 无返回值。
    @staticmethod
    def _send_webhook_sync(channel: str, url: str, title: str, body: str) -> None:
        if channel == "feishu":
            payload = {"msg_type": "text", "content": {"text": f"{title}\n\n{body}"}}
        else:
            payload = {"text": f"*{title}*\n{body}"}
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            if int(response.status) >= 300:
                raise RuntimeError(f"Webhook HTTP {response.status}")
