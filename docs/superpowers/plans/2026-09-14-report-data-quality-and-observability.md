# 报表取数质量与可观测性改造 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让报表取回的数据可信（时间范围不丢、样本不被暴力截断、缺数据能补查）、呈现由模型决策（图表类型/多图/标题），并把每次单元执行的完整上下文落成快照以支持反推排查与单单元重跑。

**Architecture:** 快照表与重跑先行（后续所有调试都依赖它）；其次修时间范围（契约层 + 提示词 + 查询拼装）；再移除硬编码截断改为模型判断；补查建立在时间范围与截断修复之上；图表与标题属呈现层，依赖 LLM 决策契约；提示词架构最后收敛。

**Tech Stack:** Python 3.14 / FastAPI / Pydantic v2 / LangGraph / asyncpg / PostgreSQL（RLS）/ React 18 + antd + ECharts / pytest

## Global Constraints

- 所有注释与 docstring 使用中文；每个方法上方三行式注释（作用 / Args / Returns），入口 debug、出口 info、异常 error(exc_info=True)。
- 日志用 `src.logging_config.get_logger`，禁止 `print()`。日志文件 `TimedRotatingFileHandler` 保留 7 天。
- 类型注解使用 Python 3.14 语法；数据验证用 Pydantic。
- LLM 调用统一走 `src/llm/client.py` 工厂与 `src/llm/invocation.invoke_structured`；提示词集中注册在 `src/llm/prompts.py`。
- 单元测试禁止调用真实模型（Mock `invoke_structured` 或 `is_task_llm_available`）；远程模型测试须 `RUN_LIVE_LLM_TESTS=1` + `pytest.mark.live_llm`。
- 测试用 `.venv/Scripts/python.exe -m pytest`（系统 python 是 3.10，会因 PEP 695 语法报假错误）。
- 提交信息**不得**出现 Claude 署名或 Co-Authored-By trailer。
- 迁移文件编号接 `035`，下一号为 `037`（036 已作废未使用）。
- 前端类型检查：`cd frontend && node node_modules/typescript/bin/tsc --noEmit` 必须 exit 0。
- 完成前必须：全量 `pytest -m "not live_llm"` 通过 + `tsc --noEmit` 通过 + 浏览器实跑一次真实报表。

## 已知前置事实（来自 2026-09-14 实测）

- 事故报表 `1b9d3d72-c467-4896-8a72-f2f36cd168dd`：prompt 写「时间范围 2016-01-01 至 2036-12-31」，22 个单元 `time_range` 全为 `{"kind":"relative","value":"90d"}`。
- 20 个图表块 `series.type` **全部是 `bar`**（`document.py` 硬编码）。
- `report_live_queries` 有 `sql_text` 但**无 `unit_id`**，13 条 SQL 对不上 22 个单元。
- `report_run_steps` 有 node/status/duration/error，**无提示词**。
- 单元执行走 `runner._default_execute_unit` → `src.graph.workflow.app.ainvoke`（与对话分析共用节点，报表侧硬编码 `suppress_supplemental_query=True`）。
- 三处硬编码截断：`multi_source.py:266,270`（`data[:50]` + `len(data)>50`）、`merge_results_node`（`len(batch)>30`、`len(all_data)>200`）、`execute_sql.py:179`（`masked_rows[:200]`）。

---

## Task 1: 运行快照表与单单元重跑

**为什么先做：** 后续每个修复的验证都依赖"能按单元看到它当时的 SQL、提示词、行数与截断原因"。当前 SQL 连 `unit_id` 都没有，无法反推。

**Files:**
- Create: `migrations/037_report_unit_snapshots.sql`
- Create: `src/reporting/snapshots.py`
- Modify: `src/reporting/store.py`（新增 `save_unit_snapshot` / `list_unit_snapshots`）
- Modify: `src/reporting/runner.py`（`_default_execute_unit` 落快照）
- Modify: `src/api/routes/report_workbench.py`（两个端点）
- Modify: `frontend/src/api/reportWorkbench.ts`、`frontend/src/pages/ReportListPage.tsx`
- Test: `tests/test_reporting/test_unit_snapshots.py`

