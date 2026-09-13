"""报表工作台 Phase 1 API：计划、任务、私有页面、分享和快照 Drilldown。"""

from __future__ import annotations

import secrets
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request

from src.api.auth import get_current_identity
from src.api.routes._helpers import _call_with_optional_tenant
from src.app_context import get_tenant_policy
from src.datasource.registry import get_registry
from src.logging_config import get_logger
from src.reporting.drilldown import execute_snapshot_drilldown, find_drilldown
from src.reporting.models import (
    AuthorizationCodeRequest,
    DrilldownRequest,
    PageLifecycleRequest,
    QuotaPolicyRequest,
    ReportCreateRequest,
    ReportJobCreateRequest,
    ReportPlan,
    ReportPlanRequest,
    ReportSchedulePatchRequest,
    ReportShareCreateRequest,
)
from src.reporting.plan_worker import get_report_plan_worker
from src.reporting.planner import ReportPlanner
from src.reporting.quota import (
    QuotaExceededError,
    QuotaManager,
    QuotaPolicy,
    ReportPermissionService,
    describe_quota_error,
)
from src.reporting.quota_reset import (
    QuotaUsageResetRequest,
    QuotaUsageResetResponse,
)
from src.reporting.share_analytics import (
    BatchRevokeSharesRequest,
    ShareVisitCreateRequest,
    ShareVisitUpdateRequest,
    TenantReportSettings,
    hash_client_ip,
)
from src.reporting.sharing import (
    can_access_user_scope,
    generate_share_token,
    hash_authorization_code,
    hash_share_token,
    verify_authorization_code,
)
from src.reporting.store import get_report_store

logger = get_logger(__name__)
router = APIRouter(prefix="/report-workbench")
_plan_cache: dict[tuple[int, int, str], ReportPlan] = {}
_unlock_sessions: dict[str, tuple[str, datetime]] = {}


# 方法作用：把数据库或内存分享记录中的用户范围统一转换为整数列表。
# Args: share - 分享记录。
# Returns: 去重后的用户 ID 列表。
def _share_user_ids(share: dict[str, Any]) -> list[int]:
    values = share.get("user_ids", []) or []
    if isinstance(values, str):
        try:
            import json

            values = json.loads(values)
        except (TypeError, ValueError):
            values = []
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(int(item) for item in values if str(item).isdigit() and int(item) > 0))


# 方法作用：校验指定用户分享的租户边界和用户授权范围。
# Args: identity - 当前登录身份；share - 分享记录。
# Returns: 当前身份允许访问返回 True。
def _can_access_share_scope(identity: Any, share: dict[str, Any]) -> bool:
    if str(share.get("scope", "public")) != "user":
        return True
    if int(getattr(identity, "tenant_id", 0) or 0) != int(share.get("tenant_id", 0) or 0):
        return False
    return can_access_user_scope(
        identity,
        int(share.get("created_by", 0) or 0),
        "user",
        _share_user_ids(share),
    )


# 方法作用：把数据库时间统一转换为带 UTC 时区的 datetime。
# Args: value - 数据库或 API 时间值。
# Returns: 规范化时间；无法解析时返回 None。
def _as_utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    normalized = value
    if isinstance(normalized, str):
        try:
            normalized = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(normalized, datetime):
        return None
    return normalized.replace(tzinfo=timezone.utc) if normalized.tzinfo is None else normalized.astimezone(timezone.utc)


# 方法作用：兼容数据库返回的带时区和不带时区过期时间。
# Args: expires_at - 分享或页面过期时间。
# Returns: 已过期返回 True。
def _is_expired(expires_at: Any) -> bool:
    value = _as_utc_datetime(expires_at)
    if expires_at is not None and value is None:
        return True
    if value is None:
        return False
    return value <= datetime.now(timezone.utc)


# 方法作用：获取当前应用上下文中的报表配额服务。
# Args: 无。
# Returns: QuotaManager 单例。
def get_report_quota_manager() -> QuotaManager:
    from src.app_context import get_app_context

    return get_app_context().get_or_create("report_quota_manager", lambda: QuotaManager(store=get_report_store()))


# 方法作用：获取当前应用上下文中的报表能力服务。
# Args: 无。
# Returns: ReportPermissionService 单例。
def get_report_permission_service() -> ReportPermissionService:
    from src.app_context import get_app_context

    return get_app_context().get_or_create(
        "report_permission_service",
        lambda: ReportPermissionService(store=get_report_store()),
    )


# 方法作用：校验当前身份是否具备指定报表能力。
# Args: identity - 当前请求身份；capability - report.* 能力键。
# Returns: 校验成功无返回值，否则抛出 HTTP 403。
async def _require_capability(identity: Any, capability: str) -> None:
    if not await get_report_permission_service().has(identity, capability):
        logger.warning("报表能力授权拒绝", tenant_id=getattr(identity, "tenant_id", 0), user_id=getattr(identity, "user_id", 0), capability=capability)
        raise HTTPException(403, f"缺少报表能力: {capability}")


# 方法作用：判断当前身份是否具备任一报表能力，用于共用入口的兼容鉴权。
# Args: identity - 当前请求身份；capabilities - 候选能力键。
# Returns: 任一能力命中返回 True。
async def _has_any_capability(identity: Any, capabilities: tuple[str, ...]) -> bool:
    service = get_report_permission_service()
    for capability in capabilities:
        if await service.has(identity, capability):
            return True
    return False


# 方法作用：判断当前身份是否具备查看全租户报表的能力。
# Args: identity - 当前请求身份。
# Returns: 具备 report.view_all 返回 True。
async def _has_view_all(identity: Any) -> bool:
    return await get_report_permission_service().has(identity, "report.view_all")


# 方法作用：读取单条报表记录，不存在或越权时统一返回 404。
# Args: identity - 当前请求身份；report_id - 报表标识。
# Returns: 报表字典（含 plan 对象）。
async def _load_report(identity: Any, report_id: str) -> dict[str, Any]:
    report = await get_report_store().get_report_row(report_id, identity, view_all=await _has_view_all(identity))
    if report is None:
        logger.warning("报表记录读取失败", report_id=report_id, tenant_id=getattr(identity, "tenant_id", 0), user_id=getattr(identity, "user_id", 0))
        raise HTTPException(404, "报表不存在或无权访问")
    return report


# 方法作用：把报表记录转换为对外响应，计划对象单独由调用方返回。
# Args: report - 报表记录字典。
# Returns: 不含内部计划对象的报表摘要。
def _public_report(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key not in {"plan", "tenant_id"}}


# 方法作用：校验报表写操作必须由创建者本人发起。
# Args: identity - 当前请求身份；report - 报表记录字典。
# Returns: 无返回值；非创建者时抛出 HTTP 403。
def _require_report_owner(identity: Any, report: dict[str, Any]) -> None:
    # 数据库 RLS 的 WITH CHECK 只允许本人写自己的行，租户管理员也不能替代业务用户改计划；
    # 内存分支与它保持一致，避免两种部署方式出现不同权限。
    if int(report.get("user_id", 0) or 0) != int(getattr(identity, "user_id", 0) or 0):
        logger.warning("报表写操作归属拒绝", report_id=report.get("id", ""), tenant_id=getattr(identity, "tenant_id", 0), user_id=getattr(identity, "user_id", 0))
        raise HTTPException(403, "只能以报表创建者身份操作，不能用管理员权限替代业务用户")


# 方法作用：校验「有限期转永久」是否超出租户配置的永久报表上限。
# Args: identity - 当前身份；report_id - 页面标识或任务标识；store - 报表 Store。
# Returns: 校验通过无返回值；超限时抛出 HTTP 409 并写拒绝审计。
async def _enforce_permanent_limit(identity: Any, report_id: str, store: Any) -> None:
    """0 表示不限制；已是永久的页面不计数（幂等）；租户管理员本人不受限。"""
    role = str(getattr(identity, "role", "") or "")
    # 取舍：上限由租户管理员配置，若管理员本人也受自己配置的上限约束，一旦把上限改小到低于自己已有的永久页面数，
    # 就再无法保存永久报表、只能改数据库解围；因此管理员豁免该上限（不是绕过权限校验：能力校验仍先于本函数执行）。
    if role == "tenant_admin":
        return
    # 只有「由有限期转为永久」才计数：已经是永久页面的重复调用必须保持幂等，不得报 409。
    version = await store.get_report_version(report_id, identity)
    if version is None or version.expires_at is None:
        return
    settings = await store.get_tenant_settings(int(getattr(identity, "tenant_id", 0) or 0))
    # 设置行不存在或显式配 0 时按「不限制」处理。
    limit = int(settings.get("max_permanent_reports_per_user", 0) or 0)
    if limit <= 0:
        return
    permanent_count = await store.count_permanent_versions_for_user(identity, int(getattr(identity, "user_id", 0) or 0))
    if permanent_count < limit:
        logger.debug("永久报表上限校验通过", report_id=report_id, limit=limit, permanent_count=permanent_count)
        return
    logger.warning("永久报表数量超限", report_id=report_id, user_id=getattr(identity, "user_id", 0), limit=limit, permanent_count=permanent_count)
    # 审计先于抛错写入：被拒绝的请求不能改动任何页面数据，但必须留痕。
    await store.record_audit({
        "tenant_id": getattr(identity, "tenant_id", 0),
        "user_id": getattr(identity, "user_id", 0),
        "report_id": report_id,
        "action": "page_lifecycle",
        "result": "denied",
        "metadata": {"reason": "permanent_limit_reached", "limit": limit, "permanent_count": permanent_count},
    })
    raise HTTPException(409, f"永久报表已达上限 {limit}，请联系租户管理员调整")


