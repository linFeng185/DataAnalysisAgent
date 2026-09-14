"""report.plan 提示词的编号层级契约。

背景：需求里同时写了章节号（一、二、）与条目号（1./2.）两层编号时，旧提示词
把两种符号并列称作「章节」，只说「一个章节只拆一个单元」，没有交代该按哪一层拆。
模型只能自己猜，于是外层分组标题被拆成了独立单元（「经营总览】」这类残片），
单元数从 16 虚增到 22。

契约要点：提示词给的是**按嵌套层级判断**的通用原则——最内层的编号才是分析单元，
外层编号只是分组标题。不得写成「某个符号是章节、某个符号是单元」的符号映射，
否则换一种编号形式（①、1)、第一节…）又会失效。
"""

from __future__ import annotations

# 旧措辞：把两类编号并列成同一层，是本次切分异常的来源。
_AMBIGUOUS_SENTENCE = "（如 1./2./一、）一个章节只拆一个单元"


class TestPlanPromptLayering:
    """覆盖 report.plan 提示词的分层表述与渲染可用性。"""

    # 方法作用：验证提示词按嵌套层级描述单元边界，而不是给出符号映射。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_prompt_describes_nesting_instead_of_symbol_mapping(self) -> None:
        """必须说清“最内层编号才是单元、外层只是分组”，否则换种编号形式又失效。"""
        # Arrange
        from src.llm.prompts import get_prompt_definition

        template = get_prompt_definition("report.plan").template

        # Act
        has_layering = any(word in template for word in ("最内层", "嵌套", "层级"))
        has_grouping = any(word in template for word in ("分组", "标题", "上层"))

        # Assert
        assert has_layering, "提示词未交代按编号层级判断单元边界"
        assert has_grouping, "提示词未说明外层编号只是分组而不是单元"
        assert _AMBIGUOUS_SENTENCE not in template, (
            "旧的并列举例仍在：把「1.」与「一、」当成同一层，两层编号的需求会被拆错"
        )

    # 方法作用：验证修改措辞后提示词仍能正常渲染。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_prompt_still_renders_after_wording_change(self) -> None:
        """模板里的 JSON 示例用了转义花括号，措辞改错会在这里炸出 KeyError。"""
        # Arrange
        from src.llm.prompts import render_system_prompt

        # Act
        rendered = render_system_prompt("report.plan", max_units="36")

        # Assert
        assert "analysis_units" in rendered
        assert "unit_id" in rendered
        # 单元上限来自 system_values，模板里不得再写死数字
        assert "最多 36 个单元" in rendered

    # 方法作用：验证提示词不再把单元数写死。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_prompt_unit_limit_is_not_hardcoded(self) -> None:
        """写死的 12 曾让模型正确拆出的 16 个单元被契约拒收并整批降级。"""
        # Arrange
        from src.llm.prompts import get_prompt_definition

        template = get_prompt_definition("report.plan").template

        # Act / Assert
        assert "{max_units}" in template
        assert "最多 12 个单元" not in template
