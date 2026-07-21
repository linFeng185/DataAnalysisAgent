"""FastAPI 应用入口 — 挂载路由 + 异常处理。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.middleware import register_exception_handlers
from src.api.routes import router
from src.bootstrap import bootstrap_all, shutdown_all
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


# 方法作用：管理 FastAPI 应用启动、运行和关闭生命周期。
# Args: app - 当前 FastAPI 应用实例。
# Returns: 应用运行期间的异步上下文迭代器。
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    del app
    setup_logging()
    s = get_settings()
    logger.info("=" * 50)
    logger.info("智能体启动", env=s.env, version="0.1.0")
    logger.info("LLM 配置", provider=s.llm_provider, model=s.llm_model,
                 base_url=s.openai_base_url or "https://api.openai.com/v1",
                 api_key_set=bool(s.openai_api_key))
    logger.info("=" * 50)

    try:
        await bootstrap_all(s)
        yield
    finally:
        await shutdown_all()
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
