"""25.7 数据源配置迁移的事务、凭证与插件回归测试。"""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from asyncpg import UniqueViolationError

from src.datasource.config import DataSourceConfig
from src.datasource.providers.external import ExternalDataSourceProvider
from src.datasource.transfer import DataSourceBundle, ImportDataSource, export_config
from src.logging_config import get_logger  # 方法日志测试依赖

logger = get_logger(__name__)


class TestDataSourceTransfer:
    """覆盖 25.7：批量提交、失败回滚、跨方言和无凭证导出。"""

    # 方法作用：验证导出字段白名单和递归敏感键过滤。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_export_nested_secrets_and_runtime_excluded(self) -> None:
        """插件扩展的非敏感参数保留，嵌套凭证及运行时状态不导出。"""
        # Arrange
        source = DataSourceConfig(name="sales", dialect="postgres", mode="external", password="do-not-export", tenant_id=99, extra_params={
            "schema": "sales", "warehouse": "east", "ssl": {"apiKey": "nested-secret", "mode": "verify-full"},
            "options": [{"AccessToken": "nested-secret", "timeout": 3}], "dsn": "postgres://user:secret@host/db",
        })
        # Act
        result = export_config(source)
        payload = json.dumps(result)
        # Assert
        assert "secret" not in payload
        assert "password" not in result and "tenant_id" not in result
        assert result["extra_params"]["ssl"] == {"mode": "verify-full"}
        assert result["extra_params"]["options"] == [{"timeout": 3}]
        assert source.extra_params["ssl"]["apiKey"] == "nested-secret"

    # 方法作用：验证内置数据库的无凭证配置可重新解析。
    # Args: self - 实例；dialect - 待验证数据库类型。
    # Returns: 无返回值。
    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "clickhouse", "sqlite"])
    def test_export_import_round_trip(self, dialect: str) -> None:
        """导出契约与动态插件创建契约一致。"""
        # Arrange
        source = DataSourceConfig(name="sales", dialect=dialect, mode="external", database="sales", extra_params={"schema": "public"})
        # Act
        bundle = DataSourceBundle.model_validate({"version": 1, "datasources": [export_config(source)]})
        # Assert
        assert bundle.datasources[0].dialect == dialect
        assert bundle.datasources[0].password == ""
        assert bundle.datasources[0].extra_params == source.extra_params

    # 方法作用：模拟事务提交与第二条写入失败，验证内存和持久化始终一致。
    # Args: self - 实例；monkeypatch - 依赖替换；failure - 第二条写入异常。
    # Returns: 无返回值。
    @pytest.mark.parametrize("failure", [None, RuntimeError, UniqueViolationError])
    async def test_batch_transaction_commit_or_rollback(self, monkeypatch: pytest.MonkeyPatch, failure: type[Exception] | None) -> None:
        """数据库提交前不发布内存，任一失败回滚配置及权限。"""
        # Arrange
        provider = ExternalDataSourceProvider()
        writes: list[tuple[str, tuple]] = []
        existing = DataSourceConfig(name="keep", dialect="sqlite", mode="external", tenant_id=2)
        provider._register(existing)

        # 方法作用：提供可回滚的内存事务边界。
        # Args: 无。
        # Returns: 模拟事务上下文。
        @asynccontextmanager
        async def transaction():
            try:
                yield
            except Exception:
                writes.clear()
                raise

        # 方法作用：记录事务写入并在第二条连接写入时注入故障。
        # Args: sql - 写入语句；values - 参数。
        # Returns: 无返回值。
        async def execute(sql: str, *values: object) -> None:
            assert set(provider._sources) == {(2, "keep")}
            if values[0] == "second" and failure:
                raise failure("模拟事务失败")
            writes.append((sql, values))

        connection = SimpleNamespace(transaction=transaction, execute=execute)

        # 方法作用：模拟借用状态库连接。
        # Args: 无。
        # Returns: 测试连接。
        @asynccontextmanager
        async def acquire():
            yield connection

        monkeypatch.setattr("src.memory.pg_pool.get_pg_pool", AsyncMock(return_value=SimpleNamespace(acquire=acquire)))
        requests = [ImportDataSource(name=name, dialect="sqlite", password="secret-value") for name in ("first", "second")]
        # Act / Assert
        if failure:
            with pytest.raises(ValueError if failure is UniqueViolationError else failure):
                await provider.import_configs(requests, tenant_id=2, owner_user_id=7)
            assert not writes
            assert set(provider._sources) == {(2, "keep")}
        else:
            result = await provider.import_configs(requests, tenant_id=2, owner_user_id=7)
            assert len(writes) == 4
            assert [item.name for item in result] == ["first", "second"]
            assert all(item.password != "secret-value" for item in result)
            assert all(item.tenant_id == 2 and item.owner_user_id == 7 for item in result)
            assert set(provider._sources) == {(2, "keep"), (2, "first"), (2, "second")}
        assert provider._sources[(2, "keep")] is existing

    # 方法作用：验证同租户重名改为追加随机后缀，且不覆盖原配置。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_duplicate_renames_instead_of_failing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """导入不再整批拒绝；原名保留，新条目带后缀且不覆盖已存在配置。"""
        # Arrange
        import re

        provider = ExternalDataSourceProvider()
        source = DataSourceConfig(name="sales", dialect="sqlite", mode="external", tenant_id=2)
        provider._register(source)
        writes: list[tuple[str, tuple]] = []

        # 方法作用：提供可回滚的内存事务边界。
        # Args: 无。
        # Returns: 模拟事务上下文。
        @asynccontextmanager
        async def transaction():
            try:
                yield
            except Exception:
                writes.clear()
                raise

        # 方法作用：记录事务写入。
        # Args: sql - 写入语句；values - 参数。
        # Returns: 无返回值。
        async def execute(sql: str, *values: object) -> None:
            writes.append((sql, values))

        connection = SimpleNamespace(transaction=transaction, execute=execute)

        # 方法作用：模拟借用状态库连接。
        # Args: 无。
        # Returns: 测试连接。
        @asynccontextmanager
        async def acquire():
            yield connection

        monkeypatch.setattr("src.memory.pg_pool.get_pg_pool", AsyncMock(return_value=SimpleNamespace(acquire=acquire)))
        # Act
        result = await provider.import_configs(
            [ImportDataSource(name="sales", dialect="sqlite")], tenant_id=2, owner_user_id=7,
        )
        # Assert
        assert len(result) == 1
        assert re.fullmatch(r"sales_[0-9a-z]{6}", result[0].name), result[0].name
        assert writes[0][1][0] == result[0].name
        assert provider._sources[(2, "sales")] is source


