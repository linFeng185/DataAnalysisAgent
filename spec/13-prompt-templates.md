# 12. Prompt 模板管理

## 12. Prompt 模板管理

### 12.1 集中式 Prompt 管理

所有 System Prompt 常量集中定义在 `src/llm/prompts.py`，节点负责组装经过裁剪的 HumanMessage 上下文；禁止在业务节点散落可覆盖安全边界的角色指令：

```
src/llm/prompts.py          # 所有 Prompt 模板
  ├── INTENT_CLASSIFY_PROMPT
  ├── SQL_GENERATION_PROMPT
  ├── DATA_ANALYSIS_PROMPT
  ├── CHART_RECOMMEND_PROMPT
  └── RESPONSE_BUILD_PROMPT
```

### 12.2 数据分析 Node 的 Prompt 模板

```python
from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate, HumanMessagePromptTemplate

DATA_ANALYSIS_PROMPT = ChatPromptTemplate.from_messages([
    SystemMessagePromptTemplate.from_template(
        "你是一个资深数据分析师。"
        "根据用户问题、执行的SQL和查询结果给出分析报告。"
        "使用中文回复。"
    ),
    HumanMessagePromptTemplate.from_template("""
## 用户原始问题
{user_query}

## 执行的 SQL
```sql
{generated_sql}
```

## 查询结果 (前 50 行)
{data_sample}

## 自动统计摘要
{statistics}

请按以下格式输出分析报告：
1. **数据摘要**：1-2句话概括
2. **关键发现**：列出2-5条洞察
3. **推荐图表**：建议的可视化类型和维度
4. **值得追问的方向**：用户可能想进一步了解的点

{format_instructions}
""")
])
```

### 12.3 数据库分析 Prompt 上下文契约

数据库分析链路按“可验证事实优先”组织上下文，禁止只靠角色描述提高模型表现：

1. `retrieve_schema` 输出表名、字段、类型、主键/索引、可空性、外键、行数估算和枚举值。
2. `generate_sql` 同时接收业务规则、知识库命中、已验证 SQL 示例、Skill 指令、当前时间、
   对话上下文和上一轮错误；优先级为系统安全/租户权限/只读约束 > 用户明确要求 > 业务规则 >
   Schema > 示例。用户、知识文档和 Skill 均不得覆盖安全与授权边界。
3. SQL Prompt 强制先确定结果粒度、再选择表和 JOIN；对一对多 JOIN、`COUNT` 去重、NULL、
   除零、时间边界、枚举谓词、方言函数和 LIMIT 做显式检查。
4. 模型只能输出单条只读 SQL。信息不足时返回空 SQL 和缺失信息说明，禁止猜测表、字段、
   关联关系、枚举值或业务口径。
5. `analyze_result` 必须接收用户原问题、SQL、全量/采样标签、结果总行数、截断状态、统计摘要
   和业务上下文；输出区分事实、解释性假设、限制和行动建议，禁止把相关性写成因果关系。
6. 分析输出保留兼容字段 `summary / insights / recommended_chart_type /
   follow_up_questions`，并允许增加 `data_quality / limitations / confidence /
   recommended_actions`。

Prompt 的强度通过上下文完备度、结构化输出、代码层校验和回归样例共同保证，不以暴露模型
思维链为目标。SQL 数值正确性继续由 `sqlglot`、数据库 `EXPLAIN` 和确定性统计处理器兜底。

### 12.4 节点级模型策略

1. 节点统一通过 `get_task_llm(task)` / `is_task_llm_available(task)` 选择模型，禁止直接按全局
   API Key 判断所有节点都可调用远程模型。
2. `generate_sql` 默认是唯一远程任务，由 `LLM_REMOTE_TASKS` 显式授权，并强制
   `reasoning=False`；远程不可用且配置了本地模型时可回退本地。
3. `classify_intent / decompose_query / direct_answer / analyze_result / polish_result /
   multi_source_merge / mcp_agent / context_summary` 默认使用 `LOCAL_LLM_*` 指定的快速本地
   OpenAI-compatible 模型。本地未配置时走确定性规则，不隐式等待远程模型。
4. 只有 `LLM_ALLOW_REMOTE_FALLBACK=true` 时，非远程任务才允许回退到配置模型。
5. 单元、回归与回测只 Mock 任务模型工厂并断言 prompt/messages/参数/状态；真实远程模型只在
   `RUN_LIVE_LLM_TESTS=1` 下运行。

---
