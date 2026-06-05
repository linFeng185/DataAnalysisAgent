"""凭证加解密管理。"""

from __future__ import annotations

import os
import re
from base64 import b64encode

from cryptography.fernet import Fernet


class CredentialManager:
    """AES-256 凭证加解密。"""

    def __init__(self, key: str | None = None) -> None:
        raw = key or os.getenv("CREDENTIAL_ENCRYPTION_KEY", "changeme_default_key_32bytes!")
        self._fernet = Fernet(b64encode(raw.encode()[:32].ljust(32, b"\0")))

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode() if token else ""

    @staticmethod
    def resolve_env_ref(value: str) -> str:
        """解析 ${VAR_NAME} 占位符。"""
        return re.sub(
            r"\$\{(\w+)\}",
            lambda m: os.getenv(m.group(1), m.group(0)),
            value,
        )
