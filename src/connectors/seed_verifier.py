"""独立读取测试数据库，核验计数、金额、键空间和确定性中文样本。"""

from decimal import Decimal
from typing import Any

from src.connectors.base import ConnectorBase
from src.connectors.registry import create_connector
from src.datasource.config import DataSourceConfig
from src.logging_config import get_logger
from src.testdata.catalog import TableSpec, build_catalog
from src.testdata.generator import GenerationConfig, make_row, row_counts
from src.testdata.sql import quote_identifier

logger = get_logger(__name__)


# 将数据库日期和精度值归一化，避免驱动表示差异掩盖业务差异。
# Args: value - 数据库值；kind - 目录逻辑类型。
# Returns: 可精确比较的值。
def _normalize(value: Any, kind: str) -> Any:
    if value is None:
        return None
    if kind == "date":
        return str(value)[:10]
    if kind == "decimal":
        return Decimal(str(value)).quantize(Decimal("0.01"))
    return value


# 用聚合查询和边界样本验证一张真实表，不信任导入器的成功标记。
# Args: connector - 已连接的只读查询边界；table - 目录规格；expected - 清单指标；config - 原始生成参数。
# Returns: 核验通过的实际指标；不一致时抛出 ValueError。
async def verify_database_table(
    connector: ConnectorBase,
    table: TableSpec,
    expected: dict[str, Any],
    config: GenerationConfig,
) -> dict[str, Any]:
    """验证全表计数、唯一主键、日期、定点金额、空值、外键范围及中文样本。"""
    dialect = connector.config.dialect
    quoted = {column.name: quote_identifier(column.name, dialect) for column in table.columns}
    name = quote_identifier(table.name, dialect)
    distinct = f"COUNT(DISTINCT {quoted['id']})"
    if dialect == "clickhouse":
        distinct = f"uniqExact({quoted['id']})"
    expressions = {
        "row_count": "COUNT(*)",
        "distinct_ids": distinct,
        "min_id": f"MIN({quoted['id']})",
        "max_id": f"MAX({quoted['id']})",
        "min_date": f"MIN({quoted['event_date']})",
        "max_date": f"MAX({quoted['event_date']})",
    }
    for column in table.columns:
        if column.name in expected.get("sums", {}):
            expressions[f"sum_{column.name}"] = f"SUM({quoted[column.name]})"
        if column.nullable:
            expressions[f"null_{column.name}"] = f"COUNT(*) - COUNT({quoted[column.name]})"
        if column.reference:
            expressions[f"min_{column.name}"] = f"MIN({quoted[column.name]})"
            expressions[f"max_{column.name}"] = f"MAX({quoted[column.name]})"
    selections = ", ".join(
        f"{expression} AS {quote_identifier(alias, dialect)}"
        for alias, expression in expressions.items()
    )
    logger.info(
        "实库核验查询边界",
        table=table.name,
        expected_rows=expected["rows"],
        metrics=len(expressions),
    )
    result = await connector.execute(f"SELECT {selections} FROM {name}")
    actual = {key.lower(): value for key, value in result[0].items()}
    logger.info(
        "实库核验结果边界",
        table=table.name,
        actual_rows=actual["row_count"],
        expected_nulls=expected.get("null_counts", {}),
        actual_nulls={key: value for key, value in actual.items() if key.startswith("null_")},
    )
    count = expected["rows"]
    if (
        any(actual[key] != count for key in ("row_count", "distinct_ids", "max_id"))
        or actual["min_id"] != 1
    ):
        raise ValueError(f"{table.name} 计数或主键不一致: {actual}")
    for key in ("min_date", "max_date"):
        if _normalize(actual[key], "date") != expected[key]:
            raise ValueError(f"{table.name} 日期不一致: {key}")
    counts = row_counts(config)
    for column in table.columns:
        if column.name in expected.get("sums", {}):
            if Decimal(str(actual[f"sum_{column.name}"])) != Decimal(expected["sums"][column.name]):
                raise ValueError(f"{table.name} 金额汇总不一致: {column.name}")
        if column.nullable and actual[f"null_{column.name}"] != expected.get("null_counts", {}).get(
            column.name, 0
        ):
            raise ValueError(f"{table.name} 空值计数不一致: {column.name}")
        if column.reference and not (
            1
            <= actual[f"min_{column.name}"]
            <= actual[f"max_{column.name}"]
            <= counts[column.reference]
        ):
            raise ValueError(f"{table.name} 外键越界: {column.name}")
    sample_ids = sorted({1, count, *(min(sample, count) for sample in (13, 17, 19, 31))})
    samples = await connector.execute(
        f"SELECT * FROM {name} WHERE {quoted['id']} IN ({', '.join(map(str, sample_ids))})"
    )
    if len(samples) != len(sample_ids):
        raise ValueError(f"{table.name} 样本数量不一致")
    for sample in samples:
        normalized = {key.lower(): value for key, value in sample.items()}
        wanted = make_row(table, int(normalized["id"]), config, counts)
        if normalized.keys() != wanted.keys():
            raise ValueError(f"{table.name} 样本列集合不一致")
        for column in table.columns:
            if _normalize(normalized[column.name], column.kind) != _normalize(
                wanted[column.name], column.kind
            ):
                raise ValueError(
                    f"{table.name} 样本内容不一致: id={normalized['id']}, column={column.name}"
                )
    logger.info("实库表核验完成", table=table.name, rows=count, samples=len(samples))
    return {
        "name": table.name,
        "source": table.source,
        "rows": count,
        "metrics": actual,
        "sample_ids": sample_ids,
    }


# 逐源核验完整清单，任何失败都不返回成功报告并关闭全部连接。
# Args: targets - 专用测试数据源；manifest - 已校验的导出清单。
# Returns: 全量实际数据库验收报告。
async def verify_database_dataset(
    targets: dict[str, DataSourceConfig], manifest: dict[str, Any]
) -> dict[str, Any]:
    """只读核验清单声明的所有表，不计入同一数据库内的其他遗留表。"""
    from src.connectors.seed_writer import validate_baseline, validate_targets

    validate_targets(targets, True)
    config = GenerationConfig(**manifest["config"])
    expected_tables = validate_baseline(config, manifest)
    catalog = build_catalog()
    report: dict[str, Any] = {"status": "complete", "tables": [], "sources": {}, "total_rows": 0}
    logger.debug("全量数据库核验入口", sources=list(targets))
    try:
        for source, target in targets.items():
            connector = create_connector(target)
            source_report = {"tables": 0, "rows": 0}
            try:
                await connector.create_engine()
                for name, expected in expected_tables.items():
                    if catalog[name].source != source:
                        continue
                    verified = await verify_database_table(
                        connector, catalog[name], expected, config
                    )
                    report["tables"].append(verified)
                    source_report["tables"] += 1
                    source_report["rows"] += verified["rows"]
            finally:
                await connector.close()
            if not source_report["tables"]:
                raise ValueError(f"{source} 缺少待核验表")
            report["sources"][source] = source_report
            report["total_rows"] += source_report["rows"]
    except Exception:
        logger.error("全量数据库核验失败", exc_info=True)
        raise
    logger.info("全量数据库核验完成", tables=len(report["tables"]), rows=report["total_rows"])
    return report
