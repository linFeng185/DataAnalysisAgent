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

    # 初始化演示数据源
    from src.datasource.setup import ensure_demo_datasource
    await ensure_demo_datasource()

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
