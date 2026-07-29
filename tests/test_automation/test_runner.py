"""主动洞察和定时报告 Runner 测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
class TestAutomationRunner:
    """覆盖功能 19.1、19.2 的检测、报告、投递和调度。"""

    # 方法作用：构造测试调度定义。
    # Args: kind - insight 或 report；channels - 投递渠道。
    # Returns: ScheduleDefinition。
    @staticmethod
    def _schedule(kind: str, channels: list[str] | None = None):
        from src.automation.models import ScheduleDefinition

        return ScheduleDefinition(
            id="schedule-1",
            tenant_id=3,
            user_id=7,
            user_role="analyst",
            name="销售监控",
            kind=kind,
            datasource="sales",
            sql="SELECT region, SUM(amount) AS sales FROM orders GROUP BY region",
            dialect="postgres",
            frequency="daily",
            threshold_pct=10,
            channels=channels or ["in_app"],
            recipient_email="owner@example.com",
            enabled=True,
            next_run_at=datetime.now(timezone.utc),
        )

    # 方法作用：验证指标汇总和阈值洞察保留变化方向与基准。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_detect_insights_compares_current_with_previous_metrics(self) -> None:
        """超过阈值的增长和下降都应生成可审计事件。"""
        # Arrange
        from src.automation.runner import detect_insights

        # Act
        events = detect_insights(
            {"sales": 120, "orders": 80},
            {"sales": 100, "orders": 100},
            threshold_pct=10,
        )

        # Assert
        assert [(event.metric, event.direction) for event in events] == [
            ("orders", "down"),
            ("sales", "up"),
        ]
        assert events[1].change_pct == 20.0

    # 方法作用：验证主动洞察首次运行只建立基线，后续异常才投递通知。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_insight_runner_establishes_baseline_then_notifies(self) -> None:
        """没有上次成功数据时不能把所有指标误报为异常。"""
        # Arrange
        from src.automation.runner import ScheduledAnalysisRunner

        schedule = self._schedule("insight")
        store = SimpleNamespace(
            latest_success_payload=AsyncMock(side_effect=[None, {"metrics": {"sales": 100}}]),
            record_success=AsyncMock(),
            record_failure=AsyncMock(),
        )
        executor = AsyncMock(side_effect=[
            [{"region": "华东", "sales": 100}],
            [{"region": "华东", "sales": 130}],
        ])
        dispatcher = SimpleNamespace(deliver=AsyncMock(return_value={"in_app": "success"}))
        runner = ScheduledAnalysisRunner(store, executor=executor, dispatcher=dispatcher)

        # Act
        baseline = await runner.run(schedule)
        changed = await runner.run(schedule)

        # Assert
        assert baseline["notification"] is None
        assert changed["notification"]["kind"] == "insight"
        assert changed["events"][0]["change_pct"] == 30.0
        dispatcher.deliver.assert_awaited_once()
        assert store.record_success.await_count == 2
        store.record_failure.assert_not_awaited()

    # 方法作用：验证定时报告生成 Markdown 并向配置渠道投递。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_report_runner_renders_and_delivers_markdown(self) -> None:
        """报告应包含标题、指标和结果表格，且不依赖真实 LLM。"""
        # Arrange
        from src.automation.runner import ScheduledAnalysisRunner

        schedule = self._schedule("report", ["in_app", "email"])
        store = SimpleNamespace(
            latest_success_payload=AsyncMock(return_value=None),
            record_success=AsyncMock(),
            record_failure=AsyncMock(),
        )
        dispatcher = SimpleNamespace(deliver=AsyncMock(return_value={
            "in_app": "success", "email": "success",
        }))
        runner = ScheduledAnalysisRunner(
            store,
            executor=AsyncMock(return_value=[
                {"region": "华东", "sales": 120},
                {"region": "华南", "sales": 80},
            ]),
            dispatcher=dispatcher,
        )

        # Act
        result = await runner.run(schedule)

        # Assert
        report = result["report"]
        assert "# 销售监控" in report
        assert "| region | sales |" in report
        assert "sales: 200" in report
        dispatcher.deliver.assert_awaited_once()
        assert result["delivery"]["email"] == "success"

    # 方法作用：验证执行失败只记录失败状态且不投递陈旧结果。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_runner_records_failure_without_delivery(self) -> None:
        """数据库失败不能复用上次结果生成新报告。"""
        # Arrange
        from src.automation.runner import ScheduledAnalysisRunner

        schedule = self._schedule("report")
        store = SimpleNamespace(
            latest_success_payload=AsyncMock(),
            record_success=AsyncMock(),
            record_failure=AsyncMock(),
        )
        dispatcher = SimpleNamespace(deliver=AsyncMock())
        runner = ScheduledAnalysisRunner(
            store,
            executor=AsyncMock(side_effect=RuntimeError("database unavailable")),
            dispatcher=dispatcher,
        )

        # Act
        result = await runner.run(schedule)

        # Assert
        assert result["success"] is False
        store.record_failure.assert_awaited_once()
        store.record_success.assert_not_awaited()
        dispatcher.deliver.assert_not_awaited()

    # 方法作用：验证周期服务只执行到期且启用的任务并保持单轮隔离。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_service_run_once_executes_due_schedules(self) -> None:
        """一个任务失败不应阻断同批其他任务。"""
        # Arrange
        from src.automation.service import AutomationService

        schedules = [self._schedule("insight"), self._schedule("report")]
        store = SimpleNamespace(list_due=AsyncMock(return_value=schedules))
        runner = SimpleNamespace(run=AsyncMock(side_effect=[
            {"success": False}, {"success": True},
        ]))
        service = AutomationService(store, runner, interval_seconds=60)

        # Act
        result = await service.run_once()

        # Assert
        assert result == {"due": 2, "succeeded": 1, "failed": 1}
        assert runner.run.await_count == 2
