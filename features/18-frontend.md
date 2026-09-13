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
| 18.2.1 | Chat 对话界面 | `frontend/src/pages/ChatPage.tsx` | 消息输入 + 数据源选择 + 发送/取消/清空 + 会话 ID 显示；取消后明确标记本轮状态 | 单测完成 | P2 |
| 18.2.2 | 流式进度展示 | `frontend/src/hooks/useChat.ts` | SSE 逐 Node 进度 Tags（8 个节点状态）；中间 token 仅兼容协议，不在前端保存或展示 | 单测完成 | P2 |
| 18.2.3 | SQL 代码高亮 | `SqlPanel.tsx` | highlight.js github-dark 主题 + 复制按钮；高亮异常兜底 HTML 转义 | 单测完成 | P2 |
| 18.2.4 | 数据表格展示 | `ChatPage.tsx` TurnCard | Ant Design Table 动态列 + 分页（20条/页，最多100行） | 开发完成 | P2 |
| 18.2.5 | ECharts 图表渲染 | `ChatPage.tsx` TurnCard | echarts-for-react 渲染 chart.option，空数据时显示 fallback | 开发完成 | P2 |
| 18.2.6 | 推理过程兼容展示 | `ChatPage.tsx` TurnCard | 仅兼容历史 reasoning 字段；新请求不再接收或展示模型原始推理链 | 单测完成 | P2 |
| 18.2.7 | 推荐追问 | `ChatPage.tsx` | 完成后显示 follow_up_questions Tags，点击自动发送 | 开发完成 | P2 |
| 18.2.8 | 欢迎引导页 | `ChatPage.tsx` | 无对话时居中显示示例问题卡片 + 渐变 Logo | 开发完成 | P2 |
| 18.2.9 | 数据源动态加载 | `ChatPage.tsx` | 从 GET /datasources 动态获取选项 | 开发完成 | P2 |
| 18.2.10 | 分析结果稳定渲染 | `DataTable.tsx`、`ResultCard.tsx` | 所有行字段并集兜底、按数据源展示最终 SQL、table 结果不重复显示空图表；指标目录按主题展示，业务分析按关联指标/影响/原因/建议分区 | 单测完成 | P1 |
| 18.2.11 | 并行模型流分组 | `useChat.ts` | 兼容识别 stream_id，避免原始中间响应进入聊天消息；多数据源结果按最终结构化事件归并 | 单测完成 | P1 |
| 18.2.12 | 历史会话逐轮恢复 | `useChat.ts`、`ChatPage.tsx` | 每轮消费自己的 final_result，恢复 SQL、数据、分析、图表和推理；长会话向前分页；兼容 StrictMode 回放和路由切换后的状态保持 | 集成测试完成 | P1 |
| 18.2.13 | 触点归因结果展示 | `ResultCard.tsx`、`types/index.ts` | 展示选中模型、选择来源、资格摘要和按触点归因值/占比，证据不足时展示缺口 | 单测完成 | P1 |
| 18.2.14 | 可信知识来源提示 | `ChatPage.tsx`、`types/index.ts` | 结果卡只展示去重后的可信来源摘要，最多平铺两个标签，其余聚合为数量并通过 Tooltip 查看来源元数据，不展示知识正文 | 单测完成 | P3 |

### 18.3 数据源管理页 (DatasourcePage)

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.3.1 | 数据源列表 | `frontend/src/pages/DatasourcePage.tsx` | Table 展示名称/方言/主机/端口/数据库，支持分页 | 开发完成 | P2 |
| 18.3.2 | 新增数据源 | 同上 | Modal 表单（名称/方言/主机/端口/数据库/用户名/密码/描述） | 开发完成 | P2 |
| 18.3.3 | 删除数据源 | 同上 | Popconfirm 确认删除 | 开发完成 | P2 |
| 18.3.4 | 编辑数据源 | 同上 | 更新已有数据源配置，空密码沿用原凭证，探测和持久化成功后替换旧配置 | 单测完成 | P2 |
| 18.3.5 | 测试连接 | 同上 | 新增或编辑前使用短生命周期连接探测，不注册、不落库并及时释放资源 | 单测完成 | P2 |

