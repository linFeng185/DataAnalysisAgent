"""数据源管理路由。"""

from __future__ import annotations

import asyncio
import io
import json
import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from src.api.routes._helpers import _call_with_optional_tenant
from src.api.schemas import (
    DataSourceCreateRequest,
    DataSourceInfo,
    DataSourceUpdateRequest,
)
from src.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()
_started_at = time.time()


# 方法作用：导出当前租户应用内数据源的无凭证配置，可按名称筛选子集。
# Args: names - 可选的数据源名称，可重复；不传或为空即导出全部。
# Returns: 可下载的 JSON 配置文件；名称不可用时返回 404 且不产出文件。
@router.get("/datasources/export")
async def export_datasources(names: Annotated[list[str] | None, Query()] = None) -> StreamingResponse:
    """导出不包含密码，导入后需在应用中补充凭证。"""
    import src.api.routes as routes_package
    from src.api.auth import get_current_tenant_id, require_tenant_admin
    from src.datasource.transfer import export_config

    require_tenant_admin()
    tenant_id = get_current_tenant_id()
    # 空字符串与未传参等价：FastAPI 会把 `?names=` 解析为 [""]，必须先剔除空串再判断，
    # 否则 {""} 不是可用名子集会被误判为 404。
    names = [name for name in (names or []) if name]
    logger.debug("数据源导出入口", tenant_id=tenant_id, requested=len(names))
    provider = routes_package._registry().get_provider("external")
    sources = await provider.list_all(tenant_id=tenant_id) if provider else []
    available = [source for source in sources if int(source.tenant_id) == tenant_id]
    if names:
        # 去重后按 Provider 顺序过滤：重复名称会产出重复条目，导入侧将整批拒绝。
        wanted = set(names)
        if not wanted <= {source.name for source in available}:
            logger.warning("数据源导出请求包含不可用名称", tenant_id=tenant_id, requested=len(wanted))
            raise HTTPException(404, "指定的数据源不存在")
        available = [source for source in available if source.name in wanted]
    configs = [export_config(source) for source in available]
    payload = json.dumps({"version": 1, "datasources": configs}, ensure_ascii=False, indent=2).encode("utf-8")
    logger.info("数据源导出完成", tenant_id=tenant_id, count=len(configs), filtered=bool(names))
    return StreamingResponse(io.BytesIO(payload), media_type="application/json", headers={
        "Content-Disposition": 'attachment; filename="datasources.json"', "Cache-Control": "no-store",
    })


# 方法作用：校验 JSON 文件后通过应用 Provider 原子新增数据源。
# Args: file - 最大 1 MiB、最多一百条配置的上传文件。
# Returns: 导入的数据源名称与改名映射；改名后仍冲突才整批拒绝。
@router.post("/datasources/import", status_code=201)
async def import_datasources(file: UploadFile) -> dict:
    """仅新增当前租户配置，不从文件采纳归属身份或覆盖现有数据源。"""
    import src.api.routes as routes_package
    from src.api.auth import get_current_tenant_id, get_current_user_id, require_tenant_admin
    from src.datasource.providers.external import ExternalDataSourceProvider
    from src.datasource.transfer import MAX_IMPORT_BYTES, DataSourceBundle, DataSourceConflictError

    require_tenant_admin()
    tenant_id = get_current_tenant_id()
    logger.debug("数据源导入入口", tenant_id=tenant_id)
    try:
        content = await file.read(MAX_IMPORT_BYTES + 1)
    finally:
        await file.close()
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(413, "数据源配置文件不能超过 1 MiB")
    try:
        bundle = DataSourceBundle.model_validate_json(content)
    except ValidationError:
        # 不返回校验器的原始输入，避免非法配置携带的密码出现在响应或日志中。
        raise HTTPException(422, "配置格式无效：需要 version=1 和 1—100 条名称唯一、数据库插件可用的数据源配置") from None
    registry = routes_package._registry()
    provider = registry.get_provider("external")
    if provider is None:
        provider = ExternalDataSourceProvider()
        registry.register_provider("external", provider)
    try:
        sources = await provider.import_configs(bundle.datasources, tenant_id=tenant_id, owner_user_id=get_current_user_id())
    except DataSourceConflictError:
        # 名称冲突已自动追加随机后缀重试，走到这里只可能是重试耗尽或并发唯一约束冲突，
        # 两者都是瞬时冲突，重试即可，不应指引用户改名。
        raise HTTPException(409, "数据源名称冲突，整批未导入，请稍后重试") from None
    except Exception:
        logger.error("数据源批量导入失败", tenant_id=tenant_id, exc_info=True)
        raise HTTPException(500, "数据源配置保存失败，整批未导入") from None
    for source in sources:
        registry.invalidate(source.name, tenant_id=tenant_id)
    requested = [item.name for item in bundle.datasources]
    returned = [source.name for source in sources]
    renamed = [
        {"from": original, "to": current}
        for original, current in zip(requested, returned, strict=True)
        if original != current
    ]
    logger.info("数据源导入完成", tenant_id=tenant_id, count=len(sources), renamed_count=len(renamed))
    return {"imported": returned, "renamed": renamed}


