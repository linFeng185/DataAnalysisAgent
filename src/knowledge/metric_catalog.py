"""租户指标目录：不可变定义版本、审核状态与生效口径。"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal, Self
from uuid import uuid4

import sqlglot
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlglot import exp

from src.logging_config import get_logger
from src.memory.pg_pool import get_pg_pool, pg_pool_connection
from src.security.tenant_policy import RequestIdentity

logger = get_logger(__name__)


class MetricConflict(ValueError):
    """版本已被修改或生命周期状态不允许当前操作。"""


class MetricDefinition(BaseModel):
    """租户维护的业务公式和统计口径，不包含数据库凭证。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    name: str = Field(min_length=1, max_length=128)
    datasource: str = Field(min_length=1, max_length=64)
    dialect: str = Field(default="postgres", pattern=r"^[a-z][a-z0-9_]{0,63}$")
    expression: str = Field(min_length=1, max_length=4000)
    description: str = Field(default="", max_length=8000)
    unit: str = Field(default="", max_length=32)

    # 方法作用：按声明方言验证公式只包含一个表达式而不是可执行查询。
    # Args: self - 待保存定义。
    # Returns: 校验后的定义，非法公式抛出 ValueError。
    @model_validator(mode="after")
    def validate_expression(self) -> Self:
        """表达式仅用于口径引用，执行 SQL 仍经过完整权限与只读校验。"""
        try:
            from src.connectors.plugins import sql_dialect
            expressions = sqlglot.parse(f"SELECT {self.expression}", read=sql_dialect(self.dialect))
            forbidden = (exp.Query, exp.DML, exp.DDL, exp.Command, exp.Subquery)
            logger.debug("指标表达式语法边界", ast_types=[type(item).__name__ for item in expressions if item is not None])
            if len(expressions) != 1 or not isinstance(expressions[0], exp.Select):
                raise ValueError("指标公式必须是单个 SQL 表达式")
            selection = expressions[0]
            if len(selection.expressions) != 1 or any(value for key, value in selection.args.items() if key != "expressions"):
                raise ValueError("指标公式不允许携带查询子句")
            if any(isinstance(node, forbidden) for node in selection.expressions[0].walk()):
                raise ValueError("指标公式不能包含查询或写入语句")
        except sqlglot.errors.ParseError:
            raise ValueError("指标公式不符合声明方言语法") from None
        return self


class MetricRevisionRequest(BaseModel):
    """创建新定义版本时必须携带已读取的最新版本号。"""

    model_config = ConfigDict(extra="forbid")
    definition: MetricDefinition
    expected_version: int = Field(ge=0)


class MetricTransition(BaseModel):
    """审核动作与带时区的预定生效时间。"""

    model_config = ConfigDict(extra="forbid")
    action: Literal["submit", "approve", "reject"]
    effective_at: AwareDatetime | None = None
    note: str = Field(default="", max_length=2000)


# 方法作用：禁止匿名或平台身份进入租户业务口径，并限制写入角色。
# Args: identity - 显式请求身份；write - 是否需要写权限。
# Returns: 无返回值，无权时抛出 PermissionError。
def _authorize(identity: RequestIdentity, *, write: bool = False) -> None:
    roles = {"tenant_admin"} if write else {"tenant_admin", "analyst", "viewer"}
    if identity.tenant_id <= 0 or identity.user_id <= 0 or identity.role not in roles:
        raise PermissionError("指标口径由当前租户管理员维护，租户成员可读取已生效版本")


# 方法作用：把数据库 JSON 文本或对象统一还原为不共享引用的指标响应。
# Args: row - 数据库记录。
# Returns: 可 JSON 序列化的指标版本。
def _record(row: Any) -> dict[str, Any]:
    result = dict(row)
    value = result["definition"]
    result["definition"] = json.loads(value) if isinstance(value, str) else dict(value)
    result["reference"] = f"@metric({result['code']}@{result['version']})"
    return result