### 18.4 表结构浏览页 (SchemaPage)

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.4.1 | 表列表展示 | `frontend/src/pages/SchemaPage.tsx` | Table 含表名/描述/字段数，可搜索 | 开发完成 | P2 |
| 18.4.2 | 展开行查看字段 | 同上 | 展开行显示列名/类型/注释/可空/主键 | 开发完成 | P2 |
| 18.4.3 | Schema 刷新 | 同上 | POST /schema/refresh 重新扫描数据库 | 开发完成 | P2 |
| 18.4.4 | 字段详情抽屉 | 同上 | 点击表名打开 Drawer 显示完整列信息 + 表关系 | 开发完成 | P2 |
| 18.4.5 | 列注释编辑 | 同上 | 对接 PUT /schema/tables/{name}/columns/{col}/comment，显式携带当前 datasource | 单测完成 | P2 |

### 18.5 查询历史页 (HistoryPage)

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.5.1 | 历史列表 | `frontend/src/pages/HistoryPage.tsx` | Table 含时间/查询/SQL/状态/行数/失败原因 + 数据源筛选下拉 | 开发完成 | P2 |
| 18.5.2 | 搜索过滤 | 同上 | 按查询内容/SQL 关键字前端搜索 | 开发完成 | P2 |
| 18.5.3 | 回放历史对话 | 同上 | 点击历史条目逐轮恢复结构化响应，Checkpointer 不可用或服务重启时回退 JSONB 查询历史 | 集成测试完成 | P3 |

### 18.6 指标文档管理

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.6.1 | 指标列表 | `frontend/src/pages/MetricsPage.tsx` | 租户指标列表、搜索、分页、历史、生效时间和复制固定版本引用 | 集成测试完成 | P3 |
| 18.6.2 | 指标编辑 | 同上 | 租户管理员保存新版本、提交/批准/驳回，冲突不覆盖，普通成员只读 | 集成测试完成 | P3 |

### 18.7 基础设施

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.7.1 | API 客户端 | `frontend/src/api/client.ts` | get/post/streamChat 封装 + SSE 解析；支持 AbortController 取消回调和 FastAPI detail 错误解析 | 单测完成 | P2 |
| 18.7.2 | useChat Hook | `frontend/src/hooks/useChat.ts`、`chatStorage.ts` | 对话仅保留在内存，历史由授权 API 恢复；由 ChatProvider 在路由外层持有 SSE 与轮次状态；身份变化时清理旧状态；启动时清理旧版 sessionStorage 敏感缓存 | 单测完成 | P2 |
| 18.7.3 | TypeScript 类型 | `frontend/src/types/index.ts` | SSEEvent / ChatResponse / DatasourceConfig / TableInfo / ColumnInfo，包含激活 Skill 和知识来源摘要 | 开发完成 | P2 |
| 18.7.4 | 公共组件库 | `frontend/src/components/` | SqlPanel / DataTable / ChartPanel / ProgressBar / ReasoningPanel / ResultCard | 开发完成 | P2 |
| 18.7.5 | ErrorBoundary | `frontend/src/components/ErrorBoundary.tsx` | React Error Boundary 包裹所有页面路由 | 开发完成 | P2 |
| 18.7.6 | 服务状态指示器 | `App.tsx` Header | 启动时 GET /health，仅展示应用服务正常或不可用；租户 LLM 状态由管理连接测试负责 | 开发完成 | P2 |
| 18.7.7 | 响应式布局 | 全局 | 767px 以下使用抽屉导航，Header/对话/图表/表格和 Modal 自适应 | 单测完成 | P3 |
| 18.7.8 | SSE 读取失败处理与依赖安全 | `frontend/src/api/client.ts`、`frontend/src/App.tsx`、`frontend/src/components/ChartPanel.tsx`、`vite.config.ts`、`package.json` | reader 异常调用 onError 且不触发 onDone；页面与 ECharts 按需加载并拆分 vendor chunk；ECharts 6.1.x，npm audit 无漏洞 | 单测完成 | P1 |

### 18.8 高级特性

