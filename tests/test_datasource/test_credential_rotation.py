"""覆盖 2.4.3 的版本化加密与历史密文兼容。"""

from contextlib import asynccontextmanager
from copy import deepcopy

import pytest

from src.datasource.credential_manager import CredentialManager
from src.datasource.rotate_credentials import main, rotate_persisted_credentials


class RotationConnection:
    """模拟有事务回滚和行更新约束的数据库边界，不连接真实数据库。"""

    # 方法作用：构造两个凭证表和可控异常位置。
    # Args: self - 实例；tokens - 每个表的密文。
    # Returns: 无返回值。
    def __init__(self, tokens: tuple[str, str]) -> None:
        self.rows = {
            "datasource_configs": [{"id": 1, "ciphertext": tokens[0]}],
            "tenant_llm_connections": [{"id": 2, "ciphertext": tokens[1]}],
        }
        self.writes = 0
        self.fail_second = False
        self.locked = True
        self.commands = []

    # 方法作用：模拟单事务失败后恢复两张表。
    # Args: self - 实例。
    # Returns: 事务上下文。
    @asynccontextmanager
    async def transaction(self):
        original = deepcopy(self.rows)
        try:
            yield
        except Exception:
            self.rows = original
            raise

    # 方法作用：返回可控制的全局轮换锁结果。
    # Args: self - 实例；query - SQL；args - SQL 参数。
    # Returns: 是否取得互斥锁。
    async def fetchval(self, query, *args):
        return self.locked

    # 方法作用：模拟游标分页并返回待锁定行。
    # Args: self - 实例；query - SQL；after_id - 分页游标；limit - 页大小。
    # Returns: 当前页记录。
    async def fetch(self, query, after_id, limit):
        table = next(name for name in self.rows if name in query)
        return [row.copy() for row in self.rows[table] if row["id"] > after_id][:limit]

    # 方法作用：模拟维护模式和密文写入，允许第二次写入失败。
    # Args: self - 实例；query - SQL；args - SQL 参数。
    # Returns: 模拟 SQL 执行状态。
    async def execute(self, query, *args):
        self.commands.append(query)
        if query.startswith("UPDATE"):
            self.writes += 1
            if self.fail_second and self.writes == 2:
                raise RuntimeError("模拟数据库写入失败")
            table = next(name for name in self.rows if name in query)
            self.rows[table][0]["ciphertext"] = args[0]
        return "UPDATE 1"


