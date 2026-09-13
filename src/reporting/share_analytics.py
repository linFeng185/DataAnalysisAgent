"""链接管理与分享访问统计的 Pydantic 契约和纯函数辅助。

本模块单独承载 C 批（需求 3）新增的接口契约，避免与并行开发中的
`src/reporting/models.py` 相互冲突；契约稳定后可整体并入 models.py。
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.logging_config import get_logger

logger = get_logger(__name__)

# 报表标题摘要长度：report_jobs 暂无 title 列，先用 prompt 截断占位。
_REPORT_TITLE_LIMIT = 40


# 方法作用：把报表描述截断成链接管理列表可用的短标题。
# Args: prompt - 报表任务的原始自然语言描述；limit - 最大字符数。
# Returns: 压缩空白后的标题摘要。
def summarize_report_title(prompt: str, limit: int = _REPORT_TITLE_LIMIT) -> str:
    text = " ".join(str(prompt or "").split())
    title = text if len(text) <= limit else f"{text[:limit]}…"
    logger.debug("报表标题摘要生成完成", prompt_chars=len(text), title_chars=len(title))
    return title


# 方法作用：用部署密钥对客户端 IP 做不可逆摘要，审计只落摘要不落明文。
# Args: client_ip - 由访问策略中间件解析出的客户端 IP。
# Returns: HMAC-SHA256 十六进制摘要；空 IP 返回空字符串。
def hash_client_ip(client_ip: str) -> str:
    from src.config import get_settings

    value = str(client_ip or "").strip()
    if not value:
        logger.debug("客户端 IP 摘要跳过", reason="empty")
        return ""
    secret = str(getattr(get_settings(), "jwt_secret", "") or "report-share-fallback").encode("utf-8")
    digest = hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()
    logger.debug("客户端 IP 摘要生成完成", digest_chars=len(digest))
    return digest


class ShareVisitCreateRequest(BaseModel):
    """公开侧上报一次分享访问的可选请求体。"""

    unlocked: bool = False


class ShareVisitCreateResponse(BaseModel):
    """访问上报结果，匿名与登录访客共用同一响应结构。"""

    visit_id: str
    visitor_kind: Literal["user", "anonymous"]


class ShareVisitUpdateRequest(BaseModel):
    """上报累积停留时长，幂等覆盖而非增量累加。"""

    # 上限 30 天：心跳上报理论上不该超过单次会话寿命，防御异常客户端的整数溢出。
    duration_seconds: int = Field(..., ge=0, le=2_592_000)


class ShareVisitUpdateResponse(BaseModel):
    """停留时长写入结果。"""

    visit_id: str
    duration_seconds: int


class ShareListItem(BaseModel):
    """链接管理平铺列表的一行，含按访问明细聚合的统计。"""

    id: str
    report_id: str
    report_title: str = ""
    version: int = 0
    scope: str = "public"
    user_scope: str = "none"
    require_code: bool = False
    created_by: int = 0
    created_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    visitor_count: int = 0
    system_visitor_count: int = 0
    anonymous_visitor_count: int = 0
    total_duration_seconds: int = 0
    average_duration_seconds: int = 0


class ShareListResponse(BaseModel):
    """链接管理列表分页响应。"""

    shares: list[ShareListItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


class ShareVisitItem(BaseModel):
    """最近访问的系统用户及其停留时长。"""

    user_id: int
    username: str = ""
    started_at: datetime | None = None
    last_seen_at: datetime | None = None
    duration_seconds: int = 0
    ip_hash: str = ""


class ShareVisitsResponse(BaseModel):
    """分享访问者列表响应。"""

    visits: list[ShareVisitItem] = Field(default_factory=list)
    total: int = 0


class BatchRevokeSharesRequest(BaseModel):
    """批量撤销分享的请求体。"""

    share_ids: list[str] = Field(..., min_length=1, max_length=100)


class BatchRevokeSharesResponse(BaseModel):
    """批量撤销结果，skipped 计入无权限、已撤销或不存在的标识。"""

    revoked: int = 0
    skipped: int = 0


class TenantReportSettings(BaseModel):
    """租户级报表设置。"""

    # 需求 3.2：关闭后即使携带登录身份也按 anonymous 记录，不写 user_id。
    record_share_visitor_identity: bool = True
    # 需求 2.4：0 表示不限制单个用户可保存的永久报表数量。
    max_permanent_reports_per_user: int = Field(default=0, ge=0, le=100_000)


# 方法作用：把数据库或内存记录转换为链接管理列表行的标准字典。
# Args: row - 含聚合字段的记录字典。
# Returns: 可直接序列化的列表行字典。
def to_share_list_item(row: dict[str, Any]) -> dict[str, Any]:
    logger.debug("链接管理列表行转换入口", share_id=str(row.get("id", "")))
    item = ShareListItem(
        id=str(row.get("id", "")),
        report_id=str(row.get("report_id", "")),
        # report_jobs 目前没有 title 列，先取 prompt 摘要；后续批次新增 title 后，
        # 只需把这里的取值改为优先 title、缺失再回退 prompt 摘要。
        report_title=summarize_report_title(str(row.get("report_prompt", "") or "")),
        version=int(row.get("version", 0) or 0),
        scope=str(row.get("scope", "public") or "public"),
        user_scope=str(row.get("user_scope", "none") or "none"),
        require_code=bool(row.get("require_code", False)),
        created_by=int(row.get("created_by", 0) or 0),
        created_at=row.get("created_at"),
        expires_at=row.get("expires_at"),
        revoked_at=row.get("revoked_at"),
        visitor_count=int(row.get("visitor_count", 0) or 0),
        system_visitor_count=int(row.get("system_visitor_count", 0) or 0),
        anonymous_visitor_count=int(row.get("anonymous_visitor_count", 0) or 0),
        total_duration_seconds=int(row.get("total_duration_seconds", 0) or 0),
        average_duration_seconds=int(row.get("average_duration_seconds", 0) or 0),
    )
    logger.debug("链接管理列表行转换完成", share_id=item.id, visitor_count=item.visitor_count)
    return item.model_dump(mode="json")
