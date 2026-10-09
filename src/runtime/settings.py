"""数据库覆盖启动默认值；只允许有明确热更新语义的配置。"""

import asyncio
import json
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from src.app_context import get_app_context
from src.config import Settings
from src.logging_config import get_logger
from src.memory.pg_pool import pg_connection

logger = get_logger(__name__)
LIVE_FIELDS = frozenset(
    name for name in Settings.model_fields
    if name.startswith(("max_", "context_", "attribution_", "analysis_data_"))
    or name in {
        "login_max_per_hour", "login_lockout_threshold", "login_lockout_minutes",
        "jwt_access_token_expire_hours", "jwt_refresh_token_expire_days", "explain_skip_dialects",
        "datasource_host_allowlist", "mcp_remote_host_allowlist",
        "llm_provider", "llm_model", "cheap_llm_model",
        "llm_temperature", "llm_max_tokens", "llm_timeout", "analysis_llm_timeout",
        "llm_stream_chunk_timeout", "local_llm_timeout", "llm_remote_tasks",
        "llm_allow_remote_fallback", "knowledge_evolution_promotion_threshold",
        "chat_show_analysis_process",
    }
)


class RuntimeSettingsRequest(BaseModel):
    """使用版本号防止多人同时保存互相覆盖，null 移除该字段覆盖。"""

    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    values: dict[str, Any]


# 校验热配置白名单及完整 Settings，保持环境默认值不可变。
# Args: base - 启动默认值；overrides - 数据库覆盖。
# Returns: 验证后的新配置快照。
def merge_settings(base: Settings, overrides: dict[str, Any]) -> Settings:
    """禁止秘密和初始化依赖绕过启动配置边界。"""
    unknown = overrides.keys() - LIVE_FIELDS
    if unknown:
        raise ValueError(f"不支持热更新的配置: {', '.join(sorted(unknown))}")
    for name, value in overrides.items():
        TypeAdapter(Settings.model_fields[name].annotation).validate_python(value, strict=True)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
            raise ValueError(f"{name} 不能为负数")
        if name.endswith(("timeout", "timeout_seconds", "expire_hours", "expire_days")) and value <= 0:
            raise ValueError(f"{name} 必须大于零")
    settings = Settings.model_validate({**base.model_dump(), **overrides})
    if not 0 <= settings.llm_temperature <= 2:
        raise ValueError("llm_temperature 必须在 0 到 2 之间")
    if not settings.context_compression_target_ratio < settings.context_compression_trigger_ratio < settings.context_hard_limit_ratio:
        raise ValueError("上下文目标比例必须小于触发比例，触发比例必须小于硬上限")
    return settings


