# 15. 评估与质量保障

## 15. 评估与质量保障 `[P1:2 P2:4]`

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 15.1 | NL2SQL 标注数据集 | `tests/fixtures/nl2sql_benchmark.json` | (question, tables, expected_sql, expected_analysis) 四元组 | 待开发 |
| 15.2 | SQL 正确性 evaluator | `tests/evaluators/sql_correctness.py` | sqlparse 标准化后比对 | 待开发 |
| 15.3 | SQL 安全拦截 evaluator | `tests/evaluators/sql_security.py` | 注入危险 SQL 的测试用例集 | 待开发 |
| 15.4 | LangSmith aevaluate 集成 | `tests/evaluators/run_eval.py` | 批量回归测试，LangSmith Dataset 驱动 | 待开发 |
| 15.5 | Schema 检索命中率评估 | `tests/evaluators/schema_recall.py` | Top-5 召回率测量 | 待开发 |
| 15.6 | CI 自动化测试 | `.github/workflows/test.yml` | GitHub Actions: 单元测试 + 集成测试 + 评估 | 待开发 |

---
