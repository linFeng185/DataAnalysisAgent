"""Session、History 与 FileStore 租户隔离测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


logger = logging.getLogger(__name__)


class TestMemoryTenantIsolation:
    """覆盖内存回退路径的用户与租户隔离。"""

    async def test_sessions_are_invisible_to_another_identity(self, monkeypatch):
        """一个用户创建的会话不得被其他用户读取或删除。"""
        # Arrange
        import src.api.auth as auth
        import src.memory.session_store as session_module

        monkeypatch.setattr(session_module, "get_settings", lambda: SimpleNamespace(database_url=""))
        store = session_module.SessionStore()
        user_token = auth._current_user_id.set(101)  # noqa: SLF001
        tenant_token = auth._current_tenant_id.set(11)  # noqa: SLF001
        try:
            await store.create("session-a", "demo", "查询订单")
        finally:
            auth._current_user_id.reset(user_token)  # noqa: SLF001
            auth._current_tenant_id.reset(tenant_token)  # noqa: SLF001

        # Act
        user_token = auth._current_user_id.set(202)  # noqa: SLF001
        tenant_token = auth._current_tenant_id.set(22)  # noqa: SLF001
        try:
            foreign_list = await store.list()
            foreign_item = await store.get("session-a")
            foreign_delete = await store.delete("session-a")
        finally:
            auth._current_user_id.reset(user_token)  # noqa: SLF001
            auth._current_tenant_id.reset(tenant_token)  # noqa: SLF001

        # Assert
        assert foreign_list == []
        assert foreign_item is None
        assert foreign_delete is False

    async def test_history_is_invisible_to_another_identity(self, monkeypatch):
        """查询历史的内存回退也必须按用户和租户过滤。"""
        # Arrange
        import src.api.auth as auth
        import src.memory.history_store as history_module

        monkeypatch.setattr(history_module, "get_settings", lambda: SimpleNamespace(database_url=""))
        store = history_module.HistoryStore()
        user_token = auth._current_user_id.set(101)  # noqa: SLF001
        tenant_token = auth._current_tenant_id.set(11)  # noqa: SLF001
        try:
            store.add("查询订单", "demo", "session-a")
        finally:
            auth._current_user_id.reset(user_token)  # noqa: SLF001
            auth._current_tenant_id.reset(tenant_token)  # noqa: SLF001

        # Act
        user_token = auth._current_user_id.set(202)  # noqa: SLF001
        tenant_token = auth._current_tenant_id.set(22)  # noqa: SLF001
        try:
            result = await store.list()
        finally:
            auth._current_user_id.reset(user_token)  # noqa: SLF001
            auth._current_tenant_id.reset(tenant_token)  # noqa: SLF001

        # Assert
        assert result["history"] == []
        assert result["total"] == 0

    # 方法作用：验证查询历史内存回退完整保存每轮结构化响应。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_history_preserves_per_turn_final_result(self, monkeypatch):
        """每轮 SQL、数据、分析和图表必须作为同一条历史记录恢复。"""
        logger.debug("test_history_preserves_per_turn_final_result 入口")
        # Arrange
        import src.memory.history_store as history_module

        monkeypatch.setattr(history_module, "get_settings", lambda: SimpleNamespace(database_url=""))
        store = history_module.HistoryStore()
        final_result = {
            "success": True,
            "sql": "SELECT 1",
            "sql_statements": [{
                "datasource": "demo", "dialect": "sqlite", "sql": "SELECT 1",
            }],
            "data": [{"value": 1}],
            "row_count": 1,
            "truncated": False,
            "analysis": {"summary": "完整回答"},
            "chart": {"type": "table", "option": {}},
        }

        # Act
        store.add(
            "查询一", "demo", "session-rich",
            generated_sql="SELECT 1", final_result=final_result,
        )
        result = await store.list_session("session-rich")

        # Assert
        assert result[0]["final_result"] == final_result
        assert result[0]["turn_id"] == 1
        logger.info("test_history_preserves_per_turn_final_result 完成")


class TestFileStoreTenantIsolation:
    """覆盖知识文件 PostgreSQL 查询的身份过滤。"""

    async def test_file_queries_include_tenant_and_user(self, monkeypatch):
        """文件查询必须分离系统、当前租户和当前用户三种可见范围。"""
        # Arrange
        import asyncpg

        import src.api.auth as auth
        import src.knowledge.file_store as file_module

        connection = SimpleNamespace(
            fetchrow=AsyncMock(return_value={"id": 1}),
            fetch=AsyncMock(return_value=[]),
            execute=AsyncMock(return_value="DELETE 0"),
            close=AsyncMock(),
        )
        monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=connection))
        monkeypatch.setattr(file_module, "get_settings", lambda: SimpleNamespace(
            database_url="postgresql+asyncpg://test",
        ))
        store = file_module.FileStore()
        store._ready = True  # noqa: SLF001
        user_token = auth._current_user_id.set(101)  # noqa: SLF001
        tenant_token = auth._current_tenant_id.set(11)  # noqa: SLF001
        role_token = auth._current_role.set("analyst")  # noqa: SLF001

        try:
            # Act / Assert: save
            await store.save("a.txt", b"hello", knowledge_scope="private", tag_ids=[1, 2])
            save_call = connection.fetchrow.await_args
            assert "knowledge_scope" in save_call.args[0]
            assert save_call.args[-5:] == (11, 101, "private", "", [1, 2])

            # Act / Assert: get
            connection.fetchrow.reset_mock(return_value=True)
            connection.fetchrow.return_value = None
            await store.get(1)
            get_call = connection.fetchrow.await_args
            assert "knowledge_scope = 'system'" in get_call.args[0]
            assert "knowledge_scope = 'tenant'" in get_call.args[0]
            assert "knowledge_scope = 'private'" in get_call.args[0]
            assert get_call.args[-2:] == (11, 101)

            # Act / Assert: get_by_name
            connection.fetchrow.reset_mock(return_value=True)
            connection.fetchrow.return_value = None
            await store.get_by_name("a.txt", knowledge_scope="private")
            name_call = connection.fetchrow.await_args
            assert "knowledge_scope = 'system'" in name_call.args[0]
            assert "knowledge_scope = 'tenant'" in name_call.args[0]
            assert "knowledge_scope = 'private'" in name_call.args[0]
            assert name_call.args[-3:] == (11, 101, "private")

            # Act / Assert: list_files
            await store.list_files()
            list_call = connection.fetch.await_args
            assert "knowledge_scope = 'system'" in list_call.args[0]
            assert "knowledge_scope = 'tenant'" in list_call.args[0]
            assert "knowledge_scope = 'private'" in list_call.args[0]
            assert list_call.args[-2:] == (11, 101)

            # Act / Assert: delete
            await store.delete("a.txt", knowledge_scope="private")
            delete_call = connection.execute.await_args
            assert "knowledge_scope = 'system'" in delete_call.args[0]
            assert "knowledge_scope = 'tenant'" in delete_call.args[0]
            assert "knowledge_scope = 'private'" in delete_call.args[0]
            assert delete_call.args[-4:] == (11, 101, "analyst", "private")
        finally:
            auth._current_user_id.reset(user_token)  # noqa: SLF001
            auth._current_tenant_id.reset(tenant_token)  # noqa: SLF001
            auth._current_role.reset(role_token)  # noqa: SLF001


class TestTenantMigration:
    """覆盖租户表结构与 RLS 迁移顺序。"""

    def test_tables_exist_before_alter_and_policies_combine_identity(self):
        """迁移应先建业务表，并用单一 AND policy 同时校验租户和用户。"""
        # Arrange
        from pathlib import Path

        sql = Path("migrations/001_batch1.sql").read_text(encoding="utf-8")

        # Act
        sessions_create = sql.index("CREATE TABLE IF NOT EXISTS sessions")
        sessions_alter = sql.index("ALTER TABLE sessions")
        history_create = sql.index("CREATE TABLE IF NOT EXISTS query_history")
        history_alter = sql.index("ALTER TABLE query_history")

        # Assert
        assert sessions_create < sessions_alter
        assert history_create < history_alter
        assert "CREATE POLICY session_identity_isolation" in sql
        assert "CREATE POLICY history_identity_isolation" in sql
        assert "tenant_id =" in sql and "AND user_id =" in sql


class TestSessionRouteIsolation:
    """覆盖会话轮次 API 的资源归属检查。"""

    async def test_turns_reject_unknown_session_before_checkpointer_access(self, monkeypatch):
        """无权会话不得直接读取 LangGraph Checkpointer。"""
        # Arrange
        import src.api.routes as routes
        import src.memory.session_store as session_module

        store = SimpleNamespace(get=AsyncMock(return_value=None))
        monkeypatch.setattr(session_module, "get_session_store", lambda: store)

        # Act / Assert
        with pytest.raises(Exception) as caught:
            await routes.list_session_turns("foreign-session", limit=20)
        assert getattr(caught.value, "status_code", None) == 404
        store.get.assert_awaited_once_with("foreign-session")

    async def test_turns_fall_back_to_query_history_when_checkpoint_missing(self, monkeypatch):
        """Checkpointer 无状态时，会话接口应从持久化查询历史恢复轮次。"""
        import src.api.routes as routes
        import src.memory.checkpointer as checkpointer_module
        import src.memory.history_store as history_module

        checkpoint = SimpleNamespace(aget_tuple=AsyncMock(return_value=None))
        history = SimpleNamespace(list_session=AsyncMock(return_value=[{
            "query": "查询订单", "sql": "SELECT 1", "success": True,
            "row_count": 1, "time": "2026-07-16 23:00:00",
        }]))
        monkeypatch.setattr(checkpointer_module, "get_checkpointer", AsyncMock(return_value=checkpoint))
        monkeypatch.setattr(history_module, "get_history_store", lambda: history)

        turns = await routes._load_session_turns("session-fixed")

        assert len(turns) == 1
        assert turns[0]["user_query"] == "查询订单"
        assert turns[0]["sql"] == "SELECT 1"
        history.list_session.assert_awaited_once_with("session-fixed", before=None, limit=20)

    async def test_turns_restore_partial_checkpoint_user_query(self, monkeypatch):
        """旧 checkpoint 只有 user_query 时，也应恢复一条未完成对话。"""
        import src.api.routes as routes
        import src.memory.checkpointer as checkpointer_module
        import src.memory.history_store as history_module

        checkpoint_tuple = SimpleNamespace(checkpoint={"channel_values": {
            "user_query": "查询 Oracle 客户消费",
            "messages": [],
            "conversation_history": [],
            "execution_error": "Schema 加载失败",
        }})
        checkpoint = SimpleNamespace(aget_tuple=AsyncMock(return_value=checkpoint_tuple))
        history = SimpleNamespace(list_session=AsyncMock(return_value=[]))
        monkeypatch.setattr(checkpointer_module, "get_checkpointer", AsyncMock(return_value=checkpoint))
        monkeypatch.setattr(history_module, "get_history_store", lambda: history)

        turns = await routes._load_session_turns("legacy-session")

        assert len(turns) == 1
        assert turns[0]["user_query"] == "查询 Oracle 客户消费"
        assert turns[0]["assistant_summary"] == "Schema 加载失败"
        assert turns[0]["sql"] == ""
        assert turns[0]["final_result"]["analysis"]["summary"] == "Schema 加载失败"
        history.list_session.assert_not_awaited()

    # 验证命名空间迁移前的 checkpoint 仍可恢复完整对话轮次。
    # Args: monkeypatch - pytest 提供的运行时替换工具。
    # Returns: 无返回值，断言旧 thread_id 回退结果。
    async def test_turns_fall_back_to_legacy_checkpoint_thread_id(self, monkeypatch):
        """scoped checkpoint 为空时，应继续查询原始 session_id。"""
        # Arrange
        from langchain_core.messages import AIMessage, HumanMessage
        import src.api.auth as auth
        import src.api.routes as routes
        import src.memory.checkpointer as checkpointer_module
        import src.memory.history_store as history_module

        legacy_tuple = SimpleNamespace(checkpoint={"channel_values": {
            "messages": [
                HumanMessage(content="按商品分类统计销售额"),
                AIMessage(content="SQL: SELECT category, SUM(amount) FROM orders GROUP BY category\n结论: 已按销售额降序统计"),
            ],
        }})
        checkpoint = SimpleNamespace(
            aget_tuple=AsyncMock(side_effect=[None, legacy_tuple]),
        )
        history = SimpleNamespace(list_session=AsyncMock(return_value=[]))
        monkeypatch.setattr(checkpointer_module, "get_checkpointer", AsyncMock(return_value=checkpoint))
        monkeypatch.setattr(history_module, "get_history_store", lambda: history)

        # Act
        turns = await routes._load_session_turns("legacy-session")

        # Assert
        scoped_id = auth.scope_thread_id("legacy-session")
        assert checkpoint.aget_tuple.await_args_list[0].args[0]["configurable"]["thread_id"] == scoped_id
        assert checkpoint.aget_tuple.await_args_list[1].args[0]["configurable"]["thread_id"] == "legacy-session"
        assert len(turns) == 1
        assert turns[0]["user_query"] == "按商品分类统计销售额"
        assert turns[0]["assistant_summary"] == "已按销售额降序统计"
        assert turns[0]["sql"] == "SELECT category, SUM(amount) FROM orders GROUP BY category"
        assert turns[0]["final_result"]["sql"] == turns[0]["sql"]
        history.list_session.assert_not_awaited()

    # 验证最新富状态也兼容命名空间迁移前的 checkpoint。
    # Args: monkeypatch - pytest 提供的运行时替换工具。
    # Returns: 无返回值，断言旧 thread_id 的最新状态。
    async def test_latest_state_falls_back_to_legacy_checkpoint_thread_id(self, monkeypatch):
        """scoped checkpoint 为空时，最新状态应从原始 session_id 恢复。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes as routes
        import src.memory.checkpointer as checkpointer_module
        import src.memory.history_store as history_module

        legacy_tuple = SimpleNamespace(checkpoint={"channel_values": {
            "generated_sql": "SELECT category, SUM(amount) FROM orders GROUP BY category",
            "analysis_result": {"summary": "分类销售额统计完成"},
            "chart_config": {"type": "bar"},
            "query_result_sample": [{"category": "数码", "amount": 100}],
            "execution_error": "",
        }})
        checkpoint = SimpleNamespace(
            aget_tuple=AsyncMock(side_effect=[None, legacy_tuple]),
        )
        history = SimpleNamespace(list_session=AsyncMock(return_value=[]))
        monkeypatch.setattr(checkpointer_module, "get_checkpointer", AsyncMock(return_value=checkpoint))
        monkeypatch.setattr(history_module, "get_history_store", lambda: history)

        # Act
        state = await routes._load_latest_state("legacy-session")

        # Assert
        scoped_id = auth.scope_thread_id("legacy-session")
        assert checkpoint.aget_tuple.await_args_list[0].args[0]["configurable"]["thread_id"] == scoped_id
        assert checkpoint.aget_tuple.await_args_list[1].args[0]["configurable"]["thread_id"] == "legacy-session"
        assert state["sql"] == "SELECT category, SUM(amount) FROM orders GROUP BY category"
        assert state["analysis"] == {"summary": "分类销售额统计完成"}
        assert state["chart"] == {"type": "bar"}
        assert state["data"] == [{"category": "数码", "amount": 100}]
        assert state["success"] is True
        assert state["sql_statements"] == []
        history.list_session.assert_not_awaited()


