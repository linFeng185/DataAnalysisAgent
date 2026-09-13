"""报表配额上限层级与超额提示回归。

覆盖真实运行暴露的两件事：
1. 「租户管理员的用户上限不得超过超级管理员配置的系统上限」此前未实现；
2. 超额提示只有一句"配额不足"，无法定位要调哪一项。
"""

from __future__ import annotations

import pytest


class TestQuotaErrorDescription:
    """验证超额提示带维度、上限与本次需求。"""

    # 方法作用：验证完整数值信息转成可定位的提示。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_describes_dimension_limit_and_request(self) -> None:
        """用户要能直接对上「去后台调哪一项、该调到多少」。"""
        # Arrange
        from src.reporting.quota import QuotaExceededError, describe_quota_error

        error = QuotaExceededError("raw", field="queries", limit=16, requested=47)

        # Act
        message = describe_quota_error(error)

        # Assert
        assert "单次查询上限" in message
        assert "16" in message
        assert "47" in message

    # 方法作用：验证缺失数值信息时回退通用文案而不是报错。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_falls_back_without_details(self) -> None:
        """旧构造方式（只有字符串）仍需可用。"""
        # Arrange
        from src.reporting.quota import QuotaExceededError, describe_quota_error

        # Act / Assert
        assert describe_quota_error(QuotaExceededError("QUOTA_EXCEEDED: share_links")) == "本周期报表配额不足，请联系租户管理员"
        assert describe_quota_error(QuotaExceededError("x", field="unknown_field", limit=5, requested=9)) == "本周期报表配额不足，请联系租户管理员"

    # 方法作用：验证每个配额维度都有中文标签。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_every_dimension_has_a_label(self) -> None:
        """缺标签会让提示退回通用文案，失去定位能力。

        键名是 QuotaManager.reserve 里 checks 元组的第三项（运行时检查名），
        不是 QuotaPolicy 的字段名（max_queries_per_run 那种），两者命名不同。
        """
        # Arrange
        from src.reporting.quota import QUOTA_FIELD_LABELS

        # Act：与 reserve 的 checks 元组保持一致的运行时检查名
        check_names = {
            "jobs_created", "runs_started", "concurrent_runs", "analysis_units",
            "queries", "rows", "llm_tokens", "share_links",
        }

        # Assert
        assert check_names <= set(QUOTA_FIELD_LABELS)


class TestReserveCarriesDetails:
    """验证预占拒绝时异常携带具体数值。"""

    # 方法作用：验证超限抛出带维度的异常。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reserve_raises_with_details(self) -> None:
        """没有这些字段，上层拼不出可定位的提示。"""
        # Arrange
        from src.reporting.quota import QuotaExceededError, QuotaManager, QuotaPolicy

        quota = QuotaManager(default_policy=QuotaPolicy(max_queries_per_run=16))
        identity = type("Identity", (), {"tenant_id": 1, "user_id": 1, "role": "tenant_admin"})()

        # Act / Assert
        with pytest.raises(QuotaExceededError) as excinfo:
            await quota.reserve(identity, runs=1, units=1, queries=47)
        assert excinfo.value.field == "queries"
        assert excinfo.value.limit == 16
        assert excinfo.value.requested == 47


