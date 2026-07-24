"""统一 YAML 配置目录回归测试，覆盖功能 21.1.1-21.1.3。"""

from __future__ import annotations

import logging
from pathlib import Path


logger = logging.getLogger(__name__)


class TestUnifiedConfig:
    """覆盖统一配置文件、环境覆盖和旧 MCP 文件收敛。"""

    # 方法作用：验证 YAML 提供默认值且环境变量具有更高优先级。
    # Args: self - pytest 测试类实例；tmp_path - 临时目录；monkeypatch - 环境变量补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_yaml_defaults_and_environment_override(self, tmp_path, monkeypatch) -> None:
        """部署环境应能覆盖统一配置中的非敏感默认值。"""
        logger.debug("test_yaml_defaults_and_environment_override 入口")
        config_path = tmp_path / "app.yaml"
        config_path.write_text(
            "env: dev\n"
            "registration_enabled: false\n"
            "login_lockout_threshold: 5\n"
            "mcp_servers:\n"
            "  demo:\n"
            "    enabled: false\n"
            "    transport: stdio\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
        monkeypatch.setenv("REGISTRATION_ENABLED", "true")

        from src.config import Settings

        settings = Settings(_env_file=None)

        assert settings.env == "dev"
        assert settings.registration_enabled is True
        assert settings.login_lockout_threshold == 5
        assert settings.mcp_servers["demo"]["transport"] == "stdio"
        logger.info("test_yaml_defaults_and_environment_override 完成")

    # 方法作用：验证仓库统一配置列出全部 Settings 字段并内嵌 MCP 配置。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_repository_app_yaml_is_complete(self) -> None:
        """配置目录不能继续依赖独立 MCP YAML。"""
        logger.debug("test_repository_app_yaml_is_complete 入口")
        import yaml

        from src.config import Settings

        config = yaml.safe_load(Path("config/app.yaml").read_text(encoding="utf-8"))

        assert set(Settings.model_fields).issubset(config)
        assert "mcp_servers" in config
        assert not Path("config/mcp_servers.yaml").exists()
        logger.info("test_repository_app_yaml_is_complete 完成")

    # 方法作用：验证可选数据源文件缺失时 Provider 返回空列表。
    # Args: self - pytest 测试类实例；tmp_path - 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_missing_datasources_yaml_is_allowed(self, tmp_path) -> None:
        """页面维护数据源时无需预先创建 datasources.yaml。"""
        logger.debug("test_missing_datasources_yaml_is_allowed 入口")
        from src.datasource.providers.external import ExternalDataSourceProvider

        provider = ExternalDataSourceProvider.from_yaml(str(tmp_path / "missing.yaml"))

        assert provider._sources == {}  # noqa: SLF001
        logger.info("test_missing_datasources_yaml_is_allowed 完成")
