# 提示词预算动态化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 LLM 提示词预算按超管配置的模型窗口动态推导（注册值降级为设计上限），修复 analysis.result 预算挤兑导致模型看不到数据的线上事故。

**Architecture:** 复用会话层已有的模型窗口解析链（`get_context_budget`）；`prepare_invocation` 统一注入动态预算；数据层采样量跟随动态预算成为"模型可见数据量"的唯一阀门。

**Tech Stack:** Python 3.14、pytest、Pydantic、LangChain 消息协议。

**项目执行规则（覆盖一切技能流程）：**
- **禁止任何 `git commit` / `git push`**（项目 CLAUDE.md 最高优先级）。每个任务的完成标志是"指定测试通过"，不是 commit。
- 所有代码注释、日志、docstring 用中文；测试函数名用英文小写下划线。
- 测试命令统一用 `./.venv/Scripts/python.exe -m pytest ...`（项目虚拟环境）。
- 单元测试禁止调用真实模型（Mock 模型工厂，见 CLAUDE.md 测试策略）；不得破坏现有测试的语义。

**设计依据：** `spec/53-prompt-budget-model-window.md`（每个任务标注对应设计节）。

---

### Task 1: 全局设置项与能力字段

**Files:**
- Modify: `src/config.py`（"---- 分析器 ----" 节前新增配置节，约 413 行）
- Modify: `src/llm/capability_schema.py`（`default_capability_schema()` 的 `fields` 列表，`max_tokens_limit` 定义之后，约 200 行）
- Test: `tests/test_llm/test_capability_schema.py`（追加用例）

对应设计：第三节第 2 条（换算系数 K 的读取链）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_llm/test_capability_schema.py` 的测试类内追加：

```python
    # 方法作用：验证默认能力表单包含提示词预算换算系数并可校验覆盖值。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_default_schema_includes_prompt_budget_factor(self) -> None:
        """spec/53：供应商 token 效率差异由能力字段覆盖，缺省不物化以便回退全局默认。"""
        # Arrange
        from src.llm.capability_schema import default_capability_schema, validate_capability_values

        schema = default_capability_schema()

        # Act
        fields = {field["key"]: field for field in schema["fields"]}
        filled = validate_capability_values(schema, {})
        overridden = validate_capability_values(schema, {"prompt_budget_char_factor": 2.0})

        # Assert
        assert fields["prompt_budget_char_factor"]["type"] == "number"
        assert fields["prompt_budget_char_factor"]["default"] is None
        assert "prompt_budget_char_factor" not in filled
        assert overridden["prompt_budget_char_factor"] == 2.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_llm/test_capability_schema.py -k prompt_budget_factor -v`
Expected: FAIL（KeyError: 'prompt_budget_char_factor'）

- [ ] **Step 3: 实现**

`src/config.py`，在 `# ---- 分析器 ----` 节之前插入：

```python
    # ---- 提示词预算动态化（spec/53）----
    # token→字符折算系数：默认 1.0 对中文优化 tokenizer 安全（按 1 token = 1 字符保守估算）；
    # 中文费 token 的模型按能力字段 prompt_budget_char_factor 调小（约 0.5）。
    prompt_budget_token_char_factor: float = Field(default=1.0, ge=0.1, le=8)
    # 动态推导的全局保底字符数：防超小窗口把预算算成负数或装不下固定提示词。
    prompt_budget_min_chars: int = Field(default=8_000, ge=1_000, le=100_000)
```

`src/llm/capability_schema.py`，`default_capability_schema()` 的 `fields` 列表中，`max_tokens_limit` 字段定义之后插入：

```python
        {
            "key": "prompt_budget_char_factor",
            "label": "预算换算系数",
            "type": "number",
            "minimum": 0.1,
            "maximum": 8.0,
            "description": "1 token 折算的字符数；不填回退全局默认（1.0），中文费 token 的模型调小（约 0.5）",
        },
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_llm/test_capability_schema.py -v`
Expected: 全部 PASS

---

