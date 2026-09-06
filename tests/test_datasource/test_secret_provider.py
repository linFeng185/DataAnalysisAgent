"""覆盖 2.4.3、19.20 的托管密钥读取和配置安全边界。"""

import json

import httpx
import pytest

from src.config import Settings
from src.datasource.secret_provider import SecretProviderError, load_secret_settings


# 方法作用：构造不读取本地环境密钥的可重复托管配置。
# Args: overrides - 测试需要覆盖的配置字段。
# Returns: 独立的测试配置。
def secret_settings(**overrides) -> Settings:
    """避免测试依赖开发机真实 Vault 或凭证。"""
    return Settings(
        _env_file=None, env="test", credential_encryption_key="initial-key-" * 4,
        **overrides,
    )


class TestSecretProvider:
    """覆盖功能 2.4.3、19.20：正常、异常、边界与显式兼容路径。"""

    # 方法作用：验证未启用托管时不读取外部服务且保持原配置。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_environment_unchanged(self) -> None:
        """环境模式保持兼容，不产生托管请求。"""
        settings = secret_settings(secret_provider="environment")
        result = await load_secret_settings(settings)
        assert result is settings

    # 方法作用：验证只读文件覆盖密钥但不修改原配置。
    # Args: self - 测试实例；tmp_path - 临时目录。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_file_atomic_override(self, tmp_path) -> None:
        """受管字段可注入，原配置和非敏感字段保持不变。"""
        secret_file = tmp_path / "secrets.json"
        secret_file.write_text(json.dumps({
            "credential_encryption_key": "new-master-key-" * 4,
            "credential_key_id": "release-2",
            "credential_previous_keys": {"release-1": "old-master-key-" * 4},
        }), encoding="utf-8")
        settings = secret_settings(secret_provider="file", secret_file=str(secret_file))
        result = await load_secret_settings(settings)
        assert result.credential_key_id == "release-2"
        assert result.credential_previous_keys["release-1"] == "old-master-key-" * 4
        assert settings.credential_encryption_key == "initial-key-" * 4
        assert result.env == settings.env

    # 方法作用：验证缺失文件、弱密钥和非法字段均失败关闭。
    # Args: self - 测试实例；tmp_path - 临时目录；payload - 不合法配置。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [
        {}, [], {"credential_encryption_key": "weak"},
        {"credential_encryption_key": "valid-key-" * 4, "env": "dev"},
        {"credential_encryption_key": 123},
    ])
    async def test_file_invalid_fails_closed(self, tmp_path, payload) -> None:
        """配置错误不可回退为原有环境主密钥。"""
        secret_file = tmp_path / "secrets.json"
        secret_file.write_text(json.dumps(payload), encoding="utf-8")
        settings = secret_settings(secret_provider="file", secret_file=str(secret_file))
        with pytest.raises(SecretProviderError):
            await load_secret_settings(settings)
        assert settings.credential_encryption_key == "initial-key-" * 4

    # 方法作用：验证丢失的托管文件不能继续启动。
    # Args: self - 测试实例；tmp_path - 临时目录。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_missing_file_fails_closed(self, tmp_path) -> None:
        """即使存在环境密钥，丢失文件也不能降级。"""
        settings = secret_settings(secret_provider="file", secret_file=str(tmp_path / "missing"))
        with pytest.raises(SecretProviderError):
            await load_secret_settings(settings)

    # 方法作用：验证 Vault 协议及每次重新读取 Agent token。
    # Args: self - 测试实例；tmp_path - 临时目录。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_vault_protocol_and_token_refresh(self, tmp_path) -> None:
        """请求正确的 KV v2 路径、版本与命名空间，并不缓存旧认证。"""
        token_file = tmp_path / "token"
        token_file.write_text("first-token\n", encoding="utf-8")
        requests = []

        # 方法作用：记录 KV 请求并返回固定的脱离外部服务的密钥。
        # Args: request - httpx 请求。
        # Returns: 模拟的 KV v2 响应。
        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"data": {"data": {
                "credential_encryption_key": "vault-managed-key-" * 3,
                "credential_key_id": "current",
            }, "metadata": {"version": 7}}})

        settings = secret_settings(
            secret_provider="vault", vault_addr="https://vault.example",
            vault_token_file=str(token_file), vault_mount="secret",
            vault_secret_path="agent/production", vault_namespace="team",
            vault_secret_version=7,
        )
        transport = httpx.MockTransport(handle)
        first = await load_secret_settings(settings, transport=transport)
        token_file.write_text("second-token", encoding="utf-8")
        await load_secret_settings(settings, transport=transport)
        assert first.credential_key_id == "current"
        assert str(requests[0].url) == "https://vault.example/v1/secret/data/agent/production?version=7"
        assert requests[0].headers["X-Vault-Namespace"] == "team"
        assert requests[0].headers["X-Vault-Token"] == "first-token"
        assert requests[1].headers["X-Vault-Token"] == "second-token"

    # 方法作用：验证服务错误和重定向不泄露响应正文或触发回退。
    # Args: self - 测试实例；tmp_path - 临时目录；status - HTTP 状态。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [302, 403, 404, 500])
    async def test_vault_error_sanitized(self, tmp_path, status) -> None:
        """包含敏感正文的错误只能向上暴露固定安全摘要。"""
        token_file = tmp_path / "token"
        token_file.write_text("sensitive-auth", encoding="utf-8")
        calls = []

        # 方法作用：构造含敏感正文的服务端错误。
        # Args: request - 请求。
        # Returns: 失败响应。
        def handle(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(status, text="private-secret", headers={"location": "https://other.example"})

        settings = secret_settings(
            secret_provider="vault", vault_addr="https://vault.example",
            vault_token_file=str(token_file), vault_secret_path="agent",
        )
        with pytest.raises(SecretProviderError) as captured:
            await load_secret_settings(settings, transport=httpx.MockTransport(handle))
        assert "private-secret" not in str(captured.value)
        assert "sensitive-auth" not in str(captured.value)
        assert len(calls) == 1

    # 方法作用：验证不安全 URL 和路径在发送认证前被拒绝。
    # Args: self - 测试实例；overrides - 不安全配置。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    @pytest.mark.parametrize("overrides", [
        {"vault_addr": "http://vault.example"},
        {"vault_addr": "https://user:password@vault.example"},
        {"vault_addr": "https://vault.example?token=private"},
        {"vault_addr": "https://vault.example", "vault_secret_path": "../agent"},
        {"vault_addr": "https://vault.example", "vault_mount": "secret/../sys"},
    ])
    async def test_vault_unsafe_config_rejected(self, overrides) -> None:
        """生产及测试模式都不允许明文远程认证和路径穿越。"""
        settings = secret_settings(secret_provider="vault", **overrides)
        with pytest.raises(SecretProviderError):
            await load_secret_settings(settings)

    # 方法作用：验证超时和不完整 KV 数据都转换为无敏感内容的失败。
    # Args: self - 实例；tmp_path - 临时目录；mode - 上游故障类别。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["timeout", "empty", "malformed", "oversized"])
    async def test_vault_bad_response_fails_closed(self, tmp_path, mode) -> None:
        """协议错误不能被识别为有效托管密钥。"""
        token_file = tmp_path / "token"
        token_file.write_text("auth-token", encoding="utf-8")

        # 方法作用：模拟连接超时、空 Secret 和格式不合法响应。
        # Args: request - HTTP 请求。
        # Returns: 测试响应，超时模式抛出 ReadTimeout。
        def handle(request: httpx.Request) -> httpx.Response:
            if mode == "timeout":
                raise httpx.ReadTimeout("private-upstream-details", request=request)
            if mode == "empty":
                return httpx.Response(200, json={"data": {"data": None}})
            return httpx.Response(200, text="x" * (70_000 if mode == "oversized" else 1))

        settings = secret_settings(
            secret_provider="vault", vault_addr="https://vault.example",
            vault_token_file=str(token_file), vault_secret_path="agent",
        )
        with pytest.raises(SecretProviderError) as captured:
            await load_secret_settings(settings, transport=httpx.MockTransport(handle))
        assert "private-upstream-details" not in str(captured.value)

    # 方法作用：验证托管配置注入在所有启动阶段之前完成。
    # Args: self - 实例；monkeypatch - 补丁工具；tmp_path - 临时目录。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_lifespan_injects_before_bootstrap(self, monkeypatch, tmp_path) -> None:
        """启动阶段只接收新配置，原配置不被污染。"""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import src.main as main_module
        from src.app_context import AppContext

        secret_file = tmp_path / "secrets.json"
        secret_file.write_text(json.dumps({"credential_encryption_key": "hosted-" * 8}), encoding="utf-8")
        settings = secret_settings(secret_provider="file", secret_file=str(secret_file))
        context = AppContext(settings)
        bootstrap = AsyncMock()
        shutdown = AsyncMock()
        monkeypatch.setattr(main_module, "bootstrap_all", bootstrap)
        monkeypatch.setattr(main_module, "shutdown_all", shutdown)
        monkeypatch.setattr(main_module, "setup_logging", lambda: None)
        app = SimpleNamespace(state=SimpleNamespace(app_context=context))
        async with main_module.lifespan(app):
            assert context.settings.credential_encryption_key == "hosted-" * 8
            assert bootstrap.await_args.args[0] is context.settings
            assert settings.credential_encryption_key == "initial-key-" * 4
        shutdown.assert_awaited_once_with(context)
