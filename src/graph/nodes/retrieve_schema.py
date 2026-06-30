"""4.3 retrieve_schema Node — 通过 SchemaManager 三级回退获取表结构。"""

from __future__ import annotations

import time

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


async def retrieve_schema_node(state: AnalysisState) -> dict:
    _start = time.monotonic()
    logger.info("节点开始", node="retrieve_schema")
    datasource_name = state.get("datasource", "")
    schema = state.get("resolved_schema")

    if schema is None:
        try:
            from src.knowledge.schema_manager import get_schema_manager
            manager = get_schema_manager()
            schema = await manager.get_or_fetch_schema(
                datasource_name,
                user_query=state.get("user_query", ""),
            )
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

    # 多轮对话：优先从 messages 复原（保证持久化），回退到 conversation_history
    history = list(state.get("conversation_history", []) or [])
    if not history:
        msgs = state.get("messages", []) or []
        for msg in msgs:
            if hasattr(msg, 'content') and msg.content:
                role = 'user' if msg.__class__.__name__ == 'HumanMessage' else 'assistant'
                history.append({
                    "turn_id": len(history) + 1,
                    "user_query": msg.content if role == 'user' else '',
                    "generated_sql": '',
                    "execution_success": True,
                    "chart_type": '',
                    "analysis_summary": msg.content if role == 'assistant' else '',
                })
        if history:
            logger.info("对话历史从 messages 复原", turns=len(history))

    logger.info("节点完成", node="retrieve_schema", elapsed_ms=round((time.monotonic() - _start) * 1000))
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
        "long_term_memories_text": await _load_knowledge_context(datasource_name, state.get("user_query", "")),
        "conversation_history": history,
    }


async def _load_knowledge_context(datasource: str, query: str) -> str:
    """检索相关知识库条目文本（用于前端展示）。"""
    try:
        from src.knowledge.schema_manager import SchemaManager
        from src.knowledge.schema_manager import get_schema_manager
        sm = get_schema_manager()
        sm._ensure_initialized()  # noqa: SLF001
        results = sm._collection.get(  # noqa: SLF001
            where={"datasource": datasource} if datasource else None,
        )
        if results and results.get("documents"):
            # 只要前 3 条的简短摘要
            summaries = [d[:80] for d in results["documents"][:3] if d]
            return "; ".join(summaries) if summaries else ""
        return ""
    except Exception:
        return ""
