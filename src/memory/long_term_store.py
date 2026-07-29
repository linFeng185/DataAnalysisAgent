"""身份隔离、可过期且双后端一致的长期记忆存储。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from src.logging_config import get_logger
from src.memory.models import LongTermMemory, MemoryType
from src.security.tenant_policy import RequestIdentity, TenantPolicy


logger = get_logger(__name__)
_RESOURCE_KIND = "long_term_memory"
_MIN_CONFIDENCE = 0.3
_MEMORY_RESOURCE = "long_term_memory_store"


class LongTermMemoryStore:
    """长期记忆写入、身份隔离检索和生命周期维护入口。"""

    # 方法作用：初始化可注入的 PostgreSQL、VectorStore 和租户策略依赖。
    # Args: self - 当前 Store；pg_pool - 可选 PG Pool；vector_store - 可选 VectorStore；policy - 租户策略。
    # Returns: 无返回值。
    def __init__(
        self,
        pg_pool: Any = None,
        vector_store: Any = None,
        policy: TenantPolicy | None = None,
    ) -> None:
        self._pg = pg_pool
        self._vector_store = vector_store
        self._policy = policy

    @property
    # 方法作用：返回长期记忆当前使用的可选 PostgreSQL Pool。
    # Args: self - 当前 Store。
    # Returns: PostgreSQL Pool；向量单后端模式返回 None。
    def pg_pool(self) -> Any:
        return self._pg

    # 方法作用：按 system、当前 tenant、当前 private 三个范围召回长期记忆。
    # Args: self - 当前 Store；query - 查询文本；memory_type - 类型过滤；top_k - 返回上限；identity - 当前身份。
    # Returns: 通过身份、TTL 和置信度校验并已刷新访问状态的记忆列表。
    async def search(
        self,
        query: str,
        memory_type: MemoryType | None = None,
        top_k: int = 5,
        *,
        identity: RequestIdentity | None = None,
    ) -> list[LongTermMemory]:
        current_identity = self._resolve_identity(identity)
        limit = max(1, min(int(top_k), 50))
        logger.debug(
            "长期记忆检索入口",
            tenant_id=current_identity.tenant_id,
            user_id=current_identity.user_id,
            memory_type=memory_type.value if memory_type else "all",
            top_k=limit,
        )
        try:
            store = await self._vector()
            results = []
            for filters in self._visible_filters(current_identity, memory_type):
                results.extend(await store.search(query, top_k=limit, filters=filters))

            selected: dict[str, tuple[float, LongTermMemory]] = {}
            expired_ids: list[str] = []
            for result in results:
                memory = _memory_from_vector(result)
                if not self._is_visible(memory, current_identity):
                    logger.warning(
                        "长期记忆越界结果已拒绝",
                        entry_id=memory.id,
                        visibility=memory.visibility,
                        tenant_id=memory.tenant_id,
                        owner_user_id=memory.owner_user_id,
                    )
                    continue
                if memory.is_expired():
                    expired_ids.append(memory.id)
                    continue
                if memory.confidence < _MIN_CONFIDENCE:
                    continue
                existing = selected.get(memory.id)
                if existing is None or result.score > existing[0]:
                    selected[memory.id] = (result.score, memory)

            if expired_ids:
                await self._delete_ids(
                    list(dict.fromkeys(expired_ids)),
                    identity=current_identity,
                )
            memories = [item[1] for item in sorted(
                selected.values(), key=lambda item: item[0], reverse=True,
            )[:limit]]
            for memory in memories:
                await self._touch(memory, identity=current_identity)
            logger.info(
                "长期记忆检索完成",
                tenant_id=current_identity.tenant_id,
                user_id=current_identity.user_id,
                result_count=len(memories),
                expired_count=len(set(expired_ids)),
            )
            return memories
        except Exception as exc:
            logger.error("长期记忆检索失败", error=str(exc), exc_info=True)
            return []

    # 方法作用：保存成功 SQL 为当前身份私有模板，审核后可显式写入租户范围。
    # Args: self - 当前 Store；user_query/sql/dialect - 模板内容；verified - 是否审核；identity - 当前身份；visibility - 作用域；ttl_days - 有效期。
    # Returns: 已持久化的长期记忆。
    async def save_sql_template(
        self,
        user_query: str,
        sql: str,
        dialect: str,
        verified: bool = False,
        *,
        identity: RequestIdentity | None = None,
        visibility: str = "private",
        ttl_days: int | None = 180,
    ) -> LongTermMemory:
        current_identity = self._resolve_identity(identity)
        entry = self._new_entry(
            memory_type=MemoryType.SQL_TEMPLATE,
            content=f"问题: {user_query}\n方言: {dialect}\nSQL: {sql}",
            payload={"question": user_query, "sql": sql, "dialect": dialect},
            confidence=0.9 if verified else 0.5,
            identity=current_identity,
            visibility=visibility,
            ttl_days=ttl_days,
        )
        await self._upsert(entry, identity=current_identity)
        return entry

    # 方法作用：保存当前用户的 SQL 纠正记录。
    # Args: self - 当前 Store；user_id - 兼容用户 ID；wrong_sql/correct_sql/feedback - 纠正内容；identity - 当前身份。
    # Returns: 已持久化的私有纠正记忆。
    async def save_correction(
        self,
        user_id: int,
        wrong_sql: str,
        correct_sql: str,
        feedback: str,
        *,
        identity: RequestIdentity | None = None,
    ) -> LongTermMemory:
        current_identity = self._resolve_identity(identity)
        self._require_same_user(user_id, current_identity)
        entry = self._new_entry(
            memory_type=MemoryType.CORRECTION,
            content=f"错误: {wrong_sql}\n正确: {correct_sql}\n原因: {feedback}",
            payload={"wrong_sql": wrong_sql, "correct_sql": correct_sql, "feedback": feedback},
            confidence=0.95,
            identity=current_identity,
            visibility="private",
            ttl_days=None,
        )
        await self._upsert(entry, identity=current_identity)
        return entry

    # 方法作用：保存当前用户偏好并阻止替其他用户写入。
    # Args: self - 当前 Store；user_id - 目标用户；preference/value - 偏好；identity - 当前身份。
    # Returns: 已持久化的私有偏好记忆。
    async def save_preference(
        self,
        user_id: int,
        preference: str,
        value: Any,
        *,
        identity: RequestIdentity | None = None,
    ) -> LongTermMemory:
        current_identity = self._resolve_identity(identity)
        self._require_same_user(user_id, current_identity)
        entry = self._new_entry(
            memory_type=MemoryType.USER_PREFERENCE,
            content=f"用户偏好: {preference} = {value}",
            payload={"preference": preference, "value": value},
            confidence=1.0,
            identity=current_identity,
            visibility="private",
            ttl_days=None,
        )
        await self._upsert(entry, identity=current_identity)
        return entry

    # 方法作用：精确读取当前身份的未过期用户偏好。
    # Args: self - 当前 Store；user_id - 兼容用户 ID；identity - 当前身份。
    # Returns: 偏好名到值的映射。
    async def get_preferences(
        self,
        user_id: int | None = None,
        *,
        identity: RequestIdentity | None = None,
    ) -> dict[str, Any]:
        current_identity = self._resolve_identity(identity)
        target_user = current_identity.user_id if user_id is None else int(user_id)
        self._require_same_user(target_user, current_identity)
        logger.debug(
            "用户偏好查询入口",
            tenant_id=current_identity.tenant_id,
            user_id=target_user,
        )
        if self._pg is not None:
            try:
                async with self._pg_identity_connection(current_identity) as connection:
                    rows = await connection.fetch(
                        "SELECT payload FROM long_term_memories "
                        "WHERE memory_type = $1 AND visibility = 'private' "
                        "AND tenant_id = $2 AND owner_user_id = $3 "
                        "AND (ttl_days IS NULL OR created_at + ttl_days * INTERVAL '1 day' > NOW())",
                        MemoryType.USER_PREFERENCE.value,
                        current_identity.tenant_id,
                        target_user,
                    )
                result = _preferences_from_rows(rows)
                logger.info("用户偏好查询完成", backend="postgres", count=len(result))
                return result
            except Exception as exc:
                logger.warning("PG 偏好查询失败，降级 VectorStore", error=str(exc), exc_info=True)
        return await self._get_prefs_from_vector(current_identity)

    # 方法作用：兼容旧测试入口，从 VectorStore 查询指定用户偏好。
    # Args: self - 当前 Store；user_id - 用户标识。
    # Returns: 偏好映射，存储故障时为空字典。
    async def _get_prefs_from_chroma(self, user_id: str) -> dict[str, Any]:
        try:
            numeric_user = int(user_id)
        except (TypeError, ValueError):
            numeric_user = 0
        identity = RequestIdentity(tenant_id=1, user_id=numeric_user, role="anonymous")
        return await self._get_prefs_from_vector(identity)

    # 方法作用：从 VectorStore 精确读取当前身份的偏好并执行 TTL 校验。
    # Args: self - 当前 Store；identity - 当前身份。
    # Returns: 偏好映射，存储故障时为空字典。
    async def _get_prefs_from_vector(self, identity: RequestIdentity) -> dict[str, Any]:
        try:
            store = await self._vector()
            entries = await store.get_by_filter({
                "resource_kind": _RESOURCE_KIND,
                "memory_type": MemoryType.USER_PREFERENCE.value,
                "visibility": "private",
                "tenant_id": identity.tenant_id,
                "owner_user_id": identity.user_id,
            })
            result: dict[str, Any] = {}
            expired_ids: list[str] = []
            for entry in entries:
                memory = _memory_from_entry(entry)
                if memory.is_expired():
                    expired_ids.append(memory.id)
                    continue
                preference = memory.payload.get("preference")
                if preference:
                    result[str(preference)] = memory.payload.get("value")
            if expired_ids:
                await self._delete_ids(expired_ids)
            logger.info("向量用户偏好查询完成", count=len(result), user_id=identity.user_id)
            return result
        except Exception as exc:
            logger.error(
                "向量用户偏好查询失败，降级为空字典",
                user_id=identity.user_id,
                error=str(exc),
                exc_info=True,
            )
            return {}

    # 方法作用：把长期记忆幂等写入 PostgreSQL 和 VectorStore。
    # Args: self - 当前 Store；entry - 待持久化记忆；identity - RLS 请求身份。
    # Returns: 无返回值；两个后端均失败时抛出异常。
    async def _upsert(
        self,
        entry: LongTermMemory,
        *,
        identity: RequestIdentity,
    ) -> None:
        logger.debug(
            "长期记忆写入入口",
            entry_id=entry.id,
            visibility=entry.visibility,
            tenant_id=entry.tenant_id,
            owner_user_id=entry.owner_user_id,
        )
        pg_ok = False
        pg_error: Exception | None = None
        if self._pg is not None:
            try:
                async with self._pg_identity_connection(identity) as connection:
                    await connection.execute(
                        """INSERT INTO long_term_memories (
                           id, memory_type, scope, visibility, tenant_id, owner_user_id,
                           content, payload, created_at, last_accessed_at,
                           access_count, confidence, ttl_days)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                           ON CONFLICT (id) DO UPDATE SET
                           content=$7, payload=$8, last_accessed_at=$10,
                           access_count=$11, confidence=$12, ttl_days=$13""",
                        entry.id,
                        entry.memory_type.value,
                        entry.scope,
                        entry.visibility,
                        entry.tenant_id,
                        entry.owner_user_id,
                        entry.content,
                        entry.payload,
                        entry.created_at,
                        entry.last_accessed_at,
                        entry.access_count,
                        entry.confidence,
                        entry.ttl_days,
                    )
                pg_ok = True
            except Exception as exc:
                pg_error = exc
                logger.warning("PG 长期记忆写入失败", error=str(exc), exc_info=True)

        vector_ok = False
        try:
            store = await self._vector()
            try:
                await store.delete_by_ids([entry.id])
            except Exception as exc:
                logger.warning("VectorStore 旧长期记忆删除失败", error=str(exc), exc_info=True)
            from src.memory.vector_store import VectorEntry

            await store.upsert([VectorEntry(
                id=entry.id,
                content=entry.content,
                metadata=_vector_metadata(entry),
            )])
            vector_ok = True
        except Exception as exc:
            logger.error("VectorStore 长期记忆写入失败", error=str(exc), exc_info=True)
            if pg_ok:
                await self._mark_pending_sync(entry.id)
            elif pg_error is not None:
                raise RuntimeError("长期记忆所有后端写入失败") from exc
            else:
                raise
        logger.info("长期记忆写入完成", entry_id=entry.id, pg=pg_ok, vector=vector_ok)

    # 方法作用：把一次召回的访问计数和时间同步回两个后端。
    # Args: self - 当前 Store；memory - 已召回记忆；identity - RLS 请求身份。
    # Returns: 无返回值。
    async def _touch(
        self,
        memory: LongTermMemory,
        *,
        identity: RequestIdentity,
    ) -> None:
        memory.touch()
        if self._pg is not None:
            try:
                async with self._pg_identity_connection(identity) as connection:
                    await connection.execute(
                        "UPDATE long_term_memories SET access_count = $2, last_accessed_at = $3 "
                        "WHERE id = $1",
                        memory.id,
                        memory.access_count,
                        memory.last_accessed_at,
                    )
            except Exception as exc:
                logger.warning("长期记忆访问状态写回 PG 失败", error=str(exc), exc_info=True)
        try:
            from src.memory.vector_store import VectorEntry

            store = await self._vector()
            await store.upsert([VectorEntry(
                id=memory.id,
                content=memory.content,
                metadata=_vector_metadata(memory),
            )])
        except Exception as exc:
            logger.warning("长期记忆访问状态写回 VectorStore 失败", error=str(exc), exc_info=True)

    # 方法作用：删除已过期记忆并同步清理 VectorStore。
    # Args: self - 当前 Store；now - 截止时间，缺省为当前 UTC。
    # Returns: 删除条目数。
    async def prune_expired(self, now: datetime | None = None) -> int:
        cutoff = now or datetime.now(timezone.utc)
        if self._pg is not None:
            try:
                async with self._pg_identity_connection(RequestIdentity.system()) as connection:
                    rows = await connection.fetch(
                        "DELETE FROM long_term_memories WHERE ttl_days IS NOT NULL "
                        "AND created_at + ttl_days * INTERVAL '1 day' <= $1 RETURNING id",
                        cutoff,
                    )
                ids = _row_ids(rows)
                if ids:
                    await (await self._vector()).delete_by_ids(ids)
                logger.info("过期长期记忆清理完成", count=len(ids))
                return len(ids)
            except Exception as exc:
                logger.error("过期长期记忆清理失败", error=str(exc), exc_info=True)
                return 0
        return await self._prune_vector_only(expired=True)

    # 方法作用：将 30 天未访问的 SQL 模板降权并同步 VectorStore metadata。
    # Args: self - 当前 Store。
    # Returns: 降权条目数。
    async def decay_old_templates(self) -> int:
        if self._pg is None:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        try:
            async with self._pg_identity_connection(RequestIdentity.system()) as connection:
                rows = await connection.fetch(
                    "UPDATE long_term_memories SET confidence = confidence * 0.5 "
                    "WHERE memory_type = $1 AND last_accessed_at < $2 AND confidence >= 0.4 "
                    "RETURNING id, confidence",
                    MemoryType.SQL_TEMPLATE.value,
                    cutoff,
                )
            store = await self._vector()
            for row in rows:
                entry = await store.get_by_id(str(row["id"]))
                if entry is None:
                    continue
                entry.metadata["confidence"] = float(row["confidence"])
                await store.upsert([entry])
            logger.info("长期记忆衰减完成", count=len(rows))
            return len(rows)
        except Exception as exc:
            logger.error("记忆衰减失败", error=str(exc), exc_info=True)
            return 0

    # 方法作用：删除低置信度且从未使用的 SQL 模板并同步 VectorStore。
    # Args: self - 当前 Store。
    # Returns: 删除条目数。
    async def prune_low_confidence(self) -> int:
        if self._pg is not None:
            try:
                async with self._pg_identity_connection(RequestIdentity.system()) as connection:
                    rows = await connection.fetch(
                        "DELETE FROM long_term_memories WHERE memory_type = $1 "
                        "AND confidence < $2 AND access_count = 0 RETURNING id",
                        MemoryType.SQL_TEMPLATE.value,
                        _MIN_CONFIDENCE,
                    )
                ids = _row_ids(rows)
                if ids:
                    await (await self._vector()).delete_by_ids(ids)
                logger.info("低置信度长期记忆清理完成", count=len(ids))
                return len(ids)
            except Exception as exc:
                logger.error("记忆清理失败", error=str(exc), exc_info=True)
                return 0
        return await self._prune_vector_only(expired=False)

    # 方法作用：消费 PG 向量补偿队列并恢复 VectorStore 中缺失的长期记忆。
    # Args: self - 当前 Store；limit - 单次最多处理条目数。
    # Returns: 成功同步并从补偿队列删除的条目数。
    async def reconcile_pending_sync(self, limit: int = 100) -> int:
        if self._pg is None:
            return 0
        batch_limit = max(1, min(int(limit), 1000))
        system_identity = RequestIdentity.system()
        try:
            async with self._pg_identity_connection(system_identity) as connection:
                rows = await connection.fetch(
                    "SELECT m.id, m.memory_type, m.scope, m.visibility, m.tenant_id, "
                    "m.owner_user_id, m.content, m.payload, m.created_at, "
                    "m.last_accessed_at, m.access_count, m.confidence, m.ttl_days "
                    "FROM pending_vector_sync p "
                    "JOIN long_term_memories m ON m.id = p.entry_id "
                    "WHERE p.operation = 'upsert' ORDER BY p.created_at LIMIT $1",
                    batch_limit,
                )
        except Exception as exc:
            logger.error("向量补偿队列读取失败", error=str(exc), exc_info=True)
            return 0
        store = await self._vector()
        synchronized = 0
        from src.memory.vector_store import VectorEntry

        for row in rows:
            entry_id = str(row["id"])
            try:
                memory = LongTermMemory.from_dict(dict(row))
                await store.delete_by_ids([entry_id])
                await store.upsert([VectorEntry(
                    id=entry_id,
                    content=memory.content,
                    metadata=_vector_metadata(memory),
                )])
                async with self._pg_identity_connection(system_identity) as connection:
                    await connection.execute(
                        "DELETE FROM pending_vector_sync WHERE entry_id = $1",
                        entry_id,
                    )
                synchronized += 1
            except Exception as exc:
                logger.error(
                    "向量补偿条目同步失败",
                    entry_id=entry_id,
                    error=str(exc),
                    exc_info=True,
                )
                try:
                    async with self._pg_identity_connection(system_identity) as connection:
                        await connection.execute(
                            "UPDATE pending_vector_sync SET retry_count = retry_count + 1, "
                            "last_error = $2, updated_at = NOW() WHERE entry_id = $1",
                            entry_id,
                            str(exc)[:500],
                        )
                except Exception:
                    logger.error("向量补偿失败状态写回失败", entry_id=entry_id, exc_info=True)
        logger.info("向量补偿队列处理完成", selected=len(rows), synchronized=synchronized)
        return synchronized

    # 方法作用：在无 PostgreSQL 时直接扫描并清理 VectorStore 生命周期失效项。
    # Args: self - 当前 Store；expired - True 清理过期，False 清理低置信度模板。
    # Returns: 删除条目数。
    async def _prune_vector_only(self, *, expired: bool) -> int:
        try:
            store = await self._vector()
            entries = await store.get_by_filter({"resource_kind": _RESOURCE_KIND}, limit=10_000)
            ids: list[str] = []
            for entry in entries:
                memory = _memory_from_entry(entry)
                should_delete = memory.is_expired() if expired else (
                    memory.memory_type == MemoryType.SQL_TEMPLATE
                    and memory.confidence < _MIN_CONFIDENCE
                    and memory.access_count == 0
                )
                if should_delete:
                    ids.append(memory.id)
            if ids:
                await store.delete_by_ids(ids)
            return len(ids)
        except Exception as exc:
            logger.error("VectorStore 长期记忆清理失败", error=str(exc), exc_info=True)
            return 0

    # 方法作用：删除指定长期记忆 ID 并保持两个后端一致。
    # Args: self - 当前 Store；ids - 待删除 ID；identity - RLS 请求身份。
    # Returns: 无返回值。
    async def _delete_ids(
        self,
        ids: list[str],
        *,
        identity: RequestIdentity,
    ) -> None:
        if not ids:
            return
        if self._pg is not None:
            try:
                async with self._pg_identity_connection(identity) as connection:
                    await connection.execute(
                        "DELETE FROM long_term_memories WHERE id = ANY($1::text[])",
                        ids,
                    )
            except Exception as exc:
                logger.warning("PG 长期记忆删除失败", error=str(exc), exc_info=True)
        await (await self._vector()).delete_by_ids(ids)

    # 方法作用：记录 PostgreSQL 已成功但 VectorStore 待补偿的条目。
    # Args: self - 当前 Store；entry_id - 条目 ID。
    # Returns: 无返回值。
    async def _mark_pending_sync(self, entry_id: str) -> None:
        if self._pg is None:
            return
        try:
            await self._pg.execute(
                "INSERT INTO pending_vector_sync (entry_id, operation, created_at) "
                "VALUES ($1, 'upsert', $2) ON CONFLICT (entry_id) DO UPDATE SET created_at = $2",
                entry_id,
                datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.error("向量补偿任务记录失败", entry_id=entry_id, error=str(exc), exc_info=True)

    # 方法作用：惰性获取当前应用 VectorStore，测试可直接注入替身。
    # Args: self - 当前 Store。
    # Returns: VectorStore 实例。
    async def _vector(self) -> Any:
        if self._vector_store is None:
            from src.memory.vector_store import get_vector_store

            self._vector_store = await get_vector_store()
        return self._vector_store

    # 方法作用：为当前 Store 的 PG 操作创建带 RLS 身份的事务连接。
    # Args: self - 当前 Store；identity - 当前请求或后台系统身份。
    # Returns: 可用于 async with 的异步上下文管理器。
    def _pg_identity_connection(self, identity: RequestIdentity) -> Any:
        from src.memory.pg_pool import pg_pool_connection

        return pg_pool_connection(
            self._pg,
            identity.tenant_id,
            identity.user_id,
            identity.role,
        )

    # 方法作用：解析调用身份，缺省读取认证 ContextVar。
    # Args: self - 当前 Store；identity - 显式身份。
    # Returns: 可用于记忆隔离的身份快照。
    def _resolve_identity(self, identity: RequestIdentity | None) -> RequestIdentity:
        if identity is not None:
            return identity
        from src.api.auth import get_current_identity

        return get_current_identity()

    # 方法作用：构造带当前身份和作用域的长期记忆。
    # Args: self - 当前 Store；业务字段、identity、visibility 和 ttl_days - 新记忆属性。
    # Returns: 新长期记忆。
    def _new_entry(
        self,
        *,
        memory_type: MemoryType,
        content: str,
        payload: dict[str, Any],
        confidence: float,
        identity: RequestIdentity,
        visibility: str,
        ttl_days: int | None,
    ) -> LongTermMemory:
        normalized = visibility.strip().lower()
        policy = self._policy
        if policy is None:
            from src.app_context import get_tenant_policy

            policy = get_tenant_policy()
        if not policy.can_write_scope(normalized, identity):
            raise PermissionError(f"无权写入 {normalized} 长期记忆")
        tenant_id = 0 if normalized == "system" else identity.tenant_id
        owner_user_id = identity.user_id if normalized == "private" else 0
        return LongTermMemory(
            id=str(uuid.uuid4()),
            memory_type=memory_type,
            scope=_scope_name(normalized, tenant_id, owner_user_id),
            content=content,
            payload=payload,
            confidence=confidence,
            ttl_days=ttl_days,
            visibility=normalized,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            created_at=datetime.now(timezone.utc),
            last_accessed_at=datetime.now(timezone.utc),
        )

    # 方法作用：生成当前身份允许读取的三个精确 VectorStore 过滤条件。
    # Args: self - 当前 Store；identity - 当前身份；memory_type - 可选类型。
    # Returns: system、tenant、private 过滤条件列表。
    def _visible_filters(
        self,
        identity: RequestIdentity,
        memory_type: MemoryType | None,
    ) -> list[dict[str, Any]]:
        base: dict[str, Any] = {"resource_kind": _RESOURCE_KIND}
        if memory_type is not None:
            base["memory_type"] = memory_type.value
        return [
            {**base, "visibility": "system"},
            {**base, "visibility": "tenant", "tenant_id": identity.tenant_id},
            {
                **base,
                "visibility": "private",
                "tenant_id": identity.tenant_id,
                "owner_user_id": identity.user_id,
            },
        ]

    # 方法作用：对 VectorStore 返回结果执行第二层身份可见性复核。
    # Args: self - 当前 Store；memory - 候选记忆；identity - 当前身份。
    # Returns: 当前身份可见返回 True。
    def _is_visible(self, memory: LongTermMemory, identity: RequestIdentity) -> bool:
        if memory.visibility == "system":
            return True
        if memory.visibility == "tenant":
            return memory.tenant_id == identity.tenant_id
        if memory.visibility == "private":
            return (
                memory.tenant_id == identity.tenant_id
                and memory.owner_user_id == identity.user_id
            )
        return False

    # 方法作用：阻止普通调用替其他用户读写私有偏好和纠正。
    # Args: self - 当前 Store；user_id - 目标用户；identity - 当前身份。
    # Returns: 无返回值，不匹配时抛出 PermissionError。
    def _require_same_user(self, user_id: int, identity: RequestIdentity) -> None:
        if int(user_id) != identity.user_id and identity.role != "super_admin":
            raise PermissionError("不能访问其他用户的长期记忆")


# 方法作用：获取当前 AppContext 独享的长期记忆 Store。
# Args: 无。
# Returns: 已绑定可选 PG Pool、VectorStore 和 TenantPolicy 的 Store。
async def get_long_term_memory_store() -> LongTermMemoryStore:
    from src.app_context import get_app_context, get_tenant_policy

    context = get_app_context()
    existing = context.get_resource(_MEMORY_RESOURCE)
    if isinstance(existing, LongTermMemoryStore):
        return existing
    pg_pool = None
    database_url = str(getattr(context.settings, "database_url", "") or "")
    if "postgres" in database_url:
        try:
            from src.memory.pg_pool import get_pg_pool

            pg_pool = await get_pg_pool()
        except Exception as exc:
            logger.warning("长期记忆 PG 不可用，使用 VectorStore", error=str(exc), exc_info=True)
    result = LongTermMemoryStore(pg_pool=pg_pool, policy=get_tenant_policy())
    return context.set_resource(_MEMORY_RESOURCE, result)


# 方法作用：把长期记忆转换为仅包含标量的跨后端 VectorStore metadata。
# Args: memory - 待转换长期记忆。
# Returns: ChromaDB、pgvector 和 Milvus 均可保存的 metadata。
def _vector_metadata(memory: LongTermMemory) -> dict[str, Any]:
    return {
        "resource_kind": _RESOURCE_KIND,
        "memory_type": memory.memory_type.value,
        "scope": memory.scope,
        "visibility": memory.visibility,
        "tenant_id": memory.tenant_id,
        "owner_user_id": memory.owner_user_id,
        "payload_json": json.dumps(memory.payload, ensure_ascii=False, default=str),
        "created_at": _iso(memory.created_at),
        "last_accessed_at": _iso(memory.last_accessed_at),
        "access_count": memory.access_count,
        "confidence": float(memory.confidence),
        "ttl_days": memory.ttl_days if memory.ttl_days is not None else -1,
    }


# 方法作用：从向量检索结果恢复长期记忆。
# Args: result - VectorSearchResult。
# Returns: LongTermMemory。
def _memory_from_vector(result: Any) -> LongTermMemory:
    return _memory_from_data(result.id, result.content, result.metadata)


# 方法作用：从精确读取的向量条目恢复长期记忆。
# Args: entry - VectorEntry。
# Returns: LongTermMemory。
def _memory_from_entry(entry: Any) -> LongTermMemory:
    return _memory_from_data(entry.id, entry.content, entry.metadata)


# 方法作用：兼容新旧 metadata 并恢复长期记忆模型。
# Args: entry_id/content/metadata - 向量条目字段。
# Returns: LongTermMemory。
def _memory_from_data(entry_id: str, content: str, metadata: dict[str, Any]) -> LongTermMemory:
    payload = metadata.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}
    payload_json = metadata.get("payload_json")
    if payload_json:
        try:
            loaded = json.loads(str(payload_json))
            payload = loaded if isinstance(loaded, dict) else payload
        except (TypeError, ValueError):
            logger.warning("长期记忆 payload_json 解析失败", entry_id=entry_id)
    ttl_raw = metadata.get("ttl_days")
    ttl_days = None if ttl_raw in (None, "", -1, "-1") else int(ttl_raw)
    return LongTermMemory(
        id=entry_id,
        memory_type=MemoryType(str(metadata.get("memory_type", "learned_pattern"))),
        scope=str(metadata.get("scope", "")),
        content=content,
        payload=payload,
        created_at=_parse_dt(metadata.get("created_at")),
        last_accessed_at=_parse_dt(metadata.get("last_accessed_at")),
        access_count=int(metadata.get("access_count", 0) or 0),
        confidence=float(metadata.get("confidence", 1.0) or 0),
        ttl_days=ttl_days,
        visibility=str(metadata.get("visibility", "private")),
        tenant_id=int(metadata.get("tenant_id", 1) or 0),
        owner_user_id=int(metadata.get("owner_user_id", 0) or 0),
    )


# 方法作用：把 PostgreSQL payload 行转换为偏好映射。
# Args: rows - 查询结果行。
# Returns: 偏好映射。
def _preferences_from_rows(rows: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for row in rows:
        payload = row["payload"] if "payload" in row else {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        if isinstance(payload, dict) and "preference" in payload:
            result[str(payload["preference"])] = payload.get("value")
    return result


# 方法作用：从 RETURNING 行读取字符串 ID。
# Args: rows - PostgreSQL 返回行。
# Returns: ID 列表。
def _row_ids(rows: Any) -> list[str]:
    return [str(row["id"]) for row in rows]


# 方法作用：构造新三级作用域的稳定 scope 字符串。
# Args: visibility - 可见性；tenant_id - 租户；owner_user_id - 所有者。
# Returns: scope 字符串。
def _scope_name(visibility: str, tenant_id: int, owner_user_id: int) -> str:
    if visibility == "system":
        return "system"
    if visibility == "tenant":
        return f"tenant:{tenant_id}"
    return f"private:{tenant_id}:{owner_user_id}"


# 方法作用：解析持久化时间并统一为 UTC。
# Args: value - datetime、ISO 字符串或空值。
# Returns: UTC datetime。
def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# 方法作用：把 datetime 规范化为 UTC ISO 字符串。
# Args: value - datetime。
# Returns: UTC ISO 字符串。
def _iso(value: datetime) -> str:
    return _parse_dt(value).isoformat()
