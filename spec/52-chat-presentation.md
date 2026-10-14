# 对话分析展示与流式正文

日期：2026-10-09。用户已确认；替代忽略全部正文增量的行为，原始推理仍禁止外传。

## 展示与语言
- 成功结果依次展示图表/核心数值、数据详情、分析正文；SQL 折叠。
- 展示语言优先用户明确要求，其次本轮问题、历史上下文、界面语言，默认 zh-CN。字段标签与数据键分离，不修改 SQL、原始数据或无可靠映射的枚举值。
- 模型输出字段标签与受约束 chart_plan（x、y、series、facets、metrics、aggregation），只接受真实字段，不接受可执行图表代码。
- 初始图表与类型切换共用构建器；日期优先横轴，额外维度拆系列或分面，不覆盖重复坐标，不静默截断系列，不默认求和。
- 地区、品类等 _code 编码可作为分类维度，不作为数值指标；metrics 保留用户明确要求的全部指标，不同单位由渲染器拆独立分面，facets 仅填写分类维度。
- 覆盖折线、柱状、饼图、散点、热力图、表格。缺失值为 null；折线 connectNulls=true 连接有效点，不插值或补零，表格/提示保持缺失语义。

## 流式协议
- 前端仅消费 answer_delta/answer_reset 公开正文，忽略内部 token 和 reasoning_content；按 node/stream_id 隔离，分析 JSON 仅提取 summary。
- analysis_patch 仅逐项提取顶层白名单洞察、业务影响、行动建议等列表；直接回答只提取顶层 answer，内部节点与嵌套同名字段禁止外传。
- 工具探索阶段可缓冲，公开回答使用无工具流式调用；工具参数和中间正文不进入公开回答。
- 已脱敏的数据与图表分别推送 data/chart；analysis 和最终 result 校准正文，不能重复追加。取消、断流、重试、失败恢复分别处理。
- 仅收到 done(status=complete) 才视为请求完成；提前 EOF 或 done(status=error) 显示失败。UTF-8 分片与尾部缓冲完整解码，旧轮次分片不得写入新轮次。
- JSON 契约失败重试时先 answer_reset，同时清空旧公开章节，不能把失败回合的建议追加到新答案。

## 全局过程摘要
- chat_show_analysis_process 默认 false，加入平台热配置；沿用 super_admin 权限、版本锁与审计，聊天入参不可覆盖。
- 开启时 process_summary 推送业务节点、状态、受控说明、耗时，不包含原始推理、Prompt、工具参数或业务正文。
- 单一默认折叠面板按节点分组，重试更新、并行隔离；关闭不推送摘要，不影响必要进度。
- 开关在请求开始时快照，后续请求使用新配置；历史恢复根据保存的 process_summary_enabled 决定是否显示安全业务摘要，原始思维链始终不展示。

## 接口与构建边界
- ChatRequest.display_language 是可选语言码（默认 zh-CN），透传到分析提示词及图表；不能用聊天参数开启全局过程摘要。
- AnalysisOutput 增加 column_labels 和 chart_plan；ChartPlan 使用 Pydantic 校验，aggregation 只允许 none/sum/avg。
- ChartAdjustRequest 增加 chart_plan、column_labels；手动切换通过 adapt_plan 适配坐标轴角色，复用原聚合口径并拒绝不存在字段。
- src/tools/chart_presentation.py 的 resolve_language、column_labels、build_option、adapt_plan 为共用入口。generate_chart 与 /charts/adjust 均调用该构建器。
- 重复坐标无明确聚合口径时拒绝图表并保留完整数据表，口径只能由明确决策给出：分析提示词要求模型先判断是否为查询漏带区分字段（单列不唯一、组合唯一）造成，缺维度时在数据质量说明并追问、语义为合并总量时显式 aggregation=sum/avg；失败契约保留 chart_plan 与列标签，界面提供「按名称合并求和」一键重画，不引入静默默认。多指标默认拆分独立坐标轴，超过 8 个分面不静默截断。饼图拒绝负值与全零，散点图要求两条有效数值轴，热力图保留两条分类轴及其余维度。

聊天请求示例（POST /chat）：
```json
{"query":"过去一周各地区销售趋势","stream":true,"display_language":"zh-CN"}
```

