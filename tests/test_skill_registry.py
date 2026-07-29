"""Skill Registry 客户端和安全安装测试。"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest


# 方法作用：构造包含单个合法 Skill 的 ZIP 包。
# Args: name - Skill 名称；version - Skill 版本。
# Returns: ZIP 二进制内容。
def _skill_archive(name: str = "quality", version: str = "1.2.0") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"{name}/SKILL.md",
            f"---\nname: {name}\nversion: {version}\n---\n只执行质量检查",
        )
        archive.writestr(f"{name}/templates/report.txt", "report")
    return buffer.getvalue()


@pytest.mark.asyncio
class TestSkillRegistry:
    """覆盖功能 9.4.3 的目录、校验和与安全安装。"""

    # 方法作用：验证 Registry 目录只返回审核通过且契约完整的版本。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_catalog_filters_unapproved_packages(self) -> None:
        """草稿或被拒绝版本不能出现在可安装列表。"""
        # Arrange
        from src.skill_registry import SkillRegistryClient

        archive = _skill_archive()
        catalog = {"skills": [
            {
                "name": "quality", "version": "1.2.0", "description": "质量检查",
                "download_url": "https://registry.example/quality-1.2.0.zip",
                "sha256": hashlib.sha256(archive).hexdigest(), "status": "approved",
                "api_version": "data-agent/v1",
            },
            {
                "name": "draft", "version": "0.1.0", "description": "草稿",
                "download_url": "https://registry.example/draft.zip",
                "sha256": "a" * 64, "status": "draft", "api_version": "data-agent/v1",
            },
        ]}

        # 方法作用：返回测试目录 JSON。
        # Args: url - 请求 URL；max_bytes - 响应上限。
        # Returns: JSON 二进制内容。
        async def transport(url: str, max_bytes: int) -> bytes:
            del url, max_bytes
            return json.dumps(catalog).encode("utf-8")

        client = SkillRegistryClient(
            "https://registry.example",
            trusted_hosts=["registry.example"],
            transport=transport,
        )

        # Act
        packages = await client.list_packages()

        # Assert
        assert [package.name for package in packages] == ["quality"]

    # 方法作用：验证下载包的 SHA-256 不匹配时立即阻断。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_download_rejects_checksum_mismatch(self) -> None:
        """Registry 元数据不能绕过本地完整性验证。"""
        # Arrange
        from src.skill_registry import RegistrySkillPackage, SkillRegistryClient

        package = RegistrySkillPackage(
            name="quality", version="1.2.0", description="",
            download_url="https://registry.example/quality.zip",
            sha256="0" * 64, api_version="data-agent/v1", status="approved",
        )

        # 方法作用：返回被篡改的测试包。
        # Args: url - 请求 URL；max_bytes - 响应上限。
        # Returns: 非预期 ZIP 二进制。
        async def transport(url: str, max_bytes: int) -> bytes:
            del url, max_bytes
            return _skill_archive()

        client = SkillRegistryClient(
            "https://registry.example",
            trusted_hosts=["registry.example"],
            transport=transport,
        )

        # Act / Assert
        with pytest.raises(ValueError, match="SHA-256"):
            await client.download_package(package)

    # 方法作用：验证通过审核和校验的包安装到可信 private 目录并注入缓存。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_manager_installs_verified_package_into_scoped_directory(self, tmp_path) -> None:
        """Manifest 声明不能覆盖 API 已授权的安装作用域。"""
        # Arrange
        from src.skill_manager import SkillManager
        from src.skill_registry import RegistrySkillPackage

        archive = _skill_archive()
        package = RegistrySkillPackage(
            name="quality", version="1.2.0", description="质量检查",
            download_url="https://registry.example/quality.zip",
            sha256=hashlib.sha256(archive).hexdigest(),
            api_version="data-agent/v1", status="approved",
        )
        manager = SkillManager(
            str(tmp_path / "builtin"),
            managed_dir=str(tmp_path / "managed"),
        )

        # Act
        installed = manager.install_registry_package(
            package,
            archive,
            scope="private",
            tenant_id=3,
            user_id=7,
        )

        # Assert
        expected = tmp_path / "managed" / "private" / "3" / "7" / "quality"
        assert installed.resource_id == "private:3:7:quality"
        assert (expected / "SKILL.md").exists()
        assert (expected / "templates" / "report.txt").exists()
        assert manager.skills[installed.resource_id] is installed

    # 方法作用：验证 Registry ZIP 路径穿越条目在任何写盘前被拒绝。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_manager_rejects_zip_path_traversal(self, tmp_path) -> None:
        """恶意包不能写出受管 Skill 目录。"""
        # Arrange
        from src.skill_manager import SkillManager
        from src.skill_registry import RegistrySkillPackage

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../outside.txt", "bad")
            archive.writestr("quality/SKILL.md", "---\nname: quality\nversion: 1.0.0\n---\nbody")
        payload = buffer.getvalue()
        package = RegistrySkillPackage(
            name="quality", version="1.0.0", description="",
            download_url="https://registry.example/quality.zip",
            sha256=hashlib.sha256(payload).hexdigest(),
            api_version="data-agent/v1", status="approved",
        )
        manager = SkillManager(
            str(tmp_path / "builtin"),
            managed_dir=str(tmp_path / "managed"),
        )

        # Act / Assert
        with pytest.raises(ValueError, match="非法路径"):
            manager.install_registry_package(
                package,
                payload,
                scope="private",
                tenant_id=3,
                user_id=7,
            )
        assert not (tmp_path / "outside.txt").exists()
