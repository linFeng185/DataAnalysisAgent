"""7.3 LongTermMemoryStore — ChromaDB + PostgreSQL 双写长期记忆。

依据: SPEC §3.8.3 长期记忆
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from src.logging_config import get_logger
from src.memory.models import LongTermMemory, MemoryType

logger = get_logger(__name__)


class LongTermMemoryStore:
    """7.3.3 长期记忆写入与检索。VectorStore 抽象 + PG 双写。"""

    def __init__(self, pg_pool=None):
        self._pg = pg_pool

    # ── 检索 ─────────────────────────────────────────

    async def search(
        self, query: str, memory_type: MemoryType | None = None, top_k: int = 5,
    ) -> list[LongTermMemory]:
        """7.3.4 语义检索 + 置信度过滤。"""
        try:
            from src.memory.vector_store import get_vector_store
            store = await get_vector_store()
            filters = {"not:memory_type": ""} if not memory_type else {"memory_type": memory_type.value}
            results = await store.search(query, top_k=top_k, filters=filters)

            memories: list[LongTermMemory] = []
            for r in results:
                m = r.metadata
                memories.append(LongTermMemory(
                    id=r.id, memory_type=MemoryType(m.get("memory_type", "learned_pattern")),
                    scope=m.get("scope", ""), content=r.content,
                    payload=m.get("payload", {}),
                    created_at=_parse_dt(m.get("created_at")),
                    last_accessed_at=_parse_dt(m.get("last_accessed_at")),
                    access_count=m.get("access_count", 0),
                    confidence=m.get("confidence", 1.0),
                    ttl_days=m.get("ttl_days")))
            for m in memories:
                m.touch()
            return memories
        except Exception as e:
            logger.error("长期记忆检索失败", error=str(e), exc_info=True)
            return []

    # ── 写入 ─────────────────────────────────────────

    async def save_sql_template(
        self, user_query: str, sql: str, dialect: str, verified: bool = False,
    ) -> LongTermMemory:
        """7.3.5 保存 SQL 模板。"""
        entry = LongTermMemory(
            id=str(uuid.uuid4()), memory_type=MemoryType.SQL_TEMPLATE,
            scope="project:default",
            content=f"问题: {user_query}\n方言: {dialect}\nSQL: {sql}",
            payload={"question": user_query, "sql": sql, "dialect": dialect},
            confidence=0.9 if verified else 0.5,
        )
        await self._upsert(entry)
        return entry

    async def save_correction(
        self, user_id: str, wrong_sql: str, correct_sql: str, feedback: str,
    ) -> LongTermMemory:
        """7.3.6 用户纠正。"""
        entry = LongTermMemory(
            id=str(uuid.uuid4()), memory_type=MemoryType.CORRECTION,
            scope=f"user:{user_id}",
            content=f"错误: {wrong_sql}\n正确: {correct_sql}\n原因: {feedback}",
            payload={"wrong_sql": wrong_sql, "correct_sql": correct_sql, "feedback": feedback},
            confidence=0.95,
        )
        await self._upsert(entry)
        return entry

    async def save_preference(
        self, user_id: str, preference: str, value: Any,
    ) -> LongTermMemory:
        """7.3.7 用户偏好。"""
        entry = LongTermMemory(
            id=str(uuid.uuid4()), memory_type=MemoryType.USER_PREFERENCE,
            scope=f"user:{user_id}",
            content=f"用户偏好: {preference} = {value}",
            payload={"preference": preference, "value": value},
            confidence=1.0,
        )
        await self._upsert(entry)
        return entry

    async def get_preferences(self, user_id: str) -> dict[str, Any]:
        """7.3.8 获取用户所有偏好 (PG 精确查询 + ChromaDB 回退)。"""
        logger.debug("用户偏好查询入口", user_id=user_id)
        if self._pg:
            try:
                rows = await self._pg.fetch(
                    "SELECT payload FROM long_term_memories "
                    "WHERE memory_type = $1 AND scope = $2",
                    MemoryType.USER_PREFERENCE.value, f"user:{user_id}",
                )
                result = {
                    r["payload"]["preference"]: r["payload"]["value"]
                    for r in rows if "payload" in r
                }
                logger.info("用户偏好查询完成", user_id=user_id, backend="postgres", count=len(result))
                return result
            except Exception as e:
                logger.warning("PG 偏好查询失败，降级 ChromaDB", error=str(e), exc_info=True)
        result = await self._get_prefs_from_chroma(user_id)
        logger.info("用户偏好查询完成", user_id=user_id, backend="vector", count=len(result))
        return result

    # 方法作用：从向量存储加载用户偏好，并在可恢复存储故障时降级为空字典。
    # Args: user_id - 当前用户标识。
    # Returns: 用户偏好映射，向量存储不可用时返回空字典。
    async def _get_prefs_from_chroma(self, user_id: str) -> dict[str, Any]:
        logger.debug("向量用户偏好查询入口", user_id=user_id)
        try:
            from src.memory.vector_store import get_vector_store
            store = await get_vector_store()
            results = await store.get_by_filter({
                "memory_type": MemoryType.USER_PREFERENCE.value,
                "scope": f"user:{user_id}"})
            prefs: dict[str, Any] = {}
            for r in results:
                p = r.metadata.get("payload", {})
                if "preference" in p:
                    prefs[p["preference"]] = p["value"]
            logger.info("向量用户偏好查询完成", user_id=user_id, count=len(prefs))
            return prefs
        except Exception as exc:
            logger.error(
                "向量用户偏好查询失败，降级为空字典",
                user_id=user_id,
                error=str(exc),
                exc_info=True,
            )
            return {}

    # ── 写入底层 ─────────────────────────────────────

    async def _upsert(self, entry: LongTermMemory) -> None:
        """7.3.9 幂等写入 ChromaDB + PG。PG 成功+ChromaDB 失败 → 补偿重试。"""
        meta = entry.to_dict()

        pg_ok = False
        if self._pg:
            try:
                await self._pg.execute(
                    """INSERT INTO long_term_memories (id, memory_type, scope,
                       content, payload, created_at, last_accessed_at,
                       access_count, confidence, ttl_days)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                       ON CONFLICT (id) DO UPDATE SET
                       content=$4, payload=$5, last_accessed_at=$7,
                       access_count=$8, confidence=$9""",
                    entry.id, entry.memory_type.value, entry.scope, entry.content,
                    entry.payload, entry.created_at, entry.last_accessed_at,
                    entry.access_count, entry.confidence, entry.ttl_days,
                )
                pg_ok = True
            except Exception as e:
                logger.warning("PG 写入失败", error=str(e))

        from src.memory.vector_store import get_vector_store
        store = await get_vector_store()
        try:
            try:
                await store.delete_by_ids([entry.id])
            except Exception as exc:
                logger.warning(
                    "VectorStore 旧向量删除失败，继续幂等写入",
                    entry_id=entry.id,
                    error=str(exc),
                    exc_info=True,
                )
            from src.memory.vector_store import VectorEntry
            await store.upsert([VectorEntry(id=entry.id, content=entry.content, metadata=meta)])
        except Exception as e:
            logger.error("VectorStore 写入失败", error=str(e))
            if pg_ok and self._pg:
                await self._mark_pending_sync(entry.id)

    async def _mark_pending_sync(self, entry_id: str) -> None:
        """7.3.11 记录待补偿的向量同步。"""
        try:
            await self._pg.execute(
                "INSERT INTO pending_vector_sync (entry_id, created_at) VALUES ($1, $2)",
                entry_id, datetime.now(),
            )
        except Exception as exc:
            logger.error(
                "向量补偿任务记录失败",
                entry_id=entry_id,
                error=str(exc),
                exc_info=True,
            )

    # ── 记忆维护 ─────────────────────────────────────

    async def decay_old_templates(self) -> int:
        """7.4.2 30 天未使用的 SQL 模板降置信度。"""
        if not self._pg:
            return 0
        cutoff = datetime.now() - timedelta(days=30)
        try:
            result = await self._pg.execute(
                """UPDATE long_term_memories SET confidence = confidence * 0.5
                   WHERE memory_type = $1 AND last_accessed_at < $2 AND confidence >= 0.4""",
                MemoryType.SQL_TEMPLATE.value, cutoff,
            )
            return int(str(result).split()[-1]) if result else 0
        except Exception as e:
            logger.error("记忆衰减失败", error=str(e), exc_info=True)
            return 0

    async def prune_low_confidence(self) -> int:
        """7.4.3 删除低质量自动模板。"""
        if not self._pg:
            return 0
        try:
            result = await self._pg.execute(
                """DELETE FROM long_term_memories
                   WHERE memory_type = $1 AND confidence < 0.3 AND access_count = 0""",
                MemoryType.SQL_TEMPLATE.value,
            )
            return int(str(result).split()[-1]) if result else 0
        except Exception as e:
            logger.error("记忆清理失败", error=str(e), exc_info=True)
            return 0


def _parse_dt(val: Any) -> datetime:
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        return datetime.fromisoformat(val)
    return datetime.now()
