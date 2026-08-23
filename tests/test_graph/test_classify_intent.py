"""意图分类缺陷回归测试。"""

from __future__ import annotations


class TestClassifyIntentRegression:
    """覆盖 BUG-15：短统计问题不得降级为闲聊。"""

    # 方法作用：验证统计关键词命中时即使 LLM 返回 chat 也强制走 query。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言意图和任务能力。
    async def test_statistical_query_overrides_chat_model_result(self, monkeypatch) -> None:
        """简单的总数问题必须进入 SQL 分析能力。"""
        # Arrange
        import src.graph.nodes.classify_intent as module
        from src.llm.output_contracts import TaskPlanOutput

        monkeypatch.setattr(
            module,
            "_llm_classify",
            lambda *args, **kwargs: TaskPlanOutput(
                intent="chat", operation="conversation", confidence=0.99,
            ),
        )

        # Act
        result = await module.classify_intent_node({"user_query": "统计订单总数"})

        # Assert
        assert result["intent"] == "query"
        assert result["task_plan"]["capability"] == "sql_analysis"