class TestSessionStorePagination:
    """覆盖会话存储的 PostgreSQL 游标分页。"""

    # 验证 ISO 游标在进入 asyncpg 前转换为带时区的 datetime。
    # Args: monkeypatch - pytest 提供的运行时替换工具。
    # Returns: 无返回值，断言 PostgreSQL 查询参数类型。
    async def test_list_converts_iso_cursor_for_asyncpg(self, monkeypatch):
        """asyncpg 的 timestamptz 参数不得直接接收字符串。"""
        # Arrange
        from datetime import datetime
        import src.memory.session_store as session_module

        connection = SimpleNamespace(
            fetch=AsyncMock(return_value=[]),
            close=AsyncMock(),
        )
        store = session_module.SessionStore()
        monkeypatch.setattr(store, "_ensure_pg", AsyncMock(return_value=True))
        monkeypatch.setattr(store, "_pg_conn", AsyncMock(return_value=connection))
        cursor = "2026-07-13T13:12:34.377239+00:00"

        # Act
        result = await store.list(cursor=cursor, limit=21)

        # Assert
        pg_cursor = connection.fetch.await_args.args[3]
        assert isinstance(pg_cursor, datetime)
        assert pg_cursor.isoformat() == cursor
        assert result == []


class TestUploadTaskIsolation:
    """覆盖后台上传任务的身份隔离。"""

    def test_upload_tasks_are_invisible_to_another_identity(self):
        """任务创建者之外的用户不得查询任务状态。"""
        # Arrange
        import src.api.auth as auth
        from src.knowledge.upload_manager import UploadManager

        manager = UploadManager()
        user_token = auth._current_user_id.set(101)  # noqa: SLF001
        tenant_token = auth._current_tenant_id.set(11)  # noqa: SLF001
        try:
            task = manager.create("private.txt")
        finally:
            auth._current_user_id.reset(user_token)  # noqa: SLF001
            auth._current_tenant_id.reset(tenant_token)  # noqa: SLF001

        # Act
        user_token = auth._current_user_id.set(202)  # noqa: SLF001
        tenant_token = auth._current_tenant_id.set(22)  # noqa: SLF001
        try:
            foreign_task = manager.get(task.id)
            foreign_list = manager.list_recent()
        finally:
            auth._current_user_id.reset(user_token)  # noqa: SLF001
            auth._current_tenant_id.reset(tenant_token)  # noqa: SLF001

        # Assert
        assert foreign_task is None
        assert foreign_list == []


