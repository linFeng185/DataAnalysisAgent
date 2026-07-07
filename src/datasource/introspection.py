"""DB 内省 — 查询系统表获取元数据。仅执行原始 SQL，不做缓存不做语义加工。

模块边界: 本模块遵守 SPEC 14.1 约定，只返回 list[dict] / SchemaSnapshot，
不依赖 knowledge/ 或 memory/ 中的任何模块。
"""

from __future__ import annotations

from src.datasource.config import DataSourceConfig
from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableRelation, TableSchema
from src.logging_config import get_logger

logger = get_logger(__name__)


def _parse_nullable(value) -> bool:
    """归一化 nullable 值: MySQL 返回 "YES"/"NO" 字符串, PG 返回 bool。"""
    if isinstance(value, str):
        return value.upper() == "YES"
    return value is not False


COLUMNS_QUERY = {
    "clickhouse": "SELECT name, type, comment FROM system.columns WHERE table = :table",
    "mysql": """
        SELECT COLUMN_NAME AS name, COLUMN_TYPE AS type,
               COLUMN_COMMENT AS comment, IS_NULLABLE AS is_nullable,
               COLUMN_KEY AS column_key
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = :database AND TABLE_NAME = :table
    """,
    "postgres": """
        SELECT c.column_name AS name, c.data_type AS type,
               pgd.description AS comment,
               CASE WHEN c.is_nullable = 'YES' THEN true ELSE false END AS is_nullable,
               CASE WHEN pk.column_name IS NOT NULL THEN true ELSE false END AS is_primary_key
        FROM INFORMATION_SCHEMA.COLUMNS c
        LEFT JOIN pg_catalog.pg_description pgd
            ON pgd.objsubid = c.ordinal_position AND pgd.objoid = :table::regclass
        LEFT JOIN (
            SELECT ku.column_name FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku ON tc.constraint_name = ku.constraint_name
            WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_name = :table
        ) pk ON c.column_name = pk.column_name
        WHERE c.table_name = :table
    """,
    "oracle": """
        SELECT COLUMN_NAME AS name, DATA_TYPE AS type,
               NULL AS comment, NULLABLE AS is_nullable,
               NULL AS column_key
        FROM ALL_TAB_COLUMNS
        WHERE TABLE_NAME = UPPER(:table) AND OWNER = UPPER(:database)
    """,
    "mssql": """
        SELECT COLUMN_NAME AS name, DATA_TYPE AS type,
               ISNULL(CAST(ep.value AS VARCHAR(200)), chr(39)+chr(39)) AS comment,
               IS_NULLABLE AS is_nullable,
               CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN chr(39)+PRI+chr(39) ELSE NULL END AS column_key
        FROM INFORMATION_SCHEMA.COLUMNS c
        LEFT JOIN sys.extended_properties ep ON ep.major_id = OBJECT_ID(c.TABLE_NAME) AND ep.minor_id = c.ORDINAL_POSITION AND ep.name = chr(39)+MS_Description+chr(39)
        LEFT JOIN (
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE OBJECTPROPERTY(OBJECT_ID(CONSTRAINT_NAME), chr(39)+IsPrimaryKey+chr(39)) = 1 AND TABLE_NAME = :table
        ) pk ON c.COLUMN_NAME = pk.COLUMN_NAME
        WHERE c.TABLE_NAME = :table
    """,
}

FK_QUERY = {
    "mysql": """
        SELECT COLUMN_NAME AS column_name, REFERENCED_TABLE_NAME AS target_table,
               REFERENCED_COLUMN_NAME AS target_column
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = :database AND TABLE_NAME = :table AND REFERENCED_TABLE_NAME IS NOT NULL
    """,
    "postgres": """
        SELECT kcu.column_name, ccu.table_name AS target_table, ccu.column_name AS target_column
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu ON tc.constraint_name = kcu.constraint_name
        JOIN INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE ccu ON tc.constraint_name = ccu.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name = :table
    """,
    "clickhouse": None,
    "oracle": """
        SELECT a.COLUMN_NAME AS column_name, c_pk.TABLE_NAME AS target_table,
               a.COLUMN_NAME AS target_column
        FROM ALL_CONS_COLUMNS a
        JOIN ALL_CONSTRAINTS c ON a.CONSTRAINT_NAME = c.CONSTRAINT_NAME
        JOIN ALL_CONSTRAINTS c_pk ON c.R_CONSTRAINT_NAME = c_pk.CONSTRAINT_NAME
        WHERE c.CONSTRAINT_TYPE = chr(39)+R+chr(39) AND a.TABLE_NAME = UPPER(:table)
    """,
    "mssql": """
        SELECT COL_NAME(fkc.parent_object_id, fkc.parent_column_id) AS column_name,
               OBJECT_NAME(fkc.referenced_object_id) AS target_table,
               COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) AS target_column
        FROM sys.foreign_key_columns fkc
        WHERE OBJECT_NAME(fkc.parent_object_id) = :table
    """,
}

