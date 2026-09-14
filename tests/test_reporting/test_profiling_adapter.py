"""报表确定性深挖适配层回归，覆盖 23.16。

统计数值本身由 tests/test_tools/ 覆盖，这里只测适配与披露：
参数推导是否确定性、报表级严谨性补强是否生效、结论是否可解释。
"""

from __future__ import annotations


# 方法作用：构造按月递增的销售额序列。
# Args: months - 月数；start - 起始值；step - 每月增量。
# Returns: 行数据列表。
def _monthly(months: int, start: float = 100.0, step: float = 1.0) -> list[dict]:
    return [{"月份": f"2026-{month:02d}", "销售额": start + step * (month - 1)} for month in range(1, months + 1)]


class TestParameterDetection:
    """验证 23.16：时间列、数值列与聚合方式必须确定性推导。"""

    # 方法作用：验证时间列按列值可解析性识别，优先于命名启发。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_time_column_prefers_parseable_values(self) -> None:
        """能解析成日期且命名不像时间键的列，也应被识别为时间列。"""
        # Arrange
        from src.reporting.profiling import detect_time_column

        rows = [{"period_label": "2026-01", "销售额": 1}, {"period_label": "2026-02", "销售额": 2}]

        # Act
        column = detect_time_column(rows, ["period_label", "销售额"])

        # Assert
        assert column == "period_label"

    # 方法作用：验证没有可解析日期时回退到既有时间键命名启发。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_time_column_falls_back_to_name_heuristic(self) -> None:
        """无可解析日期时用既有 _is_time_key 兜底。"""
        # Arrange
        from src.reporting.profiling import detect_time_column

        rows = [{"stat_date": "第1期", "销售额": 1}, {"stat_date": "第2期", "销售额": 2}]

        # Act
        column = detect_time_column(rows, ["stat_date", "销售额"])

        # Assert
        assert column == "stat_date"

    # 方法作用：验证两列都识别不出时不产出时间相关结论。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_time_column_absent_skips_temporal_findings(self) -> None:
        """识别不出时间列时不得凭空推断趋势或环比。"""
        # Arrange
        from src.reporting.profiling import profile_unit

        rows = [{"渠道": "线上", "销售额": value} for value in (10, 20, 30, 40, 50, 60)]

        # Act
        findings = profile_unit(unit_id="u1", rows=rows, columns=["渠道", "销售额"], metrics=["销售额"])

        # Assert
        assert not [item for item in findings if item.category == "period_over_period"]

    # 方法作用：验证数值列优先取单元声明的指标。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_value_column_prefers_declared_metrics(self) -> None:
        """声明了指标列时不应被其他数值列抢占。"""
        # Arrange
        from src.reporting.profiling import detect_value_column

        rows = [{"销售额": 10, "数量": 2}, {"销售额": 20, "数量": 3}]

        # Act / Assert
        assert detect_value_column(rows, ["销售额", "数量"], ["数量"]) == "数量"
        assert detect_value_column(rows, ["销售额", "数量"], ["缺失列"]) == "销售额"

    # 方法作用：验证率比类列按均值、其余按求和。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_aggregation_uses_mean_for_rate_like_names(self) -> None:
        """转化率求和没有意义。"""
        # Arrange
        from src.reporting.profiling import aggregation_for

        # Act / Assert
        assert aggregation_for("转化率") == "mean"
        assert aggregation_for("客单价") == "mean"
        assert aggregation_for("avg_price") == "mean"
        assert aggregation_for("销售额") == "sum"

    # 方法作用：验证聚合方式必须写进 method 供用户判断。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_aggregation_choice_is_disclosed_in_method(self) -> None:
        """启发式规则必须披露，否则用户无从判断数字口径。"""
        # Arrange
        from src.reporting.profiling import profile_unit

        # Act
        findings = profile_unit(unit_id="u1", rows=_monthly(6), columns=["月份", "销售额"], metrics=["销售额"])

        # Assert
        period = next(item for item in findings if item.category == "period_over_period")
        assert "求和" in period.method
        assert "月份" in period.method

    # 方法作用：验证行按时间升序排列且丢弃不可解析行。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sort_by_time_orders_rows_and_drops_unparseable(self) -> None:
        """环比依赖行序，乱序输入必须先排序。"""
        # Arrange
        from src.reporting.profiling import sort_by_time

        rows = [{"月份": "2026-03", "v": 3}, {"月份": "乱码", "v": 9}, {"月份": "2026-01", "v": 1}]

        # Act
        ordered = sort_by_time(rows, "月份")

        # Assert
        assert [row["v"] for row in ordered] == [1, 3]

    # 方法作用：验证聚合保留原始周期标签写法。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_aggregate_keeps_original_period_labels(self) -> None:
        """用户看到的周期名应与输入一致，不被改写成 ISO 日期。"""
        # Arrange
        from src.reporting.profiling import aggregate_by_period

        rows = [{"月份": "2026-01", "v": 1}, {"月份": "2026-02", "v": 2}]

        # Act
        aggregated = aggregate_by_period(rows, "月份", "v", "sum")

        # Assert
        assert [item["period"] for item in aggregated] == ["2026-01", "2026-02"]