**Interfaces:**
- Consumes: `ReportStore`、`UnitResult`、`ReportRunner._default_execute_unit`
- Produces:
  - `src/reporting/snapshots.py::UnitSnapshot`（Pydantic 模型）
  - `src/reporting/snapshots.py::build_snapshot(...) -> UnitSnapshot`
  - `ReportStore.save_unit_snapshot(snapshot, identity) -> None`
  - `ReportStore.list_unit_snapshots(run_id, identity) -> list[dict]`
  - `POST /report-workbench/reports/{report_id}/units/{unit_id}/rerun` → `{unit_id, status, snapshot_id}`
  - `GET /report-workbench/reports/{report_id}/units/{unit_id}/snapshot` → `{snapshot: {...} | null}`

- [ ] **Step 1: 写迁移**

```sql
-- migrations/037_report_unit_snapshots.sql
-- 报表单元执行快照：把"这一次取数到底发生了什么"完整落库，供反推排查。
-- 与 report_live_queries 的区别：后者只存 SQL 且无 unit_id 关联；本表按单元一条，
-- 覆盖提示词、模型、行数、截断原因与错误，支持"重跑这个单元"。
CREATE TABLE IF NOT EXISTS report_unit_snapshots (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL,
    report_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    unit_id VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    datasource VARCHAR(64) NOT NULL DEFAULT '',
    dialect VARCHAR(32) NOT NULL DEFAULT '',
    sql_text TEXT NOT NULL DEFAULT '',
    prompt_text TEXT NOT NULL DEFAULT '',
    prompt_id VARCHAR(64) NOT NULL DEFAULT '',
    model_id VARCHAR(128) NOT NULL DEFAULT '',
    params_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    row_count INTEGER NOT NULL DEFAULT 0,
    full_count INTEGER NOT NULL DEFAULT 0,
    truncated BOOLEAN NOT NULL DEFAULT FALSE,
    truncated_reason VARCHAR(200) NOT NULL DEFAULT '',
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    error_code VARCHAR(64) NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, unit_id)
);
CREATE INDEX IF NOT EXISTS idx_report_unit_snapshots_report
    ON report_unit_snapshots (tenant_id, report_id, created_at DESC);

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['report_unit_snapshots'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
    END LOOP;
END $$;

DROP POLICY IF EXISTS report_unit_snapshots_scope ON report_unit_snapshots;
CREATE POLICY report_unit_snapshots_scope ON report_unit_snapshots FOR ALL USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        current_setting('app.current_role', true) = 'tenant_admin'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
    )
) WITH CHECK (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        current_setting('app.current_role', true) = 'tenant_admin'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
    )
);
```

- [ ] **Step 2: 写失败测试**

```python
# tests/test_reporting/test_unit_snapshots.py
"""报表单元执行快照与单单元重跑契约。"""

from __future__ import annotations

from pathlib import Path


class TestUnitSnapshotContract:
    """覆盖快照必须覆盖的字段与迁移契约。"""

    # 方法作用：验证快照字段覆盖反推排查所需的最小集合。
    def test_snapshot_carries_debug_fields(self) -> None:
        """缺 unit_id 的 SQL 无法定位到单元，缺提示词无法解释模型为何这样写 SQL。"""
        # Arrange / Act
        from src.reporting.snapshots import UnitSnapshot

        fields = set(UnitSnapshot.model_fields)

        # Assert
        assert {"unit_id", "sql_text", "prompt_text", "model_id", "row_count",
                "full_count", "truncated", "truncated_reason", "elapsed_ms", "error_message"} <= fields

    # 方法作用：验证迁移包含 RLS 与单元维度唯一键。
    def test_migration_enforces_rls_and_uniqueness(self) -> None:
        # Arrange / Act
        sql = Path("migrations/037_report_unit_snapshots.sql").read_text(encoding="utf-8")

        # Assert
        assert "FORCE ROW LEVEL SECURITY" in sql
        assert "UNIQUE (run_id, unit_id)" in sql
```

- [ ] **Step 3: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_reporting/test_unit_snapshots.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.reporting.snapshots'`

- [ ] **Step 4: 实现 snapshots 模块**

`src/reporting/snapshots.py` 导出 `UnitSnapshot`（字段与迁移一一对应，`params: dict[str, Any]` 序列化进 `params_json`）与 `build_snapshot(...)`。实现要点：

