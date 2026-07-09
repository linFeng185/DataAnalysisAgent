"""凭证加解密管理 — PBKDF2 派生 Fernet 密钥。"""

from __future__ import annotations

import os, re
from base64 import urlsafe_b64encode
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from src.logging_config import get_logger

logger = get_logger(__name__)
_DEFAULT = "credential-encryption-key-change-in-production"


class CredentialManager:
    """PBKDF2 派生密钥，加密数据源凭证。"""

    def __init__(self, key: str | None = None) -> None:
        raw = key or os.getenv("CREDENTIAL_ENCRYPTION_KEY", "")
        if not raw or raw == _DEFAULT:
            logger.warning("CREDENTIAL_ENCRYPTION_KEY 使用默认值，生产必须配置")
            raw = _DEFAULT
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                         salt=b"data-agent-salt", iterations=480000)
        self._fernet = Fernet(urlsafe_b64encode(kdf.derive(raw.encode())))

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except Exception:
            if not token.startswith("gAAAAA"):
                logger.warning("发现未加密凭证，请重新保存数据源")
                return token
            raise

    @staticmethod
    def resolve_env_ref(value: str) -> str:
        return re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), m.group(0)), value)
