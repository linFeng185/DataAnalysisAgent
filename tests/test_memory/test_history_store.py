"""历史失败原因展示回归测试。"""

from __future__ import annotations


class TestHistoryStoreOutput:
    """覆盖 PG 与内存历史统一返回失败原因。"""

    # 方法作用：验证旧历史无错误字段时仍返回稳定的通用失败原因。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_history_error_message_fallback(self) -> None:
        """失败记录缺少结构化错误时不得把原因留空。"""
        # Arrange
        from src.memory.history_store import _history_error_message

        # Act
        result = _history_error_message({}, False)

        # Assert
        assert result == "查询失败"

    # 方法作用：验证成功历史不会误显示失败原因。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_history_error_message_success_is_empty(self) -> None:
        """成功记录的错误原因必须为空。"""
        # Arrange
        from src.memory.history_store import _history_error_message

        # Act
        result = _history_error_message({"error_message": "旧失败"}, True)

        # Assert
        assert result == ""

    # 方法作用：验证旧记录中的原始错误文本不会直接暴露给历史页面。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_history_error_message_redacts_legacy_driver_detail(self) -> None:
        """旧版本连接串或驱动堆栈必须回退到错误码对应的安全文案。"""
        # Arrange
        from src.memory.history_store import _history_error_message

        # Act
        result = _history_error_message({
            "error_code": "SQL_EXECUTION_FAILED",
            "error_message": "password=secret host=10.0.0.1 stack trace",
        }, False)

        # Assert
        assert result == "查询执行失败，请检查数据源、权限或查询条件"
        assert "secret" not in result
