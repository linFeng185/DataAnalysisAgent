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

    # 方法作用：验证 columns 容器畸形（不可迭代）时降级为空列集并留 warning，不抛异常。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_index_skips_malformed_columns_container(self) -> None:
        """B1 回归：columns=5 之类的畸形容器必须降级，不得让异常击穿校验主流程。

        校验器的 layer3 调用点没有 try/except，这里一旦抛 TypeError 就是图节点崩溃，
        因此畸形数据必须与「非 Mapping 列条目」同策略：只丢列集并留痕。
        """
        logger.debug("test_index_skips_malformed_columns_container 入口")
        from structlog.testing import capture_logs

        from src.security.sql_schema_validation import build_table_index

        # Arrange：columns 为 int（不可迭代），另有一张正常表作对照
        tables = [
            {"name": "broken_table", "columns": 5},
            {"name": "orders", "columns": [{"name": "id"}]},
        ]

        # Act：不得抛异常
        with capture_logs() as events:
            index = build_table_index(tables, ["pg_main"])

        # Assert：畸形表降级为空列集仍进索引，正常表不受影响，且留下 warning
        assert index[("pg_main", "broken_table")] == frozenset()
        assert index[("pg_main", "orders")] == frozenset({"id"})
        assert any(
            event.get("event") == "表索引跳过畸形列容器"
            and event.get("table") == "broken_table"
            and event.get("container_type") == "int"
            for event in events
        )
        logger.info("test_index_skips_malformed_columns_container 完成")


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

    # 方法作用：验证目标表列集为空时跨表错配判定同样降级，不产出 high 拦截。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_cross_table_column_degrades_when_target_has_no_columns(self) -> None:
        """B5 回归：空列集降级必须也覆盖 cross_table_column，否则同样误拦。

        场景：finance_invoices 在册但列集为空、audit_log 有 created_at。
        没有目标表的列信息就无法确认「不属于该表」，必须与 unknown_column 一样降级。
        """
        logger.debug("test_cross_table_column_degrades_when_target_has_no_columns 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [
            {"name": "finance_invoices", "columns": []},
            {"name": "audit_log", "columns": [{"name": "created_at"}]},
        ]

        # Act
        issues = validate_sql_schema(
            'SELECT "i"."created_at" FROM "finance_invoices" AS "i"',
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert：跨表错配不得在目标表无列信息时升级为 high 拦截
        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_cross_table_column_degrades_when_target_has_no_columns 完成")


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


class TestAvailableColumnsTotal:
    """覆盖可用列清单截断后的真实总数下传（B4）。"""

    # 方法作用：验证 available_columns 被上限截断时真实总数仍完整可读。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_available_columns_capped_but_total_kept(self) -> None:
        """B4 回归：60 列表截断到 40 后，available_columns_total 必须报 60。

        回喂文案的「共 N 个」若用截断后的条数，模型会把 40 当成全部列，
        剩余 20 个合法列会被误判为不存在。
        """
        logger.debug("test_available_columns_capped_but_total_kept 入口")
        from src.security.sql_schema_validation import (
            MAX_AVAILABLE_COLUMNS,
            validate_sql_schema,
        )

        # Arrange：60 张三表，超过 MAX_AVAILABLE_COLUMNS
        tables = [
            {"name": "wide_table", "columns": [{"name": f"col_{i:02d}"} for i in range(1, 61)]}
        ]

        # Act
        issues = validate_sql_schema(
            "SELECT missing_col FROM wide_table",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert：清单被截断，真实总数不被截断
        assert len(issues) == 1
        assert len(issues[0].available_columns) == MAX_AVAILABLE_COLUMNS
        assert issues[0].available_columns_total == 60
        logger.info("test_available_columns_capped_but_total_kept 完成")

    # 方法作用：验证未截断时总数与清单长度一致，避免回退路径报错误总数。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_available_columns_total_matches_list_below_cap(self) -> None:
        """未截断时 available_columns_total 必须等于清单长度。"""
        logger.debug("test_available_columns_total_matches_list_below_cap 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}, {"name": "amount"}]}]

        # Act
        issues = validate_sql_schema(
            'SELECT "o"."bogus" FROM "orders" AS "o"',
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert issues[0].available_columns_total == len(issues[0].available_columns) == 2
        logger.info("test_available_columns_total_matches_list_below_cap 完成")


class TestDegradationWarnings:
    """覆盖降级判定的 warning 留痕（B5，spec/39 §10 盲区必须留痕不得静默）。

    这里用 structlog.testing.capture_logs 而不是 pytest 的 caplog：
    校验器走 structlog（默认路由到 stdout），且 setup_logging() 会清空 root
    handlers —— 实测 caplog 既看不到 structlog 输出，也看不到 setup_logging
    之后注入的 stdlib handler，断言会退化成「永远通过」。
    """

    # 方法作用：验证空列集表的未限定列零命中留 warning（reason=tables_have_no_columns）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_unqualified_degradation_leaves_warning_trail(self) -> None:
        """删掉 warning 这行本用例必须变红：降级不能静默。"""
        logger.debug("test_unqualified_degradation_leaves_warning_trail 入口")
        from structlog.testing import capture_logs

        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": []}]

        # Act
        with capture_logs() as events:
            validate_sql_schema(
                "SELECT amount FROM orders",
                datasources=["pg_main"],
                dialect="postgres",
                tables=tables,
            )

        # Assert
        assert any(
            event.get("event") == "列引用无法判定"
            and event.get("reason") == "tables_have_no_columns"
            for event in events
        ), f"缺少降级留痕，捕获事件: {[e.get('event') for e in events]}"
        logger.info("test_unqualified_degradation_leaves_warning_trail 完成")

    # 方法作用：验证空列集表的限定列判定留 warning（reason=table_has_no_columns）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_qualified_degradation_leaves_warning_trail(self) -> None:
        """限定列路径的降级同样必须留痕，不能静默放行。"""
        logger.debug("test_qualified_degradation_leaves_warning_trail 入口")
        from structlog.testing import capture_logs

        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": []}]

        # Act
        with capture_logs() as events:
            validate_sql_schema(
                'SELECT "o"."amount" FROM "orders" AS "o"',
                datasources=["pg_main"],
                dialect="postgres",
                tables=tables,
            )

        # Assert
        assert any(
            event.get("event") == "列引用无法判定"
            and event.get("reason") == "table_has_no_columns"
            for event in events
        ), f"缺少降级留痕，捕获事件: {[e.get('event') for e in events]}"
        logger.info("test_qualified_degradation_leaves_warning_trail 完成")


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
        recorder = _MetricRecorder()
        monkeypatch.setattr(
            "src.observability.get_metrics_registry", lambda: recorder
        )

        # Act：故意传入无法解析的 SQL
        module.validate_sql_schema(
            "SELECT * FROM (",
            datasources=["pg_main"],
            dialect="postgres",
            tables=[{"name": "orders", "columns": [{"name": "id"}]}],
        )

        # Assert
        assert any(item["issue_type"] == "parse_error" for item in recorder.recorded)
        logger.info("test_parse_error_is_counted 完成")

    # 方法作用：验证 stage 标签默认 stage1，调用方可显式传入 stage2 覆写。
    # Args: self - pytest 用例实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_metric_stage_label_comes_from_caller(self, monkeypatch) -> None:
        """B3 回归：阶段二复核必须上报 stage2，否则观测里阶段一/二无法区分。"""
        logger.debug("test_metric_stage_label_comes_from_caller 入口")
        from src.security import sql_schema_validation as module

        # Arrange
        recorder = _MetricRecorder()
        monkeypatch.setattr(
            "src.observability.get_metrics_registry", lambda: recorder
        )
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]

        # Act：一次默认（stage1），一次显式 stage2
        module.validate_sql_schema(
            "SELECT bogus FROM orders",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )
        module.validate_sql_schema(
            "SELECT bogus FROM orders",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
            stage="stage2",
        )

        # Assert
        stages = [item["stage"] for item in recorder.recorded]
        assert stages == ["stage1", "stage2"]
        logger.info("test_metric_stage_label_comes_from_caller 完成")


# 方法作用：捕获指标上报入参的测试替身，避免断言全局注册表导致用例互相污染。
# Args: 无。
# Returns: 无返回值。
class _MetricRecorder:
    """指标上报替身：把每次 record_schema_validation 的入参记入 recorded。"""

    # 方法作用：初始化空的上报记录列表。
    # Args: self - 替身实例。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self.recorded: list[dict] = []
        logger.debug("_MetricRecorder 构造完成")

    # 方法作用：把一次上报的关键字参数追加到 recorded。
    # Args: self - 替身实例；kwargs - 上报关键字参数。
    # Returns: 无返回值。
    def record_schema_validation(self, **kwargs) -> None:
        logger.debug("_MetricRecorder.record_schema_validation 入口")
        self.recorded.append(kwargs)
        logger.info("_MetricRecorder.record_schema_validation 完成")


class TestSchemaQualifierMisuse:
    """覆盖「schema 名被误用为列限定符」的形态。

    背景：sqlglot 把 `FROM analytics.orders` 的表名放在 `node.name`、schema 放在 `node.db`，
    而 `scope.sources` 的键只有表名（`orders`）。于是 `SELECT analytics.col FROM analytics.orders`
    里的限定符 `analytics` 在 `local_tables` 中查不到，会走「限定符指向 CTE」分支被静默放行，
    导致该列**完全不参与校验**。Postgres 规则：无别名时表别名就是表名，用 schema 名限定列
    本身非法，DB 会报 `missing FROM-clause entry`——所以必须拦截并给出可操作提示，
    而不是把问题留到打库。
    """

    # 方法作用：验证 schema 限定符被拦为高置信，而非静默放行。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_schema_qualifier_is_rejected(self) -> None:
        """SELECT analytics.bogus_col FROM analytics.orders 必须拦截。"""
        logger.debug("test_schema_qualifier_is_rejected 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        tables = [{"name": "orders", "columns": [{"name": "amount"}]}]

        issues = validate_sql_schema(
            "SELECT analytics.bogus_col FROM analytics.orders",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        high = [item for item in issues if item.confidence == "high"]
        assert len(high) == 1
        assert high[0].type == "invalid_qualifier"
        assert high[0].qualifier == "analytics"
        assert high[0].column == "bogus_col"
        logger.info("test_schema_qualifier_is_rejected 完成")

    # 方法作用：验证即便列名合法，schema 限定符本身仍被拒。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_schema_qualifier_rejected_for_valid_column_too(self) -> None:
        """列存在不代表限定符合法：DB 仍会拒绝该 SQL。"""
        logger.debug("test_schema_qualifier_rejected_for_valid_column_too 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        tables = [{"name": "orders", "columns": [{"name": "amount"}]}]

        issues = validate_sql_schema(
            "SELECT analytics.amount FROM analytics.orders",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        high = [item for item in issues if item.confidence == "high"]
        assert len(high) == 1
        assert high[0].type == "invalid_qualifier"
        logger.info("test_schema_qualifier_rejected_for_valid_column_too 完成")

    # 方法作用：验证用表名限定是合法写法，不得误伤。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_table_name_qualifier_still_passes(self) -> None:
        """SELECT orders.amount FROM analytics.orders 是合法的。"""
        logger.debug("test_table_name_qualifier_still_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        tables = [{"name": "orders", "columns": [{"name": "amount"}]}]

        issues = validate_sql_schema(
            "SELECT orders.amount FROM analytics.orders",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        assert issues == []
        logger.info("test_table_name_qualifier_still_passes 完成")

    # 方法作用：验证三段限定名（schema.table.col）仍正常工作。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_schema_qualified_three_part_column_passes(self) -> None:
        """SELECT analytics.orders.amount 的限定符是表名，不应被当作 schema 误用。"""
        logger.debug("test_schema_qualified_three_part_column_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        tables = [{"name": "orders", "columns": [{"name": "amount"}]}]

        issues = validate_sql_schema(
            "SELECT analytics.orders.amount FROM analytics.orders",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        assert issues == []
        logger.info("test_schema_qualified_three_part_column_passes 完成")

    # 方法作用：验证限定符指向 CTE 时仍低置信放行，不被误判为 schema 误用。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_cte_qualifier_still_low_confidence(self) -> None:
        """CTE 限定符与 schema 限定符必须区分对待。"""
        logger.debug("test_cte_qualifier_still_low_confidence 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        tables = [{"name": "orders", "columns": [{"name": "id"}]}]
        sql = "WITH t AS (SELECT id FROM orders) SELECT t.id FROM t"

        issues = validate_sql_schema(
            sql, datasources=["pg_main"], dialect="postgres", tables=tables
        )

        assert [item for item in issues if item.confidence == "high"] == []
        assert all(item.type != "invalid_qualifier" for item in issues)
        logger.info("test_cte_qualifier_still_low_confidence 完成")


class TestTypeAwareValidation:
    """覆盖类型感知校验：日期函数打在字符串列上必须拦截（ORA-01722 回归）。"""

    # 方法作用：构造 Oracle 零售域快照，日期列刻意用 VARCHAR2（复现线上真实数据形态）。
    # Args: self - pytest 用例实例。
    # Returns: 单表测试快照。
    def _oracle_tables(self) -> list[dict]:
        """orders 表：order_date 是 VARCHAR2 存 'YYYY-MM-DD'，created_at 才是真 DATE。"""
        return [
            {
                "name": "orders",
                "columns": [
                    {"name": "id", "type": "NUMBER"},
                    {"name": "order_date", "type": "VARCHAR2"},
                    {"name": "created_at", "type": "DATE"},
                    {"name": "total_amount", "type": "NUMBER"},
                    {"name": "note", "type": "VARCHAR2"},
                ],
            }
        ]

    # 方法作用：验证 TRUNC(varchar2, fmt) 被判为类型不匹配并 high 拦截。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_trunc_on_varchar2_column_blocked(self) -> None:
        """复现 ORA-01722：TRUNC 对 VARCHAR2 列会按 NUMBER 隐式转换。"""
        logger.debug("test_trunc_on_varchar2_column_blocked 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange：线上失败 SQL 的最小形态
        sql = "SELECT TRUNC(o.order_date, 'MM') AS month_start FROM orders o"

        # Act
        issues = validate_sql_schema(
            sql,
            datasources=["oracle"],
            dialect="oracle",
            tables=self._oracle_tables(),
        )

        # Assert
        high = [item for item in issues if item.confidence == "high"]
        assert len(high) == 1
        assert high[0].type == "type_mismatch"
        assert high[0].column == "order_date"
        logger.info("test_trunc_on_varchar2_column_blocked 完成")

    # 方法作用：验证 TRUNC 单参数形态（sqlglot 解析为 Anonymous）同样被拦截。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_trunc_single_arg_on_varchar2_blocked(self) -> None:
        """TRUNC(col) 单参数形态也不得漏检。"""
        logger.debug("test_trunc_single_arg_on_varchar2_blocked 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        issues = validate_sql_schema(
            "SELECT TRUNC(o.order_date) FROM orders o",
            datasources=["oracle"],
            dialect="oracle",
            tables=self._oracle_tables(),
        )

        high = [item for item in issues if item.confidence == "high"]
        assert len(high) == 1
        assert high[0].type == "type_mismatch"
        logger.info("test_trunc_single_arg_on_varchar2_blocked 完成")

    # 方法作用：验证真实 DATE 列上的 TRUNC 不得被误伤。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_trunc_on_date_column_passes(self) -> None:
        """DATE 列上的 TRUNC 是合法写法，必须放行。"""
        logger.debug("test_trunc_on_date_column_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        issues = validate_sql_schema(
            "SELECT TRUNC(o.created_at, 'MM') FROM orders o",
            datasources=["oracle"],
            dialect="oracle",
            tables=self._oracle_tables(),
        )

        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_trunc_on_date_column_passes 完成")

    # 方法作用：验证 TO_CHAR(varchar2, fmt) 被拦截（fmt 被静默忽略会产生错误结果）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_to_char_on_varchar2_column_blocked(self) -> None:
        """TO_CHAR 打在 VARCHAR2 上 fmt 无效，属静默错误，比报错更危险。"""
        logger.debug("test_to_char_on_varchar2_column_blocked 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        issues = validate_sql_schema(
            "SELECT TO_CHAR(o.order_date, 'YYYY-MM') FROM orders o",
            datasources=["oracle"],
            dialect="oracle",
            tables=self._oracle_tables(),
        )

        high = [item for item in issues if item.confidence == "high"]
        assert len(high) == 1
        assert high[0].type == "type_mismatch"
        assert high[0].column == "order_date"
        logger.info("test_to_char_on_varchar2_column_blocked 完成")

    # 方法作用：验证 DATE 列上的 TO_CHAR 格式化正常放行。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_to_char_on_date_column_passes(self) -> None:
        """DATE 列格式化是合法写法。"""
        logger.debug("test_to_char_on_date_column_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        issues = validate_sql_schema(
            "SELECT TO_CHAR(o.created_at, 'YYYY-MM') FROM orders o",
            datasources=["oracle"],
            dialect="oracle",
            tables=self._oracle_tables(),
        )

        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_to_char_on_date_column_passes 完成")

    # 方法作用：验证 TO_DATE(varchar2, fmt) 这条正确修法不得被误伤。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_to_date_on_varchar2_column_passes(self) -> None:
        """TO_DATE 就是字符串转日期，是日志中模型自纠后的正确写法，必须放行。"""
        logger.debug("test_to_date_on_varchar2_column_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        issues = validate_sql_schema(
            "SELECT TRUNC(TO_DATE(o.order_date, 'YYYY-MM-DD'), 'MM') FROM orders o",
            datasources=["oracle"],
            dialect="oracle",
            tables=self._oracle_tables(),
        )

        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_to_date_on_varchar2_column_passes 完成")

    # 方法作用：验证 ADD_MONTHS 打在字符串列上同样被拦截。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_add_months_on_varchar2_blocked(self) -> None:
        """ADD_MONTHS 与 TRUNC 同属「输入必须是 DATE」的函数。"""
        logger.debug("test_add_months_on_varchar2_blocked 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        issues = validate_sql_schema(
            "SELECT ADD_MONTHS(o.order_date, 1) FROM orders o",
            datasources=["oracle"],
            dialect="oracle",
            tables=self._oracle_tables(),
        )

        high = [item for item in issues if item.confidence == "high"]
        assert len(high) == 1
        assert high[0].type == "type_mismatch"
        logger.info("test_add_months_on_varchar2_blocked 完成")

    # 方法作用：验证 NUMBER 列上的 TRUNC 不被误伤（TRUNC 也是数值函数）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_trunc_on_number_column_passes(self) -> None:
        """TRUNC(number) 截断小数是合法用法。"""
        logger.debug("test_trunc_on_number_column_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        issues = validate_sql_schema(
            "SELECT TRUNC(o.total_amount) FROM orders o",
            datasources=["oracle"],
            dialect="oracle",
            tables=self._oracle_tables(),
        )

        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_trunc_on_number_column_passes 完成")

    # 方法作用：验证列类型缺失时降级放行，不产出 high 拦截。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_missing_column_type_degrades(self) -> None:
        """快照未携带类型信息时无法判定，按「无法判定不拦截」降级。"""
        logger.debug("test_missing_column_type_degrades 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange：列只有名字没有 type
        tables = [{"name": "orders", "columns": [{"name": "order_date"}]}]

        issues = validate_sql_schema(
            "SELECT TRUNC(o.order_date, 'MM') FROM orders o",
            datasources=["oracle"],
            dialect="oracle",
            tables=tables,
        )

        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_missing_column_type_degrades 完成")

    # 方法作用：验证非 Oracle 方言下同一 SQL 不被本特性拦截（本阶段只覆盖 Oracle）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_non_oracle_dialect_not_blocked(self) -> None:
        """MySQL 的 TRUNC 语义不同，本阶段不纳入，避免误伤。"""
        logger.debug("test_non_oracle_dialect_not_blocked 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        issues = validate_sql_schema(
            "SELECT TRUNC(o.order_date, 'MM') FROM orders o",
            datasources=["mysql"],
            dialect="mysql",
            tables=self._oracle_tables(),
        )

        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_non_oracle_dialect_not_blocked 完成")


class TestCteOutputScope:
    """覆盖 CTE 输出列作用域：外层引用 CTE 不存在的列必须拦截（ORA-00904 回归）。"""

    # 方法作用：构造带 CTE 重命名场景的 Oracle 快照。
    # Args: self - pytest 用例实例。
    # Returns: 单表测试快照。
    def _tables(self) -> list[dict]:
        """orders 表：外层误引用的 order_status 在 CTE 里已被改名。"""
        return [
            {
                "name": "orders",
                "columns": [
                    {"name": "id", "type": "NUMBER"},
                    {"name": "order_status", "type": "VARCHAR2"},
                ],
            }
        ]

    # 方法作用：验证外层引用 CTE 合法输出列正常放行（CTE 内改写不误伤）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_cte_valid_output_column_passes(self) -> None:
        """agg CTE 输出 return_reason_category，外层正确引用它 → 放行。"""
        logger.debug("test_cte_valid_output_column_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange：线上失败 SQL 的 CTE 结构复现
        sql = (
            "WITH order_base AS (SELECT id AS order_id, order_status FROM orders), "
            "agg AS (SELECT order_status AS return_reason_category FROM order_base) "
            "SELECT return_reason_category FROM agg"
        )

        # Act
        issues = validate_sql_schema(
            sql, datasources=["oracle"], dialect="oracle", tables=self._tables()
        )

        # Assert：CTE 内的合法引用不报
        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_cte_valid_output_column_passes 完成")

    # 方法作用：验证外层引用 CTE 里不存在的列被判为 high 拦截。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_cte_unknown_output_column_blocked(self) -> None:
        """外层引用 agg 不存在的列 → unknown_column（high）。"""
        logger.debug("test_cte_unknown_output_column_blocked 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        sql = (
            "WITH agg AS (SELECT order_status AS return_reason_category FROM orders) "
            "SELECT CASE WHEN order_status = 'RETURNED' THEN 1 ELSE 0 END FROM agg"
        )

        issues = validate_sql_schema(
            sql, datasources=["oracle"], dialect="oracle", tables=self._tables()
        )

        high = [item for item in issues if item.confidence == "high"]
        assert len(high) == 1
        assert high[0].type == "unknown_column"
        assert high[0].column == "order_status"
        # 回喂文案必须指明来源与可用列，否则模型只能继续猜
        assert high[0].table == "agg"
        assert "return_reason_category" in high[0].available_columns
        logger.info("test_cte_unknown_output_column_blocked 完成")

    # 方法作用：验证 CTE 含 SELECT * 时输出列无法枚举，降级放行。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_cte_star_output_degrades(self) -> None:
        """SELECT * 的 CTE 输出列不可枚举，必须退回低置信放行。"""
        logger.debug("test_cte_star_output_degrades 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        sql = "WITH agg AS (SELECT * FROM orders) SELECT order_status FROM agg"

        issues = validate_sql_schema(
            sql, datasources=["oracle"], dialect="oracle", tables=self._tables()
        )

        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_cte_star_output_degrades 完成")

    # 方法作用：验证限定符指向 CTE 且列不存在时同样被拦截。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_cte_qualifier_unknown_column_blocked(self) -> None:
        """agg.order_status 中 order_status 不在 agg 输出列里 → 拦截。"""
        logger.debug("test_cte_qualifier_unknown_column_blocked 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        sql = (
            "WITH agg AS (SELECT order_status AS return_reason_category FROM orders) "
            "SELECT agg.order_status FROM agg"
        )

        issues = validate_sql_schema(
            sql, datasources=["oracle"], dialect="oracle", tables=self._tables()
        )

        high = [item for item in issues if item.confidence == "high"]
        assert len(high) == 1
        assert high[0].column == "order_status"
        logger.info("test_cte_qualifier_unknown_column_blocked 完成")

    # 方法作用：验证限定符指向 CTE 且列存在时正常放行。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_cte_qualifier_valid_column_passes(self) -> None:
        """agg.return_reason_category 是合法引用。"""
        logger.debug("test_cte_qualifier_valid_column_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        sql = (
            "WITH agg AS (SELECT order_status AS return_reason_category FROM orders) "
            "SELECT agg.return_reason_category FROM agg"
        )

        issues = validate_sql_schema(
            sql, datasources=["oracle"], dialect="oracle", tables=self._tables()
        )

        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_cte_qualifier_valid_column_passes 完成")

    # 方法作用：验证 CTE 链中未被 FROM 引用的上游 CTE 输出列对外层不可见。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_cte_chain_unreferenced_cte_not_visible(self) -> None:
        """线上真实形态：order_status 在 order_base 里，但外层 FROM 只引用 agg → 拦截。

        sqlglot 把所有 CTE 都挂在 scope.sources 上，必须用 selected_sources
        只看 FROM/JOIN 实际引用的来源，否则该越界引用会被上游 CTE 意外「救活」。
        """
        logger.debug("test_cte_chain_unreferenced_cte_not_visible 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        sql = (
            "WITH order_base AS (SELECT id AS order_id, order_status FROM orders), "
            "agg AS (SELECT order_status AS return_reason_category FROM order_base) "
            "SELECT CASE WHEN order_status = 'RETURNED' THEN 1 ELSE 0 END FROM agg"
        )

        issues = validate_sql_schema(
            sql, datasources=["oracle"], dialect="oracle", tables=self._tables()
        )

        high = [item for item in issues if item.confidence == "high"]
        assert len(high) == 1
        assert high[0].type == "unknown_column"
        assert high[0].column == "order_status"
        logger.info("test_cte_chain_unreferenced_cte_not_visible 完成")

    # 方法作用：验证直接引用定义该列的 CTE 时仍合法放行（不误伤）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_cte_chain_referenced_cte_visible(self) -> None:
        """FROM 直接引用 order_base 时，order_status 是合法可见列。"""
        logger.debug("test_cte_chain_referenced_cte_visible 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        sql = (
            "WITH order_base AS (SELECT id AS order_id, order_status FROM orders), "
            "agg AS (SELECT order_status AS return_reason_category FROM order_base) "
            "SELECT order_status FROM order_base"
        )

        issues = validate_sql_schema(
            sql, datasources=["oracle"], dialect="oracle", tables=self._tables()
        )

        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_cte_chain_referenced_cte_visible 完成")


class TestValueFormatValidation:
    """覆盖 TO_DATE 格式串与列实际值格式的比对（ORA-01830/01840 回归）。"""

    # 方法作用：构造带 value_format 的 Oracle 零售域快照。
    # Args: self - pytest 用例实例。
    # Returns: 单表测试快照。
    def _tables(self) -> list[dict]:
        """payment_time 为 19 位带时间格式，order_date 为 10 位纯日期格式。"""
        return [
            {
                "name": "payments",
                "columns": [
                    {"name": "payment_time", "type": "VARCHAR2", "value_format": "YYYY-MM-DD HH24:MI:SS"},
                ],
            },
            {
                "name": "orders",
                "columns": [
                    {"name": "order_date", "type": "VARCHAR2", "value_format": "YYYY-MM-DD"},
                ],
            },
        ]

    # 方法作用：验证格式串短于实际值时被拦截（ORA-01830 的确定性形态）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_to_date_format_mismatch_blocked(self) -> None:
        """线上真实失败：payment_time 是 19 位，TO_DATE 只用 'YYYY-MM-DD' → 拦截。"""
        logger.debug("test_to_date_format_mismatch_blocked 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange：日志中的失败 SQL 最小形态
        sql = "SELECT TO_DATE(p.payment_time, 'YYYY-MM-DD') FROM payments p"

        # Act
        issues = validate_sql_schema(
            sql, datasources=["oracle"], dialect="oracle", tables=self._tables()
        )

        # Assert
        high = [item for item in issues if item.confidence == "high"]
        assert len(high) == 1
        assert high[0].type == "type_mismatch"
        assert high[0].column == "payment_time"
        # 回喂文案必须给出正确格式与推荐写法
        assert "YYYY-MM-DD HH24:MI:SS" in high[0].describe()
        logger.info("test_to_date_format_mismatch_blocked 完成")

    # 方法作用：验证格式串与列格式匹配时正常放行。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_to_date_format_match_passes(self) -> None:
        """正确的格式串必须放行，不得误伤。"""
        logger.debug("test_to_date_format_match_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        sql = "SELECT TO_DATE(p.payment_time, 'YYYY-MM-DD HH24:MI:SS') FROM payments p"

        issues = validate_sql_schema(
            sql, datasources=["oracle"], dialect="oracle", tables=self._tables()
        )

        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_to_date_format_match_passes 完成")

    # 方法作用：验证格式串大小写与空白差异不影响相等判定。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_to_date_format_case_insensitive_passes(self) -> None:
        """'yyyy-mm-dd hh24:mi:ss' 与列格式等价。"""
        logger.debug("test_to_date_format_case_insensitive_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        sql = "SELECT TO_DATE(p.payment_time, 'yyyy-mm-dd hh24:mi:ss') FROM payments p"

        issues = validate_sql_schema(
            sql, datasources=["oracle"], dialect="oracle", tables=self._tables()
        )

        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_to_date_format_case_insensitive_passes 完成")

    # 方法作用：验证推荐写法（字符串比较 + SUBSTR）不被误伤。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_recommended_string_approach_passes(self) -> None:
        """字符串比较与 SUBSTR 取月份是本库推荐写法。"""
        logger.debug("test_recommended_string_approach_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        sql = (
            "SELECT SUBSTR(p.payment_time, 1, 7) AS ym FROM payments p "
            "WHERE p.payment_time >= TO_CHAR(SYSDATE - 90, 'YYYY-MM-DD')"
        )

        issues = validate_sql_schema(
            sql, datasources=["oracle"], dialect="oracle", tables=self._tables()
        )

        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_recommended_string_approach_passes 完成")

    # 方法作用：验证列格式未知时降级放行（不误判）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_unknown_column_format_degrades(self) -> None:
        """采样未识别出格式的列跳过比对。"""
        logger.debug("test_unknown_column_format_degrades 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange：列无 value_format 字段
        tables = [{"name": "payments", "columns": [{"name": "payment_time", "type": "VARCHAR2"}]}]
        sql = "SELECT TO_DATE(p.payment_time, 'YYYY-MM-DD') FROM payments p"

        issues = validate_sql_schema(
            sql, datasources=["oracle"], dialect="oracle", tables=tables
        )

        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_unknown_column_format_degrades 完成")

    # 方法作用：验证 10 位纯日期列用 'YYYY-MM-DD' 正常放行。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_plain_date_column_correct_fmt_passes(self) -> None:
        """orders.order_date 用 'YYYY-MM-DD' 是正确的。"""
        logger.debug("test_plain_date_column_correct_fmt_passes 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        sql = "SELECT TO_DATE(o.order_date, 'YYYY-MM-DD') FROM orders o"

        issues = validate_sql_schema(
            sql, datasources=["oracle"], dialect="oracle", tables=self._tables()
        )

        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_plain_date_column_correct_fmt_passes 完成")

    # 方法作用：验证非 Oracle 方言不做格式比对（各库格式串语法不同）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_non_oracle_dialect_skips(self) -> None:
        """PostgreSQL 的 to_date 语法与 Oracle 不同，本阶段不纳入。"""
        logger.debug("test_non_oracle_dialect_skips 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        sql = "SELECT TO_DATE(p.payment_time, 'YYYY-MM-DD') FROM payments p"

        issues = validate_sql_schema(
            sql, datasources=["pg"], dialect="postgres", tables=self._tables()
        )

        assert [item for item in issues if item.confidence == "high"] == []
        logger.info("test_non_oracle_dialect_skips 完成")


class TestSystemPseudoTable:
    """覆盖校验层的系统伪表放行（Oracle 的 dual）。"""

    # 方法作用：验证 Oracle 下 dual 不产生 unknown_table 问题。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_oracle_dual_not_unknown_table(self) -> None:
        """dual 不在业务快照但引用合法，判为幻觉会误拦正确 SQL。"""
        logger.debug("test_oracle_dual_not_unknown_table 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT TO_CHAR(SYSDATE - 90, 'YYYY-MM-DD') AS d FROM dual",
            datasources=["oracle"],
            dialect="oracle",
            tables=tables,
        )

        # Assert
        assert issues == []
        logger.info("test_oracle_dual_not_unknown_table 完成")

    # 方法作用：验证其他方言的 dual 仍按未知表上报（白名单按方言隔离）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_postgres_dual_still_flagged(self) -> None:
        """PostgreSQL 没有 dual，引用它确实是未知表。"""
        logger.debug("test_postgres_dual_still_flagged 入口")
        from src.security.sql_schema_validation import validate_sql_schema

        tables = [{"name": "orders", "columns": [{"name": "id"}]}]

        issues = validate_sql_schema(
            "SELECT 1 AS x FROM dual",
            datasources=["pg"],
            dialect="postgres",
            tables=tables,
        )

        assert any(
            item.type == "unknown_table" and item.table == "dual" for item in issues
        )
        logger.info("test_postgres_dual_still_flagged 完成")
