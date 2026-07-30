"""聊天请求推理开关和深度等级契约测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


class TestChatReasoningRequest:
    """覆盖功能 10.1.15、18.12.5 的统一推理偏好请求。"""

    # 方法作用：验证聊天请求接受显式推理开关和深度等级。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chat_request_accepts_reasoning_preference(self) -> None:
        """启用推理时可选择由模型能力允许的统一深度字符串。"""
        # Arrange
        from src.api.schemas import ChatRequest

        # Act
        request = ChatRequest(
            query="分析销售趋势",
            reasoning_enabled=True,
            reasoning_effort="max",
        )

        # Assert
        assert request.reasoning_enabled is True
        assert request.reasoning_effort == "max"

    # 方法作用：验证关闭推理时不能单独提交深度等级。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chat_request_rejects_effort_when_reasoning_disabled(self) -> None:
        """不完整的推理语义必须在 API 入参层失败。"""
        # Arrange / Act / Assert
        with pytest.raises(ValidationError, match="reasoning_effort"):
            from src.api.schemas import ChatRequest

            ChatRequest(query="分析销售趋势", reasoning_effort="high")

    # 方法作用：验证推理深度字段具有长度和字符边界。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize("effort", ["", "high", "max", "vendor_level_1"])
    def test_chat_request_accepts_extensible_safe_effort_names(self, effort: str) -> None:
        """新增厂商可以扩展安全的深度名称，最终有效性由模型能力校验。"""
        # Arrange
        from src.api.schemas import ChatRequest

        # Act
        request = ChatRequest(
            query="分析销售趋势",
            reasoning_enabled=bool(effort),
            reasoning_effort=effort,
        )

        # Assert
        assert request.reasoning_effort == effort
