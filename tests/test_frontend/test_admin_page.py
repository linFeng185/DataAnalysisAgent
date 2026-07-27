"""平台管理前端静态契约测试，覆盖功能 21.4.1-21.4.3。"""

from __future__ import annotations

import logging
from pathlib import Path


logger = logging.getLogger(__name__)


class TestAdminFrontend:
    """验证强制登录、角色菜单和平台管理页面。"""

    # 方法作用：验证 App 仅向超级管理员注册平台管理菜单和路由。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_super_admin_route_and_menu(self) -> None:
        """普通角色不能通过侧栏或直接路由进入平台后台。"""
        logger.debug("test_super_admin_route_and_menu 入口")
        source = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

        assert "AdminPage" in source
        assert "user?.role === 'super_admin'" in source
        assert 'path="/admin"' in source
        assert "平台管理" in source
        assert "logout" in source
        logger.info("test_super_admin_route_and_menu 完成")

    # 方法作用：验证登录页根据后端开关提供注册入口。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_login_page_honors_registration_switch(self) -> None:
        """注册关闭时前端不能显示无效入口。"""
        logger.debug("test_login_page_honors_registration_switch 入口")
        login_source = Path("frontend/src/pages/LoginPage.tsx").read_text(encoding="utf-8")
        auth_source = Path("frontend/src/hooks/AuthContext.tsx").read_text(encoding="utf-8")

        assert "registrationEnabled" in auth_source
        assert "register" in auth_source
        assert "registrationEnabled" in login_source
        logger.info("test_login_page_honors_registration_switch 完成")

    # 方法作用：验证平台后台包含租户、用户和安全配置三个工作区。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_admin_page_has_required_workspaces(self) -> None:
        """平台治理工作流应集中在一个安静的管理页面。"""
        logger.debug("test_admin_page_has_required_workspaces 入口")
        source = Path("frontend/src/pages/AdminPage.tsx").read_text(encoding="utf-8")

        assert "/admin/tenants" in source
        assert "/admin/users" in source
        assert "/admin/config" in source
        assert "租户管理" in source
        assert "用户管理" in source
        assert "安全配置" in source
        logger.info("test_admin_page_has_required_workspaces 完成")

    # 方法作用：验证平台管理页包含访问策略和接口 IP 黑白名单维护入口。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_admin_page_manages_access_policies_and_ip_rules(self) -> None:
        """超级管理员应能在页面维护动态策略和 CIDR 黑白名单。"""
        logger.debug("test_admin_page_manages_access_policies_and_ip_rules 入口")
        source = Path("frontend/src/pages/AdminPage.tsx").read_text(encoding="utf-8")

        assert "/admin/access-policies" in source
        assert "/ip-rules" in source
        assert "访问策略" in source
        assert "IP 黑白名单" in source
        assert "auth_mode" in source
        assert "access_log_mode" in source
        logger.info("test_admin_page_manages_access_policies_and_ip_rules 完成")