# 单源同步状态读取上限与整批总预算：asyncio.timeout 会同时覆盖信号量等待，
# 若不设总预算，page_size=100 时最坏耗时 ceil(100/5)*5s≈100s，超过前端 30s 超时，
# 状态库降级会让整页列表请求失败，恰好绕过 unknown 想防的失败模式。
_SYNC_STATUS_PER_ITEM_TIMEOUT = 5.0
_SYNC_STATUS_TOTAL_BUDGET = 8.0


# 方法作用：附加当前页的共享同步状态，读取失败不得伪装成已完成或允许重复同步。
# Args: items - 已授权的当前页摘要；tenant_id - 当前租户。
# Returns: 包含 schema_sync 的摘要列表，整体耗时受总预算约束。
async def _with_sync_status(items: list[dict], tenant_id: int) -> list[dict]:
    from src.app_context import get_app_context

    service = get_app_context().get_resource("schema_sync")
    semaphore = asyncio.Semaphore(5)
    loop = asyncio.get_running_loop()
    # 以绝对截止时间共享预算，超预算的条目按 unknown 降级，保证接口有界。
    deadline = loop.time() + _SYNC_STATUS_TOTAL_BUDGET
    logger.debug("数据源同步状态批量读取入口", tenant_id=tenant_id, count=len(items), budget=_SYNC_STATUS_TOTAL_BUDGET)

    # 方法作用：限制并发读取共享状态，并让单源等待不超过剩余总预算。
    # Args: item - 当前数据源摘要。
    # Returns: 不修改 Registry 原摘要的状态副本；失败或超时记为 unknown。
    async def enrich(item: dict) -> dict:
        state = {"status": "idle", "phase": "", "table_count": 0, "error": ""}
        if service is not None:
            try:
                async with semaphore:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError("同步状态读取超出总预算")
                    async with asyncio.timeout(min(_SYNC_STATUS_PER_ITEM_TIMEOUT, remaining)):
                        state = await service.store.state(tenant_id, str(item["name"]))
            except Exception:
                logger.error("数据源同步状态读取失败", tenant_id=tenant_id, datasource=item["name"], exc_info=True)
                # 失败与超时都必须降级为 unknown，绝不能伪装成 idle 或已完成。
                state = {"status": "unknown", "error": "同步状态暂时不可用，请稍后重试"}
        return {**item, "schema_sync": state}

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(enrich(item) for item in items)),
            timeout=_SYNC_STATUS_TOTAL_BUDGET + 1.0,
        )
    except asyncio.TimeoutError:
        # 兜底：极端情况下（底层读取不可取消）整批降级，避免请求无限挂起。
        logger.error("数据源同步状态批量读取超出总预算", tenant_id=tenant_id, count=len(items))
        results = [
            {**item, "schema_sync": {"status": "unknown", "error": "同步状态暂时不可用，请稍后重试"}}
            for item in items
        ]
    logger.info("数据源同步状态批量读取完成", tenant_id=tenant_id, count=len(results))
    return list(results)


# 在注册、更新与连接测试入口拒绝停用或未安装类型。
# Args: dialect - 请求中的连接器标识。
# Returns: 无返回值，不可用时返回 422。
def _require_available_dialect(dialect: str) -> None:
    from src.connectors.plugins import require_plugin
    try:
        require_plugin(dialect)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


# ---- 数据源管理 (2.3.7-9) ----

# 方法作用：把内部数据源配置转换为不包含密码的 API 摘要。
# Args: datasource - 内部数据源配置。
# Returns: 可安全返回前端的数据源摘要。
def _datasource_info(datasource: object) -> DataSourceInfo:
    extra_params = getattr(datasource, "extra_params", {}) or {}
    if not isinstance(extra_params, dict):
        extra_params = {}
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
        schema=str(extra_params.get("schema", "") or ""),
        tablespace=str(extra_params.get("tablespace", "") or ""),
        service_name=str(extra_params.get("service_name", "") or ""),
        instance=str(extra_params.get("instance", "") or ""),
        file_path=(str(getattr(datasource, "database", "") or "")
                   if str(getattr(datasource, "dialect", "")).lower() == "sqlite" else ""),
        connected=False,
        updated_at=datasource.updated_at.isoformat() if getattr(datasource, "updated_at", None) else "",
    )

