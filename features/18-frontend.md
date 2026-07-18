# 18. 前端

## 18. 前端 (Phase 3) `[P2:13 P3:5]`

### 18.1 项目骨架与路由

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.1.1 | React + TypeScript 项目初始化 | `frontend/` | Vite + React + TypeScript + Ant Design + ECharts + highlight.js + react-router-dom | 开发完成 | P2 |
| 18.1.2 | 路由与布局 | `frontend/src/App.tsx` | Ant Design Layout + Header 健康状态 + Sider + 6 条路由（对话/数据源/表结构/历史/Skills/知识库）| 开发完成 | P2 |

### 18.2 对话分析页 (ChatPage)

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.2.1 | Chat 对话界面 | `frontend/src/pages/ChatPage.tsx` | 消息输入 + 数据源选择 + 发送/取消/清空 + 会话 ID 显示 | 开发完成 | P2 |
| 18.2.2 | 流式进度展示 | `frontend/src/hooks/useChat.ts` | SSE 逐 Node 进度 Tags（8 个节点状态）+ thinking/token 实时推送 | 开发完成 | P2 |
| 18.2.3 | SQL 代码高亮 | `ChatPage.tsx` TurnCard | highlight.js github-dark 主题 + 复制按钮 | 开发完成 | P2 |
| 18.2.4 | 数据表格展示 | `ChatPage.tsx` TurnCard | Ant Design Table 动态列 + 分页（20条/页，最多100行） | 开发完成 | P2 |
| 18.2.5 | ECharts 图表渲染 | `ChatPage.tsx` TurnCard | echarts-for-react 渲染 chart.option，空数据时显示 fallback | 开发完成 | P2 |
| 18.2.6 | 推理过程展示 | `ChatPage.tsx` TurnCard | 流式期间实时显示 LLM reasoning_content | 开发完成 | P2 |
| 18.2.7 | 推荐追问 | `ChatPage.tsx` | 完成后显示 follow_up_questions Tags，点击自动发送 | 开发完成 | P2 |
| 18.2.8 | 欢迎引导页 | `ChatPage.tsx` | 无对话时居中显示示例问题卡片 + 渐变 Logo | 开发完成 | P2 |
| 18.2.9 | 数据源动态加载 | `ChatPage.tsx` | 从 GET /datasources 动态获取选项 | 开发完成 | P2 |
| 18.2.10 | 多源结果稳定渲染 | `DataTable.tsx`、`ResultCard.tsx` | 所有行字段并集兜底、按数据源展示最终 SQL、table 结果不重复显示空图表 | 单测完成 | P1 |
| 18.2.11 | 并行模型流分组 | `useChat.ts` | 按 stream_id 分组推理与原始响应，避免多数据源 token 交错 | 单测完成 | P1 |
| 18.2.12 | 历史会话逐轮恢复 | `useChat.ts`、`ChatPage.tsx` | 每轮消费自己的 final_result，恢复 SQL、数据、分析、图表和推理；长会话向前分页 | 集成测试完成 | P1 |

### 18.3 数据源管理页 (DatasourcePage)

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.3.1 | 数据源列表 | `frontend/src/pages/DatasourcePage.tsx` | Table 展示名称/方言/主机/端口/数据库，支持分页 | 开发完成 | P2 |
| 18.3.2 | 新增数据源 | 同上 | Modal 表单（名称/方言/主机/端口/数据库/用户名/密码/描述） | 开发完成 | P2 |
| 18.3.3 | 删除数据源 | 同上 | Popconfirm 确认删除 | 开发完成 | P2 |
| 18.3.4 | 编辑数据源 | 同上 | 更新已有数据源配置 | 待开发 | P2 |
| 18.3.5 | 测试连接 | 同上 | 新增前测试数据源连通性 | 待开发 | P2 |

### 18.4 表结构浏览页 (SchemaPage)

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.4.1 | 表列表展示 | `frontend/src/pages/SchemaPage.tsx` | Table 含表名/描述/字段数，可搜索 | 开发完成 | P2 |
| 18.4.2 | 展开行查看字段 | 同上 | 展开行显示列名/类型/注释/可空/主键 | 开发完成 | P2 |
| 18.4.3 | Schema 刷新 | 同上 | POST /schema/refresh 重新扫描数据库 | 开发完成 | P2 |
| 18.4.4 | 字段详情抽屉 | 同上 | 点击表名打开 Drawer 显示完整列信息 + 表关系 | 待开发 | P2 |
| 18.4.5 | 列注释编辑 | 同上 | 对接 PUT /schema/tables/{name}/columns/{col}/comment | 待开发 | P2 |

