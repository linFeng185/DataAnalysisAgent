# 13. 评估指标与质量保障

## 13. 评估指标与质量保障

| 指标 | 目标 | 测量方式 |
|-----|------|---------|
| SQL 首次生成正确率 | ≥ 85% | LangSmith Dataset + 人工标注 |
| SQL 最终正确率（含重试） | ≥ 95% | 含最多3次重试，LangSmith 全链路统计 |
| 端到端响应时间（简单查询） | ≤ 3秒 | LangSmith Trace 各 Node 延迟汇总 |
| 端到端响应时间（复杂分析） | ≤ 30秒 | 含 LLM 分析 + 图表生成 |
| 危险 SQL 拦截率 | 100% | 安全测试用例集 + CI 自动化 |
| Schema 检索命中率 | ≥ 90% | Top-5 召回率，ChromaDB 检索评估 |

### 13.1 LangSmith 驱动的评估流程

```python
from langsmith import Client

client = Client()

# 1. 上传标注数据集
dataset = client.create_dataset(
    "nl2sql-benchmark",
    data_type="chat",
    inputs=[{"question": "上月销售额Top10", "tables": [...]}],
    outputs=[{"expected_sql": "SELECT ...", "expected_analysis": "..."}],
)

# 2. 定义 evaluator
def sql_correctness(outputs: dict, reference_outputs: dict) -> bool:
    return sqlparse.format(outputs["sql"]) == sqlparse.format(reference_outputs["expected_sql"])

# 3. 批量回归测试
from langsmith import aevaluate

results = await aevaluate(
    lambda x: app.ainvoke(x),  # 你的 LangGraph app
    data="nl2sql-benchmark",
    evaluators=[sql_correctness],
    max_concurrency=4,
)
```

---