class RuntimeSettingsService:
    """同一应用的配置控制器，跨 Worker 通过数据库刷新。"""

    # 保存启动默认值并初始化快照版本。
    # Args: self - 实例；base - 初始 Settings。
    # Returns: 无返回值。
    def __init__(self, base: Settings) -> None:
        self.base = base.model_copy(deep=True)
        self.revision = -1
        self.values: dict[str, Any] = {}
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    # 读取数据库完整版本，在验证成功后一次替换本应用配置。
    # Args: self - 实例。
    # Returns: 无返回值。
    async def refresh(self) -> None:
        """仅在校验和插件目录刷新成功后替换应用快照。"""
        logger.debug("运行时配置刷新入口")
        async with self.lock:
            async with pg_connection(tenant_id=0, user_id=0, role="super_admin") as connection:
                row = await connection.fetchrow("SELECT revision,values_json,plugins_json FROM platform_runtime_settings WHERE id=1")
            if row is None:
                raise RuntimeError("平台运行时配置表尚未初始化")
            values = json.loads(row["values_json"]) if isinstance(row["values_json"], str) else dict(row["values_json"])
            plugins = json.loads(row["plugins_json"]) if isinstance(row["plugins_json"], str) else dict(row["plugins_json"])
            if int(row["revision"]) != self.revision:
                settings = merge_settings(self.base, values)
                from src.connectors.plugins import apply_plugin_policy, discover_plugins
                discover_plugins()
                apply_plugin_policy(plugins)
                get_app_context().settings = settings
                self.values, self.revision = values, int(row["revision"])
                logger.info("运行时配置版本已应用", revision=self.revision, field_count=len(values))

    # 提供可热改字段的当前值、默认值与类型，不返回启动秘密。
    # Args: self - 实例。
    # Returns: 前端表单目录和当前版本。
    def describe(self) -> dict[str, Any]:
        """只读配置只返回键名，防止泄露启动凭证。"""
        properties = Settings.model_json_schema()["properties"]
        current = merge_settings(self.base, self.values)
        return {"revision": self.revision, "fields": [
            {"key": name, "value": getattr(current, name), "default": getattr(self.base, name),
             "overridden": name in self.values, "schema": properties[name], "apply_mode": "live"}
            for name in sorted(LIVE_FIELDS)
        ], "read_only": sorted(set(Settings.model_fields) - LIVE_FIELDS)}

    # 保存合法覆盖并在同一事务追加审计，提交后刷新本 Worker。
    # Args: self - 实例；request - 乐观锁与字段补丁；actor - 超管 ID。
    # Returns: 已应用的配置描述。
    async def save(self, request: RuntimeSettingsRequest, actor: int) -> dict:
        """原子提交配置版本和审计，不记录配置值。"""
        logger.debug("平台热配置保存入口", actor=actor, keys=sorted(request.values))
        if request.values.keys() - LIVE_FIELDS:
            raise HTTPException(422, "请求包含只读或未知配置")
        async with pg_connection(tenant_id=0, user_id=0, role="super_admin") as connection:
            row = await connection.fetchrow("SELECT revision,values_json FROM platform_runtime_settings WHERE id=1 FOR UPDATE")
            if not row or int(row["revision"]) != request.revision:
                raise HTTPException(409, "配置已被其他管理员修改，请重新加载")
            values = json.loads(row["values_json"]) if isinstance(row["values_json"], str) else dict(row["values_json"])
            for name, value in request.values.items():
                if value is None:
                    values.pop(name, None)
                else:
                    values[name] = value
            try:
                merge_settings(self.base, values)
            except ValueError:
                raise HTTPException(422, "配置值类型、范围或关联约束不合法") from None
            await connection.execute("UPDATE platform_runtime_settings SET values_json=$1::jsonb,revision=revision+1,updated_at=NOW() WHERE id=1", json.dumps(values))
            await connection.execute("INSERT INTO platform_runtime_audit(actor_user_id,action,keys) VALUES ($1,'settings',$2::jsonb)", actor, json.dumps(sorted(request.values)))
        await self.refresh()
        logger.info("平台热配置保存完成", actor_user_id=actor, revision=self.revision, fields=sorted(request.values))
        return self.describe()

    # 定时同步跨 Worker 版本，故障保留最后有效配置并重试。
    # Args: self - 实例。
    # Returns: 关闭时结束。
    async def run(self) -> None:
        while True:
            await asyncio.sleep(2)
            try:
                await self.refresh()
            except Exception:
                logger.error("运行时配置刷新失败，保留最后有效版本", revision=self.revision, exc_info=True)

    # 关闭配置刷新后台任务。
    # Args: self - 实例。
    # Returns: 无返回值。
    async def close(self) -> None:
        logger.debug("关闭运行时配置服务")
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


# 获取已启动的配置控制器，禁止 API 临时创建不受管后台任务。
# Args: 无。
# Returns: 当前应用的控制器。
def get_runtime_settings() -> RuntimeSettingsService:
    """初始化未完成返回明确的服务不可用状态。"""
    service = get_app_context().get_resource("runtime_settings")
    if service is None:
        raise HTTPException(503, "平台运行时配置服务未启动")
    return service
