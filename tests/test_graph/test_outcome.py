"""统一结果状态、错误脱敏和多数据源成败语义回归测试。"""

from __future__ import annotations


class TestOutcomeContract:
    """覆盖结果契约和 build_response 的失败边界。"""

    # 方法作用：验证需要补充输入的响应不会被记录为成功。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_status_from_response_preserves_needs_input(self) -> None:
        """需要补充时间范围时不应被记录为成功或普通失败。"""
        # Arrange
        from src.graph.outcome import is_successful_response, status_from_response

        response = {"success": True, "status": "needs_input", "needs_time_range": True}

        # Act
        status = status_from_response(response)

        # Assert
        assert status == "needs_input"
        assert is_successful_response(response) is False

    # 方法作用：验证公开错误映射对已知和未知错误码均返回脱敏文本。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_public_error_message_known_and_unknown_codes(self) -> None:
        """内部异常细节不得从错误码映射边界泄漏。"""
        # Arrange
        from src.graph.outcome import public_error_message

        # Act
        known = public_error_message("SQL_EXECUTION_FAILED")
        unknown = public_error_message("PRIVATE_DETAIL", fallback="处理失败")

        # Assert
        assert known == "查询执行失败，请检查数据源、权限或查询条件"
        assert unknown == "处理失败"

    # 方法作用：验证旧响应中的显式失败优先于矛盾的 success 状态文本。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_explicit_failure_overrides_legacy_success_status(self) -> None:
        """旧响应字段矛盾时，显式 success=false 必须阻止伪成功。"""
        # Arrange
        from src.graph.outcome import is_successful_response, status_from_response

        response = {"success": False, "status": "success"}

        # Act / Assert
        assert status_from_response(response) == "failed"
        assert is_successful_response(response) is False

    # 方法作用：验证公开输出递归移除所有内部推理字段。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sanitize_public_output_removes_nested_reasoning(self) -> None:
        """旧历史的嵌套推理字段也不得通过公开接口恢复。"""
        # Arrange
        from src.graph.outcome import sanitize_public_output

        value = {
            "analysis": {"summary": "ok", "reasoning_content": "secret"},
            "items": [{"sql_reasoning_content": "secret"}],
        }

        # Act
        sanitized = sanitize_public_output(value)

        # Assert
        assert sanitized == {"analysis": {"summary": "ok"}, "items": [{}]}

    # 方法作用：验证多源全部失败时最终响应使用统一失败契约。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_build_response_marks_all_multi_source_failures(self) -> None:
        """多源全部失败时不能因 execution_error 为空而伪装成成功。"""
        # Arrange
        from src.graph.nodes.build_response import build_response_node

        state = {
            "user_query": "查询总量",
            "datasource": "one",
            "selected_datasources": ["one", "two"],
            "multi_source_results": [
                {"datasource": "one", "success": False, "error": "内部连接细节"},
                {"datasource": "two", "success": False, "error": "内部权限细节"},
            ],
            "analysis_result": {"summary": "无成功数据源"},
            "conversation_history": [],
        }

        # Act
        result = await build_response_node(state)

        # Assert
        final = result["final_response"]
        assert final["success"] is False
        assert final["status"] == "failed"
        assert final["error_code"] == "MULTI_SOURCE_FAILED"
        assert "内部" not in final["error_message"]
