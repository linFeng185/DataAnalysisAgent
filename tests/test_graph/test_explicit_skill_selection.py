"""会话级显式 Skill 选择测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.skill_manager import Skill, SkillManager


class TestExplicitSkillSelection:
    """覆盖功能 18.8.2 的请求级 Skill 授权和激活。"""

    # 方法作用：构造带最小 Manifest 的测试 Skill。
    # Args: name - Skill 名称；resource_id - 复合资源 ID；enabled - 是否启用；owner_user_id - 所有者。
    # Returns: 可注入 SkillManager 的 Skill。
    @staticmethod
    def _skill(
        name: str,
        resource_id: str,
        *,
        enabled: bool = True,
        owner_user_id: int = 7,
    ) -> Skill:
        return Skill(
            name=name,
            version="1.0.0",
            description="测试技能",
            triggers={},
            depends_on={},
            tools=[],
            system_prompt_override="只执行数据质量检查",
            enabled=enabled,
            scope="private",
            tenant_id=3,
            owner_user_id=owner_user_id,
            resource_id=resource_id,
        )

    # 方法作用：验证只能解析当前身份可见且已启用的复合资源 ID。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_manager_resolves_visible_enabled_resource_ids(self, tmp_path) -> None:
        """显式选择不能越权激活其他用户 Skill。"""
        # Arrange
        manager = SkillManager(str(tmp_path / "builtin"), managed_dir=str(tmp_path / "managed"))
        visible = self._skill("quality", "private:3:7:quality")
        hidden = self._skill("hidden", "private:3:8:hidden", owner_user_id=8)
        manager.add_skill(visible)
        manager.add_skill(hidden)

        # Act
        selected = manager.resolve_requested_skills(
            ["private:3:7:quality"],
            tenant_id=3,
            user_id=7,
        )

        # Assert
        assert selected == [visible]
        with pytest.raises(PermissionError, match="不可见"):
            manager.resolve_requested_skills(
                ["private:3:8:hidden"],
                tenant_id=3,
                user_id=7,
            )

    # 方法作用：验证显式 Skill 优先于关键词自动匹配且仍构建授权工具预算。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_activation_uses_explicit_selection(self, monkeypatch) -> None:
        """用户已选择 Skill 时不得再自动加入未授权的匹配项。"""
        # Arrange
        import src.skill_manager as skill_module
        from src.graph.skill_activation import activate_skills

        selected = self._skill("quality", "private:3:7:quality")
        manager = SimpleNamespace(
            resolve_requested_skills=MagicMock(return_value=[selected]),
            match_skills=MagicMock(),
            build_skill_prompt=MagicMock(return_value="prompt"),
            get_active_tools=MagicMock(return_value=[]),
            get_tool_budget=MagicMock(return_value=5),
        )
        monkeypatch.setattr(skill_module, "get_skill_manager", lambda: manager)

        # Act
        result = activate_skills({
            "user_query": "检查数据",
            "intent": "query",
            "enabled_skill_ids": ["private:3:7:quality"],
            "tenant_id": 3,
            "user_id": 7,
        })

        # Assert
        assert result["activated_skills"] == ["quality"]
        manager.resolve_requested_skills.assert_called_once()
        manager.match_skills.assert_not_called()
