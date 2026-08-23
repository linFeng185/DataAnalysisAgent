"""
枚举值发现 — 对低基数列自动采样唯一值。

安全约束（SPEC 14.2.1）:
  - 仅采样行数 < 1000 万的表
  - SELECT DISTINCT 超时 5 秒
  - 最多采样 50 个值
  - 唯一值 <= 20 判定为可能枚举
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from src.knowledge.models import KnowledgeEntry, KnowledgeSource
from src.logging_config import get_logger

logger = get_logger(__name__)

# 安全阈值
MAX_ROWS_FOR_SAMPLING = 10_000_000  # 不采样行数超过 1000 万的表
MAX_DISTINCT_VALUES = 50            # 最多返回 50 个唯一值
MAX_EXECUTION_SECONDS = 5           # 超时 5 秒
LOW_CARDINALITY_THRESHOLD = 20      # 唯一值 <= 20 即判定为枚举候选


def _quote_qualified_identifier(dialect, identifier: str) -> str:
    """按 SQLAlchemy 方言安全引用可限定的 SQL 标识符。

    Args:
        dialect: SQLAlchemy 数据库方言。
        identifier: 表名或列名，可包含 schema 前缀。

    Returns:
        各层级均已安全引用的标识符。
    """
    logger.debug("引用枚举采样标识符入口", identifier_length=len(identifier))
    parts = identifier.split(".")
    if not parts or any(not part or "\x00" in part for part in parts):
        logger.error("引用枚举采样标识符失败", error="标识符层级为空或包含 NUL")
        raise ValueError("SQL 标识符无效")
    preparer = dialect.identifier_preparer
    result = ".".join(preparer.quote_identifier(part) for part in parts)
    logger.info("引用枚举采样标识符完成", part_count=len(parts))
    return result


# 方法作用：在授权数据源中安全采样低基数列的枚举值并生成知识条目。
# Args: ds_name - 数据源名称；table - 表名；column - 列名；max_distinct - 最大采样数；tenant_id - 可选租户 ID。
# Returns: 枚举知识条目；数据源不可用、超限或查询失败时返回 None。
async def auto_discover_enum_values(
    ds_name: str,
    table: str,
    column: str,
    max_distinct: int = MAX_DISTINCT_VALUES,
    *,
    tenant_id: int | None = None,
) -> KnowledgeEntry | None:
    """
    对指定列采样唯一值，返回 KnowledgeEntry 或 None。

    不适用于:
      - 表行数 > 1000 万（全表扫描代价太大）
      - 唯一值过多（不是枚举）
    """
    try:
        from src.datasource.registry import get_registry

        registry = get_registry()
        ds = (
            await registry.resolve(ds_name, tenant_id=tenant_id)
            if tenant_id is not None
            else await registry.resolve(ds_name)
        )
        if ds is None or (
            getattr(ds, "engine", None) is None
            and getattr(ds, "connector", None) is None
        ):
            return None

        # 检查行数估值
        row_estimate = _get_row_estimate(ds, table)
        if row_estimate is not None and row_estimate > MAX_ROWS_FOR_SAMPLING:
            logger.info("跳过枚举采样：表行数过大", table=table, rows=row_estimate)
            return None

        # 采样唯一值
        values = await _sample_distinct(ds, table, column, max_distinct)
        if values is None:
            return None

        if not is_low_cardinality_candidate(values):
            logger.info("跳过枚举采样：值过多", column=column, unique_count=len(values))
            return None

        now = datetime.now(timezone.utc)
        entry = KnowledgeEntry(
            id=f"enum:{ds_name}.{table}.{column}",
            content=f"{table}.{column} 的取值: {', '.join(values)}",
            source=KnowledgeSource.AUTO_INTROSPECT,
            category="column",
            table_name=table,
            column_name=column,
            created_at=now,
            ttl=86400,  # 1 天
            metadata={"enum_values": values},
        )
        logger.info("枚举值发现完成", column=entry.id, values_count=len(values))
        return entry
    except Exception as e:
        logger.error(
            "枚举值采样失败",
            table=table,
            column=column,
            error=str(e),
            exc_info=True,
        )
        return None


def is_low_cardinality_candidate(
    distinct_values: list[str], total_rows: int | None = None
) -> bool:
    """判断是否可能是枚举类型。"""
    return len(distinct_values) <= LOW_CARDINALITY_THRESHOLD


# 方法作用：根据数据库方言生成带参数上限的 DISTINCT 采样 SQL。
# Args: dialect - SQLAlchemy 方言对象；quoted_table - 已引用表名；quoted_column - 已引用列名。
# Returns: 不含用户可控 SQL 片段的采样语句模板。
def _build_sample_query(dialect: Any, quoted_table: str, quoted_column: str) -> str:
    logger.debug(
        "构造枚举采样 SQL 入口",
        dialect=str(getattr(dialect, "name", "")),
    )
    dialect_name = str(getattr(dialect, "name", "") or "").strip().lower()
    if dialect_name in {"mssql", "sqlserver"}:
        result = (
            f"SELECT DISTINCT TOP (:sample_limit) {quoted_column} "
            f"FROM {quoted_table}"
        )
    elif dialect_name == "oracle":
        result = (
            f"SELECT DISTINCT {quoted_column} FROM {quoted_table} "
            "FETCH FIRST :sample_limit ROWS ONLY"
        )
    else:
        result = f"SELECT DISTINCT {quoted_column} FROM {quoted_table} LIMIT :sample_limit"
    logger.info(
        "构造枚举采样 SQL 完成",
        dialect=dialect_name,
        uses_limit=" LIMIT " in result.upper(),
    )
    return result


# 方法作用：获取枚举采样所需的 SQLAlchemy 方言，兼容延迟创建 Engine 的连接器。
# Args: ds - 已解析的数据源配置或兼容对象。
# Returns: 可用于标识符引用和采样语法判断的 SQLAlchemy 方言对象。
def _sampling_dialect(ds: Any) -> Any:
    engine = getattr(ds, "engine", None)
    dialect = getattr(engine, "dialect", None)
    if dialect is not None:
        return dialect

    connector = getattr(ds, "connector", None)
    candidates = [
        getattr(ds, "dialect", None),
        getattr(connector, "dialect", None),
        getattr(getattr(connector, "config", None), "dialect", None),
    ]
    dialect_name = next(
        (str(candidate).strip().lower() for candidate in candidates if candidate),
        "",
    )
    aliases = {
        "postgres": "postgresql",
        "sqlserver": "mssql",
    }
    normalized_name = aliases.get(dialect_name, dialect_name)
    if normalized_name:
        try:
            from sqlalchemy.dialects import registry

            dialect_class = registry.load(normalized_name)
            resolved = dialect_class()
            logger.info(
                "从配置解析枚举采样方言完成",
                dialect=normalized_name,
                source="connector_or_config",
            )
            return resolved
        except Exception as exc:
            logger.warning(
                "加载枚举采样方言失败，使用通用方言",
                dialect=normalized_name,
                error=str(exc),
            )

    from sqlalchemy.engine.default import DefaultDialect

    logger.warning("枚举采样缺少方言，使用通用标识符引用", datasource=getattr(ds, "name", ""))
    return DefaultDialect()


# 方法作用：从数据库结果行中提取第一列并兼容 Row、Mapping 和元组。
# Args: row - SQLAlchemy 或连接器返回的单行对象。
# Returns: 第一列值；空行或无法读取时返回 None。
def _first_row_value(row: Any) -> Any:
    logger.debug("读取枚举采样结果行入口", row_type=type(row).__name__)
    if row is None:
        return None
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        result = next(iter(mapping.values()), None)
    elif isinstance(row, Mapping):
        result = next(iter(row.values()), None)
    else:
        try:
            result = row[0]
        except (IndexError, KeyError, TypeError):
            result = None
    logger.debug("读取枚举采样结果行完成", has_value=result is not None)
    return result


# 方法作用：执行一次受方言和时间预算约束的采样查询。
# Args: ds - 已解析数据源；statement_text - 已生成 SQL；params - 绑定参数。
# Returns: 原始结果行列表。
async def _execute_sample_query(
    ds: Any,
    statement_text: str,
    params: dict[str, int],
) -> list[Any]:
    logger.debug(
        "执行枚举采样查询入口",
        datasource=getattr(ds, "name", ""),
        sql_chars=len(statement_text),
    )
    connector = getattr(ds, "connector", None)
    bounded_execute = getattr(connector, "execute_bounded", None)
    if callable(bounded_execute):
        raw_result = bounded_execute(
            statement_text,
            params["sample_limit"],
            params,
        )
        result = await raw_result if inspect.isawaitable(raw_result) else raw_result
        rows = result[0] if isinstance(result, tuple) and len(result) == 2 else result
        logger.info("连接器执行枚举采样完成", row_count=len(rows or []))
        return list(rows or [])

    engine = getattr(ds, "engine", None)
    if engine is None:
        raise RuntimeError("数据源缺少可用执行引擎")
    import sqlalchemy as sa

    # 连接对象的上下文协议区分 SQLAlchemy AsyncEngine 与同步 Engine。
    connection = engine.connect()
    if hasattr(connection, "__aenter__") and hasattr(connection, "__aexit__"):
        async with connection as conn:
            raw_result = conn.execute(sa.text(statement_text), params)
            result = await raw_result if inspect.isawaitable(raw_result) else raw_result
            rows = result.fetchall()
            rows = await rows if inspect.isawaitable(rows) else rows
            logger.info("异步引擎执行枚举采样完成", row_count=len(rows))
            return list(rows)

    def _run_sync() -> list[Any]:
        with connection as conn:
            result = conn.execute(sa.text(statement_text), params)
            rows = result.fetchall()
            return list(rows)

    rows = await asyncio.to_thread(_run_sync)
    logger.info("同步引擎执行枚举采样完成", row_count=len(rows))
    return rows


# 方法作用：执行受限的 DISTINCT 查询并转换为去空字符串值。
# Args: ds - 已解析数据源；table - 表名；column - 列名；limit - 采样上限。
# Returns: 非空唯一值列表；查询失败或超时返回 None。
async def _sample_distinct(ds, table: str, column: str, limit: int) -> list[str] | None:
    """执行安全引用标识符的 SELECT DISTINCT 采样。

    Args:
        ds: 已解析的数据源。
        table: 待采样表名。
        column: 待采样列名。
        limit: 最大返回值数量。

    Returns:
        非空唯一值列表；失败时返回 None。
    """
    logger.debug("枚举 DISTINCT 查询入口", table=table, column=column, limit=limit)
    try:
        safe_limit = max(1, min(int(limit), MAX_DISTINCT_VALUES))
        dialect = _sampling_dialect(ds)
        quoted_table = _quote_qualified_identifier(dialect, table)
        quoted_column = _quote_qualified_identifier(dialect, column)
        statement_text = _build_sample_query(
            dialect,
            quoted_table,
            quoted_column,
        )
        rows = await asyncio.wait_for(
            _execute_sample_query(
                ds,
                statement_text,
                {"sample_limit": safe_limit},
            ),
            timeout=max(0.001, float(MAX_EXECUTION_SECONDS)),
        )
        values = [
            str(value)
            for value in (_first_row_value(row) for row in rows)
            if value is not None
        ]
        logger.info("枚举 DISTINCT 查询完成", value_count=len(values))
        return values
    except TimeoutError:
        logger.warning(
            "枚举 DISTINCT 查询超时",
            table=table,
            column=column,
            timeout_seconds=MAX_EXECUTION_SECONDS,
        )
        return None
    except Exception as exc:
        logger.error(
            "枚举 DISTINCT 查询失败",
            table=table,
            column=column,
            error=str(exc),
            exc_info=True,
        )
        return None


def _get_row_estimate(ds, table: str) -> int | None:
    """获取表的行数估值。"""
    if ds.schema:
        for t in ds.schema.tables:
            if t.name == table:
                return t.row_count_estimate or None
    return None
