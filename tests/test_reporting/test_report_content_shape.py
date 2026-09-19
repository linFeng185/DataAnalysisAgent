"""报表内容构成契约：只呈现图表与分析，不承担明细。

2026-09-14 实机反馈（分享页）：
* 19 个单元的「明细样本」表格块 —— 5~18 列 × 最多 100 行，单元格被 `ellipsis` 挤成
  "每格几个字加省略号"，用户的原话是"列表丑死了，可读性也差"；明细本应由使用方
  自己去数据源看，报表只需结论。
* 22/24 个区段标题就是英文 unit_id（`sales_base_overview`、`three_way_reconciliation`）。
* relations 区段 20 块里多数是"未发现共同字段"的 `no_relation`，占版面不提供信息。
"""

from __future__ import annotations

from typing import Any

from src.reporting.document import build_report_document
from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult
from src.reporting.relations import RelationEvidence

_QUESTION = "销售基盘：按年月、地区、渠道以及历史模拟/当年/未来情景分组，统计订单净销售额与订单数"


# 方法作用：构造含指定单元问题的计划。
# Args: questions - 单元问题文本；titles - 可选的单元标题列表，按位置对应问题，缺省全为空。
# Returns: 已校验的报表计划。
def _plan(*questions: str, titles: list[str] | None = None) -> ReportPlan:
    title_list = list(titles or [])
    return ReportPlan(
        requested_datasources=["sales"],
        analysis_units=[
            AnalysisUnit(unit_id=f"unit_{index}", question=text, title=title_list[index - 1] if index <= len(title_list) else "")
            for index, text in enumerate(questions, start=1)
        ],
    )


# 方法作用：构造带数据行的单元结果。
# Args: unit_id - 单元标识；rows - 结果行。
# Returns: 单元结果。
def _result(unit_id: str, rows: list[dict[str, Any]] | None = None) -> UnitResult:
    return UnitResult(
        unit_id=unit_id,
        datasource="sales",
        rows=rows if rows is not None else [{"year_month": "2026-06", "net_sales": 200.0}],
        columns=["year_month", "net_sales"],
    )


# 方法作用：收集整份页面里的块类型。
# Args: artifact - 页面产物。
# Returns: 块类型列表。
def _block_types(artifact) -> list[str]:
    return [str(block.type) for section in artifact.document.sections for block in section.blocks]


