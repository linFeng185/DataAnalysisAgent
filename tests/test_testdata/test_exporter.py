"""覆盖 16.5.3 的文件、方言、校验和及失败语义。"""

import gzip
import json
from pathlib import Path

import pytest
import sqlglot

from src.testdata.catalog import SOURCES, build_catalog
from src.testdata.exporter import export_dataset, verify_export
from src.testdata.generator import GenerationConfig
from src.testdata.sql import build_ddl, insert_sql, schema_comments


class TestDatasetExport:
    """覆盖功能 16.5.3。"""

    # 验证中文表列释义同时写入真实数据库元数据。
    # Args: self - 测试实例；dialect - 数据库方言。
    # Returns: 无返回值。
    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "mssql", "oracle", "clickhouse"])
    def test_chinese_schema_comments(self, dialect: str) -> None:
        """内省数据库时能获取中文含义，而非只在离线文件里看到注释。"""
        table = build_catalog()["retail_orders"]
        sql = build_ddl(table, dialect) + "\n".join(schema_comments(table, dialect))
        assert table.description in sql
        assert "业务日期" in sql
        assert "订单净额" in sql

    # 验证所有表在五种方言下的建表与类型约束。
    # Args: self - 测试实例；dialect - 数据库方言。
    # Returns: 无返回值。
    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "mssql", "oracle", "clickhouse"])
    def test_ddl_parses_all_tables(self, dialect: str) -> None:
        """3125 组 DDL 均可解析，ClickHouse 日期使用 Date32。"""
        for table in build_catalog().values():
            ddl = build_ddl(table, dialect)
            parsed = sqlglot.parse_one(ddl, read="tsql" if dialect == "mssql" else dialect)
            assert isinstance(parsed, sqlglot.exp.Create)
            assert "PRIMARY KEY" in ddl or "ORDER BY" in ddl
            if dialect == "clickhouse":
                assert "Date32" in ddl
                assert "Nullable(String)" in ddl

    # 验证真实落盘数据与清单一致，单源导出保留跨源关系。
    # Args: self - 测试实例；tmp_path - 临时目录；fmt - 文件格式。
    # Returns: 无返回值。
    @pytest.mark.parametrize("fmt", ["csv", "jsonl", "sql"])
    async def test_export_roundtrip_and_manifest(self, tmp_path: Path, fmt: str) -> None:
        """压缩文件、行数、日期、摘要与校验和可独立核验。"""
        output = tmp_path / "dataset"
        config = GenerationConfig(scale=100, anchor_year=2026, batch_size=7)
        manifest = await export_dataset(config, output, sources=["finance"], fmt=fmt)
        verified = await verify_export(output)
        assert manifest == verified
        assert manifest["status"] == "complete"
        assert len(manifest["tables"]) == 125
        assert manifest["total_rows"] >= 100
        assert manifest["external_dependencies"]
        assert (output / "relationships.json").is_file()
        assert (output / "questions.json").is_file()
        table = next(item for item in manifest["tables"] if item["name"] == "finance_invoices")
        assert table["min_date"] == "2016-01-01"
        assert table["max_date"] == "2036-12-31"
        with gzip.open(output / table["file"], "rt", encoding="utf-8") as stream:
            text = stream.read()
        assert "2016-01-01" in text
        assert "2036-12-31" in text
        assert len(table["sha256"]) == 64

    # 验证篡改产物无法通过独立校验。
    # Args: self - 测试实例；tmp_path - 临时目录。
    # Returns: 无返回值。
    async def test_verify_rejects_corruption_and_reuse(self, tmp_path: Path) -> None:
        """禁止静默覆盖和忽略校验和错误。"""
        output = tmp_path / "dataset"
        config = GenerationConfig(scale=100)
        manifest = await export_dataset(config, output, sources=["workforce"])
        with pytest.raises(FileExistsError):
            await export_dataset(config, output, sources=["workforce"])
        target = output / manifest["tables"][0]["file"]
        with target.open("ab") as stream:
            stream.write(b"corrupted")
        with pytest.raises(ValueError, match="校验"):
            await verify_export(output)

    # 验证非法源和格式在写文件之前失败。
    # Args: self - 测试实例；tmp_path - 临时目录。
    # Returns: 无返回值。
    async def test_invalid_export_input(self, tmp_path: Path) -> None:
        """拒绝空选择、未知源及路径注入形式的格式。"""
        config = GenerationConfig(scale=100)
        for sources, fmt in [([], "csv"), (["missing"], "csv"), (["retail"], "../sql")]:
            with pytest.raises(ValueError):
                await export_dataset(config, tmp_path / "not-created", sources=sources, fmt=fmt)
        assert not (tmp_path / "not-created").exists()

    # 验证文本转义、日期字面量及 SQL Server 中文保留。
    # Args: self - 测试实例；dialect - 数据库方言。
    # Returns: 无返回值。
    @pytest.mark.parametrize("dialect", [item[0] for item in SOURCES.values()])
    def test_insert_literal_roundtrip(self, dialect: str) -> None:
        """空值、单引号、反斜线及中文不破坏 SQL 语句边界。"""
        from datetime import date
        from decimal import Decimal

        row = {
            "id": 1,
            "notes": "中文 O'Reilly\\path",
            "event_date": date(2036, 12, 31),
            "net_amount": Decimal("-10.01"),
        }
        sql = insert_sql("retail_orders", row, dialect)
        assert len(sqlglot.parse(sql, read="tsql" if dialect == "mssql" else dialect)) == 1
        assert "-10.01" in sql
        if dialect == "mssql":
            assert "N'中文" in sql
        if dialect == "oracle":
            assert "DATE '2036-12-31'" in sql

    # 验证清单路径不能越过产物目录。
    # Args: self - 测试实例；tmp_path - 临时目录。
    # Returns: 无返回值。
    async def test_verify_rejects_path_traversal(self, tmp_path: Path) -> None:
        """即使手工修改清单也不能读取目录外文件。"""
        output = tmp_path / "dataset"
        await export_dataset(GenerationConfig(scale=100), output, sources=["finance"])
        manifest_file = output / "manifest.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["tables"][0]["file"] = "../secret"
        manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ValueError, match="路径"):
            await verify_export(output)
