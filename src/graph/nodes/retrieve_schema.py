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
                if ds:
                    # 尝试从 Registry 获取已缓存的 schema
                    if ds.schema:
                        schema = ds.schema
                        logger.info("Schema 从 Registry 缓存获取", tables=len(schema.tables))
                    else:
                        # 缓存无数据 → 直接内省
                        from src.datasource.introspection import introspect_database
                        import sqlalchemy as sa

                        async def _exec(ds_cfg, sql, params):
                            async with ds_cfg.engine.connect() as c:
                                r = await c.execute(sa.text(sql), params)
                                return [dict(row._mapping) for row in r]
                        ds.schema = await introspect_database(ds, _exec)
                        schema = ds.schema
                        logger.info("Schema 从实时内省获取", tables=len(schema.tables))
            except Exception:
                pass

    # 从 Registry 获取当前数据源的方言
    # 注意：不信任 state 中缓存的旧 dialect（切换数据源时可能过时）
    dialect = ""
    try:
        from src.datasource.registry import get_registry
        ds = await get_registry().resolve_or_none(datasource_name)
        if ds:
            dialect = ds.dialect
    except Exception:
        pass
    if not dialect:
        dialect = state.get("dialect", "") or "clickhouse"

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

    # 知识库加载策略：只要可能走 SQL 生成就加载，只有闲聊跳过
    #   - 非 chat 意图 → 加载（表结构/业务规则/函数手册来自知识库）
    #   - retry > 0 → 加载（SQL 执行失败，参考知识库修正）
    #   - chat 意图且非 retry → 跳过（无需 SQL，省延迟）
    intent = state.get("intent", "")
    retry_count = state.get("retry_count", 0)
    skip_intents = {"chat"}

    should_load = intent not in skip_intents or retry_count > 0
    knowledge_text = await _load_knowledge_context(datasource_name, state.get("user_query", "")) if should_load else ""
    if not should_load:
        logger.debug("知识库跳过（chat 意图无需 SQL）", datasource=datasource_name)

    return {
        "dialect": dialect,
        "resolved_schema": schema,
        "relevant_tables": [
            {"name": t.name, "description": t.description,
             "columns": [{"name": c.name, "type": c.type, "comment": c.comment,
                          "is_indexed": getattr(c, "is_indexed", False)}
                         for c in t.columns],
             "indexes": [{"columns": idx.columns, "unique": idx.unique}
                         for idx in (getattr(t, "indexes", []) or [])]}
            for t in tables
        ],
        "few_shot_examples": [],
        "business_rules_text": "",
        "enum_dictionary": await _load_enum_dictionary(datasource_name, tables),
        "long_term_memories_text": knowledge_text,
        "conversation_history": history,
    }


async def _load_enum_dictionary(datasource: str, tables: list) -> dict[str, list[str]]:
    """从知识库加载每列的合法枚举值列表。

    用户上传的知识库文档中可定义: status: paid|refunded|cancelled
    """
    try:
        from src.memory.vector_store import get_vector_store
        store = await get_vector_store()
        results = await store.get_by_filter(
            {"datasource": datasource, "category": "enum_value"}, limit=200)
        enum_dict: dict[str, list[str]] = {}
        for r in results:
            col = r.metadata.get("column_name", "")
            vals = r.metadata.get("values", "")
            if col and vals:
                enum_dict[col] = [v.strip() for v in vals.split("|") if v.strip()]
        if enum_dict:
            logger.info("枚举值字典加载", datasource=datasource, columns=len(enum_dict))
        return enum_dict
    except Exception:
        return {}


async def _load_knowledge_context(datasource: str, query: str) -> str:
    """语义检索知识库 — 用用户查询做向量匹配，返回 Top-3 完整 chunk。

    与 LLM prompt 拼接：
    - 每个 chunk 完整注入（≤1000 字符），不做 80 字符截断
    - 只返回最相关的 Top-3，避免上下文爆炸
    """
    try:
        from src.memory.vector_store import get_vector_store
        store = await get_vector_store()
        total = await store.count()
        if total == 0:
            return ""

        results = await store.search(query if query else datasource, top_k=5)
        relevant = [r for r in results if r.score > 0.3]
        relevant.sort(key=lambda x: x.score, reverse=True)

        top_chunks: list[str] = []
        for r in relevant[:3]:
            chunk = r.content[:1000]
            top_chunks.append(chunk)
            logger.debug("  知识库匹配 [score=%.3f] %s: %s", r.score, r.id[:30], r.content[:80])

        if top_chunks:
            logger.info("知识库语义检索命中", datasource=datasource or "全部",
                        total=total, matched=len(relevant), top=len(top_chunks),
                        query=query[:60] if query else "")
            return "\n\n---\n\n".join(top_chunks)
        logger.info("知识库无匹配结果", datasource=datasource or "全部",
                    total=total, query=query[:60] if query else "")
        return ""
    except Exception as e:
        logger.warning("知识库检索失败", error=str(e))
        return ""
