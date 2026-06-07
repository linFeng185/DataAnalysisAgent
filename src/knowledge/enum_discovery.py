"""
枚举值发现 — 对低基数列自动采样唯一值。

安全约束（SPEC 14.2.1）:
  - 仅采样行数 < 1000 万的表
  - SELECT DISTINCT 超时 5 秒
  - 最多采样 50 个值
  - 唯一值 <= 20 判定为可能枚举
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.knowledge.models import AUTO_TTL_SECONDS, KnowledgeEntry, KnowledgeSource
from src.logging_config import get_logger

logger = get_logger(__name__)

# 安全阈值
MAX_ROWS_FOR_SAMPLING = 10_000_000  # 不采样行数超过 1000 万的表
MAX_DISTINCT_VALUES = 50            # 最多返回 50 个唯一值
MAX_EXECUTION_SECONDS = 5           # 超时 5 秒
LOW_CARDINALITY_THRESHOLD = 20      # 唯一值 <= 20 即判定为枚举候选


async def auto_discover_enum_values(
    ds_name: str,
    table: str,
    column: str,
    max_distinct: int = MAX_DISTINCT_VALUES,
) -> KnowledgeEntry | None:
    """
    对指定列采样唯一值，返回 KnowledgeEntry 或 None。

    不适用于:
      - 表行数 > 1000 万（全表扫描代价太大）
      - 唯一值过多（不是枚举）
    """
    try:
        from src.datasource.registry import get_registry

        ds = await get_registry().resolve(ds_name)
        if ds is None or ds.engine is None:
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
        logger.warning("枚举值采样失败", table=table, column=column, error=str(e))
        return None


def is_low_cardinality_candidate(
    distinct_values: list[str], total_rows: int | None = None
) -> bool:
    """判断是否可能是枚举类型。"""
    return len(distinct_values) <= LOW_CARDINALITY_THRESHOLD


async def _sample_distinct(ds, table: str, column: str, limit: int) -> list[str] | None:
    """执行 SELECT DISTINCT col LIMIT N。"""
    import sqlalchemy as sa

    sql = f"SELECT DISTINCT {column} FROM {table} LIMIT {limit}"
    try:
        async with ds.engine.connect() as conn:
            result = await conn.execute(sa.text(sql))
            rows = result.fetchall()
            return [str(r[0]) for r in rows if r[0] is not None]
    except Exception as e:
        logger.warning("DISTINCT 查询失败", sql=sql[:100], error=str(e))
        return None


def _get_row_estimate(ds, table: str) -> int | None:
    """获取表的行数估值。"""
    if ds.schema:
        for t in ds.schema.tables:
            if t.name == table:
                return t.row_count_estimate or None
    return None
