"""厂商级模型能力动态表单定义、JSONB 归一化与值校验。"""

from __future__ import annotations

import json
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

CapabilityFieldType = Literal[
    "boolean",
    "integer",
    "number",
    "text",
    "select",
    "multiselect",
]


class CapabilityOption(BaseModel):
    """动态选择字段的一个可选值。"""

    label: str = Field(..., min_length=1, max_length=64)
    value: str = Field(..., min_length=1, max_length=64)


class CapabilityFieldDefinition(BaseModel):
    """厂商模型能力表单中的一个字段定义。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    key: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(..., min_length=1, max_length=64)
    type: CapabilityFieldType
    required: bool = False
    default_value: Any = Field(default=None, alias="default")
    options: list[CapabilityOption] = Field(default_factory=list, max_length=50)
    minimum: float | None = None
    maximum: float | None = None
    description: str = Field(default="", max_length=500)

    # 方法作用：校验选择字段、数值范围和选项定义自身一致。
    # Args: self - 已解析的能力字段定义。
    # Returns: 校验通过的字段定义。
    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        option_types = {"select", "multiselect"}
        if self.type in option_types and not self.options:
            raise ValueError(f"能力字段 {self.key} 必须配置可选值")
        if self.type not in option_types and self.options:
            raise ValueError(f"能力字段 {self.key} 的类型不允许配置选项")
        values = [option.value for option in self.options]
        if len(values) != len(set(values)):
            raise ValueError(f"能力字段 {self.key} 的可选值重复")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError(f"能力字段 {self.key} 的最小值不能大于最大值")
        if self.default_value is not None:
            _validate_field_value(self, self.default_value)
        return self


class CapabilityFormSchema(BaseModel):
    """一个厂商用于创建和编辑模型的能力表单定义。"""

    fields: list[CapabilityFieldDefinition] = Field(default_factory=list, max_length=30)

    # 方法作用：禁止厂商能力表单定义重复字段键。
    # Args: self - 已解析的能力表单。
    # Returns: 校验通过的能力表单。
    @model_validator(mode="after")
    def validate_unique_keys(self) -> Self:
        keys = [field.key for field in self.fields]
        if len(keys) != len(set(keys)):
            raise ValueError("能力表单字段 key 不能重复")
        return self


# 方法作用：把 asyncpg 返回的 JSONB 对象或字符串统一转换为字典。
# Args: value - JSONB 原始值；field_name - 错误信息中的字段名称。
# Returns: 解析后的字典，空值返回空字典。
def normalize_json_object(value: Any, *, field_name: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} 不是合法 JSON 对象") from exc
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"{field_name} 必须是 JSON 对象")


# 方法作用：按单个动态字段定义校验模型能力值。
# Args: definition - 字段定义；value - 待校验值。
# Returns: 类型和范围校验通过的原值。
def _validate_field_value(definition: CapabilityFieldDefinition, value: Any) -> Any:
    key = definition.key
    if definition.type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"能力字段 {key} 必须是布尔值")
    elif definition.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"能力字段 {key} 必须是整数")
    elif definition.type == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"能力字段 {key} 必须是数字")
    elif definition.type in {"text", "select"}:
        if not isinstance(value, str):
            raise ValueError(f"能力字段 {key} 必须是字符串")
    elif definition.type == "multiselect":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"能力字段 {key} 必须是字符串列表")

    if definition.type in {"select", "multiselect"}:
        allowed = {option.value for option in definition.options}
        selected = value if isinstance(value, list) else [value]
        if any(item not in allowed for item in selected):
            raise ValueError(f"能力字段 {key} 包含未定义的可选值")
    if definition.type in {"integer", "number"}:
        numeric = float(value)
        if definition.minimum is not None and numeric < definition.minimum:
            raise ValueError(f"能力字段 {key} 小于最小值")
        if definition.maximum is not None and numeric > definition.maximum:
            raise ValueError(f"能力字段 {key} 大于最大值")
    return value


# 方法作用：按照厂商表单定义校验并补齐一个模型的能力对象。
# Args: schema_value - 厂商能力表单；capabilities_value - 模型能力输入。
# Returns: 仅包含已定义字段且已补齐默认值的能力字典。
def validate_capability_values(
    schema_value: Any,
    capabilities_value: Any,
) -> dict[str, Any]:
    schema = CapabilityFormSchema.model_validate(
        normalize_json_object(schema_value, field_name="capability_schema"),
    )
    capabilities = normalize_json_object(capabilities_value, field_name="capabilities")
    definitions = {field.key: field for field in schema.fields}
    unknown = sorted(set(capabilities) - set(definitions))
    if definitions and unknown:
        raise ValueError(f"模型能力包含未定义字段: {', '.join(unknown)}")
    if not definitions:
        return capabilities

    result: dict[str, Any] = {}
    for key, definition in definitions.items():
        if key in capabilities:
            result[key] = _validate_field_value(definition, capabilities[key])
        elif definition.default_value is not None:
            result[key] = _validate_field_value(definition, definition.default_value)
        elif definition.required:
            raise ValueError(f"模型能力缺少必填字段: {key}")
    return result


# 方法作用：提供新厂商默认的通用模型能力表单。
# Args: 无。
# Returns: 可直接持久化的动态表单字典。
def default_capability_schema() -> dict[str, Any]:
    fields = [
        {"key": "streaming", "label": "流式输出", "type": "boolean", "default": True},
        {"key": "reasoning", "label": "支持推理", "type": "boolean", "default": False},
        {
            "key": "reasoning_content_in_response",
            "label": "返回推理字段",
            "type": "boolean",
            "default": False,
        },
        {
            "key": "function_calling",
            "label": "工具调用",
            "type": "boolean",
            "default": True,
        },
        {"key": "json_mode", "label": "JSON 输出", "type": "boolean", "default": True},
        {"key": "vision", "label": "视觉输入", "type": "boolean", "default": False},
        {
            "key": "context_window",
            "label": "上下文窗口",
            "type": "integer",
            "required": True,
            "default": 128000,
            "minimum": 1,
        },
        {
            "key": "max_tokens_limit",
            "label": "最大输出 Token",
            "type": "integer",
            "required": True,
            "default": 8192,
            "minimum": 1,
        },
        {
            "key": "reasoning_efforts",
            "label": "支持的推理深度",
            "type": "multiselect",
            "default": [],
            "options": [
                {"label": "低", "value": "low"},
                {"label": "中", "value": "medium"},
                {"label": "高", "value": "high"},
                {"label": "最大", "value": "max"},
            ],
        },
        {
            "key": "reasoning_default_effort",
            "label": "默认推理深度",
            "type": "select",
            "default": "high",
            "options": [
                {"label": "低", "value": "low"},
                {"label": "中", "value": "medium"},
                {"label": "高", "value": "high"},
                {"label": "最大", "value": "max"},
            ],
        },
        {
            "key": "reasoning_default_enabled",
            "label": "对话默认开启推理",
            "type": "boolean",
            "default": False,
        },
    ]
    return CapabilityFormSchema(fields=fields).model_dump(by_alias=True)


# 方法作用：提供 DeepSeek V4 Pro/Flash 的官方能力表单定义。
# Args: 无。
# Returns: 仅提供 high/max 有效深度的动态表单字典。
def deepseek_capability_schema() -> dict[str, Any]:
    schema = default_capability_schema()
    for field in schema["fields"]:
        if field["key"] in {"reasoning_efforts", "reasoning_default_effort"}:
            field["options"] = [
                {"label": "高", "value": "high"},
                {"label": "最大", "value": "max"},
            ]
        if field["key"] == "reasoning_default_enabled":
            field["default"] = True
    return CapabilityFormSchema.model_validate(schema).model_dump(by_alias=True)
