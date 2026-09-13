"""LLM 适配器正文解析回归测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


class TestAnalyzeAdapterContent:
    """覆盖适配器清理后的正文作为 JSON 输入。"""

    # 方法作用：验证模型 JSON 契约异常仅重试一次，不随意拼接或丢弃额外对象。
    # Args: self - 测试实例；monkeypatch - 补丁工具；recover - 第二次响应是否恢复。
    # Returns: 无返回值。
    @pytest.mark.parametrize("recover", [True, False])
    async def test_invalid_json_retries_once(self, monkeypatch, recover: bool) -> None:
        """复现真实 Extra data 错误；恢复则保留分析，连续失败则带堆栈回退。"""
        # Arrange
        import src.graph.nodes.analyze_result as analyze_module

        invalid = SimpleNamespace(content='{"summary":"第一段"}{"limitations":["不能丢掉的限制"]}')
        valid = SimpleNamespace(content='{"summary":"重试后的完整结论","limitations":["样本截断"]}')
        model = SimpleNamespace(ainvoke=AsyncMock(side_effect=[invalid, valid if recover else invalid]))
        monkeypatch.setattr(analyze_module, "_get_analysis_llm", lambda **kwargs: model)
        error = Mock()
        monkeypatch.setattr(analyze_module.logger, "error", error)

        # Act
        result = await analyze_module._llm_analyze([{"amount": 1}], "SELECT 1", {}, "", "", "")

        # Assert
        assert model.ainvoke.await_count == 2
        if recover:
            assert result["summary"] == "重试后的完整结论"
            assert result["limitations"] == ["样本截断"]
            error.assert_not_called()
        else:
            error.assert_called_once()
            assert error.call_args.kwargs["exc_info"] is True

    # 验证原始响应不是 JSON 时，适配器提供的正文仍可被正常解析。
    # Args: self - 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_llm_analyze_uses_parsed_content(self, monkeypatch) -> None:
        import src.config as config_module
        import src.graph.nodes.analyze_result as analyze_module
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
