"""structlog 结构化日志配置。"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from collections.abc import Mapping
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, TextIO

import structlog

from src.config import get_settings

_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<label>\b[\w-]*(?:password|passwd|passphrase|secret|token|"
    r"api[_-]?key|apikey|authorization|cookie|credential)\b)"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?:(?:Bearer|Basic)\s+)?"
    r"(?:(?P<quote>['\"])(?P<quoted>[^\r\n]*?)(?P=quote)|"
    r"(?P<bare>[^\s,;\)\]}]+))",
    flags=re.IGNORECASE,
)
_BEARER_TOKEN_PATTERN = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/=-]+",
    flags=re.IGNORECASE,
)
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


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


# 方法作用：隐藏异常文本中的键值凭证和 Bearer/Basic 令牌。
# Args: value - 待脱敏的异常或堆栈文本。
# Returns: 已替换凭证值但保留异常定位信息的文本。
def _redact_exception_text(value: str) -> str:
    """在不丢失异常类型和调用栈的前提下移除常见凭证原文。"""
    # Rich 可能在字符中间插入 ANSI 样式码，先移除控制码再匹配凭证边界。
    sanitized = _ANSI_ESCAPE_PATTERN.sub("", value)
    sanitized = _redact_url_credentials(sanitized)
    sanitized = _CREDENTIAL_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group('label')}{match.group('separator')}[REDACTED]",
        sanitized,
    )
    return _BEARER_TOKEN_PATTERN.sub("Bearer [REDACTED]", sanitized)


# 方法作用：递归脱敏 format_exc_info 生成的异常字段。
# Args: logger - structlog 日志器；method_name - 当前日志级别；event_dict - 日志字段。
# Returns: 异常字段已脱敏的日志字段。
def _redact_exception_fields(logger, method_name: str, event_dict: dict) -> dict:
    """处理 JSON 渲染前已经格式化的异常字段。"""
    del logger, method_name
    exception = event_dict.get("exception")
    if isinstance(exception, str):
        event_dict["exception"] = _redact_exception_text(exception)
    elif isinstance(exception, list):
        event_dict["exception"] = [
            _redact_exception_text(item) if isinstance(item, str) else item
            for item in exception
        ]
    return event_dict


# 方法作用：在 Rich traceback 写入日志前统一脱敏异常文本。
# Args: stream - Rich 输出目标；exc_info - Python 异常三元组。
# Returns: 无返回值，结果写入 stream。
def _redacting_rich_exception_formatter(stream: TextIO, exc_info: Any) -> None:
    """保留 Rich 堆栈格式，同时过滤异常消息中的敏感值。"""
    buffer = io.StringIO()
    structlog.dev.RichTracebackFormatter(show_locals=False)(buffer, exc_info)
    stream.write(_redact_exception_text(buffer.getvalue()))


# 方法作用：兼容新旧 BoundLogger 的异常字段并避免重复格式化警告。
# Args: logger - 底层日志器；method_name - 当前日志级别；event_dict - 待渲染字段。
# Returns: 渲染后的控制台日志文本。
class _RedactingConsoleRenderer:
    """新异常使用 Rich，旧缓存 logger 的字符串异常使用普通堆栈渲染。"""

    # 方法作用：初始化新旧异常字段所需的控制台渲染器。
    # Args: self - 控制台渲染器实例。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self._rich_renderer = structlog.dev.ConsoleRenderer(
            exception_formatter=_redacting_rich_exception_formatter,
        )
        self._plain_renderer = structlog.dev.ConsoleRenderer(
            exception_formatter=structlog.dev.plain_traceback,
        )

    # 方法作用：根据异常字段形态选择兼容的渲染器并过滤异常原文。
    # Args: logger - 底层日志器；method_name - 当前日志级别；event_dict - 待渲染字段。
    # Returns: 控制台格式的日志文本。
    def __call__(self, logger: Any, method_name: str, event_dict: dict[str, Any]) -> str:
        exception = event_dict.get("exception")
        if isinstance(exception, str):
            event_dict["exception"] = _redact_exception_text(exception)
            return self._plain_renderer(logger, method_name, event_dict)
        return self._rich_renderer(logger, method_name, event_dict)


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

        sanitized = _redact_exception_text(value)
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
        shared_processors.insert(-1, _redact_exception_fields)
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False)
    else:
        renderer = _RedactingConsoleRenderer()

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
