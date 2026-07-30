# 15. 评估与质量保障

## 15. 评估与质量保障 `[P1:2 P2:4]`

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 15.1 | NL2SQL 标注数据集 | `tests/fixtures/nl2sql_benchmark.json` | 12 条 question/tables/expected_sql/expected_analysis/dialect 固定样本 | 单测完成 |
| 15.2 | SQL 正确性 evaluator | `tests/evaluators/sql_correctness.py` | sqlglot AST 规范化后比对并返回离线评分明细 | 单测完成 |
| 15.3 | SQL 安全拦截 evaluator | `tests/evaluators/sql_security.py` | 12 类危险 SQL 的 100% 拦截率评测 | 单测完成 |
| 15.4 | LangSmith aevaluate 集成 | `tests/evaluators/run_eval.py` | 批量回归测试，LangSmith Dataset 驱动 | 单测完成 |
| 15.5 | Schema 检索命中率评估 | `tests/evaluators/schema_recall.py` | Top-5 召回率测量 | 单测完成 |
| 15.6 | CI 自动化测试 | `.github/workflows/test.yml` | GitHub Actions: 单元测试 + 集成测试 + 评估 | 单测完成 |
| 15.7 | 全链路离线评测集 | `tests/fixtures/graph_benchmark.json`、`tests/evaluators/graph_regression.py` | 路由、SQL、安全、分析产物和失败语义固定回归 | 单测完成 |
| 15.8 | 真实多数据源验收 | `tests/test_graph/test_live_multi_source.py` | 本地真实连接器验收 + 显式环境开关的外部数据源验收 | 集成测试完成 |

---

### 模块收尾

模块功能点共 8 项，已完成 8 项，待开发 0 项。当前模块无待开发项。

---
