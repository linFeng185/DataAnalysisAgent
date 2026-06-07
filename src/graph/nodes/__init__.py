"""
流水线节点 — LangGraph 工作流的 9 个执行步骤。

1  classify_intent  — 意图分类（关键词匹配 → 7 种类别）
2  retrieve_schema  — Schema 检索（从 Registry 获取表结构）
3  generate_sql     — SQL 生成（LLM 主路径 + 模板回退）
4  layer3_validate  — 安全校验（防注入 + sqlglot 语法解析）
5  layer4_explain   — EXPLAIN 验证（Phase 2，当前为桩）
6  execute_sql      — 数据库执行（各方言超时 + 连接管理）
7  analyze_result   — 统计分析（纯 Python 统计 + LLM 洞察）
8  generate_chart   — 图表生成（ECharts 配置，Phase 2）
9  build_response   — 响应组装（成功/错误两种 JSON 形态）
"""
