"""报表视觉模板后端契约回归，覆盖 23.15。"""

from __future__ import annotations

import pytest


# 方法作用：构造一个最小计划。
# Args: 无。
# Returns: 通过校验的 ReportPlan。
def _plan():
    from src.reporting.models import AnalysisUnit, ReportPlan

    return ReportPlan(analysis_units=[AnalysisUnit(question="销售", unit_id="u1")])


# 方法作用：构造一个最小身份对象。
# Args: 无。
# Returns: 带租户、用户与角色的轻量身份。
def _identity():
    return type("Identity", (), {"tenant_id": 1, "user_id": 1, "role": "tenant_admin"})()


class TestTemplateContract:
    """验证 23.15：模板只落 Job，不进 Plan 与哈希。"""

    # 方法作用：验证模板标识写入页面文档的 theme。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_document_theme_carries_template_id(self) -> None:
        """后端只承载模板标识，不承载任何颜色或密度取值。"""
        # Arrange
        from src.reporting.document import build_report_document

        # Act
        artifact = build_report_document(_plan(), [], template_id="ink")

        # Assert
        assert artifact.document.theme == {"template_id": "ink"}
        # 旧版遗留的 accent/density 键不再下发，避免后端持有模板知识
        assert "accent" not in artifact.document.theme
        assert "density" not in artifact.document.theme

    # 方法作用：验证未指定模板时落到默认模板。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_document_theme_defaults_to_insight(self) -> None:
        """缺省值必须是前端注册表里的默认模板 id。"""
        # Arrange
        from src.reporting.document import build_report_document

        # Act / Assert
        assert build_report_document(_plan(), []).document.theme["template_id"] == "insight"
        assert build_report_document(_plan(), [], template_id="").document.theme["template_id"] == "insight"

    # 方法作用：验证模板标识不影响计划哈希。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_template_does_not_affect_plan_hash(self) -> None:
        """模板是纯表现设置，不进防篡改哈希——否则换配色要重新预览并重新确认。"""
        # Arrange
        from src.reporting.models import ReportJobCreateRequest

        # Act
        without = ReportJobCreateRequest(prompt="生成报表", plan_hash="sha256:" + "a" * 64)
        with_template = ReportJobCreateRequest(prompt="生成报表", plan_hash="sha256:" + "a" * 64, template_id="ink")

        # Assert
        assert without.template_id is None
        assert with_template.template_id == "ink"
        # ReportPlan 上根本没有该字段，因此哈希不可能被它影响
        assert not hasattr(_plan(), "template_id")

    # 方法作用：验证非法格式的模板标识被拒绝。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_malformed_template_id_is_rejected(self) -> None:
        """只校格式不校清单：清单在前端，后端再存一份必然脱钩。"""
        # Arrange
        from src.reporting.models import ReportJobCreateRequest

        # Act / Assert：非法格式必须吵出来，而不是静默降级
        for bad in ("", "Insight", "ink blue", "ink/blue", "a" * 33):
            with pytest.raises(ValueError):
                ReportJobCreateRequest(prompt="生成报表", plan_hash="sha256:" + "a" * 64, template_id=bad)
        # 32 字符合法边界与带短横线的标识必须通过
        assert ReportJobCreateRequest(prompt="x", plan_hash="sha256:" + "a" * 64, template_id="a" * 32).template_id
        assert ReportJobCreateRequest(prompt="x", plan_hash="sha256:" + "a" * 64, template_id="deep-blue_2").template_id


class TestTemplatePersistence:
    """验证 23.15：模板随任务落库并可回读。"""

    # 方法作用：验证创建任务时记录模板并在读取时回显。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_job_records_and_returns_template(self) -> None:
        """定时任务的每次运行都要沿用创建时选定的模板。"""
        # Arrange
        from src.reporting.store import ReportStore

        store = ReportStore(memory=True)

        # Act
        job = await store.create_job(_identity(), "生成报表", _plan(), template_id="ink")
        reloaded = await store.get_job(job.id, _identity())

        # Assert
        assert job.template_id == "ink"
        assert reloaded is not None and reloaded.template_id == "ink"

    # 方法作用：验证不选模板时记录为空，由前端回退默认模板。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_job_without_template_records_none(self) -> None:
        """为空表示使用前端默认模板，历史任务同样适用。"""
        # Arrange
        from src.reporting.store import ReportStore

        store = ReportStore(memory=True)

        # Act
        job = await store.create_job(_identity(), "生成报表", _plan())

        # Assert
        assert job.template_id is None
