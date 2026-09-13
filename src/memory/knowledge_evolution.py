"""成功 SQL 驱动的知识库自进化服务。"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import sqlglot
from pydantic import BaseModel
from sqlglot import exp

from src.logging_config import get_logger
from src.security.sql_execution import normalize_sql_dialect, validate_sql
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
        self._local_claims: dict[tuple[int, int, str], str] = {}
        self._local_claim_lock = asyncio.Lock()

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
        # sqlglot 不认项目里的方言别名（mssql 实为 tsql），必须先规范化，
        # 否则 parse_one 抛 ValueError 而非 ParseError，异常逃逸到调用方。
        parse_dialect = normalize_sql_dialect(dialect) or None
        try:
            tree = sqlglot.parse_one(validation.sql, read=parse_dialect)
        except sqlglot.errors.ParseError:
            return KnowledgeEvolutionResult(ignored_reason="parse_failed")
        if not isinstance(tree, (exp.Select, exp.Union)):
            return KnowledgeEvolutionResult(ignored_reason="not_query")
        normalized_sql = tree.sql(dialect=parse_dialect, pretty=False)
        fingerprint = hashlib.sha256(
            f"{dialect.strip().lower()}\n{normalized_sql}".encode("utf-8"),
        ).hexdigest()
        key = (int(identity.tenant_id), int(identity.user_id), fingerprint)
        count, already_promoted, memory_id = await self._record_observation(
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
        if already_promoted:
            result.promoted = True
            result.memory_id = memory_id
            logger.info("知识库自进化观察完成", fingerprint=fingerprint, observation_count=count, promoted=True, already_promoted=True)
            return result
        if count < self.promotion_threshold:
            logger.info("知识库自进化观察完成", fingerprint=fingerprint, observation_count=count, promoted=False)
            return result
        claim_token = await self._claim_promotion(key, identity)
        if not claim_token:
            logger.info("知识库自进化晋升跳过", fingerprint=fingerprint, reason="another_worker_claimed")
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
            await self._safe_release_promotion_claim(key, identity, claim_token, str(exc))
            logger.error("知识库自进化晋升失败", fingerprint=fingerprint, error=str(exc), exc_info=True)
            return result
        result.memory_id = str(getattr(memory, "id", ""))
        try:
            await self._complete_promotion(key, identity, result.memory_id, claim_token)
        except Exception as exc:
            # VectorStore 已经写入时仍释放声明，稳定记忆 ID 使下一次重试幂等。
            await self._safe_release_promotion_claim(key, identity, claim_token, str(exc))
            logger.error("知识库自进化状态写回失败", fingerprint=fingerprint, error=str(exc), exc_info=True)
            return result
        self._promoted.add(key)
        result.promoted = True
        logger.info("知识库自进化晋升完成", fingerprint=fingerprint, observation_count=count)
        return result

    # 方法作用：把一次观察幂等累加到 PostgreSQL 或进程级回退计数器。
    # Args: key/fingerprint - 稳定身份和 SQL 指纹；SQL 证据与 identity - 观察内容。
    # Returns: 当前累计观察次数、是否已经晋升和已写入的记忆 ID。
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
    ) -> tuple[int, bool, str]:
        if self.pg_pool is None:
            record = self._local_observations.setdefault(key, {"count": 0})
            record["count"] += 1
            return int(record["count"]), bool(record.get("promoted")), str(record.get("memory_id", ""))
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
                    RETURNING observation_count, promoted_at, memory_id""",
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
            return (
                int(row["observation_count"]),
                _row_value(row, "promoted_at") is not None,
                str(_row_value(row, "memory_id") or ""),
            )
        except Exception as exc:
            logger.error("知识库自进化观察写入失败", fingerprint=fingerprint, error=str(exc), exc_info=True)
            raise

    # 方法作用：以数据库条件更新原子晋升声明，确保多 Worker 只允许一个执行者。
    # Args: key - 租户、用户和 SQL 指纹；identity - RLS 身份。
    # Returns: 当前 Worker 的声明令牌；未取得声明时返回空字符串。
    async def _claim_promotion(self, key: tuple[int, int, str], identity: RequestIdentity) -> str:
        claim_token = uuid4().hex
        if self.pg_pool is None:
            async with self._local_claim_lock:
                if key in self._promoted or key in self._local_claims:
                    return ""
                self._local_claims[key] = claim_token
                return claim_token
        _, _, fingerprint = key
        async with self.memory_store._pg_identity_connection(identity) as connection:  # noqa: SLF001
            row = await connection.fetchrow(
                """UPDATE knowledge_sql_observations
                   SET promotion_claimed_at = NOW(), promotion_claim_token = $5,
                       last_promotion_error = ''
                 WHERE tenant_id=$1 AND owner_user_id=$2 AND fingerprint=$3
                   AND observation_count >= $4 AND promoted_at IS NULL
                   AND (promotion_claimed_at IS NULL
                        OR promotion_claimed_at < NOW() - INTERVAL '10 minutes')
                 RETURNING fingerprint""",
                identity.tenant_id,
                identity.user_id,
                fingerprint,
                self.promotion_threshold,
                claim_token,
            )
        return claim_token if row is not None else ""

    # 方法作用：将晋升结果和稳定记忆 ID 持久化，并清除进行中的声明。
    # Args: key - 租户、用户和 SQL 指纹；identity - RLS 身份；memory_id/claim_token - 记忆 ID 与声明令牌。
    # Returns: 无返回值。
    async def _complete_promotion(
        self,
        key: tuple[int, int, str],
        identity: RequestIdentity,
        memory_id: str,
        claim_token: str,
    ) -> None:
        if self.pg_pool is None:
            async with self._local_claim_lock:
                if self._local_claims.get(key) != claim_token:
                    raise RuntimeError("知识库自进化晋升声明已失效")
                record = self._local_observations.setdefault(key, {"count": 0})
                record["promoted"] = True
                record["memory_id"] = memory_id
                self._local_claims.pop(key, None)
            return
        _, _, fingerprint = key
        async with self.memory_store._pg_identity_connection(identity) as connection:  # noqa: SLF001
            status = await connection.execute(
                """UPDATE knowledge_sql_observations
                   SET promoted_at = NOW(), memory_id = $4,
                       promotion_claimed_at = NULL, promotion_claim_token = '',
                       last_promotion_error = ''
                 WHERE tenant_id=$1 AND owner_user_id=$2 AND fingerprint=$3
                   AND promoted_at IS NULL AND promotion_claim_token=$5""",
                identity.tenant_id,
                identity.user_id,
                fingerprint,
                memory_id,
                claim_token,
            )
        if not str(status).endswith(" 1"):
            raise RuntimeError("知识库自进化晋升声明已失效")

    # 方法作用：释放失败晋升的声明并保留可重试的错误摘要。
    # Args: key - 租户、用户和 SQL 指纹；identity - RLS 身份；claim_token - 声明令牌；error - 错误摘要。
    # Returns: 无返回值。
    async def _release_promotion_claim(
        self,
        key: tuple[int, int, str],
        identity: RequestIdentity,
        claim_token: str,
        error: str,
    ) -> None:
        if self.pg_pool is None:
            async with self._local_claim_lock:
                if self._local_claims.get(key) == claim_token:
                    self._local_claims.pop(key, None)
            return
        _, _, fingerprint = key
        async with self.memory_store._pg_identity_connection(identity) as connection:  # noqa: SLF001
            await connection.execute(
                """UPDATE knowledge_sql_observations
                   SET promotion_claimed_at = NULL, promotion_claim_token = '',
                       last_promotion_error = $4
                 WHERE tenant_id=$1 AND owner_user_id=$2 AND fingerprint=$3
                   AND promoted_at IS NULL AND promotion_claim_token=$5""",
                identity.tenant_id,
                identity.user_id,
                fingerprint,
                str(error or "")[:500],
                claim_token,
            )

    # 方法作用：尽力释放晋升声明，避免清理故障覆盖原始晋升异常。
    # Args: key - 租户、用户和 SQL 指纹；identity - RLS 身份；claim_token - 声明令牌；error - 错误摘要。
    # Returns: 无返回值。
    async def _safe_release_promotion_claim(
        self,
        key: tuple[int, int, str],
        identity: RequestIdentity,
        claim_token: str,
        error: str,
    ) -> None:
        try:
            await self._release_promotion_claim(key, identity, claim_token, error)
        except Exception as release_exc:
            logger.error(
                "知识库自进化声明释放失败",
                fingerprint=key[2],
                error=str(release_exc),
                exc_info=True,
            )


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


# 方法作用：兼容 asyncpg Record 和测试字典读取可选状态字段。
# Args: row - 数据库返回行；key - 字段名。
# Returns: 字段值，不存在时返回 None。
def _row_value(row: Any, key: str) -> Any:
    try:
        getter = getattr(row, "get", None)
        return getter(key) if getter is not None else row[key]
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
