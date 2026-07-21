"""行列级权限校验——列白名单拦截 + 行过滤注入。"""

from __future__ import annotations

from src.logging_config import get_logger
from src.memory.pg_pool import get_pg_pool

logger = get_logger(__name__)


# 方法作用：在可用数据源中解析当前身份允许访问的数据源及其行列权限。
# Args: available_datasources - Registry 数据源摘要；requested_datasources - 用户显式选择；tenant_id/user_id/role - 当前身份；multi_tenant - 是否启用多租户。
# Returns: 按候选顺序返回的数据源权限映射，键为数据源名称。
async def resolve_datasource_access(
    available_datasources: list[dict],
    requested_datasources: list[str],
    *,
    tenant_id: int,
    user_id: int,
    role: str,
    multi_tenant: bool,
) -> dict[str, dict]:
    """先完成服务端授权，再把候选集合交给模型选择或 SQL 工作流。

    Args:
        available_datasources: Registry 当前可见的数据源摘要。
        requested_datasources: 用户显式选择的数据源；空列表表示自动发现。
        tenant_id: 当前租户 ID。
        user_id: 当前用户 ID。
        role: 当前用户角色。
        multi_tenant: 是否启用多租户隔离。

    Returns:
        包含数据源描述、列白名单和行过滤条件的有序映射。

    Raises:
        PermissionError: 数据源无权访问或权限服务不可用。
    """
    normalized_role = str(role or "anonymous").strip().lower()
    available_by_name = {
        str(item.get("name", "")).strip(): dict(item)
        for item in available_datasources
        if str(item.get("name", "")).strip()
    }
    requested = list(dict.fromkeys(
        str(name).strip() for name in requested_datasources if str(name).strip()
    ))
    candidate_names = requested or list(available_by_name)
    logger.debug(
        "数据源访问解析入口",
        tenant_id=tenant_id,
        user_id=user_id,
        role=normalized_role,
        multi_tenant=multi_tenant,
        requested_count=len(requested),
        available_count=len(available_by_name),
    )

    unknown = [name for name in candidate_names if name not in available_by_name]
    if unknown:
        logger.warning("数据源访问解析拒绝", reason="数据源不存在", datasources=unknown)
        raise PermissionError(f"无权访问数据源: {', '.join(unknown)}")

    if not multi_tenant or normalized_role == "super_admin":
        result = {
            name: {
                **available_by_name[name],
                "allowed_columns": [],
                "row_filter_sql": "",
                "access_level": "admin" if normalized_role == "super_admin" else "read",
            }
            for name in candidate_names
        }
        if not result:
            logger.warning("数据源访问解析拒绝", reason="没有可用数据源")
            raise PermissionError("没有可访问的数据源")
        logger.info("数据源访问解析完成", authorized_count=len(result), mode="trusted")
        return result

    try:
        pool = await get_pg_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT datasource_name, owner_user_id, visibility, access_level, "
                "allowed_columns, row_filter_sql FROM datasource_permissions "
                "WHERE tenant_id=$1",
                tenant_id,
            )
    except Exception as exc:
        logger.error(
            "数据源权限存储读取失败",
            tenant_id=tenant_id,
            user_id=user_id,
            error=str(exc),
            exc_info=True,
        )
        raise PermissionError("数据源权限服务不可用") from exc

    permissions: dict[str, dict] = {}
    for row in rows:
        name = str(row["datasource_name"] or "").strip()
        visibility = str(row["visibility"] or "private").strip().lower()
        access_level = str(row["access_level"] or "").strip().lower()
        owner_user_id = int(row["owner_user_id"] or 0)
        readable = access_level in {"read", "write", "admin"}
        visible = visibility == "tenant" or (
            visibility == "private" and owner_user_id == user_id
        )
        if name in available_by_name and readable and visible:
            permissions[name] = {
                **available_by_name[name],
                "allowed_columns": list(row["allowed_columns"] or []),
                "row_filter_sql": str(row["row_filter_sql"] or ""),
                "access_level": access_level,
            }

    result = {
        name: permissions[name]
        for name in candidate_names
        if name in permissions
    }
    unauthorized = [name for name in requested if name not in result]
    if unauthorized:
        logger.warning(
            "数据源访问解析拒绝",
            reason="权限不足",
            tenant_id=tenant_id,
            user_id=user_id,
            datasources=unauthorized,
        )
        raise PermissionError(f"无权访问数据源: {', '.join(unauthorized)}")
    if not result:
        logger.warning(
            "数据源访问解析拒绝",
            reason="没有授权候选",
            tenant_id=tenant_id,
            user_id=user_id,
        )
        raise PermissionError("没有可访问的数据源")
    logger.info(
        "数据源访问解析完成",
        authorized_count=len(result),
        tenant_id=tenant_id,
        user_id=user_id,
        discovery=not requested,
    )
    return result


