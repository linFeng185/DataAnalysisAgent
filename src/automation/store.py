"""自动化任务、运行记录和站内通知 PostgreSQL 存储。"""

from __future__ import annotations

import calendar
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from src.automation.models import ScheduleDefinition
from src.logging_config import get_logger

logger = get_logger(__name__)
_FREQUENCY_SECONDS = {
    "hourly": 60 * 60,
    "daily": 24 * 60 * 60,
    "weekly": 7 * 24 * 60 * 60,
    "monthly": 30 * 24 * 60 * 60,
}


# 方法作用：按固定频率计算严格晚于基准时间的下一次运行时间。
# Args: frequency - hourly/daily/weekly/monthly；base - 基准 UTC 时间。
# Returns: 下一次 UTC 运行时间。
def calculate_next_run(frequency: str, base: datetime | None = None) -> datetime:
    if frequency not in _FREQUENCY_SECONDS:
        raise ValueError(f"不支持的执行频率: {frequency}")
    origin = base or datetime.now(timezone.utc)
    if origin.tzinfo is None:
        origin = origin.replace(tzinfo=timezone.utc)
    if frequency != "monthly":
        return origin + timedelta(seconds=_FREQUENCY_SECONDS[frequency])
    next_year = origin.year + (1 if origin.month == 12 else 0)
    next_month = 1 if origin.month == 12 else origin.month + 1
    day = min(origin.day, calendar.monthrange(next_year, next_month)[1])
    return origin.replace(year=next_year, month=next_month, day=day)