公开 SSE 事件示例（每个 data 行之后有空行）：
```text
data: {"type":"presentation","process_summary_enabled":false}

data: {"type":"answer_reset","node":"analyze_result","stream_id":"run-1"}

data: {"type":"answer_delta","node":"analyze_result","stream_id":"run-1","content":"销售整体增长"}

data: {"type":"analysis_patch","node":"analyze_result","stream_id":"run-1","analysis":{"insights":["地区之间存在差异"]}}

data: {"type":"done","status":"complete"}
```

管理员热配置示例（PUT /admin/runtime-settings，revision 使用 GET 返回的当前版本）：
```json
{"revision":2,"values":{"chat_show_analysis_process":true}}
```

启动默认值可使用 CHAT_SHOW_ANALYSIS_PROCESS=false 或 YAML 的 chat_show_analysis_process: false；平台热覆盖优先，用户与租户管理员不得读写该平台配置。

## 验收
- Fake LLM 验证增量早于 final、工具过滤、并行隔离、非法输出与重试、管理员开关、取消与历史恢复。
- 图表覆盖日期×地区×产品、多指标、重复坐标、缺失/零、标识符、负数饼图、热力图及类型切换，输入不被修改。
- 离线测试与真实鉴权服务验收分别报告。
- 2026-10-09：统一聚焦回归 326 项通过，Python 3.14 语法及 Ruff 检查通过，前端生产构建通过；独立 Edge 浏览器用例通过，覆盖结果结束前的数据/正文/洞察展示、显示顺序、过程面板开关和取消下一轮恢复。
- 首轮浏览器使用隔离 API 与实际 ReadableStream，后端使用实际 LangGraph/Fake LLM；该轮没有调用远程模型，不能代替真实业务验收。后续真实模型验收及尚未完成的边界见下节。

## 2026-10-09 首轮真实验收（重启前）

用户授权重启并要求 bsk 浏览器、curl、代码、日志与数据库交叉核验。服务停止和独立服务启动命令均被执行环境策略拦截；8000/5173 仍为原进程。bsk doctor/browsers 确认没有已连接扩展，未开展真实业务浏览器 UI 操作，不宣称完成浏览器、刷新身份或真实网络断流验收。

| 验收方式 | 结果 | 证据与范围 |
|---|---|---|
| curl 现服务 | 基础通过，新版待部署 | 两个测试账号登录 200；test2 的平台配置读取 403；超管目录仍无新增开关，确认运行服务尚未加载新版 |
| 新版真实模型/数据库 ASGI | 核心链路通过 | 不 Mock 模型或数据库，在测试进程加载真实应用；不是网络 Socket 或浏览器验收 |
| 前后端代码核查 | 定位并修复展示缺陷 | region_code 被维度排除规则误拒绝；多指标提示词没有明确要求保留全部指标 |
| 后端日志 | 与结果一致 | 实际查询执行、分析回合、图表成功/负值拒绝、流结束及管理员操作有记录，无新增诊断探针 |
| 数据库只读核验 | 通过 | test2 身份为 tenant_id=3/user_id=3；历史持久化成功；全局覆盖恢复为空，revision=2，保留 2 条 actor_user_id=1 的配置审计 |
| bsk 真实浏览器 UI | 未完成 | 无已连接扩展，且原后端尚未重启 |

### 已验证细节
- 中文真实查询返回 49 行，312 个 answer_delta 在 result 前发出：首个公开增量约 8.419 秒、最终结果约 34.099 秒；日期主轴、地区编码分组、销售金额与订单数量分别分面。过程摘要默认关闭，过程事件为 0。
- 英文展示查询返回 49 行、259 个公开增量，展示语言 en-US，表头为 Date/Region/Net Sales Amount/Order Count；界面 zh-CN 不覆盖明确英文要求。
- 同一真实结果切换折线、柱状、散点、热力图和表格返回 200；收入中存在负值，饼图正确返回 422，使用真实的非负订单数量指标可生成饼图，未对收入取绝对值。
- 缺失测试只在内存副本置空一个指标，响应保留 null 且折线 connectNulls=true；原始查询结果不变，不补零或插值。
- 仅超管可开启全局摘要，租户管理员 GET/PUT 均 403。开启后真实直接问答包含 3 个业务节点的 6 条过程状态及 23 个公开正文增量；测试结束恢复原覆盖，最终保持默认关闭。
- ASGI 流执行期间身份请求 200、约 76 毫秒；非法聊天入参 422 后下一次真实问答正常完成，结束后身份仍为 200。此项不替代真实浏览器取消、Cookie 刷新及网络断流测试。
- 追加地区字符串/整数编码、五种图表类型、图表节点和多指标提示词回归；统一聚焦测试 338 项通过，Ruff 检查通过。验收日志与证据扫描未发现原始 JWT。

