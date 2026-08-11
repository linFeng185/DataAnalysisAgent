"""FastAPI 应用入口 — 挂载路由 + 异常处理。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from src.api.middleware import register_exception_handlers
from src.api.routes import router
from src.app_context import (
    AppContext,
    AppContextMiddleware,
    create_app_context,
    use_app_context_async,
)
from src.bootstrap import bootstrap_all, shutdown_all
from src.config import Settings, get_settings
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
    context: AppContext = app.state.app_context
    setup_logging()
    async with use_app_context_async(context):
        settings = context.settings
        logger.info("=" * 50)
        logger.info("智能体启动", env=settings.env, version="0.1.0")
        logger.info(
            "LLM 配置",
            source="tenant_admin",
            local_model_configured=bool(settings.local_llm_model and settings.local_llm_base_url),
        )
        logger.info("=" * 50)

        try:
            await bootstrap_all(settings, context=context)
            yield
        finally:
            await shutdown_all(context)
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
            from src.api.streaming import _json_serialize, _PrecisionEncoder
            return _json.dumps(content, cls=_PrecisionEncoder, default=_json_serialize,
                               ensure_ascii=False).encode("utf-8")

    app = FastAPI(
        title="Data Analysis Agent",
        description="LLM 驱动的数据分析智能体",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.env == "dev" else None,
        redoc_url="/redoc" if settings.env == "dev" else None,
        openapi_url="/openapi.json" if settings.env == "dev" else None,
        default_response_class=_PrecisionResponse,
    )
    context = create_app_context(settings)
    app.state.app_context = context

    from src.api.access_policy import ApiAccessPolicyMiddleware
    from src.api.auth import AuthMiddleware, auth_router
    from src.api.security_headers import SecurityHeadersMiddleware
    from src.observability import PrometheusMiddleware, get_metrics_registry

    allowed_origins = [
        origin.strip()
        for origin in str(getattr(settings, "cors_allowed_origins", "")).split(",")
        if origin.strip()
    ]
    app.add_middleware(AuthMiddleware)
    app.add_middleware(ApiAccessPolicyMiddleware)
    app.add_middleware(PrometheusMiddleware, metrics=get_metrics_registry())
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Admin-Key"],
    )
    app.add_middleware(
        SecurityHeadersMiddleware,
        production=settings.env == "prod",
        hsts_seconds=int(getattr(settings, "security_hsts_seconds", 31_536_000)),
    )
    app.add_middleware(AppContextMiddleware, context=context)
    app.include_router(router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    register_exception_handlers(app)

    logger.info("create_app 完成", env=settings.env)
    return app


# 方法作用：判断当前进程是否应启用 Uvicorn 开发热重载。
# Args: settings - 当前应用配置；debugger_attached - 可选调试器连接状态。
# Returns: 仅开发环境显式启用且未连接调试器时返回 True。
def _should_enable_reload(
    settings: Settings,
    *,
    debugger_attached: bool | None = None,
) -> bool:
    """避免调试器多进程与 Uvicorn reload 叠加派生重复服务进程。"""
    attached = sys.gettrace() is not None if debugger_attached is None else debugger_attached
    return bool(
        settings.env == "dev"
        and settings.server_reload_enabled
        and not attached
    )


# 作为可导入 ASGI 模块时创建应用；直接执行时由 run_server 按运行模式创建。
app: FastAPI | None = create_app() if __name__ != "__main__" else None


# 方法作用：按环境和调试器状态启动 Uvicorn 服务。
# Args: 无。
# Returns: 服务退出后无返回值。
def run_server() -> None:
    """启动开发服务，并在无 reload 时直接传入应用对象以避免二次导入。"""
    import uvicorn

    settings = get_settings()
    reload_enabled = _should_enable_reload(settings)
    target = "src.main:app" if reload_enabled else create_app()
    loop_factory = (
        "src.main:selector_event_loop_factory"
        if reload_enabled
        else selector_event_loop_factory
    )
    uvicorn.run(
        target,
        host="0.0.0.0",
        port=8000,
        reload=reload_enabled,
        loop=loop_factory,
        access_log=False,
    )


if __name__ == "__main__":
    run_server()
