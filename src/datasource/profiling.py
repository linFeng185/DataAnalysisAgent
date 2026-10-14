"""SQL 数据画像与结构化探查服务：有界采样、授权复核、方言适配与证据渲染。

设计：spec/55-sql-profiling-and-probing.md 第 5、7、14 节。
模块边界：只读采样，不写业务库，不接受自由 SQL，不替代列/行级授权——
统一执行器不做行列权限，本模块必须在取样前自行完成身份、数据源授权、
投影列白名单与行过滤注入。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as time_of_day
from decimal import Decimal
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.config import get_settings
from src.datasource.schema_snapshot import TableSchema
from src.logging_config import get_logger
from src.security.data_masker import mask_sensitive_data
from src.security.sql_execution import normalize_sql_dialect, validate_and_execute_sql
from src.security.tenant_policy import ANONYMOUS_ROLE, normalize_role

logger = get_logger(__name__)

# ── 统计范围与唯一性语义（§5）────────────────────────────
SCOPE_COMPLETE = "complete"
SCOPE_SAMPLE = "sample"
UNIQUENESS_UNIQUE = "unique"
UNIQUENESS_DUPLICATE = "duplicate"
UNIQUENESS_UNKNOWN = "unknown"

# ── 脱敏错误码（不携带 SQL、异常原文或连接信息）────────────
ERROR_DISABLED = "disabled"
ERROR_FORBIDDEN = "forbidden"
ERROR_POLICY = "policy_unavailable"
ERROR_UNSUPPORTED = "unsupported_dialect"
ERROR_SYNC_DRIVER = "synchronous_driver"
ERROR_TIMEOUT = "timeout"
ERROR_EXECUTION = "execution_error"
ERROR_QUERY_BUDGET = "query_budget"
ERROR_TOTAL_TIME = "total_timeout"
ERROR_TABLE_LIMIT = "table_limit"
ERROR_UNKNOWN_TABLE = "unknown_table"
ERROR_UNKNOWN_COLUMN = "unknown_column"
ERROR_COLUMN_NOT_ALLOWED = "column_not_allowed"
ERROR_BUDGET_EXHAUSTED = "budget_exhausted"

# ── 默认边界（与 src/config.py 的 Field 默认一致）──────────
DEFAULT_MAX_TABLES = 6
DEFAULT_MAX_COLUMNS_PER_TABLE = 6
DEFAULT_SAMPLE_ROWS = 2_000
# 取样行数的额外硬上限：该配置只是天花板，不授权任何无界扫描。
DEFAULT_FULL_SCAN_ROW_LIMIT = 50_000
DEFAULT_MAX_QUERIES = 6
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_TOTAL_SECONDS = 8.0
DEFAULT_TTL_SECONDS = 300.0
DEFAULT_CACHE_ENTRIES = 128
DEFAULT_PROBE_MAX_CALLS = 3
DEFAULT_PROBE_LIMIT = 50

# 结果与文本硬上限：单值、画像文本、探查结果总字符。
VALUE_MAX_CHARS = 200
TOP_VALUES_LIMIT = 8
PROFILE_TEXT_MAX_CHARS = 6_000
PROBE_RESULT_MAX_CHARS = 6_000
# 锁字典上限：超出后清理空闲锁，锁只用于合并同键并发填充。
_LOCK_ENTRY_LIMIT = 256

# 受控且可超时的方言白名单：驱动必须支持真正的异步取消。
# Oracle/MSSQL 等未验证方言必须明确降级，不得伪称已停止服务端查询。
_BOUNDED_DIALECTS = frozenset({"mysql", "postgres", "postgresql", "clickhouse", "sqlite"})

PROBE_OPERATIONS = ("entity_lookup", "value_domain", "column_repeat", "time_range", "table_sample")
_SINGLE_COLUMN_OPERATIONS = frozenset({"entity_lookup", "value_domain", "column_repeat", "time_range"})

# 时间范围兼容判定：类型名或统一文本格式二者其一成立。
_TEMPORAL_TYPE_MARKERS = ("date", "time", "timestamp", "datetime")
_TEXT_TYPE_PREFIXES = ("char", "varchar", "nvarchar", "nchar", "text", "string", "clob")


# 方法作用：返回单调时钟秒数，独立函数便于测试注入时间。
# Args: 无。
# Returns: 当前单调时钟秒数。
def _now() -> float:
    return time.monotonic()


class _PolicyError(RuntimeError):
    """行列策略不可安全应用时使用的内部异常；由调用方转成脱敏错误码。"""


# ── 对外契约（§14）──────────────────────────────────────


class ProfilingContext(BaseModel):
    """一次画像/探查的服务端上下文：真实身份、授权表结构、行列策略与结构指纹。

    由服务端在请求路径上构造；模型与工具参数都不能提供或替换其中的任何字段。
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: int = Field(ge=0)
    user_id: int = Field(ge=0)
    role: str = Field(default="", max_length=64)
    datasource: str = Field(min_length=1, max_length=128)
    dialect: str = Field(default="", max_length=32)
    # 完整授权表结构：键为表名，值为该表的授权列结构。
    tables: dict[str, TableSchema] = Field(default_factory=dict)
    allowed_columns: list[str] = Field(default_factory=list)
    row_filter_sql: str = Field(default="", max_length=2_000)
    schema_fingerprint: str = Field(default="", max_length=200)


class ColumnProfile(BaseModel):
    """单列画像统计；scope 标明统计范围是完整可见数据还是有界样本。"""

    name: str
    scope: Literal["complete", "sample"] = SCOPE_SAMPLE
    null_count: int = 0
    non_null_count: int = 0
    distinct_count: int = 0
    top_values: list[dict[str, Any]] = Field(default_factory=list)
    top_value: Any = None
    # 示例值是否被脱敏：脱敏值只是展示形态，不能作为精确过滤原值使用。
    top_value_masked: bool = False
    top_frequency: int = 0
    uniqueness: Literal["unique", "duplicate", "unknown"] = UNIQUENESS_UNKNOWN
    # 单组且频次等于行数表示常量列：它不是唯一列，也不能作实体键。
    constant: bool = False


class TableProfile(BaseModel):
    """单表画像统计；error 非空时表示该表未取得有效统计。"""

    name: str
    scope: Literal["complete", "sample"] = SCOPE_SAMPLE
    sampled_rows: int = 0
    row_count_estimate: int = 0
    columns: list[ColumnProfile] = Field(default_factory=list)
    error: str = ""


class ProfileSnapshot(BaseModel):
    """一次画像调用的结构化结果；query_count 为本次真实新增查询数。"""

    tables: list[TableProfile] = Field(default_factory=list)
    query_count: int = 0
    errors: list[str] = Field(default_factory=list)


