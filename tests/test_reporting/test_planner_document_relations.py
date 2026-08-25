"""报表工作台计划、页面 DSL 和关系证据测试。"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
class TestReportPlanner:
    """覆盖功能 23.1：自然语言计划解析与预览。"""

    # 方法作用：验证没有调度词时使用立即执行一次的安全默认值。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_plan_defaults_to_immediate_once(self) -> None:
        """无调度词的报表请求应立即执行一次且页面保留七天。"""
        # Arrange
        from src.reporting.planner import ReportPlanner

        planner = ReportPlanner()

        # Act
        plan = await planner.parse("生成销售和库存报表", datasources=["sales"])

        # Assert
        assert plan.execution_mode == "immediate"
        assert plan.max_runs == 1
        assert plan.page_ttl_days == 7
        assert len(plan.analysis_units) == 2
        assert plan.needs_confirmation is False

    # 方法作用：验证周期、次数和页面期限从中文文本中被确定性抽取。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_plan_parses_schedule_and_retention(self) -> None:
        """每周和执行次数应进入结构化计划，并要求执行确认。"""
        # Arrange
        from src.reporting.planner import ReportPlanner

        planner = ReportPlanner()

        # Act
        plan = await planner.parse(
            "每周一上午九点执行4次，报表保留30天",
            datasources=["sales"],
        )

        # Assert
        assert plan.execution_mode == "scheduled"
        assert plan.frequency == "weekly"
        assert plan.max_runs == 4
        assert plan.page_ttl_days == 30
        assert plan.needs_confirmation is True

    # 方法作用：验证缺失周期的定时请求不会被擅自转换为周期任务。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_plan_requires_confirmation_for_incomplete_schedule(self) -> None:
        """只有“以后/定时”而没有频率时应返回确认警告。"""
        # Arrange
        from src.reporting.planner import ReportPlanner

        # Act
        plan = await ReportPlanner().parse("以后定时生成销售报表", datasources=["sales"])

        # Assert
        assert plan.execution_mode == "scheduled"
        assert plan.frequency is None
        assert plan.needs_confirmation is True
        assert any("频率" in warning for warning in plan.warnings)

    # 方法作用：拒绝客户端提交的越界数据源名称和页面期限。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_plan_rejects_invalid_datasource(self) -> None:
        """数据源名称包含控制字符时必须在计划阶段失败。"""
        # Arrange
        from src.reporting.planner import ReportPlanner

        # Act / Assert
        with pytest.raises(ValueError, match="数据源"):
            await ReportPlanner().parse("生成销售报表", datasources=["sales;DROP"])


class TestReportDocument:
    """覆盖功能 23.5：动态页面 DSL 与 ECharts option 白名单。"""

    # 方法作用：允许白名单图表配置并拒绝脚本、formatter 和外链字段。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_echarts_option_whitelist(self) -> None:
        """普通 axis/series/tooltip JSON 可通过，危险函数字段必须被拒绝。"""
        # Arrange
        from src.reporting.document import validate_echarts_option

        valid = {
            "xAxis": {"type": "category", "data": ["一月", "二月"]},
            "yAxis": {"type": "value"},
            "tooltip": {"trigger": "axis"},
            "series": [{"type": "bar", "data": [1, 2]}],
        }

        # Act
        accepted = validate_echarts_option(valid)

        # Assert
        assert accepted == valid
        with pytest.raises(ValueError, match="formatter|字段|白名单"):
            validate_echarts_option({"series": [{"formatter": "() => alert(1)"}]})
        with pytest.raises(ValueError, match="白名单"):
            validate_echarts_option({"graphic": {"type": "text"}})

    # 方法作用：根据块类型生成不同列跨度，保证页面布局不是固定模板。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_build_document_creates_dynamic_layout_and_manifest(self) -> None:
        """多个分析单元应生成可引用数据、图表和可见限制说明。"""
        # Arrange
        from src.reporting.document import build_report_document
        from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult

        plan = ReportPlan(
            requested_datasources=["sales"],
            analysis_units=[
                AnalysisUnit(unit_id="sales", question="销售趋势", metrics=["销售额"]),
                AnalysisUnit(unit_id="customers", question="客户数量", metrics=["客户数"]),
            ],
        )
        results = [
            UnitResult(unit_id="sales", datasource="sales", rows=[{"month": "1", "amount": 10}]),
            UnitResult(unit_id="customers", datasource="sales", rows=[]),
        ]

        # Act
        artifact = build_report_document(plan, results)

        # Assert
        assert artifact.document.schema_version == "report.document.v1"
        assert artifact.data_manifest
        assert any(section.blocks for section in artifact.document.sections)
        assert any(block.type == "callout" for section in artifact.document.sections for block in section.blocks)


class TestReportRelations:
    """覆盖功能 23.3 基础及评审意见 1：同源关系与跨源降级。"""

    # 方法作用：对同一数据源且有共同键的分析单元生成可验证关系证据。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_same_source_relation_is_verified(self) -> None:
        """同源共同字段应标记 verified_relation 而不是伪造因果。"""
        # Arrange
        from src.reporting.relations import build_relations

        units = [
            {"unit_id": "sales", "datasource": "sales", "columns": ["customer_id", "week"]},
            {"unit_id": "retention", "datasource": "sales", "columns": ["customer_id", "week"]},
        ]

        # Act
        relations = build_relations(units)

        # Assert
        assert relations[0].evidence_level == "verified_relation"
        assert relations[0].join_keys == ["customer_id", "week"]
        assert "因果" in "".join(relations[0].limitations)

    # 方法作用：跨数据源关系只返回降级证据，避免首期越权联合查询。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_cross_source_relation_is_deferred(self) -> None:
        """跨源共同字段不能在 Phase 1 直接联合，页面应显示降级文案。"""
        # Arrange
        from src.reporting.relations import build_relations

        units = [
            {"unit_id": "sales", "datasource": "sales", "columns": ["customer_id"]},
            {"unit_id": "retention", "datasource": "crm", "columns": ["customer_id"]},
        ]

        # Act
        relations = build_relations(units)

        # Assert
        assert relations[0].evidence_level == "unsupported_cross_source"
        assert "受控分析器" in relations[0].limitations[0]