### 尚未关闭的问题
1. 本轮之后用户已手动重启后端，真实网络与独立浏览器验收见下一节；bsk 扩展仍需连接，不能将独立浏览器验收冒充 bsk 操作。
2. 额外发现既有 TaskPlan 时间范围规则仅按词匹配：明确的英文 `from January 1 to January 7, 2026` 被判为 needs_input 并重复要求时间范围。加入既有规则支持的 week 文案后可完成英文展示测试，但这不代表原英文日期问题已修复。本轮未夹带该跨模块日期规则修改。

脱敏证据：logs/chat-live-acceptance-evidence-20261009.json、logs/chat-live-db-proof-20261009.json；日志：logs/chat-live-process-20261009.log、logs/chat-live-retest-20261009.log。这些本地验收产物位于忽略目录，不包含账号密码、Cookie 或完整业务结果。

## 2026-10-09 重启后部署验收（核心通过，尚未全部验收）

用户手动重启后端后，确认 8000 新进程为 5712，5173 前端进程为 23948。两测试账号经过正式登录验证，超管运行参数目录包含新开关。通过 curl 的真实网络 Socket、Vite 前端代理、真实模型与数据库，以及独立 Edge/Playwright 的真实 UI 操作验收；不 Mock 请求、模型或数据库。本轮未修改生产代码，没有新增后端诊断探针，也未重启用户的其他进程。

### 已通过的真实路径

| 验收项 | 结果 | 证据与范围 |
|---|---|---|
| 新版认证与开关权限 | 通过 | 默认 false；tenant_admin GET/PUT 均 403；super_admin 可保存；旧 revision 为 409、字符串布尔值为 422 |
| curl 真实正文流 | 通过 | 中文/英文各 49 行，337/365 个公开正文增量在最终结果前接收；均收到 done(status=complete) |
| 数据优先与增量渲染 | 通过 | 中文网络流约 18.853 秒收到数据、19.355 秒收到首个正文、48.728 秒收到最终结果；浏览器在未完成时显示数据并观察到 70 个不同正文长度状态 |
| 展示布局和历史回放 | 核心通过 | DOM 顺序为图表→数据→分析；刷新后身份仍为 tenant_admin，通过历史会话入口恢复同一顺序 |
| 图表类型与缺失值 | 核心通过 | 折线、柱状、散点、热力图、表格为 200；负值饼图为 422，真实非负订单指标饼图为 200；内存副本保留 null 且 connectNulls=true，不修改原结果 |
| 网络断开与新轮恢复 | 通过 | curl 12 秒主动断开，退出 28、未收到 done；后续真实问答正常完成；浏览器点击取消导致原请求失败，新轮完成，无身份跳转或旧流污染 |
| 并发身份 | 通过 | 网络流执行中 /auth/me 返回 200、约 249 毫秒，authenticated=true |
| 单源中文/英文标签 | 通过 | 中文表头按问题展示，明确英文要求优先于 zh-CN 界面；图表和表格共用标签；英文日期 guard 单独列为失败，不使用 week 用例代替 |
| 管理员 UI 与多节点摘要 | 通过 | 租户管理页没有运行时配置页签；超管在工作区真实开启并保存；摘要外层默认折叠，包含 3 节点，可分别展开 |
| 数据库与日志 | 通过（含预期负例） | 只读核验 users、platform_runtime_settings、platform_runtime_audit、sessions、query_history；最终 revision=8、覆盖为空，保留 8 条 actor_user_id=1 的开关审计，最后一轮 UI 的 3 个会话及结构化历史已持久化 |
| bsk 专项 | 未完成 | daemon 正常，但 browsers=[]，没有连接浏览器扩展；Edge/Playwright 结果单独标明，不等同 bsk |

