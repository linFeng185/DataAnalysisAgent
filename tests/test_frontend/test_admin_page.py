"""平台管理前端静态契约测试，覆盖功能 21.4.1-21.4.3。"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class TestAdminFrontend:
    """验证强制登录、角色菜单和平台管理页面。"""

    # 方法作用：验证 App 向超级管理员和租户管理员注册管理菜单和路由。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_super_admin_route_and_menu(self) -> None:
        """普通角色不能通过侧栏或直接路由进入平台后台。"""
        logger.debug("test_super_admin_route_and_menu 入口")
        source = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

        assert "AdminPage" in source
        assert "['super_admin', 'tenant_admin'].includes(user?.role || '')" in source
        assert 'path="/admin"' in source
        assert "管理工作区" in source
        assert "logout" in source
        logger.info("test_super_admin_route_and_menu 完成")

    # 方法作用：验证登录页固定使用租户编码登录且不再暴露公开注册。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_login_page_requires_tenant_code_without_registration(self) -> None:
        """所有账号都必须输入 tenant_code，注册接口永久隐藏。"""
        logger.debug("test_login_page_honors_registration_switch 入口")
        login_source = Path("frontend/src/pages/LoginPage.tsx").read_text(encoding="utf-8")
        auth_source = Path("frontend/src/hooks/AuthContext.tsx").read_text(encoding="utf-8")

        assert "tenant_code" in auth_source
        assert "tenant_code" in login_source
        assert "register" not in auth_source
        assert "register" not in login_source
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
        assert "当前租户用户" in source
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

    # 方法作用：验证管理页包含平台目录与租户命名连接配置入口。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_admin_page_has_llm_governance_workspaces(self) -> None:
        """管理页必须覆盖厂商目录、模型目录和租户连接。"""
        source = Path("frontend/src/pages/AdminPage.tsx").read_text(encoding="utf-8")

        assert "/admin/llm/providers" in source
        assert "/admin/llm/connections" in source
        assert "/admin/llm/default" in source
        assert "tenant_admin" in source
        logger.info("test_admin_page_manages_access_policies_and_ip_rules 完成")
