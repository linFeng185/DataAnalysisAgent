"""报表预览裁剪与计划单调删减回归，覆盖 23.20。"""

from __future__ import annotations

import pytest


# 方法作用：构造含三个单元的计划，其中两个是发散单元。
# Args: 无。
# Returns: 通过校验的 ReportPlan。
def _plan():
    from src.reporting.models import AnalysisUnit, ReportPlan

    return ReportPlan(
        divergence="standard",
        analysis_units=[
            AnalysisUnit(question="用户需求", unit_id="u1"),
            AnalysisUnit(question="计划期发散", unit_id="d1", origin="diverged", rationale="相关", decided_by="plan"),
            AnalysisUnit(question="计划期发散二", unit_id="d2", origin="diverged", rationale="相关", decided_by="plan"),
        ],
    )


# 方法作用：构造一个最小身份对象。
# Args: 无。
# Returns: 带租户、用户与角色的轻量身份。
def _identity():
    return type("Identity", (), {"tenant_id": 1, "user_id": 1, "role": "tenant_admin"})()


class TestTrimValidation:
    """验证 23.20：裁剪只允许删减，非法输入必须报错。"""

    # 方法作用：验证全部保留时计划不变。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_full_acceptance_keeps_plan(self) -> None:
        """勾选全部单元不应产生任何改动与审计痕迹。"""
        # Arrange
        from src.api.routes.report_workbench import _apply_unit_trimming

        plan = _plan()

        # Act
        trimmed = _apply_unit_trimming(plan, ["u1", "d1", "d2"])

        # Assert
        assert trimmed is plan
        assert trimmed.excluded_unit_ids == []

    # 方法作用：验证非预览单元被拒绝。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unknown_unit_id_is_rejected(self) -> None:
        """没有这条校验，客户端就能凭空构造执行范围。"""
        # Arrange
        from fastapi import HTTPException
        from src.api.routes.report_workbench import _apply_unit_trimming

        # Act / Assert
        with pytest.raises(HTTPException) as excinfo:
            _apply_unit_trimming(_plan(), ["u1", "forged_unit"])
        assert excinfo.value.status_code == 422

    # 方法作用：验证显式空数组被拒绝。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_explicit_empty_accepted_ids_is_rejected(self) -> None:
        """空数组是「全部取消」，与不传该字段语义必须区分开。"""
        # Arrange
        from fastapi import HTTPException
        from src.api.routes.report_workbench import _apply_unit_trimming

        # Act / Assert
        with pytest.raises(HTTPException) as excinfo:
            _apply_unit_trimming(_plan(), [])
        assert excinfo.value.status_code == 422


class TestTrimResult:
    """验证 23.20：裁剪后的有效计划与审计痕迹。"""

    # 方法作用：验证只保留勾选单元。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_keeps_only_listed_units(self) -> None:
        """取消勾选的发散单元不得进入执行范围。"""
        # Arrange
        from src.api.routes.report_workbench import _apply_unit_trimming

        # Act
        trimmed = _apply_unit_trimming(_plan(), ["u1", "d1"])

        # Assert
        assert [unit.unit_id for unit in trimmed.analysis_units] == ["u1", "d1"]

    # 方法作用：验证被删减的标识记入审计字段。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_trimmed_ids_recorded_in_excluded_unit_ids(self) -> None:
        """审计线索要能回答「这份计划相对预览删了哪些」。"""
        # Arrange
        from src.api.routes.report_workbench import _apply_unit_trimming

        # Act
        trimmed = _apply_unit_trimming(_plan(), ["u1"])

        # Assert
        assert trimmed.excluded_unit_ids == ["d1", "d2"]

    # 方法作用：验证裁剪后哈希变化。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_trimmed_plan_has_different_hash(self) -> None:
        """有效计划与预览计划是两份不同的计划。"""
        # Arrange
        from src.api.routes.report_workbench import _apply_unit_trimming

        plan = _plan()

        # Act
        trimmed = _apply_unit_trimming(plan, ["u1"])

        # Assert
        assert trimmed.canonical_hash() != plan.canonical_hash()


class TestRequestContract:
    """验证 23.20：请求契约区分「不传」与「空数组」。"""

    # 方法作用：验证不传时表示全部接受。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_missing_accepted_ids_defaults_to_none(self) -> None:
        """不传该字段必须与空数组合义不同。"""
        # Arrange
        from src.reporting.models import ReportJobCreateRequest

        # Act
        request = ReportJobCreateRequest(prompt="生成报表", plan_hash="sha256:" + "a" * 64)

        # Assert
        assert request.accepted_unit_ids is None

    # 方法作用：验证显式空数组被原样保留。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_explicit_empty_list_is_preserved(self) -> None:
        """空数组要原样保留，由路由层拒绝，而不是被悄悄当成不传。"""
        # Arrange
        from src.reporting.models import ReportJobCreateRequest

        # Act
        request = ReportJobCreateRequest(prompt="生成报表", plan_hash="sha256:" + "a" * 64, accepted_unit_ids=[])

        # Assert
        assert request.accepted_unit_ids == []


class TestPreviewHash:
    """验证 23.20：预览凭证与有效计划哈希分列记录。"""

    # 方法作用：验证未裁剪时两者相等。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_preview_hash_equals_plan_hash_when_not_trimmed(self) -> None:
        """未裁剪时预览凭证就是计划哈希本身。"""
        # Arrange
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.store import ReportStore

        store = ReportStore(memory=True)
        plan = ReportPlan(analysis_units=[AnalysisUnit(question="销售", unit_id="u1")])

        # Act
        job = await store.create_job(_identity(), "生成报表", plan)

        # Assert
        assert job.preview_hash == plan.canonical_hash()
        assert job.plan_hash == plan.canonical_hash()

    # 方法作用：验证裁剪后凭证仍记录原始预览哈希。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_preview_hash_records_original_credential(self) -> None:
        """审计要能回答「这份计划来自哪次预览」。"""
        # Arrange
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.store import ReportStore

        store = ReportStore(memory=True)
        preview_plan = ReportPlan(analysis_units=[
            AnalysisUnit(question="销售", unit_id="u1"),
            AnalysisUnit(question="发散", unit_id="d1", origin="diverged", rationale="相关", decided_by="plan"),
        ])
        credential = preview_plan.canonical_hash()
        trimmed = preview_plan.model_copy(update={"analysis_units": [preview_plan.analysis_units[0]]})

        # Act
        job = await store.create_job(_identity(), "生成报表", trimmed, preview_hash=credential)

        # Assert
        assert job.preview_hash == credential
        # plan_hash 仍是实际存储计划的校验和，语义不变
        assert job.plan_hash == trimmed.canonical_hash()
        assert job.plan_hash != job.preview_hash
        assert len(job.plan.analysis_units) == 1
