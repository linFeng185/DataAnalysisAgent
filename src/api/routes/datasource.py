"""数据源管理路由。"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query

from src.api.schemas import (
    DataSourceCreateRequest, DataSourceInfo, DataSourceUpdateRequest,
)
from src.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()
_started_at = time.time()


# ---- 数据源管理 (2.3.7-9) ----

# 方法作用：把内部数据源配置转换为不包含密码的 API 摘要。
# Args: datasource - 内部数据源配置。
# Returns: 可安全返回前端的数据源摘要。
def _datasource_info(datasource: object) -> DataSourceInfo:
    return DataSourceInfo(
        name=str(getattr(datasource, "name", "")),
        dialect=str(getattr(datasource, "dialect", "")),
        version=str(getattr(datasource, "version", "") or ""),
        mode=str(getattr(datasource, "mode", "external")),
        host=str(getattr(datasource, "host", "") or ""),
        port=int(getattr(datasource, "port", 0) or 0),
        database=str(getattr(datasource, "database", "") or ""),
        username=str(getattr(datasource, "username", "") or ""),
        description=str(getattr(datasource, "description", "") or ""),
        connected=False,
    )

@router.post("/datasources", status_code=201)
async def register_datasource(req: DataSourceCreateRequest):
    """把外部数据源注册到全局 Provider/Registry。

    Args:
        req: 数据源注册请求体。

    Returns:
        已注册数据源摘要。
    """
    from src.datasource.providers.external import ExternalDataSourceProvider
    from src.api.auth import (
        get_current_tenant_id, get_current_user_id, require_tenant_admin,
    )
    import src.api.routes as routes_package

    require_tenant_admin()
    registry = routes_package._registry()
    provider = registry.get_provider("external")
    if provider is None:
        provider = ExternalDataSourceProvider()
        registry.register_provider("external", provider)
    ds = await provider.register(req)
    try:
        await provider.persist(
            ds,
            tenant_id=get_current_tenant_id(),
            owner_user_id=get_current_user_id(),
        )
    except Exception as exc:
        await provider.unregister(ds.name)
        logger.error("数据源注册持久化失败", datasource=ds.name, exc_info=True)
        raise HTTPException(500, "数据源配置保存失败") from exc
    registry.invalidate(ds.name)
    logger.info("数据源注册路由完成", datasource=ds.name)
    return _datasource_info(ds)


@router.post("/datasources/test")
# 方法作用：使用请求中的临时凭证探测数据源，不注册也不持久化。
# Args: req - 待测试的数据源配置。
# Returns: 连通性结果和面向用户的简短消息。
async def test_datasource_connection(req: DataSourceCreateRequest):
    from src.api.auth import require_tenant_admin
    from src.datasource.providers.external import ExternalDataSourceProvider
    import src.api.routes as routes_package

    require_tenant_admin()
    registry = routes_package._registry()
    provider = registry.get_provider("external")
    if provider is None:
        provider = ExternalDataSourceProvider()
        registry.register_provider("external", provider)
    connected = await provider.probe_request(req, registry._create_engine)
    message = "连接成功" if connected else "连接失败，请检查地址、凭证和网络"
    logger.info(
        "数据源临时连接测试路由完成",
        datasource=req.name,
        dialect=req.dialect,
        connected=connected,
    )
    return {"success": connected, "message": message}


@router.put("/datasources/{name}")
# 方法作用：更新当前租户已有数据源，连接测试和持久化成功后才替换配置。
# Args: name - 数据源名称；req - 新的数据源配置。
# Returns: 更新后的安全摘要。
async def update_datasource(name: str, req: DataSourceUpdateRequest):
    from src.api.auth import (
        get_current_tenant_id,
        get_current_user_id,
        is_platform_super_admin,
        require_tenant_admin,
    )
    import src.api.routes as routes_package

    require_tenant_admin()
    registry = routes_package._registry()
    provider = registry.get_provider("external")
    if provider is None:
        raise HTTPException(404, f"数据源 '{name}' 未找到")
    current = await provider.lookup(name)
    tenant_id = get_current_tenant_id()
    if current is None or (
        int(getattr(current, "tenant_id", 0)) != tenant_id
        and not is_platform_super_admin()
    ):
        raise HTTPException(404, f"数据源 '{name}' 未找到")
    try:
        updated = await provider.update(
            name,
            req,
            engine_factory=registry._create_engine,
            tenant_id=tenant_id,
            owner_user_id=get_current_user_id(),
        )
    except ConnectionError as exc:
        logger.warning("数据源更新连接测试失败", datasource=name, error=str(exc)[:500])
        raise HTTPException(400, "连接测试失败，原配置未变更") from exc
    except Exception as exc:
        logger.error("数据源更新失败", datasource=name, exc_info=True)
        raise HTTPException(500, "数据源配置更新失败") from exc
    registry.invalidate(name)
    logger.info("数据源更新路由完成", datasource=name, tenant_id=tenant_id)
    return _datasource_info(updated)


@router.delete("/datasources/{name}")
async def delete_datasource(name: str):
    """从全局 Registry 删除数据源并释放连接。

    Args:
        name: 数据源名称。

    Returns:
        删除状态。
    """
    logger.debug("数据源删除路由入口", datasource=name)
    from src.api.auth import (
        get_current_tenant_id, is_platform_super_admin, require_tenant_admin,
    )
    import src.api.routes as routes_package

    require_tenant_admin()
    provider = routes_package._registry().get_provider("external")
    if provider is not None and hasattr(provider, "delete_persisted"):
        await provider.delete_persisted(
            name,
            tenant_id=get_current_tenant_id(),
            platform_admin=is_platform_super_admin(),
        )
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
