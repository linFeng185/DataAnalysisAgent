"""25.1 动态配置快照边界测试。"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.app_context import AppContext, use_app_context
from src.config import Settings


class TestRuntimeSettings:
    """覆盖环境默认值、热覆盖、重置与非法字段。"""

    # 验证环境默认值可覆盖且恢复默认不修改原始对象。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_overlay_and_reset(self) -> None:
        """新的完整快照校验后生效，原始设置保持不变。"""
        from src.runtime.settings import merge_settings
        base = Settings(_env_file=None, max_queries_per_hour=100)
        assert merge_settings(base, {"max_queries_per_hour": 12}).max_queries_per_hour == 12
        assert merge_settings(base, {}).max_queries_per_hour == 100
        assert base.max_queries_per_hour == 100

    # 验证秘密与启动参数不进入热更新通道。
    # Args: self - 测试实例；values - 非法覆盖。
    # Returns: 无返回值。
    @pytest.mark.parametrize("values", [{"jwt_secret": "secret"}, {"database_url": "bad"}, {"max_queries_per_hour": -1}, {"unknown": 1}, {"max_queries_per_hour": True}, {"max_queries_per_hour": "12"}, {"llm_timeout": 0}, {"llm_temperature": 3}, {"context_compression_target_ratio": 0.79, "context_compression_trigger_ratio": 0.7}])
    def test_reject_unsafe_overrides(self, values: dict) -> None:
        """拒绝未知配置、负预算和敏感启动参数。"""
        from src.runtime.settings import merge_settings
        with pytest.raises(ValueError):
            merge_settings(Settings(_env_file=None), values)

    # 验证版本传播、最后有效快照和字段重置，不连接实际元数据库。
    # Args: self - 实例；monkeypatch - 数据库替身。
    # Returns: 无返回值。
    async def test_shared_revision_and_invalid_snapshot(self, monkeypatch) -> None:
        """多实例读取同一数据库版本，坏配置不能覆盖上次有效设置。"""
        from src.config import get_settings
        from src.runtime import settings as module
        row = {"revision": 1, "values_json": {"max_queries_per_hour": 12}, "plugins_json": {}}
        connection = AsyncMock()
        connection.fetchrow.side_effect = lambda *args: dict(row)
        # 提供单一共享数据库记录，模拟两个 Worker 的读取。
        # Args: kwargs - 内部平台身份。
        # Returns: 模拟数据库连接。
        @asynccontextmanager
        async def connect(**kwargs):
            yield connection
        monkeypatch.setattr(module, "pg_connection", connect)
        monkeypatch.setattr("src.connectors.plugins.discover_plugins", lambda: [])
        base = Settings(_env_file=None, max_queries_per_hour=100)
        first, second = module.RuntimeSettingsService(base), module.RuntimeSettingsService(base)
        first_context, second_context = AppContext(settings=base), AppContext(settings=base)
        with use_app_context(first_context):
            await first.refresh()
            assert get_settings().max_queries_per_hour == 12
            assert all(field["key"] != "jwt_secret" for field in first.describe()["fields"])
        with use_app_context(second_context):
            await second.refresh()
            assert get_settings().max_queries_per_hour == 12
            row.update(revision=2, values_json={"max_queries_per_hour": -1})
            with pytest.raises(ValueError):
                await second.refresh()
            assert get_settings().max_queries_per_hour == 12
            assert second.revision == 1
            row.update(revision=3, values_json={})
            await second.refresh()
            assert get_settings().max_queries_per_hour == 100
        await first.close()
        await second.close()

    # 验证过期版本返回冲突，并且非法字段在写数据库之前拒绝。
    # Args: self - 实例；monkeypatch - 数据库替身。
    # Returns: 无返回值。
    async def test_save_conflict_and_reset(self, monkeypatch) -> None:
        """保存和重置保留版本约束，审计只记录字段键。"""
        from src.runtime import settings as module
        row = {"revision": 4, "values_json": {"max_queries_per_hour": 12}, "plugins_json": {}}
        connection = AsyncMock()
        connection.fetchrow.side_effect = lambda *args: dict(row)
        # 模拟提交时更新共享记录和版本，不写真实配置。
        # Args: sql - 写语句；args - 参数。
        # Returns: 无返回值。
        async def execute(sql, *args):
            import json
            if "UPDATE platform_runtime_settings" in sql:
                row.update(values_json=json.loads(args[0]), revision=row["revision"] + 1)
        connection.execute.side_effect = execute
        # 为保存事务提供数据库连接。
        # Args: kwargs - 当前平台身份。
        # Returns: 模拟连接。
        @asynccontextmanager
        async def connect(**kwargs):
            yield connection
        monkeypatch.setattr(module, "pg_connection", connect)
        monkeypatch.setattr("src.connectors.plugins.discover_plugins", lambda: [])
        base = Settings(_env_file=None, max_queries_per_hour=100)
        service = module.RuntimeSettingsService(base)
        with use_app_context(AppContext(settings=base)):
            with pytest.raises(HTTPException) as error:
                await service.save(module.RuntimeSettingsRequest(revision=3, values={"max_queries_per_hour": 5}), 1)
            assert error.value.status_code == 409
            connection.execute.assert_not_awaited()
            result = await service.save(module.RuntimeSettingsRequest(revision=4, values={"max_queries_per_hour": None}), 1)
            assert result["revision"] == 5
            assert service.values == {}
            assert "settings" in connection.execute.await_args.args[0]
