"""报表预估查询数的上限契约（复现 2026-09-14 计划失败事故）。

事故：5 个数据源 + 22 个规则单元 + deep 档 6 个发散额度 = (22+6)×5 = 140 次预估查询，
撞上 EstimatedResources.query_count 里写死的 ``le=64``，``parse()`` 直接抛
ValidationError，报表落 plan_failed；参数确定，重试多少次都是同一个结果。

契约要点：
1. 字段静态上界必须容纳超级管理员可配置的最大值（report_max_queries_per_run
   允许到 1000），不能写死成与配置脱钩的常量；
2. 真实钳位一律通过 ``max_plan_queries()`` 读配置，planner 与 runner 共用同一函数；
3. 扇出超过上限时按配置钳位，而不是让整份计划失败。
"""

from __future__ import annotations

import pytest

# 事故当次的已选数据源数量：未绑定源的单元会在每个源上各跑一次查询。
_SOURCE_COUNT = 5
# 事故当次的发散档位：deep 档额度 6。
_DEEP_BUDGET = 6
# 被击穿的旧静态上界，用于证明回归用例确实越过了它。
_LEGACY_CAP = 64
# 中文章节与阿拉伯条目两层编号的分节结构：(章节名, 章节内条目数)。
_CHAPTERS: tuple[tuple[str, int], ...] = (
    ("一、经营总览", 4),
    ("二、财务对账", 4),
    ("三、供应链", 3),
    ("四、人力", 1),
    ("五、营销与外部环境", 3),
    ("六、数据质量", 1),
)


# 方法作用：从 Pydantic 字段元数据里取出上界约束值。
# Args: field - model_fields 里的字段信息对象。
# Returns: le 上界；没有该约束时返回 None。
def _le(field) -> int | None:
    from annotated_types import Le

    ceilings = [meta.le for meta in field.metadata if isinstance(meta, Le)]
    return ceilings[0] if ceilings else None


# 方法作用：构造与事故同构的两层编号需求文本。
# Args: unit_count - 需要的阿拉伯条目总数，必须等于 _CHAPTERS 的条目数之和。
# Returns: 含中文章节标题与阿拉伯条目号的需求文本。
def _two_level_prompt(unit_count: int) -> str:
    lines = ["立即生成一份《集团经营全景分析报表》，时间范围 2016-01-01 至 2036-12-31。", "", "以下为核心分析单元，必须全部完成。", ""]
    index = 0
    for name, count in _CHAPTERS:
        lines.append(f"【{name}】")
        for _ in range(count):
            index += 1
            lines.append(f"{index}. 分析主题{index}：按年月与地区统计订单净销售额、订单数与客单价；")
        lines.append("")
    assert index == unit_count, f"章节结构产出 {index} 个条目，与期望的 {unit_count} 不一致"
    return "\n".join(lines)


# 方法作用：按计划自身的单元数复算预估扇出上界，避免测试写死单元数。
# Args: unit_count - 计划里的分析单元数。
# Returns: 预估查询数上界。
def _expected_fanout(unit_count: int) -> int:
    return (unit_count + _DEEP_BUDGET) * _SOURCE_COUNT


class TestQueryCountCeiling:
    """覆盖预估查询数的静态上界与配置钳位。"""

    # 方法作用：验证预估查询数的静态上界不小于配置允许的最大值。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_static_ceiling_covers_configurable_max(self) -> None:
        """配置能配到 1000，字段上界就不能写死成 64——那等于配置形同虚设。"""
        # Arrange
        from src.config import Settings
        from src.reporting.models import EstimatedResources

        # Act
        field_ceiling = _le(EstimatedResources.model_fields["query_count"])
        config_ceiling = _le(Settings.model_fields["report_max_queries_per_run"])

        # Assert
        assert field_ceiling is not None, "query_count 缺少 le 上界约束"
        assert config_ceiling is not None, "report_max_queries_per_run 缺少 le 上界约束"
        assert field_ceiling >= config_ceiling, (
            f"query_count 上界 {field_ceiling} 小于配置可配的最大值 {config_ceiling}，"
            "配置调大后必然击穿并让计划解析抛 ValidationError"
        )

    # 方法作用：验证超过旧上界的多源深挖扇出仍能建成计划。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_fanout_over_legacy_cap_builds_plan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """(单元数 + deep 额度) × 源数 越过旧上界时必须照常出计划，而不是 plan_failed。"""
        # Arrange
        import src.llm.client as llm_client
        from src.config import get_settings
        from src.reporting.planner import ReportPlanner

        # 规则路径是模型不可用时的唯一回退，显式关闭模型才能确定性验证。
        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)
        monkeypatch.setattr(get_settings(), "report_max_queries_per_run", 1000)
        datasources = [f"ds{index}" for index in range(_SOURCE_COUNT)]

        # Act
        plan = await ReportPlanner().parse(
            _two_level_prompt(16), datasources=datasources, divergence="deep"
        )

        # Assert
        fanout = _expected_fanout(len(plan.analysis_units))
        assert fanout > _LEGACY_CAP, "用例未越过旧上界，失去回归意义"
        assert plan.estimated.query_count == fanout

    # 方法作用：验证预估查询数按配置的系统上限钳位。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_query_count_clamped_to_configured_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """系统上限设为 50 时，预估应钳到 50 而不是抛校验异常。"""
        # Arrange
        import src.llm.client as llm_client
        from src.config import get_settings
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)
        monkeypatch.setattr(get_settings(), "report_max_queries_per_run", 50)
        datasources = [f"ds{index}" for index in range(_SOURCE_COUNT)]

        # Act
        plan = await ReportPlanner().parse(
            _two_level_prompt(16), datasources=datasources, divergence="deep"
        )

        # Assert
        assert plan.estimated.query_count == 50


class TestReservePlanQueryCeiling:
    """覆盖运行期预占与计划期共用同一份配置上限。"""

    # 方法作用：验证运行期预占同样受配置上限约束。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_reserve_plan_respects_configured_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """预占曾是 min(1000, 实际扇出)，与字段契约脱钩；必须与计划期同源。"""
        # Arrange
        from src.config import get_settings
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner

        monkeypatch.setattr(get_settings(), "report_max_queries_per_run", 50)
        datasources = [f"ds{index}" for index in range(_SOURCE_COUNT)]
        units = [AnalysisUnit(unit_id=f"unit_{index}", question=f"销售分析{index}") for index in range(12)]
        plan = ReportPlan(divergence="deep", requested_datasources=datasources, analysis_units=units)
        routed = {unit.unit_id: datasources for unit in units}

        # Act
        reserved = ReportRunner._reserve_plan(plan, routed)

        # Assert
        assert reserved.estimated.query_count == 50
