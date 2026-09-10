"""知识库检索边界：统一租户、可见性、数据源过滤和证据转换。"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from src.api.auth import get_current_tenant_id, get_current_user_id
from src.app_context import get_tenant_policy
from src.knowledge.asset_models import Evidence
from src.logging_config import get_logger

logger = get_logger(__name__)


# 构造所有知识检索必须携带的结构化过滤条件。
# Args: datasource - 可选数据库数据源名称；category - 可选知识类别；asset_id - 可选资产 ID。
#       owner_only - 是否限制当前用户；tenant_id - 测试或后台任务显式租户；source - 可选知识来源。
# Returns: VectorStore 兼容的 metadata 精确过滤字典。
def build_knowledge_filters(
    datasource: str = "",
    category: str = "",
    asset_id: str = "",
    owner_only: bool = False,
    tenant_id: int | None = None,
    source: str = "",
) -> dict[str, Any]:
    logger.debug(
        "构造知识检索过滤入口",
        datasource=datasource,
        category=category,
        asset_id=asset_id,
        source=source,
        owner_only=owner_only,
    )
    from src.api.auth import get_current_role

    platform = get_current_role() == "super_admin"
    filters: dict[str, Any] = {"visibility": "system" if platform else "tenant"}
    policy = get_tenant_policy()
    effective_tenant_id = tenant_id if tenant_id is not None else get_current_tenant_id()
    if not platform and policy.tenant_filter(effective_tenant_id, explicit=tenant_id is not None):
        filters["tenant_id"] = effective_tenant_id
        if owner_only:
            filters["owner_user_id"] = get_current_user_id()
    if datasource:
        filters["datasource"] = datasource
    if category:
        filters["category"] = category
    if asset_id:
        filters["asset_id"] = asset_id
    if source:
        filters["source"] = source
    # 每次检索都触发，属于循环内进度，按项目规则只能用 debug
    logger.debug("构造知识检索过滤完成", filter_keys=sorted(filters))
    return filters


# 构造当前身份可访问的系统、租户和个人三组知识过滤条件。
# Args: datasource - 可选数据库数据源名称；category - 可选知识类别；asset_id - 可选资产 ID；
#       tenant_id - 后台任务显式租户；user_id - 后台任务显式用户；source - 可选知识来源。
# Returns: 三组彼此隔离的 VectorStore metadata 精确过滤字典。
def build_accessible_knowledge_filters(
    datasource: str = "",
    category: str = "",
    asset_id: str = "",
    tenant_id: int | None = None,
    user_id: int | None = None,
    source: str = "",
) -> list[dict[str, Any]]:
    logger.debug(
        "构造可访问知识过滤入口",
        datasource=datasource,
        category=category,
        asset_id=asset_id,
        source=source,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    policy = get_tenant_policy()
    current_tenant_id = tenant_id if tenant_id is not None else get_current_tenant_id()
    current_user_id = user_id if user_id is not None else get_current_user_id()
    shared: dict[str, Any] = {}
    if datasource:
        shared["datasource"] = datasource
    if category:
        shared["category"] = category
    if asset_id:
        shared["asset_id"] = asset_id
    if source:
        shared["source"] = source

    filters: list[dict[str, Any]] = [{"visibility": "system", **shared}]
    from src.api.auth import get_current_role

    if get_current_role() == "super_admin":
        return filters
    tenant_filter: dict[str, Any] = {"visibility": "tenant", **shared}
    private_filter: dict[str, Any] = {"visibility": "private", **shared}
    if policy.tenant_filter(current_tenant_id, explicit=tenant_id is not None):
        tenant_filter["tenant_id"] = current_tenant_id
        private_filter["tenant_id"] = current_tenant_id
    private_filter["owner_user_id"] = current_user_id
    filters.extend([tenant_filter, private_filter])
    # 每次检索都触发，属于循环内进度，按项目规则只能用 debug
    logger.debug(
        "构造可访问知识过滤完成",
        scope_count=len(filters),
        tenant_id=current_tenant_id,
        user_id=current_user_id,
    )
    return filters


# 将 VectorStore 搜索结果转换成带来源定位的 Evidence。
# Args: result - VectorSearchResult；default_version - 缺少元数据时的版本回退值。
# Returns: 可供分析产物和 API 返回的 Evidence。
def vector_result_to_evidence(result: Any, default_version: str = "v1") -> Evidence:
    logger.debug("转换知识检索证据入口", result_id=getattr(result, "id", ""))
    metadata = dict(getattr(result, "metadata", {}) or {})
    locator = metadata.get("locator", metadata.get("locator_json", {}))
    if isinstance(locator, str):
        try:
            locator = json.loads(locator)
        except (TypeError, json.JSONDecodeError):
            locator = {"raw": locator}
    if not isinstance(locator, dict) or not locator:
        locator = {"source_file": metadata.get("source_file", "")}
    evidence = Evidence(
        content=str(getattr(result, "content", "") or ""),
        source_id=str(getattr(result, "id", "") or ""),
        version=str(metadata.get("document_version", default_version) or default_version),
        locator=locator,
        scores={"relevance": float(getattr(result, "score", 0.0) or 0.0)},
        metadata=metadata,
    )
    logger.debug(
        "转换知识检索证据完成",
        source_id=evidence.source_id,
        version=evidence.version,
    )
    return evidence


# 执行带强制范围过滤的语义检索并去重排序。
# Args: store - VectorStore 实例；query - 用户检索文本；datasource - 数据源名称。
#       category - 可选知识类别；asset_id - 可选资产 ID；top_k - 返回数量；source - 可选知识来源。
# Returns: 按相关性排序的 Evidence 列表。
async def search_knowledge(
    store: Any,
    query: str,
    datasource: str = "",
    category: str = "",
    asset_id: str = "",
    top_k: int = 5,
    hybrid: bool = True,
    source: str = "",
) -> list[Evidence]:
    logger.debug(
        "安全知识检索入口",
        query=query[:80],
        datasource=datasource,
        category=category,
        source=source,
        top_k=top_k,
    )
    if not query.strip():
        logger.info("安全知识检索跳过", reason="查询为空")
        return []
    filter_groups = build_accessible_knowledge_filters(
        datasource=datasource,
        category=category,
        asset_id=asset_id,
        source=source,
    )
    bounded_top_k = min(max(top_k, 1), 50)
    unique: dict[str, Evidence] = {}
    scope_priority = {"system": 1, "private": 2, "tenant": 3}
    for filters in filter_groups:
        try:
            results = await store.search(query, top_k=bounded_top_k, filters=filters)
        except Exception as exc:
            from src.failure_policy import FailureDomain, fallback_allowed

            if not fallback_allowed(FailureDomain.KNOWLEDGE):
                raise
            logger.error(
                "知识库向量分范围召回失败",
                visibility=filters.get("visibility", ""),
                error=str(exc),
                exc_info=True,
            )
            continue
        for result in results:
            evidence = vector_result_to_evidence(result)
            if not evidence.source_id:
                continue
            visibility = str(filters.get("visibility", "tenant"))
            evidence.metadata.setdefault("visibility", visibility)
            logger.debug(
                "知识向量候选边界",
                source_id=evidence.source_id[:40],
                category=str(evidence.metadata.get("category", "") or ""),
                source=str(evidence.metadata.get("source", "") or ""),
                visibility=visibility,
                relevance=evidence.scores.get("relevance", 0.0),
            )
            evidence.scores["vector"] = evidence.scores.get("relevance", 0.0)
            evidence.scores["lexical"] = 0.0
            evidence.scores["scope_priority"] = float(scope_priority.get(visibility, 0))
            evidence.scores["knowledge_priority"] = _knowledge_priority(evidence)
            evidence.scores["fused"] = _fused_score(evidence)
            existing = unique.get(evidence.source_id)
            if (
                existing is None
                or evidence.scores["scope_priority"] > existing.scores.get("scope_priority", 0.0)
                or evidence.scores["fused"] > existing.scores.get("fused", 0.0)
            ):
                unique[evidence.source_id] = evidence

        if hybrid and hasattr(store, "get_by_filter"):
            try:
                lexical_entries = await store.get_by_filter(
                    filters,
                    limit=min(max(bounded_top_k * 20, 60), 1000),
                )
                for entry in lexical_entries:
                    lexical_score = _lexical_score(query, entry.content, entry.metadata)
                    if lexical_score <= 0:
                        continue
                    evidence = vector_result_to_evidence(
                        type("LexicalResult", (), {
                            "id": entry.id,
                            "content": entry.content,
                            "metadata": entry.metadata,
                            "score": lexical_score,
                        })(),
                    )
                    visibility = str(filters.get("visibility", "tenant"))
                    evidence.metadata.setdefault("visibility", visibility)
                    logger.debug(
                        "知识关键词候选边界",
                        source_id=evidence.source_id[:40],
                        category=str(evidence.metadata.get("category", "") or ""),
                        source=str(evidence.metadata.get("source", "") or ""),
                        visibility=visibility,
                        lexical=lexical_score,
                    )
                    priority = float(scope_priority.get(visibility, 0))
                    existing = unique.get(evidence.source_id)
                    if existing:
                        existing_priority = existing.scores.get("scope_priority", 0.0)
                        if priority >= existing_priority:
                            existing.scores["lexical"] = max(
                                lexical_score,
                                existing.scores.get("lexical", 0.0),
                            )
                            existing.scores["scope_priority"] = priority
                            existing.scores["knowledge_priority"] = _knowledge_priority(existing)
                            existing.scores["fused"] = _fused_score(existing)
                    else:
                        evidence.scores["vector"] = 0.0
                        evidence.scores["lexical"] = lexical_score
                        evidence.scores["scope_priority"] = priority
                        evidence.scores["knowledge_priority"] = _knowledge_priority(evidence)
                        evidence.scores["fused"] = _fused_score(evidence)
                        unique[evidence.source_id] = evidence
            except Exception as exc:
                from src.failure_policy import FailureDomain, fallback_allowed

                if not fallback_allowed(FailureDomain.KNOWLEDGE):
                    raise
                logger.error(
                    "知识库关键词分范围召回失败，保留向量结果",
                    visibility=filters.get("visibility", ""),
                    error=str(exc),
                    exc_info=True,
                )
    from src.knowledge.reranker import rerank_evidence
    candidates = sorted(
        unique.values(),
        key=lambda item: item.scores.get("fused", item.scores.get("relevance", 0.0)),
        reverse=True,
    )
    logger.debug("知识证据重排切换线程池", candidates=len(candidates), top_k=bounded_top_k)
    ordered = await asyncio.to_thread(rerank_evidence, candidates, query, bounded_top_k)
    # 每次检索都触发，属于循环内进度，按项目规则只能用 debug
    logger.debug(
        "安全知识检索完成",
        hits=len(ordered),
        scope_count=len(filter_groups),
        hybrid=hybrid,
        category_counts=_count_metadata(ordered, "category"),
        source_counts=_count_metadata(ordered, "source"),
        top_sources=[item.source_id[:40] for item in ordered[:5]],
    )
    return ordered


# 方法作用：按知识类别和来源计算业务知识优先级，避免 Schema 自动枚举淹没业务规则。
# Args: evidence - 已转换的知识证据。
# Returns: 0 到 1 之间的可信业务优先级分数。
def _knowledge_priority(evidence: Evidence) -> float:
    metadata = evidence.metadata or {}
    category = str(metadata.get("category", "") or "").strip().lower()
    source = str(metadata.get("source", "") or "").strip().lower()
    category_scores = {
        "business_rule": 1.0,
        "metric": 0.98,
        "indicator": 0.98,
        "sql_template": 0.86,
        "general": 0.78,
        "table": 0.28,
        "column": 0.14,
        "enum_value": 0.12,
    }
    source_scores = {
        "manual_doc": 1.0,
        "user_upload": 0.96,
        "user_correction": 0.95,
        "orm_model": 0.72,
        "db_comment": 0.66,
        "auto_introspect": 0.18,
        "system_warning": 0.0,
    }
    category_score = category_scores.get(category, 0.55)
    source_score = source_scores.get(source, 0.5)
    return round(category_score * 0.65 + source_score * 0.35, 4)


# 方法作用：把向量、关键词、范围和业务优先级合并为可比较的召回分数。
# Args: evidence - 已写入基础分数的证据。
# Returns: 用于候选排序的融合分数。
def _fused_score(evidence: Evidence) -> float:
    vector = float(evidence.scores.get("vector", evidence.scores.get("relevance", 0.0)) or 0.0)
    lexical = float(evidence.scores.get("lexical", 0.0) or 0.0)
    scope = float(evidence.scores.get("scope_priority", 0.0) or 0.0)
    business_priority = float(evidence.scores.get("knowledge_priority", _knowledge_priority(evidence)) or 0.0)
    return vector * 0.65 + lexical * 0.75 + scope * 0.03 + business_priority * 0.4


# 方法作用：统计结果中来源或类别的数量，供检索诊断日志使用。
# Args: evidence - 检索结果；field_name - metadata 字段名。
# Returns: 字段值到数量的映射。
def _count_metadata(evidence: list[Evidence], field_name: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in evidence:
        value = str(item.metadata.get(field_name, "") or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


# 方法作用：计算查询与知识正文/字段元数据的关键词重叠分数。
# Args: query - 用户查询；content - 知识正文；metadata - 表名、字段名和标签等元数据。
# Returns: 0 到 1 之间的词法相关性分数。
def _lexical_score(query: str, content: str, metadata: dict[str, Any] | None = None) -> float:
    logger.debug("计算知识词法相关性入口", query=query[:60])
    query_tokens = set(_tokenize(query))
    if not query_tokens:
        return 0.0
    body_tokens = set(_tokenize(content))
    meta = metadata or {}
    field_text = " ".join(str(meta.get(key, "")) for key in ("table_name", "column_name", "tags", "asset_id"))
    field_tokens = set(_tokenize(field_text))
    body_overlap = len(query_tokens & body_tokens) / len(query_tokens)
    field_overlap = len(query_tokens & field_tokens) / len(query_tokens)
    result = min(1.0, body_overlap * 0.6 + field_overlap * 0.4)
    logger.debug("计算知识词法相关性完成", score=round(result, 4))
    return result


# 方法作用：切分中英文字段名和查询词，支持专有名词精确匹配。
# Args: text - 待分词文本。
# Returns: 规范化 token 列表。
def _tokenize(text: str) -> list[str]:
    logger.debug("知识查询分词入口", text_size=len(text))
    tokens = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", str(text).lower())
    logger.debug("知识查询分词完成", token_count=len(tokens))
    return tokens
