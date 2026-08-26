"""数据库分析上下文与 Prompt 增强测试，覆盖 4.3.7、4.4.16、4.8.10。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.datasource.schema_snapshot import (
    ColumnInfo,
    SchemaSnapshot,
    TableRelation,
    TableSchema,
)

logger = logging.getLogger(__name__)


class _StreamingLLM:
    """记录 SQL Prompt 并返回受控流式 JSON 的测试模型。"""

    # 初始化消息记录容器。
    # Args: 无。
    # Returns: 无返回值。
    def __init__(self) -> None:
        logger.debug("初始化 _StreamingLLM 入口")
        self.messages = []
        logger.info("初始化 _StreamingLLM 完成")

    # 记录模型消息并流式返回单条合法 SQL。
    # Args: messages - LangChain 消息；config - Runnable 配置。
    # Returns: 异步生成一个 AIMessageChunk 兼容对象。
    async def astream(self, messages, config):
        logger.debug("_StreamingLLM.astream 入口", extra={"message_count": len(messages)})
        self.messages = messages
        yield SimpleNamespace(
            content=json.dumps({"sql": "SELECT COUNT(*) FROM orders", "explanation": "ok"}),
            additional_kwargs={},
        )
        logger.info("_StreamingLLM.astream 完成")


class TestRetrieveSchemaContext:
    """覆盖功能 4.3.7：Schema 上下文在节点边界无损传递。"""

    # 验证枚举字典属于 LangGraph 状态契约，避免节点写回后被过滤。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_enum_dictionary_is_declared_in_analysis_state(self):
        """AnalysisState 必须显式声明 enum_dictionary。"""
        logger.debug("test_enum_dictionary_is_declared_in_analysis_state 入口")
        from src.graph.state import AnalysisState

        assert "enum_dictionary" in AnalysisState.__annotations__
        logger.info("test_enum_dictionary_is_declared_in_analysis_state 完成")

    # 验证生成 SQL 所需的字段和关系不会在轻量化时丢失。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_retrieve_schema_preserves_database_semantics(self, monkeypatch):
        """节点输出应包含 PK、可空性、枚举、FK 和行数估算。"""
        logger.debug("test_retrieve_schema_preserves_database_semantics 入口")
        import src.graph.nodes.retrieve_schema as retrieve_module

        schema = SchemaSnapshot(
            tables=[TableSchema(
                name="orders",
                description="订单事实表",
                row_count_estimate=12345,
                columns=[
                    ColumnInfo(
                        name="id", type="BIGINT", is_nullable=False,
                        is_primary_key=True, is_indexed=True,
                    ),
                    ColumnInfo(
                        name="status", type="VARCHAR", enum_values=["paid", "refunded"],
                    ),
                ],
                relations=[
                    TableRelation(
                        target_table="users", join_key="orders.user_id = users.id",
                        relation_type="many_to_one",
                    ),
                ],
            )],
            business_rules=[{"content": "GMV 只统计 paid 订单"}],
            sql_templates=[{
                "question": "统计订单数",
                "sql": "SELECT COUNT(*) FROM orders",
            }],
        )
        monkeypatch.setattr(retrieve_module, "_load_enum_dictionary", AsyncMock(return_value={}))

        result = await retrieve_module.retrieve_schema_node({
            "datasource": "demo",
            "resolved_schema": schema,
            "dialect": "postgres",
            "intent": "chat",
        })

        table = result["relevant_tables"][0]
        assert table["row_count_estimate"] == 12345
        assert table["columns"][0]["is_primary_key"] is True
        assert table["columns"][0]["is_nullable"] is False
        assert table["columns"][1]["enum_values"] == ["paid", "refunded"]
        assert table["relations"] == [{
            "target_table": "users",
            "join_key": "orders.user_id = users.id",
            "relation_type": "many_to_one",
        }]
        assert result["business_rules_text"] == "GMV 只统计 paid 订单"
        assert result["few_shot_examples"][0]["sql"] == "SELECT COUNT(*) FROM orders"
        logger.info("test_retrieve_schema_preserves_database_semantics 完成")

    # 方法作用：验证知识库内容在长期记忆前写入 grounding 上下文。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言业务知识不会被预算优先截断。
    async def test_retrieve_schema_puts_knowledge_before_memory(self, monkeypatch):
        """有限 Prompt 预算下，业务规则应优先于可选长期记忆。"""
        # Arrange
        import src.graph.nodes.retrieve_schema as retrieve_module

        monkeypatch.setattr(
            retrieve_module,
            "_load_knowledge_context",
            AsyncMock(return_value="业务口径：VIP 必须使用 customers.customer_level='VIP'"),
        )
        monkeypatch.setattr(retrieve_module, "_load_enum_dictionary", AsyncMock(return_value={}))

        # Act
        result = await retrieve_module.retrieve_schema_node({
            "datasource": "demo",
            "resolved_schema": SchemaSnapshot(),
            "dialect": "postgres",
            "intent": "query",
            "long_term_memories_text": "长期记忆：历史 SQL 模板和用户偏好" * 200,
        })

        # Assert
        assert result["long_term_memories_text"].startswith("业务口径：VIP")

    # 方法作用：验证同一知识文件的多个命中分块只生成一个前端来源摘要。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言来源去重和命中数聚合。
    def test_knowledge_source_summaries_are_deduplicated(self) -> None:
        """知识正文可以使用多个 chunk，但结果卡片不得重复展示相同文件。"""
        # Arrange
        from src.graph.nodes.retrieve_schema import _deduplicate_knowledge_sources

        sources = [
            {
                "source_id": "orders-1",
                "source": "user_upload",
                "source_file": "orders.md",
                "category": "business_rule",
                "relevance": 0.91,
            },
            {
                "source_id": "orders-2",
                "source": "user_upload",
                "source_file": "orders.md",
                "category": "business_rule",
                "relevance": 0.86,
            },
            {
                "source_id": "customers-1",
                "source": "manual_doc",
                "source_file": "customers.md",
                "category": "metric",
                "relevance": 0.80,
            },
        ]

        # Act
        result = _deduplicate_knowledge_sources(sources)

        # Assert
        assert [item["source_file"] for item in result] == ["orders.md", "customers.md"]
        assert result[0]["match_count"] == 2
        assert result[0]["relevance"] == 0.91
        assert result[1]["match_count"] == 1
        assert _deduplicate_knowledge_sources([]) == []


class TestSQLPromptGrounding:
    """覆盖功能 4.4.16：SQL Prompt 证据上下文和约束。"""

    # 验证分散在 state 中的业务知识被结构化组装为一个上下文块。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_build_sql_grounding_context_includes_all_evidence(self):
        """业务规则、知识命中、枚举和已验证示例均应进入 SQL 上下文。"""
        logger.debug("test_build_sql_grounding_context_includes_all_evidence 入口")
        from src.graph.nodes.generate_sql import build_sql_grounding_context

        context = build_sql_grounding_context({
            "intent": "aggregation",
            "business_rules_text": "GMV 只统计 paid 订单",
            "long_term_memories_text": "退款金额需要单独扣除",
            "enum_dictionary": {"orders.status": ["paid", "refunded"]},
            "few_shot_examples": [{
                "question": "统计 GMV",
                "sql": "SELECT SUM(amount) FROM orders WHERE status='paid'",
            }],
        })

        assert "aggregation" in context
        assert "GMV 只统计 paid 订单" in context
        assert "退款金额需要单独扣除" in context
        assert "orders.status" in context and "refunded" in context
        assert "SELECT SUM(amount)" in context
        logger.info("test_build_sql_grounding_context_includes_all_evidence 完成")

    # 验证模型拿到真实方言、证据上下文和强制 SQL 正确性规则。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_llm_generate_uses_real_dialect_and_strong_constraints(self, monkeypatch):
        """SQL System Prompt 应约束粒度、JOIN 膨胀、NULL 和除零风险。"""
        logger.debug("test_llm_generate_uses_real_dialect_and_strong_constraints 入口")
        import src.graph.nodes.generate_sql as generate_module

        llm = _StreamingLLM()
        monkeypatch.setattr(generate_module, "get_llm", lambda temperature=0: llm)

        sql, _, explanation = await generate_module._llm_generate(
            schema_text="### 表: orders",
            dialect_hint="SQLite 方言",
            dialect="sqlite",
            query="统计已支付订单数",
            error_ctx="",
            skill_prompt="使用订单分析技能",
            grounding_context="GMV 只统计 paid 订单",
            config={},
            conversation_history=None,
            table_whitelist="合法物理表白名单：\n- orders\n- daily_sales",
        )

        system = llm.messages[0].content
        human = llm.messages[1].content
        assert "sqlite" in system.lower()
        assert "结果粒度" in system
        assert "JOIN" in system and "膨胀" in system
        assert "NULL" in system and "除零" in system
        assert "信息不足" in system
        assert "GMV 只统计 paid 订单" in human
        assert "daily_sales" in human
        assert "使用订单分析技能" in system
        assert sql == "SELECT COUNT(*) FROM orders"
        assert explanation == "ok"

    def test_select_prompt_tables_keeps_relevant_schema_and_budget(self):
        """详细 Schema 应优先保留与问题相关的表，同时不影响完整表白名单。"""
        # Arrange
        from src.graph.nodes.generate_sql import (
            build_table_whitelist_context,
            format_schema_for_prompt,
            select_prompt_tables,
        )

        tables = [
            {
                "name": "users",
                "description": "用户资料",
                "columns": [{"name": "user_id", "type": "BIGINT", "comment": "用户编号"}],
            },
            {
                "name": "orders",
                "description": "订单事实",
                "columns": [{"name": "order_amount", "type": "DECIMAL", "comment": "订单金额"}],
            },
            {
                "name": "inventory",
                "description": "库存快照",
                "columns": [{"name": "stock_qty", "type": "INT", "comment": "库存数量"}],
            },
        ]

        # Act
        selected = select_prompt_tables(
            tables,
            "统计订单金额",
            dialect="mysql",
            max_chars=900,
        )
        rendered = format_schema_for_prompt(selected, "mysql")
        whitelist = build_table_whitelist_context(tables)

        # Assert
        assert selected[0]["name"] == "orders"
        assert len(rendered) <= 900 or len(selected) == 1
        assert all(table["name"] in whitelist for table in tables)
        logger.info("test_llm_generate_uses_real_dialect_and_strong_constraints 完成")

    # 方法作用：验证详细 Schema 按列裁剪时保留业务相关列和 JOIN 安全列。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言无关列被移除且关系字段保留。
    def test_select_prompt_tables_prunes_irrelevant_columns(self) -> None:
        """大表只向模型暴露相关列、主键和关系字段，降低 Prompt 延迟。"""
        # Arrange
        from src.graph.nodes.generate_sql import select_prompt_tables

        orders = {
            "name": "orders",
            "description": "订单事实表",
            "columns": [
                {"name": "id", "type": "BIGINT", "is_primary_key": True},
                {"name": "customer_id", "type": "BIGINT", "is_indexed": True},
                {"name": "order_amount", "type": "DECIMAL", "comment": "订单金额"},
                {"name": "order_status", "type": "VARCHAR", "comment": "订单状态"},
                {"name": "internal_note", "type": "TEXT", "comment": "内部备注"},
            ],
            "relations": [{
                "target_table": "customers",
                "join_key": "orders.customer_id = customers.id",
                "relation_type": "many_to_one",
            }],
        }

        # Act
        selected = select_prompt_tables(
            [orders],
            "统计订单金额",
            dialect="mysql",
            max_chars=1_000,
        )
        selected_columns = {item["name"] for item in selected[0]["columns"]}

        # Assert
        assert {"id", "customer_id", "order_amount"}.issubset(selected_columns)
        assert "internal_note" not in selected_columns

    # 方法作用：验证 SQL 生成入口采用较小的详细 Schema 预算，避免恢复到完整大 Prompt。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言调用参数和结果状态。
    async def test_generate_sql_uses_column_budget(self, monkeypatch) -> None:
        """生成 SQL 时详细 Schema 上限应低于旧的 6000 字符预算。"""
        # Arrange
        import src.graph.nodes.generate_sql as generate_module

        captured: dict[str, object] = {}

        async def fake_llm(**kwargs):
            captured.update(kwargs)
            return "SELECT COUNT(*) FROM orders", "", "ok"

        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(generate_module, "_llm_generate", fake_llm)
        state = {
            "user_query": "统计订单金额",
            "datasource": "demo",
            "dialect": "mysql",
            "relevant_tables": [{
                "name": "orders",
                "columns": [
                    {"name": "id", "type": "BIGINT", "is_primary_key": True},
                    {"name": "order_amount", "type": "DECIMAL", "comment": "订单金额"},
                    {"name": "internal_note", "type": "TEXT", "comment": "内部备注"},
                ],
            }],
            "retry_count": 0,
        }

        # Act
        result = await generate_module.generate_sql_node(state, {})

        # Assert
        assert result["generated_sql"] == "SELECT COUNT(*) FROM orders"
        assert len(str(captured["schema_text"])) < 6_000
        assert "internal_note" not in str(captured["schema_text"])

    # 方法作用：验证同步 LLM 工厂冷启动不会阻塞多数据源 worker 共用的事件循环。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_llm_factory_does_not_block_event_loop(self, monkeypatch):
        """模型构造较慢时，事件循环中的其他协程仍应按时获得执行机会。"""
        # Arrange
        import src.graph.nodes.generate_sql as generate_module

        llm = _StreamingLLM()

        # 方法作用：模拟真实环境中耗时的同步模型冷启动。
        # Args: temperature - 生成温度。
        # Returns: 固定流式测试模型。
        def slow_get_llm(temperature=0):
            time.sleep(0.15)
            return llm

        monkeypatch.setattr(generate_module, "get_llm", slow_get_llm)
        started = time.monotonic()

        # Act
        task = asyncio.create_task(generate_module._llm_generate(
            schema_text="### 表: orders",
            dialect_hint="PostgreSQL 方言",
            dialect="postgres",
            query="统计订单数",
            error_ctx="",
            skill_prompt="",
            grounding_context="",
            config={},
            conversation_history=None,
        ))
        await asyncio.sleep(0.02)
        heartbeat_elapsed = time.monotonic() - started
        result = await task

        # Assert
        assert heartbeat_elapsed < 0.1
        assert result[0] == "SELECT COUNT(*) FROM orders"

    # 方法作用：验证流式连接在首块前僵死时只回退一次非流式调用。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_llm_generate_falls_back_to_non_stream_when_first_chunk_times_out(
        self,
        monkeypatch,
    ) -> None:
        """零块超时属于可恢复传输故障，必须复用原 Prompt 完成 SQL 生成。"""
        # Arrange
        from langchain_openai import StreamChunkTimeoutError

        import src.graph.nodes.generate_sql as generate_module

        calls: list[bool] = []
        captured: dict[str, object] = {}

        class TimedOutStreamingLLM:
            # 方法作用：模拟连接建立后没有收到任何 SSE 块的超时。
            # Args: messages - 原始消息；config - Runnable 配置。
            # Returns: 不返回数据并抛出 StreamChunkTimeoutError。
            async def astream(self, messages, config):
                del messages, config
                if False:
                    yield None
                raise StreamChunkTimeoutError(
                    0.1,
                    model_name="deepseek-v4-flash",
                    chunks_received=0,
                )

        class NonStreamingLLM:
            # 方法作用：记录回退消息并返回合法 SQL JSON。
            # Args: messages - 原始消息；config - Runnable 配置。
            # Returns: 包含合法 SQL 的非流式响应。
            async def ainvoke(self, messages, config):
                captured["messages"] = messages
                captured["config"] = config
                return SimpleNamespace(
                    content=json.dumps({
                        "sql": "SELECT SUM(amount) FROM sales",
                        "explanation": "stream fallback",
                    }),
                    additional_kwargs={},
                )

        # 方法作用：按调用模式返回流式超时或非流式成功模型。
        # Args: temperature - 生成温度；stream - 是否创建流式模型。
        # Returns: 对应调用模式的测试模型。
        def fake_get_llm(temperature=0, *, stream=True):
            del temperature
            calls.append(stream)
            return TimedOutStreamingLLM() if stream else NonStreamingLLM()

        monkeypatch.setattr(generate_module, "get_llm", fake_get_llm)

        # Act
        sql, reasoning, explanation = await generate_module._llm_generate(
            schema_text="### 表: sales",
            dialect_hint="SQLite 方言",
            dialect="sqlite",
            query="统计销售额",
            error_ctx="",
            skill_prompt="",
            grounding_context="",
            config={"metadata": {"request_id": "fallback-1"}},
            conversation_history=None,
        )

        # Assert
        assert calls == [True, False]
        assert sql == "SELECT SUM(amount) FROM sales"
        assert reasoning == ""
        assert explanation == "stream fallback"
        assert captured["messages"]
        assert captured["config"]["metadata"]["request_id"] == "fallback-1"


class TestAnalysisPromptGrounding:
    """覆盖功能 4.8.10、4.8.11：分析提示完整性、证据化输出和补查回注。"""

    # 验证分析模型收到原问题、截断信息，并保留增强输出字段。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_llm_analysis_receives_question_and_completeness(self, monkeypatch):
        """采样分析必须声明边界，不能把局部结果伪装成全量结论。"""
        logger.debug("test_llm_analysis_receives_question_and_completeness 入口")
        import src.graph.nodes.analyze_result as analyze_module
        import src.llm.adapters.registry as adapter_module

        response = SimpleNamespace(content=json.dumps({
            "summary": "退款率上升",
            "insights": ["样本中退款占比增加"],
            "recommended_chart_type": "line",
            "follow_up_questions": ["是否按渠道拆分"],
            "data_quality": ["结果被截断"],
            "limitations": ["无法证明促销导致退款"],
            "confidence": "medium",
            "recommended_actions": ["按渠道核查退款原因"],
        }, ensure_ascii=False))
        llm = SimpleNamespace(ainvoke=AsyncMock(return_value=response))
        monkeypatch.setattr(analyze_module, "get_llm", lambda temperature=0.3: llm)
        monkeypatch.setattr(
            adapter_module,
            "get_adapter",
            lambda model: SimpleNamespace(
                parse_response=lambda resp: SimpleNamespace(reasoning_content=""),
            ),
        )

        result = await analyze_module._llm_analyze(
            [{"refund_rate": 0.2}, {"refund_rate": 0.3}],
            "SELECT refund_rate FROM daily_metrics",
            {"columns": {"refund_rate": {"mean": 0.25}}},
            "趋势: 上升",
            "",
            "",
            user_query="为什么退款率上升",
            result_full_count=500,
            result_truncated=True,
            business_context="退款口径不含取消订单",
        )

        prompt = llm.ainvoke.await_args.args[0][1].content
        system = llm.ainvoke.await_args.args[0][0].content
        assert "为什么退款率上升" in prompt
        assert "500" in prompt and "截断" in prompt
        assert "退款口径不含取消订单" in prompt
        assert "相关性" in system and "因果" in system
        assert result["limitations"] == ["无法证明促销导致退款"]
        assert result["confidence"] == "medium"
        assert result["recommended_actions"] == ["按渠道核查退款原因"]
        logger.info("test_llm_analysis_receives_question_and_completeness 完成")

    async def test_reanalysis_receives_authorized_schema_and_supplemental_evidence(
        self,
        monkeypatch,
    ) -> None:
        """补查后的重新分析必须同时看到授权字段和已执行证据。"""
        # Arrange
        logger.debug("test_reanalysis_receives_authorized_schema_and_supplemental_evidence 入口")
        import src.graph.nodes.analyze_result as analyze_module
        import src.llm.adapters.registry as adapter_module

        response = SimpleNamespace(content=json.dumps({
            "summary": "需求与交付差额为二",
            "insights": ["需求工单数为五十，交付工单数为四十八"],
            "evidence_status": "sufficient",
            "evidence_gaps": [],
            "supplemental_questions": [],
            "recommended_chart_type": "line",
        }, ensure_ascii=False))
        llm = SimpleNamespace(ainvoke=AsyncMock(return_value=response))
        monkeypatch.setattr(analyze_module, "get_llm", lambda temperature=0.3: llm)
        monkeypatch.setattr(
            adapter_module,
            "get_adapter",
            lambda model: SimpleNamespace(
                parse_response=lambda resp: SimpleNamespace(
                    content=resp.content,
                    reasoning_content="",
                ),
            ),
        )
        schema_context = (
            '[{"table":"daily_orders","columns":['
            '{"name":"f_daily_delivered_orders","comment":"当日交付工单数"}]}]'
        )
        supplemental_evidence = (
            '[{"question":"联查每日交付工单数","success":true,'
            '"data":[{"delivered_orders":48}]}]'
        )

        # Act
        result = await analyze_module._llm_analyze(
            [{"demand_orders": 50}],
            "SELECT demand_orders FROM daily_orders",
            {"columns": {"demand_orders": {"mean": 50}}},
            "需求工单数持平",
            "",
            "",
            user_query="分析每日需求工单数",
            schema_context=schema_context,
            supplemental_evidence=supplemental_evidence,
        )

        # Assert
        prompt = llm.ainvoke.await_args.args[0][1].content
        assert "当前授权 Schema" in prompt
        assert "f_daily_delivered_orders" in prompt
        assert "已执行的补查证据" in prompt
        assert "delivered_orders" in prompt
        assert result["evidence_status"] == "sufficient"
        logger.info("test_reanalysis_receives_authorized_schema_and_supplemental_evidence 完成")
