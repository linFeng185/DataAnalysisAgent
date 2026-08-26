"""证据不足自主补查闭环测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock


class TestSupplementalQuery:
    """覆盖功能 4.8.11：补查选择、查询安全链、轮次预算和失败降级。"""

    # 方法作用：验证同表补查问题被合并为单个任务。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言合并任务契约。
    def test_select_question_merges_same_table_requests(self) -> None:
        """同一张表的多个证据缺口应合并为一次 SQL 补查并保留来源问题。"""
        # Arrange
        from src.graph.nodes.supplemental_query import (
            build_supplemental_request,
        )

        analysis = {
            "supplemental_questions": [
                "users 表中共有多少用户？",
                "各 vip_level 等级的用户数量分别是多少？",
                "订单总数是多少？",
            ],
        }
        evidence = []

        # Act
        request = build_supplemental_request(
            analysis,
            evidence,
            relevant_tables=[
                {"name": "users", "columns": []},
                {"name": "orders", "columns": []},
            ],
            primary_sql="SELECT COUNT(*) AS total_users FROM users",
        )

        # Assert
        assert len(request["source_questions"]) == 2
        assert "users 表中共有多少用户？" in request["question"]
        assert "各 vip_level 等级的用户数量分别是多少？" in request["question"]
        assert "一条 SQL" in request["question"]
        assert request["table_names"] == ["users"]

    # 方法作用：验证不同表补查问题不会被错误合并。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言问题来源列表保持独立。
    def test_select_question_does_not_merge_different_tables(self) -> None:
        """不同数据表的问题不得被错误合并，后续轮次仍可分别执行。"""
        # Arrange
        from src.graph.nodes.supplemental_query import build_supplemental_request

        analysis = {
            "supplemental_questions": [
                "users 表中共有多少用户？",
                "订单总数是多少？",
            ],
        }

        # Act
        request = build_supplemental_request(
            analysis,
            [],
            relevant_tables=[
                {"name": "users", "columns": []},
                {"name": "orders", "columns": []},
            ],
            primary_sql="SELECT COUNT(*) FROM users",
        )

        # Assert
        assert request["source_questions"] == ["users 表中共有多少用户？"]
        assert "订单总数是多少？" not in request["question"]

    # 方法作用：验证字段注释和业务别名能把自然语言补查归属到同一张表。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言同表问题被合并。
    def test_select_question_uses_column_comments_and_aliases(self) -> None:
        """用户说业务别名而不是物理列名时，同表补查仍应合并。"""
        # Arrange
        from src.graph.nodes.supplemental_query import build_supplemental_request

        analysis = {
            "supplemental_questions": [
                "订单金额总和是多少？",
                "平均客单价是多少？",
                "用户总数是多少？",
            ],
        }

        # Act
        request = build_supplemental_request(
            analysis,
            [],
            relevant_tables=[
                {
                    "name": "orders",
                    "description": "订单事实表",
                    "columns": [
                        {"name": "order_amount", "comment": "订单金额"},
                        {"name": "customer_unit_price", "comment": "客单价"},
                    ],
                },
                {"name": "users", "columns": [{"name": "user_id", "comment": "用户编号"}]},
            ],
            primary_sql="SELECT SUM(order_amount) FROM orders",
        )

        # Assert
        assert request["table_names"] == ["orders"]
        assert request["source_questions"] == [
            "订单金额总和是多少？",
            "平均客单价是多少？",
        ]
        assert "一条 SQL" in request["question"]

    # 方法作用：验证合并任务的来源问题会参与后续去重。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言不再返回重复任务。
    def test_select_question_skips_source_questions_from_merged_evidence(self) -> None:
        """已执行合并任务中的原始问题再次出现时不得重复补查。"""
        # Arrange
        from src.graph.nodes.supplemental_query import select_supplemental_question

        analysis = {
            "supplemental_questions": [
                "users 表中共有多少用户？",
                "各 vip_level 等级的用户数量分别是多少？",
            ],
        }
        evidence = [{
            "question": "请使用一条 SQL 一次性返回所有补查所需结果",
            "source_questions": [
                "users 表中共有多少用户？",
                "各 vip_level 等级的用户数量分别是多少？",
            ],
            "success": True,
        }]

        # Act
        question = select_supplemental_question(analysis, evidence)

        # Assert
        assert question == ""

    def test_select_question_skips_normalized_duplicates(self) -> None:
        """已执行问题应忽略空白和大小写差异，选择下一条新问题。"""
        # Arrange
        from src.graph.nodes.supplemental_query import select_supplemental_question

        analysis = {
            "supplemental_questions": [
                "按 车间 拆分需求工单数",
                "联查每日交付工单数",
            ],
        }
        evidence = [{"question": "按车间拆分需求工单数", "success": True}]

        # Act
        question = select_supplemental_question(analysis, evidence)

        # Assert
        assert question == "联查每日交付工单数"

    def test_route_enters_supplemental_query_for_single_source(self, monkeypatch) -> None:
        """单数据源分析声明可补查且预算未耗尽时应进入补查节点。"""
        # Arrange
        import src.config as config_module
        from src.graph.workflow import after_analyze_result

        monkeypatch.setattr(
            config_module,
            "get_settings",
            lambda: SimpleNamespace(max_supplemental_query_rounds=2),
        )
        state = {
            "intent": "attribution",
            "datasource": "demo",
            "selected_datasources": ["demo"],
            "analysis_result": {
                "evidence_status": "supplementable",
                "supplemental_questions": ["联查每日交付工单数"],
            },
            "supplemental_query_round": 0,
            "supplemental_evidence": [],
        }

        # Act
        target = after_analyze_result(state)

        # Assert
        assert target == "supplemental_query"

    def test_route_stops_when_round_budget_is_exhausted(self, monkeypatch) -> None:
        """达到补查轮次上限后必须继续图表阶段，不能形成无界循环。"""
        # Arrange
        import src.config as config_module
        from src.graph.workflow import after_analyze_result

        monkeypatch.setattr(
            config_module,
            "get_settings",
            lambda: SimpleNamespace(max_supplemental_query_rounds=2),
        )
        state = {
            "intent": "query",
            "datasource": "demo",
            "selected_datasources": ["demo"],
            "analysis_result": {
                "evidence_status": "supplementable",
                "supplemental_questions": ["联查每日交付工单数"],
            },
            "supplemental_query_round": 2,
            "supplemental_evidence": [],
        }

        # Act
        target = after_analyze_result(state)

        # Assert
        assert target == "generate_chart"

    def test_route_stops_when_only_duplicate_question_remains(self, monkeypatch) -> None:
        """模型重复提出已经执行的问题时不得再次访问数据库。"""
        # Arrange
        import src.config as config_module
        from src.graph.workflow import after_analyze_result

        monkeypatch.setattr(
            config_module,
            "get_settings",
            lambda: SimpleNamespace(max_supplemental_query_rounds=2),
        )
        state = {
            "intent": "query",
            "datasource": "demo",
            "selected_datasources": ["demo"],
            "analysis_result": {
                "evidence_status": "supplementable",
                "supplemental_questions": ["联查 每日交付工单数"],
            },
            "supplemental_query_round": 1,
            "supplemental_evidence": [
                {"question": "联查每日交付工单数", "success": True},
            ],
        }

        # Act
        target = after_analyze_result(state)

        # Assert
        assert target == "generate_chart"

    async def test_execute_question_reuses_complete_sql_safety_chain(self, monkeypatch) -> None:
        """每次补查必须依次经过结构检索、生成、双层校验和受限执行。"""
        # Arrange
        import src.config as config_module
        import src.graph.nodes.execute_sql as execute_module
        import src.graph.nodes.generate_sql as generate_module
        import src.graph.nodes.layer3_validate as layer3_module
        import src.graph.nodes.layer4_explain as layer4_module
        import src.graph.nodes.retrieve_schema as retrieve_module
        from src.graph.nodes.supplemental_query import _execute_supplemental_question

        calls: list[str] = []

        async def retrieve_schema(state: dict) -> dict:
            calls.append("retrieve_schema")
            assert state["allowed_columns"] == ["f_date", "f_daily_delivered_orders"]
            return {"relevant_tables": [{"name": "daily_orders", "columns": []}]}

        async def generate_sql(state: dict, config: dict) -> dict:
            calls.append("generate_sql")
            assert state["relevant_tables"][0]["name"] == "daily_orders"
            assert config["metadata"]["supplemental"] is True
            return {"generated_sql": "SELECT SUM(f_daily_delivered_orders) FROM daily_orders"}

        async def validate_sql(state: dict) -> dict:
            calls.append("layer3_validate")
            assert state["generated_sql"].startswith("SELECT")
            return {"validation_errors": [], "sql_valid": True}

        async def explain_sql(state: dict) -> dict:
            calls.append("layer4_explain")
            assert state["sql_valid"] is True
            return {"explain_errors": [], "sql_explain_checked": True}

        async def execute_sql(state: dict) -> dict:
            calls.append("execute_sql")
            assert state["sql_explain_checked"] is True
            return {
                "query_result_sample": [{"delivered_orders": 9}],
                "query_result_full_count": 1,
                "execution_error": "",
            }

        monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(max_retry_count=2))
        monkeypatch.setattr(retrieve_module, "retrieve_schema_node", retrieve_schema)
        monkeypatch.setattr(generate_module, "generate_sql_node", generate_sql)
        monkeypatch.setattr(layer3_module, "layer3_validate_node", validate_sql)
        monkeypatch.setattr(layer4_module, "layer4_explain_node", explain_sql)
        monkeypatch.setattr(execute_module, "execute_sql_node", execute_sql)
        state = {
            "user_query": "分析每日需求工单数",
            "datasource": "demo",
            "selected_datasources": ["demo"],
            "dialect": "mysql",
            "allowed_columns": ["f_date", "f_daily_delivered_orders"],
            "request_rate_limit_checked": True,
        }

        # Act
        record = await _execute_supplemental_question(
            state,
            "联查每日交付工单数",
            {"metadata": {"request_id": "supplement-test"}},
        )

        # Assert
        assert calls == [
            "retrieve_schema",
            "generate_sql",
            "layer3_validate",
            "layer4_explain",
            "execute_sql",
        ]
        assert record == {
            "question": "联查每日交付工单数",
            "success": True,
            "sql": "SELECT SUM(f_daily_delivered_orders) FROM daily_orders",
            "dialect": "mysql",
            "row_count": 1,
            "complete": True,
            "data": [{"delivered_orders": 9}],
            "error": "",
        }

    # 方法作用：验证补查复用主查询已授权上下文，避免重复 Schema 和知识库检索。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言检索节点未被重复调用且缓存字段继续传递。
    async def test_execute_question_reuses_cached_schema_context(self, monkeypatch) -> None:
        """同一请求的补查不应再次访问 Schema/向量检索。"""
        # Arrange
        import src.graph.nodes.execute_sql as execute_module
        import src.graph.nodes.generate_sql as generate_module
        import src.graph.nodes.layer3_validate as layer3_module
        import src.graph.nodes.layer4_explain as layer4_module
        import src.graph.nodes.retrieve_schema as retrieve_module
        from src.graph.nodes.supplemental_query import _execute_supplemental_question

        retrieve = AsyncMock(side_effect=AssertionError("补查不应重复检索 Schema"))
        monkeypatch.setattr(retrieve_module, "retrieve_schema_node", retrieve)

        async def generate_sql(state: dict, config: dict) -> dict:
            del config
            assert state["relevant_tables"][0]["name"] == "orders"
            assert state["business_rules_text"] == "订单金额只统计 paid"
            assert state["activated_knowledge"][0]["source_file"] == "orders.md"
            return {"generated_sql": "SELECT COUNT(*) AS total FROM orders"}

        async def validate_sql(state: dict) -> dict:
            assert state["generated_sql"].startswith("SELECT")
            return {"validation_errors": [], "sql_valid": True}

        async def explain_sql(state: dict) -> dict:
            return {"explain_errors": [], "sql_explain_checked": True}

        async def execute_sql(state: dict) -> dict:
            return {
                "query_result_sample": [{"total": 3}],
                "query_result_full_count": 1,
                "execution_error": "",
            }

        monkeypatch.setattr(generate_module, "generate_sql_node", generate_sql)
        monkeypatch.setattr(layer3_module, "layer3_validate_node", validate_sql)
        monkeypatch.setattr(layer4_module, "layer4_explain_node", explain_sql)
        monkeypatch.setattr(execute_module, "execute_sql_node", execute_sql)
        state = {
            "user_query": "统计订单数",
            "datasource": "demo",
            "selected_datasources": ["demo"],
            "dialect": "mysql",
            "resolved_schema": object(),
            "relevant_tables": [{"name": "orders", "columns": []}],
            "business_rules_text": "订单金额只统计 paid",
            "activated_knowledge": [{"source_file": "orders.md"}],
            "request_rate_limit_checked": True,
        }

        # Act
        record = await _execute_supplemental_question(state, "统计订单数", {})

        # Assert
        assert record["success"] is True
        retrieve.assert_not_awaited()

    async def test_node_appends_successful_evidence_and_requests_reanalysis(self, monkeypatch) -> None:
        """补查成功后应累计证据、增加轮次并清空旧分析触发重新分析。"""
        # Arrange
        import src.config as config_module
        import src.graph.nodes.supplemental_query as supplemental_module

        record = {
            "question": "联查每日交付工单数",
            "success": True,
            "sql": "SELECT delivered FROM daily_orders",
            "dialect": "mysql",
            "row_count": 3,
            "data": [{"delivered": 48}],
            "error": "",
        }
        execute = AsyncMock(return_value=record)
        monkeypatch.setattr(supplemental_module, "_execute_supplemental_question", execute)
        monkeypatch.setattr(
            config_module,
            "get_settings",
            lambda: SimpleNamespace(max_supplemental_query_rounds=2),
        )
        state = {
            "user_query": "分析每日需求工单数",
            "analysis_result": {
                "evidence_status": "supplementable",
                "supplemental_questions": ["联查每日交付工单数"],
            },
            "supplemental_query_round": 0,
            "supplemental_evidence": [],
            "query_result_sample": [{"date": "2026-08-01", "demand": 50}],
            "generated_sql": "SELECT date, demand FROM daily_orders",
        }

        # Act
        update = await supplemental_module.supplemental_query_node(state, {})

        # Assert
        assert update["supplemental_query_round"] == 1
        assert update["supplemental_evidence"] == [record]
        assert update["analysis_result"] == {}
        assert "query_result_sample" not in update
        assert "generated_sql" not in update
        execute.assert_awaited_once()

    async def test_failed_supplement_preserves_primary_result(self, monkeypatch) -> None:
        """补查失败只能记录限制，不得覆盖主查询数据和主查询 SQL。"""
        # Arrange
        import src.config as config_module
        import src.graph.nodes.supplemental_query as supplemental_module

        failed_record = {
            "question": "联查每日交付工单数",
            "success": False,
            "sql": "",
            "dialect": "mysql",
            "row_count": 0,
            "data": [],
            "error": "字段不可用",
        }
        monkeypatch.setattr(
            supplemental_module,
            "_execute_supplemental_question",
            AsyncMock(return_value=failed_record),
        )
        monkeypatch.setattr(
            config_module,
            "get_settings",
            lambda: SimpleNamespace(max_supplemental_query_rounds=2),
        )
        state = {
            "user_query": "分析每日需求工单数",
            "analysis_result": {
                "evidence_status": "supplementable",
                "supplemental_questions": ["联查每日交付工单数"],
            },
            "supplemental_query_round": 0,
            "supplemental_evidence": [],
            "query_result_sample": [{"date": "2026-08-01", "demand": 50}],
            "generated_sql": "SELECT date, demand FROM daily_orders",
        }

        # Act
        update = await supplemental_module.supplemental_query_node(state, {})
        merged = {**state, **update}

        # Assert
        assert merged["query_result_sample"] == state["query_result_sample"]
        assert merged["generated_sql"] == state["generated_sql"]
        assert merged["supplemental_evidence"][0]["success"] is False
        assert merged["supplemental_evidence"][0]["error"] == "字段不可用"


class TestSupplementalResponse:
    """覆盖功能 4.8.11：主查询与成功补查 SQL 的公开响应契约。"""

    async def test_response_contains_primary_and_successful_supplemental_sql(self) -> None:
        """最终响应应区分主查询和成功补查，并忽略没有执行成功的补查 SQL。"""
        # Arrange
        from src.graph.nodes.build_response import build_response_node

        state = {
            "user_query": "分析每日需求工单数",
            "datasource": "demo",
            "dialect": "mysql",
            "generated_sql": "SELECT f_date, demand FROM daily_orders",
            "query_result_sample": [{"f_date": "2026-08-01", "demand": 50}],
            "query_result_full_count": 1,
            "analysis_result": {"summary": "需求工单数稳定"},
            "chart_config": {"type": "none", "option": {}, "status": "not_applicable"},
            "supplemental_evidence": [
                {
                    "question": "联查每日交付工单数",
                    "success": True,
                    "sql": "SELECT f_date, delivered FROM daily_orders",
                    "dialect": "mysql",
                },
                {
                    "question": "联查设备负荷",
                    "success": False,
                    "sql": "SELECT load FROM devices",
                    "dialect": "mysql",
                },
            ],
        }

        # Act
        response = (await build_response_node(state))["final_response"]

        # Assert
        assert len(response["sql_statements"]) == 2
        assert response["sql_statements"][0]["purpose"] == "primary"
        assert response["sql_statements"][1]["purpose"] == "supplemental"
        assert response["sql_statements"][1]["question"] == "联查每日交付工单数"
        assert "SELECT load FROM devices" not in response["sql"]