### 18.5 查询历史页 (HistoryPage)

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.5.1 | 历史列表 | `frontend/src/pages/HistoryPage.tsx` | Table 含时间/查询/SQL/状态/行数 + 数据源筛选下拉 | 开发完成 | P2 |
| 18.5.2 | 搜索过滤 | 同上 | 按查询内容/SQL 关键字前端搜索 | 开发完成 | P2 |
| 18.5.3 | 回放历史对话 | 同上 | 点击历史条目逐轮恢复结构化响应，Checkpointer 不可用或服务重启时回退 JSONB 查询历史 | 集成测试完成 | P3 |

### 18.6 指标文档管理

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.6.1 | 指标列表 | `frontend/src/pages/MetricsPage.tsx`（新建） | 业务指标口径文档列表展示 | 待开发 | P3 |
| 18.6.2 | 指标编辑 | 同上 | 在线编辑业务规则/指标口径 | 待开发 | P3 |

### 18.7 基础设施

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.7.1 | API 客户端 | `frontend/src/api/client.ts` | get/post/streamChat 封装 + SSE 解析 | 开发完成 | P2 |
| 18.7.2 | useChat Hook | `frontend/src/hooks/useChat.ts` | 对话状态管理 + SSE 事件分发 | 开发完成 | P2 |
| 18.7.3 | TypeScript 类型 | `frontend/src/types/index.ts` | SSEEvent / ChatResponse / DatasourceConfig / TableInfo / ColumnInfo | 开发完成 | P2 |
| 18.7.4 | 公共组件库 | `frontend/src/components/` | SqlPanel / DataTable / ChartPanel / ProgressBar / ReasoningPanel / ResultCard | 开发完成 | P2 |
| 18.7.5 | ErrorBoundary | `frontend/src/components/ErrorBoundary.tsx` | React Error Boundary 包裹所有页面路由 | 开发完成 | P2 |
| 18.7.6 | 连接状态指示器 | `App.tsx` Header | 启动时 GET /health, Header 显示 LLM 可用性 + 数据源计数 | 开发完成 | P2 |
| 18.7.7 | 响应式布局 | 全局 | 移动端适配（可折叠侧栏 + 自适应内容区） | 待开发 | P3 |

### 18.8 高级特性

| # | 功能 | 描述 | 状态 | 优先级 |
|---|------|------|------|--------|
| 18.8.1 | 数据脱敏提示 | 前端显示敏感数据已脱敏的标识 | 待开发 | P3 |
| 18.8.2 | 技能选择面板 | 对话前选择要启用的 Skill（如数据质量检查、自定义报告） | 待开发 | P3 |

### 18.9 Skills 管理页 (SkillsPage)

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.9.1 | Skills 列表展示 | `frontend/src/pages/SkillsPage.tsx` | Table 含名称/版本/启用状态/描述/触发词/工具/依赖 + is_builtin 标识 | 开发完成 | P2 |
| 18.9.2 | 启用/禁用切换 | 同上 | Switch 组件调用 PUT /skills/{name}/toggle | 开发完成 | P2 |
| 18.9.3 | 批量导入 | 同上 | webkitdirectory 文件夹上传 + 单文件导入，递归匹配 SKILL.md | 开发完成 | P2 |
| 18.9.4 | 删除 Skill | 同上 | 内置 Skill 禁用删除按钮，用户 Skill 支持 Popconfirm 删除 | 开发完成 | P2 |
| 18.9.5 | 查看详情 | 同上 | Modal 展示元数据 + SKILL.md 原始内容（从 GET /skills/{name}/content 加载） | 开发完成 | P2 |
| 18.9.6 | 手动刷新 | 同上 | 刷新按钮调用 POST /skills/refresh 全量重扫 | 开发完成 | P2 |
| 18.9.7 | Skill 三级作用域管理 | 同上 | system/tenant/private 范围选择、徽标及操作请求精确携带 scope | 开发完成 | P1 |

