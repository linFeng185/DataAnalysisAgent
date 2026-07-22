"""LangGraph 编排引擎测试 — 状态、路由、Node、e2e。"""

from __future__ import annotations

import asyncio
import logging

import pytest


logger = logging.getLogger(__name__)


class TestAnalysisState:
    """4.1.1"""

    def test_minimal(self):
        from src.graph.state import AnalysisState
        s: AnalysisState = {"user_query": "查订单", "datasource": "ch"}
        assert s["user_query"] == "查订单"

    def test_defaults(self):
        from src.graph.state import AnalysisState
        s: AnalysisState = {}
        assert s.get("generated_sql") is None

    # 验证时间范围提示标志属于 LangGraph 状态契约。
    # Args: self - pytest 测试类实例
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_needs_time_range_field_is_declared(self):
        """显式时间范围标志必须能由 LangGraph 在节点间传递。"""
        logger.debug("test_needs_time_range_field_is_declared 入口")
        try:
            # Arrange / Act：读取 AnalysisState 声明字段。
            from src.graph.state import AnalysisState
            fields = AnalysisState.__annotations__

            # Assert：生成 SQL 节点写出的标志不能被状态图丢弃。
            assert "needs_time_range" in fields
            logger.info("test_needs_time_range_field_is_declared 完成", extra={"field_count": len(fields)})
        except Exception as exc:
            logger.error("test_needs_time_range_field_is_declared 异常: %s", exc, exc_info=True)
            raise


class TestConditionalRouting:
    """4.1.3-6"""

    def test_layer3_security_block(self):
        from src.graph.workflow import after_layer3
        assert after_layer3({"validation_errors": [{"type": "security_block"}]}) == "build_response"

    def test_layer3_syntax_retry(self):
        from src.graph.workflow import after_layer3
        assert after_layer3({"validation_errors": [{"type": "syntax_error"}]}) == "generate_sql"

    def test_layer3_pass(self):
        from src.graph.workflow import after_layer3
        assert after_layer3({"validation_errors": []}) == "layer4_explain"

    def test_layer4_retry(self):
        from src.graph.workflow import after_layer4
        assert after_layer4({"explain_errors": [{}], "retry_count": 0}) == "generate_sql"

    def test_layer4_exhausted(self):
        from src.graph.workflow import after_layer4
        assert after_layer4({"explain_errors": [{}], "retry_count": 3}) == "build_response"

    def test_layer4_pass(self):
        from src.graph.workflow import after_layer4
        assert after_layer4({"explain_errors": []}) == "execute_sql"

    def test_retry_with_error(self):
        from src.graph.workflow import should_retry
        assert should_retry({"execution_error": "t", "retry_count": 0}) == "generate_sql"

    def test_retry_exhausted(self):
        from src.graph.workflow import should_retry
        assert should_retry({"execution_error": "t", "retry_count": 3}) == "build_response"

    def test_retry_no_error(self):
        from src.graph.workflow import should_retry
        assert should_retry({"execution_error": "", "retry_count": 0}) == "analyze_result"

    def test_intent_file(self):
        from src.graph.workflow import route_by_intent
        assert route_by_intent({"intent": "file_analysis"}) == "mcp_agent"

    def test_intent_normal(self):
        from src.graph.workflow import route_by_intent
        assert route_by_intent({"intent": "query"}) == "retrieve_schema"


class TestIntentClassification:
    """4.2"""

    @pytest.mark.parametrize("q,exp", [
        ("为什么GMV下降", "attribution"),
        ("近30天趋势", "trend"),
        ("各品类排名", "aggregation"),
        ("表结构", "metadata"),
        ("上传CSV", "file_analysis"),
        ("查订单数", "query"),
        ("你好", "chat"),
    ])
    def test_classify(self, q, exp):
        from src.graph.nodes.classify_intent import classify_intent_node
        r = asyncio.run(classify_intent_node({"user_query": q}))
        assert r["intent"] == exp


