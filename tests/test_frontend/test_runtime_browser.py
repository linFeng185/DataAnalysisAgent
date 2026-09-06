"""25.1—25.6 运行时管理与双语界面的隔离浏览器验收。"""

import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

import pytest

from tests.test_frontend.test_report_browser import browser_page as browser_page
from tests.test_frontend.test_report_browser import frontend_origin as frontend_origin

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="需要显式开启浏览器验收")]


class TestRuntimeBrowser:
    """覆盖 25.5 语言持久化和 25.6 同步状态按钮契约。"""

    # 验证中英文切换作用于页面并跨刷新持久化，不改写业务表名。
    # Args: self - 实例；browser_page/frontend_origin - 隔离浏览器和前端。
    # Returns: 无返回值。
    def test_language_persistence_and_schema_cancel(self, browser_page: Any, frontend_origin: str) -> None:
        """同步中禁重复提交，终止中不提前解锁，取消后可重试。"""
        from playwright.sync_api import expect
        page = browser_page
        state = {"status": "idle", "phase": "collecting", "run_id": "run-1"}
        actions = []
        # 返回隔离用户与数据，记录真实按钮动作而不访问业务数据库。
        # Args: route - 浏览器请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            method = route.request.method
            logging.getLogger(__name__).info("表结构浏览器请求探针 path=%s method=%s state=%s", path, method, state["status"])
            if path == "/auth/me":
                data = {"auth_required": True, "authenticated": True, "tenant_id": 2, "user_id": 7, "role": "tenant_admin", "username": "tenant-user"}
            elif path == "/datasources":
                data = {"datasources": [{"name": "sales", "dialect": "sqlite"}], "total": 1}
            elif path == "/schema/tables":
                data = {"tables": [{"name": "销售订单", "columns": [], "description": "业务原文"}], "total": 1}
            elif path == "/schema/sync":
                if method == "POST":
                    actions.append("start")
                    state["status"] = "running"
                data = dict(state)
            elif path == "/schema/sync/cancel":
                actions.append("cancel")
                state["status"] = "cancelling"
                data = dict(state)
            else:
                data = {}
            route.fulfill(json=data)
        page.route("**/api/v1/**", handle)
        page.goto(f"{frontend_origin}/schema")
        expect(page.get_by_role("button", name="同步表结构", exact=True)).to_be_enabled()
        page.locator('div.ant-select[aria-label="Language / 语言"]').click()
        page.get_by_title("English", exact=True).click()
        expect(page.get_by_role("button", name="Sync table structure", exact=True)).to_be_visible()
        expect(page.get_by_role("cell", name="销售订单", exact=True)).to_be_visible()
        page.reload()
        expect(page.get_by_role("button", name="Sync table structure", exact=True)).to_be_enabled()
        page.get_by_role("button", name="Sync table structure", exact=True).click()
        expect(page.get_by_role("button", name="Sync table structure")).to_be_disabled()
        page.get_by_role("button", name="Stop sync", exact=True).click()
        expect(page.get_by_role("button", name="Stop sync", exact=True)).to_be_disabled()
        expect(page.get_by_role("button", name="Sync table structure")).to_be_disabled()
        logging.getLogger(__name__).info("同步浏览器探针 status=%s actions=%s", state["status"], actions)
        state["status"] = "cancelled"
        expect(page.get_by_role("button", name="Sync table structure")).to_be_enabled()
        assert actions == ["start", "cancel"]
        assert page.evaluate("localStorage.getItem('data-agent.language')") == "en-US"

    # 验证插件目录驱动数据源下拉，不需要在前端注册新类型。
    # Args: self - 实例；browser_page/frontend_origin - 隔离浏览器和前端。
    # Returns: 无返回值。
    def test_dynamic_database_type(self, browser_page: Any, frontend_origin: str) -> None:
        """外部插件类型和扩展字段由服务端目录决定。"""
        from playwright.sync_api import expect
        page = browser_page
        saved = []
        # 返回新增插件并记录最终请求中的扩展参数。
        # Args: route - 浏览器请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            if path == "/auth/me":
                data = {"authenticated": True, "auth_required": True, "tenant_id": 2, "user_id": 7, "role": "tenant_admin", "username": "tenant-user"}
            elif path == "/datasource-types":
                data = {"items": [{"dialect": "customdb", "label": "CustomDB", "enabled": True, "fields": ["host", "port", "database", "warehouse"], "external": True}]}
            elif path == "/datasources" and route.request.method == "POST":
                saved.append(route.request.post_data_json)
                data = {"name": "custom", "dialect": "customdb"}
            elif path == "/datasources":
                data = {"datasources": [], "total": 0}
            else:
                data = {}
            route.fulfill(json=data)
        page.route("**/api/v1/**", handle)
        page.goto(f"{frontend_origin}/datasource")
        expect(page.get_by_text("数据源管理", exact=True)).to_be_visible()
        logging.getLogger(__name__).info("动态插件页面按钮探针 buttons=%s", page.get_by_role("button").all_text_contents())
        logging.getLogger(__name__).info("动态插件按钮可访问名称探针 snapshot=%s", page.locator('body').aria_snapshot())
        page.get_by_role("button", name=re.compile(r"添\s*加")).click()
        dialog = page.get_by_role("dialog")
        dialog.locator('.ant-select-selector').first.click()
        page.get_by_title("CustomDB", exact=True).click()
        dialog.get_by_label("名称", exact=True).fill("custom")
        dialog.get_by_label("主机", exact=True).fill("db.internal")
        dialog.get_by_label("端口", exact=True).fill("9000")
        dialog.get_by_label("数据库名", exact=True).fill("sales")
        dialog.get_by_label("warehouse", exact=True).fill("analytics")
        dialog.get_by_role("button", name=re.compile(r"添\s*加")).click()
        expect(page.get_by_text("数据源已添加", exact=True)).to_be_visible()
        assert saved[0]["dialect"] == "customdb"
        assert saved[0]["extra_params"] == {"warehouse": "analytics"}
