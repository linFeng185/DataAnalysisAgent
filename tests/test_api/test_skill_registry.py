"""Skill Registry API 测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
class TestSkillRegistryApi:
    """覆盖功能 9.4.3 的目录和安装 API。"""

    # 方法作用：构造 API 测试使用的已审核 Registry 包。
    # Args: 无。
    # Returns: RegistrySkillPackage。
    @staticmethod
    def _package():
        from src.skill_registry import RegistrySkillPackage

        return RegistrySkillPackage(
            name="quality", version="1.2.0", description="质量检查",
            download_url="https://registry.example/quality.zip",
            sha256="a" * 64, api_version="data-agent/v1", status="approved",
        )

    # 方法作用：验证目录 API 在未配置时不访问网络并返回明确状态。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_list_registry_returns_unconfigured_state(self, monkeypatch) -> None:
        """默认部署不应因没有中心 Registry 而报错。"""
        # Arrange
        import src.api.routes as routes
        import src.config as config_module

        monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(skill_registry_url=""))

        # Act
        result = await routes.list_skill_registry()

        # Assert
        assert result == {"configured": False, "skills": [], "total": 0}

    # 方法作用：验证安装 API 先做作用域授权再下载并安装指定审核版本。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_install_registry_skill_uses_authorized_scope(self, monkeypatch) -> None:
        """请求体 scope 不能绕过现有三级作用域权限。"""
        # Arrange
        import src.api.routes as routes
        import src.api.routes._helpers as helpers
        import src.config as config_module
        import src.skill_manager as manager_module
        import src.skill_registry as registry_module
        from src.api.schemas import SkillRegistryInstallRequest

        package = self._package()
        client = SimpleNamespace(
            list_packages=AsyncMock(return_value=[package]),
            download_package=AsyncMock(return_value=b"zip"),
        )
        installed = SimpleNamespace(name="quality", version="1.2.0", resource_id="private:3:7:quality")
        manager = SimpleNamespace(install_registry_package=MagicMock(return_value=installed))
        monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(skill_registry_url="https://registry.example"))
        monkeypatch.setattr(registry_module, "get_skill_registry_client", lambda: client)
        monkeypatch.setattr(manager_module, "get_skill_manager", lambda: manager)
        monkeypatch.setattr(helpers, "_authorize_extension_scope", lambda scope: ("private", 3, 7, "analyst"))
        request = SkillRegistryInstallRequest(version="1.2.0", scope="private")

        # Act
        result = await routes.install_registry_skill("quality", request)

        # Assert
        assert result["resource_id"] == "private:3:7:quality"
        client.download_package.assert_awaited_once_with(package)
        manager.install_registry_package.assert_called_once_with(
            package,
            b"zip",
            scope="private",
            tenant_id=3,
            user_id=7,
        )
