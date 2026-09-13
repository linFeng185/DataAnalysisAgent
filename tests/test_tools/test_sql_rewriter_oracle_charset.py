"""覆盖 26.7.1：Oracle 集合运算字符集定向修正。"""

from __future__ import annotations

from dataclasses import asdict
from unittest.mock import patch

import pytest
import sqlglot
from sqlglot import exp
from structlog.testing import capture_logs

from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema
from src.tools.sql_rewriter import rewrite_sql

REPORT_SQL = '''WITH "order_monthly" AS (
    SELECT SUBSTR("o"."order_date", 1, 7) AS "year_month",
           SUM("o"."total_amount") AS "order_net_amount",
           SUM(CASE WHEN "o"."total_amount" < 0 THEN "o"."total_amount" ELSE 0 END) AS "refund_amount"
    FROM "orders" "o"
    WHERE "o"."order_date" >= TO_CHAR(SYSDATE - 90, 'YYYY-MM-DD')
      AND "o"."order_date" < TO_CHAR(SYSDATE + 1, 'YYYY-MM-DD')
    GROUP BY SUBSTR("o"."order_date", 1, 7)
), "payment_monthly" AS (
    SELECT SUBSTR("p"."payment_time", 1, 7) AS "year_month", SUM("p"."amount") AS "invoice_amount"
    FROM "payments" "p"
    WHERE "p"."payment_time" >= TO_CHAR(SYSDATE - 90, 'YYYY-MM-DD')
      AND "p"."payment_time" < TO_CHAR(SYSDATE + 1, 'YYYY-MM-DD')
    GROUP BY SUBSTR("p"."payment_time", 1, 7)
), "budget_monthly" AS (
    SELECT "wp"."year_month" AS "year_month", SUM("wp"."budget_amount") AS "budget_amount"
    FROM "workforce_projects" "wp"
    WHERE "wp"."event_date" >= TRUNC(SYSDATE, 'DD') - 90
      AND "wp"."event_date" < TRUNC(SYSDATE, 'DD') + 1
    GROUP BY "wp"."year_month"
), "actual_monthly" AS (
    SELECT "wpi"."year_month" AS "year_month", SUM("wpi"."component_amount") AS "actual_amount"
    FROM "workforce_payroll_items" "wpi"
    WHERE "wpi"."event_date" >= TRUNC(SYSDATE, 'DD') - 90
      AND "wpi"."event_date" < TRUNC(SYSDATE, 'DD') + 1
    GROUP BY "wpi"."year_month"
), "months" AS (
    SELECT "year_month" FROM "order_monthly"
    UNION SELECT "year_month" FROM "payment_monthly"
    UNION SELECT "year_month" FROM "budget_monthly"
    UNION SELECT "year_month" FROM "actual_monthly"
)
SELECT "m"."year_month" AS "year_month",
       ROUND(NVL("om"."order_net_amount", 0), 2) AS "order_net_amount",
       ROUND(NVL("pm"."invoice_amount", 0), 2) AS "invoice_amount",
       ROUND(NVL("om"."order_net_amount", 0) - NVL("pm"."invoice_amount", 0), 2) AS "net_invoice_variance",
       ROUND(NVL("om"."refund_amount", 0), 2) AS "refund_amount",
       ROUND(NVL("bm"."budget_amount", 0), 2) AS "budget_amount",
       ROUND(NVL("am"."actual_amount", 0), 2) AS "actual_amount",
       ROUND(NVL("am"."actual_amount", 0) / NULLIF("bm"."budget_amount", 0), 2) AS "budget_execution_rate"
FROM "months" "m"
LEFT JOIN "order_monthly" "om" ON "m"."year_month" = "om"."year_month"
LEFT JOIN "payment_monthly" "pm" ON "m"."year_month" = "pm"."year_month"
LEFT JOIN "budget_monthly" "bm" ON "m"."year_month" = "bm"."year_month"
LEFT JOIN "actual_monthly" "am" ON "m"."year_month" = "am"."year_month"
ORDER BY "m"."year_month"'''


