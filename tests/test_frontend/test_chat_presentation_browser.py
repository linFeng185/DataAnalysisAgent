"""覆盖 28.1—28.3 的隔离真实浏览器验收，不访问模型与业务数据。"""

import os
import re
from typing import Any
from urllib.parse import urlparse

import pytest

from tests.test_frontend.test_report_browser import browser_page as browser_page
from tests.test_frontend.test_report_browser import frontend_origin as frontend_origin

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="需显式启用浏览器验收"),
]


class TestChatPresentationBrowser:
    """在 final 未到达时验证浏览器已经显示数据和增量正文。"""

    # 验证真实 ReadableStream 在多次发送分片后增量更新 React 视图。
    # Args: self - 实例；browser_page - 隔离浏览器；frontend_origin - 隔离前端服务。
    # Returns: 无返回值。
    def test_data_and_answer_render_before_final(
        self, browser_page: Any, frontend_origin: str
    ) -> None:
        """数据先显示，正文分批出现，过程关闭不显示，图表排在数据之前。"""
        from playwright.sync_api import expect

        # Arrange
        page = browser_page
        page.add_init_script("""
const originalFetch = window.fetch.bind(window);
window.fetch = (url, options) => {
  if (String(url).endsWith('/api/v1/chat') && options?.method === 'POST') {
    window.chatTestRequest = JSON.parse(options.body);
    const stream = new ReadableStream({start(controller) {window.chatTestStream = controller;}});
    return Promise.resolve(new Response(stream, {headers:{'Content-Type':'text/event-stream'}}));
  }
  return originalFetch(url, options);
};
""")

        # 仅返回页面初始化契约，防止访问真实后端。
        # Args: route - 浏览器 API 请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            if path == "/auth/me":
                data = {
                    "authenticated": True,
                    "auth_required": True,
                    "role": "analyst",
                    "user_id": 7,
                    "tenant_id": 2,
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
            else:
                data = {"items": [], "unread_count": 0}
            route.fulfill(json=data)

        # 将一条公开 SSE 分片注入真实响应流。
        # Args: event - 公开事件。
        # Returns: 无返回值。
        def push(event: dict) -> None:
            page.evaluate(
                "event => window.chatTestStream.enqueue(new TextEncoder().encode('data: ' + JSON.stringify(event) + '\\n\\n'))",
                event,
            )

        page.route("**/api/v1/**", handle)
        page.goto(frontend_origin)
        textarea = page.get_by_role("textbox", name="输入分析问题，Shift+Enter 换行")
        expect(textarea).to_be_visible()
        textarea.fill("按日期比较各地区销售额")
        textarea.press("Enter")
        page.wait_for_function("window.chatTestStream")
        # Act
        push({"type": "presentation", "process_summary_enabled": False})
        push(
            {
                "type": "data",
                "data": [{"date": "2026-10-01", "sales": 3}],
                "column_labels": {"date": "日期", "sales": "销售额"},
            }
        )
        expect(page.locator('[data-section="data"]')).to_be_visible()
        push({"type": "answer_reset", "node": "analyze_result", "stream_id": "a"})
        push(
            {
                "type": "answer_delta",
                "node": "analyze_result",
                "stream_id": "a",
                "content": "销售增长",
            }
        )
        expect(page.locator('[data-section="analysis"]')).to_contain_text("销售增长")
        push(
            {
                "type": "answer_delta",
                "node": "analyze_result",
                "stream_id": "a",
                "content": "，需持续观察",
            }
        )
        expect(page.locator('[data-section="analysis"]')).to_contain_text("销售增长，需持续观察")
        push(
            {
                "type": "analysis_patch",
                "node": "analyze_result",
                "stream_id": "a",
                "analysis": {"insights": ["已观察到上升趋势"]},
            }
        )
        expect(page.locator('[data-section="analysis"]')).to_contain_text("已观察到上升趋势")
        push(
            {
                "type": "chart",
                "chart": {
                    "type": "bar",
                    "option": {
                        "xAxis": {"type": "category", "data": ["2026-10-01"]},
                        "yAxis": {"type": "value"},
                        "series": [{"type": "bar", "data": [3]}],
                    },
                },
            }
        )
        # Assert
        expect(page.locator('[data-section="chart"]')).to_be_visible()
        assert page.locator("[data-section]").evaluate_all(
            "elements => elements.map(element => element.dataset.section)"
        ) == ["chart", "data", "analysis"]
        expect(page.get_by_text("分析过程摘要", exact=True)).to_have_count(0)
        assert page.evaluate("window.chatTestRequest.display_language") == "zh-CN"
        push({"type": "presentation", "process_summary_enabled": True})
        push(
            {
                "type": "process_summary",
                "node": "analyze_result",
                "stream_id": "p1",
                "status": "done",
                "summary": "正在分析结果...",
                "elapsed_ms": 30,
            }
        )
        push(
            {
                "type": "process_summary",
                "node": "generate_chart",
                "stream_id": "p2",
                "status": "running",
                "summary": "正在生成图表...",
            }
        )
        expect(page.get_by_text("分析过程摘要", exact=True)).to_be_visible()
        page.get_by_text("分析过程摘要", exact=True).click()
        expect(page.get_by_text("正在分析结果...", exact=True)).to_be_visible()
        push({"type": "done", "status": "complete"})
        page.evaluate("window.chatTestStream.close()")
        expect(page.get_by_role("button", name=re.compile(r"取\s*消"))).to_have_count(0)
        # Act：下一轮取消后，旧流不能污染再下一轮。
        textarea.fill("重新分析")
        textarea.press("Enter")
        expect(page.get_by_role("button", name=re.compile(r"取\s*消"))).to_be_visible()
        page.evaluate("window.chatOldStream = window.chatTestStream")
        page.get_by_role("button", name=re.compile(r"取\s*消")).click()
        expect(page.get_by_text("本轮分析已取消，可重新发送问题。", exact=True)).to_be_visible()
        textarea.fill("再次分析")
        textarea.press("Enter")
        page.wait_for_function("window.chatTestRequest.query === '再次分析'")
        page.evaluate(
            "window.chatOldStream.enqueue(new TextEncoder().encode('data: '+JSON.stringify({type:'answer_delta',node:'analyze_result',stream_id:'old',content:'旧流不可见'})+'\\n\\n'))"
        )
        push({"type": "answer_reset", "node": "analyze_result", "stream_id": "new"})
        push(
            {
                "type": "answer_delta",
                "node": "analyze_result",
                "stream_id": "new",
                "content": "新轮次已恢复",
            }
        )
        expect(page.get_by_text("新轮次已恢复", exact=True)).to_be_visible()
        expect(page.get_by_text("旧流不可见", exact=True)).to_have_count(0)
        push({"type": "done", "status": "complete"})
        page.evaluate("window.chatTestStream.close(); window.chatOldStream.close()")
