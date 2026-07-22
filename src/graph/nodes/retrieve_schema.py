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
            except Exception as exc:
                logger.warning(
                    "Schema Registry 回退失败",
                    datasource=datasource_name,
                    error=str(exc),
                    exc_info=True,
                )

    # 从 Registry 获取当前数据源的方言
    # 注意：不信任 state 中缓存的旧 dialect（切换数据源时可能过时）
    dialect = ""
    try:
        from src.datasource.registry import get_registry
        ds = await get_registry().resolve_or_none(datasource_name)
        if ds:
            dialect = ds.dialect
    except Exception as exc:
        logger.warning(
            "数据源方言解析失败，使用状态回退",
            datasource=datasource_name,
            error=str(exc),
            exc_info=True,
        )
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

    enum_dictionary = await _load_enum_dictionary(datasource_name, tables)
    raw_business_rules = list(getattr(schema, "business_rules", []) or []) if schema else []
    business_rules_text = "\n".join(
        str(rule.get("content", "") if isinstance(rule, dict) else rule).strip()
        for rule in raw_business_rules
        if str(rule.get("content", "") if isinstance(rule, dict) else rule).strip()
    )
    few_shot_examples = list(getattr(schema, "sql_templates", []) or []) if schema else []
    result = {
        "dialect": dialect,
        "resolved_schema": schema,
        "relevant_tables": [
            {"name": t.name, "description": t.description,
             "columns": [{"name": c.name, "type": c.type, "comment": c.comment,
                          "is_indexed": getattr(c, "is_indexed", False),
                          "is_primary_key": getattr(c, "is_primary_key", False),
                          "is_nullable": getattr(c, "is_nullable", True),
                          "enum_values": getattr(c, "enum_values", []) or
                          enum_dictionary.get(f"{t.name}.{c.name}", []) or
                          enum_dictionary.get(c.name, [])}
                         for c in t.columns],
             "indexes": [{"columns": idx.columns, "unique": idx.unique}
                         for idx in (getattr(t, "indexes", []) or [])],
             "relations": [{
                 "target_table": relation.target_table,
                 "join_key": relation.join_key,
                 "relation_type": relation.relation_type,
             } for relation in (getattr(t, "relations", []) or [])],
             "row_count_estimate": getattr(t, "row_count_estimate", 0),
             "partition_key": getattr(t, "partition_key", "")}
            for t in tables
        ],
        "few_shot_examples": few_shot_examples[:5],
        "business_rules_text": business_rules_text,
        "enum_dictionary": enum_dictionary,
        "long_term_memories_text": knowledge_text,
        "conversation_history": history,
    }
    logger.info(
        "retrieve_schema 状态写回完成",
        datasource=datasource_name,
        table_count=len(result["relevant_tables"]),
        enum_columns=len(enum_dictionary),
        knowledge_chars=len(knowledge_text),
        business_rule_count=len(raw_business_rules),
        few_shot_count=len(few_shot_examples[:5]),
    )
    return result


# 方法作用：从知识向量存储加载数据源字段枚举，并在增强数据故障时降级为空字典。
# Args: datasource - 数据源名称；tables - 当前相关表列表。
# Returns: 表字段到枚举值列表的映射，检索故障时返回空字典。
async def _load_enum_dictionary(datasource: str, tables: list) -> dict[str, list[str]]:
    """从知识库加载每列的合法枚举值列表。

    用户上传的知识库文档中可定义: status: paid|refunded|cancelled
    """
    logger.debug("枚举值字典加载入口", datasource=datasource, table_count=len(tables))
    try:
        from src.memory.vector_store import get_vector_store
        from src.knowledge.retrieval import build_knowledge_filters
        store = await get_vector_store()
        enum_dict: dict[str, list[str]] = {}
        for category in ("column", "enum_value"):
            results = await store.get_by_filter(
                build_knowledge_filters(datasource=datasource, category=category),
                limit=200,
            )
            for result in results:
                metadata = dict(result.metadata or {})
                column = str(metadata.get("column_name", "") or "")
                table = str(metadata.get("table_name", "") or "")
                values = metadata.get("enum_values") or metadata.get("values", "")
                if isinstance(values, str):
                    values = [value.strip() for value in values.split("|") if value.strip()]
                if column and values:
                    enum_dict[f"{table}.{column}" if table else column] = [str(value) for value in values]
        logger.info("枚举值字典加载完成", datasource=datasource, columns=len(enum_dict))
        return enum_dict
    except Exception as exc:
        logger.error(
            "枚举值字典加载失败，降级为空字典",
            datasource=datasource,
            error=str(exc),
            exc_info=True,
        )
        return {}


async def _load_knowledge_context(datasource: str, query: str) -> str:
    """语义检索知识库 — 用用户查询做向量匹配，返回 Top-3 完整 chunk。

    与 LLM prompt 拼接：
    - 每个 chunk 完整注入（≤1000 字符），不做 80 字符截断
    - 只返回最相关的 Top-3，避免上下文爆炸
    """
    try:
        from src.memory.vector_store import get_vector_store
        from src.knowledge.retrieval import search_knowledge
        from src.knowledge.content_safety import render_evidence_context
        store = await get_vector_store()
        total = await store.count()
        if total == 0:
            return ""

        relevant = await search_knowledge(
            store,
            query if query else datasource,
            datasource=datasource,
            top_k=5,
        )

        top_chunks: list[str] = []
        for evidence in relevant[:3]:
            chunk = render_evidence_context(evidence, max_chars=1000)
            top_chunks.append(chunk)
            logger.debug(
                "知识库匹配",
                score=evidence.scores.get("relevance", 0.0),
                source_id=evidence.source_id[:30],
                preview=evidence.content[:80],
                lexical_score=evidence.scores.get("lexical", 0.0),
            )

        if top_chunks:
            logger.info("知识库语义检索命中", datasource=datasource or "全部",
                        total=total, matched=len(relevant), top=len(top_chunks),
                        query=query[:60] if query else "")
            return "\n\n---\n\n".join(top_chunks)
        logger.info("知识库无匹配结果", datasource=datasource or "全部",
                    total=total, query=query[:60] if query else "")
        return ""
    except Exception as e:
        logger.error("知识库检索失败", error=str(e), exc_info=True)
        return ""
