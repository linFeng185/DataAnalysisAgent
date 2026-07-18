"""DataSourceRegistry ClickHouse 适配层测试。

覆盖 _ClickHouseResult 对 SQLAlchemy 风格结果协议的兼容性。
"""

from __future__ import annotations

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
