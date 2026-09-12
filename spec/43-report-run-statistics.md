# 报表运行计数统计设计（LLM 调用 · SQL 执行 · 成败）

> 状态：设计待评审
> 关联：spec/30-report-workbench.md（报表工作台）、spec/42-report-divergent-analysis.md（发散分析，会显著增加 LLM 与 SQL 次数）
> 关联功能点：features/23-report-workbench.md（23.21 报表运行计数统计）

---

## 1. 背景与缺陷

### 1.1 需求

自动化报表运行完成后需要计数统计：调用了多少次大模型、执行了多少次 SQL、失败了多少、成功了多少。

### 1.2 现状：字段存在，但填的是**估算值**

`report_runs` 表已有三个相关字段（`migrations/018_report_workbench.sql:41-43`）：

| 字段 | 现状 | 真实性 |
|---|---|---|
| `query_count` | `runner.py:131` 写入 `plan.estimated.query_count` | ❌ **planner 的估算**（`planner.py:117`：`min(1000, max(1, fanout))`） |
| `token_usage` | `runner.py:131` 写入 `plan.estimated.llm_tokens` | ❌ **估算** |
| `row_count` | `runner.py:116` 写入 `sum(len(item.rows))` | ✅ 真实 |

**即界面上显示的"查询次数"是计划阶段的预测值，不是实际发生值。** 这是本设计要修的核心缺陷，也是本需求的价值所在。

`report_run_steps`（`018_report_workbench.sql:53-69`）有每节点的 `status`，但它记录的是报表子图节点，**不含 LLM 调用与 SQL 执行粒度**。

全仓库 grep `token_usage|record_usage|usage_record|llm_call` 在 `src/llm/` 与 `src/observability/` **零命中** —— 不存在任何 LLM 调用计数机制。

### 1.3 LLM 侧已有指标基建，但是**全局 Prometheus，不是 per-run**

`src/observability.py` 已实现完整的 LLM 指标链路：

| 符号 | 位置 | 已有能力 |
|---|---|---|
| `MetricsRegistry.record_llm()` | `:88-108` | 记录一次调用的 task/model/**status（success\|error）**/耗时/token |
| `LLMMetricsCallback` | `:144` | LangChain 回调基类；`on_llm_start`（`:158`）、`on_llm_end`（`:179`，**已提取真实 token**）、`on_llm_error`（`:201`） |
| `attach_llm_metrics(model, task)` | `:272` | 把回调挂到 model 上；**在 `get_task_llm` 的两个返回分支都被调用**（租户分支 `client.py:279-281`、回退分支 `:316-318`） |
| `_extract_token_usage(response)` | `:333` | 从 `LLMResult` 提取 prompt/completion token，兼容 `token_usage` 与 `usage` 两种字段名 |

**结论：不新建包装代理，扩展既有 `LLMMetricsCallback`。** 它已覆盖全部 LLM 调用（`get_task_llm` 是项目 CLAUDE.md 规定的统一入口），且已经区分成功与失败。

缺的只是"把计数落到**这一次运行**上" —— Prometheus 是全局聚合，没有 run 维度。这正是本设计要补的。

**顺带修正 spec 的一个错误判断**：原稿把"真实 token 数"列为不做项，理由是"需从 `usage_metadata` 提取，涉及 provider 差异"。实际上 `_extract_token_usage`（`:333`）**已经处理了这些差异**，`on_llm_end` 里现成拿到 `prompt_tokens` 与 `completion_tokens`。因此真实 token 数顺手可得，纳入本设计。

SQL 侧唯一执行入口是 `src/security/sql_execution.py:566` 的 `validate_and_execute_sql()`（调用方：`src/graph/nodes/execute_sql.py:146`、`src/reporting/live_drilldown.py:76`、`src/automation/runner.py:231`、`src/api/routes/grafana.py:21`）。

---

## 2. 计数口径（必须先定义清楚，否则数字无法解释）