- `build_snapshot` **只做字段归一，不做 IO**：SQL 超长截到 100_000 字符；`params` 剔除敏感键（`api_key`/`token`/`password`/`secret`）；`truncated_reason` 由调用方给出，本函数不推断。
- 新增 `describe_truncation(displayed, full_count, limit) -> str`，产出形如「查询返回 80000 行，页面展示 5000 行（上限 5000）」的可读原因；未截断返回空串。
- `ReportStore.save_unit_snapshot`：走 `pg_pool_connection(pool, tenant_id, user_id, role)`，`ON CONFLICT (run_id, unit_id) DO UPDATE`（重跑覆盖旧快照）；内存分支写入 `self._unit_snapshots[(run_id, unit_id)]`。
- `ReportStore.list_unit_snapshots(run_id, identity)`：按 `unit_id` 排序返回列表；内存分支同源。

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_reporting/test_unit_snapshots.py -v`
Expected: PASS

- [ ] **Step 6: 在 runner 落快照**

在 `runner._default_execute_unit` 返回 `UnitResult` 之前组装并保存快照。**快照写入失败只记 warning，不得影响报表生成**（与通知同策略）。`elapsed_ms` 用函数入口的 `time.monotonic()` 差值。

- [ ] **Step 7: 加重跑端点与前端入口**

- `POST /reports/{report_id}/units/{unit_id}/rerun`：校验 `report.create_page` 能力与创建者身份 → 取该报表最新 run 的 plan → 重跑单个单元（复用 `_default_execute_unit`）→ 覆盖写快照 → 返回 `{unit_id, status, snapshot_id}`。**不自动重生成页面**，避免副作用。
- `GET /reports/{report_id}/units/{unit_id}/snapshot`：返回该单元最近一条快照。
- 前端 `ReportListPage` 详情抽屉的单元行加「重跑本单元」与「查看快照」；快照抽屉展示 SQL、提示词、行数、截断原因、错误。

- [ ] **Step 8: 全量验证**

Run: `.venv/Scripts/python.exe -m pytest tests/test_reporting tests/test_api -q` → 全绿
Run: `cd frontend && node node_modules/typescript/bin/tsc --noEmit` → exit 0

---

## Task 2: 修复单元时间范围丢失

**Files:**
- Modify: `src/llm/output_contracts.py:152-161`（`ReportPlanUnit` 加 `time_range`）
- Modify: `src/llm/prompts.py:370-388`（`report.plan` 交代抽取规则）
- Modify: `src/reporting/planner.py`（规则层兜底抽取 + 套用）
- Modify: `src/reporting/runner.py::_compose_unit_query`
- Test: `tests/test_reporting/test_unit_time_range.py`

**Interfaces:**
- Consumes: Task 1 的快照（验证时按单元看 SQL）
- Produces: `ReportPlanUnit.time_range: ReportPlanUnitTimeRange | None`；`planner.extract_time_range(text) -> TimeRange | None`

- [ ] **Step 1: 写失败测试（复现事故）**

```python
# tests/test_reporting/test_unit_time_range.py
"""单元时间范围抽取契约（复现 2026-09-14 事故）。"""

from __future__ import annotations

import pytest

_ACCIDENT_PROMPT = (
    "立即生成一份《集团经营全景分析报表》，时间范围 2016-01-01 至 2036-12-31，页面保留 30 天。\n"
    "1. 销售基盘：按年月统计订单净销售额、订单数；\n"
    "2. 目标达成：按门店和年月对比销售目标与实际净销售额。"
)


