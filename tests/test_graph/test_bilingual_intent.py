"""功能 34.1/34.2：中英文意图规则回退一致性与既有路由回归。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.llm.output_contracts import TaskPlanOutput


# 方法作用：把分类模型与 Skill 激活替换为确定性替身，隔离外部依赖。
# Args: monkeypatch - pytest 补丁工具；llm_result - _llm_classify 返回值或 None；skill_sink - 记录 Skill 激活入参的列表。
# Returns: 无返回值。
def _patch_boundaries(
    monkeypatch: Any,
    llm_result: TaskPlanOutput | None = None,
    skill_sink: list[dict[str, Any]] | None = None,
) -> None:
    import src.graph.nodes.classify_intent as classify
    import src.graph.skill_activation as activation

    monkeypatch.setattr(classify, "_llm_classify", AsyncMock(return_value=llm_result))

    # 方法作用：记录 Skill 激活入参并返回固定两阶段激活结果。
    # Args: state - 分类节点传入的状态增量。
    # Returns: 与真实 activate_skills 同构的字段字典。
    def _fake_activate(state: dict[str, Any]) -> dict[str, Any]:
        if skill_sink is not None:
            skill_sink.append(state)
        return {
            "activated_skills": ["sales_skill"],
            "activated_skill_ids": ["skill-sales"],
            "skill_candidate_ids": ["skill-sales"],
            "skill_activation_stage": "intent",
            "skill_prompt_override": "override",
            "skill_tools": [],
            "skill_tool_budget": 2,
        }

    monkeypatch.setattr(activation, "activate_skills", _fake_activate)


class TestBilingualIntentFallback:
    """覆盖功能 34.1：模型不可用时中英文同义请求进入相同能力。"""

    # 验证模型不可用时英文规则回退与中文主链识别同一意图。
    # Args: self - 实例；monkeypatch - 补丁工具；query - 用户问题；expected - 期望意图。
    # Returns: 无返回值。
    @pytest.mark.parametrize("query,expected", [
        ("How many orders were placed this month?", "query"),
        ("What is the average order amount?", "query"),
        ("Show the total sales for this month", "query"),
        ("What columns are in orders?", "metadata"),
        ("List available tables", "metadata"),
        ("Show sales trends for the last year", "trend"),
        ("Why did sales decline this month?", "attribution"),
        ("Rank products by revenue this month", "aggregation"),
        ("Hello", "chat"),
        ("What is the correlation between sales and returns?", "attribution"),
        ("Show revenue by category this month", "aggregation"),
        ("Sales trend over time", "trend"),
        ("Thanks!", "chat"),
        ("What can you do", "chat"),
        ("本月有多少订单", "query"),
    ])
    async def test_model_unavailable_language_parity(
        self, monkeypatch: Any, query: str, expected: str,
    ) -> None:
        """规则回退必须把中英文同义问题映射到同一意图与任务能力。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(monkeypatch)

        # Act
        result = await classify.classify_intent_node({"user_query": query})

        # Assert
        assert result["intent"] == expected
        expected_capability = "direct_answer" if expected in {"metadata", "chat"} else "sql_analysis"
        assert result["task_plan"]["capability"] == expected_capability

    # 验证大小写与标点差异不影响英文规则命中。
    # Args: self - 实例；monkeypatch - 补丁工具；query - 用户问题；expected - 期望意图。
    # Returns: 无返回值。
    @pytest.mark.parametrize("query,expected", [
        ("LIST AVAILABLE TABLES!", "metadata"),
        ("How MANY orders were PLACED?", "query"),
        ("  Show the TOTAL sales.  ", "query"),
        ("WHY did sales decline??", "attribution"),
    ])
    async def test_english_case_and_punctuation_invariance(
        self, monkeypatch: Any, query: str, expected: str,
    ) -> None:
        """英文规则匹配必须对大小写、首尾空白和标点保持稳定。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(monkeypatch)

        # Act
        result = await classify.classify_intent_node({"user_query": query})

        # Assert
        assert result["intent"] == expected

    # 验证包含统计子串的普通单词不得误命中英文统计规则。
    # Args: self - 实例；monkeypatch - 补丁工具；query - 用户问题。
    # Returns: 无返回值。
    @pytest.mark.parametrize("query", [
        "What is the account balance this month?",
        "Give me a summary of the data",
        "Show me the user_count metric",
    ])
    async def test_substring_words_do_not_trigger_statistics(
        self, monkeypatch: Any, query: str,
    ) -> None:
        """account/summary/user_count 不得因 count/sum 子串进入 SQL 链路。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(monkeypatch)

        # Act
        result = await classify.classify_intent_node({"user_query": query})

        # Assert
        assert result["intent"] == "chat"
        assert result["task_plan"]["capability"] == "direct_answer"

    # 验证词边界误报在模型返回闲聊时也不会被统计兜底改写。
    # Args: self - 实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_substring_words_survive_chat_model_result(self, monkeypatch: Any) -> None:
        """模型回答 chat 时 account/summary 仍不得触发统计兜底。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(
            monkeypatch,
            TaskPlanOutput(intent="chat", operation="conversation", confidence=0.9),
        )

        # Act
        result = await classify.classify_intent_node(
            {"user_query": "What is the account balance this month?"},
        )

        # Assert
        assert result["intent"] == "chat"

    # 验证完整英文统计词组仍能命中 query。
    # Args: self - 实例；monkeypatch - 补丁工具；query - 用户问题。
    # Returns: 无返回值。
    @pytest.mark.parametrize("query", [
        "What is the sum of revenue this year?",
        "Count the orders placed this week",
        "How much did we sell last month?",
    ])
    async def test_english_statistic_words_still_trigger_query(
        self, monkeypatch: Any, query: str,
    ) -> None:
        """count/sum 作为完整单词出现时必须命中普通统计。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(monkeypatch)

        # Act
        result = await classify.classify_intent_node({"user_query": query})

        # Assert
        assert result["intent"] == "query"
        assert result["task_plan"]["capability"] == "sql_analysis"


