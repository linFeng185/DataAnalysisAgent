"""FastAPI 应用入口 — 挂载路由 + 异常处理。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.middleware import register_exception_handlers
from src.api.routes import router
from src.config import get_settings
from src.logging_config import get_logger, setup_logging

logger = get_logger(__name__)


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

    # 预热 SchemaManager（提前加载嵌入模型 + ChromaDB，避免首次检索时等待）
    try:
        from src.knowledge.schema_manager import get_schema_manager
        sm = get_schema_manager()
        sm._ensure_initialized()  # noqa: SLF001
        logger.info("SchemaManager 预热完成（嵌入模型 + ChromaDB）")
    except Exception as e:
        logger.warning("SchemaManager 预热失败", error=str(e))

    # 预热 LLM 客户端（验证 API Key + 网络连通性）
    try:
        from src.llm.client import is_llm_available, get_llm
        if is_llm_available():
            get_llm(temperature=0, reasoning=False)
            logger.info("LLM 客户端预热完成", model=s.llm_model)
    except Exception as e:
        logger.warning("LLM 预热失败", error=str(e))

    # 9.1.3 加载 Skills
    try:
        from src.skill_manager import get_skill_manager
        await get_skill_manager(s.skills_dir, s.extra_skills_dirs).discover()
    except Exception as e:
        logger.warning("Skill 加载失败", error=str(e))

    # 8.1.2 连接外部 MCP Server
    try:
        from src.mcp.client_manager import get_mcp_client_manager
        await get_mcp_client_manager().connect_all()
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
    settings = get_settings()

    app = FastAPI(
        title="Data Analysis Agent",
        description="LLM 驱动的数据分析智能体",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.env == "dev" else None,
    )

    app.include_router(router, prefix="/api/v1")
    register_exception_handlers(app)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)
