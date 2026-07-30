"""自动化失败语义与日历月回归测试。"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
class TestAutomationFailureSemantics:
    """覆盖通知失败和月末调度。"""

    async def test_delivery_failure_does_not_advance_baseline(self) -> None:
        """配置渠道投递失败时不能记录成功运行或推进洞察基线。"""
        # Arrange
        from src.automation.models import ScheduleDefinition
        from src.automation.runner import ScheduledAnalysisRunner

        schedule = ScheduleDefinition(
            id="schedule-1",
            tenant_id=3,
            user_id=7,
            user_role="analyst",
            name="销售报告",
            kind="report",
            datasource="sales",
            sql="SELECT 1",
            dialect="postgres",
            frequency="daily",
            channels=["email"],
            recipient_email="owner@example.com",
            next_run_at=datetime.now(timezone.utc),
        )
        store = SimpleNamespace(
            latest_success_payload=AsyncMock(return_value=None),
            record_success=AsyncMock(),
            record_failure=AsyncMock(),
        )
        runner = ScheduledAnalysisRunner(
            store,
            executor=AsyncMock(return_value=[{"sales": 10}]),
            dispatcher=SimpleNamespace(
                deliver=AsyncMock(return_value={"email": "not_configured"}),
            ),
        )

        # Act
        result = await runner.run(schedule)

        # Assert
        assert result["success"] is False
        store.record_failure.assert_awaited_once()
        store.record_success.assert_not_awaited()

    async def test_monthly_schedule_clamps_to_month_end(self) -> None:
        """一月三十一日的月度任务应落在二月月末，不得漂移到三月。"""
        # Arrange
        from src.automation.store import calculate_next_run

        base = datetime(2024, 1, 31, 12, 30, tzinfo=timezone.utc)

        # Act
        result = calculate_next_run("monthly", base)

        # Assert
        assert result == datetime(2024, 2, 29, 12, 30, tzinfo=timezone.utc)
