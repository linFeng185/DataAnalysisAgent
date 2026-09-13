"""DB 内省 — 查询系统表获取元数据。仅执行原始 SQL，不做缓存不做语义加工。

模块边界: 本模块遵守 SPEC 14.1 约定，只返回 list[dict] / SchemaSnapshot，
不依赖 knowledge/ 或 memory/ 中的任何模块。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from src.datasource.config import DataSourceConfig
from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableRelation, TableSchema
from src.logging_config import get_logger

logger = get_logger(__name__)

_METADATA_PERMISSION_CODES = {
    "42501",  # PostgreSQL insufficient_privilege
    "1044", "1045",  # MySQL database/credential access denied
    "01031", "00942",  # Oracle insufficient privileges / hidden relation
    "229",  # SQL Server SELECT permission denied
    "497",  # ClickHouse ACCESS_DENIED
}
_METADATA_PERMISSION_MARKERS = (
    "permission denied",
    "insufficient privilege",
    "insufficient privileges",
    "access denied",
    "not authorized",
    "not authorised",
    "does not have permission",
    "无权限",
    "权限不足",
)


class MetadataPermissionError(PermissionError):
    """数据库拒绝读取 Schema 元数据时使用的脱敏统一异常。"""

    # 方法作用：保存告警所需的低敏数据源、方言和操作信息。
    # Args: self - 异常实例；datasource - 数据源名称；dialect - 方言；operation - 元数据操作。
    # Returns: 无返回值。
    def __init__(self, *, datasource: str, dialect: str, operation: str) -> None:
        super().__init__(f"数据源 '{datasource}' 缺少 Schema 元数据读取权限")
        self.datasource = datasource
        self.dialect = dialect
        self.operation = operation


# 方法作用：按方言错误码和稳定消息片段识别元数据权限异常。
# Args: exc - 数据库驱动或 SQLAlchemy 抛出的异常。
# Returns: 属于权限拒绝时返回 True。
def is_metadata_permission_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        code = str(
            getattr(current, "sqlstate", "")
            or getattr(current, "pgcode", "")
            or getattr(current, "code", "")
            or ""
        ).strip().upper()
        message = str(current).lower()
        if code in _METADATA_PERMISSION_CODES:
            return True
        if any(marker in message for marker in _METADATA_PERMISSION_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


# 方法作用：将底层权限异常转换为不携带 SQL 和连接细节的统一异常。
# Args: ds - 数据源配置；operation - 元数据操作；exc - 底层异常。
# Returns: MetadataPermissionError 实例。
def _permission_error(
    ds: DataSourceConfig,
    operation: str,
    exc: BaseException,
) -> MetadataPermissionError:
    logger.error(
        "Schema 元数据权限不足",
        datasource=ds.name,
        dialect=ds.dialect,
        operation=operation,
        error_type=type(exc).__name__,
        exc_info=True,
    )
    return MetadataPermissionError(
        datasource=ds.name,
        dialect=ds.dialect,
        operation=operation,
    )


def _parse_nullable(value) -> bool:
    """归一化 nullable 值: MySQL 返回 "YES"/"NO" 字符串, PG 返回 bool。"""
    if isinstance(value, str):
        return value.upper() == "YES"
    return value is not False


# 方言 → 库内命名空间的默认值。仅 Postgres / MSSQL 需要：它们的 schema 在库内，
# 而 MySQL / ClickHouse 的命名空间就是连接的 database，无需默认值。
DEFAULT_NAMESPACE = {
    "postgres": "public",
    "mssql": "dbo",
}


# 方法作用：解析数据源实际使用的命名空间，供元数据查询的 schema / database 过滤。
# Args: ds - 数据源配置。
# Returns: 命名空间字符串；该方言无命名空间概念时返回空串。
def resolve_namespace(ds: DataSourceConfig) -> str:
    """按方言语义确定命名空间。

    MySQL / ClickHouse 的命名空间就是连接的 database；Postgres / MSSQL 取配置的
    extra_params.schema，未显式配置时回落方言默认值（public / dbo）；
    SQLite 等无命名空间概念的方言返回空串。
    """
    logger.debug("解析命名空间入口", dialect=ds.dialect, database=ds.database)
    if ds.dialect in {"mysql", "clickhouse"}:
        resolved = str(ds.database or "")
    else:
        configured = str((ds.extra_params or {}).get("schema", "") or "").strip()
        resolved = configured or DEFAULT_NAMESPACE.get(ds.dialect, "")
    logger.info("解析命名空间完成", dialect=ds.dialect, namespace=resolved)
    return resolved


# 方言 → 表名列表查询。`:schema` 占位符由 resolve_namespace 的取值绑定：
# Postgres 原先写死 `schemaname = 'public'`，使非 public schema 的数据源永远列不出表；
# MSSQL 原先按 TABLE_CATALOG（库名）过滤，无法区分同库不同 schema 的同名表。
TABLE_NAME_QUERY = {
    "clickhouse": "SELECT name FROM system.tables WHERE database = :schema",
    "mysql": "SELECT TABLE_NAME AS name FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = :schema AND TABLE_TYPE = 'BASE TABLE'",
    "postgres": "SELECT tablename AS name FROM pg_catalog.pg_tables WHERE schemaname = :schema",
    "oracle": """
            SELECT TABLE_NAME AS name
            FROM ALL_TABLES
            WHERE OWNER = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
            ORDER BY TABLE_NAME
        """,
    "mssql": "SELECT TABLE_NAME AS name FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = :schema AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME",
    "sqlite": "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
}


COLUMNS_QUERY = {
    "clickhouse": "SELECT name, type, comment FROM system.columns WHERE table = :table AND database = :schema",
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
            ON pgd.objsubid = c.ordinal_position
            AND pgd.objoid = (
                SELECT cls.oid FROM pg_class cls
                JOIN pg_namespace ns ON ns.oid = cls.relnamespace
                WHERE cls.relname = :table AND ns.nspname = :schema
            )
        LEFT JOIN (
            SELECT ku.column_name FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku ON tc.constraint_name = ku.constraint_name
            WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_name = :table
              AND tc.table_schema = :schema
        ) pk ON c.column_name = pk.column_name
        WHERE c.table_name = :table AND c.table_schema = :schema
    """,
    "oracle": """
        SELECT c.COLUMN_NAME AS name, c.DATA_TYPE AS type,
               NULL AS column_comment, c.NULLABLE AS is_nullable,
               CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 'PRI' ELSE NULL END AS column_key
        FROM ALL_TAB_COLUMNS c
        LEFT JOIN (
            SELECT acc.COLUMN_NAME
            FROM ALL_CONS_COLUMNS acc
            JOIN ALL_CONSTRAINTS ac
              ON ac.OWNER = acc.OWNER AND ac.CONSTRAINT_NAME = acc.CONSTRAINT_NAME
            WHERE ac.CONSTRAINT_TYPE = 'P'
              AND ac.OWNER = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
              AND acc.TABLE_NAME = :table_name
        ) pk ON pk.COLUMN_NAME = c.COLUMN_NAME
        WHERE c.TABLE_NAME = :table_name
          AND c.OWNER = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
    """,
    "mssql": """
        SELECT c.COLUMN_NAME AS name, c.DATA_TYPE AS type,
               CAST(ep.value AS NVARCHAR(200)) AS comment,
               c.IS_NULLABLE AS is_nullable,
               CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 'PRI' ELSE NULL END AS column_key
        FROM INFORMATION_SCHEMA.COLUMNS c
        LEFT JOIN sys.extended_properties ep
            ON ep.major_id = OBJECT_ID(c.TABLE_SCHEMA + '.' + c.TABLE_NAME)
            AND ep.minor_id = c.ORDINAL_POSITION
            AND ep.name = 'MS_Description'
        LEFT JOIN (
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE OBJECTPROPERTY(OBJECT_ID(CONSTRAINT_NAME), 'IsPrimaryKey') = 1
              AND TABLE_NAME = :table AND TABLE_SCHEMA = :schema
        ) pk ON c.COLUMN_NAME = pk.COLUMN_NAME
        WHERE c.TABLE_NAME = :table AND c.TABLE_SCHEMA = :schema
        ORDER BY c.ORDINAL_POSITION
    """,
    "sqlite": "PRAGMA table_info({table})",
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
          AND tc.table_schema = :schema
    """,
    "clickhouse": None,
    "oracle": """
        SELECT a.COLUMN_NAME AS column_name, c_pk.TABLE_NAME AS target_table,
               c_pkc.COLUMN_NAME AS target_column
        FROM ALL_CONS_COLUMNS a
        JOIN ALL_CONSTRAINTS c
          ON c.OWNER = a.OWNER AND c.CONSTRAINT_NAME = a.CONSTRAINT_NAME
        JOIN ALL_CONSTRAINTS c_pk
          ON c_pk.OWNER = c.R_OWNER AND c_pk.CONSTRAINT_NAME = c.R_CONSTRAINT_NAME
        JOIN ALL_CONS_COLUMNS c_pkc
          ON c_pkc.OWNER = c_pk.OWNER
         AND c_pkc.CONSTRAINT_NAME = c_pk.CONSTRAINT_NAME
         AND c_pkc.POSITION = a.POSITION
        WHERE c.CONSTRAINT_TYPE = 'R'
          AND c.OWNER = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
          AND a.TABLE_NAME = :table_name
    """,
    "mssql": """
        SELECT COL_NAME(fkc.parent_object_id, fkc.parent_column_id) AS column_name,
               OBJECT_NAME(fkc.referenced_object_id) AS target_table,
               COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) AS target_column
        FROM sys.foreign_key_columns fkc
        WHERE OBJECT_NAME(fkc.parent_object_id) = :table
          AND OBJECT_SCHEMA_NAME(fkc.parent_object_id) = :schema
    """,
    "sqlite": "PRAGMA foreign_key_list({table})",
}

ROW_COUNT_QUERY = {
    "clickhouse": "SELECT COUNT(*) AS count FROM {table}",
    "mysql": "SELECT TABLE_ROWS AS table_rows FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = :database AND TABLE_NAME = :table",
    "postgres": "SELECT cls.reltuples::bigint AS count FROM pg_class cls JOIN pg_namespace ns ON ns.oid = cls.relnamespace WHERE cls.relname = :table AND ns.nspname = :schema",
    "oracle": """
        SELECT NUM_ROWS AS count
        FROM ALL_TABLES
        WHERE TABLE_NAME = :table_name
          AND OWNER = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
    """,
    "mssql": "SELECT SUM(row_count) AS count FROM sys.dm_db_partition_stats WHERE OBJECT_ID = OBJECT_ID(:schema + '.' + :table) AND index_id IN (0,1)",
    "sqlite": "SELECT COUNT(*) AS count FROM {table}",
}


def _quote_sqlite_identifier(identifier: str) -> str:
    """安全引用 SQLite 标识符。

    Args:
        identifier: 表名等 SQLite 标识符。

    Returns:
        双引号转义后的标识符。
    """
    logger.debug("SQLite 标识符引用入口", identifier=identifier)
    quoted = '"' + identifier.replace('"', '""') + '"'
    logger.info("SQLite 标识符引用完成", identifier=identifier)
    return quoted


# 方法作用：校验并引用 ClickHouse 数据库名和表名，阻止标识符注入。
# Args: identifier - 表名或 database.table 完整名称。
# Returns: 使用反引号逐段引用的 ClickHouse 标识符。
def _quote_clickhouse_identifier(identifier: str) -> str:
    """ClickHouse 行数探针不支持绑定表名，因此只允许保守标识符字符集。"""
    parts = str(identifier or "").split(".")
    if not parts or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", part) for part in parts):
        raise ValueError("ClickHouse 表名包含非法字符")
    return ".".join(f"`{part}`" for part in parts)


# ── 文本日期列的值格式采样 ────────────────────────────────
# 很多库里"日期"以文本存储（如 Oracle 的 VARCHAR2 存 'YYYY-MM-DD'），且同一库内
# 不同列的格式可能不一致（'YYYY-MM-DD' vs 'YYYY-MM-DD HH24:MI:SS'）。模型看不到
# 真实格式就会用错 TO_DATE 格式串（ORA-01830/01840）。这里采样少量值识别格式，
# 写回 ColumnInfo.value_format 供 Prompt 标注，所有操作只读且失败降级。

# 字符串族类型前缀：只有文本列才需要标注值格式
_TEXT_TYPE_PREFIXES = (
    "char", "varchar", "nvarchar", "nchar", "text", "string", "clob",
)

# 列名提示：含这些词才可能是「以文本存储的日期/时间列」，避免对全部文本列采样
_DATE_LIKE_NAME_RE = re.compile(r"(date|time|_at$|day$|month$)", re.IGNORECASE)

# 每次采样的行数上限
_VALUE_FORMAT_SAMPLE_ROWS = 5

# 值格式模式（顺序敏感）：带时间的模式必须排在纯日期模式之前，否则会被抢占
_VALUE_FORMAT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$"), "YYYY-MM-DD HH24:MI:SS"),
    (re.compile(r"^\d{4}-\d{2}-\d{2}$"), "YYYY-MM-DD"),
    (re.compile(r"^\d{4}/\d{2}/\d{2}$"), "YYYY/MM/DD"),
    (re.compile(r"^\d{4}-\d{2}$"), "YYYY-MM"),
    (re.compile(r"^\d{2}:\d{2}:\d{2}$"), "HH24:MI:SS"),
]


# 方法作用：剥离类型声明中的长度/精度后缀，返回基类型小写名。
# Args: column_type - 类型字符串，如 "VARCHAR2(50)"。
# Returns: 基类型小写名；空输入返回空串。
def _base_column_type(column_type: str) -> str:
    """VARCHAR2(50) -> varchar2。"""
    normalized = str(column_type or "").strip().lower()
    if not normalized:
        return ""
    return re.split(r"[(\s]", normalized, maxsplit=1)[0]


# 方法作用：判断列是否为「字符串族类型 + 列名暗示日期/时间」的采样候选。
# Args: name - 列名；column_type - 类型字符串。
# Returns: 是候选返回 True。
def is_date_like_text_column(name: str, column_type: str) -> bool:
    """两个条件同时满足才算候选，避免对全部文本列发起采样。"""
    base = _base_column_type(column_type)
    if not base or not base.startswith(_TEXT_TYPE_PREFIXES):
        return False
    return bool(_DATE_LIKE_NAME_RE.search(str(name or "")))


# 方法作用：从采样值识别统一的值格式标签。
# Args: samples - 采样得到的原始值序列。
# Returns: 全部非空样例命中同一模式时返回格式标签；无样例或格式混杂返回空串。
def detect_value_format(samples: list[Any]) -> str:
    """格式混杂或无法识别时返回空串：不标注胜过标注错误。"""
    texts = [str(v).strip() for v in samples if v is not None and str(v).strip()]
    if not texts:
        return ""
    for pattern, label in _VALUE_FORMAT_PATTERNS:
        if all(pattern.match(text) for text in texts):
            logger.debug("值格式识别完成", format=label, sample_count=len(texts))
            return label
    return ""


# 方法作用：构建单列值格式采样 SQL，兼容各方言的行数限制语法。
# Args: dialect_name - 方言名；quoted_table - 已引用表名；quoted_column - 已引用列名。
# Returns: 采样语句；方言未知时返回空串。
def _build_value_format_query(
    dialect_name: str, quoted_table: str, quoted_column: str
) -> str:
    """Oracle 用 FETCH FIRST，MSSQL 用 TOP，其余用 LIMIT。"""
    name = str(dialect_name or "").strip().lower()
    limit = _VALUE_FORMAT_SAMPLE_ROWS
    if name in {"mssql", "sqlserver"}:
        return f"SELECT TOP {limit} {quoted_column} FROM {quoted_table}"
    if name == "oracle":
        return (
            f"SELECT {quoted_column} FROM {quoted_table} "
            f"FETCH FIRST {limit} ROWS ONLY"
        )
    if name in {"mysql", "postgres", "clickhouse", "sqlite"}:
        return f"SELECT {quoted_column} FROM {quoted_table} LIMIT {limit}"
    return ""


# 方法作用：获取内省可用的 SQLAlchemy 方言对象，兼容延迟创建 Engine 的连接器。
# Args: ds - 数据源配置或兼容对象。
# Returns: 方言对象；不可得时返回 None。
def _introspection_dialect(ds: Any) -> Any:
    """与枚举采样的取方言方式保持一致：engine 优先，其次连接器内部 engine。"""
    engine = getattr(ds, "engine", None)
    if engine is None:
        connector = getattr(ds, "connector", None)
        engine = getattr(connector, "engine", None) or getattr(connector, "_engine", None)
    return getattr(engine, "dialect", None)


# 方法作用：按方言安全引用表名/列名，非法标识符抛 ValueError。
# Args: dialect - SQLAlchemy 方言；identifier - 表名或列名。
# Returns: 已引用标识符。
def _quote_sampling_identifier(dialect: Any, identifier: str) -> str:
    """标识符来自数据库元数据，仍按 SQLAlchemy 规则引用以防注入。"""
    name = str(identifier or "")
    if not name or "\x00" in name:
        raise ValueError("SQL 标识符无效")
    return dialect.identifier_preparer.quote_identifier(name)


# 方法作用：从执行器返回的单行（Mapping 或序列）中取第一个值。
# Args: row - 执行器返回的一行。
# Returns: 第一个值；结构异常时返回 None。
def _first_row_value(row: Any) -> Any:
    """采样查询只 SELECT 一列，取首个值即可。"""
    if isinstance(row, Mapping):
        return next(iter(row.values()), None)
    if isinstance(row, (list, tuple)):
        return row[0] if row else None
    # sqlalchemy Row 等序列对象：按索引取值，结构异常时记录堆栈后降级
    try:
        return row[0]
    except Exception as exc:
        logger.debug("采样行结构异常", error=str(exc), exc_info=True)
        return None


# 方法作用：采样候选列的值并识别其文本格式。
# Args: ds - 数据源配置；table_name - 表名；columns - 已内省的列信息；executor - 异步 SQL 执行器。
# Returns: {列名: 格式标签}；无候选、方言不可得或查询失败时返回空字典。
async def sample_value_formats(
    ds: DataSourceConfig,
    table_name: str,
    columns: list[ColumnInfo],
    executor,
) -> dict[str, str]:
    """逐候选列采样少量值；任何失败都降级为不标注，绝不阻塞内省主流程。"""
    logger.debug("列值格式采样入口", table=table_name, column_count=len(columns))
    candidates = [c for c in columns if is_date_like_text_column(c.name, c.type)]
    if not candidates:
        return {}
    dialect = _introspection_dialect(ds)
    dialect_name = str(getattr(dialect, "name", "") or "")
    if dialect is None or not _build_value_format_query(dialect_name, "t", "c"):
        logger.warning(
            "列值格式采样跳过", table=table_name, reason="方言不可得或不支持"
        )
        return {}
    formats: dict[str, str] = {}
    for column in candidates:
        try:
            quoted_table = _quote_sampling_identifier(dialect, table_name)
            quoted_column = _quote_sampling_identifier(dialect, column.name)
        except ValueError:
            logger.warning(
                "列值格式采样跳过",
                table=table_name,
                column=column.name,
                reason="标识符非法",
            )
            continue
        sql = _build_value_format_query(dialect_name, quoted_table, quoted_column)
        try:
            rows = await executor(ds, sql, {})
        except Exception as exc:
            # 采样是增强信息，任何失败都不能阻塞内省：记 warning 后继续下一列
            logger.warning(
                "列值格式采样查询失败",
                table=table_name,
                column=column.name,
                error=str(exc)[:200],
            )
            continue
        samples = [_first_row_value(row) for row in rows or []]
        label = detect_value_format(samples)
        if label:
            formats[column.name] = label
    if formats:
        logger.info("列值格式识别完成", table=table_name, formats=formats)
    return formats


async def introspect_columns(
    ds: DataSourceConfig, table_name: str, executor
) -> list[ColumnInfo]:
    """查询指定表的所有字段。"""
    sql = COLUMNS_QUERY.get(ds.dialect)
    if not sql:
        logger.warning("不支持的方言", dialect=ds.dialect)
        return []
    if ds.dialect == "sqlite":
        sql = sql.format(table=_quote_sqlite_identifier(table_name))
    try:
        result = await executor(ds, sql, {
            "table": table_name, "table_name": table_name,
            "database": ds.database, "schema": resolve_namespace(ds),
        })
    except Exception as exc:
        if is_metadata_permission_error(exc):
            raise _permission_error(ds, "columns", exc) from exc
        raise
    columns = []
    for row in result:
        sqlite = ds.dialect == "sqlite"
        columns.append(ColumnInfo(
            name=row.get("name", ""),
            type=row.get("type", ""),
            comment=row.get("comment") or row.get("column_comment") or "",
            is_nullable=(not bool(row.get("notnull", 0))) if sqlite
                        else _parse_nullable(row.get("is_nullable", True)),
            is_primary_key=bool(row.get("pk", 0)) if sqlite
                           else row.get("column_key") == "PRI" or row.get("is_primary_key", False),
            is_indexed=row.get("column_key", "") in ("PRI", "UNI", "MUL"),
        ))
    # 采样识别文本日期列的值格式：让 schema 提示能标注「值格式 YYYY-MM-DD」，
    # 避免模型用错 TO_DATE 格式串（ORA-01830/01840）。采样失败只降级不阻塞内省。
    try:
        formats = await sample_value_formats(ds, table_name, columns, executor)
    except Exception as exc:
        logger.warning(
            "列值格式采样异常降级", table=table_name, error=str(exc)[:200]
        )
        formats = {}
    for column in columns:
        if fmt := formats.get(column.name):
            column.value_format = fmt
    return columns


async def introspect_foreign_keys(
    ds: DataSourceConfig, table_name: str, executor
) -> list[TableRelation]:
    """查询指定表的外键关系。"""
    sql = FK_QUERY.get(ds.dialect)
    if not sql:
        return []
    if ds.dialect == "sqlite":
        sql = sql.format(table=_quote_sqlite_identifier(table_name))
    try:
        result = await executor(ds, sql, {
            "table": table_name, "table_name": table_name,
            "database": ds.database, "schema": resolve_namespace(ds),
        })
    except Exception as exc:
        if is_metadata_permission_error(exc):
            raise _permission_error(ds, "foreign_keys", exc) from exc
        raise
    return [
        TableRelation(
            target_table=row.get("target_table", "") or row.get("table", ""),
            join_key=row.get("column_name", "") or row.get("from", ""),
            relation_type="many_to_one",
        )
        for row in result
    ]


# 方法作用：使用方言元数据查询估算表行数，失败时回退为零。
# Args: ds - 数据源配置；table_name - 表名；executor - 异步 SQL 执行器。
# Returns: 非负行数估算值。
async def estimate_row_count(
    ds: DataSourceConfig, table_name: str, executor
) -> int:
    """估算表行数。"""
    sql = ROW_COUNT_QUERY.get(ds.dialect)
    if not sql:
        return 0
    if ds.dialect == "sqlite":
        sql = sql.format(table=_quote_sqlite_identifier(table_name))
    elif ds.dialect == "clickhouse":
        sql = sql.format(table=_quote_clickhouse_identifier(table_name))
    try:
        result = await executor(ds, sql, {
            "table": table_name, "table_name": table_name,
            "database": ds.database, "schema": resolve_namespace(ds),
        })
        if result:
            count = result[0].get("count", 0) or result[0].get("table_rows", 0) or 0
            return int(count)
    except Exception as exc:
        if is_metadata_permission_error(exc):
            raise _permission_error(ds, "row_count", exc) from exc
        logger.warning(
            "行数估算失败，回退为零",
            table=table_name,
            dialect=ds.dialect,
            error=str(exc),
            exc_info=True,
        )
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


# 内置数据库走原有元数据契约，扩展数据库走插件声明的异步内省。
# Args: ds - 数据源配置；executor - 异步执行器；table_names - 可选表过滤。
# Returns: 已验证的表结构快照。
async def introspect_database(
    ds: DataSourceConfig, executor, table_names: list[str] | None = None, *, strict: bool = False
) -> SchemaSnapshot:
    """内省整个数据库。"""
    from src.connectors.plugins import require_plugin
    from src.connectors.registry import create_connector
    require_plugin(ds.dialect)
    if ds.dialect not in {"clickhouse", "mysql", "postgres", "oracle", "mssql", "sqlite"}:
        connector = ds.connector or create_connector(ds)
        if ds.engine is not None:
            connector.attach_engine(ds.engine)
        logger.info("插件表结构内省入口", datasource=ds.name, dialect=ds.dialect)
        snapshot = await connector.introspect_schema(executor=executor, table_names=table_names)
        if not isinstance(snapshot, SchemaSnapshot):
            raise ValueError("插件内省必须返回 SchemaSnapshot")
        return snapshot
    if table_names is None:
        table_names = await _list_tables(ds, executor)
    tables = []
    for name in table_names:
        try:
            tables.append(await introspect_table(ds, name, executor))
        except MetadataPermissionError:
            raise
        except Exception as e:
            if strict:
                raise
            logger.warning("表内省失败", table=name, error=str(e))
    return SchemaSnapshot(tables=tables)


# 方法作用：按明确支持的数据库方言列出当前库中的业务表。
# Args: ds - 数据源配置；executor - 异步 SQL 执行器。
# Returns: 表名列表；未知方言抛出 ValueError。
async def _list_tables(ds: DataSourceConfig, executor) -> list[str]:
    """列出所有表。"""
    if ds.dialect not in TABLE_NAME_QUERY:
        logger.error("列出数据表失败", dialect=ds.dialect, reason="未知方言")
        raise ValueError(f"不支持的数据源方言: {ds.dialect}")
    sql = TABLE_NAME_QUERY[ds.dialect]
    try:
        result = await executor(ds, sql, {
            "database": ds.database, "schema": resolve_namespace(ds),
        })
    except Exception as exc:
        if is_metadata_permission_error(exc):
            raise _permission_error(ds, "tables", exc) from exc
        raise
    return [r["name"] for r in result]