| 字段 | 口径 | 说明 |
|---|---|---|
| `llm_call_count` | **发起 LLM 调用的次数**（含失败） | 对应 `LLMMetricsCallback.on_llm_start`。重试各计 1 次（真实计费口径） |
| `llm_failure_count` | 其中失败的次数 | 对应 `on_llm_error`；`llm_success = llm_call_count - llm_failure_count` |
| `token_usage`（既有列，**改为真实值**） | prompt + completion token 之和 | 来自 `on_llm_end` 已提取的 `prompt_tokens`/`completion_tokens`。见 4.1 的语义变更说明 |
| `sql_query_count` | **提交执行的 SQL 次数**（含失败） | 走 `validate_and_execute_sql` 的每一次调用计 1，**校验未通过而未提交执行的也计 1**（它确实被尝试过） |
| `sql_success_count` | 执行返回成功 | `SQLExecutionResult` 非失败态 |
| `sql_failure_count` | 执行返回失败 | 含 SQL 校验失败、执行异常、超时 |

**边界约定**：

- 一条 SQL 因 Layer3 校验失败被重试 → 计 2 次 `sql_query_count`（1 失败 + 1 成功，或 2 失败）。这是真实尝试次数。
- "没有数据源能支撑该问题"（`runner.py:192` 的分支）**不计入 SQL 失败** —— 没有执行 SQL，是可解释的结论。这与该处既有注释的语义保持一致。
- 报表子图节点数**不是**计数口径，不用于推导 LLM 或 SQL 次数。

---

## 3. 架构：contextvar + 可变计数器对象

### 3.1 关键陷阱：asyncio task 会复制 contextvar

`contextvars` 在 `asyncio.create_task()` 时会**复制**当前上下文。若计数器是**不可变 int** 存在 contextvar 里，子 task 里的 `counter.set(n+1)` 只改子 task 自己的副本，**父 task 读不到**。

报表单元执行调用 `app.ainvoke()`（`runner.py:207`），LangGraph 内部会创建 task，因此这个陷阱**必然触发**。

**解决（两条路径，强度不同）**：

| 计数 | 机制 | 可靠性 |
|---|---|---|
| LLM | `attach_llm_metrics` 在**运行上下文内**被调用（`get_task_llm` 的执行者就是发起该次 LLM 调用的节点），因此在挂载时**按引用捕获**当前 `RunStats` 对象并传给回调。回调直接对捕获的引用计数，**不依赖 contextvar 穿过 LangChain 内部 task** | 最强 —— 甚至不要求计数器是 contextvar 可传播的，因为它拿的是对象引用 |
| SQL | `validate_and_execute_sql` 在同一条调用链上，读 contextvar。contextvar 复制的是**同一个可变对象的引用**，对属性的修改对所有 task 可见 | 成立，但**前提是计数器必须是可变对象** |

**实现约束**：contextvar 里存的必须是 `RunStats` **实例**（可变），不得写成 int、tuple 或 frozen dataclass。改成不可变结构会让 SQL 计数静默丢失（不报错、只是数字偏小），这是最难发现的一类 bug —— 必须有专门测试覆盖（见第 7 节）。

### 3.2 新增 `src/reporting/run_stats.py`

```python
class RunStats:
    """一次报表运行的真实计数（LLM 调用 · token · SQL 执行）。

    前提：执行模型是 asyncio 单线程事件循环，属性自增不会撕裂。
    若将来引入多线程/多进程执行，需改为带锁实现。
    """
    llm_call_count: int = 0
    llm_failure_count: int = 0
    llm_token_usage: int = 0
    sql_query_count: int = 0
    sql_success_count: int = 0
    sql_failure_count: int = 0

_current: ContextVar[RunStats | None] = ContextVar("report_run_stats", default=None)

# 方法作用：读取当前作用域的计数器，无活动计数器时返回 None。
# Args: 无。
# Returns: 当前 RunStats 实例或 None。
def current() -> RunStats | None

# 方法作用：在报表运行作用域内激活计数器。
# Args: 无。
# Returns: 本次运行的计数器对象与用于恢复上下文的 Token。
def activate() -> tuple[RunStats, Token]

# 方法作用：记录一次 LLM 调用结果，无活动计数器时是空操作。
# Args: failed - 本次调用是否失败；tokens - 本次消耗的 token 数。
# Returns: 无返回值。
def record_llm_call(failed: bool = False, tokens: int = 0) -> None

# 方法作用：记录一次 SQL 执行结果，无活动计数器时是空操作。
# Args: failed - 本次执行是否失败。
# Returns: 无返回值。
def record_sql_execution(failed: bool = False) -> None
```

