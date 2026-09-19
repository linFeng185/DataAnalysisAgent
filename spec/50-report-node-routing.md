# 报表单元节点路由由大模型决策设计（节点数据表 · 强护栏 · 静态兜底）

> 状态：已确认，实施中
> 关联：[30-report-workbench.md](30-report-workbench.md)、[42-report-divergent-analysis.md](42-report-divergent-analysis.md)
> 关联功能点：features/23-report-workbench.md

---

## 1. 背景与问题

### 1.1 诉求

报表单元执行当前直接复用对话主工作流的**静态图**（`runner._execute_unit_workflow` → `workflow.app.ainvoke`）。
走哪些节点由 `workflow.py` 的条件边函数按**问题文本**推断，报表侧只能硬编码抑制其中几条
（`suppress_supplemental_query=True`、`request_rate_limit_checked=True`）。

这带来两个问题：

1. **报表单元的业务语义比对话更明确**，却拿不到比"按文本猜意图"更好的路由依据。例如
   「三方对账」天然该走多源合并，「人力成本」是单源取数——这些在单元层面已知，静态图却只能重新猜一遍。
2. **节点清单散落在代码里**，没有任何一份可交给模型阅读的"节点数据表"，模型无从判断该调用哪个节点。

### 1.2 现状实测

| # | 事实 | 位置 |
|---|---|---|
| 1 | 节点表只有 `name` / `handler` / `progress_message`，**没有路由元数据** | `src/graph/node_registry.py:14-20` |
| 2 | 静态图共 23 个节点，入口 `prepare_turn`，出口 `build_response` | `node_registry.py:82-106`、`workflow.py:320-364` |
| 3 | SQL 流与多源流是**两条互斥分支**，由 `route_by_intent` 决定 | `workflow.py:326-336` |
| 4 | 报表单元执行把 `session_id` 设为 `report-{run_id}-{unit_id}`，与对话线程隔离 | `runner.py:520` |

---

## 2. 设计目标与非目标

**目标**

- 维护一份**节点数据表**（节点名 / 用途 / 何时该用 / 产出什么 / 是否必选），可被渲染成提示词。
- 报表单元执行时，由模型从节点数据表里选出**本次要走的节点**。
- **强护栏**：模型的选择不得削弱安全边界；任何异常退回现有静态图。
- 仅作用于**报表单元执行路径**；对话路径完全不变。

**非目标**

- 不改 `workflow.py` 的静态图（它仍是对话路径与兜底路径的唯一执行体）。
- 不让模型决定节点的**内部行为**（提示词、SQL 生成规则不变）。
- 不做跨单元共享路由：每个单元独立决策。

---

## 3. 核心设计

### 3.1 节点数据表（`NodeDefinition` 扩展）

`NodeDefinition` 增加四个路由元数据字段，均为**可选**，未填写即视为「不可被模型选择」：

| 字段 | 说明 |
|---|---|
| `purpose` | 一句话说明这个节点做什么 |
| `when_to_use` | 什么情况下该选它（给人看，也给模型看） |
| `produces` | 产出什么，供模型判断后续是否还需要别的节点 |
| `mandatory` | 是否必选（安全与骨架节点为 True） |

### 3.2 规范顺序（canonical order）

**模型给的是"集合"，不是"序列"。** 这是本设计最关键的一条取舍：

让模型直接产出有序列表，等于把拓扑正确性与安全顺序交给模型保证——
一旦它把 `execute_sql` 排在 `layer3_validate` 之前就是**安全漏洞**。

因此：注册表维护一个**规范顺序**（与静态图一致的全序），模型只选**参与哪些节点**，
服务端按规范顺序排序后成序。

```
selected = mandatory ∪ (模型选择 ∩ 可路由白名单)
sequence = [name for name in CANONICAL_ORDER if name in selected]
```

### 3.3 强护栏（不可协商）

| # | 护栏 | 目的 |
|---|---|---|
| G1 | 白名单：只接受规范顺序里出现过的节点名，未知节点直接丢弃 | 防注入未知 handler |
| G2 | 无条件必选集合只包含 `prepare_turn` / `build_response`；SQL 安全节点是**路径条件必选**，不能塞进直答或多源骨架 | 避免空 SQL 先跑校验器 |
| G3 | **取数流完整性**：选择 SQL 生成、校验、执行、结果分析或图表中的任一节点 ⟹ 补齐完整 SQL 流；校验与执行之间复用静态图条件边。调用方已确认多个相关数据源时，SQL 选择必须提升为 `multi_source_dispatch → merge_results`，不得只用首源主标签执行单源图 | 安全校验不可绕过，失败不得继续执行；多源不得静默降级为单源 |
| G4 | 首节点固定 `prepare_turn`，末节点固定 `build_response` | 状态初始化与响应组装 |
| G5 | 序列长度上限（默认 16），超限截断 | 防失控 |
| G6 | 任何异常（模型不可用、契约校验失败、建图失败）→ **退回静态图 `workflow.app`** | 零回归风险 |

