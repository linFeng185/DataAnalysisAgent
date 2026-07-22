"""1.3.7 全局异常处理 — 统一错误响应格式。"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from src.exceptions import (
    DataAnalysisAgentError, DataSourceNotFoundError, ExecutionError,
    RateLimitError, SQLSecurityError, SQLValidationError,
)
from src.logging_config import get_logger

logger = get_logger(__name__)


# 方法作用：为 FastAPI 应用注册领域异常到安全 HTTP 响应的统一映射。
# Args: app - 待注册异常处理器的 FastAPI 应用。
# Returns: 无返回值。
def register_exception_handlers(app) -> None:
    """注册应用异常处理器并确保未知异常对客户端脱敏。"""
    logger.debug("全局异常处理器注册入口")

    # 方法作用：把数据源不存在异常映射为 404。
    # Args: request - 当前请求；exc - 数据源不存在异常。
    # Returns: 统一错误 JSON 响应。
    @app.exception_handler(DataSourceNotFoundError)
    async def _ds_not_found(request: Request, exc: DataSourceNotFoundError):
        logger.info("数据源不存在异常已映射", path=str(request.url))
        return JSONResponse(status_code=404, content={
            "success": False, "error_code": "DATASOURCE_NOT_FOUND", "error_message": str(exc),
        })

    # 方法作用：把 SQL 校验异常映射为 422。
    # Args: request - 当前请求；exc - SQL 校验异常。
    # Returns: 统一错误 JSON 响应。
    @app.exception_handler(SQLValidationError)
    async def _validation(request: Request, exc: SQLValidationError):
        logger.info("SQL 校验异常已映射", path=str(request.url), error_count=len(exc.errors))
        return JSONResponse(status_code=422, content={
            "success": False, "error_code": "SQL_VALIDATION_FAILED",
            "error_message": str(exc), "detail": exc.errors,
        })

    # 方法作用：把 SQL 安全拦截异常映射为 403。
    # Args: request - 当前请求；exc - SQL 安全异常。
    # Returns: 统一错误 JSON 响应。
    @app.exception_handler(SQLSecurityError)
    async def _security(request: Request, exc: SQLSecurityError):
        logger.warning("SQL 安全异常已映射", path=str(request.url))
        return JSONResponse(status_code=403, content={
            "success": False, "error_code": "SQL_SECURITY_BLOCKED", "error_message": str(exc),
        })

    # 方法作用：把 SQL 执行异常映射为脱敏 500。
    # Args: request - 当前请求；exc - SQL 执行异常。
    # Returns: 统一错误 JSON 响应。
    @app.exception_handler(ExecutionError)
    async def _execution(request: Request, exc: ExecutionError):
        logger.error("SQL 执行异常已映射", path=str(request.url), exc_info=True)
        return JSONResponse(status_code=500, content={
            "success": False, "error_code": "EXECUTION_FAILED",
            "error_message": exc.message, "detail": {"retry_count": exc.retry_count},
        })

    # 方法作用：把限流异常映射为 429。
    # Args: request - 当前请求；exc - 限流异常。
    # Returns: 统一错误 JSON 响应。
    @app.exception_handler(RateLimitError)
    async def _rate_limit(request: Request, exc: RateLimitError):
        logger.info("限流异常已映射", path=str(request.url))
        return JSONResponse(status_code=429, content={
            "success": False, "error_code": "RATE_LIMITED", "error_message": str(exc),
        })

    # 方法作用：把基础业务异常映射为 400。
    # Args: request - 当前请求；exc - 基础业务异常。
    # Returns: 统一错误 JSON 响应。
    @app.exception_handler(DataAnalysisAgentError)
    async def _agent(request: Request, exc: DataAnalysisAgentError):
        logger.info("业务异常已映射", path=str(request.url), exception=type(exc).__name__)
        return JSONResponse(status_code=400, content={
            "success": False, "error_code": "AGENT_ERROR", "error_message": str(exc),
        })

    # 方法作用：把未知异常映射为不暴露内部细节的 500。
    # Args: request - 当前请求；exc - 未处理异常。
    # Returns: 脱敏统一错误 JSON 响应。
    @app.exception_handler(Exception)
    async def _generic(request: Request, exc: Exception):
        logger.error("未处理异常", path=str(request.url), error=str(exc), exc_info=True)
        return JSONResponse(status_code=500, content={
            "success": False, "error_code": "INTERNAL_ERROR", "error_message": "服务器内部错误",
        })
    logger.info("全局异常处理器注册完成", handler_count=7)
