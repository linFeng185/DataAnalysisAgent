"""structlog 结构化日志配置。"""

from __future__ import annotations

import hashlib
import logging
import re
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import structlog

from src.config import get_settings


# 方法作用：在日志渲染前把 SQL、用户查询和疑似 PII 字符串替换为 hash 与长度。
# Args: logger - structlog 日志器；method_name - 日志级别；event_dict - 结构化日志字段。
# Returns: 不含查询原文和敏感标识符的日志字段。
def _redact_sensitive_fields(logger, method_name: str, event_dict: dict) -> dict:
    del logger, method_name
    sensitive_names = {"query", "query_preview", "user_query", "sql_preview"}
    sql_pattern = re.compile(r"\b(select|insert|update|delete|merge|call|with|explain|show|describe)\b", re.I)
    pii_pattern = re.compile(
        r"(?:1[3-9]\d{9})|(?:\d{17}[\dXx])|(?:[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})"
    )
    redacted = dict(event_dict)
    for key, value in list(event_dict.items()):
        if key == "event" or not isinstance(value, str):
            continue
        normalized_key = key.lower()
        named_sensitive = (
            normalized_key in sensitive_names
            or ("sql" in normalized_key and not normalized_key.endswith("_hash"))
        )
        content_sensitive = bool(sql_pattern.search(value) or pii_pattern.search(value))
        if not named_sensitive and not content_sensitive:
            continue
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        redacted.pop(key, None)
        redacted[f"{key}_hash"] = digest
        redacted[f"{key}_chars"] = len(value)
    return redacted


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
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

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
    structlog.get_logger(__name__).info(
        "日志配置完成", log_file=str(log_path), backup_days=file_handler.backupCount,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """获取结构化日志实例。"""
    return structlog.get_logger(name)
