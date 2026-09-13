"""报表运行器的数据源扇出契约测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


# 方法作用：构造只记录入参的假工作流，避免真实 LangGraph 与数据库依赖。
# Args: captured - 收集工作流入参的列表。
# Returns: 提供 await ainvoke 的假工作流对象。
def _fake_workflow(captured: list[dict]):
    class FakeApp:
        # 方法作用：记录工作流入参并返回无数据的成功结果。
        # Args: payload - 工作流状态；config - 线程配置。
        # Returns: 含 final_response 的固定结果。
        async def ainvoke(self, payload, config):
            captured.append(payload)
            return {"final_response": {"success": True, "data": []}}

    return FakeApp()


@pytest.mark.asyncio
class TestReportRunnerDatasourceFanout:
    """覆盖功能 23.3：分析单元数据源在执行期的解析与扇出。"""

    # 方法作用：验证单元未指定数据源时，全部已选数据源都参与执行期检索。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unspecified_unit_fans_out_to_all_sources(self, monkeypatch) -> None:
        """单元数据源留空时应把全部已选数据源交给工作流并行检索。"""
        # Arrange
        import src.graph.workflow as workflow_module
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        captured: list[dict] = []
        monkeypatch.setattr(workflow_module, "app", _fake_workflow(captured))
        runner = ReportRunner(store=ReportStore(memory=True))
        sources = ["sqlserver", "oracle", "ch"]
        plan = ReportPlan(
            requested_datasources=sources,
            analysis_units=[AnalysisUnit(question="销售趋势")],
        )
        state = {
            "plan": plan,
            "run_id": "run-1",
            "identity": {"tenant_id": 3, "user_id": 3, "role": "tenant_admin"},
            "authorized_datasources": {name: {} for name in sources},
        }

        # Act
        await runner._default_execute_unit(plan.analysis_units[0], state)

        # Assert
        assert captured[0]["selected_datasources"] == sources
        # 契约要求 datasource 必须属于 selected_datasources，取首个作为主源标签
        assert captured[0]["datasource"] == "sqlserver"

    # 方法作用：验证单元明确指定数据源时维持单源，不扩大执行范围。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_specified_unit_stays_single_source(self, monkeypatch) -> None:
        """单元点名数据源时只能在该数据源上执行，不得扇出到其他源。"""
        # Arrange
        import src.graph.workflow as workflow_module
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        captured: list[dict] = []
        monkeypatch.setattr(workflow_module, "app", _fake_workflow(captured))
        runner = ReportRunner(store=ReportStore(memory=True))
        sources = ["sqlserver", "oracle", "ch"]
        plan = ReportPlan(
            requested_datasources=sources,
            analysis_units=[AnalysisUnit(question="库存明细", datasource="oracle")],
        )
        state = {
            "plan": plan,
            "run_id": "run-2",
            "identity": {"tenant_id": 3, "user_id": 3, "role": "tenant_admin"},
            "authorized_datasources": {name: {} for name in sources},
        }

        # Act
        await runner._default_execute_unit(plan.analysis_units[0], state)

        # Assert
        assert captured[0]["selected_datasources"] == ["oracle"]
        assert captured[0]["datasource"] == "oracle"

    # 方法作用：验证单元命中路由结果时只在该单元命中的数据源上执行。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_routed_unit_uses_matched_sources_only(self, monkeypatch) -> None:
        """路由命中后只能执行命中的数据源，不再广播到全部已选源。"""
        # Arrange
        import src.graph.workflow as workflow_module
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        captured: list[dict] = []
        monkeypatch.setattr(workflow_module, "app", _fake_workflow(captured))
        runner = ReportRunner(store=ReportStore(memory=True))
        unit = AnalysisUnit(question="供应链履约走势")
        plan = ReportPlan(requested_datasources=["oracle", "sqlserver", "ch"], analysis_units=[unit])
        state = {
            "plan": plan,
            "run_id": "run-3",
            "identity": {"tenant_id": 3, "user_id": 3, "role": "tenant_admin"},
            "authorized_datasources": {},
            "unit_datasources": {unit.unit_id: ["sqlserver"]},
        }

        # Act
        await runner._default_execute_unit(unit, state)

        # Assert
        assert captured[0]["selected_datasources"] == ["sqlserver"]
        assert captured[0]["datasource"] == "sqlserver"

    # 方法作用：验证没有任何数据源命中时按“不适用”处理而不是失败。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unit_without_matching_source_is_not_failure(self, monkeypatch) -> None:
        """路由判定无命中时应跳过查询并给出可读说明，且不调用工作流。"""
        # Arrange
        import src.graph.workflow as workflow_module
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        captured: list[dict] = []
        monkeypatch.setattr(workflow_module, "app", _fake_workflow(captured))
        runner = ReportRunner(store=ReportStore(memory=True))
        unit = AnalysisUnit(question="市场景气指数相关分析")
        plan = ReportPlan(requested_datasources=["oracle", "sqlserver"], analysis_units=[unit])
        state = {
            "plan": plan,
            "run_id": "run-4",
            "identity": {"tenant_id": 3, "user_id": 3, "role": "tenant_admin"},
            "authorized_datasources": {},
            "unit_datasources": {unit.unit_id: []},
        }

        # Act
        result = await runner._default_execute_unit(unit, state)

        # Assert：不能计入失败，也不能发起无效查询
        assert result.status == "partial"
        assert "未找到相关数据" in result.user_error_message
        assert captured == []

    # 方法作用：验证预占用计划按路由命中数修正，而不是沿用低报的单元数。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reserve_plan_scales_with_routed_sources(self) -> None:
        """多源扇出的预占量必须等于 单元数 × 命中源数，不能只数单元数。"""
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner

        # Arrange
        unit_a = AnalysisUnit(question="销售")
        unit_b = AnalysisUnit(question="财务")
        # 用保守档隔离基础扇出：档位总额度为 0，预占只剩「单元数 × 命中源数」这一项。
        # 档位带来的最坏情况预占由 test_divergence.py 的 test_reserve_covers_worst_case_rounds 覆盖。
        plan = ReportPlan(divergence="conservative", requested_datasources=["a", "b", "c"], analysis_units=[unit_a, unit_b])

        # Act：unit_a 命中 1 个源，unit_b 命中 2 个源
        adjusted = ReportRunner._reserve_plan(plan, {unit_a.unit_id: ["a"], unit_b.unit_id: ["a", "b"]})

        # Assert
        assert adjusted.estimated.query_count == 3
        # 未路由时保持原估计，不做二次打折
        assert ReportRunner._reserve_plan(plan, {}).estimated.query_count == plan.estimated.query_count

    # 方法作用：验证计划估算按多源扇出取上界，不低报。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_planner_estimate_scales_with_fanout(self, monkeypatch) -> None:
        """未绑定数据源的单元按 单元数 × 源数 计上界，只数单元数会低报 N 倍。"""
        # Arrange
        import src.llm.client as llm_client
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)

        # Act：保守档隔离基础扇出，档位项为 0
        plan = await ReportPlanner().parse("生成销售报表和库存报表", datasources=["a", "b", "c"], divergence="conservative")

        # Assert
        assert plan.estimated.query_count == len(plan.analysis_units) * 3

    # 方法作用：验证路由只接受已授权数据源，模型越权返回被忽略。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_route_units_drops_unauthorized_sources(self, monkeypatch) -> None:
        """模型返回未授权数据源时必须被剔除，不能扩大执行范围。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.llm.output_contracts import ReportUnitRoutesOutput
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        unit = AnalysisUnit(question="库存走势")
        plan = ReportPlan(requested_datasources=["oracle", "ch"], analysis_units=[unit])
        runner = ReportRunner(store=ReportStore(memory=True))
        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)
        monkeypatch.setattr(ReportRunner, "_datasource_table_catalog", AsyncMock(return_value="### oracle\nT1"))

        # 方法作用：返回一个混入未授权数据源的路由结果。
        # Args: prompt_id - 注册的 Prompt 标识；sections - 提示段落；kwargs - 透传参数。
        # Returns: 含越权数据源的路由契约实例。
        async def fake_invoke_structured(prompt_id, sections, **kwargs):
            return ReportUnitRoutesOutput(routes=[{"unit_id": unit.unit_id, "datasources": ["oracle", "crm"]}])

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke_structured)

        # Act
        routes = await runner._route_units(plan, ["oracle", "ch"], 3)

        # Assert
        assert routes == {unit.unit_id: ["oracle"]}

    # 方法作用：验证单元执行显式声明报表能力，关闭旧的自主补查回环。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_execute_unit_declares_report_capability(self, monkeypatch) -> None:
        """不声明 capability 会让单元问句被判为 sql_analysis，从而进入无配额控制的旧回环。"""
        # Arrange
        import src.graph.workflow as workflow_module
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        captured: list[dict] = []
        monkeypatch.setattr(workflow_module, "app", _fake_workflow(captured))
        runner = ReportRunner(store=ReportStore(memory=True))
        unit = AnalysisUnit(question="各地区销售额趋势")
        plan = ReportPlan(requested_datasources=["oracle"], analysis_units=[unit])
        state = {
            "plan": plan,
            "run_id": "run-5",
            "identity": {"tenant_id": 3, "user_id": 3, "role": "tenant_admin"},
            "authorized_datasources": {"oracle": {}},
        }

        # Act
        await runner._default_execute_unit(unit, state)

        # Assert
        assert captured[0]["capability"] == "report"

    # 方法作用：验证执行器透传旧工作流已有的截断信号。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_execute_unit_passes_through_truncation_signal(self, monkeypatch) -> None:
        """丢弃截断信号会让统计结论无从披露样本局限。"""
        # Arrange
        import src.graph.workflow as workflow_module
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        class TruncatingApp:
            # 方法作用：返回带截断标记的成功结果。
            # Args: payload - 工作流状态；config - 线程配置。
            # Returns: 含 query_result_statistics 的固定结果。
            async def ainvoke(self, payload, config):
                return {
                    "final_response": {"success": True, "data": [{"v": 1}]},
                    "query_result_statistics": {"row_count": 1, "truncated": True},
                }

        monkeypatch.setattr(workflow_module, "app", TruncatingApp())
        runner = ReportRunner(store=ReportStore(memory=True))
        unit = AnalysisUnit(question="明细")
        plan = ReportPlan(requested_datasources=["oracle"], analysis_units=[unit])
        state = {
            "plan": plan,
            "run_id": "run-6",
            "identity": {"tenant_id": 3, "user_id": 3, "role": "tenant_admin"},
            "authorized_datasources": {"oracle": {}},
        }

        # Act
        result = await runner._default_execute_unit(unit, state)

        # Assert
        assert result.truncated is True

    # 方法作用：验证缺失截断信号时按未截断处理。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_missing_truncation_signal_defaults_to_false(self, monkeypatch) -> None:
        """无信号时不得凭空判为截断。"""
        # Arrange
        import src.graph.workflow as workflow_module
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        captured: list[dict] = []
        monkeypatch.setattr(workflow_module, "app", _fake_workflow(captured))
        runner = ReportRunner(store=ReportStore(memory=True))
        unit = AnalysisUnit(question="明细")
        plan = ReportPlan(requested_datasources=["oracle"], analysis_units=[unit])
        state = {
            "plan": plan,
            "run_id": "run-7",
            "identity": {"tenant_id": 3, "user_id": 3, "role": "tenant_admin"},
            "authorized_datasources": {"oracle": {}},
        }

        # Act
        result = await runner._default_execute_unit(unit, state)

        # Assert
        assert result.truncated is False
