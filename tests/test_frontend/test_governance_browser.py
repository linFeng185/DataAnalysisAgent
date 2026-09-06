"""真实浏览器验证租户额度和平台安全治理，不访问业务后端。"""

import os
from typing import Any
from urllib.parse import urlparse

import pytest

from tests.test_frontend.test_report_browser import browser_page, frontend_origin

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="浏览器测试需要显式开启")]
__all__ = ["browser_page", "frontend_origin"]


class TestGovernanceBrowser:
    """覆盖 G24.1/G24.2/G24.3/G24.4 的页面权限与真实点击请求。"""

    # 验证租户可设置软额度，页面请求不携带可伪造的租户编号。
    # Args: self - 测试实例；browser_page - 浏览器；frontend_origin - 隔离服务。
    # Returns: 无返回值。
    def test_tenant_query_quota_save(self, browser_page: Any, frontend_origin: str) -> None:
        """租户管理出现查询额度，平台安全停用页签不出现。"""
        from playwright.sync_api import expect

        page = browser_page
        saved = []

        # 拦截所有后端请求，保证只验证前端状态及请求契约。
        # Args: route - 被拦截的请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            if path == "/auth/me":
                data = {"auth_required": True, "authenticated": True, "tenant_id": 2, "user_id": 7, "role": "tenant_admin", "username": "owner"}
            elif path == "/admin/query-quota":
                if route.request.method == "PUT":
                    saved.append(route.request.post_data_json)
                data = {"tenant_per_hour": 50, "user_per_hour": 10, "hard_limit": 60, "window": "UTC 自然小时"}
            else:
                data = {"users": [], "providers": [], "connections": []}
            route.fulfill(json=data)

        page.route("**/api/v1/**", handle)
        page.goto(f"{frontend_origin}/admin")
        page.get_by_role("tab", name="查询额度", exact=True).click()
        expect(page.get_by_label("租户每小时总次数")).to_have_value("50")
        page.get_by_label("每位用户每小时次数").fill("8")
        page.get_by_role("button", name="保存租户额度").click()
        expect(page.get_by_text("查询额度已更新", exact=True)).to_be_visible()
        assert saved == [{"tenant_per_hour": 50, "user_per_hour": 8}]
        expect(page.get_by_role("tab", name="MCP 安全停用")).to_have_count(0)

    # 验证超管即使收到旧能力接口的放行也看不到报表业务入口。
    # Args: self - 测试实例；browser_page - 浏览器；frontend_origin - 隔离服务。
    # Returns: 无返回值。
    def test_platform_security_workspace(self, browser_page: Any, frontend_origin: str) -> None:
        """平台安全请求不包含连接 URL 或凭证字段。"""
        from playwright.sync_api import expect

        page = browser_page
        saved = []

        # 模拟平台基础目录和空安全状态。
        # Args: route - 被拦截的请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            if path == "/auth/me":
                data = {"auth_required": True, "authenticated": True, "tenant_id": 1, "user_id": 1, "role": "super_admin", "username": "platform"}
            elif path.endswith("/permissions/me"):
                data = {"menu_allowed": True, "capabilities": ["report.menu"]}
            elif path == "/mcp/security-blocks":
                if route.request.method == "POST":
                    saved.append(route.request.post_data_json)
                data = {"items": []}
            else:
                data = {"tenants": [], "providers": [], "policies": [], "ip_rules": []}
            route.fulfill(json=data)

        page.route("**/api/v1/**", handle)
        page.goto(f"{frontend_origin}/admin")
        page.get_by_role("tab", name="MCP 安全停用", exact=True).click()
        expect(page.get_by_role("link", name="报表工作台")).to_have_count(0)
        expect(page.get_by_role("tab", name="查询额度", exact=True)).to_have_count(0)
        page.get_by_label("租户 ID", exact=True).fill("2")
        page.get_by_label("连接名称", exact=True).fill("search")
        page.get_by_label("理由", exact=True).fill("安全工单排查")
        page.get_by_role("button", name="提交安全操作").click()
        expect(page.get_by_text("安全状态已更新并审计", exact=True)).to_be_visible()
        assert saved == [{"tenant_id": 2, "scope": "tenant", "owner_user_id": 0, "name": "search", "blocked": True, "reason": "安全工单排查"}]

    # 验证拥有者从可管理文件列表签发授权，随后可原地撤销。
    # Args: self - 测试实例；browser_page - 浏览器；frontend_origin - 隔离服务。
    # Returns: 无返回值。
    def test_private_knowledge_grant_and_revoke(self, browser_page: Any, frontend_origin: str) -> None:
        """页面只传文件 ID 和限时授权，不提交自定义归属。"""
        from playwright.sync_api import expect

        page = browser_page
        operations = []

        # 提供本人可管理文件和只读授权回执。
        # Args: route - 被拦截的请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            if path == "/auth/me":
                data = {"auth_required": True, "authenticated": True, "tenant_id": 2, "user_id": 7, "role": "analyst", "username": "owner"}
            elif path == "/knowledge/docs":
                data = {"docs": [{"id": 12, "name": "private.md", "scope": "private", "can_delete": True, "tag_ids": [], "size": 10, "modified": 1}]}
            elif path == "/knowledge/emergency-grants/recipients":
                data = {"items": [{"id": 1, "username": "platform-admin"}]}
            elif path.startswith("/knowledge/emergency-grants"):
                operations.append((route.request.method, route.request.post_data_json))
                data = {"id": "11111111-1111-1111-1111-111111111111", "expires_at": "2026-09-06T12:00:00Z"}
            else:
                data = {"entries": [], "tags": [], "total": 0}
            route.fulfill(json=data)

        page.route("**/api/v1/**", handle)
        page.goto(f"{frontend_origin}/knowledge")
        page.get_by_role("tab", name="应急授权", exact=True).click()
        page.get_by_label("授权文档", exact=True).click()
        page.get_by_text("private.md (private)", exact=True).click()
        page.get_by_label("平台账号", exact=True).click()
        page.get_by_text("platform-admin", exact=True).click()
        page.get_by_label("工单或排障理由").fill("工单排查说明")
        page.get_by_role("button", name="签发只读授权").click()
        expect(page.get_by_text("只读授权已签发", exact=True)).to_be_visible()
        page.get_by_role("button", name="撤销本人授权").click()
        expect(page.get_by_text("授权已撤销", exact=True)).to_be_visible()
        assert operations[0][1] == {"file_id": 12, "grantee_user_id": 1, "minutes": 15, "reason": "工单排查说明"}
        assert operations[1][0] == "DELETE"
