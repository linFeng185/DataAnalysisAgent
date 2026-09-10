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

    # 方法作用：验证结构化模型补充不能扩大计划阶段已授权的数据源集合。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_structured_llm_cannot_expand_datasource_scope(self) -> None:
        """模型返回未授权数据源时应回退到规则计划，而不是改变执行范围。"""
        # Arrange
        from src.reporting.planner import ReportPlanner

        class FakePlanner:
            # 方法作用：返回一个尝试绑定未授权数据源的结构化计划片段。
            # Args: prompt - 规则解析后的用户文本。
            # Returns: 含越权数据源的分析单元列表。
            async def ainvoke(self, prompt: str) -> dict[str, object]:
                return {"analysis_units": [{"unit_id": "sales", "question": prompt, "datasource": "crm"}]}

        # Act
        plan = await ReportPlanner(FakePlanner()).parse("生成销售报表", datasources=["sales"])

        # Assert
        assert plan.requested_datasources == ["sales"]
        assert all(unit.datasource in {"", "sales"} for unit in plan.analysis_units)

    # 方法作用：验证用户书写的编号章节被识别为分析单元边界。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_numbered_sections_become_units(self, monkeypatch) -> None:
        """编号章节应逐个成为分析单元，章节内的并列词不再被切碎。"""
        # Arrange
        import src.llm.client as llm_client
        from src.reporting.planner import ReportPlanner

        # 规则路径是模型不可用时的唯一回退，必须显式关闭模型才能确定性验证。
        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)
        prompt = (
            "立即生成一份《2026 年经营全景报表》，不要定时，页面保留 7 天：\n"
            "1. 销售：按年月、地区、渠道统计订单净销售额、订单数、客单价；\n"
            "2. 财务：核对订单净额与发票金额的差额，统计退款规模；\n"
            "3. 履约：对比配送费用、采购加权单位成本与订单量的走势；\n"
            "4. 营销：广告归因收入与订单净销售额的对比；\n"
            "5. 数据质量：说明时间覆盖范围与未能建立的关系。"
        )

        # Act
        plan = await ReportPlanner().parse(prompt, datasources=["sales"])

        # Assert
        questions = [unit.question for unit in plan.analysis_units]
        assert len(questions) == 5
        assert questions[0].startswith("销售")
        assert questions[2].startswith("履约")
        assert questions[4].startswith("数据质量")
        # 章节内部的并列指标与维度不再被切成独立单元
        assert "地区" not in questions
        assert "客单价" not in questions
        assert "不要" not in questions

    # 方法作用：验证调度描述被清除且不留残片。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_schedule_noise_leaves_no_fragment(self, monkeypatch) -> None:
        """“不要定时”“页面保留 30 天”不得残留为分析单元。"""
        # Arrange
        import src.llm.client as llm_client
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)

        # Act
        plan = await ReportPlanner().parse("立即生成销售报表，不要定时，页面保留 30 天", datasources=["sales"])

        # Assert
        questions = [unit.question for unit in plan.analysis_units]
        assert not any(question in {"不要", "页面", "定时"} for question in questions)
        assert plan.page_ttl_days == 30

    # 方法作用：验证被否定的调度词不会被识别为定时意图。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_negated_schedule_stays_immediate(self, monkeypatch) -> None:
        """用户明确说“不要定时”时应按立即执行一次处理。"""
        # Arrange
        import src.llm.client as llm_client
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)

        # Act
        plan = await ReportPlanner().parse("立即生成销售报表，不要定时，页面保留 7 天", datasources=["sales"])

        # Assert
        assert plan.execution_mode == "immediate"
        assert plan.max_runs == 1
        assert plan.needs_confirmation is False
        assert not any("频率" in warning for warning in plan.warnings)


