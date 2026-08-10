"""轻量 PostgreSQL 迁移执行器测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path
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
    """覆盖功能 17.2.2 的顺序、版本、兼容、回滚和连接生命周期。"""

    # 方法作用：验证项目迁移编号唯一且保持数值升序。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_project_migrations_have_unique_versions(self):
        """真实迁移目录中的版本号必须唯一，避免启动扫描直接失败。"""
        # Arrange
        migration_dir = Path(__file__).resolve().parents[2] / "migrations"
        from src.db.migrations import _discover_migrations

        # Act
        migrations = _discover_migrations(migration_dir)

        # Assert
        versions = [version for version, _, _ in migrations]
        assert len(versions) == len(set(versions))
        assert versions == sorted(versions, key=int)

    # 方法作用：验证迁移 checksum 不受 Git 在 Windows/Linux 间转换换行符影响。
    # Args: self - pytest 测试类实例；tmp_path - 临时迁移目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_migration_checksum_is_line_ending_stable(self, tmp_path) -> None:
        """语义相同的 LF 与 CRLF SQL 必须生成同一登记 checksum。"""
        # Arrange
        from src.db.migrations import _discover_migrations

        path = tmp_path / "001_first.sql"
        path.write_bytes(b"SELECT 1;\n")
        lf_checksum = _discover_migrations(tmp_path)[0][2]

        # Act
        path.write_bytes(b"SELECT 1;\r\n")
        crlf_checksum = _discover_migrations(tmp_path)[0][2]

        # Assert
        assert crlf_checksum == lf_checksum

    # 方法作用：验证知识自进化修复迁移会启用强制 RLS 并重建身份策略。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_knowledge_evolution_repair_migration_enforces_rls(self) -> None:
        """旧版 015 升级后必须恢复当前设计要求的租户和用户隔离。"""
        # Arrange
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "migrations" / "017_knowledge_evolution_rls_repair.sql"

        # Act
        sql = migration.read_text(encoding="utf-8")

        # Assert
        assert "ENABLE ROW LEVEL SECURITY" in sql
        assert "FORCE ROW LEVEL SECURITY" in sql
        assert "CREATE POLICY knowledge_sql_observations_identity" in sql
        assert "app.current_tenant_id" in sql
        assert "app.current_user_id" in sql

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

    # 方法作用：验证历史登记的 CRLF checksum 可在 LF 工作区中继续启动。
    # Args: self - pytest 测试类实例；tmp_path - 临时目录；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_run_migrations_accepts_equivalent_line_ending_checksum(
        self, tmp_path, monkeypatch,
    ) -> None:
        """仅换行符不同的历史 checksum 不应被误判为迁移内容篡改。"""
        # Arrange
        content_lf = b"SELECT 1;\n"
        content_crlf = b"SELECT 1;\r\n"
        (tmp_path / "001_first.sql").write_bytes(content_lf)
        connection = _Connection(applied=[{
            "version": "001",
            "name": "001_first.sql",
            "checksum": hashlib.sha256(content_crlf).hexdigest(),
        }])
        import asyncpg

        from src.db.migrations import run_migrations

        monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=connection))

        # Act
        applied = await run_migrations(
            "postgresql://user:secret@db/app", migrations_dir=tmp_path,
        )

        # Assert
        assert applied == []
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

    # 方法作用：验证已确认的 015 旧 checksum 只有在修复迁移存在时才可继续升级。
    # Args: self - pytest 测试类实例；tmp_path - 临时目录；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_run_migrations_accepts_pinned_legacy_checksum_with_repair(
        self, tmp_path, monkeypatch,
    ) -> None:
        """精确匹配的历史 015 应保留审计记录，并继续执行 017 RLS 修复。"""
        # Arrange
        project_root = Path(__file__).resolve().parents[2]
        current_015 = (project_root / "migrations" / "015_knowledge_evolution.sql").read_bytes()
        (tmp_path / "015_knowledge_evolution.sql").write_bytes(current_015)
        (tmp_path / "017_knowledge_evolution_rls_repair.sql").write_text(
            "SELECT 17;", encoding="utf-8",
        )
        connection = _Connection(applied=[{
            "version": "015",
            "name": "015_knowledge_evolution.sql",
            "checksum": "6705b6d1b4ee1cf911f2b46abcf612b638098dcf264e6c299b30c47835a3a995",
        }])
        import asyncpg

        from src.db.migrations import run_migrations

        monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=connection))

        # Act
        applied = await run_migrations(
            "postgresql://user:secret@db/app", migrations_dir=tmp_path,
        )

        # Assert
        assert applied == ["017_knowledge_evolution_rls_repair.sql"]
        assert not any(args and args[0] == "015" for _, args in connection.calls)
        assert any(args and args[0] == "017" for _, args in connection.calls)

    # 方法作用：验证受控旧 checksum 在缺少指定修复迁移时仍然失败关闭。
    # Args: self - pytest 测试类实例；tmp_path - 临时目录；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_run_migrations_rejects_pinned_legacy_checksum_without_repair(
        self, tmp_path, monkeypatch,
    ) -> None:
        """兼容入口不能在 RLS 修复文件缺失时绕过不可变迁移保护。"""
        # Arrange
        project_root = Path(__file__).resolve().parents[2]
        current_015 = (project_root / "migrations" / "015_knowledge_evolution.sql").read_bytes()
        (tmp_path / "015_knowledge_evolution.sql").write_bytes(current_015)
        connection = _Connection(applied=[{
            "version": "015",
            "name": "015_knowledge_evolution.sql",
            "checksum": "6705b6d1b4ee1cf911f2b46abcf612b638098dcf264e6c299b30c47835a3a995",
        }])
        import asyncpg

        from src.db.migrations import MigrationError, run_migrations

        monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=connection))

        # Act / Assert
        with pytest.raises(MigrationError, match="checksum"):
            await run_migrations(
                "postgresql://user:secret@db/app", migrations_dir=tmp_path,
            )

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
