"""Schema 缓存周期刷新、DDL 指纹轮询与多实例锁。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from src.datasource.schema_snapshot import SchemaSnapshot
from src.logging_config import get_logger

logger = get_logger(__name__)


# 方法作用：生成不受表和字段返回顺序影响的稳定 Schema SHA-256 指纹。
# Args: snapshot - 实时数据库 Schema 快照。
# Returns: 64 位十六进制指纹。
def compute_schema_fingerprint(snapshot: SchemaSnapshot) -> str:
    tables = []
    for table in sorted(snapshot.tables, key=lambda item: item.name):
        columns = [
            {
                "name": column.name,
                "type": column.type,
                "nullable": column.is_nullable,
                "primary_key": column.is_primary_key,
            }
            for column in sorted(table.columns, key=lambda item: item.name)
        ]
        relations = [
            {
                "target": relation.target_table,
                "key": relation.join_key,
                "type": relation.relation_type,
            }
            for relation in sorted(
                table.relations,
                key=lambda item: (item.target_table, item.join_key, item.relation_type),
            )
        ]
        tables.append({"name": table.name, "columns": columns, "relations": relations})
    canonical = json.dumps(tables, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CacheRefresher:
    """清理过期自动知识，并按实时 Schema 指纹主动刷新缓存。"""

    # 方法作用：注入 SchemaManager、VectorStore 和可选 Redis 分布式锁客户端。
    # Args: self - 刷新器；schema_manager - Schema 管理器；vector_store - 可选向量库；redis_client - 可选 Redis；lock_ttl_seconds - 锁租约。
    # Returns: 无返回值。
    def __init__(
        self,
        *,
        schema_manager: Any,
        vector_store: Any | None = None,
        redis_client: Any | None = None,
        lock_ttl_seconds: int = 60,
    ) -> None:
        self._schema_manager = schema_manager
        self._vector_store = vector_store
        self._redis = redis_client
        self._lock_ttl_seconds = max(10, int(lock_ttl_seconds))
        self._local_locks: set[str] = set()
        self._schema_fingerprints: dict[str, str] = {}

    # 方法作用：删除已过期 AUTO_INTROSPECT 条目并主动刷新受影响数据源。
    # Args: self - 刷新器。
    # Returns: 实际删除的过期条目数。
    async def refresh_expired(self) -> int:
        logger.debug("过期 Schema 缓存刷新入口")
        store = await self._get_vector_store()
        entries = await store.get_by_filter(
            {"source": "auto_introspect"},
            limit=10_000,
        )
        expired = [entry for entry in entries if self._is_expired(entry.metadata)]
        if not expired:
            logger.info("过期 Schema 缓存刷新完成", expired=0, deleted=0)
            return 0
        datasources = sorted({
            self._entry_datasource(entry.id, entry.metadata)
            for entry in expired
            if self._entry_datasource(entry.id, entry.metadata)
        })
        deleted = 0
        for datasource in datasources:
            token = await self._acquire_lock(datasource)
            if token is None:
                logger.info("过期 Schema 主动刷新跳过", datasource=datasource, reason="锁被占用")
                continue
            try:
                await self._schema_manager.refresh(datasource)
                # SchemaManager 成功后再检查旧 ID；若刷新已写入同 ID 的新快照，不删除新条目。
                refreshed_entries = await store.get_by_filter(
                    {"source": "auto_introspect", "datasource": datasource},
                    limit=10_000,
                )
                refreshed_by_id = {entry.id: entry for entry in refreshed_entries}
                stale_ids = [
                    entry.id
                    for entry in expired
                    if self._entry_datasource(entry.id, entry.metadata) == datasource
                    and (
                        entry.id not in refreshed_by_id
                        or self._is_expired(refreshed_by_id[entry.id].metadata)
                    )
                ]
                if stale_ids:
                    deleted += int(await store.delete_by_ids(stale_ids))
                logger.info(
                    "过期 Schema 刷新后清理完成",
                    datasource=datasource,
                    stale_count=len(stale_ids),
                )
            except Exception as exc:
                logger.error(
                    "过期 Schema 主动刷新失败",
                    datasource=datasource,
                    error=str(exc),
                    exc_info=True,
                )
            finally:
                await self._release_lock(datasource, token)
        logger.info(
            "过期 Schema 缓存刷新完成",
            expired=len(expired),
            deleted=deleted,
            datasource_count=len(datasources),
        )
        return int(deleted)

    # 方法作用：轮询实时 Schema 指纹，仅在 DDL 变化时使用已有快照刷新缓存。
    # Args: self - 刷新器；datasource_name - 数据源名称。
    # Returns: 本轮检测到变化并刷新时返回 True。
    async def refresh_on_schema_change(self, datasource_name: str) -> bool:
        token = await self._acquire_lock(datasource_name)
        if token is None:
            logger.info("Schema 指纹轮询跳过", datasource=datasource_name, reason="锁被占用")
            return False
        try:
            snapshot = await self._schema_manager.inspect_live_schema(datasource_name)
            current = compute_schema_fingerprint(snapshot)
            previous = await self._get_fingerprint(datasource_name)
            if not previous:
                await self._set_fingerprint(datasource_name, current)
                logger.info("Schema 指纹基线建立", datasource=datasource_name)
                return False
            if previous == current:
                logger.info("Schema 指纹未变化", datasource=datasource_name)
                return False
            await self._schema_manager.refresh_from_snapshot(datasource_name, snapshot)
            await self._set_fingerprint(datasource_name, current)
            logger.warning(
                "Schema 变化已触发缓存刷新",
                datasource=datasource_name,
                previous=previous[:12],
                current=current[:12],
            )
            return True
        except Exception as exc:
            logger.error(
                "Schema 指纹轮询失败",
                datasource=datasource_name,
                error=str(exc),
                exc_info=True,
            )
            return False
        finally:
            await self._release_lock(datasource_name, token)

    # 方法作用：关闭由刷新器持有的 Redis 客户端。
    # Args: self - 刷新器。
    # Returns: 无返回值。
    async def close(self) -> None:
        if self._redis is None:
            return
        close = getattr(self._redis, "aclose", None)
        if close is not None:
            await close()
        logger.info("Schema 缓存刷新器已关闭")

    # 方法作用：惰性获取当前 AppContext 的 VectorStore。
    # Args: self - 刷新器。
    # Returns: 可执行精确过滤和批量删除的 VectorStore。
    async def _get_vector_store(self):
        if self._vector_store is None:
            from src.memory.vector_store import get_vector_store

            self._vector_store = await get_vector_store()
        return self._vector_store

    # 方法作用：判断向量条目的 created_at 和 ttl 是否已到期。
    # Args: self - 刷新器；metadata - VectorEntry 元数据。
    # Returns: 已过期返回 True。
    def _is_expired(self, metadata: dict[str, Any]) -> bool:
        try:
            ttl = int(metadata.get("ttl", 0) or 0)
            if ttl <= 0:
                return False
            created_at = datetime.fromisoformat(str(metadata.get("created_at", "")))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) >= created_at + timedelta(seconds=ttl)
        except (TypeError, ValueError):
            logger.warning("Schema 缓存时间元数据无效，按未过期处理")
            return False

    # 方法作用：从显式 metadata 或稳定条目 ID 恢复数据源名称。
    # Args: self - 刷新器；entry_id - 向量条目 ID；metadata - 向量元数据。
    # Returns: 数据源名称，无法解析返回空字符串。
    def _entry_datasource(self, entry_id: str, metadata: dict[str, Any]) -> str:
        explicit = str(metadata.get("datasource", "") or "")
        if explicit:
            return explicit
        if ":" not in entry_id or "." not in entry_id:
            return ""
        return entry_id.split(":", 1)[1].split(".", 1)[0]

    # 方法作用：使用 Redis SET NX EX 或进程内集合获取单数据源刷新锁。
    # Args: self - 刷新器；datasource_name - 数据源名称。
    # Returns: 锁令牌；锁被占用返回 None。
    async def _acquire_lock(self, datasource_name: str) -> str | None:
        token = uuid4().hex
        if self._redis is not None:
            acquired = await self._redis.set(
                f"schema_refresh_lock:{datasource_name}",
                token,
                nx=True,
                ex=self._lock_ttl_seconds,
            )
            return token if acquired else None
        if datasource_name in self._local_locks:
            return None
        self._local_locks.add(datasource_name)
        return token

    # 方法作用：仅由锁持有者释放 Redis 或进程内刷新锁。
    # Args: self - 刷新器；datasource_name - 数据源名称；token - 获取锁时的令牌。
    # Returns: 无返回值。
    async def _release_lock(self, datasource_name: str, token: str) -> None:
        if self._redis is None:
            self._local_locks.discard(datasource_name)
            return
        key = f"schema_refresh_lock:{datasource_name}"
        evaluate = getattr(self._redis, "eval", None)
        if evaluate is not None:
            await evaluate(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                key,
                token,
            )
            return
        current = await self._redis.get(key)
        current_text = current.decode("utf-8") if isinstance(current, bytes) else current
        if current_text == token:
            await self._redis.delete(key)

    # 方法作用：读取进程内或 Redis 中保存的上一轮 Schema 指纹。
    # Args: self - 刷新器；datasource_name - 数据源名称。
    # Returns: 上一轮指纹；不存在返回空字符串。
    async def _get_fingerprint(self, datasource_name: str) -> str:
        if self._redis is None:
            return self._schema_fingerprints.get(datasource_name, "")
        raw = await self._redis.get(f"schema_fingerprint:{datasource_name}")
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw or "")

    # 方法作用：持久保存最新 Schema 指纹供下一轮比较。
    # Args: self - 刷新器；datasource_name - 数据源名称；fingerprint - 最新指纹。
    # Returns: 无返回值。
    async def _set_fingerprint(self, datasource_name: str, fingerprint: str) -> None:
        if self._redis is None:
            self._schema_fingerprints[datasource_name] = fingerprint
            return
        await self._redis.set(f"schema_fingerprint:{datasource_name}", fingerprint)


class CacheRefreshService:
    """绑定应用生命周期的 Schema 缓存周期刷新服务。"""

    # 方法作用：保存刷新器、Registry 和执行周期但不立即启动任务。
    # Args: self - 服务；refresher - 缓存刷新器；registry - 数据源 Registry；interval_seconds - 轮询周期。
    # Returns: 无返回值。
    def __init__(self, refresher: CacheRefresher, registry: Any, interval_seconds: int) -> None:
        self._refresher = refresher
        self._registry = registry
        self._interval_seconds = max(60, int(interval_seconds))
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    # 方法作用：返回周期刷新任务是否仍在运行。
    # Args: self - 服务。
    # Returns: 后台任务存在且未结束时返回 True。
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # 方法作用：幂等启动 Schema 缓存周期刷新任务。
    # Args: self - 服务。
    # Returns: 无返回值。
    async def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="schema-cache-refresh")
        logger.info("Schema 缓存周期刷新服务已启动", interval_seconds=self._interval_seconds)

    # 方法作用：停止周期任务并关闭刷新器持有的资源。
    # Args: self - 服务。
    # Returns: 无返回值。
    async def close(self) -> None:
        if self._task is not None:
            self._stop_event.set()
            await self._task
            self._task = None
        await self._refresher.close()
        logger.info("Schema 缓存周期刷新服务已关闭")

    # 方法作用：执行一轮过期清理和全部数据源的 DDL 指纹检查。
    # Args: self - 服务。
    # Returns: 过期删除数、检查数和变更数摘要。
    async def run_once(self) -> dict[str, int]:
        deleted = await self._refresher.refresh_expired()
        items = await self._registry.list_all()
        names = sorted({str(item.get("name", "")) for item in items if item.get("name")})
        changed = 0
        for name in names:
            if await self._refresher.refresh_on_schema_change(name):
                changed += 1
        result = {"deleted": deleted, "checked": len(names), "changed": changed}
        logger.info("Schema 缓存周期刷新完成", **result)
        return result

    # 方法作用：按配置周期等待并执行刷新，单轮失败不终止后台服务。
    # Args: self - 服务。
    # Returns: 无返回值。
    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval_seconds,
                )
                continue
            except TimeoutError:
                logger.debug("Schema 缓存刷新等待周期结束")
            try:
                await self.run_once()
            except Exception as exc:
                logger.error("Schema 缓存周期刷新失败", error=str(exc), exc_info=True)
