"""真实 ClickHouse 报表中的聚合别名遮蔽回归（26.1）。"""

import pytest

from src.security.sql_schema_validation import validate_sql_schema


class TestProjectionAliasValidation:
    """覆盖输入列不能因为与输出别名同名而逃过校验。"""

    # 方法作用：验证聚合输入假列与自身别名同名仍应被拦截。
    # Args: self - 测试实例；dialect - 数据库方言。
    # Returns: 无返回值。
    @pytest.mark.parametrize("dialect", ["clickhouse", "mysql", "postgres", "oracle", "mssql"])
    def test_self_alias_does_not_authorize_missing_input(self, dialect: str) -> None:
        """ROUND(AVG(avg_temp_c),2) AS avg_temp_c 不能凭空定义 AVG 的输入字段。"""
        # Arrange
        tables = [{"name": "insight_weather_daily", "columns": [{"name": "event_date"}, {"name": "region_code"}, {"name": "avg_temperature"}]}]
        sql = "SELECT region_code, ROUND(AVG(avg_temp_c), 2) AS avg_temp_c FROM insight_weather_daily GROUP BY region_code ORDER BY avg_temp_c"

        # Act
        issues = validate_sql_schema(sql, datasources=["weather"], dialect=dialect, tables=tables)

        # Assert
        assert any(issue.type == "unknown_column" and issue.column == "avg_temp_c" and issue.confidence == "high" for issue in issues)

    # 方法作用：验证正确输入列使用同名输出及 ORDER BY 别名均保持合法。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_existing_input_and_output_alias_remain_valid(self) -> None:
        """不能通过禁止全部输出别名来修复自引用漏检。"""
        # Arrange
        tables = [{"name": "weather", "columns": [{"name": "temperature"}]}]

        # Act
        issues = validate_sql_schema("SELECT AVG(temperature) AS temperature FROM weather ORDER BY temperature", datasources=["weather"], dialect="postgres", tables=tables)

        # Assert
        assert not [issue for issue in issues if issue.confidence == "high"]

    # 方法作用：验证未引用的前置 CTE 不能让当前物理表的缺列校验降级。
    # Args: self - 测试实例；dialect - 数据库方言；star - 无关 CTE 是否投影星号。
    # Returns: 无返回值。
    @pytest.mark.parametrize("dialect", ["clickhouse", "mysql", "postgres", "oracle", "mssql"])
    @pytest.mark.parametrize("star", [False, True])
    def test_unused_cte_does_not_disable_input_validation(self, dialect: str, star: bool) -> None:
        """重现第七轮天气 CTE 缺 avg_temp_c 却被 cte_or_unresolved 放行。"""
        # Arrange
        tables = [{"name": "market", "columns": [{"name": "region_code"}]}, {"name": "weather", "columns": [{"name": "region_code"}, {"name": "temperature_c"}]}]
        projection = "*" if star else "region_code"
        sql = f"WITH monthly AS (SELECT {projection} FROM market), temperatures AS (SELECT region_code, ROUND(AVG(avg_temp_c), 2) AS avg_temp_c FROM weather GROUP BY region_code) SELECT w.avg_temp_c FROM temperatures w"

        # Act
        issues = validate_sql_schema(sql, datasources=["ch"], dialect=dialect, tables=tables)

        # Assert
        assert any(issue.type == "unknown_column" and issue.column == "avg_temp_c" and issue.confidence == "high" for issue in issues)
