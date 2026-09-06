"""通过真实会话恢复验证 18.8.1，不访问真实后端。"""

import os
from typing import Any
from urllib.parse import urlparse

import pytest

from tests.test_frontend.test_report_browser import browser_page as browser_page
from tests.test_frontend.test_report_browser import frontend_origin as frontend_origin

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="浏览器测试需要显式开启")]


class TestMaskingBrowser:
    """覆盖功能 18.8.1：历史结果的表头提示和旧数据无误报。"""

    # 方法作用：验证前端沿历史接口恢复元数据并显示或隐藏脱敏标识。
    # Args: self - 实例；browser_page - 浏览器；frontend_origin - 隔离前端地址；masked - 是否携带元数据。
    # Returns: 无返回值。
    @pytest.mark.parametrize("masked", [True, False])
    def test_history_result_masking_badge(self, browser_page: Any, frontend_origin: str, masked: bool) -> None:
        """相同的星号数据，仅携带后端元数据时才显示已脱敏。"""
        from playwright.sync_api import expect

        page = browser_page
        session = {"session_id": "masking-session", "title": "脱敏验收会话", "datasource": "sales", "turn_count": 1, "created_at": "2026-09-06T00:00:00Z", "last_active_at": "2026-09-06T00:00:00Z"}
        result = {"success": True, "status": "success", "data": [{"email": "a***@example.com"}], "row_count": 1, "analysis": {"summary": "查询完成"}, "chart": {"type": "table"}}
        if masked:
            result["masking"] = {"applied": True, "fields": [{"column": "email", "categories": ["邮箱"], "masked_count": 1}]}

        # 方法作用：提供完整但隔离的会话恢复契约。
        # Args: route - 浏览器请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            if path == "/auth/me":
                data = {"auth_required": True, "authenticated": True, "tenant_id": 3, "user_id": 7, "role": "analyst", "username": "tester"}
            elif path == "/datasources":
                data = {"datasources": [{"name": "sales", "dialect": "postgres"}]}
            elif path == "/sessions":
                data = {"sessions": [session], "next_cursor": None, "has_more": False}
            elif path == "/sessions/masking-session":
                data = {"session": session, "has_more": False, "latest_state": result, "turns": [{"turn_id": 1, "user_query": "展示邮箱", "assistant_summary": "查询完成", "sql": "", "timestamp": "2026-09-06T00:00:00Z", "final_result": result}]}
            else:
                data = {}
            route.fulfill(status=200, json=data)

        page.route("**/api/v1/**", handle)
        page.goto(frontend_origin)
        page.get_by_role("button", name="历史会话").click()
        page.get_by_text("脱敏验收会话", exact=True).click()
        expect(page.get_by_text("已恢复会话", exact=True)).to_be_visible()
        page.get_by_text("数据 (1 行)", exact=False).first.click()
        expect(page.get_by_role("cell", name="a***@example.com", exact=True)).to_be_visible()
        if masked:
            expect(page.get_by_text("敏感数据已脱敏", exact=True)).to_be_visible()
            expect(page.get_by_role("columnheader", name="email 已脱敏")).to_be_visible()
        else:
            expect(page.get_by_text("敏感数据已脱敏", exact=True)).to_have_count(0)
            expect(page.get_by_text("已脱敏", exact=True)).to_have_count(0)