### 3.4 执行方式：按分支形态建**保留条件边的动态图**

节点集合仍由模型选择、服务端成序，但“成序”只用于确定分支形态和可选后处理节点，
**不能**把静态图的条件边降级成无条件直线。单源 SQL 路径必须调用
`src.graph.subgraphs.sql_analysis.add_sql_analysis_flow()`，与静态图共享以下语义：

- `generate_sql` 为空或幻觉时重试/终止，不得直接进校验；
- `layer3_validate` 失败时重试或进 `build_response`，不得执行 EXPLAIN；
- `layer4_explain` 失败时重试或终止，绝不进入 `execute_sql`；
- `execute_sql` 失败按错误类别重试或终止，只有成功才进入 `analyze_result`。

多源流（dispatch → merge）与直答流没有 SQL 条件边，可按受控固定边连接。无法归入这三种
完整形态的节点集合视为不完整决策，触发 G6 退回静态图。所有动态图继续复用
`AnalysisState`、同一批 handler 与独立 thread_id 的 checkpointer。

### 3.5 报表单元的默认可选集

报表单元执行已显式抑制旧的自主补查回环，故以下节点**默认不开放给模型**：
`supplemental_query`、`action_subgraph`、`save_preference`、`restore_previous_result`。
开放的是**取数形态**上的选择：单源 SQL 流、多源流、直答流、图表、预测子图。

---

## 4. 接口

```python
# src/graph/node_routing.py
class NodeRouteDecision(BaseModel):
    """模型给出的节点选择：只选参与哪些节点，顺序由服务端按规范顺序决定。"""
    nodes: list[str] = Field(default_factory=list, max_length=16)
    reason: str = Field(default="", max_length=300)

def render_node_catalog(*, allowed: set[str] | None = None) -> str: ...
def sanitize_node_sequence(chosen: list[str]) -> list[str]: ...
async def decide_node_sequence(question: str) -> NodeRouteDecision | None: ...
async def build_sequence_graph(sequence: list[str]) -> Any | None: ...
```

- `sanitize_node_sequence` **纯函数**，护栏 G1~G5 全部在此，便于单测穷举。
- `decide_node_sequence` 失败返回 `None`，调用方据此退回静态图（G6）。

---

## 5. 可观测性

选中的序列写入**单元快照**的 `params_json.routed_nodes`，与 SQL、提示词并列，
使"这个单元为什么这样跑"可反推。日志：决策入口 debug、生效时 info（含序列）、
失败时 warning（含降级原因），**不写 warning 到用户可见文案**。

---

## 6. 测试

| 用例 | 断言 |
|---|---|
| `test_canonical_order_covers_static_nodes` | 规范顺序覆盖静态图全部 SQL 流节点 |
| `test_unknown_node_is_dropped` | G1 |
| `test_mandatory_nodes_always_present` | G2 |
| `test_sql_flow_integrity_enforced` | G3：只给 `execute_sql` 或 `analyze_result` 时自动补齐完整 SQL 流 |
| `test_multi_source_sql_choice_is_promoted_to_multi_source_flow` | G3：多源上下文里的 SQL 选择提升为完整多源流，不遗漏后续来源 |
| `test_dynamic_sql_graph_reuses_conditional_flow` | 动态 SQL 图复用共享装配器，存在校验/EXPLAIN/执行失败分支 |
| `test_first_and_last_are_fixed` | G4 |
| `test_over_long_sequence_is_truncated` | G5 |
| `test_decision_failure_falls_back` | G6 |
| `test_catalog_lists_only_routable_nodes` | 目录渲染不含未开放节点 |

---

## 7. 验收

1. 全量 `pytest -m "not live_llm"` 通过。
2. 五库实跑：快照里每个单元都有 `routed_nodes`，且 22 个单元完成率不低于改造前。
3. 人为让模型返回非法节点名，确认被丢弃且报表正常产出（护栏实测）。