class TestReportContentShape:
    """覆盖报表页面的内容构成。"""

    # 方法作用：验证报表不再产出明细表格块。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_no_detail_table_blocks(self) -> None:
        """明细表既难读又不该由报表承担；明细走数据源或既有钻取。"""
        # Arrange / Act
        artifact = build_report_document(_plan(_QUESTION), [_result("unit_1")])

        # Assert
        assert "table" not in _block_types(artifact)
        # 图表与分析仍在：去掉明细不等于去掉内容
        types = _block_types(artifact)
        assert "chart" in types and "kpi" in types

    # 方法作用：验证文档层防御性裁剪分析结果时向页面披露被省略数量。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_result_limit_trimming_is_disclosed(self, monkeypatch) -> None:
        """配置在计划生成后收紧时，超出上限的区段不能无声消失。"""
        # Arrange
        import src.reporting.document as document_module

        plan = _plan("销售", "库存", "客户")
        results = [_result("unit_1"), _result("unit_2"), _result("unit_3")]
        monkeypatch.setattr(document_module, "max_plan_units", lambda: 2)

        # Act
        artifact = build_report_document(plan, results)

        # Assert
        assert {section.id for section in artifact.document.sections} >= {"unit_1", "unit_2"}
        assert "unit_3" not in {section.id for section in artifact.document.sections}
        assert any("3" in item and "2" in item and "分析单元" in item for item in artifact.document.limitations)

    # 方法作用：验证区段标题使用业务短标题而不是英文标识。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_section_title_is_business_text(self) -> None:
        """标题曾等于 unit_id，页面上整片英文。"""
        # Arrange / Act
        artifact = build_report_document(_plan(_QUESTION), [_result("unit_1")])

        # Assert
        section = next(item for item in artifact.document.sections if item.id == "unit_1")
        assert section.title == "销售基盘"
        assert section.title != "unit_1"

    # 方法作用：验证没有业务前缀的问题回退为截断文本而不是标识。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_title_falls_back_to_truncated_question(self) -> None:
        """问题没有「小标题：」结构时也不能拿 unit_id 充当标题。"""
        # Arrange / Act
        artifact = build_report_document(_plan("按门店和年月对比销售目标与实际净销售额并给出达成率分布"), [_result("unit_1")])

        # Assert
        section = next(item for item in artifact.document.sections if item.id == "unit_1")
        assert section.title.endswith("…")
        assert section.title != "unit_1"
        assert len(section.title) <= 21

    # 方法作用：验证模型给出的精简标题直接成为区段标题。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_section_title_uses_model_title(self) -> None:
        """标题由大模型生成后，页面标题必须等于它，而不是问题原文的硬截断。"""
        # Arrange
        plan = _plan(_QUESTION, titles=["销售基盘与情景对比"])

        # Act
        artifact = build_report_document(plan, [_result("unit_1")])

        # Assert
        section = next(item for item in artifact.document.sections if item.id == "unit_1")
        assert section.title == "销售基盘与情景对比"
        # 硬截断的痕迹（省略号）与模型标题并存说明有别的分支覆盖了 title
        assert not section.title.endswith("…")
        assert section.title != _QUESTION[:20]

    # 方法作用：验证模型没给标题时区段标题仍走原有回退逻辑。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_section_title_falls_back_without_model_title(self) -> None:
        """模型未给标题（发散单元或模型降级）时不能报错，也不能产出空标题。"""
        # Arrange / Act
        artifact = build_report_document(
            _plan(_QUESTION, "按门店和年月对比销售目标与实际净销售额并给出达成率分布"),
            [_result("unit_1"), _result("unit_2")],
        )

        # Assert
        titles = {section.id: section.title for section in artifact.document.sections}
        assert titles["unit_1"] == "销售基盘"       # 冒号前业务名仍生效
        assert titles["unit_2"].endswith("…")       # 无冒号结构仍按截断回退
        assert all(titles.values())                 # 任何单元都不出现空标题

    # 方法作用：验证模型标题经 _coerce_llm_units 透传，且异常长度不会拖垮整个单元。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_llm_unit_title_is_passed_through(self) -> None:
        """模型标题是单元的一部分；超长标题必须收窄，不能连问题一起被契约拒收。"""
        # Arrange
        from src.reporting.planner import ReportPlanner

        raw_units = [
            {"unit_id": "sales", "question": "销售基盘分析", "title": "销售基盘"},
            {"unit_id": "risk", "question": "风险敞口分析", "title": "溢" * 200},
            {"unit_id": "plain", "question": "无标题单元"},
        ]

        # Act
        units = ReportPlanner._coerce_llm_units(raw_units)

        # Assert
        assert len(units) == 3, "过长标题不得导致单元被整体拒收"
        assert units[0].title == "销售基盘"
        assert units[1].title and len(units[1].title) <= 20
        assert units[2].title == ""

    # 方法作用：验证契约层把模型标题收敛到 12 字，且越界只截断不拒收。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_plan_unit_contract_caps_title_length(self) -> None:
        """标题进契约才拦得住；但展示字段越界不能拖垮整批单元。

        历史教训：把上限做成硬拒收时，模型多写一个字就会让 parse_json_model
        整批校验失败，整个计划降级成规则拆分的标题残渣。
        """
        # Arrange
        from src.llm.output_contracts import ReportPlanUnit

        # Act / Assert
        assert ReportPlanUnit(question="销售分析", title="销售基盘").title == "销售基盘"
        assert ReportPlanUnit(question="销售分析").title == ""
        assert ReportPlanUnit(question="销售分析", title="超" * 13).title == "超" * 12

    # 方法作用：验证模型标题从拆解输出一路走到页面区段标题。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_model_title_reaches_section_title_end_to_end(self) -> None:
        """标题必须并入既有拆解调用；这一条锁住"模型输出→计划→页面"的完整链路。"""
        # Arrange
        from src.reporting.planner import ReportPlanner

        class FakePlanner:
            # 方法作用：返回带标题的分析单元建议。
            # Args: prompt - 规则解析传入的用户文本。
            # Returns: 含一个带 title 单元的模型输出字典。
            async def ainvoke(self, prompt: str) -> dict[str, object]:
                return {"analysis_units": [{"unit_id": "sales", "question": _QUESTION, "title": "销售基盘"}]}

        # Act
        plan = await ReportPlanner(FakePlanner()).parse("生成销售报表", datasources=["sales"])
        artifact = build_report_document(plan, [_result("sales")])

        # Assert
        assert plan.analysis_units[0].title == "销售基盘"
        section = next(item for item in artifact.document.sections if item.id == "sales")
        assert section.title == "销售基盘"

    # 方法作用：验证 report.plan 提示词包含标题生成约束。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_plan_prompt_asks_for_unit_title(self) -> None:
        """标题必须并入既有拆解调用，提示词里得明确要求，否则模型不会给。"""
        # Arrange
        from src.llm.prompts import get_prompt_definition

        # Act
        template = get_prompt_definition("report.plan").template

        # Assert
        assert "title" in template
        assert "12" in template
        assert "维度罗列" in template

    # 方法作用：验证未建立关系的证据不单独占块。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_no_relation_blocks_are_dropped(self) -> None:
        """20 块里多数是"未发现共同字段"，占版面却不提供信息。"""
        # Arrange
        relations = [
            RelationEvidence(left_unit_id="unit_1", right_unit_id="unit_2", relation_type="entity_time_join", join_keys=["年月"], evidence_level="verified_relation", match_rate=1.0),
            RelationEvidence(left_unit_id="unit_1", right_unit_id="unit_3", relation_type="none", evidence_level="no_relation"),
        ]

        # Act
        artifact = build_report_document(_plan(_QUESTION, _QUESTION, _QUESTION), [_result("unit_1"), _result("unit_2"), _result("unit_3")], relations=relations)
        titles = [block.title for section in artifact.document.sections if section.id == "relations" for block in section.blocks]

        # Assert
        assert len(titles) == 1
        assert "unit_1 与 unit_2" in titles[0]

    # 方法作用：验证分位数一类的统计量不再当作"发现"。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_distribution_stats_are_not_findings(self) -> None:
        """P25/P50/偏度是统计过程输出，不是可行动的业务发现。"""
        # Arrange
        from src.reporting.profiling import profile_unit

        rows = [{"year_month": f"2026-{month:02d}", "net_sales": month * 100 + month % 3} for month in range(1, 13)]

        # Act
        findings = profile_unit(unit_id="u1", rows=rows, columns=["year_month", "net_sales"], metrics=["net_sales"])

        # Assert
        assert not [item for item in findings if item.category == "distribution"]

    # 方法作用：验证异常点用业务标签定位而不是行号。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_anomaly_uses_business_label(self) -> None:
        """「第 32 行 / z-score=3.25」对用户没有意义，必须落到时间或维度取值上。"""
        # Arrange
        from src.reporting.profiling import profile_unit

        rows = [{"year_month": f"2026-{month:02d}", "net_sales": 100 + month} for month in range(1, 12)]
        rows.append({"year_month": "2026-12", "net_sales": 90000})

        # Act
        findings = profile_unit(unit_id="u1", rows=rows, columns=["year_month", "net_sales"], metrics=["net_sales"])
        anomalies = [item for item in findings if item.category == "anomaly"]

        # Assert：必须检出，否则本用例空洞
        assert anomalies, "构造的极值未被检出，用例失去意义"
        labels = [item.label for item in anomalies[0].items]
        assert "2026-12" in labels
        for item in anomalies[0].items:
            assert "行" not in item.label
            assert "z-score" not in (item.baseline or "")

    # 方法作用：验证截断判定只看报表展示的行集。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_truncation_reflects_displayed_rows_only(self) -> None:
        """上游"给模型的样本被裁到 50 行"与用户可见行集无关，不能拿来报截断。"""
        # Arrange
        from src.reporting.models import display_truncated

        # Act / Assert
        assert display_truncated(60, 60) is False      # 60 行全量展示：实机误报的就是这种
        assert display_truncated(50, 90) is True       # 展示 50 / 全量 90：确实被裁
        assert display_truncated(60, 0) is False       # 全量未知时不臆断

    # 方法作用：验证全部关系都未建立时不产出关系区段。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_relations_section_absent_without_evidence(self) -> None:
        """整段都是"没关系"时，区段本身就不该出现在页面上。"""
        # Arrange
        relations = [RelationEvidence(left_unit_id="unit_1", right_unit_id="unit_2", relation_type="none", evidence_level="no_relation")]

        # Act
        artifact = build_report_document(_plan(_QUESTION, _QUESTION), [_result("unit_1"), _result("unit_2")], relations=relations)

        # Assert
        assert not [section for section in artifact.document.sections if section.id == "relations"]
