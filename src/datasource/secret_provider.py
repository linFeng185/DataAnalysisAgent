"""启动时从只读文件或 Vault KV v2 注入托管凭证。"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.config import Settings
from src.logging_config import get_logger

logger = get_logger(__name__)
_MAX_SECRET_BYTES = 65_536


class SecretProviderError(RuntimeError):
    """不携带上游正文、认证令牌或秘密值的安全异常。"""


class ManagedSecrets(BaseModel):
    """仅允许托管认证和加密字段，不能通过 Secret 修改授权配置。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    credential_encryption_key: str = Field(min_length=32, repr=False)
    credential_key_id: str = Field(default="", max_length=64)
    credential_previous_keys: dict[str, str] = Field(default_factory=dict, repr=False)
    jwt_secret: str = Field(default="", min_length=32, repr=False)
    database_url: str = Field(default="", min_length=1, repr=False)
    admin_api_key: str = Field(default="", repr=False)
    super_admin_password: str = Field(default="", repr=False)


# 方法作用：限制读取体积并避免把路径或文件内容放入异常消息。
# Args: filename - 部署指定的 Secret 或 Agent token 文件；limit - 最大字节数。
# Returns: 严格 UTF-8 解码且去除首尾空白的文本。
def _read_private_file(filename: str, limit: int = _MAX_SECRET_BYTES) -> str:
    if not filename:
        raise SecretProviderError("托管凭证文件未配置")
    try:
        with Path(filename).open("rb") as stream:
            content = stream.read(limit + 1)
        if not content or len(content) > limit:
            raise SecretProviderError("托管凭证文件为空或超过大小限制")
        return content.decode("utf-8").strip()
    except (OSError, UnicodeError):
        raise SecretProviderError("无法读取托管凭证文件") from None


# 方法作用：校验 Vault 地址和路径，防止认证被发送到不安全或意外端点。
# Args: settings - 包含 Vault 部署参数的配置。
# Returns: 安全的 KV v2 读取 URL。
def _vault_url(settings: Settings) -> str:
    try:
        parsed = urlsplit(settings.vault_addr)
        valid = (
            parsed.scheme == "https" and parsed.hostname and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment
            and parsed.path in ("", "/")
        )
        if not valid:
            raise SecretProviderError("Vault 必须使用无认证信息的 HTTPS 根地址")
        for path in (settings.vault_mount, settings.vault_secret_path):
            if not path or any(
                part in ("", ".", "..") or not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
                for part in path.split("/")
            ):
                raise SecretProviderError("Vault KV 路径不合法")
        return (
            f"{settings.vault_addr.rstrip('/')}/v1/{settings.vault_mount}"
            f"/data/{settings.vault_secret_path}"
        )
    except ValueError:
        raise SecretProviderError("Vault 地址不合法") from None


# 方法作用：通过有限超时和严格 TLS 从 KV v2 读取一个托管对象。
# Args: settings - Vault 配置；transport - 仅供测试注入的 HTTP 传输器。
# Returns: Vault data.data 对象。
async def _read_vault(
    settings: Settings, transport: httpx.AsyncBaseTransport | None,
) -> Any:
    url = _vault_url(settings)
    token = await asyncio.to_thread(_read_private_file, settings.vault_token_file, 8192)
    if not token or any(character.isspace() for character in token):
        raise SecretProviderError("Vault Agent token 格式不合法")
    headers = {"X-Vault-Token": token}
    if settings.vault_namespace:
        headers["X-Vault-Namespace"] = settings.vault_namespace
    parameters = {"version": settings.vault_secret_version} if settings.vault_secret_version else {}
    try:
        tls = ssl.create_default_context(cafile=settings.vault_ca_file or None)
        async with httpx.AsyncClient(
            verify=tls, timeout=settings.vault_timeout_seconds,
            follow_redirects=False, trust_env=False, transport=transport,
        ) as client:
            async with client.stream("GET", url, headers=headers, params=parameters) as response:
                if response.status_code != 200:
                    raise SecretProviderError(f"Vault 读取被拒绝（HTTP {response.status_code}）")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_SECRET_BYTES:
                        raise SecretProviderError("Vault 响应超过大小限制")
        payload = json.loads(body)
        return payload["data"]["data"]
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
        raise SecretProviderError("Vault 请求或响应校验失败") from None


# 方法作用：原子读取托管源并构建完成白名单校验的新配置。
# Args: settings - 未注入配置；transport - 可选的测试 HTTP 传输器。
# Returns: 环境模式返回原配置，托管模式返回新配置；失败不回退。
async def load_secret_settings(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
) -> Settings:
    """在任何数据库连接或后台任务启动前完成敏感配置注入。"""
    provider = getattr(settings, "secret_provider", "environment")
    logger.debug("load_secret_settings 入口", provider=provider)
    if provider == "environment":
        return settings
    try:
        if provider == "file":
            text = await asyncio.to_thread(_read_private_file, settings.secret_file)
            try:
                payload = json.loads(text)
            except ValueError:
                raise SecretProviderError("托管凭证文件不是有效 JSON") from None
        elif provider == "vault":
            payload = await _read_vault(settings, transport)
        else:
            raise SecretProviderError("不支持的 Secret Provider")
        try:
            managed = ManagedSecrets.model_validate(payload)
            updates = managed.model_dump(exclude_unset=True)
            values = settings.model_dump()
            values.update(updates)
            result = Settings.model_validate(values)
            from src.datasource.credential_manager import validate_keyring

            validate_keyring(
                result.credential_encryption_key, result.credential_key_id,
                result.credential_previous_keys,
            )
        except (ValidationError, ValueError):
            raise SecretProviderError("托管凭证字段或密钥集合校验失败") from None
    except SecretProviderError:
        logger.error("托管凭证加载失败，阻断启动", provider=provider, exc_info=True)
        raise
    logger.info("托管凭证加载完成", provider=provider, field_count=len(updates))
    return result