# 方法作用：在计划阶段重新解析当前身份可用的数据源和行列授权。
# Args: identity - 当前请求身份；plan - 待执行报表计划。
# Returns: 数据源名称到授权摘要的映射。
async def _resolve_plan_access(identity: Any, plan: ReportPlan) -> dict[str, dict[str, Any]]:
    requested = list(plan.requested_datasources)
    if not requested:
        return {}
    registry = get_registry()
    try:
        available = await _call_with_optional_tenant(registry.list_all, tenant_id=int(identity.tenant_id))
        policy = get_tenant_policy()
        if policy.datasource_isolation_enabled:
            from src.security.permission_check import resolve_datasource_access

            return await resolve_datasource_access(
                [item if isinstance(item, dict) else {"name": getattr(item, "name", "")} for item in available],
                requested,
                tenant_id=int(identity.tenant_id),
                user_id=int(identity.user_id),
                role=str(identity.role),
                tenant_policy=policy,
            )
        result: dict[str, dict[str, Any]] = {}
        for name in requested:
            datasource = await _call_with_optional_tenant(registry.resolve, name, tenant_id=int(identity.tenant_id))
            result[name] = {"name": name, "dialect": str(getattr(datasource, "dialect", "")), "allowed_columns": [], "row_filter_sql": ""}
        return result
    except PermissionError as exc:
        logger.warning("报表计划数据源授权拒绝", tenant_id=identity.tenant_id, user_id=identity.user_id)
        raise HTTPException(403, "无权使用计划中的数据源") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("报表计划数据源解析失败", error=str(exc), exc_info=True)
        raise HTTPException(503, "数据源授权服务暂时不可用") from exc


# 方法作用：把计划模型转换为不含内部权限快照的预览响应。
# Args: plan - 报表计划。
# Returns: 计划 JSON、哈希和估算摘要。
# 方法作用：按用户勾选结果删减计划单元，只允许删减不允许新增。
# Args: plan - 预览确认过的计划；accepted_unit_ids - 用户保留的单元标识列表。
# Returns: 裁剪后的有效计划；入参非法时抛出 HTTPException。
def _apply_unit_trimming(plan: ReportPlan, accepted_unit_ids: list[str]) -> ReportPlan:
    from src.reporting.models import ReportPlan as _ReportPlan

    accepted = list(dict.fromkeys(str(item) for item in accepted_unit_ids))
    if not accepted:
        # 空数组是"全部取消"，等价于空计划，属非法请求；不传该字段才表示全部接受。
        raise HTTPException(422, "accepted_unit_ids 不能是空数组；如需全部接受请不提交该字段")
    known = {unit.unit_id for unit in plan.analysis_units}
    unknown = [item for item in accepted if item not in known]
    if unknown:
        # 没有这条校验，客户端就能凭空构造执行范围。
        raise HTTPException(422, f"包含不属于预览计划的分析单元：{'、'.join(unknown[:5])}")
    kept = set(accepted)
    excluded = [unit.unit_id for unit in plan.analysis_units if unit.unit_id not in kept]
    if not excluded:
        return plan
    candidate = plan.model_dump(mode="json")
    candidate["analysis_units"] = [unit.model_dump(mode="json") for unit in plan.analysis_units if unit.unit_id in kept]
    # 记录被删减的标识，保留审计线索；该字段进 canonical_hash。
    candidate["excluded_unit_ids"] = list(dict.fromkeys([*plan.excluded_unit_ids, *excluded]))
    # 重新走完整校验：单元被删后 requested_datasources 会重算，unit_id 唯一性也会再验一次。
    trimmed = _ReportPlan.model_validate(candidate)
    logger.info("报表计划按用户裁剪", kept_count=len(trimmed.analysis_units), excluded_count=len(excluded))
    return trimmed


def _plan_response(plan: ReportPlan) -> dict[str, Any]:
    return {"plan": plan.model_dump(mode="json"), "plan_hash": plan.canonical_hash(), "estimated": plan.estimated.model_dump(mode="json"), "job_id": None}


# 方法作用：解析预览计划可用的租户模型，未配置时返回 None 交给规则回退。
# Args: tenant_id - 当前认证租户；connection_id - 客户端显式选择的连接；model_id - 客户端显式选择的模型。
# Returns: 可用的租户 LLM 选择，或未配置时的 None。
async def _resolve_plan_llm_selection(tenant_id: int, connection_id: int | None = None, model_id: str = ""):
    from src.llm.tenant_config import resolve_tenant_llm_selection

    try:
        # 客户端显式选择命名连接时优先使用；未选择时回退租户默认连接。
        return await resolve_tenant_llm_selection(
            tenant_id=int(tenant_id),
            connection_id=connection_id,
            model_id=model_id,
        )
    except (LookupError, ValueError) as exc:
        # 未配置模型属于正常状态，规划会自动回退规则拆分，不能因此让预览接口失败。
        logger.info("报表计划未启用租户模型，按规则拆分", tenant_id=tenant_id, reason=str(exc))
        return None
    except Exception:
        # 配置查询异常必须保留堆栈，便于区分“未配置”和“服务不可用”。
        logger.error("报表租户模型解析失败", tenant_id=tenant_id, exc_info=True)
        return None


@router.post("/plans")
# 方法作用：解析自然语言并返回报表计划预览，不产生任务或扣运行配额。
# Args: request - 计划预览请求。
# Returns: 可供确认的结构化计划。
async def preview_plan(request: ReportPlanRequest) -> dict[str, Any]:
    """兼容入口：前端已改用 POST /reports 的后台计划生成，这里只保留旧链路。"""
    identity = get_current_identity()
    await _require_capability(identity, "report.menu")
    planner = ReportPlanner()
    from src.llm.tenant_config import use_tenant_llm_selection

    # 绑定租户模型后，Planner 才会用大模型做业务语义拆解；未配置时走规则回退。
    selection = await _resolve_plan_llm_selection(
        identity.tenant_id,
        connection_id=request.llm_connection_id,
        model_id=request.model_id,
    )
    with use_tenant_llm_selection(selection):
        plan = await planner.parse(
            request.prompt,
            datasources=request.datasources,
            timezone=request.timezone,
            divergence=request.divergence,
        )
        # 计划期发散在需求拆解之后单独走一次调用，使两者可独立降级与独立评测。
        # 表名清单让模型知道"还可以查什么"，与 report.route 共用同一份目录。
        from src.reporting.runner import ReportRunner

        catalog = await ReportRunner._datasource_table_catalog(
            list(plan.requested_datasources), int(identity.tenant_id)
        )
        plan = await planner.diverge(plan, datasources=request.datasources, catalog=catalog)
    await _resolve_plan_access(identity, plan)
    _plan_cache[(int(identity.tenant_id), int(identity.user_id), plan.canonical_hash())] = plan
    logger.info("报表计划预览完成", tenant_id=identity.tenant_id, user_id=identity.user_id, plan_hash=plan.canonical_hash())
    return _plan_response(plan)


