"""成功 SQL 驱动的知识库自进化服务。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import sqlglot
from pydantic import BaseModel
from sqlglot import exp

from src.logging_config import get_logger
from src.security.sql_execution import validate_sql
from src.security.tenant_policy import RequestIdentity

logger = get_logger(__name__)
_RESOURCE_NAME = "knowledge_evolution_service"


class KnowledgeEvolutionResult(BaseModel):
    """一次 SQL 观察的结果摘要。"""

    fingerprint: str = ""
    observation_count: int = 0
    promoted: bool = False
    memory_id: str = ""
    ignored_reason: str = ""


class KnowledgeEvolutionService:
    """聚合已验证 SQL，并在阈值后晋升为私有学习模式。"""

    # 方法作用：初始化自进化服务及可注入的长期记忆和数据库依赖。
    # Args: memory_store - 长期记忆 Store；pg_pool - 可选 PG Pool；promotion_threshold - 晋升观察次数。
    # Returns: 无返回值。
    def __init__(
        self,
        memory_store: Any,
        pg_pool: Any = None,
        promotion_threshold: int = 3,
    ) -> None:
        self.memory_store = memory_store
        self.pg_pool = pg_pool
        self.promotion_threshold = max(2, min(int(promotion_threshold), 100))
        self._local_observations: dict[tuple[int, int, str], dict[str, Any]] = {}
        self._promoted: set[tuple[int, int, str]] = set()

    # 方法作用：仅记录成功、已校验且无副作用的 SQL，并在达到阈值时晋升。
    # Args: user_query/sql/dialect - SQL 语义；execution_success/validated - 执行与校验证据；identity - 当前身份；datasource/execution_ms - 可选证据。
    # Returns: 观察次数、晋升状态和稳定指纹。
    async def observe_successful_sql(
        self,
        user_query: str,
        sql: str,
        dialect: str,
        *,
        execution_success: bool,
        validated: bool,
        identity: RequestIdentity,
        datasource: str = "",
        execution_ms: float | None = None,
    ) -> KnowledgeEvolutionResult:
        logger.debug(
            "知识库自进化观察入口",
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            execution_success=execution_success,
            validated=validated,
        )
        if not execution_success:
            return KnowledgeEvolutionResult(ignored_reason="execution_failed")
        if not validated:
            return KnowledgeEvolutionResult(ignored_reason="validation_failed")
        if not user_query.strip() or not sql.strip():
            return KnowledgeEvolutionResult(ignored_reason="empty_input")
        validation = validate_sql(sql, dialect)
        if not validation.valid:
            logger.warning("知识库自进化安全校验拒绝", dialect=dialect)
            return KnowledgeEvolutionResult(ignored_reason="validation_failed")
        try:
            tree = sqlglot.parse_one(validation.sql, read=dialect or None)
        except sqlglot.errors.ParseError:
            return KnowledgeEvolutionResult(ignored_reason="parse_failed")
        if not isinstance(tree, (exp.Select, exp.Union)):
            return KnowledgeEvolutionResult(ignored_reason="not_query")
        normalized_sql = tree.sql(dialect=dialect or None, pretty=False)
        fingerprint = hashlib.sha256(
            f"{dialect.strip().lower()}\n{normalized_sql}".encode("utf-8"),
        ).hexdigest()
        key = (int(identity.tenant_id), int(identity.user_id), fingerprint)
        count = await self._record_observation(
            key=key,
            fingerprint=fingerprint,
            user_query=user_query,
            sql=normalized_sql,
            dialect=dialect,
            datasource=datasource,
            execution_ms=execution_ms,
            identity=identity,
        )
        result = KnowledgeEvolutionResult(fingerprint=fingerprint, observation_count=count)
        if count < self.promotion_threshold or key in self._promoted:
            logger.info("知识库自进化观察完成", fingerprint=fingerprint, observation_count=count, promoted=False)
            return result
        try:
            memory = await self.memory_store.save_learned_pattern(
                user_query,
                normalized_sql,
                dialect,
                fingerprint=fingerprint,
                observation_count=count,
                identity=identity,
                visibility="private",
            )
        except Exception as exc:
            logger.error("知识库自进化晋升失败", fingerprint=fingerprint, error=str(exc), exc_info=True)
            return result
        self._promoted.add(key)
        result.promoted = True
        result.memory_id = str(getattr(memory, "id", ""))
        logger.info("知识库自进化晋升完成", fingerprint=fingerprint, observation_count=count)
        return result

    # 方法作用：把一次观察幂等累加到 PostgreSQL 或进程级回退计数器。
    # Args: key/fingerprint - 稳定身份和 SQL 指纹；SQL 证据与 identity - 观察内容。
    # Returns: 当前累计观察次数。
    async def _record_observation(
        self,
        *,
        key: tuple[int, int, str],
        fingerprint: str,
        user_query: str,
        sql: str,
        dialect: str,
        datasource: str,
        execution_ms: float | None,
        identity: RequestIdentity,
    ) -> int:
        if self.pg_pool is None:
            record = self._local_observations.setdefault(key, {"count": 0})
            record["count"] += 1
            return int(record["count"])
        try:
            async with self.memory_store._pg_identity_connection(identity) as connection:  # noqa: SLF001
                row = await connection.fetchrow(
                    """INSERT INTO knowledge_sql_observations (
                       tenant_id, owner_user_id, fingerprint, dialect, datasource,
                       user_query, normalized_sql, observation_count, last_execution_ms,
                       first_seen_at, last_seen_at
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,1,$8,$9,$9)
                    ON CONFLICT (tenant_id, owner_user_id, fingerprint) DO UPDATE SET
                       observation_count = knowledge_sql_observations.observation_count + 1,
                       user_query = EXCLUDED.user_query,
                       normalized_sql = EXCLUDED.normalized_sql,
                       last_execution_ms = EXCLUDED.last_execution_ms,
                       last_seen_at = EXCLUDED.last_seen_at
                    RETURNING observation_count""",
                    identity.tenant_id,
                    identity.user_id,
                    fingerprint,
                    dialect,
                    datasource,
                    user_query,
                    sql,
                    execution_ms,
                    datetime.now(timezone.utc),
                )
            return int(row["observation_count"])
        except Exception as exc:
            logger.error("知识库自进化观察写入失败", fingerprint=fingerprint, error=str(exc), exc_info=True)
            raise


# 方法作用：获取当前 AppContext 绑定的自进化服务。
# Args: 无。
# Returns: 当前应用独享的 KnowledgeEvolutionService。
async def get_knowledge_evolution_service() -> KnowledgeEvolutionService:
    from src.app_context import get_app_context
    from src.config import get_settings
    from src.memory.long_term_store import get_long_term_memory_store

    context = get_app_context()

    async def _factory() -> KnowledgeEvolutionService:
        store = await get_long_term_memory_store()
        settings = get_settings()
        threshold = int(getattr(settings, "knowledge_evolution_promotion_threshold", 3))
        return KnowledgeEvolutionService(
            memory_store=store,
            pg_pool=store.pg_pool,
            promotion_threshold=threshold,
        )

    return await context.get_or_create_async(_RESOURCE_NAME, _factory)
