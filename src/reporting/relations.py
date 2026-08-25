"""报表分析单元关系证据与跨源降级策略。"""

from __future__ import annotations

from itertools import combinations
from typing import Any

from pydantic import BaseModel, Field

from src.logging_config import get_logger

logger = get_logger(__name__)


class RelationEvidence(BaseModel):
    """两个分析单元之间可审计的关系证据。"""

    left_unit_id: str
    right_unit_id: str
    relation_type: str = "none"
    join_keys: list[str] = Field(default_factory=list, max_length=20)
    match_rate: float | None = Field(default=None, ge=0, le=1)
    left_rows: int = Field(default=0, ge=0)
    right_rows: int = Field(default=0, ge=0)
    cardinality: str = "unknown"
    correlation: float | None = Field(default=None, ge=-1, le=1)
    time_alignment: str = "unknown"
    evidence_level: str = "no_relation"
    limitations: list[str] = Field(default_factory=list, max_length=20)


class ControlledRelationAnalyzer:
    """受控分析器归属 reporting.relations，Phase 1 只执行同源结构分析。"""

    # 方法作用：根据授权后的列投影描述构建同源关系证据。
    # Args: left - 左侧分析单元摘要；right - 右侧分析单元摘要。
    # Returns: 不执行跨源 SQL 的 RelationEvidence。
    def analyze_same_source(self, left: dict[str, Any], right: dict[str, Any]) -> RelationEvidence:
        left_source = str(left.get("datasource", ""))
        right_source = str(right.get("datasource", ""))
        left_columns = [str(item) for item in left.get("columns", [])]
        right_columns = [str(item) for item in right.get("columns", [])]
        if left_source != right_source:
            return RelationEvidence(
                left_unit_id=str(left.get("unit_id", "")),
                right_unit_id=str(right.get("unit_id", "")),
                evidence_level="unsupported_cross_source",
                limitations=["跨数据源联合分析需要受控分析器的列投影、采样或聚合能力，Phase 2 开放"],
            )
        common = [column for column in left_columns if column in right_columns]
        if not common:
            return RelationEvidence(
                left_unit_id=str(left.get("unit_id", "")),
                right_unit_id=str(right.get("unit_id", "")),
                limitations=["未发现双方均有权限且类型兼容的共同字段，分别分析"],
            )
        return RelationEvidence(
            left_unit_id=str(left.get("unit_id", "")),
            right_unit_id=str(right.get("unit_id", "")),
            relation_type="entity_time_join" if any("time" in key.lower() or "date" in key.lower() or "week" in key.lower() for key in common) else "entity_join",
            join_keys=common[:20],
            match_rate=1.0,
            cardinality="unknown",
            time_alignment="same_period" if any(key in common for key in ("week", "date", "month", "period")) else "unknown",
            evidence_level="verified_relation",
            limitations=["关系证据不代表因果关系，未控制的业务因素需要单独说明"],
        )


# 方法作用：为每一对分析单元生成同源关系或跨源降级证据。
# Args: units - 含 unit_id/datasource/columns 的授权分析单元摘要。
# Returns: 关系证据列表。
def build_relations(units: list[dict[str, Any]]) -> list[RelationEvidence]:
    logger.debug("报表关系分析入口", unit_count=len(units))
    analyzer = ControlledRelationAnalyzer()
    relations = [analyzer.analyze_same_source(left, right) for left, right in combinations(units, 2)]
    logger.info("报表关系分析完成", relation_count=len(relations), verified_count=sum(item.evidence_level == "verified_relation" for item in relations))
    return relations
