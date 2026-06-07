"""
分析流水线 — LangGraph 工作流（核心链路）。

业务职责：
  这是整个智能体的「大脑」。将一次分析请求拆解为 9 个有序步骤。
  节点间通过 AnalysisState（共享 TypedDict）传递数据，
  支持 3 类重试循环（语法错误 / EXPLAIN 失败 / 执行瞬态错误）。

核心模块：
  state.py   — AnalysisState 定义（30+ 字段贯穿流水线）
  workflow.py— StateGraph 组装 + 5 个条件路由 + 编译
  nodes/     — 9 个流水线节点实现
"""
