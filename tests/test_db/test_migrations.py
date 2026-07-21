"""轻量 PostgreSQL 迁移执行器测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


class _Transaction:
    """记录迁移事务是否按异常结果退出。"""

    def __init__(self) -> None:
        self.exits: list[type[BaseException] | None] = []

    # 方法作用：进入测试事务上下文。
    # Args: self - 测试事务实例。
    # Returns: 当前事务实例。
    async def __aenter__(self):
        return self

    # 方法作用：记录事务退出时的异常类型。
    # Args: self - 测试事务实例；exc_type/exc/tb - 上下文异常信息。
    # Returns: False，确保异常继续抛出。
    async def __aexit__(self, exc_type, exc, tb):
        del exc, tb
        self.exits.append(exc_type)
        return False


class _Connection:
    """提供迁移执行器所需的最小 asyncpg 连接契约。"""

    def __init__(self, applied: list[dict] | None = None) -> None:
        self.applied = applied or []
        self.calls: list[tuple[str, tuple]] = []
        self.transactions: list[_Transaction] = []
        self.closed = False

    # 方法作用：模拟执行 SQL 并记录参数。
    # Args: self - 测试连接；sql - SQL 文本；args - 绑定参数。
    # Returns: 固定命令状态字符串。
    async def execute(self, sql: str, *args):
        self.calls.append((sql, args))
        if "RAISE_TEST_ERROR" in sql:
            raise RuntimeError("migration failed")
        return "OK"

    # 方法作用：返回已应用迁移版本。
    # Args: self - 测试连接；sql - 查询语句。
    # Returns: 构造的已应用记录。
    async def fetch(self, sql: str):
        del sql
        return self.applied

    # 方法作用：创建可观察的事务上下文。
    # Args: self - 测试连接。
    # Returns: 新事务实例。
    def transaction(self):
        transaction = _Transaction()
        self.transactions.append(transaction)
        return transaction

    # 方法作用：标记测试连接已关闭。
    # Args: self - 测试连接。
    # Returns: 无返回值。
    async def close(self) -> None:
        self.closed = True


class TestMigrationRunner:
    """覆盖功能 17.2.2 的顺序、版本、回滚和连接生命周期。"""

    # 方法作用：验证迁移按编号执行并在同一事务登记版本。
    # Args: self - pytest 测试类实例；tmp_path - 临时目录；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_run_migrations_applies_files_in_version_order(self, tmp_path, monkeypatch):
        """文件系统顺序不能改变数据库迁移顺序。"""
        # Arrange
        (tmp_path / "003_third.sql").write_text("BEGIN;\nSELECT 3;\nCOMMIT;", encoding="utf-8")
        (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
        connection = _Connection()
        import asyncpg
        from src.db.migrations import run_migrations

        monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=connection))

        # Act
        applied = await run_migrations(
            "postgresql+asyncpg://user:secret@db/app", migrations_dir=tmp_path,
        )

        # Assert
        assert applied == ["001_first.sql", "003_third.sql"]
        scripts = [sql for sql, args in connection.calls if not args and "SELECT " in sql]
        assert scripts == ["SELECT 1;", "SELECT 3;"]
        assert all(transaction.exits == [None] for transaction in connection.transactions)
        assert connection.closed is True

    # 方法作用：验证已登记迁移内容被修改时启动失败。
    # Args: self - pytest 测试类实例；tmp_path - 临时目录；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_run_migrations_rejects_checksum_mismatch(self, tmp_path, monkeypatch):
        """已执行 SQL 文件不得被静默篡改后重新使用。"""
        # Arrange
        (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
        connection = _Connection(applied=[{
            "version": "001", "name": "001_first.sql", "checksum": "wrong",
        }])
        import asyncpg
        from src.db.migrations import MigrationError, run_migrations

        monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=connection))

        # Act / Assert
        with pytest.raises(MigrationError, match="checksum"):
            await run_migrations("postgresql://user:secret@db/app", migrations_dir=tmp_path)
        assert connection.closed is True

    # 方法作用：验证 SQL 失败会回滚当前迁移且仍释放锁和连接。
    # Args: self - pytest 测试类实例；tmp_path - 临时目录；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_run_migrations_rolls_back_failed_file(self, tmp_path, monkeypatch):
        """失败迁移不能登记版本，并必须释放进程间锁。"""
        # Arrange
        (tmp_path / "001_broken.sql").write_text("SELECT RAISE_TEST_ERROR;", encoding="utf-8")
        connection = _Connection()
        import asyncpg
        from src.db.migrations import MigrationError, run_migrations

        monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=connection))

        # Act / Assert
        with pytest.raises(MigrationError, match="001_broken.sql"):
            await run_migrations("postgresql://user:secret@db/app", migrations_dir=tmp_path)
        assert connection.transactions[0].exits == [RuntimeError]
        assert any("pg_advisory_unlock" in sql for sql, _ in connection.calls)
        assert not any(args and args[0] == "001" for _, args in connection.calls)
        assert connection.closed is True
