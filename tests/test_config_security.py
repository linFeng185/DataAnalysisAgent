"""生产配置、日志轮转和 MCP 启动安全回归测试。"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from unittest.mock import AsyncMock

import pytest


class TestProductionSettings:
    """覆盖生产配置安全校验。"""

    def test_prod_rejects_anonymous_mode(self):
        """生产环境必须启用多租户认证。"""
        # Arrange
        from src.config import Settings, validate_production_settings

        settings = Settings(env="prod", multi_tenant=False)

        # Act / Assert
        with pytest.raises(ValueError, match="MULTI_TENANT"):
            validate_production_settings(settings)

    def test_prod_accepts_complete_security_configuration(self):
        """完整生产安全配置应通过校验。"""
        # Arrange
        from src.config import Settings, validate_production_settings

        settings = Settings(
            env="prod",
            multi_tenant=True,
            jwt_secret="j" * 32,
            admin_api_key="a" * 32,
            credential_encryption_key="c" * 32,
            database_readonly_url="postgresql+asyncpg://reader:secret@db/app",
        )

        # Act
        result = validate_production_settings(settings)

        # Assert
        assert result is None


class TestLoggingRetention:
    """覆盖日志文件保留七天要求。"""

    def test_setup_logging_adds_seven_day_rotating_handler(self, tmp_path, monkeypatch):
        """日志配置应每天轮转并仅保留七份备份。"""
        # Arrange
        from src.config import Settings
        from src import logging_config

        log_file = tmp_path / "logs" / "app.log"
        settings = Settings(log_file=str(log_file), log_format="json", log_level="INFO")
        monkeypatch.setattr(logging_config, "get_settings", lambda: settings)

        # Act
        logging_config.setup_logging()

        # Assert
        handlers = [
            handler
            for handler in logging.getLogger().handlers
            if isinstance(handler, TimedRotatingFileHandler)
        ]
        assert len(handlers) == 1
        assert handlers[0].backupCount == 7
        assert handlers[0].when == "D"


class TestMCPStartupSafety:
    """覆盖默认 MCP 服务禁用行为。"""

    async def test_connect_all_skips_disabled_server(self, tmp_path):
        """enabled=false 的 MCP 服务不应启动任何子进程。"""
        # Arrange
        from src.mcp_client.client_manager import MCPClientManager

        config_path = tmp_path / "mcp.yaml"
        config_path.write_text(
            "mcp_servers:\n"
            "  filesystem:\n"
            "    enabled: false\n"
            "    transport: stdio\n"
            "    command: npx\n"
            "    args: ['-y', 'untrusted-package']\n",
            encoding="utf-8",
        )
        manager = MCPClientManager(str(config_path))
        manager._connect_single = AsyncMock()  # noqa: SLF001

        # Act
        await manager.connect_all()

        # Assert
        manager._connect_single.assert_not_awaited()  # noqa: SLF001
        assert manager._server_configs == {}  # noqa: SLF001


class TestDockerSecrets:
    """覆盖 Docker Compose 明文密码回归。"""

    def test_compose_has_no_committed_shared_password(self):
        """Compose 文件不得包含审计中发现的共享明文密码。"""
        # Arrange
        from pathlib import Path

        compose = Path("docker-compose.yml").read_text(encoding="utf-8")

        # Act / Assert
        assert "1Qaz@2wsx124" not in compose
        assert "${MYSQL_ROOT_PASSWORD:" in compose

    def test_datasource_yaml_uses_environment_credentials(self):
        """外挂数据源配置只能保存环境变量凭证占位符。"""
        # Arrange
        from pathlib import Path
        import yaml

        config = yaml.safe_load(Path("config/datasources.yaml").read_text(encoding="utf-8"))

        # Act
        passwords = [
            str(item.get("password", ""))
            for item in config.get("datasources", {}).values()
            if item.get("dialect") != "sqlite"
        ]

        # Assert
        assert passwords
        assert all(value.startswith("${") and value.endswith("}") for value in passwords)

    def test_data_import_script_has_no_hardcoded_database_passwords(self):
        """数据导入辅助脚本不得内置数据库明文密码。"""
        # Arrange
        from pathlib import Path

        script = Path("tests/import_test_data.py").read_text(encoding="utf-8")

        # Act / Assert
        assert "1Qaz@2wsx124" not in script
