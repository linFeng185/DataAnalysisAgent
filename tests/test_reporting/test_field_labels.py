"""报表字段备注与生成字段描述回归，覆盖 23.22。"""

from __future__ import annotations


class TestColumnLabels:
    """验证 23.22：列标题按「列名（说明）」生成。"""

    # 方法作用：验证有数据源备注时追加括号说明。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_appends_schema_comment(self) -> None:
        """数据库列注释应显示在字段名后的括号里。"""
        # Arrange
        from src.reporting.document import column_labels

        # Act
        labels = column_labels(["销售额"], {"销售额": "含税口径"}, {})

        # Assert
        assert labels == {"销售额": "销售额（含税口径）"}

    # 方法作用：验证数据源备注优先于模型给出的字段说明。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_schema_comment_wins_over_model_description(self) -> None:
        """数据库注释是权威口径，模型说明不能覆盖它。"""
        # Arrange
        from src.reporting.document import column_labels

        # Act
        labels = column_labels(["销售额"], {"销售额": "含税口径"}, {"销售额": "模型自己编的说明"})

        # Assert
        assert "含税口径" in labels["销售额"]
        assert "模型自己编的说明" not in labels["销售额"]

    # 方法作用：验证没有数据源备注时使用模型给出的生成字段说明。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_falls_back_to_model_description_for_generated_fields(self) -> None:
        """聚合与表达式产生的字段没有数据库注释，只能靠模型说明。"""
        # Arrange
        from src.reporting.document import column_labels

        # Act
        labels = column_labels(["total_amount"], {}, {"total_amount": "订单金额合计"})

        # Assert
        assert labels == {"total_amount": "total_amount（订单金额合计）"}

    # 方法作用：验证两处都没有说明时保持原列名。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_keeps_plain_name_without_any_note(self) -> None:
        """没有说明时不得凭空补一个。"""
        # Arrange
        from src.reporting.document import column_labels

        # Act / Assert
        assert column_labels(["uid"], {}, {}) == {"uid": "uid"}
        assert column_labels(["uid"], {"uid": "   "}, {"uid": ""}) == {"uid": "uid"}
        assert column_labels(["uid"], None, None) == {"uid": "uid"}


class TestExtraction:
    """验证 23.22：备注与字段说明从既有链路取到，不新增查询。"""

    # 方法作用：验证从相关工作流证据里抽取列备注。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_extract_column_comments(self) -> None:
        """列备注来自既有 relevant_tables，不需要额外查询数据源。"""
        # Arrange
        from src.reporting.runner import _extract_column_comments

        tables = [
            {"name": "orders", "columns": [
                {"name": "amount", "comment": "含税金额"},
                {"name": "uid", "comment": ""},
                {"name": "raw"},
            ]},
            "not-a-dict",
        ]

        # Act
        comments = _extract_column_comments(tables)

        # Assert
        assert comments == {"amount": "含税金额"}

    # 方法作用：验证抽取字段说明时丢弃空值。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_extract_field_descriptions_drops_empty(self) -> None:
        """空说明不得进入映射，否则会产出「列名（）」。"""
        # Arrange
        from src.reporting.runner import _extract_field_descriptions

        # Act / Assert
        assert _extract_field_descriptions({"field_descriptions": {"a": "说明", "b": "  ", "": "空键"}}) == {"a": "说明"}
        assert _extract_field_descriptions({}) == {}
        assert _extract_field_descriptions(None) == {}
        assert _extract_field_descriptions({"field_descriptions": "not-a-dict"}) == {}


class TestDocumentIntegration:
    """验证 23.22：页面块带上字段标题（明细表格已移除，能力落在 KPI 与钻取上）。"""

    # 方法作用：构造带备注与字段说明的单元结果。
    # Args: self - 测试实例。
    # Returns: 已构建的报表产物。
    def _artifact(self):
        from src.reporting.document import build_report_document
        from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult

        plan = ReportPlan(analysis_units=[AnalysisUnit(question="销售额")])
        result = UnitResult(
            unit_id="u1",
            columns=["月份", "amount"],
            rows=[{"月份": "2026-01", "amount": 10}, {"月份": "2026-02", "amount": 20}],
            column_comments={"amount": "含税金额"},
            field_descriptions={"月份": "统计月份"},
        )
        return build_report_document(plan, [result])

    # 方法作用：验证页面不再产出明细表格，列标签能力落在 KPI 块与钻取定义上。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_detail_table_removed_but_column_labels_survive(self) -> None:
        """明细表格已按实机反馈移除；列备注仍要能显示在留下的块上。"""
        # Arrange
        artifact = self._artifact()

        # Act
        blocks = [block for section in artifact.document.sections for block in section.blocks]

        # Assert：不再有表格块
        assert not [block for block in blocks if block.type == "table"]
        # 列标签能力保留在 KPI 块上（带备注的展示名 + 原始列名做数据键）
        kpi = next(block for block in blocks if block.type == "kpi")
        assert kpi.content["column_labels"]["amount"] == "amount（含税金额）"
        # KPI 取首行值：列标签带备注的同时，取值口径不变
        assert kpi.content["value"] == 10
        # 钻取定义保留原始列名，否则前端按列取数会落空
        assert artifact.document.drilldowns[0]["columns"] == ["月份", "amount"]

    # 方法作用：验证 KPI 块标题带说明。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_kpi_title_carries_note(self) -> None:
        """KPI 取首个数值列，标题应带该列的说明。"""
        # Arrange
        artifact = self._artifact()

        # Act
        kpis = [
            block
            for section in artifact.document.sections
            for block in section.blocks
            if block.type == "kpi"
        ]

        # Assert
        assert kpis
        assert "含税金额" in kpis[0].title
