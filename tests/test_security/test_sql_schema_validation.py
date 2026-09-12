"""SQL Schema 校验层单元测试。"""

from __future__ import annotations

import logging

import pytest

logger = logging.getLogger(__name__)


class TestTableIndex:
    """覆盖表/列索引与表归属解析（spec/39 §4.2、§5.1）。"""

    # 方法作用：验证索引键为 (数据源, 表名) 且缺省源回落到白名单首项。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_index_uses_datasource_and_table_key(self) -> None:
        """单源场景下表归属缺省回落到 datasources[0]。"""
        logger.debug("test_index_uses_datasource_and_table_key 入口")
        from src.security.sql_schema_validation import build_table_index

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}, {"name": "amount"}]}]

        # Act
        index = build_table_index(tables, ["pg_main"])

        # Assert
        assert index[("pg_main", "orders")] == frozenset({"id", "amount"})
        logger.info("test_index_uses_datasource_and_table_key 完成")

    # 方法作用：验证未授权源的表被整体跳过，不作为校验证据。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_index_skips_unauthorized_datasource(self) -> None:
        """datasource 不在白名单内的表必须整体跳过。"""
        logger.debug("test_index_skips_unauthorized_datasource 入口")
        from src.security.sql_schema_validation import build_table_index

        # Arrange
        tables = [
            {"name": "orders", "datasource": "pg_main", "columns": [{"name": "id"}]},
            {"name": "secret", "datasource": "pg_other", "columns": [{"name": "token"}]},
        ]

        # Act
        index = build_table_index(tables, ["pg_main"])

        # Assert
        assert ("pg_main", "orders") in index
        assert ("pg_other", "secret") not in index
        logger.info("test_index_skips_unauthorized_datasource 完成")

    # 方法作用：锁定空白名单契约——空白名单不做数据源过滤，快照条目全部进索引。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_empty_whitelist_skips_source_filter(self) -> None:
        """空白名单表示不做数据源过滤，调用方自带单一数据源快照。"""
        logger.debug("test_empty_whitelist_skips_source_filter 入口")
        from src.security.sql_schema_validation import build_table_index

        # Arrange
        # 空白名单表示「调用方提供的是单一数据源快照，不按源过滤」
        tables = [
            {"name": "orders", "columns": [{"name": "id"}, {"name": "amount"}]},
            {"name": "customers", "columns": [{"name": "name"}]},
        ]

        # Act
        index = build_table_index(tables, [])

        # Assert：白名单没有过滤掉任何条目，两条都落在缺省（空串）源键下
        assert index[("", "orders")] == frozenset({"id", "amount"})
        assert index[("", "customers")] == frozenset({"name"})
        assert len(index) == 2
        logger.info("test_empty_whitelist_skips_source_filter 完成")

    # 方法作用：锁定多源白名单下未声明数据源的表仍按首项回落，但必须留 warning 痕迹。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_multi_source_missing_datasource_falls_back_to_first(self) -> None:
        """多源白名单 + 表缺 datasource → 仍落到 datasources[0]，行为不变只留痕。"""
        logger.debug("test_multi_source_missing_datasource_falls_back_to_first 入口")
        from src.security.sql_schema_validation import build_table_index

        # Arrange：两颗源在册，orders 未声明归属
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]

        # Act
        index = build_table_index(tables, ["pg_a", "pg_b"])

        # Assert：回落行为保持不变，只落到首项源键下
        assert index == {("pg_a", "orders"): frozenset({"id"})}
        logger.info("test_multi_source_missing_datasource_falls_back_to_first 完成")

    # 方法作用：验证单源归属唯一、多源同名判为 ambiguous、不存在返回 None。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_resolve_owner_single_and_ambiguous(self) -> None:
        """归属解析三分支必须正确。"""
        logger.debug("test_resolve_owner_single_and_ambiguous 入口")
        from src.security.sql_schema_validation import build_table_index, resolve_table_owner

        # Arrange
        single = build_table_index(
            [{"name": "orders", "columns": [{"name": "id"}]}], ["pg_main"]
        )
        multi = build_table_index(
            [
                {"name": "orders", "datasource": "pg_a", "columns": [{"name": "id"}]},
                {"name": "orders", "datasource": "pg_b", "columns": [{"name": "amount"}]},
            ],
            ["pg_a", "pg_b"],
        )

        # Act & Assert
        assert resolve_table_owner(single, "orders") == "pg_main"
        assert resolve_table_owner(single, "missing") is None
        assert resolve_table_owner(multi, "orders") == "ambiguous"
        logger.info("test_resolve_owner_single_and_ambiguous 完成")