class ProbeRequest(BaseModel):
    """五种固定探查原语的参数契约；不接受自由 SQL、身份或权限字段。"""

    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "entity_lookup", "value_domain", "column_repeat", "time_range", "table_sample"
    ]
    table: str = Field(min_length=1, max_length=128)
    column: str = Field(default="", max_length=128)
    pattern: str = Field(default="", max_length=200)
    columns: list[str] = Field(default_factory=list, max_length=32)
    # 0 表示使用服务端默认上限；最终仍受 sql_probe_limit 强制约束。
    limit: int = Field(default=0, ge=0, le=1_000)

    # 方法作用：归一化文本字段并校验各原语的必需参数。
    # Args: self - 已初步解析的请求。
    # Returns: 校验通过的请求。
    @model_validator(mode="after")
    def _normalize_and_require_fields(self) -> "ProbeRequest":
        cleaned: list[str] = []
        for item in self.columns:
            name = str(item or "").strip()
            if not name or len(name) > 128:
                raise ValueError("columns 必须是长度不超过 128 的非空列名")
            if name not in cleaned:
                cleaned.append(name)
        self.columns = cleaned
        self.table = self.table.strip()
        self.column = self.column.strip()
        self.pattern = self.pattern.strip()
        if not self.table:
            raise ValueError("table 不能为空白")
        if self.operation in _SINGLE_COLUMN_OPERATIONS and not self.column:
            raise ValueError(f"{self.operation} 需要 column")
        if self.operation == "entity_lookup" and not self.pattern:
            raise ValueError("entity_lookup 需要非空 pattern")
        if self.operation == "table_sample" and not self.columns:
            raise ValueError("table_sample 需要 columns")
        return self


class ProbeResult(BaseModel):
    """一次探查的返回契约（§7）；样本未命中不代表全表不存在。"""

    success: bool
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    scope: Literal["complete", "sample"] = SCOPE_SAMPLE
    error: str = ""
    error_code: str = ""


@dataclass(slots=True)
class ProbeBudget:
    """一次上下文绑定内的探查次数预算，不跨请求共享。"""

    max_calls: int = DEFAULT_PROBE_MAX_CALLS
    calls: int = 0

    # 方法作用：在发起真实查询前占用一次额度。
    # Args: self - 当前预算。
    # Returns: 成功占用返回 True；已耗尽返回 False。
    def reserve(self) -> bool:
        if self.calls >= max(0, int(self.max_calls)):
            return False
        self.calls += 1
        return True


@dataclass(slots=True)
class ProbeScope:
    """一次探查作用域：绑定的服务端上下文与共享预算。"""

    context: ProfilingContext
    budget: ProbeBudget


_probe_scope_var: ContextVar[ProbeScope | None] = ContextVar("data_probe_scope", default=None)


# 方法作用：读取当前绑定的探查作用域。
# Args: 无。
# Returns: 已绑定的 ProbeScope；未绑定时返回 None。
def current_probe_scope() -> ProbeScope | None:
    return _probe_scope_var.get()


# 方法作用：读取探查调用次数上限配置。
# Args: 无。
# Returns: 正整数调用上限。
def _probe_max_calls() -> int:
    return max(1, int(getattr(get_settings(), "sql_probe_max_calls", DEFAULT_PROBE_MAX_CALLS)))