# 构造与真实故障一致的列类型，不依赖外部数据库。
# Args: 无。
# Returns: 四张报表表的 Schema 快照。
def report_schema() -> SchemaSnapshot:
    """提供原始故障 SQL 的最小权威 Schema。"""
    definitions = {
        "orders": {"order_date": "VARCHAR2(30)", "total_amount": "NUMBER"},
        "payments": {"payment_time": "VARCHAR2(30)", "amount": "NUMBER"},
        "workforce_projects": {
            "year_month": "NVARCHAR2(7)", "budget_amount": "NUMBER", "event_date": "DATE",
        },
        "workforce_payroll_items": {
            "year_month": "NVARCHAR2(7)", "component_amount": "NUMBER", "event_date": "DATE",
        },
    }
    return SchemaSnapshot(tables=[
        TableSchema(name=name, columns=[ColumnInfo(name=column, type=kind)
                                       for column, kind in columns.items()])
        for name, columns in definitions.items()
    ])


class TestOracleSetCharset:
    """覆盖 26.7.1 的原始故障、类型边界、幂等和脱敏日志。"""

    # 验证四层月份 UNION 只修正两条普通字符分支，不删除实际金额等指标。
    # Args: self - 测试实例；serialized - 是否使用序列化快照。
    # Returns: 无返回值，失败由 pytest 报告。
    @pytest.mark.parametrize("serialized", [False, True])
    def test_report_union_preserves_business_query(self, serialized: bool) -> None:
        """完整报表查询只增加两处 TO_NCHAR，其他 AST 与 Schema 保持不变。"""
        snapshot = report_schema()
        before_schema = asdict(snapshot)
        baseline = sqlglot.parse_one(rewrite_sql(REPORT_SQL, "oracle"), read="oracle")

        rewritten = rewrite_sql(REPORT_SQL, "oracle", schema=before_schema if serialized else snapshot)
        tree = sqlglot.parse_one(rewritten, read="oracle")

        conversions = list(tree.find_all(exp.Anonymous))
        assert len(conversions) == 2
        assert all(node.name.upper() == "TO_NCHAR" for node in conversions)
        for node in conversions:
            assert node.expressions[0].name == "year_month"
            projection = node.parent
            assert isinstance(projection, exp.Alias)
            projection.replace(node.expressions[0].copy())
        assert tree == baseline
        assert asdict(snapshot) == before_schema
        assert rewrite_sql(rewritten, "oracle", schema=snapshot) == rewritten

    # 验证集合运算按位置而不是列别名识别冲突。
    # Args: self - 测试实例；operator - 集合操作；reverse - 是否交换分支顺序。
    # Returns: 无返回值。
    @pytest.mark.parametrize("operator", ["UNION", "UNION ALL", "INTERSECT", "MINUS"])
    @pytest.mark.parametrize("reverse", [False, True])
    def test_set_operations_preserve_order_and_numeric_columns(self, operator: str, reverse: bool) -> None:
        """文本列统一字符集，但不同别名和数值列不得被串位或转换。"""
        ordinary = 'SELECT "order_date" AS "period", "total_amount" AS "amount" FROM "orders"'
        national = 'SELECT "year_month" AS "different_alias", "budget_amount" AS "value" FROM "workforce_projects"'
        branches = [national, ordinary] if reverse else [ordinary, national]
        sql = f" {operator} ".join(branches)

        rewritten = rewrite_sql(sql, "oracle", schema=report_schema())
        tree = sqlglot.parse_one(rewritten, read="oracle")

        conversions = list(tree.find_all(exp.Anonymous))
        assert len(conversions) == 1
        assert conversions[0].expressions[0].name == "order_date"
        assert tree.selects[0].alias == ("different_alias" if reverse else "period")
        assert type(tree) is type(sqlglot.parse_one(sql, read="oracle"))
        assert tree.args.get("distinct") == sqlglot.parse_one(sql, read="oracle").args.get("distinct")

    # 验证派生表和 CTE 显式列名的类型能够传递到外层集合操作。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_derived_tables_and_cte_column_aliases(self) -> None:
        """只在消费派生列的位置转换，不修改原 CTE 的文本日期截取逻辑。"""
        sql = '''WITH "daily"("month_key") AS (
            SELECT SUBSTR("order_date", 1, 7) FROM "orders"
        ) SELECT "derived"."month_key" FROM (SELECT "month_key" FROM "daily") "derived"
          UNION ALL SELECT SUBSTR("year_month", 1, 7) FROM "workforce_projects"'''

        rewritten = rewrite_sql(sql, "oracle", schema=report_schema())

        assert 'TO_NCHAR("derived"."month_key") AS "month_key"' in rewritten
        assert rewritten.count("TO_NCHAR(") == 1

    # 验证明确类型、同字符集、未知类型和不支持表达式的保守边界。
    # Args: self - 测试实例；expression - 左分支表达式；expected - 应增加的转换数量。
    # Returns: 无返回值。
    @pytest.mark.parametrize(("expression", "expected"), [
        ('"order_date"', 1),
        ('SUBSTR("order_date", 1, 7)', 1),
        ('TO_CHAR("order_date")', 1),
        ('CAST("order_date" AS VARCHAR2(30))', 1),
        ("'中文月份'", 1),
        ("N'中文月份'", 0),
        ('CAST("order_date" AS NCHAR(30))', 0),
        ('CAST("order_date" AS NVARCHAR2(30))', 0),
        ('TO_NCHAR("order_date")', 0),
        ('"total_amount"', 0),
        ('"missing_column"', 0),
        ('CUSTOM_FUNC("order_date")', 0),
        ("SYSDATE", 0),
        ("NULL", 0),
    ])
    def test_expression_type_boundaries(self, expression: str, expected: int) -> None:
        """仅已确认数据库字符集与国家字符集混用时增加转换。"""
        sql = f'SELECT {expression} AS "period" FROM "orders" UNION SELECT "year_month" FROM "workforce_projects"'
        baseline = rewrite_sql(sql, "oracle")

        rewritten = rewrite_sql(sql, "oracle", schema=report_schema())

        assert rewritten.count("TO_NCHAR(") - baseline.count("TO_NCHAR(") == expected
        assert rewrite_sql(rewritten, "oracle", schema=report_schema()) == rewritten

    # 验证没有类型证据时不盲目修正字符串表达式。
    # Args: self - 测试实例；sql - 证据不足的 SQL。
    # Returns: 无返回值。
    @pytest.mark.parametrize("sql", [
        'SELECT * FROM "orders" UNION SELECT * FROM "workforce_projects"',
        'SELECT "order_date" FROM "unknown" UNION SELECT "year_month" FROM "workforce_projects"',
        'SELECT "order_date" FROM "foreign_owner"."orders" UNION SELECT "year_month" FROM "workforce_projects"',
        'SELECT "order_date" FROM "orders" CROSS JOIN "payments" UNION SELECT "year_month" FROM "workforce_projects"',
        'SELECT "year_month" FROM "workforce_projects" UNION SELECT "year_month" FROM "workforce_payroll_items"',
        'SELECT "order_date" FROM "orders" UNION SELECT "order_date", "total_amount" FROM "orders"',
    ])
    def test_uncertain_or_compatible_queries_are_unchanged(self, sql: str) -> None:
        """星号、缺表、限定表、歧义、同字符集和列数不齐不发生字符集改写。"""
        baseline = rewrite_sql(sql, "oracle")

        rewritten = rewrite_sql(sql, "oracle", schema=report_schema())

        assert rewritten == baseline

    # 验证其他方言和缺失快照保持历史重写行为。
    # Args: self - 测试实例；dialect - 目标方言。
    # Returns: 无返回值。
    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "clickhouse", "mssql"])
    def test_other_dialects_are_unchanged(self, dialect: str) -> None:
        """Oracle Schema 不得触发其他方言的字符集转换。"""
        sql = 'SELECT order_date FROM orders UNION SELECT year_month FROM workforce_projects'
        baseline = rewrite_sql(sql, dialect)

        rewritten = rewrite_sql(sql, dialect, schema=report_schema())

        assert rewritten == baseline

    # 验证高频修正不留下逐次 INFO 或 SQL 正文。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_repeated_rewrites_only_emit_debug_summaries(self) -> None:
        """重复执行新规则仅有 DEBUG 计数，既有日志由各自回归测试负责。"""
        with capture_logs() as logs:
            for _ in range(20):
                rewrite_sql(REPORT_SQL, "oracle", schema=report_schema())

        events = [item for item in logs if "字符集" in str(item.get("event", ""))]
        assert events
        assert all(item["log_level"] == "debug" for item in events)
        assert all("sql" not in item and REPORT_SQL not in str(item) for item in events)

    # 验证无快照或空类型清单保持已有行为。
    # Args: self - 测试实例；schema - 缺少类型证据的快照。
    # Returns: 无返回值。
    @pytest.mark.parametrize("schema", [None, {}, {"tables": []}, SchemaSnapshot()])
    def test_empty_schema_keeps_existing_rewrite(self, schema: object) -> None:
        """不为得到可执行 SQL 而伪造列类型。"""
        baseline = rewrite_sql(REPORT_SQL, "oracle")

        rewritten = rewrite_sql(REPORT_SQL, "oracle", schema=schema)

        assert rewritten == baseline

    # 验证列类型缺失、重复列及大小写不同的真实表不会被错误合并。
    # Args: self - 测试实例；kind - Schema 异常或歧义形态。
    # Returns: 无返回值。
    @pytest.mark.parametrize("kind", ["missing_type", "duplicate_column", "duplicate_table", "case_sensitive"])
    def test_schema_ambiguities_do_not_guess(self, kind: str) -> None:
        """带引号标识符按真实大小写匹配，重复定义和缺失类型均保守回退。"""
        snapshot = report_schema()
        if kind == "missing_type":
            snapshot.tables[0].columns[0].type = ""
        elif kind == "duplicate_column":
            snapshot.tables[0].columns.append(ColumnInfo(name="order_date", type="NVARCHAR2"))
        elif kind == "duplicate_table":
            snapshot.tables.append(snapshot.tables[0])
        else:
            snapshot.tables[0].name = "ORDERS"
        sql = 'SELECT "order_date" FROM "orders" UNION SELECT "year_month" FROM "workforce_projects"'
        baseline = rewrite_sql(sql, "oracle")

        rewritten = rewrite_sql(sql, "oracle", schema=snapshot)

        assert rewritten == baseline

    # 验证未知函数位于内层集合时不能把合并结果猜成左侧的普通字符集。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_unknown_union_branch_does_not_inherit_first_type(self) -> None:
        """内层输出类型不明时，外层不对整个分支盲目推广类型。"""
        sql = '''(SELECT "order_date" AS "period" FROM "orders"
            UNION SELECT CUSTOM_FUNC("payment_time") FROM "payments")
            UNION SELECT "year_month" FROM "workforce_projects"'''
        baseline = rewrite_sql(sql, "oracle")

        rewritten = rewrite_sql(sql, "oracle", schema=report_schema())

        assert rewritten == baseline

    # 验证空值分支参与集合运算时仅转换明确普通文本，空值不强转。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_null_branch_does_not_block_known_charset_conversion(self) -> None:
        """NULL 是兼容占位，外层国家字符集只要求转换内层普通文本。"""
        sql = '''SELECT NULL AS "period" FROM DUAL
            UNION SELECT "order_date" FROM "orders"
            UNION SELECT "year_month" FROM "workforce_projects"'''

        rewritten = rewrite_sql(sql, "oracle", schema=report_schema())

        assert rewritten.count("TO_NCHAR(") == 1
        assert 'TO_NCHAR("order_date")' in rewritten
        assert "TO_NCHAR(NULL)" not in rewritten

    # 验证字符集分析异常保留栈信息而不把解析器携带的 SQL 正文写到日志。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_analysis_failure_keeps_traceback_without_sql(self) -> None:
        """分析失败回退原 SQL，且新规则只记录一次脱敏异常。"""
        from src.tools.sql_rewriter import _oracle_normalize_set_charsets

        secret_text = "SELECT 'private_business_value' FROM secret_table"
        with patch("sqlglot.optimizer.scope.traverse_scope", side_effect=ValueError(secret_text)):
            with capture_logs() as logs:
                rewritten, count = _oracle_normalize_set_charsets(REPORT_SQL, report_schema())

        assert rewritten == REPORT_SQL and count == 0
        failures = [entry for entry in logs if entry["log_level"] == "error"]
        assert len(failures) == 1
        assert failures[0]["error_type"] == "ValueError"
        assert failures[0]["exc_info"][2] is not None
        assert secret_text not in str(failures)

    # 验证空 SQL 和语法损坏仍交给下游安全校验，不伪造可执行结果。
    # Args: self - 测试实例；sql - 无效 SQL。
    # Returns: 无返回值。
    @pytest.mark.parametrize("sql", ["", "SELECT 'unterminated"])
    def test_invalid_sql_remains_invalid(self, sql: str) -> None:
        """字符集分析失败不会把非法查询改成合法查询。"""
        from src.tools.sql_rewriter import _oracle_normalize_set_charsets

        with capture_logs():
            rewritten, count = _oracle_normalize_set_charsets(sql, report_schema())

        assert rewritten == sql and count == 0
