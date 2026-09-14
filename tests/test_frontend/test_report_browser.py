"""通过真实浏览器验证报表按钮和租户配额界面，不访问业务后端。"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from src.logging_config import get_logger

# 浏览器标记只挂在需要真实浏览器的用例类上：同文件的静态契约用例默认参与执行。
BROWSER_MARKERS = [pytest.mark.browser, pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="浏览器测试需要显式开启")]
logger = get_logger(__name__)


# 方法作用：启动独立前端服务，避免使用开发者正在运行的应用。
# Args: 无。
# Returns: 本次测试独占的本地服务地址。
@pytest.fixture(scope="module")
def frontend_origin() -> Iterator[str]:
    root = Path(__file__).resolve().parents[2]
    node = shutil.which("node")
    if not node:
        pytest.fail("浏览器测试需要安装 Node.js")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [node, str(root / "frontend/node_modules/vite/bin/vite.js"), "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
        cwd=root / "frontend", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        for _attempt in range(100):
            try:
                if httpx.get(origin, timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            pytest.fail("隔离前端服务未就绪")
        yield origin
    finally:
        process.terminate()
        process.wait(timeout=10)


# 方法作用：创建真实浏览器页面并拒绝未被用例匹配的业务请求。
# Args: 无。
# Returns: 本次用例独占的浏览器页面。
@pytest.fixture
def browser_page() -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        channel = os.getenv("BROWSER_CHANNEL", "msedge" if sys.platform == "win32" else "chromium")
        browser = playwright.chromium.launch(channel=channel, headless=True)
        page = browser.new_page()
        page.set_default_timeout(8000)
        yield page
        browser.close()


class TestReportBrowser:
    """覆盖功能 23.9：真实点击、失败重试和请求幂等性。"""

    pytestmark = BROWSER_MARKERS

    # 方法作用：验证用户点击生成计划、确认和刷新均触发请求。
    # Args: self - 测试实例；browser_page - 浏览器页面；frontend_origin - 隔离服务地址。
    # Returns: 无返回值。
    def test_workbench_buttons_and_retry(self, browser_page: Any, frontend_origin: str) -> None:
        """失败后重试沿用幂等键，成功后能够显示任务和刷新列表。"""
        from playwright.sync_api import expect

        page = browser_page
        calls: list[tuple[str, str, dict[str, Any]]] = []
        job = {"id": "job-1", "prompt": "销售额", "status": "succeeded", "runs_started": 1, "plan": {}}
        estimate = {"query_count": 1, "llm_tokens": 1000, "estimated_seconds": 5}
        quota = {"jobs_remaining": 19, "runs_remaining": 49, "concurrent_remaining": 2, "tokens_remaining": 199000, "shares_remaining": 20, "period_start": "2026-08-31T16:00:00Z", "period_end": "2026-09-30T16:00:00Z"}

        # 方法作用：仅返回测试契约数据，记录按钮触发的请求。
        # Args: route - 被浏览器拦截的 API 请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            method = route.request.method
            payload = route.request.post_data_json or {}
            calls.append((method, path, payload))
            logger.info("浏览器请求探针", path=path, method=method)
            if path == "/auth/me":
                data = {"auth_required": True, "authenticated": True, "tenant_id": 3, "user_id": 7, "role": "tenant_admin", "username": "tester"}
            elif path == "/report-workbench/permissions/me":
                data = {"menu_allowed": True, "capabilities": ["report.menu", "report.create"]}
            elif path == "/datasources":
                data = {"datasources": [{"name": "sales", "dialect": "postgres"}]}
            elif path == "/report-workbench/quotas/me":
                data = quota
            elif path == "/report-workbench/plans":
                data = {"plan_hash": "sha256:test", "estimated": estimate, "plan": {"execution_mode": "immediate", "analysis_units": [{"unit_id": "sales", "question": "销售额"}], "requested_datasources": ["sales"], "page_ttl_days": 7, "permanent": False, "warnings": []}}
            elif path == "/report-workbench/jobs" and method == "POST":
                requests = [item for item in calls if item[:2] == ("POST", path)]
                if len(requests) == 1:
                    route.fulfill(status=503, json={"detail": "临时失败，请重试"})
                    return
                data = {"job_id": "job-1", "status": "queued"}
            elif path == "/report-workbench/jobs":
                data = {"jobs": [job], "total": 1}
            elif path == "/report-workbench/jobs/job-1":
                data = {"job": job, "latest_run": None, "latest_report": None}
            elif path == "/report-workbench/jobs/job-1/runs":
                data = {"runs": []}
            else:
                data = {}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

        page.route("**/api/v1/**", handle)
        page.goto(f"{frontend_origin}/report-workbench")
        page.get_by_placeholder("输入要分析的业务内容").fill("销售额")
        logger.info("工作台按钮探针", buttons=page.get_by_role("button").all_text_contents())
        page.get_by_role("button", name="生成计划").click()
        expect(page.get_by_text("计划预览", exact=True)).to_be_visible()
        page.get_by_role("button", name="确认并立即生成").click()
        expect(page.get_by_text("临时失败，请重试", exact=True)).to_be_visible()
        page.get_by_role("button", name="确认并立即生成").click()
        expect(page.get_by_text("报表任务已排队", exact=True)).to_be_visible()
        submissions = [item[2] for item in calls if item[:2] == ("POST", "/report-workbench/jobs")]
        assert len(submissions) == 2
        assert submissions[0]["idempotency_key"] == submissions[1]["idempotency_key"]
        before = sum(item[:2] == ("GET", "/report-workbench/jobs") for item in calls)
        page.locator(".report-workbench-toolbar button").click()
        page.wait_for_timeout(250)
        assert sum(item[:2] == ("GET", "/report-workbench/jobs") for item in calls) > before

    # 方法作用：验证租户管理员能够打开额度表单并保存用户策略。
    # Args: self - 测试实例；browser_page - 浏览器页面；frontend_origin - 隔离服务地址。
    # Returns: 无返回值。
    def test_tenant_admin_edits_report_policy(self, browser_page: Any, frontend_origin: str) -> None:
        """租户管理界面读取当前配置，保存后提交完整且未扩张租户范围的策略。"""
        from playwright.sync_api import expect

        from src.reporting.models import QuotaPolicyRequest

        page = browser_page
        saved: list[dict[str, Any]] = []
        policy = QuotaPolicyRequest(capabilities=["report.menu", "report.create"]).model_dump()

        # 方法作用：为租户管理页提供隔离的策略和用户响应。
        # Args: route - 被拦截请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            if path == "/auth/me":
                data = {"auth_required": True, "authenticated": True, "tenant_id": 3, "user_id": 7, "role": "tenant_admin", "username": "owner"}
            elif path == "/admin/users":
                data = {"users": [{"id": 9, "username": "analyst", "tenant_id": 3, "role": "analyst", "is_active": True, "failed_login_attempts": 0}]}
            elif path == "/admin/llm/providers":
                data = {"providers": []}
            elif path == "/admin/llm/connections":
                data = {"connections": []}
            elif path == "/report-workbench/admin/users/9/policy":
                if route.request.method == "PUT":
                    saved.append(route.request.post_data_json)
                data = {"policy": {key: value for key, value in policy.items() if key != "capabilities"}, "capabilities": policy["capabilities"], "period": "calendar_month"}
            else:
                data = {}
            logger.info("额度管理浏览器探针", path=path, method=route.request.method)
            route.fulfill(status=200, json=data)

        page.route("**/api/v1/**", handle)
        page.goto(f"{frontend_origin}/admin")
        page.get_by_role("button", name="报表额度").click()
        # 弹窗底部的重置区也有同名复选框，严格模式下必须限定到表单里的第一个输入框。
        expect(page.get_by_label("每月可创建任务数").first).to_have_value("20")
        page.get_by_label("每月可创建任务数").first.fill("30")
        page.get_by_role("button", name="保存额度").click()
        expect(page.get_by_text("报表额度已保存", exact=True)).to_be_visible()
        assert len(saved) == 1
        assert saved[0]["max_jobs_created"] == 30
        assert saved[0]["capabilities"] == ["report.menu", "report.create"]
        assert "tenant_id" not in saved[0]


class TestReportPageShell:
    """覆盖 23.15：私有查看页与公开分享页共用的整页外壳（整页底色、限宽居中、顶栏作用域）。"""

    # 方法作用：验证两个报表页都由统一外壳承载整页背景。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_viewer_and_shared_pages_use_shared_shell(self) -> None:
        """页面根节点曾是裸 div，主题底色只覆盖中间内容，四周露出浏览器默认白底。"""
        # Arrange
        viewer = Path("frontend/src/pages/ReportViewerPage.tsx").read_text(encoding="utf-8")
        shared = Path("frontend/src/pages/SharedReportPage.tsx").read_text(encoding="utf-8")

        # Act / Assert
        for page in (viewer, shared):
            assert "import ReportPageShell from '../components/report/ReportPageShell'" in page
            # 不得再把 .report-document 直接当作返回根节点，否则外层底色又露出来
            assert not re.search(r"return <div className=\"report-document\"", page)
        # 加载态、错误态与正文态都在同一外壳内：查看页 3 处，分享页 4 处（含授权码锁定态）
        assert viewer.count("<ReportPageShell") == 3
        assert shared.count("<ReportPageShell") == 4

    # 方法作用：验证顶栏渲染在 TemplateScope 作用域内部。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_toolbar_renders_inside_template_scope(self) -> None:
        """顶栏曾在作用域外，--report-* 变量取不到，只能落到 CSS 回退值，与正文割裂。"""
        # Arrange
        shell = Path("frontend/src/components/report/ReportPageShell.tsx").read_text(encoding="utf-8")
        viewer = Path("frontend/src/pages/ReportViewerPage.tsx").read_text(encoding="utf-8")
        shared = Path("frontend/src/pages/SharedReportPage.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert shell.index("<TemplateScope") < shell.index("{toolbar") < shell.index("</TemplateScope>")
        for page in (viewer, shared):
            # 顶栏经外壳槽位传入，页面不再自建作用域外的顶栏
            assert "toolbar=" in page
            assert "report-viewer-toolbar" not in page

    # 方法作用：验证整页底色取模板变量、内容占满可用宽度，且窄屏断点同步收窄内边距。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_page_shell_takes_template_background_and_centered_column(self) -> None:
        """页面级背景必须消费 --report-bg，不能写死白色；嵌在应用 Layout 内要扣掉顶栏高度。
        正文占满可用宽度（对齐菜单页 .app-page），不再写死 1440 上限在宽屏留白。"""
        # Arrange
        styles = Path("frontend/src/index.css").read_text(encoding="utf-8")
        shell = Path("frontend/src/components/report/ReportPageShell.tsx").read_text(encoding="utf-8")

        # Act
        page_rule = re.search(r"\.report-page \{[^}]*\}", styles)
        body_rule = re.search(r"\.report-page-body \{[^}]*\}", styles)
        mobile_rule = re.search(r"@media \(max-width: 767px\) \{.*\n\}", styles, re.DOTALL)

        # Assert
        # 外壳组件必须真的挂上这两个类名，否则样式规则无处生效
        assert 'className="report-page"' in shell
        assert 'className="report-page-body"' in shell
        assert page_rule and "var(--report-bg" in page_rule.group(0)
        # 私有查看页高度扣掉 64px 应用顶栏，避免多出一条滚动条
        assert ".app-content .report-page" in styles
        assert body_rule and "width: 100%" in body_rule.group(0)
        assert body_rule and "1440" not in body_rule.group(0)
        # 移动端断点必须覆盖新外壳的容器类，否则窄屏沿用桌面内边距
        assert mobile_rule and ".report-page-body" in mobile_rule.group(0)
