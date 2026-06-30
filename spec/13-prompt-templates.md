# 12. Prompt 模板管理

## 12. Prompt 模板管理

### 12.1 集中式 Prompt 管理

所有 Prompt 通过 `ChatPromptTemplate` 定义在 `src/llm/prompts.py`，支持版本控制和 A/B 测试：

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

---
