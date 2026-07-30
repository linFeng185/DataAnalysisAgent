"""凭证加解密管理 — 使用随机 salt 派生版本化 Fernet 密钥。"""

from __future__ import annotations

import os
import re
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from src.logging_config import get_logger

logger = get_logger(__name__)
_LEGACY_SALT = b"data-agent-salt"
_SALT_BYTES = 16
_TOKEN_VERSION = "v2"
_EPHEMERAL_NON_PROD_KEY = secrets.token_urlsafe(48)


# 方法作用：把凭证引用归类为可安全记录的固定标签。
# Args: value - 加密令牌、环境变量引用或已配置的明文值。
# Returns: 不含任何凭证内容的类别名称。
def describe_credential_reference(value: str) -> str:
    """日志只记录类别，不根据分隔符截取可能的明文密码。"""
    if not value:
        return "empty"
    if value.startswith("${") and value.endswith("}"):
        return "environment_reference"
    if value.startswith(f"{_TOKEN_VERSION}:"):
        return "encrypted_v2"
    return "configured"


class CredentialManager:
    """使用 PBKDF2 随机 salt 加密凭证，并兼容历史固定 salt 密文。"""

    # 方法作用：保存凭证主密钥并准备历史密文解密器。
    # Args: self - 凭证管理器实例；key - 可选主密钥，空值从环境变量读取。
    # Returns: 无返回值。
    def __init__(self, key: str | None = None) -> None:
        try:
            from src.app_context import get_bound_app_context

            context = get_bound_app_context()
            settings = context.settings if context is not None else None
            settings_environment = str(getattr(settings, "env", "")).strip().lower()
            settings_key = str(
                getattr(settings, "credential_encryption_key", "") or ""
            )
            environment = settings_environment or os.getenv("ENV", "prod").strip().lower()
            environment_key = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "")
            raw = key or settings_key or environment_key
            key_source = (
                "explicit"
                if key
                else "app_context"
                if settings_key
                else "environment"
                if environment_key
                else "missing"
            )
            logger.info(
                "CredentialManager 主密钥选择边界",
                environment=environment,
                app_context_bound=context is not None,
                settings_key_configured=bool(settings_key),
                explicit_key_configured=bool(key),
                environment_key_configured=bool(environment_key),
                selected_source=key_source,
            )
            if not raw:
                if environment == "prod":
                    logger.error("CredentialManager.__init__ 失败", error="生产主密钥未配置")
                    raise ValueError("生产环境必须配置 CREDENTIAL_ENCRYPTION_KEY")
                raw = _EPHEMERAL_NON_PROD_KEY
                key_source = "ephemeral"
                logger.warning(
                    "非生产环境使用进程级临时凭证密钥，重启后无法解密持久化密文",
                    environment=environment,
                )
            if environment == "prod" and len(raw) < 32:
                logger.error("CredentialManager.__init__ 失败", error="生产主密钥强度不足")
                raise ValueError("生产环境 CREDENTIAL_ENCRYPTION_KEY 至少需要 32 字符")
            self._raw_key = raw.encode("utf-8")
            self._legacy_fernet = self._derive_fernet(_LEGACY_SALT)
            logger.info(
                "CredentialManager 初始化完成",
                environment=environment,
                selected_source=key_source,
                key_length=len(raw),
            )
        except Exception as exc:
            logger.error("CredentialManager.__init__ 失败", error=str(exc), exc_info=True)
            raise

    # 方法作用：使用主密钥和指定 salt 派生独立 Fernet 实例。
    # Args: self - 凭证管理器实例；salt - 与密文共同保存的随机 salt。
    # Returns: 可用于本次加解密的 Fernet 实例。
    def _derive_fernet(self, salt: bytes) -> Fernet:
        try:
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=480000,
            )
            return Fernet(urlsafe_b64encode(kdf.derive(self._raw_key)))
        except Exception as exc:
            logger.error("派生凭证 Fernet 失败", error=str(exc), exc_info=True)
            raise

    # 方法作用：使用本次随机 salt 加密明文并输出版本化密文。
    # Args: self - 凭证管理器实例；plain - 待加密凭证明文。
    # Returns: 包含版本、salt 和 Fernet token 的字符串。
    def encrypt(self, plain: str) -> str:
        try:
            salt = os.urandom(_SALT_BYTES)
            salt_text = urlsafe_b64encode(salt).decode("ascii")
            token = self._derive_fernet(salt).encrypt(plain.encode("utf-8")).decode("ascii")
            return f"{_TOKEN_VERSION}:{salt_text}:{token}"
        except Exception as exc:
            logger.error("凭证加密失败", error=str(exc), exc_info=True)
            raise

    # 方法作用：解密版本化随机 salt 密文，并兼容历史固定 salt token 和旧明文配置。
    # Args: self - 凭证管理器实例；token - 待解密字符串。
    # Returns: 解密后的凭证明文，空输入返回空字符串。
    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        try:
            if token.startswith(f"{_TOKEN_VERSION}:"):
                version, salt_text, encrypted = token.split(":", maxsplit=2)
                salt = urlsafe_b64decode(salt_text.encode("ascii"))
                if version != _TOKEN_VERSION or len(salt) != _SALT_BYTES or not encrypted:
                    raise ValueError("凭证密文格式无效")
                return self._derive_fernet(salt).decrypt(
                    encrypted.encode("ascii")
                ).decode("utf-8")
            if token.startswith("gAAAAA"):
                return self._legacy_fernet.decrypt(token.encode("ascii")).decode("utf-8")
            if not token.startswith("gAAAAA"):
                logger.warning("发现未加密凭证，请重新保存数据源")
                return token
            raise ValueError("凭证密文格式无效")
        except Exception as exc:
            logger.error("凭证解密失败", error=str(exc), exc_info=True)
            raise

    # 方法作用：将配置字符串中的环境变量占位符替换为当前环境值。
    # Args: value - 可能包含 ${VAR_NAME} 的配置字符串。
    # Returns: 已解析的字符串；缺失变量继续保留原占位符。
    @staticmethod
    def resolve_env_ref(value: str) -> str:
        try:
            return re.sub(
                r"\$\{(\w+)\}",
                lambda match: os.getenv(match.group(1), match.group(0)),
                value,
            )
        except Exception as exc:
            logger.error("解析凭证环境变量失败", error=str(exc), exc_info=True)
            raise
