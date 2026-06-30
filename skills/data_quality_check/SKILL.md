---
name: data-quality-check
version: 1.0.0
description: 对查询结果执行数据质量检查 (空值率、重复值、异常值)
author: data-team
tags: [quality, validation, production]

triggers:
  keywords: [数据质量, 空值, 重复, 异常检测, 数据校验, 完整性, 脏数据]
  intents: [aggregation, query]
  tables: []

depends_on:
  mcp_servers: []
  skills: []
  python_packages: [pandas, numpy]

tools:
  - name: check_null_rate
    description: 检查指定列的空值率
  - name: check_duplicates
    description: 检查指定列的重复值
  - name: detect_outliers
    description: 用 Z-Score 方法检测异常值
---

当用户询问数据质量相关问题时，你应当:
1. 在 SQL 生成后自动附加质量检查
2. 在分析结论中单独列出数据质量问题
3. 对于空值率 > 10% 的字段，主动提示用户
4. 对于检测到的异常值，标注可能的数据录入错误
