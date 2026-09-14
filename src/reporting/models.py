"""报表工作台的 Pydantic 契约和运行领域模型。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from datetime import timezone as datetime_timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from src.logging_config import get_logger

logger = get_logger(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TIMEZONE_RE = re.compile(r"^[A-Za-z0-9_./+-]{1,64}$")

DivergenceLevel = Literal["conservative", "standard", "deep"]

# 档位是一次性总授权额度，计划期与运行期共享同一份额度，避免成本随轮数倍增。
_DIVERGENCE_UNIT_BUDGET: dict[str, int] = {"conservative": 0, "standard": 3, "deep": 6}
# 运行期回路最大轮数与单轮追加上限。
_DIVERGENCE_MAX_ROUNDS: dict[str, int] = {"conservative": 0, "standard": 1, "deep": 2}
_DIVERGENCE_UNITS_PER_ROUND: dict[str, int] = {"conservative": 0, "standard": 2, "deep": 3}
# 单元数的静态上界，仅用于 Pydantic 字段约束。真实上限由超级管理员在配置里
# 设定（report_max_units_per_run），运行时一律通过 max_plan_units() 读取——
# 散落的硬编码会导致"配置改了但某处仍卡在 12"。
_UNIT_COUNT_STATIC_MAX = 200
# 预估查询数的静态上界，同样只做契约兜底，必须与 report_max_queries_per_run
# 的可配最大值保持一致。上一版写死 64（按 18 单元 + 档位额度 6 再乘平均 4 个源
# 估算），而配置已放开到 36 单元：22 个规则单元 + deep 档 6 个发散额度命中 5 个
# 源即 (22+6)×5 = 140，直接击穿字段校验，parse() 抛 ValidationError，
# 报表落 plan_failed 且参数确定、重试必复现。
_QUERY_COUNT_STATIC_MAX = 1000


# 方法作用：读取超级管理员配置的计划单元数上限。
# Args: 无。
# Returns: 当前允许的分析单元总数（含发散单元），至少为 1。
def max_plan_units() -> int:
    from src.config import get_settings

    return max(1, int(get_settings().report_max_units_per_run))


# 方法作用：读取超级管理员配置的单次运行查询数上限。
# Args: 无。
# Returns: 当前允许的单次运行查询数上限，至少为 1。
def max_plan_queries() -> int:
    from src.config import get_settings

    return max(1, int(get_settings().report_max_queries_per_run))


# 方法作用：判断报表展示的行集是否相对查询全量被截断。
# Args: displayed - 报表实际展示的行数；full_count - 查询返回的全量行数，未知传 0。
# Returns: 展示的行数少于全量时返回 True。
def display_truncated(displayed: int, full_count: int) -> bool:
    """只按报表自己的展示行集判断截断。

    上游的 `query_result_truncated` 还包含"给模型的样本被裁到 50/30 行"这类与用户
    可见行集无关的标记，直接透传会把"共 60 行、全部展示"的单元判成"样本被截断"——
    实机页面上 106 条发现条目逐条刷屏"样本被截断"，就是这么来的。
    全量未知（0）时不臆断为截断。
    """
    if full_count <= 0:
        return False
    return int(full_count) > int(displayed)


# 「计划生成中」的卡死阈值：后台规划任务活在进程内，服务重启后记录会永远停在
# planning——既不能重试（接口只允许 plan_failed）也不能删除，用户无路可走。
# 超过该时长即视为卡死，允许中断并重新生成。
# 取 5 分钟：含模型拆解与计划期发散的正常规划远快于此，因此不会误伤在途任务。
PLAN_STUCK_SECONDS = 300


# 方法作用：判断一条 planning 记录是否已经卡死到可以中断重来。
# Args: updated_at - 记录最后更新时间（datetime 或 ISO 字符串）；now - 可注入当前时间。
# Returns: 无法判定或未超阈值时返回 False，只有确认超时才返回 True。
def plan_is_stuck(updated_at: Any, *, now: datetime | None = None) -> bool:
    moment = updated_at
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        except ValueError:
            # 时间不可解析时按"未卡死"处理：宁可让用户多等，也不要误中断在途任务。
            logger.warning("报表更新时间不可解析，按未卡死处理", updated_at=str(updated_at)[:40])
            return False
    if not isinstance(moment, datetime):
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime_timezone.utc)
    reference = now or datetime.now(datetime_timezone.utc)
    return (reference - moment).total_seconds() >= PLAN_STUCK_SECONDS


# 单个分析单元在页面上的块预算：kpi / chart / table / narrative / quality 五块，
# 再为确定性深挖发现与关系证据留出同量级余量。findings 是按发现条数生成的，
# 单元数上调后块数会同步放大，所以块上限必须由单元数推导而不是写死。
_UNIT_BLOCK_BUDGET = 10
# 与单元数无关的固定区段（总览/关系证据/指标口径/跨源聚合）预留的块数。
_FIXED_BLOCK_BUDGET = 40
# 页面块数下限，保证单单元的小报表仍有原 80 块的空间。
_MIN_DOCUMENT_BLOCKS = 80


# 方法作用：按可配置的分析单元上限推导报表页面的块数上限。
# Args: 无。
# Returns: 页面级块数上限；随 report_max_units_per_run 缩放，避免单元数上调后
#          页面校验把已成功取数的整份报表判为失败。
def max_document_blocks() -> int:
    return max(_MIN_DOCUMENT_BLOCKS, max_plan_units() * _UNIT_BLOCK_BUDGET + _FIXED_BLOCK_BUDGET)


# 因果措辞黑名单：提示词约束是软约束，服务端必须硬校验。
_CAUSAL_PHRASE_RE = re.compile(r"导致|造成|因为|引起|致使|归因于")


# 方法作用：返回指定档位允许系统自主追加的单元总数。
# Args: level - 档位值。
# Returns: 允许的追加单元数；未知档位按标准档处理。
def divergence_unit_budget(level: str) -> int:
    return _DIVERGENCE_UNIT_BUDGET.get(str(level or ""), _DIVERGENCE_UNIT_BUDGET["standard"])


# 方法作用：返回指定档位允许的运行期回路轮数。
# Args: level - 档位值。
# Returns: 最大轮数；未知档位按标准档处理。
def divergence_max_rounds(level: str) -> int:
    return _DIVERGENCE_MAX_ROUNDS.get(str(level or ""), _DIVERGENCE_MAX_ROUNDS["standard"])


# 方法作用：返回指定档位允许的单轮追加单元上限。
# Args: level - 档位值。
# Returns: 单轮上限；未知档位按标准档处理。
def divergence_units_per_round(level: str) -> int:
    return _DIVERGENCE_UNITS_PER_ROUND.get(str(level or ""), _DIVERGENCE_UNITS_PER_ROUND["standard"])


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
    # 单元来源：用户显式要求，还是系统发散建议。进 canonical_hash。
    origin: Literal["user", "diverged"] = "user"
    # 仅发散单元非空：为什么建议分析这个。预览页展示，是发散可解释性的核心。
    rationale: str = Field(default="", max_length=500)
    # 发散单元由哪一阶段产生：计划期或运行期。
    decided_by: Literal["", "plan", "runtime"] = ""

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
        self.rationale = self.rationale.strip()
        # 用户单元不得携带发散元信息，否则说明单元来源被误标。
        if self.origin == "user":
            if self.rationale:
                raise ValueError("用户单元不允许携带 rationale")
            if self.decided_by:
                raise ValueError("用户单元不允许携带 decided_by")
        else:
            if not self.rationale:
                raise ValueError("发散单元必须说明 rationale")
            if self.decided_by not in {"plan", "runtime"}:
                raise ValueError("发散单元必须标注 decided_by 为 plan 或 runtime")
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

    # 静态上界只做契约兜底，真实钳位由 max_plan_queries() 读配置完成：写死一个
    # 与配置脱钩的常量，会变成"配置允许 1000、这里仍卡在 64"的埋雷。该值只影响
    # 预占的保守程度，实际扣费仍由 runner 按真实扇出修正。
    query_count: int = Field(default=1, ge=0, le=_QUERY_COUNT_STATIC_MAX)
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
    analysis_units: list[AnalysisUnit] = Field(..., min_length=1, max_length=_UNIT_COUNT_STATIC_MAX)
    # 发散档位。同一需求不同档位对应不同执行范围与成本上限，因此进 canonical_hash。
    divergence: DivergenceLevel = "standard"
    # 相对预览计划被用户删减掉的单元标识，保留审计线索。同样进 canonical_hash。
    excluded_unit_ids: list[str] = Field(default_factory=list, max_length=_UNIT_COUNT_STATIC_MAX)
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
        # 字段上的静态上界只是 Pydantic 边界，真实上限由配置决定。
        limit = max_plan_units()
        if len(self.analysis_units) > limit:
            raise ValueError(f"分析单元数超过系统上限 {limit}")
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
    # 发散档位由用户在预览前选定；模型无权决定成本上限。
    divergence: DivergenceLevel = "standard"
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
    # 用户在预览页勾选保留的单元。None 表示全部接受（与既有行为一致）；
    # 显式空数组表示全部取消，属非法请求。只允许删减，不允许新增。
    accepted_unit_ids: list[str] | None = Field(default=None, max_length=_UNIT_COUNT_STATIC_MAX)
    # 视觉模板标识。**不进 ReportPlan、不进 canonical_hash**：模板只影响呈现，
    # 不改变执行范围与成本，因此不该让改配色触发重新预览与重新确认。
    # 只校格式不校清单：清单在前端注册表，后端再存一份必然脱钩。
    template_id: str | None = Field(default=None, max_length=32, pattern=r"^[a-z0-9_-]{1,32}$")
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
    max_analysis_units_per_run: int = Field(default=12, ge=1, le=_UNIT_COUNT_STATIC_MAX)
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
    # 单元产出形态：data 表示应返回数据行，text 表示直答（元数据说明 / 知识类），
    # 无数据行是它的正常形态。成功单元的判据据此区分，否则直答单元会被判成
    # 部分完成，整份报表被拖成 partial。
    answer_kind: Literal["data", "text"] = "data"
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    # 旧工作流已给出截断信号，执行器必须透传：统计结论需要据此披露样本局限。
    truncated: bool = False
    columns: list[str] = Field(default_factory=list, max_length=100)
    # 单元问题里 @metric(...) 引用解析出的已批准口径快照，供页面做口径溯源。
    metric_citations: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    # 数据源列备注，键为列名。来自既有工作流的 relevant_tables，不额外查询。
    column_comments: dict[str, str] = Field(default_factory=dict)
    # 结果字段的中文说明，由分析模型给出。聚合与表达式计算字段主要靠它标注。
    field_descriptions: dict[str, str] = Field(default_factory=dict)
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
            self.columns = list(dict.fromkeys(column for row in self.rows for column in row))[:100]
        return self


class ReportJobRecord(BaseModel):
    """报表任务的 API 安全摘要。"""

    id: str
    tenant_id: int
    user_id: int
    prompt: str
    plan: ReportPlan
    # 有效计划的校验和；plan_hash 与 preview_hash 在未裁剪时相等。
    plan_hash: str = ""
    # 客户端提交的预览凭证，用于审计「这份计划来自哪次预览」。
    preview_hash: str = ""
    # 创建任务时选定的视觉模板；为空表示使用前端默认模板。
    template_id: str | None = None
    status: str = "draft"
    runs_started: int = 0
    next_run_at: datetime | None = None
    is_paused: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # 报表摘要标题。点「生成计划」时为空，后台规划完成后由 report.title 任务写入；
    # 模型不可用或解析失败时回退为 prompt 前 20 字。
    title: str = ""
    # 发散档位冗余字段，报表信息列表据此出列，无需解析 plan_json。
    divergence: str = "standard"
    # 创建时选定的数据源，同样供列表出列。
    datasources: list[str] = Field(default_factory=list)
    # 计划失败的可读原因，仅 plan_failed 状态非空。
    plan_error: str = ""


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
    # 以下为运行期真实计数，与 query_count 的配额估算口径相互独立。
    llm_call_count: int = 0
    llm_failure_count: int = 0
    sql_query_count: int = 0
    sql_success_count: int = 0
    sql_failure_count: int = 0
    partial: bool = False
    error_code: str = ""
    user_error_message: str = ""
    trace_id: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None

    # 方法作用：把运行记录转换为前端可直接消费的计数汇总。
    # Args: self - 运行记录。
    # Returns: 含 LLM 与 SQL 成败计数、以及预估查询数的字典。
    def stats_summary(self) -> dict[str, int]:
        """返回运行计数汇总，供页面同时展示预估与实际。"""
        return {
            "llm_call_count": self.llm_call_count,
            "llm_failure_count": self.llm_failure_count,
            "llm_success_count": max(0, self.llm_call_count - self.llm_failure_count),
            "llm_token_usage": self.token_usage,
            "sql_query_count": self.sql_query_count,
            "sql_success_count": self.sql_success_count,
            "sql_failure_count": self.sql_failure_count,
            "estimated_query_count": self.query_count,
        }


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


# 模板标识的格式约束与 ReportJobCreateRequest 保持一致：只校格式不校清单，
# 清单在前端注册表，后端再存一份必然脱钩。空字符串表示使用前端默认模板。
_TEMPLATE_ID_PATTERN = r"^[a-z0-9_-]{0,32}$"


class ReportCreateRequest(BaseModel):
    """点「生成计划」创建报表记录并启动后台规划；不预占任何额度。"""

    prompt: str = Field(..., min_length=1, max_length=20_000)
    datasources: list[str] = Field(default_factory=list, max_length=20)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    # 发散档位由用户在创建页选定；模型无权决定成本上限。
    divergence: DivergenceLevel = "standard"
    template_id: str = Field(default="", max_length=32, pattern=_TEMPLATE_ID_PATTERN)
    model_id: str = Field(default="", max_length=128)
    llm_connection_id: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(default="", max_length=128)

    # 方法作用：校验提示词、数据源名、时区和模型连接成对提交。
    # Args: self - 创建请求。
    # Returns: 规范化后的创建请求。
    @model_validator(mode="after")
    def validate_create(self) -> "ReportCreateRequest":
        self.prompt = self.prompt.strip()
        if not self.prompt:
            # 空白提示词无法生成计划，必须在入口拦截而不是让后台任务失败。
            raise ValueError("报表描述不能为空")
        # 数据源名会进占位计划的 requested_datasources，格式必须与 ReportPlan 一致，
        # 否则创建成功、后台规划时才在模型校验处失败。
        self.datasources = list(dict.fromkeys(item.strip() for item in self.datasources if item.strip()))
        if any(not _IDENTIFIER_RE.fullmatch(item) for item in self.datasources):
            raise ValueError("数据源名称格式无效")
        self.timezone = self.timezone.strip()
        if not _TIMEZONE_RE.fullmatch(self.timezone):
            raise ValueError("时区格式无效")
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("未知的 IANA 时区") from exc
        if (self.llm_connection_id is None) != (not self.model_id.strip()):
            raise ValueError("llm_connection_id 与 model_id 必须同时提交")
        return self


class ReportSchedulePatchRequest(BaseModel):
    """报表信息页编辑计划与调度：全部字段可选，分析单元只允许删减。"""

    # None 表示不提交该字段；显式空数组表示全部取消（由 _apply_unit_trimming 拒绝）。
    accepted_unit_ids: list[str] | None = Field(default=None, max_length=_UNIT_COUNT_STATIC_MAX)
    execution_mode: Literal["immediate", "scheduled"] | None = None
    frequency: Literal["hourly", "daily", "weekly", "monthly"] | None = None
    run_at: datetime | None = None
    max_runs: int | None = Field(default=None, ge=1, le=1000)
    schedule_end_at: datetime | None = None
    page_ttl_days: int | None = Field(default=None, ge=1, le=90)
    permanent: bool | None = None
    timezone: str | None = Field(default=None, max_length=64)
    template_id: str | None = Field(default=None, max_length=32, pattern=_TEMPLATE_ID_PATTERN)
