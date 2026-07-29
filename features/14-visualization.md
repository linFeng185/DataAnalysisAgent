# 14. 可视化引擎

## 14. 可视化引擎 `[P1:6 P2:2]`

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 14.1 | 智能选图 classify_chart_type() | `src/tools/chart_generator.py` | 时间+数值→line / 分类+数值→bar / 占比→pie / 双数值→scatter / 通用→table | 开发完成 |
| 14.2 | ECharts 折线图生成 | 同上 | 生成 line chart 的 ECharts option JSON | 开发完成 |
| 14.3 | ECharts 柱状图生成 | 同上 | 生成 bar chart 的 ECharts option JSON | 开发完成 |
| 14.4 | ECharts 饼图生成 | 同上 | 生成 pie chart 的 ECharts option JSON | 开发完成 |
| 14.5 | ECharts 散点图生成 | 同上 | 生成 scatter chart 的 ECharts option JSON | 开发完成 |
| 14.6 | ECharts 热力图生成 | 同上 | 双维度 + 数值生成坐标、visualMap 和 heatmap series | 单测完成 |
| 14.7 | 表格渲染 | 同上 | 生成 Markdown 表格或 HTML table | 开发完成 |
| 14.8 | 用户调整图表 | 同上 | 白名单自然语言/类型指令复用原结果重生成图表，不重复执行 SQL | 单测完成 |

---