class TestKnowledgeGovernanceMigration:
    """覆盖三范围文件 ACL 与标签初始化迁移。"""

    # 验证迁移包含三范围 RLS 和两级管理员边界。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_migration_defines_scope_policies_and_admin_roles(self) -> None:
        """数据库策略必须区分平台、租户和个人写权限。"""
        # Arrange
        from pathlib import Path

        sql = Path("migrations/003_knowledge_governance.sql").read_text(encoding="utf-8")

        # Act / Assert
        assert "knowledge_scope IN ('system', 'tenant', 'private')" in sql
        assert "knowledge_files_read_scope" in sql
        assert "knowledge_files_insert_scope" in sql
        assert "'super_admin', 'tenant_admin'" in sql
        assert "owner_user_id" in sql

    # 验证初始化标签不包含业务领域固定数据。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_seed_tags_exclude_business_domains(self) -> None:
        """订单、客户、财务等业务标签应留给超级管理员维护。"""
        # Arrange
        from pathlib import Path

        sql = Path("migrations/003_knowledge_governance.sql").read_text(encoding="utf-8")

        # Act / Assert
        assert "数据字典" in sql
        assert "PostgreSQL" in sql
        assert "ClickHouse" in sql
        assert "('订单'" not in sql
        assert "('客户'" not in sql
        assert "('财务'" not in sql
class TestMCPResourceScopeMigration:
    """覆盖功能 8.4.1：MCP 三级作用域数据库策略。"""

    # 方法作用：验证迁移可独立建表并包含三级读取和写入策略。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_mcp_scope_migration_is_self_contained(self) -> None:
        """迁移必须支持空数据库，并约束 system/tenant/private 所有者字段。"""
        # Arrange
        from pathlib import Path

        sql = Path("migrations/004_resource_scopes.sql").read_text(encoding="utf-8")

        # Act / Assert
        assert "CREATE TABLE IF NOT EXISTS mcp_servers" in sql
        assert "scope IN ('system', 'tenant', 'private')" in sql
        assert "mcp_servers_read_scope" in sql
        assert "mcp_servers_update_scope" in sql
        assert "owner_user_id" in sql
