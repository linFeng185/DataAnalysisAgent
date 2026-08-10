"""自动优化建议确定性核心测试。"""

from __future__ import annotations


class TestOptimizationAdvisor:
    """覆盖自动优化建议功能 19.10。"""

    # 方法作用：验证 SELECT * 和 WHERE 列缺少索引时生成可审计建议且不执行操作。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值。
    def test_advice_reports_static_risks_without_execution(self) -> None:
        """正常 SQL 应返回 SELECT *、索引建议和 executed=false。"""
        # Arrange
        from src.optimization.advisor import OptimizationAdvisor

        advisor = OptimizationAdvisor()

        # Act
        report = advisor.advise(
            "SELECT * FROM orders WHERE customer_id = 7 GROUP BY customer_id",
            "postgres",
            schema={"orders": {"columns": ["customer_id", "amount"]}},
            indexes={"orders": [{"name": "orders_pk", "columns": ["id"]}]},
            statistics={"orders": {"row_count": 2_000_000}},
        )

        # Assert
        assert report.valid is True
        assert report.executed is False
        assert report.issues
        assert any(item.code == "MISSING_INDEX" for item in report.issues)
        assert any(item.code == "SELECT_STAR" for item in report.issues)
        assert report.candidate_indexes

    # 方法作用：验证空输入和写 SQL 均被安全拒绝且没有候选 DDL。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值。
    def test_advice_rejects_empty_and_mutating_sql(self) -> None:
        """空 SQL 与 DDL 不得被优化器误认为可执行查询。"""
        # Arrange
        from src.optimization.advisor import OptimizationAdvisor

        advisor = OptimizationAdvisor()

        # Act
        empty = advisor.advise("", "postgres")
        mutating = advisor.advise("CREATE INDEX idx ON orders(customer_id)", "postgres")

        # Assert
        assert empty.valid is False
        assert mutating.valid is False
        assert empty.executed is False and mutating.executed is False
        assert not mutating.candidate_indexes

    # 方法作用：验证非 SARGable 函数条件被标记并给出重写方向。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值。
    def test_advice_detects_non_sargable_predicate(self) -> None:
        """对列套函数的过滤条件应提示函数索引或范围改写。"""
        # Arrange
        from src.optimization.advisor import OptimizationAdvisor

        # Act
        report = OptimizationAdvisor().advise(
            "SELECT id FROM users WHERE LOWER(email) = 'a@example.com'",
            "postgres",
        )

        # Assert
        assert report.valid is True
        assert any(item.code == "NON_SARGABLE" for item in report.issues)
