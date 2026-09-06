"""覆盖 16.6：每源百表、旧数据不漂移和显式中文定义。"""

from collections import Counter
from datetime import date
from decimal import Decimal

import pytest

from src.testdata.catalog import DEFINITIONS, SOURCES, build_catalog
from src.testdata.generator import GenerationConfig, make_row, row_counts


class TestDomainExpansion:
    """覆盖 16.6.1 和 16.6.2。"""

    # 验证每个数据源独立达到百表以上，不接受全局累计凑数。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_each_source_has_125_distinct_business_tables(self) -> None:
        """每源 125 表、100 张新增表、十个新增业务主题。"""
        catalog = build_catalog()
        assert Counter(table.source for table in catalog.values()) == dict.fromkeys(SOURCES, 125)
        for source in SOURCES:
            expanded = [
                table for table in catalog.values() if table.source == source and table.subject
            ]
            assert len(expanded) == 100
            assert len({table.subject for table in expanded}) == 10
            assert len({table.description for table in expanded}) == 100
            assert len({tuple(column.name for column in table.columns) for table in expanded}) >= 90
            assert all(column.description for table in expanded for column in table.columns[7:])

    # 锁定先前已入库数据的主键空间和默认计数，避免扩容使外键漂移。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_legacy_counts_and_order_values_are_unchanged(self) -> None:
        """默认旧表仍合计 5,019,603 行，新表独立增加至少 500 万行。"""
        config = GenerationConfig(anchor_year=2026)
        catalog = build_catalog()
        counts = row_counts(config)
        original = {
            f"{source}_{definition[0]}"
            for source, definitions in DEFINITIONS.items()
            for definition in definitions
        }
        assert sum(counts[name] for name in original) == 5_019_603
        assert counts["retail_customers"] == 48929
        assert counts["retail_orders"] == counts["finance_invoices"] == 141510
        assert counts["retail_order_items"] == 424530
        for source in SOURCES:
            assert (
                sum(
                    counts[name]
                    for name, table in catalog.items()
                    if table.source == source and name not in original
                )
                >= 1_000_000
            )
        order = make_row(catalog["retail_orders"], 1, config, counts)
        assert order["event_date"] == date(2016, 1, 1)
        assert isinstance(order["net_amount"], Decimal)

    # 对新增表逐列检查引用与中文状态，并覆盖首末二十年窗口。
    # Args: self - 测试实例；source - 业务源。
    # Returns: 无返回值。
    @pytest.mark.parametrize("source", list(SOURCES))
    def test_expanded_rows_have_valid_references_and_dates(self, source: str) -> None:
        """所有新表都生成非空真实样本，并且关系指向已存在主键。"""
        catalog = build_catalog()
        config = GenerationConfig(anchor_year=2026)
        counts = row_counts(config)
        for table in catalog.values():
            if table.source != source or not table.subject:
                continue
            for row_id in [1, counts[table.name]]:
                row = make_row(table, row_id, config, counts)
                assert row["event_date"] == (config.start_date if row_id == 1 else config.end_date)
                assert row["scenario"] in {"历史模拟", "未来情景"}
                for column in table.columns:
                    if column.reference:
                        assert 1 <= row[column.name] <= counts[column.reference]
