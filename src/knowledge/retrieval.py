"""知识库检索边界：统一租户、可见性、数据源过滤和证据转换。"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from src.api.auth import get_current_tenant_id, get_current_user_id
from src.config import get_settings
from src.knowledge.asset_models import Evidence
from src.logging_config import get_logger

logger = get_logger(__name__)


# 构造所有知识检索必须携带的结构化过滤条件。
# Args: datasource - 可选数据库数据源名称；category - 可选知识类别；asset_id - 可选资产 ID。
#       owner_only - 是否限制当前用户；tenant_id - 测试或后台任务显式租户。
# Returns: VectorStore 兼容的 metadata 精确过滤字典。
def build_knowledge_filters(
    datasource: str = "",
    category: str = "",
    asset_id: str = "",
    owner_only: bool = False,
    tenant_id: int | None = None,
) -> dict[str, Any]:
    logger.debug(
        "构造知识检索过滤入口",
        datasource=datasource,
        category=category,
        asset_id=asset_id,
        owner_only=owner_only,
    )
    filters: dict[str, Any] = {"visibility": "tenant"}
    settings = get_settings()
    if settings.multi_tenant:
        filters["tenant_id"] = tenant_id if tenant_id is not None else get_current_tenant_id()
        if owner_only:
            filters["owner_user_id"] = get_current_user_id()
    elif tenant_id is not None:
        filters["tenant_id"] = tenant_id
    if datasource:
        filters["datasource"] = datasource
    if category:
        filters["category"] = category
    if asset_id:
        filters["asset_id"] = asset_id
    logger.info("构造知识检索过滤完成", filter_keys=sorted(filters))
    return filters


# 构造当前身份可访问的系统、租户和个人三组知识过滤条件。
# Args: datasource - 可选数据库数据源名称；category - 可选知识类别；asset_id - 可选资产 ID；
#       tenant_id - 后台任务显式租户；user_id - 后台任务显式用户。
# Returns: 三组彼此隔离的 VectorStore metadata 精确过滤字典。
def build_accessible_knowledge_filters(
    datasource: str = "",
    category: str = "",
    asset_id: str = "",
    tenant_id: int | None = None,
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    logger.debug(
        "构造可访问知识过滤入口",
        datasource=datasource,
        category=category,
        asset_id=asset_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    settings = get_settings()
    current_tenant_id = tenant_id if tenant_id is not None else get_current_tenant_id()
    current_user_id = user_id if user_id is not None else get_current_user_id()
    shared: dict[str, Any] = {}
    if datasource:
        shared["datasource"] = datasource
    if category:
        shared["category"] = category
    if asset_id:
        shared["asset_id"] = asset_id

    filters: list[dict[str, Any]] = [{"visibility": "system", **shared}]
    tenant_filter: dict[str, Any] = {"visibility": "tenant", **shared}
    private_filter: dict[str, Any] = {"visibility": "private", **shared}
    if settings.multi_tenant or tenant_id is not None:
        tenant_filter["tenant_id"] = current_tenant_id
        private_filter["tenant_id"] = current_tenant_id
    private_filter["owner_user_id"] = current_user_id
    filters.extend([tenant_filter, private_filter])
    logger.info(
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
    logger.info(
        "转换知识检索证据完成",
        source_id=evidence.source_id,
        version=evidence.version,
    )
    return evidence


# 执行带强制范围过滤的语义检索并去重排序。
# Args: store - VectorStore 实例；query - 用户检索文本；datasource - 数据源名称。
#       category - 可选知识类别；asset_id - 可选资产 ID；top_k - 返回数量。
# Returns: 按相关性排序的 Evidence 列表。
async def search_knowledge(
    store: Any,
    query: str,
    datasource: str = "",
    category: str = "",
    asset_id: str = "",
    top_k: int = 5,
    hybrid: bool = True,
) -> list[Evidence]:
    logger.debug(
        "安全知识检索入口",
        query=query[:80],
        datasource=datasource,
        category=category,
        top_k=top_k,
    )
    if not query.strip():
        logger.info("安全知识检索跳过", reason="查询为空")
        return []
    filter_groups = build_accessible_knowledge_filters(
        datasource=datasource,
        category=category,
        asset_id=asset_id,
    )
    bounded_top_k = min(max(top_k, 1), 50)
    unique: dict[str, Evidence] = {}
    scope_priority = {"system": 1, "private": 2, "tenant": 3}
    for filters in filter_groups:
        try:
            results = await store.search(query, top_k=bounded_top_k, filters=filters)
        except Exception as exc:
            logger.warning(
                "知识库向量分范围召回失败",
                visibility=filters.get("visibility", ""),
                error=str(exc),
            )
            continue
        for result in results:
            evidence = vector_result_to_evidence(result)
            if not evidence.source_id:
                continue
            visibility = str(filters.get("visibility", "tenant"))
            evidence.metadata.setdefault("visibility", visibility)
            evidence.scores["vector"] = evidence.scores.get("relevance", 0.0)
            evidence.scores["lexical"] = 0.0
            evidence.scores["scope_priority"] = float(scope_priority.get(visibility, 0))
            evidence.scores["fused"] = evidence.scores["vector"] * 0.65
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
                            existing.scores["fused"] = (
                                existing.scores.get("vector", 0.0) * 0.65
                                + existing.scores["lexical"] * 0.75
                            )
                    else:
                        evidence.scores["vector"] = 0.0
                        evidence.scores["lexical"] = lexical_score
                        evidence.scores["scope_priority"] = priority
                        evidence.scores["fused"] = lexical_score * 0.75
                        unique[evidence.source_id] = evidence
            except Exception as exc:
                logger.warning(
                    "知识库关键词分范围召回失败，保留向量结果",
                    visibility=filters.get("visibility", ""),
                    error=str(exc),
                )
    from src.knowledge.reranker import rerank_evidence
    candidates = sorted(
        unique.values(),
        key=lambda item: item.scores.get("fused", item.scores.get("relevance", 0.0)),
        reverse=True,
    )
    logger.debug("知识证据重排切换线程池", candidates=len(candidates), top_k=bounded_top_k)
    ordered = await asyncio.to_thread(rerank_evidence, candidates, query, bounded_top_k)
    logger.info(
        "安全知识检索完成",
        hits=len(ordered),
        scope_count=len(filter_groups),
        hybrid=hybrid,
    )
    return ordered


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
    logger.info("计算知识词法相关性完成", score=round(result, 4))
    return result


# 方法作用：切分中英文字段名和查询词，支持专有名词精确匹配。
# Args: text - 待分词文本。
# Returns: 规范化 token 列表。
def _tokenize(text: str) -> list[str]:
    logger.debug("知识查询分词入口", text_size=len(text))
    tokens = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", str(text).lower())
    logger.info("知识查询分词完成", token_count=len(tokens))
    return tokens
