"""DB 内省模块测试 — 2.5.1~9。覆盖 3 种方言 × 3 种查询 = 9 个函数。"""

from __future__ import annotations

import asyncio

import pytest

from src.datasource.config import DataSourceConfig
from src.datasource.introspection import (
    estimate_row_count,
    introspect_columns,
    introspect_database,
    introspect_foreign_keys,
    introspect_table,
)
from src.datasource.schema_snapshot import ColumnInfo, TableRelation


# ---- helpers ----

def _ds(dialect: str) -> DataSourceConfig:
    return DataSourceConfig(
        name=f"test_{dialect}", dialect=dialect, mode="embedded",
        host="localhost", port=9000, database="test_db", username="test",
    )


def _exec(rows: list[dict]):
    """返回固定数据的 mock executor。"""
    async def _run(ds, sql, params):
        return rows
    return _run


def _raise(exc: Exception):
    """抛出异常的 mock executor。"""
    async def _run(ds, sql, params):
        raise exc
    return _run


_EMPTY = _exec([])


# ================================================================
# introspect_columns() — 2.5.1 ClickHouse | 2.5.2 MySQL | 2.5.3 PG
# ================================================================

class TestIntrospectColumns:

    def test_clickhouse_returns_all_fields(self):
        """正常路径: ClickHouse system.columns 含 name/type/comment。"""
        cols = asyncio.run(introspect_columns(_ds("clickhouse"), "orders", _exec([
            {"name": "order_id", "type": "UInt64", "comment": "订单ID"},
            {"name": "amount", "type": "Decimal(18,2)", "comment": ""},
        ])))
        assert len(cols) == 2
        assert cols[0].name == "order_id"
        assert cols[0].type == "UInt64"
        assert cols[0].comment == "订单ID"
        assert cols[1].comment == ""

    def test_clickhouse_empty_table(self):
        """边界条件: 空表无字段。"""
        cols = asyncio.run(introspect_columns(_ds("clickhouse"), "empty", _EMPTY))
        assert cols == []

    def test_mysql_primary_key_detection(self):
        """正常路径: MySQL column_key=PRI 映射为 is_primary_key。"""
        cols = asyncio.run(introspect_columns(_ds("mysql"), "users", _exec([
            {"name": "id", "type": "bigint", "comment": "PK",
             "is_nullable": "NO", "column_key": "PRI"},
            {"name": "name", "type": "varchar(255)", "comment": "",
             "is_nullable": "YES", "column_key": ""},
        ])))
        assert cols[0].is_primary_key is True
        assert cols[0].is_nullable is False
        assert cols[1].is_primary_key is False
        assert cols[1].is_nullable is True

    def test_mysql_nullable_mapping(self):
        """边界条件: IS_NULLABLE YES/NO 正确映射 bool。"""
        cols = asyncio.run(introspect_columns(_ds("mysql"), "t", _exec([
            {"name": "a", "type": "int", "comment": "", "is_nullable": "YES", "column_key": ""},
            {"name": "b", "type": "int", "comment": "", "is_nullable": "NO", "column_key": ""},
        ])))
        assert cols[0].is_nullable is True
        assert cols[1].is_nullable is False

    def test_postgres_pk_detection(self):
        """正常路径: PostgreSQL is_primary_key 字段映射。"""
        cols = asyncio.run(introspect_columns(_ds("postgres"), "t", _exec([
            {"name": "id", "type": "integer", "comment": "PK",
             "is_nullable": False, "is_primary_key": True},
        ])))
        assert cols[0].is_primary_key is True

    def test_postgres_null_comment(self):
        """边界条件: PostgreSQL 无注释时 comment 为 None → 转为 ""。"""
        cols = asyncio.run(introspect_columns(_ds("postgres"), "t", _exec([
            {"name": "col", "type": "text", "comment": None,
             "is_nullable": True, "is_primary_key": False},
        ])))
        assert cols[0].comment == ""

    def test_unsupported_dialect_returns_empty(self):
        """边界条件: 未知方言返回空列表。"""
        cols = asyncio.run(introspect_columns(_ds("xxx"), "t", _EMPTY))
        assert cols == []

    def test_executor_exception_propagates(self):
        """错误路径: executor 异常向上传播。"""
        with pytest.raises(ValueError, match="boom"):
            asyncio.run(introspect_columns(
                _ds("mysql"), "t", _raise(ValueError("boom"))
            ))


# ================================================================
# introspect_foreign_keys() — 2.5.4 MySQL | 2.5.5 PG | 2.5.6 CH
# ================================================================