class TestTemporalFindings:
    """验证 23.16：同比窗口、趋势显著性与样本披露。"""

    # 方法作用：验证跨度不足一年时不产同比。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_yoy_skipped_when_span_below_one_year(self) -> None:
        """12 个月数据无法回看一年，不得产出同比结论。"""
        # Arrange
        from src.reporting.profiling import profile_unit

        # Act
        findings = profile_unit(unit_id="u1", rows=_monthly(12), columns=["月份", "销售额"], metrics=["销售额"])

        # Assert
        assert not [item for item in findings if item.title == "同比变化"]

    # 方法作用：验证跨两个年度时产出同比并标注去年同期。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_yoy_emitted_when_span_covers_a_year(self) -> None:
        """跨度覆盖一年时应给出同比，基准写明去年同期。"""
        # Arrange
        from src.reporting.profiling import profile_unit

        rows = [{"月份": f"{year}-{month:02d}", "销售额": float(amount)} for year, amount in ((2025, 100), (2026, 150)) for month in range(1, 13)]

        # Act
        findings = profile_unit(unit_id="u1", rows=rows, columns=["月份", "销售额"], metrics=["销售额"])

        # Assert
        yoy = next(item for item in findings if item.title == "同比变化")
        assert "去年同期" in yoy.items[0].baseline

    # 方法作用：验证拟合优度不足时只报不显著并写入限制。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_trend_marks_insignificant_when_r_squared_low(self) -> None:
        """噪声序列不得给出方向性措辞。"""
        # Arrange
        from src.reporting.profiling import profile_unit

        rows = [{"月份": f"2026-{month:02d}", "销售额": float(value)} for month, value in enumerate((10, 90, 20, 80, 30, 70, 40, 60), start=1)]

        # Act
        findings = profile_unit(unit_id="u1", rows=rows, columns=["月份", "销售额"], metrics=["销售额"])

        # Assert
        trend = next(item for item in findings if item.category == "trend")
        assert trend.limitations
        assert trend.items[0].significance == "low"

    # 方法作用：验证样本被截断时每个结论都带披露。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_truncated_input_marks_every_finding(self) -> None:
        """样本局限必须随结论一起出现，不能被藏起来。"""
        # Arrange
        from src.reporting.profiling import profile_unit

        # Act
        findings = profile_unit(unit_id="u1", rows=_monthly(12), columns=["月份", "销售额"], metrics=["销售额"], truncated=True)

        # Assert
        assert findings
        for finding in findings:
            assert finding.truncated is True
            assert all("样本被截断" in caveat for item in finding.items for caveat in item.caveats)

    # 方法作用：验证每个结论都带可解释的 method。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_findings_carry_method_description(self) -> None:
        """没有 method 的结论无法审计。"""
        # Arrange
        from src.reporting.profiling import profile_unit

        # Act
        findings = profile_unit(unit_id="u1", rows=_monthly(12), columns=["月份", "销售额"], metrics=["销售额"])

        # Assert
        assert findings
        for finding in findings:
            assert finding.method.strip()


class TestAnomalyDualCriteria:
    """验证 23.16：异常必须 IQR 与 MAD 双判据同时确认。"""

    # 方法作用：验证 IQR 无法确认时不采信处理器的单判据。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_anomaly_requires_iqr_confirmation(self) -> None:
        """处理器 z-score 判为异常但 IQR 无法确认时不得报异常。

        十个 1 加一个 100：处理器标准差非零会判异常，但 IQR 为 0，
        第二判据无从确认，此时不能仅凭 z-score 下结论。
        """
        # Arrange
        from src.reporting.profiling import profile_unit

        rows = [{"月份": f"2026-{month:02d}", "销售额": 1.0} for month in range(1, 11)]
        rows.append({"月份": "2026-11", "销售额": 100.0})

        # Act
        findings = profile_unit(unit_id="u1", rows=rows, columns=["月份", "销售额"], metrics=["销售额"])

        # Assert
        assert not [item for item in findings if item.category == "anomaly"]

    # 方法作用：验证双判据同时命中时正常报出异常。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_anomaly_reported_when_both_criteria_hold(self) -> None:
        """明显离群且 IQR 可确认时应报出异常点。"""
        # Arrange
        from src.reporting.profiling import profile_unit

        values = [10.0, 10, 10, 10, 11, 12, 13, 14, 15, 16, 90]
        rows = [{"月份": f"2026-{month:02d}", "销售额": value} for month, value in enumerate(values, start=1)]

        # Act
        findings = profile_unit(unit_id="u1", rows=rows, columns=["月份", "销售额"], metrics=["销售额"])

        # Assert
        anomaly = next(item for item in findings if item.category == "anomaly")
        assert "IQR" in anomaly.method
        assert "MAD" in anomaly.method
        assert anomaly.items


