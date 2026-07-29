"""主动洞察与定时报告 API 测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
class TestAutomationApi:
    """覆盖功能 19.1、19.2 的任务、手动运行和通知 API。"""

    # 方法作用：构造 API 测试使用的调度领域模型。
    # Args: 无。
    # Returns: ScheduleDefinition。
    @staticmethod
    def _schedule():
        from src.automation.models import ScheduleDefinition

        return ScheduleDefinition(
            id="7e152d59-80c6-4327-bb5f-21ec3b351328",
            tenant_id=3,
            user_id=7,
            user_role="analyst",
            name="销售日报",
            kind="report",
            datasource="sales",
            sql="SELECT SUM(amount) AS sales FROM orders",
            dialect="postgres",
            frequency="daily",
            channels=["in_app"],
            next_run_at=datetime.now(timezone.utc),
        )

    # 方法作用：验证创建 API 完成只读校验后用当前身份持久化任务。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_create_schedule_validates_and_persists(self, monkeypatch) -> None:
        """合法任务应返回 201 契约所需的完整调度摘要。"""
        # Arrange
        import src.api.routes.automation as route
        from src.api.schemas import AutomationScheduleCreateRequest

        schedule = self._schedule()
        store = SimpleNamespace(create=AsyncMock(return_value=schedule))
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_automation_store", lambda: store)
        monkeypatch.setattr(route, "_validate_schedule_request", AsyncMock(return_value="postgres"))
        request = AutomationScheduleCreateRequest(
            name="销售日报",
            kind="report",
            datasource="sales",
            sql="SELECT SUM(amount) AS sales FROM orders",
            frequency="daily",
            channels=["in_app"],
        )

        # Act
        result = await route.create_automation_schedule(request)

        # Assert
        assert result["id"] == schedule.id
        assert result["kind"] == "report"
        route._validate_schedule_request.assert_awaited_once_with(identity, request)
        store.create.assert_awaited_once_with(identity, request, "postgres")

    # 方法作用：验证创建 API 在写入前阻断危险 SQL。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_create_schedule_rejects_non_readonly_sql(self, monkeypatch) -> None:
        """DELETE 等写操作不能进入 PostgreSQL 调度表。"""
        # Arrange
        import src.api.routes.automation as route
        from src.api.schemas import AutomationScheduleCreateRequest

        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        store = SimpleNamespace(create=AsyncMock())
        registry = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(dialect="postgres")))
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_automation_store", lambda: store)
        monkeypatch.setattr(route, "get_registry", lambda: registry)
        monkeypatch.setattr(
            route,
            "get_tenant_policy",
            lambda: SimpleNamespace(datasource_isolation_enabled=False),
        )
        request = AutomationScheduleCreateRequest(
            name="危险任务",
            kind="report",
            datasource="sales",
            sql="DELETE FROM orders",
            frequency="daily",
        )

        # Act / Assert
        with pytest.raises(HTTPException) as error:
            await route.create_automation_schedule(request)
        assert error.value.status_code == 422
        store.create.assert_not_awaited()

    # 方法作用：验证列表和删除 API 复用 Store 的身份隔离。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_list_and_delete_schedule_use_current_identity(self, monkeypatch) -> None:
        """路由不能接受调用方提交 tenant_id 或 user_id。"""
        # Arrange
        import src.api.routes.automation as route

        schedule = self._schedule()
        store = SimpleNamespace(
            list_for_identity=AsyncMock(return_value=[schedule]),
            delete=AsyncMock(return_value=True),
        )
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_automation_store", lambda: store)

        # Act
        listed = await route.list_automation_schedules()
        deleted = await route.delete_automation_schedule(schedule.id)

        # Assert
        assert listed["total"] == 1
        assert deleted == {"status": "deleted"}
        store.list_for_identity.assert_awaited_once_with(identity)
        store.delete.assert_awaited_once_with(schedule.id, identity)

    # 方法作用：验证立即运行只能执行当前身份可管理的任务。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_run_schedule_now_rechecks_visibility(self, monkeypatch) -> None:
        """未知或越权 UUID 应返回 404，不能把任意任务交给 Runner。"""
        # Arrange
        import src.api.routes.automation as route

        schedule = self._schedule()
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        store = SimpleNamespace(get_for_identity=AsyncMock(return_value=schedule))
        runner = SimpleNamespace(run=AsyncMock(return_value={"success": True, "row_count": 2}))
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_automation_store", lambda: store)
        monkeypatch.setattr(route, "get_scheduled_analysis_runner", lambda: runner)

        # Act
        result = await route.run_automation_schedule(schedule.id)

        # Assert
        assert result == {"success": True, "row_count": 2}
        store.get_for_identity.assert_awaited_once_with(schedule.id, identity)
        runner.run.assert_awaited_once_with(schedule)

    # 方法作用：验证站内通知 API 只返回当前用户的有界结果。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_list_notifications_uses_current_identity(self, monkeypatch) -> None:
        """limit 应由 Store 二次限制且响应包含总数。"""
        # Arrange
        import src.api.routes.automation as route

        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        notifications = [{"id": "n1", "title": "日报"}]
        store = SimpleNamespace(list_notifications=AsyncMock(return_value=notifications))
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_automation_store", lambda: store)

        # Act
        result = await route.list_automation_notifications(limit=25)

        # Assert
        assert result == {"notifications": notifications, "total": 1}
        store.list_notifications.assert_awaited_once_with(identity, 25)