class TestReportPlannerTextRules:
    """覆盖功能 23.1：计划解析的文本规则边界。"""

    # 方法作用：验证否定前缀只认多字否定词，不误伤“类别”等常见业务词。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_negation_prefix_spares_common_words(self) -> None:
        """“类别每月”“区分每周”不得被当成被否定的调度意图。"""
        # Arrange
        from src.reporting.planner import _NEGATED_SCHEDULE_RE

        # Act / Assert：单字“别/不”若进入词表，会把“类别每月”误判为否定调度
        assert _NEGATED_SCHEDULE_RE.search("按类别每月统计") is None
        assert _NEGATED_SCHEDULE_RE.search("区分每周数据") is None
        assert _NEGATED_SCHEDULE_RE.search("不要定时") is not None


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

    # 方法作用：验证跨数据源的授权样本可以生成匹配率和基数证据。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_cross_source_controlled_relation_is_verified(self) -> None:
        """跨源共同实体和时间键匹配率达到阈值时应发布 verified_relation。"""
        # Arrange
        from src.reporting.models import UnitResult
        from src.reporting.relations import RelationPolicy, build_relations

        units = [
            {
                "unit_id": "sales",
                "datasource": "sales_db",
                "columns": ["customer_id", "week", "amount", "secret_column"],
                "dimensions": ["customer_id", "week"],
                "metrics": ["amount"],
            },
            {
                "unit_id": "retention",
                "datasource": "crm_db",
                "columns": ["customer_id", "week", "amount"],
                "dimensions": ["customer_id", "week"],
                "metrics": ["amount"],
            },
        ]
        results = [
            UnitResult(
                unit_id="sales",
                datasource="sales_db",
                columns=["customer_id", "week", "amount", "secret_column"],
                rows=[
                    {"customer_id": "c1", "week": "2026-08-01", "amount": 10, "secret_column": "hidden"},
                    {"customer_id": "c2", "week": "2026-08-01", "amount": 20, "secret_column": "hidden"},
                    {"customer_id": "c3", "week": "2026-08-01", "amount": 30, "secret_column": "hidden"},
                ],
            ),
            UnitResult(
                unit_id="retention",
                datasource="crm_db",
                columns=["customer_id", "week", "amount"],
                rows=[
                    {"customer_id": "c1", "week": "2026-08-01", "amount": 1},
                    {"customer_id": "c2", "week": "2026-08-01", "amount": 2},
                    {"customer_id": "other", "week": "2026-08-01", "amount": 3},
                ],
            ),
        ]

        # Act
        relation = build_relations(units, results, RelationPolicy(min_match_rate=0.6))[0]

        # Assert
        assert relation.evidence_level == "verified_relation"
        assert relation.join_keys == ["customer_id", "week"]
        assert relation.match_rate == pytest.approx(2 / 3)
        assert relation.cardinality == "one_to_one"
        assert relation.estimated_join_rows == 2
        assert "secret_column" not in relation.join_keys

    # 方法作用：验证低匹配率的跨源关系不会被误发布为可联合证据。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_cross_source_low_match_rate_is_blocked(self) -> None:
        """匹配率低于租户阈值时应标记 blocked 并给出降级限制。"""
        # Arrange
        from src.reporting.models import UnitResult
        from src.reporting.relations import build_relations

        units = [
            {"unit_id": "left", "datasource": "a", "columns": ["customer_id"]},
            {"unit_id": "right", "datasource": "b", "columns": ["customer_id"]},
        ]
        results = [
            UnitResult(unit_id="left", rows=[{"customer_id": "c1"}, {"customer_id": "c2"}]),
            UnitResult(unit_id="right", rows=[{"customer_id": "c1"}, {"customer_id": "c3"}]),
        ]

        # Act
        relation = build_relations(units, results)[0]

        # Assert
        assert relation.evidence_level == "blocked"
        assert relation.match_rate == pytest.approx(0.5)
        assert any("匹配率" in item for item in relation.limitations)

    # 方法作用：验证多对多关系和预计连接膨胀会被阻断。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_many_to_many_relation_is_blocked(self) -> None:
        """双方重复键会造成重复计数，不能生成 verified_relation。"""
        # Arrange
        from src.reporting.models import UnitResult
        from src.reporting.relations import RelationPolicy, build_relations

        units = [
            {"unit_id": "left", "datasource": "a", "columns": ["customer_id"]},
            {"unit_id": "right", "datasource": "b", "columns": ["customer_id"]},
        ]
        results = [
            UnitResult(unit_id="left", rows=[{"customer_id": "c1"}, {"customer_id": "c1"}]),
            UnitResult(unit_id="right", rows=[{"customer_id": "c1"}, {"customer_id": "c1"}]),
        ]

        # Act
        relation = build_relations(units, results, RelationPolicy(max_join_rows=3))[0]

        # Assert
        assert relation.evidence_level == "blocked"
        assert relation.cardinality == "many_to_many"
        assert relation.estimated_join_rows == 4
        assert any("多对多" in item or "连接行数" in item for item in relation.limitations)

    # 方法作用：验证共同字段类型不兼容时不会执行跨源联合。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_cross_source_incompatible_key_type_is_blocked(self) -> None:
        """数字 ID 与文本 ID 不能直接比较，关系应被安全阻断。"""
        # Arrange
        from src.reporting.models import UnitResult
        from src.reporting.relations import build_relations

        units = [
            {"unit_id": "left", "datasource": "a", "columns": ["customer_id"]},
            {"unit_id": "right", "datasource": "b", "columns": ["customer_id"]},
        ]
        results = [
            UnitResult(unit_id="left", rows=[{"customer_id": 1}, {"customer_id": 2}]),
            UnitResult(unit_id="right", rows=[{"customer_id": "1"}, {"customer_id": "2"}]),
        ]

        # Act
        relation = build_relations(units, results)[0]

        # Assert
        assert relation.evidence_level == "blocked"
        assert any("类型不兼容" in item for item in relation.limitations)

    # 方法作用：验证共同指标不会被误判为可连接的业务关系键。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_common_metric_is_not_join_key(self) -> None:
        """双方只有 amount 共同字段时应分别分析，不生成伪关系。"""
        # Arrange
        from src.reporting.relations import build_relations

        units = [
            {"unit_id": "left", "datasource": "sales", "columns": ["amount"], "metrics": ["amount"]},
            {"unit_id": "right", "datasource": "sales", "columns": ["amount"], "metrics": ["amount"]},
        ]

        # Act
        relation = build_relations(units)[0]

        # Assert
        assert relation.evidence_level == "no_relation"
        assert relation.join_keys == []