浏览器按 1440/768/375 像素视口实测，页面外层均无横向溢出；此项不代表图表刻度可读性全部合格。未捕获页面运行异常，控制台存在 Antd 弃用/上下文警告、favicon 404，以及预期的负值饼图 422。后端两条 pg_connection ERROR 经堆栈确认分别是版本冲突 409 和类型校验 422 的 HTTPException，不是数据库断连；没有因此误判认证失效。

### 修复前验收问题（历史记录）

| 编号 | 问题与复现 | 已有定位证据 | 不在本轮修改原因 | 可开发/验收条件 | 预计时机 |
|---|---|---|---|---|---|
| QA-28-1 | 中文跨源结果仍有 region_code、sales_amount 等英文展示名 | 真实 oracle+mysql 查询返回 105 行，历史 column_labels 保留英文键名；该跨源最终分析没有 chart_plan，未提供完整字段标签，column_labels 的可靠别名兜底未覆盖这些字段；原数据未改写 | 本轮为重启后的验收，未夹带跨源实现修改 | 已有真实回放、curl、数据库和源码证据；可先补跨源标签回归，再做最小修复 | 下一轮优先修复 |
| QA-28-2 | 375 像素多指标折线图部分纵轴数字拥挤，标题与轴名称重复 | 单源和跨源实际截图均可复现；页面无外层横向溢出，但固定分面高度和轴标签布局不足以保证窄屏可读性 | 本轮记录视觉问题，不将“无横向溢出”误报为布局完全通过 | 截图及真实数据已具备；补窄屏多面板回归并分别检查所有图表 | 下一轮展示优化 |
| QA-28-3 | 英文明确月份日期被重复要求补充时间范围 | 原始 from January 1 to January 7, 2026 在重启后的 curl 中仍返回 needs_input、needs_time_range=true；src/graph/contracts.py 既有规则未识别该日期表达 | 属于既有 TaskPlan 日期规则缺陷，本轮没有跨模块改动 | 真实失败路径与源码已具备，可直接补日期解析回归 | 下一轮优先修复 |
| QA-28-4 | 用户指定的 bsk 真实操作未执行 | bsk browsers 仍为空 | 浏览器扩展外部依赖未就绪 | 用户连接扩展后 bsk browsers 出现浏览器 | 连接后补验收 |

脱敏证据：logs/chat-network-acceptance-evidence-20261009.json、logs/chat-network-english-date-guard-evidence-20261009.json、logs/chat-browser-acceptance-evidence-20261009.json、logs/chat-browser-multisource-completed-evidence-20261009.json、logs/chat-browser-source-proof-20261009.json、logs/chat-post-restart-proof-20261009.json。截图：logs/chat-browser-chart-{1440,768,375}-20261009.png。验收脚本和证据均在本地忽略目录；Cookie、密码与原始行集不落盘，凭证扫描命中 0。

本节结论仅适用于此前重启后的验收：当时核心部署路径通过，但 QA-28-1/2/3 和 bsk 专项未闭环，当时没有改生产代码。最新修复已经修改后端源码，是否需要重启及当前验收结论以下节为准。

## 继续修复及新版链路复验（2026-10-09）

### 已确认根因与最小修复
- 中文跨源字段：共用 column_labels 未覆盖可靠业务别名，且 supplied 中的原键名/格式化英文占位可覆盖可靠中文。补充 region_code/region_name/sales_amount/net_sales_amount 映射并识别占位标签；不翻译未知缩写、不替换原始地区编码或业务值，保留有意义的自定义标签和英文结果语言。
- 窄屏图表：固定分面高度不足，且移动 CSS 的 height:280px !important 覆盖动态高度，真实浏览器复现多面板被压缩至 280px。新增纯布局模块 frontend/src/components/chatChartLayout.ts，ChartPanel 使用 ResizeObserver 测量容器，删除该 CSS 高度覆盖。数字刻度按结果语言紧凑显示、隐藏重叠、去掉重复轴名；标题/图例、热力色阶与饼图位置按面板重排，保留全部分类、数据、null、索引和精确提示值。
- 完整英文日期：build_task_plan 仅检查既有时间词，未识别 January 7, 2026 等完整日期。新增日历有效性检查，支持英文月份先后顺序、缩写/序数及 YYYY-MM-DD/斜杠格式；不补年份、不接受非法日期/编码内日期，相对时间规则和澄清回退不变。
- 非流式元数据：真实新版 /chat 的 final_response 已含中文标签，图表标签也正确，但 ChatResponse 未声明且路由构造未传入顶层展示字段。补充 display_language、column_labels、process_summary_enabled，与公开最终结果一致，缺失时分别采用请求语言、独立空字典和 false；不返回 reasoning_content 或其他原始推理。