class TestContributionAndDistribution:
    """验证 23.16：高基维度不产出贡献度，分布形态不冒充集中度。"""

    # 方法作用：验证维度基数过高时不产出贡献度。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_contribution_skipped_for_high_cardinality(self) -> None:
        """高基维度的贡献度没有解释力。"""
        # Arrange
        from src.reporting.profiling import profile_unit

        rows = [{"渠道": f"渠道{index}", "销售额": float(index)} for index in range(60)]

        # Act
        findings = profile_unit(unit_id="u1", rows=rows, columns=["渠道", "销售额"], metrics=["销售额"])

        # Assert
        assert not [item for item in findings if item.category == "contribution"]

    # 方法作用：验证纯数值列既不冒充集中度，也不再产出统计量"发现"。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_distribution_stats_are_dropped_not_misread_as_concentration(self) -> None:
        """分位数/偏度已按实机反馈移出报表；集中度指标更不适用于纯数值列。"""
        # Arrange
        from src.reporting.profiling import profile_unit

        # Act
        findings = profile_unit(unit_id="u1", rows=_monthly(12), columns=["月份", "销售额"], metrics=["销售额"])

        # Assert：既没有集中度，也没有分布统计量
        assert not [item for item in findings if item.category == "distribution"]
        labels = [item_label for finding in findings for item_label in (finding_item.label for finding_item in finding.items)]
        assert not any("CR3" in label or "HHI" in label for label in labels)
        assert not any("P25" in label or "P50" in label or "P75" in label for label in labels)


class TestDegradation:
    """验证 23.16：数据不足时宁可不说。"""

    # 方法作用：验证空输入与单行输入不产出结论也不抛错。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_empty_and_single_row_inputs_return_no_findings(self) -> None:
        """数据不足时不得降级到猜测。"""
        # Arrange
        from src.reporting.profiling import profile_unit

        # Act / Assert
        assert profile_unit(unit_id="u1", rows=[], columns=["月份", "销售额"], metrics=["销售额"]) == []
        assert profile_unit(unit_id="u1", rows=[{"月份": "2026-01", "销售额": 1}], columns=["月份", "销售额"], metrics=["销售额"]) == []
        assert profile_unit(unit_id="u1", rows=_monthly(3), columns=[], metrics=[]) == []

    # 方法作用：验证图表类型取自处理器结果而不是自行映射。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chart_type_comes_from_processor_result(self) -> None:
        """图表类型应复用处理器的判断，避免两处口径不一致。"""
        # Arrange
        from src.reporting.profiling import profile_unit

        # Act
        findings = profile_unit(unit_id="u1", rows=_monthly(12), columns=["月份", "销售额"], metrics=["销售额"])

        # Assert
        period = next(item for item in findings if item.category == "period_over_period")
        assert period.chart_type == "line"


class TestDeepAnalysisWiring:
    """验证 23.16：深挖节点与页面 findings 区段的接线。"""

    # 方法作用：构造一个最小页面产物作为追加目标。
    # Args: self - 测试实例。
    # Returns: 可追加区段的 ReportArtifact。
    def _artifact(self):
        from src.reporting.document import build_report_document
        from src.reporting.models import AnalysisUnit, ReportPlan

        return build_report_document(ReportPlan(analysis_units=[AnalysisUnit(question="销售额")]), [])

    # 方法作用：验证只对成功单元做深挖。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_node_profiles_successful_units_only(self) -> None:
        """失败单元没有数据可加工，不得产出发现。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import report_analyze_units
        from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult

        state = {
            "plan": ReportPlan(analysis_units=[AnalysisUnit(question="销售额")]),
            "resolved_units": [
                {"unit_id": "u1", "question": "销售额", "metrics": ["销售额"]},
                {"unit_id": "u2", "question": "失败单元", "metrics": []},
            ],
            "unit_results": [
                UnitResult(unit_id="u1", columns=["月份", "销售额"], rows=_monthly(12)),
                UnitResult(unit_id="u2", status="failed"),
            ],
        }

        # Act
        result = await report_analyze_units(state)

        # Assert
        assert result["unit_findings"]
        assert all(item.unit_id == "u1" for item in result["unit_findings"])

    # 方法作用：验证发现区段带上单元问题与计算方法。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_section_appended_with_method(self) -> None:
        """页面块标题要能定位到单元，且必须带计算方法。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import _append_findings
        from src.reporting.profiling import profile_unit

        artifact = self._artifact()
        findings = profile_unit(unit_id="u1", rows=_monthly(12), columns=["月份", "销售额"], metrics=["销售额"])

        # Act
        _append_findings(artifact, findings, [{"unit_id": "u1", "question": "销售额"}])

        # Assert
        sections = [item for item in artifact.document.sections if item.id == "findings"]
        assert len(sections) == 1
        block = sections[0].blocks[0]
        assert block.type == "findings"
        assert "销售额" in block.title
        assert block.content["method"]

    # 方法作用：验证没有发现时不产生空区段。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_no_findings_means_no_section(self) -> None:
        """无发现时不得留下空壳区段。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import _append_findings

        artifact = self._artifact()

        # Act
        _append_findings(artifact, [], [])

        # Assert
        assert not [item for item in artifact.document.sections if item.id == "findings"]
