"""覆盖 16.6.3 的安全扩容、基线一致性与数据库独立核验。"""

from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import sqlalchemy as sa
import sqlglot

from src.connectors.seed_verifier import verify_database_dataset, verify_database_table
from src.connectors.seed_writer import import_dataset, validate_baseline
from src.datasource.config import DataSourceConfig
from src.testdata.catalog import build_catalog
from src.testdata.generator import GenerationConfig, make_row, row_counts


# 构造小型旧清单，核验真实入库而非相信跳过标记。
# Args: config - 固定生成参数。
# Returns: 旧零售日历的导入清单。
def baseline_manifest(config: GenerationConfig) -> dict[str, Any]:
    """按日历实际生成规则构造旧数据验收清单。"""
    return {
        "status": "complete",
        "config": config.model_dump(),
        "sources": ["retail"],
        "tables": [
            {
                "name": "retail_calendar",
                "source": "retail",
                "rows": row_counts(config)["retail_calendar"],
                "min_date": str(config.start_date),
                "max_date": str(config.end_date),
                "sums": {},
                "null_counts": {"notes": row_counts(config)["retail_calendar"] // 13},
            }
        ],
    }


class TestIncrementalImport:
    """覆盖 16.6.3，不访问外部数据库。"""

    # 验证独立验收 API 汇总实库结果并在失败时释放连接。
    # Args: self - 测试实例；tmp_path - 临时目录；monkeypatch - 替身工具。
    # Returns: 无返回值。
    async def test_dataset_verification_and_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SQLite 完整验收成功，异常输入不返回成功且连接被关闭。"""
        config = GenerationConfig(scale=100, anchor_year=2026, years_before=0, years_after=0)
        target = DataSourceConfig(
            name="retail",
            dialect="sqlite",
            mode="external",
            database=str(tmp_path / "test_verify.db"),
        )
        monkeypatch.setattr(
            "src.connectors.seed_writer.build_catalog",
            Mock(return_value={"retail_calendar": build_catalog()["retail_calendar"]}),
        )
        await import_dataset(config, {"retail": target}, confirm_test_database=True)
        report = await verify_database_dataset({"retail": target}, baseline_manifest(config))
        assert report["status"] == "complete"
        assert report["sources"] == {"retail": {"tables": 1, "rows": 365}}
        connector = SimpleNamespace(
            create_engine=AsyncMock(side_effect=RuntimeError("模拟连接失败")), close=AsyncMock()
        )
        monkeypatch.setattr(
            "src.connectors.seed_verifier.create_connector", Mock(return_value=connector)
        )
        with pytest.raises(RuntimeError, match="模拟连接失败"):
            await verify_database_dataset({"retail": target}, baseline_manifest(config))
        connector.close.assert_awaited_once()

    # 验证五方言实际查询列与中文样本契约一致，包含各项损坏拦截。
    # Args: self - 测试实例；dialect - 五数据库方言；corruption - 预设损坏类别。
    # Returns: 无返回值。
    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "mssql", "oracle", "clickhouse"])
    @pytest.mark.parametrize(
        "corruption",
        [None, "sum", "date", "null", "reference", "sample", "columns", "sample_count"],
    )
    async def test_verifier_metrics_and_dialects(
        self, dialect: str, corruption: str | None
    ) -> None:
        """正常契约通过；金额、日期、空值、外键和样本任何一项损坏都会失败。"""
        config = GenerationConfig(scale=100, anchor_year=2026)
        table = build_catalog()["retail_business_accounts"]
        counts = row_counts(config)
        rows = [
            make_row(table, row_id, config, counts) for row_id in range(1, counts[table.name] + 1)
        ]
        count = len(rows)
        expected = {
            "rows": count,
            "min_date": str(config.start_date),
            "max_date": str(config.end_date),
            "sums": {"annual_spend": str(sum((row["annual_spend"] for row in rows), Decimal(0)))},
            "null_counts": {"notes": sum(row["notes"] is None for row in rows)},
        }
        metrics = {
            "row_count": count,
            "distinct_ids": count,
            "min_id": 1,
            "max_id": count,
            "min_date": config.start_date,
            "max_date": config.end_date,
            "sum_annual_spend": Decimal(expected["sums"]["annual_spend"]),
            "null_notes": expected["null_counts"]["notes"],
            "min_customer_id": min(row["customer_id"] for row in rows),
            "max_customer_id": max(row["customer_id"] for row in rows),
        }
        samples = deepcopy(rows)
        if corruption == "sum":
            metrics["sum_annual_spend"] += Decimal("0.01")
        elif corruption == "date":
            metrics["max_date"] = config.start_date
        elif corruption == "null":
            metrics["null_notes"] += 1
        elif corruption == "reference":
            metrics["max_customer_id"] = counts["retail_customers"] + 1
        elif corruption == "sample":
            samples[0]["industry"] = "错误行业"
        elif corruption == "columns":
            samples[0].pop("industry")
        elif corruption == "sample_count":
            samples.pop()
        connector = SimpleNamespace(
            config=SimpleNamespace(dialect=dialect),
            execute=AsyncMock(side_effect=[[metrics], samples]),
        )
        if corruption:
            with pytest.raises(ValueError):
                await verify_database_table(connector, table, expected, config)
        else:
            report = await verify_database_table(connector, table, expected, config)
            assert report["rows"] == count
            assert report["sample_ids"] == [1, 2]
        for invocation in connector.execute.await_args_list:
            query = invocation.args[0]
            assert isinstance(
                sqlglot.parse_one(query, read="tsql" if dialect == "mssql" else dialect),
                sqlglot.exp.Select,
            )
            if dialect == "oracle":
                assert '"retail_business_accounts"' in query

    # 验证旧表先核验再跳过，新增表实际提交且返回最终计数。
    # Args: self - 测试实例；tmp_path - 临时目录；monkeypatch - 替身工具。
    # Returns: 无返回值。
    async def test_extend_preserves_original_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """保留旧表并创建新增表，重跑时仍拒绝覆盖新增表。"""
        config = GenerationConfig(scale=100, anchor_year=2026, years_before=0, years_after=0)
        target = DataSourceConfig(
            name="retail",
            dialect="sqlite",
            mode="external",
            database=str(tmp_path / "test_extend.db"),
        )
        catalog = build_catalog()
        old = {"retail_calendar": catalog["retail_calendar"]}
        monkeypatch.setattr("src.connectors.seed_writer.build_catalog", Mock(return_value=old))
        await import_dataset(config, {"retail": target}, confirm_test_database=True)
        expanded = {**old, "retail_business_accounts": catalog["retail_business_accounts"]}
        monkeypatch.setattr("src.connectors.seed_writer.build_catalog", Mock(return_value=expanded))
        report = await import_dataset(
            config,
            {"retail": target},
            confirm_test_database=True,
            baseline=baseline_manifest(config),
        )
        assert report["preserved_rows"] == 365
        assert report["inserted_rows"] == row_counts(config)["retail_business_accounts"]
        assert len(report["tables"]) == 2
        with pytest.raises(FileExistsError):
            await import_dataset(
                config,
                {"retail": target},
                confirm_test_database=True,
                baseline=baseline_manifest(config),
            )

    # 验证旧基线的参数和表集合不能被伪造或漂移。
    # Args: self - 测试实例；change - 非法修改类别。
    # Returns: 无返回值。
    @pytest.mark.parametrize(
        "change", ["seed", "scale", "rows", "unknown", "duplicate", "incomplete"]
    )
    def test_baseline_rejects_incompatible_input(self, change: str) -> None:
        """基线错误在创建连接之前终止。"""
        config = GenerationConfig(scale=100, anchor_year=2026)
        baseline = baseline_manifest(config)
        if change in {"seed", "scale"}:
            baseline["config"][change] += 1
        elif change == "rows":
            baseline["tables"][0]["rows"] -= 1
        elif change == "unknown":
            baseline["tables"][0]["name"] = "unknown"
        elif change == "duplicate":
            baseline["tables"].append(baseline["tables"][0])
        else:
            baseline["status"] = "ddl_only"
        with pytest.raises(ValueError):
            validate_baseline(config, baseline)

    # 验证旧表内容变化后不创建任何新表。
    # Args: self - 测试实例；tmp_path - 临时目录；monkeypatch - 替身工具。
    # Returns: 无返回值。
    async def test_modified_baseline_aborts_before_insert(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """篡改旧行中文状态可被基线采样拦截。"""
        config = GenerationConfig(scale=100, anchor_year=2026, years_before=0, years_after=0)
        database = tmp_path / "test_mismatch.db"
        target = DataSourceConfig(
            name="retail", dialect="sqlite", mode="external", database=str(database)
        )
        catalog = build_catalog()
        monkeypatch.setattr(
            "src.connectors.seed_writer.build_catalog",
            Mock(return_value={"retail_calendar": catalog["retail_calendar"]}),
        )
        await import_dataset(config, {"retail": target}, confirm_test_database=True)
        engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
        try:
            with engine.begin() as connection:
                connection.execute(
                    sa.text("UPDATE retail_calendar SET scenario='已改动' WHERE id=1")
                )
            monkeypatch.setattr(
                "src.connectors.seed_writer.build_catalog",
                Mock(
                    return_value={
                        "retail_calendar": catalog["retail_calendar"],
                        "retail_business_accounts": catalog["retail_business_accounts"],
                    }
                ),
            )
            with pytest.raises(ValueError, match="样本"):
                await import_dataset(
                    config,
                    {"retail": target},
                    confirm_test_database=True,
                    baseline=baseline_manifest(config),
                )
            assert "retail_business_accounts" not in sa.inspect(engine).get_table_names()
        finally:
            engine.dispose()

    # 验证数据库定点指标或计数不符不能产生成功报告。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_verifier_rejects_wrong_counts(self) -> None:
        """独立核验读取真实查询结果而不是复用生成器行数。"""
        from src.connectors.seed_verifier import verify_database_table

        config = GenerationConfig(scale=100, anchor_year=2026)
        connector = SimpleNamespace(
            config=SimpleNamespace(dialect="postgres"),
            execute=AsyncMock(
                return_value=[
                    {
                        "row_count": 0,
                        "distinct_ids": 0,
                        "min_id": None,
                        "max_id": None,
                        "min_date": None,
                        "max_date": None,
                    }
                ]
            ),
        )
        with pytest.raises(ValueError, match="计数"):
            await verify_database_table(
                connector,
                build_catalog()["retail_calendar"],
                baseline_manifest(config)["tables"][0],
                config,
            )