class TestNodes:
    """4.3-4.10"""

    def test_retrieve_schema_empty(self):
        from src.graph.nodes.retrieve_schema import retrieve_schema_node
        r = asyncio.run(retrieve_schema_node({}))
        assert r["relevant_tables"] == []

    def test_generate_sql(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "")  # 使用模板回退，避免真实 API 调用
        import src.graph.nodes.generate_sql as generate_module
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: False)
        r = asyncio.run(generate_module.generate_sql_node({
            "user_query": "统计t表记录数",
            "relevant_tables": [{"name": "t", "columns": []}],
        }, {}))
        assert "t" in r["generated_sql"]

    def test_layer3_pass(self):
        from src.graph.nodes.layer3_validate import layer3_validate_node
        r = asyncio.run(layer3_validate_node({"generated_sql": "SELECT 1"}))
        assert r["sql_valid"] is True

    def test_layer3_block_drop(self):
        from src.graph.nodes.layer3_validate import layer3_validate_node
        r = asyncio.run(layer3_validate_node({"generated_sql": "DROP TABLE users"}))
        assert r["sql_valid"] is False
        assert r["validation_errors"][0]["type"] == "security_block"

    def test_layer3_block_delete(self):
        from src.graph.nodes.layer3_validate import layer3_validate_node
        r = asyncio.run(layer3_validate_node({"generated_sql": "DELETE FROM t"}))
        assert r["sql_valid"] is False

    def test_execute_sql(self):
        """无数据源时返回空结果 + 错误提示。"""
        from src.graph.nodes.execute_sql import execute_sql_node
        r = asyncio.run(execute_sql_node({
            "datasource": "nonexistent",
            "generated_sql": "SELECT 1",
            "dialect": "sqlite",
        }))
        assert r["execution_error"] != ""  # 应该有错误提示
        assert "query_result_sample" in r

    def test_analyze_result(self):
        from src.graph.nodes.analyze_result import analyze_result_node
        r = asyncio.run(analyze_result_node({}))
        assert "summary" in r["analysis_result"]

    def test_generate_chart(self):
        from src.graph.nodes.generate_chart import generate_chart_node
        assert asyncio.run(generate_chart_node({}))["chart_config"]["type"] == "table"

    def test_build_response_ok(self):
        from src.graph.nodes.build_response import build_response_node
        r = asyncio.run(build_response_node({"user_query": "q"}))
        assert r["final_response"]["success"] is True

    def test_build_response_error(self):
        from src.graph.nodes.build_response import build_response_node
        r = asyncio.run(build_response_node({"validation_errors": [{}]}))
        assert r["final_response"]["success"] is False

    # 验证多数据源合并结果不会因缺少顶层 SQL 被误判为时间提示。
    # Args: self - pytest 测试类实例
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_build_response_preserves_multi_source_result_without_top_level_sql(self):
        """多源路径应返回已合并数据和分析，不要求额外选择时间范围。"""
        logger.debug("test_build_response_preserves_multi_source_result_without_top_level_sql 入口")
        try:
            # Arrange：模拟 merge_results 已完成、但没有单条顶层 SQL 的状态。
            from src.graph.nodes.build_response import build_response_node
            merged_data = [
                {"total_customers": 100000, "_datasource": "mysql_test"},
                {"total_customers": 3, "_datasource": "demo"},
            ]
            state = {
                "user_query": "统计客户总数",
                "multi_source_results": [
                    {
                        "datasource": "mysql_test", "success": True,
                        "dialect": "mysql", "sql": "SELECT total_customers FROM customers",
                    },
                    {
                        "datasource": "demo", "success": True,
                        "dialect": "sqlite", "sql": "SELECT COUNT(*) AS total_customers FROM customers",
                    },
                ],
                "query_result_sample": merged_data,
                "analysis_result": {"summary": "两个数据源共返回客户统计结果"},
                "chart_config": {"type": "table", "option": {}},
                "needs_time_range": False,
            }

            # Act：组装最终响应。
            result = asyncio.run(build_response_node(state))["final_response"]

            # Assert：合并数据和分析必须原样进入最终响应。
            assert result.get("needs_time_range") is not True
            assert result["data"] == merged_data
            assert result["analysis"]["summary"] == "两个数据源共返回客户统计结果"
            assert len(result["sql_statements"]) == 2
            assert result["sql_statements"][0] == {
                "datasource": "mysql_test",
                "dialect": "mysql",
                "sql": "SELECT total_customers FROM customers",
            }
            assert "-- datasource: mysql_test" in result["sql"]
            assert "-- datasource: demo" in result["sql"]
            logger.info(
                "test_build_response_preserves_multi_source_result_without_top_level_sql 完成",
                extra={"data_rows": len(result["data"])},
            )
        except Exception as exc:
            logger.error(
                "test_build_response_preserves_multi_source_result_without_top_level_sql 异常: %s",
                exc,
                exc_info=True,
            )
            raise


