"""真实浏览器验证过期页面的授权管理。"""

import os
from typing import Any
from urllib.parse import urlparse

import pytest

from tests.test_frontend.test_report_browser import browser_page, frontend_origin

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="浏览器测试需要显式开启")]
__all__ = ["browser_page", "frontend_origin"]


class TestReportLifecycleBrowser:
    """覆盖 23.13 的过期恢复与真实点击请求。"""

    # 方法作用：验证过期页面提供延期动作，点击后重新加载正文且不提升永久权限。
    # Args: self - 测试实例；browser_page - 浏览器；frontend_origin - 隔离前端地址。
    # Returns: 无返回值。
    def test_expired_page_extension(self, browser_page: Any, frontend_origin: str) -> None:
        """用户点延期后发送 days=7；无永久权限不出现该按钮。"""
        from playwright.sync_api import expect

        page = browser_page
        state = {"expired": True}
        operations = []

        # 方法作用：隔离页面生命周期请求并验证延期前后状态。
        # Args: request_route - 拦截的 API 请求。
        # Returns: 无返回值。
        def handle(request_route: Any) -> None:
            path = urlparse(request_route.request.url).path.removeprefix("/api/v1")
            if path == "/auth/me":
                data = {"auth_required": True, "authenticated": True, "tenant_id": 3, "user_id": 7, "role": "tenant_admin", "username": "tester"}
            elif path.endswith("/permissions/me"):
                data = {"menu_allowed": True, "capabilities": ["report.menu", "report.extend"]}
            elif path.endswith("/lifecycle"):
                data = {"report_id": "report", "expired": state["expired"], "expires_at": "2026-09-01T00:00:00Z", "can_extend": True, "can_permanent": False}
            elif path.endswith("/extend"):
                operations.append(request_route.request.post_data_json)
                state["expired"] = False
                data = {"report_id": "report", "expires_at": "2030-09-01T00:00:00Z"}
            elif path == "/report-workbench/reports/report":
                if state["expired"]:
                    request_route.fulfill(status=410, json={"detail": "报表页面已过期"})
                    return
                data = {"report_id": "report", "version": 1, "data_manifest": {}, "document": {"title": "已恢复的报表", "generated_at": "2026-09-01T00:00:00Z", "expires_at": "2030-09-01T00:00:00Z", "version": 1, "limitations": [], "sections": [], "sources": [], "drilldowns": []}}
            else:
                data = {}
            request_route.fulfill(json=data)

        page.route("**/api/v1/**", handle)
        page.goto(f"{frontend_origin}/reports/report")
        expect(page.get_by_text("报表页面已过期", exact=True)).to_be_visible()
        page.get_by_role("button", name="延长 7 天", exact=True).click()
        expect(page.get_by_role("heading", name="已恢复的报表")).to_be_visible()
        expect(page.get_by_role("button", name="设置永久")).to_have_count(0)
        assert operations == [{"days": 7}]
