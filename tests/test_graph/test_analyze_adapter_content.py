"""LLM 适配器正文解析回归测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


class TestAnalyzeAdapterContent:
    """覆盖适配器清理后的正文作为 JSON 输入。"""

    # 验证原始响应不是 JSON 时，适配器提供的正文仍可被正常解析。
    # Args: self - 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_llm_analyze_uses_parsed_content(self, monkeypatch) -> None:
        import src.graph.nodes.analyze_result as analyze_module
        import src.config as config_module
        import src.llm.adapters.registry as adapter_module

        parsed_payload = {
            "summary": "适配器正文",
            "insights": [],
            "recommended_chart_type": "table",
            "follow_up_questions": [],
        }
        llm = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content="not-json")))
        monkeypatch.setattr(analyze_module, "get_llm", lambda temperature=0.3: llm)
        monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(llm_model="test"))
        monkeypatch.setattr(analyze_module, "_to_compact", lambda rows: "[]")
        monkeypatch.setattr(
            adapter_module,
            "get_adapter",
            lambda model: SimpleNamespace(
                parse_response=lambda response: SimpleNamespace(
                    content=json.dumps(parsed_payload, ensure_ascii=False),
                    reasoning_content="",
                ),
            ),
        )

        result = await analyze_module._llm_analyze(
            [{"value": 1}], "SELECT value FROM t", {"row_count": 1}, "", "", "",
        )

        assert result["summary"] == "适配器正文"

    # 验证润色异常不会静默消失，日志必须带异常堆栈。
    # Args: self - 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_llm_polish_logs_exception(self, monkeypatch) -> None:
        import src.graph.nodes.analyze_result as analyze_module

        error = Mock()
        monkeypatch.setattr(analyze_module, "logger", SimpleNamespace(error=error))
        monkeypatch.setattr(
            analyze_module,
            "_get_task_llm",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("llm down")),
        )

        result = await analyze_module._llm_polish("摘要", ["洞察"], "[]")

        assert result is None
        error.assert_called_once()
        assert error.call_args.kwargs["exc_info"] is True
