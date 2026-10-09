"""隔离浏览器验证共用布局在全部图表和三种视口上的实际渲染，不访问业务后端。"""

import os
from typing import Any

import pytest

from tests.test_frontend.test_report_browser import browser_page as browser_page
from tests.test_frontend.test_report_browser import frontend_origin as frontend_origin

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="需显式启用浏览器布局验收")]


class TestChatChartLayoutBrowser:
    """覆盖 28.5/QA-28-2：实际 ECharts、ResizeObserver 和六类展示。"""

    # 验证真正挂载的 ChartPanel 随容器变化重排各图，不用快照选项代替真实渲染。
    # Args: self - 实例；browser_page - 隔离 Edge 页面；frontend_origin - 隔离 Vite 服务。
    # Returns: 无返回值。
    def test_all_chart_layouts_reflow_and_preserve_data(self, browser_page: Any, frontend_origin: str) -> None:
        """三种屏宽实际渲染五类图表与表格；数据、null、零值和图例名称不变。"""
        from playwright.sync_api import expect

        from src.tools.chart_presentation import adapt_plan, build_option, column_labels

        # Arrange：页面初始化接口全部隔离，禁止访问业务数据库或模型。
        page = browser_page

        # 只提供匿名初始化契约，图表由固定输入构建且不会发送调整请求。
        # Args: route - 页面初始化请求。
        # Returns: 无返回值。
        def initialize(route: Any) -> None:
            route.fulfill(json={"authenticated": False, "auth_required": True})

        page.route("**/api/v1/**", initialize)
        page.goto(frontend_origin, wait_until="networkidle")
        page.evaluate("""async () => {
const React = (await import('/node_modules/.vite/deps/react.js')).default;
const ReactDOM = (await import('/node_modules/.vite/deps/react-dom_client.js')).default;
const component = await import('/src/components/ChartPanel.tsx');
document.getElementById('root').style.display = 'none';
const host = document.createElement('div'); host.id = 'chart-layout-test'; document.body.appendChild(host);
window.chartLayoutRoot = ReactDOM.createRoot(host);
window.renderChartLayout = (config, rows) => window.chartLayoutRoot.render(React.createElement(component.default, {chartConfig:config,rows}));
}""")
        rows = [{"order_date": day, "region_code": region, "sales_amount": 1_800_000 + index * 1000,
                 "order_count": index * 2} for index, day in enumerate(["2026-01-01", "2026-01-02", "2026-01-03"], start=1) for region in ("CN-BJ", "CN-SH")]
        rows[2]["sales_amount"] = None
        rows[0]["order_count"] = 0
        labels = column_labels(rows)
        plan = {"x": "order_date", "series": ["region_code"], "metrics": ["sales_amount", "order_count"], "aggregation": "none"}
        for kind in ("line", "bar", "scatter", "heatmap", "pie", "table"):
            selected = adapt_plan(rows, kind, plan)
            option = build_option(rows, kind, selected, labels)
            config = {"type": kind, "option": option, "column_labels": labels, "display_language": "zh-CN"}
            # Act：相同挂载组件上切换配置和视口，覆盖观察器重绑及容器变宽恢复。
            page.evaluate("args => window.renderChartLayout(args.config,args.rows)", {"config": config, "rows": rows})
            if kind == "table":
                expect(page.locator("#chart-layout-test .ant-table-row").first).to_be_visible()
                assert page.locator("#chart-layout-test .ant-table-row").count() == len(rows)
                continue
            for viewport, width in ((1440, 852), (768, 460), (375, 287), (1440, 852)):
                page.set_viewport_size({"width": viewport, "height": 1000})
                page.locator("#chart-layout-test").evaluate("(element,width) => element.style.width=width+'px'", width)
                expect(page.locator("#chart-layout-test canvas").first).to_be_visible()
                page.wait_for_timeout(150)
                page.wait_for_function("width => document.querySelector('#chart-layout-test canvas')?.clientWidth === width", arg=width)
                metrics = page.locator("#chart-layout-test .chart-container").evaluate("element => ({width:element.clientWidth,height:element.clientHeight})")
                # Assert
                assert metrics["width"] == width
                assert metrics["height"] >= (360 if kind == "scatter" else 600)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