class AutomationStore:
    """通过 RLS 身份连接持久化自动化资源。"""

    # 方法作用：保存可选注入 PG Pool，生产缺省使用共享池。
    # Args: self - Store；pg_pool - 可选 asyncpg Pool。
    # Returns: 无返回值。
    def __init__(self, pg_pool: Any | None = None) -> None:
        self._pg_pool = pg_pool

    # 方法作用：创建当前用户拥有的只读自动化任务。
    # Args: self - Store；identity - 请求身份；request - 创建请求；dialect - 真实方言。
    # Returns: 新任务定义。
    async def create(self, identity: Any, request: Any, dialect: str) -> ScheduleDefinition:
        schedule_id = str(uuid4())
        now = datetime.now(timezone.utc)
        next_run = calculate_next_run(request.frequency, now)
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(
            pool, identity.tenant_id, identity.user_id, identity.role,
        ) as connection:
            row = await connection.fetchrow(
                "INSERT INTO analysis_schedules "
                "(id, tenant_id, user_id, name, kind, datasource, sql_text, dialect, "
                "frequency, threshold_pct, channels, recipient_email, enabled, next_run_at) "
                "VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12,TRUE,$13) "
                "RETURNING *",
                schedule_id, identity.tenant_id, identity.user_id,
                request.name, request.kind, request.datasource, request.sql, dialect,
                request.frequency, request.threshold_pct,
                json.dumps(request.channels, ensure_ascii=False),
                request.recipient_email, next_run,
            )
        logger.info(
            "自动化任务创建完成",
            schedule_id=schedule_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            kind=request.kind,
        )
        return self._to_schedule(row)

    # 方法作用：列出当前身份可见的任务，管理员可见本租户全部任务。
    # Args: self - Store；identity - 请求身份。
    # Returns: 按创建时间倒序的任务列表。
    async def list_for_identity(self, identity: Any) -> list[ScheduleDefinition]:
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(
            pool, identity.tenant_id, identity.user_id, identity.role,
        ) as connection:
            rows = await connection.fetch(
                "SELECT s.*, u.role AS user_role FROM analysis_schedules s "
                "JOIN users u ON u.id=s.user_id "
                "WHERE s.tenant_id=$1 AND ($2 IN ('super_admin','tenant_admin') OR s.user_id=$3) "
                "ORDER BY s.created_at DESC",
                identity.tenant_id, identity.role, identity.user_id,
            )
        return [self._to_schedule(row) for row in rows]

    # 方法作用：按身份读取单个可管理任务。
    # Args: self - Store；schedule_id - UUID；identity - 请求身份。
    # Returns: 任务定义；不可见或不存在返回 None。
    async def get_for_identity(self, schedule_id: str, identity: Any) -> ScheduleDefinition | None:
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(
            pool, identity.tenant_id, identity.user_id, identity.role,
        ) as connection:
            row = await connection.fetchrow(
                "SELECT s.*, u.role AS user_role FROM analysis_schedules s "
                "JOIN users u ON u.id=s.user_id WHERE s.id=$1::uuid AND s.tenant_id=$2 "
                "AND u.is_active=TRUE "
                "AND ($3 IN ('super_admin','tenant_admin') OR s.user_id=$4)",
                schedule_id, identity.tenant_id, identity.role, identity.user_id,
            )
        return self._to_schedule(row) if row else None

    # 方法作用：删除当前身份可管理的任务并级联清理运行和通知。
    # Args: self - Store；schedule_id - UUID；identity - 请求身份。
    # Returns: 实际删除时返回 True。
    async def delete(self, schedule_id: str, identity: Any) -> bool:
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(
            pool, identity.tenant_id, identity.user_id, identity.role,
        ) as connection:
            result = await connection.execute(
                "DELETE FROM analysis_schedules WHERE id=$1::uuid AND tenant_id=$2 "
                "AND ($3 IN ('super_admin','tenant_admin') OR user_id=$4)",
                schedule_id, identity.tenant_id, identity.role, identity.user_id,
            )
        return str(result).endswith(" 1")

    # 方法作用：用 SKIP LOCKED 原子认领到期任务并提前推进下一运行时间。
    # Args: self - Store；limit - 单轮最大任务数。
    # Returns: 本实例独占认领的任务列表。
    async def list_due(self, limit: int = 100) -> list[ScheduleDefinition]:
        bounded_limit = max(1, min(int(limit), 500))
        logger.info("自动化到期任务认领入口", limit=bounded_limit)
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, 0, 0, "super_admin") as connection:
            rows = await connection.fetch(
                "WITH due AS ("
                " SELECT due_schedule.id FROM analysis_schedules due_schedule"
                " JOIN users due_user ON due_user.id=due_schedule.user_id"
                " WHERE due_schedule.enabled=TRUE AND due_schedule.next_run_at<=NOW()"
                " AND due_user.is_active=TRUE ORDER BY due_schedule.next_run_at"
                " FOR UPDATE OF due_schedule SKIP LOCKED LIMIT $1"
                ") UPDATE analysis_schedules s SET last_run_at=NOW(), next_run_at="
                " CASE s.frequency WHEN 'hourly' THEN NOW()+INTERVAL '1 hour'"
                " WHEN 'daily' THEN NOW()+INTERVAL '1 day'"
                " WHEN 'weekly' THEN NOW()+INTERVAL '7 days'"
                " ELSE date_trunc('month', s.next_run_at + INTERVAL '1 month') "
                " + (LEAST(EXTRACT(DAY FROM s.next_run_at)::int, "
                "EXTRACT(DAY FROM (date_trunc('month', s.next_run_at + "
                "INTERVAL '2 months') - INTERVAL '1 day'))::int) - 1) * INTERVAL '1 day' "
                " + s.next_run_at::time END"
                " FROM due, users u WHERE s.id=due.id AND u.id=s.user_id"
                " RETURNING s.*, u.role AS user_role",
                bounded_limit,
            )
        schedules = [self._to_schedule(row) for row in rows]
        logger.info("自动化到期任务认领完成", claimed_count=len(schedules))
        return schedules

    # 方法作用：读取指定任务最近一次成功运行的 JSONB 结果作为洞察基线。
    # Args: self - Store；schedule_id - UUID。
    # Returns: 最近成功 payload；不存在返回 None。
    async def latest_success_payload(self, schedule_id: str) -> dict[str, Any] | None:
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, 0, 0, "super_admin") as connection:
            value = await connection.fetchval(
                "SELECT result_payload FROM analysis_schedule_runs "
                "WHERE schedule_id=$1::uuid AND status='success' "
                "ORDER BY started_at DESC LIMIT 1",
                schedule_id,
            )
        return dict(value) if value else None

    # 方法作用：同事务写入成功运行和可选站内通知。
    # Args: self - Store；schedule - 任务；payload - 运行结果；notification - 可选通知。
    # Returns: 无返回值。
    async def record_success(
        self,
        schedule: ScheduleDefinition,
        payload: dict[str, Any],
        notification: dict[str, Any] | None,
    ) -> None:
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, 0, 0, "super_admin") as connection:
            async with connection.transaction():
                run_id = str(uuid4())
                await connection.execute(
                    "INSERT INTO analysis_schedule_runs "
                    "(id,schedule_id,tenant_id,user_id,status,result_payload,finished_at) "
                    "VALUES ($1::uuid,$2::uuid,$3,$4,'success',$5::jsonb,NOW())",
                    run_id, schedule.id, schedule.tenant_id, schedule.user_id,
                    json.dumps(payload, ensure_ascii=False),
                )
                if notification is not None:
                    await connection.execute(
                        "INSERT INTO analysis_notifications "
                        "(id,schedule_id,run_id,tenant_id,user_id,kind,title,body) "
                        "VALUES ($1::uuid,$2::uuid,$3::uuid,$4,$5,$6,$7,$8)",
                        str(uuid4()), schedule.id, run_id, schedule.tenant_id,
                        schedule.user_id, notification["kind"],
                        notification["title"], notification["body"],
                    )

    # 方法作用：写入失败运行摘要，不保存底层连接串或凭证。
    # Args: self - Store；schedule - 任务；error - 截断后的错误摘要。
    # Returns: 无返回值。
    async def record_failure(self, schedule: ScheduleDefinition, error: str) -> None:
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, 0, 0, "super_admin") as connection:
            await connection.execute(
                "INSERT INTO analysis_schedule_runs "
                "(id,schedule_id,tenant_id,user_id,status,error_message,finished_at) "
                "VALUES ($1::uuid,$2::uuid,$3,$4,'failed',$5,NOW())",
                str(uuid4()), schedule.id, schedule.tenant_id, schedule.user_id,
                error[:500],
            )

    # 方法作用：分页列出当前用户站内通知。
    # Args: self - Store；identity - 请求身份；limit - 数量上限。
    # Returns: 最新通知字典列表。
    async def list_notifications(self, identity: Any, limit: int = 50) -> list[dict[str, Any]]:
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(
            pool, identity.tenant_id, identity.user_id, identity.role,
        ) as connection:
            rows = await connection.fetch(
                "SELECT id::text,schedule_id::text,kind,title,body,is_read,created_at "
                "FROM analysis_notifications WHERE tenant_id=$1 AND user_id=$2 "
                "ORDER BY created_at DESC LIMIT $3",
                identity.tenant_id, identity.user_id, max(1, min(int(limit), 100)),
            )
        return [dict(row) for row in rows]

    # 方法作用：惰性获取共享 PostgreSQL Pool。
    # Args: self - Store。
    # Returns: asyncpg Pool。
    async def _pool(self):
        if self._pg_pool is None:
            from src.memory.pg_pool import get_pg_pool

            self._pg_pool = await get_pg_pool()
        return self._pg_pool

    # 方法作用：把 asyncpg Record 转换为调度领域模型。
    # Args: row - 数据库记录。
    # Returns: ScheduleDefinition。
    @staticmethod
    def _to_schedule(row: Any) -> ScheduleDefinition:
        return ScheduleDefinition(
            id=str(row["id"]),
            tenant_id=int(row["tenant_id"]),
            user_id=int(row["user_id"]),
            user_role=str(row.get("user_role", "analyst") or "analyst"),
            name=str(row["name"]),
            kind=str(row["kind"]),
            datasource=str(row["datasource"]),
            sql=str(row["sql_text"]),
            dialect=str(row["dialect"]),
            frequency=str(row["frequency"]),
            threshold_pct=float(row["threshold_pct"]),
            channels=list(row["channels"] or ["in_app"]),
            recipient_email=str(row["recipient_email"] or ""),
            enabled=bool(row["enabled"]),
            next_run_at=row["next_run_at"],
            last_run_at=row["last_run_at"],
        )


# 方法作用：获取当前 AppContext 共享的 AutomationStore。
# Args: 无。
# Returns: AutomationStore 单例。
def get_automation_store() -> AutomationStore:
    from src.app_context import get_app_context

    return get_app_context().get_or_create("automation_store", AutomationStore)
