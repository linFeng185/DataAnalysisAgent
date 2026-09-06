"""G24.2/G24.3/G24.4 API 与存储事务边界回归。"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException


# 绑定无外部依赖的数据库连接，并保留异常传播。
# Args: connection - 查询和写入均可断言的替身。
# Returns: 可替换 pg_connection 的上下文工厂。
def connection_factory(connection):
    # 暴露同一测试事务连接，供边界调用捕获 SQL 参数。
    # Args: kwargs - 请求身份。
    # Returns: 测试连接。
    @asynccontextmanager
    async def connect(**kwargs):
        yield connection
    return connect


# 配置当前请求身份，避免测试调用真实认证或数据库。
# Args: monkeypatch - pytest 替身；role/tenant_id/user_id - 测试身份。
# Returns: 无返回值。
def bind_identity(monkeypatch, role="tenant_admin", tenant_id=2, user_id=7) -> None:
    monkeypatch.setattr("src.api.auth.get_current_role", lambda: role)
    monkeypatch.setattr("src.api.auth.get_current_tenant_id", lambda: tenant_id)
    monkeypatch.setattr("src.api.auth.get_current_user_id", lambda: user_id)


class TestGovernanceBoundaries:
    """验证外部入口不以平台权限代管租户，也不在审计失败后返回正文。"""

    # 验证租户额度 API 使用当前租户并拒绝平台代配。
    # Args: self - 测试实例；monkeypatch - 配置和连接替身。
    # Returns: 无返回值。
    async def test_query_policy_current_tenant_only(self, monkeypatch) -> None:
        """软额度不能超过硬上限，读写仅限 tenant_admin。"""
        from src.api.routes.query_quota import get_query_quota, update_query_quota
        from src.security import query_quota

        bind_identity(monkeypatch)
        connection = AsyncMock()
        connection.fetchrow.return_value = {"tenant_per_hour": 10, "user_per_hour": 4}
        monkeypatch.setattr(query_quota, "pg_connection", connection_factory(connection))
        monkeypatch.setattr(query_quota, "get_settings", lambda: SimpleNamespace(max_queries_per_hour=20))
        assert (await get_query_quota())["hard_limit"] == 20
        policy = query_quota.QueryQuotaPolicy(tenant_per_hour=10, user_per_hour=4)
        assert (await update_query_quota(policy))["user_per_hour"] == 4
        with pytest.raises(HTTPException) as error:
            await update_query_quota(query_quota.QueryQuotaPolicy(tenant_per_hour=21, user_per_hour=4))
        assert error.value.status_code == 422
        bind_identity(monkeypatch, role="super_admin", user_id=1)
        with pytest.raises(HTTPException) as error:
            await update_query_quota(policy)
        assert error.value.status_code == 403

    # 验证签发和撤销均在数据库写入对应审计。
    # Args: self - 测试实例；monkeypatch - 认证和数据库替身。
    # Returns: 无返回值。
    async def test_emergency_create_and_revoke_audited(self, monkeypatch) -> None:
        """授权只绑定当前私有文件，撤销只针对原授权者。"""
        from src.api.routes.emergency_access import create_grant, revoke_grant
        from src.security import emergency_access

        bind_identity(monkeypatch, role="analyst")
        connection = AsyncMock()
        connection.fetchrow.side_effect = [
            {"tenant_id": 2, "user_id": 7, "knowledge_scope": "private"},
            {"expires_at": datetime.now(timezone.utc)}, {"id": uuid4()},
        ]
        connection.fetchval.return_value = True
        monkeypatch.setattr(emergency_access, "pg_connection", connection_factory(connection))
        result = await create_grant(emergency_access.EmergencyGrantRequest(file_id=12, grantee_user_id=1, reason="工单排查"))
        assert result["id"]
        await revoke_grant(uuid4())
        actions = [call.args[3] for call in connection.execute.await_args_list if "governance_audit" in call.args[0]]
        assert actions == ["knowledge.grant", "knowledge.revoke"]

    # 验证只读专用路由的审计写入失败时不返回任何内容。
    # Args: self - 测试实例；monkeypatch - 当前超管与数据库替身。
    # Returns: 无返回值。
    async def test_emergency_read_requires_committed_audit(self, monkeypatch) -> None:
        """正常返回不可缓存附件，审计失败必须传播异常。"""
        from src.api.routes.emergency_access import read_granted_file
        from src.security import emergency_access

        bind_identity(monkeypatch, role="super_admin", user_id=1)
        now = datetime.now(timezone.utc)
        grant = {"file_id": 12, "tenant_id": 2, "scope": "private", "owner_user_id": 7,
                 "issued_by": 7, "grantee_user_id": 1, "reason": "工单排查", "revoked_at": None,
                 "expires_at": now + timedelta(minutes=10), "issuer_role": "analyst", "issuer_active": True, "tenant_active": True}
        file = {"tenant_id": 2, "user_id": 7, "knowledge_scope": "private", "file_data": b"safe", "content_type": "text/plain"}
        connection = AsyncMock()
        connection.fetchrow.side_effect = [grant, file, grant, file]
        connection.fetchval.return_value = now
        monkeypatch.setattr(emergency_access, "pg_connection", connection_factory(connection))
        response = await read_granted_file(uuid4())
        assert response.body == b"safe"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-disposition"] == "attachment"
        connection.execute.side_effect = [None, RuntimeError("审计写入失败")]
        with pytest.raises(RuntimeError, match="审计"):
            await read_granted_file(uuid4())

    # 验证安全停用只写独立状态，不更新或读取受管连接凭证。
    # Args: self - 测试实例；monkeypatch - 平台身份和连接替身。
    # Returns: 无返回值。
    async def test_mcp_security_block_does_not_edit_connection(self, monkeypatch) -> None:
        """安全阻断支持解除但不自动启用连接，租户不能自行解除。"""
        from src.api.routes.mcp import list_security_blocks, set_security_block
        from src.security import mcp_safety

        bind_identity(monkeypatch, role="super_admin", user_id=1)
        connection = AsyncMock()
        connection.fetchval.return_value = True
        connection.fetch.return_value = []
        monkeypatch.setattr(mcp_safety, "pg_connection", connection_factory(connection))
        request = mcp_safety.MCPSecurityBlockRequest(tenant_id=2, scope="tenant", name="search", reason="安全工单", blocked=False)
        assert (await set_security_block(request))["blocked"] is False
        assert (await list_security_blocks())["items"] == []
        assert all("mcp_servers" not in str(call) for call in connection.mock_calls)
        bind_identity(monkeypatch)
        with pytest.raises(HTTPException) as error:
            await set_security_block(request)
        assert error.value.status_code == 403
