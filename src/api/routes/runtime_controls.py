"""平台热配置与数据库连接器插件管理 API。"""

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.api.auth import get_current_user_id, require_super_admin
from src.connectors.plugins import discover_plugins, plugin_catalog
from src.logging_config import get_logger
from src.memory.pg_pool import pg_connection
from src.runtime.settings import RuntimeSettingsRequest, get_runtime_settings

router = APIRouter()
logger = get_logger(__name__)


# 读取非敏感热配置目录。
# Args: 无。
# Returns: 字段描述和版本。
@router.get("/admin/runtime-settings")
async def runtime_settings() -> dict:
    """环境变量只作为默认值，数据库覆盖优先。"""
    require_super_admin()
    service = get_runtime_settings()
    await service.refresh()
    return service.describe()


# 保存有版本约束的热配置变更。
# Args: request - 版本和补丁。
# Returns: 应用后的配置。
@router.put("/admin/runtime-settings")
async def save_runtime_settings(request: RuntimeSettingsRequest) -> dict:
    """null 表示恢复环境默认值。"""
    require_super_admin()
    return await get_runtime_settings().save(request, get_current_user_id())


# 提供当前可用的数据库类型给数据源与指标表单。
# Args: 无。
# Returns: 已启用插件列表。
@router.get("/datasource-types")
async def datasource_types() -> dict:
    """不返回任何数据库连接信息。"""
    return {"items": [item for item in plugin_catalog() if item["enabled"]]}


# 平台查看完整插件目录，包括停用项。
# Args: 无。
# Returns: 插件元数据。
@router.get("/admin/connector-plugins")
async def connector_plugins() -> dict:
    """插件代码须由可信部署过程安装。"""
    require_super_admin()
    return {"items": plugin_catalog()}


# 重新发现当前环境中新装或已移除的插件发行包。
# Args: 无。
# Returns: 刷新后的目录。
@router.post("/admin/connector-plugins/discover")
async def refresh_plugins() -> dict:
    """不在请求内执行 pip 或不受信任文件。"""
    require_super_admin()
    items = discover_plugins()
    async with pg_connection(tenant_id=0, user_id=0, role="super_admin") as connection:
        await connection.execute("UPDATE platform_runtime_settings SET revision=revision+1 WHERE id=1")
        await connection.execute("INSERT INTO platform_runtime_audit(actor_user_id,action,keys) VALUES ($1,'plugin.discover','[]'::jsonb)", get_current_user_id())
    await get_runtime_settings().refresh()
    logger.info("平台重新发现数据库插件", actor_user_id=get_current_user_id(), count=len(items))
    return {"items": items}


class PluginStateRequest(BaseModel):
    """平台插件启停请求。"""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    enabled: bool
    reason: str = Field(min_length=3, max_length=500)


# 持久化插件状态并记录平台审计。
# Args: dialect - 注册类型；request - 状态与原因。
# Returns: 已保存状态。
@router.put("/admin/connector-plugins/{dialect}")
async def set_plugin_state(dialect: str, request: PluginStateRequest) -> dict:
    """在途查询完成，新查询遵守下一有效配置版本。"""
    require_super_admin()
    if dialect not in {item["dialect"] for item in plugin_catalog()}:
        raise HTTPException(404, "数据库插件不存在")
    async with pg_connection(tenant_id=0, user_id=0, role="super_admin") as connection:
        await connection.execute("UPDATE platform_runtime_settings SET plugins_json=jsonb_set(plugins_json,ARRAY[$1],$2::jsonb),revision=revision+1 WHERE id=1", dialect, json.dumps(request.enabled))
        await connection.execute("INSERT INTO platform_runtime_audit(actor_user_id,action,keys) VALUES ($1,$2,$3::jsonb)", get_current_user_id(), "plugin.enable" if request.enabled else "plugin.disable", json.dumps([dialect, request.reason]))
    await get_runtime_settings().refresh()
    logger.info("平台数据库插件状态已保存", dialect=dialect, enabled=request.enabled, actor_user_id=get_current_user_id())
    return {"dialect": dialect, "enabled": request.enabled}
