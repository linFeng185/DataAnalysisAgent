"""知识库管理页可见范围测试，覆盖 spec/44 §2.1。

管理视角与检索链路的过滤口径不同：检索链路中 super_admin 只召回 system，
管理页则需包含 system + 本租户 + 本人。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.api import auth  # noqa: E402
from src.knowledge.retrieval import build_admin_visible_filters  # noqa: E402


class TestAdminVisibleFilters:
    """验证 spec/44 §2.1：四种角色在管理列表中的可见范围。"""

    @pytest.fixture(autouse=True)
    def _identity(self):
        """统一注入租户身份，用例结束后复位。"""
        tokens = [
            auth._current_role.set("tenant_admin"),
            auth._current_tenant_id.set(3),
            auth._current_user_id.set(3),
        ]
        yield
        auth._current_role.reset(tokens[0])
        auth._current_tenant_id.reset(tokens[1])
        auth._current_user_id.reset(tokens[2])

    # 方法作用：验证租户管理员的默认可见范围不含 system。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_tenant_admin_defaults_to_tenant_and_private(self) -> None:
        """默认展示不含 system，避免平台知识污染租户视图。"""
        # Act
        groups = build_admin_visible_filters()

        # Assert
        visibilities = {group.get("visibility") for group in groups}
        assert visibilities == {"tenant", "private"}

    # 方法作用：验证 include_system 让非超管放宽到只读可见 system。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_include_system_adds_system_group(self) -> None:
        """勾选「显示系统知识」后应追加 system 组。"""
        # Act
        groups = build_admin_visible_filters(include_system=True)

        # Assert
        visibilities = {group.get("visibility") for group in groups}
        assert visibilities == {"system", "tenant", "private"}

    # 方法作用：验证 private 组恒定绑定当前用户。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_private_group_bound_to_current_user(self) -> None:
        """private 组必须带 owner_user_id，否则会串号。"""
        # Act
        groups = build_admin_visible_filters(include_system=True)

        # Assert
        private_group = next(g for g in groups if g["visibility"] == "private")
        assert private_group["owner_user_id"] == 3

    # 方法作用：验证 category 下推到所有分组。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_category_pushed_into_all_groups(self) -> None:
        """分类条件必须下推，否则各范围过滤会不一致。"""
        # Act
        groups = build_admin_visible_filters(category="business_rule", include_system=True)

        # Assert
        assert all(group.get("category") == "business_rule" for group in groups)

    # 方法作用：验证平台超管默认即可见系统知识。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_platform_super_admin_includes_system_by_default(self) -> None:
        """超管是平台操作身份，无需勾选即含 system。"""
        # Arrange
        role_token = auth._current_role.set("super_admin")
        user_token = auth._current_user_id.set(1)

        # Act
        groups = build_admin_visible_filters()

        # Assert
        visibilities = {group.get("visibility") for group in groups}
        assert visibilities == {"system", "tenant", "private"}

        # Cleanup
        auth._current_role.reset(role_token)
        auth._current_user_id.reset(user_token)
