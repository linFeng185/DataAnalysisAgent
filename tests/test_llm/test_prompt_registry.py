"""提示词注册表和 Pydantic 输出契约回归测试。"""

from __future__ import annotations

import pytest


class TestPromptRegistry:
    """覆盖功能 10.2.10 的提示词 ID、版本、回滚和结构化解析。"""

    # 方法作用：验证注册 Prompt 能校验变量并渲染方言和 Skill 内容。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_registered_prompt_renders_required_variables(self) -> None:
        """SQL 提示词应由注册表渲染并保留方言和 Skill 上下文。"""
        # Arrange
        from src.llm.prompts import get_prompt_definition, render_system_prompt

        definition = get_prompt_definition("sql.generate")

        # Act
        rendered = render_system_prompt(
            "sql.generate",
            dialect="mysql",
            skill_instructions="只读质量检查",
        )

        # Assert
        assert definition.version
        assert definition.task == "generate_sql"
        assert definition.output_model is not None
        assert definition.context_budget > 0
        assert "mysql" in rendered
        assert "只读质量检查" in rendered

    # 方法作用：验证未知 Prompt ID 在节点调用前快速失败。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unknown_prompt_id_fails_fast(self) -> None:
        """拼写错误的提示词 ID 应在节点调用前暴露。"""
        # Arrange
        from src.llm.prompts import get_prompt_definition

        # Act / Assert
        with pytest.raises(KeyError):
            get_prompt_definition("not.registered")

    # 方法作用：验证结构化查询计划拒绝未来步骤依赖。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_decompose_contract_rejects_future_dependency(self) -> None:
        """查询分解不得依赖未来步骤，避免生成无法执行的计划。"""
        # Arrange
        from src.llm.output_contracts import DecomposeOutput

        # Act / Assert
        with pytest.raises(ValueError):
            DecomposeOutput.model_validate({
                "needs_decompose": True,
                "steps": [{
                    "step": 1,
                    "question": "先查订单",
                    "depends_on": [2],
                }, {
                    "step": 2,
                    "question": "再汇总",
                    "depends_on": [],
                }],
            })

    # 方法作用：验证分析输出契约忽略未知字段并保留合法核心字段。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_analysis_contract_drops_unknown_fields(self) -> None:
        """模型新增字段不应污染业务状态，核心字段仍经过契约校验。"""
        # Arrange
        from src.llm.output_contracts import AnalysisOutput, parse_json_model

        # Act
        parsed = parse_json_model(
            '{"summary":"ok","recommended_chart_type":"line","internal":"secret"}',
            AnalysisOutput,
        )

        # Assert
        assert parsed.summary == "ok"
        assert parsed.recommended_chart_type == "line"
        assert "internal" not in parsed.model_dump()

    # 方法作用：验证扩展 Prompt 可注册、可渲染且默认禁止重复覆盖。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_register_prompt_extension_and_reject_duplicate(self) -> None:
        """外部能力应通过稳定 ID 注册，并在冲突或缺变量时快速失败。"""
        # Arrange
        from src.llm.output_contracts import TextAnswerOutput
        from src.llm.prompts import (
            PROMPT_REGISTRY,
            PromptDefinition,
            register_prompt,
            render_system_prompt,
        )

        definition = PromptDefinition(
            prompt_id="extension.test",
            version="1.0.0",
            task="extension_test",
            template="处理 {subject}",
            output_model=TextAnswerOutput,
        )

        try:
            # Act
            register_prompt(definition)
            rendered = render_system_prompt("extension.test", subject="订单")

            # Assert
            assert "处理 订单" in rendered
            with pytest.raises(ValueError, match="已注册"):
                register_prompt(definition)
            with pytest.raises(ValueError, match="缺少变量"):
                render_system_prompt("extension.test")
        finally:
            PROMPT_REGISTRY.pop("extension.test", None)

    # 方法作用：验证结构化解析兼容 fenced JSON 并拒绝无对象输出。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_parse_json_model_fenced_and_invalid_boundaries(self) -> None:
        """兼容回退只能接受可解析 JSON 对象，不能吞掉非法纯文本。"""
        # Arrange
        from src.llm.output_contracts import TextAnswerOutput, parse_json_model

        # Act
        parsed = parse_json_model(
            '```json\n{"answer":"完成"}\n```',
            TextAnswerOutput,
        )

        # Assert
        assert parsed.answer == "完成"
        with pytest.raises(ValueError):
            parse_json_model("not-json", TextAnswerOutput)

    def test_prompt_versions_can_activate_and_rollback(self) -> None:
        """新版本激活后必须可回滚，并保留稳定 Prompt ID。"""
        # Arrange
        from src.llm.prompts import (
            PROMPT_REGISTRY,
            PromptDefinition,
            activate_prompt_version,
            get_prompt_definition,
            list_prompt_versions,
            register_prompt,
            rollback_prompt,
        )

        first = PromptDefinition(
            prompt_id="versioned.test",
            version="1.0.0",
            task="version_test",
            template="第一版",
        )
        second = PromptDefinition(
            prompt_id="versioned.test",
            version="2.0.0",
            task="version_test",
            template="第二版",
        )

        try:
            # Act
            register_prompt(first)
            register_prompt(second)
            active = get_prompt_definition("versioned.test")
            rolled_back = rollback_prompt("versioned.test")
            activated = activate_prompt_version("versioned.test", "2.0.0")

            # Assert
            assert list_prompt_versions("versioned.test") == ("1.0.0", "2.0.0")
            assert active.version == "2.0.0"
            assert rolled_back.version == "1.0.0"
            assert activated.version == "2.0.0"
        finally:
            PROMPT_REGISTRY.pop("versioned.test", None)