class TestResolveAvailableName:
    """覆盖 25.7 改名解析：随机后缀、长度截断与重试上限。"""

    # 方法作用：验证名称未被占用时原样返回，不产生多余改名。
    # Args: self - 测试类实例。
    # Returns: 无返回值。
    def test_keeps_name_when_free(self) -> None:
        """不冲突就不改名，避免导入结果出现无意义后缀。"""
        logger.debug("test_keeps_name_when_free 入口")
        from src.datasource.transfer import resolve_available_name

        assert resolve_available_name("sales", lambda candidate: False) == "sales"
        logger.info("test_keeps_name_when_free 完成")

    # 方法作用：验证冲突时追加 6 位 base36 后缀且结果为未占用名称。
    # Args: self - 测试类实例。
    # Returns: 无返回值。
    def test_appends_six_char_suffix_on_conflict(self) -> None:
        """后缀格式固定为下划线加 6 位小写 base36，便于排查与 URL 安全。"""
        logger.debug("test_appends_six_char_suffix_on_conflict 入口")
        import re

        from src.datasource.transfer import resolve_available_name

        taken = {"sales"}
        result = resolve_available_name("sales", taken.__contains__)
        assert re.fullmatch(r"sales_[0-9a-z]{6}", result), result
        logger.info("test_appends_six_char_suffix_on_conflict 完成")

    # 方法作用：验证超长名称截断后仍满足 64 字符上限。
    # Args: self - 测试类实例。
    # Returns: 无返回值。
    def test_truncates_long_name_to_limit(self) -> None:
        """原名可达 64 字符，追加后缀必须截断而非超出数据库列长度。"""
        logger.debug("test_truncates_long_name_to_limit 入口")
        from src.datasource.transfer import resolve_available_name

        long_name = "a" * 64
        result = resolve_available_name(long_name, lambda candidate: candidate == long_name)
        assert result != long_name, "原名已占用时必须改名"
        assert len(result) == 64
        assert result.startswith("a" * 57)
        logger.info("test_truncates_long_name_to_limit 完成")

    # 方法作用：验证一直冲突时超过重试上限抛错，不无限循环。
    # Args: self - 测试类实例。
    # Returns: 无返回值。
    def test_raises_after_max_attempts(self) -> None:
        """候选名全部被占用时必须失败，不能死循环。"""
        logger.debug("test_raises_after_max_attempts 入口")
        import pytest as _pytest

        from src.datasource.transfer import DataSourceConflictError, resolve_available_name

        with _pytest.raises(DataSourceConflictError):
            resolve_available_name("sales", lambda candidate: True, max_attempts=3)
        logger.info("test_raises_after_max_attempts 完成")
