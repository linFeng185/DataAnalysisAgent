"""4.3 retrieve_schema Node — 通过 SchemaManager 三级回退获取表结构。"""

from __future__ import annotations

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


async def retrieve_schema_node(state: AnalysisState) -> dict:
    datasource_name = state.get("datasource", "")
    schema = state.get("resolved_schema")

    if schema is None:
        try:
            from src.knowledge.schema_manager import get_schema_manager
            manager = get_schema_manager()
            schema = await manager.get_or_fetch_schema(datasource_name)
            tables_count = len(schema.tables) if schema else 0
            logger.info("Schema 检索成功", tables=tables_count)
            if tables_count == 0:
                schema = None
        except Exception as e:
            logger.warning("Schema 检索失败", error=str(e))
            schema = None
        if schema is None:
            try:
                from src.datasource.registry import get_registry
                ds = await get_registry().resolve_or_none(datasource_name)
                if ds and ds.schema:
                    schema = ds.schema
                    logger.info("Schema 从 Registry 回退获取", tables=len(schema.tables))
            except Exception:
                pass

    # 从 Registry 获取数据源方言
    dialect = state.get("dialect", "")
    if not dialect:
        try:
            from src.datasource.registry import get_registry
            ds = await get_registry().resolve_or_none(datasource_name)
            if ds:
                dialect = ds.dialect
        except Exception:
            pass
    if not dialect:
        dialect = "clickhouse"  # 最终兜底

    tables = schema.tables if schema else []
    logger.info("Schema 检索完成", table_count=len(tables),
                table_names=[t.name for t in tables] if tables else [],
                dialect=dialect)

    return {
        "dialect": dialect,
        "resolved_schema": schema,
        "relevant_tables": [
            {"name": t.name, "description": t.description,
             "columns": [{"name": c.name, "type": c.type, "comment": c.comment} for c in t.columns]}
            for t in tables
        ],
        "few_shot_examples": [],
        "business_rules_text": "",
        "long_term_memories_text": "",
    }
