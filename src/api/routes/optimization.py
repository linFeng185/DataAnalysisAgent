"""SQL 自动优化建议 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from src.api.schemas import OptimizationRequest
from src.logging_config import get_logger
from src.optimization.advisor import OptimizationAdvisor

logger = get_logger(__name__)
router = APIRouter()


@router.post("/optimization/advice")
# 方法作用：校验请求并返回确定性的 SQL 优化建议，不执行数据库变更。
# Args: payload - JSON 请求体，按 OptimizationRequest 校验。
# Returns: OptimizationReport 的 JSON 字典。
async def advice(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    logger.debug("SQL 优化建议 API 入口", payload_keys=sorted(payload.keys()))
    try:
        request = OptimizationRequest.model_validate(payload)
    except Exception as exc:
        logger.warning("SQL 优化建议请求无效", error=str(exc))
        raise HTTPException(422, "优化建议请求格式无效") from exc

    schema = dict(request.schema_data)
    indexes = dict(request.indexes)
    statistics = dict(request.statistics)
    dialect = request.dialect
    if request.datasource.strip():
        from src.api.auth import get_current_identity
        from src.datasource.registry import get_registry
        from src.security.permission_check import resolve_datasource_access

        identity = get_current_identity()
        registry = get_registry()
        try:
            available = await registry.list_all(tenant_id=identity.tenant_id)
            access = await resolve_datasource_access(
                available,
                [request.datasource],
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                role=identity.role,
            )
            datasource = await registry.resolve(request.datasource, tenant_id=identity.tenant_id)
        except PermissionError as exc:
            logger.warning(
                "SQL 优化建议数据源权限拒绝",
                datasource=request.datasource,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
            )
            raise HTTPException(403, "无权使用该数据源") from exc
        except Exception as exc:
            logger.error("SQL 优化建议数据源解析失败", datasource=request.datasource, error=str(exc), exc_info=True)
            raise HTTPException(503, "数据源服务暂时不可用") from exc
        dialect = str(getattr(datasource, "dialect", dialect) or dialect)
        permission = access.get(request.datasource, {})
        schema = schema or dict(getattr(datasource, "schema", {}) or {})
        from src.security.permission_check import check_column_whitelist, inject_row_filter

        column_error = check_column_whitelist(
            request.sql,
            list(permission.get("allowed_columns", []) or []),
        )
        if column_error:
            raise HTTPException(403, column_error)
        try:
            scoped_sql = inject_row_filter(
                request.sql,
                str(permission.get("row_filter_sql", "") or ""),
            )
        except Exception as exc:
            logger.error("SQL 优化建议行权限注入失败", datasource=request.datasource, error=str(exc), exc_info=True)
            raise HTTPException(403, "数据源行权限无法安全注入") from exc
        report = OptimizationAdvisor().advise(
            scoped_sql,
            dialect,
            schema=schema,
            indexes=indexes,
            statistics=statistics,
            execution_ms=request.execution_ms,
        )
        report.limitations.append(
            "已复核数据源 ACL；优化器不会执行 CREATE INDEX、ALTER TABLE 或其他 DDL"
        )
        if permission.get("allowed_columns"):
            report.limitations.append("当前数据源存在列白名单，候选建议仍需按权限重新审核")
    else:
        report = OptimizationAdvisor().advise(
            request.sql,
            dialect,
            schema=schema,
            indexes=indexes,
            statistics=statistics,
            execution_ms=request.execution_ms,
        )
    result = report.model_dump(mode="json")
    logger.info("SQL 优化建议 API 完成", valid=report.valid, issue_count=len(report.issues))
    return result
