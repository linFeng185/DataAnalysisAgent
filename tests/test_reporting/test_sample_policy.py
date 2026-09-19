"""样本策略契约：阈值可配、不再静默砍行、超限可解释、SQL 层先聚合。

覆盖计划 Task 3：取消硬编码暴力截断。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


class TestSamplePolicy:
    """覆盖阈值来源与截断语义的分离。"""

    # 方法作用：验证样本阈值来自配置且默认放宽。
    def test_threshold_is_configurable(self) -> None:
        """原 50/30/200 三处硬编码无法调整，超出即静默砍行。"""
        # Arrange / Act
        from src.config import Settings

        field = Settings.model_fields["report_max_sample_rows"]

        # Assert
        assert field.default >= 1000

    # 方法作用：验证超阈值时产出可解释的截断原因而不是静默丢弃。
    def test_overflow_produces_reason(self) -> None:
        # Arrange / Act
        from src.reporting.snapshots import describe_truncation

        reason = describe_truncation(sample_rows=5000, full_count=80000, limit=5000)

        # Assert
        assert "80000" in reason and "5000" in reason

    # 方法作用：验证未超限时不产生截断措辞。
    def test_within_limit_has_no_reason(self) -> None:
        # Arrange / Act
        from src.reporting.snapshots import describe_truncation

        # Assert
        assert describe_truncation(sample_rows=60, full_count=60, limit=5000) == ""

    # 方法作用：验证"给模型的提示词样本被裁"不再冒充"用户可见行集被截断"。
    def test_prompt_sample_cap_is_not_truncation(self) -> None:
        """事故：multi_source 把 len(data) > 50 计入 truncated，60 行全量也被判截断。"""
        # Arrange / Act
        from src.graph.contracts import SourceQueryResult

        result = SourceQueryResult(
            datasource="mysql",
            success=True,
            sql="SELECT 1",
            data=[{"a": 1}],
            full_count=80,
            truncated=False,
            sample_capped=True,
        )

        # Assert
        assert result.truncated is False
        assert result.sample_capped is True
        assert result.to_legacy_dict()["sample_capped"] is True

    # 方法作用：验证未裁剪样本时标记保持默认关闭。
    def test_sample_capped_defaults_false(self) -> None:
        from src.graph.contracts import SourceQueryResult

        result = SourceQueryResult(datasource="mysql", success=True, sql="SELECT 1")

        assert result.sample_capped is False

    # 方法作用：验证报表单元查询要求 SQL 层先聚合，明细不该进报表。
    def test_unit_query_demands_sql_side_aggregation(self) -> None:
        # Arrange
        from src.reporting.models import AnalysisUnit
        from src.reporting.runner import _compose_unit_query

        unit = AnalysisUnit(unit_id="u1", question="按员工、项目统计人力成本")

        # Act
        query = _compose_unit_query(unit)

        # Assert
        assert "GROUP BY" in query or "聚合" in query


class TestSampleGuardDecision:
    """覆盖超限时的模型决策契约。"""

    # 方法作用：验证决策契约接受聚合建议并保留可读理由。
    def test_decision_contract_fields(self) -> None:
        from src.reporting.sample_guard import SampleDecision

        decision = SampleDecision(
            action="aggregate",
            reason="返回的是员工×项目明细，未聚合",
            suggested_group_by=["employee_id", "project_id"],
        )

        assert decision.action == "aggregate"
        assert decision.suggested_group_by == ["employee_id", "project_id"]

    # 方法作用：验证默认动作是"保留"，不会默不作声地丢数据。
    def test_keep_action_is_default(self) -> None:
        from src.reporting.sample_guard import SampleDecision

        assert SampleDecision().action == "keep"

    # 方法作用：验证非法动作被契约拒绝。
    def test_illegal_action_rejected(self) -> None:
        from pydantic import ValidationError

        from src.reporting.sample_guard import SampleDecision

        with pytest.raises(ValidationError):
            SampleDecision(action="drop_rows")

    # 方法作用：验证未超阈值时不消耗模型调用（模块层判据）。
    @pytest.mark.asyncio
    async def test_within_limit_skips_model_at_module(self, monkeypatch) -> None:
        # Arrange
        from src.reporting import sample_guard

        async def _boom(*args, **kwargs):
            raise AssertionError("未超阈值不应调用模型")

        monkeypatch.setattr(sample_guard, "_invoke_sample_decision", _boom)

        # Act / Assert
        assert (
            await sample_guard.decide_sample_action(
                question="按年月统计净销售额",
                columns=["year_month", "net_sales"],
                row_count=10,
                full_count=10,
                time_span="12 个月",
                limit=5000,
            )
            is None
        )

    # 方法作用：验证超阈值但模型不可用时保守保留并给出披露理由。
    @pytest.mark.asyncio
    async def test_overflow_without_model_keeps_rows(self, monkeypatch) -> None:
        # Arrange
        from src.reporting import sample_guard

        async def _no_decision(*args, **kwargs):
            return None

        monkeypatch.setattr(sample_guard, "_invoke_sample_decision", _no_decision)

        # Act
        decision = await sample_guard.decide_sample_action(
            question="按员工、项目统计人力成本",
            columns=["employee_id", "project_id", "cost"],
            row_count=9000,
            full_count=9000,
            time_span="120 个月",
            limit=5000,
        )

        # Assert：不得静默丢行，也不得假装没有超限
        assert decision is not None
        assert decision.action == "keep"
        assert "9000" in decision.reason

    # 方法作用：验证判据在「上游裁剪过样本、真实行数未超限」时放行且不调用模型。
    @pytest.mark.asyncio
    async def test_capped_within_limit_discloses_without_model(self, monkeypatch) -> None:
        """判据写成 row_count <= limit 就返回 None 时本用例必红：该分支根本不可达。"""
        # Arrange
        from src.reporting import sample_guard

        called: list[str] = []

        # 方法作用：记录模型入口是否被触达，避免异常被吞掉后测试仍然变绿。
        # Args: args/kwargs - 决策入口的任意参数。
        # Returns: 固定返回 None（若真被调用，调用记录会暴露）。
        async def _record(*args, **kwargs):
            called.append("invoke_sample_decision")
            return None

        monkeypatch.setattr(sample_guard, "_invoke_sample_decision", _record)

        # Act：进入链路 20 行、全量 200 行都没超上限 5000，只有 sample_capped 为真
        decision = await sample_guard.decide_sample_action(
            question="按年月统计净销售额",
            columns=["year_month", "net_sales"],
            row_count=20,
            full_count=200,
            time_span="12 个月",
            limit=5000,
            sample_capped=True,
        )

        # Assert：不烧模型调用，但事实照样以两个真实计数披露
        assert called == []
        assert decision is not None
        assert decision.action == "keep"
        assert "上游执行层已对样本做裁剪" in decision.reason
        assert "本次进入分析链路 20 行" in decision.reason
        assert "查询全量 200 行" in decision.reason
        # 走模型不可用降级文案时也算失败：那说明裁剪分支根本没走到
        assert "模型不可用" not in decision.reason

    # 方法作用：验证全量行数未上报时不拿进入链路行数冒充全量。
    @pytest.mark.asyncio
    async def test_capped_without_full_count_marks_unknown(self, monkeypatch) -> None:
        # Arrange
        from src.reporting import sample_guard

        called: list[str] = []

        # 方法作用：记录模型入口是否被触达。
        # Args: args/kwargs - 决策入口的任意参数。
        # Returns: 固定返回 None。
        async def _record(*args, **kwargs):
            called.append("invoke_sample_decision")
            return None

        monkeypatch.setattr(sample_guard, "_invoke_sample_decision", _record)

        # Act：多源合并把真实全量丢在上游，full_count 回传 0
        decision = await sample_guard.decide_sample_action(
            question="按年月统计净销售额",
            columns=["year_month"],
            row_count=200,
            full_count=0,
            limit=5000,
            sample_capped=True,
        )

        # Assert
        assert called == []
        assert decision is not None
        assert "全量行数不可得" in decision.reason
        assert "查询全量 200 行" not in decision.reason
        assert "模型不可用" not in decision.reason


# 方法作用：构造只返回固定结果的工作流替身，避免真实 LangGraph 与数据库依赖。
# Args: response - ainvoke 返回的结果字典。
# Returns: 提供 await ainvoke 的假工作流对象。
def _fake_workflow(response: dict):
    class FakeApp:
        # 方法作用：忽略入参直接返回固定结果的浅拷贝。
        # Args: payload - 工作流状态；config - 线程配置。
        # Returns: 预置结果字典的浅拷贝。
        async def ainvoke(self, payload, config):
            # 真实 CompiledStateGraph 每次 ainvoke 都重建一份状态字典；替身若反复返回
            # 同一个对象，执行侧在单元收尾时释放状态引用就会跨调用串味（单测当场抓到）。
            return dict(response)

    return FakeApp()


# 方法作用：按列构造指定行数的结果行。
# Args: count - 行数；columns - 列名。
# Returns: 行字典列表。
def _rows(count: int, columns: tuple[str, ...] = ("employee_id", "project_id", "cost")) -> list[dict[str, Any]]:
    return [{name: index for name in columns} for index in range(count)]


# 方法作用：取出页面里某个单元 quality 块披露的限制条目。
# Args: artifact - 页面构建产物；unit_id - 单元标识。
# Returns: limitations 列表；该单元没有 quality 块时为空列表。
def _quality_limitations(artifact: Any, unit_id: str) -> list[str]:
    section = next(item for item in artifact.document.sections if item.id == unit_id)
    block = next((item for item in section.blocks if item.type == "quality"), None)
    return list(block.content.get("limitations", [])) if block is not None else []


class TestSampleGuardWiring:
    """覆盖 Task 3 收尾接线：提示词注册、超限决策落快照与页面披露。"""

    # 方法作用：验证 report.sample_guard 已注册且输出契约就是处置建议类。
    def test_prompt_registered_with_decision_contract(self) -> None:
        """提示词未注册时 invoke_structured 必然 KeyError，再被吞成保守 keep，表现成「什么都没发生」。"""
        # Arrange
        from src.llm.output_contracts import SampleDecision
        from src.llm.prompts import get_prompt_definition

        # Act
        definition = get_prompt_definition("report.sample_guard")

        # Assert
        assert definition.task == "report_sample_guard"
        assert definition.output_model is SampleDecision
        assert definition.context_budget >= 6000

    # 方法作用：验证探针文本含列清单与返回行数，且渲染过程不调用模型。
    def test_probe_contains_columns_and_counts(self) -> None:
        # Arrange
        from src.reporting.sample_guard import _render_probe

        # Act
        text = _render_probe(
            question="按员工、项目统计人力成本",
            columns=["employee_id", "project_id", "cost"],
            rows=[{"employee_id": 1, "project_id": 2, "cost": 3}],
            row_count=9000,
            full_count=9000,
            time_span="最近 90 天",
        )

        # Assert
        assert "employee_id" in text and "cost" in text
        assert "9000" in text
        assert "最近 90 天" in text

    # 方法作用：准备一条已接线的单元执行现场，模型调用被替换为记录型替身。
    # Args: monkeypatch - pytest 补丁工具；response - 假工作流返回结果；decision - 替身返回的处置建议。
    # Returns: (runner, store, unit, state, captured)。
    def _prepare(self, monkeypatch, response: dict, decision: Any):
        import src.graph.workflow as workflow_module
        import src.llm.client as llm_client
        import src.llm.invocation as invocation_module
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        monkeypatch.setattr(workflow_module, "app", _fake_workflow(response))
        captured: list[dict[str, Any]] = []

        # 方法作用：替换 invoke_structured，记录调用参数并按契约返回处置建议。
        # Args: prompt_id - 提示词标识；sections - 分区列表；kwargs - 其余调用参数。
        # Returns: 预置的处置建议。
        async def _invoke(prompt_id, sections, **kwargs):
            captured.append({"prompt_id": prompt_id, "sections": sections, **kwargs})
            return decision

        # 任务路由与模型可用性一律走替身，测试不发真实模型请求
        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)
        monkeypatch.setattr(invocation_module, "invoke_structured", _invoke)
        unit = AnalysisUnit(question="按员工、项目统计人力成本")
        plan = ReportPlan(requested_datasources=["oracle"], analysis_units=[unit])
        store = ReportStore(memory=True)
        runner = ReportRunner(store=store)
        state = {
            "plan": plan, "run_id": "run-guard-1", "report_id": "rep-guard-1",
            "identity": {"tenant_id": 3, "user_id": 7, "role": "analyst"},
            "authorized_datasources": {"oracle": {}},
        }
        return runner, store, unit, state, captured

    # 方法作用：从记录到的模型调用里筛出样本处置调用。
    # Args: captured - _prepare 记录到的调用列表。
    # Returns: 只含 report.sample_guard 的调用列表。
    @staticmethod
    def _guard_calls(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """单元执行链路还会调节点路由等提示词，断言必须只看样本处置这一支。"""
        return [item for item in captured if item.get("prompt_id") == "report.sample_guard"]

    # 方法作用：验证真实行数规模超限时模型决策进入 warnings、页面 quality 块与快照。
    @pytest.mark.asyncio
    async def test_overflow_decision_reaches_page_and_snapshot(self, monkeypatch) -> None:
        """进入链路 20 行、查询全量 9000 行：判据必须用真实规模而不是裁完的行数。"""
        # Arrange
        from src.llm.output_contracts import SampleDecision
        from src.reporting.document import build_report_document

        decision = SampleDecision(
            action="aggregate",
            reason="返回的是员工×项目明细，未聚合",
            suggested_group_by=["employee_id"],
        )
        runner, store, unit, state, captured = self._prepare(
            monkeypatch,
            {
                "final_response": {"success": True, "data": _rows(20)},
                "generated_sql": "SELECT employee_id, project_id, cost FROM t",
                "dialect": "mysql",
                "query_result_full_count": 9000,
            },
            decision,
        )

        # Act
        result = await runner._default_execute_unit(unit, state)

        # Assert：模型调用走的是已注册提示词，未注册会先在 invoke_structured 抛 KeyError
        guard = self._guard_calls(captured)
        assert guard, "超限单元必须触发一次样本处置调用"
        assert guard[0]["task"] == "report_sample_guard"
        assert guard[0]["output_model"] is SampleDecision
        # 探针里的两个计数必须各自如实：传成 row_count=max(...) 时会渲染出两个 9000
        probe = guard[0]["sections"][0].content
        assert "进入分析链路 20 行" in probe
        assert "查询全量 9000 行" in probe
        assert result.status == "success"
        assert any("补充聚合" in item for item in result.warnings)
        artifact = build_report_document(state["plan"], [result])
        assert any("补充聚合" in item for item in _quality_limitations(artifact, unit.unit_id))
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        rows = await store.list_unit_snapshots(state["run_id"], owner)
        recorded = rows[0]["params"]["sample_decision"]
        assert recorded["action"] == "aggregate"
        assert recorded["suggested_group_by"] == ["employee_id"]
        assert "补充聚合" in recorded["text"]

    # 方法作用：验证「上游裁剪过样本、真实行数未超限」时产生披露且不调用模型。
    @pytest.mark.asyncio
    async def test_sample_capped_within_limit_discloses_without_model(self, monkeypatch) -> None:
        """sample_capped 条款被删掉时本用例必红：判据不放行，页面与快照都不会有披露。"""
        # Arrange：进入链路 20 行、全量 200 行都在上限 5000 以下，只有 sample_capped 为真
        from src.llm.output_contracts import SampleDecision
        from src.reporting.document import build_report_document

        runner, store, unit, state, captured = self._prepare(
            monkeypatch,
            {
                "final_response": {"success": True, "data": _rows(20)},
                "generated_sql": "SELECT employee_id, project_id, cost FROM t",
                "dialect": "mysql",
                "query_result_full_count": 200,
                "query_result_sample_capped": True,
            },
            SampleDecision(action="aggregate", reason="模型不该被调用"),
        )

        # Act
        result = await runner._default_execute_unit(unit, state)

        # Assert：不烧模型调用，但裁剪事实以两个真实计数披露到页面与快照
        assert self._guard_calls(captured) == []
        assert result.status == "success"
        disclosure = next(item for item in result.warnings if "上游执行层已对样本做裁剪" in item)
        assert "本次进入分析链路 20 行" in disclosure
        assert "查询全量 200 行" in disclosure
        artifact = build_report_document(state["plan"], [result])
        assert any("上游执行层已对样本做裁剪" in item for item in _quality_limitations(artifact, unit.unit_id))
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        rows = await store.list_unit_snapshots(state["run_id"], owner)
        recorded = rows[0]["params"]["sample_decision"]
        assert recorded["action"] == "keep"
        assert "上游执行层已对样本做裁剪" in recorded["text"]

    # 方法作用：验证探针里的「进入分析链路」与「查询全量」是两个不同的真实数字。
    def test_probe_keeps_entering_and_full_counts_distinct(self) -> None:
        """执行层按上限先裁再回传，进入链路行数会小于全量；传同一个数会让模型拿到假证据。"""
        # Arrange
        from src.reporting.sample_guard import _render_probe

        # Act
        text = _render_probe(
            question="按员工、项目统计人力成本",
            columns=["employee_id", "cost"],
            rows=[{"employee_id": 1, "cost": 2}],
            row_count=5000,
            full_count=9000,
            time_span="最近 90 天",
        )

        # Assert
        assert "进入分析链路 5000 行" in text
        assert "查询全量 9000 行" in text

    # 方法作用：验证外部快速闸门与内层决策共用同一判据，不会放行后被卡死。
    def test_gate_and_decision_share_criterion(self) -> None:
        # Arrange
        from src.reporting.sample_guard import needs_sample_action

        # Assert：上游裁剪过但未超限 —— 判据必须放行，否则裁剪事实静默通过
        assert needs_sample_action(row_count=20, full_count=200, limit=5000, sample_capped=True) is True
        # 既未超限也未裁剪 —— 不放行
        assert needs_sample_action(row_count=20, full_count=200, limit=5000, sample_capped=False) is False
        # 进入链路被裁到 5000、全量 9000 —— 真实规模超限，放行
        assert needs_sample_action(row_count=5000, full_count=9000, limit=5000, sample_capped=False) is True
        # 上限未配置 —— 不决策，避免把 0 当上限把所有单元判成超限
        assert needs_sample_action(row_count=9000, full_count=9000, limit=0, sample_capped=True) is False

    # 方法作用：验证未超限的单元既不调用模型，也不产生任何处置披露。
    @pytest.mark.asyncio
    async def test_within_limit_skips_model_at_wiring(self, monkeypatch) -> None:
        # Arrange
        from src.llm.output_contracts import SampleDecision

        runner, _, unit, state, captured = self._prepare(
            monkeypatch,
            {"final_response": {"success": True, "data": _rows(20)}, "query_result_full_count": 20},
            SampleDecision(action="keep", reason="聚合结果，粒度合理"),
        )

        # Act
        result = await runner._default_execute_unit(unit, state)

        # Assert
        assert result.status == "success"
        assert self._guard_calls(captured) == []
        assert all("保留原始行集" not in item for item in result.warnings)

    # 方法作用：验证「产出行数超过本单元样本上限」这条判据同样触发决策。
    @pytest.mark.asyncio
    async def test_row_count_over_limit_triggers_decision(self, monkeypatch) -> None:
        # Arrange：把上限压低到 5 行，20 行的单元即属超限
        import src.config as config_module
        from src.llm.output_contracts import SampleDecision

        real_settings = config_module.get_settings()
        monkeypatch.setattr(
            config_module,
            "get_settings",
            lambda: real_settings.model_copy(update={"report_max_sample_rows": 5}),
        )
        runner, _, unit, state, captured = self._prepare(
            monkeypatch,
            {"final_response": {"success": True, "data": _rows(20)}, "query_result_full_count": 20},
            SampleDecision(action="aggregate", reason="明细未聚合", suggested_group_by=["cost"]),
        )

        # Act
        result = await runner._default_execute_unit(unit, state)

        # Assert
        assert self._guard_calls(captured), "超限单元必须触发一次样本处置调用"
        assert any("补充聚合" in item for item in result.warnings)

    # 方法作用：验证模型不可用时仍留下「已超限、未处理」的可解释披露。
    @pytest.mark.asyncio
    async def test_model_unavailable_still_discloses(self, monkeypatch) -> None:
        # Arrange
        import src.llm.client as llm_client
        from src.llm.output_contracts import SampleDecision

        runner, store, unit, state, captured = self._prepare(
            monkeypatch,
            {
                "final_response": {"success": True, "data": _rows(20)},
                "query_result_full_count": 9000,
                "query_result_sample_capped": True,
            },
            SampleDecision(),
        )
        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)

        # Act
        result = await runner._default_execute_unit(unit, state)

        # Assert：没走模型，但超限事实照样披露
        assert self._guard_calls(captured) == []
        assert any("9000" in item and "保留原始行集" in item for item in result.warnings)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        rows = await store.list_unit_snapshots(state["run_id"], owner)
        assert rows[0]["params"]["sample_decision"]["action"] == "keep"

    # 方法作用：验证决策抛异常时单元照常产出、页面照常生成且不判 failed。
    @pytest.mark.asyncio
    async def test_decision_failure_keeps_unit_healthy(self, monkeypatch) -> None:
        # Arrange
        from src.llm.output_contracts import SampleDecision
        from src.reporting import sample_guard as sample_guard_module
        from src.reporting.document import build_report_document

        runner, _, unit, state, _ = self._prepare(
            monkeypatch,
            {
                "final_response": {"success": True, "data": _rows(20)},
                "query_result_full_count": 9000,
                "query_result_sample_capped": True,
            },
            SampleDecision(),
        )

        # 方法作用：模拟决策链路抛出未预期异常。
        # Args: kwargs - 决策入口的关键字参数。
        # Returns: 不返回，直接抛出。
        async def _boom(**kwargs):
            raise RuntimeError("decision exploded")

        monkeypatch.setattr(sample_guard_module, "decide_sample_action", _boom)

        # Act
        result = await runner._default_execute_unit(unit, state)

        # Assert
        assert result.status == "success"
        assert result.status != "failed"
        assert not result.warnings
        artifact = build_report_document(state["plan"], [result])
        assert any(item.id == unit.unit_id for item in artifact.document.sections)


    # 以下用例覆盖截断原因与真实切割点同源：上限必须来自配置，不能是页面渲染的另一个数字。

    # 方法作用：验证截断原因里的上限就是配置的样本上限，而不是已被淘汰的硬编码值。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_truncation_reason_uses_configured_row_limit(self, monkeypatch) -> None:
        """进入链路行数 = 生效样本上限、全量再高出 4000 行时，原因里的上限必须写真实上限而不是 200。

        回归：runner 曾把页面渲染用的 200 当成本次样本切割点写进原因，快照抽屉会
        出现「返回 900 行、进入分析 800 行（上限 200）」这种自相矛盾的数字。
        """
        # Arrange：执行层按**生效上限**先裁再回传，全量比上限多 4000 行。
        # 这里不再把配置压到 500 来绕开契约上限：进入链路的行数直接取真实上限，
        # 顺带锁住「执行层回传的整批行数必须能被 UnitResult 接受」这条可达性。
        from src.llm.output_contracts import SampleDecision
        from src.reporting.runner import _sample_row_limit

        limit = _sample_row_limit()
        # 旧契约上限 1000：上限本身落在它之上，本用例才真正走到「契约覆盖配置」的路径
        assert limit > 1000, f"生效样本上限 {limit} 未超过旧契约上限，回归覆盖不到真实路径"
        full_count = limit + 4000
        runner, store, unit, state, _ = self._prepare(
            monkeypatch,
            {
                "final_response": {"success": True, "data": _rows(limit)},
                "generated_sql": "SELECT employee_id, project_id, cost FROM t",
                "dialect": "mysql",
                "query_result_full_count": full_count,
                "query_result_sample_capped": True,
            },
            SampleDecision(action="keep", reason="聚合结果，粒度合理"),
        )

        # Act
        result = await runner._default_execute_unit(unit, state)

        # Assert
        assert result.status == "success"
        assert len(result.rows) == limit
        assert result.truncated is True
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        rows = await store.list_unit_snapshots(state["run_id"], owner)
        reason = rows[0]["truncated_reason"]
        assert f"生效样本上限 {limit} 行" in reason
        # 旧口径的措辞不得再出现：断言「页面展示」而不是数字 200——
        # 数字会随配置变化（1200/2000/3200 都含 "200"），按数字断言会假红；
        # 「上限是否真是配置值」由上面那条正向断言负责，不依赖数字巧合。
        assert "页面展示" not in reason


class TestContractRowCapCoversSampleLimit:
    """覆盖 Task 3 暴露的跨契约矛盾：UnitResult.rows 的静态上限必须覆盖可配的样本上限。"""

    # 方法作用：校验契约静态上限、配置合法上限与配置默认值三者不脱钩。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_row_cap_is_not_smaller_than_configured_max(self) -> None:
        """契约写死 1000、配置 le 放到 100000（默认 5000）时两侧脱钩，没有任何测试会响。

        后果：执行层回传 1001~5000 行时构造 UnitResult 抛 ValidationError，单元被判
        UNIT_EXECUTION_FAILED；改动前（硬编码 200）这些单元本来是正常的。
        """
        # Arrange
        from src.config import Settings
        from src.reporting.models import UnitResult

        config_field = Settings.model_fields["report_max_sample_rows"]
        config_max = max(int(item.le) for item in config_field.metadata if hasattr(item, "le"))
        rows_field = UnitResult.model_fields["rows"]
        contract_max = max(int(item.max_length) for item in rows_field.metadata if hasattr(item, "max_length"))

        # Assert：语义不变量 —— 配置的合法上限必须落在契约静态上限之内
        assert config_max <= contract_max
        # 单一真源：与 _UNIT_COUNT_STATIC_MAX / _QUERY_COUNT_STATIC_MAX 同惯例严格对齐，
        # 任一侧被单独改小（或改大）都会立刻变红
        assert contract_max == config_max
        # 默认值必须可达：默认 5000 被旧上限 1000 判失败，正是本用例要拦住的那一类回归
        assert int(config_field.default) <= contract_max

    # 方法作用：验证默认样本上限规模的结果行能通过契约校验。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_default_sample_limit_rows_pass_contract(self) -> None:
        """默认 5000 行恰好落在旧契约上限 1000 之上：契约未放开时此处抛 ValidationError。"""
        # Arrange
        from src.config import Settings
        from src.reporting.models import UnitResult

        default_limit = int(Settings.model_fields["report_max_sample_rows"].default)

        # Act
        result = UnitResult(unit_id="unit_contract_rows", rows=_rows(default_limit))

        # Assert
        assert len(result.rows) == default_limit
        # 列集合由行集推导，行数放开后这条推导链路同样要能用
        assert result.columns == ["employee_id", "project_id", "cost"]


class TestOversampleRetryQuery:
    """覆盖上卷重查文本：必须给出具体目标行数，否则模型只删一个维度、回来仍被裁。"""

    # 方法作用：验证重查文本带上具体目标行数而不是"控制在上限以内"。
    def test_retry_query_states_concrete_target(self) -> None:
        """实机：5 个单元重查后仍被裁，根因是要求太松。"""
        # Arrange
        from src.reporting.runner import _oversample_retry_query

        # Act
        text = _oversample_retry_query(question="按年月、地区、门店统计净销售额", full_count=19925, limit=5000)

        # Assert
        assert "500" in text, "limit=5000 时目标应为 limit//10=500 行"
        assert "19925" in text and "5000" in text
        # 不许用 LIMIT 掩盖截断
        assert "LIMIT" in text

    # 方法作用：验证目标行数有下限，避免把聚合逼到只剩几个点。
    def test_retry_target_has_floor(self) -> None:
        # Arrange
        from src.reporting.runner import _oversample_retry_query

        # Act
        text = _oversample_retry_query(question="统计", full_count=100, limit=100)

        # Assert：100//10=10 低于下限 50，应取 50。
        assert "50 行以内" in text

    # 方法作用：验证上限未知时仍给出可用目标而不是 0。
    def test_retry_target_when_limit_unknown(self) -> None:
        # Arrange
        from src.reporting.runner import _oversample_retry_query

        # Act
        text = _oversample_retry_query(question="统计", full_count=8000, limit=0)

        # Assert
        assert "500 行以内" in text


class TestAggregationRequirementBudget:
    """覆盖取数要求的行数预算：含糊的"数百行以内"挡不住明细级 SQL。"""

    # 方法作用：验证取数要求带具体行数预算，且预算由样本上限推出。
    def test_requirement_states_concrete_budget(self) -> None:
        """实机：两个单元第一次就写成明细粒度（19925 / 27364 行）。"""
        # Arrange
        from src.reporting.runner import _unit_aggregation_requirement

        # Act
        text = _unit_aggregation_requirement()

        # Assert：默认上限 5000 → 预算 500
        assert "500 行以内" in text
        assert "数百行" not in text
        # 仍然要求库内聚合、并明确禁止靠上层截断兜底
        assert "GROUP BY" in text
        assert "截断" in text

    # 方法作用：验证重查文本给出"按主指标取前 N 名"这条出路。
    def test_retry_offers_ranked_top_n(self) -> None:
        """粒度必须保留时（每商品/每门店），排序前 N 名有业务含义，截断留下的前 N 行没有。"""
        # Arrange
        from src.reporting.runner import _oversample_retry_query

        # Act
        text = _oversample_retry_query(question="按商品统计加权平均成本", full_count=27364, limit=5000)

        # Assert
        assert "按主指标降序取前" in text
        assert "前 500 名" in text


class TestReleaseUnitRowsAfterPublish:
    """覆盖发布后释放行数据：这是内存不回落的主要来源（实机 7 万~14 万个 Decimal）。"""

    # 方法作用：验证已发布的行数据被清空且返回释放统计。
    def test_rows_are_cleared(self) -> None:
        # Arrange
        from src.reporting.models import UnitResult
        from src.reporting.runner import _release_unit_rows

        first = UnitResult(unit_id="u1", rows=[{"a": 1}, {"a": 2}])
        second = UnitResult(unit_id="u2", rows=[{"b": 3}])
        empty = UnitResult(unit_id="u3", rows=[])

        # Act
        released = _release_unit_rows([first, second, empty, "not-a-unit"])

        # Assert
        assert released == 2
        assert first.rows == [] and second.rows == []
        # 空结果不算释放，非 UnitResult 元素被忽略而不抛错
        assert empty.rows == []

    # 方法作用：验证列表为空时是空操作。
    def test_empty_list_is_noop(self) -> None:
        # Arrange
        from src.reporting.runner import _release_unit_rows

        # Act / Assert
        assert _release_unit_rows([]) == 0

    # 方法作用：验证发布之后才释放（发布前调会清掉页面要用的行）。
    def test_release_happens_after_publish(self) -> None:
        # Arrange
        import inspect

        from src.reporting import runner as runner_module

        source = inspect.getsource(runner_module.ReportRunner.run_job)

        # Assert
        assert "_release_unit_rows" in source
        assert source.index("persist_version") < source.index("_release_unit_rows")

    # 方法作用：验证图状态里的行数据通道被清空，标量字段保持可读。
    def test_state_payloads_are_cleared(self) -> None:
        """单元 rows 清空后驻留只降四成，剩下的大头挂在图状态的取数通道上。"""
        # Arrange
        from src.reporting.runner import _release_state_payloads

        state = {
            "query_result": [{"a": 1}, {"a": 2}],
            "query_result_sample": [{"b": 3}],
            "analysis_result": {"data": [{"c": 4}]},
            "artifact": object(),
            "run_status": "succeeded",
            "summary": "关键结论",
        }

        # Act
        cleared = _release_state_payloads(state)

        # Assert：容器就地清空（别的持有者也能真正释放行），摘要字段原样保留
        assert set(cleared) == {"query_result", "query_result_sample", "analysis_result", "artifact"}
        assert state["query_result"] == []
        assert state["analysis_result"] == {}
        assert state["artifact"] is None
        assert state["run_status"] == "succeeded" and state["summary"] == "关键结论"

    # 方法作用：验证非字典状态是空操作。
    def test_non_dict_state_is_noop(self) -> None:
        # Arrange
        from src.reporting.runner import _release_state_payloads

        # Act / Assert
        assert _release_state_payloads(None) == []
        assert _release_state_payloads([1, 2]) == []

    # 方法作用：验证图状态的行数据也在发布之后才释放。
    def test_state_release_also_after_publish(self) -> None:
        # Arrange
        import inspect

        from src.reporting import runner as runner_module

        source = inspect.getsource(runner_module.ReportRunner.run_job)

        # Assert
        assert "_release_state_payloads" in source
        assert source.index("persist_version") < source.index("_release_state_payloads")


class TestUnitStateReleaseKeepsRows:
    """覆盖单元收尾时的状态释放：只放状态里的引用，不能把单元结果的行清掉。"""

    # 方法作用：验证释放状态引用后，单元结果仍带着完整行数据。
    async def test_unit_result_keeps_rows_after_state_release(self, monkeypatch) -> None:
        """实机风险点：rows 很可能就是 state["query_result_sample"] 这同一个列表。"""
        # Arrange
        import src.graph.workflow as workflow_module
        from src.reporting.models import AnalysisUnit, TimeRange, UnitResult
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        rows = [{f"c{index}": index for index in range(5)} for _ in range(5000)]
        state_payload = {
            "generated_sql": "SELECT 1",
            "dialect": "postgres",
            "query_result_sample": rows,
            "query_result_full_count": 5000,
            "query_result_sample_capped": False,
            "final_response": {"success": True, "data": rows, "analysis": {}},
        }

        returned: list[dict[str, Any]] = []

        class _FakeApp:
            async def ainvoke(self, payload, config):
                state = dict(state_payload)
                returned.append(state)
                return state

        monkeypatch.setattr(workflow_module, "app", _FakeApp())
        runner = ReportRunner(store=ReportStore(memory=True))

        unit = AnalysisUnit(
            unit_id="u1",
            question="按员工统计人力成本",
            datasource="postgres",
            time_range=TimeRange(kind="absolute", value="2016-01-01/2016-12-31"),
        )
        run_state = {
            "plan": SimpleNamespace(requested_datasources=["postgres"]),
            "identity": {"tenant_id": 3, "user_id": 3, "role": "tenant_admin"},
            "authorized_datasources": {"postgres": {"allowed_columns": [], "row_filter_sql": ""}},
            "run_id": "run-1",
        }

        # Act
        result = await runner._execute_unit_workflow(unit, dict(run_state), {})

        # Assert：行数据完整保留在单元结果里（调用方要继续用），而图状态里那一份引用
        # 已经被断开——两份引用收敛成一份，才是释放内存的同时不丢数据。
        assert isinstance(result, UnitResult)
        assert len(result.rows) == 5000
        assert result.rows[0] == {"c0": 0, "c1": 1, "c2": 2, "c3": 3, "c4": 4}
        assert returned, "假图必须被调用到"
        assert returned[0]["query_result_sample"] == []
        assert returned[0]["final_response"] in ({}, None) or returned[0]["final_response"]
