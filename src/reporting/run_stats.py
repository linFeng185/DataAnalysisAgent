"""报表运行计数：LLM 调用与 SQL 执行的真实计数（覆盖 23.21）。

设计要点：contextvars 在 asyncio.create_task 时复制上下文，若计数器是不可变结构，
子任务里的自增只改副本、父任务读不到，导致数字静默偏小。因此本模块的上下文里
存的是**可变对象引用**（RunStats 实例），子任务复制的是同一个引用，修改对所有
任务可见。不得把 RunStats 改成 int、tuple 或 frozen dataclass。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

from src.logging_config import get_logger

logger = get_logger(__name__)


# 类作用：一次报表运行的真实计数（LLM 调用 · token · SQL 执行）。
# 前提：执行模型是 asyncio 单线程事件循环，属性自增不会被撕裂；
#       若将来引入多线程或多进程执行，需改为带锁实现。
@dataclass
class RunStats:
    """报表运行期的可变计数器，跨 asyncio 子任务共享同一实例。"""

    llm_call_count: int = 0
    llm_failure_count: int = 0
    llm_token_usage: int = 0
    # 输入/输出拆分：原先只累加总量，明细页看不到"用了多少输入、多少输出 token"。
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    sql_query_count: int = 0
    sql_success_count: int = 0
    sql_failure_count: int = 0
    # 运行期硬预算与真实结果计数分离：reserved 在访问数据源前占用，
    # query_count 在结果返回后计数，二者不能互相替代。
    sql_query_limit: int | None = None
    sql_reserved_count: int = 0


_current: ContextVar[RunStats | None] = ContextVar("report_run_stats", default=None)


# 方法作用：读取当前作用域的计数器。
# Args: 无。
# Returns: 当前 RunStats 实例；不在报表运行作用域内时返回 None。
def current() -> RunStats | None:
    stats = _current.get()
    if stats is None:
        logger.debug("读取报表运行计数器", active=False)
    return stats


# 方法作用：在报表运行作用域内激活计数器并设置可选 SQL 查询硬上限。
# Args: sql_query_limit - 本轮允许提交的最大 SQL 尝试数；None 表示暂未配置硬上限，0 表示禁止 SQL。
# Returns: 本次运行的计数器对象，以及用于恢复上下文的重置令牌。
def activate(*, sql_query_limit: int | None = None) -> tuple[RunStats, Token]:
    if sql_query_limit is not None and int(sql_query_limit) < 0:
        raise ValueError("SQL 查询上限不能为负数")
    stats = RunStats(
        sql_query_limit=None if sql_query_limit is None else int(sql_query_limit),
    )
    token = _current.set(stats)
    logger.info("报表运行计数器已激活", sql_query_limit=stats.sql_query_limit)
    return stats, token


# 方法作用：退出报表运行作用域并恢复上一层计数器。
# Args: token - activate 返回的重置令牌。
# Returns: 无返回值。
def deactivate(token: Token) -> None:
    _current.reset(token)
    logger.info("报表运行计数器已重置")


# 方法作用：给当前报表运行设置 SQL 查询硬上限并清零既有占用。
# Args: limit - 本轮预占后允许提交的最大 SQL 尝试数，必须为非负整数。
# Returns: 无返回值；无活动报表计数器时为空操作。
def configure_sql_query_limit(limit: int) -> None:
    stats = _current.get()
    if stats is None:
        return
    bounded = int(limit)
    if bounded < 0:
        raise ValueError("SQL 查询上限不能为负数")
    stats.sql_query_limit = bounded
    stats.sql_reserved_count = 0
    logger.info("报表 SQL 查询预算已配置", sql_query_limit=bounded)


# 方法作用：在访问数据源前为当前调用原子占用一次 SQL 查询额度。
# Args: 无。
# Returns: 非报表路径或仍有额度时返回 True；报表额度耗尽时返回 False。
def try_reserve_sql_query() -> bool:
    stats = _current.get()
    if stats is None or stats.sql_query_limit is None:
        return True
    # 本项目的报表单元并发运行在同一 asyncio 事件循环；此处无 await，
    # 比较与自增在一次 task step 内完成，不会在两者之间切换协程。
    if stats.sql_reserved_count >= stats.sql_query_limit:
        logger.warning(
            "报表 SQL 查询预算已耗尽",
            sql_query_limit=stats.sql_query_limit,
            sql_reserved_count=stats.sql_reserved_count,
        )
        return False
    stats.sql_reserved_count += 1
    logger.debug(
        "报表 SQL 查询额度已占用",
        sql_query_limit=stats.sql_query_limit,
        sql_reserved_count=stats.sql_reserved_count,
    )
    return True


# 方法作用：只读判断当前报表运行是否仍有 SQL 查询额度，不执行占用。
# Args: 无。
# Returns: 非报表路径、未配置上限或仍有额度时返回 True；额度耗尽时返回 False。
def has_sql_query_capacity() -> bool:
    stats = _current.get()
    if stats is None or stats.sql_query_limit is None:
        return True
    return stats.sql_reserved_count < stats.sql_query_limit


# 方法作用：把一次 LLM 调用结果写入指定计数器；目标为 None 时是空操作。
# Args: stats - 目标计数器（回调在挂载时按引用捕获的对象）；failed - 本次调用是否失败；tokens - prompt 与 completion token 之和。
# Returns: 无返回值。
def record_llm_call_for(
    stats: RunStats | None,
    *,
    failed: bool = False,
    tokens: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    if stats is None:
        return
    stats.llm_call_count += 1
    if failed:
        stats.llm_failure_count += 1
    if tokens > 0:
        stats.llm_token_usage += int(tokens)
    # 拆分单独累加：只给总量的调用方（旧回调）保持原行为，不强行拆分。
    stats.llm_prompt_tokens += int(prompt_tokens or 0)
    stats.llm_completion_tokens += int(completion_tokens or 0)
    logger.debug(
        "记录 LLM 调用",
        failed=failed,
        tokens=tokens,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        llm_call_count=stats.llm_call_count,
        llm_failure_count=stats.llm_failure_count,
    )


# 方法作用：记录一次 LLM 调用结果到当前作用域计数器；无活动计数器时是空操作。
# Args: failed - 本次调用是否失败；tokens - 本次消耗的 prompt 与 completion token 之和。
# Returns: 无返回值。
def record_llm_call(
    failed: bool = False,
    tokens: int = 0,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    record_llm_call_for(
        _current.get(),
        failed=failed,
        tokens=tokens,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


# 方法作用：记录一次 SQL 执行结果；无活动计数器时是空操作。
# Args: failed - 本次执行是否失败（含校验失败、执行异常与超时）。
# Returns: 无返回值。
def record_sql_execution(failed: bool = False) -> None:
    stats = _current.get()
    if stats is None:
        return
    stats.sql_query_count += 1
    if failed:
        stats.sql_failure_count += 1
    else:
        stats.sql_success_count += 1
    logger.debug(
        "记录 SQL 执行",
        failed=failed,
        sql_query_count=stats.sql_query_count,
        sql_success_count=stats.sql_success_count,
        sql_failure_count=stats.sql_failure_count,
    )
