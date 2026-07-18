"""structlog 结构化日志配置。"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import structlog

from src.config import get_settings


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
    sqlalchemy_log_level = logging.INFO if settings.env == "dev" else logging.WARNING
    logging.getLogger("sqlalchemy.engine").setLevel(sqlalchemy_log_level)
    # 移除 SQLAlchemy 自带的 handler，防止与 structlog handler 重复输出
    logging.getLogger("sqlalchemy.engine").handlers.clear()
    logging.getLogger("sqlalchemy.engine").propagate = True
    structlog.get_logger(__name__).info(
        "日志配置完成", log_file=str(log_path), backup_days=file_handler.backupCount,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """获取结构化日志实例。"""
    return structlog.get_logger(name)