class MetricCatalog:
    """只在元数据库持久化指标；应用查询和 RLS 同时约束租户。"""

    # 方法作用：接受可选连接池以便使用隔离测试数据库。
    # Args: self - 实例；pool - 可选池或测试连接。
    # Returns: 无返回值。
    def __init__(self, pool: Any = None) -> None:
        self._pool = pool

    # 方法作用：在请求身份作用域内创建事务，确保版本操作原子性。
    # Args: self - 实例；identity - 请求身份。
    # Returns: 事务连接。
    @asynccontextmanager
    async def _connection(self, identity: RequestIdentity) -> AsyncIterator[Any]:
        pool = self._pool if self._pool is not None else await get_pg_pool()
        async with pg_pool_connection(pool, identity.tenant_id, identity.user_id, identity.role) as connection:
            async with connection.transaction():
                yield connection

    # 方法作用：串行化同一租户同一指标的创建和审核。
    # Args: self - 实例；connection - 事务连接；identity - 身份；code - 指标编码。
    # Returns: 无返回值。
    async def _lock(self, connection: Any, identity: RequestIdentity, code: str) -> None:
        await connection.fetchval(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"metric:{identity.tenant_id}:{code}",
        )

    # 方法作用：以乐观版本号保存不可变的新草稿，不覆盖任何旧定义。
    # Args: self - 实例；identity - 请求身份；request - 新定义和已读版本。
    # Returns: 已提交的新草稿。
    async def save_revision(self, identity: RequestIdentity, request: MetricRevisionRequest) -> dict:
        """所有保存都生成新版本，冲突交由调用方刷新后重试。"""
        _authorize(identity, write=True)
        definition = request.definition
        logger.debug("指标版本保存入口", tenant_id=identity.tenant_id, code=definition.code)
        async with self._connection(identity) as connection:
            await self._lock(connection, identity, definition.code)
            current = int(await connection.fetchval(
                "SELECT COALESCE(MAX(version), 0) FROM business_metrics WHERE tenant_id=$1 AND code=$2",
                identity.tenant_id, definition.code,
            ))
            if current != request.expected_version:
                raise MetricConflict("指标版本已变化，请刷新后重试")
            row = await connection.fetchrow(
                "INSERT INTO business_metrics (id,tenant_id,code,version,status,definition,created_by,created_at) "
                "VALUES ($1,$2,$3,$4,'draft',$5::jsonb,$6,$7) RETURNING *",
                str(uuid4()), identity.tenant_id, definition.code, current + 1,
                definition.model_dump_json(), identity.user_id, datetime.now(timezone.utc),
            )
        logger.info("指标新版本已保存", tenant_id=identity.tenant_id, code=definition.code, version=current + 1, actor=identity.user_id)
        return _record(row)

    # 方法作用：按当前状态执行提交或审核，并保存审核者和时间口径。
    # Args: self - 实例；identity - 身份；metric_id - 版本 ID；request - 审核动作。
    # Returns: 更新后的版本。
    async def transition(self, identity: RequestIdentity, metric_id: str, request: MetricTransition) -> dict:
        """旧版本不可重复审核，未来生效不会提前替换当前口径。"""
        _authorize(identity, write=True)
        logger.debug("指标状态变更入口", tenant_id=identity.tenant_id, action=request.action)
        expected, target = {"submit": ("draft", "pending"), "approve": ("pending", "approved"), "reject": ("pending", "rejected")}[request.action]
        async with self._connection(identity) as connection:
            row = await connection.fetchrow(
                "SELECT * FROM business_metrics WHERE tenant_id=$1 AND id=$2 FOR UPDATE", identity.tenant_id, metric_id,
            )
            if row is None:
                raise LookupError("指标版本不存在")
            await self._lock(connection, identity, row["code"])
            latest = await connection.fetchval(
                "SELECT MAX(version) FROM business_metrics WHERE tenant_id=$1 AND code=$2", identity.tenant_id, row["code"],
            )
            if row["status"] != expected or (request.action != "reject" and row["version"] != latest):
                raise MetricConflict("指标状态或版本已变化，不能执行该操作")
            effective_at = (request.effective_at or datetime.now(timezone.utc)).astimezone(timezone.utc) if target == "approved" else None
            updated = await connection.fetchrow(
                "UPDATE business_metrics SET status=$1, reviewed_by=$2, effective_at=$3, review_note=$4, reviewed_at=$5 "
                "WHERE tenant_id=$6 AND id=$7 RETURNING *", target,
                identity.user_id if request.action != "submit" else None,
                effective_at, request.note,
                datetime.now(timezone.utc) if request.action != "submit" else None,
                identity.tenant_id, metric_id,
            )
        logger.info("指标状态已变更", tenant_id=identity.tenant_id, metric_id=metric_id, status=target, actor=identity.user_id)
        return _record(updated)

    # 方法作用：分页读取每个编码的最新可见版本，并按编码和定义搜索。
    # Args: self - 实例；identity - 身份；query - 搜索词；limit/offset - 分页边界。
    # Returns: items 和 total。
    async def list_metrics(self, identity: RequestIdentity, *, query: str = "", limit: int = 20, offset: int = 0) -> dict:
        """管理员可见草稿，其他成员只读已生效的批准版本。"""
        _authorize(identity)
        if not 1 <= limit <= 100 or offset < 0 or len(query) > 128:
            raise ValueError("指标分页或搜索参数不合法")
        base = (
            "FROM business_metrics current WHERE tenant_id=$1 "
            "AND ($2 OR (status='approved' AND effective_at <= $3)) "
            "AND (LOWER(code) LIKE $4 OR LOWER(definition::text) LIKE $4) "
            "AND NOT EXISTS (SELECT 1 FROM business_metrics newer WHERE newer.tenant_id=current.tenant_id "
            "AND newer.code=current.code AND newer.version>current.version "
            "AND ($2 OR (newer.status='approved' AND newer.effective_at <= $3)))"
        )
        parameters = (identity.tenant_id, identity.role == "tenant_admin", datetime.now(timezone.utc), f"%{query.lower()}%")
        async with self._connection(identity) as connection:
            total = await connection.fetchval(f"SELECT COUNT(*) {base}", *parameters)
            rows = await connection.fetch(f"SELECT current.* {base} ORDER BY code LIMIT $5 OFFSET $6", *parameters, limit, offset)
        return {"items": [_record(row) for row in rows], "total": total}

    # 方法作用：读取指定编码的可见版本历史，不越过租户或审批边界。
    # Args: self - 实例；identity - 身份；code - 编码。
    # Returns: 按版本倒序的记录，最多 100 个。
    async def versions(self, identity: RequestIdentity, code: str) -> list[dict]:
        """保留历史定义供审核与结果追溯。"""
        _authorize(identity)
        async with self._connection(identity) as connection:
            rows = await connection.fetch(
                "SELECT * FROM business_metrics WHERE tenant_id=$1 AND code=$2 "
                "AND ($3 OR (status='approved' AND effective_at <= $4)) ORDER BY version DESC LIMIT 100",
                identity.tenant_id, code, identity.role == "tenant_admin", datetime.now(timezone.utc),
            )
        return [_record(row) for row in rows]

    # 方法作用：解析当前生效或显式指定的已生效版本，可校验执行数据源。
    # Args: self - 实例；identity - 身份；code - 编码；version - 固定版本或 0；datasource - 可选来源约束。
    # Returns: 已批准且已生效的版本，不匹配时抛出 LookupError。
    async def effective(self, identity: RequestIdentity, code: str, *, version: int = 0, datasource: str = "") -> dict:
        """固定版本引用仍须经过当前租户、批准和生效校验。"""
        _authorize(identity)
        async with self._connection(identity) as connection:
            row = await connection.fetchrow(
                "SELECT * FROM business_metrics WHERE tenant_id=$1 AND code=$2 AND status='approved' "
                "AND effective_at <= $3 AND ($4=0 OR version=$4) ORDER BY version DESC LIMIT 1",
                identity.tenant_id, code, datetime.now(timezone.utc), version,
            )
        if row is None:
            raise LookupError("指标不存在或尚未批准生效")
        result = _record(row)
        if datasource and result["definition"]["datasource"] != datasource:
            raise LookupError("指标口径不适用于当前数据源")
        return result