class TestCredentialRotation:
    """覆盖新密钥写入、旧密钥读取及损坏密文失败关闭。"""

    # 方法作用：验证轮换后仅新密钥写入且仍能读取旧密文。
    # Args: self - 测试实例；monkeypatch - 环境补丁。
    # Returns: 无返回值。
    def test_versioned_rotation_keeps_old_readable(self, monkeypatch) -> None:
        """v2 和历史 v3 均可读，新写入始终标注当前 key_id。"""
        monkeypatch.setenv("ENV", "test")
        old_key = "old-secret-key-" * 4
        old = CredentialManager(old_key)
        old_versioned = CredentialManager(old_key, key_id="old")
        current = CredentialManager("new-secret-key-" * 4, key_id="new", previous_keys={"old": old_key})
        legacy = old.encrypt("database-password")
        previous = old_versioned.encrypt("api-key")
        fresh = current.encrypt("new-password")
        assert current.decrypt(legacy) == "database-password"
        assert current.decrypt(previous) == "api-key"
        assert fresh.startswith("v3:new:")
        assert current.decrypt(fresh) == "new-password"

    # 方法作用：验证未知密钥版本和损坏密文不能作为明文返回。
    # Args: self - 测试实例；monkeypatch - 环境补丁；token - 损坏密文。
    # Returns: 无返回值。
    @pytest.mark.parametrize("token", ["v3:unknown:bad:bad", "v3:new", "v3::bad:bad"])
    def test_invalid_versioned_ciphertext_rejected(self, monkeypatch, token) -> None:
        """任何 v3 前缀都必须验证，未知 ID 不尝试其他密钥。"""
        monkeypatch.setenv("ENV", "test")
        manager = CredentialManager("new-secret-key-" * 4, key_id="new")
        with pytest.raises(ValueError):
            manager.decrypt(token)

    # 方法作用：验证主密钥标识和旧密钥集合的输入约束。
    # Args: self - 测试实例；monkeypatch - 环境补丁；options - 无效参数。
    # Returns: 无返回值。
    @pytest.mark.parametrize("options", [
        {"key_id": "../key"}, {"key_id": "new", "previous_keys": {"new": "old-" * 10}},
        {"key_id": "new", "previous_keys": {"old": "short"}},
        {"previous_keys": {"old": "old-" * 10}},
    ])
    def test_invalid_keyring_rejected(self, monkeypatch, options) -> None:
        """无效配置在写入新密文之前拒绝，避免产生不可恢复数据。"""
        monkeypatch.setenv("ENV", "test")
        with pytest.raises(ValueError):
            CredentialManager("new-secret-key-" * 4, **options)

    # 方法作用：验证核验模式零写入，执行模式可重复且覆盖两个表。
    # Args: self - 实例；monkeypatch - 环境补丁。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_rotation_dry_run_apply_idempotent(self, monkeypatch) -> None:
        """重复执行不重复加密，所有行仍先完成解密核验。"""
        monkeypatch.setenv("ENV", "test")
        old_key = "old-key-" * 5
        old = CredentialManager(old_key)
        current = CredentialManager("new-key-" * 5, key_id="new", previous_keys={"old": old_key})
        connection = RotationConnection((old.encrypt("db-pass"), old.encrypt("api-key")))
        dry = await rotate_persisted_credentials(connection, current)
        assert dry == {"checked": 2, "changed": 0, "pending": 2}
        assert connection.writes == 0
        applied = await rotate_persisted_credentials(connection, current, apply=True)
        repeated = await rotate_persisted_credentials(connection, current, apply=True)
        assert applied == {"checked": 2, "changed": 2, "pending": 0}
        assert repeated == {"checked": 2, "changed": 0, "pending": 0}
        assert "SET LOCAL row_security = off" in connection.commands
        assert current.decrypt(connection.rows["datasource_configs"][0]["ciphertext"]) == "db-pass"

    # 方法作用：验证一张表写入失败后另一张表也回滚。
    # Args: self - 实例；monkeypatch - 环境补丁。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_rotation_failure_rolls_back_all(self, monkeypatch) -> None:
        """数据库异常不会留下只轮换了一部分表的状态。"""
        monkeypatch.setenv("ENV", "test")
        manager = CredentialManager("new-key-" * 5, key_id="new")
        connection = RotationConnection(("legacy-db", "legacy-api"))
        before = deepcopy(connection.rows)
        connection.fail_second = True
        with pytest.raises(RuntimeError):
            await rotate_persisted_credentials(connection, manager, apply=True)
        assert connection.rows == before

    # 方法作用：验证另一个轮换占用锁时不会读取或写入业务行。
    # Args: self - 实例；monkeypatch - 环境补丁。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_rotation_lock_conflict_rejected(self, monkeypatch) -> None:
        """竞争失败必须终止，不能绕过互斥锁继续执行。"""
        monkeypatch.setenv("ENV", "test")
        manager = CredentialManager("new-key-" * 5, key_id="new")
        connection = RotationConnection(("db", "api"))
        connection.locked = False
        with pytest.raises(RuntimeError, match="轮换"):
            await rotate_persisted_credentials(connection, manager, apply=True)
        assert connection.writes == 0

    # 方法作用：验证 CLI 不允许未经停机确认的写入，也不连接数据库。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_cli_requires_offline_confirmation(self) -> None:
        """缺少显式确认时应在启动任何维护资源前以参数错误退出。"""
        with pytest.raises(SystemExit) as captured:
            main(["--apply"])
        assert captured.value.code == 2
