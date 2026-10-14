"""迁移执行器 connection_setup 会话前序回调测试（36.4）。

边界：Fake 连接 + 原迁移器 run_migrations，仅替换 asyncpg.connect；
不建立真实数据库连接，不读取任何用户数据库或配置。

覆盖：
- 同一连接上 setup 恰执行一次，且严格早于 advisory lock 与 schema_migrations 建表；
- 零待应用迁移时 setup 仍恰一次且保持在锁前；
- setup 抛出异常时连接关闭、未加锁、不应用任何迁移 SQL；
- connection_setup=None 与旧调用完全一致（默认兼容）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest


class _Transaction:
    """记录迁移事务退出时的异常类型。"""

    # 作用：初始化事务退出记录。
    # Args: self - 测试事务实例。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self.exits: list[type[BaseException] | None] = []

    # 作用：进入测试事务上下文。
    # Args: self - 测试事务实例。
    # Returns: 当前事务实例。
    async def __aenter__(self) -> "_Transaction":
        return self

    # 作用：记录事务退出时的异常类型。
    # Args: self - 测试事务实例；exc_type/exc/tb - 上下文异常信息。
    # Returns: False，确保异常继续抛出。
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        del exc, tb
        self.exits.append(exc_type)
        return False


class _Connection:
    """提供迁移执行器所需的最小 asyncpg 连接契约，并记录调用顺序。"""

    # 作用：初始化调用账本与已应用迁移记录。
    # Args: self - 测试连接；applied - 已登记迁移行。
    # Returns: 无返回值。
    def __init__(self, applied: list[dict[str, Any]] | None = None) -> None:
        """applied 行结构必须含 version/name/checksum 三个键。"""
        self.applied = applied or []
        self.calls: list[tuple[str, tuple]] = []
        self.transactions: list[_Transaction] = []
        self.closed = False

    # 作用：模拟执行 SQL 并记录参数。
    # Args: self - 测试连接；sql - SQL 文本；args - 绑定参数。
    # Returns: 固定命令状态字符串。
    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        return "OK"

    # 作用：返回已应用迁移登记行。
    # Args: self - 测试连接；sql - 查询语句。
    # Returns: 构造的已应用记录。
    async def fetch(self, sql: str) -> list[dict[str, Any]]:
        del sql
        return self.applied

    # 作用：创建可观察的事务上下文。
    # Args: self - 测试连接。
    # Returns: 新事务实例。
    def transaction(self) -> _Transaction:
        transaction = _Transaction()
        self.transactions.append(transaction)
        return transaction

    # 作用：标记测试连接已关闭。
    # Args: self - 测试连接。
    # Returns: 无返回值。
    async def close(self) -> None:
        self.closed = True


class TestMigrationConnectionSetup:
    """36.4：connection_setup 的时机、唯一性、故障关闭与 None 兼容。"""

    # 作用：验证 setup 在 advisory lock 前、于同一连接上恰执行一次。
    # Args: self - 测试实例；tmp_path - 临时迁移目录；monkeypatch - 补丁夹具。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    async def test_setup_runs_once_on_same_connection_before_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """两个待应用迁移只触发一次 setup，且 setup 时尚未执行任何 SQL。"""
        # Arrange
        (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
        (tmp_path / "002_second.sql").write_text("SELECT 2;", encoding="utf-8")
        connection = _Connection()
        observed: dict[str, Any] = {}
        setup_calls: list[Any] = []
        import asyncpg

        from src.db.migrations import run_migrations

        monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=connection))

        # 作用：记录 setup 收到的连接与当时已执行的 SQL 数量。
        # Args: target - 迁移器传入的连接。
        # Returns: 无返回值。
        async def setup(target: Any) -> None:
            setup_calls.append(target)
            observed["connection"] = target
            observed["calls_before_setup"] = len(connection.calls)

        # Act
        applied = await run_migrations(
            "postgresql+asyncpg://user:secret@db/app",
            migrations_dir=tmp_path,
            connection_setup=setup,
        )

        # Assert
        assert applied == ["001_first.sql", "002_second.sql"]
        assert setup_calls == [connection]
        assert observed["connection"] is connection
        assert observed["calls_before_setup"] == 0
        assert "pg_advisory_lock" in connection.calls[0][0]
        assert connection.calls[1][0].startswith("CREATE TABLE IF NOT EXISTS schema_migrations")
        assert all(transaction.exits == [None] for transaction in connection.transactions)
        assert connection.closed is True

    # 作用：验证零待应用迁移时 setup 仍恰一次且保持在 advisory lock 前。
    # Args: self - 测试实例；tmp_path - 临时迁移目录；monkeypatch - 补丁夹具。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    async def test_setup_runs_once_even_when_nothing_to_apply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """已登记且校验通过的迁移不产生额外执行，setup 仍在锁前恰一次。"""
        # Arrange
        (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
        from src.db.migrations import _discover_migrations, run_migrations

        checksum = _discover_migrations(tmp_path)[0][2]
        connection = _Connection(
            applied=[{"version": "001", "name": "001_first.sql", "checksum": checksum}],
        )
        calls: list[tuple[Any, int]] = []
        import asyncpg

        monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=connection))

        # 作用：记录 setup 次数与该时刻的 SQL 计数。
        # Args: target - 迁移器传入的连接。
        # Returns: 无返回值。
        async def setup(target: Any) -> None:
            calls.append((target, len(connection.calls)))

        # Act
        applied = await run_migrations(
            "postgresql://user:secret@db/app",
            migrations_dir=tmp_path,
            connection_setup=setup,
        )

        # Assert
        assert applied == []
        assert calls == [(connection, 0)]
        assert connection.transactions == []
        assert connection.closed is True

    # 作用：验证 setup 异常必须失败关闭：连接关闭、未加锁、零迁移 SQL。
    # Args: self - 测试实例；tmp_path - 临时迁移目录；monkeypatch - 补丁夹具。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    async def test_setup_failure_closes_connection_without_applying_sql(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """setup 异常包装为 MigrationError，且不得创建版本表或执行任何迁移。"""
        # Arrange
        (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
        connection = _Connection()
        setup_calls: list[Any] = []
        import asyncpg

        from src.db.migrations import MigrationError, run_migrations

        monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=connection))

        # 作用：抛出会话初始化异常。
        # Args: target - 迁移器传入的连接。
        # Returns: 无返回值；始终抛出 RuntimeError。
        async def setup(target: Any) -> None:
            setup_calls.append(target)
            raise RuntimeError("setup boom")

        # Act / Assert
        with pytest.raises(MigrationError, match="无法完成"):
            await run_migrations(
                "postgresql://user:secret@db/app",
                migrations_dir=tmp_path,
                connection_setup=setup,
            )
        assert setup_calls == [connection]
        assert connection.closed is True
        assert connection.calls == []
        assert connection.transactions == []

    # 作用：验证不传 setup 与显式 None 的调用序列完全一致（默认兼容）。
    # Args: self - 测试实例；tmp_path - 临时迁移目录；monkeypatch - 补丁夹具。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    async def test_none_setup_keeps_legacy_call_sequence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """connection_setup=None 不得引入任何额外语句或行为变化。"""
        # Arrange
        (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
        default_connection = _Connection()
        explicit_connection = _Connection()
        connections = [default_connection, explicit_connection]
        import asyncpg

        from src.db.migrations import run_migrations

        monkeypatch.setattr(asyncpg, "connect", AsyncMock(side_effect=connections))

        # Act：缺省调用与显式 None 各执行一次
        default_applied = await run_migrations(
            "postgresql://user:secret@db/app", migrations_dir=tmp_path,
        )
        explicit_applied = await run_migrations(
            "postgresql://user:secret@db/app",
            migrations_dir=tmp_path,
            connection_setup=None,
        )

        # Assert
        assert default_applied == ["001_first.sql"] == explicit_applied
        assert [sql for sql, _ in default_connection.calls] == [
            sql for sql, _ in explicit_connection.calls
        ]
        assert default_connection.closed is True and explicit_connection.closed is True
