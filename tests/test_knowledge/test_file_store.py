"""知识文件存储权限和连接生命周期回归测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock


class TestFileStore:
    """覆盖文件删除的角色归一化和租户范围约束。"""

    # 验证删除 SQL 对角色做归一化，并限制 super_admin 的租户范围。
    # Args: self - 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_delete_normalizes_role_and_keeps_tenant_scope(self, monkeypatch) -> None:
        import src.api.auth as auth_module
        import src.knowledge.file_store as file_module

        connection = SimpleNamespace(
            execute=AsyncMock(return_value="DELETE 1"),
            close=AsyncMock(),
        )
        store = file_module.FileStore()
        monkeypatch.setattr(store, "_ensure", AsyncMock())

        # 方法作用：提供 FileStore 池化连接测试上下文。
        # Args: 无。
        # Returns: 测试事务范围内的数据库连接。
        @asynccontextmanager
        async def scoped_connection():
            yield connection

        monkeypatch.setattr(store, "_connect", scoped_connection)
        monkeypatch.setattr(file_module, "_current_identity", lambda: (7, 9))
        monkeypatch.setattr(auth_module, "get_current_role", lambda: " SUPER_ADMIN ")

        result = await store.delete("common.md", knowledge_scope="tenant")

        sql = connection.execute.await_args.args[0]
        params = connection.execute.await_args.args[1:]
        assert result is True
        assert params[3] == "super_admin"
        assert "tenant_id = $2" in sql
        assert "knowledge_scope IN ('tenant', 'private')" not in sql
