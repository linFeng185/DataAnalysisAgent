"""租户指标目录 API，与 Prometheus 指标端点分离。"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from src.api.auth import get_current_identity
from src.app_context import get_tenant_policy
from src.datasource.registry import get_registry
from src.knowledge.metric_catalog import (
    MetricConflict,
    MetricRevisionRequest,
    MetricTransition,
    get_metric_catalog,
)
from src.logging_config import get_logger
from src.security.permission_check import resolve_datasource_access

logger = get_logger(__name__)
router = APIRouter(prefix="/metric-catalog", tags=["租户指标目录"])


class MetricSaveRequest(BaseModel):
    """先以权威数据源补全方言，再验证指标定义。"""
    model_config = ConfigDict(extra="forbid")
    definition: dict[str, Any]
    expected_version: int = Field(ge=0)


# 使用当前租户数据源目录及 ACL 确定权威方言，不建立业务数据库连接。
# Args: name - 请求的数据源名。
# Returns: 实际数据源方言。
async def metric_datasource_dialect(name: str) -> str:
    """平台身份不能绕过租户目录创建业务指标。"""
    identity = get_current_identity()
    if identity.role != "tenant_admin" or identity.tenant_id <= 0:
        raise PermissionError("指标口径由当前租户管理员维护")
    items = await get_registry().list_all(tenant_id=identity.tenant_id)
    source = next((item for item in items if item["name"] == name), None)
    if source is None:
        raise LookupError("数据源不存在或不可见")
    await resolve_datasource_access(items, [name], tenant_id=identity.tenant_id,
                                    user_id=identity.user_id, role=identity.role, tenant_policy=get_tenant_policy())
    return str(source["dialect"])


# 方法作用：在唯一 HTTP 边界把业务异常映射为明确状态码。
# Args: 无。
# Returns: 异常转换上下文。
@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except HTTPException:
        raise
    except PermissionError as exc:
        logger.warning("指标目录访问被拒绝")
        raise HTTPException(403, str(exc)) from None
    except MetricConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception:
        logger.error("指标目录操作失败", exc_info=True)
        raise HTTPException(503, "指标目录暂不可用，请稍后重试") from None


# 方法作用：按当前身份返回分页可见指标。
# Args: query - 搜索词；limit/offset - 分页。
# Returns: 最新可见指标列表和总数。
@router.get("")
async def list_metrics(query: str = Query("", max_length=128), limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict:
    """普通成员只读取已生效批准版本，租户管理员包含草稿。"""
    logger.debug("指标目录列表入口", limit=limit, offset=offset)
    with _errors():
        return await get_metric_catalog().list_metrics(get_current_identity(), query=query, limit=limit, offset=offset)


# 方法作用：保存指标的新定义版本，身份只来自认证上下文。
# Args: request - 指标定义和预期版本。
# Returns: 新草稿。
@router.post("")
async def save_metric(request: MetricSaveRequest) -> dict:
    """并发保存冲突返回 409，定义不会被原位覆盖。"""
    logger.debug("指标目录保存入口")
    with _errors():
        name = str(request.definition.get("datasource", "")).strip()
        dialect = await metric_datasource_dialect(name)
        logger.info("指标数据源方言边界", datasource=name, dialect=dialect)
        verified = MetricRevisionRequest.model_validate({"definition": {**request.definition, "datasource": name, "dialect": dialect}, "expected_version": request.expected_version})
        return await get_metric_catalog().save_revision(get_current_identity(), verified)


# 方法作用：读取一个编码的可见版本历史。
# Args: code - 指标编码。
# Returns: 历史版本列表。
@router.get("/{code}/versions")
async def metric_versions(code: str) -> dict:
    """审核历史始终限制在当前租户。"""
    logger.debug("指标历史读取入口")
    with _errors():
        return {"items": await get_metric_catalog().versions(get_current_identity(), code)}


# 方法作用：读取当前生效版本并提供固定版本引用。
# Args: code - 指标编码。
# Returns: 可安全引用的批准版本。
@router.get("/{code}/effective")
async def effective_metric(code: str) -> dict:
    """未批准或生效时间未到时返回 404。"""
    logger.debug("指标生效版本读取入口")
    with _errors():
        return await get_metric_catalog().effective(get_current_identity(), code)


# 方法作用：执行租户管理员的提交、批准或驳回动作。
# Args: metric_id - 指标版本 ID；request - 审核动作。
# Returns: 已持久化的状态。
@router.post("/{metric_id}/transition")
async def transition_metric(metric_id: str, request: MetricTransition) -> dict:
    """批准定义与未来生效分开，避免新口径提前替代旧口径。"""
    logger.debug("指标审核入口", action=request.action)
    with _errors():
        return await get_metric_catalog().transition(get_current_identity(), metric_id, request)
