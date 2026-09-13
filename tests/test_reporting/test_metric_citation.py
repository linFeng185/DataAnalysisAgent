"""报表指标口径溯源回归，覆盖 23.19。"""

from __future__ import annotations

from typing import Any


# 方法作用：构造一条旧工作流返回的指标口径来源。
# Args: code - 指标编码；version - 已批准版本号；expression - 口径表达式。
# Returns: activated_knowledge 中的单条来源字典。
def _metric_source(code: str, version: int = 3, expression: str = "SUM(order_amount)") -> dict[str, Any]:
    return {
        "source_id": f"metric:1:{code}:v{version}",
        "category": "business_rule",
        "source": "metric_catalog",
        "source_file": "成交总额",
        "trust_level": "tenant_approved_metric",
        "locator": {"metric_code": code, "version": version, "name": "成交总额", "expression": expression, "unit": "元"},
    }


class TestMetricCitationExtraction:
    """验证 23.19：执行器从既有工作流结果里提取指标口径引用。"""

    # 方法作用：验证只提取指标来源，忽略知识库来源与非法结构。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_extracts_metric_sources_only(self) -> None:
        """知识库来源不能被当成指标口径。"""
        # Arrange
        from src.reporting.runner import _extract_metric_citations

        activated = [
            _metric_source("gmv"),
            {"source_id": "kb:doc-1", "category": "doc", "locator": {"metric_code": "x"}},
            "not-a-dict",
        ]

        # Act
        citations = _extract_metric_citations(activated)

        # Assert
        assert citations == [{"code": "gmv", "name": "成交总额", "expression": "SUM(order_amount)", "unit": "元", "version": 3}]

    # 方法作用：验证同一指标被多个单元引用时按编码去重。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_deduplicates_by_metric_code(self) -> None:
        """重复引用不应在页面上重复出现。"""
        # Arrange
        from src.reporting.runner import _extract_metric_citations

        # Act
        citations = _extract_metric_citations([_metric_source("gmv"), _metric_source("gmv", version=4)])

        # Assert
        assert len(citations) == 1

    # 方法作用：验证结构不完整的来源被跳过而不是产出猜测性引用。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_incomplete_sources_are_skipped(self) -> None:
        """缺少 locator 或 metric_code 时不得产出引用。"""
        # Arrange
        from src.reporting.runner import _extract_metric_citations

        # Act / Assert
        assert _extract_metric_citations(None) == []
        assert _extract_metric_citations([]) == []
        assert _extract_metric_citations([{"source_id": "metric:1:gmv:v1"}]) == []
        assert _extract_metric_citations([{"source_id": "metric:1::v1", "locator": {}}]) == []


class TestMetricCitationSection:
    """验证 23.19：口径引用以 source_citation 块进入页面。"""

    # 方法作用：构造一个最小页面产物作为追加目标。
    # Args: self - 测试实例。
    # Returns: 可追加区段的 ReportArtifact。
    def _artifact(self):
        from src.reporting.document import build_report_document
        from src.reporting.models import AnalysisUnit, ReportPlan

        plan = ReportPlan(analysis_units=[AnalysisUnit(question="销售额")])
        return build_report_document(plan, [])

    # 方法作用：验证节点汇总各单元的引用并按编码去重。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_node_collects_and_dedupes(self) -> None:
        """节点输出应覆盖全部单元且不含重复编码。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import report_attach_metric_refs
        from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult

        state = {
            "plan": ReportPlan(analysis_units=[AnalysisUnit(question="销售额")]),
            "unit_results": [
                UnitResult(unit_id="u1", metric_citations=[{"code": "gmv", "version": 3}]),
                UnitResult(unit_id="u2", metric_citations=[{"code": "gmv", "version": 3}, {"code": "refund", "version": 1}]),
            ],
        }

        # Act
        result = await report_attach_metric_refs(state)

        # Assert
        assert sorted(item["code"] for item in result["metric_citations"]) == ["gmv", "refund"]

    # 方法作用：验证保守档不产出口径溯源。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_conservative_divergence_skips_citations(self) -> None:
        """保守档保持完全不加料的语义。

        divergence 字段由 23.17 引入，这里用替身对象验证闸门本身。
        """
        # Arrange
        from src.graph.subgraphs.report_workbench import report_attach_metric_refs
        from src.reporting.models import UnitResult

        class _ConservativePlan:
            divergence = "conservative"

        state = {
            "plan": _ConservativePlan(),
            "unit_results": [UnitResult(unit_id="u1", metric_citations=[{"code": "gmv", "version": 3}])],
        }

        # Act
        result = await report_attach_metric_refs(state)

        # Assert
        assert result["metric_citations"] == []

    # 方法作用：验证区段带上可读摘要与结构化条目。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_section_appended_with_summary_and_items(self) -> None:
        """页面需要同时给出可读摘要和结构化口径，便于前端展示。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import _append_metric_citations

        artifact = self._artifact()

        # Act
        _append_metric_citations(artifact, [{"code": "gmv", "name": "成交总额", "version": 3, "expression": "SUM(x)"}])

        # Assert
        sections = [item for item in artifact.document.sections if item.id == "metric_refs"]
        assert len(sections) == 1
        block = sections[0].blocks[0]
        assert block.type == "source_citation"
        assert "成交总额" in block.content["summary"]
        assert block.content["items"][0]["code"] == "gmv"

    # 方法作用：验证没有引用时不产生空区段。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_no_citations_means_no_section(self) -> None:
        """无口径引用时不得留下空壳区段。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import _append_metric_citations

        artifact = self._artifact()

        # Act
        _append_metric_citations(artifact, [])

        # Assert
        assert not [item for item in artifact.document.sections if item.id == "metric_refs"]

    # 方法作用：验证页面校验拒绝该区段时降级为不展示而不是整份报表失败。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rejected_section_degrades_without_breaking_document(self) -> None:
        """装饰性来源块无权让整份报表构建失败。

        指标名进入顶层 summary 后会被页面块的脚本特征校验拦下。
        """
        # Arrange
        from src.graph.subgraphs.report_workbench import _append_metric_citations

        artifact = self._artifact()
        before = len(artifact.document.sections)

        # Act
        _append_metric_citations(artifact, [{"code": "gmv", "name": "=> 恶意名", "version": 1}])

        # Assert
        assert len(artifact.document.sections) == before
        assert not [item for item in artifact.document.sections if item.id == "metric_refs"]