class TestBilingualHistoryRouting:
    """覆盖功能 34.2：历史追问与新统计目标的优先级区分。"""

    HISTORY = [{"user_query": "上个月销售额", "analysis_summary": "环比下降 10%"}]

    # 验证存在历史时英文结果追问进入 meta。
    # Args: self - 实例；monkeypatch - 补丁工具；query - 用户问题。
    # Returns: 无返回值。
    @pytest.mark.parametrize("query", [
        "Summarize these results",
        "What was the previous result?",
        "Analyze the results above",
    ])
    async def test_history_follow_up_uses_meta(self, monkeypatch: Any, query: str) -> None:
        """对已有结果的追问必须进入 meta 恢复链路。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(monkeypatch)

        # Act
        result = await classify.classify_intent_node(
            {"user_query": query, "conversation_history": self.HISTORY},
        )

        # Assert
        assert result["intent"] == "meta"
        assert result["task_plan"]["capability"] == "restore_previous_result"

    # 验证历史存在时新明确统计目标不被追问规则吞掉。
    # Args: self - 实例；monkeypatch - 补丁工具；query - 用户问题；expected - 期望意图。
    # Returns: 无返回值。
    @pytest.mark.parametrize("query,expected", [
        ("How many orders were placed this month?", "query"),
        ("Show the total sales for this month", "query"),
        ("Rank products by revenue this month", "aggregation"),
        ("Why did sales decline this month?", "attribution"),
    ])
    async def test_history_does_not_swallow_new_statistics(
        self, monkeypatch: Any, query: str, expected: str,
    ) -> None:
        """历史上下文不得把包含新统计目标的请求降级为 meta。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(monkeypatch)

        # Act
        result = await classify.classify_intent_node(
            {"user_query": query, "conversation_history": self.HISTORY},
        )

        # Assert
        assert result["intent"] == expected

    # 验证历史指代伴随明确新查询信号时以新统计目标为准。
    # Args: self - 实例；monkeypatch - 补丁工具；query - 用户问题。
    # Returns: 无返回值。
    @pytest.mark.parametrize("query", [
        "Ignore the previous results and count orders for June 2026.",
        "Compared with these results, run a new query for total sales in July 2026.",
        "Forget the previous results and give me the total sales for July 2026.",
        "Ignore the results above and show the average order amount this month.",
        "忽略之前的分析，重新统计 6 月的订单",
        "忽略之前的查询结果，重新查询 7 月的销售额",
    ])
    async def test_explicit_new_query_signal_beats_history_reference(
        self, monkeypatch: Any, query: str,
    ) -> None:
        """历史指代词与明确新查询信号冲突时，必须执行新统计而非回放旧结果。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(monkeypatch)

        # Act
        result = await classify.classify_intent_node(
            {"user_query": query, "conversation_history": self.HISTORY},
        )

        # Assert
        assert result["intent"] == "query"
        assert result["task_plan"]["capability"] == "sql_analysis"

    # 验证仅处理已有结果的问法在历史指代下仍进入 meta。
    # Args: self - 实例；monkeypatch - 补丁工具；query - 用户问题。
    # Returns: 无返回值。
    @pytest.mark.parametrize("query", [
        "Summarize these results",
        "Summarize the previous results",
        "Count in the previous results",
        "What was the average in the previous result?",
        "总结一下这些数据",
        "这些数据的平均值是多少",
    ])
    async def test_existing_result_processing_stays_meta(
        self, monkeypatch: Any, query: str,
    ) -> None:
        """汇总、合计已有结果的请求不得被统计词改写为新 SQL。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(monkeypatch)

        # Act
        result = await classify.classify_intent_node(
            {"user_query": query, "conversation_history": self.HISTORY},
        )

        # Assert
        assert result["intent"] == "meta"
        assert result["task_plan"]["capability"] == "restore_previous_result"

    # 验证无历史时结果追问词组不再命中 meta。
    # Args: self - 实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_follow_up_phrase_without_history_stays_chat(self, monkeypatch: Any) -> None:
        """meta 仅在存在历史时生效，无历史仍走既有闲聊回退。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(monkeypatch)

        # Act
        result = await classify.classify_intent_node({"user_query": "Summarize these results"})

        # Assert
        assert result["intent"] == "chat"

    # 验证未知英文请求沿用既有模型/闲聊回退分支。
    # Args: self - 实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_unknown_english_fallback_paths(self, monkeypatch: Any) -> None:
        """未知英文问题无历史落 chat、有历史落 meta，均不进入 SQL。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(monkeypatch)
        query = "Explain the idea of entropy"

        # Act
        without_history = await classify.classify_intent_node({"user_query": query})
        with_history = await classify.classify_intent_node(
            {"user_query": query, "conversation_history": self.HISTORY},
        )

        # Assert
        assert without_history["intent"] == "chat"
        assert with_history["intent"] == "meta"


