"""租户指标工作台的真实浏览器验收。"""

import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

import pytest

from tests.test_frontend.test_report_browser import browser_page as browser_page
from tests.test_frontend.test_report_browser import frontend_origin as frontend_origin

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="浏览器测试需要显式开启")]


class TestMetricsBrowser:
    """覆盖 18.6.1、18.6.2：列表、创建版本、审核和角色边界。"""

    # 方法作用：验证编辑保存携带旧版本，提交和审核操作真实发送请求。
    # Args: self - 实例；browser_page - 浏览器；frontend_origin - 隔离前端服务。
    # Returns: 无返回值。
    def test_tenant_edit_and_approve_version(self, browser_page: Any, frontend_origin: str) -> None:
        """修改不原位覆盖已有版本，审核后刷新列表状态。"""
        from playwright.sync_api import expect

        page = browser_page
        calls = []
        metric = {"id": "v1", "code": "gmv", "version": 1, "status": "approved", "definition": {"code": "gmv", "name": "支付金额", "datasource": "sales", "dialect": "postgres", "expression": "SUM(amount)", "description": "支付订单", "unit": "元"}, "effective_at": "2026-09-01T00:00:00Z", "reference": "@metric(gmv@1)"}

        # 方法作用：模拟完整指标版本生命周期并记录写请求。
        # Args: route - 浏览器请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            method = route.request.method
            if path == "/auth/me":
                data = {"auth_required": True, "authenticated": True, "tenant_id": 3, "user_id": 7, "role": "tenant_admin", "username": "tester"}
            elif path == "/datasources":
                data = {"datasources": [{"name": "sales", "dialect": "postgres"}], "total": 1}
            elif path == "/datasource-types":
                data = {"items": [{"dialect": "postgres"}]}
            elif path == "/metric-catalog" and method == "POST":
                payload = route.request.post_data_json
                calls.append(payload)
                metric.update({"id": "v2", "version": 2, "status": "draft", "definition": payload["definition"]})
                data = dict(metric)
            elif path.endswith("/transition"):
                payload = route.request.post_data_json
                calls.append(payload)
                metric["status"] = "pending" if payload["action"] == "submit" else "approved"
                data = dict(metric)
            elif path == "/metric-catalog":
                data = {"items": [metric], "total": 1}
            elif path.endswith("/versions"):
                data = {"items": [metric]}
            else:
                data = {}
            route.fulfill(status=200, json=data)

        page.route("**/api/v1/**", handle)
        page.goto(f"{frontend_origin}/metric-catalog")
        expect(page.get_by_role("cell", name="支付金额", exact=True)).to_be_visible()
        page.get_by_role("button", name="编辑新版本").click()
        page.get_by_label("计算表达式").fill("SUM(paid_amount)")
        page.get_by_role("button", name="保存新版本").click()
        expect(page.get_by_text("新版本已保存", exact=True)).to_be_visible()
        assert calls[0]["expected_version"] == 1
        assert calls[0]["definition"]["expression"] == "SUM(paid_amount)"
        page.get_by_role("button", name="提交审核").click()
        expect(page.get_by_text("待审核", exact=True)).to_be_visible()
        logging.getLogger(__name__).info("指标审核页面探针 status=%s buttons=%s", metric["status"], page.get_by_role("button").all_text_contents())
        page.get_by_role("button", name=re.compile(r"^审\s*核$")).click()
        page.get_by_role("button", name="确认审核").click()
        expect(page.get_by_text("审核已保存", exact=True)).to_be_visible()
        assert [item["action"] for item in calls if "action" in item] == ["submit", "approve"]

    # 方法作用：验证租户只读成员不获得创建或审核按钮。
    # Args: self - 实例；browser_page - 浏览器；frontend_origin - 隔离前端。
    # Returns: 无返回值。
    def test_viewer_has_no_edit_controls(self, browser_page: Any, frontend_origin: str) -> None:
        """只读成员可访问目录，不能维护业务口径。"""
        from playwright.sync_api import expect

        page = browser_page

        # 方法作用：为只读成员提供空目录。
        # Args: route - 浏览器请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path
            data = {"items": [], "total": 0} if path.endswith("metric-catalog") else {}
            if path.endswith("auth/me"):
                data = {"auth_required": True, "authenticated": True, "tenant_id": 3, "user_id": 8, "role": "viewer", "username": "reader"}
            route.fulfill(status=200, json=data)

        page.route("**/api/v1/**", handle)
        page.goto(f"{frontend_origin}/metric-catalog")
        expect(page.get_by_role("heading", name="指标目录", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="新建指标")).to_have_count(0)
