"""25.1—25.6 运行时管理与双语界面的隔离浏览器验收。"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from tests.test_frontend.test_report_browser import browser_page as browser_page
from tests.test_frontend.test_report_browser import frontend_origin as frontend_origin

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="需要显式开启浏览器验收")]


class TestRuntimeBrowser:
    """覆盖 25.5 语言持久化和 25.6 同步状态按钮契约。"""

    # 方法作用：通过真实文件选择和下载事件验证导入导出按钮。
    # Args: self - 实例；browser_page/frontend_origin - 隔离浏览器和前端。
    # Returns: 无返回值。
    def test_datasource_file_import_export(self, browser_page: Any, frontend_origin: str) -> None:
        """导入成功刷新数据源行，导出产生 JSON 文件且不发起表结构同步。"""
        from playwright.sync_api import expect

        # Arrange
        page = browser_page
        imported: list[str] = []
        exported = {"version": 1, "datasources": [{"name": "warehouse", "dialect": "sqlite", "database": "warehouse.db"}]}
        # 方法作用：记录上传文件并提供模拟管理接口，不接触真实数据库。
        # Args: route - 拦截的 HTTP 请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            if path == "/auth/me":
                data = {"authenticated": True, "auth_required": True, "role": "tenant_admin", "tenant_id": 2}
            elif path == "/datasources/import":
                assert "multipart/form-data" in route.request.headers["content-type"]
                assert b"warehouse" in route.request.post_data_buffer
                imported.append("warehouse")
                data = {"imported": imported}
            elif path == "/datasources/export":
                route.fulfill(body=json.dumps(exported), content_type="application/json", headers={"Content-Disposition": 'attachment; filename="datasources.json"'})
                return
            elif path == "/datasources":
                data = {"datasources": [{"name": name, "dialect": "sqlite", "schema_sync": {"status": "idle"}} for name in imported], "total": len(imported)}
            else:
                assert not path.startswith("/schema/")
                data = {}
            route.fulfill(json=data)

        page.route("**/api/v1/**", handle)
        # Act
        page.goto(f"{frontend_origin}/datasource")
        expect(page.get_by_role("button", name="导入数据源", exact=True)).to_be_visible()
        page.locator('input[type="file"]').set_input_files({"name": "datasources.json", "mimeType": "application/json", "buffer": json.dumps(exported).encode()})
        # Assert
        expect(page.get_by_role("cell", name="warehouse", exact=True)).to_be_visible()
        expect(page.get_by_text("未同步", exact=True)).to_be_visible()
        with page.expect_download() as downloading:
            page.get_by_role("button", name="导出数据源", exact=True).click()
        download = downloading.value
        assert download.suggested_filename == "datasources.json"
        assert json.loads(Path(download.path()).read_text(encoding="utf-8")) == exported
        assert imported == ["warehouse"]

    # 方法作用：验证勾选一个或多个数据源时按 names 参数导出，未勾选时全量导出。
    # Args: self - 实例；browser_page/frontend_origin - 隔离浏览器和前端。
    # Returns: 无返回值。
    def test_datasource_export_selection(self, browser_page: Any, frontend_origin: str) -> None:
        """单选、多选与不选三种路径必须产生不同的导出请求。"""
        from playwright.sync_api import expect

        # Arrange
        page = browser_page
        export_urls: list[str] = []
        rows = [
            {"name": "sales", "dialect": "sqlite", "schema_sync": {"status": "idle"}},
            {"name": "orders", "dialect": "sqlite", "schema_sync": {"status": "idle"}},
        ]

        # 方法作用：提供两行数据源并记录导出请求的完整 URL。
        # Args: route - 拦截的 HTTP 请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            if path == "/auth/me":
                data = {"authenticated": True, "auth_required": True, "role": "tenant_admin", "tenant_id": 2}
            elif path == "/datasources/export":
                export_urls.append(route.request.url)
                route.fulfill(body=json.dumps({"version": 1, "datasources": []}), content_type="application/json",
                              headers={"Content-Disposition": 'attachment; filename="datasources.json"'})
                return
            elif path == "/datasources":
                data = {"datasources": rows, "total": len(rows)}
            else:
                data = {}
            route.fulfill(json=data)

        page.route("**/api/v1/**", handle)
        page.goto(f"{frontend_origin}/datasource")
        expect(page.get_by_role("cell", name="sales", exact=True)).to_be_visible()

        # Act / Assert：不勾选时全量导出，URL 不得带 names
        with page.expect_download():
            page.get_by_role("button", name="导出数据源", exact=True).click()
        assert "names=" not in export_urls[-1]

        # Act / Assert：勾选 1 个即单选导出，导出后勾选被清空
        page.get_by_role("row", name=re.compile("sales")).get_by_role("checkbox").check()
        with page.expect_download():
            page.get_by_role("button", name=re.compile("导出所选")).click()
        assert export_urls[-1].count("names=") == 1
        expect(page.get_by_role("button", name="导出数据源", exact=True)).to_be_visible()

        # Act / Assert：勾选 2 个即多选导出
        page.get_by_role("row", name=re.compile("sales")).get_by_role("checkbox").check()
        page.get_by_role("row", name=re.compile("orders")).get_by_role("checkbox").check()
        with page.expect_download():
            page.get_by_role("button", name=re.compile("导出所选")).click()
        assert export_urls[-1].count("names=") == 2

    # 验证认证服务暂时无响应时仍显示可见加载状态，不渲染空白页面。
    # Args: self - 测试实例；browser_page/frontend_origin - 隔离浏览器和前端。
    # Returns: 无返回值。
    def test_auth_probe_pending_does_not_render_blank_page(self, browser_page: Any, frontend_origin: str) -> None:
        """后端探针挂起时页面必须显示加载提示，而不是 body 为空。"""
        from playwright.sync_api import expect

        # Arrange：让认证请求保持挂起，模拟同步后刷新时后端暂时不可用。
        browser_page.route("**/api/v1/auth/me", lambda route: None)

        # Act
        browser_page.goto(f"{frontend_origin}/datasource", wait_until="domcontentloaded")

        # Assert
        logging.getLogger(__name__).info("认证挂起渲染探针 url=%s body=%r", browser_page.url, browser_page.locator("body").inner_text())
        expect(browser_page.get_by_text("页面加载中...", exact=True)).to_be_visible()
        assert browser_page.locator("body").inner_text().strip()

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
                data = {"datasources": [{"name": "sales", "dialect": "sqlite", "schema_sync": dict(state)}], "total": 1}
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
        page.goto(f"{frontend_origin}/datasource")
        expect(page.get_by_role("button", name="同步表结构", exact=True)).to_be_enabled()
        page.locator('div.ant-select[aria-label="Language / 语言"]').click()
        page.get_by_title("English", exact=True).click()
        expect(page.get_by_role("button", name="Sync table structure", exact=True)).to_be_visible()
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
        page.goto(f"{frontend_origin}/schema")
        expect(page.get_by_role("cell", name="销售订单", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Sync table structure", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="Stop sync", exact=True)).to_have_count(0)

    # 方法作用：验证认证超时留在当前路由，恢复服务后刷新可重新进入应用。
    # Args: self - 实例；browser_page/frontend_origin - 浏览器和隔离前端。
    # Returns: 无返回值。
    def test_auth_timeout_and_recovery(self, browser_page: Any, frontend_origin: str) -> None:
        """超时显示恢复页面而不是误退出，刷新重新验证已有会话。"""
        from playwright.sync_api import expect

        # Arrange
        page = browser_page
        state = {"ready": False}
        # 方法作用：模拟暂时挂起并可恢复的认证边界。
        # Args: route - 拦截请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path
            if path.endswith("/auth/me"):
                if not state["ready"]:
                    return
                data = {"auth_required": True, "authenticated": True, "role": "tenant_admin", "tenant_id": 2}
            else:
                data = {}
            route.fulfill(json=data)

        page.route("**/api/v1/**", handle)
        # Act
        page.goto(f"{frontend_origin}/datasource")
        expect(page.get_by_text("页面加载中...", exact=True)).to_be_visible()
        # Assert
        expect(page.get_by_text("暂时无法确认登录状态", exact=True)).to_be_visible(timeout=16000)
        assert urlparse(page.url).path == "/datasource"
        state["ready"] = True
        page.reload()
        expect(page.get_by_text("数据源管理", exact=True)).to_be_visible()

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

    # 验证同步后刷新遇到网络错误时保留路由，并能重新验证同一会话。
    # Args: self - 实例；browser_page/frontend_origin - 隔离浏览器；failure - 故障种类。
    # Returns: 无返回值。
    @pytest.mark.parametrize("failure", ["offline", "server_error", "timeout"])
    def test_sync_reload_auth_failure_recovers_without_logout(
        self, browser_page: Any, frontend_origin: str, failure: str,
    ) -> None:
        """网络异常不是认证失效，不能清 Cookie 或跳登录，恢复无需再输密码。"""
        from playwright.sync_api import expect

        # Arrange
        page = browser_page
        state = {"fail": False, "sync": "idle"}
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        # 只在同步后的认证探测注入故障，其他请求返回可用的租户数据。
        # Args: route - 浏览器请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            if path == "/auth/me":
                if state["fail"]:
                    if failure == "server_error":
                        route.fulfill(status=503, json={"detail": "temporarily unavailable"})
                    elif failure == "offline":
                        route.abort("connectionrefused")
                    return
                data = {"auth_required": True, "authenticated": True, "tenant_id": 2, "user_id": 7, "role": "tenant_admin", "username": "tenant-user"}
            elif path == "/datasources":
                data = {"datasources": [{"name": "sales", "dialect": "sqlite", "schema_sync": {"status": state["sync"], "phase": "collecting"}}], "total": 1}
            elif path == "/schema/sync":
                state["sync"] = "running"
                data = {"status": "running", "phase": "collecting"}
            else:
                data = {}
            route.fulfill(json=data)

        page.route("**/api/v1/**", handle)
        page.context.add_cookies([{"name": "test-session", "value": "preserve-me", "url": frontend_origin, "httpOnly": True}])
        page.goto(f"{frontend_origin}/datasource")
        page.get_by_role("button", name="同步表结构", exact=True).click()
        expect(page.get_by_role("button", name="终止同步", exact=True)).to_be_visible()
        # Act
        state["fail"] = True
        page.reload()
        # Assert
        expect(page.get_by_text("暂时无法确认登录状态", exact=True)).to_be_visible(timeout=15000)
        assert urlparse(page.url).path == "/datasource"
        assert any(cookie["name"] == "test-session" for cookie in page.context.cookies())
        state["fail"] = False
        page.get_by_role("button", name="重试验证", exact=True).click()
        expect(page.get_by_text("数据源管理", exact=True)).to_be_visible()
        assert urlparse(page.url).path == "/datasource"
        assert not errors

    # 验证只有服务端明确判定未登录时才跳转登录页。
    # Args: self - 实例；browser_page/frontend_origin - 隔离浏览器；status - 响应状态。
    # Returns: 无返回值。
    @pytest.mark.parametrize("status", [200, 401])
    def test_confirmed_unauthenticated_session_still_redirects(
        self, browser_page: Any, frontend_origin: str, status: int,
    ) -> None:
        """临时不可用恢复不能绕过真正的身份过期校验。"""
        from playwright.sync_api import expect

        # Arrange
        browser_page.route("**/api/v1/**", lambda route: route.fulfill(status=status, json={"auth_required": True, "authenticated": False}))
        # Act
        browser_page.goto(f"{frontend_origin}/datasource")
        # Assert
        expect(browser_page.get_by_label("用户名", exact=True)).to_be_visible()
        assert urlparse(browser_page.url).path == "/login"

    # 方法作用：验证数据源页在无进行中同步时不会持续轮询列表接口。
    # Args: self - 实例；browser_page/frontend_origin - 隔离浏览器和前端。
    # Returns: 无返回值。
    def test_idle_datasource_page_stops_polling(self, browser_page: Any, frontend_origin: str) -> None:
        """空闲页面每 1.5 秒请求一次列表属于无谓负载。"""
        from playwright.sync_api import expect

        # Arrange
        page = browser_page
        calls: list[str] = []
        rows = [{"name": "sales", "dialect": "sqlite", "schema_sync": {"status": "idle"}}]

        # 方法作用：返回空闲数据源并统计列表请求次数。
        # Args: route - 拦截的 HTTP 请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            if path == "/auth/me":
                data = {"authenticated": True, "auth_required": True, "role": "tenant_admin", "tenant_id": 2}
            elif path == "/datasources":
                calls.append(route.request.url)
                data = {"datasources": rows, "total": len(rows)}
            else:
                data = {}
            route.fulfill(json=data)

        page.route("**/api/v1/**", handle)
        page.goto(f"{frontend_origin}/datasource")
        expect(page.get_by_role("cell", name="sales", exact=True)).to_be_visible()

        # Act：静置 5 秒，远超原来的 1.5 秒轮询间隔
        baseline = len(calls)
        page.wait_for_timeout(5000)

        # Assert
        assert len(calls) == baseline, f"空闲页面仍在轮询：{calls[baseline:]}"
