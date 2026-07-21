"""FastAPI 应用入口 — 挂载路由 + 异常处理。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.middleware import register_exception_handlers
from src.api.routes import router
from src.config import get_settings
from src.logging_config import get_logger, setup_logging
from src.memory.checkpointer import configure_asyncio_event_loop

logger = get_logger(__name__)
configure_asyncio_event_loop()


def selector_event_loop_factory(use_subprocess: bool = False) -> asyncio.AbstractEventLoop:
    """创建兼容 psycopg 异步连接的 SelectorEventLoop，供 Uvicorn 使用。

    Args:
        use_subprocess: Uvicorn 是否需要子进程支持；当前服务不改变事件循环选择。

    Returns:
        新建的 asyncio SelectorEventLoop。
    """
    logger.debug("创建 SelectorEventLoop 入口", use_subprocess=use_subprocess)
    loop = asyncio.SelectorEventLoop()
    logger.info("创建 SelectorEventLoop 完成", loop_type=type(loop).__name__)
    return loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    s = get_settings()
    logger.info("=" * 50)
    logger.info("智能体启动", env=s.env, version="0.1.0")
    logger.info("LLM 配置", provider=s.llm_provider, model=s.llm_model,
                 base_url=s.openai_base_url or "https://api.openai.com/v1",
                 api_key_set=bool(s.openai_api_key))
    logger.info("=" * 50)

    # 在任何依赖表的组件初始化前完成版本化迁移。
    if s.run_migrations_on_startup:
        try:
            from src.db.migrations import run_migrations

            applied_migrations = await run_migrations(s.database_url)
            logger.info("启动迁移完成", applied_count=len(applied_migrations))
        except Exception as exc:
            logger.error("启动迁移失败", error=str(exc), exc_info=True)
            if s.env == "prod":
                raise
            logger.warning("非生产环境降级跳过迁移", env=s.env)

    # 初始化 LangGraph 工作流（含 Checkpointer）
    from src.graph.workflow import init_app
    await init_app()

    # 初始化知识库文件存储表（PG）
    try:
        from src.knowledge.file_store import get_file_store
        await get_file_store()._ensure()  # noqa: SLF001
    except Exception:
        pass

    # 初始化演示数据源（SQLite 内存库）
    from src.datasource.setup import ensure_demo_datasource
    await ensure_demo_datasource()

    if get_settings().vector_store_type == "chroma":
        try:
            from src.knowledge.schema_manager import get_schema_manager
            sm = get_schema_manager()
            sm._ensure_initialized()  # noqa: SLF001
            logger.info("ChromaDB 预热完成")
        except Exception as e:
            logger.warning("ChromaDB 预热失败", error=str(e))

    # 按配置目录增量摄取平台系统知识。
    if s.system_knowledge_dirs.strip():
        try:
            from src.knowledge.system_scanner import scan_configured_system_knowledge

            scan_result = await scan_configured_system_knowledge()
            logger.info(
                "系统知识目录预热完成",
                ingested=scan_result.ingested_files,
                skipped=scan_result.skipped_files,
                errors=scan_result.error_files,
                chunks=scan_result.written_chunks,
            )
        except Exception as e:
            logger.error("系统知识目录预热失败", error=str(e), exc_info=True)

    # 只预热快速本地模型；远程模型按显式任务在请求阶段创建，避免拖慢应用启动。
    try:
        from src.llm.client import get_task_llm, resolve_llm_task_target
        target = resolve_llm_task_target("classify_intent", settings=s)
        if target == "local":
            get_task_llm("classify_intent", temperature=0, reasoning=False)
            logger.info("本地 LLM 客户端预热完成", model=s.local_llm_model)
        else:
            logger.info("LLM 客户端预热跳过", target=target, reason="仅预热本地轻量模型")
    except Exception as e:
        logger.warning("LLM 预热失败", error=str(e))

    # 预热 SessionStore 和 HistoryStore（提前建 PG 表，避免首次请求时卡顿）
    try:
        from src.memory.session_store import get_session_store
        await get_session_store().list(limit=1)
    except Exception as e:
        logger.warning("SessionStore 预热失败", error=str(e))
    try:
        from src.memory.history_store import get_history_store
        await get_history_store().list(page=1, page_size=1)
    except Exception as e:
        logger.warning("HistoryStore 预热失败", error=str(e))

    # 9.1.3 加载 Skills
    try:
        from src.skill_manager import get_skill_manager
        await get_skill_manager(
            s.skills_dir, s.extra_skills_dirs, s.managed_skills_dir,
        ).discover()
    except Exception as e:
        logger.warning("Skill 加载失败", error=str(e))

    # 8.1.2 连接外部 MCP Server
    try:
        from src.mcp_client.client_manager import get_mcp_client_manager
        mcp_manager = get_mcp_client_manager()
        await mcp_manager.ensure_schema()
        await mcp_manager.connect_all()
    except Exception as e:
        logger.warning("MCP 连接失败", error=str(e))

    # 加载外部数据源（config/datasources.yaml）
    try:
        from src.datasource.providers.external import ExternalDataSourceProvider
        from src.datasource.registry import get_registry
        provider = ExternalDataSourceProvider.from_yaml("config/datasources.yaml")
        get_registry().register_provider("external", provider)
        registered = await provider.list_all()
        logger.info("外部数据源加载完成", count=len(registered),
                     names=[r.name for r in registered])
    except Exception as e:
        logger.warning("外部数据源加载失败", error=str(e))

    yield
    logger.info("智能体已关闭")


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用。

    Args:
        无。

    Returns:
        已挂载中间件、路由和生命周期的 FastAPI 应用。
    """
    settings = get_settings()
    logger.debug("create_app 入口", env=settings.env)
    from src.config import validate_production_settings
    validate_production_settings(settings)

    import json as _json
    from starlette.responses import JSONResponse as _JSONResponse

    class _PrecisionResponse(_JSONResponse):
        """FastAPI 全局 JSON 响应：float → Decimal → 精确序列化。"""
        def render(self, content) -> bytes:
            from src.api.streaming import _PrecisionEncoder, _json_serialize
            return _json.dumps(content, cls=_PrecisionEncoder, default=_json_serialize,
                               ensure_ascii=False).encode("utf-8")

    app = FastAPI(
        title="Data Analysis Agent",
        description="LLM 驱动的数据分析智能体",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.env == "dev" else None,
        default_response_class=_PrecisionResponse,
    )

    from src.api.auth import AuthMiddleware, auth_router
    app.add_middleware(AuthMiddleware)
    app.include_router(router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    register_exception_handlers(app)

    logger.info("create_app 完成", env=settings.env)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.main:app", host="0.0.0.0", port=8000, reload=True,
        loop="src.main:selector_event_loop_factory",
    )