| # | 功能 | 描述 | 状态 | 优先级 |
|---|------|------|------|--------|
| 18.8.1 | 数据脱敏提示 | 后端字段级元数据贯穿单源、多源、补查、响应和历史；表头与摘要标识，不从星号推断 | 集成测试完成 | P3 |
| 18.8.2 | 技能选择面板 | 对话前按复合 resource_id 选择已启用 Skill，后端重新校验身份与状态 | 单测完成 | P3 |

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
| 18.10.7 | 删除条目/文档 | 同上 | 系统与 Schema 内省条目禁用删除并提示「随表结构自动维护」，用户上传条目按 can_delete 控制 | 单测完成 | P1 |
| 18.10.8 | 三范围知识视图 | 同上 | system/tenant/private 筛选、范围徽标及按角色控制上传范围 | 单测完成 | P1 |
| 18.10.9 | 多标签上传 | 同上 | 标签远程搜索、多选、创建个人标签和数据源绑定 | 单测完成 | P1 |
| 18.10.10 | 全局标签治理 | 同上 | super_admin 创建/停用全局标签并提升个人标签 | 单测完成 | P1 |

### 18.11 MCP 管理页 (McpPage)

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.11.1 | MCP 三级作用域管理 | `frontend/src/pages/McpPage.tsx` | 创建范围选择、范围徽标、同名资源按 scope 测试和删除 | 开发完成 | P1 |

### 18.12 租户身份与 LLM 管理

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.12.1 | 租户编码登录 | `LoginPage.tsx`、`AuthContext.tsx` | 固定输入 tenant_code+大小写敏感用户名+密码，不展示公开注册 | 单测完成 | P0 |
| 18.12.2 | 角色化管理工作区 | `AdminPage.tsx`、`App.tsx` | super_admin 管租户/模型目录，tenant_admin 管当前租户用户/LLM 连接 | 单测完成 | P0 |
| 18.12.3 | 对话连接与模型选择 | `ChatPage.tsx`、`useChat.ts` | 展示当前租户连接和模型，默认值自动选择并随请求传递 | 单测完成 | P0 |
| 18.12.4 | 模型能力动态表单与物理删除 | `AdminPage.tsx` | 厂商可视化维护能力字段，模型动态渲染控件，厂商/模型支持确认删除 | 单测完成 | P0 |
| 18.12.5 | 对话推理开关与深度 | `ChatPage.tsx`、`useChat.ts`、`client.ts` | 按模型能力启用推理开关并动态展示有效深度，统一随请求传递 | 单测完成 | P0 |
| 18.12.6 | 租户管理员展开治理 | `AdminPage.tsx` | 超级管理员租户列表展示编码，展开行按需查看管理员并重置密码 | 单测完成 | P0 |

### 18.13 报表视觉模板渲染

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.13.1 | 报表模板渲染与切换 → 见 23.15 | `frontend/src/report/templates.ts`、`frontend/src/components/report/TemplateScope.tsx`、`frontend/src/components/report/chartBase.ts`、`frontend/src/hooks/useReportTemplate.ts`、`frontend/src/index.css` | 四套内置模板的 token 注册表与解析回退；CSS 变量 + 嵌套 antd ConfigProvider + ECharts 基线三面注入，作用域限定报表容器；渲染器消费 `document.theme.template_id`；阅读端与分享页可临时切换。设计见 `spec/41-report-visual-templates.md` | 待开发（→ 见 23.15） | P2 |

> 18.13 是 23.15 的渲染侧视角，主实现与完成状态在 `features/23-report-workbench.md` 下跟踪，本页不重复计数。

