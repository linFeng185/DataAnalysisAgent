"""生成五方言的可执行 DDL 和带安全字面量的离线 SQL。"""

import re
from datetime import date
from decimal import Decimal
from typing import Any

from src.testdata.catalog import TableSpec

DIALECTS = {"mysql", "postgres", "mssql", "oracle", "clickhouse", "sqlite"}


# 限制标识符来自程序目录，禁止路径或 SQL 片段混入。
# Args: name - 标识符；dialect - 数据库方言。
# Returns: 方言对应的引用标识符。
def quote_identifier(name: str, dialect: str) -> str:
    """验证并引用一个表名或列名。"""
    if dialect not in DIALECTS or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError("非法 SQL 标识符或方言")
    if dialect in {"mysql", "clickhouse"}:
        return f"`{name}`"
    if dialect == "mssql":
        return f"[{name}]"
    return f'"{name}"'


# 将业务逻辑类型映射为数据库原生类型，不依赖隐式日期格式。
# Args: table - 表规格；dialect - 数据库方言。
# Returns: 单条建表 SQL。
def build_ddl(table: TableSpec, dialect: str) -> str:
    """创建新表，不包含 DROP、TRUNCATE 或静默复用。"""
    from src.testdata.knowledge import column_description

    name = quote_identifier(table.name, dialect)
    types = {
        "integer": "BIGINT",
        "decimal": "DECIMAL(18, 2)",
        "date": "DATE",
        "text": "VARCHAR(256)",
    }
    if dialect == "oracle":
        types.update(integer="NUMBER(19)", decimal="NUMBER(18, 2)", text="NVARCHAR2(256)")
    elif dialect == "mssql":
        types["text"] = "NVARCHAR(256)"
    elif dialect == "clickhouse":
        types = {
            "integer": "Int64",
            "decimal": "Decimal(18, 2)",
            "date": "Date32",
            "text": "String",
        }
    columns = []
    for column in table.columns:
        datatype = types[column.kind]
        if dialect == "clickhouse":
            datatype = f"Nullable({datatype})" if column.nullable else datatype
            constraint = ""
        else:
            constraint = "" if column.nullable else " NOT NULL"
            if column.name == "id":
                constraint += " PRIMARY KEY"
        comment = (
            f" COMMENT {_literal(column_description(column), dialect)}"
            if dialect in {"mysql", "clickhouse"}
            else ""
        )
        columns.append(
            f"  {quote_identifier(column.name, dialect)} {datatype}{constraint}{comment}"
        )
    suffix = ""
    if dialect == "clickhouse":
        suffix = f" ENGINE = MergeTree() ORDER BY (event_date, id) COMMENT {_literal(table.description, dialect)}"
    elif dialect == "mysql":
        suffix = (
            f" ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT={_literal(table.description, dialect)}"
        )
    return f"CREATE TABLE {name} (\n" + ",\n".join(columns) + f"\n){suffix};"


# 为不支持内联注释的数据库生成表列元数据语句。
# Args: table - 表规格；dialect - 方言。
# Returns: 建表后依次执行的注释语句。
def schema_comments(table: TableSpec, dialect: str) -> list[str]:
    """中文说明进入真实 Schema 内省结果，不只停留在 Markdown。"""
    from src.testdata.knowledge import column_description

    table_name = quote_identifier(table.name, dialect)
    if dialect in {"mysql", "clickhouse", "sqlite"}:
        return []
    if dialect in {"postgres", "oracle"}:
        statements = [f"COMMENT ON TABLE {table_name} IS {_literal(table.description, 'postgres')}"]
        statements.extend(
            f"COMMENT ON COLUMN {table_name}.{quote_identifier(column.name, dialect)} IS {_literal(column_description(column), 'postgres')}"
            for column in table.columns
        )
        return statements
    statements = []
    for column_name, description in [
        (None, table.description),
        *[(column.name, column_description(column)) for column in table.columns],
    ]:
        statement = (
            "DECLARE @schema_name sysname = SCHEMA_NAME(); EXEC sys.sp_addextendedproperty "
            f"@name=N'MS_Description', @value={_literal(description, dialect)}, "
            "@level0type=N'SCHEMA', @level0name=@schema_name, "
            f"@level1type=N'TABLE', @level1name={_literal(table.name, dialect)}"
        )
        if column_name:
            statement += f", @level2type=N'COLUMN', @level2name={_literal(column_name, dialect)}"
        statements.append(statement)
    return statements


# 显式处理日期、Unicode、空值和引号，避免拼接注入或 NLS 依赖。
# Args: value - 已生成值；dialect - 数据库方言。
# Returns: 合法 SQL 字面量。
def _literal(value: Any, dialect: str) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, date):
        if dialect in {"oracle", "postgres"}:
            return f"DATE '{value.isoformat()}'"
        return f"'{value.isoformat()}'"
    escaped = str(value).replace("'", "''")
    if dialect in {"mysql", "clickhouse"}:
        escaped = escaped.replace("\\", "\\\\")
    prefix = "N" if dialect in {"mssql", "oracle"} else ""
    return f"{prefix}'{escaped}'"


# 输出单行 INSERT 保持 Oracle 和其他方言的一致执行能力。
# Args: table_name - 表名；row - 有序字段值；dialect - 目标方言。
# Returns: 包含结尾分号的 SQL。
def insert_sql(table_name: str, row: dict[str, Any], dialect: str) -> str:
    """离线 SQL 使用精确 Decimal 字面量，中文值不丢失。"""
    name = quote_identifier(table_name, dialect)
    columns = ", ".join(quote_identifier(column, dialect) for column in row)
    values = ", ".join(_literal(value, dialect) for value in row.values())
    return f"INSERT INTO {name} ({columns}) VALUES ({values});"
