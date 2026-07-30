"""凭证随机 salt 与旧密文兼容回归测试。"""

from __future__ import annotations

import logging
from base64 import urlsafe_b64encode

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)


class TestCredentialManagerRandomSalt:
    """覆盖功能 2.4.1：随机 salt、版本化密文和旧格式兼容。"""

    # 方法作用：验证每次加密都会保存独立随机 salt 且可正常解密。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_encrypt_uses_random_persisted_salt(self) -> None:
        """相同明文的两次加密必须携带不同 salt。"""
        logger.debug("test_encrypt_uses_random_persisted_salt 入口")
        try:
            # Arrange
            from src.datasource.credential_manager import CredentialManager

            manager = CredentialManager(key="test-master-key-with-at-least-32-bytes")

            # Act
            first = manager.encrypt("database-password")
            second = manager.encrypt("database-password")
            first_parts = first.split(":", maxsplit=2)
            second_parts = second.split(":", maxsplit=2)

            # Assert
            assert first_parts[0] == "v2"
            assert second_parts[0] == "v2"
            assert first_parts[1] != second_parts[1]
            assert manager.decrypt(first) == "database-password"
            assert manager.decrypt(second) == "database-password"
            logger.info("test_encrypt_uses_random_persisted_salt 完成")
        except Exception as exc:
            logger.error("test_encrypt_uses_random_persisted_salt 异常: %s", exc, exc_info=True)
            raise

    # 方法作用：验证凭证日志分类不会返回明文或密文主体。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_credential_reference_description_never_contains_secret(self) -> None:
        """日志只能记录安全类别，不能记录无冒号明文密码。"""
        # Arrange
        from src.datasource.credential_manager import describe_credential_reference

        values = {
            "": "empty",
            "${DATABASE_PASSWORD}": "environment_reference",
            "v2:salt:ciphertext": "encrypted_v2",
            "plain-secret-password": "configured",
        }

        # Act
        descriptions = {
            value: describe_credential_reference(value)
            for value in values
        }

        # Assert
        assert descriptions == values
        assert all("secret" not in description for description in descriptions.values())

    # 方法作用：验证升级后仍能解密使用历史固定 salt 生成的 Fernet 密文。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_decrypt_supports_legacy_fixed_salt_token(self) -> None:
        """旧数据源凭证不得因随机 salt 升级而失效。"""
        logger.debug("test_decrypt_supports_legacy_fixed_salt_token 入口")
        try:
            # Arrange
            from src.datasource.credential_manager import CredentialManager

            raw_key = "test-master-key-with-at-least-32-bytes"
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b"data-agent-salt",
                iterations=480000,
            )
            legacy_token = Fernet(
                urlsafe_b64encode(kdf.derive(raw_key.encode("utf-8")))
            ).encrypt(b"legacy-password").decode("ascii")
            manager = CredentialManager(key=raw_key)

            # Act
            result = manager.decrypt(legacy_token)

            # Assert
            assert result == "legacy-password"
            logger.info("test_decrypt_supports_legacy_fixed_salt_token 完成")
        except Exception as exc:
            logger.error(
                "test_decrypt_supports_legacy_fixed_salt_token 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证统一 Settings 中的稳定主密钥可跨进程级临时密钥变化解密。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_app_context_key_survives_process_restart(self, monkeypatch) -> None:
        """应用已加载稳定主密钥时，重启不得退回随机临时密钥。"""
        logger.debug("test_app_context_key_survives_process_restart 入口")
        try:
            # Arrange
            from types import SimpleNamespace

            import src.datasource.credential_manager as credential_module
            from src.app_context import AppContext, use_app_context

            stable_key = "stable-settings-key-with-at-least-32-bytes"
            context = AppContext(SimpleNamespace(
                env="dev",
                multi_tenant=False,
                credential_encryption_key=stable_key,
            ))
            monkeypatch.setenv("ENV", "dev")
            monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)

            # Act
            with use_app_context(context):
                first = credential_module.CredentialManager()
                token = first.encrypt("persisted-password")
                monkeypatch.setattr(
                    credential_module,
                    "_EPHEMERAL_NON_PROD_KEY",
                    "different-process-ephemeral-key-with-32-bytes",
                )
                second = credential_module.CredentialManager()
                result = second.decrypt(token)

            # Assert
            assert result == "persisted-password"
            logger.info("test_app_context_key_survives_process_restart 完成")
        except Exception as exc:
            logger.error(
                "test_app_context_key_survives_process_restart 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证生产环境缺少凭证主密钥时管理器自身失败关闭。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_production_missing_master_key_is_rejected(self, monkeypatch) -> None:
        """即使绕过应用启动校验，也不得使用公开默认主密钥。"""
        logger.debug("test_production_missing_master_key_is_rejected 入口")
        try:
            from src.datasource.credential_manager import CredentialManager

            monkeypatch.setenv("ENV", "prod")
            monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)

            with pytest.raises(ValueError, match="CREDENTIAL_ENCRYPTION_KEY"):
                CredentialManager()
            logger.info("test_production_missing_master_key_is_rejected 完成")
        except Exception as exc:
            logger.error(
                "test_production_missing_master_key_is_rejected 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证非生产环境缺少主密钥时使用进程级随机临时密钥。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_development_missing_key_uses_ephemeral_process_key(self, monkeypatch) -> None:
        """开发回退密钥必须随机生成，且同一进程内可稳定解密。"""
        logger.debug("test_development_missing_key_uses_ephemeral_process_key 入口")
        try:
            # Arrange
            from src.datasource.credential_manager import CredentialManager

            monkeypatch.setenv("ENV", "dev")
            monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
            first = CredentialManager()
            second = CredentialManager()

            # Act
            token = first.encrypt("development-password")

            # Assert
            assert second.decrypt(token) == "development-password"
            logger.info("test_development_missing_key_uses_ephemeral_process_key 完成")
        except Exception as exc:
            logger.error(
                "test_development_missing_key_uses_ephemeral_process_key 异常: %s",
                exc,
                exc_info=True,
            )
            raise