**无活动计数器时是 no-op**：聊天、自动化、Grafana 等非报表路径走同一份埋点代码，不受影响、无额外开销。

### 3.3 埋点位置

| 位置 | 改动 |
|---|---|
| `src/observability.py` 的 `LLMMetricsCallback` | 构造函数新增可选的 `stats: RunStats \| None` 参数。`on_llm_start` 记 `llm_call_count`；`on_llm_end` 记 `llm_token_usage += prompt+completion`；`on_llm_error` 记 `llm_failure_count`。**`stats is None` 时全部跳过**（聊天等其他路径零开销） |
| `src/observability.py` 的 `attach_llm_metrics()`（`:272`） | 挂载时调用 `run_stats.current()` **按引用捕获**当前计数器并传给回调。此处已在运行上下文内，无需依赖 contextvar 在后续传播 |
| `src/security/sql_execution.py` 的 `validate_and_execute_sql()`（`:566`） | 返回前按结果记 `record_sql_execution(failed=...)`。**只加 1~2 行**，不动既有校验与执行逻辑 |

**为什么不在 `get_task_llm` 里包代理**：既有 `LLMMetricsCallback` 已经是官方回调挂载点，且已区分成功/失败、已提取 token。包代理是重复实现，还会破坏 `attach_llm_metrics` 的"保持对象身份不变"契约（该函数 docstring 明文如此）。

**冲突提示**：`src/security/sql_execution.py` 处在另一条并行开发线的活跃目录（SQL Schema 校验层）。埋点刻意放在函数返回处、位置独立，便于 git 自动合并。

### 3.4 并发安全

asyncio 单线程事件循环下 `+=` 不会撕裂。若将来引入多线程或多进程执行，需改为带锁实现 —— 当前不做（YAGNI），但在类 docstring 中注明该前提。

---

## 4. 存储

新增 `migrations/029_report_run_stats.sql`：

```sql
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS llm_call_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS llm_failure_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS sql_query_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS sql_success_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS sql_failure_count INTEGER NOT NULL DEFAULT 0;
```

### 4.1 `token_usage` 语义变更（唯一一处既有列语义调整）

`token_usage` 当前在 `runner.py:131` 写入 `plan.estimated.llm_tokens` —— 即**把估算值写进实际值字段**。核实其消费者：

- `store.py:262` 白名单、`store.py:239` 初始 0、`store.py:1078` 读出到 `ReportRunRecord`、`models.py:310` 字段定义。
- **没有任何读取方用它做配额或计费判断** —— 配额走 `plan.estimated.llm_tokens`（`store.py:952`），与这一列无关。它是纯展示字段。
- 因此把 `token_usage` 改为**真实 token 数**是**修复**，不是破坏。无需新增重复列。

### 4.2 `query_count` 保持原语义不动

`query_count` 是**配额计费口径的估算值**，配额预占与释放依赖它（`store.py:952`、`runner.py:305-324`），改语义会波及配额链路。新增的 `sql_query_count` 是真实观测值，两者并存。

界面上可对比展示"预估 6 次 / 实际 4 次" —— 这个对比本身对用户有价值，也是验证 planner 估算准确度的观测手段。

### 4.3 读写同步点

- `store.update_run()` 的白名单（`store.py:262`）加入五个新字段。
- `ReportRunRecord`（`models.py:308-310`）加对应字段；行转记录方法（`store.py:1078`）同步读取。
- `store.create_run()` 的初始行（`store.py:239`）补默认 0。

---

## 5. 展示

`runner.run_job()` 在 `finally`（`runner.py:157-161`）释放配额前，读取 `RunStats` 并随既有的 `update_run` 一起落库。

- 私有 API `GET /report-workbench/runs/{run_id}` 与运行列表接口自动带回新字段（`ReportRunRecord` 已扩展）。
- 前端在**运行日志抽屉**（`ReportWorkbenchPage.tsx` 现有 `runSteps` 区域）展示一行统计：`LLM 调用 N 次（失败 M）/ SQL 执行 N 次（成功 M，失败 K）`。
- 失败数非 0 时该行用告警色，便于快速定位。