class TestUnitTimeRange:
    """覆盖时间范围从需求到单元、再到查询的传递。"""

    # 方法作用：验证需求里的绝对时间范围落到每个单元。
    async def test_absolute_range_reaches_every_unit(self, monkeypatch) -> None:
        """事故：prompt 写了十年区间，22 个单元却全是默认 90d。"""
        # Arrange
        import src.llm.client as llm_client
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)

        # Act
        plan = await ReportPlanner().parse(_ACCIDENT_PROMPT, datasources=["sales"])

        # Assert
        for unit in plan.analysis_units:
            assert unit.time_range.kind == "absolute"
            assert unit.time_range.value == "2016-01-01/2036-12-31"

    # 方法作用：验证需求未写时间时保持相对默认值。
    async def test_missing_range_keeps_relative_default(self, monkeypatch) -> None:
        # Arrange
        import src.llm.client as llm_client
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)

        # Act
        plan = await ReportPlanner().parse("1. 销售：按年月统计净销售额；2. 库存：按商品统计库存量。", datasources=["sales"])

        # Assert
        assert plan.analysis_units[0].time_range.kind == "relative"

    # 方法作用：验证时间范围被拼进单元查询。
    def test_query_includes_absolute_range(self) -> None:
        # Arrange
        from src.reporting.models import AnalysisUnit, TimeRange
        from src.reporting.runner import _compose_unit_query

        unit = AnalysisUnit(unit_id="u1", question="按年月统计净销售额", time_range=TimeRange(kind="absolute", value="2016-01-01/2036-12-31"))

        # Act
        query = _compose_unit_query(unit)

        # Assert
        assert "2016-01-01" in query and "2036-12-31" in query
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_reporting/test_unit_time_range.py -v`
Expected: FAIL（时间为 `relative/90d`；查询不含区间）

- [ ] **Step 3: 契约加字段**

```python
class ReportPlanUnitTimeRange(StrictOutputModel):
    """单元的时间范围建议；模型无法判断时留空，由服务端补默认值。"""

    kind: Literal["relative", "absolute", "all"] = "relative"
    value: str = Field(default="", max_length=64)


# ReportPlanUnit 内新增字段
    time_range: ReportPlanUnitTimeRange | None = None
```

- [ ] **Step 4: 提示词交代抽取规则**

在 `report.plan` 的编号层级段之后追加（注意模板用 `{{ }}` 转义）：

```
"从需求里抽取时间范围，每个单元的 time_range 都要给："
"需求写了绝对区间（如「时间范围 2016-01-01 至 2036-12-31」）就用 "
"{{\"kind\": \"absolute\", \"value\": \"起/止\"}}（ISO 日期、用 / 分隔）；"
"写了相对区间（如「最近三个月」「近 90 天」）就用 {{\"kind\": \"relative\", \"value\": \"90d\"}}；"
"需求完全没写时间就留空，不要自己编一个区间。"
"时间范围决定取数窗口：写窄了会取不到同比/环比的基期，必须忠实于用户原话。"
```

- [ ] **Step 5: 规则层兜底抽取**

```python
# planner.py 模块级
# 绝对区间：2016-01-01 至 2036-12-31（兼容 到/~/-/—/起 等连接词）。
_ABSOLUTE_RANGE_RE = re.compile(
    r"((?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2})\s*(?:至|到|~|-|—|起)\s*((?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2})"
)
# 相对区间：最近/近/过去 3 个月、90 天、2 年。
_RELATIVE_RANGE_RE = re.compile(r"(?:最近|近|过去|往前)\s*(\d{1,4})\s*(天|周|个月|月|年)")
```

新增 `extract_time_range(text) -> TimeRange | None`（绝对优先，其次相对，都没有返回 None）。
在构造 `ReportPlan` 之前，对**所有** `time_range == TimeRange()` 的单元套用抽取结果；LLM 已给出 `time_range` 的单元优先生效。
既有的 `year_match`「XXXX年…报表」补丁改为复用本函数（`extract_time_range` 覆盖该写法），删掉重复逻辑。

- [ ] **Step 6: 查询拼装带上区间**

`_compose_unit_query(unit)` 在问题文本后追加一行：

- `kind == "absolute"`：`时间范围：2016-01-01 至 2036-12-31（查询必须覆盖该区间；需要同比/环比时同时取到基期）。`
- `kind == "relative"`：`时间范围：最近 {value}。`
- `kind == "all"`：不追加。

- [ ] **Step 7: 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_reporting -q` → 全绿

---

## Task 3: 取消硬编码暴力截断

**Files:**
- Modify: `src/config.py`（阈值可配）
- Modify: `src/graph/nodes/multi_source.py:266,270`、`merge_results_node`
- Modify: `src/graph/nodes/execute_sql.py:179`
- Create: `src/reporting/sample_guard.py`
- Modify: `src/llm/prompts.py`（`report.sample_guard`）
- Test: `tests/test_reporting/test_sample_policy.py`