class TestExistingCapabilityPriority:
    """覆盖功能 34.2：文件、预测等既有能力优先级不变。"""

    # 验证文件上下文优先于英文统计规则。
    # Args: self - 实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_file_ids_take_priority_over_english_statistics(self, monkeypatch: Any) -> None:
        """存在文件 ID 时英文统计问题仍进入文件分析。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(monkeypatch)
        state = {"user_query": "Show the total sales for this month", "file_ids": ["file-1"]}

        # Act
        result = await classify.classify_intent_node(state)

        # Assert
        assert result["intent"] == "file_analysis"
        assert result["task_plan"]["capability"] == "file_analysis"

    # 验证中英文预测请求的能力路由保持一致。
    # Args: self - 实例；monkeypatch - 补丁工具；query - 用户问题。
    # Returns: 无返回值。
    @pytest.mark.parametrize("query", [
        "Forecast the total sales for next quarter",
        "预测一下销售额",
    ])
    async def test_forecast_capability_priority_preserved(
        self, monkeypatch: Any, query: str,
    ) -> None:
        """含预测请求时能力保持 forecast，意图与中文一致为 query。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(monkeypatch)

        # Act
        result = await classify.classify_intent_node({"user_query": query})

        # Assert
        assert result["intent"] == "query"
        assert result["task_plan"]["capability"] == "forecast"


