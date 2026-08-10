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
    metadata_limitations: list[str] = []
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
        datasource_schema = getattr(datasource, "schema", None)
        if not schema and isinstance(datasource_schema, dict):
            schema = dict(datasource_schema)
        elif not schema and datasource_schema is not None:
            schema, discovered_indexes, discovered_statistics = _snapshot_evidence(datasource_schema)
            indexes = indexes or discovered_indexes
            statistics = statistics or discovered_statistics
        if not schema:
            try:
                from src.knowledge.schema_manager import get_schema_manager

                snapshot = await get_schema_manager().get_or_fetch_schema(request.datasource)
                schema, discovered_indexes, discovered_statistics = _snapshot_evidence(snapshot)
                indexes = indexes or discovered_indexes
                statistics = statistics or discovered_statistics
                logger.info(
                    "SQL 优化建议 Schema 自动获取完成",
                    datasource=request.datasource,
                    table_count=len(schema),
                )
            except Exception as exc:
                metadata_limitations.append("自动获取 Schema 失败，已降级为 SQL AST 静态分析")
                logger.warning(
                    "SQL 优化建议 Schema 自动获取失败",
                    datasource=request.datasource,
                    error=str(exc),
                    exc_info=True,
                )
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
    report.limitations.extend(
        item for item in metadata_limitations if item not in report.limitations
    )
    result = report.model_dump(mode="json")
    logger.info("SQL 优化建议 API 完成", valid=report.valid, issue_count=len(report.issues))
    return result


# 方法作用：把 SchemaSnapshot 转换为建议器需要的 Schema、索引和行数证据。
# Args: snapshot - SchemaManager 返回的结构快照。
# Returns: 三个按表名组织且不含原始业务数据的证据映射。
def _snapshot_evidence(snapshot: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    schema: dict[str, Any] = {}
    indexes: dict[str, Any] = {}
    statistics: dict[str, Any] = {}
    remaining_columns = 1_000
    for table in list(getattr(snapshot, "tables", []) or [])[:500]:
        table_name = str(getattr(table, "name", "") or "").strip()
        if not table_name:
            continue
        columns = []
        table_indexes = []
        for column in list(getattr(table, "columns", []) or [])[:remaining_columns]:
            column_name = str(getattr(column, "name", "") or "").strip()
            if not column_name:
                continue
            columns.append({
                "name": column_name,
                "type": str(getattr(column, "type", "") or ""),
            })
            if bool(getattr(column, "is_indexed", False)) or bool(getattr(column, "is_primary_key", False)):
                table_indexes.append({"columns": [column_name], "source": "schema_introspection"})
        remaining_columns -= len(columns)
        schema[table_name] = {"columns": columns}
        if table_indexes:
            indexes[table_name] = table_indexes
        row_count = int(getattr(table, "row_count_estimate", 0) or 0)
        if row_count > 0:
            statistics[table_name] = {"row_count": row_count, "source": "schema_introspection"}
        if remaining_columns <= 0:
            break
    return schema, indexes, statistics