### 18.10 知识库管理页 (KnowledgePage)

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.10.1 | 知识条目列表 | `frontend/src/pages/KnowledgePage.tsx` | Table 含类别/源文件/内容摘要/来源，支持类别筛选 + 搜索 | 开发完成 | P2 |
| 18.10.2 | 已索引文档列表 | 同上 | Table 含文件名/大小/修改时间，仅用户上传的可删除 | 开发完成 | P2 |
| 18.10.3 | 文档上传 | 同上 | 支持 PDF/Word/TXT/MD，选择文件后弹出分块配置 Modal（策略/大小/重叠/类别） | 开发完成 | P2 |
| 18.10.4 | 异步处理 + 进度通知 | 同上 | 上传后返回 task_id，前端每秒轮询 GET /knowledge/upload/status，完成时浏览器通知 | 开发完成 | P2 |
| 18.10.5 | 条目详情 | 同上 | Modal 展示类别/源文件/来源/类型 + 完整内容 | 开发完成 | P2 |
| 18.10.6 | 文档内容查看 | 同上 | Modal 展示，PDF 用 iframe 嵌入渲染，Word 转 HTML，TXT/MD 纯文本 | 开发完成 | P2 |
| 18.10.7 | 删除条目/文档 | 同上 | 系统条目/文档禁用删除按钮，用户上传的支持 Popconfirm 删除 | 开发完成 | P2 |
| 18.10.8 | 三范围知识视图 | 同上 | system/tenant/private 筛选、范围徽标及按角色控制上传范围 | 单测完成 | P1 |
| 18.10.9 | 多标签上传 | 同上 | 标签远程搜索、多选、创建个人标签和数据源绑定 | 单测完成 | P1 |
| 18.10.10 | 全局标签治理 | 同上 | super_admin 创建/停用全局标签并提升个人标签 | 单测完成 | P1 |

### 18.11 MCP 管理页 (McpPage)

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.11.1 | MCP 三级作用域管理 | `frontend/src/pages/McpPage.tsx` | 创建范围选择、范围徽标、同名资源按 scope 测试和删除 | 开发完成 | P1 |

### 模块收尾

模块功能点共 55 项，已完成 46 项，待开发 9 项。

| 功能点 | 不开发原因 | 可开发条件 | 预计开发时机 |
|--------|------------|------------|--------------|
| 18.3.4 编辑数据源 | 当前数据源 Provider 的更新事务与凭证轮换接口尚未统一 | 后端提供带版本校验的 PUT /datasources/{name} | Phase 3，数据源生命周期增强 |
| 18.3.5 测试连接 | 注册前的临时凭证探测 API 尚未提供 | 后端提供不落库的 POST /datasources/test | Phase 3，数据源生命周期增强 |
| 18.4.4 字段详情抽屉 | 当前 Schema API 返回数据已具备，但本轮聚焦知识治理 | 完成字段关系响应契约和移动端宽度设计 | Phase 3，Schema 浏览体验批次 |
| 18.4.5 列注释编辑 | 需明确租户管理员、数据源所有者的编辑权限 | 后端列注释 API 接入 RBAC 并完成审计 | Phase 3，Schema 治理批次 |
| 18.6.1 指标列表 | 指标仍以知识条目存在，缺少独立指标生命周期 API | 建立 MetricDefinition 模型、审核状态和分页 API | Phase 4，指标治理模块 |
| 18.6.2 指标编辑 | 依赖指标版本、审核和冲突处理能力 | 18.6.1 及指标草稿/发布 API 完成 | Phase 4，指标治理模块 |
| 18.7.7 响应式布局 | 当前桌面工作台可用，移动端全页面回归尚未建立 | 建立 375/768/1440 视口 Playwright 回归 | Phase 3，前端质量批次 |
| 18.8.1 数据脱敏提示 | 后端响应未返回统一 masking metadata | 脱敏器在响应中提供字段级脱敏标识 | Phase 3，安全可视化批次 |
| 18.8.2 技能选择面板 | 当前 Skill 由意图自动匹配，缺少会话级显式授权参数 | ChatRequest 增加 enabled_skill_ids 并校验权限 | Phase 4，Skill 交互增强 |

---