**关键约束：** `multi_source.py:270` 的 `len(data) > 50` 有既有测试守着（`tests/test_graph/test_multi_source_truncation.py::test_worker_preserves_sample_cut`）——那是**给模型的提示词样本被裁**的语义，本身没错。改法是把它与**用户可见截断**分开：保留 `sample_capped`（原 `truncated` 的 50 行语义），报表侧不再消费它（Task 中已由 `display_truncated` 保证）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_reporting/test_sample_policy.py
"""样本策略契约：阈值可配、不再静默砍行、超限可解释。"""

from __future__ import annotations


class TestSamplePolicy:
    """覆盖阈值来源与截断原因。"""

    # 方法作用：验证样本阈值来自配置且默认放宽。
    def test_threshold_is_configurable(self) -> None:
        # Arrange / Act
        from src.config import Settings

        field = Settings.model_fields["report_max_sample_rows"]

        # Assert
        assert field.default >= 1000

    # 方法作用：验证超阈值时产出可解释的截断原因而不是静默丢弃。
    def test_overflow_produces_reason(self) -> None:
        # Arrange / Act
        from src.reporting.snapshots import describe_truncation

        reason = describe_truncation(displayed=5000, full_count=80000, limit=5000)

        # Assert
        assert "80000" in reason and "5000" in reason

    # 方法作用：验证未超限时不产生截断措辞。
    def test_within_limit_has_no_reason(self) -> None:
        from src.reporting.snapshots import describe_truncation

        assert describe_truncation(displayed=60, full_count=60, limit=5000) == ""
```

- [ ] **Step 2: 跑测试确认失败** → `ImportError` / `KeyError: 'report_max_sample_rows'`

- [ ] **Step 3: 加配置**

```python
# src/config.py
    # 报表单元样本上限：原 50/30/200 三处硬编码会静默砍行，且与"给模型的
    # 提示词样本"混为一谈。放宽为可配阈值，超出时不再静默丢弃。
    report_max_sample_rows: int = Field(default=5000, ge=100, le=100_000)
```

- [ ] **Step 4: 逐处替换硬编码**

| 位置 | 原值 | 改后 |
|---|---|---|
| `multi_source.py:266` `data[:50]` | 50 | 保持 50（**这是给模型的提示词样本**，与展示无关），但标记改名 `sample_capped` |
| `multi_source.py:270` | `len(data) > 50` 计入 `truncated` | 移出 `truncated`，改为 `sample_capped = len(data) > 50` |
| `merge_results_node` `len(batch) > 30` / `len(all_data) > 200` | 30/200 | 同源语义归入 `sample_capped`；`truncated` 只由真实执行截断决定 |
| `execute_sql.py:179` `masked_rows[:200]` | 200 | `masked_rows[:settings.report_max_sample_rows]` |

同步更新 `tests/test_graph/test_multi_source_truncation.py` 的断言（`truncated` → `sample_capped`），保留其原有意图。

- [ ] **Step 5: 超限交给模型判断**

`src/reporting/sample_guard.py`：

```python
class SampleDecision(StrictOutputModel):
    """样本超限时的处置建议。"""

    action: Literal["aggregate", "narrow_range", "keep"] = "keep"
    reason: str = Field(default="", max_length=300)
    suggested_group_by: list[str] = Field(default_factory=list, max_length=10)
    suggested_range: str = Field(default="", max_length=64)
```

提示词 `report.sample_guard` 输入：返回行数 / 查询全量行数 / 时间跨度 / 列清单 / 单元问题。要求模型在**加 GROUP BY 聚合**与**缩小时间范围**之间选一个并说明理由，都不必要时选 `keep`。

**第一版只登记不自动重跑**：决策结果写入快照的 `params_json` 与页面 `limitations`，重跑统一由 Task 4 的补查链路处理，避免两条自动回路叠加。

- [ ] **Step 6: 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph tests/test_reporting -q` → 全绿

---

## Task 4: 时间跨度不足时触发补查

**Files:**
- Modify: `src/reporting/profiling.py`（新增检测函数）
- Modify: `src/graph/subgraphs/report_workbench.py`（前置筛选增加该触发条件）
- Test: `tests/test_reporting/test_supplement_for_missing_baseline.py`

**关键：** 现有运行期发散回路的前置筛选要求「额度未用尽 + 轮次未到 + 单元数 < 12 + **至少一条 high/moderate 发现**」。事故报表只拿回单月数据，**恰好产不出任何发现**，于是回路永不触发、同比永远为 null。新增一条**独立于发现**的触发条件。

