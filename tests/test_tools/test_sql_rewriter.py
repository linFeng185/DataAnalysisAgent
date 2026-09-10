"""SQL 方言重写器的 PostgreSQL 运行时兼容回归测试。"""

from __future__ import annotations

import logging

from sqlglot import exp, parse_one

from src.tools.sql_rewriter import rewrite_sql

logger = logging.getLogger(__name__)


class TestSqlRewriter:
    """覆盖功能 4.1.9 多源 SQL 的 PostgreSQL 方言修正。"""

    # 验证 PostgreSQL 两参数 ROUND 会把 double precision 表达式转为 DECIMAL。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_postgres_round_with_precision_casts_decimal(self):
        """两参数 ROUND 经重写后应匹配 PostgreSQL 的 numeric 函数签名。"""
        logger.debug("test_rewrite_postgres_round_with_precision_casts_decimal 入口")
        try:
            # Arrange：使用真实失败 SQL 中的聚合表达式。
            sql = (
                "SELECT ROUND(COALESCE(SUM(total_amount), 0), 2) "
                "AS total_order_amount FROM orders"
            )

            # Act：执行 PostgreSQL 方言重写并解析 AST。
            rewritten = rewrite_sql(sql, "postgres")
            tree = parse_one(rewritten, read="postgres")
            round_node = next(tree.find_all(exp.Round))

            # Assert：ROUND 的输入已显式转换为 DECIMAL，精度参数保持不变。
            assert isinstance(round_node.this, exp.Cast)
            assert round_node.this.args["to"].this == exp.DataType.Type.DECIMAL
            assert round_node.args["decimals"].this == "2"
            logger.info(
                "test_rewrite_postgres_round_with_precision_casts_decimal 完成",
                extra={"rewritten": rewritten},
            )
        except Exception as exc:
            logger.error(
                "test_rewrite_postgres_round_with_precision_casts_decimal 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 验证 PostgreSQL 单参数 ROUND 不需要改变原表达式类型。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_postgres_round_without_precision_keeps_expression(self):
        """单参数 ROUND 原本支持 double precision，不应增加无必要转换。"""
        logger.debug("test_rewrite_postgres_round_without_precision_keeps_expression 入口")
        try:
            # Arrange：构造 PostgreSQL 原生支持的单参数 ROUND。
            sql = "SELECT ROUND(AVG(total_amount)) AS average_amount FROM orders"

            # Act：执行重写并读取 ROUND 输入节点。
            rewritten = rewrite_sql(sql, "postgres")
            round_node = next(parse_one(rewritten, read="postgres").find_all(exp.Round))

            # Assert：单参数表达式不应被 DECIMAL CAST 包裹。
            assert not isinstance(round_node.this, exp.Cast)
            assert round_node.args.get("decimals") is None
            logger.info(
                "test_rewrite_postgres_round_without_precision_keeps_expression 完成",
                extra={"rewritten": rewritten},
            )
        except Exception as exc:
            logger.error(
                "test_rewrite_postgres_round_without_precision_keeps_expression 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 验证 PostgreSQL 两参数 ROUND 的方言重写可重复调用且不会叠加 CAST。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_postgres_round_is_idempotent(self):
        """已转换为 DECIMAL 的 ROUND 再次重写时应保持原 SQL 不变。"""
        logger.debug("test_rewrite_postgres_round_is_idempotent 入口")
        try:
            # Arrange：模拟 Layer 4 与执行节点连续调用同一重写器。
            sql = (
                "SELECT ROUND(COALESCE(SUM(total_amount), 0), 2) "
                "AS total_order_amount FROM orders"
            )

            # Act：连续执行两次 PostgreSQL 方言重写。
            rewritten_once = rewrite_sql(sql, "postgres")
            rewritten_twice = rewrite_sql(rewritten_once, "postgres")
            round_node = next(parse_one(rewritten_twice, read="postgres").find_all(exp.Round))

            # Assert：第二次重写不改变文本，ROUND 输入只有一层 DECIMAL CAST。
            assert rewritten_twice == rewritten_once
            assert isinstance(round_node.this, exp.Cast)
            assert not isinstance(round_node.this.this, exp.Cast)
            logger.info(
                "test_rewrite_postgres_round_is_idempotent 完成",
                extra={"rewritten": rewritten_twice},
            )
        except Exception as exc:
            logger.error(
                "test_rewrite_postgres_round_is_idempotent 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 验证损坏 SQL 在 AST 修正失败时仍按原有回退策略返回文本。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_postgres_malformed_sql_falls_back(self):
        """无法解析的 SQL 不应让重写器抛出异常。"""
        logger.debug("test_rewrite_postgres_malformed_sql_falls_back 入口")
        try:
            # Arrange：构造无法闭合的函数调用。
            sql = "SELECT ROUND("

            # Act：执行方言重写。
            rewritten = rewrite_sql(sql, "postgres")

            # Assert：解析失败时保留可供上层校验的原始文本。
            assert rewritten == sql
            logger.info("test_rewrite_postgres_malformed_sql_falls_back 完成")
        except Exception as exc:
            logger.error(
                "test_rewrite_postgres_malformed_sql_falls_back 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 验证 SQL Server 方言经映射后能正常加方括号，而不是整条重写静默失效。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_mssql_maps_to_tsql_dialect(self):
        """mssql 必须映射到 sqlglot 的 tsql，否则报 Unknown dialect 并放弃重写。"""
        logger.debug("test_rewrite_mssql_maps_to_tsql_dialect 入口")
        # Arrange：year_month 与 rank 都是 SQL Server 保留字，必须被引用。
        sql = "SELECT year_month, rank FROM sales"

        # Act：执行 SQL Server 方言重写。
        rewritten = rewrite_sql(sql, "mssql")

        # Assert：SQL Server 使用方括号引用标识符。
        assert "[year_month]" in rewritten
        assert "[rank]" in rewritten
        logger.info("test_rewrite_mssql_maps_to_tsql_dialect 完成")

    # 验证正确的 ClickHouse 日期截断不会被 sqlglot 归一化改坏。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_clickhouse_keeps_to_start_of_month(self):
        """sqlglot 会把 toStartOfMonth 渲染回 dateTrunc，而 dateTrunc 在 Date32 列上报 code 43。"""
        logger.debug("test_rewrite_clickhouse_keeps_to_start_of_month 入口")
        # Arrange：这正是模型按方言提示写出的正确写法。
        sql = "SELECT toStartOfMonth(event_date) AS month FROM insight_order_attribution"

        # Act
        rewritten = rewrite_sql(sql, "clickhouse")

        # Assert：不得被归一化成 dateTrunc
        assert "toStartOfMonth" in rewritten
        assert "dateTrunc" not in rewritten
        logger.info("test_rewrite_clickhouse_keeps_to_start_of_month 完成")

    # 验证模型误写的 dateTrunc 会被确定性地改成 toStartOfX。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_clickhouse_converts_datetrunc(self):
        """dateTrunc 属于会报错的写法，必须按粒度改写为 toStartOfX。"""
        logger.debug("test_rewrite_clickhouse_converts_datetrunc 入口")
        # Arrange
        sql = "SELECT DATE_TRUNC('MONTH', d) AS m, toStartOfQuarter(d) AS q FROM t"

        # Act
        rewritten = rewrite_sql(sql, "clickhouse")

        # Assert
        assert "toStartOfMonth" in rewritten
        assert "toStartOfQuarter" in rewritten
        assert "dateTrunc" not in rewritten
        assert "DATE_TRUNC" not in rewritten
        logger.info("test_rewrite_clickhouse_converts_datetrunc 完成")
