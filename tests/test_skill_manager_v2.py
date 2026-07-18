"""Phase E Skill Manifest v2 校验测试。"""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_skill(root: Path, name: str, keyword: str) -> None:
    """写入最小 Skill 清单供作用域发现测试使用。"""
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "version: 1.0.0\n"
        f"description: {name}\n"
        "triggers:\n"
        f"  keywords: [{keyword}]\n"
        "---\n说明\n",
        encoding="utf-8",
    )


class TestSkillManifestV2:
    """覆盖权限、资源预算和输入资产类型校验。"""

    def test_parse_manifest_v2_preserves_permissions_and_resources(self, tmp_path):
        """Manifest v2 字段应进入 Skill 对象，而不是被静默丢弃。"""
        # Arrange
        from src.skill_manager import SkillManager

        skill_dir = tmp_path / "forecast"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "api_version: data-agent/v2\n"
            "name: forecast\n"
            "version: 2.0.0\n"
            "capabilities: [timeseries.forecast]\n"
            "accepts: [table_file]\n"
            "permissions:\n"
            "  network: []\n"
            "  files: read_asset_only\n"
            "  datasources: read_only\n"
            "resources:\n"
            "  timeout_seconds: 30\n"
            "  max_tool_calls: 5\n"
            "---\n说明\n",
            encoding="utf-8",
        )

        # Act
        skill = SkillManager()._parse_skill_manifest(skill_dir / "SKILL.md")

        # Assert
        assert skill.api_version == "data-agent/v2"
        assert skill.capabilities == ["timeseries.forecast"]
        assert skill.permissions["files"] == "read_asset_only"
        assert skill.resources["max_tool_calls"] == 5

    def test_validate_skill_request_blocks_unauthorized_asset_and_budget(self):
        """请求资产类型、网络权限和调用预算不满足时必须拒绝。"""
        # Arrange
        from src.skill_manager import Skill, validate_skill_request

        skill = Skill(
            name="forecast", version="2.0.0", description="", triggers={}, depends_on={}, tools=[],
            system_prompt_override="", accepts=["table_file"],
            permissions={"network": [], "files": "read_asset_only"},
            resources={"max_tool_calls": 2},
        )

        # Act / Assert
        assert validate_skill_request(skill, asset_kind="document", tool_calls=1) is False
        assert validate_skill_request(skill, asset_kind="table_file", tool_calls=3) is False
        assert validate_skill_request(skill, asset_kind="table_file", tool_calls=1) is True


class TestSkillScopeIsolation:
    """覆盖功能 9.1.11：Skill system/tenant/private 三级隔离。"""

    # 方法作用：验证当前用户只能看到系统、本租户和本人 Skill。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_visible_skills_filter_tenant_and_owner(self, tmp_path: Path) -> None:
        """其他租户和同租户其他用户的 Skill 均不得进入匹配候选。"""
        # Arrange
        from src.skill_manager import SkillManager

        builtin = tmp_path / "builtin"
        managed = tmp_path / "managed"
        _write_skill(builtin, "system-report", "报告")
        _write_skill(managed / "tenant" / "4", "tenant-report", "报告")
        _write_skill(managed / "tenant" / "5", "other-tenant", "报告")
        _write_skill(managed / "private" / "4" / "7", "my-report", "报告")
        _write_skill(managed / "private" / "4" / "8", "other-user", "报告")
        manager = SkillManager(str(builtin), managed_dir=str(managed))

        # Act
        await manager.discover()
        visible = manager.get_visible_skills(tenant_id=4, user_id=7)

        # Assert
        assert {skill.name for skill in visible} == {
            "system-report", "tenant-report", "my-report",
        }

    # 方法作用：验证同名 Skill 按个人、租户、系统顺序确定唯一可见版本。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_private_skill_precedes_tenant_and_system_same_name(self, tmp_path: Path) -> None:
        """高优先级可见 Skill 应覆盖同名低优先级候选，但不得覆盖其他名称。"""
        # Arrange
        from src.skill_manager import SkillManager

        builtin = tmp_path / "builtin"
        managed = tmp_path / "managed"
        _write_skill(builtin, "report", "系统报告")
        _write_skill(managed / "tenant" / "4", "report", "租户报告")
        _write_skill(managed / "private" / "4" / "7", "report", "个人报告")
        manager = SkillManager(str(builtin), managed_dir=str(managed))
        await manager.discover()

        # Act
        matched = manager.match_skills(
            "生成个人报告", "query", [], tenant_id=4, user_id=7,
        )

        # Assert
        assert len(matched) == 1
        assert matched[0].scope == "private"
        assert matched[0].tenant_id == 4
        assert matched[0].owner_user_id == 7

    # 方法作用：验证上传目录由受信任身份与作用域计算而非 Manifest 声明。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_upload_directory_uses_trusted_scope_identity(self, tmp_path: Path) -> None:
        """tenant/private 上传必须落入当前身份命名空间。"""
        # Arrange
        from src.skill_manager import SkillManager

        manager = SkillManager(str(tmp_path / "builtin"), managed_dir=str(tmp_path / "managed"))

        # Act / Assert
        assert manager.get_upload_dir("tenant", tenant_id=4, user_id=7) == (
            tmp_path / "managed" / "tenant" / "4"
        )
        assert manager.get_upload_dir("private", tenant_id=4, user_id=7) == (
            tmp_path / "managed" / "private" / "4" / "7"
        )