# 方法作用：返回当前应用隔离的指标目录实例，不在构造时连接数据库。
# Args: 无。
# Returns: 指标目录。
def get_metric_catalog() -> MetricCatalog:
    """数据库连接延迟到有明确指标操作时创建。"""
    from src.app_context import get_app_context

    return get_app_context().get_or_create("metric_catalog", MetricCatalog)


# 方法作用：将显式指标引用解析为已批准版本的口径文本与可追溯来源。
# Args: query - 问题；datasource - 当前授权来源；identity - 显式身份；catalog - 可选测试仓库。
# Returns: 口径文本和知识来源，未显式引用时不访问数据库。
async def resolve_metric_references(
    query: str, datasource: str, identity: RequestIdentity, *, catalog: MetricCatalog | None = None,
) -> tuple[str, list[dict]]:
    """错误引用失败关闭，不能偷偷省略用户指定的业务口径。"""
    if "@metric(" not in query:
        return "", []
    matches = re.findall(r"@metric\(([a-z][a-z0-9_]{0,63})(?:@([1-9][0-9]*))?\)", query)
    if len(matches) != query.count("@metric(") or len(matches) > 10:
        raise ValueError("指标引用格式不合法或超过 10 个")
    store = catalog or get_metric_catalog()
    texts: list[str] = []
    sources: list[dict] = []
    for code, requested_version in dict.fromkeys(matches):
        metric = await store.effective(identity, code, version=int(requested_version or 0), datasource=datasource)
        definition = metric["definition"]
        snapshot = {"metric_code": code, "version": metric["version"], **definition}
        texts.append("已批准指标口径：" + json.dumps(snapshot, ensure_ascii=False))
        sources.append({
            "source_id": f"metric:{identity.tenant_id}:{code}:v{metric['version']}",
            "category": "business_rule", "source": "metric_catalog",
            "source_file": definition.get("name", code), "table_name": "",
            "locator": snapshot, "relevance": 1.0, "trust_level": "tenant_approved_metric", "match_count": 1,
        })
    logger.info("指标口径引用已解析", tenant_id=identity.tenant_id, count=len(sources))
    return "\n".join(texts), sources