---

## 6. 错误处理与降级

| 场景 | 行为 |
|---|---|
| 非报表路径（聊天/自动化/Grafana/钻取） | 无活动计数器 → 埋点为 no-op，**零影响** |
| 计数落库失败 | 记 `logger.error` 并保留堆栈；**不影响报表发布**（统计是观测数据，不是业务结果） |
| 运行中途异常 | `finally` 中仍落库已累计的计数（真实发生的调用不应因失败而丢失） |
| 计数器未激活时落库 | 写入全 0，不报错 |
| 报表单元无命中数据源 | 不计入 SQL 失败（未执行 SQL） |

---

## 7. 测试

### `tests/test_reporting/test_run_stats.py`（新增）

| 测试函数 | 场景 |
|---|---|
| `test_record_is_noop_without_active_counter` | 未激活时调用 `record_llm_call` / `record_sql_execution` 不抛错、无副作用 |
| `test_llm_call_and_failure_counted` | 正常与失败各若干次 → 计数正确 |
| `test_token_usage_accumulates_both_prompt_and_completion` | token 累加来自 prompt + completion |
| `test_sql_execution_success_and_failure_counted` | 成功/失败分别计数 |
| `test_counter_survives_asyncio_task_copy` | **在 `asyncio.create_task` 子任务内计数 → 父作用域可读到**（3.1 陷阱的核心用例） |
| `test_counter_is_mutable_object_not_immutable` | 断言 `RunStats` 不是 frozen/不可变（防止有人改成 dataclass(frozen=True) 导致 SQL 计数静默丢失） |
| `test_activate_and_reset_restores_previous` | 嵌套激活/重置后回到上一状态 |
| `test_llm_callback_records_to_captured_stats` | `LLMMetricsCallback` 用挂载时捕获的 `RunStats` 计数（不依赖 contextvar 传播） |
| `test_llm_callback_is_noop_without_stats` | `stats=None` 时回调不计数、不抛错（聊天等非报表路径） |
| `test_counts_persisted_on_run_finish` | 运行结束落库，`ReportRunRecord` 返回真实值 |
| `test_counts_persisted_even_when_run_fails` | 运行异常时 `finally` 仍落库已累计计数 |
| `test_existing_query_count_semantics_unchanged` | `query_count` 仍写配额估算值（防止有人"顺手"改成真实值破坏配额链路） |
| `test_no_hit_unit_not_counted_as_sql_failure` | 无命中数据源的单元不计入 SQL 失败 |

### `tests/test_observability/`（扩展，若该目录存在；否则并入 `test_reporting`）

| 测试函数 | 场景 |
|---|---|
| `test_existing_metrics_still_recorded_when_stats_absent` | 不传 `stats` 时既有 Prometheus 指标行为完全不变（回归断言） |

### `tests/test_frontend/test_report_workbench.py`（扩展）

| 测试函数 | 场景 |
|---|---|
| `test_workbench_displays_run_counts` | 运行日志区域渲染 LLM/SQL 计数 |

---

## 8. 明确不做（YAGNI）

| 不做 | 原因 |
|---|---|
| 修改 `query_count` 的既有语义 | 配额链路依赖它，改动风险远大于收益 |
| 在 `get_task_llm` 外面再包一层计数代理 | 既有 `LLMMetricsCallback` 已是官方挂载点且已区分成败与提取 token；重复实现会破坏 `attach_llm_metrics` 的"保持对象身份不变"契约 |
| 多线程/多进程下的原子计数 | 当前执行模型是 asyncio 单线程，加锁是无收益的复杂度 |
| 按单元/按数据源分解的细粒度统计 | 需求是运行级汇总；细粒度可从 `report_run_steps` 扩展，属后续 |
| 把计数暴露为 Prometheus 指标 | 已有独立可观测性体系；本设计只做运行级持久化统计 |
| 历史运行的计数回填 | 历史数据没有真实值可回填，填 0 比填假值诚实 |
