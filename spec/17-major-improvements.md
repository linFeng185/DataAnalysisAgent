# 17. 主要缺点修复 — 详细设计文档

> 涵盖: #2 查询分解、#3 MCP Agent 路径、#9 重试透明化、#10 昂贵查询保护、#11 LLM 意图分类
> 五轮论证后定稿，后续开发严格按本文档。

---

## 17.1 查询分解 (#2)

### 问题

LLM 对复杂问题只生成一条 SQL。跨步骤查询（"先查维度 ID，再查事实表"）只能用 CTE 嵌套，失败率高。

### 设计

新增 `decompose_query_node`，放在 `retrieve_schema` 和 `generate_sql` 之间：

```
retrieve_schema → decompose_query → [generate_sql × N] → merge → validate → execute
```

1. **LLM 一次性规划**：输出 `{"needs_decompose": bool, "steps": [{"step": N, "question": "...", "depends_on": [N-1], "output_columns": [...]}]}`
2. **顺序执行**：子 SQL 按依赖顺序执行，后面可引用前面结果（值嵌入，上限 1000 行）
3. **合并**：最后一步的结果作为最终结果，前面步骤汇总到分析报告
4. **回退**：分解失败 → 回退到原单 SQL 路径；任何步骤失败 → 返回已成功部分

### Prompt

```
System: 你是 SQL 查询规划器。判断是否需要多步。输出 JSON:
{"needs_decompose": bool, "steps": [{"step": 1, "question": "...", "depends_on": [], "output_columns": ["id"]}]}
```

### 五轮自辩

| 轮次 | 焦点 | 结论 |
|------|------|------|
| 1 | 一次性规划 vs 动态规划 | 一次性规划 + 依赖合法性校验 |
| 2 | 依赖引用方式 | 值嵌入（≤1000 行）+ 超限回退 |
| 3 | 失败处理 | 步骤失败 → 返回已成功部分 |
| 4 | 状态管理 | `state.decompose_steps: list[dict]` |
| 5 | 兼容性 | `needs_decompose=false` → 直接跳过 |

---

## 17.2 MCP Agent 路径 (#3)

### 问题

`mcp_agent → END`，跳过 analysis/chart/build_response。文件分析无结构输出、无图表。

### 设计

`mcp_agent_node` 输出标准化为与 `execute_sql_node` 相同格式：

```python
return {
    "query_result_sample": parsed_data,
    "analysis_result": {"summary": agent_text, ...},
    "chart_config": {"type": "table", "option": {}},
    "final_response": {"success": True, "source": "mcp_agent", "data": parsed_data, ...},
}
```

`workflow.py`：`mcp_agent → END` 改为 `mcp_agent → build_response`。

### 五轮自辩

| 轮次 | 焦点 | 结论 |
|------|------|------|
| 1 | 数据格式 | CSV/JSON 自动解析，纯文本 data=[] |
| 2 | 图表 | parsed_data 非空 + 多列 → generate_chart |
| 3 | 错误处理 | success=False → build_response 正常输出 |
| 4 | 改动范围 | 仅 mcp_agent_node + workflow 一条边 |
| 5 | 兼容性 | 非文件分析路径不受影响 |

---

## 17.3 重试透明化 (#9)

### 问题

SQL 失败静默重试，用户看到"卡住"。

### 设计

后端：`on_chain_start` 检测 `generate_sql` + `retry_count > 0` → 发 SSE `retry_status` 事件。

### SSE 事件

```json
{"type": "retry_status", "node": "generate_sql", "retry": 2, "max": 3,
 "reason": "执行错误: Unknown column 'created_at'"}
```

前端：TurnBubble 显示 `<Tag color="orange">第 2/3 次重试</Tag>` + 错误原因摘要。

### 五轮自辩

| 轮次 | 焦点 | 结论 |
|------|------|------|
| 1 | 时机 | `on_chain_start`，`retry_count>0` 才发 |
| 2 | 原因 | 从 `execution_error` 提取，≤120 字符 |
| 3 | 次数 | 读 `get_settings().max_retry_count` |
| 4 | 并发 | 无问题（LangGraph 顺序节点） |
| 5 | 兼容性 | 新 SSE 类型，旧前端忽略 |

---

## 17.4 昂贵查询保护 (#10)

### 问题

`SELECT * FROM 1亿行表` 无拦截，云数仓可能巨额费用。

### 设计

`execute_sql` 前检查：表行数 > `max_safe_rows` 且 SQL 无时间过滤或 LIMIT → 暂停，发 `confirm_expensive_query` 等待用户确认（15s 超时取消）。

**配置**：

```python
max_safe_rows: int = 1_000_000
expensive_query_confirm: bool = True
```

### 五轮自辩

| 轮次 | 焦点 | 结论 |
|------|------|------|
| 1 | 过滤精度 | 有时间过滤或 LIMIT 就放行 |
| 2 | 绕过 | 子查询从大表查不可绕过（用表名查估算） |
| 3 | 超时 | 15s 未确认自动取消 |
| 4 | 精度 | TABLE_ROWS 估算误差 ~30%，留余量 |
| 5 | UX | 弹窗：表名 + 行数 + SQL 摘要 + 继续/取消 |

---

## 17.5 LLM 意图分类 (#11)

### 问题

关键词匹配不支持语义相近的查询。

### 设计

`classify_intent_node` 增加 LLM 分支：关键词匹配置信度低时调用 cheap_llm。

**何时调 LLM**：
- 多个关键词匹配到不同意图
- 或一个都没匹配到
- 且查询长度 > 10 字符

LLM 失败 → 回退规则匹配。

### Prompt

```
System: 意图分类器。只输出类型名（query/aggregation/trend/attribution/metadata/chat/file_analysis）。
User: {query}
```

### 五轮自辩

| 轮次 | 焦点 | 结论 |
|------|------|------|
| 1 | 延迟 | 仅模糊时调 LLM，~500ms |
| 2 | 准确率 | 需 > 95%（70 样本测试） |
| 3 | 状态 | 每次独立分类 |
| 4 | Skill | 意图后 Skill 匹配仍用规则 |
| 5 | 降级 | LLM 不可用 → 回退规则 |

---

## 17.6 实现检查清单

### 查询分解
- [ ] `src/graph/nodes/decompose_query.py`
- [ ] `src/llm/prompts.py`：QUERY_DECOMPOSE 模板
- [ ] `src/graph/state.py`：+decompose_steps
- [ ] `src/graph/workflow.py`：注册 + 条件边

### MCP Agent
- [ ] `src/graph/nodes/mcp_agent.py`：输出标准化
- [ ] `src/graph/workflow.py`：mcp_agent → build_response

### 重试透明
- [ ] `src/api/streaming.py`：retry_status 事件
- [ ] `frontend/src/hooks/useChat.ts`：处理事件
- [ ] `frontend/src/pages/ChatPage.tsx`：重试标签

### 昂贵查询
- [ ] `src/graph/nodes/execute_sql.py`：行数检查
- [ ] `src/api/streaming.py`：confirm 事件
- [ ] `src/config.py`：+max_safe_rows + expensive_query_confirm
- [ ] 前端确认弹窗

### LLM 意图
- [ ] `src/graph/nodes/classify_intent.py`：LLM 分支
- [ ] `src/llm/prompts.py`：INTENT_LLM_CLASSIFY
