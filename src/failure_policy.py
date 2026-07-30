"""跨模块异常处理决策矩阵。"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType

from src.logging_config import get_logger

logger = get_logger(__name__)


class FailureMode(StrEnum):
    """异常发生后的统一处理模式。"""

    FAIL_CLOSED = "fail_closed"
    FAIL_OPEN = "fail_open"


class FailureDomain(StrEnum):
    """需要显式声明异常策略的系统边界。"""

    SQL_SECURITY = "sql_security"
    DATABASE = "database"
    LLM = "llm"
    KNOWLEDGE = "knowledge"
    DATA_PROCESSOR = "data_processor"


_FAILURE_MODES = MappingProxyType({
    FailureDomain.SQL_SECURITY: FailureMode.FAIL_CLOSED,
    FailureDomain.DATABASE: FailureMode.FAIL_CLOSED,
    FailureDomain.LLM: FailureMode.FAIL_OPEN,
    FailureDomain.KNOWLEDGE: FailureMode.FAIL_OPEN,
    FailureDomain.DATA_PROCESSOR: FailureMode.FAIL_OPEN,
})


# 方法作用：读取指定系统边界的异常处理模式。
# Args: domain - SQL、数据库、LLM、知识库或数据处理器边界。
# Returns: 对应的 fail-open 或 fail-closed 模式。
def get_failure_mode(domain: FailureDomain) -> FailureMode:
    logger.debug("读取异常处理策略入口", domain=domain.value)
    try:
        result = _FAILURE_MODES[domain]
    except KeyError as exc:
        logger.error("读取异常处理策略失败", domain=str(domain), exc_info=True)
        raise ValueError(f"未声明异常处理策略: {domain}") from exc
    logger.info("读取异常处理策略完成", domain=domain.value, mode=result.value)
    return result


# 方法作用：判断指定边界发生异常时是否必须阻断后续操作。
# Args: domain - 待判断的系统边界。
# Returns: fail-closed 时返回 True。
def must_fail_closed(domain: FailureDomain) -> bool:
    logger.debug("判断失败关闭策略入口", domain=domain.value)
    result = get_failure_mode(domain) is FailureMode.FAIL_CLOSED
    logger.info("判断失败关闭策略完成", domain=domain.value, fail_closed=result)
    return result


# 方法作用：判断指定边界发生异常时是否允许执行可用性降级。
# Args: domain - 待判断的系统边界。
# Returns: fail-open 时返回 True。
def fallback_allowed(domain: FailureDomain) -> bool:
    logger.debug("判断失败开放策略入口", domain=domain.value)
    result = get_failure_mode(domain) is FailureMode.FAIL_OPEN
    logger.info("判断失败开放策略完成", domain=domain.value, fail_open=result)
    return result