@router.post("/jobs", status_code=202)
# 方法作用：按确认且未被篡改的计划创建立即报表任务并后台启动运行。
# Args: request - 计划创建请求。
# Returns: Job、Run 排队摘要和剩余配额。
async def create_job(request: ReportJobCreateRequest) -> dict[str, Any]:
    """兼容入口：前端已改用 POST /reports/{id}/run，这里保留「先预览后确认」的旧链路。"""
    identity = get_current_identity()
    await _require_capability(identity, "report.create")
    cache_key = (int(identity.tenant_id), int(identity.user_id), request.plan_hash)
    previewed_plan = _plan_cache.get(cache_key)
    if request.plan_override is not None:
        # 计划覆盖只能携带与当前身份预览缓存完全一致的哈希，不能由客户端自行签发执行范围。
        if previewed_plan is None or previewed_plan.canonical_hash() != request.plan_hash:
            logger.warning(
                "报表计划覆盖未匹配预览缓存",
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                plan_hash=request.plan_hash,
            )
            raise HTTPException(409, "计划未经过当前身份预览确认，请重新生成预览")
        plan = request.plan_override
    else:
        plan = previewed_plan
    if plan is None:
        raise HTTPException(409, "计划已过期，请重新生成预览")
    if plan.canonical_hash() != request.plan_hash:
        raise HTTPException(409, "计划哈希不匹配，请重新确认计划")
    if request.accepted_unit_ids is not None:
        # 裁剪只能删减预览计划，不能新增：执行范围只可能更小，因此不构成越权。
        # 凭证校验必须发生在裁剪之前，客户端仍须持有完整预览计划的哈希。
        if request.plan_override is not None:
            raise HTTPException(422, "accepted_unit_ids 与 plan_override 不能同时提交")
        plan = _apply_unit_trimming(plan, request.accepted_unit_ids)
    if plan.needs_confirmation and not request.confirm:
        raise HTTPException(409, "该计划需要用户确认后才能执行")
    if plan.execution_mode == "scheduled":
        await _require_capability(identity, "report.schedule")
        if not plan.frequency:
            raise HTTPException(422, "定时任务必须明确每小时、每天、每周或每月频率")
        from src.app_context import get_app_context

        scheduler = get_app_context().get_resource("report_scheduler")
        if scheduler is None or not scheduler.running:
            raise HTTPException(503, "持久化报表调度服务尚未启动，请联系平台管理员")
        if plan.schedule_end_at is not None and _is_expired(plan.schedule_end_at):
            raise HTTPException(422, "调度结束时间必须在未来")
    if plan.permanent:
        await _require_capability(identity, "report.permanent")
    if plan.share.enabled:
        await _require_capability(identity, "report.share")
    access = await _resolve_plan_access(identity, plan)
    quota_manager = get_report_quota_manager()
    store = get_report_store()
    try:
        # plan_json 存裁剪后的有效计划，plan_hash 仍是它的校验和；
        # preview_hash 单独记录客户端提交的预览凭证，用于审计「这份计划来自哪次预览」。
        job, created, quota = await store.create_job_with_quota(
            identity, request.prompt, plan, request.idempotency_key, quota_manager, request.plan_hash, request.template_id
        )
    except QuotaExceededError as exc:
        raise HTTPException(429, describe_quota_error(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    from src.reporting.runner import get_report_runner

    if created and plan.execution_mode == "immediate":
        get_report_runner().enqueue(job.id, identity, authorized_datasources=access)
    logger.info("报表任务创建并排队完成", job_id=job.id, tenant_id=identity.tenant_id, user_id=identity.user_id)
    return {"job_id": job.id, "status": job.status, "execution_mode": plan.execution_mode, "run_id": None, "report_id": None, "estimated": plan.estimated.model_dump(mode="json"), "quota": asdict(quota)}


@router.get("/jobs")
# 方法作用：列出当前身份可见的报表任务。
# Args: limit - 返回数量上限。
# Returns: 任务数组和总数。
async def list_jobs(limit: int = Query(default=50, ge=1, le=100)) -> dict[str, Any]:
    identity = get_current_identity()
    await _require_capability(identity, "report.menu")
    items = await get_report_store().list_jobs(identity, limit)
    return {"jobs": [item.model_dump(mode="json") for item in items], "total": len(items)}


@router.get("/jobs/{job_id}")
# 方法作用：返回任务计划和最近运行状态。
# Args: job_id - 报表任务 ID。
# Returns: 任务详情。
async def get_job(job_id: str) -> dict[str, Any]:
    identity = get_current_identity()
    await _require_capability(identity, "report.menu")
    store = get_report_store()
    job = await store.get_job(job_id, identity)
    if job is None:
        raise HTTPException(404, "报表任务不存在")
    runs = await store.list_runs(job_id, identity, 1)
    latest_version = await store.get_latest_version_for_job(job_id, identity)
    return {
        "job": job.model_dump(mode="json"),
        "latest_run": runs[0].model_dump(mode="json") if runs else None,
        "latest_report": latest_version.model_dump(mode="json", exclude={"document", "data_manifest"}) if latest_version else None,
    }


@router.post("/jobs/{job_id}/run", status_code=202)
# 方法作用：重新校验当前权限后手动排队运行任务。
# Args: job_id - 报表任务 ID。
# Returns: 排队状态。
async def run_job_now(job_id: str) -> dict[str, Any]:
    logger.info("手动重跑授权入口探针", job_id=job_id)
    identity = get_current_identity()
    await _require_capability(identity, "report.create")
    job = await get_report_store().get_job(job_id, identity)
    if job is None:
        raise HTTPException(404, "报表任务不存在")
    if job.plan.execution_mode == "scheduled":
        await _require_capability(identity, "report.schedule")
    if job.is_paused:
        raise HTTPException(409, "任务已暂停，请先恢复")
    if job.user_id != identity.user_id:
        raise HTTPException(403, "只能以任务创建者身份重新运行，不能用管理员权限替代业务用户")
    access = await _resolve_plan_access(identity, job.plan)
    from src.reporting.runner import get_report_runner

    get_report_runner().enqueue(job_id, identity, authorized_datasources=access, force=True)
    return {"job_id": job_id, "status": "queued"}


@router.post("/jobs/{job_id}/pause")
# 方法作用：暂停租户内可管理的任务，正在运行的任务在心跳或发布边界停止。
# Args: job_id - 任务标识。
# Returns: 暂停状态。
async def pause_job(job_id: str) -> dict[str, Any]:
    """不删除既有页面，也不增加任何剩余次数。"""
    identity = get_current_identity()
    await _require_capability(identity, "report.schedule")
    if not await get_report_store().set_job_paused(job_id, identity, True):
        raise HTTPException(404, "任务不存在或无权访问")
    return {"job_id": job_id, "is_paused": True}


@router.post("/jobs/{job_id}/resume")
# 方法作用：恢复未耗尽次数且仍有效的任务，后续执行仍重建原业务身份。
# Args: job_id - 任务标识。
# Returns: 恢复状态。
async def resume_job(job_id: str) -> dict[str, Any]:
    """失败暂停必须显式恢复，不能无限重试认证或配置错误。"""
    identity = get_current_identity()
    await _require_capability(identity, "report.schedule")
    store = get_report_store()
    job = await store.get_job(job_id, identity)
    if job is None:
        raise HTTPException(404, "任务不存在或无权访问")
    if job.plan.max_runs is not None and job.runs_started >= job.plan.max_runs or job.plan.schedule_end_at is not None and _is_expired(job.plan.schedule_end_at):
        raise HTTPException(409, "任务次数或调度期限已耗尽，请创建新任务")
    await store.set_job_paused(job_id, identity, False)
    await store.update_dispatch(job_id, identity, status="queued", next_run_at=job.next_run_at or datetime.now(timezone.utc))
    return {"job_id": job_id, "is_paused": False}


@router.get("/jobs/{job_id}/runs")
# 方法作用：返回任务最近运行和节点状态摘要。
# Args: job_id - 报表任务 ID；limit - 返回数量上限。
# Returns: 运行数组和总数。
async def list_job_runs(job_id: str, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    identity = get_current_identity()
    await _require_capability(identity, "report.menu")
    runs = await get_report_store().list_runs(job_id, identity, limit)
    if not runs and await get_report_store().get_job(job_id, identity) is None:
        raise HTTPException(404, "报表任务不存在")
    return {"runs": [item.model_dump(mode="json") for item in runs], "total": len(runs)}


@router.get("/runs/{run_id}")
# 方法作用：返回运行用户可见错误、节点日志和耗时摘要，不暴露内部堆栈。
# Args: run_id - 运行 ID。
# Returns: 运行详情和步骤。
async def get_run(run_id: str) -> dict[str, Any]:
    identity = get_current_identity()
    await _require_capability(identity, "report.menu")
    store = get_report_store()
    run = await store.get_run(run_id, identity)
    if run is None:
        raise HTTPException(404, "报表运行不存在")
    steps = await store.list_steps(run_id, identity)
    # 内存 Store 也可能保留内部异常字段，API 边界统一剥离，避免把堆栈或连接细节暴露给普通用户。
    safe_steps = [
        {key: value for key, value in step.items() if key not in {"internal_error", "internal_error_message", "input_hash"}}
        for step in steps
    ]
    return {"run": run.model_dump(mode="json"), "steps": safe_steps}


@router.get("/reports/{report_id}")
# 方法作用：读取报表详情（计划、最近运行、页面列表），并兼容旧查看页的页面正文读取。
# Args: report_id - 报表 ID。
# Returns: 报表摘要、计划、运行、页面与（页面未过期时的）页面文档。
async def get_report(report_id: str) -> dict[str, Any]:
    """同一个入口服务两类消费者：报表信息详情按记录读取，旧查看页按页面读取。"""
    identity = get_current_identity()
    # 旧查看页只持有 report.menu，报表信息页使用 report.list，两者都能通过。
    if not await _has_any_capability(identity, ("report.list", "report.menu")):
        raise HTTPException(403, "缺少报表能力: report.list")
    store = get_report_store()
    view_all = await _has_view_all(identity)
    report = await store.get_report_row(report_id, identity, view_all=view_all)
    if report is None:
        # 兼容路径：按页面 report_id 读取旧查看页正文。
        version = await store.get_report_version(report_id, identity)
        if version is None:
            raise HTTPException(404, "报表不存在或无权访问")
        if _is_expired(version.expires_at):
            raise HTTPException(410, "报表页面已过期")
        document = _filtered_document(version.document, await get_report_permission_service().has(identity, "report.live_drilldown"))
        return {"report": None, "plan": None, "latest_run": None, "latest_report": None, "runs": [], "pages": [], "report_id": version.report_id, "version": version.version, "document": document, "data_manifest": version.data_manifest}
    # 页面正文只在最新页面未过期时下发；过期页面保留 410 语义，避免查看页拿到空正文。
    version = await store.get_latest_version_for_job(report_id, identity)
    if version is not None and _is_expired(version.expires_at):
        raise HTTPException(410, "报表页面已过期")
    runs = await store.list_runs(report_id, identity, 20)
    pages = await store.list_report_pages(report_id, identity, view_all=view_all)
    payload: dict[str, Any] = {
        "report": _public_report(report),
        "plan": report["plan"].model_dump(mode="json") if report.get("plan") is not None else None,
        "latest_run": runs[0].model_dump(mode="json") if runs else None,
        "latest_report": next((item for item in pages if item["is_latest"]), pages[0] if pages else None),
        "runs": [item.model_dump(mode="json") for item in runs],
        "pages": pages,
        # 顶层再暴露一次失败原因：前端详情类型把它声明在响应根上，避免读到 undefined。
        "plan_error": report["plan_error"],
    }
    if version is not None:
        payload["report_id"] = version.report_id
        payload["version"] = version.version
        payload["document"] = _filtered_document(version.document, await get_report_permission_service().has(identity, "report.live_drilldown"))
        payload["data_manifest"] = version.data_manifest
    logger.info("报表详情读取完成", report_id=report_id, user_id=identity.user_id, status=report["status"], page_count=len(pages), run_count=len(runs))
    return payload


# 方法作用：按实时钻取能力裁剪页面正文中的钻取定义。
# Args: document - 页面文档；live_allowed - 是否具备 report.live_drilldown。
# Returns: 裁剪后的页面文档副本。
def _filtered_document(document: dict[str, Any], live_allowed: bool) -> dict[str, Any]:
    result = dict(document)
    if not live_allowed:
        result["drilldowns"] = [item for item in result.get("drilldowns", []) if item.get("mode", "snapshot") == "snapshot"]
    return result


@router.post("/reports", status_code=202)
# 方法作用：点「生成计划」立即创建报表记录并启动后台规划，不预占任何额度。
# Args: request - 创建请求（描述、数据源、时区、档位、模板、模型、幂等键）。
# Returns: 报表标识、状态与创建时间。
async def create_report(request: ReportCreateRequest) -> dict[str, Any]:
    """需求 1.5：额度在点「生成」时才预占，这里只建记录。"""
    identity = get_current_identity()
    await _require_capability(identity, "report.create_page")
    store = get_report_store()
    logger.debug("报表创建入口", tenant_id=identity.tenant_id, user_id=identity.user_id, datasource_count=len(request.datasources), divergence=request.divergence)
    try:
        job, created = await store.create_planning_job(
            identity,
            request.prompt,
            datasources=list(request.datasources),
            timezone_name=request.timezone,
            divergence=request.divergence,
            template_id=request.template_id.strip() or None,
            idempotency_key=request.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if created:
        # 后台规划复用 runner 的 asyncio.Task 模式，异常在工作器内转成 plan_failed。
        get_report_plan_worker().enqueue(job.id, identity, model_id=request.model_id, connection_id=request.llm_connection_id)
    logger.info("报表记录创建完成", report_id=job.id, tenant_id=identity.tenant_id, user_id=identity.user_id, created=created)
    return {"report_id": job.id, "status": job.status, "created_at": job.created_at}


@router.get("/reports")
# 方法作用：按可见范围分页列出报表信息，默认创建时间倒序。
# Args: user_id - 可选目标用户（仅租户管理员）；status - 可选状态；page/page_size - 分页。
# Returns: 报表列表、总数与分页参数。
async def list_reports(
    user_id: int | None = Query(default=None, ge=1),
    status: Literal["planning", "plan_failed", "draft", "queued", "running", "succeeded", "partial", "failed", "cancelled", "expired", "deleted"] | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1),
) -> dict[str, Any]:
    """租户管理员可按用户筛选，其他角色只返回自己的记录。"""
    identity = get_current_identity()
    await _require_capability(identity, "report.list")
    if user_id is not None and str(identity.role) != "tenant_admin" and int(user_id) != int(identity.user_id):
        raise HTTPException(403, "仅租户管理员可按用户筛选报表")
    bounded_size = max(1, min(int(page_size), 100))
    items, total = await get_report_store().list_reports(
        identity,
        user_id=user_id,
        status=status,
        page=page,
        page_size=bounded_size,
        view_all=await _has_view_all(identity),
    )
    logger.info("报表信息列表完成", tenant_id=identity.tenant_id, user_id=identity.user_id, total=total, returned=len(items))
    return {"reports": [_public_report(item) for item in items], "total": total, "page": page, "page_size": bounded_size}


@router.patch("/reports/{report_id}")
# 方法作用：编辑「未生成」报表的计划与调度，分析单元只允许删减。
# Args: report_id - 报表标识；request - 可选的分析单元、执行方式、频率与页面策略。
# Returns: 更新后的报表详情。
async def update_report(report_id: str, request: ReportSchedulePatchRequest) -> dict[str, Any]:
    """需求 1.6：改计划后必须重算 plan_hash 并重新校验全部既有约束。"""
    identity = get_current_identity()
    await _require_capability(identity, "report.create_page")
    store = get_report_store()
    report = await _load_report(identity, report_id)
    _require_report_owner(identity, report)
    if report["status"] != "draft":
        raise HTTPException(409, "仅「未生成」状态的报表可以修改计划")
    plan = report.get("plan")
    if plan is None:
        raise HTTPException(409, "该报表计划不可用，请重试生成计划")
    if request.accepted_unit_ids is not None:
        # 复用确认入口的裁剪逻辑：只允许删减，越界或空数组直接 422。
        plan = _apply_unit_trimming(plan, request.accepted_unit_ids)
    candidate = plan.model_dump(mode="json")
    if request.execution_mode is not None:
        candidate["execution_mode"] = request.execution_mode
    if request.frequency is not None:
        candidate["frequency"] = request.frequency
    if request.run_at is not None:
        candidate["run_at"] = request.run_at.isoformat()
    if request.max_runs is not None:
        candidate["max_runs"] = request.max_runs
    if request.schedule_end_at is not None:
        candidate["schedule_end_at"] = request.schedule_end_at.isoformat()
    if request.page_ttl_days is not None:
        candidate["page_ttl_days"] = request.page_ttl_days
    if request.permanent is not None:
        candidate["permanent"] = request.permanent
    if request.timezone is not None:
        candidate["timezone"] = request.timezone
    try:
        updated = ReportPlan.model_validate(candidate)
    except ValueError as exc:
        logger.warning("报表计划编辑校验失败", report_id=report_id, reason=str(exc))
        raise HTTPException(422, str(exc)) from exc
    # 改动后重新校验全部既有约束，避免用编辑接口绕过创建时的权限检查。
    if updated.execution_mode == "scheduled" and not updated.frequency:
        raise HTTPException(422, "定时报表必须明确每小时、每天、每周或每月频率")
    if updated.execution_mode == "scheduled":
        await _require_capability(identity, "report.schedule")
    if updated.permanent:
        await _require_capability(identity, "report.permanent")
    if updated.share.enabled:
        await _require_capability(identity, "report.share")
    columns = {
        # 立即执行没有下次运行时间；定时执行沿用计划里的首次时间。
        "next_run_at": (updated.run_at if updated.execution_mode == "scheduled" else None),
        "template_id": (request.template_id if request.template_id is not None else report["template_id"]),
    }
    if not await store.update_report_plan(report_id, identity, plan=updated, columns=columns):
        raise HTTPException(404, "报表不存在或无权访问")
    logger.info("报表计划编辑完成", report_id=report_id, user_id=identity.user_id, plan_hash=updated.canonical_hash(), unit_count=len(updated.analysis_units))
    return await get_report(report_id)


@router.post("/reports/{report_id}/run", status_code=202)
# 方法作用：点「生成」预占任务额度并排队运行未生成报表。
# Args: report_id - 报表标识。
# Returns: 排队状态、执行方式与配额快照。
async def run_report(report_id: str) -> dict[str, Any]:
    """需求 1.5：jobs_created 只在这里预占，预占失败时状态保持不变。"""
    identity = get_current_identity()
    await _require_capability(identity, "report.create_page")
    store = get_report_store()
    report = await _load_report(identity, report_id)
    _require_report_owner(identity, report)
    if report["status"] != "draft":
        raise HTTPException(409, "仅「未生成」状态的报表可以运行")
    plan = report.get("plan")
    if plan is None:
        raise HTTPException(409, "该报表计划不可用，请重试生成计划")
    if plan.execution_mode == "scheduled":
        await _require_capability(identity, "report.schedule")
        if not plan.frequency:
            raise HTTPException(422, "定时任务必须明确每小时、每天、每周或每月频率")
        from src.app_context import get_app_context

        scheduler = get_app_context().get_resource("report_scheduler")
        if scheduler is None or not scheduler.running:
            raise HTTPException(503, "持久化报表调度服务尚未启动，请联系平台管理员")
        if plan.schedule_end_at is not None and _is_expired(plan.schedule_end_at):
            raise HTTPException(422, "调度结束时间必须在未来")
    # 授权先解析：越权请求不能先消耗用户的任务额度。
    access = await _resolve_plan_access(identity, plan)
    try:
        quota = await get_report_quota_manager().reserve(identity, jobs=1, timezone_name=plan.timezone)
    except QuotaExceededError as exc:
        logger.warning("报表运行配额拒绝", report_id=report_id, user_id=identity.user_id, error=str(exc))
        raise HTTPException(429, describe_quota_error(exc)) from exc
    if plan.execution_mode == "scheduled":
        # 定时任务交给调度器认领：写回下次运行时间并置为排队。
        await store.update_dispatch(report_id, identity, status="queued", next_run_at=plan.run_at or datetime.now(timezone.utc))
    else:
        await store.update_dispatch(report_id, identity, status="queued", next_run_at=None)
        from src.reporting.runner import get_report_runner

        get_report_runner().enqueue(report_id, identity, authorized_datasources=access)
    logger.info("报表运行已排队", report_id=report_id, tenant_id=identity.tenant_id, user_id=identity.user_id, execution_mode=plan.execution_mode)
    return {"report_id": report_id, "status": "queued", "execution_mode": plan.execution_mode, "run_id": None, "quota": asdict(quota)}


@router.post("/reports/{report_id}/retry-plan", status_code=202)
# 方法作用：重试失败的计划生成，清空失败原因后重新入队后台规划。
# Args: report_id - 报表标识。
# Returns: 复位后的状态。
async def retry_report_plan(report_id: str) -> dict[str, Any]:
    """仅 plan_failed 可重试，其他状态返回 409。"""
    identity = get_current_identity()
    await _require_capability(identity, "report.create_page")
    store = get_report_store()
    report = await _load_report(identity, report_id)
    _require_report_owner(identity, report)
    if report["status"] != "plan_failed":
        raise HTTPException(409, "仅计划失败的报表可以重试生成计划")
    if not await store.reset_plan_status(report_id, identity):
        raise HTTPException(404, "报表不存在或无权访问")
    get_report_plan_worker().enqueue(report_id, identity)
    logger.info("报表计划重试已排队", report_id=report_id, user_id=identity.user_id)
    return {"report_id": report_id, "status": "planning"}


@router.delete("/reports/{report_id}")
# 方法作用：删除未生成、计划失败或已结束的报表记录。
# Args: report_id - 报表标识。
# Returns: 删除状态。
async def delete_report(report_id: str) -> dict[str, Any]:
    """生成中与运行中的报表禁止删除，避免与后台任务竞争同一记录。"""
    identity = get_current_identity()
    await _require_capability(identity, "report.create_page")
    report = await _load_report(identity, report_id)
    _require_report_owner(identity, report)
    if report["status"] in {"planning", "queued", "running"}:
        raise HTTPException(409, "报表正在生成或运行中，请等待结束后再删除")
    if not await get_report_store().delete_report(report_id, identity, view_all=await _has_view_all(identity)):
        raise HTTPException(404, "报表不存在或无权访问")
    logger.info("报表记录删除完成", report_id=report_id, user_id=identity.user_id, previous_status=report["status"])
    return {"report_id": report_id, "status": "deleted"}


@router.get("/reports/{report_id}/pages")
# 方法作用：列出该报表的全部页面版本摘要，按版本倒序。
# Args: report_id - 报表标识。
# Returns: 页面摘要列表与总数。
async def list_report_pages(report_id: str) -> dict[str, Any]:
    """为多页面独立可访问做准备，本次只做只读列表。"""
    identity = get_current_identity()
    await _require_capability(identity, "report.list")
    view_all = await _has_view_all(identity)
    await _load_report(identity, report_id)
    pages = await get_report_store().list_report_pages(report_id, identity, view_all=view_all)
    logger.info("报表页面列表完成", report_id=report_id, user_id=identity.user_id, total=len(pages))
    return {"pages": pages, "total": len(pages)}


@router.get("/reports/{report_id}/lifecycle")
# 方法作用：返回页面生命周期摘要，让已过期页面可管理而不泄露正文。
# Args: report_id - 页面标识。
# Returns: 有效期和当前用户可执行的生命周期能力。
async def get_page_lifecycle(report_id: str) -> dict[str, Any]:
    """已清理页面不可恢复，过期但仍保留的页面只返回管理元数据。"""
    identity = get_current_identity()
    await _require_capability(identity, "report.menu")
    version = await get_report_store().get_report_version(report_id, identity)
    if version is None:
        raise HTTPException(404, "报表不存在或无权访问")
    permissions = await get_report_permission_service().capabilities(identity)
    return {"report_id": report_id, "expires_at": version.expires_at, "expired": _is_expired(version.expires_at), "can_extend": "report.extend" in permissions, "can_permanent": "report.permanent" in permissions}


@router.put("/reports/{report_id}/lifecycle")
# 方法作用：按租户能力显式延期或永久保留已有页面。
# Args: report_id - 页面标识；request - 延期或永久操作。
# Returns: 更新后的生命周期摘要。
async def update_page_lifecycle(report_id: str, request: PageLifecycleRequest) -> dict[str, Any]:
    """不会自动延长、恢复或新建任何旧分享授权。"""
    identity = get_current_identity()
    logger.debug("页面生命周期 API 入口", report_id=report_id, user_id=identity.user_id, permanent=request.permanent, days=request.days)
    await _require_capability(identity, "report.permanent" if request.permanent else "report.extend")
    store = get_report_store()
    if request.permanent:
        # PUT /lifecycle 与 POST /permanent 共用本处理函数，永久上限校验只需在这里做一次。
        await _enforce_permanent_limit(identity, report_id, store)
    version = await store.update_page_lifecycle(report_id, identity, **request.model_dump())
    if version is None:
        raise HTTPException(404, "报表不存在或无权访问")
    await store.record_audit({"tenant_id": identity.tenant_id, "user_id": identity.user_id, "report_id": report_id, "action": "page_lifecycle", "result": "success", "metadata": request.model_dump()})
    return {"report_id": report_id, "expires_at": version.expires_at, "permanent": version.expires_at is None}


@router.post("/reports/{report_id}/extend")
# 方法作用：提供设计约定的页面延期入口，最多延长三十天。
# Args: report_id - 页面标识；request - 延期天数，默认七天。
# Returns: 更新后的生命周期摘要。
async def extend_page(report_id: str, request: PageLifecycleRequest = PageLifecycleRequest(days=7)) -> dict[str, Any]:
    """延期入口不接受永久操作。"""
    if request.permanent:
        raise HTTPException(422, "请使用永久保留接口")
    return await update_page_lifecycle(report_id, request)


@router.post("/reports/{report_id}/permanent")
# 方法作用：提供设计约定的永久保留入口，单独检查租户业务能力。
# Args: report_id - 页面标识。
# Returns: 永久保留后的生命周期摘要。
async def make_page_permanent(report_id: str) -> dict[str, Any]:
    """永久页面的旧分享仍保持原到期时间。"""
    return await update_page_lifecycle(report_id, PageLifecycleRequest(permanent=True))


@router.get("/reports/{report_id}/versions")
# 方法作用：列出当前用户可访问的报表版本。
# Args: report_id - 报表 ID。
# Returns: 版本摘要；页面正文只通过单版本接口返回。
async def list_report_versions(report_id: str) -> dict[str, Any]:
    result = await get_report(report_id)
    return {"versions": [{"report_id": result["report_id"], "version": result["version"], "expires_at": result["document"].get("expires_at")}], "total": 1}


@router.post("/reports/{report_id}/shares", status_code=201)
# 方法作用：为当前用户可访问的不可变版本创建公开或指定用户分享。
# Args: report_id - 报表 ID；request - 分享范围、授权码和交互权限。
# Returns: 只在本次响应返回一次的分享 URL 和摘要。
async def create_share(report_id: str, request: ReportShareCreateRequest) -> dict[str, Any]:
    identity = get_current_identity()
    await _require_capability(identity, "report.share")
    store = get_report_store()
    version = await store.get_report_version(report_id, identity)
    if version is None:
        raise HTTPException(404, "报表不存在或无权访问")
    version_expires_at = _as_utc_datetime(version.expires_at)
    if version_expires_at is not None and version_expires_at <= datetime.now(timezone.utc):
        raise HTTPException(410, "报表页面已过期，不能创建分享")
    if request.scope == "user" and not request.user_ids:
        raise HTTPException(422, "指定用户分享必须提供 user_ids")
    try:
        quota = await get_report_quota_manager().reserve(identity, shares=1)
    except QuotaExceededError as exc:
        raise HTTPException(429, describe_quota_error(exc)) from exc
    token, token_hash = generate_share_token()
    code_hash = await hash_authorization_code(request.authorization_code) if request.require_code else ""
    expires_at = datetime.now(timezone.utc) + timedelta(days=request.expires_in_days)
    if version_expires_at is not None:
        expires_at = min(expires_at, version_expires_at)
    record = {
        "report_version_id": version.id,
        "report_id": version.report_id,
        "version": version.version,
        "tenant_id": int(identity.tenant_id),
        "created_by": int(identity.user_id),
        "scope": request.scope,
        "user_scope": "specified" if request.scope == "user" else "none",
        "user_ids": request.user_ids,
        "allow_drilldown": bool(request.allow_drilldown),
        "allow_download": bool(request.allow_download),
        "pin_latest": bool(request.pin_latest),
        "expires_at": expires_at,
    }
    share = await store.create_share(record, token_hash, code_hash)
    await store.record_audit({"tenant_id": identity.tenant_id, "user_id": identity.user_id, "share_id": share.id, "report_id": report_id, "action": "share_create", "result": "success", "metadata": {"scope": request.scope}})
    return {"share_id": share.id, "scope": request.scope, "url": f"/shared/reports/{token}", "expires_at": expires_at, "authorization_code_hint": "已设置授权码" if request.require_code else "", "quota": asdict(quota)}


@router.get("/reports/{report_id}/shares")
# 方法作用：列出当前用户可管理的分享摘要。
# Args: report_id - 报表 ID。
# Returns: 不含 token 和授权码的分享列表。
async def list_report_shares(report_id: str) -> dict[str, Any]:
    identity = get_current_identity()
    await _require_capability(identity, "report.share")
    items = await get_report_store().list_shares(report_id, identity)
    return {"shares": items, "total": len(items)}


@router.delete("/shares/{share_id}")
# 方法作用：撤销分享链接并写入审计。
# Args: share_id - 分享 ID。
# Returns: 撤销状态。
async def revoke_share(share_id: str) -> dict[str, str]:
    identity = get_current_identity()
    await _require_capability(identity, "report.share")
    store = get_report_store()
    if not await store.revoke_share(share_id, identity):
        raise HTTPException(404, "分享不存在或已撤销")
    await store.record_audit({"tenant_id": identity.tenant_id, "user_id": identity.user_id, "share_id": share_id, "action": "share_revoke", "result": "success"})
    return {"status": "revoked"}


@router.post("/reports/{report_id}/drilldowns/{drilldown_id}")
# 方法作用：执行私有报表版本的 snapshot Drilldown，不接受客户端 SQL。
# Args: report_id - 报表 ID；drilldown_id - 页面签发 ID；request - 过滤和分页。
# Returns: 当前版本允许字段的脱敏明细。
async def drilldown(report_id: str, drilldown_id: str, request: DrilldownRequest) -> dict[str, Any]:
    identity = get_current_identity()
    await _require_capability(identity, "report.menu")
    store = get_report_store()
    version = await store.get_report_version(report_id, identity)
    if version is None:
        raise HTTPException(404, "报表不存在或无权访问")
    definition = find_drilldown(version.document, drilldown_id)
    logger.info("私有钻取生命周期探针", report_id=report_id, expired=_is_expired(version.expires_at), definition_found=definition is not None)
    if _is_expired(version.expires_at):
        await store.record_audit({"tenant_id": identity.tenant_id, "user_id": identity.user_id, "report_id": report_id, "action": "drilldown", "result": "expired"})
        raise HTTPException(410, "报表页面已过期")
    if definition is None or not bool(version.document.get("permissions", {}).get("drilldown", False)):
        await store.record_audit({"tenant_id": identity.tenant_id, "user_id": identity.user_id, "report_id": report_id, "action": "drilldown", "result": "denied"})
        raise HTTPException(403, "Drilldown 未授权")
    try:
        if definition.get("mode", "snapshot") == "live":
            result = await _execute_private_live(version, definition, request, identity)
        else:
            result = execute_snapshot_drilldown(definition, version.data_manifest, request.filters, request.page, request.page_size)
    except ValueError as exc:
        await store.record_audit({"tenant_id": identity.tenant_id, "user_id": identity.user_id, "report_id": report_id, "action": "drilldown", "result": "invalid"})
        raise HTTPException(422, str(exc)) from exc
    await store.record_audit({"tenant_id": identity.tenant_id, "user_id": identity.user_id, "report_id": report_id, "action": "drilldown", "result": "success", "metadata": {"drilldown_id": drilldown_id, "row_count": len(result["rows"])}})
    return result


# 方法作用：在实时查询入口重验数据源权限、限流和并发预算，失败时不回退旧权限。
# Args: version - 当前授权页面；definition - 签发定义；request - 客户端参数；identity - 当前身份。
# Returns: 脱敏实时明细。
async def _execute_private_live(version: Any, definition: dict[str, Any], request: DrilldownRequest, identity: Any) -> dict[str, Any]:
    from src.reporting.live_drilldown import execute_live_drilldown
    from src.reporting.models import AnalysisUnit
    from src.security.data_masker import check_rate_limit

    await _require_capability(identity, "report.live_drilldown")
    store = get_report_store()
    query = await store.get_live_query(str(definition.get("query_ref", "")), version, identity)
    if query is None:
        raise HTTPException(403, "实时查询模板不存在或无权访问")
    plan = ReportPlan(analysis_units=[AnalysisUnit(unit_id="live", question="受控实时钻取", datasource=query["datasource"])])
    access = await _resolve_plan_access(identity, plan)
    if query["datasource"] not in access:
        raise HTTPException(403, "实时钻取数据源授权已撤销")
    if not await check_rate_limit(identity.user_id, tenant_id=identity.tenant_id):
        raise HTTPException(429, "实时钻取请求过于频繁")
    quota = get_report_quota_manager()
    reservation = None
    try:
        reservation = await quota.reserve(identity, concurrent=1, queries=1, rows=request.page_size + 1)
        return await execute_live_drilldown(query, access[query["datasource"]], definition, request.filters, request.page, request.page_size, tenant_id=identity.tenant_id)
    except QuotaExceededError as exc:
        raise HTTPException(429, describe_quota_error(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except RuntimeError as exc:
        logger.error("实时钻取执行边界失败", report_id=version.report_id, exc_info=True)
        raise HTTPException(503, "实时查询暂不可用") from exc
    finally:
        if reservation is not None:
            await quota.release_concurrent(identity, period_start=reservation.period_start)


@router.get("/quotas/me")
# 方法作用：返回当前用户剩余报表周期配额。
# Args: 无。
# Returns: 配额快照。
async def get_my_quota() -> dict[str, Any]:
    identity = get_current_identity()
    await _require_capability(identity, "report.menu")
    snapshot = await get_report_quota_manager().snapshot(identity)
    return asdict(snapshot)


@router.get("/permissions/me")
# 方法作用：返回当前身份的报表能力，用于前端菜单和路由守卫，不授予额外权限。
# Args: 无。
# Returns: report.* 能力列表和菜单可见标记。
async def get_my_permissions() -> dict[str, Any]:
    identity = get_current_identity()
    capabilities = await get_report_permission_service().capabilities(identity)
    return {"capabilities": capabilities, "menu_allowed": "report.menu" in capabilities}


# 超级管理员在配置里设定的系统上限：租户管理员只能在它之下配置用户额度。
# 一张表同时驱动校验文案与下发字段，避免两处各写一份而脱钩。
# 格式：策略字段名 -> (配置项名, 中文标签)
_SYSTEM_LIMIT_FIELDS: dict[str, tuple[str, str]] = {
    "max_analysis_units_per_run": ("report_max_units_per_run", "单次分析单元上限"),
    "max_queries_per_run": ("report_max_queries_per_run", "单次查询上限"),
    "max_concurrent_runs": ("report_max_concurrency", "同时运行上限"),
    "max_page_bytes": ("report_max_page_bytes", "单页大小上限"),
}


# 方法作用：读取超级管理员配置的报表系统上限。
# Args: 无。
# Returns: 策略字段名到系统上限的映射。
def _system_quota_limits() -> dict[str, int]:
    from src.config import get_settings

    settings = get_settings()
    return {field: int(getattr(settings, setting_name)) for field, (setting_name, _) in _SYSTEM_LIMIT_FIELDS.items()}


# 方法作用：校验租户管理员提交的用户额度没有越过超级管理员配置的系统上限。
# Args: payload - 待保存的策略字段字典（不含 capabilities）。
# Returns: 无返回值；越界时抛出 422 并指明是哪一项与上限值。
def _validate_within_system_limits(payload: dict[str, Any]) -> None:
    # 「不能超过超级管理员配置的上限」此前从未被任何代码读取与拦截，
    # 租户管理员实际只被前端表单卡在更小的值上。这里把规则真正落到服务端。
    for field_name, ceiling in _system_quota_limits().items():
        if int(payload.get(field_name, 0) or 0) > ceiling:
            label = _SYSTEM_LIMIT_FIELDS[field_name][1]
            raise HTTPException(422, f"{label}不能超过系统上限 {ceiling}")


@router.put("/admin/users/{user_id}/policy")
# 方法作用：由租户管理员设置目标用户报表能力和周期配额。
# Args: user_id - 目标用户；request - 能力与配额配置。
# Returns: 更新后的非敏感配置摘要。
async def set_user_policy(user_id: int, request: QuotaPolicyRequest) -> dict[str, Any]:
    """由当前租户管理员原子替换用户额度和菜单能力。"""
    identity = get_current_identity()
    if str(identity.role) != "tenant_admin":
        raise HTTPException(403, "报表使用额度由当前租户管理员配置")
    if int(user_id) <= 0:
        raise HTTPException(422, "用户 ID 无效")
    if int(identity.tenant_id) <= 0:
        raise HTTPException(403, "当前身份没有租户")
    payload = request.model_dump(exclude={"capabilities"})
    _validate_within_system_limits(payload)
    policy = QuotaPolicy(**payload)
    store = get_report_store()
    try:
        await store.save_user_policy(identity, user_id, policy, request.capabilities)
    except LookupError as exc:
        raise HTTPException(404, "目标用户不存在或不属于当前租户") from exc
    quota = get_report_quota_manager()
    quota.set_policy(int(identity.tenant_id), int(user_id), policy)
    permissions = get_report_permission_service()
    permissions.set_user_capabilities(int(user_id), request.capabilities, tenant_id=int(identity.tenant_id))
    await store.record_audit({"tenant_id": identity.tenant_id, "user_id": identity.user_id, "action": "quota_policy_update", "result": "success", "metadata": {"target_user_id": int(user_id)}})
    return {"user_id": int(user_id), "capabilities": request.capabilities, "policy": request.model_dump(exclude={"capabilities"})}


@router.get("/admin/users/{user_id}/policy")
# 方法作用：向租户管理员展示目标用户的有效配额、能力及周期用量。
# Args: user_id - 当前租户内的用户 ID。
# Returns: 策略、能力和余额摘要。
async def get_user_policy(user_id: int) -> dict[str, Any]:
    """平台管理员不介入租户日常业务配额配置。"""
    identity = get_current_identity()
    if str(identity.role) != "tenant_admin":
        raise HTTPException(403, "报表使用额度由当前租户管理员配置")
    if user_id <= 0:
        raise HTTPException(422, "用户 ID 无效")
    store = get_report_store()
    try:
        user = await store.get_policy_subject(identity, user_id)
    except LookupError as exc:
        raise HTTPException(404, "目标用户不存在或不属于当前租户") from exc
    subject = SimpleNamespace(tenant_id=int(identity.tenant_id), user_id=int(user_id), role=str(user["role"]))
    persisted = await store.get_quota_policy(subject.tenant_id, subject.user_id)
    policy = QuotaPolicyRequest.model_validate(persisted or {})
    capabilities = await get_report_permission_service().capabilities(subject)
    snapshot = await get_report_quota_manager().snapshot(subject)
    return {
        "user_id": user_id,
        "capabilities": capabilities,
        "policy": policy.model_dump(exclude={"capabilities"}),
        # 下发系统上限，供前端把输入框上限对齐到超级管理员的配置，
        # 而不是在表单里写死一个更小的值。
        "limits": _system_quota_limits(),
        "quota": asdict(snapshot),
        "period": "calendar_month",
    }


@router.post("/admin/quota-usage/reset")
# 方法作用：由租户管理员把单个、多个或全租户用户的本月已用量清零，不改动额度上限。
# Args: request - 目标范围（user_ids 与 all_users 互斥）与要清零的字段。
# Returns: 实际清零用户数、实际字段与当前周期起止。
async def reset_quota_usage(request: QuotaUsageResetRequest) -> dict[str, Any]:
    """字段仅限四个按月累计项；个别目标不存在或跨租户时跳过，不让整次请求失败。"""
    identity = get_current_identity()
    tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
    operator_id = int(getattr(identity, "user_id", 0) or 0)
    logger.debug("报表额度重置路由入口", tenant_id=tenant_id, user_id=operator_id, all_users=bool(request.all_users), target_count=len(request.user_ids), fields=list(request.fields))
    if str(identity.role) != "tenant_admin":
        raise HTTPException(403, "报表使用额度由当前租户管理员配置")
    if tenant_id <= 0:
        raise HTTPException(403, "当前身份没有租户")
    store = get_report_store()
    try:
        period_start, period_end, cleared = await get_report_quota_manager().reset_usage(
            identity,
            list(request.user_ids),
            list(request.fields),
            all_users=bool(request.all_users),
        )
    except ValueError as exc:
        logger.warning("报表额度重置字段非法", tenant_id=tenant_id, error=str(exc))
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logger.error("报表额度重置失败", tenant_id=tenant_id, user_id=operator_id, exc_info=True)
        raise HTTPException(503, "额度重置暂不可用") from exc
    # 每个被实际清零的用户各写一条审计；跨租户与本期无用量行的目标不产生记录。
    for target_user_id in cleared:
        await store.record_audit({"tenant_id": identity.tenant_id, "user_id": identity.user_id, "action": "quota_usage_reset", "result": "success", "metadata": {"target_user_id": int(target_user_id), "fields": list(request.fields)}})
    logger.info("报表额度重置路由完成", tenant_id=tenant_id, user_id=operator_id, reset_users=len(cleared), all_users=bool(request.all_users), fields=list(request.fields))
    return QuotaUsageResetResponse(reset_users=len(cleared), fields=list(request.fields), period_start=period_start, period_end=period_end).model_dump()


@router.get("/shared/reports/{token}")
# 方法作用：通过高熵 token 访问脱敏报表快照，公开路由不暴露数据库或权限信息。
# Args: token - URL 中的分享 token；share_session - 授权码解锁会话。
# Returns: 脱敏页面和允许的快照数据。
async def get_shared_report(token: str, share_session: str | None = Header(default=None, alias="X-Report-Share-Session")) -> dict[str, Any]:
    store = get_report_store()
    token_hash = hash_share_token(token)
    share = await store.get_share_by_token(token_hash)
    if not share or share.get("revoked_at") is not None:
        await store.record_audit({"action": "share_access", "result": "not_found"})
        raise HTTPException(404, "分享链接不存在或已撤销")
    expires_at = share.get("expires_at")
    if _is_expired(expires_at):
        await store.record_audit({"tenant_id": share.get("tenant_id"), "share_id": share.get("id"), "action": "share_access", "result": "expired"})
        raise HTTPException(410, "分享链接已过期")
    code_hash = str(share.get("code_hash", "") or "")
    if code_hash:
        session_data = _unlock_sessions.get(str(share_session or ""))
        valid_session = bool(session_data and session_data[0] == str(share["id"]) and session_data[1] > datetime.now(timezone.utc))
        if not valid_session:
            await store.record_audit({"tenant_id": share.get("tenant_id"), "share_id": share.get("id"), "action": "share_access", "result": "locked"})
            raise HTTPException(401, "分享链接需要授权码解锁")
    version = await store.get_version_by_id(str(share.get("report_version_id", "")))
    if version is None:
        raise HTTPException(404, "报表版本不存在")
    if _is_expired(version.expires_at):
        await store.record_audit({"tenant_id": share.get("tenant_id"), "share_id": share.get("id"), "action": "share_access", "result": "expired"})
        raise HTTPException(410, "报表页面已过期")
    if str(share.get("scope", "public")) == "user":
        identity = get_current_identity()
        if not _can_access_share_scope(identity, share):
            await store.record_audit({"tenant_id": share.get("tenant_id"), "share_id": share.get("id"), "action": "share_access", "result": "denied"})
            raise HTTPException(403, "当前用户不在分享授权范围内")
    document = dict(version.document)
    document["drilldowns"] = [item for item in document.get("drilldowns", []) if item.get("mode", "snapshot") == "snapshot"]
    if not bool(share.get("allow_drilldown", False)):
        document["drilldowns"] = []
        document["permissions"] = {**document.get("permissions", {}), "drilldown": False}
    await store.record_audit({"tenant_id": share.get("tenant_id"), "share_id": share.get("id"), "report_id": version.report_id, "action": "share_access", "result": "success", "metadata": {"scope": share.get("scope", "public")}})
    return {"report_id": version.report_id, "version": version.version, "document": document, "data_manifest": version.data_manifest if bool(share.get("allow_drilldown", False)) else {}}


@router.post("/shared/reports/{token}/drilldowns/{drilldown_id}")
# 方法作用：执行公开分享允许的 snapshot Drilldown，并复用分享授权码与范围校验。
# Args: token - 分享 token；drilldown_id - 页面签发的 Drilldown ID；request - 过滤和分页；share_session - 短时授权会话。
# Returns: 当前分享版本允许字段的脱敏明细。
async def shared_drilldown(
    token: str,
    drilldown_id: str,
    request: DrilldownRequest,
    share_session: str | None = Header(default=None, alias="X-Report-Share-Session"),
) -> dict[str, Any]:
    store = get_report_store()
    token_hash = hash_share_token(token)
    share = await store.get_share_by_token(token_hash)
    if not share or share.get("revoked_at") is not None:
        await store.record_audit({"action": "share_drilldown", "result": "not_found"})
        raise HTTPException(404, "分享链接不存在或已撤销")
    expires_at = share.get("expires_at")
    if _is_expired(expires_at):
        await store.record_audit({"tenant_id": share.get("tenant_id"), "share_id": share.get("id"), "action": "share_drilldown", "result": "expired"})
        raise HTTPException(410, "分享链接已过期")
    code_hash = str(share.get("code_hash", "") or "")
    if code_hash:
        session_data = _unlock_sessions.get(str(share_session or ""))
        valid_session = bool(session_data and session_data[0] == str(share["id"]) and session_data[1] > datetime.now(timezone.utc))
        if not valid_session:
            await store.record_audit({"tenant_id": share.get("tenant_id"), "share_id": share.get("id"), "action": "share_drilldown", "result": "locked"})
            raise HTTPException(401, "分享链接需要授权码解锁")
    if not bool(share.get("allow_drilldown", False)):
        await store.record_audit({"tenant_id": share.get("tenant_id"), "share_id": share.get("id"), "action": "share_drilldown", "result": "denied"})
        raise HTTPException(403, "分享未授权明细钻取")
    if str(share.get("scope", "public")) == "user":
        identity = get_current_identity()
        if not _can_access_share_scope(identity, share):
            await store.record_audit({"tenant_id": share.get("tenant_id"), "share_id": share.get("id"), "action": "share_drilldown", "result": "denied"})
            raise HTTPException(403, "当前用户不在分享授权范围内")
    version = await store.get_version_by_id(str(share.get("report_version_id", "")))
    if version is None:
        raise HTTPException(404, "报表版本不存在")
    if _is_expired(version.expires_at):
        await store.record_audit({"tenant_id": share.get("tenant_id"), "share_id": share.get("id"), "action": "share_drilldown", "result": "expired"})
        raise HTTPException(410, "报表页面已过期")
    definition = find_drilldown(version.document, drilldown_id)
    if definition is None or str(definition.get("mode", "snapshot")) != "snapshot":
        await store.record_audit({"tenant_id": share.get("tenant_id"), "share_id": share.get("id"), "report_id": version.report_id, "action": "share_drilldown", "result": "invalid"})
        raise HTTPException(422, "Drilldown 定义无效")
    try:
        result = execute_snapshot_drilldown(definition, version.data_manifest, request.filters, request.page, request.page_size)
    except ValueError as exc:
        await store.record_audit({"tenant_id": share.get("tenant_id"), "share_id": share.get("id"), "report_id": version.report_id, "action": "share_drilldown", "result": "invalid"})
        raise HTTPException(422, str(exc)) from exc
    await store.record_audit({"tenant_id": share.get("tenant_id"), "share_id": share.get("id"), "report_id": version.report_id, "action": "share_drilldown", "result": "success", "metadata": {"drilldown_id": drilldown_id, "row_count": len(result["rows"])}})
    return result


@router.post("/shared/reports/{token}/unlock")
# 方法作用：验证授权码并签发短时内存会话，不把明文授权码写入存储。
# Args: token - 分享 token；request - 用户提交的授权码。
# Returns: 短时授权会话。
async def unlock_shared_report(token: str, request: AuthorizationCodeRequest) -> dict[str, Any]:
    store = get_report_store()
    token_hash = hash_share_token(token)
    share = await store.get_share_by_token(token_hash)
    if not share or not verify_authorization_code(request.authorization_code, str(share.get("code_hash", "") or "")):
        await store.record_audit({"tenant_id": share.get("tenant_id") if share else None, "share_id": share.get("id") if share else None, "action": "share_unlock", "result": "denied"})
        raise HTTPException(401, "授权码错误或分享不存在")
    session = secrets.token_urlsafe(24)
    _unlock_sessions[session] = (str(share["id"]), datetime.now(timezone.utc) + timedelta(minutes=15))
    await store.record_audit({"tenant_id": share.get("tenant_id"), "share_id": share.get("id"), "action": "share_unlock", "result": "success"})
    return {"session_token": session, "expires_in_seconds": 900}


# 方法作用：解析客户端 IP 并做不可逆摘要，供访问明细与审计使用。
# Args: request - 当前 HTTP 请求。
# Returns: IP 摘要；无法解析时返回空字符串。
def _client_ip_hash(request: Request) -> str:
    # 访问策略中间件已解析可信代理链，缺失时回退到直连地址。
    client_ip = str(getattr(request.state, "client_ip", "") or "")
    if not client_ip:
        client = getattr(request, "client", None)
        client_ip = str(getattr(client, "host", "") or "")
    return hash_client_ip(client_ip)


# 方法作用：按分享 token 读取未撤销且未过期的分享，供访问上报复用同一套 404/410 语义。
# Args: store - 报表 Store；token - URL 中的分享 token。
# Returns: 分享记录字典。
async def _require_share_for_visit(store: Any, token: str) -> dict[str, Any]:
    share = await store.get_share_by_token(hash_share_token(token), count_view=False)
    if not share or share.get("revoked_at") is not None:
        await store.record_audit({"action": "share_visit", "result": "not_found"})
        raise HTTPException(404, "分享链接不存在或已撤销")
    if _is_expired(share.get("expires_at")):
        await store.record_audit({"tenant_id": share.get("tenant_id"), "share_id": share.get("id"), "action": "share_visit", "result": "expired"})
        raise HTTPException(410, "分享链接已过期")
    return share


@router.post("/shared/reports/{token}/visits")
# 方法作用：公开侧上报一次分享访问，按租户开关决定是否记录登录身份。
# Args: token - 分享 token；request - 当前请求（客户端 IP）；payload - 可选授权码解锁标记。
# Returns: 访问标识与实际记录的访客类型。
async def create_share_visit_report(token: str, request: Request, payload: ShareVisitCreateRequest = ShareVisitCreateRequest()) -> dict[str, Any]:
    """匿名访问同样计入访问量与时长，只是不记录用户身份。"""
    store = get_report_store()
    logger.debug("分享访问上报入口", token_prefix=token[:6], unlocked=bool(payload.unlocked))
    share = await _require_share_for_visit(store, token)
    identity = get_current_identity()
    tenant_id = int(share.get("tenant_id", 0) or 0)
    visitor_kind = "anonymous"
    visitor_user_id = 0
    if int(getattr(identity, "user_id", 0) or 0) > 0 and str(getattr(identity, "role", "") or "") != "anonymous":
        # 需求 3.2：租户关闭身份记录时，携带登录身份也按匿名记录，不写 user_id。
        settings = await store.get_tenant_settings(tenant_id)
        if bool(settings.get("record_share_visitor_identity", True)):
            visitor_kind = "user"
            visitor_user_id = int(identity.user_id)
        else:
            logger.info("分享访问按匿名记录", share_id=share.get("id"), tenant_id=tenant_id, reason="租户关闭分享身份记录")
    record = await store.create_share_visit(share, tenant_id, visitor_kind, visitor_user_id, _client_ip_hash(request), bool(payload.unlocked))
    await store.record_audit({"tenant_id": tenant_id, "share_id": share.get("id"), "report_id": share.get("report_id"), "action": "share_visit", "result": "success", "ip_hash": str(record.get("ip_hash", "")), "metadata": {"visitor_kind": visitor_kind, "unlocked": bool(payload.unlocked)}})
    logger.info("分享访问上报完成", share_id=share.get("id"), visit_id=record.get("id"), visitor_kind=visitor_kind)
    return {"visit_id": str(record.get("id", "")), "visitor_kind": visitor_kind}


@router.patch("/shared/reports/{token}/visits/{visit_id}")
# 方法作用：上报累积停留时长，幂等覆盖并刷新最近可见时间。
# Args: token - 分享 token；visit_id - 访问标识；payload - 累积秒数。
# Returns: 访问标识与写入后的累积秒数。
async def update_share_visit_duration(token: str, visit_id: str, payload: ShareVisitUpdateRequest) -> dict[str, Any]:
    """只校验 token 归属与撤销状态：页面卸载时的最后一笔时长仍允许落库。"""
    store = get_report_store()
    logger.debug("分享停留时长上报入口", visit_id=visit_id, duration_seconds=int(payload.duration_seconds))
    record = await store.update_share_visit(hash_share_token(token), visit_id, int(payload.duration_seconds))
    if record is None:
        raise HTTPException(404, "访问记录不存在或不属于该分享链接")
    logger.info("分享停留时长上报完成", visit_id=visit_id, duration_seconds=int(record.get("duration_seconds", 0) or 0))
    return {"visit_id": str(record.get("id", "")), "duration_seconds": int(record.get("duration_seconds", 0) or 0)}


@router.get("/shares")
# 方法作用：按可见范围返回租户内分享平铺列表，含访问计数与停留时长。
# Args: report_id - 可选报表筛选；created_by - 可选创建人筛选（仅租户管理员）；status - 有效状态筛选；page/page_size - 分页。
# Returns: 列表行、总数与分页参数。
async def list_shares(
    report_id: str | None = Query(default=None),
    created_by: int | None = Query(default=None, ge=1),
    status: Literal["active", "revoked", "expired"] | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1),
) -> dict[str, Any]:
    """需求 3.3：租户管理员看全租户并可按创建人筛选，其他角色只看自己创建的。"""
    identity = get_current_identity()
    role = str(getattr(identity, "role", "") or "")
    if created_by is not None and role != "tenant_admin":
        raise HTTPException(403, "仅租户管理员可按创建人筛选分享链接")
    # page_size 超上限按 100 夹紧而不是报错，避免前端分页参数越界直接失败。
    bounded_size = max(1, min(int(page_size), 100))
    items, total = await get_report_store().list_tenant_shares(identity, report_id=report_id, created_by=created_by, status=status, page=page, page_size=bounded_size)
    logger.info("链接管理列表完成", tenant_id=identity.tenant_id, user_id=identity.user_id, total=total, returned=len(items))
    return {"shares": items, "total": total, "page": page, "page_size": bounded_size}


@router.get("/shares/{share_id}/visits")
# 方法作用：返回该分享最近访问的系统用户及其停留时长。
# Args: share_id - 分享标识。
# Returns: 访问者列表与条数。
async def list_share_visitors(share_id: str) -> dict[str, Any]:
    """权限与该分享的可见范围一致：租户管理员可看任意，其他人只看自己创建的。"""
    identity = get_current_identity()
    visits = await get_report_store().list_share_visitors(share_id, identity)
    if visits is None:
        raise HTTPException(404, "分享不存在或无权访问")
    logger.info("分享访问者列表完成", share_id=share_id, user_id=identity.user_id, visit_count=len(visits))
    return {"visits": visits, "total": len(visits)}


@router.post("/shares/batch-revoke")
# 方法作用：批量撤销可见范围内的分享，个别标识无权限不影响整体结果。
# Args: payload - 待撤销的分享标识列表（1..100）。
# Returns: 成功撤销数与跳过数。
async def batch_revoke_shares(payload: BatchRevokeSharesRequest) -> dict[str, int]:
    """skipped 计入不在可见范围、已撤销或不存在的标识。"""
    identity = get_current_identity()
    store = get_report_store()
    logger.debug("批量撤销分享入口", tenant_id=identity.tenant_id, user_id=identity.user_id, request_count=len(payload.share_ids))
    revoked, skipped = await store.revoke_shares(payload.share_ids, identity)
    await store.record_audit({"tenant_id": identity.tenant_id, "user_id": identity.user_id, "action": "share_revoke_batch", "result": "success", "metadata": {"requested": len(payload.share_ids), "revoked": revoked, "skipped": skipped}})
    logger.info("批量撤销分享完成", tenant_id=identity.tenant_id, revoked=revoked, skipped=skipped)
    return {"revoked": revoked, "skipped": skipped}


# 方法作用：校验当前身份是租户管理员且归属明确租户。
# Args: identity - 当前请求身份。
# Returns: 租户标识；不满足时抛出 HTTP 403。
def _require_tenant_admin_identity(identity: Any) -> int:
    if str(getattr(identity, "role", "") or "") != "tenant_admin":
        logger.warning("租户报表设置授权拒绝", tenant_id=getattr(identity, "tenant_id", 0), user_id=getattr(identity, "user_id", 0))
        raise HTTPException(403, "租户级报表设置由当前租户管理员维护")
    tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
    if tenant_id <= 0:
        raise HTTPException(403, "当前身份没有租户")
    return tenant_id


@router.get("/admin/tenant-settings")
# 方法作用：读取当前租户的报表设置。
# Args: 无。
# Returns: 身份记录开关与永久报表上限。
async def get_tenant_report_settings() -> dict[str, Any]:
    """GET 与 PUT 同样只开放给租户管理员。"""
    identity = get_current_identity()
    tenant_id = _require_tenant_admin_identity(identity)
    stored = await get_report_store().get_tenant_settings(tenant_id)
    settings = TenantReportSettings(
        record_share_visitor_identity=bool(stored.get("record_share_visitor_identity", True)),
        max_permanent_reports_per_user=int(stored.get("max_permanent_reports_per_user", 0) or 0),
    )
    logger.info("租户报表设置读取完成", tenant_id=tenant_id, user_id=identity.user_id)
    return settings.model_dump(mode="json")


@router.put("/admin/tenant-settings")
# 方法作用：保存当前租户的报表设置并写入审计。
# Args: payload - 身份记录开关与永久报表上限。
# Returns: 保存后的设置。
async def update_tenant_report_settings(payload: TenantReportSettings) -> dict[str, Any]:
    """max_permanent_reports_per_user 为 0 表示不限制，负数在契约层即被拒绝。"""
    identity = get_current_identity()
    tenant_id = _require_tenant_admin_identity(identity)
    store = get_report_store()
    stored = await store.save_tenant_settings(tenant_id, payload.model_dump(), int(identity.user_id))
    await store.record_audit({"tenant_id": tenant_id, "user_id": identity.user_id, "action": "tenant_settings_update", "result": "success", "metadata": payload.model_dump()})
    settings = TenantReportSettings(
        record_share_visitor_identity=bool(stored.get("record_share_visitor_identity", True)),
        max_permanent_reports_per_user=int(stored.get("max_permanent_reports_per_user", 0) or 0),
    )
    logger.info("租户报表设置保存完成", tenant_id=tenant_id, user_id=identity.user_id)
    return settings.model_dump(mode="json")
