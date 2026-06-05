"""4.3 retrieve_schema Node — 表结构和业务规则检索。"""

from __future__ import annotations

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


async def retrieve_schema_node(state: AnalysisState) -> dict:
    """从 state 或 Registry 中获取 Schema。"""
    schema = state.get("resolved_schema")

    # Phase 1: 如果 state 中没有 schema，尝试从 Registry 获取
    if schema is None:
        try:
            from src.datasource.registry import get_registry
            ds = await get_registry().resolve_or_none(state.get("datasource", ""))
            if ds and ds.schema:
                schema = ds.schema
                logger.info("Schema 从 Registry 获取", tables=len(schema.tables) if schema else 0)
        except Exception:
            pass

    tables = schema.tables if schema else []
    logger.info("Schema 检索完成", table_count=len(tables),
                table_names=[t.name for t in tables] if tables else [])

    return {
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
