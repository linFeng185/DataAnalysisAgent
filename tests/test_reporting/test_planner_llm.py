"""报表工作台计划解析的结构化 LLM 规划与降级测试。"""

from __future__ import annotations


class TestReportPlannerLlmUnits:
    """覆盖功能 23.1：结构化 LLM 业务语义拆解与规则回退。"""

    # 方法作用：验证模型给出的合规分析单元被采用，且规则安全字段不被覆盖。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_llm_units_replace_rule_units(self) -> None:
        """模型返回合规单元时应替换规则单元，并保留调度与数据源约束。"""
        # Arrange
        from src.reporting.planner import ReportPlanner

        class FakePlanner:
            # 方法作用：返回两个语义清晰的分析单元建议。
            # Args: prompt - 规则解析传入的用户文本。
            # Returns: 含两个单元的模型输出字典。
            async def ainvoke(self, prompt: str) -> dict[str, object]:
                return {
                    "analysis_units": [
                        {"unit_id": "sales", "question": "销售按年月与地区分析", "metrics": ["销售额"]},
                        {"unit_id": "finance", "question": "订单净额与发票金额差额核对"},
                    ]
                }

        # Act
        plan = await ReportPlanner(FakePlanner()).parse("定时生成销售和财务报表", datasources=["sales"])

        # Assert
        assert [unit.unit_id for unit in plan.analysis_units] == ["sales", "finance"]
        assert plan.analysis_units[0].metrics == ["销售额"]
        assert plan.requested_datasources == ["sales"]
        assert plan.execution_mode == "scheduled"
        assert not any("模型规划不可用" in warning for warning in plan.warnings)

    # 方法作用：验证模型单元部分校验失败时公开候选数与拒绝数。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值。
    async def test_partially_invalid_llm_units_are_disclosed(self) -> None:
        """保留合法单元时也必须说明另一个候选被服务端拒绝，不能静默消失。"""
        # Arrange
        from src.reporting.planner import ReportPlanner

        class FakePlanner:
            # 方法作用：返回一个合法单元和一个优先级非法的单元。
            # Args: prompt - 规则解析传入的用户文本。
            # Returns: 含部分非法单元的模型输出字典。
            async def ainvoke(self, prompt: str) -> dict[str, object]:
                return {
                    "analysis_units": [
                        {"unit_id": "sales", "question": "销售趋势"},
                        {"unit_id": "risk", "question": "风险分析", "priority": "tertiary"},
                    ]
                }

        # Act
        plan = await ReportPlanner(FakePlanner()).parse("生成销售与风险报表", datasources=["sales"])

        # Assert
        assert [unit.unit_id for unit in plan.analysis_units] == ["sales"]
        assert any("2" in warning and "1" in warning and "未通过服务端校验" in warning for warning in plan.warnings)

    # 方法作用：验证模型返回空单元时保留规则拆分并给出降级提示。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_empty_llm_units_fall_back_with_warning(self) -> None:
        """模型未给出任何单元时应回退规则拆分并标注降级。"""
        # Arrange
        from src.reporting.planner import ReportPlanner

        class FakePlanner:
            # 方法作用：返回空的分析单元列表。
            # Args: prompt - 规则解析传入的用户文本。
            # Returns: 空单元输出。
            async def ainvoke(self, prompt: str) -> dict[str, object]:
                return {"analysis_units": []}

        # Act
        plan = await ReportPlanner(FakePlanner()).parse("生成销售报表", datasources=["sales"])

        # Assert
        assert len(plan.analysis_units) >= 1
        assert any("模型规划不可用" in warning for warning in plan.warnings)

    # 方法作用：验证模型调用抛异常时不影响计划生成。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_llm_exception_falls_back_with_warning(self) -> None:
        """模型超时或抛异常时应回退规则拆分并标注降级。"""
        # Arrange
        from src.reporting.planner import ReportPlanner

        class FakePlanner:
            # 方法作用：模拟模型调用失败。
            # Args: prompt - 规则解析传入的用户文本。
            # Returns: 不返回，直接抛出超时异常。
            async def ainvoke(self, prompt: str) -> dict[str, object]:
                raise TimeoutError("模型响应超时")

        # Act
        plan = await ReportPlanner(FakePlanner()).parse("生成销售报表", datasources=["sales"])

        # Assert
        assert len(plan.analysis_units) >= 1
        assert any("模型规划不可用" in warning for warning in plan.warnings)

    # 方法作用：验证模型给出越权数据源时整体回退，不扩大执行范围。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_out_of_scope_datasource_falls_back(self) -> None:
        """模型绑定未授权数据源时应回退规则计划并标注降级。"""
        # Arrange
        from src.reporting.planner import ReportPlanner

        class FakePlanner:
            # 方法作用：返回绑定未授权数据源的单元。
            # Args: prompt - 规则解析传入的用户文本。
            # Returns: 含越权数据源的模型输出字典。
            async def ainvoke(self, prompt: str) -> dict[str, object]:
                return {"analysis_units": [{"unit_id": "sales", "question": "销售分析", "datasource": "crm"}]}

        # Act
        plan = await ReportPlanner(FakePlanner()).parse("生成销售报表", datasources=["sales"])

        # Assert
        assert plan.requested_datasources == ["sales"]
        assert all(unit.datasource in {"", "sales"} for unit in plan.analysis_units)
        assert any("模型规划不可用" in warning for warning in plan.warnings)

    # 方法作用：验证模型给出超过上限的单元数量时被截断并公开披露。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_excess_llm_units_are_truncated(self, monkeypatch) -> None:
        """模型返回超过配置上限的单元时应截断，保证配额不被突破。"""
        # Arrange
        from src.config import get_settings
        from src.reporting.planner import ReportPlanner

        # 上限来自配置（app.yaml），测试显式钉住它，避免随部署配置漂移而失去约束语义。
        monkeypatch.setattr(get_settings(), "report_max_units_per_run", 12)

        class FakePlanner:
            # 方法作用：返回 15 个编号唯一的分析单元。
            # Args: prompt - 规则解析传入的用户文本。
            # Returns: 超量单元输出。
            async def ainvoke(self, prompt: str) -> dict[str, object]:
                return {
                    "analysis_units": [
                        {"unit_id": f"unit_{index}", "question": f"第 {index} 个业务问题"}
                        for index in range(1, 16)
                    ]
                }

        # Act
        plan = await ReportPlanner(FakePlanner()).parse("生成经营全景报表", datasources=["sales"])

        # Assert
        assert len(plan.analysis_units) == 12
        assert any("15" in warning and "12" in warning and "分析单元" in warning for warning in plan.warnings)

    # 方法作用：验证规则拆分超过单元上限时同样留下裁剪披露。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_excess_rule_units_are_truncated_with_warning(self, monkeypatch) -> None:
        """模型降级后的规则路径也不能把尾部章节无声丢掉。"""
        # Arrange
        from src.config import get_settings
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(get_settings(), "report_max_units_per_run", 2)
        warnings: list[str] = []
        prompt = "一、销售分析\n二、库存分析\n三、客户分析"

        # Act
        units = ReportPlanner._build_units(prompt, warnings=warnings)

        # Assert
        assert len(units) == 2
        assert any("3" in warning and "2" in warning and "分析单元" in warning for warning in warnings)

    # 方法作用：验证模型成功替换规则单元后不会遗留规则路径的裁剪提示。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_successful_model_replacement_removes_rule_trim_warning(self, monkeypatch) -> None:
        """最终只采用一个模型单元时，页面不能仍声称规则拆分丢了第三个单元。"""
        # Arrange
        from src.config import get_settings
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(get_settings(), "report_max_units_per_run", 2)

        class FakePlanner:
            # 方法作用：把三段规则输入收敛成一个完整业务单元。
            # Args: prompt - 规则解析传入的用户文本。
            # Returns: 仅含一个最终分析单元的模型输出。
            async def ainvoke(self, prompt: str) -> dict[str, object]:
                return {"analysis_units": [{"unit_id": "overview", "question": "经营综合分析"}]}

        # Act
        plan = await ReportPlanner(FakePlanner()).parse(
            "一、销售分析\n二、库存分析\n三、客户分析",
            datasources=["sales"],
        )

        # Assert
        assert [unit.unit_id for unit in plan.analysis_units] == ["overview"]
        assert not any("需求共拆分出" in warning for warning in plan.warnings)

    # 方法作用：验证模型给出的非法单元标识被替换为安全语义标识。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_invalid_unit_id_is_replaced(self) -> None:
        """模型输出中文标识时应替换为合规标识而不是整批回退。"""
        # Arrange
        from src.reporting.planner import ReportPlanner

        class FakePlanner:
            # 方法作用：返回中文 unit_id 的单元。
            # Args: prompt - 规则解析传入的用户文本。
            # Returns: 标识非法的模型输出字典。
            async def ainvoke(self, prompt: str) -> dict[str, object]:
                return {"analysis_units": [{"unit_id": "销售单元", "question": "销售趋势分析"}]}

        # Act
        plan = await ReportPlanner(FakePlanner()).parse("生成销售报表", datasources=["sales"])

        # Assert
        assert [unit.unit_id for unit in plan.analysis_units] == ["sales"]
        assert not any("模型规划不可用" in warning for warning in plan.warnings)

    # 方法作用：验证模型在授权集合内猜测的数据源被清空，交给执行期定位。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_guessed_datasource_is_cleared(self) -> None:
        """模型猜的数据源若未在原文点名，必须清空为执行期检索。"""
        # Arrange
        from src.reporting.planner import ReportPlanner

        class FakePlanner:
            # 方法作用：返回绑定了授权集合内、但用户未点名的数据源。
            # Args: prompt - 规则解析传入的用户文本。
            # Returns: 含猜测数据源的模型输出字典。
            async def ainvoke(self, prompt: str) -> dict[str, object]:
                return {"analysis_units": [{"unit_id": "sales", "question": "销售趋势", "datasource": "oracle"}]}

        # Act
        plan = await ReportPlanner(FakePlanner()).parse("生成销售报表", datasources=["oracle", "ch"])

        # Assert
        assert [unit.datasource for unit in plan.analysis_units] == [""]
        assert not any("模型规划不可用" in warning for warning in plan.warnings)

    # 方法作用：验证用户在原文点名的数据源被保留。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_datasource_named_by_user_is_kept(self) -> None:
        """用户明确写了数据源名称时应保留该绑定，不退化为多源检索。"""
        # Arrange
        from src.reporting.planner import ReportPlanner

        class FakePlanner:
            # 方法作用：返回与用户原文一致的数据源。
            # Args: prompt - 规则解析传入的用户文本。
            # Returns: 含点名数据源的模型输出字典。
            async def ainvoke(self, prompt: str) -> dict[str, object]:
                return {"analysis_units": [{"unit_id": "sales", "question": "销售趋势", "datasource": "oracle"}]}

        # Act
        plan = await ReportPlanner(FakePlanner()).parse("从 oracle 里生成销售报表", datasources=["oracle", "ch"])

        # Assert
        assert [unit.datasource for unit in plan.analysis_units] == ["oracle"]


