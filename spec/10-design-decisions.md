# 9. 关键设计决策

## 9. 关键设计决策

| 决策点 | 选择 | 理由 |
|-------|------|------|
| LLM 框架 | LangChain + LangGraph | 标准化工具接口、内置会话持久化(checkpointer)、LangSmith全链路追踪、社区生态成熟 |
| 分析计算 | pandas 侧执行 | LLM 不擅长精确计算，统计交给代码 |
| Schema 检索 | 向量 + 倒排混合 | 语义相似 + 关键词精确匹配互补 |
| 可视化方案 | ECharts (前端渲染) | 交互性强，图表种类丰富 |
| SQL 方言 | 每个数据源独立 Prompt | ClickHouse 和 MySQL 函数差异大 |

---
