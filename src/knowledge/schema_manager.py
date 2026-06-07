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

    async def get_or_fetch_schema(self, datasource_name: str):
        """
        获取 Schema 的主入口 — 三级回退。

        返回: SchemaSnapshot（永远不为 None，最差返回空快照）
        """
        self._ensure_initialized()

        # ① 查 ChromaDB 缓存
        cached = self._query_cache(datasource_name)
        if cached and not self._any_expired(cached):
            logger.info("Schema 缓存命中", datasource=datasource_name, entries=len(cached))
            return self._build_snapshot(cached)

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
        return self._build_snapshot(result)

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
            tables.append(TableSchema(
                name=t_name,
                description=t_entry.content.split(" - ", 1)[-1] if " - " in t_entry.content else "",
                columns=columns,
                relations=[],
                row_count_estimate=int(t_entry.metadata.get("row_count_estimate", 0)),
            ))

        return SchemaSnapshot(tables=tables)

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
        """基于配置创建嵌入函数，EMBEDDING_MODEL_PATH 未配置则直接报错。"""
        from pathlib import Path

        model_dir = settings.embedding_model_path
        if not model_dir:
            raise ValueError(
                "EMBEDDING_MODEL_PATH 未配置，请在 .env 中设置 "
                "all-MiniLM-L6-v2 模型目录路径（如 EMBEDDING_MODEL_PATH=D:/work/all-MiniLM-L6-v2）"
            )

        model_path = Path(model_dir)
        if not model_path.exists():
            raise FileNotFoundError(f"嵌入模型路径不存在: {model_dir}")

        # 校验 ONNX 模型所需的 6 个文件
        onnx_dir = model_path / "onnx"
        required_files = [
            "config.json", "model.onnx", "special_tokens_map.json",
            "tokenizer_config.json", "tokenizer.json", "vocab.txt",
        ]
        # 优先检查 onnx 子目录，不存在则检查模型根目录
        search_dir = onnx_dir if onnx_dir.exists() else model_path
        missing = [f for f in required_files if not (search_dir / f).exists()]
        if missing:
            raise FileNotFoundError(
                f"嵌入模型目录缺少文件: {missing}\n"
                f"请确保 {model_dir} 包含完整的 all-MiniLM-L6-v2 ONNX 模型"
            )

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
