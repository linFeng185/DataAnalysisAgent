---
name: custom-report
version: 1.0.0
api_version: data-agent/v2
description: 根据查询结果生成周报、月报等自定义数据报告
author: data-team
tags: [report, presentation, export]

triggers:
  keywords: [周报, 月报, 报告, 总结, 导出, 报表]
  intents: []
  tables: []

depends_on:
  mcp_servers: []
  skills: []
  python_packages: [jinja2]

tools:
  - name: render_report
    description: 用模板渲染数据报告
    input_schema: schemas/render_report_input.json
    output_schema: schemas/render_report_output.json

capabilities: [report.render]
accepts: [database, table_file]
permissions:
  network: []
  files: read_asset_only
  datasources: read_only
resources:
  timeout_seconds: 30
  cpu_seconds: 10
  memory_mb: 256
  max_tool_calls: 1
  max_input_bytes: 2097152
  max_output_bytes: 524288
entrypoints:
  execute: tools:render_report
---

当用户请求生成报告时，你应当:
1. 先完成数据查询和分析
2. 根据时间范围选择合适的模板 (周报/月报)
3. 报告应包含: 关键指标摘要、趋势分析、异常标注、行动建议
