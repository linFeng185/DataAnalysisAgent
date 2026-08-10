"""SQL 优化 Skill 契约测试。"""

from __future__ import annotations


class TestSqlOptimizationSkill:
    """覆盖 SQL 优化 Skills 功能 19.15。"""

    # 方法作用：验证 Skill 工具输出优化建议契约且不执行 SQL。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值。
    async def test_tool_returns_non_executed_report(self) -> None:
        """Skill 正常调用应产生 executed=false 的结构化结果。"""
        # Arrange
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "sql_optimization_tools", Path("skills/sql-optimization/tools.py"),
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        OptimizeSqlTool = module.OptimizeSqlTool

        # Act
        result = await OptimizeSqlTool()._arun(
            sql="SELECT * FROM orders WHERE customer_id = 1",
            dialect="postgres",
            schema={"orders": {"columns": ["id", "customer_id"]}},
        )

        # Assert
        assert result["executed"] is False
        assert "issues" in result

    # 方法作用：验证 Skill 工具拒绝写 SQL。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值。
    async def test_tool_rejects_mutating_sql(self) -> None:
        """DDL 输入必须返回无效报告而不是候选执行语句。"""
        # Arrange
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "sql_optimization_tools_invalid", Path("skills/sql-optimization/tools.py"),
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        OptimizeSqlTool = module.OptimizeSqlTool

        # Act
        result = await OptimizeSqlTool()._arun(
            sql="ALTER TABLE orders ADD COLUMN x INT",
            dialect="postgres",
        )

        # Assert
        assert result["valid"] is False
        assert result["executed"] is False
