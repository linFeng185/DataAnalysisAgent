"""分析节点 Skill 工具执行回归测试。"""

from __future__ import annotations

import re
from decimal import Decimal
from unittest.mock import AsyncMock


class _FakeTool:
    """最小异步工具替身，不调用真实模型或外部服务。"""

    # 方法作用：构造带固定异步返回值的测试工具。
    # Args: self - 工具实例；name - 工具名；value - 调用返回值。
    # Returns: 无返回值。
    def __init__(self, name: str, value):
        self.name = name
        self.ainvoke = AsyncMock(return_value=value)


class TestAnalyzeResultSkillTools:
    """覆盖分析后工具可达性和请求级预算。"""

    # 方法作用：验证报告 Skill 能消费分析负载并写回渲染结果。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_report_tool_is_executed_with_analysis_payload(self) -> None:
        """custom-report 的渲染工具应在分析完成后真实执行。"""
        # Arrange
        from src.graph.nodes.analyze_result import _execute_post_analysis_skill_tools

        report_tool = _FakeTool("render_report", "# 周报")
        result = {"summary": "销售上升", "insights": ["趋势向上"]}
        rows = [{"amount": 12}]
        state = {
            "user_query": "生成周报",
            "skill_tools": [report_tool],
            "skill_tool_budget": 2,
            "skill_tool_calls": 0,
        }

        # Act
        enriched, calls = await _execute_post_analysis_skill_tools(result, rows, state)

        # Assert
        assert calls == 1
        assert enriched["rendered_report"] == "# 周报"
        report_tool.ainvoke.assert_awaited_once()
        payload = report_tool.ainvoke.await_args.args[0]
        assert payload["template"] == "weekly_report"
        assert payload["data"]["summary"] == "销售上升"

    # 方法作用：验证报告指标覆盖全部结果行而不是只读取第一行。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_report_metrics_include_all_result_rows(self) -> None:
        """分组查询的后续行必须进入报告，否则报告会静默遗漏业务结果。"""
        # Arrange
        from src.graph.nodes.analyze_result import _execute_post_analysis_skill_tools

        report_tool = _FakeTool("render_report", "# 周报")
        rows = [
            {"region": "华东", "amount": 12},
            {"region": "华南", "amount": 18},
        ]

        # Act
        await _execute_post_analysis_skill_tools(
            {"summary": "区域销售", "insights": []},
            rows,
            {
                "user_query": "生成周报",
                "skill_tools": [report_tool],
                "skill_tool_budget": 1,
                "skill_tool_calls": 0,
            },
        )

        # Assert
        metrics = report_tool.ainvoke.await_args.args[0]["data"]["metrics"]
        assert metrics == {"华东 / amount": 12, "华南 / amount": 18}

    # 方法作用：验证已用调用数达到预算后不再执行额外工具。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_skill_tool_budget_blocks_extra_calls(self) -> None:
        """预算耗尽后不得继续调用已授权工具。"""
        # Arrange
        from src.graph.nodes.analyze_result import _execute_post_analysis_skill_tools

        null_tool = _FakeTool("check_null_rate", {"null_rate": 0})
        result = {"summary": "ok"}
        rows = [{"a": 1, "b": 2, "c": 3}]
        state = {
            "skill_tools": [null_tool],
            "skill_tool_budget": 1,
            "skill_tool_calls": 1,
        }

        # Act
        enriched, calls = await _execute_post_analysis_skill_tools(result, rows, state)

        # Assert
        assert calls == 1
        assert "skill_outputs" not in enriched
        null_tool.ainvoke.assert_not_awaited()

    # 方法作用：验证显式零预算不会被解释为默认工具额度。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_zero_skill_tool_budget_blocks_all_calls(self) -> None:
        """Manifest 将工具预算设为零时必须完全禁止调用。"""
        # Arrange
        from src.graph.nodes.analyze_result import _execute_post_analysis_skill_tools

        report_tool = _FakeTool("render_report", "不应生成")
        state = {
            "user_query": "生成周报",
            "skill_tools": [report_tool],
            "skill_tool_budget": 0,
            "skill_tool_calls": 0,
        }

        # Act
        enriched, calls = await _execute_post_analysis_skill_tools(
            {"summary": "原始摘要"},
            [{"amount": 12}],
            state,
        )

        # Assert
        assert calls == 0
        assert enriched == {"summary": "原始摘要"}
        report_tool.ainvoke.assert_not_awaited()