**Interfaces:**
- Produces: `needs_baseline_supplement(unit, rows, findings) -> str | None`（返回补查理由；None 表示不需要）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_reporting/test_supplement_for_missing_baseline.py
"""缺同比/环比基数时的补查触发契约。"""

from __future__ import annotations


class TestBaselineSupplement:
    """覆盖"数据残缺也要能补查"。"""

    # 方法作用：验证只有单月数据但要求同比时触发补查。
    def test_single_month_with_yoy_request_triggers_supplement(self) -> None:
        """事故：单月数据产不出发现，回路门槛因此永不满足。"""
        # Arrange
        from src.reporting.profiling import needs_baseline_supplement

        rows = [{"year_month": "2026-06", "net_sales": 100}]
        unit = {"question": "按年月统计净销售额并给出同比"}

        # Act
        reason = needs_baseline_supplement(unit, rows, findings=[])

        # Assert
        assert reason and "同比" in reason

    # 方法作用：验证跨度足够时不补查。
    def test_sufficient_span_does_not_supplement(self) -> None:
        # Arrange
        from src.reporting.profiling import needs_baseline_supplement

        rows = [{"year_month": f"2026-{month:02d}", "net_sales": 100 + month} for month in range(1, 13)]
        rows.append({"year_month": "2025-06", "net_sales": 90})

        # Act / Assert
        assert needs_baseline_supplement({"question": "按年月统计净销售额并给出同比"}, rows, findings=[]) is None

    # 方法作用：验证未要求同环比时不补查。
    def test_no_baseline_request_does_not_supplement(self) -> None:
        from src.reporting.profiling import needs_baseline_supplement

        rows = [{"year_month": "2026-06", "net_sales": 100}]
        assert needs_baseline_supplement({"question": "列出本月净销售额"}, rows, findings=[]) is None
```

- [ ] **Step 2: 跑测试确认失败** → `ImportError`

- [ ] **Step 3: 实现检测**

判定规则：单元问题含「同比/去年同期/上年同期」→ 需要 ≥13 个月跨度（含基期）；含「环比/上月/上期」→ 需要 ≥2 个周期。用 `detect_time_column` + 日期解析取实际跨度，不足则返回可读理由（写明缺什么基数、当前跨度多少）。

- [ ] **Step 4: 接入前置筛选**

在 `report_decide_supplement` 的前置筛选里增加：`baseline_reason` 非空即视为满足进入条件（与"至少一条 high/moderate 发现"是**或**关系）。

- [ ] **Step 5: 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_reporting -q` → 全绿

---

## Task 5: 多图/多系列/可切换的图表契约

> **范围变更（2026-09-14 用户裁定）**：「图表类型写死为 bar」与并行进行的
> `spec/49-report-presentation-redesign.md` 重叠，**归该设计主导** —— 它主张复用对话路径
> 已验收的既有实现（`chart_generator.py::_classify_chart_type`/`_build_option`），
> 而不是另造第三套图表构建。本 task 只补 spec/49 未覆盖的部分：
> **一个单元出多张图、一张图多系列、维度可切换**的后端契约与前端渲染。
>
> **执行前置**：先读 `spec/49` 最新状态并对齐字段名，**不得另造一套结构**。若 spec/49 尚未
> 落地，本 task 顺延到最后执行。

**Files:**
- Modify: `src/reporting/document.py`（chart 块支持 `charts` 数组）
- Modify: `src/llm/output_contracts.py`（图表方案契约，与 spec/49 对齐命名）
- Modify: `frontend/src/components/report/ReportDocumentRenderer.tsx`（多图与切换）
- Test: `tests/test_reporting/test_chart_plan_contract.py`

**Interfaces:**
- Produces:

```python
class ChartSpec(BaseModel):
    """单张图表的呈现方案。"""

    chart_type: Literal["line", "bar", "pie", "scatter", "heatmap", "table"] = "bar"
    title: str = ""
    x_field: str = ""
    series_fields: list[str] = Field(default_factory=list, max_length=10)
    group_field: str = ""                      # 非空表示可切换维度
    switchable_fields: list[str] = Field(default_factory=list, max_length=10)


class ReportChartPlan(BaseModel):
    """一个单元的图表方案：可多张图，每张可多系列、可切换。"""

    charts: list[ChartSpec] = Field(default_factory=list, max_length=6)
```

