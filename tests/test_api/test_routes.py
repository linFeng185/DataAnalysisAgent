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
        assert ChatRequest(query="q").datasource == ""

    def test_chat_request_requires_query(self):
        from src.api.schemas import ChatRequest
        with pytest.raises(Exception):
            ChatRequest()

    # 方法作用：验证聊天请求模型限制超长输入和过多数据源。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chat_request_rejects_static_resource_extremes(self):
        """协议层必须在进入应用前拒绝明显异常的查询体。"""
        # Arrange
        from src.api.schemas import ChatRequest

        # Act / Assert
        with pytest.raises(Exception):
            ChatRequest(query="x" * 20_001)
        with pytest.raises(Exception):
            ChatRequest(query="q", datasources=[f"source_{index}" for index in range(21)])

    def test_datasource_create(self):
        from src.api.schemas import DataSourceCreateRequest
        r = DataSourceCreateRequest(name="ch", dialect="clickhouse")
        assert r.name == "ch"

    # 方法作用：验证 API 不接受尚无连接器实现的 Presto/Hive 方言。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_datasource_create_rejects_unimplemented_dialects(self):
        """未实现方言必须显式拒绝，不能回退 PostgreSQL 驱动。"""
        from src.api.schemas import DataSourceCreateRequest

        for dialect in ("presto", "hive"):
            with pytest.raises(Exception):
                DataSourceCreateRequest(name="unsupported", dialect=dialect)

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

    # 方法作用：验证聊天限流在工作流和授权解析之前执行。
    # Args: self - pytest 测试类实例；client - ASGI 客户端；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_chat_rate_limit_rejects_before_workflow(self, client, monkeypatch):
        """被限流请求不得触发 LLM、Schema 或数据源权限读取。"""
        # Arrange
        from unittest.mock import AsyncMock

        import src.api.routes as routes
        import src.security.data_masker as masker

        access = AsyncMock(side_effect=AssertionError("限流后不应解析数据源"))
        monkeypatch.setattr(routes, "_resolve_chat_access", access, raising=False)
        monkeypatch.setattr(masker, "check_rate_limit", lambda user_id=None: False)

        # Act
        response = await client.post("/api/v1/chat", json={
            "query": "统计销售额", "datasource": "sales",
        })

        # Assert
        assert response.status_code == 429
        access.assert_not_awaited()

    # 方法作用：验证非流式聊天把统一授权结果写入工作流状态。
    # Args: self - pytest 测试类实例；client - ASGI 客户端；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_chat_passes_resolved_datasource_access_into_workflow(
        self, client, monkeypatch,
    ):
        """流式和非流式必须消费同一个服务端权限快照。"""
        # Arrange
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import src.api.routes as routes

        access = {
            "sales": {
                "name": "sales",
                "description": "销售订单",
                "allowed_columns": ["order_id", "amount"],
                "row_filter_sql": "org_id = 9",
                "access_level": "read",
            },
        }
        workflow = SimpleNamespace(ainvoke=AsyncMock(return_value={
            "final_response": {"success": True},
        }))
        monkeypatch.setattr(routes, "_app", lambda: workflow)
        monkeypatch.setattr(routes, "_resolve_chat_access", AsyncMock(return_value=access), raising=False)

        # Act
        response = await client.post("/api/v1/chat", json={
            "query": "统计销售额", "datasource": "sales", "stream": False,
        })

        # Assert
        assert response.status_code == 200
        state = workflow.ainvoke.await_args.args[0]
        assert state["datasource_access"] == access
        assert state["allowed_columns"] == ["order_id", "amount"]
        assert state["row_filter_sql"] == "org_id = 9"

    # 方法作用：验证未选择数据源时把授权候选交给工作流而非默认全部执行。
    # Args: self - pytest 测试类实例；client - ASGI 客户端；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_chat_without_datasource_passes_authorized_candidates(
        self, client, monkeypatch,
    ):
        """自动发现模式应保留空主数据源，让分类节点在授权候选中选择。"""
        # Arrange
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import src.api.routes as routes

        access = {
            "sales": {"name": "sales", "allowed_columns": [], "row_filter_sql": ""},
            "warehouse": {"name": "warehouse", "allowed_columns": ["sku"], "row_filter_sql": ""},
        }
        workflow = SimpleNamespace(ainvoke=AsyncMock(return_value={
            "final_response": {"success": True},
        }))
        monkeypatch.setattr(routes, "_app", lambda: workflow)
        monkeypatch.setattr(routes, "_resolve_chat_access", AsyncMock(return_value=access), raising=False)

        # Act
        response = await client.post("/api/v1/chat", json={"query": "库存还有多少"})

        # Assert
        assert response.status_code == 200
        state = workflow.ainvoke.await_args.args[0]
        assert state["datasource"] == ""
        assert state["selected_datasources"] == []
        assert list(state["datasource_access"]) == ["sales", "warehouse"]

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
        assert response.json()["session_id"] == "session-fixed"

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

    # 方法作用：验证多租户数据源列表仅返回服务端授权后的条目。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_list_datasources_filters_by_current_identity(self, monkeypatch):
        """列表接口不能泄露其他用户的数据源名称和描述。"""
        # Arrange
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import src.api.auth as auth
        import src.api.routes as routes
        import src.config as config_module
        import src.security.permission_check as permission_module

        items = [
            {"name": "sales", "description": "销售", "dialect": "postgres"},
            {"name": "payroll", "description": "薪资", "dialect": "postgres"},
        ]
        resolver = AsyncMock(return_value={"sales": {**items[0], "row_filter_sql": "tenant_id=3"}})
        monkeypatch.setattr(routes, "_registry", lambda: SimpleNamespace(list_all=AsyncMock(return_value=items)))
        monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(multi_tenant=True))
        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 3)
        monkeypatch.setattr(auth, "get_current_user_id", lambda: 7)
        monkeypatch.setattr(auth, "get_current_role", lambda: "analyst")
        monkeypatch.setattr(permission_module, "resolve_datasource_access", resolver)

        # Act
        result = await routes.list_datasources(page=1, page_size=20)

        # Assert
        assert [item["name"] for item in result["datasources"]] == ["sales"]
        assert "row_filter_sql" not in result["datasources"][0]
        resolver.assert_awaited_once()

    # 方法作用：验证删除会话会同步清理历史和新旧 Checkpointer 线程。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_delete_session_cleans_all_persistence_layers(self, monkeypatch):
        """删除后不得从 checkpoint 或 query_history 恢复已删除会话。"""
        # Arrange
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, call

        import src.api.auth as auth
        import src.api.routes as routes
        import src.memory.checkpointer as checkpointer_module
        import src.memory.history_store as history_module
        import src.memory.session_store as session_module

        store = SimpleNamespace(
            get=AsyncMock(return_value={"session_id": "session-fixed"}),
            delete=AsyncMock(return_value=True),
        )
        history = SimpleNamespace(delete_session=AsyncMock(return_value=True))
        checkpointer = SimpleNamespace(adelete_thread=AsyncMock())
        monkeypatch.setattr(session_module, "get_session_store", lambda: store)
        monkeypatch.setattr(history_module, "get_history_store", lambda: history)
        monkeypatch.setattr(checkpointer_module, "get_checkpointer", AsyncMock(return_value=checkpointer))

        # Act
        result = await routes.delete_session("session-fixed")

        # Assert
        scoped_id = auth.scope_thread_id("session-fixed")
        assert result["status"] == "ok"
        history.delete_session.assert_awaited_once_with("session-fixed")
        assert checkpointer.adelete_thread.await_args_list == [call(scoped_id), call("session-fixed")]
        store.delete.assert_awaited_once_with("session-fixed")

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

    # 方法作用：验证仅有消息 checkpoint 时仍合并查询历史中的逐轮富数据。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_load_session_turns_merges_history_for_message_checkpoint(self, monkeypatch):
        """消息 checkpoint 不应阻断 SQL、数据、图表从 query_history 恢复。"""
        logger.debug("test_load_session_turns_merges_history_for_message_checkpoint 入口")
        try:
            # Arrange：模拟旧 checkpoint 只保存 messages，新历史保存每轮富结果。
            from types import SimpleNamespace
            from unittest.mock import AsyncMock

            from langchain_core.messages import AIMessage, HumanMessage

            import src.api.routes as routes
            import src.memory.history_store as history_module

            checkpoint = SimpleNamespace(checkpoint={"channel_values": {
                "messages": [
                    HumanMessage(content="问题一"), AIMessage(content="回答一"),
                    HumanMessage(content="问题二"), AIMessage(content="回答二"),
                ],
                "conversation_history": [],
            }})
            rich_rows = [
                {"turn_id": 1, "query": "问题一", "sql": "SQL-1", "time": "T1",
                 "final_result": {"success": True, "sql": "SQL-1",
                                  "sql_statements": [{"datasource": "a", "dialect": "mysql", "sql": "SQL-1"}],
                                  "data": [{"value": 1}], "analysis": {"summary": "结论一"},
                                  "chart": {"type": "table", "option": {}}}},
                {"turn_id": 2, "query": "问题二", "sql": "SQL-2", "time": "T2",
                 "final_result": {"success": True, "sql": "SQL-2",
                                  "sql_statements": [{"datasource": "b", "dialect": "postgres", "sql": "SQL-2"}],
                                  "data": [{"value": 2}], "analysis": {"summary": "结论二"},
                                  "chart": {"type": "bar", "option": {}}}},
            ]
            monkeypatch.setattr(
                routes, "_load_checkpoint_tuple", AsyncMock(return_value=checkpoint),
            )
            history = SimpleNamespace(list_session=AsyncMock(return_value=rich_rows))
            monkeypatch.setattr(history_module, "get_history_store", lambda: history)

            # Act
            turns = await routes._load_session_turns("session-message-only", limit=20)

            # Assert：两轮都必须保留各自的结构化结果。
            assert turns[0]["final_result"]["data"] == [{"value": 1}]
            assert turns[1]["final_result"]["sql_statements"][0]["datasource"] == "b"
            history.list_session.assert_awaited_once_with(
                "session-message-only", before=None, limit=1000,
            )
            logger.info("test_load_session_turns_merges_history_for_message_checkpoint 完成")
        except Exception as exc:
            logger.error(
                "test_load_session_turns_merges_history_for_message_checkpoint 异常: %s",
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

    # 方法作用：验证贫化 latest_state 不会覆盖已恢复的最后一轮富结果。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_get_session_preserves_rich_last_turn_when_latest_state_is_partial(self, monkeypatch):
        """checkpoint 回退结果为空时，最后一轮仍应保留 query_history 富数据。"""
        logger.debug("test_get_session_preserves_rich_last_turn_when_latest_state_is_partial 入口")
        try:
            # Arrange：逐轮历史已有完整结果，但兼容字段 latest_state 只有摘要。
            from types import SimpleNamespace
            from unittest.mock import AsyncMock

            import src.api.routes as routes
            import src.memory.session_store as session_module

            rich_last = {
                "success": True, "sql": "SQL-2",
                "sql_statements": [{"datasource": "demo", "dialect": "sqlite", "sql": "SQL-2"}],
                "data": [{"value": 2}], "row_count": 1,
                "analysis": {"summary": "完整回答二"},
                "chart": {"type": "bar", "option": {}},
            }
            turns = [
                {"turn_id": 1, "user_query": "问题一", "sql": "SQL-1",
                 "assistant_summary": "回答一", "timestamp": "", "final_result": {
                     "success": True, "sql": "SQL-1", "data": [{"value": 1}]}},
                {"turn_id": 2, "user_query": "问题二", "sql": "SQL-2",
                 "assistant_summary": "完整回答二", "timestamp": "", "final_result": rich_last},
            ]
            partial_latest = {
                "success": True, "sql": "", "sql_statements": [], "data": [],
                "row_count": 0, "analysis": {}, "chart": {},
            }
            store = SimpleNamespace(get=AsyncMock(return_value={"session_id": "session-rich"}))
            monkeypatch.setattr(session_module, "get_session_store", lambda: store)
            monkeypatch.setattr(routes, "_load_session_turns", AsyncMock(return_value=turns))
            monkeypatch.setattr(routes, "_load_latest_state", AsyncMock(return_value=partial_latest))

            # Act
            result = await routes.get_session("session-rich")

            # Assert：兼容状态只能补缺，不能抹掉最后一轮富字段。
            assert result["turns"][1]["final_result"] == rich_last
            assert result["latest_state"] == rich_last
            logger.info("test_get_session_preserves_rich_last_turn_when_latest_state_is_partial 完成")
        except Exception as exc:
            logger.error(
                "test_get_session_preserves_rich_last_turn_when_latest_state_is_partial 异常: %s",
                exc,
                exc_info=True,
            )
            raise
