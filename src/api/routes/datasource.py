"""数据源管理路由。"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query

from src.api.schemas import (
    DataSourceCreateRequest, DataSourceInfo,
)
from src.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()
_started_at = time.time()


# ---- 数据源管理 (2.3.7-9) ----

@router.post("/datasources", status_code=201)
async def register_datasource(req: DataSourceCreateRequest):
    """把外部数据源注册到全局 Provider/Registry。

    Args:
        req: 数据源注册请求体。

    Returns:
        已注册数据源摘要。
    """
    from src.datasource.providers.external import ExternalDataSourceProvider
    import src.api.routes as routes_package

    registry = routes_package._registry()
    provider = registry.get_provider("external")
    if provider is None:
        provider = ExternalDataSourceProvider()
        registry.register_provider("external", provider)
    ds = await provider.register(req)
    registry.invalidate(ds.name)
    logger.info("数据源注册路由完成", datasource=ds.name)
    return DataSourceInfo(name=ds.name, dialect=ds.dialect, version=ds.version,
                          mode=ds.mode, host=ds.host, database=ds.database,
                          description=ds.description)


@router.delete("/datasources/{name}")
async def delete_datasource(name: str):
    """从全局 Registry 删除数据源并释放连接。

    Args:
        name: 数据源名称。

    Returns:
        删除状态。
    """
    logger.debug("数据源删除路由入口", datasource=name)
    import src.api.routes as routes_package

    if not await routes_package._registry().unregister(name):
        logger.warning("数据源删除目标不存在", datasource=name)
        raise HTTPException(404, f"数据源 '{name}' 未找到")
    logger.info("数据源删除路由完成", datasource=name)
    return {"status": "ok", "message": f"数据源 '{name}' 已删除"}


@router.get("/datasources")
# 方法作用：按当前身份的数据源授权分页返回可见摘要。
# Args: page - 页码；page_size - 每页数量。
# Returns: 不包含行列权限细节的数据源分页结果。
async def list_datasources(page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100)):
    from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
    from src.app_context import get_tenant_policy
    from src.security.permission_check import resolve_datasource_access

    import src.api.routes as routes_package

    items = await routes_package._registry().list_all()
    logger.debug(
        "数据源列表授权入口",
        available_count=len(items),
        tenant_id=get_current_tenant_id(),
        user_id=get_current_user_id(),
    )
    policy = get_tenant_policy()
    if policy.datasource_isolation_enabled:
        try:
            authorized = await resolve_datasource_access(
                items,
                [],
                tenant_id=get_current_tenant_id(),
                user_id=get_current_user_id(),
                role=get_current_role(),
                tenant_policy=policy,
            )
            authorized_names = set(authorized)
            items = [item for item in items if str(item.get("name", "")) in authorized_names]
        except PermissionError as exc:
            if str(exc) == "没有可访问的数据源":
                logger.info(
                    "数据源列表授权为空",
                    tenant_id=get_current_tenant_id(),
                    user_id=get_current_user_id(),
                )
                items = []
            else:
                logger.error("数据源列表授权失败", error=str(exc), exc_info=True)
                raise HTTPException(503, "数据源权限服务不可用") from exc
    total = len(items)
    start = (page - 1) * page_size
    result = {
        "datasources": items[start:start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    logger.info("数据源列表授权完成", total=total, returned=len(result["datasources"]))
    return result