### Task 2: 动态预算推导函数

**Files:**
- Modify: `src/memory/context_builder.py`（在 `resolve_context_window` 之后新增公开函数，约 198 行）
- Modify: `src/llm/prompt_budget.py`（新增 `resolve_dynamic_budget`，`build_budgeted_prompt` 之前）
- Test: `tests/test_llm/test_dynamic_prompt_budget.py`（新建）

对应设计：第三节（核心机制公式）。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_llm/test_dynamic_prompt_budget.py`：

```python
"""覆盖 spec/53：提示词预算按超管模型窗口动态推导。"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

from src.llm.prompt_budget import resolve_dynamic_budget
from src.llm.prompts import PROMPT_REGISTRY, PromptDefinition, register_prompt


@contextlib.contextmanager
def _fake_window(usable_tokens: int):
    """用假可用输入 token 替换会话层预算解析，隔离真实配置链。"""

    class _Budget:
        pass

    budget = _Budget()
    budget.usable_input_tokens = usable_tokens
    with patch("src.memory.context_builder.get_context_budget", return_value=budget):
        yield


class TestDynamicPromptBudget:
    """覆盖窗口阶梯、系数覆盖与缺失回退。"""

    # 方法作用：构造带设计上限的临时提示词定义。
    # Args: cap - 设计上限字符数。
    # Returns: 已注册的 PromptDefinition。
    def _definition(self, cap: int) -> PromptDefinition:
        definition = PromptDefinition(
            prompt_id="budget.dynamic",
            version="1.0.0",
            task="test",
            template="BASE",
            context_budget=cap,
            system_policy="S",
            capability_policy="C",
        )
        register_prompt(definition)
        return definition

    def teardown_method(self) -> None:
        PROMPT_REGISTRY.pop("budget.dynamic", None)

    # 验证 128K 窗口推导值大于设计上限时封顶到上限。
    def test_large_window_caps_at_registered_upper(self) -> None:
        """窗口再大也不突破设计上限。"""
        # Arrange
        definition = self._definition(50_000)

        # Act
        with _fake_window(108_000):
            effective = resolve_dynamic_budget(definition)

        # Assert
        assert effective == 50_000

    # 验证 1M 窗口同样被设计上限封顶。
    def test_million_window_still_capped(self) -> None:
        """1M 窗口推导值近百万字符，仍取设计上限。"""
        # Arrange
        definition = self._definition(50_000)

        # Act
        with _fake_window(979_808):
            effective = resolve_dynamic_budget(definition)

        # Assert
        assert effective == 50_000

    # 验证小窗口收缩到窗口推导值。
    def test_small_window_converges_below_cap(self) -> None:
        """32K 窗口下预算自动收缩，不超窗。"""
        # Arrange
        definition = self._definition(85_000)

        # Act
        with _fake_window(12_576):
            effective = resolve_dynamic_budget(definition)

        # Assert
        assert effective == 12_576

    # 验证极小窗口命中全局下限。
    def test_tiny_window_hits_global_floor(self) -> None:
        """可用输入不足时不得推导出负数或装不下固定提示词的预算。"""
        # Arrange
        definition = self._definition(85_000)

        # Act
        with _fake_window(1):
            effective = resolve_dynamic_budget(definition)

        # Assert
        assert effective == 8_000

    # 验证模型能力声明的换算系数优先于全局默认。
    def test_model_factor_overrides_global_default(self) -> None:
        """中文费 token 的 tokenizer 供应商把系数调小后预算随之收缩。"""
        # Arrange
        definition = self._definition(500_000)

        # Act / Assert
        with _fake_window(30_000):
            with patch(
                "src.memory.context_builder.resolve_prompt_budget_factor",
                return_value=0.5,
            ):
                assert resolve_dynamic_budget(definition) == 15_000

    # 验证系数缺失时回退全局默认 1.0。
    def test_missing_factor_falls_back_to_default(self) -> None:
        """能力链读不到系数时按 1 token = 1 字符保守折算。"""
        # Arrange
        definition = self._definition(500_000)

        # Act
        with _fake_window(30_000):
            effective = resolve_dynamic_budget(definition)

        # Assert
        assert effective == 30_000
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_llm/test_dynamic_prompt_budget.py -v`
Expected: FAIL（ImportError: cannot import name 'resolve_dynamic_budget'）

- [ ] **Step 3: 实现**

`src/memory/context_builder.py`，在 `resolve_context_window` 函数之后（`get_context_budget` 之前）插入：

```python
# 方法作用：读取当前模型声明的提示词预算换算系数。
# Args: model_id - 显式模型标识；model - 可选模型实例。
# Returns: 正数系数；未声明或非法时返回 None（由调用方回退全局默认）。
def resolve_prompt_budget_factor(
    *,
    model_id: str | None = None,
    model: Any | None = None,
) -> float | None:
    """供提示词预算动态化读取供应商级 token→字符折算系数（spec/53）。"""
    _, capabilities = _resolve_model_identity(model_id, model)
    raw = capabilities.get("prompt_budget_char_factor")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None
```

`src/llm/prompt_budget.py`：文件顶部 import 区加：

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.llm.prompts import PromptDefinition
```

在 `build_budgeted_prompt` 之前插入：

```python
# 方法作用：按当前模型窗口推导单次 Prompt 的实际预算（spec/53）。
# Args: definition - 已注册的提示词定义；model_id/model - 可选模型覆盖，供测试与特殊调用。
# Returns: min(注册上限, max(全局下限, 可用输入 token × 换算系数))。
def resolve_dynamic_budget(
    definition: PromptDefinition,
    *,
    model_id: str | None = None,
    model: Any | None = None,
) -> int:
    """复用会话层模型窗口解析链；任何缺失都回退保守默认。"""
    from src.config import get_settings
    from src.memory.context_builder import get_context_budget, resolve_prompt_budget_factor

    settings = get_settings()
    floor = max(1, int(getattr(settings, "prompt_budget_min_chars", 8000)))
    default_factor = float(getattr(settings, "prompt_budget_token_char_factor", 1.0))
    factor = resolve_prompt_budget_factor(model_id=model_id, model=model) or default_factor
    context_budget = get_context_budget(model_id=model_id, model=model)
    derived = int(context_budget.usable_input_tokens * factor)
    return min(int(definition.context_budget), max(floor, derived))
```

（`prompt_budget.py` 顶部现有 `from typing import Literal`，需改为 `from typing import TYPE_CHECKING, Any, Literal`。）

- [ ] **Step 4: 跑测试确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_llm/test_dynamic_prompt_budget.py -v`
Expected: 全部 PASS

---

### Task 3: 预算器接入动态覆盖值（接线）

**Files:**
- Modify: `src/llm/prompt_budget.py`（`build_budgeted_prompt` 增加 `budget_override` 参数，约 108-121 行）
- Modify: `src/llm/invocation.py`（`prepare_invocation` 计算并传入动态预算，约 52-62 行）
- Test: `tests/test_llm/test_prompt_budget.py`（追加用例）

对应设计：第三节第 3、5 条。

- [ ] **Step 1: 写失败测试**

在 `tests/test_llm/test_prompt_budget.py` 的 `TestPromptBudget` 类内追加：

```python
    # 方法作用：验证预算覆盖值优先于注册值生效。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_budget_override_takes_precedence(self) -> None:
        """spec/53：动态推导值作为实际预算，注册值仅作上限。"""
        # Arrange
        from src.llm.prompt_budget import PromptSection, build_budgeted_prompt
        from src.llm.prompts import PROMPT_REGISTRY, PromptDefinition, register_prompt

        definition = PromptDefinition(
            prompt_id="budget.override",
            version="1.0.0",
            task="test",
            template="X",
            context_budget=200_000,
        )
        try:
            register_prompt(definition)
            sections = [
                PromptSection("data", "D" * 60_000, priority=100, min_chars=1000, max_chars=55_000),
            ]

            # Act
            prompt = build_budgeted_prompt("budget.override", sections, budget_override=10_000)

            # Assert
            assert prompt.used_chars <= 10_000
            assert "data" in prompt.truncated_sections
        finally:
            PROMPT_REGISTRY.pop("budget.override", None)

    # 方法作用：验证 prepare_invocation 走动态预算而不是注册上限。
    # Args: self - pytest 测试类实例；monkeypatch - 补丁器。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_prepare_invocation_uses_dynamic_budget(self, monkeypatch) -> None:
        """spec/53：统一调用入口按当前模型窗口分配，注册上限不直接生效。"""
        # Arrange
        from types import SimpleNamespace

        from src.llm.invocation import prepare_invocation
        from src.llm.prompt_budget import PromptSection
        from src.llm.prompts import PROMPT_REGISTRY, PromptDefinition, register_prompt

        definition = PromptDefinition(
            prompt_id="budget.call",
            version="1.0.0",
            task="test",
            template="S",
            context_budget=200_000,
        )
        register_prompt(definition)

        class _Budget:
            usable_input_tokens = 30_000

        from src.config import get_settings

        # 钉死预算设置，避免环境配置改变时断言隐式漂移（与 spec/54 Task 2 修复一致的隔离模式）。
        monkeypatch.setattr(get_settings(), "prompt_budget_token_char_factor", 1.0)
        monkeypatch.setattr(get_settings(), "prompt_budget_min_chars", 8_000)
        monkeypatch.setattr(
            "src.memory.context_builder.get_context_budget", lambda **kwargs: _Budget()
        )
        monkeypatch.setattr(
            "src.memory.context_builder.resolve_prompt_budget_factor", lambda **kwargs: None
        )

        try:
            # Act
            prepared = prepare_invocation(
                "budget.call",
                [PromptSection("data", "D" * 60_000, priority=100, min_chars=1000, max_chars=55_000)],
                model=SimpleNamespace(),
            )

            # Assert
            used = len(prepared.messages[0].content) + len(prepared.messages[1].content)
            assert used <= 30_000
        finally:
            PROMPT_REGISTRY.pop("budget.call", None)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_llm/test_prompt_budget.py -k "override or dynamic" -v`
Expected: FAIL（`build_budgeted_prompt` 不接受 `budget_override`）

- [ ] **Step 3: 实现**

`src/llm/prompt_budget.py`，`build_budgeted_prompt` 签名与预算取值处修改：

```python
def build_budgeted_prompt(
    prompt_id: str,
    sections: list[PromptSection],
    *,
    system_values: dict[str, object] | None = None,
    reserve_chars: int = 0,
    budget_override: int | None = None,
) -> BudgetedPrompt:
    """固定 System 不截断，动态 section 先保底再按优先级扩展；reserve_chars 留给工具 schema。

    budget_override 为 spec/53 的动态推导值（按模型窗口）；注册值此时仅作为上限。
    """
    from src.llm.prompts import get_prompt_definition

    definition = get_prompt_definition(prompt_id)
    budget = int(definition.context_budget) if budget_override is None else int(budget_override)
    if budget <= 0:
        raise ValueError(f"Prompt {prompt_id} 的 context_budget 必须大于零")
```

（其余分配逻辑不变。）

`src/llm/invocation.py`，`prepare_invocation` 中替换预算构建段：

```python
    definition = get_prompt_definition(prompt_id)
    effective_task = task or definition.task
    # 可信身份下为工具定义、一条有界工具结果与协议开销预留空间。
    from src.llm.knowledge_loop import knowledge_tool_reserve_chars
    from src.llm.prompt_budget import resolve_dynamic_budget

    # spec/53：预算按当前模型窗口动态推导，注册值仅作设计上限。
    effective_budget = resolve_dynamic_budget(definition)
    budgeted = build_budgeted_prompt(
        prompt_id,
        sections,
        system_values=system_values,
        reserve_chars=knowledge_tool_reserve_chars(definition.context_budget),
        budget_override=effective_budget,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_llm/test_prompt_budget.py tests/test_llm/test_invocation.py -v`
Expected: 全部 PASS（含既有用例）

---

### Task 4: 注册值重审

**Files:**
- Modify: `src/llm/prompts.py`（`analysis.result` 约 633-640 行；`context.summary` 约 714-723 行）
- Test: `tests/test_llm/test_prompt_registry.py`（追加用例）

对应设计：第四节（注册值重审表）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_llm/test_prompt_registry.py` 追加（按该文件既有类结构放入合适测试类）：

```python
    # 方法作用：验证分析与会话摘要的注册值已升级为设计上限（spec/53）。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_analysis_and_summary_budget_caps(self) -> None:
        """analysis.result=85000、context.summary=5000；不重审其余场景（原值转上限语义）。"""
        # Arrange
        from src.llm.prompts import get_prompt_definition

        # Act / Assert
        assert get_prompt_definition("analysis.result").context_budget == 85_000
        assert get_prompt_definition("context.summary").context_budget == 5_000
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_llm/test_prompt_registry.py -k budget_caps -v`
Expected: FAIL（当前值为 7000/3000）

- [ ] **Step 3: 实现**

`src/llm/prompts.py`：

- `analysis.result` 定义：`context_budget=7000` 改为 `context_budget=85000`，注释写明依据：

```python
    "analysis.result": PromptDefinition(
        prompt_id="analysis.result",
        version="1.0.0",
        task="analyze_result",
        template=DATA_ANALYSIS_SYSTEM,
        output_model=AnalysisOutput,
        # spec/53：注册值升级为设计上限（分段上限之和 78400 + 固定 2673 + 余量）；
        # 实际预算由 resolve_dynamic_budget 按超管模型窗口推导。
        context_budget=85000,
    ),
```

- `context.summary` 定义：`context_budget=3000` 改为 `context_budget=5000`。

- [ ] **Step 4: 跑测试确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_llm/test_prompt_registry.py tests/test_llm/test_prompt_budget.py -v`
Expected: 全部 PASS

---

### Task 5: 数据层单阀门（analyze_result）

**Files:**
- Modify: `src/graph/nodes/analyze_result.py`（`_to_compact` 约 507-529 行；新增 `_resolve_analysis_sample_budget`；`_llm_analyze` 采样调用约 872 行；`result_sample`/`statistics` 分段上限约 930-943 行）
- Test: `tests/test_graph/test_analyze_result.py`（追加用例）

对应设计：第五节（数据层单阀门）、第六节（分段上限重审）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_graph/test_analyze_result.py` 追加（Mock 方式与 `tests/test_graph/test_chat_presentation.py::test_analysis_prompt_uses_display_language_fallback` 相同）：

```python
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
        assert "/120 行" in sample_line
        assert "...[已截断]" not in human

    # 验证数据段可用量公式：大窗口封顶数据层阀门、小窗口收敛到下限。
    def test_sample_budget_formula(self, monkeypatch) -> None:
        """spec/53：available = budget − 固定 − 其余段上限，clamp [2000, 数据层设置]。"""
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_graph/test_analyze_result.py -k TestAnalysisSampleBudget -v`
Expected: FAIL（`_resolve_analysis_sample_budget` 不存在 / `resolve_dynamic_budget` 未导入到该模块）

- [ ] **Step 3: 实现**

`src/graph/nodes/analyze_result.py`：

1）`_to_compact` 参数化（替换原 `limit = get_settings().analysis_data_max_chars`）：

```python
# 方法作用：按来源分别均匀采样并交错展示，避免字符裁剪只保留首个来源。
# Args: rows - 已授权的查询结果样本；max_chars - 可选字符上限，缺省读全局设置。
# Returns: 各来源均有代表行的紧凑 JSON；单源保持原顺序。
def _to_compact(rows: list[dict], max_chars: int | None = None) -> str:
    """全量数据转紧凑 JSON，超限按来源均匀抽取。"""
    if not rows:
        return "[]"
    from src.config import get_settings
    limit = int(max_chars) if max_chars is not None else get_settings().analysis_data_max_chars
```

（其余函数体不变。）

2）`_llm_analyze` 之前新增模块级常量与辅助函数（与 `_format_sample_scope` 相邻放置）：

```python
# 其余动态段的设计上限之和（spec/53 第六节：78400 − result_sample 55000）。
_OTHER_SECTIONS_CAP_CHARS = 23_400


# 方法作用：按当前模型窗口推导"查询结果段"可用的字符量。
# Args: 无（读取当前请求模型解析链与提示词注册表）。
# Returns: 数据层采样应遵守的字符上限；小窗口自动收敛，大窗口受全局阀门封顶。
def _resolve_analysis_sample_budget() -> int:
    """数据层单阀门：模型可见数据量只由此处决定（spec/53 第五节）。"""
    from src.config import get_settings
    from src.llm.prompt_budget import resolve_dynamic_budget
    from src.llm.prompts import get_prompt_definition

    settings = get_settings()
    definition = get_prompt_definition("analysis.result")
    budget = resolve_dynamic_budget(definition)
    available = budget - len(definition.render()) - _OTHER_SECTIONS_CAP_CHARS
    return min(settings.analysis_data_max_chars, max(2_000, available))
```

3）`_llm_analyze` 中采样调用改为：

```python
    total_rows = len(rows)
    sample = _to_compact(rows, _resolve_analysis_sample_budget())
```

4）分段上限重审（`prompt_sections` 列表内）：

```python
            PromptSection(
                "result_sample",
                f"## 查询结果 ({input_label})\n{sample}",
                priority=90,
                min_chars=2000,
                max_chars=55000,
            ),
```

```python
            PromptSection(
                "statistics",
                f"## 统计摘要\n{stat_text}",
                priority=85,
                min_chars=700,
                max_chars=8000,
            ),
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_graph/test_analyze_result.py tests/test_graph/test_chat_presentation.py -v`
Expected: 全部 PASS

---

### Task 6: 全量回归、探针验证与文档同步

**Files:**
- Modify: `spec/13-prompt-templates.md`（§12.5 第 4、5 条）
- Modify: `spec/53-prompt-budget-model-window.md`（追加验收记录）
- Modify: `spec/README.md`（索引追加 54 行）
- Modify: `features/10-llm.md`（10.2.9 行描述）
- Delete: `_tmp_budget_probe.py`

- [ ] **Step 1: 全量回归**

Run: `./.venv/Scripts/python.exe -m pytest tests/ -m "not live_llm" -q`
Expected: 全部通过（如有失败，逐条修复后再进入下一步）

Run: `./.venv/Scripts/python.exe -m ruff check src tests`
Expected: 无告警

- [ ] **Step 2: 探针端到端验证**

以生产函数版探针做端到端验证（脚本完成后删除）：

```python
"""端到端验证 spec/53：动态预算 + 数据层单阀门后 120 行样本完整进入提示词（直接调用生产函数）。"""

from src.config import get_settings
from src.graph.nodes.analyze_result import _resolve_analysis_sample_budget, _to_compact
from src.llm.prompt_budget import PromptSection, build_budgeted_prompt, resolve_dynamic_budget
from src.llm.prompts import get_prompt_definition

definition = get_prompt_definition("analysis.result")
budget = resolve_dynamic_budget(definition)
print("analysis.result 动态预算 =", budget)
print("fixed_system_chars =", len(definition.render()))
print("数据段预算（生产函数）= ", _resolve_analysis_sample_budget())

rows = [
    {
        "workshop_name": f"{index}号车间",
        "total_output_qty": 500000 + index,
        "total_defect_qty": 1000 + index,
        "net_output_qty": 499000 + index,
    }
    for index in range(120)
]
sample = _to_compact(rows, _resolve_analysis_sample_budget())
print("样本字符数 =", len(sample), "| 首行含 0号车间 =", '"0号车间"' in sample,
      "| 末行含 119号车间 =", '"119号车间"' in sample)

sections = [
    PromptSection("query", "## 用户原问题\n今年各车间的产量数据是多少？", priority=100, min_chars=400, max_chars=1800),
    PromptSection("result_sample", f"## 查询结果 (输入全量 120 行)\n{sample}", priority=90, min_chars=2000, max_chars=55000),
]
prompt = build_budgeted_prompt("analysis.result", sections, budget_override=budget)
print("提示词预算 =", prompt.budget, "| 实际用量 =", prompt.used_chars)
print("样本完整进入 =", '"119号车间"' in prompt.human, "| 无截断标记 =", "...[已截断]" not in prompt.human)
```

Run: `./.venv/Scripts/python.exe _tmp_budget_probe.py`
Expected（128K 兜底窗口）：动态预算 ≥ 85000（封顶）、样本完整、`无截断标记 = True`；然后 `rm _tmp_budget_probe.py`。

- [ ] **Step 3: 文档同步**

1）`spec/13-prompt-templates.md` §12.5：
- 第 4 条改为：

```markdown
4. `context_budget` 以字符数声明单次 Prompt 的**设计上限**；实际预算按超管配置的模型上下文窗口
   动态推导（`resolve_dynamic_budget`：min(设计上限, max(全局下限, 可用输入 token × 换算系数))，
   见 `spec/53-prompt-budget-model-window.md`）。固定安全策略不可截断；若固定 System Prompt 自身
   超过最终预算，必须在调用模型前失败。
```

- 第 5 条追加一句：

```markdown
   数据段不得对序列化 JSON 做字符级腰斩；模型可见数据量由数据层行级采样单阀门决定（spec/53 第五节）。
```

2）`spec/53-prompt-budget-model-window.md` 末尾"验收记录"节填写：全量测试数、探针输出关键行、实施中与设计的偏差（如"请求级缓存未新增——核实 `get_current_tenant_llm_selection` 为纯 ContextVar 读取，解析无数据库查询"）。

3）核对 `spec/README.md` 索引已含 54 行（本计划落盘时已同步，如缺失则补）：

```markdown
| [54-prompt-budget-model-window-implementation-plan.md](54-prompt-budget-model-window-implementation-plan.md) | 提示词预算动态化实施计划（预算推导 · 注册上限 · 数据层单阀门） | 动态 |
```

4）`features/10-llm.md` 10.2.9 行描述更新（保持状态列不变）：

```markdown
| 10.2.9 | Prompt 统一总预算 | `src/llm/prompt_budget.py`、各 LLM 调用节点 | System、用户问题、Schema、知识、Skill、示例、结果和历史共享字符预算，按优先级保留与裁剪；spec/53 起预算按超管模型窗口动态推导、注册值为设计上限 | 单测完成 | P1 |
```

- [ ] **Step 4: 收尾检查**

Run: `./.venv/Scripts/python.exe -m pytest tests/ -m "not live_llm" -q`
Expected: 全部通过；且 `git status` 中无 `_tmp_budget_probe.py`（已删除）。**不执行任何 git 提交**，改动留待用户统一验收。

---

## 自审记录

- **Spec 覆盖**：设计二至九节逐条对应——第三节 → Task 1/2/3；第四节 → Task 4；第五、六节 → Task 5；第七节 → Task 1（系数字段）+ 无需改动的既有兜底；第八节 → Task 2（下限）+ Task 6（缓存偏差说明）；第九节 → 各 Task 测试 + Task 6 全量回归。第十节 → Task 6 Step 3。第十一节（不做项）与第十二节（影响面）已按文件清单落实。
- **占位符扫描**：无 TODO/TBD；所有步骤含完整代码与预期结果。
- **类型一致性**：`resolve_dynamic_budget(definition, *, model_id=None, model=None)`、`build_budgeted_prompt(..., budget_override=None)`、`resolve_prompt_budget_factor(*, model_id=None, model=None)`、`_resolve_analysis_sample_budget()`、`_to_compact(rows, max_chars=None)` 在全部任务中签名一致。
