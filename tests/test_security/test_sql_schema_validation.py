"""SQL Schema 校验层单元测试。"""

from __future__ import annotations

import logging

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
        from src.security.sql_schema_validation import ValidationIssue

        # Arrange & Act
        text = ValidationIssue(type="parse_error", datasource="pg_main").describe()

        # Assert
        assert "解析失败" in text
