"""报表页面生命周期测试，覆盖 23.13。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.reporting.models import AnalysisUnit, ReportPlan
from src.reporting.store import ReportStore


class TestReportLifecycle:
    """覆盖 23.13 的过期恢复、分享边界和租户隔离。"""

    # 方法作用：验证延期只恢复页面，不自动扩大既有分享授权。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_extend_expired_page_keeps_share_deadline(self) -> None:
        """过期但未清理的页面可以恢复，旧分享仍保持过期。"""
        store = ReportStore(memory=True)
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        plan = ReportPlan(analysis_units=[AnalysisUnit(unit_id="sales", question="销量")])
        job = await store.create_job(identity, "销量", plan)
        expired = datetime.now(timezone.utc) - timedelta(days=2)
        version = await store.persist_version(job.id, "run", identity, {"report_id": "report", "expires_at": expired.isoformat()}, {}, expired)
        store._shares["share"] = {"report_version_id": version.id, "expires_at": expired}

        updated = await store.update_page_lifecycle("report", identity, days=7)

        assert updated.expires_at > datetime.now(timezone.utc) + timedelta(days=6)
        assert updated.document["expires_at"] == updated.expires_at.isoformat()
        assert store._shares["share"]["expires_at"] == expired
        assert updated.checksum != version.checksum

    # 方法作用：验证永久转有限期限时同步收紧分享到期时间。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_permanent_page_can_return_to_finite_expiry(self) -> None:
        """收紧页面期限不得留下超过页面寿命的分享。"""
        store = ReportStore(memory=True)
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin")
        version = await store.persist_version("job", "run", identity, {"report_id": "report"}, {}, None)
        store._shares["share"] = {"report_version_id": version.id, "expires_at": None}

        updated = await store.update_page_lifecycle("report", identity, days=30)
        permanent = await store.update_page_lifecycle("report", identity, permanent=True)

        assert store._shares["share"]["expires_at"] == updated.expires_at
        assert permanent.expires_at is None
        assert permanent.document["expires_at"] is None

    # 方法作用：验证越权、已清理页面和非法参数不能修改生命周期。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_lifecycle_rejects_missing_and_cross_tenant(self) -> None:
        """租户管理员不能修改其他租户页面。"""
        from src.reporting.models import PageLifecycleRequest

        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        await store.persist_version("job", "run", owner, {"report_id": "report"}, {}, None)
        stranger = SimpleNamespace(tenant_id=4, user_id=8, role="tenant_admin")

        assert await store.update_page_lifecycle("report", stranger, days=7) is None
        assert await store.update_page_lifecycle("missing", owner, days=7) is None
        for payload in ({}, {"days": 0}, {"days": 91}, {"days": 7, "permanent": True}):
            with pytest.raises(ValidationError):
                PageLifecycleRequest(**payload)
