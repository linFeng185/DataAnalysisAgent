"""Schema 查询与标注路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from src.api.routes._helpers import _call_with_optional_tenant
from src.api.schemas import ColumnCommentRequest, TableInfo
from src.exceptions import DataSourceNotFoundError
from src.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()


# 启动前复用现有数据源可见性与写权限校验。
# Args: datasource - 数据源名称；write - 是否为写操作。
# Returns: 当前租户和已授权数据源名。
async def _sync_scope(datasource: str, *, write: bool = False) -> tuple[int, str]:
    from src.api.auth import get_current_identity, require_tenant_admin
    if write:
        require_tenant_admin()
    name = await _resolve_datasource_name(datasource)
    if not name:
        raise HTTPException(404, "当前身份没有可用数据源")
    return get_current_identity().tenant_id, name


# 查询当前数据源跨 Worker 同步状态。
# Args: datasource - 当前数据源。
# Returns: 任务状态，无任务时为 idle。
@router.get("/schema/sync")
async def schema_sync_status(datasource: str = Query(..., min_length=1, max_length=64)) -> dict:
    """读状态同样执行当前租户数据源 ACL。"""
    from src.knowledge.schema_sync import get_schema_sync
    tenant, name = await _sync_scope(datasource)
    return await get_schema_sync().store.state(tenant, name)


# 为已授权数据源启动共享互斥的后台同步。
# Args: datasource - 当前数据源。
# Returns: 启动后的共享任务状态。
@router.post("/schema/sync", status_code=202)
async def start_schema_sync(datasource: str = Query(..., min_length=1, max_length=64)) -> dict:
    """重复启动返回 409，不创建第二个实际同步任务。"""
    from src.knowledge.schema_sync import get_schema_sync
    tenant, name = await _sync_scope(datasource, write=True)
    return await get_schema_sync().start(tenant, name)


# 向拥有任务的 Worker 发送持久化终止请求。
# Args: datasource - 当前数据源。
# Returns: cancelling 或既有终态。
@router.post("/schema/sync/cancel")
async def cancel_schema_sync(datasource: str = Query(..., min_length=1, max_length=64)) -> dict:
    """真正退出采集后才释放互斥；发布中不能取消。"""
    from src.knowledge.schema_sync import get_schema_sync
    tenant, name = await _sync_scope(datasource, write=True)
    return await get_schema_sync().store.cancel(tenant, name)


# 方法作用：在未显式传入数据源时选择当前身份可访问的第一个数据源。
# Args: datasource - 请求中的可选数据源名称。
# Returns: 规范化数据源名称；无可用数据源时返回空字符串。
async def _resolve_datasource_name(datasource: str) -> str:
    normalized = str(datasource or "").strip()
    import src.api.routes as routes_package
    from src.api.auth import get_current_identity
    from src.app_context import get_tenant_policy
    from src.security.permission_check import resolve_datasource_access

    identity = get_current_identity()
    registry = routes_package._registry()
    if not hasattr(registry, "list_all"):
        logger.warning("Schema Registry 缺少列表能力，保留显式数据源兼容路径")
        return normalized
    items = await _call_with_optional_tenant(
        registry.list_all,
        tenant_id=identity.tenant_id,
    )
    if not items:
        logger.info("Schema 默认数据源为空", reason="当前身份没有可访问数据源")
        if normalized:
            raise HTTPException(404, f"数据源 '{normalized}' 未找到")
        return ""
    available_names = {
        str(item.get("name", "") or "").strip()
        for item in items
    }
    if normalized and normalized not in available_names:
        logger.warning(
            "Schema 数据源不可见",
            datasource=normalized,
            tenant_id=identity.tenant_id,
        )
        raise HTTPException(404, f"数据源 '{normalized}' 未找到")
    try:
        access = await resolve_datasource_access(
            items,
            [normalized] if normalized else [],
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            role=identity.role,
            tenant_policy=get_tenant_policy(),
        )
    except PermissionError as exc:
        if normalized:
            logger.warning("Schema 数据源权限拒绝", datasource=normalized, tenant_id=identity.tenant_id)
            raise HTTPException(403, "无权访问该数据源") from exc
        logger.info("Schema 默认数据源为空", reason="当前身份没有授权数据源")
        return ""
    selected = normalized or next(iter(access), "")
    logger.info("Schema 默认数据源选择完成", datasource=selected, candidate_count=len(access))
    return selected


def _schema_manager():
    """获取全局 SchemaManager 实例。

    Returns:
        SchemaManager 单例。
    """
    from src.knowledge.schema_manager import get_schema_manager
    return get_schema_manager()


# ---- Schema (11.1.3-6) ----

@router.get("/schema/tables")
async def list_tables(
    datasource: str = Query(default=""),
    page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
    search: str = Query(default=""),
) -> dict:
    datasource = await _resolve_datasource_name(datasource)
    if not datasource:
        return {"tables": [], "datasource": "", "total": 0, "page": page, "page_size": page_size}
    try:
        import src.api.routes as routes_package
        from src.api.auth import get_current_identity

        identity = get_current_identity()
        ds = await _call_with_optional_tenant(
            routes_package._registry().resolve,
            datasource,
            tenant_id=identity.tenant_id,
        )
    except DataSourceNotFoundError as exc:
        raise HTTPException(404, f"数据源 '{datasource}' 未找到") from exc
    if ds.schema is None:
        logger.info("Schema 路由触发延迟内省", datasource=datasource)
        ds.schema = await _call_with_optional_tenant(
            routes_package._schema_manager().get_or_fetch_schema,
            datasource,
            tenant_id=identity.tenant_id,
        )
    tables = []
    for t in (ds.schema.tables if ds.schema else []):
        if search and search.lower() not in t.name.lower():
            continue
        tables.append(TableInfo(name=t.name, description=t.description,
            columns=[{"name": c.name, "type": c.type, "comment": c.comment,
                      "is_indexed": c.is_indexed, "is_primary_key": c.is_primary_key}
                     for c in t.columns],
            row_count_estimate=t.row_count_estimate))
    total = len(tables)
    start = (page - 1) * page_size
    return {"tables": tables[start:start+page_size], "datasource": datasource,
            "total": total, "page": page, "page_size": page_size}


@router.get("/schema/tables/{table_name}")
async def get_table(table_name: str, datasource: str = Query(default="")):
    datasource = await _resolve_datasource_name(datasource)
    if not datasource:
        raise HTTPException(404, "当前身份没有可用数据源")
    try:
        import src.api.routes as routes_package
        from src.api.auth import get_current_identity

        identity = get_current_identity()
        ds = await _call_with_optional_tenant(
            routes_package._registry().resolve,
            datasource,
            tenant_id=identity.tenant_id,
        )
    except DataSourceNotFoundError as exc:
        raise HTTPException(404, f"数据源 '{datasource}' 未找到") from exc
    if ds.schema is None:
        logger.info("表详情路由触发延迟内省", datasource=datasource)
        ds.schema = await _call_with_optional_tenant(
            routes_package._schema_manager().get_or_fetch_schema,
            datasource,
            tenant_id=identity.tenant_id,
        )
    for t in (ds.schema.tables if ds.schema else []):
        if t.name == table_name:
            return TableInfo(name=t.name, description=t.description,
                columns=[{"name": c.name, "type": c.type, "comment": c.comment,
                          "is_nullable": c.is_nullable, "is_primary_key": c.is_primary_key}
                         for c in t.columns],
                row_count_estimate=t.row_count_estimate)
    raise HTTPException(404, f"表 '{table_name}' 未找到")


@router.post("/schema/refresh")
async def refresh_schema(datasource: str = Query(default="")):
    """验证数据源后真实刷新 Schema 缓存。

    Args:
        datasource: 数据源名称。

    Returns:
        刷新状态和表数量。
    """
    datasource = await _resolve_datasource_name(datasource)
    logger.debug("Schema 刷新路由入口", datasource=datasource)
    if not datasource:
        raise HTTPException(404, "当前身份没有可用数据源")
    from src.api.auth import require_tenant_admin

    require_tenant_admin()
    try:
        import src.api.routes as routes_package
        from src.api.auth import get_current_identity

        identity = get_current_identity()
        await _call_with_optional_tenant(
            routes_package._registry().resolve,
            datasource,
            tenant_id=identity.tenant_id,
        )
    except DataSourceNotFoundError as exc:
        logger.warning("Schema 刷新数据源不存在", datasource=datasource)
        raise HTTPException(404, f"数据源 '{datasource}' 未找到") from exc
    from src.knowledge.schema_sync import get_schema_sync
    result = await get_schema_sync().start(identity.tenant_id, datasource, wait=True)
    if result["status"] != "succeeded":
        raise HTTPException(409 if result["status"] == "cancelled" else 503, result.get("error") or "表结构同步未完成")
    return {**result, "status": "ok", "message": "同步完成"}



@router.put("/schema/tables/{table_name}/columns/{column_name}/comment")
async def update_column_comment(
    table_name: str, column_name: str, req: ColumnCommentRequest,
    datasource: str = Query(default=""),
):
    """更新指定数据源中的字段备注。

    Args:
        table_name: 表名。
        column_name: 字段名。
        req: 备注请求体。
        datasource: 数据源名称。

    Returns:
        更新后的字段备注摘要。
    """
    datasource = await _resolve_datasource_name(datasource)
    logger.debug("字段备注路由入口", datasource=datasource, table=table_name, column=column_name)
    if not datasource:
        raise HTTPException(404, "当前身份没有可用数据源")
    from src.api.auth import require_tenant_admin

    require_tenant_admin()
    try:
        import src.api.routes as routes_package
        from src.api.auth import get_current_identity

        identity = get_current_identity()
        await _call_with_optional_tenant(
            routes_package._registry().resolve,
            datasource,
            tenant_id=identity.tenant_id,
        )
    except DataSourceNotFoundError as exc:
        raise HTTPException(404, f"数据源 '{datasource}' 未找到") from exc
    updated = await _call_with_optional_tenant(
        routes_package._schema_manager().update_column_comment,
        datasource,
        table_name,
        column_name,
        req.comment,
        tenant_id=identity.tenant_id,
    )
    if not updated:
        raise HTTPException(404, f"字段 '{table_name}.{column_name}' 未找到")
    from src.app_context import get_app_context
    service = get_app_context().get_resource("schema_sync")
    if service is not None:
        await service.store.annotate(identity.tenant_id, datasource, table_name, column_name, req.comment)
    logger.info("字段备注路由完成", datasource=datasource, table=table_name, column=column_name)
    return {"status": "ok", "table": table_name, "column": column_name, "comment": req.comment}
