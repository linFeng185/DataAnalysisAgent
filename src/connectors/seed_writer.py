"""测试数据写入边界，复用项目连接器并默认阻止破坏性操作。"""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from src.connectors.base import ConnectorBase
from src.connectors.registry import create_connector
from src.datasource.config import DataSourceConfig
from src.logging_config import get_logger
from src.testdata.catalog import SOURCES, TableSpec, build_catalog
from src.testdata.generator import GenerationConfig, iter_batches, row_counts
from src.testdata.sql import build_ddl, quote_identifier, schema_comments

logger = get_logger(__name__)


# 验证增量基线与当前生成契约兼容，禁止误用其他种子或规模的旧数据。
# Args: config - 本次生成参数；baseline - 已完成的旧导出清单。
# Returns: 按表名索引的旧表指标。
def validate_baseline(
    config: GenerationConfig, baseline: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """允许调整批次大小，不允许旧表计数、时间窗口或随机种子漂移。"""
    if baseline.get("status") != "complete":
        raise ValueError("增量基线必须是完整数据集")
    original = GenerationConfig(**baseline.get("config", {}))
    if original.model_dump(exclude={"batch_size"}) != config.model_dump(exclude={"batch_size"}):
        raise ValueError("增量基线生成参数不一致")
    catalog = build_catalog()
    counts = row_counts(config)
    indexed = {}
    for summary in baseline.get("tables", []):
        name = summary.get("name")
        if name not in catalog or name in indexed:
            raise ValueError("增量基线存在未知或重复表")
        if summary.get("source") != catalog[name].source or summary.get("rows") != counts[name]:
            raise ValueError(f"增量基线表来源或行数不一致: {name}")
        indexed[name] = summary
    if not indexed:
        raise ValueError("增量基线不能为空")
    return indexed


# 在任何连接或文件副作用前验证所有写入目标。
# Args: targets - 业务域到配置映射；confirmed - 是否明确确认测试库。
# Returns: 无返回值，非法目标抛出异常。
def validate_targets(targets: dict[str, DataSourceConfig], confirmed: bool) -> None:
    """只允许明确命名的专用测试数据库或 Oracle 测试账号。"""
    if not confirmed or not targets or set(targets) - SOURCES.keys():
        raise ValueError("必须指定有效业务源并显式确认测试数据库")
    for source, target in targets.items():
        if target.dialect not in {SOURCES[source][0], "sqlite"}:
            raise ValueError(f"{source} 数据库方言不匹配")
        name = target.username if target.dialect == "oracle" else target.database
        if target.dialect == "sqlite":
            name = Path(name).stem
        if not name.lower().startswith(("test_", "daa_test_")):
            raise ValueError("写入目标必须使用 test_ 或 daa_test_ 前缀；Oracle 使用专用测试用户")
        if target.extra_params.get("schema"):
            raise ValueError("测试数据导入不允许额外切换 schema")


# 在线程中执行同步引擎事务，不通过只读 Connector.execute 提交数据。
# Args: engine - 同步 SQLAlchemy 引擎；statement - SQL；rows - 可选批次参数。
# Returns: SELECT 的标量结果或 None。
def _sync_execute(engine: Any, statement: Any, rows: list[dict[str, Any]] | None) -> Any:
    with engine.begin() as connection:
        result = (
            connection.execute(statement, rows)
            if rows is not None
            else connection.execute(statement)
        )
        return result.scalar_one() if result.returns_rows else None


# 统一异步与同步引擎的提交语义，批次失败由事务回滚。
# Args: engine - SQLAlchemy 引擎；statement - SQL；rows - 可选批次。
# Returns: 查询标量或 None。
async def _execute(engine: Any, statement: Any, rows: list[dict[str, Any]] | None = None) -> Any:
    if isinstance(engine, AsyncEngine):
        async with engine.begin() as connection:
            result = (
                await connection.execute(statement, rows)
                if rows is not None
                else await connection.execute(statement)
            )
            return result.scalar_one() if result.returns_rows else None
    return await asyncio.to_thread(_sync_execute, engine, statement, rows)


# 在当前数据库列出现有表，以便写入任何表之前阻断冲突。
# Args: connector - 已建立引擎的连接器。
# Returns: 当前数据库表名集合。
async def _existing_tables(connector: ConnectorBase) -> set[str]:
    engine = connector.engine
    if connector.config.dialect == "clickhouse":
        result = await asyncio.to_thread(engine.client.query, "SHOW TABLES")
        return {row[0] for row in result.result_rows}
    if isinstance(engine, AsyncEngine):
        async with engine.connect() as connection:
            return set(await connection.run_sync(_inspect_names))
    return set(await asyncio.to_thread(_inspect_names, engine))


# 通过 SQLAlchemy 内省读取已存在表，避免用失败 SQL 探测。
# Args: connection - 引擎或连接。
# Returns: 表名列表。
def _inspect_names(connection: Any) -> list[str]:
    return sa.inspect(connection).get_table_names()


# 提供 StopIteration 不跨 Future 边界的批次获取函数。
# Args: batches - 批次迭代器。
# Returns: 下一批或 None。
def _next_batch(batches: Iterator[list[dict[str, Any]]]) -> list[dict[str, Any]] | None:
    return next(batches, None)


# 构造有类型参数的批量 SQL，使 Decimal 和日期跨驱动保持一致。
# Args: table - 表规格；dialect - 方言。
# Returns: 参数化 INSERT。
def _insert_statement(table: TableSpec, dialect: str) -> Any:
    columns = ", ".join(quote_identifier(column.name, dialect) for column in table.columns)
    parameters = ", ".join(f":{column.name}" for column in table.columns)
    statement = sa.text(
        f"INSERT INTO {quote_identifier(table.name, dialect)} ({columns}) VALUES ({parameters})"
    )
    types = {
        "integer": sa.BigInteger(),
        "decimal": sa.Numeric(18, 2),
        "date": sa.Date(),
        "text": sa.Unicode(256),
    }
    return statement.bindparams(
        *(sa.bindparam(column.name, type_=types[column.kind]) for column in table.columns)
    )


# 创建并分批持久化一张测试表，完成后从数据库核对行数。
# Args: connector - 连接器；table - 目录规格；config - 生成配置。
# Returns: 实际写入行数。
async def _import_table(
    connector: ConnectorBase, table: TableSpec, config: GenerationConfig
) -> int:
    engine = connector.engine
    dialect = connector.config.dialect
    logger.debug("测试表导入入口", table=table.name, dialect=dialect)
    if dialect == "clickhouse":
        await asyncio.to_thread(engine.client.command, build_ddl(table, dialect))
    else:
        ddl = build_ddl(table, dialect)
        if dialect == "oracle":
            # 离线脚本分号不属于 Oracle DBAPI 单条语句，避免 ORA-00922。
            ddl = ddl.rstrip().removesuffix(";")
            logger.info("Oracle 建表驱动边界", table=table.name, script_terminator_removed=True)
        await _execute(engine, sa.text(ddl))
        for comment in schema_comments(table, dialect):
            await _execute(engine, sa.text(comment))
    batches = iter(iter_batches(table, config))
    total = 0
    statement = None if dialect == "clickhouse" else _insert_statement(table, dialect)
    columns = [column.name for column in table.columns]
    while (batch := await asyncio.to_thread(_next_batch, batches)) is not None:
        if dialect == "clickhouse":
            values = [[row[column] for column in columns] for row in batch]
            await asyncio.to_thread(engine.client.insert, table.name, values, column_names=columns)
        else:
            await _execute(engine, statement, batch)
        total += len(batch)
        logger.debug("测试表批次已提交", table=table.name, committed_rows=total)
    query = f"SELECT COUNT(*) FROM {quote_identifier(table.name, dialect)}"
    if dialect == "clickhouse":
        actual = (await asyncio.to_thread(engine.client.query, query)).result_rows[0][0]
    else:
        actual = await _execute(engine, sa.text(query))
    if actual != total:
        raise ValueError(f"{table.name} 导入行数不一致：期望 {total}，实际 {actual}")
    logger.info("测试表导入完成", table=table.name, rows=actual)
    return total


# 预检全部目标，再经项目连接器创建新表并分批导入。
# Args: config - 生成配置；targets - 显式测试目标；confirm_test_database - 必须为真；baseline - 已完成的旧数据清单。
# Returns: 成功导入的表与实际行数清单。
async def import_dataset(
    config: GenerationConfig,
    targets: dict[str, DataSourceConfig],
    confirm_test_database: bool = False,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """不删除现有数据；失败保留已提交批次供审计，禁止盲目重试追加。"""
    validate_targets(targets, confirm_test_database)
    from src.connectors.seed_verifier import verify_database_table

    preserved = validate_baseline(config, baseline) if baseline is not None else {}
    logger.debug("测试数据导入入口", sources=list(targets), scale=config.scale)
    connectors: dict[str, ConnectorBase] = {}
    report: dict[str, Any] = {
        "status": "complete",
        "tables": [],
        "total_rows": 0,
        "preserved_rows": 0,
        "inserted_rows": 0,
    }
    catalog = build_catalog()
    try:
        for source, target in targets.items():
            connector = create_connector(target)
            connectors[source] = connector
            await connector.create_engine()
            existing = {name.lower() for name in await _existing_tables(connector)}
            requested = {name for name, table in catalog.items() if table.source == source}
            old_names = {name for name in preserved if catalog[name].source == source}
            if baseline is not None and not old_names:
                raise ValueError(f"{source} 不在增量基线中")
            if missing := old_names - existing:
                raise ValueError(f"旧表缺失，拒绝扩容: {', '.join(sorted(missing))}")
            if conflict := existing & (requested - old_names):
                raise FileExistsError(
                    f"目标已有测试表，拒绝覆盖或追加: {', '.join(sorted(conflict))}"
                )
            logger.info(
                "增量导入预检边界",
                source=source,
                preserved_tables=len(old_names),
                new_tables=len(requested - old_names),
            )
            for name in sorted(old_names):
                await verify_database_table(connector, catalog[name], preserved[name], config)
        for table in catalog.values():
            if table.source not in connectors:
                continue
            is_preserved = table.name in preserved
            count = (
                preserved[table.name]["rows"]
                if is_preserved
                else await _import_table(connectors[table.source], table, config)
            )
            if count != row_counts(config)[table.name]:
                raise ValueError("生成与导入计数不一致")
            report["tables"].append(
                {
                    "name": table.name,
                    "source": table.source,
                    "rows": count,
                    "action": "preserved" if is_preserved else "inserted",
                }
            )
            report["total_rows"] += count
            report["preserved_rows" if is_preserved else "inserted_rows"] += count
    except Exception:
        logger.error(
            "测试数据导入失败；已提交批次保留，须人工检查后重新选择空测试库", exc_info=True
        )
        raise
    finally:
        for connector in connectors.values():
            await connector.close()
    logger.info("测试数据导入完成", tables=len(report["tables"]), rows=report["total_rows"])
    return report
