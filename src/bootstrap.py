"""应用启动与关闭编排。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

from src.app_context import AppContext, use_app_context_async
from src.config import Settings
from src.logging_config import get_logger


logger = get_logger(__name__)


# 启动阶段名称和说明集中声明，便于测试顺序和未来增加阶段。
_BOOTSTRAP_STEPS: tuple[tuple[str, str], ...] = (
    ("_run_migrations", "数据库迁移"),
    ("_ensure_super_admin", "平台超级管理员"),
    ("_load_api_access_policies", "API 访问策略"),
    ("_configure_observability", "可观测性"),
    ("_init_workflow", "LangGraph 工作流"),
    ("_ensure_demo_datasource", "演示数据源"),
    ("_warmup_knowledge", "知识库预热"),
    ("_warmup_llm", "LLM 客户端预热"),
    ("_warmup_stores", "会话存储预热"),
    ("_start_memory_maintenance", "记忆维护服务"),
    ("_load_skills", "Skills 加载"),
    ("_connect_mcp_servers", "MCP 连接"),
    ("_load_external_datasources", "外部数据源加载"),
    ("_start_schema_cache_refresh", "Schema 缓存刷新服务"),
    ("_start_automation", "主动洞察与定时报告服务"),
)

_REQUIRED_BOOTSTRAP_STEPS = {
    "_run_migrations", "_ensure_super_admin", "_load_api_access_policies",
}


# 方法作用：执行数据库版本化迁移并返回已应用迁移数量。
# Args: settings - 当前应用配置。
# Returns: 无返回值。
async def _run_migrations(settings: Settings) -> None:
    logger.debug("_run_migrations 入口", enabled=settings.run_migrations_on_startup)
    if not settings.run_migrations_on_startup:
        logger.info("_run_migrations 完成", skipped=True)
        return
    from src.db.migrations import run_migrations

    applied = await run_migrations(settings.database_url)
    logger.info("_run_migrations 完成", applied_count=len(applied))


# 方法作用：幂等创建或校验固定 id=1 的平台超级管理员。
# Args: settings - 当前应用配置，首次初始化时提供账号和密码。
# Returns: 无返回值，冲突或缺少首次密码时抛出 RuntimeError。
async def _ensure_super_admin(settings: Settings) -> None:
    logger.debug("_ensure_super_admin 入口", username=settings.super_admin_username)
    from src.api.auth import SUPER_ADMIN_USER_ID, _hash_password
    from src.memory.pg_pool import get_pg_pool

    username = settings.super_admin_username.strip()
    if not username:
        logger.error("固定超级管理员初始化失败", reason="用户名为空")
        raise RuntimeError("SUPER_ADMIN_USERNAME 必须配置")
    pool = await get_pg_pool()
    async with pool.acquire() as connection:
        transaction_factory = getattr(connection, "transaction", None)

        # 方法作用：在事务中校验或创建固定平台账号。
        # Args: 无，使用外层配置和数据库连接。
        # Returns: 是否新建账号。
        async def _upsert() -> bool:
            logger.debug("固定超级管理员写入入口", user_id=SUPER_ADMIN_USER_ID)
            row = await connection.fetchrow(
                "SELECT id, username, role, is_active FROM users WHERE id=$1 FOR UPDATE",
                SUPER_ADMIN_USER_ID,
            )
            if row is not None:
                if str(row["role"]) != "super_admin" or not bool(row["is_active"]):
                    logger.error("固定超级管理员校验失败", user_id=SUPER_ADMIN_USER_ID)
                    raise RuntimeError("users.id=1 必须是启用的 super_admin")
                logger.info("固定超级管理员写入完成", created=False, username=row["username"])
                return False
            if not settings.super_admin_password:
                logger.error("固定超级管理员初始化失败", reason="首次密码为空")
                raise RuntimeError("首次启动必须配置 SUPER_ADMIN_PASSWORD")
            conflict = await connection.fetchval(
                "SELECT id FROM users WHERE LOWER(username)=LOWER($1)",
                username,
            )
            if conflict is not None:
                logger.error("固定超级管理员初始化失败", reason="用户名已占用")
                raise RuntimeError("SUPER_ADMIN_USERNAME 已被其他账号占用")
            password_hash = await asyncio.to_thread(_hash_password, settings.super_admin_password)
            await connection.execute(
                "INSERT INTO users (id, username, password_hash, role, tenant_id, is_active) "
                "VALUES ($1, $2, $3, 'super_admin', 1, TRUE)",
                SUPER_ADMIN_USER_ID,
                username,
                password_hash,
            )
            await connection.execute(
                "SELECT setval(pg_get_serial_sequence('users', 'id'), "
                "GREATEST((SELECT MAX(id) FROM users), 1), TRUE)",
            )
            logger.info("固定超级管理员写入完成", created=True, username=username)
            return True

        if callable(transaction_factory):
            async with transaction_factory():
                created = await _upsert()
        else:
            logger.warning("超级管理员连接不支持事务，使用兼容路径")
            created = await _upsert()
    logger.info("_ensure_super_admin 完成", created=created, user_id=SUPER_ADMIN_USER_ID)


# 方法作用：加载 YAML 基线和 PostgreSQL 动态 API 访问策略快照。
# Args: settings - 当前应用配置，由 AppContext 中的同一实例持有。
# Returns: 无返回值。
async def _load_api_access_policies(settings: Settings) -> None:
    logger.debug("_load_api_access_policies 入口", env=settings.env)
    from src.security.api_access_policy import initialize_api_access_policy_manager

    manager = await initialize_api_access_policy_manager()
    snapshot = manager.export_snapshot()
    logger.info(
        "_load_api_access_policies 完成",
        policy_count=len(snapshot["policies"]),
        rule_count=len(snapshot["ip_rules"]),
    )


# 方法作用：在工作流初始化前配置 LangSmith 和 Prometheus 共享注册器。
# Args: settings - 当前应用配置。
# Returns: 无返回值。
async def _configure_observability(settings: Settings) -> None:
    logger.debug("_configure_observability 入口")
    from src.observability import configure_langsmith, get_metrics_registry

    tracing_enabled = configure_langsmith(settings)
    get_metrics_registry()
    logger.info("_configure_observability 完成", langsmith=tracing_enabled)


# 方法作用：初始化 LangGraph 工作流及其 Checkpointer。
# Args: settings - 当前应用配置。
# Returns: 无返回值。
async def _init_workflow(settings: Settings) -> None:
    del settings
    logger.debug("_init_workflow 入口")
    from src.graph.workflow import init_app

    await init_app()
    logger.info("_init_workflow 完成")


# 方法作用：初始化演示 SQLite 数据源，保证开发环境有可用样例。
# Args: settings - 当前应用配置。
# Returns: 无返回值。
async def _ensure_demo_datasource(settings: Settings) -> None:
    del settings
    logger.debug("_ensure_demo_datasource 入口")
    from src.datasource.setup import ensure_demo_datasource

    await ensure_demo_datasource()
    logger.info("_ensure_demo_datasource 完成")


# 方法作用：预热知识文件存储、向量库和系统知识目录。
# Args: settings - 当前应用配置。
# Returns: 无返回值。
async def _warmup_knowledge(settings: Settings) -> None:
    logger.debug("_warmup_knowledge 入口", vector_store_type=settings.vector_store_type)
    from src.knowledge.file_store import get_file_store
    from src.memory.vector_store import get_vector_store

    await get_file_store().initialize()
    await get_vector_store()
    if settings.system_knowledge_dirs.strip():
        from src.knowledge.system_scanner import scan_configured_system_knowledge

        result = await scan_configured_system_knowledge()
        logger.info(
            "系统知识目录预热完成",
            ingested=result.ingested_files,
            skipped=result.skipped_files,
            errors=result.error_files,
            chunks=result.written_chunks,
        )
    logger.info("_warmup_knowledge 完成")


# 方法作用：仅在配置为本地模型时预热轻量 LLM 客户端。
# Args: settings - 当前应用配置。
# Returns: 无返回值。
async def _warmup_llm(settings: Settings) -> None:
    logger.debug("_warmup_llm 入口", provider=settings.llm_provider)
    from src.llm.client import get_task_llm, resolve_llm_task_target

    target = resolve_llm_task_target("classify_intent", settings=settings)
    if target == "local":
        get_task_llm("classify_intent", temperature=0, reasoning=False)
        logger.info("_warmup_llm 完成", target=target, model=settings.local_llm_model)
        return
    logger.info("_warmup_llm 完成", target=target, skipped=True)


# 方法作用：提前初始化会话与历史存储，避免首个请求承担建表延迟。
# Args: settings - 当前应用配置。
# Returns: 无返回值。
async def _warmup_stores(settings: Settings) -> None:
    del settings
    logger.debug("_warmup_stores 入口")
    from src.memory.session_store import get_session_store
    from src.memory.history_store import get_history_store

    await get_session_store().list(limit=1)
    await get_history_store().list(page=1, page_size=1)
    logger.info("_warmup_stores 完成")


# 方法作用：关闭 AppContext 持有的周期记忆维护服务。
# Args: service - 待停止的维护服务。
# Returns: 无返回值。
async def _close_memory_maintenance(service: Any) -> None:
    await service.close()


# 方法作用：创建并注册绑定应用生命周期的周期记忆维护服务。
# Args: settings - 当前应用配置。
# Returns: 无返回值。
async def _start_memory_maintenance(settings: Settings) -> None:
    logger.debug(
        "_start_memory_maintenance 入口",
        enabled=settings.memory_maintenance_enabled,
        interval_seconds=settings.memory_maintenance_interval_seconds,
    )
    if not settings.memory_maintenance_enabled:
        logger.info("_start_memory_maintenance 完成", skipped=True)
        return
    from src.app_context import get_app_context
    from src.memory.long_term_store import get_long_term_memory_store
    from src.memory.session_archive import MemoryMaintenanceService, SessionMaintenance

    store = await get_long_term_memory_store()
    maintenance = SessionMaintenance(pg_pool=store.pg_pool, memory_store=store)
    service = MemoryMaintenanceService(
        maintenance,
        interval_seconds=settings.memory_maintenance_interval_seconds,
    )
    await service.start()
    get_app_context().set_resource(
        "memory_maintenance_service",
        service,
        closer=_close_memory_maintenance,
    )
    logger.info(
        "_start_memory_maintenance 完成",
        skipped=False,
        has_pg=store.pg_pool is not None,
    )


# 方法作用：发现并加载配置目录下的 Skills。
# Args: settings - 当前应用配置。
# Returns: 无返回值。
async def _load_skills(settings: Settings) -> None:
    logger.debug("_load_skills 入口", skills_dir=settings.skills_dir)
    from src.skill_manager import get_skill_manager

    await get_skill_manager(
        settings.skills_dir,
        settings.extra_skills_dirs,
        settings.managed_skills_dir,
    ).discover()
    logger.info("_load_skills 完成")


# 方法作用：初始化 MCP 配置表并连接受管外部 MCP Server。
# Args: settings - 当前应用配置。
# Returns: 无返回值。
async def _connect_mcp_servers(settings: Settings) -> None:
    del settings
    logger.debug("_connect_mcp_servers 入口")
    from src.mcp_client.client_manager import get_mcp_client_manager

    manager = get_mcp_client_manager()
    await manager.ensure_schema()
    await manager.connect_all()
    logger.info("_connect_mcp_servers 完成")


# 方法作用：读取 YAML 中声明的外部数据源并注册到统一 Registry。
# Args: settings - 当前应用配置。
# Returns: 无返回值。
async def _load_external_datasources(settings: Settings) -> None:
    logger.info(
        "_load_external_datasources 配置边界",
        config_path="config/datasources.yaml",
        environment=settings.env,
        settings_key_configured=bool(settings.credential_encryption_key),
        environment_key_configured=bool(os.getenv("CREDENTIAL_ENCRYPTION_KEY", "")),
    )
    from src.datasource.providers.external import ExternalDataSourceProvider
    from src.datasource.registry import get_registry

    provider = ExternalDataSourceProvider.from_yaml("config/datasources.yaml")
    persisted_count = await provider.load_persisted()
    get_registry().register_provider("external", provider)
    sources = await provider.list_all()
    logger.info(
        "_load_external_datasources 完成",
        count=len(sources),
        persisted_count=persisted_count,
        yaml_optional=True,
        env=settings.env,
    )


# 方法作用：关闭 AppContext 持有的 Schema 缓存周期刷新服务。
# Args: service - 待关闭的周期刷新服务。
# Returns: 无返回值。
async def _close_schema_cache_refresh(service: Any) -> None:
    await service.close()


# 方法作用：在数据源恢复完成后启动 Schema TTL 清理和 DDL 指纹轮询服务。
# Args: settings - 当前应用配置。
# Returns: 无返回值。
async def _start_schema_cache_refresh(settings: Settings) -> None:
    logger.debug(
        "_start_schema_cache_refresh 入口",
        enabled=settings.schema_cache_refresh_enabled,
        interval_seconds=settings.schema_cache_refresh_interval_seconds,
        cache_backend=settings.datasource_cache_backend,
    )
    if not settings.schema_cache_refresh_enabled:
        logger.info("_start_schema_cache_refresh 完成", skipped=True)
        return
    redis_client = None
    if settings.datasource_cache_backend == "redis":
        from redis.asyncio import Redis

        redis_client = Redis.from_url(settings.redis_url)
    from src.app_context import get_app_context
    from src.datasource.registry import get_registry
    from src.knowledge.cache_refresher import CacheRefresher, CacheRefreshService
    from src.knowledge.schema_manager import SchemaManager

    manager = SchemaManager()
    refresher = CacheRefresher(
        schema_manager=manager,
        redis_client=redis_client,
    )
    service = CacheRefreshService(
        refresher,
        get_registry(),
        settings.schema_cache_refresh_interval_seconds,
    )
    await service.start()
    get_app_context().set_resource(
        "schema_cache_refresh_service",
        service,
        closer=_close_schema_cache_refresh,
    )
    logger.info(
        "_start_schema_cache_refresh 完成",
        skipped=False,
        distributed_lock=redis_client is not None,
    )


# 方法作用：关闭 AppContext 持有的自动化调度服务。
# Args: service - 待关闭的 AutomationService。
# Returns: 无返回值。
async def _close_automation(service: Any) -> None:
    await service.close()


# 方法作用：按配置创建并启动主动洞察与定时报告轮询服务。
# Args: settings - 当前应用配置。
# Returns: 无返回值。
async def _start_automation(settings: Settings) -> None:
    logger.debug(
        "_start_automation 入口",
        enabled=settings.automation_enabled,
        interval_seconds=settings.automation_poll_interval_seconds,
    )
    if not settings.automation_enabled:
        logger.info("_start_automation 完成", skipped=True)
        return
    from src.app_context import get_app_context
    from src.automation.runner import ScheduledAnalysisRunner
    from src.automation.service import AutomationService
    from src.automation.store import get_automation_store

    store = get_automation_store()
    runner = ScheduledAnalysisRunner(store)
    service = AutomationService(
        store,
        runner,
        interval_seconds=settings.automation_poll_interval_seconds,
    )
    context = get_app_context()
    context.set_resource("automation_runner", runner)
    context.set_resource(
        "automation_service",
        service,
        closer=_close_automation,
    )
    await service.start()
    logger.info(
        "_start_automation 完成",
        skipped=False,
        interval_seconds=settings.automation_poll_interval_seconds,
    )


# 方法作用：按固定顺序执行所有启动阶段，并绑定显式应用 Context。
# Args: settings - 当前应用配置；context - 可选应用级依赖容器。
# Returns: 无返回值。
async def bootstrap_all(
    settings: Settings,
    *,
    context: AppContext | None = None,
) -> None:
    logger.debug("bootstrap_all 入口", env=settings.env)
    if context is not None:
        async with use_app_context_async(context):
            await _run_bootstrap_steps(settings)
        logger.info("bootstrap_all 完成", env=settings.env, context_bound=True)
        return
    await _run_bootstrap_steps(settings)
    logger.info("bootstrap_all 完成", env=settings.env, context_bound=False)


# 方法作用：执行启动阶段并应用生产阻断、非生产降级策略。
# Args: settings - 当前应用配置。
# Returns: 无返回值。
async def _run_bootstrap_steps(settings: Settings) -> None:
    logger.debug("_run_bootstrap_steps 入口", env=settings.env)
    for name, description in _BOOTSTRAP_STEPS:
        step: Callable[[Settings], Awaitable[Any]] = globals()[name]
        logger.debug("启动阶段开始", step=name, description=description)
        try:
            await step(settings)
        except Exception:
            logger.error("启动阶段失败", step=name, description=description, exc_info=True)
            if settings.env == "prod" or name in _REQUIRED_BOOTSTRAP_STEPS:
                raise
            logger.warning("非生产环境跳过启动阶段", step=name, env=settings.env)
    logger.info("_run_bootstrap_steps 完成", env=settings.env)


# 方法作用：通过 AppContext 逆序关闭当前应用已创建的全部共享资源。
# Args: context - 待关闭的应用级依赖容器；缺省时使用当前 Context。
# Returns: 无返回值。
async def shutdown_all(context: AppContext | None = None) -> None:
    logger.debug("shutdown_all 入口")
    if context is None:
        from src.app_context import get_app_context

        context = get_app_context()
    await context.close()
    logger.info("shutdown_all 完成")
