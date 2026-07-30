"""受控处理摘要回归测试。"""


class TestDecisionSummary:
    """覆盖受控摘要的内容边界与长度限制。"""

    def test_summary_excludes_private_reasoning_and_errors(self) -> None:
        """摘要只能使用低敏状态元数据，不能复述推理、SQL 或数据库异常。"""
        # Arrange
        from src.graph.outcome import build_decision_summary

        state = {
            "retry_count": 2,
            "activated_skills": ["quality-check"],
            "sql_reasoning_content": "私有推理",
            "execution_error": "password=secret",
            "generated_sql": "SELECT * FROM private_table",
        }

        # Act
        summary = build_decision_summary(
            state,
            {"success": True, "status": "success", "source": "sql_query", "row_count": 8},
        )

        # Assert
        assert "返回8行结果" in summary
        assert "自动重试2次" in summary
        assert "私有推理" not in summary
        assert "secret" not in summary
        assert "private_table" not in summary
        assert len(summary) <= 320
