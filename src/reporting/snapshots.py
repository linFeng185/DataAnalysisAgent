"""报表单元执行快照：字段归一与截断原因生成。

本模块只做纯计算，不做任何 IO；持久化由 ReportStore.save_unit_snapshot 负责。
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from src.logging_config import get_logger

logger = get_logger(__name__)

# SQL 文本入库上限：迁移列是无界 TEXT，这里限幅保护单条记录体积。
SQL_TEXT_LIMIT = 100_000
# 报表页面单单元展示行数上限，与 execute_sql 的 query_result_sample[:200] 同源。
DISPLAY_ROW_LIMIT = 200
# 参数键命中这些片段即视为敏感：快照用于排查，不承担密钥存储职责。
_SENSITIVE_PARAM_TOKENS = ("api_key", "token", "password", "secret")
# 迁移中定长列的宽度上限，超长先裁剪再入库，避免写入被列宽拒绝。
_COLUMN_WIDTHS = {
    "unit_id": 64,
    "datasource": 64,
    "dialect": 32,
    "prompt_id": 64,
    "model_id": 128,
    "truncated_reason": 200,
    "error_code": 64,
}


# 方法作用：把文本裁剪到指定宽度，非字符串输入统一转为字符串。
# Args: value - 原始值；width - 允许的最大字符数。
# Returns: 裁剪后的字符串。
def _clip(value: Any, width: int) -> str:
    return str(value or "")[:width]


# 方法作用：剔除参数中的敏感键，只保留排查需要的普通参数。
# Args: params - 原始参数字典。
# Returns: 去掉敏感键后的新字典。
def _strip_sensitive_params(params: dict[str, Any] | None) -> dict[str, Any]:
    kept = {
        str(key): value
        for key, value in dict(params or {}).items()
        if not any(token in str(key).lower() for token in _SENSITIVE_PARAM_TOKENS)
    }
    dropped = len(params or {}) - len(kept)
    if dropped:
        logger.debug("单元快照参数已剔除敏感键", dropped_count=dropped)
    return kept


class UnitSnapshot(BaseModel):
    """一次单元执行的完整调试快照，字段与 report_unit_snapshots 表列一一对应。"""

    id: str = ""
    run_id: str = ""
    report_id: str = ""
    tenant_id: int = 0
    user_id: int = 0
    unit_id: str = ""
    status: Literal["success", "partial", "failed"] = "success"
    datasource: str = ""
    dialect: str = ""
    sql_text: str = ""
    prompt_text: str = ""
    prompt_id: str = ""
    model_id: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    row_count: int = 0
    full_count: int = 0
    truncated: bool = False
    truncated_reason: str = ""
    elapsed_ms: int = 0
    error_code: str = ""
    error_message: str = ""
    created_at: str = ""


# 方法作用：生成截断原因的可读文案，未截断时返回空串。
# Args: displayed - 实际展示行数；full_count - 全量行数；limit - 展示行数上限。
# Returns: 形如「查询返回 80000 行，页面展示 5000 行（上限 5000）」的说明文本。
def describe_truncation(displayed: int, full_count: int, limit: int) -> str:
    logger.debug("截断原因生成入口", displayed=displayed, full_count=full_count, limit=limit)
    full = int(full_count or 0)
    shown = int(displayed or 0)
    # 无全量信号或全量不超过展示数时都按未截断处理，不凭空制造截断说明。
    if full <= 0 or full <= shown:
        return ""
    if int(limit or 0) > 0:
        text = f"查询返回 {full} 行，页面展示 {shown} 行（上限 {int(limit)}）"
    else:
        text = f"查询返回 {full} 行，页面展示 {shown} 行"
    logger.info("截断原因生成完成", text=text)
    return text


# 方法作用：解析本次执行使用的模型标识，优先显式值并回退到请求级租户模型选择。
# Args: explicit - 调用方已知的显式模型标识。
# Returns: 模型标识；上下文未绑定租户模型选择时返回空串。
def resolve_model_id(explicit: str = "") -> str:
    logger.debug("单元快照模型标识解析入口", explicit=str(explicit or ""))
    if str(explicit or "").strip():
        return str(explicit).strip()
    try:
        from src.llm.tenant_config import get_current_tenant_llm_selection

        selection = get_current_tenant_llm_selection()
    except Exception:
        # 解析失败不影响快照主体，模型标识留空即可。
        logger.warning("单元快照模型标识解析失败", exc_info=True)
        return ""
    model_id = str(getattr(selection, "model_id", "") or "") if selection is not None else ""
    logger.info("单元快照模型标识解析完成", model_id=model_id)
    return model_id


# 方法作用：把执行现场归一成一条可入库的单元快照，纯计算不做 IO。
# Args: unit_id - 分析单元标识；run_id/report_id - 归属运行与报表；
#       tenant_id/user_id - 执行身份归属；status - 单元状态；datasource/dialect/sql - 查询现场；
#       prompt_text/prompt_id - 提示词现场；model_id - 模型标识；params - 非敏感参数；
#       row_count/full_count/truncated/truncated_reason - 行数与截断现场；
#       elapsed_ms - 执行耗时；error_code/error_message - 失败现场。
# Returns: UnitSnapshot 实例。
def build_snapshot(
    *,
    unit_id: str,
    run_id: str = "",
    report_id: str = "",
    tenant_id: int = 0,
    user_id: int = 0,
    status: str = "success",
    datasource: str = "",
    dialect: str = "",
    sql: str = "",
    prompt_text: str = "",
    prompt_id: str = "",
    model_id: str = "",
    params: dict[str, Any] | None = None,
    row_count: int = 0,
    full_count: int = 0,
    truncated: bool = False,
    truncated_reason: str = "",
    elapsed_ms: int = 0,
    error_code: str = "",
    error_message: str = "",
) -> UnitSnapshot:
    """truncated_reason 由调用方给出；本函数只做字段归一，不推断截断原因。"""
    logger.debug("单元快照组装入口", unit_id=str(unit_id or ""), run_id=str(run_id or ""), status=status)
    snapshot = UnitSnapshot(
        id=str(uuid4()),
        run_id=str(run_id or ""),
        report_id=str(report_id or ""),
        tenant_id=int(tenant_id or 0),
        user_id=int(user_id or 0),
        unit_id=_clip(unit_id, _COLUMN_WIDTHS["unit_id"]),
        status=status,
        datasource=_clip(datasource, _COLUMN_WIDTHS["datasource"]),
        dialect=_clip(dialect, _COLUMN_WIDTHS["dialect"]),
        # SQL 是排查核心证据，只做上限截断不做其它改写。
        sql_text=str(sql or "")[:SQL_TEXT_LIMIT],
        prompt_text=str(prompt_text or ""),
        prompt_id=_clip(prompt_id, _COLUMN_WIDTHS["prompt_id"]),
        model_id=_clip(model_id, _COLUMN_WIDTHS["model_id"]),
        params=_strip_sensitive_params(params),
        row_count=int(row_count or 0),
        full_count=int(full_count or 0),
        truncated=bool(truncated),
        truncated_reason=_clip(truncated_reason, _COLUMN_WIDTHS["truncated_reason"]),
        elapsed_ms=int(elapsed_ms or 0),
        error_code=_clip(error_code, _COLUMN_WIDTHS["error_code"]),
        error_message=str(error_message or ""),
    )
    logger.info(
        "单元快照组装完成",
        unit_id=snapshot.unit_id,
        status=snapshot.status,
        row_count=snapshot.row_count,
        truncated=snapshot.truncated,
        has_sql=bool(snapshot.sql_text),
    )
    return snapshot