ROW_COUNT_QUERY = {
    "clickhouse": "SELECT COUNT(*) AS count FROM {table}",
    "mysql": "SELECT TABLE_ROWS AS table_rows FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = :database AND TABLE_NAME = :table",
    "postgres": "SELECT reltuples::bigint AS count FROM pg_class WHERE relname = :table",
    "oracle": "SELECT NUM_ROWS AS count FROM ALL_TABLES WHERE TABLE_NAME = UPPER(:table)",
    "mssql": "SELECT SUM(row_count) AS count FROM sys.dm_db_partition_stats WHERE OBJECT_ID = OBJECT_ID(:table) AND index_id IN (0,1)",
}


async def introspect_columns(
    ds: DataSourceConfig, table_name: str, executor
) -> list[ColumnInfo]:
    """查询指定表的所有字段。"""
    sql = COLUMNS_QUERY.get(ds.dialect)
    if not sql:
        logger.warning("不支持的方言", dialect=ds.dialect)
        return []
    result = await executor(ds, sql, {"table": table_name, "database": ds.database})
    columns = []
    for row in result:
        columns.append(ColumnInfo(
            name=row.get("name", ""),
            type=row.get("type", ""),
            comment=row.get("comment") or "",
            is_nullable=_parse_nullable(row.get("is_nullable", True)),
            is_primary_key=row.get("column_key") == "PRI" or row.get("is_primary_key", False),
            is_indexed=row.get("column_key", "") in ("PRI", "UNI", "MUL"),
        ))
    return columns


async def introspect_foreign_keys(
    ds: DataSourceConfig, table_name: str, executor
) -> list[TableRelation]:
    """查询指定表的外键关系。"""
    sql = FK_QUERY.get(ds.dialect)
    if not sql:
        return []
    result = await executor(ds, sql, {"table": table_name, "database": ds.database})
    return [
        TableRelation(
            target_table=row.get("target_table", ""),
            join_key=row.get("column_name", ""),
            relation_type="many_to_one",
        )
        for row in result
    ]


async def estimate_row_count(
    ds: DataSourceConfig, table_name: str, executor
) -> int:
    """估算表行数。"""
    sql = ROW_COUNT_QUERY.get(ds.dialect)
    if not sql:
        return 0
    try:
        result = await executor(ds, sql, {"table": table_name, "database": ds.database})
        if result:
            count = result[0].get("count", 0) or result[0].get("table_rows", 0) or 0
            return int(count)
    except Exception:
        logger.debug("行数估算失败", table=table_name)
    return 0


async def introspect_table(
    ds: DataSourceConfig, table_name: str, executor
) -> TableSchema:
    """内省单张表。"""
    columns = await introspect_columns(ds, table_name, executor)
    fks = await introspect_foreign_keys(ds, table_name, executor)
    row_count = await estimate_row_count(ds, table_name, executor)
    return TableSchema(
        name=table_name, columns=columns, relations=fks, row_count_estimate=row_count
    )


async def introspect_database(
    ds: DataSourceConfig, executor, table_names: list[str] | None = None
) -> SchemaSnapshot:
    """内省整个数据库。"""
    if table_names is None:
        table_names = await _list_tables(ds, executor)
    tables = []
    for name in table_names:
        try:
            tables.append(await introspect_table(ds, name, executor))
        except Exception as e:
            logger.warning("表内省失败", table=name, error=str(e))
    return SchemaSnapshot(tables=tables)


async def _list_tables(ds: DataSourceConfig, executor) -> list[str]:
    """列出所有表。"""
    sql_map = {
        "clickhouse": "SELECT name FROM system.tables WHERE database = :database",
        "mysql": "SELECT TABLE_NAME AS name FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = :database AND TABLE_TYPE = 'BASE TABLE'",
        "postgres": "SELECT tablename AS name FROM pg_catalog.pg_tables WHERE schemaname = 'public'",
    }
    sql = sql_map.get(ds.dialect, sql_map["postgres"])
    result = await executor(ds, sql, {"database": ds.database})
    return [r["name"] for r in result]
