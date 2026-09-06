"""报表工作台交互、分享、配额和存储测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class TestSnapshotDrilldown:
    """覆盖功能 23.6：快照 Drilldown 仅访问签发字段。"""

    # 方法作用：验证快照过滤和分页只返回 Drilldown 契约允许的字段。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_snapshot_drilldown_filters_and_paginates(self) -> None:
        """合法 eq 过滤应返回脱敏且有界的明细。"""
        # Arrange
        from src.reporting.drilldown import execute_snapshot_drilldown

        definition = {
            "drilldown_id": "dd-1",
            "mode": "snapshot",
            "allowed_filters": [{"field": "region", "operator": "eq", "value_type": "string"}],
            "columns": ["region", "sales"],
            "max_rows": 2,
            "data_ref": "data:sales",
        }
        manifest = {"data:sales": [
            {"region": "华东", "sales": 10, "secret": "x"},
            {"region": "华南", "sales": 20, "secret": "y"},
            {"region": "华东", "sales": 30, "secret": "z"},
        ]}

        # Act
        result = execute_snapshot_drilldown(definition, manifest, {"region": "华东"}, page=1, page_size=10)

        # Assert
        assert result["total"] == 2
        assert result["rows"] == [{"region": "华东", "sales": 10}, {"region": "华东", "sales": 30}]

    # 方法作用：阻止客户端提交未签发字段或任意过滤操作符。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_snapshot_drilldown_rejects_unknown_filter(self) -> None:
        """未知字段和 gt 操作符不能穿透到快照数据。"""
        # Arrange
        from src.reporting.drilldown import execute_snapshot_drilldown

        definition = {
            "drilldown_id": "dd-1", "mode": "snapshot", "allowed_filters": [],
            "columns": ["region"], "max_rows": 10, "data_ref": "data:sales",
        }

        # Act / Assert
        with pytest.raises(ValueError, match="过滤|字段"):
            execute_snapshot_drilldown(definition, {"data:sales": [{"region": "华东"}]}, {"secret": "x"}, 1, 10)


class TestReportQuota:
    """覆盖功能 23.8：菜单能力和周期配额。"""

    # 方法作用：验证用户配额在周期内达到上限后阻断新任务。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_quota_reservation_is_atomic_in_memory(self) -> None:
        """第二次超出任务上限的预占应返回 QUOTA_EXCEEDED。"""
        # Arrange
        from src.reporting.quota import QuotaExceededError, QuotaManager, QuotaPolicy

        manager = QuotaManager(default_policy=QuotaPolicy(max_jobs_created=1, max_runs_started=1))
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")

        # Act
        first = await manager.reserve(identity, jobs=1)

        # Assert
        assert first.jobs_remaining == 0
        with pytest.raises(QuotaExceededError, match="QUOTA_EXCEEDED"):
            await manager.reserve(identity, jobs=1)

    # 方法作用：验证默认角色能力和管理员覆盖能力的边界。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_permission_capabilities(self) -> None:
        """analyst 默认可创建，viewer 默认不可创建，显式覆盖可授予菜单。"""
        # Arrange
        from src.reporting.quota import ReportPermissionService

        service = ReportPermissionService()

        # Act / Assert
        assert await service.has(SimpleNamespace(role="analyst", user_id=7, tenant_id=3), "report.create")
        assert not await service.has(SimpleNamespace(role="viewer", user_id=8, tenant_id=3), "report.create")
        service.set_user_capabilities(8, ["report.menu", "report.create"])
        assert await service.has(SimpleNamespace(role="viewer", user_id=8, tenant_id=3), "report.create")

    # 方法作用：验证 PostgreSQL 用量释放委托给持久化 Store，避免并发槽泄漏。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_persistent_concurrency_release_is_delegated(self) -> None:
        """非内存 Store 应收到并发槽释放调用。"""
        # Arrange
        from src.reporting.quota import QuotaManager

        class PersistentStore:
            _memory = False

            def __init__(self) -> None:
                self.calls: list[tuple[int, str]] = []

            # 方法作用：记录持久化并发槽释放请求。
            # Args: identity - 当前身份；amount - 释放数量；timezone_name - 周期时区。
            # Returns: 异步完成后无返回值。
            async def release_concurrent_quota(self, identity, amount=1, timezone_name="Asia/Shanghai", period_start=None) -> None:
                self.calls.append((amount, timezone_name))

        store = PersistentStore()
        manager = QuotaManager(store=store)
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")

        # Act
        await manager.release_concurrent(identity, amount=2, timezone_name="Asia/Shanghai")

        # Assert
        assert store.calls == [(2, "Asia/Shanghai")]


class TestReportStore:
    """覆盖功能 23.2、23.7、23.10：内存 Store 的幂等、运行与审计。"""

    # 方法作用：验证 SQLite 持久化模拟认领后会推进任务状态，避免重复认领同一任务。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_sqlite_claim_updates_status_with_application_lock(self) -> None:
        """SQLite 方言不使用 SKIP LOCKED，但必须通过条件更新完成认领。"""
        # Arrange
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.store import ReportStore

        class FakeTransaction:
            # 方法作用：提供连接事务上下文的测试替身。
            # Args: self - 测试事务实例。
            # Returns: 异步上下文管理器自身。
            async def __aenter__(self):
                return self

            # 方法作用：结束测试事务上下文。
            # Args: self - 测试事务实例；exc_type/exc/value - 异常信息。
            # Returns: False 让异常继续传播。
            async def __aexit__(self, exc_type, exc, value):
                return False

        class FakeConnection:
            # 方法作用：初始化一个只含单条到期任务的 SQLite 连接替身。
            # Args: self - 测试连接实例。
            # Returns: 无返回值。
            def __init__(self) -> None:
                self.row = {
                    "id": "job-1", "tenant_id": 3, "user_id": 7, "prompt": "销售报表",
                    "plan_json": ReportPlan(analysis_units=[AnalysisUnit(question="销售")]).model_dump(mode="json"),
                    "status": "queued", "runs_started": 0,
                }
                self.update_calls = 0

            # 方法作用：接收 RLS 身份初始化语句，不执行真实数据库操作。
            # Args: self - 测试连接；query - SQL；args - 参数。
            # Returns: 无返回值。
            async def execute(self, query, *args):
                return "OK"

            # 方法作用：返回当前仍处于 queued 且到期的任务。
            # Args: self - 测试连接；query - SQL；args - 参数。
            # Returns: 到期任务行列表。
            async def fetch(self, query, *args):
                return [dict(self.row)] if self.row["status"] == "queued" else []

            # 方法作用：执行条件状态更新并返回成功认领的任务行。
            # Args: self - 测试连接；query - SQL；args - 任务 ID。
            # Returns: 更新后的任务行或 None。
            async def fetchrow(self, query, *args):
                self.update_calls += 1
                if self.row["status"] != "queued":
                    return None
                self.row["status"] = "running"
                return dict(self.row)

            # 方法作用：提供事务上下文。
            # Args: self - 测试连接实例。
            # Returns: FakeTransaction。
            def transaction(self):
                return FakeTransaction()

        class FakeAcquire:
            # 方法作用：提供连接池 acquire 上下文。
            # Args: connection - 测试连接。
            # Returns: 无返回值。
            def __init__(self, connection) -> None:
                self.connection = connection

            # 方法作用：进入连接池上下文。
            # Args: self - acquire 上下文。
            # Returns: 测试连接。
            async def __aenter__(self):
                return self.connection

            # 方法作用：退出连接池上下文。
            # Args: self - acquire 上下文；exc_type/exc/value - 异常信息。
            # Returns: False 让异常继续传播。
            async def __aexit__(self, exc_type, exc, value):
                return False

        class FakePool:
            # 方法作用：初始化带单连接的测试连接池。
            # Args: self - 测试连接池实例。
            # Returns: 无返回值。
            def __init__(self) -> None:
                self.connection = FakeConnection()

            # 方法作用：借出测试连接。
            # Args: self - 测试连接池实例。
            # Returns: acquire 上下文。
            def acquire(self):
                return FakeAcquire(self.connection)

        pool = FakePool()
        store = ReportStore(pg_pool=pool, dialect="sqlite", memory=False)

        # Act
        first = await store.claim_due_jobs()
        second = await store.claim_due_jobs()

        # Assert
        assert [item.id for item in first] == ["job-1"]
        assert second == []
        assert pool.connection.update_calls == 1

    # 方法作用：验证同一幂等键不会重复创建报表任务。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_job_idempotency_and_step_logging(self) -> None:
        """重复提交返回同一个 Job，步骤同时保留用户错误和内部错误字段。"""
        # Arrange
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.store import ReportStore

        store = ReportStore()
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        plan = ReportPlan(analysis_units=[AnalysisUnit(question="销售趋势")])

        # Act
        first = await store.create_job(identity, "生成销售报表", plan, "req-1")
        second = await store.create_job(identity, "生成销售报表", plan, "req-1")
        run = await store.create_run(first.id, identity, attempt=1)
        await store.append_step(run.id, "report_plan_queries", "failed", error_code="QUERY_FAILED", internal_error="connection secret", user_error="查询暂时失败")
        steps = await store.list_steps(run.id, identity)

        # Assert
        assert first.id == second.id
        assert len(steps) == 1
        assert steps[0]["user_error_message"] == "查询暂时失败"
        assert steps[0]["internal_error_message"] == "connection secret"

    # 方法作用：验证任务可以读取其最新不可变报表版本，供前端运行完成后跳转。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_latest_version_lookup_by_job(self) -> None:
        """同一任务有多个版本时应返回版本号最大的可见版本。"""
        # Arrange
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.store import ReportStore

        store = ReportStore()
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        plan = ReportPlan(analysis_units=[AnalysisUnit(question="销售趋势")])
        job = await store.create_job(identity, "生成销售报表", plan, "latest-version")
        first_run = await store.create_run(job.id, identity)
        second_run = await store.create_run(job.id, identity, attempt=2)
        first = await store.persist_version(
            job.id,
            first_run.id,
            identity,
            {"report_id": "report-latest", "version": 1, "sections": []},
            {},
            None,
        )
        second = await store.persist_version(
            job.id,
            second_run.id,
            identity,
            {"report_id": "report-latest", "version": 2, "sections": []},
            {},
            None,
        )

        # Act
        latest = await store.get_latest_version_for_job(job.id, identity)

        # Assert
        assert first.version == 1
        assert second.version == 2
        assert latest is not None
        assert latest.version == 2
