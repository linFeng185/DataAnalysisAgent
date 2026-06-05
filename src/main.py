"""FastAPI 应用入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.config import get_settings
from src.logging_config import get_logger, setup_logging

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。"""
    setup_logging()
    settings = get_settings()
    logger.info("智能体启动中", env=settings.env, provider=settings.llm_provider)

    # TODO Phase 1: DataSourceRegistry 初始化
    # TODO Phase 1: MCPClientManager.connect_all()
    # TODO Phase 1: SkillManager.discover()
    # TODO Phase 1: BusinessRuleStore.initialize()

    yield

    # TODO Phase 1: MCPClientManager.close_all()
    logger.info("智能体已关闭")


def create_app() -> FastAPI:
    """创建 FastAPI 实例。"""
    settings = get_settings()

    app = FastAPI(
        title="Data Analysis Agent",
        description="LLM 驱动的数据分析智能体",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.env == "dev" else None,
    )

    # TODO Phase 1: 挂载路由
    # app.include_router(chat_router, prefix="/api/v1")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)
