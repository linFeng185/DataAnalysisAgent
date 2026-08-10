"""租户物理删除的运行时与关系数据清理测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


class TestTenantPurge:
    """覆盖租户删除的缓存、Skill、MCP、Checkpointer 和关系表边界。"""

    # 方法作用：验证固定顺序 SQL 覆盖全部租户关系表并汇总删除数量。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值。
    async def test_purge_tenant_rows_covers_scoped_tables(self) -> None:
        """租户删除必须先清理叶子表，再由路由删除 tenants 主记录。"""
        # Arrange
        from src.tenant_purge import purge_tenant_rows

        connection = SimpleNamespace(execute=AsyncMock(return_value="DELETE 1"))

        # Act
        result = await purge_tenant_rows(connection, 9)

        # Assert
        assert result["analysis_notifications"] == 1
        assert result["knowledge_sql_observations"] == 1
        assert result["tenant_llm_connections"] == 1
        assert result["users"] == 1
        assert connection.execute.await_count == len(result)

    # 方法作用：验证租户运行时清理会同步清除所有非关系型残留。
    # Args: self - pytest 测试类实例；monkeypatch/tmp_path - 依赖替身与临时 Skill 目录。
    # Returns: 无返回值。
    async def test_purge_tenant_runtime_removes_all_scoped_state(self, monkeypatch, tmp_path) -> None:
        """向量、Schema 缓存、Skill、MCP、会话和 checkpoint 均必须按租户清理。"""
        # Arrange
        import src.datasource.registry as registry_module
        import src.memory.checkpointer as checkpointer_module
        import src.memory.vector_store as vector_module
        from src.tenant_purge import purge_tenant_runtime

        registry = SimpleNamespace(unregister=AsyncMock(return_value=True))
        vector = SimpleNamespace(delete_by_filter=AsyncMock(return_value=3))
        cache = SimpleNamespace(delete=AsyncMock())
        skill_manager = SimpleNamespace(skills={}, managed_dir=tmp_path, remove_skill=MagicMock())
        (tmp_path / "tenant" / "9").mkdir(parents=True)
        (tmp_path / "private" / "9" / "7").mkdir(parents=True)
        mcp_manager = SimpleNamespace(
            _server_tenants={"tenant_9": 9, "system": 0},
            _loaded_identities={(9, 7), (1, 1)},
            remove_server=AsyncMock(),
        )
        checkpointer = SimpleNamespace(adelete_thread=AsyncMock())
        session_store = SimpleNamespace(_items=[{"tenant_id": 9}, {"tenant_id": 1}])
        history_store = SimpleNamespace(_items=[{"tenant_id": 9}, {"tenant_id": 1}])
        upload_manager = SimpleNamespace(_tasks={
            "mine": SimpleNamespace(tenant_id=9),
            "other": SimpleNamespace(tenant_id=1),
        })
        evolution = SimpleNamespace(
            _local_observations={(9, 7, "a"): {}, (1, 1, "b"): {}},
            _promoted={(9, 7, "a"), (1, 1, "b")},
            _local_claims={(9, 7, "a"): "mine", (1, 1, "b"): "other"},
        )
        resources = {
            "vector_store": vector,
            "datasource_cache": cache,
            "skill_manager": skill_manager,
            "mcp_client_manager": mcp_manager,
            "session_store": session_store,
            "history_store": history_store,
            "upload_manager": upload_manager,
            "knowledge_evolution_service": evolution,
        }
        context = SimpleNamespace(get_resource=MagicMock(side_effect=resources.get))
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
        monkeypatch.setattr(vector_module, "get_vector_store", AsyncMock(return_value=vector))
        monkeypatch.setattr(checkpointer_module, "get_checkpointer", AsyncMock(return_value=checkpointer))
        datasource_rows = [{
            "name": "warehouse", "dialect": "postgres", "host": "db", "port": 5432,
            "database_name": "app", "username": "reader", "extra_params": {},
        }]
        session_rows = [{"session_id": "session-1", "user_id": 7}]

        # Act
        result = await purge_tenant_runtime(
            9, datasource_rows, session_rows, context=context,
        )

        # Assert
        assert result["datasources"] == 1
        assert result["vector_entries"] == 3
        assert result["cache_entries"] == 1
        assert result["mcp_servers"] == 1
        assert result["checkpoints"] == 2
        assert result["sessions"] == 1
        assert result["history"] == 1
        assert result["upload_tasks"] == 1
        assert not (tmp_path / "tenant" / "9").exists()
        assert not (tmp_path / "private" / "9").exists()
        assert (9, 7) not in mcp_manager._loaded_identities
        assert all(key[0] != 9 for key in evolution._local_observations)
        assert checkpointer.adelete_thread.await_count == 2
