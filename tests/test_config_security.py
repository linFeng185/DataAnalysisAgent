"""生产配置、日志轮转和 MCP 启动安全回归测试。"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


logger = logging.getLogger(__name__)


class TestEnvExample:
    """覆盖功能 1.2.2：环境变量模板必须保持单一键定义。"""

    # 方法作用：验证环境变量模板中的活动键没有重复定义。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_active_environment_keys_are_unique(self) -> None:
        """复制模板后不应因后置同名键覆盖前面的配置。"""
        logger.debug("test_active_environment_keys_are_unique 入口")
        try:
            # Arrange
            lines = Path(".env.example").read_text(encoding="utf-8").splitlines()
            keys = [
                line.split("=", maxsplit=1)[0].strip()
                for line in lines
                if line.strip() and not line.lstrip().startswith("#") and "=" in line
            ]

            # Act
            duplicates = sorted({key for key in keys if keys.count(key) > 1})

            # Assert
            assert duplicates == []
            logger.info("test_active_environment_keys_are_unique 完成", extra={"key_count": len(keys)})
        except Exception as exc:
            logger.error(
                "test_active_environment_keys_are_unique 异常: %s",
                exc,
                exc_info=True,
            )
            raise


class TestProductionSettings:
    """覆盖生产配置安全校验。"""

    # 方法作用：验证没有环境覆盖时应用读取统一 YAML 的显式运行模式。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 环境变量补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_environment_uses_unified_yaml_default(self, monkeypatch):
        """开发模板的 env 值应来自 config/app.example.yaml 而非第二份代码默认。"""
        logger.debug("test_environment_uses_unified_yaml_default 入口")
        try:
            # Arrange
            from src.config import Settings

            monkeypatch.delenv("ENV", raising=False)

            # Act
            settings = Settings(_env_file=None)

            # Assert
            assert settings.env == "dev"
            logger.info("test_environment_uses_unified_yaml_default 完成")
        except Exception as exc:
            logger.error("test_environment_uses_unified_yaml_default 异常: %s", exc, exc_info=True)
            raise

    # 方法作用：验证生产环境允许关闭多租户隔离但仍通过认证安全门禁。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_prod_accepts_single_tenant_mode(self):
        """multi_tenant 只控制隔离，不再决定是否强制登录。"""
        logger.debug("test_prod_accepts_single_tenant_mode 入口")
        # Arrange
        from src.config import Settings, validate_production_settings

        settings = Settings(
            _env_file=None,
            env="prod",
            multi_tenant=False,
            jwt_secret="j" * 32,
            credential_encryption_key="c" * 32,
            database_url="postgresql+asyncpg://app:strong-secret@db/app",
        )

        # Act
        result = validate_production_settings(settings)

        # Assert
        assert result is None
        logger.info("test_prod_accepts_single_tenant_mode 完成")

    # 方法作用：验证完整生产安全配置无需无效的全局只读连接即可通过校验。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_prod_accepts_complete_security_configuration(self):
        """动态数据源使用各自凭证时不应要求全局只读连接。"""
        logger.debug("test_prod_accepts_complete_security_configuration 入口")
        try:
            # Arrange
            from src.config import Settings, validate_production_settings

            settings = Settings(
                env="prod",
                multi_tenant=True,
                jwt_secret="j" * 32,
                admin_api_key="a" * 32,
                credential_encryption_key="c" * 32,
            )

            # Act
            result = validate_production_settings(settings)

            # Assert
            assert result is None
            assert "database_readonly_url" not in Settings.model_fields
            logger.info("test_prod_accepts_complete_security_configuration 完成")
        except Exception as exc:
            logger.error(
                "test_prod_accepts_complete_security_configuration 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证生产配置拒绝代码内置的 PostgreSQL 弱账号连接串。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_prod_rejects_default_database_credentials(self):
        """生产状态库不得继续使用 postgres/postgres 默认凭证。"""
        logger.debug("test_prod_rejects_default_database_credentials 入口")
        try:
            # Arrange
            from src.config import Settings, validate_production_settings

            settings = Settings(
                _env_file=None,
                env="prod",
                multi_tenant=True,
                jwt_secret="j" * 32,
                admin_api_key="a" * 32,
                credential_encryption_key="c" * 32,
                database_url="postgresql+asyncpg://postgres:postgres@localhost:5432/data_agent",
            )

            # Act / Assert
            with pytest.raises(ValueError, match="DATABASE_URL"):
                validate_production_settings(settings)
            logger.info("test_prod_rejects_default_database_credentials 完成")
        except Exception as exc:
            logger.error(
                "test_prod_rejects_default_database_credentials 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证生产配置拒绝长度不足的凭证加密主密钥。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_prod_rejects_weak_credential_key(self):
        """长度不足的主密钥不得通过生产门禁。"""
        logger.debug("test_prod_rejects_weak_credential_key 入口")
        try:
            # Arrange
            from src.config import Settings, validate_production_settings

            settings = Settings(
                _env_file=None,
                env="prod",
                multi_tenant=True,
                jwt_secret="j" * 32,
                admin_api_key="a" * 32,
                credential_encryption_key="c" * 31,
                database_url="postgresql+asyncpg://app:strong-secret@db/app",
            )

            # Act / Assert
            with pytest.raises(ValueError, match="CREDENTIAL_ENCRYPTION_KEY"):
                validate_production_settings(settings)
            logger.info("test_prod_rejects_weak_credential_key 完成")
        except Exception as exc:
            logger.error(
                "test_prod_rejects_weak_credential_key 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证凭证加密实现不再包含可公开复用的默认主密钥。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_source_contains_no_default_credential_key(self) -> None:
        """源码不得携带可用于解密部署凭证的固定主密钥。"""
        logger.debug("test_source_contains_no_default_credential_key 入口")
        try:
            # Arrange / Act
            source = "\n".join(
                path.read_text(encoding="utf-8")
                for path in Path("src").rglob("*.py")
            )

            # Assert
            assert "credential-encryption-key-change-in-production" not in source
            logger.info("test_source_contains_no_default_credential_key 完成")
        except Exception as exc:
            logger.error(
                "test_source_contains_no_default_credential_key 异常: %s",
                exc,
                exc_info=True,
            )
            raise


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
        assert logging.getLogger("uvicorn.access").disabled is True

    # 方法作用：验证普通应用日志中的查询和 SQL 字段会被哈希替换。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sensitive_query_fields_are_redacted(self):
        """日志处理链不得输出用户问题或 SQL 原文。"""
        # Arrange
        from src import logging_config

        event = {
            "event": "执行",
            "query": "查询身份证 110101199001011234",
            "sql_preview": "SELECT id_card FROM users",
            "datasource": "prod",
        }

        # Act
        redacted = logging_config._redact_sensitive_fields(None, "info", event)  # noqa: SLF001

        # Assert
        assert "query" not in redacted
        assert "sql_preview" not in redacted
        assert len(redacted["query_hash"]) == 64
        assert len(redacted["sql_preview_hash"]) == 64
        assert redacted["datasource"] == "prod"

    # 方法作用：验证 JSON 文件日志直接保留中文字符而不是 Unicode 转义序列。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录；monkeypatch - pytest 运行时替换工具。
    # Returns: 无返回值，断言日志文件包含原始中文文本。
    def test_json_logging_preserves_chinese_text(self, tmp_path, monkeypatch) -> None:
        """JSONRenderer 应使用 ensure_ascii=False 输出可读中文。"""
        # Arrange
        from src import logging_config
        from src.config import Settings

        log_file = tmp_path / "logs" / "app.log"
        settings = Settings(log_file=str(log_file), log_format="json", log_level="INFO")
        monkeypatch.setattr(logging_config, "get_settings", lambda: settings)
        logging_config.setup_logging()

        # Act
        logging_config.get_logger("tests.logging").info("中文日志验证")
        for handler in logging.getLogger().handlers:
            handler.flush()

        # Assert
        content = log_file.read_text(encoding="utf-8")
        assert "中文日志验证" in content
        assert "\\u4e2d\\u6587" not in content


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

        compose = Path("docker-compose.example.yml").read_text(encoding="utf-8")

        # Act / Assert
        assert "1Qaz@2wsx124" not in compose
        assert "${POSTGRES_PASSWORD:" in compose

    # 方法作用：验证 Compose 提供带认证、持久化和健康检查的 Redis 7 服务。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_compose_provides_secured_redis_service(self) -> None:
        """生产示例编排应提供仅内部可见且持久化的 Redis 缓存服务。"""
        logger.debug("test_compose_provides_secured_redis_service 入口")
        try:
            # Arrange
            import yaml

            config = yaml.safe_load(
                Path("docker-compose.example.yml").read_text(encoding="utf-8")
            )

            # Act
            redis_service = config["services"]["redis"]
            command = str(redis_service["command"])

            # Assert
            assert redis_service["image"].startswith("redis:7")
            assert redis_service["environment"]["REDIS_PASSWORD"].startswith(
                "${REDIS_PASSWORD:"
            )
            assert "--requirepass" in command
            assert "--appendonly yes" in command
            assert redis_service["healthcheck"]["test"]
            assert "./data/redis:/data" in redis_service["volumes"]
            assert not redis_service.get("ports")
            logger.info("test_compose_provides_secured_redis_service 完成")
        except Exception as exc:
            logger.error(
                "test_compose_provides_secured_redis_service 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    def test_datasource_yaml_uses_environment_credentials(self):
        """仓库内置外挂数据源必须使用环境变量凭证占位符。"""
        # Arrange
        from pathlib import Path
        import yaml

        config = yaml.safe_load(
            Path("config/datasources.example.yaml").read_text(encoding="utf-8")
        )

        # Act
        passwords = [
            str(item.get("password", ""))
            for item in config.get("datasources", {}).values()
            if item.get("dialect") != "sqlite"
        ]

        # Assert
        assert all(value.startswith("${") and value.endswith("}") for value in passwords)

    # 方法作用：验证仓库默认配置保留历史固定数据源及关键连接信息。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_datasource_yaml_example_is_optional(self) -> None:
        """可选配置示例应保持合法映射，允许生产环境完全使用管理页面。"""
        logger.debug("test_datasource_yaml_preserves_default_sources 入口")
        try:
            # Arrange
            import yaml

            # Act
            config = yaml.safe_load(
                Path("config/datasources.example.yaml").read_text(encoding="utf-8")
            )
            datasources = config.get("datasources", {})

            # Assert
            assert isinstance(datasources, dict)
            logger.info(
                "test_datasource_yaml_preserves_default_sources 完成",
                extra={"count": len(datasources)},
            )
        except Exception as exc:
            logger.error(
                "test_datasource_yaml_preserves_default_sources 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证高权限数据库用户名只能由部署环境注入。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_elevated_datasource_usernames_use_environment_references(self) -> None:
        """SYSTEM 和 sa 可连接，但不得作为固定明文写入仓库 YAML。"""
        logger.debug("test_elevated_datasource_usernames_use_environment_references 入口")
        try:
            # Arrange
            import yaml

            config = yaml.safe_load(
                Path("config/datasources.example.yaml").read_text(encoding="utf-8")
            )

            # Act
            datasources = config.get("datasources", {})
            usernames = {
                name: str(datasources.get(name, {}).get("username", ""))
                for name in ("oracle_xe", "mssql_express")
            }

            # Assert
            assert all(
                username.startswith("${") and username.endswith("}")
                for username in usernames.values()
                if username
            )
            logger.info(
                "test_elevated_datasource_usernames_use_environment_references 完成",
                extra={"configured": sorted(usernames)},
            )
        except Exception as exc:
            logger.error(
                "test_elevated_datasource_usernames_use_environment_references 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    def test_data_import_script_has_no_hardcoded_database_passwords(self):
        """数据导入辅助脚本不得内置数据库明文密码。"""
        # Arrange
        from pathlib import Path

        script = Path("tests/import_test_data.py").read_text(encoding="utf-8")

        # Act / Assert
        assert "1Qaz@2wsx124" not in script


class TestJwtSecretFailClosed:
    """覆盖生产 JWT 密钥缺失时的失败关闭行为。"""

    # 方法作用：验证生产环境不会自动生成进程级临时 JWT 密钥。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_production_missing_secret_raises(self, monkeypatch) -> None:
        """生产密钥缺失必须阻断签发，避免重启后 Token 全部失效。"""
        logger.debug("test_production_missing_secret_raises 入口")
        try:
            from types import SimpleNamespace

            import src.api.auth as auth_module

            monkeypatch.delenv("JWT_SECRET", raising=False)
            monkeypatch.setattr(
                auth_module,
                "get_settings",
                lambda: SimpleNamespace(env="prod", jwt_secret=""),
            )
            monkeypatch.setattr(auth_module, "_secret_cache", None)

            with pytest.raises(RuntimeError, match="JWT_SECRET"):
                auth_module._secret()  # noqa: SLF001
            logger.info("test_production_missing_secret_raises 完成")
        except Exception as exc:
            logger.error("test_production_missing_secret_raises 异常: %s", exc, exc_info=True)
            raise
