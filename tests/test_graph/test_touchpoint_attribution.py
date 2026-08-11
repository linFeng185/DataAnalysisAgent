"""触点归因模型选择、资格补查和分析合并测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError


def _touchpoint_rows() -> list[dict]:
    """构造两条完整已转化路径。"""
    return [
        {"path_id": "p1", "touchpoint": "搜索", "touch_order": 1,
         "touch_time": "2026-01-01T00:00:00Z", "is_conversion": 0,
         "conversion_value": None},
        {"path_id": "p1", "touchpoint": "短信", "touch_order": 2,
         "touch_time": "2026-01-02T00:00:00Z", "is_conversion": 1,
         "conversion_value": 100},
        {"path_id": "p2", "touchpoint": "内容", "touch_order": 1,
         "touch_time": "2026-01-03T00:00:00Z", "is_conversion": 0,
         "conversion_value": None},
        {"path_id": "p2", "touchpoint": "直播", "touch_order": 2,
         "touch_time": "2026-01-04T00:00:00Z", "is_conversion": 1,
         "conversion_value": 200},
    ]


def _schema() -> list[dict]:
    """构造具备全部路径字段的授权 Schema。"""
    return [{
        "name": "conversion_journey",
        "columns": [
            {"name": "path_id"},
            {"name": "touchpoint"},
            {"name": "touch_order"},
            {"name": "touch_time"},
            {"name": "is_conversion"},
            {"name": "conversion_value"},
        ],
    }]


class TestTouchpointAttributionSelection:
    """覆盖功能 4.8.12 的模型优先级与受约束推荐。"""

    # 方法作用：验证用户明确指定模型时不调用推荐模型。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_explicit_model_has_priority_over_llm(self, monkeypatch) -> None:
        """末次互动的明确要求必须直接进入规则复核。"""
        # Arrange
        import src.graph.attribution as attribution_module

        availability = MagicMock(side_effect=AssertionError("不应调用推荐模型"))
        monkeypatch.setattr(attribution_module, "is_task_llm_available", availability)
        rows = _touchpoint_rows()

        # Act
        resolution = await attribution_module.resolve_touchpoint_attribution(
            {
                "query_result_sample": rows,
                "attribution_candidate_rows": rows,
                "query_result_full_count": len(rows),
            },
            user_query="请用末次互动模型分析渠道归因",
            relevant_tables=_schema(),
        )

        # Assert
        assert resolution is not None and resolution.applied is True
        attribution = resolution.analysis["attribution"]
        assert attribution["selected_model"] == "last_touch"
        assert attribution["selection_source"] == "user_explicit"
        availability.assert_not_called()

    # 方法作用：验证未指定模型时只能消费结构化枚举推荐。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unspecified_model_uses_structured_llm_recommendation(self, monkeypatch) -> None:
        """模型推荐 U 型后仍必须通过确定性字段和样本复核。"""
        # Arrange
        import src.graph.attribution as attribution_module
        import src.llm.invocation as invocation_module

        invoke = AsyncMock(return_value=SimpleNamespace(
            model="position_based",
            reason="同时重视首次获客和最终转化",
        ))
        monkeypatch.setattr(attribution_module, "is_task_llm_available", lambda task: True)
        monkeypatch.setattr(invocation_module, "invoke_structured", invoke)
        rows = _touchpoint_rows()

        # Act
        resolution = await attribution_module.resolve_touchpoint_attribution(
            {
                "query_result_sample": rows,
                "attribution_candidate_rows": rows,
                "query_result_full_count": len(rows),
            },
            user_query="请为渠道转化选择合适的归因模型",
            relevant_tables=_schema(),
        )

        # Assert
        assert resolution is not None and resolution.applied is True
        attribution = resolution.analysis["attribution"]
        assert attribution["selected_model"] == "position_based"
        assert attribution["selection_source"] == "llm_recommended"
        assert attribution["selection_reason"] == "同时重视首次获客和最终转化"
        assert invoke.await_count == 1

    # 方法作用：验证推荐模型不可用时使用透明的线性规则默认值。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unavailable_llm_uses_visible_linear_default(self, monkeypatch) -> None:
        """无模型环境不能伪装成大模型推荐。"""
        # Arrange
        import src.graph.attribution as attribution_module

        monkeypatch.setattr(attribution_module, "is_task_llm_available", lambda task: False)
        rows = _touchpoint_rows()

        # Act
        resolution = await attribution_module.resolve_touchpoint_attribution(
            {
                "query_result_sample": rows,
                "attribution_candidate_rows": rows,
                "query_result_full_count": len(rows),
            },
            user_query="分析渠道归因",
            relevant_tables=_schema(),
        )

        # Assert
        attribution = resolution.analysis["attribution"]
        assert attribution["selected_model"] == "linear"
        assert attribution["selection_source"] == "rule_default"
        assert "中性" in attribution["selection_reason"]


class TestTouchpointAttributionEvidence:
    """覆盖功能 4.8.12 的资格阻断、自主补查和重新分析。"""

    # 方法作用：验证字段不足但授权 Schema 可补齐时生成路径级补查问题。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_missing_path_fields_requests_supplemental_query(self, monkeypatch) -> None:
        """聚合渠道数据不能直接用于时间衰减，应先补查路径明细。"""
        # Arrange
        import src.graph.attribution as attribution_module

        monkeypatch.setattr(attribution_module, "is_task_llm_available", lambda task: False)

        # Act
        resolution = await attribution_module.resolve_touchpoint_attribution(
            {
                "query_result_sample": [{"channel": "搜索", "conversions": 20}],
                "query_result_full_count": 1,
                "supplemental_evidence": [],
                "supplemental_query_round": 0,
            },
            user_query="使用时间衰减模型分析渠道归因",
            relevant_tables=_schema(),
        )

        # Assert
        assert resolution is not None and resolution.applied is False
        analysis = resolution.analysis
        assert analysis["evidence_status"] == "supplementable"
        assert len(analysis["supplemental_questions"]) == 1
        assert "每行一个触点" in analysis["supplemental_questions"][0]
        assert analysis["attribution"]["status"] == "insufficient"

    # 方法作用：验证重新分析会选择成功补查的完整路径数据执行算法。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reanalysis_consumes_complete_supplemental_paths(self, monkeypatch) -> None:
        """补查结果不能只作为模型文本上下文，必须进入确定性引擎。"""
        # Arrange
        import src.graph.attribution as attribution_module

        monkeypatch.setattr(attribution_module, "is_task_llm_available", lambda task: False)
        rows = _touchpoint_rows()

        # Act
        resolution = await attribution_module.resolve_touchpoint_attribution(
            {
                "query_result_sample": [{"channel": "搜索", "conversions": 20}],
                "query_result_full_count": 1,
                "supplemental_query_round": 1,
                "supplemental_evidence": [{
                    "question": "查询完整路径",
                    "success": True,
                    "row_count": len(rows),
                    "complete": True,
                    "data": rows,
                }],
            },
            user_query="使用首次互动模型分析渠道归因",
            relevant_tables=_schema(),
        )

        # Assert
        assert resolution is not None and resolution.applied is True
        attribution = resolution.analysis["attribution"]
        assert attribution["dataset_source"] == "supplemental"
        assert attribution["selected_model"] == "first_touch"
        assert len(attribution["results"]) == 2

    # 方法作用：验证自主补查前后的归因模型选择保持不变。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reanalysis_reuses_persisted_model_selection(self, monkeypatch) -> None:
        """补查只补数据证据，不能在第二轮重新选择另一种归因口径。"""
        # Arrange
        import src.graph.attribution as attribution_module
        import src.llm.invocation as invocation_module

        first_recommendation = AsyncMock(return_value=SimpleNamespace(
            model="time_decay",
            reason="业务目标更重视临近转化互动",
        ))
        monkeypatch.setattr(attribution_module, "is_task_llm_available", lambda task: True)
        monkeypatch.setattr(invocation_module, "invoke_structured", first_recommendation)
        first = await attribution_module.resolve_touchpoint_attribution(
            {
                "query_result_sample": [{"channel": "搜索", "conversions": 20}],
                "query_result_full_count": 1,
            },
            user_query="为渠道转化选择合适的归因模型",
            relevant_tables=_schema(),
        )
        second_recommendation = AsyncMock(
            side_effect=AssertionError("补查后不应重新选择模型"),
        )
        monkeypatch.setattr(invocation_module, "invoke_structured", second_recommendation)
        rows = _touchpoint_rows()

        # Act
        second = await attribution_module.resolve_touchpoint_attribution(
            {
                "query_result_sample": [{"channel": "搜索", "conversions": 20}],
                "query_result_full_count": 1,
                "attribution_selection": first.selection,
                "supplemental_query_round": 1,
                "supplemental_evidence": [{
                    "question": first.analysis["supplemental_questions"][0],
                    "success": True,
                    "row_count": len(rows),
                    "complete": True,
                    "data": rows,
                }],
            },
            user_query="为渠道转化选择合适的归因模型",
            relevant_tables=_schema(),
        )

        # Assert
        assert second is not None and second.applied is True
        assert second.analysis["attribution"]["selected_model"] == "time_decay"
        assert second.analysis["attribution"]["selection_source"] == "llm_recommended"
        first_recommendation.assert_awaited_once()
        second_recommendation.assert_not_awaited()

    # 方法作用：验证局部路径样本不会被用于全量归因。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_incomplete_candidate_rows_require_user_narrowing(self, monkeypatch) -> None:
        """触点行超过请求级上限时必须要求缩小范围。"""
        # Arrange
        import src.graph.attribution as attribution_module

        monkeypatch.setattr(attribution_module, "is_task_llm_available", lambda task: False)
        rows = _touchpoint_rows()

        # Act
        resolution = await attribution_module.resolve_touchpoint_attribution(
            {
                "query_result_sample": rows,
                "attribution_candidate_rows": rows,
                "query_result_full_count": 100,
            },
            user_query="使用线性归因分析渠道",
            relevant_tables=_schema(),
        )

        # Assert
        assert resolution is not None and resolution.applied is False
        assert resolution.analysis["evidence_status"] == "needs_user_input"
        assert any(
            "仅获得 4/100 行" in gap
            for gap in resolution.analysis["evidence_gaps"]
        )


class TestTouchpointAttributionAnalysisNode:
    """验证触点归因与现有分析节点的集成边界。"""

    # 方法作用：验证无模型环境仍返回完整确定性归因结构。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_analysis_node_returns_deterministic_attribution(self, monkeypatch) -> None:
        """触点归因数值不依赖真实模型调用。"""
        # Arrange
        import src.graph.nodes.analyze_result as analyze_module

        monkeypatch.setattr(analyze_module, "_is_task_llm_available", lambda task: False)
        rows = _touchpoint_rows()

        # Act
        result = await analyze_module.analyze_result_node({
            "user_query": "使用末次互动模型分析渠道归因",
            "intent": "attribution",
            "generated_sql": "SELECT path_id, touchpoint FROM conversion_journey",
            "query_result_sample": rows,
            "attribution_candidate_rows": rows,
            "query_result_full_count": len(rows),
            "relevant_tables": _schema(),
        })

        # Assert
        analysis = result["analysis_result"]
        assert analysis["processor_name"] == "touchpoint_attribution"
        assert analysis["attribution"]["status"] == "applied"
        assert analysis["attribution"]["selected_model"] == "last_touch"
        assert sum(
            item["attribution_pct"] for item in analysis["attribution"]["results"]
        ) == 100

    # 方法作用：验证解释模型不能覆盖确定性摘要、洞察和归因明细。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_llm_only_adds_business_interpretation(self, monkeypatch) -> None:
        """即使解释模型返回冲突数字，公开归因数字仍以规则引擎为准。"""
        # Arrange
        import src.graph.nodes.analyze_result as analyze_module

        monkeypatch.setattr(analyze_module, "_is_task_llm_available", lambda task: True)
        llm_analysis = AsyncMock(return_value={
            "summary": "错误地声称归因总额为九百",
            "insights": ["错误占比"],
            "data_quality": ["模型补充质量说明"],
            "limitations": [],
            "confidence": "high",
            "related_metrics": ["建议联查获客成本"],
            "business_impacts": ["可能影响渠道预算"],
            "possible_causes": ["待验证：渠道结构变化"],
            "recommended_actions": ["按周监控渠道归因"],
            "evidence_status": "sufficient",
            "evidence_gaps": [],
            "supplemental_questions": [],
            "recommended_chart_type": "pie",
            "follow_up_questions": [],
        })
        monkeypatch.setattr(analyze_module, "_llm_analyze", llm_analysis)
        rows = _touchpoint_rows()

        # Act
        result = await analyze_module.analyze_result_node({
            "user_query": "使用首次互动模型分析渠道归因",
            "intent": "attribution",
            "query_result_sample": rows,
            "attribution_candidate_rows": rows,
            "query_result_full_count": len(rows),
            "relevant_tables": _schema(),
        })

        # Assert
        analysis = result["analysis_result"]
        assert "九百" not in analysis["summary"]
        assert "错误占比" not in analysis["insights"]
        assert analysis["attribution"]["total_attributed_value"] == 300
        assert analysis["business_impacts"] == ["可能影响渠道预算"]
        assert analysis["recommended_chart_type"] == "bar"


class TestTouchpointAttributionContracts:
    """验证触点归因的结构化模型与提示词白名单。"""

    # 方法作用：验证推荐输出拒绝五种模型之外的自由文本。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_model_recommendation_contract_rejects_unknown_model(self) -> None:
        """大模型不能新增沙普利值或其他尚未实现的模型。"""
        # Arrange
        from src.llm.output_contracts import AttributionModelRecommendationOutput

        # Act / Assert
        with pytest.raises(ValidationError):
            AttributionModelRecommendationOutput(
                model="shapley",  # type: ignore[arg-type]
                reason="自由选择",
            )

    # 方法作用：验证归因推荐和路径 SQL 要求已进入版本化提示词注册表。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_attribution_prompts_are_registered_and_grounded(self) -> None:
        """选择模型和生成路径数据都必须受集中提示词契约约束。"""
        # Arrange
        from src.llm.prompts import get_prompt_definition

        # Act
        selector = get_prompt_definition("attribution.select")
        sql_prompt = get_prompt_definition("sql.generate").template

        # Assert
        assert selector.output_model.__name__ == "AttributionModelRecommendationOutput"
        assert "first_touch" in selector.template
        assert "position_based" in selector.template
        assert "不得声称" in selector.template
        assert "path_id" in sql_prompt
        assert "is_conversion" in sql_prompt
        assert "禁止在 SQL 中预先分配归因权重" in sql_prompt