class TestModelBoundaryFallback:
    """覆盖功能 34.1：模型错分兜底、模型契约与取消传播。"""

    # 验证模型把英文统计问题错分为 chat 时必须兜底为 query。
    # Args: self - 实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_chat_misclassification_rescued_by_statistical_markers(
        self, monkeypatch: Any,
    ) -> None:
        """模型错分 chat 时英文统计问题必须改写为 query 并丢弃会话操作字段。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(
            monkeypatch,
            TaskPlanOutput(intent="chat", operation="conversation", confidence=0.9),
        )

        # Act
        result = await classify.classify_intent_node(
            {"user_query": "What is the amount spent by customers this month?"},
        )

        # Assert
        assert result["intent"] == "query"
        assert result["task_plan"]["operation"] == "query"

    # 验证问候语夹带统计目标时中英文同样兜底为 query。
    # Args: self - 实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_greeting_with_statistics_is_rescued(self, monkeypatch: Any) -> None:
        """Hello/你好 等问候命中闲聊规则后，统计目标仍应兜底为 query。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(monkeypatch)

        # Act
        english = await classify.classify_intent_node(
            {"user_query": "Hello, how many orders were placed?"},
        )
        chinese = await classify.classify_intent_node({"user_query": "你好，有多少订单"})

        # Assert
        assert english["intent"] == "query"
        assert chinese["intent"] == "query"

    # 验证无规则命中时模型输出契约保持原样。
    # Args: self - 实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_model_result_respected_without_rules(self, monkeypatch: Any) -> None:
        """规则未命中且无统计标记时，模型意图、操作和置信度必须原样保留。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(
            monkeypatch,
            TaskPlanOutput(intent="aggregation", operation="group_rank", confidence=0.8),
        )

        # Act
        result = await classify.classify_intent_node({"user_query": "Tell me something interesting"})

        # Assert
        assert result["intent"] == "aggregation"
        assert result["task_plan"]["operation"] == "group_rank"
        assert result["task_plan"]["confidence"] == 0.8

    # 验证模型调用被取消时异常向上传播而非降级为闲聊。
    # Args: self - 实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_model_cancellation_propagates_through_node(self, monkeypatch: Any) -> None:
        """取消属于 BaseException，必须穿透 _llm_classify 与节点边界。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        monkeypatch.setattr("src.llm.client.is_task_llm_available", lambda task: True)
        monkeypatch.setattr(
            "src.llm.invocation.invoke_structured",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )

        # Act / Assert
        with pytest.raises(asyncio.CancelledError):
            await classify.classify_intent_node({"user_query": "Explain the idea of entropy"})


class TestRoutingStatePreservation:
    """覆盖功能 34.2：Skill 激活入参与授权数据源状态保持。"""

    # 验证规则得到的意图传入 Skill 激活且原始状态字段不被改写。
    # Args: self - 实例；monkeypatch - 补丁工具；query - 用户问题；expected_intent - 期望传入意图。
    # Returns: 无返回值。
    @pytest.mark.parametrize("query,expected_intent", [
        ("How many orders were placed this month?", "query"),
        ("本月有多少订单", "query"),
        ("List available tables", "metadata"),
    ])
    async def test_skill_activation_receives_rule_intent_and_keeps_state(
        self, monkeypatch: Any, query: str, expected_intent: str,
    ) -> None:
        """Skill 激活必须收到规则意图，用户问题与历史原样传递。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        sink: list[dict[str, Any]] = []
        _patch_boundaries(monkeypatch, skill_sink=sink)
        history = [{"user_query": "旧问题", "analysis_summary": "旧结论"}]

        # Act
        result = await classify.classify_intent_node(
            {"user_query": query, "conversation_history": history},
        )

        # Assert
        assert sink and sink[0]["intent"] == expected_intent
        assert sink[0]["user_query"] == query
        assert sink[0]["conversation_history"] == history
        assert result["activated_skills"] == ["sales_skill"]
        assert result["routing_context"]["skill_candidate_ids"] == ["skill-sales"]
        assert result["routing_context"]["skill_activation_stage"] == "intent"

    # 验证 SQL 能力下授权数据源的确定性选择与权限快照保持不变。
    # Args: self - 实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_authorized_datasource_selection_preserved(self, monkeypatch: Any) -> None:
        """单授权候选必须被确定性选中并把权限快照写入状态。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(monkeypatch)
        state = {
            "user_query": "How many orders were placed this month?",
            "datasource_access": {
                "sales_db": {
                    "description": "销售数据仓库",
                    "allowed_columns": ["id", "amount"],
                    "row_filter_sql": "tenant_id = 1",
                },
            },
        }

        # Act
        result = await classify.classify_intent_node(state)

        # Assert
        assert result["intent"] == "query"
        assert result["datasource"] == "sales_db"
        assert result["selected_datasources"] == ["sales_db"]
        assert result["allowed_columns"] == ["id", "amount"]
        assert result["row_filter_sql"] == "tenant_id = 1"
        assert result["task_plan"]["datasources"] == ["sales_db"]

    # 验证直接回答能力不触发数据源选择。
    # Args: self - 实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_direct_answer_intent_skips_datasource_selection(self, monkeypatch: Any) -> None:
        """元数据等直接回答不得因授权数据源存在而进入 SQL 链路。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(monkeypatch)
        state = {
            "user_query": "List available tables",
            "datasource_access": {"sales_db": {"description": "销售库"}},
        }

        # Act
        result = await classify.classify_intent_node(state)

        # Assert
        assert result["intent"] == "metadata"
        assert result["task_plan"]["capability"] == "direct_answer"
        assert "datasource" not in result


class TestChineseRulePriorityUnchanged:
    """覆盖功能 34.1：中文既有规则优先级不因英文补齐而改变。"""

    # 验证中文既有规则在英文规则加入后保持原判定。
    # Args: self - 实例；monkeypatch - 补丁工具；query - 用户问题；expected - 期望意图。
    # Returns: 无返回值。
    @pytest.mark.parametrize("query,expected", [
        ("统计订单总数", "query"),
        ("为什么销售额下降", "attribution"),
        ("销售额趋势", "trend"),
        ("销售额排名", "aggregation"),
        ("有哪些表", "metadata"),
        ("你好", "chat"),
    ])
    async def test_chinese_rules_keep_priority(
        self, monkeypatch: Any, query: str, expected: str,
    ) -> None:
        """中文结构、趋势、归因、排名与统计的判定顺序必须保持不变。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify

        _patch_boundaries(monkeypatch)

        # Act
        result = await classify.classify_intent_node({"user_query": query})

        # Assert
        assert result["intent"] == expected