# 方法作用：读取单次探查查询超时配置。
# Args: 无。
# Returns: 正数秒数。
def _probe_timeout_seconds() -> float:
    return max(
        0.001,
        float(getattr(get_settings(), "sql_probe_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
    )


# 方法作用：读取探查结果行数上限配置。
# Args: 无。
# Returns: 正整数行数上限。
def _probe_limit() -> int:
    return max(1, int(getattr(get_settings(), "sql_probe_limit", DEFAULT_PROBE_LIMIT)))


# 方法作用：绑定一次请求的探查上下文与共享预算。
# Args: context - 服务端构造的画像上下文。
# Returns: 产出 ProbeScope 的上下文管理器，退出时恢复原作用域。
@contextmanager
def bind_probe_scope(context: ProfilingContext) -> Iterator[ProbeScope]:
    """同一绑定内的所有工具实例共享预算；重复构建工具不得重置。"""
    scope = ProbeScope(context=context, budget=ProbeBudget(max_calls=_probe_max_calls()))
    token = _probe_scope_var.set(scope)
    logger.debug(
        "探查作用域绑定",
        datasource=context.datasource,
        tenant_id=context.tenant_id,
        max_calls=scope.budget.max_calls,
    )
    try:
        yield scope
    finally:
        _probe_scope_var.reset(token)


# 方法作用：读取画像开关与各项预算的当前值。
# Args: 无。
# Returns: 画像配置字典。
def _profiling_settings() -> dict[str, Any]:
    settings = get_settings()
    return {
        "enabled": bool(getattr(settings, "sql_profiling_enabled", True)),
        "max_tables": max(1, int(getattr(settings, "sql_profiling_max_tables", DEFAULT_MAX_TABLES))),
        "max_columns": max(
            1,
            int(getattr(settings, "sql_profiling_max_columns_per_table", DEFAULT_MAX_COLUMNS_PER_TABLE)),
        ),
        "sample_rows": max(1, min(
            int(getattr(settings, "sql_profiling_sample_rows", DEFAULT_SAMPLE_ROWS)),
            int(getattr(
                settings, "sql_profiling_full_scan_row_limit", DEFAULT_FULL_SCAN_ROW_LIMIT
            )),
        )),
        "max_queries": max(1, int(getattr(settings, "sql_profiling_max_queries", DEFAULT_MAX_QUERIES))),
        "timeout_seconds": max(
            0.001,
            float(getattr(settings, "sql_profiling_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        ),
        "total_seconds": max(
            0.001,
            float(getattr(settings, "sql_profiling_total_seconds", DEFAULT_TOTAL_SECONDS)),
        ),
        "ttl_seconds": max(
            0.0, float(getattr(settings, "sql_profiling_ttl_seconds", DEFAULT_TTL_SECONDS))
        ),
        "cache_entries": max(
            1, int(getattr(settings, "sql_profiling_cache_entries", DEFAULT_CACHE_ENTRIES))
        ),
    }


# ── 值序列化与统计 ──────────────────────────────────────


# 方法作用：把任意驱动返回值安全序列化为可 JSON 化的标量并裁剪长度。
# Args: value - 驱动返回的原始值；max_chars - 文本长度上限。
# Returns: 可进入模型上下文的标量；无法安全表示时返回 None 或长度提示。
def _safe_cell(value: Any, max_chars: int = VALUE_MAX_CHARS) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, (datetime, date, time_of_day)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary:{len(bytes(value))}>"
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "…"
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= max_chars else text[:max_chars] + "…"


# 方法作用：把值转换为可哈希的统计键，保持类型不可哈希时的可比较性。
# Args: value - 驱动返回的原始值。
# Returns: 可哈希的键。
def _stat_key(value: Any) -> Any:
    try:
        hash(value)
    except TypeError:
        return _safe_cell(value)
    return value


# 方法作用：统计完成后复用既有脱敏策略处理单个输出值，防画像/探查成为脱敏旁路。
# Args: column - 原始列名（决定字段级规则）；value - 已完成统计的原始单元格值。
# Returns: (可对外展示值, 是否发生脱敏) 二元组。
def _mask_output_value(column: str, value: Any) -> tuple[Any, bool]:
    if value is None:
        return None, False
    try:
        masked = mask_sensitive_data([{column: value}])[0][column]
    except Exception:
        # 脱敏策略异常时宁可整体隐藏该值，也不能把原值透出；本边界只记一次堆栈。
        logger.error("画像输出脱敏失败", value_type=type(value).__name__, exc_info=True)
        return None, True
    return masked, masked != value


# 方法作用：按可见范围与重复事实判定列唯一性，常量列永不标记唯一。
# Args: row_count - 参与统计的可见行数；null_count - 空值数；distinct_count - 非空不同值数；
#       top_frequency - 最高频次；scope - 统计范围。
# Returns: (uniqueness, constant) 二元组。
def _uniqueness(
    row_count: int,
    null_count: int,
    distinct_count: int,
    top_frequency: int,
    scope: str,
) -> tuple[str, bool]:
    if row_count <= 0:
        # 空表没有任何可证明的唯一性。
        return UNIQUENESS_UNKNOWN, False
    if null_count >= row_count:
        # 全 NULL 列不是唯一列。
        return UNIQUENESS_UNKNOWN, False
    constant = distinct_count == 1 and top_frequency == row_count
    if constant:
        # 单组且频次等于行数：常量列。单行退化为未知，多行是重复。
        return (UNIQUENESS_DUPLICATE if row_count > 1 else UNIQUENESS_UNKNOWN), True
    if scope != SCOPE_COMPLETE:
        # 样本中未见重复只能标记 unknown，不得提升为全表唯一。
        return (UNIQUENESS_DUPLICATE if top_frequency > 1 else UNIQUENESS_UNKNOWN), False
    non_null = row_count - null_count
    if distinct_count < non_null:
        return UNIQUENESS_DUPLICATE, False
    if null_count == 0 and distinct_count == row_count:
        return UNIQUENESS_UNIQUE, False
    # 无重复但含 NULL：不能作为唯一键。
    return UNIQUENESS_UNKNOWN, False


# 方法作用：对单列样本值做空值、不同值数、最高频次与 TOP 值统计。
# Args: name - 列名；values - 按行顺序排列的原始值序列；scope - 统计范围。
# Returns: ColumnProfile。
def _summarize_column(name: str, values: list[Any], scope: str) -> ColumnProfile:
    null_count = sum(1 for value in values if value is None)
    counter = Counter(_stat_key(value) for value in values if value is not None)
    top_items = counter.most_common(TOP_VALUES_LIMIT)
    # 统计基于原始值；输出值按原列名脱敏。先统计后脱敏避免掩码碰撞改变基数与唯一性。
    top_values: list[dict[str, Any]] = []
    top_value_masked = False
    for index, (value, count) in enumerate(top_items):
        masked, was_masked = _mask_output_value(name, _safe_cell(value))
        top_values.append({"value": masked, "count": count})
        if index == 0:
            top_value_masked = was_masked
    row_count = len(values)
    uniqueness, constant = _uniqueness(
        row_count,
        null_count,
        len(counter),
        top_items[0][1] if top_items else 0,
        scope,
    )
    return ColumnProfile(
        name=name,
        scope=SCOPE_SAMPLE if scope != SCOPE_COMPLETE else SCOPE_COMPLETE,
        null_count=null_count,
        non_null_count=row_count - null_count,
        distinct_count=len(counter),
        top_values=top_values,
        top_value=top_values[0]["value"] if top_values else None,
        top_value_masked=top_value_masked,
        top_frequency=top_items[0][1] if top_items else 0,
        uniqueness=uniqueness,
        constant=constant,
    )


# 方法作用：从执行结果中识别数据行与截断标志，兼容 data/rows 两种结构。
# Args: execution - 统一执行结果对象或字典。
# Returns: (字典行列表, 是否截断)。
def _execution_rows(execution: Any) -> tuple[list[dict], bool]:
    rows = getattr(execution, "data", None)
    if rows is None:
        rows = getattr(execution, "rows", None)
    if rows is None and isinstance(execution, dict):
        rows = execution.get("data", execution.get("rows"))
    if isinstance(execution, dict):
        truncated = bool(execution.get("truncated", False))
    else:
        truncated = bool(getattr(execution, "truncated", False))
    result: list[dict] = []
    for row in rows or []:
        if isinstance(row, dict):
            result.append(row)
        else:
            mapping = getattr(row, "_mapping", None)
            if mapping is not None:
                result.append(dict(mapping))
    return result, truncated


# 方法作用：按列名从结果行精确取值，大小写歧义时宁缺勿借。
# Args: row - 结果行；column - 请求的列名。
# Returns: 列值；无精确键且大小写不敏感匹配不唯一时返回 None。
def _row_value(row: dict, column: str) -> Any:
    if column in row:
        return row[column]
    # 仅当大小写不敏感匹配唯一时才回退：多个候选说明物理列大小写歧义，
    # 借用另一列的值会把未授权数据混入统计，必须返回缺失。
    matched = [
        value for key, value in row.items() if str(key).casefold() == column.casefold()
    ]
    if len(matched) == 1:
        return matched[0]
    return None


# 方法作用：按权威方言渲染一列或多列的有界投影查询。
# Args: table - 授权表名；columns - 已校验的授权列；row_filter_sql - 服务端行过滤片段；
#       sqlglot_dialect - 已归一化的 sqlglot 方言名；limit - 行数上限。
# Returns: 标准 SQL 文本。
def _build_projection_sql(
    table: str,
    columns: list[str],
    row_filter_sql: str,
    sqlglot_dialect: str,
    limit: int,
) -> str:
    import sqlglot
    from sqlglot import exp

    select = exp.select(*[exp.column(name) for name in columns]).from_(exp.table_(table))
    filter_text = str(row_filter_sql or "").strip()
    if filter_text:
        try:
            filter_expr = sqlglot.parse_one(filter_text, read=sqlglot_dialect or None)
        except Exception as exc:
            logger.warning("行过滤条件解析失败", reason="row_filter_parse_error")
            raise _PolicyError("行过滤条件无法解析") from exc
        if (
            filter_expr is None
            or isinstance(filter_expr, (exp.Query, exp.Command, exp.Subquery, exp.Where))
            or filter_expr.find(exp.Select) is not None
        ):
            logger.warning("行过滤条件被拒绝", reason="row_filter_not_boolean")
            raise _PolicyError("行过滤条件必须是单表布尔表达式")
        select = select.where(filter_expr)
    select = select.limit(max(1, int(limit)))
    return select.sql(dialect=sqlglot_dialect or None, identify=True)


# 方法作用：判断列是否在服务端列白名单内（None 表示两侧都未声明列级限制）。
# Args: allowed_columns - 生效的授权列集合；table - 表名；column - 列名。
# Returns: 允许输出时返回 True；空集合表示限制交集为空，一律拒绝。
def _column_allowed(allowed_columns: list[str] | None, table: str, column: str) -> bool:
    if allowed_columns is None:
        return True
    allowed = {
        str(item).strip() for item in allowed_columns if str(item).strip()
    }
    if not allowed:
        # 空交集与"没有列限制"是两种语义：没有任何可授权列时必须拒绝。
        return False
    # 精确比较授权标识符：大小写不同的物理列是不同列，不能归一后共享权限。
    return column in allowed or f"{table}.{column}" in allowed


# 方法作用：把请求列集合归一化成可采样的授权列。
# Args: allowed_columns - 生效授权列（None 表示未声明列级限制）；table - 表名；schema - 授权表结构；
#       requested - 请求列；max_columns - 每表列数上限。
# Returns: (已校验列, 错误码)；错误码非空时列集合无效。
def _select_columns(
    allowed_columns: list[str] | None,
    table: str,
    schema: TableSchema,
    requested: list[str],
    max_columns: int,
) -> tuple[list[str], str]:
    authorized = {str(column.name) for column in schema.columns if str(column.name)}
    selected: list[str] = []
    for item in requested:
        name = str(item or "").strip()
        if not name:
            continue
        if name not in authorized:
            return [], ERROR_UNKNOWN_COLUMN
        if not _column_allowed(allowed_columns, table, name):
            return [], ERROR_COLUMN_NOT_ALLOWED
        if name not in selected:
            selected.append(name)
        if len(selected) >= max_columns:
            break
    return selected, ""


# 方法作用：构造失败结果并保证错误文案不含 SQL、异常或连接信息。
# Args: code - 脱敏错误码；message - 面向模型的简短中文说明；scope - 统计范围。
# Returns: ProbeResult。
def _probe_failure(code: str, message: str, scope: str = SCOPE_SAMPLE) -> ProbeResult:
    return ProbeResult(success=False, scope=scope, error=message, error_code=code)


# 方法作用：按总字符预算裁剪探查行集。
# Args: columns - 返回列名；rows - 候选行；truncated - 已发生的截断标志；scope - 统计范围。
# Returns: 受限后的 ProbeResult。
def _finalize_probe(
    columns: list[str],
    rows: list[dict[str, Any]],
    truncated: bool,
    scope: str,
) -> ProbeResult:
    kept: list[dict[str, Any]] = []
    for row in rows:
        candidate = [*kept, row]
        payload_chars = len(
            json.dumps(
                {"columns": columns, "rows": candidate},
                ensure_ascii=False,
                default=str,
            )
        )
        if payload_chars > PROBE_RESULT_MAX_CHARS:
            truncated = True
            break
        kept.append(row)
    return ProbeResult(
        success=True,
        columns=list(columns),
        rows=kept,
        row_count=len(kept),
        truncated=bool(truncated),
        scope=SCOPE_SAMPLE if scope != SCOPE_COMPLETE else SCOPE_COMPLETE,
    )


# 方法作用：渲染表级统计范围文案，禁止把样本表述为全量。
# Args: table - 表画像。
# Returns: 表头范围说明。
def _table_scope_text(table: TableProfile) -> str:
    if table.scope == SCOPE_COMPLETE:
        estimate = (
            f"；估算 {table.row_count_estimate} 行" if table.row_count_estimate > 0 else ""
        )
        return f"完整可见 {table.sampled_rows} 行{estimate}"
    return f"样本 {table.sampled_rows} 行，非全量（不保证代表总体）"


# 方法作用：渲染单列结论文案，按表级范围选择完整或样本措辞。
# Args: column - 列画像；table_scope - 所属表的统计范围。
# Returns: 列结论文案；示例值被脱敏时明确标注不能作为精确过滤原值。
def _column_scope_text(column: ColumnProfile, table_scope: str) -> str:
    # 脱敏示例只是展示形态，必须标注，防止模型把它当作可精确过滤的原值。
    masked_note = "（已脱敏展示，不可作为精确过滤原值）" if column.top_value_masked else ""
    if table_scope != SCOPE_COMPLETE:
        if column.uniqueness == UNIQUENESS_DUPLICATE:
            return (
                f"样本内 {column.distinct_count} 个值，最高重复 ×{column.top_frequency}"
                "——样本内已有重复，单列不唯一"
            )
        return (
            f"样本内 {column.distinct_count} 个值未见重复——样本非全量，唯一性未知，"
            "不得当作全表唯一键"
        )
    if column.constant:
        return (
            f"完全重复的常量列（{column.top_frequency} 行同值 {column.top_value}{masked_note}）"
            "——不能作为区分键"
        )
    if column.uniqueness == UNIQUENESS_UNIQUE:
        return f"完整数据 {column.distinct_count} 个值，无重复，唯一"
    if column.uniqueness == UNIQUENESS_DUPLICATE:
        return (
            f"完整数据 {column.distinct_count} 个值，最高重复 ×{column.top_frequency}"
            f"（如 {column.top_value}{masked_note}）——单列不唯一，需与区分键组合"
        )
    rows = column.null_count + column.non_null_count
    return (
        f"完整数据 {rows} 行中 {column.null_count} 行为 NULL，"
        f"非空 {column.distinct_count} 个值未见重复——含 NULL 不能作为唯一键"
    )


# 方法作用：把画像快照渲染成紧凑证据文本。
# Args: snapshot - 画像快照。
# Returns: 含 complete/sample 语义的文本；无有效表时返回空串。
def render_profile(snapshot: ProfileSnapshot) -> str:
    """样本结论一律标注样本语义，禁止描述为全表事实。"""
    if snapshot is None or not snapshot.tables:
        return ""
    lines = ["## 数据画像（生成 SQL 前必读）"]
    for table in snapshot.tables:
        if table.error:
            lines.append(f"{table.name}（未采集：{table.error}）")
            continue
        lines.append(f"{table.name}（{_table_scope_text(table)}）")
        for column in table.columns:
            lines.append(f"  - {column.name}: {_column_scope_text(column, table.scope)}")
    text = "\n".join(lines)
    if len(text) <= PROFILE_TEXT_MAX_CHARS:
        return text
    # 超长时保留头部：scope、键列与重复度结论优先进入预算。
    return text[:PROFILE_TEXT_MAX_CHARS].rstrip() + "\n…（画像已按字符预算截断）"


# 方法作用：计算任意文本的短哈希。
# Args: text - 待哈希文本。
# Returns: sha1 十六进制摘要。
def _hash_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# 方法作用：计算表结构的稳定指纹，用于缓存键隔离。
# Args: context - 画像上下文。
# Returns: 结构指纹哈希。
def _structure_fingerprint(context: ProfilingContext) -> str:
    payload = {
        table: [[column.name, column.type] for column in schema.columns]
        for table, schema in sorted(context.tables.items())
    }
    return _hash_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


# 方法作用：判断上下文是否具备可信身份（真实认证用户）。
# Args: context - 画像上下文。
# Returns: user_id 为正整数时返回 True。
def _trusted_context(context: ProfilingContext) -> bool:
    try:
        return int(context.user_id) > 0
    except (TypeError, ValueError):
        return False


# 方法作用：读取执行期的真实当前身份，作为服务入口的身份核对基准。
# Args: 无。
# Returns: 当前请求 RequestIdentity；读取失败时返回 None（调用方必须失败关闭）。
def _current_request_identity() -> Any:
    from src.api.auth import get_current_identity

    return get_current_identity()


# 方法作用：把快照授权列与执行前最新授权列取交集，并区分"无限制"与"空交集"。
# Args: snapshot - 上下文捕获的授权列；live - 执行前重新解析的授权列。
# Returns: None 表示两侧都未声明列级限制；列表为生效交集（可能为空，空交集必须拒绝）。
def _effective_allowed_columns(snapshot: list[str], live: list[str]) -> list[str] | None:
    snapshot_columns = [str(item).strip() for item in (snapshot or []) if str(item).strip()]
    live_columns = [str(item).strip() for item in (live or []) if str(item).strip()]
    if not snapshot_columns and not live_columns:
        # 只有两侧都未声明列级限制才是真正的不限列；空列表不能表达该语义。
        return None
    if not snapshot_columns:
        return list(dict.fromkeys(live_columns))
    if not live_columns:
        return list(dict.fromkeys(snapshot_columns))
    live_lookup = set(live_columns)
    # 精确字符串交集：大小写变体是不同列，交集只保留两侧同时授权的原始拼写。
    merged = [column for column in snapshot_columns if column in live_lookup]
    return list(dict.fromkeys(merged))


# 方法作用：合并快照行过滤与最新行过滤，两侧非空时取 AND 而不是宽侧覆盖。
# Args: snapshot - 上下文捕获的行过滤片段；live - 执行前重新解析的行过滤片段。
# Returns: 生效行过滤片段；两侧都为空时返回空串。
def _effective_row_filter(snapshot: str, live: str) -> str:
    left = str(snapshot or "").strip()
    right = str(live or "").strip()
    if not left:
        return right
    if not right or right == left:
        return left
    return f"({left}) AND ({right})"


# 方法作用：比较两个画像上下文是否指向同一身份、数据源与策略。
# Args: left - 已绑定上下文；right - 调用传入上下文。
# Returns: 一致时返回 True。
def _same_context(left: ProfilingContext, right: ProfilingContext) -> bool:
    return (
        left.user_id == right.user_id
        and left.tenant_id == right.tenant_id
        and left.datasource == right.datasource
        and left.schema_fingerprint == right.schema_fingerprint
        and left.row_filter_sql == right.row_filter_sql
        and list(left.allowed_columns) == list(right.allowed_columns)
    )


# 方法作用：判断列的样本是否可给出时间范围，并返回边界与格式标签。
# Args: column_type - 授权结构中的类型声明；values - 非空样本值。
# Returns: (min, max, value_format)；类型不兼容时返回 None。
def _temporal_bounds(column_type: str, values: list[Any]) -> tuple[Any, Any, str] | None:
    if not values:
        return None
    base_type = str(column_type or "").strip().lower().split("(", 1)[0].split(" ", 1)[0]
    if any(marker in base_type for marker in _TEMPORAL_TYPE_MARKERS):
        parsed: list[datetime] = []
        for value in values:
            if isinstance(value, datetime):
                parsed.append(value)
                continue
            if isinstance(value, date):
                parsed.append(datetime(value.year, value.month, value.day))
                continue
            if isinstance(value, str):
                try:
                    parsed.append(datetime.fromisoformat(value))
                    continue
                except ValueError:
                    return None
            return None
        lowest, highest = min(parsed), max(parsed)
        return _safe_cell(lowest), _safe_cell(highest), "iso"
    if base_type.startswith(_TEXT_TYPE_PREFIXES):
        # 文本日期只在统一格式可识别时给范围：同格式下词典序即时间序。
        from src.datasource.introspection import detect_value_format

        label = detect_value_format(values)
        if not label:
            return None
        texts = [
            str(value).strip() for value in values if value is not None and str(value).strip()
        ]
        return min(texts), max(texts), label
    return None


@dataclass(slots=True)
class _Target:
    """一次授权复核后的执行目标：连接通道与快照/最新授权的交集策略。"""

    dialect: str
    resolved: Any
    # 生效授权列：None 表示两侧都未声明列级限制；列表为精确交集（空交集已被拒绝）。
    allowed_columns: list[str] | None
    # 生效行过滤：两侧非空时 AND 合并，绝不丢弃窄侧策略。
    row_filter_sql: str


@dataclass(slots=True)
class _CacheEntry:
    """画像缓存条目：过期时间与快照。"""

    expires_at: float
    snapshot: ProfileSnapshot


class DataProfiler:
    """列级画像与结构化探查的有界采集器；进程内单例由 get_data_profiler 提供。"""

    # 方法作用：初始化画像缓存与同键并发锁。
    # Args: self - 实例。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self._cache: dict[tuple[Any, ...], _CacheEntry] = {}
        self._locks: dict[tuple[Any, ...], asyncio.Lock] = {}

    # 方法作用：复核真实身份、最新数据源授权与可控执行通道，任何失败一律关闭。
    # Args: self - 实例；context - 画像上下文。
    # Returns: (执行目标, 错误码)；错误码非空时不得继续取样。
    async def _prepare_target(self, context: ProfilingContext) -> tuple[_Target | None, str]:
        # 身份核对基准是执行期真实身份，不接受上下文伪造用户、租户或角色提权。
        try:
            current = _current_request_identity()
        except Exception:
            # 身份通道不可用时失败关闭；异常原文在本边界只记录一次。
            logger.error(
                "数据画像身份读取失败",
                datasource=context.datasource,
                exc_info=True,
            )
            return None, ERROR_FORBIDDEN
        current_tenant = int(getattr(current, "tenant_id", 0) or 0)
        current_user = int(getattr(current, "user_id", 0) or 0)
        current_role = normalize_role(getattr(current, "role", None))
        if (
            not _trusted_context(context)
            or current_user <= 0
            or current_role == ANONYMOUS_ROLE
            or current_user != int(context.user_id)
            or current_tenant != int(context.tenant_id)
        ):
            logger.warning(
                "数据画像身份拒绝",
                reason="identity_mismatch",
                datasource=context.datasource,
            )
            return None, ERROR_FORBIDDEN
        try:
            from src.datasource.registry import get_registry
            from src.security.permission_check import resolve_datasource_access

            registry = get_registry()
            summaries = await registry.list_all(tenant_id=current_tenant)
            # 授权解析使用实际当前角色：上下文角色只做核对，不能用来提权。
            access = await resolve_datasource_access(
                summaries,
                [context.datasource],
                tenant_id=current_tenant,
                user_id=current_user,
                role=current_role,
                multi_tenant=bool(getattr(get_settings(), "multi_tenant", False)),
            )
            if context.datasource not in access:
                logger.warning(
                    "数据画像数据源拒绝",
                    reason="not_in_access_scope",
                    datasource=context.datasource,
                )
                return None, ERROR_POLICY
            granted = access[context.datasource]
            if not isinstance(granted, dict):
                logger.warning(
                    "数据画像授权结构异常",
                    reason="invalid_access_payload",
                    datasource=context.datasource,
                )
                return None, ERROR_POLICY
            resolved = await registry.resolve(context.datasource, tenant_id=current_tenant)
        except Exception as exc:
            # 授权或解析失败必须失败关闭；异常原文只在这一处边界记录。
            logger.error(
                "数据画像授权解析失败",
                datasource=context.datasource,
                error_type=type(exc).__name__,
                exc_info=True,
            )
            return None, ERROR_POLICY
        dialect = str(getattr(resolved, "dialect", "") or context.dialect).strip().lower()
        if dialect not in _BOUNDED_DIALECTS:
            logger.warning("数据画像方言不支持", dialect=dialect, reason="not_bounded")
            return None, ERROR_UNSUPPORTED
        engine = getattr(resolved, "engine", None)
        if engine is None:
            connector = getattr(resolved, "connector", None)
            engine = getattr(connector, "engine", None) or getattr(connector, "_engine", None)
        if engine is not None and getattr(getattr(engine, "dialect", None), "is_async", None) is False:
            # 同步驱动无法真正取消服务端 SQL，首期明确降级而不是伪称已停止。
            logger.warning("数据画像同步驱动降级", datasource=context.datasource, dialect=dialect)
            return None, ERROR_SYNC_DRIVER
        # 快照权限与最新权限取交集：执行期收紧的列/行策略必须立即生效，
        # 缓存键也随该交集变化，命中缓存不能跳过本复核。
        allowed_columns = _effective_allowed_columns(
            context.allowed_columns, list(granted.get("allowed_columns") or []),
        )
        if allowed_columns is not None and not allowed_columns:
            # 两侧都声明了列限制但交集为空：必须整体拒绝执行与缓存读取，
            # 绝不能把空交集解释成"没有列限制"。
            logger.warning(
                "数据画像列权限交集为空",
                reason="empty_column_intersection",
                datasource=context.datasource,
            )
            return None, ERROR_COLUMN_NOT_ALLOWED
        row_filter_sql = _effective_row_filter(
            context.row_filter_sql, str(granted.get("row_filter_sql") or ""),
        )
        return _Target(
            dialect=dialect,
            resolved=resolved,
            allowed_columns=allowed_columns,
            row_filter_sql=row_filter_sql,
        ), ""

    # 方法作用：执行一次有界只读查询并解释结果。
    # Args: self - 实例；context - 画像上下文；sql - 已构造 SQL；max_rows - 行数上限；
    #       timeout_seconds - 本次超时秒数。
    # Returns: (字典行列表, 是否截断, 错误码)。
    async def _run_bounded_query(
        self,
        context: ProfilingContext,
        sql: str,
        max_rows: int,
        timeout_seconds: float,
    ) -> tuple[list[dict], bool, str]:
        try:
            async with asyncio.timeout(max(0.001, float(timeout_seconds))):
                execution = await validate_and_execute_sql(
                    sql,
                    context.datasource,
                    context.dialect,
                    explain=False,
                    max_rows=max(1, int(max_rows)),
                    tenant_id=context.tenant_id,
                )
        except (TimeoutError, asyncio.TimeoutError):
            return [], False, ERROR_TIMEOUT
        except asyncio.CancelledError:
            # 取消不是降级路径：必须原样传播，且不写缓存。
            raise
        except Exception as exc:
            # 统一执行器已处理并返回 success=False 的分支在下方单独降级，不再重复堆栈；
            # 能漏到这里的才是执行器未接住的异常，必须在本边界记录一次排障堆栈。
            # 异常正文可能含 SQL/数据/凭证，用固定中文文案承载原始 traceback。
            safe_error = RuntimeError("数据画像查询边界捕获未处理异常，当前表按执行失败降级")
            logger.warning(
                "数据画像查询异常",
                datasource=context.datasource,
                error_type=type(exc).__name__,
                exc_info=(type(safe_error), safe_error, exc.__traceback__),
            )
            return [], False, ERROR_EXECUTION
        if not getattr(execution, "success", False):
            logger.warning(
                "数据画像查询被拒绝",
                datasource=context.datasource,
                error_type=str(getattr(execution, "error_type", "") or "unknown"),
            )
            return [], False, ERROR_EXECUTION
        rows, truncated = _execution_rows(execution)
        return rows, truncated, ""

    # 方法作用：为一次画像请求生成缓存键，权限部分绑定生效交集策略。
    # Args: self - 实例；context - 画像上下文；columns_by_table - 归一化后的请求列；
    #       settings_key - 采样配置指纹；allowed_columns - 生效授权列（None 表示不限列）；
    #       row_filter_sql - 生效行过滤。
    # Returns: 可哈希缓存键。
    def _cache_key(
        self,
        context: ProfilingContext,
        columns_by_table: dict[str, list[str]],
        settings_key: tuple[Any, ...],
        allowed_columns: list[str] | None,
        row_filter_sql: str,
    ) -> tuple[Any, ...]:
        structure = context.schema_fingerprint or _structure_fingerprint(context)
        # 权限指纹保留授权标识符原始大小写：大小写变体是不同列，缓存不能合并。
        # None（不限列）与列表（含空交集）必须映射成不同指纹，避免互相复用。
        permission = _hash_text(json.dumps({
            "allowed": (
                None if allowed_columns is None
                else sorted(str(item).strip() for item in allowed_columns if str(item).strip())
            ),
            "row_filter": str(row_filter_sql or ""),
        }, ensure_ascii=False, sort_keys=True))
        columns_part = tuple(
            (table, tuple(columns)) for table, columns in sorted(columns_by_table.items())
        )
        return (
            context.tenant_id,
            context.user_id,
            context.datasource,
            structure,
            permission,
            columns_part,
            settings_key,
        )

    # 方法作用：读取未过期的缓存画像并返回副本。
    # Args: self - 实例；key - 缓存键。
    # Returns: 快照副本；未命中或已过期时返回 None。
    def _cache_get(self, key: tuple[Any, ...]) -> ProfileSnapshot | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.expires_at <= _now():
            self._cache.pop(key, None)
            return None
        return entry.snapshot.model_copy(deep=True)

    # 方法作用：写入缓存并在容量超限时淘汰最旧条目。
    # Args: self - 实例；key - 缓存键；snapshot - 画像快照；ttl_seconds - 有效期；
    #       capacity - 容量上限。
    # Returns: 无返回值。
    def _cache_put(
        self,
        key: tuple[Any, ...],
        snapshot: ProfileSnapshot,
        ttl_seconds: float,
        capacity: int,
    ) -> None:
        self._cache[key] = _CacheEntry(expires_at=_now() + max(0.0, ttl_seconds), snapshot=snapshot)
        while len(self._cache) > max(1, capacity):
            oldest = min(self._cache.items(), key=lambda item: item[1].expires_at)[0]
            self._cache.pop(oldest, None)

    # 方法作用：为缓存键取得并发填充锁，必要时清理空闲锁控制字典规模。
    # Args: self - 实例；key - 缓存键。
    # Returns: 该键专属的 asyncio.Lock。
    def _lock_for(self, key: tuple[Any, ...]) -> asyncio.Lock:
        if len(self._locks) >= _LOCK_ENTRY_LIMIT:
            for existing in [item for item, lock in self._locks.items() if not lock.locked()]:
                self._locks.pop(existing, None)
        return self._locks.setdefault(key, asyncio.Lock())

    # 方法作用：对请求列集合做归一化并按表分组。
    # Args: self - 实例；columns_by_table - 原始请求列。
    # Returns: 表名到请求列列表的映射。
    @staticmethod
    def _normalize_request(columns_by_table: dict[str, list[str]]) -> dict[str, list[str]]:
        normalized: dict[str, list[str]] = {}
        for table, columns in (columns_by_table or {}).items():
            name = str(table or "").strip()
            if not name:
                continue
            normalized[name] = [str(column or "").strip() for column in (columns or [])]
        return normalized

    # 方法作用：按表执行有界列投影采集并汇总画像快照。
    # Args: self - 实例；context - 画像上下文；target - 复核后的执行目标（含生效策略）；
    #       selected - 归一化请求列；settings - 配置快照。
    # Returns: ProfileSnapshot。
    async def _collect_profile(
        self,
        context: ProfilingContext,
        target: _Target,
        selected: dict[str, list[str]],
        settings: dict[str, Any],
    ) -> ProfileSnapshot:
        sample_rows = settings["sample_rows"]
        deadline = _now() + settings["total_seconds"]
        remaining_queries = settings["max_queries"]
        tables: list[TableProfile] = []
        errors: list[str] = []
        query_count = 0
        sqlglot_dialect = normalize_sql_dialect(target.dialect)
        for table, requested in selected.items():
            if len(tables) >= settings["max_tables"]:
                errors.append(ERROR_TABLE_LIMIT)
                break
            schema = context.tables.get(table)
            if schema is None:
                tables.append(TableProfile(name=table, error=ERROR_UNKNOWN_TABLE))
                errors.append(ERROR_UNKNOWN_TABLE)
                continue
            columns, code = _select_columns(
                target.allowed_columns, table, schema, requested, settings["max_columns"]
            )
            if code:
                tables.append(TableProfile(name=table, error=code))
                errors.append(code)
                continue
            if not columns:
                tables.append(TableProfile(name=table, error=ERROR_UNKNOWN_COLUMN))
                errors.append(ERROR_UNKNOWN_COLUMN)
                continue
            if remaining_queries <= 0:
                errors.append(ERROR_QUERY_BUDGET)
                break
            remaining = deadline - _now()
            if remaining <= 0:
                errors.append(ERROR_TOTAL_TIME)
                break
            try:
                sql = _build_projection_sql(
                    table,
                    columns,
                    target.row_filter_sql,
                    sqlglot_dialect,
                    sample_rows + 1,
                )
            except _PolicyError:
                tables.append(TableProfile(name=table, error=ERROR_POLICY))
                errors.append(ERROR_POLICY)
                continue
            remaining_queries -= 1
            query_count += 1
            rows, truncated, code = await self._run_bounded_query(
                context,
                sql,
                sample_rows + 1,
                min(settings["timeout_seconds"], max(0.001, deadline - _now())),
            )
            if code:
                tables.append(TableProfile(name=table, error=code))
                errors.append(code)
                continue
            sampled = rows[:sample_rows]
            # 服务端截断同样只能算样本：不得因截断而标 complete。
            scope = SCOPE_SAMPLE if (truncated or len(rows) > sample_rows) else SCOPE_COMPLETE
            table_profile = TableProfile(
                name=table,
                scope=scope,
                sampled_rows=len(sampled),
                row_count_estimate=max(0, int(getattr(schema, "row_count_estimate", 0) or 0)),
                columns=[
                    _summarize_column(column, [_row_value(row, column) for row in sampled], scope)
                    for column in columns
                ],
            )
            tables.append(table_profile)
        return ProfileSnapshot(tables=tables, query_count=query_count, errors=errors)

    # 方法作用：采集一次有界画像，命中缓存时零新增查询。
    # Args: self - 实例；context - 服务端画像上下文；columns_by_table - 每表请求列。
    # Returns: ProfileSnapshot。
    async def profile(
        self,
        context: ProfilingContext,
        columns_by_table: dict[str, list[str]],
    ) -> ProfileSnapshot:
        """按表执行一次有界列投影采样；样本结论不提升为全表事实。"""
        logger.debug(
            "数据画像采集入口",
            datasource=context.datasource,
            tenant_id=context.tenant_id,
            table_count=len(columns_by_table or {}),
        )
        settings = _profiling_settings()
        if not settings["enabled"]:
            logger.info("数据画像已禁用", datasource=context.datasource)
            return ProfileSnapshot(tables=[], query_count=0, errors=[ERROR_DISABLED])
        selected = self._normalize_request(columns_by_table)
        # 复核在最前：身份、最新权限与生效交集策略全部确认后才允许读缓存或查询。
        target, code = await self._prepare_target(context)
        if target is None:
            return ProfileSnapshot(tables=[], query_count=0, errors=[code])
        settings_key = (settings["sample_rows"], settings["max_columns"])
        key = self._cache_key(
            context, selected, settings_key, target.allowed_columns, target.row_filter_sql,
        )
        cached = self._cache_get(key)
        if cached is not None:
            cached.query_count = 0
            logger.debug("数据画像缓存命中", datasource=context.datasource)
            return cached
        lock = self._lock_for(key)
        async with lock:
            # 双重检查：并发填充由同键锁合并，等待方直接复用已完成结果。
            cached = self._cache_get(key)
            if cached is not None:
                cached.query_count = 0
                return cached
            snapshot = await self._collect_profile(context, target, selected, settings)
            if not snapshot.errors:
                self._cache_put(key, snapshot, settings["ttl_seconds"], settings["cache_entries"])
            logger.info(
                "数据画像采集完成",
                datasource=context.datasource,
                table_count=len(snapshot.tables),
                query_count=snapshot.query_count,
                error_count=len(snapshot.errors),
            )
            # 缓存已持有原对象，返回副本避免调用方修改污染后续命中。
            return snapshot.model_copy(deep=True)

    # 方法作用：解析五原语的投影列与行数上限，列权限用生效交集策略判定。
    # Args: self - 实例；context - 画像上下文；target - 复核后的执行目标；request - 已校验探查请求。
    # Returns: (表结构, 投影列, 采样行数, 错误码)。
    def _resolve_probe_columns(
        self,
        context: ProfilingContext,
        target: _Target,
        request: ProbeRequest,
    ) -> tuple[TableSchema | None, list[str], int, str]:
        schema = context.tables.get(request.table)
        if schema is None:
            return None, [], 0, ERROR_UNKNOWN_TABLE
        authorized = {str(column.name) for column in schema.columns if str(column.name)}
        if request.operation == "table_sample":
            columns: list[str] = []
            for name in request.columns:
                if name not in authorized:
                    return None, [], 0, ERROR_UNKNOWN_COLUMN
                if not _column_allowed(target.allowed_columns, request.table, name):
                    return None, [], 0, ERROR_COLUMN_NOT_ALLOWED
                if name not in columns:
                    columns.append(name)
            ceiling = _probe_limit()
            limit = min(request.limit or ceiling, ceiling)
            return schema, columns, max(1, limit), ""
        column = str(request.column or "").strip()
        if column not in authorized:
            return None, [], 0, ERROR_UNKNOWN_COLUMN
        if not _column_allowed(target.allowed_columns, request.table, column):
            return None, [], 0, ERROR_COLUMN_NOT_ALLOWED
        return schema, [column], _profiling_settings()["sample_rows"], ""

    # 方法作用：把单列样本统计转成探查行，输出值按原列名脱敏。
    # Args: operation - 原语；column - 原列名（脱敏字段规则依据）；values - 非空原始值序列；
    #       row_count - 可见行数；scope - 统计范围；limit - 返回行数上限；
    #       pattern - entity_lookup 的查找文本；column_type - 授权列类型。
    # Returns: 探查结果行列表；匹配与统计基于原值，输出值在统计完成后脱敏。
    @staticmethod
    def _probe_rows(
        operation: str,
        column: str,
        values: list[Any],
        row_count: int,
        scope: str,
        limit: int,
        pattern: str,
        column_type: str,
    ) -> list[dict[str, Any]]:
        if operation == "entity_lookup":
            counter: Counter = Counter()
            for value in values:
                text = _safe_cell(value)
                if not isinstance(text, str) or pattern not in text:
                    continue
                # 字面包含：% 与 _ 只作普通字符，不做通配。
                counter[_stat_key(value)] += 1
            matched = [
                (str(_safe_cell(key)), count, str(_safe_cell(key)) == pattern)
                for key, count in counter.items()
            ]
            matched.sort(key=lambda item: (not item[2], -item[1], item[0]))
            rows: list[dict[str, Any]] = []
            for text, count, is_exact in matched[: max(1, limit)]:
                masked, was_masked = _mask_output_value(column, text)
                # 脱敏后的值不是数据原值：不能再声称可直接精确匹配。
                rows.append({
                    "value": masked,
                    "count": count,
                    "exact": bool(is_exact and not was_masked),
                })
            return rows
        if operation == "value_domain":
            counter = Counter(_stat_key(value) for value in values)
            return [
                {"value": _mask_output_value(column, _safe_cell(value))[0], "count": count}
                for value, count in counter.most_common(max(1, limit))
            ]
        if operation == "column_repeat":
            counter = Counter(_stat_key(value) for value in values)
            top_items = counter.most_common(1)
            uniqueness, constant = _uniqueness(
                row_count,
                row_count - len(values),
                len(counter),
                top_items[0][1] if top_items else 0,
                scope,
            )
            return [{
                "uniqueness": uniqueness,
                "constant": constant,
                "row_count": row_count,
                "non_null_count": len(values),
                "distinct_count": len(counter),
                "top_value": (
                    _mask_output_value(column, _safe_cell(top_items[0][0]))[0]
                    if top_items else None
                ),
                "top_frequency": top_items[0][1] if top_items else 0,
            }]
        # time_range：只对类型兼容的样本给范围，未知文本格式不声称时间范围。
        parsed = _temporal_bounds(column_type, values)
        if parsed is None:
            return [{
                "min": None,
                "max": None,
                "value_format": "",
                "compatible": False,
                "reason": "incompatible_type",
            }]
        lowest, highest, value_format = parsed
        return [{
            "min": _mask_output_value(column, lowest)[0],
            "max": _mask_output_value(column, highest)[0],
            "value_format": value_format,
            "compatible": True,
        }]

    # 方法作用：在绑定作用域内执行一次探查原语。
    # Args: self - 实例；context - 画像上下文；request - 已校验请求；budget - 共享预算。
    # Returns: ProbeResult。
    async def _probe_in_scope(
        self,
        context: ProfilingContext,
        request: ProbeRequest,
        budget: ProbeBudget,
    ) -> ProbeResult:
        # 先复核身份与最新权限，列解析和 SQL 构建都使用生效交集策略。
        target, code = await self._prepare_target(context)
        if target is None:
            return _probe_failure(code, "本次请求无权在该数据源上执行探查")
        schema, columns, limit_rows, code = self._resolve_probe_columns(context, target, request)
        if schema is None or code:
            return _probe_failure(code, "请求的表或列不在本次授权范围内")
        if not budget.reserve():
            logger.info("探查预算耗尽", datasource=context.datasource, calls=budget.calls)
            return _probe_failure(ERROR_BUDGET_EXHAUSTED, "本次请求的探查调用次数已用尽")
        try:
            sql = _build_projection_sql(
                request.table,
                columns,
                target.row_filter_sql,
                normalize_sql_dialect(target.dialect),
                limit_rows + 1,
            )
        except _PolicyError:
            return _probe_failure(ERROR_POLICY, "行列策略无法安全应用，探查已拒绝")
        rows, truncated, code = await self._run_bounded_query(
            context, sql, limit_rows + 1, _probe_timeout_seconds()
        )
        if code:
            return _probe_failure(code, "探查查询未完成")
        if request.operation == "table_sample":
            sampled = rows[:limit_rows]
            # 样例行对外前按原列名脱敏：不得让画像/探查成为结果脱敏旁路。
            payload_rows = [
                {
                    column: _mask_output_value(column, _safe_cell(_row_value(row, column)))[0]
                    for column in columns
                }
                for row in sampled
            ]
            return _finalize_probe(
                columns,
                payload_rows,
                truncated or len(rows) > limit_rows,
                SCOPE_SAMPLE,
            )
        # 单列慢路径：样本最多 sample_rows 行，触顶即为样本范围。
        sampled = rows[:limit_rows]
        scope = SCOPE_SAMPLE if (truncated or len(rows) > limit_rows) else SCOPE_COMPLETE
        values = [_row_value(row, columns[0]) for row in sampled]
        non_null = [value for value in values if value is not None]
        column_type = ""
        for definition in schema.columns:
            if str(definition.name) == columns[0]:
                column_type = str(getattr(definition, "type", "") or "")
                break
        ceiling = _probe_limit()
        payload_rows = self._probe_rows(
            request.operation,
            columns[0],
            non_null,
            len(sampled),
            scope,
            ceiling if request.limit <= 0 else min(request.limit, ceiling),
            request.pattern,
            column_type,
        )
        return _finalize_probe(columns, payload_rows, False, scope)

    # 方法作用：执行一次结构化探查；绑定作用域内共享预算，未绑定时使用一次性预算。
    # Args: self - 实例；context - 服务端画像上下文；request - 探查请求。
    # Returns: ProbeResult；预算耗尽返回明确错误码而不是抛异常。
    async def probe(self, context: ProfilingContext, request: ProbeRequest) -> ProbeResult:
        """每次真实探查都受次数与超时预算约束，取消原样传播。"""
        logger.debug(
            "数据探查入口",
            datasource=context.datasource,
            operation=request.operation,
            table=request.table,
        )
        scope = current_probe_scope()
        if scope is not None and _same_context(scope.context, context):
            return await self._probe_in_scope(context, request, scope.budget)
        # 未绑定（直接调用）时使用一次性作用域，语义与工具路径保持一致。
        with bind_probe_scope(context) as fresh:
            return await self._probe_in_scope(context, request, fresh.budget)


# 进程级单例：共享画像缓存与同键并发锁。
_data_profiler: DataProfiler | None = None


# 方法作用：返回进程内共享的画像服务实例。
# Args: 无。
# Returns: DataProfiler 单例。
def get_data_profiler() -> DataProfiler:
    global _data_profiler
    if _data_profiler is None:
        _data_profiler = DataProfiler()
    return _data_profiler
