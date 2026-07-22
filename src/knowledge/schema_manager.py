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

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

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

    def __init__(self, datasource_cache: Any | None = None) -> None:
        """初始化 Schema 管理器及连接级共享缓存。

        Args:
            datasource_cache: 可选缓存后端，测试和定制部署可显式注入。

        Returns:
            无返回值。
        """
        logger.debug(
            "初始化 SchemaManager 入口",
            injected_cache=datasource_cache is not None,
        )
        if datasource_cache is None:
            from src.knowledge.datasource_cache import get_datasource_cache

            datasource_cache = get_datasource_cache()
        self._datasource_cache = datasource_cache
        self._client = None
        self._collection = None
        self._initialized = False
        logger.info(
            "初始化 SchemaManager 完成",
            cache_backend=type(self._datasource_cache).__name__,
        )

    # ── 公开接口 ──────────────────────────────────────

    async def get_or_fetch_schema(self, datasource_name: str, user_query: str = ""):
        """
        获取 Schema 的主入口 — 三级回退。

        当 user_query 提供且表数量超过阈值时，自动启用语义搜索 + FK 扩张，
        避免将数千张表全部塞入 LLM prompt。

        返回: SchemaSnapshot（永远不为 None，最差返回空快照）
        """
        logger.debug(
            "获取 Schema 入口",
            datasource=datasource_name,
            has_query=bool(user_query),
        )
        datasource = await self._resolve_datasource(datasource_name)
        fingerprint = ""
        if datasource is not None:
            from src.knowledge.datasource_cache import build_connection_fingerprint

            fingerprint = build_connection_fingerprint(datasource)

        # ① 查连接级精确缓存，不按用户、会话或显示名称切分
        shared_cached = await self._read_shared_cache(fingerprint)
        doc_entries = self._load_from_docs(datasource_name)
        if shared_cached and self._entries_complete(shared_cached):
            rebound = self._rebind_entries(shared_cached, datasource_name)
            await self._upsert_to_cache(rebound)
            merged = self._merge_entries(doc_entries, rebound)
            logger.info(
                "Schema 连接级共享缓存命中",
                datasource=datasource_name,
                fingerprint=fingerprint[:12],
                entries=len(shared_cached),
            )
            snapshot = self._build_snapshot(merged)
            if user_query:
                snapshot = await self._filter_relevant_tables(
                    snapshot, user_query, datasource_name,
                )
            logger.info(
                "获取 Schema 完成",
                datasource=datasource_name,
                source="shared_cache",
                tables=len(snapshot.tables),
            )
            return snapshot
        if shared_cached:
            logger.warning(
                "Schema 连接级共享缓存不完整，触发刷新",
                datasource=datasource_name,
                fingerprint=fingerprint[:12],
            )
            await self._delete_shared_cache(fingerprint)

        # ② 兼容旧 VectorStore 缓存，并在命中后迁移到连接级缓存
        cached = await self._query_cache(datasource_name)
        if cached and not self._any_expired(cached):
            if self._entries_complete(cached):
                logger.info("Schema 缓存命中", datasource=datasource_name, entries=len(cached))
                await self._write_shared_cache(fingerprint, cached)
                snapshot = self._build_snapshot(cached)
                if user_query:
                    snapshot = await self._filter_relevant_tables(snapshot, user_query, datasource_name)
                logger.info(
                    "获取 Schema 完成",
                    datasource=datasource_name,
                    source="vector_cache",
                    tables=len(snapshot.tables),
                )
                return snapshot
            logger.warning(
                "Schema 缓存缺少字段，触发内省刷新",
                datasource=datasource_name,
            )

        if cached:
            logger.info("Schema 缓存部分过期，触发内省刷新", datasource=datasource_name)

        # ③ DB 内省，文档条目只参与当前知识合并，不写入跨用户精确缓存
        logger.info("Schema 缓存未命中，触发 DB 内省", datasource=datasource_name)
        introspected = await self._introspect_from_db(datasource_name)

        # 合并：文档优先于自动内省
        merged = self._merge_entries(doc_entries, introspected)
        if merged:
            await self._upsert_to_cache(merged)
            await self._write_shared_cache(fingerprint, introspected)
            merged_ids = {entry.id for entry in merged}
            stale_ids = sorted(entry.id for entry in cached if entry.id not in merged_ids)
            if stale_ids:
                try:
                    from src.memory.vector_store import get_vector_store
                    store = await get_vector_store()
                    deleted = await store.delete_by_ids(stale_ids)
                    logger.info(
                        "Schema 孤儿缓存清理完成",
                        datasource=datasource_name,
                        stale_count=len(stale_ids),
                        deleted=deleted,
                    )
                except Exception as exc:
                    logger.error(
                        "Schema 孤儿缓存清理失败",
                        datasource=datasource_name,
                        stale_count=len(stale_ids),
                        error=str(exc),
                        exc_info=True,
                    )

        result = merged or cached or []
        snapshot = self._build_snapshot(result)
        if user_query:
            snapshot = await self._filter_relevant_tables(snapshot, user_query, datasource_name)
        logger.info(
            "获取 Schema 完成",
            datasource=datasource_name,
            source="database" if introspected else "fallback",
            tables=len(snapshot.tables),
        )
        return snapshot

    async def _resolve_datasource(self, datasource_name: str):
        """解析当前数据源配置以生成连接级缓存身份。

        Args:
            datasource_name: 数据源显示名称。

        Returns:
            DataSourceConfig；解析失败时返回 None 并由旧缓存或内省链路处理。
        """
        logger.debug("解析共享缓存数据源入口", datasource=datasource_name)
        try:
            from src.datasource.registry import get_registry

            self._ensure_external_provider()
            datasource = await get_registry().resolve_or_none(datasource_name)
            logger.info(
                "解析共享缓存数据源完成",
                datasource=datasource_name,
                found=datasource is not None,
            )
            return datasource
        except Exception as exc:
            logger.error(
                "解析共享缓存数据源失败，降级旧缓存链路",
                datasource=datasource_name,
                error=str(exc),
                exc_info=True,
            )
            return None

    async def _read_shared_cache(self, fingerprint: str) -> list[KnowledgeEntry]:
        """读取连接级共享缓存并将后端异常降级为 miss。

        Args:
            fingerprint: 连接指纹；空字符串表示无法建立连接身份。

        Returns:
            缓存知识条目；未命中或异常时返回空列表。
        """
        logger.debug("读取连接级共享缓存入口", fingerprint=fingerprint[:12])
        if not fingerprint:
            logger.info("读取连接级共享缓存跳过", reason="无连接指纹")
            return []
        try:
            entries = await self._datasource_cache.get(fingerprint)
            result = entries or []
            logger.info(
                "读取连接级共享缓存完成",
                fingerprint=fingerprint[:12],
                entry_count=len(result),
            )
            return result
        except Exception as exc:
            logger.error(
                "读取连接级共享缓存失败，降级旧缓存链路",
                fingerprint=fingerprint[:12],
                error=str(exc),
                exc_info=True,
            )
            return []

    async def _write_shared_cache(
        self, fingerprint: str, entries: list[KnowledgeEntry],
    ) -> None:
        """写入仅含数据库派生元数据的连接级共享缓存。

        Args:
            fingerprint: 连接指纹。
            entries: DB 内省或旧缓存中的知识条目。

        Returns:
            无返回值；缓存故障只记录日志，不阻断分析链路。
        """
        logger.debug(
            "写入连接级共享缓存入口",
            fingerprint=fingerprint[:12],
            entry_count=len(entries),
        )
        if not fingerprint or not entries:
            logger.info(
                "写入连接级共享缓存跳过",
                reason="无连接指纹或条目为空",
            )
            return
        shareable_sources = {
            KnowledgeSource.AUTO_INTROSPECT,
            KnowledgeSource.DB_COMMENT,
            KnowledgeSource.ORM_MODEL,
        }
        shareable = [entry for entry in entries if entry.source in shareable_sources]
        if not shareable:
            logger.info("写入连接级共享缓存跳过", reason="无可共享数据库元数据")
            return
        sanitized = self._sanitize_shared_entries(shareable)
        try:
            await self._datasource_cache.set(fingerprint, sanitized)
            logger.info(
                "写入连接级共享缓存完成",
                fingerprint=fingerprint[:12],
                entry_count=len(sanitized),
            )
        except Exception as exc:
            logger.error(
                "写入连接级共享缓存失败，继续当前请求",
                fingerprint=fingerprint[:12],
                error=str(exc),
                exc_info=True,
            )

    async def _delete_shared_cache(self, fingerprint: str) -> None:
        """删除连接级共享缓存并将后端故障限制在刷新边界。

        Args:
            fingerprint: 连接指纹。

        Returns:
            无返回值。
        """
        logger.debug("删除连接级共享缓存入口", fingerprint=fingerprint[:12])
        if not fingerprint:
            logger.info("删除连接级共享缓存跳过", reason="无连接指纹")
            return
        try:
            deleted = await self._datasource_cache.delete(fingerprint)
            logger.info(
                "删除连接级共享缓存完成",
                fingerprint=fingerprint[:12],
                deleted=deleted,
            )
        except Exception as exc:
            logger.error(
                "删除连接级共享缓存失败，继续刷新",
                fingerprint=fingerprint[:12],
                error=str(exc),
                exc_info=True,
            )

    def _entries_complete(self, entries: list[KnowledgeEntry]) -> bool:
        """检查缓存中每张表是否至少包含一个字段条目。

        Args:
            entries: 待检查的 Schema 知识条目。

        Returns:
            条目未过期且表字段完整时返回 True。
        """
        logger.debug("检查 Schema 缓存完整性入口", entry_count=len(entries))
        if not entries or self._any_expired(entries):
            logger.info("检查 Schema 缓存完整性完成", complete=False, reason="空或过期")
            return False
        table_names = {
            entry.table_name for entry in entries
            if entry.category == "table" and entry.table_name
        }
        column_tables = {
            entry.table_name for entry in entries
            if entry.category == "column" and entry.table_name
        }
        complete = bool(table_names) and not (table_names - column_tables)
        logger.info(
            "检查 Schema 缓存完整性完成",
            complete=complete,
            table_count=len(table_names),
            missing_columns=len(table_names - column_tables),
        )
        return complete

    def _sanitize_shared_entries(
        self, entries: list[KnowledgeEntry],
    ) -> list[KnowledgeEntry]:
        """移除用户、租户、别名信息后生成可跨请求共享的条目。

        Args:
            entries: 当前请求上下文中的知识条目。

        Returns:
            仅保留数据库派生内容的条目副本。
        """
        logger.debug("清理共享缓存条目入口", entry_count=len(entries))
        result: list[KnowledgeEntry] = []
        for entry in entries:
            metadata = dict(entry.metadata)
            for key in ("datasource", "owner_user_id", "tenant_id", "visibility"):
                metadata.pop(key, None)
            if entry.category == "table":
                entry_id = f"table:shared.{entry.table_name}"
            elif entry.category == "column":
                entry_id = f"column:shared.{entry.table_name}.{entry.column_name}"
            else:
                continue
            result.append(replace(entry, id=entry_id, metadata=metadata))
        logger.info("清理共享缓存条目完成", entry_count=len(result))
        return result

    def _rebind_entries(
        self, entries: list[KnowledgeEntry], datasource_name: str,
    ) -> list[KnowledgeEntry]:
        """将共享条目绑定到当前别名和安全上下文用于向量索引。

        Args:
            entries: 不含用户身份的共享缓存条目。
            datasource_name: 当前请求使用的数据源显示名称。

        Returns:
            可写入当前 VectorStore 分区的条目副本。
        """
        logger.debug(
            "绑定共享缓存条目入口",
            datasource=datasource_name,
            entry_count=len(entries),
        )
        result: list[KnowledgeEntry] = []
        for entry in entries:
            metadata = dict(entry.metadata)
            metadata.update({
                "datasource": datasource_name,
                "tenant_id": self._current_tenant_id(),
                "owner_user_id": self._current_user_id(),
                "visibility": "tenant",
            })
            if entry.category == "table":
                entry_id = f"table:{datasource_name}.{entry.table_name}"
            elif entry.category == "column":
                entry_id = f"column:{datasource_name}.{entry.table_name}.{entry.column_name}"
            else:
                continue
            result.append(replace(entry, id=entry_id, metadata=metadata))
        logger.info(
            "绑定共享缓存条目完成",
            datasource=datasource_name,
            entry_count=len(result),
        )
        return result

    # ── 私有：缓存查询 ─────────────────────────────────

    async def _query_cache(self, datasource_name: str) -> list[KnowledgeEntry]:
        """从向量存储检索指定数据源的所有知识条目。"""
        logger.debug("查询 Schema 缓存入口", datasource=datasource_name)
        try:
            from src.memory.vector_store import get_vector_store
            store = await get_vector_store()
            filters = {
                "table_name": {"$ne": ""},
                "datasource": datasource_name,
                "visibility": "tenant",
            }
            from src.app_context import get_tenant_policy
            if get_tenant_policy().knowledge_isolation_enabled:
                from src.api.auth import get_current_tenant_id
                filters["tenant_id"] = get_current_tenant_id()
            results = await store.get_by_filter(
                filters, limit=10000)
            entries = []
            for r in results:
                if not self._belongs_to_datasource(r.id, datasource_name):
                    continue
                metadata = dict(r.metadata or {})
                try:
                    import json
                    metadata.update(json.loads(metadata.get("meta_json", "{}")))
                except (TypeError, ValueError):
                    logger.warning("Schema 缓存元数据解析失败", entry_id=r.id)
                raw_entry = {
                    "id": r.id,
                    "content": r.content,
                    **metadata,
                    "meta_json": metadata.get("meta_json", "{}"),
                }
                entries.append(KnowledgeEntry.from_dict(raw_entry))
            logger.info(
                "查询 Schema 缓存完成",
                datasource=datasource_name,
                count=len(entries),
                source_filtered=True,
            )
            return entries
        except Exception as exc:
            logger.error("向量存储缓存查询失败，降级到 DB 内省", error=str(exc), exc_info=True)
            return []

    async def refresh(self, datasource_name: str):
        """清理指定数据源缓存并执行真实 DB Schema 刷新。

        Args:
            datasource_name: 数据源名称。

        Returns:
            刷新后的 SchemaSnapshot。
        """
        logger.debug("Schema 刷新入口", datasource=datasource_name)
        datasource = await self._resolve_datasource(datasource_name)
        fingerprint = ""
        if datasource is not None:
            from src.knowledge.datasource_cache import build_connection_fingerprint

            fingerprint = build_connection_fingerprint(datasource)
        await self._delete_shared_cache(fingerprint)
        try:
            from src.memory.vector_store import get_vector_store
            store = await get_vector_store()
            cached = await self._query_cache(datasource_name)
            if cached:
                deleted = await store.delete_by_ids([entry.id for entry in cached])
                logger.info("Schema 旧缓存已删除", datasource=datasource_name, deleted=deleted)
        except Exception as exc:
            logger.warning("Schema 旧缓存清理失败，继续内省", datasource=datasource_name, error=str(exc))

        entries = await self._introspect_from_db(datasource_name)
        if entries:
            await self._upsert_to_cache(entries)
            await self._write_shared_cache(fingerprint, entries)
        snapshot = self._build_snapshot(entries)
        logger.info("Schema 刷新完成", datasource=datasource_name, tables=len(snapshot.tables))
        return snapshot

    async def update_column_comment(
        self, datasource_name: str, table_name: str, column_name: str, comment: str,
    ) -> bool:
        """更新缓存中的字段备注。

        Args:
            datasource_name: 数据源名称。
            table_name: 表名。
            column_name: 字段名。
            comment: 新备注文本。

        Returns:
            找到并更新字段时返回 True，否则返回 False。
        """
        logger.debug(
            "字段备注更新入口",
            datasource=datasource_name,
            table=table_name,
            column=column_name,
        )
        self._ensure_initialized()
        try:
            from src.memory.vector_store import VectorEntry, get_vector_store
            store = await get_vector_store()
            entry_id = f"column:{datasource_name}.{table_name}.{column_name}"
            entry = await store.get_by_id(entry_id)
            if entry is None:
                logger.warning("字段备注更新目标不存在", entry_id=entry_id)
                return False
            metadata = dict(entry.metadata or {})
            column_type = metadata.get("type", "String")
            entry.metadata = metadata
            entry.content = self._format_column_detail(table_name, column_name, column_type, comment)
            await store.upsert([VectorEntry(entry.id, entry.content, metadata)])
            logger.info("字段备注更新完成", entry_id=entry_id)
            return True
        except Exception as exc:
            logger.error("字段备注更新失败", error=str(exc), exc_info=True)
            return False

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
            logger.error("文档加载失败", error=str(e), exc_info=True)
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
                from sqlalchemy.ext.asyncio import AsyncEngine
                if isinstance(_ds.engine, AsyncEngine):
                    async with _ds.engine.connect() as conn:
                        result = await conn.execute(sa.text(sql), params)
                        return [dict(row._mapping) for row in result]
                else:
                    with _ds.engine.connect() as conn:
                        result = conn.execute(sa.text(sql), params)
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
        except Exception as exc:
            logger.error(
                "DB 内省失败",
                datasource=datasource_name,
                error=str(exc),
                exc_info=True,
            )
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
                    "partition_key": table.partition_key or "",
                    "tags": list(table.tags or []),
                    "datasource": datasource_name,
                    "tenant_id": self._current_tenant_id(),
                    "owner_user_id": self._current_user_id(),
                    "visibility": "tenant",
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
                        "comment": col.comment or "",
                        "is_nullable": col.is_nullable,
                        "is_primary_key": col.is_primary_key,
                        "is_indexed": getattr(col, "is_indexed", False),
                        "enum_values": list(col.enum_values or []),
                        "datasource": datasource_name,
                        "tenant_id": self._current_tenant_id(),
                        "owner_user_id": self._current_user_id(),
                        "visibility": "tenant",
                    },
                ))

        return entries

    @staticmethod
    def _current_tenant_id() -> int:
        """获取当前租户 ID，供缓存元数据写入使用。

        Returns:
            当前租户 ID；无请求上下文时返回 1。
        """
        logger.debug("读取 Schema 当前租户入口")
        from src.api.auth import get_current_tenant_id

        result = get_current_tenant_id()
        logger.info("读取 Schema 当前租户完成", tenant_id=result)
        return result

    @staticmethod
    def _current_user_id() -> int:
        """获取当前用户 ID，供缓存元数据写入使用。

        Returns:
            当前用户 ID；无请求上下文时返回 0。
        """
        logger.debug("读取 Schema 当前用户入口")
        from src.api.auth import get_current_user_id

        result = get_current_user_id()
        logger.info("读取 Schema 当前用户完成", user_id=result)
        return result

    # ── 私有：缓存写入 ──────────────────────────────────

    async def _upsert_to_cache(self, entries: list[KnowledgeEntry]) -> None:
        """将 KnowledgeEntry 批量写入向量存储。"""
        logger.debug("写入 Schema 缓存入口", count=len(entries))
        if not entries:
            logger.info("写入 Schema 缓存跳过", reason="条目为空")
            return
        try:
            from src.memory.vector_store import VectorEntry, get_vector_store
            store = await get_vector_store()
            vec_entries = []
            for entry in entries:
                metadata = entry.to_dict()
                datasource = str(entry.metadata.get("datasource", ""))
                if not datasource and ":" in entry.id and "." in entry.id:
                    datasource = entry.id.split(":", 1)[1].split(".", 1)[0]
                metadata["datasource"] = datasource
                vec_entries.append(VectorEntry(
                    id=entry.id,
                    content=entry.content,
                    metadata=metadata,
                ))
            await store.upsert(vec_entries)
            logger.info("向量存储缓存写入完成", count=len(entries))
        except Exception as exc:
            logger.error("向量存储缓存写入失败", error=str(exc), exc_info=True)

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
                    comment=c.metadata.get("comment", "") or "",
                    is_nullable=c.metadata.get("is_nullable", True),
                    is_primary_key=c.metadata.get("is_primary_key", False),
                    is_indexed=c.metadata.get("is_indexed", False),
                    enum_values=list(c.metadata.get("enum_values", []) or []),
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
                partition_key=str(t_entry.metadata.get("partition_key", "") or ""),
                tags=list(t_entry.metadata.get("tags", []) or []),
            ))

        return SchemaSnapshot(tables=tables)

    # ── 语义搜索 + FK 扩张 ─────────────────────────────

    # 表数量超过此阈值时触发语义筛选
    _FILTER_THRESHOLD: int = 30
    # 语义搜索返回的候选表数量
    _SEMANTIC_TOP_K: int = 20

    async def _filter_relevant_tables(
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
        matched_names = await self._semantic_search_tables(user_query, datasource_name, tables)
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

    async def _semantic_search_tables(
        self, user_query: str, datasource_name: str, tables: list
    ) -> set[str]:
        """通过向量语义搜索找到与用户查询最相关的表。"""
        logger.debug(
            "Schema 语义搜索入口",
            datasource=datasource_name,
            query=user_query[:60],
            table_count=len(tables),
        )
        try:
            from src.app_context import get_tenant_policy
            from src.memory.vector_store import get_vector_store
            store = await get_vector_store()
            results = await store.search(
                user_query,
                top_k=50,
                filters={
                    "category": "table",
                    "datasource": datasource_name,
                    "visibility": "tenant",
                    **(
                        {"tenant_id": self._current_tenant_id()}
                        if get_tenant_policy().knowledge_isolation_enabled
                        else {}
                    ),
                },
            )
            if not results:
                logger.warning("语义搜索无结果，回退到全部表")
                return {t.name.lower() for t in tables}

            prefix = f"table:{datasource_name}."
            matched: list[tuple[str, float]] = []
            for r in results:
                if r.id.startswith(prefix):
                    table_name = r.id[len(prefix):]
                    matched.append((table_name, 1.0 - r.score))

            # 按相似度排序，取 top-K
            matched.sort(key=lambda x: x[1])
            selected = {name for name, _ in matched[:self._SEMANTIC_TOP_K]}
            logger.info("语义搜索匹配", query=user_query[:60],
                        candidates=len(matched), selected=len(selected))
            return selected
        except Exception as exc:
            logger.warning(
                "语义搜索失败，回退到全部表",
                error=str(exc),
                exc_info=True,
            )
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
        except Exception as exc:
            logger.error("ChromaDB 初始化失败", error=str(exc), exc_info=True)
            raise

    @staticmethod
    def _create_embedding_function(settings):
        """创建嵌入函数。

        Args:
            settings: 包含 embedding_model_path 的运行配置。

        Returns:
            使用本地目录或默认缓存的 ChromaDB 嵌入函数。
        """
        from pathlib import Path

        model_dir = settings.embedding_model_path
        logger.debug("创建嵌入函数入口", configured=bool(model_dir))
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

            class _LocalModelEmbeddingFunction(ONNXMiniLM_L6_V2):
                """把 ChromaDB ONNX 模型读取目录固定到已校验的本地路径。"""

                DOWNLOAD_PATH = str(model_path)
                EXTRACTED_FOLDER_NAME = "onnx" if onnx_dir.exists() else ""

            result = _LocalModelEmbeddingFunction(
                preferred_providers=["CPUExecutionProvider"],
            )
            logger.info("嵌入模型加载成功", model_dir=str(model_path))
            return result

        # 未配置 → HuggingFace 自动下载
        logger.info("EMBEDDING_MODEL_PATH 未配置，从 HuggingFace 自动下载 all-MiniLM-L6-v2")
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
        result = ONNXMiniLM_L6_V2(preferred_providers=["CPUExecutionProvider"])
        logger.info("默认嵌入模型加载成功")
        return result

    def _row_to_entry(
        self, entry_id: str, content: str, metadata: dict
    ) -> KnowledgeEntry:
        """ChromaDB 行 → KnowledgeEntry。"""
        return KnowledgeEntry.from_dict({"id": entry_id, "content": content, **metadata})

    async def close(self) -> None:
        """释放 ChromaDB 资源。"""
        logger.debug("关闭 SchemaManager 入口")
        close_cache = getattr(self._datasource_cache, "close", None)
        if close_cache is not None:
            try:
                await close_cache()
            except Exception as exc:
                logger.error("关闭数据库内容缓存失败", error=str(exc), exc_info=True)
        self._client = None
        self._collection = None
        self._initialized = False
        logger.info("关闭 SchemaManager 完成")


# 方法作用：从当前 AppContext 获取 SchemaManager。
# Args: 无。
# Returns: 当前应用独享的 SchemaManager 实例。
def get_schema_manager() -> SchemaManager:
    """获取当前应用的 SchemaManager。"""
    from src.app_context import get_app_context

    logger.debug("获取 SchemaManager 入口")
    result = get_app_context().get_or_create(
        "schema_manager",
        SchemaManager,
        closer=SchemaManager.close,
    )
    logger.info("获取 SchemaManager 完成")
    return result