class TestIntentLogSanitization:
    """覆盖 34.1/34.2 日志审计：分类链路只记录元信息，不得携带问题或历史正文。"""

    # 验证重复分类下三条入口日志均不含用户问题与历史正文。
    # Args: self - 实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_repeated_calls_keep_question_and_history_out_of_logs(
        self, monkeypatch: Any,
    ) -> None:
        """统计兜底、模型分类入口与数据源入口重复调用后，日志仅保留长度元信息。"""
        # Arrange
        import json as json_module

        from structlog.testing import capture_logs

        import src.graph.nodes.classify_intent as classify
        import src.graph.skill_activation as activation

        monkeypatch.setattr(activation, "activate_skills", lambda state: {})
        # 保留真实 _llm_classify 以覆盖其入口日志；模型不可用走确定性回退。
        monkeypatch.setattr("src.llm.client.is_task_llm_available", lambda task: False)

        q_marker = "zqmarker7749"
        h_marker = "histmarker7749"
        history = [
            {"user_query": f"旧问题 {h_marker}", "analysis_summary": f"旧结论 {h_marker}"},
        ]
        statistical_query = f"Hello {q_marker} how many orders?"
        llm_query = f"Explain {q_marker} entropy"
        datasource_query = f"How many {q_marker} orders this month?"

        # Act
        with capture_logs() as events:
            for _round in range(2):
                statistical = await classify.classify_intent_node(
                    {"user_query": statistical_query, "conversation_history": history},
                )
                llm_path = await classify.classify_intent_node(
                    {"user_query": llm_query, "conversation_history": history},
                )
                datasource = await classify.classify_intent_node(
                    {
                        "user_query": datasource_query,
                        "datasource_access": {"sales_db": {"description": "销售库"}},
                    },
                )

        # Assert
        serialized = json_module.dumps(events, ensure_ascii=False, default=str).lower()
        assert q_marker not in serialized
        assert h_marker not in serialized
        assert statistical["intent"] == "query"
        assert llm_path["intent"] == "meta"
        assert datasource["datasource"] == "sales_db"
        expected_lengths = {
            "统计类问题意图兜底": len(statistical_query),
            "LLM 意图分类入口": len(llm_query),
            "授权候选数据源选择入口": len(datasource_query),
        }
        for event_name, expected_chars in expected_lengths.items():
            matched = [event for event in events if event.get("event") == event_name]
            assert len(matched) == 2, event_name
            for event in matched:
                assert "query" not in event, (event_name, event)
                assert event.get("query_chars") == expected_chars, (event_name, event)
