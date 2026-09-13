"""链接管理与分享访问统计测试，覆盖需求 3（C 批 030 迁移）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI

import src.api.routes.report_workbench as route
from src.logging_config import get_logger
from src.reporting.models import AnalysisUnit, ReportPlan
from src.reporting.sharing import hash_share_token
from src.reporting.store import ReportStore

logger = get_logger(__name__)
PREFIX = "/report-workbench"


# 方法作用：构造一份带任务与版本的公开分享，供访问上报和列表用例复用。
# Args: store - 内存 Store；identity - 分享创建者身份；token - 明文分享 token；code_hash - 授权码哈希；prompt - 任务描述（标题摘要来源）；report_id - 报表标识；expires_at - 到期时间；revoked - 是否直接置为已撤销。
# Returns: 内存分享记录字典。
async def _create_share(
    store: ReportStore,
    identity: Any,
    token: str,
    *,
    code_hash: str = "",
    prompt: str = "季度销售分析",
    report_id: str | None = None,
    expires_at: datetime | None = None,
    revoked: bool = False,
) -> dict[str, Any]:
    logger.debug("测试分享构造入口", token=token, prompt=prompt)
    plan = ReportPlan(analysis_units=[AnalysisUnit(unit_id="sales", question="销量")])
    job = await store.create_job(identity, prompt, plan)
    resolved_report_id = report_id or str(uuid4())
    version = await store.persist_version(job.id, "run", identity, {"report_id": resolved_report_id}, {}, None)
    record = {
        "report_version_id": version.id,
        "report_id": resolved_report_id,
        "version": version.version,
        "tenant_id": int(identity.tenant_id),
        "created_by": int(identity.user_id),
        "scope": "public",
        "user_scope": "none",
        "user_ids": [],
        "allow_drilldown": False,
        "allow_download": False,
        "pin_latest": False,
        "expires_at": expires_at if expires_at is not None else datetime.now(timezone.utc) + timedelta(days=7),
    }
    share = await store.create_share(record, hash_share_token(token), code_hash)
    if revoked:
        store._shares[share.id]["revoked_at"] = datetime.now(timezone.utc)
    logger.info("测试分享构造完成", share_id=share.id, token=token)
    return store._shares[share.id]


# 方法作用：把路由模块的全局依赖替换为指定内存 Store 与当前身份。
# Args: monkeypatch - pytest 补丁工具；store - 内存 Store；identity - 当前请求身份。
# Returns: 无返回值。
def _bind(monkeypatch, store: ReportStore, identity: Any) -> None:
    logger.debug("测试路由依赖绑定入口", tenant_id=identity.tenant_id, user_id=identity.user_id)
    monkeypatch.setattr(route, "get_current_identity", lambda: identity)
    monkeypatch.setattr(route, "get_report_store", lambda: store)


# 方法作用：构造只挂载报表工作台路由的测试客户端。
# Args: 无。
# Returns: httpx 异步客户端。
def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(route.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestShareVisitReportApi:
    """覆盖访问上报、停留时长与访问者列表接口。"""

    # 方法作用：验证开启身份记录时登录用户按 user 记录并写入 user_id。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_visit_report_records_logged_in_user(self, monkeypatch) -> None:
        """登录身份访问分享时 visitor_kind 为 user 且落库真实 user_id。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        await _create_share(store, owner, "token-user")
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            response = await client.post(f"{PREFIX}/shared/reports/token-user/visits", json={"unlocked": True})

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["visitor_kind"] == "user"
        record = store._share_visits[body["visit_id"]]
        assert record["user_id"] == 7
        assert record["unlocked"] is True
        assert len(body["visit_id"]) == 36

    # 方法作用：验证匿名请求按 anonymous 记录且不写用户标识。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_visit_report_records_anonymous_visitor(self, monkeypatch) -> None:
        """未登录访问按匿名记录，user_id 保持 0。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        await _create_share(store, owner, "token-anon")
        anonymous = SimpleNamespace(tenant_id=1, user_id=0, role="anonymous")
        _bind(monkeypatch, store, anonymous)

        # Act
        async with _client() as client:
            response = await client.post(f"{PREFIX}/shared/reports/token-anon/visits", json={})

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["visitor_kind"] == "anonymous"
        assert store._share_visits[body["visit_id"]]["user_id"] == 0

    # 方法作用：验证租户关闭身份记录开关后登录身份也降级为匿名。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_visit_report_respects_tenant_identity_switch(self, monkeypatch) -> None:
        """record_share_visitor_identity 为 False 时不写 user_id。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        await _create_share(store, owner, "token-switch")
        store._tenant_settings[3] = {"record_share_visitor_identity": False, "max_permanent_reports_per_user": 0}
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            response = await client.post(f"{PREFIX}/shared/reports/token-switch/visits", json={})

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["visitor_kind"] == "anonymous"
        assert store._share_visits[body["visit_id"]]["user_id"] == 0

    # 方法作用：验证无效、撤销与过期 token 的上报语义与分享读取一致。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_visit_report_token_states(self, monkeypatch) -> None:
        """不存在或已撤销返回 404，已过期返回 410。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        await _create_share(store, owner, "token-revoked", revoked=True)
        await _create_share(store, owner, "token-expired", expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            missing = await client.post(f"{PREFIX}/shared/reports/token-missing/visits", json={})
            revoked = await client.post(f"{PREFIX}/shared/reports/token-revoked/visits", json={})
            expired = await client.post(f"{PREFIX}/shared/reports/token-expired/visits", json={})

        # Assert
        assert missing.status_code == 404
        assert revoked.status_code == 404
        assert expired.status_code == 410

    # 方法作用：验证停留时长按累积值幂等覆盖而不是逐次累加。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_visit_duration_overwrites_cumulative_value(self, monkeypatch) -> None:
        """第二次上报更小的累积值时结果必须是第二次的值。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        await _create_share(store, owner, "token-duration")
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            created = await client.post(f"{PREFIX}/shared/reports/token-duration/visits", json={})
            visit_id = created.json()["visit_id"]
            first = await client.patch(f"{PREFIX}/shared/reports/token-duration/visits/{visit_id}", json={"duration_seconds": 83})
            second = await client.patch(f"{PREFIX}/shared/reports/token-duration/visits/{visit_id}", json={"duration_seconds": 30})

        # Assert
        assert first.status_code == 200
        assert first.json() == {"visit_id": visit_id, "duration_seconds": 83}
        assert second.json()["duration_seconds"] == 30
        record = store._share_visits[visit_id]
        assert record["duration_seconds"] == 30
        assert record["last_seen_at"] >= record["started_at"]

    # 方法作用：验证负数时长与非法请求体被契约层拒绝。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_visit_duration_rejects_negative_value(self, monkeypatch) -> None:
        """duration_seconds 为负返回 422，不写入任何记录。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        await _create_share(store, owner, "token-negative")
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            created = await client.post(f"{PREFIX}/shared/reports/token-negative/visits", json={})
            visit_id = created.json()["visit_id"]
            negative = await client.patch(f"{PREFIX}/shared/reports/token-negative/visits/{visit_id}", json={"duration_seconds": -1})

        # Assert
        assert negative.status_code == 422
        assert store._share_visits[visit_id]["duration_seconds"] == 0

    # 方法作用：验证访问记录归属校验，跨 token 上报必须 404。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_visit_duration_rejects_cross_token_visit(self, monkeypatch) -> None:
        """visit_id 属于其他分享时返回 404 且不修改时长。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        await _create_share(store, owner, "token-a")
        await _create_share(store, owner, "token-b")
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            created = await client.post(f"{PREFIX}/shared/reports/token-a/visits", json={})
            visit_id = created.json()["visit_id"]
            cross = await client.patch(f"{PREFIX}/shared/reports/token-b/visits/{visit_id}", json={"duration_seconds": 12})

        # Assert
        assert cross.status_code == 404
        assert store._share_visits[visit_id]["duration_seconds"] == 0

    # 方法作用：验证访问者列表只返回系统用户且受分享可见范围限制。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_share_visitors_list_only_system_users(self, monkeypatch) -> None:
        """列表只含 user 类型的访问，非创建者返回 404。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        share = await _create_share(store, owner, "token-visitors")
        store._usernames[7] = "analyst1"
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            created = await client.post(f"{PREFIX}/shared/reports/token-visitors/visits", json={})
            visit_id = created.json()["visit_id"]
            await client.patch(f"{PREFIX}/shared/reports/token-visitors/visits/{visit_id}", json={"duration_seconds": 83})
            anonymous = SimpleNamespace(tenant_id=1, user_id=0, role="anonymous")
            _bind(monkeypatch, store, anonymous)
            await client.post(f"{PREFIX}/shared/reports/token-visitors/visits", json={})
            _bind(monkeypatch, store, owner)
            listed = await client.get(f"{PREFIX}/shares/{share['id']}/visits")
            _bind(monkeypatch, store, SimpleNamespace(tenant_id=3, user_id=8, role="analyst"))
            stranger = await client.get(f"{PREFIX}/shares/{share['id']}/visits")

        # Assert
        assert listed.status_code == 200
        body = listed.json()
        assert body["total"] == 1
        assert body["visits"][0]["user_id"] == 7
        assert body["visits"][0]["username"] == "analyst1"
        assert body["visits"][0]["duration_seconds"] == 83
        assert body["visits"][0]["ip_hash"]
        assert stranger.status_code == 404


class TestShareListApi:
    """覆盖链接管理平铺列表的可见范围、聚合与分页边界。"""

    # 方法作用：验证租户管理员看全租户、普通用户只看自己创建的分享。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_share_list_visibility_scope(self, monkeypatch) -> None:
        """管理员看到两条，普通用户只看到自己那条。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        admin = SimpleNamespace(tenant_id=3, user_id=9, role="tenant_admin")
        await _create_share(store, owner, "token-owner", code_hash="hashed-code", prompt="季度销售分析")
        await _create_share(store, admin, "token-admin", prompt="库存周转分析")
        _bind(monkeypatch, store, admin)

        # Act
        async with _client() as client:
            admin_view = await client.get(f"{PREFIX}/shares")
            _bind(monkeypatch, store, owner)
            owner_view = await client.get(f"{PREFIX}/shares")

        # Assert
        assert admin_view.status_code == 200
        admin_body = admin_view.json()
        assert admin_body["total"] == 2
        assert {item["created_by"] for item in admin_body["shares"]} == {7, 9}
        titles = {item["report_title"] for item in admin_body["shares"]}
        assert titles == {"季度销售分析", "库存周转分析"}
        owner_body = owner_view.json()
        assert owner_body["total"] == 1
        assert owner_body["shares"][0]["created_by"] == 7
        assert owner_body["shares"][0]["require_code"] is True
        assert owner_body["shares"][0]["report_id"]
        assert owner_body["shares"][0]["version"] == 1
        assert owner_body["shares"][0]["created_at"] is not None
        assert owner_body["shares"][0]["revoked_at"] is None

    # 方法作用：验证列表聚合访问计数、时长、平均值以及分页上限夹紧。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_share_list_aggregates_and_clamps_page_size(self, monkeypatch) -> None:
        """两条访问聚合成 2 次、130 秒、平均 65 秒，page_size 超限按 100 夹紧。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        await _create_share(store, owner, "token-agg")
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            first = await client.post(f"{PREFIX}/shared/reports/token-agg/visits", json={})
            await client.patch(f"{PREFIX}/shared/reports/token-agg/visits/{first.json()['visit_id']}", json={"duration_seconds": 100})
            _bind(monkeypatch, store, SimpleNamespace(tenant_id=1, user_id=0, role="anonymous"))
            second = await client.post(f"{PREFIX}/shared/reports/token-agg/visits", json={})
            await client.patch(f"{PREFIX}/shared/reports/token-agg/visits/{second.json()['visit_id']}", json={"duration_seconds": 30})
            _bind(monkeypatch, store, owner)
            listed = await client.get(f"{PREFIX}/shares", params={"page_size": 500})

        # Assert
        body = listed.json()
        assert body["page_size"] == 100
        assert body["page"] == 1
        row = body["shares"][0]
        assert row["visitor_count"] == 2
        assert row["system_visitor_count"] == 1
        assert row["anonymous_visitor_count"] == 1
        assert row["total_duration_seconds"] == 130
        assert row["average_duration_seconds"] == 65

    # 方法作用：验证非租户管理员不能用创建人筛选越权查询。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_share_list_created_by_filter_requires_admin(self, monkeypatch) -> None:
        """普通用户携带 created_by 返回 403。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        await _create_share(store, owner, "token-filter")
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            forbidden = await client.get(f"{PREFIX}/shares", params={"created_by": 9})
            allowed = await client.get(f"{PREFIX}/shares", params={"report_id": store._shares["token-filter" and next(iter(store._shares))]["report_id"]})

        # Assert
        assert forbidden.status_code == 403
        assert allowed.status_code == 200
        assert allowed.json()["total"] == 1

    # 方法作用：验证状态筛选区分有效、已撤销与已过期。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_share_list_status_filter(self, monkeypatch) -> None:
        """status=revoked 只返回已撤销分享，status=active 只返回有效分享。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        await _create_share(store, owner, "token-active")
        await _create_share(store, owner, "token-revoked", revoked=True)
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            active = await client.get(f"{PREFIX}/shares", params={"status": "active"})
            revoked = await client.get(f"{PREFIX}/shares", params={"status": "revoked"})
            invalid = await client.get(f"{PREFIX}/shares", params={"status": "unknown"})

        # Assert
        assert active.json()["total"] == 1
        assert active.json()["shares"][0]["revoked_at"] is None
        assert revoked.json()["total"] == 1
        assert revoked.json()["shares"][0]["revoked_at"] is not None
        assert invalid.status_code == 422


class TestBatchRevokeApi:
    """覆盖批量撤销的跳过语义与边界校验。"""

    # 方法作用：验证越权、已撤销与不存在的标识只计入 skipped。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_batch_revoke_skips_invisible_and_revoked(self, monkeypatch) -> None:
        """一条成功撤销，其余三条跳过且接口整体成功。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        outsider = SimpleNamespace(tenant_id=3, user_id=9, role="analyst")
        mine = await _create_share(store, owner, "token-mine")
        revoked = await _create_share(store, owner, "token-already-revoked", revoked=True)
        others = await _create_share(store, outsider, "token-others")
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            response = await client.post(
                f"{PREFIX}/shares/batch-revoke",
                json={"share_ids": [mine["id"], revoked["id"], others["id"], "not-a-uuid"]},
            )

        # Assert
        assert response.status_code == 200
        assert response.json() == {"revoked": 1, "skipped": 3}
        assert store._shares[mine["id"]]["revoked_at"] is not None
        assert store._shares[others["id"]]["revoked_at"] is None
        assert store._audits[-1]["action"] == "share_revoke_batch"

    # 方法作用：验证空列表与非 UUID 覆盖的边界输入。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_batch_revoke_rejects_empty_list(self, monkeypatch) -> None:
        """空 share_ids 返回 422，超过 100 条同样被拒绝。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            empty = await client.post(f"{PREFIX}/shares/batch-revoke", json={"share_ids": []})
            oversized = await client.post(f"{PREFIX}/shares/batch-revoke", json={"share_ids": [str(uuid4()) for _ in range(101)]})

        # Assert
        assert empty.status_code == 422
        assert oversized.status_code == 422


class TestTenantSettingsApi:
    """覆盖租户级报表设置的权限、校验与审计。"""

    # 方法作用：验证只有租户管理员可以读写租户设置。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_tenant_settings_requires_admin(self, monkeypatch) -> None:
        """普通用户 GET 与 PUT 均返回 403。"""
        # Arrange
        store = ReportStore(memory=True)
        analyst = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        _bind(monkeypatch, store, analyst)

        # Act
        async with _client() as client:
            read = await client.get(f"{PREFIX}/admin/tenant-settings")
            write = await client.put(f"{PREFIX}/admin/tenant-settings", json={"record_share_visitor_identity": True, "max_permanent_reports_per_user": 5})

        # Assert
        assert read.status_code == 403
        assert write.status_code == 403
        assert store._tenant_settings == {}

    # 方法作用：验证负数上限被契约层拒绝。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_tenant_settings_rejects_negative_limit(self, monkeypatch) -> None:
        """max_permanent_reports_per_user 为负返回 422。"""
        # Arrange
        store = ReportStore(memory=True)
        admin = SimpleNamespace(tenant_id=3, user_id=9, role="tenant_admin")
        _bind(monkeypatch, store, admin)

        # Act
        async with _client() as client:
            negative = await client.put(f"{PREFIX}/admin/tenant-settings", json={"record_share_visitor_identity": True, "max_permanent_reports_per_user": -1})

        # Assert
        assert negative.status_code == 422

    # 方法作用：验证租户管理员保存设置后写入审计并可回读。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_tenant_settings_update_writes_audit(self, monkeypatch) -> None:
        """PUT 成功后动作 tenant_settings_update，GET 返回保存值。"""
        # Arrange
        store = ReportStore(memory=True)
        admin = SimpleNamespace(tenant_id=3, user_id=9, role="tenant_admin")
        _bind(monkeypatch, store, admin)

        # Act
        async with _client() as client:
            default_read = await client.get(f"{PREFIX}/admin/tenant-settings")
            saved = await client.put(f"{PREFIX}/admin/tenant-settings", json={"record_share_visitor_identity": False, "max_permanent_reports_per_user": 5})
            read_back = await client.get(f"{PREFIX}/admin/tenant-settings")

        # Assert
        assert default_read.json() == {"record_share_visitor_identity": True, "max_permanent_reports_per_user": 0}
        assert saved.status_code == 200
        assert saved.json() == {"record_share_visitor_identity": False, "max_permanent_reports_per_user": 5}
        assert read_back.json() == saved.json()
        assert store._tenant_settings[3]["updated_by"] == 9
        assert store._audits[-1]["action"] == "tenant_settings_update"
