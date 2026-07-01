"""
Schema 缓存管理器 — 三级回退获取表结构。

核心流程:
  get_or_fetch_schema(datasource_name)
    ├─ ① ChromaDB 缓存查询 — 命中且未过期 → 直接返回（毫秒级）
    ├─ ② Markdown 文档加载 — [Phase 1 桩] 返回空
    └─ ③ DB 系统表内省 — introspect_database() → 写入缓存 → 返回

缓存策略:
  - auto_introspect 来源默认 TTL=7 天
  - manual_doc 来源 TTL=0（永不过期）
  - 过期条目自动触发重新内省
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from src.config import get_settings
from src.knowledge.models import (
    AUTO_TTL_SECONDS,
    KnowledgeEntry,
    KnowledgeSource,
    source_priority,
)
from src.logging_config import get_logger

logger = get_logger(__name__)


_schema_singleton = None


def get_schema_manager():
    global _schema_singleton
    if _schema_singleton is None:
        _schema_singleton = SchemaManager()
    return _schema_singleton


class SchemaManager:
    """
    Schema 缓存管理器。

    无论有没有预先准备文档，保证调用者总能拿到完整的表结构信息。
    通过 ChromaDB 缓存避免重复查询 INFORMATION_SCHEMA。
    """

    def __init__(self) -> None:
        self._client = None
        self._collection = None
        self._initialized = False

    # ── 公开接口 ──────────────────────────────────────

    async def get_or_fetch_schema(self, datasource_name: str, user_query: str = ""):
        """
        获取 Schema 的主入口 — 三级回退。

        当 user_query 提供且表数量超过阈值时，自动启用语义搜索 + FK 扩张，
        避免将数千张表全部塞入 LLM prompt。

        返回: SchemaSnapshot（永远不为 None，最差返回空快照）
        """
        self._ensure_initialized()

        # ① 查 ChromaDB 缓存
        cached = self._query_cache(datasource_name)
        if cached and not self._any_expired(cached):
            logger.info("Schema 缓存命中", datasource=datasource_name, entries=len(cached))
            snapshot = self._build_snapshot(cached)
            if user_query:
                snapshot = self._filter_relevant_tables(snapshot, user_query, datasource_name)
            return snapshot

        if cached:
            logger.info("Schema 缓存部分过期，触发内省刷新", datasource=datasource_name)

        # ② 文档加载（Phase 1 桩 — 返回空）
        doc_entries = self._load_from_docs(datasource_name)

        # ③ DB 内省
        logger.info("Schema 缓存未命中，触发 DB 内省", datasource=datasource_name)
        introspected = await self._introspect_from_db(datasource_name)

        # 合并：文档优先于自动内省
        merged = self._merge_entries(doc_entries, introspected)
        if merged:
            self._upsert_to_cache(merged)

        result = merged or cached or []
        snapshot = self._build_snapshot(result)
        if user_query:
            snapshot = self._filter_relevant_tables(snapshot, user_query, datasource_name)
        return snapshot

    # ── 私有：缓存查询 ─────────────────────────────────

    def _query_cache(self, datasource_name: str) -> list[KnowledgeEntry]:
        """从 ChromaDB 缓存中检索指定数据源的所有知识条目。"""
        try:
            results = self._collection.get(
                where={"table_name": {"$ne": ""}},
            )
            entries = []
            ids = results.get("ids", [])
            if not ids:
                return []

            metadatas = results.get("metadatas", [])
            documents = results.get("documents", [])

            for i, entry_id in enumerate(ids):
                if not self._belongs_to_datasource(entry_id, datasource_name):
                    continue
                meta = metadatas[i] if i < len(metadatas) else {}
                content = documents[i] if i < len(documents) else ""
                entries.append(self._row_to_entry(entry_id, content, meta))
            return entries
        except Exception:
            logger.warning("ChromaDB 缓存查询失败，降级到 DB 内省")
            return []

    def _belongs_to_datasource(self, entry_id: str, datasource_name: str) -> bool:
        """检查 entry id 是否属于指定的数据源。"""
        try:
            _, qualified = entry_id.split(":", 1)
            return qualified.startswith(datasource_name + ".")
        except (ValueError, AttributeError):
            return False

    def _any_expired(self, entries: list[KnowledgeEntry]) -> bool:
        """检查条目列表中是否有已过期的。"""
        return any(e.is_expired() for e in entries)

    # ── 私有：文档加载 ──────────────────────────────────

    def _load_from_docs(self, datasource_name: str) -> list[KnowledgeEntry]:
        """
        从 docs/metrics/ 目录加载 Markdown 文档。

        通过 DocLoader 扫描并解析 YAML frontmatter，
        过滤出 tags 或 tables 中包含当前数据源名称的条目。
        """
        try:
            from src.knowledge.doc_loader import DocLoader

            loader = DocLoader()
            all_entries = loader.scan_and_load()
            return [
                e for e in all_entries
                if datasource_name in (e.tags or []) or datasource_name in (e.metadata.get("tables", []) or [])
            ]
        except Exception as e:
            logger.warning("文档加载失败", error=str(e))
            return []


    def _ensure_external_provider(self) -> None:
        """确保外部数据源 Provider 已注册（启动后首次调用时懒加载）。"""
        if getattr(self, "_ext_provider_registered", False):
            return
        try:
            from src.datasource.providers.external import ExternalDataSourceProvider
            from src.datasource.registry import get_registry

            provider = ExternalDataSourceProvider.from_yaml("config/datasources.yaml")
            get_registry().register_provider("external", provider)
            self._ext_provider_registered = True
            logger.info("外部数据源 Provider 已注册")
        except Exception as e:
            logger.warning("外部数据源 Provider 注册失败", error=str(e))
            self._ext_provider_registered = True  # 不重试

    # ── 私有：DB 内省 ──────────────────────────────────

    async def _introspect_from_db(self, datasource_name: str) -> list[KnowledgeEntry]:
        """从数据库系统表自动拉取表结构，转为 KnowledgeEntry 列表。"""
        try:
            from src.datasource.introspection import introspect_database
            from src.datasource.registry import get_registry

            self._ensure_external_provider()

            ds = await get_registry().resolve(datasource_name)
            if ds is None:
                logger.warning("数据源未找到，无法内省", datasource=datasource_name)
                return []

            # 构建 executor：匹配 introspection 的 3 参数签名 (ds, sql, params)
            async def _executor(_ds, sql: str, params: dict):
                import sqlalchemy as sa

                if _ds.engine is None:
                    raise RuntimeError(f"数据源 {_ds.name} 无可用引擎")
                async with _ds.engine.connect() as conn:
                    result = await conn.execute(sa.text(sql), params)
                    return [dict(row._mapping) for row in result]

            snapshot = await introspect_database(ds, _executor)
            entries = self._snapshot_to_entries(datasource_name, snapshot)
            logger.info(
                "DB 内省完成",
                datasource=datasource_name,
                tables=len(snapshot.tables) if snapshot else 0,
                entries=len(entries),
            )
            return entries
        except Exception as e:
            logger.error("DB 内省失败", datasource=datasource_name, error=str(e))
            return []

    def _snapshot_to_entries(
        self, datasource_name: str, snapshot
    ) -> list[KnowledgeEntry]:
        """将 SchemaSnapshot 转为表级 + 字段级双粒度 KnowledgeEntry 列表。"""
        entries: list[KnowledgeEntry] = []
        now = datetime.now(timezone.utc)

        for table in (snapshot.tables if snapshot else []):
            columns_desc = ", ".join(
                f"{c.name}({c.type})" for c in (table.columns or [])
            )

            # 表级：用于「找跟订单相关的表」
            entries.append(KnowledgeEntry(
                id=f"table:{datasource_name}.{table.name}",
                content=self._format_table_summary(
                    table.name, table.description or "", columns_desc
                ),
                source=KnowledgeSource.AUTO_INTROSPECT,
                category="table",
                table_name=table.name,
                created_at=now,
                ttl=AUTO_TTL_SECONDS,
                metadata={
                    "row_count_estimate": table.row_count_estimate or 0,
                    "description": table.description or "",
                    "datasource": datasource_name,
                    "foreign_keys": [
                        {"target_table": r.target_table, "join_key": r.join_key,
                         "relation_type": r.relation_type}
                        for r in (table.relations or [])
                    ],
                },
            ))

            # 字段级：每个字段独立一条，用于精确定位「amount 单位」
            for col in (table.columns or []):
                entries.append(KnowledgeEntry(
                    id=f"column:{datasource_name}.{table.name}.{col.name}",
                    content=self._format_column_detail(
                        table.name, col.name, col.type, col.comment or ""
                    ),
                    source=KnowledgeSource.AUTO_INTROSPECT,
                    category="column",
                    table_name=table.name,
                    column_name=col.name,
                    created_at=now,
                    ttl=AUTO_TTL_SECONDS,
                    metadata={
                        "type": col.type,
                        "is_nullable": col.is_nullable,
                        "is_primary_key": col.is_primary_key,
                    },
                ))

        return entries

    # ── 私有：缓存写入 ──────────────────────────────────

    def _upsert_to_cache(self, entries: list[KnowledgeEntry]) -> None:
        """将 KnowledgeEntry 批量写入 ChromaDB 缓存。"""
        if not entries:
            return
        try:
            ids = [e.id for e in entries]
            documents = [e.content for e in entries]
            metadatas = [e.to_dict() for e in entries]

            # 先删后写，实现 upsert 语义
            try:
                self._collection.delete(ids=ids)
            except Exception:
                pass

            self._collection.add(ids=ids, documents=documents, metadatas=metadatas)
            logger.info("ChromaDB 缓存写入完成", count=len(entries))
        except Exception as e:
            logger.error("ChromaDB 缓存写入失败", error=str(e))

    # ── 私有：条目合并 ──────────────────────────────────

    def _merge_entries(
        self, doc_entries: list[KnowledgeEntry], auto_entries: list[KnowledgeEntry]
    ) -> list[KnowledgeEntry]:
        """合并文档条目和自动内省条目，相同 ID 时文档优先（优先级更高）。"""
        merged: dict[str, KnowledgeEntry] = {}
        for e in auto_entries:
            merged[e.id] = e
        for e in doc_entries:
            if e.id in merged:
                if source_priority(e.source) <= source_priority(merged[e.id].source):
                    merged[e.id] = e
            else:
                merged[e.id] = e
        return list(merged.values())

    # ── 私有：SchemaSnapshot 组装 ───────────────────────

    def _build_snapshot(self, entries: list[KnowledgeEntry]):
        """将 KnowledgeEntry 列表组装为 SchemaSnapshot。"""
        from src.datasource.schema_snapshot import (
            ColumnInfo,
            SchemaSnapshot,
            TableRelation,
            TableSchema,
        )

        # 按表名分组
        table_entries: dict[str, KnowledgeEntry] = {}
        column_entries: dict[str, list[KnowledgeEntry]] = {}

        for e in entries:
            if e.category == "table":
                table_entries[e.table_name] = e
            elif e.category == "column":
                column_entries.setdefault(e.table_name, []).append(e)

        tables: list[TableSchema] = []
        for t_name, t_entry in table_entries.items():
            cols = column_entries.get(t_name, [])
            columns = [
                ColumnInfo(
                    name=c.column_name,
                    type=c.metadata.get("type", "String"),
                    comment=c.content.split(": ", 1)[-1] if ": " in c.content else c.content,
                    is_nullable=c.metadata.get("is_nullable", True),
                    is_primary_key=c.metadata.get("is_primary_key", False),
                )
                for c in cols
            ]
            # 从缓存恢复 FK 关系（兼容旧缓存无 foreign_keys 字段）
            relations = []
            for fk in t_entry.metadata.get("foreign_keys", []) or []:
                relations.append(TableRelation(
                    target_table=fk.get("target_table", ""),
                    join_key=fk.get("join_key", ""),
                    relation_type=fk.get("relation_type", "many_to_one"),
                ))
            tables.append(TableSchema(
                name=t_name,
                description=t_entry.content.split(" - ", 1)[-1] if " - " in t_entry.content else "",
                columns=columns,
                relations=relations,
                row_count_estimate=int(t_entry.metadata.get("row_count_estimate", 0)),
            ))

        return SchemaSnapshot(tables=tables)

    # ── 语义搜索 + FK 扩张 ─────────────────────────────

    # 表数量超过此阈值时触发语义筛选
    _FILTER_THRESHOLD: int = 30
    # 语义搜索返回的候选表数量
    _SEMANTIC_TOP_K: int = 20

    def _filter_relevant_tables(
        self, snapshot, user_query: str, datasource_name: str
    ):
        """语义搜索 + FK 图扩张，从大量表中筛选与查询相关的子集。"""
        tables = snapshot.tables if snapshot else []
        if len(tables) <= self._FILTER_THRESHOLD:
            logger.info("表数量未超阈值，跳过筛选", table_count=len(tables),
                        threshold=self._FILTER_THRESHOLD)
            return snapshot

        # ① 构建 FK 图（用于后续扩张）
        fk_graph = self._build_fk_graph(tables)

        # ② 语义搜索相关表
        matched_names = self._semantic_search_tables(user_query, datasource_name, tables)
        logger.info("语义搜索完成", matched=len(matched_names),
                    tables=sorted(matched_names))

        # ③ FK 图扩张：包含与匹配表有 FK 关系的表（1-hop）
        expanded = self._expand_fk_neighbors(matched_names, fk_graph)
        added = expanded - matched_names
        if added:
            logger.info("FK 扩张", added_tables=sorted(added),
                        added_count=len(added))

        # ④ 构建筛选后的快照
        all_selected = {t.lower() for t in expanded}
        filtered_tables = [t for t in tables if t.name.lower() in all_selected]
        from src.datasource.schema_snapshot import SchemaSnapshot
        logger.info("表筛选完成", total=len(tables), selected=len(filtered_tables),
                    filtered_out=len(tables) - len(filtered_tables))
        return SchemaSnapshot(tables=filtered_tables)

    @staticmethod
    def _build_fk_graph(tables: list) -> dict[str, set[str]]:
        """构建 FK 双向邻接表 {table_name: {related_table_names}}。

        同时建立出边（我引用谁）和入边（谁引用我），
        确保 JOIN 查询的上下游表都被覆盖。
        """
        graph: dict[str, set[str]] = {}
        for t in tables:
            graph.setdefault(t.name.lower(), set())
            for r in (t.relations or []):
                src = t.name.lower()
                dst = r.target_table.lower()
                graph.setdefault(src, set()).add(dst)
                graph.setdefault(dst, set()).add(src)
        return graph

    @staticmethod
    def _expand_fk_neighbors(
        seed_tables: set[str], fk_graph: dict[str, set[str]]
    ) -> set[str]:
        """将种子表集合按 FK 图扩张 1-hop。"""
        result = set(seed_tables)
        for t in seed_tables:
            neighbors = fk_graph.get(t.lower(), set())
            result.update(neighbors)
        return result

    def _semantic_search_tables(
        self, user_query: str, datasource_name: str, tables: list
    ) -> set[str]:
        """通过 ChromaDB 语义搜索找到与用户查询最相关的表。

        使用 ChromaDB 的 query 做 embedding 相似度匹配，
        然后通过 ID 前缀过滤到目标数据源。
        """
        try:
            # 获取足够多的候选结果（跨数据源），再后过滤
            n_fetch = max(self._SEMANTIC_TOP_K * 5, 100)
            results = self._collection.query(
                query_texts=[user_query],
                where={"category": "table"},
                n_results=min(n_fetch, self._collection.count()),
            )
            ids_list = results.get("ids", [[]])
            distances = results.get("distances", [[]])
            if not ids_list or not ids_list[0]:
                logger.warning("语义搜索无结果，回退到全部表")
                return {t.name.lower() for t in tables}

            prefix = f"table:{datasource_name}."
            matched: list[tuple[str, float]] = []
            for entry_id, dist in zip(ids_list[0], distances[0] if distances else []):
                if entry_id.startswith(prefix):
                    table_name = entry_id[len(prefix):]
                    matched.append((table_name, dist))

            # 按相似度排序，取 top-K
            matched.sort(key=lambda x: x[1])
            selected = {name for name, _ in matched[:self._SEMANTIC_TOP_K]}
            logger.info("语义搜索匹配", query=user_query[:60],
                        candidates=len(matched), selected=len(selected))
            return selected
        except Exception as e:
            logger.warning("语义搜索失败，回退到全部表", error=str(e))
            return {t.name.lower() for t in tables}

    # ── 格式化辅助 ─────────────────────────────────────

    def _format_table_summary(
        self, table_name: str, description: str, columns_desc: str
    ) -> str:
        """
        「orders - 订单表，包含字段: id(UInt64), amount(Float64), ...」
        """
        parts = [table_name]
        if description:
            parts.append(f" - {description}")
        if columns_desc:
            parts.append(f"，包含字段: {columns_desc}")
        return "".join(parts)

    def _format_column_detail(
        self, table_name: str, col_name: str, col_type: str, comment: str
    ) -> str:
        """「orders.amount: Float64 - 订单金额」"""
        base = f"{table_name}.{col_name}: {col_type}"
        if comment:
            base += f" - {comment}"
        return base

    # ── ChromaDB 生命周期 ───────────────────────────────

    def _ensure_initialized(self) -> None:
        """延迟初始化 ChromaDB 客户端和 collection。"""
        if self._initialized:
            return
        try:
            import chromadb

            settings = get_settings()
            embedding_function = self._create_embedding_function(settings)

            self._client = chromadb.PersistentClient(path=settings.chroma_persist_dir)
            self._collection = self._client.get_or_create_collection(
                name=settings.chroma_collection_name,
                embedding_function=embedding_function,
                metadata={"hnsw:space": "cosine"},
            )
            self._initialized = True
            logger.info(
                "ChromaDB 初始化完成",
                path=settings.chroma_persist_dir,
                collection=settings.chroma_collection_name,
                model_path=settings.embedding_model_path,
            )
        except Exception as e:
            logger.error("ChromaDB 初始化失败", error=str(e))
            raise

    @staticmethod
    def _create_embedding_function(settings):
        """创建嵌入函数。

        优先 EMBEDDING_MODEL_PATH 本地路径，未配置则从 HuggingFace 自动下载（首次 ~80MB）。
        """
        from pathlib import Path

        model_dir = settings.embedding_model_path
        if model_dir:
            model_path = Path(model_dir)
            if not model_path.exists():
                raise FileNotFoundError(f"嵌入模型路径不存在: {model_dir}")
            onnx_dir = model_path / "onnx"
            search_dir = onnx_dir if onnx_dir.exists() else model_path
            required = ["config.json", "model.onnx", "special_tokens_map.json",
                        "tokenizer_config.json", "tokenizer.json", "vocab.txt"]
            missing = [f for f in required if not (search_dir / f).exists()]
            if missing:
                raise FileNotFoundError(f"嵌入模型目录缺少文件: {missing}")
            from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
            return ONNXMiniLM_L6_V2(preferred_providers=["CPUExecutionProvider"])

        # 未配置 → HuggingFace 自动下载
        logger.info("EMBEDDING_MODEL_PATH 未配置，从 HuggingFace 自动下载 all-MiniLM-L6-v2")
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
        return ONNXMiniLM_L6_V2(preferred_providers=["CPUExecutionProvider"])

        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

        class _LocalModelEmbeddingFunction(ONNXMiniLM_L6_V2):
            DOWNLOAD_PATH = str(model_path)
            EXTRACTED_FOLDER_NAME = "onnx" if onnx_dir.exists() else ""

        logger.info("嵌入模型加载成功", model_dir=str(model_path))
        return _LocalModelEmbeddingFunction()

    def _row_to_entry(
        self, entry_id: str, content: str, metadata: dict
    ) -> KnowledgeEntry:
        """ChromaDB 行 → KnowledgeEntry。"""
        return KnowledgeEntry.from_dict({"id": entry_id, "content": content, **metadata})

    async def close(self) -> None:
        """释放 ChromaDB 资源。"""
        self._client = None
        self._collection = None
        self._initialized = False


# ── 单例 ──────────────────────────────────────────────

_manager: SchemaManager | None = None


def get_schema_manager() -> SchemaManager:
    """获取 SchemaManager 全局单例。"""
    global _manager
    if _manager is None:
        _manager = SchemaManager()
    return _manager