class TestAnalyzeResultDecimal:
    """覆盖数据库 Decimal 在规则分析分组中的数值识别。"""

    # 方法作用：验证规则分组正确识别并累计 Decimal 指标。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_group_by_includes_decimal_values(self) -> None:
        """Decimal 指标不能被静默当成非数值并汇总为零。"""
        # Arrange
        from src.graph.nodes.analyze_result import _group_by

        rows = [
            {"category": "A", "amount": Decimal("1.25")},
            {"category": "A", "amount": Decimal("2.75")},
            {"category": "B", "amount": Decimal("3.00")},
        ]

        # Act
        grouped = _group_by(rows, "category", "amount")

        # Assert
        assert grouped == [("A", 4.0), ("B", 3.0)]


class TestAnalyzeResultFallback:
    """覆盖 LLM 分析失败后的规则回退上下文。"""

    # 方法作用：验证模型失败时规则分析保留调用方计算出的图表类型和真实意图。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_llm_failure_preserves_chart_type_and_intent(self, monkeypatch) -> None:
        """趋势问题失败回退不能静默降级为普通表格查询。"""
        # Arrange
        import src.graph.nodes.analyze_result as analyze_module

        captured: dict[str, str] = {}

        # 方法作用：记录规则回退实际收到的图表类型和意图。
        # Args: rows/stats/trend/outlier/conc - 规则输入；chart_type - 图表类型；intent - 真实意图。
        # Returns: 包含推荐图表类型的最小分析结果。
        def fallback(rows, stats, trend, outlier, conc, chart_type, intent):
            del rows, stats, trend, outlier, conc
            captured.update(chart_type=chart_type, intent=intent)
            return {"recommended_chart_type": chart_type}

        monkeypatch.setattr(
            analyze_module,
            "get_llm",
            lambda temperature=0.3: (_ for _ in ()).throw(RuntimeError("model down")),
        )
        monkeypatch.setattr(analyze_module, "_rule_analyze", fallback)

        # Act
        result = await analyze_module._llm_analyze(
            [{"date": "2026-07-01", "amount": 1}],
            "SELECT date, amount FROM metrics",
            {"columns": {}},
            "趋势上升",
            "",
            "",
            chart_type="line",
            intent="trend",
        )

        # Assert
        assert captured == {"chart_type": "line", "intent": "trend"}
        assert result["recommended_chart_type"] == "line"