class TestValidationIssue:
    """覆盖 ValidationIssue 描述渲染（spec/39 §4.3）。"""

    # 方法作用：验证跨表错配描述包含「不属于表」与真正提供该列的表名。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_describe_cross_table_column(self) -> None:
        """describe() 必须点明真实归属，供模型改正。"""
        from src.security.sql_schema_validation import ValidationIssue

        # Arrange
        issue = ValidationIssue(
            type="cross_table_column",
            datasource="pg_main",
            table="finance_invoices",
            column="created_at",
            qualifier="i",
            provided_by=("orders",),
        )

        # Act
        text = issue.describe()

        # Assert
        assert "不属于表" in text
        assert "orders" in text
        assert "i.created_at" in text

    # 方法作用：验证解析失败描述包含「解析失败」字样。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_describe_parse_error(self) -> None:
        """parse_error 必须可被既有回归用例识别。"""
        logger.debug("test_describe_parse_error 入口")
        from src.security.sql_schema_validation import ValidationIssue

        # Arrange & Act
        text = ValidationIssue(type="parse_error", datasource="pg_main").describe()

        # Assert
        assert "解析失败" in text
        logger.info("test_describe_parse_error 完成")


class TestUnknownTable:
    """覆盖表级校验（spec/39 §5.1 步骤 4、证据 3）。"""

    # 方法作用：验证引用不存在的表被拦为高置信。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_unknown_table_blocked(self) -> None:
        """不存在的表 → unknown_table，high 置信。"""
        logger.debug("test_unknown_table_blocked 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT id FROM ghost_table",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].type == "unknown_table"
        assert issues[0].table == "ghost_table"
        assert issues[0].confidence == "high"
        logger.info("test_unknown_table_blocked 完成")

    # 方法作用：验证空基准时表引用降级为低置信，不误报「表不存在」。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_unknown_table_is_low_confidence_without_baseline(self) -> None:
        """没有任何权威基准时不得断言表不存在（设计 §5.4 无法判定不拦截）。"""
        logger.debug("test_unknown_table_is_low_confidence_without_baseline 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange：空基准——真实原因是快照不可得，而不是表不存在
        tables: list[dict] = []

        # Act
        issues = validate_sql_schema(
            "SELECT id FROM ghost_table",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert：仍留痕但降为低置信，避免误导重试去改表名，也不得产出任何 high 拦截
        table_issues = [item for item in issues if item.type == "unknown_table"]
        assert len(table_issues) == 1
        assert table_issues[0].table == "ghost_table"
        assert table_issues[0].confidence == "low"
        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_unknown_table_is_low_confidence_without_baseline 完成")

    # 方法作用：验证 CTE 名不被误判为不存在的表。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_cte_name_not_treated_as_unknown_table(self) -> None:
        """CTE 名会出现在 find_all(Table) 中，必须排除。"""
        logger.debug("test_cte_name_not_treated_as_unknown_table 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]
        sql = "WITH x AS (SELECT id FROM orders) SELECT x.id FROM x"

        # Act
        issues = validate_sql_schema(
            sql, datasources=["pg_main"], dialect="postgres", tables=tables
        )

        # Assert
        assert issues == []
        logger.info("test_cte_name_not_treated_as_unknown_table 完成")

    # 方法作用：验证派生表别名不被误判。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_derived_table_alias_not_treated_as_table(self) -> None:
        """派生表别名不出现在 find_all(Table) 中。"""
        logger.debug("test_derived_table_alias_not_treated_as_table 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "a"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT d.a FROM (SELECT a FROM orders) d",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert issues == []
        logger.info("test_derived_table_alias_not_treated_as_table 完成")


class TestQualifiedColumn:
    """覆盖带限定符的列引用校验（spec/39 §5.1 步骤 3、§5.4）。"""

    # 方法作用：验证限定符指向真实表时能查出该表缺失的列。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_unknown_column_on_known_table(self) -> None:
        """表存在但列不存在 → unknown_column，high 置信。"""
        logger.debug("test_unknown_column_on_known_table 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]

        # Act
        issues = validate_sql_schema(
            'SELECT "o"."bogus" FROM "orders" AS "o"',
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].type == "unknown_column"
        assert issues[0].column == "bogus"
        assert issues[0].table == "orders"
        assert issues[0].confidence == "high"
        logger.info("test_unknown_column_on_known_table 完成")

    # 方法作用：验证列名在别的表存在但不属于被引用表时判为跨表错配。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_cross_table_column_detected(self) -> None:
        """created_at 只属于 audit_log，却挂在 finance_invoices 的别名上。"""
        logger.debug("test_cross_table_column_detected 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [
            {"name": "finance_invoices", "columns": [{"name": "event_date"}]},
            {"name": "audit_log", "columns": [{"name": "created_at"}]},
        ]

        # Act
        issues = validate_sql_schema(
            "SELECT DATE_TRUNC('MONTH', \"i\".\"created_at\") "
            'FROM "finance_invoices" AS "i"',
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].type == "cross_table_column"
        assert issues[0].table == "finance_invoices"
        assert issues[0].provided_by == ("audit_log",)
        assert "不属于表" in issues[0].describe()
        logger.info("test_cross_table_column_detected 完成")

    # 方法作用：验证列真属于被引用表时不误报。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_valid_qualified_column_passes(self) -> None:
        """合法列必须放行。"""
        logger.debug("test_valid_qualified_column_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "finance_invoices", "columns": [{"name": "year_month"}]}]

        # Act
        issues = validate_sql_schema(
            'SELECT "i"."year_month" FROM "finance_invoices" AS "i"',
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert issues == []
        logger.info("test_valid_qualified_column_passes 完成")

    # 方法作用：验证表在索引中但列集为空时限定列判定降级，不产出 high 置信拦截。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_qualified_column_degrades_when_table_has_no_columns(self) -> None:
        """快照只知道表存在、未携带列信息时，不得把该表上的列判为幻觉列。"""
        logger.debug("test_qualified_column_degrades_when_table_has_no_columns 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange：orders 在册但列集为空（快照未展开列详情）
        tables = [{"name": "orders", "columns": []}]

        # Act
        issues = validate_sql_schema(
            'SELECT "o"."amount" FROM "orders" AS "o"',
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert：无列信息即无法确认列不存在，必须降级而不是 high 拦截
        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_qualified_column_degrades_when_table_has_no_columns 完成")

    # 方法作用：验证限定符指向 CTE 时判为无法判定而非幻觉。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_cte_qualifier_is_not_blocked(self) -> None:
        """CTE 列不在物理表快照中，不得判为幻觉。"""
        logger.debug("test_cte_qualifier_is_not_blocked 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]
        sql = "WITH t AS (SELECT id FROM orders) SELECT t.id FROM t"

        # Act
        issues = validate_sql_schema(
            sql, datasources=["pg_main"], dialect="postgres", tables=tables
        )

        # Assert
        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_cte_qualifier_is_not_blocked 完成")


class TestUnqualifiedColumn:
    """覆盖未限定列的归属判定（spec/39 §5.3）。"""

    # 方法作用：验证单表场景下未限定列能正确匹配。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_unqualified_single_table_passes(self) -> None:
        """单表未限定列命中即通过。"""
        logger.debug("test_unqualified_single_table_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "amount"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT amount FROM orders",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert issues == []
        logger.info("test_unqualified_single_table_passes 完成")

    # 方法作用：验证未限定列在多表同时存在时判为歧义列。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_unqualified_ambiguous_column(self) -> None:
        """id 在 orders 与 customers 同时存在 → ambiguous_column。"""
        logger.debug("test_unqualified_ambiguous_column 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [
            {"name": "orders", "columns": [{"name": "id"}, {"name": "cid"}]},
            {"name": "customers", "columns": [{"name": "id"}, {"name": "name"}]},
        ]

        # Act
        issues = validate_sql_schema(
            "SELECT id FROM orders o JOIN customers c ON o.cid = c.id",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].type == "ambiguous_column"
        assert issues[0].column == "id"
        logger.info("test_unqualified_ambiguous_column 完成")

    # 方法作用：验证未限定列在纯物理表作用域中零命中时判为幻觉。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_unqualified_zero_hit_is_hallucination(self) -> None:
        """全物理表 scope 中找不到该列 → unknown_column，high 置信。"""
        logger.debug("test_unqualified_zero_hit_is_hallucination 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "amount"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT bogus FROM orders",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].type == "unknown_column"
        assert issues[0].confidence == "high"
        assert issues[0].column == "bogus"
        logger.info("test_unqualified_zero_hit_is_hallucination 完成")

    # 方法作用：验证表在索引中但列集为空时未限定列零命中不判为 high 置信幻觉。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_column_check_degrades_when_table_has_no_columns(self) -> None:
        """快照只知道表存在、未携带列信息时，零命中不得判为幻觉列（否则所有查询都被误拦）。"""
        logger.debug("test_column_check_degrades_when_table_has_no_columns 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange：orders 在册但列集为空
        tables = [{"name": "orders", "columns": []}]

        # Act
        issues = validate_sql_schema(
            "SELECT amount FROM orders",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert：没有列信息就没有比对基准，不得产出 high 置信的 unknown_column
        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_column_check_degrades_when_table_has_no_columns 完成")

    # 方法作用：验证列信息已知时未知列仍被 high 置信拦截，空列集降级不开洞。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_column_check_still_blocks_when_columns_are_known(self) -> None:
        """列集已知且列不在其中时，必须继续产出 high 置信 unknown_column。"""
        logger.debug("test_column_check_still_blocks_when_columns_are_known 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange：orders 的列信息完整（只有 id）
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT amount FROM orders",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert：降级只针对无列信息的表，列信息已知的表照旧拦截
        assert len(issues) == 1
        assert issues[0].type == "unknown_column"
        assert issues[0].confidence == "high"
        assert issues[0].column == "amount"
        logger.info("test_column_check_still_blocks_when_columns_are_known 完成")

    # 方法作用：验证含 CTE 时未限定列零命中降级为低置信，不拦截。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_zero_hit_with_cte_is_low_confidence(self) -> None:
        """含 CTE 时零命中可能来自派生列，不得拦截。"""
        logger.debug("test_zero_hit_with_cte_is_low_confidence 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "amount"}]}]
        sql = "WITH t AS (SELECT amount AS derived FROM orders) SELECT derived FROM t"

        # Act
        issues = validate_sql_schema(
            sql, datasources=["pg_main"], dialect="postgres", tables=tables
        )

        # Assert
        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_zero_hit_with_cte_is_low_confidence 完成")


class TestKnownBlindSpots:
    """覆盖 spec/39 §10 盲区必须显式标记而非静默通过。"""

    # 方法作用：验证 SELECT * 产出低置信标记而不是「校验通过」。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_select_star_is_marked_low_confidence(self) -> None:
        """星号无法展开，必须留痕。"""
        logger.debug("test_select_star_is_marked_low_confidence 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT * FROM orders",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].confidence == "low"
        assert issues[0].column == "*"
        logger.info("test_select_star_is_marked_low_confidence 完成")

    # 方法作用：验证 COUNT(*) 不被误判为星号盲区（证据 2）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_count_star_is_not_flagged_as_blind_spot(self) -> None:
        """COUNT(*) 的星号在函数参数内，属合法用法。"""
        logger.debug("test_count_star_is_not_flagged_as_blind_spot 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT COUNT(*) FROM orders",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert issues == []
        logger.info("test_count_star_is_not_flagged_as_blind_spot 完成")

    # 方法作用：验证 SELECT t.* 被标记。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_qualified_star_is_marked(self) -> None:
        """t.* 同样是无法展开的星号投影。"""
        logger.debug("test_qualified_star_is_marked 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT t.* FROM orders t",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].column == "*"
        assert issues[0].confidence == "low"
        logger.info("test_qualified_star_is_marked 完成")

    # 方法作用：验证嵌套派生表里的星号不被多个 scope 重复留痕。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_nested_select_star_not_double_counted(self) -> None:
        """内外各一个星号必须恰好留 2 条痕迹，而不是 3 条。"""
        logger.debug("test_nested_select_star_not_double_counted 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange：外层 scope 若用 find_all(exp.Select) 会下探到内层，把内层星号再数一遍
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT * FROM (SELECT * FROM orders) x",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert：每个星号各留一条低置信痕迹
        star_issues = [
            item for item in issues if item.column == "*" and item.confidence == "low"
        ]
        assert len(star_issues) == 2
        logger.info("test_nested_select_star_not_double_counted 完成")


class TestProjectionAliasReferences:
    """覆盖 GROUP BY / HAVING 引用 SELECT 输出别名的合法形态（三种方言）。"""

    # 方法作用：验证 GROUP BY 引用投影别名不再被误判为幻觉列。
    # Args: self - pytest 用例实例；dialect - 参数化方言名。
    # Returns: 无返回值，断言失败由 pytest 报告。
    @pytest.mark.parametrize("dialect", ["postgres", "mysql", "clickhouse"])
    def test_group_by_alias_is_not_hallucination(self, dialect: str) -> None:
        """GROUP BY <输出别名> 是合法语法，不得高置信拦截。"""
        logger.debug(f"test_group_by_alias_is_not_hallucination 入口 dialect={dialect}")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [
            {"name": "orders", "columns": [{"name": "id"}, {"name": "amount"}]}
        ]

        # Act
        issues = validate_sql_schema(
            "SELECT SUM(amount) AS total FROM orders GROUP BY total",
            datasources=["pg_main"],
            dialect=dialect,
            tables=tables,
        )

        # Assert
        assert issues == []
        logger.info(f"test_group_by_alias_is_not_hallucination 完成 dialect={dialect}")

    # 方法作用：验证 ORDER BY 引用投影别名的既有行为不回退。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_order_by_alias_still_passes(self) -> None:
        """ORDER BY <别名> 一直放行，防回退。"""
        logger.debug("test_order_by_alias_still_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "amount"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT SUM(amount) AS total FROM orders ORDER BY total",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert issues == []
        logger.info("test_order_by_alias_still_passes 完成")

    # 方法作用：验证「真实列 GROUP BY + 别名 ORDER BY」形态不回退。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_group_by_real_column_with_alias_order_by_passes(self) -> None:
        """GROUP BY 真实列、ORDER BY 别名的既有形态必须放行。"""
        logger.debug("test_group_by_real_column_with_alias_order_by_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [
            {
                "name": "orders",
                "columns": [
                    {"name": "amount"},
                    {"name": "customer_id"},
                    {"name": "order_month"},
                ],
            }
        ]

        # Act
        issues = validate_sql_schema(
            "SELECT order_month, SUM(amount) AS total FROM orders "
            "GROUP BY order_month ORDER BY total",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert issues == []
        logger.info("test_group_by_real_column_with_alias_order_by_passes 完成")

    # 方法作用：验证放行别名不会把既非物理列也非别名的真幻觉一起放过。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_group_by_alias_does_not_mask_real_hallucination(self) -> None:
        """GROUP BY 的真幻觉列仍必须高置信拦截。"""
        logger.debug("test_group_by_alias_does_not_mask_real_hallucination 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange：ghost_col 既不在 orders 的列里，也不是任何投影别名
        tables = [{"name": "orders", "columns": [{"name": "amount"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT SUM(amount) AS total FROM orders GROUP BY ghost_col",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].type == "unknown_column"
        assert issues[0].column == "ghost_col"
        assert issues[0].confidence == "high"
        logger.info("test_group_by_alias_does_not_mask_real_hallucination 完成")

    # 方法作用：验证 HAVING 引用投影别名的合法形态放行。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_alias_reference_in_having_passes(self) -> None:
        """HAVING <别名> 是合法聚合过滤，不得拦截。"""
        logger.debug("test_alias_reference_in_having_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [
            {"name": "orders", "columns": [{"name": "amount"}, {"name": "customer_id"}]}
        ]

        # Act
        issues = validate_sql_schema(
            "SELECT SUM(amount) AS total FROM orders GROUP BY customer_id HAVING total > 100",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert issues == []
        logger.info("test_alias_reference_in_having_passes 完成")


class TestMultiSourceIsolation:
    """覆盖多源隔离契约（spec/39 §4.4）。"""

    # 方法作用：验证跨源同名表不串味，判为不可判定而非幻觉。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_same_table_name_across_sources(self) -> None:
        """a 源 orders 只有 id；b 源 orders 只有 amount。"""
        logger.debug("test_same_table_name_across_sources 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [
            {"name": "orders", "datasource": "pg_a", "columns": [{"name": "id"}]},
            {"name": "orders", "datasource": "pg_b", "columns": [{"name": "amount"}]},
        ]

        # Act
        issues = validate_sql_schema(
            'SELECT "o"."amount" FROM "orders" AS "o"',
            datasources=["pg_a", "pg_b"],
            dialect="postgres",
            tables=tables,
        )

        # Assert：同名歧义 → 归属不可判定 → 低置信，不得拦成幻觉
        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_same_table_name_across_sources 完成")

    # 方法作用：验证未授权源的表不参与校验，不作为「列存在」的证据。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_unauthorized_source_column_not_evidence(self) -> None:
        """pg_other 的列不得让 pg_main 的引用通过校验。"""
        logger.debug("test_unauthorized_source_column_not_evidence 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [
            {"name": "orders", "datasource": "pg_main", "columns": [{"name": "id"}]},
            {"name": "orders", "datasource": "pg_other", "columns": [{"name": "secret_col"}]},
        ]

        # Act
        issues = validate_sql_schema(
            'SELECT "o"."secret_col" FROM "orders" AS "o"',
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].type == "unknown_column"
        assert issues[0].datasource == "pg_main"
        logger.info("test_unauthorized_source_column_not_evidence 完成")


class TestValidationMetrics:
    """覆盖校验指标计数（spec/39 §9）。"""

    # 方法作用：验证指标方法存在且标签为低基数类型。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_record_schema_validation(self) -> None:
        """标签仅允许 issue_type / confidence / dialect / stage。"""
        logger.debug("test_record_schema_validation 入口")
        from src.observability import MetricsRegistry

        # Arrange
        registry = MetricsRegistry()

        # Act
        registry.record_schema_validation(
            issue_type="unknown_column",
            confidence="high",
            dialect="postgres",
            stage="stage1",
        )

        # Assert
        assert b"data_agent_sql_schema_validation_total" in registry.render()
        logger.info("test_record_schema_validation 完成")

    # 方法作用：验证解析失败的 SQL 也会上报 parse_error 指标。
    # Args: self - pytest 用例实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_parse_error_is_counted(self, monkeypatch) -> None:
        """解析失败的 SQL 必须计入指标，否则拦截率观测漏掉一整类。"""
        logger.debug("test_parse_error_is_counted 入口")
        from src.security import sql_schema_validation as module

        # Arrange：捕获上报调用，避免断言全局注册表导致用例互相污染
        recorded: list[dict] = []

        class _Recorder:
            """捕获指标上报入参的测试替身。"""

            # 方法作用：把一次上报的关键字参数追加到 recorded。
            # Args: self - 替身实例；kwargs - 上报关键字参数。
            # Returns: 无返回值。
            def record_schema_validation(self, **kwargs) -> None:
                logger.debug("_Recorder.record_schema_validation 入口")
                recorded.append(kwargs)
                logger.info("_Recorder.record_schema_validation 完成")

        monkeypatch.setattr(
            "src.observability.get_metrics_registry", lambda: _Recorder()
        )

        # Act：故意传入无法解析的 SQL
        module.validate_sql_schema(
            "SELECT * FROM (",
            datasources=["pg_main"],
            dialect="postgres",
            tables=[{"name": "orders", "columns": [{"name": "id"}]}],
        )

        # Assert
        assert any(item["issue_type"] == "parse_error" for item in recorded)
        logger.info("test_parse_error_is_counted 完成")