- [ ] **Step 1: 写失败测试**

```python
# tests/test_reporting/test_chart_decision.py
"""图表方案决策契约：类型来自决策，不再写死 bar。"""

from __future__ import annotations


class TestChartDecision:
    """覆盖图表类型来源与多图能力。"""

    # 方法作用：验证图表类型来自决策结果而不是硬编码。
    def test_chart_type_comes_from_decision(self, monkeypatch) -> None:
        """事故：20 个图表块 series.type 全是 bar。"""
        # Arrange
        import src.llm.client as llm_client
        from src.reporting.charts import ChartSpec, ReportChartPlan, plan_charts

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)
        monkeypatch.setattr(
            "src.reporting.charts._invoke_chart_plan",
            lambda *a, **k: ReportChartPlan(charts=[ChartSpec(chart_type="line", title="净销售额趋势", x_field="year_month", series_fields=["net_sales"])]),
        )

        # Act
        plan = plan_charts(question="按年月统计净销售额", columns=["year_month", "net_sales"], rows=[{"year_month": "2026-06", "net_sales": 1}])

        # Assert：类型来自决策结果
        assert plan.charts[0].chart_type == "line"

    # 方法作用：验证一个单元可以产出多张图。
    def test_unit_can_emit_multiple_charts(self, monkeypatch) -> None:
        """多指标单元应能按指标拆成多张图，而不是只画第一个数值列。"""
        # Arrange
        from src.reporting.charts import ChartSpec, ReportChartPlan
        from src.reporting.document import build_report_document
        from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult

        monkeypatch.setattr(
            "src.reporting.charts._invoke_chart_plan",
            lambda *a, **k: ReportChartPlan(charts=[
                ChartSpec(chart_type="line", title="净销售额", x_field="year_month", series_fields=["net_sales"]),
                ChartSpec(chart_type="bar", title="订单数", x_field="year_month", series_fields=["order_count"]),
            ]),
        )
        plan = ReportPlan(analysis_units=[AnalysisUnit(unit_id="u1", question="按年月统计净销售额与订单数")])

        # Act
        artifact = build_report_document(plan, [UnitResult(unit_id="u1", rows=[{"year_month": "2026-06", "net_sales": 1, "order_count": 2}])])

        # Assert
        charts = [b for s in artifact.document.sections for b in s.blocks if b.type == "chart"]
        assert len(charts) == 2

    # 方法作用：验证决策不可用时按列类型确定性降级而不是回到 bar。
    def test_fallback_infers_by_column_type(self) -> None:
        from src.reporting.charts import fallback_chart_plan

        plan = fallback_chart_plan(columns=["year_month", "net_sales"], rows=[{"year_month": "2026-06", "net_sales": 1}])
        assert plan.charts and plan.charts[0].chart_type == "line"
```

- [ ] **Step 2: 跑测试确认失败** → `ImportError`

- [ ] **Step 3: 实现决策与降级**

- 有 LLM：`report.chart` 输入「列名 + 每列样例值 + 行数 + 单元问题 + 可用图表类型」，输出 `ReportChartPlan`。提示词要求按**数据形态**选类型（时间序列→line；分类对比→bar；构成占比→pie；两数值相关→scatter；二维交叉→heatmap），并给出是否多图、哪些列做系列、哪个列可切换维度。
- 无 LLM / 决策失败：`fallback_chart_plan` 按列类型确定性推断（时间列+数值列→`line`；分类列+数值列→`bar`），只出一张图。**降级不得回退到"永远 bar"**。
- `document.py` 用决策结果构造 chart 块；三种形态都支持：多图（`charts` 多条 → 多个 chart 块）、多系列（`series_fields`）、可切换（`group_field` + `switchable_fields`）。

- [ ] **Step 4: 前端渲染多图与切换**

`ReportDocumentRenderer` 的 chart 块支持 `charts` 数组：逐张渲染；`switchable_fields` 非空时在图上方渲染维度切换 `Segmented`，切换只改本地 series 选择，不重新请求后端。

- [ ] **Step 5: 全量验证 + tsc**

---

## Task 6: 单元标题由大模型生成