非流式公开响应示例（省略业务行集）：
```json
{"success":true,"status":"success","display_language":"zh-CN","column_labels":{"sales_amount":"销售金额","region_code":"地区编码"},"process_summary_enabled":false}
```

### 本轮验收矩阵

| 路径 | 实测结果 | 边界 |
|---|---|---|
| 统一聚焦 pytest 回归 | 256 项通过；日期合法/非法/缺年份、三种 SQL 方言、跨源预计算路由、标签/响应契约、流式和纯布局回归 | 使用 Fake/Mock 模型，不调用真实远程模型；不与此前 338 项等计数累加 |
| 隔离真实 Edge 浏览器 | 2 项综合用例通过，六类展示、1440/768/375 后返回宽屏、ResizeObserver、实际 CSS 高度、null 和原数据保持；公开增量与过程面板回归 | 初始化业务 API 隔离，不冒充生产服务验收 |
| 运行前端真实历史 UI | 正式表单登录，实际会话回放和图表菜单切换通过；多面板高度不少于 600px，375px 刻度紧凑、标题不再与纵轴名称重复，无页面异常 | 使用当前 5173 前端；8000 仍为本轮修改前后端 |
| 原英文月份日期失败路径 | 原问题 from January 1 to January 7, 2026 不加 week；返回 49 行、en-US、图表成功、335 个公开增量早于 result，首增量约 7.65 秒、result 约 40.89 秒，done complete | 新版 ASGI 测试进程；真实模型/MySQL/PostgresSaver，非网络 Socket |
| Oracle+MySQL 中文非流式路径 | 返回 105 行，顶层及图表标签包含“地区编码”“销售金额”，zh-CN、图表成功，请求后身份 200 | 真实模型和两种数据库；新版 ASGI，部署 Socket 尚待重启 |
| 最新历史与平台设置只读核验 | 两条最新中英文历史的行数、语言、字段标签、图表与摘要关闭状态落库正确；覆盖为空、revision=8、8 条开关审计均由超管产生 | 未修改运行配置，历史持久化不只靠 Mock 推断 |
| 静态检查与构建 | Ruff、Python 3.14 语法、git diff --check、前端生产构建通过 | 既有依赖弃用和 Antd 大包提示未夹带处理 |

### 当前未关闭边界
- QA-28-1/3/5 的代码和新版真实 ASGI 验收通过，但实际 8000 仍是 2026-10-09 21:35 启动的 PID 5712，早于本轮后端修复；需要用户手动重启后用 curl 与真实 UI 再确认新版 Socket，不能把测试进程通过当成部署通过。此前服务管理被策略阻断，本轮未用替代方式绕过。
- QA-28-2 窄屏布局缺陷已关闭，前端热加载生效；新版跨源标签仍须随后端重启后的实际 UI 再看。
- QA-28-4 仍因 bsk 扩展未连接而待验收；独立 Edge/Playwright 的真实 UI 证据不等于执行了 bsk。
- 无新增运行期临时探针，摘要默认关闭、权限和原始推理隔离不变；本轮待部署边界见 features/28-chat-presentation.md 的当前验收表，功能总数仍为 5、实现完成 5、待开发 0。

脱敏证据：logs/chat-fix-live-evidence-20261009.json、logs/chat-fix-layout-evidence-20261009.json、logs/chat-fix-database-proof-20261009.json；回归/构建日志：logs/chat-fix-final-regression-20261009.log、logs/chat-fix-final-browser-20261009.log、logs/chat-fix-final-frontend-build-20261009.log。真实前端截图为 logs/chat-fix-chart-{1440,768,375}-20261009.png；高图截图可能受聊天滚动容器裁剪，不能用裁剪区域推断数据缺失。验收脚本与证据位于本地忽略目录，不记录密码、Cookie 或完整业务行集。

