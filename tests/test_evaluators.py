"""NL2SQL 离线基准和 evaluator 测试。"""

from __future__ import annotations

import json
from pathlib import Path


class TestNL2SQLEvaluation:
    """覆盖功能 15.1-15.3 的可重复评测契约。"""

    # 方法作用：验证基准集规模、字段契约和期望 SQL 均可解析。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_benchmark_dataset_contract(self) -> None:
        """基准样本必须包含问题、表、SQL、分析和方言，不依赖在线数据。"""
        # Arrange
        import sqlglot

        path = Path("tests/fixtures/nl2sql_benchmark.json")
        samples = json.loads(path.read_text(encoding="utf-8"))

        # Act / Assert
        assert len(samples) >= 12
        for sample in samples:
            assert {
                "id", "question", "tables", "expected_sql",
                "expected_analysis", "dialect",
            } <= set(sample)
            assert sample["tables"]
            assert sqlglot.parse_one(sample["expected_sql"], read=sample["dialect"])

    # 方法作用：验证 SQL evaluator 接受格式差异并拒绝结构差异。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sql_correctness_normalizes_formatting(self) -> None:
        """大小写、空白和结尾分号不应影响正确性，过滤条件变化必须失败。"""
        # Arrange
        from tests.evaluators.sql_correctness import evaluate_sql_correctness

        expected = "SELECT customer, SUM(amount) AS total FROM orders GROUP BY customer"

        # Act / Assert
        assert evaluate_sql_correctness(
            "select customer, sum(amount) as total from orders group by customer;",
            expected,
            dialect="sqlite",
        )["score"] == 1.0
        assert evaluate_sql_correctness(
            "SELECT customer, SUM(amount) AS total FROM orders WHERE amount > 100 GROUP BY customer",
            expected,
            dialect="sqlite",
        )["score"] == 0.0

    # 方法作用：验证安全 evaluator 对全部危险样本实现 100% 拦截。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sql_security_cases_are_all_blocked(self) -> None:
        """DDL、DML、多语句和状态变更函数都不能被误判为只读查询。"""
        # Arrange
        from tests.evaluators.sql_security import evaluate_security_cases

        # Act
        result = evaluate_security_cases()

        # Assert
        assert result["total"] >= 10
        assert result["blocked"] == result["total"]
        assert result["block_rate"] == 1.0
