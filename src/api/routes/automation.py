"""主动洞察、定时报告和站内通知 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.api.auth import get_current_identity
from src.api.schemas import AutomationScheduleCreateRequest
from src.app_context import get_tenant_policy
from src.automation.models import ScheduleDefinition
from src.automation.runner import get_scheduled_analysis_runner
from src.automation.store import get_automation_store
from src.datasource.registry import get_registry
from src.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()


# 方法作用：把内部调度领域模型转换为不暴露租户内部字段的 API 字典。
# Args: schedule - 调度领域模型。
# Returns: 可 JSON 序列化的任务摘要。
def _schedule_response(schedule: ScheduleDefinition) -> dict[str, Any]:
    return {
        "id": schedule.id,
        "name": schedule.name,
        "kind": schedule.kind,
        "datasource": schedule.datasource,
        "sql": schedule.sql,
        "dialect": schedule.dialect,
        "frequency": schedule.frequency,
        "threshold_pct": schedule.threshold_pct,
        "channels": list(schedule.channels),
        "recipient_email": schedule.recipient_email,
        "enabled": schedule.enabled,
        "next_run_at": schedule.next_run_at,
        "last_run_at": schedule.last_run_at,
    }


# 方法作用：在创建任务前校验数据源可见性、邮件参数和单条只读 SQL。
# Args: identity - 当前请求身份；request - 自动化任务创建请求。
# Returns: 数据源真实方言。
async def _validate_schedule_request(
    identity: Any,
    request: AutomationScheduleCreateRequest,
) -> str:
    logger.info(
        "自动化任务创建校验入口",
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        datasource=request.datasource,
        kind=request.kind,
    )
    if "email" in request.channels and not request.recipient_email.strip():
        raise HTTPException(422, "选择 email 渠道时必须填写收件邮箱")
    registry = get_registry()
    try:
        datasource = await registry.resolve(request.datasource)
    except Exception as exc:
        logger.warning(
            "自动化任务数据源解析失败",
            datasource=request.datasource,
            error_type=type(exc).__name__,
        )
        raise HTTPException(404, f"数据源 '{request.datasource}' 不可用") from exc

    policy = get_tenant_policy()
    if policy.datasource_isolation_enabled:
        from src.security.permission_check import resolve_datasource_access

        try:
            await resolve_datasource_access(
                await registry.list_all(),
                [request.datasource],
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                role=identity.role,
                tenant_policy=policy,
            )
        except PermissionError as exc:
            logger.warning(
                "自动化任务数据源权限拒绝",
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                datasource=request.datasource,
            )
            raise HTTPException(403, "无权使用该数据源创建自动化任务") from exc

    from src.security.sql_execution import validate_sql

    dialect = str(datasource.dialect)
    validation = validate_sql(request.sql, dialect)
    if not validation.valid:
        message = str(validation.errors[0].get("message", "仅允许单条只读 SQL"))
        logger.warning(
            "自动化任务只读校验拒绝",
            datasource=request.datasource,
            dialect=dialect,
            reason=message,
        )
        raise HTTPException(422, message)
    logger.info(
        "自动化任务创建校验完成",
        datasource=request.datasource,
        dialect=dialect,
    )
    return dialect


@router.post("/automation/schedules", status_code=201)
# 方法作用：创建归属于当前身份的主动洞察或定时报告任务。
# Args: request - 已由 Pydantic 校验的任务请求。
# Returns: 新任务摘要。
async def create_automation_schedule(
    request: AutomationScheduleCreateRequest,
) -> dict[str, Any]:
    identity = get_current_identity()
    dialect = await _validate_schedule_request(identity, request)
    schedule = await get_automation_store().create(identity, request, dialect)
    logger.info(
        "自动化任务创建路由完成",
        schedule_id=schedule.id,
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
    )
    return _schedule_response(schedule)


@router.get("/automation/schedules")
# 方法作用：列出当前身份可见的自动化任务。
# Args: 无。
# Returns: 任务数组和总数。
async def list_automation_schedules() -> dict[str, Any]:
    identity = get_current_identity()
    schedules = await get_automation_store().list_for_identity(identity)
    result = [_schedule_response(schedule) for schedule in schedules]
    logger.info(
        "自动化任务列表完成",
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        total=len(result),
    )
    return {"schedules": result, "total": len(result)}


@router.delete("/automation/schedules/{schedule_id}")
# 方法作用：删除当前身份可管理的自动化任务。
# Args: schedule_id - 任务 UUID。
# Returns: 删除状态。
async def delete_automation_schedule(schedule_id: str) -> dict[str, str]:
    identity = get_current_identity()
    try:
        deleted = await get_automation_store().delete(schedule_id, identity)
    except Exception as exc:
        if type(exc).__name__ in {"DataError", "InvalidTextRepresentationError"}:
            raise HTTPException(404, "自动化任务不存在") from exc
        raise
    if not deleted:
        raise HTTPException(404, "自动化任务不存在")
    logger.info(
        "自动化任务删除完成",
        schedule_id=schedule_id,
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
    )
    return {"status": "deleted"}


@router.post("/automation/schedules/{schedule_id}/run")
# 方法作用：在重新检查可见性后立即运行指定自动化任务。
# Args: schedule_id - 任务 UUID。
# Returns: Runner 的成功或失败摘要。
async def run_automation_schedule(schedule_id: str) -> dict[str, Any]:
    identity = get_current_identity()
    try:
        schedule = await get_automation_store().get_for_identity(schedule_id, identity)
    except Exception as exc:
        if type(exc).__name__ in {"DataError", "InvalidTextRepresentationError"}:
            raise HTTPException(404, "自动化任务不存在") from exc
        raise
    if schedule is None:
        raise HTTPException(404, "自动化任务不存在")
    logger.info(
        "自动化任务立即运行入口",
        schedule_id=schedule_id,
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
    )
    return await get_scheduled_analysis_runner().run(schedule)


@router.get("/automation/notifications")
# 方法作用：列出当前用户最近的站内自动化通知。
# Args: limit - 返回数量上限。
# Returns: 通知数组和总数。
async def list_automation_notifications(
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    identity = get_current_identity()
    notifications = await get_automation_store().list_notifications(identity, limit)
    logger.info(
        "自动化站内通知列表完成",
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        total=len(notifications),
    )
    return {"notifications": notifications, "total": len(notifications)}
