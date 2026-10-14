"""覆盖 28.6 的真实浏览器合并确认回归，不访问真实模型或业务服务。"""

import os
from typing import Any
from urllib.parse import urlparse

import pytest

from tests.test_frontend.test_report_browser import browser_page as browser_page
from tests.test_frontend.test_report_browser import frontend_origin as frontend_origin

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="需显式启用浏览器验收"),
]


class TestChartMergeConfirmationBrowser:
    """通过真实请求覆盖 28.6 的拒绝静默聚合与确认后类型切换。"""

    # 验证未确认时不发送模型聚合口径，显式确认成功后继续保留该口径。
    # Args: self - 测试实例；browser_page - 隔离浏览器；frontend_origin - 隔离前端地址。
    # Returns: 无返回值；断言真实浏览器请求和界面状态。
    def test_confirmation_survives_chart_type_change(
        self, browser_page: Any, frontend_origin: str
    ) -> None:
        """未确认切图失败仍可确认；确认后的折线图继续使用 sum。"""
        from playwright.sync_api import expect

        # Arrange：用可控 SSE 流提供重复坐标，所有业务请求都在浏览器内拦截。
        page = browser_page
        requests: list[dict[str, Any]] = []
        page.add_init_script("""
const originalFetch = window.fetch.bind(window);
window.fetch = (url, options) => {
  if (String(url).endsWith('/api/v1/chat') && options?.method === 'POST') {
    const stream = new ReadableStream({start(controller) {window.mergeTestStream = controller;}});
    return Promise.resolve(new Response(stream, {headers: {'Content-Type': 'text/event-stream'}}));
  }
  return originalFetch(url, options);
};
""")

        # 返回页面初始化契约并记录重画方案，禁止测试连接真实后端。
        # Args: route - 浏览器拦截的 HTTP 请求。
        # Returns: 无返回值；写入确定性响应。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            if path == "/auth/me":
                data = {
                    "authenticated": True, "auth_required": True,
                    "role": "analyst", "user_id": 7, "tenant_id": 2,
                }
            elif path == "/datasources":
                data = {"datasources": [{"name": "sales", "dialect": "sqlite"}]}
            elif path == "/models":
                data = {
                    "models": [{"id": "local", "name": "Local", "connection_id": 1}],
                    "default": {"connection_id": 1, "model_id": "local"},
                }
            elif path == "/skills":
                data = {"skills": []}
            elif path == "/charts/adjust":
                payload = route.request.post_data_json
                requests.append(payload)
                if "aggregation" not in payload["chart_plan"]:
                    route.fulfill(status=400, json={"detail": "重复坐标缺少明确聚合方式"})
                    return
                chart_type = "line" if payload["instruction"] == "line" else "bar"
                data = {
                    "type": chart_type,
                    "status": "success",
                    "chart_plan": payload["chart_plan"],
                    "option": {
                        "xAxis": {"type": "category", "data": ["车间"]},
                        "yAxis": {"type": "value"},
                        "series": [{"type": chart_type, "data": [3]}],
                    },
                }
            else:
                data = {"items": [], "unread_count": 0}
            route.fulfill(json=data)

        # 将公开事件注入页面正在消费的流，验证完整 React 更新而非源码字符串。
        # Args: event - 确定性的公开 SSE 事件。
        # Returns: 无返回值。
        def push(event: dict[str, Any]) -> None:
            page.evaluate(
                r"event => window.mergeTestStream.enqueue(new TextEncoder().encode("
                r"'data: ' + JSON.stringify(event) + '\n\n'))",
                event,
            )

        page.route("**/api/v1/**", handle)
        page.goto(frontend_origin)
        question = page.get_by_role("textbox", name="输入分析问题，Shift+Enter 换行")
        expect(question).to_be_visible()
        question.fill("各车间产量")
        question.press("Enter")
        page.wait_for_function("window.mergeTestStream")
        push({"type": "data", "data": [{"name": "车间", "value": 1}, {"name": "车间", "value": 2}]})
        push({"type": "chart", "chart": {
            "type": "bar", "status": "failed", "reason": "重复坐标，需要确认口径",
            "chart_plan": {"x_column": "name", "y_column": "value", "aggregation": "sum"},
        }})
        merge = page.get_by_role("button", name="按名称合并求和", exact=True)
        expect(merge).to_be_visible()

        # Act / Assert：模型自给 sum 不能通过类型切换变成用户确认。
        page.locator("button").filter(has_text="柱状图").click()
        with page.expect_response("**/api/v1/charts/adjust"):
            page.get_by_role("menuitem", name="折线图", exact=True).click()
        assert "aggregation" not in requests[-1]["chart_plan"]
        expect(merge).to_be_enabled()

        # Act / Assert：确认成功后合并提示消失，随后切图必须沿用已确认口径。
        with page.expect_response("**/api/v1/charts/adjust"):
            merge.click()
        assert requests[-1]["chart_plan"]["aggregation"] == "sum"
        expect(merge).not_to_be_visible()
        page.locator("button").filter(has_text="柱状图").click()
        with page.expect_response("**/api/v1/charts/adjust"):
            page.get_by_role("menuitem", name="折线图", exact=True).click()
        assert requests[-1]["instruction"] == "line"
        assert requests[-1]["chart_plan"]["aggregation"] == "sum"