class TestReportPlannerTaskRouting:
    """覆盖功能 23.1：统一 LLM 任务路由与不可用时的降级。"""

    # 方法作用：验证规划任务不可用时回退规则拆分，不抛出异常。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unavailable_task_falls_back(self, monkeypatch) -> None:
        """report_plan 任务无可用模型时应静默回退规则拆分。"""
        # Arrange
        import src.llm.client as llm_client
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)

        # Act
        plan = await ReportPlanner().parse("生成销售报表", datasources=["sales"])

        # Assert
        assert len(plan.analysis_units) >= 1
        assert any("模型规划不可用" in warning for warning in plan.warnings)

    # 方法作用：验证任务可用时走统一结构化调用并采用其返回单元。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_available_task_uses_structured_invocation(self, monkeypatch) -> None:
        """任务可用时应经 invoke_structured 取得单元并替换规则单元。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.llm.output_contracts import ReportPlanUnitsOutput
        from src.reporting.planner import ReportPlanner

        calls: list[dict[str, object]] = []
        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)

        # 方法作用：记录调用参数并返回固定的结构化单元。
        # Args: prompt_id - 注册的 Prompt 标识；sections - 提示段落；kwargs - 透传参数。
        # Returns: 含单个分析单元的输出契约实例。
        async def fake_invoke_structured(prompt_id, sections, **kwargs):
            calls.append({"prompt_id": prompt_id, **kwargs})
            return ReportPlanUnitsOutput(
                analysis_units=[{"unit_id": "sales", "question": "销售按年月分析"}]
            )

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke_structured)

        # Act
        plan = await ReportPlanner().parse("生成销售报表", datasources=["sales"])

        # Assert
        assert [unit.unit_id for unit in plan.analysis_units] == ["sales"]
        assert calls and calls[0]["prompt_id"] == "report.plan"
        assert calls[0]["task"] == "report_plan"
        assert not any("模型规划不可用" in warning for warning in plan.warnings)


class TestPlanUnitLimitContract:
    """覆盖报表规划输出的单元数上限：必须由配置决定，不能写死。

    2026-09-14 实测事故：`ReportPlanUnitsOutput.analysis_units` 写死 `max_length=12`，
    用户明确要求 16 个单元时模型**正确拆出了 16 个**，却在契约校验处被拒收：
        List should have at most 12 items after validation, not 16
    异常被降级捕获 → 回退规则拆分 → 产出「经营总览】」这类标题残渣与 `unit_1`
    这类无语义标识。模型那条链路从未生效，与 `query_count le=64` 是同一个坑换字段。
    """

    # 方法作用：验证契约接受与配置上限同量级的单元数。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_output_contract_accepts_units_within_static_ceiling(self) -> None:
        """16 个单元必须能通过契约校验——这正是被 too_long 拒收的量。"""
        # Arrange
        from src.llm.output_contracts import ReportPlanUnitsOutput

        units = [
            {"unit_id": f"unit_{index}", "question": f"第 {index} 个业务问题"}
            for index in range(1, 17)
        ]

        # Act
        output = ReportPlanUnitsOutput(analysis_units=units)

        # Assert
        assert len(output.analysis_units) == 16

    # 方法作用：验证契约上界不低于全局单元数静态上界。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_output_contract_ceiling_covers_static_unit_ceiling(self) -> None:
        """业务上限由调用方 max_plan_units() 裁剪；契约只做理智兜底，不能更窄。"""
        # Arrange
        from annotated_types import MaxLen

        from src.llm.output_contracts import ReportPlanUnitsOutput
        from src.reporting.models import _UNIT_COUNT_STATIC_MAX

        # Act
        ceilings = [
            meta.max_length
            for meta in ReportPlanUnitsOutput.model_fields["analysis_units"].metadata
            if isinstance(meta, MaxLen)
        ]

        # Assert
        assert ceilings, "analysis_units 缺少 max_length 约束"
        assert ceilings[0] >= _UNIT_COUNT_STATIC_MAX, (
            f"契约上界 {ceilings[0]} 小于全局静态上界 {_UNIT_COUNT_STATIC_MAX}，"
            "配置允许的单元数会被契约提前拒收并静默回退规则拆分"
        )

    # 方法作用：验证模型拆出 16 个单元时不再降级为规则拆分。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_sixteen_units_from_model_do_not_fall_back(self, monkeypatch) -> None:
        """复现事故：模型给出 16 个单元却因契约拒收而整批降级。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.llm.output_contracts import ReportPlanUnitsOutput
        from src.reporting.models import max_plan_units
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)
        calls: list[dict[str, object]] = []

        # 方法作用：返回 16 个单元，并把调用参数记录下来。
        # Args: prompt_id - 注册 Prompt 标识；sections - 提示段落；kwargs - 透传参数。
        # Returns: 含 16 个分析单元的输出契约实例。
        async def fake_invoke_structured(prompt_id, sections, **kwargs):
            calls.append({"prompt_id": prompt_id, **kwargs})
            return ReportPlanUnitsOutput(
                analysis_units=[
                    {"unit_id": f"unit_{index}", "question": f"第 {index} 个业务问题"}
                    for index in range(1, 17)
                ]
            )

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke_structured)

        # Act
        plan = await ReportPlanner().parse("生成经营全景报表", datasources=["sales"])

        # Assert
        assert len(plan.analysis_units) == 16, "模型拆出的单元被契约或调用方截断"
        assert not any("模型规划不可用" in warning for warning in plan.warnings)
        assert [unit.unit_id for unit in plan.analysis_units][0] == "unit_1"
        # 提示词的单元上限随配置下发，不能在模板里写死
        assert calls and calls[0]["system_values"]["max_units"] == str(max_plan_units())
