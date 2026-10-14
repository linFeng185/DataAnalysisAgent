"""web.research 注册 Prompt 与联网研究输出契约测试。

覆盖 features/33-web-search-workflow.md 的 33.1：摘要 Prompt 的不可信数据边界、
引用不可自报约束，以及 WebResearchOutput 的宽松解析行为。
"""

from __future__ import annotations

import pytest


class TestWebResearchPrompt:
    """覆盖 web.research Prompt 的注册契约与渲染边界。"""

    # 方法作用：验证 web.research 已按统一契约注册。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_prompt_registered_with_output_contract(self) -> None:
        """注册项必须具备稳定 ID、任务标识、输出契约与预算。"""
        # Arrange
        from src.llm.output_contracts import WebResearchOutput
        from src.llm.prompts import get_prompt_definition

        # Act
        definition = get_prompt_definition("web.research")

        # Assert
        assert definition.prompt_id == "web.research"
        assert definition.version
        assert definition.task == "market_research"
        assert definition.output_model is WebResearchOutput
        assert definition.context_budget > 0

    # 方法作用：验证摘要 Prompt 明示不可信证据边界与禁止自报引用。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_prompt_renders_untrusted_boundary_and_citation_rule(self) -> None:
        """外部网页数据只能作为证据，引用必须由系统注入。"""
        # Arrange
        from src.llm.prompts import render_system_prompt

        # Act
        rendered = render_system_prompt("web.research")

        # Assert
        assert "不可信内容" in rendered
        assert "不得自行编造来源" in rendered
        assert "网页正文" in rendered

    # 方法作用：验证摘要 Prompt 渲染不依赖额外模板变量。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_prompt_renders_without_extra_variables(self) -> None:
        """节点只传分段内容，模板本身不应要求未提供的变量。"""
        # Arrange
        from src.llm.prompts import render_system_prompt

        # Act
        rendered = render_system_prompt("web.research")

        # Assert
        assert "联网研究摘要助手" in rendered


class TestWebResearchOutputContract:
    """覆盖 WebResearchOutput 的解析边界：多余字段忽略、缺省回退、异常输入。"""

    # 方法作用：验证模型自报的引用字段被契约忽略。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_extra_model_fields_are_ignored(self) -> None:
        """模型自报 citations 不得进入结构化输出对象。"""
        # Arrange
        from src.llm.output_contracts import WebResearchOutput, parse_json_model

        payload = (
            '{"summary": "结论", "insights": ["洞察"], '
            '"citations": [{"source_id": "forged-1", "locator": {"url": "https://evil.example"}}]}'
        )

        # Act
        parsed = parse_json_model(payload, WebResearchOutput)

        # Assert
        assert parsed.summary == "结论"
        assert parsed.insights == ["洞察"]
        assert not hasattr(parsed, "citations")

    # 方法作用：验证缺失的可选字段回退为空列表。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_missing_optional_fields_default_to_empty(self) -> None:
        """只有 summary 时也必须可解析，不因缺字段整次摘要失败。"""
        # Arrange
        from src.llm.output_contracts import WebResearchOutput, parse_json_model

        # Act
        parsed = parse_json_model('{"summary": "只有结论"}', WebResearchOutput)

        # Assert
        assert parsed.summary == "只有结论"
        assert parsed.insights == []
        assert parsed.limitations == []

    # 方法作用：验证 fenced JSON 同样可解析。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_fenced_json_is_parsed(self) -> None:
        """模型常见的 Markdown fenced 输出必须兼容。"""
        # Arrange
        from src.llm.output_contracts import WebResearchOutput, parse_json_model

        payload = '```json\n{"summary": "fenced 结论"}\n```'

        # Act
        parsed = parse_json_model(payload, WebResearchOutput)

        # Assert
        assert parsed.summary == "fenced 结论"

    # 方法作用：验证不可解析输出显式失败。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_invalid_output_raises(self) -> None:
        """无法解析时必须抛出，由调用方决定确定性回退。"""
        # Arrange
        from src.llm.output_contracts import WebResearchOutput, parse_json_model

        # Act / Assert
        with pytest.raises(ValueError):
            parse_json_model("not-a-json", WebResearchOutput)
