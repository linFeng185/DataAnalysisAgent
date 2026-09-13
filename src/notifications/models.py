"""通用通知模块的领域常量与请求模型。

四类通知统一为一条 notifications 记录：
* platform：平台通知，仅超管发布，按租户各写一条（见 service.create_notification）。
* tenant：租户通知，仅租户管理员发布。
* group：指定用户群通知，仅租户管理员发布。
* personal：个人通知，不允许从 /publish 发布，只能由业务代码调用 notify_user 产生。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from src.logging_config import get_logger

logger = get_logger(__name__)

# 四类通知类型，与迁移 033 的 CHECK 约束保持一致。
NOTIFICATION_KINDS: tuple[str, ...] = ("platform", "tenant", "group", "personal")
# 渠道枚举：站内信默认启用，邮件/飞书/Slack 本次只保留配置面与分发接缝。
NOTIFICATION_CHANNELS: tuple[str, ...] = ("in_app", "email", "feishu", "slack")
# /publish 允许发布的类型；personal 由业务代码调用 notify_user 产生。
PUBLISHABLE_KINDS: tuple[str, ...] = ("platform", "tenant", "group")
# 投递范围枚举，写入 audience_json 供审计回溯。
AUDIENCE_SCOPES: tuple[str, ...] = ("all", "users", "role")
# 敏感配置键：入库前加密，读取接口只回显 xxx_configured 布尔值。
SENSITIVE_CHANNEL_FIELDS: tuple[str, ...] = ("smtp_password", "webhook_url")
# 每个渠道允许出现的配置键白名单，防止把任意 JSON 写进租户配置。
CHANNEL_CONFIG_FIELDS: dict[str, tuple[str, ...]] = {
    "in_app": (),
    "email": ("smtp_host", "smtp_port", "smtp_username", "smtp_password", "email_from", "use_tls"),
    "feishu": ("webhook_url",),
    "slack": ("webhook_url",),
}
# 收件箱单页上限，超出时由服务层夹紧而不是报错。
MAX_PAGE_SIZE = 100


class NotificationAudience(BaseModel):
    """通知投递范围：全部用户 / 指定用户 / 按角色。"""

    scope: Literal["all", "users", "role"] = "all"
    user_ids: list[int] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)

    # 方法作用：规范化指定用户列表，去掉重复与非正整数。
    # Args: values - 请求中的用户 ID 列表。
    # Returns: 去重后的正整数列表。
    @field_validator("user_ids")
    @classmethod
    def normalize_user_ids(cls, values: list[int]) -> list[int]:
        result: list[int] = []
        for value in values:
            user_id = int(value)
            if user_id > 0 and user_id not in result:
                result.append(user_id)
        return result

    # 方法作用：规范化角色列表，去掉空白与重复项并统一小写。
    # Args: values - 请求中的角色列表。
    # Returns: 去重后的小写角色列表。
    @field_validator("roles")
    @classmethod
    def normalize_roles(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            role = str(value).strip().lower()
            if role and role not in result:
                result.append(role)
        return result

    # 方法作用：校验投递范围与目标列表互相匹配，避免出现空投递。
    # Args: self - 已完成字段校验的范围对象。
    # Returns: 校验通过的范围对象；users/role 缺少目标时抛出 ValidationError。
    @model_validator(mode="after")
    def validate_scope_targets(self) -> "NotificationAudience":
        if self.scope == "users" and not self.user_ids:
            raise ValueError("audience.scope='users' 时 user_ids 不能为空")
        if self.scope == "role" and not self.roles:
            raise ValueError("audience.scope='role' 时 roles 不能为空")
        return self


class NotificationPublishRequest(BaseModel):
    """发布通知请求体。"""

    kind: Literal["platform", "tenant", "group"]
    title: str = Field(..., max_length=256)
    body: str = Field(default="", max_length=20000)
    link: str = Field(default="", max_length=512)
    audience: NotificationAudience = Field(default_factory=NotificationAudience)

    # 方法作用：拒绝只有空白字符的标题。
    # Args: value - 请求标题。
    # Returns: 原样返回标题；空白标题抛出 ValidationError。
    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        if not str(value).strip():
            raise ValueError("title 不能为空")
        return value


class ChannelSetting(BaseModel):
    """单个渠道的启用状态与配置项。"""

    enabled: bool = False
    config: dict[str, Any] = Field(default_factory=dict)


class ChannelsPayload(BaseModel):
    """租户渠道配置保存请求。"""

    channels: dict[str, ChannelSetting] = Field(default_factory=dict)

    # 方法作用：校验渠道名与配置键都在白名单内，防止写入未知渠道或任意 JSON。
    # Args: self - 已完成字段校验的请求对象。
    # Returns: 校验通过的请求对象。
    @model_validator(mode="after")
    def validate_channels(self) -> "ChannelsPayload":
        for channel, setting in self.channels.items():
            if channel not in NOTIFICATION_CHANNELS:
                raise ValueError(f"未知通知渠道: {channel}")
            allowed = CHANNEL_CONFIG_FIELDS.get(channel, ())
            unknown = sorted(key for key in setting.config if key not in allowed)
            if unknown:
                raise ValueError(f"渠道 {channel} 不接受配置项: {', '.join(unknown)}")
        return self