### 18.14 我的长期记忆页 (MemoryPage)

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.14.1 | 记忆列表 | `frontend/src/pages/MemoryPage.tsx`、`frontend/src/App.tsx` | 菜单「我的记忆」全角色可见；类型筛选 + 关键词搜索 + 分页，展示访问次数与置信度 | 单测完成 | P1 |
| 18.14.2 | 记忆详情 | 同上 | Modal 展示完整内容、问题与 SQL（payload），不支持在线编辑 | 单测完成 | P1 |
| 18.14.3 | 删除记忆 | 同上 | Popconfirm 二次确认，删除后刷新；越权由后端返回 404 | 单测完成 | P1 |
| 18.14.4 | 上传表单交互 | `frontend/src/pages/KnowledgePage.tsx` | 切块与重叠滑块+手动输入（overlap 上限随 chunkSize 收窄）、数据源下拉、标签弹出面板多选 | 单测完成 | P1 |
| 18.14.5 | 列表来源分层与系统知识开关 | 同上 | entry_kind 分段控件（文档知识/Schema 内省/全部）+ include_system 复选框（默认关、不持久化） | 单测完成 | P1 |
| 18.14.6 | 创建人列 | 同上 | 条目表与文档表展示 owner_username，无主显示「系统」 | 单测完成 | P2 |

### 模块收尾

模块功能点共 70 项，已完成 70 项，待开发 0 项。18.13 为渲染侧指针，其实现在 23.15 下跟踪，不计入本页待开发数。

| 功能点 | 不开发原因 | 可开发条件 | 预计开发时机 |
|--------|------------|------------|--------------|
| 无 | 当前模块台账无待开发项 | 指标目录生产使用前需应用 019 迁移；真实 Vault/PG 部署验收不包含在浏览器测试中 | 上线前 |

2026-09-06：指标与脱敏功能经开发完成后通过 Python 回归及四条真实 Edge 界面用例。
日常离线套件（含此前两条报表浏览器测试）共 1675 passed、1 deselected；未调用远程 LLM。

## 2026-09-13 报表工作台三子页与通知中心（需求 1、2、3）

本轮新增两项功能点，模块功能点由 70 项增至 **72 项**，均已完成（前端契约测试通过）。18.1.2 的菜单/路由随之扩展。

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.15 | 报表工作台三子页 | `frontend/src/pages/ReportCreatePage.tsx`、`ReportListPage.tsx`、`LinkManagementPage.tsx`、`frontend/src/report/promptExamples.ts`、`frontend/src/report/status.ts` | 父菜单拆成创建报表 / 报表信息 / 链接管理三个子项，各按 `report.create_page` / `report.list` / `report.link_manage` 控制可见性，旧 `/report-workbench` 重定向到创建页；创建页左表单右案例（案例为纯静态数据文件，不调接口，点击仅回填提示词）；报表信息页 7 列（标题/数据源/发散档位/状态/运行次数/下次运行时间/创建时间）按创建时间倒序，租户管理员可按用户筛选，非终态 2.5 秒轮询，详情抽屉含计划查看与发散单元删减、调度编辑、逐页延期/永久与「生成」「重试计划」「删除」；旧「自动化」菜单与路由摘除 | 单测完成 | P1 |
| 18.16 | 通知中心 | `frontend/src/components/notifications/`、`frontend/src/pages/NotificationsPage.tsx`、`frontend/src/hooks/useNotifications.ts`、`frontend/src/api/notifications.ts` | 顶栏铃铛（未读角标、0 隐藏、99+ 截断）与收件箱抽屉/整页；四类通知展示、标记已读、全部已读、软删；发布界面按角色开放（平台通知仅超管、租户与用户群仅租户管理员，个人通知不在此发布）；租户级渠道配置，敏感项只显示「已配置」不回显明文，并明示邮件与飞书/Slack 暂不外发；WebSocket 推送（握手复用 HttpOnly Cookie，URL 不带令牌）断线指数退避重连、长连不可用降级 60 秒轮询，未读数以推送值为准不做本地自增；StrictMode 双挂载不产生多余连接 | 单测完成 | P1 |

界面支撑：共享状态映射抽到 `frontend/src/report/status.ts`（与后端状态枚举同源），案例数据独立成文件便于维护；通知铃铛经 `App.tsx` 顶栏接入，路由 `/notifications` 懒加载。前端契约测试新增 `tests/test_frontend/test_report_pages.py`（14 例）与 `test_notifications_ui.py`（17 例），并把 `test_p3_experience.py` 里「自动化菜单存在」的断言改为「菜单/路由/引用均已移除，页面文件保留」。`npx tsc -b` 退出码 0；前端套件 177 passed、22 skipped（浏览器用例默认 skip）。

---