## 重复坐标口径补充（2026-10-14）

- 背景：工业演示库 t_workshop 约 120 行、多工厂存在同名车间，SQL 按车间实体分组但只选出 workshop_name，同一坐标出现多行；模型遵循默认 aggregation=none 时图表被构建器拒绝，界面只得到原始校验文案，且失败契约未保留可重画方案。
- 改动：分析提示词第 15 条新增重复坐标决策规则（先对照 Schema 判断是否为查询漏带区分字段的「单列不唯一、组合唯一」；缺维度在 data_quality 说明并追问、有维度放 series/facets、语义为合并总量时显式 sum/avg 并在 summary 说明、否则 table）；generate_chart 失败契约保留 chart_plan/column_labels/display_language，重复坐标失败改为面向用户的原因；ChartPanel 失败空状态提供「按名称合并求和」按钮，经 /charts/adjust 显式附加 sum 口径重画，成功后类型切换沿用该口径。后端不默认求和与构建器拒绝规则不变。
- 验证边界：新增提示词断言、失败契约单测与前端交互回归，聚焦回归统一通过；本轮未调用真实远程模型，未在测试环境做浏览器验收。

## 重复坐标合并确认闸门（2026-10-14 续）

- 背景：实机复现发现模型在同一请求的多轮分析中反复给出 `chart_plan.aggregation=sum`（正文却写明"不能把同名行相加"），绕过"重复坐标配 none 才拒绝"的保护后静默合并渲染；"华东制造基地"模糊匹配场景同样复现。同一问题出不出错图取决于模型采样随机性，提示词约束不足以兜底。
- 改动：构建器 `build_option`/`_panel`/`_reduce` 新增 `allow_implicit_merge` 参数（默认 True，保持 /charts/adjust 手动重画链路行为）；generate_chart 对话链路显式传 False——同名重复坐标的合并必须经用户确认，模型自给的 sum/avg 与 none 一并转入待确认失败契约，前端「按名称合并求和」按钮确认后经 /charts/adjust 重画；提示词第 15 条收紧（缺维度分支 aggregation 必须保持 none、recommended_chart_type 用 table、禁止用 sum/avg 掩盖；合并总量口径仅限用户明确要求且无缺失区分维度；移除"重复坐标配 none 会被后端拒绝"的反向诱导，改为说明待确认是预期行为）。
- 验证边界：新增"模型聚合口径仍需用户确认"与"唯一坐标聚合不误伤"单测、提示词断言更新；全量回归通过（排除远程模型标记）；未调用真实远程模型，浏览器验收待用户环境进行。

## 合并确认闸门闭环（2026-10-14 审计轮）

- 复现的绕过路径：失败契约保留了模型自给的 `chart_plan`（含 aggregation=sum）；失败面板的类型切换菜单把该方案原样提交 `/charts/adjust`（该链路 `allow_implicit_merge` 默认 True），同名坐标被静默合并渲染——未经「按名称合并求和」确认也能出合并图；同时成功后类型切换未沿用已确认口径（实现与"成功后类型切换沿用该口径"不符）。
- 修复：`ChartPanel` 新增纯函数 `buildAdjustPlan(plan, confirmed)`——用户未确认合并时从请求方案中移除 `aggregation`（模型自给的 sum/avg 不再进入 /charts/adjust）；「按名称合并求和」成功后置确认标记，后续类型切换沿用已确认方案；重复坐标校验失败改为业务文案提示（`readAdjustError`），不向界面暴露后端术语。语义契约：/charts/adjust 请求中的显式 aggregation 即用户确认的口径；对话链路仍由 generate_chart 全程闸门拒绝任何未确认合并。
- 回归测试：tests/test_frontend/test_chat_presentation.py 以真实 TypeScript 转译执行 `buildAdjustPlan`/`readAdjustError`（未确认移除、确认保留、其他错误原样透出）；tests/test_graph/test_generate_chart.py 新增「失败契约未经确认不能重画、显式 sum 才合并」闭环用例。
- 残余边界：服务端未区分"用户确认"与"调用方携带口径"，直接调用 /charts/adjust 并显式给出 aggregation 视为用户口径选择（前端是唯一调用方且已保证不携带未确认口径）；如需服务端强隔离需新增确认令牌字段（schemas/路由，后续加固）。