@router.post("/datasources", status_code=201)
async def register_datasource(req: DataSourceCreateRequest):
    """把外部数据源注册到全局 Provider/Registry。

    Args:
        req: 数据源注册请求体。

    Returns:
        已注册数据源摘要。
    """
    import src.api.routes as routes_package
    from src.api.auth import (
        get_current_tenant_id,
        get_current_user_id,
        require_tenant_admin,
    )
    from src.datasource.providers.external import ExternalDataSourceProvider

    require_tenant_admin()
    _require_available_dialect(req.dialect)
    registry = routes_package._registry()
    provider = registry.get_provider("external")
    if provider is None:
        provider = ExternalDataSourceProvider()
        registry.register_provider("external", provider)
    tenant_id = get_current_tenant_id()
    owner_user_id = get_current_user_id()
    ds = await provider.register(req, tenant_id=tenant_id, owner_user_id=owner_user_id)
    try:
        await provider.persist(
            ds,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
    except Exception as exc:
        await provider.unregister(ds.name, tenant_id=tenant_id)
        logger.error("数据源注册持久化失败", datasource=ds.name, exc_info=True)
        raise HTTPException(500, "数据源配置保存失败") from exc
    registry.invalidate(ds.name, tenant_id=tenant_id)
    logger.info("数据源注册路由完成", datasource=ds.name)
    return _datasource_info(ds)


@router.post("/datasources/test")
# 方法作用：使用请求中的临时凭证探测数据源，不注册也不持久化。
# Args: req - 待测试的数据源配置。
# Returns: 连通性结果和面向用户的简短消息。
async def test_datasource_connection(req: DataSourceCreateRequest):
    import src.api.routes as routes_package
    from src.api.auth import require_tenant_admin
    from src.datasource.providers.external import ExternalDataSourceProvider

    require_tenant_admin()
    _require_available_dialect(req.dialect)
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
    import src.api.routes as routes_package
    from src.api.auth import (
        get_current_tenant_id,
        get_current_user_id,
        is_platform_super_admin,
        require_tenant_admin,
    )

    require_tenant_admin()
    _require_available_dialect(req.dialect)
    registry = routes_package._registry()
    provider = registry.get_provider("external")
    if provider is None:
        raise HTTPException(404, f"数据源 '{name}' 未找到")
    tenant_id = get_current_tenant_id()
    current = await provider.lookup(name, tenant_id=tenant_id)
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
    registry.invalidate(name, tenant_id=tenant_id)
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
    import src.api.routes as routes_package
    from src.api.auth import (
        get_current_tenant_id,
        require_tenant_admin,
    )

    require_tenant_admin()
    provider = routes_package._registry().get_provider("external")
    removed_persisted = True
    tenant_id = get_current_tenant_id()
    if provider is not None and hasattr(provider, "delete_persisted"):
        removed_persisted = await provider.delete_persisted(
            name,
            tenant_id=tenant_id,
            platform_admin=False,
        )
    if not removed_persisted:
        raise HTTPException(404, f"数据源 '{name}' 未找到")
    if not await routes_package._registry().unregister(name, tenant_id=tenant_id):
        logger.warning("数据源删除目标不存在", datasource=name)
        raise HTTPException(404, f"数据源 '{name}' 未找到")
    logger.info("数据源删除路由完成", datasource=name, tenant_id=tenant_id)
    return {"status": "ok", "message": f"数据源 '{name}' 已删除"}


@router.get("/datasources")
# 方法作用：按当前身份的数据源授权分页返回可见摘要。
# Args: page - 页码；page_size - 每页数量。
# Returns: 不包含行列权限细节的数据源分页结果。
async def list_datasources(page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100)):
    import src.api.routes as routes_package
    from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
    from src.app_context import get_tenant_policy
    from src.security.permission_check import resolve_datasource_access

    registry = routes_package._registry()
    tenant_id = get_current_tenant_id()
    items = await _call_with_optional_tenant(
        registry.list_all,
        tenant_id=tenant_id,
    )
    logger.debug(
        "数据源列表授权入口",
        available_count=len(items),
        tenant_id=tenant_id,
        user_id=get_current_user_id(),
    )
    policy = get_tenant_policy()
    if policy.datasource_isolation_enabled:
        try:
            authorized = await resolve_datasource_access(
                items,
                [],
                tenant_id=tenant_id,
                user_id=get_current_user_id(),
                role=get_current_role(),
                tenant_policy=policy,
            )
            authorized_names = set(authorized)
            items = [item for item in items if str(item.get("name", "")) in authorized_names]
        except PermissionError as exc:
            if str(exc) == "没有可访问的数据源":
                logger.debug(
                    "数据源列表授权为空",
                    tenant_id=tenant_id,
                    user_id=get_current_user_id(),
                )
                items = []
            else:
                logger.error("数据源列表授权失败", error=str(exc), exc_info=True)
                raise HTTPException(503, "数据源权限服务不可用") from exc
    total = len(items)
    start = (page - 1) * page_size
    result = {
        "datasources": await _with_sync_status(items[start:start + page_size], tenant_id),
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    logger.debug("数据源列表授权完成", total=total, returned=len(result["datasources"]))
    return result
