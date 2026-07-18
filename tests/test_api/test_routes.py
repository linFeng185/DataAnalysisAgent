"""API 路由测试 — Schema + 端点 + 异常处理。"""

from __future__ import annotations

import logging

import pytest
from httpx import ASGITransport, AsyncClient


logger = logging.getLogger(__name__)


@pytest.fixture
def client():
    from src.main import create_app
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


class TestSchemas:
    """11.2"""

    def test_chat_request(self):
        from src.api.schemas import ChatRequest
        assert ChatRequest(query="q").datasource == "demo"

    def test_chat_request_requires_query(self):
        from src.api.schemas import ChatRequest
        with pytest.raises(Exception):
            ChatRequest()

    def test_datasource_create(self):
        from src.api.schemas import DataSourceCreateRequest
        r = DataSourceCreateRequest(name="ch", dialect="clickhouse")
        assert r.name == "ch"

    def test_health_response(self):
        from src.api.schemas import HealthResponse
        assert HealthResponse().status == "ok"

    def test_chat_response_reports_truncation(self):
        """非流式聊天响应应保留结果行数和截断标记。"""
        # Arrange
        from src.api.schemas import ChatResponse

        # Act
        response = ChatResponse(success=True, row_count=2, truncated=True)

        # Assert
        assert response.row_count == 2
        assert response.truncated is True

    # 方法作用：验证聊天响应可以返回每个数据源处理后的最终 SQL。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chat_response_exposes_sql_statements(self):
        """多数据源响应必须保留带数据源和方言的 SQL 列表。"""
        logger.debug("test_chat_response_exposes_sql_statements 入口")
        try:
            # Arrange
            from src.api.schemas import ChatResponse

            statements = [{
                "datasource": "mysql_test",
                "dialect": "mysql",
                "sql": "SELECT 1",
            }]

            # Act
            response = ChatResponse(success=True, sql_statements=statements)

            # Assert
            assert response.sql_statements == statements
            logger.info("test_chat_response_exposes_sql_statements 完成")
        except Exception as exc:
            logger.error(
                "test_chat_response_exposes_sql_statements 异常: %s",
                exc,
                exc_info=True,
            )
            raise