class TestIntrospectForeignKeys:

    def test_mysql_fk(self):
        """正常路径: MySQL 外键映射。"""
        fks = asyncio.run(introspect_foreign_keys(_ds("mysql"), "orders", _exec([
            {"column_name": "user_id", "target_table": "users", "target_column": "id"},
        ])))
        assert len(fks) == 1
        assert fks[0].target_table == "users"
        assert fks[0].join_key == "user_id"
        assert fks[0].relation_type == "many_to_one"

    def test_mysql_multiple_fk(self):
        """正常路径: 多外键。"""
        fks = asyncio.run(introspect_foreign_keys(_ds("mysql"), "posts", _exec([
            {"column_name": "author_id", "target_table": "users", "target_column": "id"},
            {"column_name": "cat_id", "target_table": "categories", "target_column": "id"},
        ])))
        assert len(fks) == 2

    def test_mysql_no_fk(self):
        """边界条件: 无外键返回空列表。"""
        fks = asyncio.run(introspect_foreign_keys(_ds("mysql"), "t", _EMPTY))
        assert fks == []

    def test_postgres_fk(self):
        """正常路径: PostgreSQL 外键。"""
        fks = asyncio.run(introspect_foreign_keys(_ds("postgres"), "t", _exec([
            {"column_name": "uid", "target_table": "users", "target_column": "id"},
        ])))
        assert len(fks) == 1

    def test_clickhouse_no_fk_support(self):
        """正常路径: ClickHouse 不支持外键，始终返回空。"""
        fks = asyncio.run(introspect_foreign_keys(_ds("clickhouse"), "t", _EMPTY))
        assert fks == []


# ================================================================
# estimate_row_count() — 2.5.7 CH | 2.5.8 MySQL | 2.5.9 PG
# ================================================================

class TestEstimateRowCount:

    def test_clickhouse_count(self):
        """正常路径: ClickHouse COUNT(*) 返回行数。"""
        c = asyncio.run(estimate_row_count(_ds("clickhouse"), "t", _exec([{"count": 1234567}])))
        assert c == 1_234_567

    def test_mysql_table_rows(self):
        """正常路径: MySQL INFORMATION_SCHEMA.TABLES。"""
        c = asyncio.run(estimate_row_count(_ds("mysql"), "t", _exec([{"table_rows": 50000}])))
        assert c == 50_000

    def test_postgres_reltuples(self):
        """正常路径: PostgreSQL pg_class.reltuples。"""
        c = asyncio.run(estimate_row_count(_ds("postgres"), "t", _exec([{"count": 88888}])))
        assert c == 88_888

    def test_zero_rows(self):
        """边界条件: 空表返回 0。"""
        c = asyncio.run(estimate_row_count(_ds("clickhouse"), "t", _exec([{"count": 0}])))
        assert c == 0

    def test_error_returns_zero(self):
        """错误路径: executor 异常返回 0 不崩溃。"""
        c = asyncio.run(estimate_row_count(_ds("mysql"), "t", _raise(RuntimeError("timeout"))))
        assert c == 0

    def test_empty_result_returns_zero(self):
        """边界条件: 无结果返回 0。"""
        c = asyncio.run(estimate_row_count(_ds("postgres"), "t", _EMPTY))
        assert c == 0


# ================================================================
# introspect_table() — 聚合函数
# ================================================================

class TestIntrospectTable:

    def test_full_introspection(self):
        """正常路径: 单表完整内省 = columns + fks + row_count。"""
        async def _exec(ds, sql, params):
            if "COLUMNS" in sql or "columns" in sql.lower():
                return [{"name": "id", "type": "int", "comment": "PK",
                         "is_nullable": "NO", "column_key": "PRI"}]
            if "KEY_COLUMN_USAGE" in sql or "pg_constraint" in sql or "CONSTRAINT" in sql:
                return []
            return [{"table_rows": 1000}]

        table = asyncio.run(introspect_table(_ds("mysql"), "users", _exec))
        assert table.name == "users"
        assert len(table.columns) == 1
        assert table.columns[0].is_primary_key is True
        assert table.row_count_estimate == 1000


# ================================================================
# introspect_database() — 整库内省
# ================================================================

class TestIntrospectDatabase:

    def test_specified_tables(self):
        """正常路径: 指定表列表返回 SchemaSnapshot。"""
        async def _exec(ds, sql, params):
            t = params["table"]
            return [{"name": f"{t}_id", "type": "int", "comment": "",
                     "is_nullable": "NO", "column_key": "PRI"}]

        schema = asyncio.run(
            introspect_database(_ds("mysql"), _exec, ["users", "orders"])
        )
        assert len(schema.tables) == 2
        assert {t.name for t in schema.tables} == {"users", "orders"}

    def test_single_failure_does_not_block(self):
        """错误路径: 某张表内省失败不阻塞其余表。"""
        async def _exec(ds, sql, params):
            t = params["table"]
            if t == "bad":
                raise RuntimeError("denied")
            return [{"name": "col", "type": "int", "comment": "",
                     "is_nullable": "NO", "column_key": ""}]

        schema = asyncio.run(
            introspect_database(_ds("mysql"), _exec, ["good", "bad"])
        )
        assert len(schema.tables) == 1
        assert schema.tables[0].name == "good"

    def test_all_failures(self):
        """边界条件: 所有表都内省失败返回空 SchemaSnapshot。"""
        async def _exec(ds, sql, params):
            raise RuntimeError("all tables failed")

        schema = asyncio.run(
            introspect_database(_ds("mysql"), _exec, ["t1", "t2"])
        )
        assert schema.tables == []
