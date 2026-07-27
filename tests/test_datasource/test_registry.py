"""DataSourceRegistry ClickHouse 适配层测试。

覆盖 _ClickHouseResult 对 SQLAlchemy 风格结果协议的兼容性。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.datasource.registry import _ClickHouseResult


class TestClickHouseResult:
    """_ClickHouseResult — SQLAlchemy 风格结果适配。"""

    def test_iterable_for_introspection(self):
        """回归: schema_manager._executor 用 `for row in result` 迭代结果。

        回归背景: _ClickHouseResult 只实现了 fetchall/fetchmany 未实现 __iter__，
        导致 ClickHouse DB 内省报 '_ClickHouseResult' object is not iterable。
        """
        # Arrange: 模拟 system.tables 查询结果
        result = _ClickHouseResult(["name"], [("users",), ("orders",)])

        # Act: 按 _executor 的实际消费方式迭代
        rows = [dict(r._mapping) for r in result]

        # Assert
        assert rows == [{"name": "users"}, {"name": "orders"}]

    def test_iter_consumes_cursor(self):
        """迭代与 fetchall 共享游标: 迭代完后 fetchall 返回空。"""
        result = _ClickHouseResult(["v"], [(1,), (2,)])

        consumed = list(result)

        assert len(consumed) == 2
        assert result.fetchall() == []

    def test_iter_empty_result(self):
        """边界: 空结果集迭代不抛异常。"""
        result = _ClickHouseResult(["v"], [])

        assert list(result) == []


class TestDatasourcePrivilegeWarning:
    """覆盖高权限数据库账号允许连接但必须告警的策略。"""

    # 方法作用：验证已知高权限账号只触发 warning，不中断数据源解析。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具；dialect - 数据库方言；username - 高权限用户名。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        ("dialect", "username"),
        [("oracle", "SYSTEM"), ("mssql", "sa")],
    )
    async def test_elevated_account_warns_and_continues(
        self,
        monkeypatch,
        dialect: str,
        username: str,
    ) -> None:
        """高权限账号应成功解析，并留下可观测的安全告警。"""
        # Arrange
        import src.datasource.registry as registry_module
        from src.datasource.config import DataSourceConfig
        from src.datasource.registry import DataSourceRegistry

        config = DataSourceConfig(
            name=f"{dialect}_admin",
            dialect=dialect,
            mode="external",
            username=username,
        )
        provider = MagicMock()
        provider.lookup = AsyncMock(return_value=config)
        provider.test_connection = AsyncMock(return_value=True)
        registry = DataSourceRegistry()
        registry.register_provider("external", provider)
        engine = object()
        monkeypatch.setattr(registry, "_create_engine", AsyncMock(return_value=engine))
        logger = MagicMock()
        monkeypatch.setattr(registry_module, "logger", logger)

        # Act
        resolved = await registry.resolve(config.name)

        # Assert
        assert resolved is config
        assert resolved.engine is engine
        logger.warning.assert_called_once_with(
            "数据源使用高权限数据库账号，继续连接",
            datasource=config.name,
            dialect=dialect,
            username=username,
            protection="应用层只读 SQL 校验仍启用",
        )
