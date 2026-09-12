"""报表运行计数统计回归，覆盖 23.21。"""

import asyncio
from dataclasses import fields

import pytest

from src.reporting.run_stats import (
    RunStats,
    activate,
    current,
    deactivate,
    record_llm_call,
    record_sql_execution,
)


class TestRunStatsCounter:
    """验证 23.21 的计数器语义、作用域与 asyncio 上下文传播。"""

    # 方法作用：确认未激活计数器时记录调用是空操作。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_record_is_noop_without_active_counter(self) -> None:
        """聊天、自动化等非报表路径不应因埋点抛错或产生副作用。"""
        # Arrange / Act / Assert
        assert current() is None
        record_llm_call()
        record_llm_call(failed=True, tokens=100)
        record_sql_execution()
        record_sql_execution(failed=True)
        assert current() is None

    # 方法作用：确认 LLM 调用与失败次数分别累计。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_llm_call_and_failure_counted(self) -> None:
        """成功与失败各记若干次后计数正确。"""
        # Arrange
        stats, token = activate()
        try:
            # Act
            record_llm_call()
            record_llm_call()
            record_llm_call(failed=True)
        finally:
            deactivate(token)

        # Assert
        assert stats.llm_call_count == 3
        assert stats.llm_failure_count == 1

    # 方法作用：确认 token 用量累加 prompt 与 completion 两部分。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_token_usage_accumulates_both_prompt_and_completion(self) -> None:
        """token 用量按回调给出的总额累加。"""
        # Arrange
        stats, token = activate()
        try:
            # Act
            record_llm_call(tokens=120)
            record_llm_call(tokens=80)
        finally:
            deactivate(token)

        # Assert
        assert stats.llm_token_usage == 200

    # 方法作用：确认 SQL 成功与失败分别累计。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_sql_execution_success_and_failure_counted(self) -> None:
        """每次执行都计入总数，失败再单独计入失败数。"""
        # Arrange
        stats, token = activate()
        try:
            # Act
            record_sql_execution()
            record_sql_execution()
            record_sql_execution(failed=True)
        finally:
            deactivate(token)

        # Assert
        assert stats.sql_query_count == 3
        assert stats.sql_failure_count == 1
        assert stats.sql_success_count == 2

    # 方法作用：确认计数在 asyncio 子任务内仍然写回同一对象。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_counter_survives_asyncio_task_copy(self) -> None:
        """contextvars 在 create_task 时复制；可变对象的引用必须被共享。

        报表单元执行会经 app.ainvoke 触发 LangGraph 内部建 task，
        若计数器被写成不可变结构，这里的计数会静默丢失。
        """
        # Arrange
        stats, token = activate()

        async def worker() -> None:
            record_sql_execution()
            record_llm_call()

        try:
            # Act
            await asyncio.create_task(worker())
        finally:
            deactivate(token)

        # Assert
        assert stats.sql_query_count == 1
        assert stats.llm_call_count == 1

    # 方法作用：确认计数器是可变对象且字段齐全。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_counter_is_mutable_object_with_all_fields(self) -> None:
        """防止把 RunStats 改成不可变结构导致 SQL 计数静默丢失。"""
        # Arrange
        stats = RunStats()

        # Act
        stats.sql_query_count += 1
        stats.llm_token_usage += 50

        # Assert
        assert stats.sql_query_count == 1
        assert stats.llm_token_usage == 50
        assert {item.name for item in fields(RunStats)} >= {
            "llm_call_count",
            "llm_failure_count",
            "llm_token_usage",
            "sql_query_count",
            "sql_success_count",
            "sql_failure_count",
        }

    # 方法作用：确认激活与重置可嵌套且不泄漏。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_activate_and_reset_restores_previous(self) -> None:
        """嵌套作用域退出后回到外层计数器。"""
        # Arrange
        outer, outer_token = activate()
        try:
            # Act
            inner, inner_token = activate()
            record_llm_call()
            assert current() is inner
            deactivate(inner_token)

            # Assert
            assert current() is outer
            assert outer.llm_call_count == 0
        finally:
            deactivate(outer_token)
        assert current() is None


