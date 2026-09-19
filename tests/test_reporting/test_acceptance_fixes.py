"""端到端验收轮次里定位到的三处缺陷的回归契约。

对应 2026-09-15 五库验收：报表级说明单元识别、能力子图派发抑制、词面通道口径覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace

from src.reporting.models import AnalysisUnit


class TestRuntimeQualityUnitRecognition:
    """报表级说明单元必须被认出来，否则会被当成取数单元去查库并落 partial。"""

    # 方法作用：验证需求里「给出……时间覆盖……未建立可验证关系」的写法被识别。
    def test_explaining_verb_geigei_is_recognized(self) -> None:
        """实机原文以「给出」开头，旧判据只认「说明/披露」，于是被判成取数单元。"""
        unit = AnalysisUnit(
            unit_id="data_quality",
            question=(
                "给出各数据源的时间覆盖范围、被截断或被权限过滤的记录、未来情景数据的占比，"
                "以及最终未能建立可验证关系的单元清单"
            ),
        )

        from src.reporting.profiling import is_runtime_quality_unit

        assert is_runtime_quality_unit(unit) is True

    # 方法作用：验证普通取数单元不会被误判为报表级说明。
    def test_plain_data_question_is_not_quality_unit(self) -> None:
        unit = AnalysisUnit(
            unit_id="sales",
            question="按年月、地区、渠道统计订单净销售额与订单数，并给出同比与环比",
        )

        from src.reporting.profiling import is_runtime_quality_unit

        assert is_runtime_quality_unit(unit) is False

    # 方法作用：验证只沾一个元信息词的取数问题仍不被误判。
    def test_single_meta_term_is_not_enough(self) -> None:
        """「截断」出现在业务问题里很常见，单靠一个词不足以判成说明单元。"""
        unit = AnalysisUnit(unit_id="x", question="给出被截断订单的明细，按门店汇总")

        from src.reporting.profiling import is_runtime_quality_unit

        assert is_runtime_quality_unit(unit) is False


class TestCapabilitySubgraphSuppression:
    """报表单元不得被对话侧能力子图接管。"""

    # 方法作用：验证置位开关后不派发 forecast / report 子图。
    def test_suppressed_flag_routes_to_generate_chart(self, monkeypatch) -> None:
        """实机事故：质检单元只是筛选未来情景，被判成 forecast 后已取数据被覆盖成失败。"""
        # Arrange
        import src.graph.workflow as workflow

        monkeypatch.setattr(
            workflow,
            "read_contexts",
            lambda state: SimpleNamespace(
                routing=SimpleNamespace(
                    task_plan={"capability": "forecast"},
                    selected_datasources=["sqlserver"],
                    intent="aggregation",
                )
            ),
        )
        monkeypatch.setattr(
            "src.graph.nodes.supplemental_query.build_supplemental_request",
            lambda *args, **kwargs: {},
        )
        state = {"suppress_capability_subgraphs": True, "analysis_result": {}}

        # Act
        target = workflow.after_analyze_result(state)

        # Assert
        assert target == "generate_chart"

    # 方法作用：验证不置位时保留对话侧原有派发行为。
    def test_without_flag_forecast_still_dispatches(self, monkeypatch) -> None:
        import src.graph.workflow as workflow

        monkeypatch.setattr(
            workflow,
            "read_contexts",
            lambda state: SimpleNamespace(
                routing=SimpleNamespace(
                    task_plan={"capability": "forecast"},
                    selected_datasources=["ch"],
                    intent="trend",
                )
            ),
        )
        monkeypatch.setattr(
            "src.graph.nodes.supplemental_query.build_supplemental_request",
            lambda *args, **kwargs: {},
        )
        state = {"analysis_result": {}}

        assert workflow.after_analyze_result(state) == "forecast_subgraph"


class TestPlanContractTolerance:
    """计划契约不得因为一个描述性枚举越界就整体拒收。

    实机事故：模型对补充单元写 priority="supplementary"，Literal 校验把整批 16 个单元
    打回，plan 降级成规则切分的 28 个垃圾单元（unit_1 / sales / inventory）。
    """

    # 方法作用：验证 priority 同义词被归一而不是拒收。
    def test_priority_synonyms_are_normalized(self) -> None:
        from src.llm.output_contracts import ReportPlanUnit

        assert ReportPlanUnit(question="q", priority="supplementary").priority == "secondary"
        assert ReportPlanUnit(question="q", priority="main").priority == "primary"

    # 方法作用：验证未知 priority 兜底为 secondary 而不是抛错。
    def test_unknown_priority_falls_back(self) -> None:
        from src.llm.output_contracts import ReportPlanUnit

        assert ReportPlanUnit(question="q", priority="whatever").priority == "secondary"

    # 方法作用：验证合法 priority 原样保留。
    def test_valid_priority_is_kept(self) -> None:
        from src.llm.output_contracts import ReportPlanUnit

        assert ReportPlanUnit(question="q", priority="evidence").priority == "evidence"

    # 方法作用：验证时间范围 kind 越界降级为 relative 而不是拒收。
    def test_time_range_kind_falls_back(self) -> None:
        from src.llm.output_contracts import ReportPlanUnitTimeRange

        assert ReportPlanUnitTimeRange(kind="monthly").kind == "relative"
        assert ReportPlanUnitTimeRange(kind="absolute").kind == "absolute"

    # 方法作用：验证整批单元不会因单个越界值被拒。
    def test_batch_survives_one_bad_priority(self) -> None:
        from src.llm.output_contracts import ReportPlanUnitsOutput

        output = ReportPlanUnitsOutput.model_validate({
            "analysis_units": [
                {"question": "销售基盘", "priority": "primary"},
                {"question": "补充单元", "priority": "supplementary"},
            ]
        })

        assert len(output.analysis_units) == 2


class TestLexicalHintsCoverage:
    """词面通道必须覆盖总账与人力口径，否则模型看不到表就拒绝生成 SQL。"""

    # 方法作用：验证总账口径的查询能捞到凭证与试算表。
    def test_journal_vocabulary_hits_gl_tables(self) -> None:
        from src.knowledge.schema_manager import SchemaManager

        tables = [
            SimpleNamespace(name="finance_journals"),
            SimpleNamespace(name="finance_journal_lines"),
            SimpleNamespace(name="finance_trial_balances"),
            SimpleNamespace(name="finance_invoices"),
        ]

        hits = SchemaManager._lexical_hit_tables("核对总账凭证的借贷差额是否为零，按会计期给出不平凭证清单", tables)

        assert {"finance_journals", "finance_journal_lines", "finance_trial_balances"} <= hits

    # 方法作用：验证人力口径的查询能捞到工时、薪酬与敬业度表。
    def test_workforce_vocabulary_hits_hr_tables(self) -> None:
        from src.knowledge.schema_manager import SchemaManager

        tables = [
            SimpleNamespace(name="workforce_timesheets"),
            SimpleNamespace(name="workforce_payroll"),
            SimpleNamespace(name="workforce_engagement_surveys"),
            SimpleNamespace(name="workforce_departments"),
            SimpleNamespace(name="retail_orders"),
        ]

        hits = SchemaManager._lexical_hit_tables(
            "按员工、项目和年月聚合工时与人力成本，关联所属部门与薪酬带宽，再与费用报销金额对照；"
            "给出敬业度调查的分布",
            tables,
        )

        assert {"workforce_timesheets", "workforce_payroll", "workforce_engagement_surveys"} <= hits
        assert "retail_orders" not in hits
