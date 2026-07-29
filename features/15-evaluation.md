# 15. 评估与质量保障

## 15. 评估与质量保障 `[P1:2 P2:4]`

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 15.1 | NL2SQL 标注数据集 | `tests/fixtures/nl2sql_benchmark.json` | 12 条 question/tables/expected_sql/expected_analysis/dialect 固定样本 | 单测完成 |
| 15.2 | SQL 正确性 evaluator | `tests/evaluators/sql_correctness.py` | sqlglot AST 规范化后比对并返回离线评分明细 | 单测完成 |
| 15.3 | SQL 安全拦截 evaluator | `tests/evaluators/sql_security.py` | 12 类危险 SQL 的 100% 拦截率评测 | 单测完成 |
| 15.4 | LangSmith aevaluate 集成 | `tests/evaluators/run_eval.py` | 批量回归测试，LangSmith Dataset 驱动 | 待开发 |
| 15.5 | Schema 检索命中率评估 | `tests/evaluators/schema_recall.py` | Top-5 召回率测量 | 待开发 |
| 15.6 | CI 自动化测试 | `.github/workflows/test.yml` | GitHub Actions: 单元测试 + 集成测试 + 评估 | 待开发 |

---
