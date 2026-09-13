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

    # 验证模型写对的 Oracle dual 不会被标识符加引号改坏。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_oracle_keeps_dual_unquoted(self):
        """sqlglot 的 identify 会把 dual 渲染成 "dual"，而 "dual" 在 Oracle 必然 ORA-00942。"""
        logger.debug("test_rewrite_oracle_keeps_dual_unquoted 入口")
        # Arrange：这正是模型按方言提示写出的正确写法
        sql = "SELECT TRUNC(SYSDATE, 'DD') AS d FROM dual"

        # Act
        rewritten = rewrite_sql(sql, "oracle")

        # Assert：不得被加上引号
        assert "FROM dual" in rewritten
        assert '"dual"' not in rewritten
        logger.info("test_rewrite_oracle_keeps_dual_unquoted 完成")

    # 验证模型误写的 "dual" 会被确定性地去引号。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_oracle_strips_quotes_from_dual(self):
        """带引号的小写 "dual" 匹配不到公共同义词 DUAL，必须确定性改写。"""
        logger.debug("test_rewrite_oracle_strips_quotes_from_dual 入口")
        # Arrange：真实故障现场的写法
        sql = (
            "WITH \"bounds\" AS (SELECT TRUNC(SYSDATE, 'DD') - 89 AS \"start_date\" "
            "FROM \"dual\") SELECT \"start_date\" FROM \"bounds\""
        )

        # Act
        rewritten = rewrite_sql(sql, "oracle")

        # Assert
        assert "FROM dual" in rewritten
        assert '"dual"' not in rewritten
        logger.info("test_rewrite_oracle_strips_quotes_from_dual 完成")

    # 验证撤销 dual 引号不会连带放宽小写业务表的引号。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_oracle_only_unquotes_dual(self):
        """小写业务表必须保留引号（不加引号会命中大写老表），只有 dual 例外。"""
        logger.debug("test_rewrite_oracle_only_unquotes_dual 入口")
        # Arrange
        sql = "SELECT COUNT(*) FROM orders CROSS JOIN dual"

        # Act
        rewritten = rewrite_sql(sql, "oracle")

        # Assert：orders 加引号、dual 不加引号，两者必须同时成立
        assert '"orders"' in rewritten
        assert "JOIN dual" in rewritten
        assert '"dual"' not in rewritten
        logger.info("test_rewrite_oracle_only_unquotes_dual 完成")

    # 验证重复重写结果稳定，不会在两次调用间来回加删引号。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_oracle_dual_is_idempotent(self):
        """同一条 SQL 连续重写两次结果必须一致。"""
        logger.debug("test_rewrite_oracle_dual_is_idempotent 入口")
        # Arrange
        sql = 'SELECT "o"."id" FROM "orders" "o" WHERE "o"."order_date" > SYSDATE - 90'

        # Act
        once = rewrite_sql(sql, "oracle")
        twice = rewrite_sql(once, "oracle")

        # Assert
        assert once == twice
        logger.info("test_rewrite_oracle_dual_is_idempotent 完成")

    # 验证 dual 规则不外溢到其他方言。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_dual_rule_is_oracle_scoped(self):
        """其他方言不受 dual 规则影响，避免误伤同名标识符。"""
        logger.debug("test_rewrite_dual_rule_is_oracle_scoped 入口")
        # Arrange
        sql = "SELECT d FROM dual_table"

        # Act
        rewritten = rewrite_sql(sql, "postgres")

        # Assert：表名不该被改动内容
        assert "dual_table" in rewritten
        logger.info("test_rewrite_dual_rule_is_oracle_scoped 完成")

    # 验证 Oracle 中与表同名的 CTE 会被重命名。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_oracle_renames_cte_shadowing_table(self):
        """Oracle 把同名 CTE 当递归 CTE 解析并报 ORA-32039，必须确定性改名。"""
        logger.debug("test_rewrite_oracle_renames_cte_shadowing_table 入口")
        # Arrange：CTE 名与物理表名同为 inventory
        sql = (
            'WITH "inventory" AS (SELECT "i"."product_id" AS "pid", '
            'SUM("i"."quantity") AS "qty" FROM "inventory" "i" '
            'GROUP BY "i"."product_id") SELECT "pid", "qty" FROM "inventory"'
        )

        # Act
        rewritten = rewrite_sql(sql, "oracle")

        # Assert
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(rewritten, read="oracle")
        with_node = tree.find(exp.With)
        cte_names = [cte.alias_or_name for cte in with_node.expressions]
        all_tables = [table.name for table in tree.find_all(exp.Table)]
        # CTE 定义不得再与表同名
        assert "inventory" not in cte_names
        # 主查询必须引用改名后的 CTE，不能落空
        assert cte_names[0] in all_tables
        # CTE 内部对真实表的引用不得被改（那本来就是表）
        assert "inventory" in all_tables
        logger.info("test_rewrite_oracle_renames_cte_shadowing_table 完成")

    # 验证不冲突的 CTE 名保持不变。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_oracle_keeps_non_conflicting_cte(self):
        """只有 CTE 内部引用了同名标识符才算冲突，正常 CTE 不得被改。"""
        logger.debug("test_rewrite_oracle_keeps_non_conflicting_cte 入口")
        # Arrange
        sql = (
            'WITH "delivery" AS (SELECT "o"."id" AS "oid" FROM "orders" "o") '
            'SELECT "oid" FROM "delivery"'
        )

        # Act
        rewritten = rewrite_sql(sql, "oracle")

        # Assert
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(rewritten, read="oracle")
        cte_names = [cte.alias_or_name for cte in tree.find(exp.With).expressions]
        assert cte_names == ["delivery"]
        logger.info("test_rewrite_oracle_keeps_non_conflicting_cte 完成")

    # 验证后续 CTE 与主查询对冲突 CTE 的引用都被同步改名。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_oracle_syncs_all_cte_references(self):
        """改名后所有引用点都要跟着改，否则查询会指向不存在的对象。"""
        logger.debug("test_rewrite_oracle_syncs_all_cte_references 入口")
        # Arrange：冲突 CTE 被主查询与后续 CTE 同时引用
        sql = (
            'WITH "orders" AS (SELECT "o"."id" AS "oid" FROM "orders" "o"), '
            '"joined" AS (SELECT "j"."oid" AS "oid2" FROM "orders" "j") '
            'SELECT "j"."oid2" FROM "joined" "j" JOIN "orders" "x" ON 1 = 1'
        )

        # Act
        rewritten = rewrite_sql(sql, "oracle")

        # Assert
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(rewritten, read="oracle")
        cte_names = [cte.alias_or_name for cte in tree.find(exp.With).expressions]
        assert "orders" not in cte_names
        renamed = cte_names[0]
        # 后续 CTE 与主查询里原本指向该 CTE 的引用必须已改名
        refs = [table.name for table in tree.find_all(exp.Table)]
        assert refs.count(renamed) >= 2
        logger.info("test_rewrite_oracle_syncs_all_cte_references 完成")

    # 验证 CTE 改名规则不外溢到其他方言。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_cte_rename_is_oracle_scoped(self):
        """PostgreSQL 中 CTE 遮蔽同名表是合法语义，绝不能改名。"""
        logger.debug("test_rewrite_cte_rename_is_oracle_scoped 入口")
        # Arrange
        sql = (
            'WITH "inventory" AS (SELECT "i"."product_id" AS "pid" '
            'FROM "inventory" "i") SELECT "pid" FROM "inventory"'
        )

        # Act
        rewritten = rewrite_sql(sql, "postgres")

        # Assert
        assert "inventory_base" not in rewritten
        assert '"inventory"' in rewritten
        logger.info("test_rewrite_cte_rename_is_oracle_scoped 完成")

    # 验证冲突 CTE 改名后可重复执行且结果稳定。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rewrite_oracle_cte_rename_is_idempotent(self):
        """连续重写两次结果必须一致，避免每次执行都追加后缀。"""
        logger.debug("test_rewrite_oracle_cte_rename_is_idempotent 入口")
        # Arrange
        sql = (
            'WITH "inventory" AS (SELECT "i"."product_id" AS "pid" '
            'FROM "inventory" "i") SELECT "pid" FROM "inventory"'
        )

        # Act
        once = rewrite_sql(sql, "oracle")
        twice = rewrite_sql(once, "oracle")

        # Assert
        assert once == twice
        logger.info("test_rewrite_oracle_cte_rename_is_idempotent 完成")
