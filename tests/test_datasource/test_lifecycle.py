"""数据源编辑与临时连接测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api.schemas import DataSourceCreateRequest, DataSourceUpdateRequest
from src.datasource.credential_manager import CredentialManager
from src.datasource.providers.external import ExternalDataSourceProvider


@pytest.mark.asyncio
class TestDatasourceLifecycle:
    """覆盖功能 18.3.4、18.3.5 的数据源生命周期。"""

    # 方法作用：验证临时探测仅创建短生命周期连接且必定释放资源。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_connection_probe_does_not_register_or_persist(self) -> None:
        """连接测试成功后 Provider 不应新增数据源或保留连接。"""
        # Arrange
        provider = ExternalDataSourceProvider()
        connector = SimpleNamespace(
            probe_sql="SELECT 1",
            health_check=AsyncMock(return_value=True),
            close=AsyncMock(),
        )

        # 方法作用：模拟 Registry 为临时配置创建连接器。
        # Args: datasource - 待探测的临时数据源配置。
        # Returns: 临时引擎对象。
        async def create_engine(datasource):
            datasource.connector = connector
            datasource.engine = object()
            return datasource.engine

        request = DataSourceCreateRequest(
            name="temporary",
            dialect="postgres",
            host="db.internal",
            database="sales",
            username="readonly",
            password="secret",
        )

        # Act
        connected = await provider.probe_request(request, create_engine)

        # Assert
        assert connected is True
        assert await provider.lookup("temporary") is None
        connector.close.assert_awaited_once()

    # 方法作用：验证编辑数据源时空密码沿用原密文并在探测成功后持久化。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_update_preserves_password_and_persists_after_probe(self, monkeypatch) -> None:
        """更新非凭证字段不应要求用户重新输入密码。"""
        # Arrange
        monkeypatch.setenv("ENV", "dev")
        provider = ExternalDataSourceProvider()
        original = await provider.register(DataSourceCreateRequest(
            name="sales",
            dialect="postgres",
            host="old-db",
            database="sales",
            username="readonly",
            password="secret",
        ))
        original.tenant_id = 3
        original.owner_user_id = 7
        provider.probe_request = AsyncMock(return_value=True)
        provider.persist = AsyncMock()
        request = DataSourceUpdateRequest(
            dialect="postgres",
            host="new-db",
            database="sales",
            username="readonly",
            password=None,
            description="新版只读库",
        )

        # Act
        updated = await provider.update(
            "sales",
            request,
            engine_factory=AsyncMock(),
            tenant_id=3,
            owner_user_id=7,
        )

        # Assert
        provider.probe_request.assert_awaited_once()
        provider.persist.assert_awaited_once_with(
            updated,
            tenant_id=3,
            owner_user_id=7,
        )
        assert updated.host == "new-db"
        assert updated.description == "新版只读库"
        assert CredentialManager().decrypt(updated.password) == "secret"
        assert await provider.lookup("sales") is updated

    # 方法作用：验证新配置探测失败时保留旧配置且不执行持久化。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_update_probe_failure_keeps_original_configuration(self, monkeypatch) -> None:
        """连接失败不能让一个原本可用的数据源消失。"""
        # Arrange
        monkeypatch.setenv("ENV", "dev")
        provider = ExternalDataSourceProvider()
        original = await provider.register(DataSourceCreateRequest(
            name="sales",
            dialect="postgres",
            host="old-db",
            password="secret",
        ))
        provider.probe_request = AsyncMock(return_value=False)
        provider.persist = AsyncMock()
        request = DataSourceUpdateRequest(
            dialect="postgres",
            host="bad-db",
            password="wrong",
        )

        # Act / Assert
        with pytest.raises(ConnectionError, match="连接测试失败"):
            await provider.update(
                "sales",
                request,
                engine_factory=AsyncMock(),
                tenant_id=1,
                owner_user_id=1,
            )
        assert await provider.lookup("sales") is original
        provider.persist.assert_not_awaited()
