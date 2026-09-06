"""覆盖 16.5.4 的实际事务、批量写入与危险目标阻断。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import sqlalchemy as sa

from src.connectors.seed_writer import import_dataset
from src.datasource.config import DataSourceConfig
from src.testdata.generator import GenerationConfig


class TestDatasetImport:
    """覆盖功能 16.5.4，不访问任何真实外部数据库。"""

    # 验证离线脚本终止符不传入 Oracle 驱动执行边界。
    # Args: self - 测试实例；monkeypatch - 替身工具。
    # Returns: 无返回值。
    async def test_oracle_ddl_strips_script_terminator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Oracle 原生执行不接受末尾分号，但离线 DDL 仍须保留分号。"""
        import src.connectors.seed_writer as writer
        from src.testdata.catalog import build_catalog
        from src.testdata.generator import row_counts
        from src.testdata.sql import build_ddl

        config = GenerationConfig(scale=100, batch_size=3)
        table = build_catalog()["workforce_departments"]
        counts = row_counts(config)
        target = DataSourceConfig(
            name="workforce",
            dialect="oracle",
            mode="external",
            username="TEST_USER",
            database="XEPDB1",
        )
        connector = SimpleNamespace(config=target, engine=object())
        calls: list[str] = []

        # 模拟 Oracle 对单语句终止符的严格校验。
        # Args: engine - 引擎替身；statement - SQL 对象；rows - 批次参数。
        # Returns: 计数查询返回目标行数，其他语句无返回值。
        async def execute(
            engine: object, statement: object, rows: list[dict[str, object]] | None = None
        ) -> int | None:
            sql = str(statement)
            calls.append(sql)
            assert not sql.rstrip().endswith(";"), "Oracle DBAPI 不接受脚本末尾分号"
            return counts[table.name] if sql.startswith("SELECT COUNT") else None

        monkeypatch.setattr(writer, "_execute", execute)
        actual = await writer._import_table(connector, table, config)
        assert actual == counts[table.name]
        assert calls[0].startswith('CREATE TABLE "workforce_departments"')
        assert build_ddl(table, "oracle").endswith(";")

    # 验证同步引擎仍通过线程与显式事务写入。
    # Args: self - 测试实例；tmp_path - 临时目录；monkeypatch - 替身工具。
    # Returns: 无返回值。
    async def test_sync_engine_transaction_path(self, tmp_path: Path, monkeypatch) -> None:
        """覆盖 SQL Server/Oracle 所使用的同步引擎分支。"""
        database = tmp_path / "test_sync.db"
        engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
        target = DataSourceConfig(
            name="finance", dialect="sqlite", mode="external", database=str(database)
        )
        connector = SimpleNamespace(
            config=target,
            engine=engine,
            create_engine=AsyncMock(return_value=engine),
            close=AsyncMock(side_effect=engine.dispose),
        )
        monkeypatch.setattr(
            "src.connectors.seed_writer.create_connector", Mock(return_value=connector)
        )
        report = await import_dataset(
            GenerationConfig(scale=100), {"finance": target}, confirm_test_database=True
        )
        assert report["total_rows"] >= 100
        connector.close.assert_awaited_once()
        with engine.connect() as connection:
            assert (
                connection.execute(sa.text("SELECT COUNT(*) FROM finance_invoices")).scalar_one()
                > 0
            )
        engine.dispose()

    # 验证 ClickHouse 原生批量写入和失败时关闭资源。
    # Args: self - 测试实例；monkeypatch - 替身工具；fail_insert - 是否模拟异常。
    # Returns: 无返回值。
    @pytest.mark.parametrize("fail_insert", [False, True])
    async def test_clickhouse_batch_boundary(self, monkeypatch, fail_insert: bool) -> None:
        """不连接外部 ClickHouse，验证参数、计数和异常不被吞掉。"""
        from datetime import date
        from decimal import Decimal

        counts: dict[str, int] = {}

        # 模拟当前测试库的表列表和行数查询。
        # Args: sql - 查询 SQL。
        # Returns: ClickHouse 风格结果。
        def query(sql: str) -> SimpleNamespace:
            if sql == "SHOW TABLES":
                return SimpleNamespace(result_rows=[])
            name = sql.rsplit(" ", 1)[-1].strip("`")
            return SimpleNamespace(result_rows=[[counts[name]]])

        # 核验原生驱动参数类型，并仅累计行数以保持有界内存。
        # Args: name - 表名；rows - 值矩阵；column_names - 列名列表。
        # Returns: 无返回值。
        def insert(name: str, rows: list, column_names: list[str]) -> None:
            if fail_insert:
                raise RuntimeError("模拟批次写入失败")
            assert len(rows) <= 3
            assert isinstance(rows[0][column_names.index("event_date")], date)
            assert any(isinstance(value, (str, Decimal)) for value in rows[0])
            counts[name] = counts.get(name, 0) + len(rows)

        target = DataSourceConfig(
            name="insight", dialect="clickhouse", mode="external", database="test_insight"
        )
        client = SimpleNamespace(
            command=Mock(), query=Mock(side_effect=query), insert=Mock(side_effect=insert)
        )
        engine = SimpleNamespace(client=client)
        connector = SimpleNamespace(
            config=target,
            engine=engine,
            create_engine=AsyncMock(return_value=engine),
            close=AsyncMock(),
        )
        monkeypatch.setattr(
            "src.connectors.seed_writer.create_connector", Mock(return_value=connector)
        )
        if fail_insert:
            with pytest.raises(RuntimeError, match="模拟批次"):
                await import_dataset(
                    GenerationConfig(scale=100, batch_size=3),
                    {"insight": target},
                    confirm_test_database=True,
                )
        else:
            report = await import_dataset(
                GenerationConfig(scale=100, batch_size=3),
                {"insight": target},
                confirm_test_database=True,
            )
            assert len(report["tables"]) == 125
            assert report["total_rows"] == sum(counts.values())
        connector.close.assert_awaited_once()

    # 验证 SQLite 实际建表、批量提交及重复导入拒绝。
    # Args: self - 测试实例；tmp_path - 临时目录。
    # Returns: 无返回值。
    async def test_real_transactions_and_existing_table_guard(self, tmp_path: Path) -> None:
        """首次写入可在新连接读回，第二次不破坏原表。"""
        database = tmp_path / "test_finance.db"
        target = DataSourceConfig(
            name="finance", dialect="sqlite", mode="external", database=str(database)
        )
        config = GenerationConfig(scale=100, anchor_year=2026, batch_size=3)
        report = await import_dataset(config, {"finance": target}, confirm_test_database=True)
        assert len(report["tables"]) == 125
        engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    sa.text("SELECT COUNT(*) FROM finance_invoices")
                ).scalar_one()
                assert rows > 0
                assert (
                    connection.execute(
                        sa.text("SELECT MIN(event_date) FROM finance_invoices")
                    ).scalar_one()
                    == "2016-01-01"
                )
            with pytest.raises(FileExistsError):
                await import_dataset(config, {"finance": target}, confirm_test_database=True)
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        sa.text("SELECT COUNT(*) FROM finance_invoices")
                    ).scalar_one()
                    == rows
                )
        finally:
            engine.dispose()

    # 验证显式确认及数据库白名单在连接之前生效。
    # Args: self - 测试实例；database - 危险库名；confirmed - 是否确认。
    # Returns: 无返回值。
    @pytest.mark.parametrize(
        "database,confirmed",
        [("master", True), ("postgres", True), ("production", True), ("test_finance", False)],
    )
    async def test_production_targets_are_rejected(self, database: str, confirmed: bool) -> None:
        """确认不能绕过专用测试库名称约束。"""
        target = DataSourceConfig(
            name="finance", dialect="postgres", mode="external", database=database
        )
        with pytest.raises(ValueError):
            await import_dataset(
                GenerationConfig(scale=100), {"finance": target}, confirm_test_database=confirmed
            )

    # 验证所有目标都先检查，避免先写一库才发现另一库危险。
    # Args: self - 测试实例；tmp_path - 临时目录。
    # Returns: 无返回值。
    async def test_all_targets_validated_before_side_effects(self, tmp_path: Path) -> None:
        """混入非法业务源时不创建首个 SQLite 文件。"""
        database = tmp_path / "test_finance.db"
        target = DataSourceConfig(
            name="finance", dialect="sqlite", mode="external", database=str(database)
        )
        with pytest.raises(ValueError):
            await import_dataset(
                GenerationConfig(scale=100),
                {"finance": target, "unknown": target},
                confirm_test_database=True,
            )
        assert not database.exists()
