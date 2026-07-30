"""自动化通知分发与调度时间测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
class TestAutomationDelivery:
    """覆盖功能 19.1、19.2 的渠道隔离和频率计算。"""

    # 方法作用：验证已注入外发渠道逐个调用且站内通知无需外部配置。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_dispatcher_delivers_configured_channels_independently(self) -> None:
        """一个渠道的发送器不应接触其他渠道配置。"""
        # Arrange
        from src.automation.delivery import NotificationDispatcher
        from src.automation.models import ScheduleDefinition

        email = AsyncMock()
        feishu = AsyncMock()
        schedule = ScheduleDefinition(
            id="s1", tenant_id=3, user_id=7, user_role="analyst",
            name="日报", kind="report", datasource="sales", sql="SELECT 1",
            dialect="postgres", frequency="daily",
            channels=["in_app", "email", "feishu"],
            next_run_at=datetime.now(timezone.utc),
        )
        dispatcher = NotificationDispatcher(
            settings=SimpleNamespace(),
            senders={"email": email, "feishu": feishu},
        )

        # Act
        result = await dispatcher.deliver(schedule, "标题", "正文")

        # Assert
        assert result == {"in_app": "success", "email": "success", "feishu": "success"}
        email.assert_awaited_once_with("标题", "正文")
        feishu.assert_awaited_once_with("标题", "正文")

    # 方法作用：验证外发配置缺失时记录状态而不让整次报告失败。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_dispatcher_marks_unconfigured_channel(self) -> None:
        """默认部署应保留站内报告并明确外发未配置。"""
        # Arrange
        from src.automation.delivery import NotificationDispatcher
        from src.automation.models import ScheduleDefinition

        schedule = ScheduleDefinition(
            id="s1", tenant_id=3, user_id=7, user_role="analyst",
            name="日报", kind="report", datasource="sales", sql="SELECT 1",
            dialect="postgres", frequency="daily", channels=["in_app", "slack"],
            next_run_at=datetime.now(timezone.utc),
        )
        dispatcher = NotificationDispatcher(
            settings=SimpleNamespace(automation_slack_webhook_url=""),
        )

        # Act
        result = await dispatcher.deliver(schedule, "标题", "正文")

        # Assert
        assert result == {"in_app": "success", "slack": "not_configured"}

    # 方法作用：验证所有频率都计算为严格晚于基准的 UTC 时间。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        ("frequency", "seconds"),
        [("hourly", 3600), ("daily", 86400), ("weekly", 604800), ("monthly", 2592000)],
    )
    async def test_calculate_next_run(self, frequency: str, seconds: int) -> None:
        """固定频率不依赖服务器本地时区。"""
        # Arrange
        from src.automation.store import calculate_next_run

        base = datetime(2026, 7, 29, tzinfo=timezone.utc)

        # Act / Assert
        result = calculate_next_run(frequency, base)
        if frequency == "monthly":
            assert result.year == 2026 and result.month == 8 and result.day == 29
        else:
            assert (result - base).total_seconds() == seconds


@pytest.mark.asyncio
class TestAutomationStore:
    """覆盖功能 19.1、19.2 的 PostgreSQL 调度存储边界。"""

    # 方法作用：验证到期任务认领 SQL 合法关联 users 且使用 SKIP LOCKED。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_list_due_uses_valid_update_from_clause(self, monkeypatch) -> None:
        """目标表别名只能在 WHERE 中与 FROM 表关联。"""
        # Arrange
        import src.memory.pg_pool as pg_pool_module
        from src.automation.store import AutomationStore

        connection = SimpleNamespace(fetch=AsyncMock(return_value=[]))

        @asynccontextmanager
        # 方法作用：为 Store 测试提供不访问真实 PostgreSQL 的连接上下文。
        # Args: pool - 测试 Pool；tenant_id/user_id/role - RLS 身份。
        # Returns: Fake connection 异步上下文。
        async def fake_connection(pool, tenant_id, user_id, role):
            del pool, tenant_id, user_id, role
            yield connection

        monkeypatch.setattr(pg_pool_module, "pg_pool_connection", fake_connection)
        store = AutomationStore(pg_pool=object())

        # Act
        result = await store.list_due(limit=20)

        # Assert
        assert result == []
        query = connection.fetch.await_args.args[0]
        assert "FOR UPDATE OF due_schedule SKIP LOCKED" in query
        assert "JOIN users due_user ON due_user.id=due_schedule.user_id" in query
        assert "due_user.is_active=TRUE" in query
        assert "FROM due, users u WHERE s.id=due.id AND u.id=s.user_id" in query
        assert connection.fetch.await_args.args[1] == 20

    # 方法作用：验证任务创建使用请求身份并返回包含角色的领域模型。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_create_persists_identity_and_schedule(self, monkeypatch) -> None:
        """API 传入的 tenant/user 不能被请求体覆盖。"""
        # Arrange
        import src.memory.pg_pool as pg_pool_module
        from src.automation.store import AutomationStore

        now = datetime.now(timezone.utc)
        row = {
            "id": "7e152d59-80c6-4327-bb5f-21ec3b351328",
            "tenant_id": 3,
            "user_id": 7,
            "user_role": "analyst",
            "name": "销售日报",
            "kind": "report",
            "datasource": "sales",
            "sql_text": "SELECT 1",
            "dialect": "postgres",
            "frequency": "daily",
            "threshold_pct": 10,
            "channels": ["in_app"],
            "recipient_email": "",
            "enabled": True,
            "next_run_at": now,
            "last_run_at": None,
        }
        connection = SimpleNamespace(fetchrow=AsyncMock(return_value=row))

        @asynccontextmanager
        # 方法作用：为创建测试提供 Fake PostgreSQL 连接。
        # Args: pool - 测试 Pool；tenant_id/user_id/role - RLS 身份。
        # Returns: Fake connection 异步上下文。
        async def fake_connection(pool, tenant_id, user_id, role):
            del pool
            assert (tenant_id, user_id, role) == (3, 7, "analyst")
            yield connection

        monkeypatch.setattr(pg_pool_module, "pg_pool_connection", fake_connection)
        store = AutomationStore(pg_pool=object())
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        request = SimpleNamespace(
            name="销售日报", kind="report", datasource="sales", sql="SELECT 1",
            frequency="daily", threshold_pct=10, channels=["in_app"],
            recipient_email="",
        )

        # Act
        schedule = await store.create(identity, request, "postgres")

        # Assert
        assert schedule.tenant_id == 3
        assert schedule.user_id == 7
        assert schedule.user_role == "analyst"
        args = connection.fetchrow.await_args.args
        assert args[2:4] == (3, 7)

    # 方法作用：验证站内通知列表始终限定当前用户并限制分页上限。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_list_notifications_is_user_scoped(self, monkeypatch) -> None:
        """租户管理员也不能读取同租户其他用户的个人通知。"""
        # Arrange
        import src.memory.pg_pool as pg_pool_module
        from src.automation.store import AutomationStore

        connection = SimpleNamespace(fetch=AsyncMock(return_value=[]))

        @asynccontextmanager
        # 方法作用：为通知查询测试提供 Fake PostgreSQL 连接。
        # Args: pool - 测试 Pool；tenant_id/user_id/role - RLS 身份。
        # Returns: Fake connection 异步上下文。
        async def fake_connection(pool, tenant_id, user_id, role):
            del pool
            assert (tenant_id, user_id, role) == (3, 7, "tenant_admin")
            yield connection

        monkeypatch.setattr(pg_pool_module, "pg_pool_connection", fake_connection)
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin")

        # Act
        await AutomationStore(pg_pool=object()).list_notifications(identity, limit=999)

        # Assert
        args = connection.fetch.await_args.args
        assert "tenant_id=$1 AND user_id=$2" in args[0]
        assert args[1:] == (3, 7, 100)

    # 方法作用：验证任务列表、单项读取和删除都把当前身份传入 RLS 连接。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_identity_crud_queries_are_rls_scoped(self, monkeypatch) -> None:
        """三个管理方法都不能使用系统身份绕过租户边界。"""
        # Arrange
        import src.memory.pg_pool as pg_pool_module
        from src.automation.store import AutomationStore

        connection = SimpleNamespace(
            fetch=AsyncMock(return_value=[]),
            fetchrow=AsyncMock(return_value=None),
            execute=AsyncMock(return_value="DELETE 0"),
        )
        identities: list[tuple[int, int, str]] = []

        @asynccontextmanager
        # 方法作用：记录每次 Store 查询采用的 RLS 身份。
        # Args: pool - 测试 Pool；tenant_id/user_id/role - RLS 身份。
        # Returns: Fake connection 异步上下文。
        async def fake_connection(pool, tenant_id, user_id, role):
            del pool
            identities.append((tenant_id, user_id, role))
            yield connection

        monkeypatch.setattr(pg_pool_module, "pg_pool_connection", fake_connection)
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        store = AutomationStore(pg_pool=object())

        # Act
        listed = await store.list_for_identity(identity)
        loaded = await store.get_for_identity("7e152d59-80c6-4327-bb5f-21ec3b351328", identity)
        deleted = await store.delete("7e152d59-80c6-4327-bb5f-21ec3b351328", identity)

        # Assert
        assert listed == []
        assert loaded is None
        assert deleted is False
        assert identities == [(3, 7, "analyst")] * 3
        assert "u.is_active=TRUE" in connection.fetchrow.await_args.args[0]

    # 方法作用：验证成功基线、成功运行/通知和失败运行都写入系统级 Store 边界。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_run_payload_methods_persist_success_and_failure(self, monkeypatch) -> None:
        """运行记录方法应覆盖基线读取、通知事务和失败摘要。"""
        # Arrange
        import src.memory.pg_pool as pg_pool_module
        from src.automation.models import ScheduleDefinition
        from src.automation.store import AutomationStore

        @asynccontextmanager
        # 方法作用：模拟 record_success 使用的数据库事务。
        # Args: 无。
        # Returns: 空事务异步上下文。
        async def fake_transaction():
            yield None

        connection = SimpleNamespace(
            fetchval=AsyncMock(return_value={"metrics": {"sales": 100}}),
            execute=AsyncMock(return_value="INSERT 0 1"),
            transaction=MagicMock(side_effect=fake_transaction),
        )

        @asynccontextmanager
        # 方法作用：为运行记录测试提供系统级 Fake PostgreSQL 连接。
        # Args: pool - 测试 Pool；tenant_id/user_id/role - RLS 身份。
        # Returns: Fake connection 异步上下文。
        async def fake_connection(pool, tenant_id, user_id, role):
            del pool
            assert (tenant_id, user_id, role) == (0, 0, "super_admin")
            yield connection

        monkeypatch.setattr(pg_pool_module, "pg_pool_connection", fake_connection)
        schedule = ScheduleDefinition(
            id="7e152d59-80c6-4327-bb5f-21ec3b351328",
            tenant_id=3, user_id=7, user_role="analyst", name="销售日报",
            kind="report", datasource="sales", sql="SELECT 1", dialect="postgres",
            frequency="daily", next_run_at=datetime.now(timezone.utc),
        )
        store = AutomationStore(pg_pool=object())

        # Act
        baseline = await store.latest_success_payload(schedule.id)
        await store.record_success(
            schedule,
            {"success": True, "metrics": {"sales": 120}},
            {"kind": "report", "title": "销售日报", "body": "正文"},
        )
        await store.record_failure(schedule, "database unavailable")

        # Assert
        assert baseline == {"metrics": {"sales": 100}}
        assert connection.transaction.call_count == 1
        assert connection.execute.await_count == 3
        failure_args = connection.execute.await_args.args
        assert failure_args[-1] == "database unavailable"


class TestAutomationMigration:
    """覆盖功能 19.1、19.2 的 PostgreSQL RLS 迁移契约。"""

    # 方法作用：验证自动化三张租户表对表所有者也强制执行 RLS。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_automation_tables_force_row_level_security(self) -> None:
        """仅 ENABLE RLS 不能阻止表所有者绕过租户策略。"""
        # Arrange
        migration = "\n".join(
            Path(path).read_text(encoding="utf-8")
            for path in (
                "migrations/009_automation.sql",
                "migrations/010_automation_force_rls.sql",
            )
        )

        # Act / Assert
        for table in (
            "analysis_schedules", "analysis_schedule_runs", "analysis_notifications",
        ):
            assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;" in migration
