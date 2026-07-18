"""跨资产 Join 的匹配率、基数和膨胀风险契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.logging_config import get_logger

logger = get_logger(__name__)


class JoinContractError(ValueError):
    """Join 键或输入数据不满足契约时抛出的异常。"""


@dataclass
class JoinContract:
    """记录一次跨资产 Join 的证据和人工确认状态。"""

    left_asset_id: str
    right_asset_id: str
    left_key: str
    right_key: str
    left_rows: int
    right_rows: int
    matched_left_rows: int
    matched_rows: int
    match_rate: float
    unmatched_left_rows: int
    unmatched_right_rows: int
    cardinality: str
    expansion_factor: float
    requires_confirmation: bool
    confirmation_reason: str = ""

    # 方法作用：把 JoinContract 转为 API/审计可序列化的结构。
    # Args: self - JoinContract 对象。
    # Returns: Join 契约字典。
    def to_dict(self) -> dict[str, Any]:
        logger.debug("JoinContract 序列化入口", left=self.left_asset_id, right=self.right_asset_id)
        result = self.__dict__.copy()
        logger.info("JoinContract 序列化完成", requires_confirmation=self.requires_confirmation)
        return result


# 方法作用：分析两组行的键匹配率、基数和结果集膨胀风险。
# Args: left_asset_id - 左资产 ID；right_asset_id - 右资产 ID；left_rows/right_rows - 两侧行；left_key/right_key - Join 键。
# Returns: JoinContract；低匹配或多对多时 requires_confirmation=True。
def build_join_contract(left_asset_id: str, right_asset_id: str,
                        left_rows: list[dict[str, Any]], right_rows: list[dict[str, Any]],
                        left_key: str, right_key: str) -> JoinContract:
    logger.debug("构造 JoinContract 入口", left_rows=len(left_rows), right_rows=len(right_rows),
                 left_key=left_key, right_key=right_key)
    if not left_key or not right_key:
        raise JoinContractError("Join 键不能为空")
    if any(left_key not in row for row in left_rows) or any(right_key not in row for row in right_rows):
        raise JoinContractError("Join 键不存在")
    left_counts = _key_counts(left_rows, left_key)
    right_counts = _key_counts(right_rows, right_key)
    left_keys, right_keys = set(left_counts), set(right_counts)
    matched_keys = left_keys & right_keys
    matched_left_rows = sum(left_counts[key] for key in matched_keys)
    matched_rows = sum(left_counts[key] * right_counts[key] for key in matched_keys)
    match_rate = matched_left_rows / len(left_rows) if left_rows else 0.0
    unmatched_left = len(left_rows) - matched_left_rows
    unmatched_right = len(right_rows) - sum(right_counts[key] for key in matched_keys)
    many_left = any(count > 1 for key, count in left_counts.items() if key in matched_keys)
    many_right = any(count > 1 for key, count in right_counts.items() if key in matched_keys)
    cardinality = "many_to_many" if many_left and many_right else "many_to_one" if many_left else "one_to_many" if many_right else "one_to_one"
    expansion = matched_rows / matched_left_rows if matched_left_rows else 0.0
    reasons: list[str] = []
    if match_rate < 0.8:
        reasons.append("左侧匹配率低于 80%")
    if cardinality == "many_to_many":
        reasons.append("检测到多对多 Join，结果可能膨胀")
    if expansion > 10:
        reasons.append("结果集膨胀超过 10 倍")
    contract = JoinContract(
        left_asset_id=left_asset_id,
        right_asset_id=right_asset_id,
        left_key=left_key,
        right_key=right_key,
        left_rows=len(left_rows),
        right_rows=len(right_rows),
        matched_left_rows=matched_left_rows,
        matched_rows=matched_rows,
        match_rate=match_rate,
        unmatched_left_rows=unmatched_left,
        unmatched_right_rows=unmatched_right,
        cardinality=cardinality,
        expansion_factor=expansion,
        requires_confirmation=bool(reasons),
        confirmation_reason="；".join(reasons),
    )
    logger.info("构造 JoinContract 完成", cardinality=cardinality,
                match_rate=contract.match_rate, requires_confirmation=contract.requires_confirmation)
    return contract


# 方法作用：统计一侧数据中每个 Join 键出现次数。
# Args: rows - 行字典；key - Join 键字段。
# Returns: 键到出现次数的映射。
def _key_counts(rows: list[dict[str, Any]], key: str) -> dict[Any, int]:
    logger.debug("统计 Join 键入口", key=key, rows=len(rows))
    counts: dict[Any, int] = {}
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        try:
            counts[value] = counts.get(value, 0) + 1
        except TypeError as exc:
            raise JoinContractError(f"Join 键不可哈希: {key}") from exc
    logger.info("统计 Join 键完成", key=key, unique_keys=len(counts))
    return counts
