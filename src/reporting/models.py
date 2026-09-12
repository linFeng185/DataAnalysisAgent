"""报表工作台的 Pydantic 契约和运行领域模型。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TIMEZONE_RE = re.compile(r"^[A-Za-z0-9_./+-]{1,64}$")


class TimeRange(BaseModel):
    """分析单元的时间范围契约。"""

    kind: Literal["relative", "absolute", "all"] = "relative"
    value: str = Field(default="90d", min_length=1, max_length=64)

    # 方法作用：拒绝可能注入查询表达式的时间范围值。
    # Args: value - 用户输入的时间范围值。
    # Returns: 清理后的时间范围值。
    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_:.+\-/ ]{1,64}", normalized):
            raise ValueError("时间范围格式无效")
        return normalized


class AnalysisUnit(BaseModel):
    """一个可独立取数和分析的业务问题。"""

    unit_id: str = Field(default_factory=lambda: f"unit_{uuid4().hex[:10]}", max_length=64)
    question: str = Field(..., min_length=1, max_length=4000)
    datasource: str = Field(default="", max_length=64)
    priority: Literal["primary", "secondary", "evidence"] = "primary"
    time_range: TimeRange = Field(default_factory=TimeRange)
    dimensions: list[str] = Field(default_factory=list, max_length=30)
    metrics: list[str] = Field(default_factory=list, max_length=30)

    # 方法作用：校验分析单元标识并去除重复维度和指标。
    # Args: self - 待校验的分析单元。
    # Returns: 通过校验且字段规范化后的分析单元。
    @model_validator(mode="after")
    def normalize_unit(self) -> "AnalysisUnit":
        if not _IDENTIFIER_RE.fullmatch(self.unit_id):
            raise ValueError("unit_id 只能包含字母、数字、下划线、点和短横线")
        self.datasource = self.datasource.strip()
        if self.datasource and not _IDENTIFIER_RE.fullmatch(self.datasource):
            raise ValueError("分析单元数据源名称格式无效")
        self.dimensions = list(dict.fromkeys(item.strip() for item in self.dimensions if item.strip()))
        self.metrics = list(dict.fromkeys(item.strip() for item in self.metrics if item.strip()))
        return self


class SharePlan(BaseModel):
    """计划阶段的分享意图，支持指定用户授权预留。"""

    enabled: bool = False
    require_code: bool = False
    allow_drilldown: bool = False
    scope: Literal["private", "public", "user"] = "private"
    user_scope: Literal["none", "specified", "tenant"] = "none"
    user_ids: list[int] = Field(default_factory=list, max_length=100)

    # 方法作用：保证分享范围和用户授权列表保持一致。
    # Args: self - 分享意图。
    # Returns: 规范化后的分享意图。
    @model_validator(mode="after")
    def normalize_scope(self) -> "SharePlan":
        self.user_ids = list(dict.fromkeys(int(item) for item in self.user_ids if int(item) > 0))
        if not self.enabled:
            self.scope = "private"
            self.user_scope = "none"
            self.user_ids = []
            self.require_code = False
            self.allow_drilldown = False
        elif self.scope == "user" and self.user_scope == "none":
            self.user_scope = "specified" if self.user_ids else "tenant"
        if self.scope != "user":
            self.user_scope = "none"
            self.user_ids = []
        return self


class EstimatedResources(BaseModel):
    """计划阶段的有界资源估算。"""

    query_count: int = Field(default=1, ge=0, le=16)
    llm_tokens: int = Field(default=1000, ge=0, le=2_000_000)
    estimated_seconds: int = Field(default=5, ge=0, le=3600)


class ReportPlan(BaseModel):
    """自然语言报表任务的结构化计划。"""

    plan_version: str = "1.0"
    execution_mode: Literal["immediate", "scheduled"] = "immediate"
    frequency: Literal["hourly", "daily", "weekly", "monthly"] | None = None
    run_at: datetime | None = None
    schedule_day: int | None = Field(default=None, ge=1, le=31)
    timezone: str = "Asia/Shanghai"
    max_runs: int | None = Field(default=1, ge=1, le=1000)
    schedule_end_at: datetime | None = None
    page_ttl_days: int = Field(default=7, ge=1, le=90)
    permanent: bool = False
    requested_datasources: list[str] = Field(default_factory=list, max_length=20)
    analysis_units: list[AnalysisUnit] = Field(..., min_length=1, max_length=12)
    share: SharePlan = Field(default_factory=SharePlan)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    needs_confirmation: bool = False
    estimated: EstimatedResources = Field(default_factory=EstimatedResources)

    # 方法作用：校验计划的调度、时区、数据源和分析单元唯一性。
    # Args: self - 待校验的报表计划。
    # Returns: 规范化后的报表计划。
    @model_validator(mode="after")
    def validate_plan(self) -> "ReportPlan":
        if not _TIMEZONE_RE.fullmatch(self.timezone.strip()):
            raise ValueError("时区格式无效")
        self.timezone = self.timezone.strip()
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            zone = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("未知的 IANA 时区") from exc
        for field in ("run_at", "schedule_end_at"):
            value = getattr(self, field)
            if value is not None and value.tzinfo is None:
                setattr(self, field, value.replace(tzinfo=zone))
        self.requested_datasources = list(dict.fromkeys(item.strip() for item in self.requested_datasources if item.strip()))
        if any(not _IDENTIFIER_RE.fullmatch(item) for item in self.requested_datasources):
            raise ValueError("数据源名称格式无效")
        for unit in self.analysis_units:
            if unit.datasource and unit.datasource not in self.requested_datasources:
                self.requested_datasources.append(unit.datasource)
        unit_ids = [unit.unit_id for unit in self.analysis_units]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("分析单元 unit_id 不能重复")
        if self.execution_mode == "immediate":
            self.max_runs = 1
            self.frequency = None
        elif self.frequency is None:
            self.needs_confirmation = True
        if self.permanent:
            self.page_ttl_days = max(self.page_ttl_days, 7)
        self.warnings = list(dict.fromkeys(item.strip() for item in self.warnings if item.strip()))
        return self

    # 方法作用：生成不含可变估算字段的规范计划哈希。
    # Args: self - 已校验报表计划。
    # Returns: `sha256:` 前缀的计划摘要。
    def canonical_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"warnings", "estimated", "needs_confirmation"})
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class ReportPlanRequest(BaseModel):
    """自然语言计划预览请求。"""

    prompt: str = Field(..., min_length=1, max_length=20_000)
    datasources: list[str] = Field(default_factory=list, max_length=20)
    model_id: str = Field(default="", max_length=128)
    llm_connection_id: int | None = Field(default=None, ge=1)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    confirm: bool = False
    idempotency_key: str = Field(default="", max_length=128)

    # 方法作用：阻止请求同时伪造模型连接和不完整的模型标识。
    # Args: self - 计划请求。
    # Returns: 校验通过的请求。
    @model_validator(mode="after")
    def validate_model_selection(self) -> "ReportPlanRequest":
        if (self.llm_connection_id is None) != (not self.model_id.strip()):
            raise ValueError("llm_connection_id 与 model_id 必须同时提交")
        return self


class ReportJobCreateRequest(BaseModel):
    """按确认计划创建报表任务的请求。"""

    prompt: str = Field(..., min_length=1, max_length=20_000)
    plan_hash: str = Field(..., min_length=10, max_length=80, pattern=r"^sha256:[a-f0-9]{64}$")
    plan_override: ReportPlan | None = None
    confirm: bool = False
    idempotency_key: str = Field(default="", max_length=128)


class ReportShareCreateRequest(BaseModel):
    """创建公开或指定用户分享的请求。"""

    version: int | None = Field(default=None, ge=1)
    scope: Literal["public", "user"] = "public"
    require_code: bool = False
    authorization_code: str = Field(default="", min_length=0, max_length=128)
    expires_in_days: int = Field(default=7, ge=1, le=90)
    allow_drilldown: bool = False
    allow_download: bool = False
    pin_latest: bool = False
    user_ids: list[int] = Field(default_factory=list, max_length=100)

    # 方法作用：校验授权码和指定用户分享的最小安全条件。
    # Args: self - 分享创建请求。
    # Returns: 规范化后的分享请求。
    @model_validator(mode="after")
    def validate_share(self) -> "ReportShareCreateRequest":
        self.user_ids = list(dict.fromkeys(int(item) for item in self.user_ids if int(item) > 0))
        if self.require_code and len(self.authorization_code.strip()) < 6:
            raise ValueError("授权码至少需要 6 个字符")
        if self.require_code and len(self.authorization_code.encode("utf-8")) > 72:
            raise ValueError("授权码不能超过 72 字节")
        if self.scope == "user" and not self.user_ids:
            raise ValueError("指定用户分享必须提供 user_ids")
        if self.scope == "public" and self.user_ids:
            raise ValueError("公开分享不能携带 user_ids")
        if self.scope == "public":
            self.allow_drilldown = bool(self.allow_drilldown)
        return self


class DrilldownRequest(BaseModel):
    """快照 Drilldown 请求，只允许已签发字段和过滤条件。"""

    model_config = {"extra": "forbid"}
    filters: dict[str, Any] = Field(default_factory=dict, max_length=20)
    page: int = Field(default=1, ge=1, le=10_000)
    page_size: int = Field(default=50, ge=1, le=100)


class AuthorizationCodeRequest(BaseModel):
    """公开分享授权码解锁请求。"""

    authorization_code: str = Field(..., min_length=1, max_length=128)


class QuotaPolicyRequest(BaseModel):
    """租户管理员为用户配置的报表配额和能力。"""

    capabilities: list[str] = Field(default_factory=list, max_length=20)
    max_jobs_created: int = Field(default=20, ge=0, le=100_000)
    max_runs_started: int = Field(default=50, ge=0, le=100_000)
    max_concurrent_runs: int = Field(default=2, ge=0, le=100)
    max_analysis_units_per_run: int = Field(default=12, ge=1, le=12)
    # 与管理端上限保持一致：多源扇出的真实查询数是 单元数 × 命中源数。
    max_queries_per_run: int = Field(default=16, ge=1, le=1000)
    max_rows_per_query: int = Field(default=100_000, ge=1, le=10_000_000)
    max_llm_tokens: int = Field(default=200_000, ge=0, le=10_000_000)
    max_page_bytes: int = Field(default=2_097_152, ge=1024, le=50_000_000)
    max_share_links: int = Field(default=20, ge=0, le=100_000)


class UnitResult(BaseModel):
    """单个分析单元的脱敏结果和状态。"""

    unit_id: str
    datasource: str = ""
    live_query_id: str = ""
    status: Literal["success", "partial", "failed"] = "success"
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    columns: list[str] = Field(default_factory=list, max_length=100)
    summary: str = ""
    warnings: list[str] = Field(default_factory=list, max_length=20)
    error_code: str = ""
    user_error_message: str = ""

    # 方法作用：从结果样本推导稳定列集合，避免页面引用不存在的字段。
    # Args: self - 单元结果。
    # Returns: 更新后的单元结果。
    @model_validator(mode="after")
    def infer_columns(self) -> "UnitResult":
        if not self.columns and self.rows:
            self.columns = list(self.rows[0].keys())[:100]
        return self


class ReportJobRecord(BaseModel):
    """报表任务的 API 安全摘要。"""

    id: str
    tenant_id: int
    user_id: int
    prompt: str
    plan: ReportPlan
    status: str = "draft"
    runs_started: int = 0
    next_run_at: datetime | None = None
    is_paused: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ReportRunRecord(BaseModel):
    """报表运行的用户可见状态，内部错误不直接返回。"""

    id: str
    job_id: str
    status: str = "queued"
    attempt: int = 1
    last_node: str = ""
    query_count: int = 0
    row_count: int = 0
    token_usage: int = 0
    partial: bool = False
    error_code: str = ""
    user_error_message: str = ""
    trace_id: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ReportVersionRecord(BaseModel):
    """不可变报表页面版本摘要。"""

    id: str
    report_id: str
    job_id: str
    run_id: str
    version: int
    document: dict[str, Any]
    data_manifest: dict[str, Any] = Field(default_factory=dict)
    checksum: str
    expires_at: datetime | None = None
    is_latest: bool = True


class PageLifecycleRequest(BaseModel):
    """对既有页面延期或切换永久保留，不改变旧分享授权。"""

    model_config = {"extra": "forbid"}
    days: int | None = Field(default=None, ge=1, le=30)
    permanent: bool = False

    # 方法作用：只允许一个明确的页面生命周期操作。
    # Args: self - 生命周期请求。
    # Returns: 校验后的请求。
    @model_validator(mode="after")
    def validate_operation(self) -> "PageLifecycleRequest":
        if (self.days is not None) == self.permanent:
            raise ValueError("必须且只能选择延期天数或永久保留")
        return self


class ReportShareRecord(BaseModel):
    """分享链接记录，不含明文 token 或授权码。"""

    id: str
    report_id: str
    version: int
    scope: Literal["public", "user"]
    user_ids: list[int] = Field(default_factory=list)
    allow_drilldown: bool = False
    allow_download: bool = False
    pin_latest: bool = False
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
