"""自动优化建议 API 测试。"""

from __future__ import annotations


class TestOptimizationRoute:
    """覆盖自动优化建议接口功能 19.10。"""

    # 方法作用：验证 API 仅返回确定性建议，不执行数据库操作。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值。
    async def test_advice_endpoint_is_non_mutating(self) -> None:
        """纯 SQL 输入应返回 executed=false。"""
        # Arrange
        from src.api.routes.optimization import advice

        # Act
        result = await advice({
            "sql": "SELECT id FROM orders WHERE customer_id = 1",
            "dialect": "postgres",
            "schema": {"orders": {"columns": ["id", "customer_id"]}},
        })

        # Assert
        assert result["executed"] is False
        assert result["valid"] is True
