"""通用数据资产、证据和分析产物契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DataAsset(BaseModel):
    """跨数据库、文件、文档和时序数据源的统一资产描述。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str = Field(min_length=1)
    kind: Literal[
        "database", "table_file", "document", "timeseries", "api", "stream",
    ]
    uri: str = Field(min_length=1)
    tenant_id: int = Field(ge=1)
    owner_id: int = Field(ge=0)
    mime_type: str = ""
    schema_info: dict[str, Any] = Field(default_factory=dict, alias="schema")
    temporal: dict[str, Any] = Field(default_factory=dict)
    acl: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")


class Evidence(BaseModel):
    """可回溯的证据片段，所有面向用户的结论都应引用它。"""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    locator: dict[str, Any] = Field(min_length=1)
    scores: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Citation(Evidence):
    """Evidence 的语义别名，用于响应中的引用字段。"""


class AnalysisPlan(BaseModel):
    """描述分析目标、资产、步骤、假设和验证规则。"""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1)
    asset_ids: list[str] = Field(min_length=1)
    steps: list[dict[str, Any]] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    validation_rules: list[dict[str, Any]] = Field(default_factory=list)
    resource_budget: dict[str, Any] = Field(default_factory=dict)


class AnalysisArtifact(BaseModel):
    """统一的分析、预测、报告或方案输出，保留证据和可复现信息。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["table", "chart", "report", "forecast", "scenario", "recommendation"]
    data: dict[str, Any] | list[Any]
    narrative: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    confidence: Literal["high", "medium", "low"]
    reproducibility: dict[str, Any] = Field(min_length=1)