def check_column_whitelist(sql: str, allowed_columns: list[str]) -> str | None:
    """校验 SQL 引用的列是否全部在白名单中。空列表=全部允许。

    Args:
        sql: 待校验 SQL
        allowed_columns: 允许的列名列表

    Returns: 错误消息或 None（通过）
    """
    logger.debug("列白名单校验入口", allowed_count=len(allowed_columns), sql_preview=sql[:120])
    if not allowed_columns:
        logger.info("列白名单为空，允许全部列")
        return None
    try:
        import sqlglot
        from sqlglot import exp
        whitelist = set(c.lower() for c in allowed_columns)
        tree = sqlglot.parse_one(sql)
        if not tree:
            logger.warning("列白名单校验失败", reason="SQL 解析结果为空")
            return "列权限校验解析失败: SQL 为空"

        for select in tree.find_all(exp.Select):
            has_wildcard_projection = any(
                isinstance(projection, exp.Star)
                or (isinstance(projection, exp.Column) and projection.is_star)
                for projection in select.expressions
            )
            if has_wildcard_projection:
                logger.warning("列白名单校验失败", reason="投影包含通配符")
                return "列权限不足: 启用列白名单时禁止使用通配符 *"

        violations: list[str] = []
        for node in tree.find_all(exp.Column):
            col = node.name.lower()
            if col in ("*", "1", "true", "false"):
                continue
            if col not in whitelist and col not in violations:
                violations.append(col)
        if violations:
            message = f"列权限不足: {', '.join(violations[:5])} 不在允许范围内"
            logger.warning("列白名单校验失败", violations=violations[:5])
            return message
        logger.info("列白名单校验通过", referenced_columns=len(list(tree.find_all(exp.Column))))
        return None
    except Exception as exc:
        logger.error("列白名单 SQL 解析失败", error=str(exc), exc_info=True)
        return f"列权限校验解析失败: {str(exc)[:200]}"


def inject_row_filter(sql: str, row_filter: str) -> str:
    """用 sqlglot AST 解析后在 WHERE 注入行过滤条件。

    比字符串拼接安全——不会注入到子查询或字面量中。

    Args:
        sql: 原始 SQL
        row_filter: 行过滤片段，如 "org_id = 5"

    Returns: 注入后的 SQL
    """
    logger.debug("行过滤注入入口", sql_preview=sql[:120], has_filter=bool(row_filter.strip()))
    if not row_filter or not row_filter.strip():
        logger.info("行过滤为空，保留原 SQL")
        return sql
    try:
        import sqlglot
        from sqlglot import exp
        from src.exceptions import SQLSecurityError

        tree = sqlglot.parse_one(sql)
        if not tree:
            raise SQLSecurityError("原 SQL 无法解析", "ROW_FILTER")
        filter_expr = sqlglot.parse_one(row_filter)
        if not filter_expr or isinstance(filter_expr, (exp.Query, exp.Command)):
            raise SQLSecurityError("行过滤条件不是布尔表达式", "ROW_FILTER")
        where = tree.args.get("where")
        if where:
            where.this = exp.And(this=where.this, expression=filter_expr)
        else:
            tree.set("where", exp.Where(this=filter_expr))
        result = tree.sql()
        logger.info("行过滤注入完成", filter=row_filter[:80])
        return result
    except Exception as exc:
        from src.exceptions import SQLSecurityError

        if isinstance(exc, SQLSecurityError):
            logger.error("行过滤注入被安全阻断", error=str(exc), exc_info=True)
            raise
        logger.error("行过滤注入失败", error=str(exc), exc_info=True)
        raise SQLSecurityError("行过滤注入失败", "ROW_FILTER") from exc
