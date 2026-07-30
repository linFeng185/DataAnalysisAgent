"""模型能力动态表单定义与值校验测试。"""

from __future__ import annotations

import pytest


class TestCapabilityFormSchema:
    """覆盖功能 10.1.14 的厂商级能力表单定义。"""

    # 方法作用：验证动态表单可以填充默认值并保留不同字段类型。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_validate_capabilities_applies_dynamic_defaults(self) -> None:
        """模型只提交必要值时应由厂商表单定义补齐布尔和枚举默认值。"""
        # Arrange
        from src.llm.capability_schema import validate_capability_values

        schema = {
            "fields": [
                {
                    "key": "streaming",
                    "label": "流式输出",
                    "type": "boolean",
                    "required": True,
                    "default": True,
                },
                {
                    "key": "reasoning_default_effort",
                    "label": "默认推理深度",
                    "type": "select",
                    "default": "high",
                    "options": [
                        {"label": "高", "value": "high"},
                        {"label": "最大", "value": "max"},
                    ],
                },
                {
                    "key": "context_window",
                    "label": "上下文窗口",
                    "type": "integer",
                    "required": True,
                    "minimum": 1,
                },
            ],
        }

        # Act
        result = validate_capability_values(schema, {"context_window": 1_000_000})

        # Assert
        assert result == {
            "streaming": True,
            "reasoning_default_effort": "high",
            "context_window": 1_000_000,
        }

    # 方法作用：验证动态表单拒绝未知字段、错误类型和非法选项。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        "values, message",
        [
            ({"reasoning": True, "unknown": 1}, "未定义"),
            ({"reasoning": "true"}, "布尔"),
            ({"reasoning": True, "effort": "low"}, "可选值"),
        ],
    )
    def test_validate_capabilities_rejects_invalid_dynamic_values(
        self,
        values: dict,
        message: str,
    ) -> None:
        """服务端必须把动态表单定义作为模型能力的唯一写入契约。"""
        # Arrange
        from src.llm.capability_schema import validate_capability_values

        schema = {
            "fields": [
                {"key": "reasoning", "label": "推理", "type": "boolean"},
                {
                    "key": "effort",
                    "label": "推理深度",
                    "type": "select",
                    "options": [{"label": "高", "value": "high"}],
                },
            ],
        }

        # Act / Assert
        with pytest.raises(ValueError, match=message):
            validate_capability_values(schema, values)

    # 方法作用：验证 DeepSeek 默认能力表单只暴露官方有效推理深度。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_deepseek_schema_exposes_high_and_max_efforts(self) -> None:
        """用户界面不得给 DeepSeek 提供实际无效的低、中等深度。"""
        # Arrange
        from src.llm.capability_schema import deepseek_capability_schema

        # Act
        schema = deepseek_capability_schema()
        fields = {field["key"]: field for field in schema["fields"]}

        # Assert
        assert [item["value"] for item in fields["reasoning_efforts"]["options"]] == [
            "high",
            "max",
        ]
        assert fields["reasoning_default_effort"]["default"] == "high"

    # 方法作用：验证能力字段定义阶段立即拒绝类型或枚举不合法的默认值。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        "field",
        [
            {"key": "reasoning", "label": "推理", "type": "boolean", "default": "true"},
            {
                "key": "effort",
                "label": "深度",
                "type": "select",
                "default": "low",
                "options": [{"label": "高", "value": "high"}],
            },
        ],
    )
    def test_capability_definition_rejects_invalid_default(self, field: dict) -> None:
        """错误默认值必须在保存厂商表单时失败，不能延迟到模型创建阶段。"""
        # Arrange
        from pydantic import ValidationError

        from src.llm.capability_schema import CapabilityFormSchema

        # Act / Assert
        with pytest.raises(ValidationError):
            CapabilityFormSchema.model_validate({"fields": [field]})
