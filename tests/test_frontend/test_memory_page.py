"""「我的长期记忆」页面契约测试，覆盖 spec/44 §5.3。

事实依据：前端没有 JS 测试框架，沿用仓库既有的源码静态断言模式
（tests/test_frontend/test_report_templates.py 为范例）；tests/test_frontend/
既有文件覆盖报表模板主题，无记忆页面主题；本文件只读取 .tsx 源码做断言，
不读写任何数据文件。
"""

from __future__ import annotations

import re
from pathlib import Path

PAGE_SOURCE = Path("frontend/src/pages/MemoryPage.tsx").read_text(encoding="utf-8")
APP_SOURCE = Path("frontend/src/App.tsx").read_text(encoding="utf-8")


class TestMemoryPage:
    """验证页面渲染要素。"""

    # 方法作用：验证页面调用记忆接口且不传归属参数。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_page_queries_memory_endpoint(self) -> None:
        """列表只查本人记忆，不带 owner/tenant 参数。"""
        # Act / Assert
        assert "/memory?" in PAGE_SOURCE
        assert "owner_user_id" not in PAGE_SOURCE
        assert "tenant_id" not in PAGE_SOURCE

    # 方法作用：验证页面不提供写操作。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_page_has_no_edit_action(self) -> None:
        """记忆只读 + 删除，手工编辑会污染检索上下文。"""
        # Act / Assert
        assert "patch(" not in PAGE_SOURCE
        assert "post(" not in PAGE_SOURCE

    # 方法作用：验证删除走二次确认。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_page_delete_uses_confirm(self) -> None:
        """破坏性操作需要 Popconfirm。"""
        # Act / Assert
        assert "Popconfirm" in PAGE_SOURCE
        assert re.search(r"del\(`/memory/", PAGE_SOURCE)

    # 方法作用：验证空态文案存在。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_page_empty_state_copy(self) -> None:
        """空态需引导用户理解记忆来源。"""
        # Act / Assert
        assert "暂无长期记忆" in PAGE_SOURCE


class TestMemoryMenuWiring:
    """验证菜单与路由接线。"""

    # 方法作用：验证路由与懒加载已注册。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_route_registered(self) -> None:
        """路由缺失会导致菜单 404。"""
        # Act / Assert
        assert "lazy(() => import('./pages/MemoryPage'))" in APP_SOURCE
        assert 'path="/memory"' in APP_SOURCE

    # 方法作用：验证菜单项对所有角色可见。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_menu_visible_to_all_roles(self) -> None:
        """记忆是个人数据，不按角色收敛。"""
        # Act
        menu_section = APP_SOURCE.split("navigationItems")[1].split("].filter(")[0]

        # Assert
        assert "key: 'memory'" in menu_section


class TestMemoryTypes:
    """验证记忆类型选项覆盖后端枚举。"""

    # 方法作用：验证五种记忆类型都在筛选选项中。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_all_memory_types_listed(self) -> None:
        """类型遗漏会导致用户无法筛选。"""
        # Act / Assert
        for value in ("sql_template", "user_preference", "learned_pattern",
                      "correction", "project_rule"):
            assert value in PAGE_SOURCE

    # 方法作用：验证详情入口读取 payload 中的 SQL。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_detail_renders_payload(self) -> None:
        """SQL 模板记忆的价值在 payload，必须展示。"""
        # Act / Assert
        assert "payload" in PAGE_SOURCE
