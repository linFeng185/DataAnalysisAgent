"""异构多源结果经过统计、模型上下文和报表页面的证据完整性回归。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from src.graph.nodes.analyze_result import _llm_analyze, _to_compact
from src.reporting.document import build_report_document
from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult
from src.tools.analyzer import compute_statistics


class TestMultisourceEvidence:
    """覆盖 23.3 与 23.5 的多来源异构字段传递。"""

    # 方法作用：验证后续来源独有指标进入列契约、页面和钻取且缺失值不补零。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_sparse_fields_survive_document_roundtrip(self) -> None:
        """首源没有的广告归因收入也必须显示，持久化后列集合保持一致。"""
        # Arrange
        rows = [{"_datasource": "oracle", "net_sales": 12}, {"_datasource": "ch", "attributed_revenue": 18}]
        result = UnitResult(unit_id="marketing", rows=rows)

        # Act
        restored = UnitResult.model_validate_json(result.model_dump_json())
        artifact = build_report_document(ReportPlan(analysis_units=[AnalysisUnit(unit_id="marketing", question="广告归因对比")]), [restored])
        table = next(block for section in artifact.document.sections for block in section.blocks if block.type == "table")

        # Assert
        assert restored.columns == ["_datasource", "net_sales", "attributed_revenue"]
        assert table.content["columns"] == restored.columns
        assert "attributed_revenue" not in table.content["rows"][0]
        assert artifact.document.drilldowns[0]["columns"] == restored.columns

    # 方法作用：验证数值统计不漏掉第二来源的指标，同时排除混合类型和布尔值。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_statistics_include_sparse_numeric_fields(self) -> None:
        """广告归因金额列必须独立统计，不把首源没有该列当业务零值。"""
        # Arrange
        rows = [{"sales": 12, "region": "CN-BJ"}, {"attributed_revenue": 18, "region": 1, "flag": True}, {"attributed_revenue": None}]

        # Act
        stats = compute_statistics(rows)

        # Assert
        assert stats["numeric_columns"] == ["sales", "attributed_revenue"]
        assert stats["columns"]["attributed_revenue"]["mean"] == 18

    # 方法作用：验证有界样本开头覆盖每个来源，而不是连续输出首源所有行。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_compact_sample_interleaves_sources(self) -> None:
        """字符预算裁剪前优先让各来源至少有一行证据。"""
        # Arrange
        rows = [{"_datasource": "oracle", "sales": index} for index in range(30)] + [{"_datasource": "ch", "attributed_revenue": index} for index in range(30)]

        # Act
        sample = json.loads(_to_compact(rows))

        # Assert
        assert [row["_datasource"] for row in sample[:2]] == ["oracle", "ch"]
        assert sorted(row["sales"] for row in sample if "sales" in row) == list(range(30))
        assert _to_compact([]) == "[]"
        assert json.loads(_to_compact([{"sales": 1}, {"sales": 2}])) == [{"sales": 1}, {"sales": 2}]

    # 方法作用：验证真实预算器生成的模型消息保留第二来源字段和至少一行值。
    # Args: self - 测试实例；monkeypatch - 测试补丁工具。
    # Returns: 无返回值。
    async def test_budgeted_analysis_receives_each_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """单测仅使用 Fake 模型，穿过正式 Prompt 预算和解析边界。"""
        # Arrange
        model = SimpleNamespace(ainvoke=AsyncMock())
        model.ainvoke.return_value = AIMessage(content='{"summary":"有来源证据","insights":[]}')
        monkeypatch.setattr("src.graph.nodes.analyze_result._get_analysis_llm", lambda **kwargs: model)
        rows = [{"_datasource": "oracle", "year_month": "2026-01", "region_code": "CN-BJ", "net_sales": index, "detail": "x" * 80} for index in range(30)] + [{"_datasource": "ch", "attributed_revenue": 9876, "year_month": "2026-01"} for index in range(30)]

        # Act
        await _llm_analyze(rows, "SELECT net_sales FROM orders", compute_statistics(rows), {}, {}, {}, user_query="广告归因收入与订单净额对比", result_truncated=True)

        # Assert
        messages = model.ainvoke.await_args.args[0]
        content = "\n".join(str(message.content) for message in messages)
        assert "attributed_revenue" in content
        assert "9876" in content

    # 方法作用：验证被模型字符预算裁掉的后期日期仍由确定性边界交给模型。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_prompt_preserves_actual_sample_time_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """全年样本不能因为只看头部几行被误报成仅一月。"""
        # Arrange
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content='{"summary":"全年样本"}')))
        monkeypatch.setattr("src.graph.nodes.analyze_result._get_analysis_llm", lambda **kwargs: model)
        rows = [{"_datasource": "oracle", "year_month": f"2026-{month:02d}", "amount": month, "detail": "x" * 400} for month in range(1, 13)]

        # Act
        await _llm_analyze(rows, "SELECT amount FROM orders", compute_statistics(rows), {}, {}, {}, user_query="全年销售", result_truncated=True)

        # Assert
        content = "\n".join(str(message.content) for message in model.ainvoke.await_args.args[0])
        assert "year_month=2026-01 至 2026-12" in content
        assert "不代表数据库完整范围" in content
