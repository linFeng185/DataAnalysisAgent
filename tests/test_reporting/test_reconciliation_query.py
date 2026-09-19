"""报表取数语义：对账/核对类单元必须逐期返回，不能把差额条件写进 WHERE。

实机取证（report d5e60e90-7a55-405c-b3ab-1949e169fda0，run edc9b5be-22b9-4c35-b078-9ca697f4e451）：
trial_balance 单元的取数 SQL 带了 `WHERE diff_amount <> 0`，而数据库里 12 个会计期的
差额全部为零、unbalanced 计数为 0，于是查询返回 0 行——「全部平衡」与「一张表都没有数据」
在报表里完全无法区分，报表因此给出空结论。

修复走"查询提示词约束"这一层：不写死方言、不写死表名或 unit_id，任何核对类单元都适用。
"""

from __future__ import annotations

import pytest

from src.reporting.models import AnalysisUnit
from src.reporting.runner import (
    _compose_unit_query,
    _is_reconciliation_question,
    _reconciliation_requirement,
)


class TestReconciliationDetection:
    """覆盖功能 23.41：对账语义按词表判定，不绑定具体 unit_id。"""

    # 方法作用：验证核对类问题被识别。
    # Args: self - 测试实例；question - 待判定问题。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        "question",
        [
            "核对总账凭证的借贷差额是否为零，按会计期给出不平凭证清单",
            "按会计期对账，检查订单净额与发票金额的差额",
            "试算平衡：逐月核对借贷差异",
        ],
    )
    def test_reconciliation_questions_are_detected(self, question: str) -> None:
        """借贷/差额/为零这类提问，答案只能是逐期结果，不是"不平的行"。"""
        # Arrange / Act / Assert
        assert _is_reconciliation_question(question)

    # 方法作用：验证非对账类问题不被误判。
    # Args: self - 测试实例；question - 待判定问题。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        "question",
        [
            "按区域统计净销售额趋势",
            "对比不同定价区域的折扣深度与销量弹性",
            "产能平衡计划：生产线需求工时与可用工时",
        ],
    )
    def test_other_questions_are_not_reconciliation(self, question: str) -> None:
        """「平衡」单独出现不足以判定：产能平衡计划不是对账，不能被追加逐期要求。"""
        # Arrange / Act / Assert
        assert not _is_reconciliation_question(question)

    # 方法作用：验证空问题不触发对账要求。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_blank_question_is_not_reconciliation(self) -> None:
        # Arrange / Act / Assert
        assert not _is_reconciliation_question("")
        assert _reconciliation_requirement("") == ""


class TestReconciliationRequirement:
    """覆盖功能 23.41：要求文本必须明确禁止"过滤掉平衡期"。"""

    # 方法作用：验证要求禁止把差额条件写进 WHERE，并要求逐期返回。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_requirement_forbids_filtering_out_balanced_periods(self) -> None:
        """WHERE 差额 <> 0 在全部平衡时返回 0 行，必须被明确排除。"""
        # Arrange
        question = "核对总账凭证的借贷差额是否为零，按会计期给出不平凭证清单"

        # Act
        text = _reconciliation_requirement(question)

        # Assert
        assert "逐期返回一行" in text
        assert "WHERE" in text
        assert "不平记录数" in text

    # 方法作用：验证非对账问题不追加任何要求。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_requirement_is_empty_for_other_questions(self) -> None:
        # Arrange / Act / Assert
        assert _reconciliation_requirement("按区域统计净销售额趋势") == ""

    # 方法作用：验证要求文本与方言/表名/unit_id 解耦。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_requirement_is_dialect_independent(self) -> None:
        """同一份要求要同时适配 PostgreSQL / MySQL / ClickHouse，不能写死方言函数。

        实机取证发生在 PostgreSQL 上，但取数文本由模型按目标方言生成；
        这里约束的是"不针对某个方言或某张表硬编码"。
        """
        # Arrange
        question = "核对总账凭证的借贷差额是否为零，按会计期给出不平凭证清单"

        # Act
        text = _reconciliation_requirement(question)

        # Assert
        for dialect_word in ("postgres", "mysql", "clickhouse", "sqlite", "mssql"):
            assert dialect_word not in text.lower()
        # 不绑定具体单元、表或列：任何对账单元都命中同一份要求
        assert "trial_balance" not in text
        assert "unit_id" not in text
        assert "diff_amount" not in text


class TestComposedUnitQuery:
    """覆盖功能 23.41：要求必须真的进入执行文本，而不只是算出来放着。"""

    # 方法作用：验证对账单元的执行文本带上逐期返回要求。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_composed_query_carries_requirement_with_time_range(self) -> None:
        # Arrange
        unit = AnalysisUnit(
            unit_id="trial_balance",
            question="核对总账凭证的借贷差额是否为零，按会计期给出不平凭证清单",
        )

        # Act
        query = _compose_unit_query(unit)

        # Assert：原有取数粒度要求仍在，对账要求被追加
        assert "报表取数要求" in query
        assert "核对类取数要求" in query
        assert "不平记录数" in query

    # 方法作用：验证非对账单元的执行文本不被污染。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_composed_query_omits_requirement_for_other_units(self) -> None:
        # Arrange
        unit = AnalysisUnit(unit_id="sales_trend", question="按年月统计净销售额")

        # Act
        query = _compose_unit_query(unit)

        # Assert
        assert "核对类取数要求" not in query
        assert "报表取数要求" in query

    # 方法作用：验证对账单元的 unit_id 不参与判定（换个 id 行为一致）。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_requirement_does_not_depend_on_unit_id(self) -> None:
        # Arrange
        question = "核对总账凭证的借贷差额是否为零，按会计期给出不平凭证清单"

        # Act
        first = _compose_unit_query(AnalysisUnit(unit_id="unit_a", question=question))
        second = _compose_unit_query(AnalysisUnit(unit_id="unit_b", question=question))

        # Assert
        assert first == second
