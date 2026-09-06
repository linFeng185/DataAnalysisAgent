"""覆盖 16.5.1/16.5.2 的目录、规模、时间与跨源关联契约。"""

from collections import Counter
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.testdata.catalog import build_catalog
from src.testdata.generator import GenerationConfig, iter_batches, make_row, row_counts


class TestSyntheticGenerator:
    """覆盖功能 16.5.1、16.5.2。"""

    # 验证业务表不复制且每个数据源达到百万行。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_catalog_unique_domains_and_million_rows(self) -> None:
        """625 张全局唯一表均有业务字段，默认五源各两百万行。"""
        catalog = build_catalog()
        config = GenerationConfig(anchor_year=2026)
        counts = row_counts(config)
        assert counts["retail_order_items"] == 3 * counts["retail_orders"]
        assert counts["finance_invoices"] == counts["retail_orders"]
        assert len(catalog) == 625
        assert Counter(table.source for table in catalog.values()) == dict.fromkeys(
            ["retail", "finance", "supply", "workforce", "insight"], 125
        )
        for source in {table.source for table in catalog.values()}:
            assert (
                sum(counts[name] for name, table in catalog.items() if table.source == source)
                >= 1_000_000
            )
        for table in catalog.values():
            assert table.description
            assert len({column.name for column in table.columns}) == len(table.columns)
            assert any(
                column.name
                not in {
                    "id",
                    "event_date",
                    "year_month",
                    "region_code",
                    "channel_code",
                    "scenario",
                    "notes",
                }
                for column in table.columns
            )

    # 验证日期闭区间与闰年在各张事实表中均可出现。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_time_window_and_future_labels(self) -> None:
        """默认以锚点年为中心覆盖前后十年，日期维度包含闰日。"""
        config = GenerationConfig(anchor_year=2026, scale=1000)
        counts = row_counts(config)
        assert config.start_date == date(2016, 1, 1)
        assert config.end_date == date(2036, 12, 31)
        for table in build_catalog().values():
            first = make_row(table, 1, config, counts)
            last = make_row(table, counts[table.name], config, counts)
            assert first["event_date"] == config.start_date
            assert last["event_date"] == config.end_date
            assert first["scenario"] == "历史模拟"
            assert last["scenario"] == "未来情景"
        calendar = build_catalog()["retail_calendar"]
        dates = {row["event_date"] for batch in iter_batches(calendar, config) for row in batch}
        assert date(2024, 2, 29) in dates

    # 验证批大小不改变随机数据并保证种子生效。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_deterministic_bounded_batches(self) -> None:
        """随机访问、顺序生成、不同批次边界必须返回相同结果。"""
        table = build_catalog()["retail_orders"]
        config = GenerationConfig(scale=1000, anchor_year=2026, batch_size=7)
        batches = list(iter_batches(table, config))
        rows = [row for batch in batches for row in batch]
        other = config.model_copy(update={"batch_size": 19})
        assert all(len(batch) <= 7 for batch in batches)
        assert rows == [row for batch in iter_batches(table, other) for row in batch]
        assert rows[2] == make_row(table, 3, config, row_counts(config))
        assert rows[2] != make_row(
            table, 3, config.model_copy(update={"seed": 73}), row_counts(config)
        )

    # 验证强关联外键范围和订单跨库快照保持一致。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_references_and_order_snapshot(self) -> None:
        """所有引用指向存在的主键，发票和订单的核心数据不矛盾。"""
        config = GenerationConfig(scale=1000, anchor_year=2026)
        counts = row_counts(config)
        catalog = build_catalog()
        for table in catalog.values():
            for row_id in {1, min(3, counts[table.name]), counts[table.name]}:
                row = make_row(table, row_id, config, counts)
                for column in table.columns:
                    if column.reference:
                        assert 1 <= row[column.name] <= counts[column.reference]
        for batch in iter_batches(catalog["finance_invoices"], config):
            for invoice in batch:
                order = make_row(catalog["retail_orders"], invoice["order_id"], config, counts)
                assert invoice["customer_id"] == order["customer_id"]
                assert invoice["event_date"] == order["event_date"]
                assert invoice["invoice_amount"] == order["net_amount"]
                assert invoice["region_code"] == order["region_code"]

    # 验证订单明细的聚合金额与订单头保持一致。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_order_items_reconcile_and_edge_samples(self) -> None:
        """明细求和等于订单净额，同时包含退款和零值样本。"""
        config = GenerationConfig(scale=30_000, anchor_year=2026)
        catalog = build_catalog()
        totals: dict[int, Decimal] = {}
        for batch in iter_batches(catalog["retail_order_items"], config):
            for row in batch:
                totals[row["order_id"]] = (
                    totals.get(row["order_id"], Decimal(0)) + row["line_amount"]
                )
        orders = [row for batch in iter_batches(catalog["retail_orders"], config) for row in batch]
        assert all(totals[row["id"]] == row["net_amount"] for row in orders)
        assert any(row["net_amount"] == 0 for row in orders)
        assert any(row["net_amount"] < 0 for row in orders)
        assert any(row["notes"] is None for row in orders)

    # 验证输入校验和非法行号快速失败。
    # Args: self - 测试实例；changes - 非法配置覆盖。
    # Returns: 无返回值。
    @pytest.mark.parametrize(
        "changes",
        [
            {"scale": 0},
            {"batch_size": 0},
            {"years_before": -1},
            {"anchor_year": 1890},
            {"years_after": 200},
        ],
    )
    def test_invalid_config(self, changes: dict) -> None:
        """配置非法时不创建文件或连接数据库。"""
        with pytest.raises(ValidationError):
            GenerationConfig(**changes)

    # 验证不能越过预定义主键空间。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_invalid_row_id(self) -> None:
        """零值和超范围主键不生成静默错误数据。"""
        config = GenerationConfig(scale=100)
        table = build_catalog()["retail_orders"]
        counts = row_counts(config)
        for row_id in [0, counts[table.name] + 1]:
            with pytest.raises(ValueError):
                make_row(table, row_id, config, counts)