class TestLLMCallbackRunStats:
    """验证 23.21 的 LLM 回调按挂载时捕获的计数器计数。"""

    # 方法作用：构造一次完整的成功 LLM 回调序列。
    # Args: callback - 被测回调；run_id - 调用标识；prompt/completion - token 数。
    # Returns: 无返回值。
    def _fire_success(self, callback, run_id, prompt: int, completion: int) -> None:
        from types import SimpleNamespace

        callback.on_llm_start(
            {"name": "fake-model"},
            ["prompt"],
            run_id=run_id,
            invocation_params={"model_name": "fake-model"},
        )
        callback.on_llm_end(
            SimpleNamespace(llm_output={"token_usage": {"prompt_tokens": prompt, "completion_tokens": completion}}),
            run_id=run_id,
        )

    # 方法作用：确认成功调用计入次数与 token。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_llm_callback_records_to_captured_stats(self) -> None:
        """回调必须把终态写入挂载时捕获的 RunStats，不依赖 contextvar 传播。"""
        # Arrange
        from prometheus_client import CollectorRegistry
        from uuid import uuid4

        from src.observability import LLMMetricsCallback, MetricsRegistry

        stats = RunStats()
        callback = LLMMetricsCallback(MetricsRegistry(CollectorRegistry()), task="generate_sql", stats=stats)

        # Act
        self._fire_success(callback, uuid4(), prompt=10, completion=4)
        self._fire_success(callback, uuid4(), prompt=1, completion=2)

        # Assert
        assert stats.llm_call_count == 2
        assert stats.llm_failure_count == 0
        assert stats.llm_token_usage == 17

    # 方法作用：确认失败调用计入失败数并同样计入总次数。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_llm_callback_records_failure(self) -> None:
        """失败是真实发生的调用，必须计入总次数与失败数。"""
        # Arrange
        from prometheus_client import CollectorRegistry
        from uuid import uuid4

        from src.observability import LLMMetricsCallback, MetricsRegistry

        stats = RunStats()
        callback = LLMMetricsCallback(MetricsRegistry(CollectorRegistry()), task="generate_sql", stats=stats)
        run_id = uuid4()

        # Act
        callback.on_llm_start({"name": "fake"}, ["p"], run_id=run_id, invocation_params={})
        callback.on_llm_error(RuntimeError("boom"), run_id=run_id)

        # Assert
        assert stats.llm_call_count == 1
        assert stats.llm_failure_count == 1

    # 方法作用：确认无计数器时回调不计数也不抛错。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_llm_callback_is_noop_without_stats(self) -> None:
        """聊天等非报表路径共用同一回调，必须零影响。"""
        # Arrange
        from prometheus_client import CollectorRegistry
        from uuid import uuid4

        from src.observability import LLMMetricsCallback, MetricsRegistry

        callback = LLMMetricsCallback(MetricsRegistry(CollectorRegistry()), task="generate_sql")

        # Act
        self._fire_success(callback, uuid4(), prompt=3, completion=1)

        # Assert
        assert callback.stats is None

    # 方法作用：确认 attach_llm_metrics 会捕获当前活动的报表计数器。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_attach_llm_metrics_captures_active_run_stats(self) -> None:
        """模型在报表运行上下文内创建时，回调必须拿到该次运行的计数器。"""
        # Arrange
        from src.observability import attach_llm_metrics
        from uuid import uuid4

        class _FakeModel:
            def __init__(self) -> None:
                self.callbacks: list[object] = []

        stats, token = activate()
        try:
            # Act
            model = attach_llm_metrics(_FakeModel(), "report_plan")
            callback = model.callbacks[0]
            self._fire_success(callback, uuid4(), prompt=5, completion=5)
        finally:
            deactivate(token)

        # Assert
        assert callback.stats is stats
        assert stats.llm_call_count == 1
        assert stats.llm_token_usage == 10


class TestReportRunRecordStats:
    """验证 23.21 的运行记录计数汇总。"""

    # 方法作用：确认成功数由总数与失败数推导。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_stats_summary_derives_success_counts(self) -> None:
        """页面需要同时看到预估与实际，且成功数不单独存列。"""
        # Arrange
        from src.reporting.models import ReportRunRecord

        record = ReportRunRecord(
            id="run-1",
            job_id="job-1",
            query_count=6,
            llm_call_count=9,
            llm_failure_count=2,
            token_usage=1234,
            sql_query_count=4,
            sql_success_count=3,
            sql_failure_count=1,
        )

        # Act
        summary = record.stats_summary()

        # Assert
        assert summary["llm_success_count"] == 7
        assert summary["llm_token_usage"] == 1234
        assert summary["estimated_query_count"] == 6
        assert summary["sql_query_count"] == 4

    # 方法作用：确认失败数大于总次数时不出现负数成功数。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_stats_summary_clamps_negative_success(self) -> None:
        """防御脏数据导致界面出现负数。"""
        # Arrange
        from src.reporting.models import ReportRunRecord

        record = ReportRunRecord(id="run-2", job_id="job-2", llm_call_count=1, llm_failure_count=3)

        # Act / Assert
        assert record.stats_summary()["llm_success_count"] == 0


class TestRunStatsMigration:
    """验证 23.21 的迁移声明了全部真实计数列。"""

    # 方法作用：确认迁移包含五列真实计数且不改动配额估算列语义。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_migration_declares_real_count_columns(self) -> None:
        """缺列会让计数落库静默失败。"""
        # Arrange
        from pathlib import Path

        source = Path("migrations/029_report_run_stats.sql").read_text(encoding="utf-8")

        # Act / Assert
        for column in (
            "llm_call_count",
            "llm_failure_count",
            "sql_query_count",
            "sql_success_count",
            "sql_failure_count",
        ):
            assert column in source
        assert "ALTER TABLE report_runs" in source
        # query_count 是配额口径的估算列，迁移只能新增 sql_query_count，不得改动原列。
        assert "ADD COLUMN IF NOT EXISTS query_count" not in source
        assert "ADD COLUMN IF NOT EXISTS token_usage" not in source
