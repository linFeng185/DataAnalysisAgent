"""应用启动与关闭编排。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from src.config import Settings
from src.logging_config import get_logger


logger = get_logger(__name__)


# 启动阶段名称和说明集中声明，便于测试顺序和未来增加阶段。
_BOOTSTRAP_STEPS: tuple[tuple[str, str], ...] = (
    ("_run_migrations", "数据库迁移"),
    ("_init_workflow", "LangGraph 工作流"),
    ("_ensure_demo_datasource", "演示数据源"),
    ("_warmup_knowledge", "知识库预热"),
    ("_warmup_llm", "LLM 客户端预热"),
    ("_warmup_stores", "会话存储预热"),
    ("_load_skills", "Skills 加载"),
    ("_connect_mcp_servers", "MCP 连接"),
    ("_load_external_datasources", "外部数据源加载"),
)


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

    await get_file_store()._ensure()  # noqa: SLF001
    if settings.vector_store_type == "chroma":
        from src.knowledge.schema_manager import get_schema_manager

        get_schema_manager()._ensure_initialized()  # noqa: SLF001
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
    del settings
    logger.debug("_load_external_datasources 入口")
    from src.datasource.providers.external import ExternalDataSourceProvider
    from src.datasource.registry import get_registry

    provider = ExternalDataSourceProvider.from_yaml("config/datasources.yaml")
    get_registry().register_provider("external", provider)
    sources = await provider.list_all()
    logger.info("_load_external_datasources 完成", count=len(sources))


# 方法作用：按固定顺序执行所有启动阶段，开发环境允许单阶段降级。
# Args: settings - 当前应用配置。
# Returns: 无返回值。
async def bootstrap_all(settings: Settings) -> None:
    logger.debug("bootstrap_all 入口", env=settings.env)
    for name, description in _BOOTSTRAP_STEPS:
        step: Callable[[Settings], Awaitable[Any]] = globals()[name]
        logger.debug("启动阶段开始", step=name, description=description)
        try:
            await step(settings)
        except Exception:
            logger.error("启动阶段失败", step=name, description=description, exc_info=True)
            if settings.env == "prod":
                raise
            logger.warning("非生产环境跳过启动阶段", step=name, env=settings.env)
    logger.info("bootstrap_all 完成", env=settings.env)


# 方法作用：关闭 MCP 和 PostgreSQL 共享资源，避免进程退出时连接泄漏。
# Args: 无。
# Returns: 无返回值。
async def shutdown_all() -> None:
    logger.debug("shutdown_all 入口")
    await _close_mcp()
    await _close_pg()
    logger.info("shutdown_all 完成")


# 方法作用：关闭已创建的全局 MCP 管理器。
# Args: 无。
# Returns: 无返回值。
async def _close_mcp() -> None:
    logger.debug("_close_mcp 入口")
    from src.mcp_client import client_manager

    if client_manager._client_manager is not None:  # noqa: SLF001
        await client_manager._client_manager.close_all()  # noqa: SLF001
    logger.info("_close_mcp 完成")


# 方法作用：关闭全局 PostgreSQL 连接池。
# Args: 无。
# Returns: 无返回值。
async def _close_pg() -> None:
    logger.debug("_close_pg 入口")
    from src.memory.pg_pool import close_pg_pool

    await close_pg_pool()
    logger.info("_close_pg 完成")