**Files:**
- Modify: `src/reporting/models.py`（`AnalysisUnit` 加 `title`）
- Modify: `src/llm/output_contracts.py`（`ReportPlanUnit` 加 `title`）
- Modify: `src/llm/prompts.py`（`report.plan` 要求 title）
- Modify: `src/reporting/document.py::unit_short_title`（优先用 LLM title）
- Test: `tests/test_reporting/test_report_content_shape.py`（扩展）

- [ ] **Step 1: 写失败测试**

```python
# 方法作用：验证标题优先取模型给的精简标题。
def test_section_title_prefers_model_title() -> None:
    """事故：标题是 question 前 20 字加省略号（「按年月、地区、渠道以及历史模拟/当年/未…」）。"""
    # Arrange
    from src.reporting.document import build_report_document
    from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult

    plan = ReportPlan(analysis_units=[AnalysisUnit(
        unit_id="u1",
        question="按年月、地区、渠道以及历史模拟/当年/未来情景，统计订单净销售额",
        title="销售基盘",
    )])

    # Act
    artifact = build_report_document(plan, [UnitResult(unit_id="u1", rows=[{"a": 1}])])

    # Assert
    section = next(item for item in artifact.document.sections if item.id == "u1")
    assert section.title == "销售基盘"
```

- [ ] **Step 2: 跑测试确认失败** → `TypeError`（`AnalysisUnit` 不接受 `title`）

- [ ] **Step 3: 实现**

- `AnalysisUnit.title: str = Field(default="", max_length=20)`；`ReportPlanUnit.title: str = ""`（≤12 字）。
- `report.plan` 提示词加：「为每个单元给一个不超过 12 字的精简标题，概括业务主题；不要照抄问题原文、不要以"按年月"这类维度罗列开头。」
- `unit_short_title(question, unit_id, title="")`：`title` 非空直接返回；否则维持现有冒号切分/截断回退。
- `_coerce_llm_units` 把模型的 `title` 透传进 `AnalysisUnit`。

- [ ] **Step 4: 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_reporting -q` → 全绿

---

## Task 7: 报表独立任务提示词

**Files:**
- Modify: `src/llm/prompts.py`
- Test: `tests/test_llm/test_prompt_registry.py`（扩展）

**设计：** 报表专属提示词独立注册（`report.plan` / `report.diverge` / `report.route` / `report.supplement` / `report.chart` / `report.sample_guard` / `report.title`），**通用约束段抽成模块级常量**供报表与对话分析共同引用。**不动节点路由**：静态图 + 关键点 LLM 决策（运行期发散回路为既有先例）保持不变。

- [ ] **Step 1: 写测试**

```python
# 方法作用：验证通用约束段被两侧共同引用，而不是各写一份。
def test_shared_prompt_segments_are_reused() -> None:
    from src.llm import prompts

    assert prompts.COMMON_NO_FABRICATED_DATASOURCE in prompts.get_prompt_definition("report.plan").template
    assert prompts.COMMON_NO_FABRICATED_DATASOURCE in prompts.get_prompt_definition("sql.generate").template
```

- [ ] **Step 2: 跑测试确认失败** → `AttributeError`

- [ ] **Step 3: 抽取常量并替换**

把现有重复措辞（「不得编造数据源名」「比率列不可相加」「弱关联不得写成因果」「不得为凑数编造」）抽成模块级常量，报表与对话两侧提示词共同引用。

- [ ] **Step 4: 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_llm -q` → 全绿

---

## 验证与收尾（全部完成后）

1. `.venv/Scripts/python.exe -m pytest -m "not live_llm" -q` → 全绿（当前基线：2876 passed，2 个既有 `sql_rewriter` 隔离失败）
2. `cd frontend && node node_modules/typescript/bin/tsc --noEmit` → exit 0
3. **浏览器实跑**：用事故 prompt 重新生成一份报表，确认：
   - 单元 `time_range` 为绝对区间，SQL 覆盖 2016–2036
   - 图表类型不再清一色 bar；多指标单元有多张图或可切换维度
   - 标题是精简业务名，不是截断的句子
   - 快照能按单元查到 SQL / 提示词 / 行数 / 截断原因；「重跑本单元」可用
4. 更新 `spec/30`（时间范围抽取、图表决策、截断策略）、`spec/41:566`（该待办标记为已实施）、`spec/42`（样本披露与补查门槛）、`features/23-report-workbench.md`
5. **提交由用户明确指示后再执行**（项目规则：禁止自动 commit）
