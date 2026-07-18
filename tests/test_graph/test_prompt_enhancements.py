"""数据库分析上下文与 Prompt 增强测试，覆盖 4.3.7、4.4.16、4.8.10。"""

from __future__ import annotations

import json
import logging
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
        )

        system = llm.messages[0].content
        human = llm.messages[1].content
        assert "sqlite" in system.lower()
        assert "结果粒度" in system
        assert "JOIN" in system and "膨胀" in system
        assert "NULL" in system and "除零" in system
        assert "信息不足" in system
        assert "GMV 只统计 paid 订单" in human
        assert "使用订单分析技能" in system
        assert sql == "SELECT COUNT(*) FROM orders"
        assert explanation == "ok"
        logger.info("test_llm_generate_uses_real_dialect_and_strong_constraints 完成")


class TestAnalysisPromptGrounding:
    """覆盖功能 4.8.10：分析 Prompt 完整性和证据化输出。"""

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
