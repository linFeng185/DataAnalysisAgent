"""覆盖 11.1.13、18.6.1、18.6.2 的租户指标生命周期。"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from src.knowledge.metric_catalog import (
    MetricCatalog,
    MetricConflict,
    MetricDefinition,
    MetricRevisionRequest,
    MetricTransition,
)
from src.security.tenant_policy import RequestIdentity


class MetricConnection:
    """以 SQLite 真正执行参数化读写，仅替换 PG 的类型转换和锁语法。"""

    # 方法作用：创建独立测试表和事务互斥锁。
    # Args: self - 连接替身。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self.database = sqlite3.connect(":memory:", isolation_level=None)
        self.database.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        self.database.execute("""CREATE TABLE business_metrics (
            id TEXT PRIMARY KEY, tenant_id INTEGER, code TEXT, version INTEGER,
            status TEXT, definition TEXT, created_by INTEGER, reviewed_by INTEGER,
            created_at TEXT, reviewed_at TEXT, effective_at TEXT, review_note TEXT,
            UNIQUE(tenant_id, code, version))""")

    # 方法作用：提供真实提交和回滚语义，串行化同连接并发操作。
    # Args: self - 连接替身。
    # Returns: 事务上下文。
    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        async with self.lock:
            self.database.execute("BEGIN")
            try:
                yield
                self.database.execute("COMMIT")
            except Exception:
                self.database.execute("ROLLBACK")
                raise

    # 方法作用：移除 SQLite 不支持且在测试事务中由互斥锁模拟的 PG 语法。
    # Args: self - 实例；query - 参数化 SQL；args - 参数。
    # Returns: SQLite 游标。
    def _execute(self, query: str, args: tuple[Any, ...]) -> sqlite3.Cursor:
        query = query.replace("::jsonb", "").replace("::text", "").replace(" FOR UPDATE", "")
        parameters = tuple(value.isoformat() if isinstance(value, datetime) else value for value in args)
        logging.getLogger(__name__).info("指标数据库适配探针 sql=%s parameter_count=%s parameter_type=%s", query, len(parameters), type(parameters).__name__)
        return self.database.execute(query, {str(index + 1): value for index, value in enumerate(parameters)})

    # 方法作用：执行单行查询或模拟 advisory lock。
    # Args: self - 实例；query - SQL；args - 参数。
    # Returns: 第一列值。
    async def fetchval(self, query: str, *args: Any) -> Any:
        if "pg_advisory_xact_lock" in query:
            return None
        row = self._execute(query, args).fetchone()
        return row[0] if row else None

    # 方法作用：读取一条 SQL 结果。
    # Args: self - 实例；query - SQL；args - 参数。
    # Returns: 行字典或 None。
    async def fetchrow(self, query: str, *args: Any) -> dict | None:
        row = self._execute(query, args).fetchone()
        return dict(row) if row else None

    # 方法作用：读取完整的当前分页结果。
    # Args: self - 实例；query - SQL；args - 参数。
    # Returns: 行字典列表。
    async def fetch(self, query: str, *args: Any) -> list[dict]:
        return [dict(row) for row in self._execute(query, args).fetchall()]


# 方法作用：提供无需网络的指标仓库并关闭测试连接。
# Args: 无。
# Returns: 指标仓库实例。
@pytest.fixture
def catalog() -> Any:
    connection = MetricConnection()
    try:
        yield MetricCatalog(pool=connection)
    finally:
        connection.database.close()


# 方法作用：创建参数完整的指标版本请求。
# Args: version - 期望当前版本；dialect - 测试方言。
# Returns: 待保存请求。
def revision(version: int = 0, dialect: str = "postgres") -> MetricRevisionRequest:
    return MetricRevisionRequest(expected_version=version, definition=MetricDefinition(
        code="gmv", name="支付金额", datasource="sales", dialect=dialect,
        expression="SUM(paid_amount)", description="按支付时间统计", unit="元",
    ))


ADMIN = RequestIdentity(tenant_id=3, user_id=7, role="tenant_admin")
VIEWER = RequestIdentity(tenant_id=3, user_id=8, role="viewer")


class TestMetricCatalog:
    """覆盖 11.1.13、18.6.1、18.6.2 的正常、边界、冲突与隔离。"""

    # 方法作用：验证保存、提交、批准、检索、分页与历史闭环。
    # Args: self - 实例；catalog - 仓库。
    # Returns: 无返回值。
    async def test_lifecycle_and_effective_reference(self, catalog) -> None:
        """未审核草稿不可被普通用户引用，批准后返回固定版本引用。"""
        draft = await catalog.save_revision(ADMIN, revision())
        assert (await catalog.list_metrics(VIEWER))["total"] == 0
        pending = await catalog.transition(ADMIN, draft["id"], MetricTransition(action="submit"))
        approved = await catalog.transition(ADMIN, draft["id"], MetricTransition(action="approve"))
        active = await catalog.effective(VIEWER, "gmv", datasource="sales")
        assert pending["status"] == "pending"
        assert approved["status"] == "approved"
        assert approved["reviewed_at"]
        assert active["reference"] == "@metric(gmv@1)"
        assert (await catalog.list_metrics(VIEWER, query="支付"))["total"] == 1
        assert (await catalog.versions(ADMIN, "gmv"))[0]["version"] == 1

    # 方法作用：验证新版本不覆盖已批准记录，旧版本持续生效。
    # Args: self - 实例；catalog - 仓库。
    # Returns: 无返回值。
    async def test_revision_conflict_and_future_effective(self, catalog) -> None:
        """未来版本未生效时返回旧版；过期编辑和重复审核均拒绝。"""
        first = await catalog.save_revision(ADMIN, revision())
        await catalog.transition(ADMIN, first["id"], MetricTransition(action="submit"))
        await catalog.transition(ADMIN, first["id"], MetricTransition(action="approve"))
        second = await catalog.save_revision(ADMIN, revision(1))
        await catalog.transition(ADMIN, second["id"], MetricTransition(action="submit"))
        await catalog.transition(ADMIN, second["id"], MetricTransition(
            action="approve", effective_at=datetime.now(timezone.utc) + timedelta(days=2),
        ))
        assert (await catalog.effective(VIEWER, "gmv"))["version"] == 1
        with pytest.raises(LookupError):
            await catalog.effective(VIEWER, "gmv", version=2)
        with pytest.raises(MetricConflict):
            await catalog.save_revision(ADMIN, revision(1))
        with pytest.raises(MetricConflict):
            await catalog.transition(ADMIN, first["id"], MetricTransition(action="approve"))

    # 方法作用：验证跨租户访问、平台超管和普通成员写入均被拒绝。
    # Args: self - 实例；catalog - 仓库。
    # Returns: 无返回值。
    async def test_tenant_and_role_boundaries(self, catalog) -> None:
        """业务指标由租户管理，平台身份不能代管或跨租户读取。"""
        draft = await catalog.save_revision(ADMIN, revision())
        foreign = RequestIdentity(tenant_id=9, user_id=10, role="tenant_admin")
        assert (await catalog.list_metrics(foreign))["total"] == 0
        with pytest.raises(LookupError):
            await catalog.transition(foreign, draft["id"], MetricTransition(action="submit"))
        for role in ("super_admin", "analyst", "viewer"):
            with pytest.raises(PermissionError):
                await catalog.save_revision(RequestIdentity(tenant_id=3, user_id=8, role=role), revision(1))

    # 方法作用：验证并发提交同一预期版本只保存一个新版本。
    # Args: self - 实例；catalog - 仓库。
    # Returns: 无返回值。
    async def test_concurrent_revision_has_single_winner(self, catalog) -> None:
        """乐观版本锁不允许静默覆盖其他管理员修改。"""
        results = await asyncio.gather(
            catalog.save_revision(ADMIN, revision()), catalog.save_revision(ADMIN, revision()),
            return_exceptions=True,
        )
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, MetricConflict) for result in results) == 1

    # 方法作用：验证合法公式在支持的三个数据库方言下都可解析。
    # Args: self - 实例；dialect - 方言。
    # Returns: 无返回值。
    @pytest.mark.parametrize("dialect", ["clickhouse", "mysql", "postgres"])
    def test_expression_dialects(self, dialect) -> None:
        """口径保存时进行方言语法校验，但不会直接执行公式。"""
        assert revision(dialect=dialect).definition.expression == "SUM(paid_amount)"

    # 方法作用：验证完整查询、写语句、多语句及空公式不被当作表达式保存。
    # Args: self - 实例；expression - 非法公式。
    # Returns: 无返回值。
    @pytest.mark.parametrize("expression", ["", "SELECT 1", "SUM(amount); DROP TABLE orders", "DELETE FROM orders", "(SELECT password FROM users)", "DROP TABLE orders", "ALTER TABLE orders ADD COLUMN total INT", "CREATE TABLE sample (id INT)", "SET role = 'superuser'", "GRANT SELECT ON orders TO PUBLIC"])
    def test_invalid_expression_rejected(self, expression) -> None:
        """指标表达式不能成为执行任意 SQL 的入口。"""
        with pytest.raises(ValidationError):
            MetricDefinition(code="gmv", name="GMV", datasource="sales", expression=expression)
