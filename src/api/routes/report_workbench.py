"""报表工作台 Phase 1 API：计划、任务、私有页面、分享和快照 Drilldown。"""

from __future__ import annotations

import secrets
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from src.api.auth import get_current_identity, require_tenant_admin
from src.api.routes._helpers import _call_with_optional_tenant
from src.app_context import get_tenant_policy
from src.datasource.registry import get_registry
from src.logging_config import get_logger
from src.reporting.drilldown import execute_snapshot_drilldown, find_drilldown
from src.reporting.models import (
    AuthorizationCodeRequest,
    DrilldownRequest,
    QuotaPolicyRequest,
    ReportJobCreateRequest,
    ReportPlan,
    ReportPlanRequest,
    ReportShareCreateRequest,
)
from src.reporting.planner import ReportPlanner
from src.reporting.quota import (
    QuotaExceededError,
    QuotaManager,
    QuotaPolicy,
    ReportPermissionService,
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
def _plan_response(plan: ReportPlan) -> dict[str, Any]:
    return {"plan": plan.model_dump(mode="json"), "plan_hash": plan.canonical_hash(), "estimated": plan.estimated.model_dump(mode="json"), "job_id": None}


@router.post("/plans")
# 方法作用：解析自然语言并返回报表计划预览，不产生任务或扣运行配额。
# Args: request - 计划预览请求。
# Returns: 可供确认的结构化计划。
async def preview_plan(request: ReportPlanRequest) -> dict[str, Any]:
    identity = get_current_identity()
    await _require_capability(identity, "report.menu")
    planner = ReportPlanner()
    plan = await planner.parse(request.prompt, datasources=request.datasources, timezone=request.timezone)
    await _resolve_plan_access(identity, plan)
    _plan_cache[(int(identity.tenant_id), int(identity.user_id), plan.canonical_hash())] = plan
    logger.info("报表计划预览完成", tenant_id=identity.tenant_id, user_id=identity.user_id, plan_hash=plan.canonical_hash())
    return _plan_response(plan)


@router.post("/jobs", status_code=202)
# 方法作用：按确认且未被篡改的计划创建立即报表任务并后台启动运行。
# Args: request - 计划创建请求。
# Returns: Job、Run 排队摘要和剩余配额。
async def create_job(request: ReportJobCreateRequest) -> dict[str, Any]:
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
    if plan.needs_confirmation and not request.confirm:
        raise HTTPException(409, "该计划需要用户确认后才能执行")
    if plan.execution_mode != "immediate":
        raise HTTPException(422, "定时调度将在 Phase 2 开放，请先使用立即执行")
    if plan.permanent:
        await _require_capability(identity, "report.permanent")
    if plan.share.enabled:
        await _require_capability(identity, "report.share")
    access = await _resolve_plan_access(identity, plan)
    quota_manager = get_report_quota_manager()
    try:
        quota = await quota_manager.reserve(identity, jobs=1, units=0, tokens=0, timezone_name=plan.timezone)
    except QuotaExceededError as exc:
        raise HTTPException(429, "本周期报表任务配额已用尽") from exc
    store = get_report_store()
    job = await store.create_job(identity, request.prompt, plan, request.idempotency_key)
    from src.reporting.runner import get_report_runner

    get_report_runner().enqueue(job.id, identity, authorized_datasources=access)
    logger.info("报表任务创建并排队完成", job_id=job.id, tenant_id=identity.tenant_id, user_id=identity.user_id)
    return {"job_id": job.id, "status": "queued", "execution_mode": plan.execution_mode, "run_id": None, "report_id": None, "estimated": plan.estimated.model_dump(mode="json"), "quota": asdict(quota)}


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
    identity = get_current_identity()
    await _require_capability(identity, "report.create")
    job = await get_report_store().get_job(job_id, identity)
    if job is None:
        raise HTTPException(404, "报表任务不存在")
    if job.plan.execution_mode != "immediate":
        raise HTTPException(422, "定时任务将在 Phase 2 执行")
    from src.reporting.runner import get_report_runner

    get_report_runner().enqueue(job_id, identity)
    return {"job_id": job_id, "status": "queued"}


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
# 方法作用：读取当前用户可访问且未到期的最新私有报表页面。
# Args: report_id - 报表 ID。
# Returns: 页面文档、版本和数据摘要。
async def get_report(report_id: str) -> dict[str, Any]:
    identity = get_current_identity()
    await _require_capability(identity, "report.menu")
    version = await get_report_store().get_report_version(report_id, identity)
    if version is None:
        raise HTTPException(404, "报表不存在或无权访问")
    if _is_expired(version.expires_at):
        raise HTTPException(410, "报表页面已过期")
    return {"report_id": version.report_id, "version": version.version, "document": version.document, "data_manifest": version.data_manifest}


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
        raise HTTPException(429, "本周期分享链接配额已用尽") from exc
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
    if definition is None or not bool(version.document.get("permissions", {}).get("drilldown", False)):
        await store.record_audit({"tenant_id": identity.tenant_id, "user_id": identity.user_id, "report_id": report_id, "action": "drilldown", "result": "denied"})
        raise HTTPException(403, "Drilldown 未授权")
    try:
        result = execute_snapshot_drilldown(definition, version.data_manifest, request.filters, request.page, request.page_size)
    except ValueError as exc:
        await store.record_audit({"tenant_id": identity.tenant_id, "user_id": identity.user_id, "report_id": report_id, "action": "drilldown", "result": "invalid"})
        raise HTTPException(422, str(exc)) from exc
    await store.record_audit({"tenant_id": identity.tenant_id, "user_id": identity.user_id, "report_id": report_id, "action": "drilldown", "result": "success", "metadata": {"drilldown_id": drilldown_id, "row_count": len(result["rows"])}})
    return result


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


@router.put("/admin/users/{user_id}/policy")
# 方法作用：由租户管理员设置目标用户报表能力和周期配额。
# Args: user_id - 目标用户；request - 能力与配额配置。
# Returns: 更新后的非敏感配置摘要。
async def set_user_policy(user_id: int, request: QuotaPolicyRequest) -> dict[str, Any]:
    identity = get_current_identity()
    require_tenant_admin()
    if int(user_id) <= 0:
        raise HTTPException(422, "用户 ID 无效")
    if int(identity.tenant_id) <= 0:
        raise HTTPException(403, "当前身份没有租户")
    policy = QuotaPolicy(**request.model_dump(exclude={"capabilities"}))
    quota = get_report_quota_manager()
    quota.set_policy(int(identity.tenant_id), int(user_id), policy)
    permissions = get_report_permission_service()
    permissions.set_user_capabilities(int(user_id), request.capabilities, tenant_id=int(identity.tenant_id))
    store = get_report_store()
    await store.set_quota_policy(int(identity.tenant_id), int(user_id), policy)
    await store.set_user_capabilities(int(identity.tenant_id), int(user_id), request.capabilities)
    await store.record_audit({"tenant_id": identity.tenant_id, "user_id": identity.user_id, "action": "quota_policy_update", "result": "success", "metadata": {"target_user_id": int(user_id)}})
    return {"user_id": int(user_id), "capabilities": request.capabilities, "policy": request.model_dump(exclude={"capabilities"})}


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
