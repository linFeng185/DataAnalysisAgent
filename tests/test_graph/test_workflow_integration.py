"""Fake LLM + SQLite 的完整 LangGraph 集成测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.app_context import AppContext, use_app_context_async
from src.config import Settings


@pytest.mark.integration
class TestWorkflowIntegration:
    """覆盖功能 4.8.11、4.9.7、16.3.1-16.3.3 的真实工作流边界。"""

    # 方法作用：配置工作流使用测试数据库、确定性 Schema 和 Fake LLM。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具；sqlite_memory_db - SQLite fixture；model - Fake LLM。
    # Returns: 测试 AppContext 和 SQLite Connector。
    async def _configure(
        self,
        monkeypatch,
        sqlite_memory_db,
        model,
    ) -> tuple[AppContext, object]:
        import src.datasource.registry as registry_module
        import src.graph.nodes.analyze_result as analyze_module
        import src.graph.nodes.execute_sql as execute_module
        import src.graph.nodes.generate_sql as generate_module
        import src.graph.nodes.retrieve_schema as retrieve_module
        import src.graph.skill_activation as activation_module
        import src.knowledge.schema_manager as schema_module

        datasource = sqlite_memory_db.datasource
        registry = SimpleNamespace(
            resolve=AsyncMock(return_value=datasource),
            resolve_or_none=AsyncMock(return_value=datasource),
        )
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
        manager = SimpleNamespace(
            get_or_fetch_schema=AsyncMock(return_value=datasource.schema),
        )
        monkeypatch.setattr(schema_module, "get_schema_manager", lambda: manager)
        monkeypatch.setattr(
            retrieve_module,
            "_load_knowledge_context",
            AsyncMock(return_value=""),
        )
        monkeypatch.setattr(
            retrieve_module,
            "_load_enum_dictionary",
            AsyncMock(return_value={}),
        )
        monkeypatch.setattr(
            activation_module,
            "activate_skills",
            lambda state, tables=None: {"activated_skills": []},
        )
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(generate_module, "get_llm", lambda temperature=0: model)
        monkeypatch.setattr(analyze_module, "is_llm_available", lambda: False)
        monkeypatch.setattr(analyze_module, "_is_task_llm_available", lambda task: False)
        monkeypatch.setattr(execute_module, "_record_query_audit", AsyncMock())
        settings = Settings(
            env="test",
            database_url="",
            run_migrations_on_startup=False,
            multi_tenant=False,
            max_retry_count=3,
        )
        return AppContext(settings), datasource.connector

    # 方法作用：验证完整成功链从模型生成 SQL 到 SQLite 结果和最终响应。
    # Args: self - pytest 测试类实例；monkeypatch/sqlite_memory_db/fake_llm_factory - 测试 fixture。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_complete_success_path(
        self,
        monkeypatch,
        sqlite_memory_db,
        fake_llm_factory,
    ) -> None:
        """真实校验、EXPLAIN 和执行后应返回 3 条订单的聚合结果。"""
        # Arrange
        from src.graph.workflow import build_workflow

        model = fake_llm_factory([
            '{"sql":"SELECT COUNT(*) AS total_orders FROM orders",'
            '"explanation":"统计订单总数"}',
        ])
        context, _ = await self._configure(
            monkeypatch,
            sqlite_memory_db,
            model,
        )

        # Act
        async with use_app_context_async(context):
            app = await build_workflow()
            result = await app.ainvoke({
                "user_query": "统计订单总数",
                "datasource": sqlite_memory_db.datasource.name,
                "selected_datasources": [sqlite_memory_db.datasource.name],
                "request_rate_limit_checked": True,
            }, {"configurable": {"thread_id": "integration-success"}})
        await context.close()

        # Assert
        assert result["final_response"]["success"] is True
        assert result["final_response"]["data"] == [{"total_orders": 3}]
        assert "COUNT" in result["final_response"]["sql"].upper()

    # 方法作用：验证证据不足会在主图中真实补查并重新分析。
    # Args: self - pytest 测试类实例；monkeypatch/sqlite_memory_db/fake_llm_factory - 测试 fixture。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_supplemental_query_executes_and_reanalyzes(
        self,
        monkeypatch,
        sqlite_memory_db,
        fake_llm_factory,
    ) -> None:
        """首轮证据可补齐时应执行第二条真实 SQL，并把结果用于第二轮分析。"""
        # Arrange
        import src.graph.nodes.analyze_result as analyze_module
        from src.graph.workflow import build_workflow

        model = fake_llm_factory([
            '{"sql":"SELECT COUNT(*) AS total_orders FROM orders",'
            '"explanation":"统计订单总数"}',
            '{"sql":"SELECT SUM(amount) AS total_amount FROM orders",'
            '"explanation":"补查订单金额"}',
        ])
        context, _ = await self._configure(
            monkeypatch,
            sqlite_memory_db,
            model,
        )
        analyses = [
            {
                "summary": "订单数量不足以评估规模",
                "insights": ["当前仅有订单数"],
                "evidence_status": "supplementable",
                "evidence_gaps": ["缺少订单金额"],
                "supplemental_questions": ["统计订单金额"],
                "recommended_chart_type": "table",
                "follow_up_questions": [],
            },
            {
                "summary": "订单数为三，总金额为三百",
                "insights": ["补查已提供订单金额"],
                "evidence_status": "sufficient",
                "evidence_gaps": [],
                "supplemental_questions": [],
                "recommended_chart_type": "table",
                "follow_up_questions": [],
            },
        ]
        analyze = AsyncMock(side_effect=analyses)
        monkeypatch.setattr(analyze_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(analyze_module, "_llm_analyze", analyze)

        # Act
        async with use_app_context_async(context):
            app = await build_workflow()
            result = await app.ainvoke({
                "user_query": "统计订单总数",
                "datasource": sqlite_memory_db.datasource.name,
                "selected_datasources": [sqlite_memory_db.datasource.name],
                "request_rate_limit_checked": True,
            }, {"configurable": {"thread_id": "integration-supplemental"}})
        await context.close()

        # Assert
        response = result["final_response"]
        assert analyze.await_count == 2
        second_call = analyze.await_args_list[1]
        assert "total_amount" in second_call.kwargs["supplemental_evidence"]
        assert "300" in second_call.kwargs["supplemental_evidence"]
        assert result["supplemental_query_round"] == 1
        assert len(response["sql_statements"]) == 2
        assert response["sql_statements"][0]["purpose"] == "primary"
        assert response["sql_statements"][1]["purpose"] == "supplemental"
        assert response["analysis"]["evidence_status"] == "sufficient"

    # 方法作用：验证触点归因在真实工作流中补查路径明细并执行确定性算法。
    # Args: self - pytest 测试类实例；monkeypatch/sqlite_memory_db/fake_llm_factory - 测试基础设施。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_touchpoint_attribution_supplements_paths_and_calculates(
        self,
        monkeypatch,
        sqlite_memory_db,
        fake_llm_factory,
    ) -> None:
        """聚合主查询不得直接归因，补查完整路径后应返回线性归因和图表。"""
        # Arrange
        import sqlalchemy as sa

        import src.graph.attribution as attribution_module
        from src.datasource.schema_snapshot import ColumnInfo, TableSchema
        from src.graph.workflow import build_workflow

        async with sqlite_memory_db.engine.begin() as connection:
            await connection.execute(sa.text(
                "CREATE TABLE touch_events ("
                "path_id TEXT NOT NULL, touchpoint TEXT NOT NULL, "
                "touch_order INTEGER NOT NULL, touch_time TEXT NOT NULL, "
                "is_conversion INTEGER NOT NULL, conversion_value REAL)"
            ))
            await connection.execute(sa.text(
                "INSERT INTO touch_events VALUES "
                "('p1','搜索',1,'2026-01-01T00:00:00Z',0,NULL),"
                "('p1','短信',2,'2026-01-02T00:00:00Z',1,100),"
                "('p2','内容',1,'2026-01-03T00:00:00Z',0,NULL),"
                "('p2','直播',2,'2026-01-04T00:00:00Z',1,200)"
            ))
        sqlite_memory_db.datasource.schema.tables.append(TableSchema(
            name="touch_events",
            description="渠道转化触点路径",
            columns=[
                ColumnInfo("path_id", "TEXT", "路径标识"),
                ColumnInfo("touchpoint", "TEXT", "触点渠道"),
                ColumnInfo("touch_order", "INTEGER", "触点顺序"),
                ColumnInfo("touch_time", "TEXT", "触点时间"),
                ColumnInfo("is_conversion", "INTEGER", "转化标记"),
                ColumnInfo("conversion_value", "REAL", "转化价值"),
            ],
            row_count_estimate=4,
        ))
        model = fake_llm_factory([
            '{"sql":"SELECT touchpoint AS channel, COUNT(*) AS conversions '
            'FROM touch_events GROUP BY touchpoint","explanation":"先查看渠道汇总"}',
            '{"sql":"SELECT path_id, touchpoint, touch_order, touch_time, '
            'is_conversion, conversion_value FROM touch_events ORDER BY path_id, touch_order",'
            '"explanation":"补查完整路径触点"}',
        ])
        context, _ = await self._configure(monkeypatch, sqlite_memory_db, model)
        monkeypatch.setattr(attribution_module, "is_task_llm_available", lambda task: False)

        # Act
        async with use_app_context_async(context):
            app = await build_workflow()
            result = await app.ainvoke({
                "user_query": "使用线性归因分析全部数据的渠道触点",
                "datasource": sqlite_memory_db.datasource.name,
                "selected_datasources": [sqlite_memory_db.datasource.name],
                "request_rate_limit_checked": True,
            }, {"configurable": {"thread_id": "integration-touchpoint-attribution"}})
        await context.close()

        # Assert
        response = result["final_response"]
        attribution = response["analysis"]["attribution"]
        assert result["supplemental_query_round"] == 1
        assert len(response["sql_statements"]) == 2
        assert response["sql_statements"][1]["purpose"] == "supplemental"
        assert attribution["status"] == "applied"
        assert attribution["selected_model"] == "linear"
        assert attribution["dataset_source"] == "supplemental"
        assert sum(item["attribution_pct"] for item in attribution["results"]) == 100
        assert response["chart"]["type"] == "bar"
        assert response["chart"]["status"] == "success"

    # 方法作用：验证最终图表选择会复核模型的表格建议。
    # Args: self - pytest 测试类实例；monkeypatch/sqlite_memory_db/fake_llm_factory - 测试 fixture。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_table_recommendation_is_overridden_for_category_values(
        self,
        monkeypatch,
        sqlite_memory_db,
        fake_llm_factory,
    ) -> None:
        """三行分类加数值数据即使模型推荐表格，也应生成可用柱状图。"""
        # Arrange
        import sqlalchemy as sa

        import src.graph.nodes.analyze_result as analyze_module
        from src.datasource.schema_snapshot import ColumnInfo, TableSchema
        from src.graph.workflow import build_workflow

        # 自建唯一客户名的受控数据表：fixture 的 orders 表中「张三」有两笔
        # （120.5 与 99.5），会触发 spec/52 的重复坐标校验（无明确聚合口径时
        # 拒绝图表），与用例意图（模型推 table 被确定性复核为 bar）无关，
        # 因此沿用本文件先例的自建表模式，构造三行唯一客户数据。
        async with sqlite_memory_db.engine.begin() as connection:
            await connection.execute(sa.text(
                "CREATE TABLE customer_orders ("
                "id INTEGER PRIMARY KEY, customer TEXT NOT NULL, amount REAL NOT NULL)"
            ))
            await connection.execute(sa.text(
                "INSERT INTO customer_orders (id, customer, amount) VALUES "
                "(1, '张三', 120.5), (2, '李四', 80.0), (3, '王五', 99.5)"
            ))
        sqlite_memory_db.datasource.schema.tables.append(TableSchema(
            name="customer_orders",
            description="客户订单金额测试表",
            columns=[
                ColumnInfo("id", "INTEGER", "订单编号"),
                ColumnInfo("customer", "TEXT", "客户名称"),
                ColumnInfo("amount", "REAL", "订单金额"),
            ],
            row_count_estimate=3,
        ))
        model = fake_llm_factory([
            '{"sql":"SELECT customer, amount FROM customer_orders ORDER BY id",'
            '"explanation":"查询客户订单金额"}',
        ])
        context, _ = await self._configure(
            monkeypatch,
            sqlite_memory_db,
            model,
        )
        analyze = AsyncMock(return_value={
            "summary": "返回三条客户订单金额",
            "insights": [],
            "evidence_status": "sufficient",
            "evidence_gaps": [],
            "supplemental_questions": [],
            "recommended_chart_type": "table",
            "follow_up_questions": [],
        })
        monkeypatch.setattr(analyze_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(analyze_module, "_llm_analyze", analyze)

        # Act
        async with use_app_context_async(context):
            app = await build_workflow()
            result = await app.ainvoke({
                "user_query": "查询各客户订单金额",
                "datasource": sqlite_memory_db.datasource.name,
                "selected_datasources": [sqlite_memory_db.datasource.name],
                "request_rate_limit_checked": True,
            }, {"configurable": {"thread_id": "integration-chart-eligibility"}})
        await context.close()

        # Assert
        chart = result["final_response"]["chart"]
        assert analyze.await_count == 1
        assert chart["type"] == "bar"
        assert chart["status"] == "success"
        assert chart["option"]["series"][0]["data"] == [120.5, 80.0, 99.5]

    # 方法作用：验证首次 SQL 的真实 EXPLAIN 失败会回到生成节点并最终成功。
    # Args: self - pytest 测试类实例；monkeypatch/sqlite_memory_db/fake_llm_factory - 测试 fixture。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_semantic_error_retries_then_succeeds(
        self,
        monkeypatch,
        sqlite_memory_db,
        fake_llm_factory,
    ) -> None:
        """重试必须经过 LangGraph 条件边，而不是测试直接调用两次 Node。"""
        # Arrange
        from src.graph.workflow import build_workflow

        model = fake_llm_factory([
            '{"sql":"SELECT missing_column FROM orders","explanation":"首轮错误"}',
            '{"sql":"SELECT SUM(amount) AS total_amount FROM orders","explanation":"修正"}',
        ])
        context, _ = await self._configure(
            monkeypatch,
            sqlite_memory_db,
            model,
        )

        # Act
        async with use_app_context_async(context):
            app = await build_workflow()
            result = await app.ainvoke({
                "user_query": "统计订单金额",
                "datasource": sqlite_memory_db.datasource.name,
                "selected_datasources": [sqlite_memory_db.datasource.name],
                "request_rate_limit_checked": True,
            }, {"configurable": {"thread_id": "integration-retry"}})
        await context.close()

        # Assert
        assert result["retry_count"] == 2
        assert result["final_response"]["success"] is True
        assert result["final_response"]["data"] == [{"total_amount": 300.0}]

    # 方法作用：验证危险 SQL 在 Layer 3 后直接构建失败响应且不访问 SQLite。
    # Args: self - pytest 测试类实例；monkeypatch/sqlite_memory_db/fake_llm_factory - 测试 fixture。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_security_block_short_circuits_execution(
        self,
        monkeypatch,
        sqlite_memory_db,
        fake_llm_factory,
    ) -> None:
        """安全阻断是终局分支，禁止重试和数据库执行。"""
        # Arrange
        from src.graph.workflow import build_workflow

        model = fake_llm_factory([
            '{"sql":"DELETE FROM orders","explanation":"危险操作"}',
        ])
        context, connector = await self._configure(
            monkeypatch,
            sqlite_memory_db,
            model,
        )
        execute_spy = AsyncMock(wraps=connector.execute_bounded)
        monkeypatch.setattr(connector, "execute_bounded", execute_spy)

        # Act
        async with use_app_context_async(context):
            app = await build_workflow()
            result = await app.ainvoke({
                "user_query": "删除全部订单",
                "datasource": sqlite_memory_db.datasource.name,
                "selected_datasources": [sqlite_memory_db.datasource.name],
                "request_rate_limit_checked": True,
            }, {"configurable": {"thread_id": "integration-security"}})
        await context.close()

        # Assert
        assert result["final_response"]["success"] is False
        assert result["validation_errors"][0]["type"] == "security_block"
        execute_spy.assert_not_awaited()