class TestMultiSource:
    """覆盖功能 4.1.9 多数据源并行调度。"""

    # 验证多源调度不会静默截断用户选择的数据源。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_dispatch_analyzes_every_selected_datasource(self, monkeypatch):
        """选择七个数据源时，应为七个来源全部创建分析任务。"""
        logger.debug("test_dispatch_analyzes_every_selected_datasource 入口")
        try:
            # Arrange：隔离真实数据库和 LLM，只记录 worker 调用次数。
            from unittest.mock import AsyncMock
            import src.graph.nodes.multi_source as multi_source_module

            selected = [f"source_{index}" for index in range(7)]
            worker = AsyncMock(return_value={"datasource": "source", "success": True})
            monkeypatch.setattr(multi_source_module, "_analyze_one", worker)

            # Act：执行多数据源调度。
            result = await multi_source_module.multi_source_dispatch_node({
                "user_query": "汇总客户总数",
                "selected_datasources": selected,
            })

            # Assert：所有选中来源均被调度并写回结果。
            assert worker.await_count == len(selected)
            assert len(result["multi_source_results"]) == len(selected)
            logger.info(
                "test_dispatch_analyzes_every_selected_datasource 完成",
                extra={"selected_count": len(selected), "worker_calls": worker.await_count},
            )
        except Exception as exc:
            logger.error(
                "test_dispatch_analyzes_every_selected_datasource 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 验证单数据源不会误入多源 worker 调度。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_dispatch_skips_single_datasource(self, monkeypatch):
        """仅选择一个数据源时，应返回空的多源结果且不创建 worker。"""
        logger.debug("test_dispatch_skips_single_datasource 入口")
        try:
            # Arrange：用 Mock 隔离单源分析函数。
            from unittest.mock import AsyncMock
            import src.graph.nodes.multi_source as multi_source_module

            worker = AsyncMock()
            monkeypatch.setattr(multi_source_module, "_analyze_one", worker)

            # Act：执行单源边界输入。
            result = await multi_source_module.multi_source_dispatch_node({
                "user_query": "客户总数",
                "selected_datasources": ["mysql_test"],
            })

            # Assert：多源节点应安全跳过。
            assert result == {"multi_source_results": []}
            worker.assert_not_awaited()
            logger.info("test_dispatch_skips_single_datasource 完成")
        except Exception as exc:
            logger.error("test_dispatch_skips_single_datasource 异常: %s", exc, exc_info=True)
            raise

    # 验证单个 worker 异常不会中断其他来源的结果收集。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_dispatch_collects_worker_exceptions(self, monkeypatch):
        """worker 抛出异常时，应写回对应来源的失败结果。"""
        logger.debug("test_dispatch_collects_worker_exceptions 入口")
        try:
            # Arrange：让所有 worker 抛出可识别异常。
            from unittest.mock import AsyncMock
            import src.graph.nodes.multi_source as multi_source_module

            worker = AsyncMock(side_effect=RuntimeError("连接失败"))
            monkeypatch.setattr(multi_source_module, "_analyze_one", worker)

            # Act：执行两个来源的并行调度。
            result = await multi_source_module.multi_source_dispatch_node({
                "user_query": "客户总数",
                "selected_datasources": ["mysql_test", "missing"],
            })

            # Assert：两个异常均转换为带来源的失败结果。
            failures = result["multi_source_results"]
            assert [item["datasource"] for item in failures] == ["mysql_test", "missing"]
            assert all(item["success"] is False for item in failures)
            assert all(item["error"] == "连接失败" for item in failures)
            logger.info("test_dispatch_collects_worker_exceptions 完成", extra={"failure_count": len(failures)})
        except Exception as exc:
            logger.error("test_dispatch_collects_worker_exceptions 异常: %s", exc, exc_info=True)
            raise

    # 验证不可达数据源在 Schema 与 LLM 阶段之前快速返回。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_worker_short_circuits_unreachable_datasource(self, monkeypatch):
        """Registry 解析失败后，不应继续执行 Schema 检索和 SQL 生成。"""
        logger.debug("test_worker_short_circuits_unreachable_datasource 入口")
        try:
            # Arrange：模拟 Registry 连接失败，并让 Schema 调用在误执行时立即报错。
            from unittest.mock import AsyncMock, MagicMock
            import src.datasource.registry as registry_module
            import src.graph.nodes.multi_source as multi_source_module
            import src.graph.nodes.retrieve_schema as retrieve_schema_module

            registry = MagicMock()
            registry.resolve_or_none = AsyncMock(return_value=None)
            monkeypatch.setattr(registry_module, "get_registry", MagicMock(return_value=registry))
            retrieve_schema = AsyncMock(side_effect=AssertionError("不可达数据源不应检索 Schema"))
            monkeypatch.setattr(retrieve_schema_module, "retrieve_schema_node", retrieve_schema)

            # Act：执行不可达来源 worker。
            result = await multi_source_module._analyze_one("missing", {"user_query": "客户总数"})

            # Assert：连接失败立即成为来源级错误，后续链路未执行。
            assert result == {"datasource": "missing", "success": False, "error": "数据源连接失败或不存在"}
            registry.resolve_or_none.assert_awaited_once_with("missing")
            retrieve_schema.assert_not_awaited()
            logger.info("test_worker_short_circuits_unreachable_datasource 完成")
        except Exception as exc:
            logger.error(
                "test_worker_short_circuits_unreachable_datasource 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 验证多源 worker 会把全局问题明确拆成当前来源的 SQL 子任务。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_worker_scopes_global_query_to_current_datasource(self, monkeypatch):
        """单库 SQL 生成器不应因看不到其他来源 Schema 而拒绝生成 SQL。"""
        logger.debug("test_worker_scopes_global_query_to_current_datasource 入口")
        try:
            # Arrange：隔离真实 Registry、LLM、校验与数据库执行，并捕获 SQL 生成状态。
            from types import SimpleNamespace
            from unittest.mock import AsyncMock, MagicMock

            import src.datasource.registry as registry_module
            import src.graph.nodes.execute_sql as execute_module
            import src.graph.nodes.generate_sql as generate_module
            import src.graph.nodes.layer3_validate as validate_module
            import src.graph.nodes.layer4_explain as explain_module
            import src.graph.nodes.multi_source as multi_source_module
            import src.graph.nodes.retrieve_schema as retrieve_module

            registry = MagicMock()
            registry.resolve_or_none = AsyncMock(
                return_value=SimpleNamespace(schema=None, dialect="mysql")
            )
            monkeypatch.setattr(registry_module, "get_registry", MagicMock(return_value=registry))
            monkeypatch.setattr(
                retrieve_module,
                "retrieve_schema_node",
                AsyncMock(return_value={
                    "relevant_tables": [{"name": "customers", "columns": []}],
                    "dialect": "mysql",
                }),
            )
            generate = AsyncMock(return_value={
                "generated_sql": "SELECT COUNT(*) AS total_customers FROM customers"
            })
            monkeypatch.setattr(generate_module, "generate_sql_node", generate)
            monkeypatch.setattr(
                validate_module,
                "layer3_validate_node",
                AsyncMock(return_value={"validation_errors": []}),
            )
            monkeypatch.setattr(
                explain_module,
                "layer4_explain_node",
                AsyncMock(return_value={"explain_errors": [], "sql_valid": True}),
            )
            monkeypatch.setattr(
                execute_module,
                "execute_sql_node",
                AsyncMock(return_value={"query_result_sample": [{"total_customers": 10}]}),
            )
            global_query = "MySQL和PostgreSQL两个库的客户总数有什么不同？"

            # Act：执行 MySQL 单源 worker。
            result = await multi_source_module._analyze_one(
                "mysql_test",
                {
                    "user_query": global_query,
                    "selected_datasources": ["mysql_test", "postgres_main"],
                },
            )

            # Assert：传给 SQL 生成器的是带单源职责的子任务，且保留全局问题供指标提取。
            worker_state = generate.await_args.args[0]
            worker_query = worker_state["user_query"]
            assert result["success"] is True
            assert "当前只负责数据源 `mysql_test`" in worker_query
            assert "不要因为缺少其他数据源的 Schema 而返回空 SQL" in worker_query
            assert global_query in worker_query
            logger.info(
                "test_worker_scopes_global_query_to_current_datasource 完成",
                extra={"worker_query": worker_query},
            )
        except Exception as exc:
            logger.error(
                "test_worker_scopes_global_query_to_current_datasource 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 验证后续分析不会覆盖多源查询的失败来源说明。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_merge_results_discloses_failed_datasources(self, monkeypatch):
        """成功与失败来源并存时，最终摘要应明确列出失败来源。"""
        logger.debug("test_merge_results_discloses_failed_datasources 入口")
        try:
            # Arrange：模拟统计分析覆盖初始摘要，复现失败来源信息丢失。
            from unittest.mock import AsyncMock
            import src.graph.nodes.analyze_result as analyze_module
            import src.graph.nodes.generate_chart as chart_module
            import src.llm.client as llm_module
            import src.graph.nodes.multi_source as multi_source_module

            monkeypatch.setattr(llm_module, "is_task_llm_available", lambda task: False)
            monkeypatch.setattr(
                analyze_module,
                "analyze_result_node",
                AsyncMock(return_value={"analysis_result": {"summary": "成功来源分析完成"}}),
            )
            monkeypatch.setattr(
                chart_module,
                "generate_chart_node",
                AsyncMock(return_value={"chart_config": {"type": "table", "option": {}}}),
            )
            state = {
                "user_query": "汇总客户总数",
                "multi_source_results": [
                    {
                        "datasource": "mysql_test",
                        "success": True,
                        "dialect": "mysql",
                        "sql": "SELECT COUNT(*) AS total FROM customers",
                        "data": [{"total": 10}],
                    },
                    {
                        "datasource": "clickhouse_prod",
                        "success": False,
                        "error": "数据源连接失败或不存在",
                    },
                ],
            }

            # Act：合并跨数据源结果。
            result = await multi_source_module.merge_results_node(state)

            # Assert：统计分析摘要之后仍保留失败数量和来源名称。
            summary = result["analysis_result"]["summary"]
            assert "1 个数据源查询失败" in summary
            assert "clickhouse_prod" in summary
            logger.info(
                "test_merge_results_discloses_failed_datasources 完成",
                extra={"summary": summary},
            )
        except Exception as exc:
            logger.error(
                "test_merge_results_discloses_failed_datasources 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 验证任一来源的 SQL 安全校验失败后必须关闭该 worker 的执行路径。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_worker_stops_when_sql_validation_fails(self, monkeypatch):
        """多源 worker 校验失败时不得继续执行 SQL 或标记成功。"""
        logger.debug("test_worker_stops_when_sql_validation_fails 入口")
        try:
            # Arrange：模拟 MSSQL 方言校验失败，并隔离数据库执行。
            from types import SimpleNamespace
            from unittest.mock import AsyncMock, MagicMock

            import src.datasource.registry as registry_module
            import src.graph.nodes.execute_sql as execute_module
            import src.graph.nodes.generate_sql as generate_module
            import src.graph.nodes.layer3_validate as validate_module
            import src.graph.nodes.multi_source as multi_source_module
            import src.graph.nodes.retrieve_schema as retrieve_module

            registry = MagicMock()
            registry.resolve_or_none = AsyncMock(
                return_value=SimpleNamespace(schema=None, dialect="mssql")
            )
            monkeypatch.setattr(registry_module, "get_registry", MagicMock(return_value=registry))
            monkeypatch.setattr(
                retrieve_module,
                "retrieve_schema_node",
                AsyncMock(return_value={
                    "relevant_tables": [{"name": "customers", "columns": []}],
                    "dialect": "mssql",
                }),
            )
            monkeypatch.setattr(
                generate_module,
                "generate_sql_node",
                AsyncMock(return_value={"generated_sql": "DELETE FROM customers"}),
            )
            monkeypatch.setattr(
                validate_module,
                "layer3_validate_node",
                AsyncMock(return_value={
                    "validation_errors": [
                        {"type": "security_block", "message": "禁止: DELETE"}
                    ]
                }),
            )
            execute = AsyncMock(side_effect=AssertionError("校验失败后不得执行 SQL"))
            monkeypatch.setattr(execute_module, "execute_sql_node", execute)

            # Act
            result = await multi_source_module._analyze_one(
                "mssql_express",
                {"user_query": "删除客户", "selected_datasources": ["mssql_express"]},
            )

            # Assert
            assert result["success"] is False
            assert "禁止: DELETE" in result["error"]
            execute.assert_not_awaited()
            logger.info("test_worker_stops_when_sql_validation_fails 完成")
        except Exception as exc:
            logger.error(
                "test_worker_stops_when_sql_validation_fails 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 验证“有什么不同”按跨源比较处理，不得误判为可加指标求和。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_merge_results_treats_difference_query_as_comparison(self, monkeypatch):
        """C1 原句应保留各来源全部指标，并由 LLM 生成差异结论。"""
        logger.debug("test_merge_results_treats_difference_query_as_comparison 入口")
        try:
            # Arrange：构造两库真实形态结果，并隔离 LLM、统计分析和图表生成。
            from decimal import Decimal
            from types import SimpleNamespace
            from unittest.mock import AsyncMock

            import src.graph.nodes.analyze_result as analyze_module
            import src.graph.nodes.generate_chart as chart_module
            import src.graph.nodes.multi_source as multi_source_module
            import src.llm.client as llm_module

            llm = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(
                content="两库客户数相同，PostgreSQL 订单总额更高。"
            )))
            monkeypatch.setattr(
                llm_module,
                "is_task_llm_available",
                lambda task: task == "multi_source_merge",
            )
            monkeypatch.setattr(
                llm_module,
                "get_task_llm",
                lambda task, **kwargs: llm,
            )
            monkeypatch.setattr(
                analyze_module,
                "analyze_result_node",
                AsyncMock(return_value={"analysis_result": {"summary": "统计分析完成"}}),
            )
            monkeypatch.setattr(
                chart_module,
                "generate_chart_node",
                AsyncMock(return_value={"chart_config": {"type": "bar", "option": {}}}),
            )
            state = {
                "user_query": "MySQL和PostgreSQL两个库的客户总数和订单总额有什么不同？",
                "multi_source_results": [
                    {
                        "datasource": "mysql_test",
                        "success": True,
                        "dialect": "mysql",
                        "sql": "SELECT 1",
                        "data": [{
                            "total_customers": 100000,
                            "total_order_amount": Decimal("19959909800.20039"),
                        }],
                    },
                    {
                        "datasource": "postgres_main",
                        "success": True,
                        "dialect": "postgres",
                        "sql": "SELECT 1",
                        "data": [{
                            "customer_count": 100000,
                            "total_order_amount": Decimal("19969364512.749775"),
                        }],
                    },
                ],
            }

            # Act：合并 C1 两库结果。
            result = await multi_source_module.merge_results_node(state)

            # Assert：比较问题调用 LLM，且不因列别名不一致丢失客户数。
            rows = result["query_result_sample"]
            assert result["analysis_result"]["summary"] == "两库客户数相同，PostgreSQL 订单总额更高。"
            assert rows[0]["total_customers"] == 100000
            assert rows[1]["total_customers"] == 100000
            assert all("customer_count" not in row for row in rows)
            assert all("total_order_amount" in row for row in rows)
            llm.ainvoke.assert_awaited_once()
            logger.info(
                "test_merge_results_treats_difference_query_as_comparison 完成",
                extra={"row_count": len(rows)},
            )
        except Exception as exc:
            logger.error(
                "test_merge_results_treats_difference_query_as_comparison 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证跨源结果可以同时对齐维度列和任意数量的数值指标列。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_merge_results_aligns_dimensions_and_many_metrics(self, monkeypatch):
        """不同别名但角色顺序一致时，三个指标都应映射到统一列契约。"""
        logger.debug("test_merge_results_aligns_dimensions_and_many_metrics 入口")
        try:
            # Arrange：两个来源返回一个维度和三个同序指标，但每个别名均不同。
            from decimal import Decimal
            from unittest.mock import AsyncMock

            import src.graph.nodes.analyze_result as analyze_module
            import src.graph.nodes.generate_chart as chart_module
            import src.graph.nodes.multi_source as multi_source_module
            import src.llm.client as llm_module

            captured_states: list[dict] = []

            async def _capture_analysis(state: dict) -> dict:
                captured_states.append(state)
                return {
                    "analysis_result": {
                        "summary": "跨源指标已对齐",
                        "recommended_chart_type": "table",
                    }
                }

            monkeypatch.setattr(llm_module, "is_task_llm_available", lambda task: False)
            monkeypatch.setattr(analyze_module, "analyze_result_node", _capture_analysis)
            monkeypatch.setattr(
                chart_module,
                "generate_chart_node",
                AsyncMock(return_value={"chart_config": {"type": "table", "option": {}}}),
            )
            state = {
                "user_query": "两个数据库消费最高分类的销售额、订单数和客单价分别是什么？",
                "intent": "aggregation",
                "multi_source_results": [
                    {
                        "datasource": "mysql_test",
                        "success": True,
                        "dialect": "mysql",
                        "sql": "SELECT category_name, total_sales, order_count, average_order_value",
                        "data": [{
                            "category_name": "母婴用品",
                            "total_sales": Decimal("49436176364.97"),
                            "order_count": 1200,
                            "average_order_value": Decimal("41196813.64"),
                        }],
                    },
                    {
                        "datasource": "postgres_main",
                        "success": True,
                        "dialect": "postgres",
                        "sql": "SELECT product_category, total_spent, orders_total, avg_order_amount",
                        "data": [{
                            "product_category": "美妆护肤",
                            "total_spent": Decimal("47999293737.54"),
                            "orders_total": 1100,
                            "avg_order_amount": Decimal("43635721.58"),
                        }],
                    },
                ],
            }

            # Act：执行跨源合并。
            result = await multi_source_module.merge_results_node(state)

            # Assert：第二个来源的维度和三个指标全部使用首个来源的规范列名。
            rows = result["query_result_sample"]
            expected_columns = {
                "_datasource", "category_name", "total_sales",
                "order_count", "average_order_value",
            }
            assert len(rows) == 2
            assert all(set(row) == expected_columns for row in rows)
            assert rows[1]["category_name"] == "美妆护肤"
            assert rows[1]["total_sales"] == Decimal("47999293737.54")
            assert rows[1]["order_count"] == 1100
            assert rows[1]["average_order_value"] == Decimal("43635721.58")
            assert captured_states[0]["query_result_sample"] == rows
            logger.info(
                "test_merge_results_aligns_dimensions_and_many_metrics 完成",
                extra={"columns": sorted(expected_columns)},
            )
        except Exception as exc:
            logger.error(
                "test_merge_results_aligns_dimensions_and_many_metrics 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证列角色冲突时后端拒绝不安全的跨源字段合并。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_merge_results_preserves_columns_when_roles_conflict(self, monkeypatch):
        """同位置一边为数值、一边为文本时，应保留各自原始列名。"""
        logger.debug("test_merge_results_preserves_columns_when_roles_conflict 入口")
        try:
            # Arrange
            from unittest.mock import AsyncMock

            import src.graph.nodes.analyze_result as analyze_module
            import src.graph.nodes.generate_chart as chart_module
            import src.graph.nodes.multi_source as multi_source_module
            import src.llm.client as llm_module

            monkeypatch.setattr(llm_module, "is_task_llm_available", lambda task: False)
            monkeypatch.setattr(
                analyze_module,
                "analyze_result_node",
                AsyncMock(return_value={"analysis_result": {"summary": "保留原始字段"}}),
            )
            monkeypatch.setattr(
                chart_module,
                "generate_chart_node",
                AsyncMock(return_value={"chart_config": {"type": "table", "option": {}}}),
            )
            state = {
                "user_query": "比较两个来源",
                "multi_source_results": [
                    {
                        "datasource": "mysql_test", "success": True, "sql": "SELECT 1",
                        "data": [{"category_name": "母婴用品", "total_sales": 100}],
                    },
                    {
                        "datasource": "postgres_main", "success": True, "sql": "SELECT 1",
                        "data": [{"category_name": "美妆护肤", "status_text": "已完成"}],
                    },
                ],
            }

            # Act
            result = await multi_source_module.merge_results_node(state)

            # Assert
            rows = result["query_result_sample"]
            assert "total_sales" in rows[0]
            assert "status_text" in rows[1]
            assert "status_text" not in rows[0]
            logger.info("test_merge_results_preserves_columns_when_roles_conflict 完成")
        except Exception as exc:
            logger.error(
                "test_merge_results_preserves_columns_when_roles_conflict 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 验证跨源单行可加指标由脚本精确汇总，而不是进入分布分析。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_merge_results_sums_cross_source_totals(self, monkeypatch):
        """“汇总总数”应返回跨六个数据源的确定性总计 500003。"""
        logger.debug("test_merge_results_sums_cross_source_totals 入口")
        try:
            # Arrange：构造用户复现数据，并禁止统计处理器覆盖精确汇总。
            from unittest.mock import AsyncMock

            import src.graph.nodes.analyze_result as analyze_module
            import src.graph.nodes.generate_chart as chart_module
            import src.graph.nodes.multi_source as multi_source_module
            import src.llm.client as llm_module

            monkeypatch.setattr(llm_module, "is_task_llm_available", lambda task: False)
            analyze = AsyncMock(side_effect=AssertionError("精确跨源汇总不应进入分布处理器"))
            monkeypatch.setattr(analyze_module, "analyze_result_node", analyze)
            monkeypatch.setattr(
                chart_module,
                "generate_chart_node",
                AsyncMock(return_value={"chart_config": {"type": "bar", "option": {}}}),
            )
            source_counts = [
                ("mysql_test", "total_customers", 100000),
                ("clickhouse_test", "total_customers", 100000),
                ("postgres_main", "total_customers", 100000),
                ("mssql_express", "total_customers", 100000),
                ("oracle_xe", "COUNT(*)", 100000),
                ("demo", "customer_count", 3),
            ]
            state = {
                "user_query": "从所有数据库中汇总客户总数",
                "intent": "aggregation",
                "multi_source_results": [
                    {
                        "datasource": name,
                        "success": True,
                        "dialect": "sqlite" if name == "demo" else "database",
                        "sql": "SELECT COUNT(*) AS total_customers FROM customers",
                        "data": [{column: count}],
                    }
                    for name, column, count in source_counts
                ],
            }

            # Act
            result = await multi_source_module.merge_results_node(state)

            # Assert
            analysis = result["analysis_result"]
            assert analysis["processor_name"] == "cross_source_aggregation"
            assert analysis["cross_source_totals"] == {"total_customers": 500003}
            assert "500003" in analysis["summary"]
            assert len(result["query_result_sample"]) == 6
            assert result["query_result_full_count"] == 6
            assert all(
                set(row) == {"_datasource", "total_customers"}
                for row in result["query_result_sample"]
            )
            assert result["query_result_sample"][-1]["total_customers"] == 3
            analyze.assert_not_awaited()
            logger.info(
                "test_merge_results_sums_cross_source_totals 完成",
                extra={"grand_total": analysis["cross_source_totals"]["total_customers"]},
            )
        except Exception as exc:
            logger.error(
                "test_merge_results_sums_cross_source_totals 异常: %s",
                exc,
                exc_info=True,
            )
            raise


class TestE2E:
    """集成测试"""

    async def test_workflow_compiles(self, monkeypatch):
        from types import SimpleNamespace
        from src.app_context import AppContext, use_app_context_async
        from src.graph.workflow import build_workflow

        del monkeypatch
        context = AppContext(SimpleNamespace(database_url="", multi_tenant=False))
        async with use_app_context_async(context):
            assert await build_workflow() is not None
        await context.close()

    async def test_simple_query(self, monkeypatch):
        """完整链路: 无数据源 → 返回错误提示。"""
        from types import SimpleNamespace
        from src.app_context import AppContext, use_app_context_async
        import src.graph.nodes.generate_sql as generate_module
        from src.graph.workflow import build_workflow

        monkeypatch.setattr(generate_module, "is_llm_available", lambda: False)
        context = AppContext(SimpleNamespace(database_url="", multi_tenant=False))
        async with use_app_context_async(context):
            app = await build_workflow()
            r = await app.ainvoke({
                "user_query": "查昨天订单",
                "datasource": "nonexistent",
            }, {"configurable": {"thread_id": "test-simple-query"}})
        await context.close()
        assert "final_response" in r

    def test_dangerous_sql_blocked_at_node_level(self):
        """DROP 语句在 layer3_validate Node 被拦截 (已在 TestNodes 覆盖)。"""
        from src.graph.nodes.layer3_validate import layer3_validate_node
        r = asyncio.run(layer3_validate_node({"generated_sql": "DROP TABLE users"}))
        assert r["sql_valid"] is False

    async def test_retry_path(self, monkeypatch):
        from types import SimpleNamespace
        from src.app_context import AppContext, use_app_context_async
        import src.graph.nodes.generate_sql as generate_module
        from src.graph.workflow import build_workflow

        monkeypatch.setattr(generate_module, "is_llm_available", lambda: False)
        context = AppContext(SimpleNamespace(database_url="", multi_tenant=False))
        async with use_app_context_async(context):
            app = await build_workflow()
            r = await app.ainvoke({
                "user_query": "查",
                "generated_sql": "SELECT bad",
                "retry_count": 0,
            }, {"configurable": {"thread_id": "test-retry-path"}})
        await context.close()
        assert "final_response" in r