class TestAnalysisSampleBudget:
    """覆盖 spec/53：数据段可用量按动态预算推导，样本只做行级采样。"""

    # 方法作用：构造模拟模型边界与消息捕获。
    # Args: monkeypatch - 补丁器；captured - 捕获列表。
    # Returns: 无返回值。
    def _patch_llm(self, monkeypatch, captured: list) -> None:
        import json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import src.graph.nodes.analyze_result as analyze
        import src.llm.adapters.registry as registry
        import src.llm.knowledge_loop as loop

        response = SimpleNamespace(content=json.dumps({
            "summary": "已完成",
            "insights": [],
            "recommended_chart_type": "table",
            "follow_up_questions": [],
        }))

        async def fake_ainvoke(messages, *args, **kwargs):
            captured.append(messages)
            return response

        model = SimpleNamespace(ainvoke=AsyncMock(side_effect=fake_ainvoke))
        monkeypatch.setattr(analyze, "_get_analysis_llm", lambda **kwargs: model)
        monkeypatch.setattr(loop, "build_knowledge_tools_for_call", lambda prepared=None: [])
        monkeypatch.setattr(
            registry,
            "get_adapter",
            lambda model: SimpleNamespace(
                parse_response=lambda response: SimpleNamespace(
                    content=response.content, reasoning_content=""
                ),
            ),
        )

    # 方法作用：构造 120 行车间结果。
    # Args: 无。
    # Returns: 120 行测试数据。
    def _rows(self) -> list[dict]:
        return [
            {
                "workshop_name": f"{index}号车间",
                "total_output_qty": 500000 + index,
                "total_defect_qty": 1000 + index,
                "net_output_qty": 499000 + index,
            }
            for index in range(120)
        ]

    # 验证大窗口下 120 行样本全量进入且 JSON 完整。
    async def test_large_window_keeps_full_sample(self, monkeypatch) -> None:
        """spec/53：预算足够时模型看到全量 120 行，"输入全量"标签如实。"""
        # Arrange
        import src.graph.nodes.analyze_result as analyze

        captured: list = []
        self._patch_llm(monkeypatch, captured)
        # 依赖无租户环境的 128K 兜底窗口（spec/53 第八节第 4 条）；
        # 若默认窗口配置下调需显式 patch resolve_dynamic_budget 固定预算。
        monkeypatch.setattr(analyze, "_resolve_analysis_sample_budget", lambda: 50_000)

        # Act
        result = await analyze._llm_analyze(
            self._rows(), "SELECT 1", {"columns": {}}, "", "", "",
            [], user_query="今年各车间的产量数据是多少？",
        )

        # Assert
        human = captured[0][1].content
        assert result["summary"] == "已完成"
        assert "输入全量 120 行" in human
        assert '"workshop_name":"119号车间"' in human
        assert "...[已截断]" not in human

    # 验证小窗口下样本按行采样且有如实标签，绝不字符腰斩。
    async def test_small_window_samples_rows_with_label(self, monkeypatch) -> None:
        """spec/53：小窗口收敛时数据层行级采样，JSON 保持完整。"""
        # Arrange
        import src.graph.nodes.analyze_result as analyze

        captured: list = []
        self._patch_llm(monkeypatch, captured)
        monkeypatch.setattr(analyze, "_resolve_analysis_sample_budget", lambda: 2_000)

        # Act
        await analyze._llm_analyze(
            self._rows(), "SELECT 1", {"columns": {}}, "", "", "",
            [], user_query="今年各车间的产量数据是多少？",
        )

        # Assert
        human = captured[0][1].content
        assert "输入分源均匀采样" in human
        sample_line = [line for line in human.splitlines() if line.startswith("## 查询结果")][0]
        assert re.search(r"采样 [1-9]\d*/120 行", sample_line)
        assert "...[已截断]" not in human

    # 验证结果含超长字段时数据层继续收敛，不把序列化 JSON 交给字符腰斩。
    # Args: monkeypatch - pytest 补丁器。
    # Returns: 无返回值。
    async def test_heterogeneous_rows_converge_without_character_truncation(self, monkeypatch) -> None:
        """行长异质时按真实长度对半收敛：JSON 完整、标签行数与模型可见行数一致。"""
        # Arrange：100 行短样本 + 3 行超长字段，平均值估计会低估实际体量
        import json as json_module

        import src.graph.nodes.analyze_result as analyze

        captured: list = []
        self._patch_llm(monkeypatch, captured)
        monkeypatch.setattr(analyze, "_resolve_analysis_sample_budget", lambda: 20_000)
        rows = [{"id": index, "note": "x" * 80} for index in range(100)]
        rows += [{"id": 100 + index, "note": "y" * 5_000} for index in range(3)]

        # Act
        await analyze._llm_analyze(
            rows, "SELECT 1", {"columns": {}}, "", "", "",
            [], user_query="这些记录分别是什么？",
        )

        # Assert：数据段未被字符腰斩，采样 JSON 完整可解析
        human = captured[0][1].content
        assert "...[已截断]" not in human
        heading_line, body = human.split("## 查询结果", 1)[1].split("\n", 1)
        sample_json = body[:body.find("## 统计摘要")].strip()
        parsed = json_module.loads(sample_json)
        # Assert：标签如实，采样行数与模型实际可见行数一致
        match = re.search(r"输入分源均匀采样 (\d+)/(\d+) 行", heading_line)
        assert match, heading_line
        assert int(match.group(2)) == len(rows)
        assert int(match.group(1)) == len(parsed)
        assert 1 <= len(parsed) < len(rows)

    # 验证数据段可用量公式：大窗口封顶数据层阀门、小窗口收敛到下限。
    def test_sample_budget_formula(self, monkeypatch) -> None:
        """spec/53：available = budget − 固定 − 其余段上限，clamp [2000, 设置上限]（设置上限封顶最强）。"""
        # Arrange
        import src.graph.nodes.analyze_result as analyze

        # Act / Assert：大窗口 → 数据层阀门封顶
        monkeypatch.setattr(
            "src.llm.prompt_budget.resolve_dynamic_budget", lambda definition: 85_000
        )
        assert analyze._resolve_analysis_sample_budget() == 50_000
        # 小窗口 → 收敛到下限
        monkeypatch.setattr(
            "src.llm.prompt_budget.resolve_dynamic_budget", lambda definition: 12_576
        )
        assert analyze._resolve_analysis_sample_budget() == 2_000
        # 中间档 → 公式中段（非 clamp 区），期望动态计算避免 render 长度变化导致脆断言
        from src.llm.prompts import get_prompt_definition

        monkeypatch.setattr(
            "src.llm.prompt_budget.resolve_dynamic_budget", lambda definition: 40_000
        )
        expected = (
            40_000
            - len(get_prompt_definition("analysis.result").render())
            - analyze._OTHER_SECTIONS_CAP_CHARS
        )
        assert analyze._resolve_analysis_sample_budget() == expected