class TestEndpoints:
    """11.1"""

    async def test_health(self, client):
        r = await client.get("/api/v1/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    async def test_chat(self, client, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        import src.api.routes as routes

        workflow = SimpleNamespace(ainvoke=AsyncMock(return_value={
            "final_response": {"success": True},
            "generated_sql": "",
            "query_result_sample": [],
            "query_result_full_count": 0,
            "query_result_truncated": False,
            "analysis_result": {},
            "chart_config": {},
        }))
        monkeypatch.setattr(routes, "_app", lambda: workflow)
        r = await client.post("/api/v1/chat", json={"query": "test", "datasource": "ch"})
        assert r.status_code == 200

    # 方法作用：验证非流式 API 使用 build_response 中的最终执行 SQL 契约。
    # Args: self - pytest 测试类实例；client - ASGI 客户端；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_chat_returns_processed_sql_statements(self, client, monkeypatch):
        """API 不得用工作流顶层的原始 SQL 覆盖处理后 SQL。"""
        logger.debug("test_chat_returns_processed_sql_statements 入口")
        try:
            # Arrange
            from types import SimpleNamespace
            from unittest.mock import AsyncMock

            import src.api.routes as routes

            statements = [{
                "datasource": "mysql_test",
                "dialect": "mysql",
                "sql": "SELECT `category_name` FROM `categories` LIMIT 1",
            }]
            workflow = SimpleNamespace(ainvoke=AsyncMock(return_value={
                "generated_sql": "SELECT category_name FROM categories",
                "final_response": {
                    "success": True,
                    "sql": statements[0]["sql"],
                    "sql_statements": statements,
                    "data": [],
                    "row_count": 0,
                    "truncated": False,
                    "analysis": {},
                    "chart": {},
                },
            }))
            monkeypatch.setattr(routes, "_app", lambda: workflow)

            # Act
            response = await client.post("/api/v1/chat", json={
                "query": "查询分类", "datasource": "mysql_test",
            })

            # Assert
            payload = response.json()
            assert response.status_code == 200
            assert payload["sql"] == statements[0]["sql"]
            assert payload["sql_statements"] == statements
            logger.info("test_chat_returns_processed_sql_statements 完成")
        except Exception as exc:
            logger.error(
                "test_chat_returns_processed_sql_statements 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    async def test_chat_passes_session_id_into_workflow(self, client, monkeypatch):
        """复用已有会话时，workflow 必须收到同一个 session_id 以持久化轮次。"""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        import src.api.routes as routes

        workflow = SimpleNamespace(ainvoke=AsyncMock(return_value={
            "final_response": {"success": True},
            "generated_sql": "",
            "query_result_sample": [],
            "query_result_full_count": 0,
            "query_result_truncated": False,
            "analysis_result": {},
            "chart_config": {},
        }))
        monkeypatch.setattr(routes, "_app", lambda: workflow)

        response = await client.post("/api/v1/chat", json={
            "query": "test", "datasource": "ch", "session_id": "session-fixed",
        })

        assert response.status_code == 200
        workflow.ainvoke.assert_awaited_once()
        assert workflow.ainvoke.await_args.args[0]["session_id"] == "session-fixed"

    # 验证非流式聊天与 SSE 使用相同的多数据源状态字段。
    # Args: self - pytest 测试类实例；client - HTTP 测试客户端；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_chat_passes_selected_datasources_into_workflow(
        self,
        client,
        monkeypatch,
    ):
        """stream=False 时 datasource 列表也必须进入多源调度。"""
        logger.debug("test_chat_passes_selected_datasources_into_workflow 入口")
        try:
            # Arrange：隔离真实工作流并记录入口状态。
            from types import SimpleNamespace
            from unittest.mock import AsyncMock

            import src.api.routes as routes

            workflow = SimpleNamespace(ainvoke=AsyncMock(return_value={
                "final_response": {"success": True},
                "query_result_sample": [],
                "query_result_full_count": 0,
                "analysis_result": {},
                "chart_config": {},
            }))
            monkeypatch.setattr(routes, "_app", lambda: workflow)
            selected = ["mysql_test", "clickhouse_test", "demo"]

            # Act
            response = await client.post("/api/v1/chat", json={
                "query": "汇总客户总数",
                "datasource": "mysql_test",
                "datasources": selected,
                "stream": False,
            })

            # Assert
            assert response.status_code == 200
            state = workflow.ainvoke.await_args.args[0]
            assert state["selected_datasources"] == selected
            logger.info(
                "test_chat_passes_selected_datasources_into_workflow 完成",
                extra={"source_count": len(selected)},
            )
        except Exception as exc:
            logger.error(
                "test_chat_passes_selected_datasources_into_workflow 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    async def test_list_datasources(self, client):
        r = await client.get("/api/v1/datasources")
        assert r.status_code == 200

    async def test_create_datasource(self, client):
        r = await client.post("/api/v1/datasources", json={
            "name": "tmp", "dialect": "clickhouse",
            "host": "localhost", "database": "test", "username": "r", "password": "p",
        })
        assert r.status_code in (200, 201)

    async def test_schema_not_found(self, client):
        r = await client.get("/api/v1/schema/tables?datasource=nonexistent")
        assert r.status_code == 404

    async def test_schema_tables_trigger_lazy_introspection(self, client, monkeypatch):
        """外部数据源延迟加载时，Schema 路由应调用 SchemaManager 获取表结构。"""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from src.datasource.schema_snapshot import SchemaSnapshot, TableSchema
        import src.api.routes as routes

        datasource = SimpleNamespace(schema=None)
        snapshot = SchemaSnapshot(tables=[TableSchema(name="orders")])
        registry = SimpleNamespace(resolve=AsyncMock(return_value=datasource))
        manager = SimpleNamespace(get_or_fetch_schema=AsyncMock(return_value=snapshot))
        monkeypatch.setattr(routes, "_registry", lambda: registry)
        monkeypatch.setattr(routes, "_schema_manager", lambda: manager)

        response = await client.get("/api/v1/schema/tables?datasource=oracle_xe")

        assert response.status_code == 200
        assert response.json()["tables"][0]["name"] == "orders"
        manager.get_or_fetch_schema.assert_awaited_once_with("oracle_xe")

    # 验证会话列表仅在确有下一条记录时报告下一页。
    # Args: monkeypatch - pytest 提供的运行时替换工具。
    # Returns: 无返回值，断言恰好一页的分页元数据。
    async def test_list_sessions_does_not_report_empty_next_page(self, monkeypatch):
        """恰好返回 limit 条时，has_more 应为 false。"""
        # Arrange
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        import src.api.routes as routes
        import src.memory.session_store as session_module

        sessions = [
            {"session_id": f"session-{index}", "last_active_at": f"2026-07-{20 - index:02d}T00:00:00+00:00"}
            for index in range(20)
        ]
        store = SimpleNamespace(list=AsyncMock(return_value=sessions))
        monkeypatch.setattr(session_module, "get_session_store", lambda: store)

        # Act
        result = await routes.list_sessions(cursor=None, limit=20)

        # Assert
        store.list.assert_awaited_once_with(cursor=None, limit=21)
        assert result["sessions"] == sessions
        assert result["next_cursor"] is None
        assert result["has_more"] is False

    # 验证多取一条记录能准确生成下一页游标。
    # Args: monkeypatch - pytest 提供的运行时替换工具。
    # Returns: 无返回值，断言超出一页时的分页元数据。
    async def test_list_sessions_reports_real_next_page(self, monkeypatch):
        """返回 limit + 1 条时，应裁剪结果并报告下一页。"""
        # Arrange
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        import src.api.routes as routes
        import src.memory.session_store as session_module

        sessions = [
            {"session_id": f"session-{index}", "last_active_at": f"2026-07-{21 - index:02d}T00:00:00+00:00"}
            for index in range(21)
        ]
        store = SimpleNamespace(list=AsyncMock(return_value=sessions))
        monkeypatch.setattr(session_module, "get_session_store", lambda: store)

        # Act
        result = await routes.list_sessions(cursor=None, limit=20)

        # Assert
        assert result["sessions"] == sessions[:20]
        assert result["next_cursor"] == sessions[19]["last_active_at"]
        assert result["has_more"] is True

    async def test_chat_validation(self, client):
        r = await client.post("/api/v1/chat", json={})
        assert r.status_code == 422

    # 方法作用：验证多源会话最新状态优先使用 checkpoint 的最终响应。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_load_latest_state_preserves_multi_source_final_response(
        self, monkeypatch,
    ):
        """顶层 generated_sql 为空时不得丢弃 final_response 富数据。"""
        logger.debug("test_load_latest_state_preserves_multi_source_final_response 入口")
        try:
            # Arrange：模拟多源查询最终 checkpoint，顶层 SQL 为空但最终响应完整。
            from types import SimpleNamespace
            from unittest.mock import AsyncMock

            import src.api.routes as routes
            import src.memory.history_store as history_module

            rows = [{"source": "mysql", "value": index} for index in range(35)]
            statements = [
                {"datasource": "mysql", "dialect": "mysql", "sql": "SELECT 1"},
                {"datasource": "pg", "dialect": "postgres", "sql": "SELECT 2"},
            ]
            final_response = {
                "success": True,
                "sql": "-- mysql\nSELECT 1\n-- pg\nSELECT 2",
                "sql_statements": statements,
                "data": rows,
                "row_count": 35,
                "truncated": False,
                "analysis": {"summary": "完整结论"},
                "chart": {"type": "bar", "option": {"series": []}},
                "sql_reasoning_content": "处理后的 SQL 推理",
            }
            checkpoint = SimpleNamespace(checkpoint={"channel_values": {
                "generated_sql": "",
                "final_response": final_response,
                "query_result_sample": rows,
            }})
            monkeypatch.setattr(
                routes, "_load_checkpoint_tuple", AsyncMock(return_value=checkpoint),
            )
            history = SimpleNamespace(list_session=AsyncMock(return_value=[]))
            monkeypatch.setattr(history_module, "get_history_store", lambda: history)

            # Act
            result = await routes._load_latest_state("session-rich")

            # Assert：SQL 列表、全部样本、分析与图表必须原样恢复。
            assert result["sql_statements"] == statements
            assert result["data"] == rows
            assert result["analysis"] == {"summary": "完整结论"}
            assert result["chart"]["type"] == "bar"
            assert result["row_count"] == 35
            assert result["sql_reasoning_content"] == "处理后的 SQL 推理"
            history.list_session.assert_not_awaited()
            logger.info("test_load_latest_state_preserves_multi_source_final_response 完成")
        except Exception as exc:
            logger.error(
                "test_load_latest_state_preserves_multi_source_final_response 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证每一轮会话都合并自身持久化的结构化响应。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_load_session_turns_restores_each_turn_final_result(
        self, monkeypatch,
    ):
        """历史会话的第一轮和第二轮都应包含自己的 SQL、数据与分析。"""
        logger.debug("test_load_session_turns_restores_each_turn_final_result 入口")
        try:
            # Arrange：checkpoint 保存完整摘要，查询历史保存逐轮富数据。
            from types import SimpleNamespace
            from unittest.mock import AsyncMock

            import src.api.routes as routes
            import src.memory.history_store as history_module

            long_summary = "第一轮完整回答" * 40
            checkpoint = SimpleNamespace(checkpoint={"channel_values": {
                "messages": [],
                "conversation_history": [
                    {"turn_id": 1, "user_query": "问题一", "generated_sql": "SQL-1",
                     "analysis_summary": long_summary},
                    {"turn_id": 2, "user_query": "问题二", "generated_sql": "SQL-2",
                     "analysis_summary": "第二轮完整回答"},
                ],
            }})
            rich_rows = [
                {"turn_id": 1, "query": "问题一", "sql": "SQL-1", "time": "T1",
                 "final_result": {"success": True, "sql": "SQL-1",
                                  "sql_statements": [{"datasource": "a", "dialect": "mysql", "sql": "SQL-1"}],
                                  "data": [{"value": 1}], "analysis": {"summary": long_summary},
                                  "chart": {"type": "table", "option": {}}}},
                {"turn_id": 2, "query": "问题二", "sql": "SQL-2", "time": "T2",
                 "final_result": {"success": True, "sql": "SQL-2",
                                  "sql_statements": [{"datasource": "b", "dialect": "postgres", "sql": "SQL-2"}],
                                  "data": [{"value": 2}], "analysis": {"summary": "第二轮完整回答"},
                                  "chart": {"type": "bar", "option": {}}}},
            ]
            monkeypatch.setattr(
                routes, "_load_checkpoint_tuple", AsyncMock(return_value=checkpoint),
            )
            history = SimpleNamespace(list_session=AsyncMock(return_value=rich_rows))
            monkeypatch.setattr(history_module, "get_history_store", lambda: history)

            # Act
            turns = await routes._load_session_turns("session-rich", limit=20)

            # Assert：摘要不截断，两轮各自带完整结构化结果。
            assert len(turns) == 2
            assert turns[0]["assistant_summary"] == long_summary
            assert turns[0]["final_result"]["data"] == [{"value": 1}]
            assert turns[1]["final_result"]["sql_statements"][0]["datasource"] == "b"
            assert turns[1]["final_result"]["chart"]["type"] == "bar"
            logger.info("test_load_session_turns_restores_each_turn_final_result 完成")
        except Exception as exc:
            logger.error(
                "test_load_session_turns_restores_each_turn_final_result 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证会话轮次分页默认返回最新一页并支持向前翻页。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_load_session_turns_returns_latest_page(self, monkeypatch):
        """超过 limit 的会话首次打开应看到最新轮次，不是最早轮次。"""
        logger.debug("test_load_session_turns_returns_latest_page 入口")
        try:
            # Arrange
            from types import SimpleNamespace
            from unittest.mock import AsyncMock

            import src.api.routes as routes
            import src.memory.history_store as history_module

            history_rows = [
                {"turn_id": index, "query": f"问题{index}", "sql": f"SQL-{index}",
                 "time": f"T{index}", "final_result": {"success": True, "data": [{"id": index}]}}
                for index in range(1, 26)
            ]
            checkpoint = SimpleNamespace(checkpoint={"channel_values": {
                "messages": [], "conversation_history": [],
            }})
            monkeypatch.setattr(
                routes, "_load_checkpoint_tuple", AsyncMock(return_value=checkpoint),
            )
            history = SimpleNamespace(list_session=AsyncMock(return_value=history_rows))
            monkeypatch.setattr(history_module, "get_history_store", lambda: history)

            # Act
            latest = await routes._load_session_turns("session-many", limit=20)
            older = await routes._load_session_turns("session-many", before=6, limit=20)

            # Assert
            assert [item["turn_id"] for item in latest] == list(range(6, 26))
            assert [item["turn_id"] for item in older] == list(range(1, 6))
            logger.info("test_load_session_turns_returns_latest_page 完成")
        except Exception as exc:
            logger.error(
                "test_load_session_turns_returns_latest_page 异常: %s", exc, exc_info=True,
            )
            raise

    # 方法作用：验证旧会话仅将最新 checkpoint 富数据补入最后一轮。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_get_session_enriches_only_last_legacy_turn(self, monkeypatch):
        """旧轮次不得互相污染，但最后一轮仍应使用可恢复的 latest_state。"""
        logger.debug("test_get_session_enriches_only_last_legacy_turn 入口")
        try:
            # Arrange
            from types import SimpleNamespace
            from unittest.mock import AsyncMock

            import src.api.routes as routes
            import src.memory.session_store as session_module

            turns = [
                {"turn_id": 1, "user_query": "问题一", "sql": "SQL-1",
                 "assistant_summary": "回答一", "timestamp": "",
                 "final_result": {"success": True, "sql": "SQL-1", "data": []}},
                {"turn_id": 2, "user_query": "问题二", "sql": "",
                 "assistant_summary": "回答二", "timestamp": "",
                 "final_result": {"success": True, "sql": "", "data": []}},
            ]
            latest = {
                "success": True, "sql": "SQL-2",
                "sql_statements": [{"datasource": "demo", "dialect": "sqlite", "sql": "SQL-2"}],
                "data": [{"value": 2}], "row_count": 1, "truncated": False,
                "analysis": {"summary": "回答二"},
                "chart": {"type": "bar", "option": {}},
                "error_message": "",
            }
            store = SimpleNamespace(get=AsyncMock(return_value={"session_id": "legacy"}))
            monkeypatch.setattr(session_module, "get_session_store", lambda: store)
            monkeypatch.setattr(routes, "_load_session_turns", AsyncMock(return_value=turns))
            monkeypatch.setattr(routes, "_load_latest_state", AsyncMock(return_value=latest))

            # Act
            result = await routes.get_session("legacy")

            # Assert
            assert result["turns"][0]["final_result"]["sql"] == "SQL-1"
            assert result["turns"][0]["final_result"]["data"] == []
            assert result["turns"][1]["final_result"]["sql"] == "SQL-2"
            assert result["turns"][1]["final_result"]["data"] == [{"value": 2}]
            logger.info("test_get_session_enriches_only_last_legacy_turn 完成")
        except Exception as exc:
            logger.error(
                "test_get_session_enriches_only_last_legacy_turn 异常: %s",
                exc,
                exc_info=True,
            )
            raise
