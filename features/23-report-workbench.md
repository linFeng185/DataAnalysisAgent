# 23. 报表工作台

本模块对应 `spec/30-report-workbench.md`。本轮已完成 Phase 1 后端闭环的 8 项功能，跨源联合分析和前端工作台保留在后续迭代；新工作台复用现有数据源授权、SQL 安全执行、分析引擎、可视化引擎和 LangGraph 基础设施，旧 `analysis_schedules` 继续作为兼容能力保留。

| # | 功能 | 主要文件/模块 | 描述 | Phase | 状态 | 优先级 |
|---|---|---|---|---|---|---|
| 23.1 | 自然语言计划解析 | `src/reporting/planner.py`、`src/api/routes/report_workbench.py` | 从文本解析立即/定时、频率、次数、有效期、数据源和分析单元；缺少调度信息默认立即一次 | Phase 1 | 单测完成 | P1 |
| 23.2 | 报表任务与运行模型 | `src/reporting/models.py`、`src/reporting/store.py`、`migrations/018_report_workbench.sql` | Job/Run/Step/Version 持久化、幂等、状态机和 RLS；SQLite 测试走应用层模拟 | Phase 1 | 单测完成 | P1 |
| 23.3 | 报表 LangGraph 子图 | `src/graph/subgraphs/report_workbench.py`、`src/graph/contracts.py` | 单数据源多单元拆解、授权 Schema、查询执行、确定性分析、综合洞察和页面发布 | Phase 1 | 单测完成 | P1 |
| 23.4 | 多业务关系分析 | `src/reporting/relations.py`、`src/tools/join_contract.py` | 首期生成同源关系证据并对跨源受控分析器降级；跨源联合、列投影、采样和聚合在 Phase 2 | Phase 2 | 待开发 | P1 |
| 23.5 | 动态报表页面 DSL | `src/reporting/document.py`、`frontend/src/components/report/` | 版本化页面 JSON、响应式布局、图表/表格/洞察/质量块和 ECharts 白名单校验 | Phase 1 | 单测完成 | P1 |
| 23.6 | 交互明细与 Drilldown | `src/reporting/drilldown.py`、`src/api/routes/report_workbench.py` | 私有/分享快照钻取；客户端不能提交任意 SQL，live drilldown 在 Phase 2 | Phase 1 | 单测完成 | P1 |
| 23.7 | 生命周期与分享 | `src/reporting/sharing.py`、`src/api/routes/report_workbench.py` | 默认 7 天、快照分享、指定用户范围、授权码和撤销；延期/永久策略在 Phase 2 | Phase 1 | 单测完成 | P1 |
| 23.8 | 菜单权限与用户配额 | `src/reporting/quota.py`、`src/api/routes/report_workbench.py` | `report.*` 能力、周期次数、并发、查询、Token 和分享限额 | Phase 1 | 单测完成 | P1 |
| 23.9 | 工作台与公开查看器 | `frontend/src/pages/AutomationPage.tsx`、`frontend/src/pages/SharedReportPage.tsx` | 自然语言创建、计划确认、动态报表阅读、日志抽屉、分享和过期状态 | Phase 1 | 待开发 | P1 |
| 23.10 | 重试、恢复与可观测性 | `src/reporting/runner.py`、`src/reporting/store.py` | 基础节点日志、失败状态、用户/内部错误分离和部分成功；节点重试、恢复扫描在 Phase 2/3 | Phase 1 | 单测完成 | P1 |

## 依赖关系

```text
11 API / 12 Security / 13 Analytics / 14 Visualization
             ├──────────────┐
             ▼              ▼
       23.1 Planner ──→ 23.2 Job/Run Store
                              │
                              ▼
                      23.3 Report Subgraph
                       ├──→ 23.4 Relations
                       ├──→ 23.5 Document DSL
                       ├──→ 23.6 Drilldown
                       └──→ 23.7 Lifecycle/Sharing
                              │
             21 Platform Admin ──→ 23.8 Quota/Permission
                              │
                              ▼
                      23.9 Frontend / 23.10 Operations
```

## 模块收尾

本轮 Phase 1 已完成 8 项，待开发 2 项；模块总计 10 项。

| 待开发项 | 不开发原因 | 可开发条件 | 预计开发时机 |
|---|---|---|---|
| 23.4 跨源联合分析 | Phase 1 只实现同源关系证据，受控分析器尚未具备列投影、采样和聚合隔离能力 | `ControlledRelationAnalyzer` 完成跨源授权投影、匹配率/基数验证和独立测试 | Phase 2，完成多数据源隔离执行后 |
| 23.9 前端工作台与查看器 | 本轮先完成后端计划、页面 DSL、私有 API 和公开快照安全边界，未改动现有 AutomationPage | 前端白名单渲染组件、路由权限守卫和 SSE 运行日志抽屉完成 | Phase 1 后续迭代，后端 API 契约稳定后 |
