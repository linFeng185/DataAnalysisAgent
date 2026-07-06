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
             "columns": [{"name": c.name, "type": c.type, "comment": c.comment} for c in t.columns]}
            for t in tables
        ],
        "few_shot_examples": [],
        "business_rules_text": "",
        "long_term_memories_text": knowledge_text,
        "conversation_history": history,
    }


async def _load_knowledge_context(datasource: str, query: str) -> str:
    """语义检索知识库 — 用用户查询做向量匹配，返回 Top-3 完整 chunk。

    与 LLM prompt 拼接：
    - 每个 chunk 完整注入（≤1000 字符），不做 80 字符截断
    - 只返回最相关的 Top-3，避免上下文爆炸
    """
    try:
        from src.knowledge.schema_manager import get_schema_manager
        sm = get_schema_manager()
        sm._ensure_initialized()  # noqa: SLF001
        total = sm._collection.count()
        if total == 0:
            return ""

        # 语义向量搜索：用用户问题匹配最相关的知识库片段
        n = min(5, total)
        results = sm._collection.query(  # noqa: SLF001
            query_texts=[query if query else datasource],
            n_results=n,
        )
        ids_list = results.get("ids", [[]])[0]
        docs_list = results.get("documents", [[]])[0]
        dists_list = results.get("distances", [[]])[0]

        # 过滤低相关性（距离 > 0.7 的丢弃）
        relevant: list[tuple[str, str, float]] = []
        for i in range(len(ids_list)):
            doc = docs_list[i] if i < len(docs_list) else ""
            dist = dists_list[i] if i < len(dists_list) else 1.0
            if doc and dist < 0.7:
                relevant.append((ids_list[i], doc, dist))
        relevant.sort(key=lambda x: x[2])  # 距离升序 = 相关度降序

        # Top-3 完整注入，每块截断到 1000 字符
        top_chunks: list[str] = []
        for doc_id, doc, dist in relevant[:3]:
            chunk = doc[:1000]
            top_chunks.append(chunk)
            logger.debug("  知识库匹配 [dist=%.3f] %s: %s", dist, doc_id[:30], doc[:80])

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
