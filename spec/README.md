# SPEC 技术规格

> 由 SPEC.md 拆分，按章节独立维护。

| 文件 | 章节 | 行数 |
|------|------|------|
| [01-overview.md](01-overview.md) | 1. 概述 | 14 行 |
| [02-architecture.md](02-architecture.md) | 2. 系统架构 | 55 行 |
| [03-core-components.md](03-core-components.md) | 3. 核心组件详细设计 | 2569 行 |
| [05-tech-stack.md](05-tech-stack.md) | 4. 技术栈选型 | 20 行 |
| [06-api-design.md](06-api-design.md) | 5. API 设计 | 70 行 |
| [07-data-flow.md](07-data-flow.md) | 6. 数据流示例 | 44 行 |
| [08-security.md](08-security.md) | 7. 安全设计 | 16 行 |
| [09-roadmap.md](09-roadmap.md) | 8. 实现路线图 | 51 行 |
| [10-design-decisions.md](10-design-decisions.md) | 9. 关键设计决策 | 11 行 |
| [11-directory-structure.md](11-directory-structure.md) | 10. 目录结构建议 | 94 行 |
| [12-langgraph-integration.md](12-langgraph-integration.md) | 11. LangGraph 集成细节 | 160 行 |
| [13-prompt-templates.md](13-prompt-templates.md) | 12. Prompt 模板管理 | 53 行 |
| [14-evaluation.md](14-evaluation.md) | 13. 评估指标与质量保障 | 42 行 |
| [15-design-review.md](15-design-review.md) | 14. 设计审查与风险缓解 | 192 行 |
| [21-defect-remediation-design.md](21-defect-remediation-design.md) | 缺陷系统整改设计 | 动态 |
| [22-defect-remediation-implementation-plan.md](22-defect-remediation-implementation-plan.md) | 缺陷系统整改实施计划 | 动态 |
| [23-knowledge-and-data-intelligence-extension-roadmap.md](23-knowledge-and-data-intelligence-extension-roadmap.md) | 知识库优化与通用数据智能扩展路线图 | 动态 |
| [24-architecture-remediation.md](24-architecture-remediation.md) | 架构整改设计 | 动态 |
| [25-platform-admin-and-auth.md](25-platform-admin-and-auth.md) | 平台管理、强制认证与统一配置设计 | 动态 |
| [26-api-access-policy.md](26-api-access-policy.md) | API 访问策略、日志与 IP 控制设计 | 动态 |
| [27-container-deployment.md](27-container-deployment.md) | Linux 前后端分离容器部署 | 动态 |
| [28-tenant-identity-and-llm-governance.md](28-tenant-identity-and-llm-governance.md) | 租户身份、用户自治与 LLM 治理 | 动态 |
| [29-selected-phase4-features.md](29-selected-phase4-features.md) | Phase 4 选定能力设计 | 动态 |
| [30-report-workbench.md](30-report-workbench.md) | 报表工作台与自然语言自动化设计 | 动态 |
| [31-secret-custody.md](31-secret-custody.md) | 密钥托管、启动注入与离线凭证轮换 | 动态 |
| [32-metric-catalog.md](32-metric-catalog.md) | 租户指标目录、版本审核与生效口径引用 | 动态 |
| [33-image-assets.md](33-image-assets.md) | 有界图片资产、视觉模型与证据链路 | 动态 |
| [34-report-phase2-implementation.md](34-report-phase2-implementation.md) | 报表持久化调度、实时钻取与生命周期实现约束 | 动态 |
| [35-platform-tenant-governance.md](35-platform-tenant-governance.md) | 平台报表分权、共享查询额度、知识应急授权与 MCP 安全停用 | 动态 |
| [37-runtime-platform-and-ui.md](37-runtime-platform-and-ui.md) | 平台动态配置、连接器热插拔、双语界面与表结构同步 | 动态 |
| [38-datasource-transfer-and-polling.md](38-datasource-transfer-and-polling.md) | 数据源导出选择、导入改名、列表排序与轮询修复的实施计划 | 动态 |
| [36-large-test-datasets.md](36-large-test-datasets.md) | 每源 125 表、五业务域与千万级中文模拟数据 | 动态 |
| [39-sql-schema-validation.md](39-sql-schema-validation.md) | SQL Schema 校验层设计（生成后 · 打库前） | 动态 |
| [40-sql-schema-validation-implementation-plan.md](40-sql-schema-validation-implementation-plan.md) | SQL Schema 校验层实施计划（8 个 Task，阶段 A/B） | 动态 |
| [41-report-visual-templates.md](41-report-visual-templates.md) | 报表视觉模板体系设计（配色 · 字体 · 密度） | 动态 |
| [42-report-divergent-analysis.md](42-report-divergent-analysis.md) | 报表智能发散分析设计（扩单元 · 深挖 · 关联 · 基准） | 动态 |
| [43-report-run-statistics.md](43-report-run-statistics.md) | 报表运行计数统计设计（LLM 调用 · SQL 执行 · 成败） | 动态 |
| [44-knowledge-governance-and-memory-ui.md](44-knowledge-governance-and-memory-ui.md) | 知识库治理、上传交互与长期记忆管理设计 | 动态 |
| [45-knowledge-governance-and-memory-ui-implementation-plan.md](45-knowledge-governance-and-memory-ui-implementation-plan.md) | 知识库治理与长期记忆管理实施计划（8 个 Task） | 动态 |
| [46-embedding-backend-onnx.md](46-embedding-backend-onnx.md) | 嵌入后端切换 ONNX Runtime（消除 transformers 启动延迟） | 动态 |
| [47-configurable-embedding-model.md](47-configurable-embedding-model.md) | 可替换嵌入模型与自动重编码（模型指纹 collection · 后台异步重建） | 动态 |
| [48-experience-and-workbench-remediation.md](48-experience-and-workbench-remediation.md) | 体验整改与报表工作台重构设计（A～E 五批 · 9 项需求） | 动态 |
| [49-report-presentation-redesign.md](49-report-presentation-redesign.md) | 报表呈现体系改版设计（hero 横幅 · 图文分栏 · 全图表类型） | 动态 |
| [50-report-node-routing.md](50-report-node-routing.md) | 报表单元节点路由由大模型决策（节点数据表 · 强护栏 · 静态兜底） | 动态 |
| [51-autonomous-knowledge-retrieval.md](51-autonomous-knowledge-retrieval.md) | 自主知识检索工具、全业务LLM有界循环与审核验收 | 动态 |
| [52-chat-presentation.md](52-chat-presentation.md) | 对话数据优先、公开正文流式、过程摘要权限与多维图表展示 | 动态 |
| [53-prompt-budget-model-window.md](53-prompt-budget-model-window.md) | 提示词预算跟随超管模型窗口动态推导（全员接窗口 · 注册上限 · 数据层单阀门） | 动态 |
| [54-prompt-budget-model-window-implementation-plan.md](54-prompt-budget-model-window-implementation-plan.md) | 提示词预算动态化实施计划（预算推导 · 注册上限 · 数据层单阀门） | 动态 |
| [55-sql-profiling-and-probing.md](55-sql-profiling-and-probing.md) | SQL 探查与数据画像设计（键列判断 · 画像预取 · 结构化探查原语） | 动态 |
| [56-mcp-reset-lifecycle.md](56-mcp-reset-lifecycle.md) | MCP 连接任务、重置生命周期、授权与有界恢复 | 动态 |
| [57-skill-submodule-discovery.md](57-skill-submodule-discovery.md) | 社区技能子模块发现、固定版本校验与隔离执行 | 动态 |
| [58-controlled-web-search.md](58-controlled-web-search.md) | 受控联网搜索、真实来源与研究摘要边界 | 动态 |
| [59-bilingual-intent-fallback.md](59-bilingual-intent-fallback.md) | 中英文意图规则回退、历史追问与权限路由一致性 | 动态 |
| [60-bilingual-public-messages.md](60-bilingual-public-messages.md) | 中英文固定补参、公开错误与统一响应语言 | 动态 |
| [61-common-platform-hardening.md](61-common-platform-hardening.md) | 通用平台加固（工具输出、认证故障、日志作用域、迁移前序、知识版本读取） | 动态 |
