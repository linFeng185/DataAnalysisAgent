"""structlog 结构化日志配置。"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

from src.config import get_settings


# 方法作用：隐藏字符串中 URL userinfo 的密码部分。
# Args: value - 可能包含认证 URL 的日志字符串。
# Returns: 已隐藏 URL 密码的字符串。
def _redact_url_credentials(value: str) -> str:
    return re.sub(
        r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^\s/:@]*):(?P<password>[^\s/@]+)@",
        r"\g<scheme>\g<user>:[REDACTED]@",
        value,
        flags=re.IGNORECASE,
    )


# 方法作用：递归脱敏嵌套日志映射中的凭证、查询、SQL 和 PII。
# Args: mapping - 当前层结构化日志字段。
# Returns: 当前层已脱敏字段。
def _redact_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    sensitive_names = {"query", "query_preview", "user_query", "sql_preview"}
    credential_markers = (
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "credential",
    )
    sql_pattern = re.compile(
        r"\b(select|insert|update|delete|merge|call|with|explain|show|describe)\b",
        re.IGNORECASE,
    )
    pii_pattern = re.compile(
        r"(?:1[3-9]\d{9})|(?:\d{17}[\dXx])|(?:[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})"
    )
    redacted: dict[str, Any] = {}
    for key, value in mapping.items():
        normalized_key = str(key).lower()
        if any(marker in normalized_key for marker in credential_markers):
            redacted[key] = "[REDACTED]"
            continue
        if isinstance(value, Mapping):
            redacted[key] = _redact_mapping(value)
            continue
        if isinstance(value, list):
            redacted[key] = [
                _redact_mapping(item) if isinstance(item, Mapping) else (
                    _redact_url_credentials(item) if isinstance(item, str) else item
                )
                for item in value
            ]
            continue
        if not isinstance(value, str):
            redacted[key] = value
            continue

        sanitized = _redact_url_credentials(value)
        if normalized_key == "event":
            redacted[key] = sanitized
            continue
        named_sensitive = (
            normalized_key in sensitive_names
            or ("sql" in normalized_key and not normalized_key.endswith("_hash"))
        )
        content_sensitive = bool(sql_pattern.search(sanitized) or pii_pattern.search(sanitized))
        if named_sensitive or content_sensitive:
            digest = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
            redacted[f"{key}_hash"] = digest
            redacted[f"{key}_chars"] = len(sanitized)
            continue
        redacted[key] = sanitized
    return redacted


# 方法作用：在日志渲染前递归隐藏凭证并把 SQL、用户查询和疑似 PII 替换为 hash 与长度。
# Args: logger - structlog 日志器；method_name - 日志级别；event_dict - 结构化日志字段。
# Returns: 不含查询原文和敏感标识符的日志字段。
def _redact_sensitive_fields(logger, method_name: str, event_dict: dict) -> dict:
    del logger, method_name
    return _redact_mapping(event_dict)


# 方法作用：初始化 structlog 与控制台、七天轮转文件日志。
# Args: 无。
# Returns: 无返回值。
def setup_logging() -> None:
    """初始化 structlog，并配置控制台及保留七天的文件日志。

    Args:
        无。

    Returns:
        无返回值。
    """
    settings = get_settings()

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.stdlib.PositionalArgumentsFormatter(),
        _redact_sensitive_fields,
        structlog.processors.UnicodeDecoder(),
    ]

    if settings.log_format == "json":
        shared_processors.insert(-1, structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False)
    else:
        renderer = structlog.dev.ConsoleRenderer(
            exception_formatter=structlog.dev.RichTracebackFormatter(show_locals=False),
        )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    log_path = Path(settings.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        log_path,
        when="D",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    for old_handler in root_logger.handlers:
        old_handler.close()
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    root_logger.setLevel(settings.log_level)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("chromadb").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    # 移除 SQLAlchemy 自带的 handler，防止与 structlog handler 重复输出
    logging.getLogger("sqlalchemy.engine").handlers.clear()
    logging.getLogger("sqlalchemy.engine").propagate = True
    # 接口访问摘要由 ApiAccessPolicyMiddleware 统一输出，避免 Uvicorn 重复记录。
    logging.getLogger("uvicorn.access").disabled = True
    structlog.get_logger(__name__).info(
        "日志配置完成", log_file=str(log_path), backup_days=file_handler.backupCount,
    )


# 方法作用：按模块名称获取已配置的结构化日志实例。
# Args: name - 日志器名称，通常使用模块的 __name__。
# Returns: 可记录结构化字段的 BoundLogger。
def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """获取结构化日志实例。"""
    return structlog.get_logger(name)