class TestPersistedPathRejection:
    """验证 PostgreSQL 持久化路径的拒绝也带具体数值。

    真实运行暴露：该分支原先只抛 "QUOTA_EXCEEDED: persisted quota"，
    内存分支改了、PG 分支没改，提示因此仍退回通用文案。而用户跑的是 PG。
    """

    # 方法作用：构造一个"原子检查拒绝"的持久化 Store 替身。
    # Args: usage - get_quota_usage 的返回；raise_on_usage - 是否让用量查询抛错。
    # Returns: 具备最小契约的假 Store。
    @staticmethod
    def _store(usage: dict | None = None, *, raise_on_usage: bool = False):
        class FakeStore:
            _memory = False

            async def get_quota_policy(self, tenant_id, user_id):
                return None

            async def reserve_quota(self, identity, increments, policy, start, end):
                # 原子检查拒绝：真实实现里 UPDATE ... WHERE 不匹配时返回 None。
                return None

            async def get_quota_usage(self, identity, period_start):
                if raise_on_usage:
                    raise RuntimeError("数据库不可用")
                return dict(usage or {})

        return FakeStore()

    # 方法作用：验证 PG 路径拒绝时提示指明维度与数值。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_persisted_rejection_explains_dimension(self) -> None:
        """用户要看到「哪一项、上限多少、本次需要多少」。"""
        # Arrange
        from src.reporting.quota import (
            QuotaExceededError,
            QuotaManager,
            QuotaPolicy,
            describe_quota_error,
        )

        quota = QuotaManager(store=self._store(), default_policy=QuotaPolicy(max_queries_per_run=16))
        identity = type("Identity", (), {"tenant_id": 3, "user_id": 3, "role": "tenant_admin"})()

        # Act / Assert
        with pytest.raises(QuotaExceededError) as excinfo:
            await quota.reserve(identity, runs=1, units=1, queries=27)
        assert excinfo.value.field == "queries"
        assert excinfo.value.limit == 16
        assert excinfo.value.requested == 27
        assert "27" in describe_quota_error(excinfo.value)
        assert "16" in describe_quota_error(excinfo.value)

    # 方法作用：验证用量快照不可用时拒绝本身不受影响。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_persisted_rejection_survives_usage_failure(self) -> None:
        """复算只是为了让提示更好读，不能因此改变拒绝结果或抛出别的异常。"""
        # Arrange
        from src.reporting.quota import (
            QuotaExceededError,
            QuotaManager,
            QuotaPolicy,
            describe_quota_error,
        )

        quota = QuotaManager(store=self._store(raise_on_usage=True), default_policy=QuotaPolicy(max_queries_per_run=16))
        identity = type("Identity", (), {"tenant_id": 3, "user_id": 3, "role": "tenant_admin"})()

        # Act / Assert：仍然抛配额异常，只是提示退回通用文案
        with pytest.raises(QuotaExceededError) as excinfo:
            await quota.reserve(identity, runs=1, units=1, queries=27)
        assert describe_quota_error(excinfo.value) == "本周期报表配额不足，请联系租户管理员"

    # 方法作用：验证快照字段名与复算代码一致。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_snapshot_fields_used_by_explanation_exist(self) -> None:
        """写错字段名会被 except 吞掉并静默退回通用文案，这条断言防止再犯。"""
        # Arrange
        from src.reporting.quota import QuotaSnapshot

        # Act
        fields = set(QuotaSnapshot.__dataclass_fields__)

        # Assert
        assert {"jobs_remaining", "runs_remaining", "concurrent_remaining", "tokens_remaining", "shares_remaining"} <= fields


class TestSystemLimitEnforcement:
    """验证系统上限真正生效：租户管理员不得越界配置。"""

    # 方法作用：验证系统上限来自配置且覆盖关键维度。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_system_limits_come_from_settings(self) -> None:
        """此前该配置项从未被任何代码读取。"""
        # Arrange
        from src.api.routes.report_workbench import _system_quota_limits
        from src.config import get_settings

        # Act
        limits = _system_quota_limits()

        # Assert
        settings = get_settings()
        assert limits["max_queries_per_run"] == int(settings.report_max_queries_per_run)
        assert limits["max_analysis_units_per_run"] == int(settings.report_max_units_per_run)
        assert limits["max_concurrent_runs"] == int(settings.report_max_concurrency)
        assert set(limits) == {"max_analysis_units_per_run", "max_queries_per_run", "max_concurrent_runs", "max_page_bytes"}

    # 方法作用：验证越界配置被拒绝且提示指明上限。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_over_ceiling_is_rejected_with_label(self) -> None:
        """提示要指明是哪一项、上限多少。"""
        # Arrange
        from fastapi import HTTPException

        from src.api.routes.report_workbench import (
            _system_quota_limits,
            _validate_within_system_limits,
        )

        ceiling = _system_quota_limits()["max_queries_per_run"]

        # Act / Assert
        with pytest.raises(HTTPException) as excinfo:
            _validate_within_system_limits({"max_queries_per_run": ceiling + 1})
        assert excinfo.value.status_code == 422
        assert "单次查询上限" in excinfo.value.detail
        assert str(ceiling) in excinfo.value.detail

    # 方法作用：验证等于上限的配置被接受（边界）。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_at_ceiling_is_accepted(self) -> None:
        """上限本身必须可用，否则等于把整档砍掉。"""
        # Arrange
        from src.api.routes.report_workbench import (
            _system_quota_limits,
            _validate_within_system_limits,
        )

        limits = _system_quota_limits()

        # Act / Assert：不抛异常即通过
        _validate_within_system_limits(dict(limits))

    # 方法作用：验证未配置系统上限的字段不受影响。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_fields_without_ceiling_pass(self) -> None:
        """jobs/runs/tokens 等没有系统上限配置项，不应被误拦。"""
        # Arrange
        from src.api.routes.report_workbench import _validate_within_system_limits

        # Act / Assert
        _validate_within_system_limits({"max_jobs_created": 99999, "max_llm_tokens": 9_000_000})
