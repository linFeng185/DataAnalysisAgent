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


def register_exception_handlers(app) -> None:
    @app.exception_handler(DataSourceNotFoundError)
    async def _ds_not_found(request: Request, exc: DataSourceNotFoundError):
        return JSONResponse(status_code=404, content={
            "success": False, "error_code": "DATASOURCE_NOT_FOUND", "error_message": str(exc),
        })

    @app.exception_handler(SQLValidationError)
    async def _validation(request: Request, exc: SQLValidationError):
        return JSONResponse(status_code=422, content={
            "success": False, "error_code": "SQL_VALIDATION_FAILED",
            "error_message": str(exc), "detail": exc.errors,
        })

    @app.exception_handler(SQLSecurityError)
    async def _security(request: Request, exc: SQLSecurityError):
        return JSONResponse(status_code=403, content={
            "success": False, "error_code": "SQL_SECURITY_BLOCKED", "error_message": str(exc),
        })

    @app.exception_handler(ExecutionError)
    async def _execution(request: Request, exc: ExecutionError):
        return JSONResponse(status_code=500, content={
            "success": False, "error_code": "EXECUTION_FAILED",
            "error_message": exc.message, "detail": {"retry_count": exc.retry_count},
        })

    @app.exception_handler(RateLimitError)
    async def _rate_limit(request: Request, exc: RateLimitError):
        return JSONResponse(status_code=429, content={
            "success": False, "error_code": "RATE_LIMITED", "error_message": str(exc),
        })

    @app.exception_handler(DataAnalysisAgentError)
    async def _agent(request: Request, exc: DataAnalysisAgentError):
        return JSONResponse(status_code=400, content={
            "success": False, "error_code": "AGENT_ERROR", "error_message": str(exc),
        })

    @app.exception_handler(Exception)
    async def _generic(request: Request, exc: Exception):
        logger.error("未处理异常", path=str(request.url), error=str(exc))
        return JSONResponse(status_code=500, content={
            "success": False, "error_code": "INTERNAL_ERROR", "error_message": "服务器内部错误",
        })
