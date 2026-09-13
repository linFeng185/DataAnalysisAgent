"""报表额度已用量重置的请求与响应契约（需求 4 / D 批）。

本模块单独承载 D 批新增的接口契约，避免与并行开发中的
`src/reporting/models.py` 相互冲突；契约稳定后可整体并入 models.py。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, Field, model_validator

from src.logging_config import get_logger

logger = get_logger(__name__)

# 可按自然月清零已用量的四个累计字段，与前端 ReportQuotaResetField 一一对应。
# concurrent_runs 是实时并发槽、analysis_units/queries/rows 是单次上限，都不在可重置范围内。
RESETTABLE_QUOTA_FIELDS: tuple[str, ...] = ("jobs_created", "runs_started", "llm_tokens", "share_links")

# 一次请求允许携带的目标用户数上限；越界属客户端错误，直接 422，不拖垮数据库。
MAX_RESET_TARGETS = 1000

# 字段白名单必须与 RESETTABLE_QUOTA_FIELDS 一致，由单元测试断言防止两处脱钩。
QuotaResetField = Literal["jobs_created", "runs_started", "llm_tokens", "share_links"]

# 目标用户 ID 只接受正整数：0、负数与非整数在契约层即被拒绝为 422。
_PositiveUserId = Annotated[int, Field(gt=0)]


# 方法作用：把重置字段列表归一为白名单内的名称，未知字段直接拒绝。
# Args: fields - 请求携带的字段名序列。
# Returns: 去重后的字段名列表；出现白名单外字段或空列表时抛出 ValueError。
def normalize_reset_fields(fields: Iterable[str]) -> list[str]:
    """列名只能来自白名单，绝不把请求原文拼进 SQL，这是重置接口的注入防线。"""
    normalized: list[str] = []
    for item in fields:
        name = str(item or "").strip()
        if name not in RESETTABLE_QUOTA_FIELDS:
            raise ValueError(f"不支持重置的额度字段：{name or '(空)'}")
        if name not in normalized:
            normalized.append(name)
    if not normalized:
        raise ValueError("重置字段不能为空")
    logger.debug("报表额度重置字段归一完成", fields=normalized)
    return normalized


class QuotaUsageResetRequest(BaseModel):
    """按月额度已用量重置请求：范围二选一，字段多选且不能为空。"""

    # all_users 为真时忽略 user_ids；两者同时出现属于客户端错误。
    user_ids: list[_PositiveUserId] = Field(default_factory=list, max_length=MAX_RESET_TARGETS)
    all_users: bool = False
    fields: list[QuotaResetField] = Field(..., min_length=1, max_length=len(RESETTABLE_QUOTA_FIELDS))

    # 方法作用：校验重置范围二选一，并去掉重复的目标用户与字段。
    # Args: 无（读取模型自身字段）。
    # Returns: 归一后的模型自身；范围非法时抛出 ValueError，由 FastAPI 统一转 422。
    @model_validator(mode="after")
    def _normalize_scope(self) -> "QuotaUsageResetRequest":
        if self.all_users and self.user_ids:
            raise ValueError("user_ids 与 all_users 互斥，只能提供其一")
        if not self.all_users and not self.user_ids:
            raise ValueError("必须提供 user_ids 或 all_users=true")
        # 重复目标只算一次，避免同一用户在审计里出现多条记录。
        self.user_ids = list(dict.fromkeys(int(item) for item in self.user_ids))
        self.fields = list(dict.fromkeys(self.fields))
        return self


class QuotaUsageResetResponse(BaseModel):
    """重置结果：实际被清零的用户数、实际字段与当前周期边界。"""

    # reset_users 可能小于请求人数：本期无用量行或不属于本租户的目标不计入。
    reset_users: int = 0
    fields: list[str] = Field(default_factory=list)
    period_start: datetime
    period_end: datetime


# 方法作用：断言字段白名单与 Literal 类型同源，供测试守卫使用。
# Args: 无。
# Returns: Literal 允许的全部字段名集合。
def literal_reset_fields() -> set[str]:
    logger.debug("报表额度重置字段读取完成", field_count=len(get_args(QuotaResetField)))
    return {str(item) for item in get_args(QuotaResetField)}
