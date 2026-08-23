# DataAnalysisAgent 全功能测试报告

- **测试日期**：2026-08-23 20:20 ~ 21:50
- **测试环境**：本机开发环境（Windows）
  - 前端：http://localhost:5173（Vite + React + Ant Design）
  - 后端：http://localhost:8000（FastAPI）
  - 数据库：PostgreSQL（状态库）、MySQL 8.0（业务库 192.168.195.133:3306/test）
- **测试账号**：
  - 租户 test2 / 用户 test2 / 密码 12345678（`tenant_admin`）
  - 租户 default / 用户 admin / 密码 123456（`super_admin`，测试超管功能用）
- **测试方法**：真实浏览器 UI 操作（bsk）+ API 直接调用（curl）+ 前端/后端代码审查 + 后端日志验证

---

## 一、Bug 汇总表

| 编号 | 严重等级 | 模块 | 标题 | 状态 |
|------|---------|------|------|------|
| BUG-01 | P1 | 对话分析 | 点击「取消」后前端状态不恢复，回合卡片永久停留在加载中 | ✅ 已复现，根因已定位 |
| BUG-02 | P1 | 表结构 | 列注释编辑接口返回成功但注释不生效（GET 仍为旧值） | ✅ 已复现，根因已定位 |
| BUG-03 | P2 | 知识库 | 短文本（<50 字符）上传任务显示「成功」但 0 分块，内容从未入库 | ✅ 已复现，根因已定位 |
| BUG-04 | P2 | 前端通用 | API 错误响应未解析，错误提示显示原始 JSON（如 `{"detail":"..."}`） | ✅ 根因已定位（代码） |
| BUG-05 | P2 | 知识库 | 空标签数组被渲染为字符串 `['']` | ✅ 已复现 |
| BUG-06 | P2 | 对话分析 | 一次完整问答（含 2 轮补查）耗时约 4 分钟，LLM 单次调用 35~90 秒 | ✅ 已复现（性能） |
| BUG-07 | P3 | 历史 | features 18.5.3 声称「点击历史条目回放」，但 HistoryPage 无任何回放交互实现 | ✅ 代码确认 |
| BUG-08 | P3 | 对话分析 | 中间节点（generate_sql/analyze_result/supplemental_query）的 LLM 原始 JSON 全文直接渲染为聊天消息，无折叠/格式化 | ✅ 已复现 |
| BUG-09 | P3 | 前端通用 | antd 弃用 API 警告：Menu `children`、`findDOMNode`、Modal `destroyOnClose` | ✅ 浏览器控制台确认 |
| BUG-10 | P3 | MCP | 添加 MCP 失败时仅提示「添加失败」，不展示具体原因（如 host 不在 allowlist） | ✅ 已复现 |
| BUG-11 | P3 | Grafana | `/grafana/query` 时间过滤需 `$__timeFilter` 宏，接口文档未说明（行为正确，文档缺失） | ✅ 代码确认（非 bug，文档问题） |
| BUG-12 | P3 | 自动化 | 「立即运行」后任务列表「上次运行」列仍显示 `-` | ✅ 已复现，根因已定位 |
| BUG-13 | P1 | 知识库生效 | 知识库检索命中但注入 prompt 时被长期记忆挤占截断，知识实际不生效（业务口径被忽略） | ✅ 已复现，根因已定位 |
| BUG-14 | P2 | 知识进化 | 知识库自进化 SQL 观察对多语句 SQL 校验失败被拒，`knowledge_sql_observations` 恒为 0 | ✅ 已复现，根因已定位 |
| BUG-15 | P2 | 意图分类 | 简单统计查询（如「统计订单总数」）被识别为 chat 意图，不查数据库且回答误导（「当前未连接数据源」） | ✅ 已复现 |
| BUG-16 | P2 | 性能 | 每轮对话 prepare_turn 阶段长期记忆检索+touch 写放大，固定耗时 10~16 秒 | ✅ 已复现，根因已定位 |

> 严重等级说明：P1 = 功能错误/数据错误，影响核心体验；P2 = 明显功能缺陷；P3 = 体验/规范/文档问题。

---

## 二、Bug 详细描述

### BUG-01【P1】对话取消后前端状态不恢复（加载卡死）

- **触发条件**：在对话分析页发送任意问题，流式处理过程中点击「取消」按钮。
- **复现步骤**：
  1. 登录 test2 租户，对话页选择 mysql 数据源；
  2. 输入「本月各产品的销售额排名是怎样的？」并发送；
  3. 在节点推进过程中点击「取 消」按钮；
  4. 观察：输入栏恢复为可输入状态，但**回合卡片仍显示 loading 图标和「分析结果」节点 sync 状态，永不恢复**；后端连接实际已断开（日志确认 `API 访问完成 elapsed_ms=236328`）。
- **预期行为**：取消后回合卡片应标记为「已取消」或恢复为可重试状态，加载指示消失。
- **根因**（代码确认）：
  1. `frontend/src/api/client.ts` 第 183-188 行：`reader.read()` 抛 AbortError 后，`controller.signal.aborted` 为 true，**既不调用 onError 也不调用 onDone**，回调链断裂；
  2. `frontend/src/hooks/useChat.ts` 第 268-273 行：`cancel()` 只 `setLoading(false)`，**没有更新处于 `streaming` 状态的 turn**（turns 数组中的该轮仍为 streaming + progressNodes 残留）。
- **修复建议**：取消时主动把当前 streaming turn 置为 `done`/`cancelled`，并清空 progressNodes；client.ts 在 abort 分支调用一个独立的取消回调。

---

### BUG-02【P1】列注释编辑接口「假成功」

- **触发条件**：表结构页 → 表详情抽屉 → 点击字段「编辑」注释 → 保存。
- **复现步骤**（API 级）：
  1. `PUT /api/v1/schema/tables/categories/columns/category_name/comment?datasource=mysql`，body `{"comment":"QA 测试注释2"}`；
  2. 返回 `{"status":"ok","comment":"QA 测试注释2"}`；
  3. 立即 `GET /api/v1/schema/tables/categories?datasource=mysql`，`category_name` 的 comment 仍为 `""`。
- **预期行为**：PUT 成功后 GET 应返回新注释。
- **根因**（代码确认）：`src/knowledge/schema_manager.py::update_column_comment` 只更新 Chroma 向量缓存条目（`entry.content`），**不更新内存中 DataSource 对象的 `schema` 快照**；而 GET 接口（`src/api/routes/schema.py`）读的是 `ds.schema`。且 `refresh()` 会从数据库重新内省并覆盖缓存，因此该功能实际上**永远无法生效**（数据库原样，内存不同步，缓存被刷新覆盖）。
- **修复建议**：更新注释后同步修改内存 schema 快照（并考虑写回数据库 COMMENT 或持久化到独立标注表）。

---

### BUG-03【P2】知识库短文本上传「静默成功」实际未入库

- **触发条件**：上传内容过短（分块配置 `min_chunk_size=50` 时文本 < 50 字符）的 TXT/MD 文档。
- **复现步骤**：
  1. `POST /api/v1/knowledge/docs/upload?strategy=recursive&chunk_size=200&category=qa_test`，上传 34 字符的 `qa_test_kb.txt`；
  2. 返回 `tasks[0].task_id`，轮询 `GET /api/v1/knowledge/upload/status` 返回 `status=done, chunks_count=0`；
  3. 知识条目列表中不出现任何新条目。
- **预期行为**：要么至少生成 1 个分块，要么任务状态为失败/警告并提示「内容过短」。
- **根因**（代码+日志确认）：`src/knowledge/doc_parser.py` 分块器按 `min_chunk_size=50` 过滤了 34 字符的文本，`upload_manager.process()` 得到空列表后仍置 `status="done"`，`chunks_count=0`。日志：`文档分块 text_len=34` → `写入上传分块完成 count=0`。
- **修复建议**：分块结果为 0 时任务状态置 `error` 并给出可读原因；或对短文本放宽 min_chunk_size。

---

### BUG-04【P2】API 错误提示显示原始 JSON

- **触发条件**：任何后端返回 4xx/5xx 且响应体为 JSON 的请求（如数据源编辑连接失败、MCP host 不在 allowlist 等）。
- **复现**：数据源编辑保存失败时，前端 `message.error` 展示的是 `{"detail":"连接测试失败，原配置未变更"}` 原始 JSON 字符串。
- **根因**（代码确认）：`frontend/src/api/client.ts` 第 25-28 行 `throw new Error(text || ...)` 直接抛出 `res.text()` 全文，未解析 FastAPI 的 `detail` 字段。
- **修复建议**：尝试 `JSON.parse(text).detail` 作为错误消息，解析失败再回退原文。

---

### BUG-05【P2】知识库列表空标签显示为 `['']`

- **触发条件**：知识库「知识条目」列表任意无标签条目（本环境 577 条租户公共条目全部如此）。
- **复现**：知识库页列表「标签」列显示字符串 `['']`（应为空/「—」）。
- **根因**（推测，代码层）：前端将 `tags` 数组直接 `JSON.stringify` 渲染，空数组含空字符串 `['']`。
- **修复建议**：`tags?.filter(Boolean).join(', ') || '—'`。

---

### BUG-06【P2】单轮完整问答耗时约 4 分钟（性能）

- **触发条件**：普通聚合查询（含证据补查路径）。
- **复现**：`20:28 发送 → 20:33:20 取消`，日志 `elapsed_ms=236328`；其中补查 2 轮，每次 `analyze_result` 的 LLM 调用 35~90 秒（日志显示 `20:31:58.90` 开始调用，`20:32:36` 仍在执行）。
- **影响**：用户等待时间过长，取消成为常见操作，进而触发 BUG-01。
- **建议**：缩短 analyze_result prompt（当前预算 10000）、补查并发化、为每轮 LLM 调用增加合理超时。

---

### BUG-07【P3】历史页「回放」功能缺失

- **触发条件**：查询历史页。
- **复现**：历史列表行无任何点击/回放交互；`frontend/src/pages/HistoryPage.tsx` 全文件无 onClick/回放逻辑。
- **与文档不符**：`features/18-frontend.md` 18.5.3 状态标记为「集成测试完成」。
- **建议**：实现回放交互或修正 features 状态。

---

### BUG-08【P3】聊天页中间节点原始 JSON 全文渲染

- **触发条件**：任意一次问答。
- **复现**：消息区出现 `【generate_sql · 01a02e99】```json {...完整 SQL 生成 JSON...}````、`【analyze_result · …】{...完整分析 JSON...}` 等大段原始输出（数千字符），与最终结果卡片并存，用户看到的是调试格式而非可读内容。
- **根因**：`useChat.ts` 第 189-193 行把每个 LLM 流式调用缓冲的 token 以 `【node · id】` 拼接后直接渲染。
- **建议**：折叠展示、仅展示必要中间产物（如 SQL），或按 18.2.6 的设计不再展示。

---

### BUG-09【P3】antd 弃用 API 警告

- **复现**：浏览器控制台持续出现：
  - `[antd: Menu] children is deprecated. Please use items instead.`
  - `findDOMNode is deprecated...`
  - `[antd: Modal] destroyOnClose is deprecated. Please use destroyOnHidden instead.`
- **影响**：当前功能正常，但升级 antd 大版本会破坏；建议随版本升级修复。

---

### BUG-10【P3】MCP 添加失败错误信息不具体

- **复现**：添加 SSE 类型 MCP，URL host 不在 allowlist（后端返回 400 具体原因），前端仅 toast「添加失败」。
- **根因**：`McpPage.tsx` `add()` 的 catch 中 `message.error('添加失败')` 丢弃了错误详情。
- **建议**：展示 `error.message`。

---

### BUG-11【P3】Grafana query range 参数需要 `$__timeFilter` 宏（文档缺失）

- **复现**：`POST /grafana/query` 携带 `range` 与不含宏的 rawSql，返回全量数据（与 range 不符）。
- **结论（代码确认）**：`src/api/routes/grafana.py::_expand_macros` 仅替换 SQL 中的 `$__timeFilter(col)` 宏；未使用宏时 range 不会自动生效。**属预期行为**，但 API 文档未说明该约定，使用者容易误以为 range 自动过滤。
- **建议**：在接口文档/OpenAPI 描述中明确「时间过滤需在 rawSql 中使用 `$__timeFilter(时间列)` 宏」。

---

### BUG-12【P3】自动化「立即运行」后上次运行时间未刷新

- **复现**：自动化页点击「立即运行」（后端日志确认执行成功，88ms，row_count=1），列表「上次运行」仍为 `-`。
- **根因**（代码确认）：`src/automation/store.py::record_success()`（立即运行路径）只插入 `analysis_schedule_runs` 运行记录，**不更新 `analysis_schedules.last_run_at`**；`last_run_at` 仅在调度认领路径 `list_due()` 中更新（第 228 行）。因此手动「立即运行」成功后，任务列表的「上次运行」在下次定时调度前一直显示 `-`。
- **修复建议**：`record_success()`/`record_failure()` 中同步 `UPDATE analysis_schedules SET last_run_at=NOW() WHERE id=$1`。

---

### BUG-13【P1】知识库命中但注入被长期记忆挤占截断（知识实际不生效）

- **触发条件**：用户拥有较多长期记忆（SQL 模板/偏好，约 2000+ 字符）时，任何依赖知识库业务口径的 SQL 生成查询。
- **复现步骤**：
  1. 上传知识文档，内容定义业务口径：「统计 VIP 客户数量时，必须使用 customers 表的 `customer_level='VIP'` 过滤」；
  2. 提问「VIP 客户有多少？」；
  3. 后端日志显示知识库检索命中（`知识库语义检索命中 matched=5 top=3 total=926`，`knowledge_chars=849`）；
  4. 但生成的 SQL 为 `SELECT COUNT(*) FROM users WHERE vip_level > 0`——**完全未遵循知识口径**（应查 customers 表、用 customer_level='VIP'）。
- **根因**（代码确认）：
  1. `src/graph/nodes/retrieve_schema.py` 第 143-145 行：`merged_context = memory_text + knowledge_text`（**长期记忆在前，知识在后**），memory_chars≈2981 + knowledge_chars≈849 ≈ 3830 字符；
  2. `src/graph/nodes/generate_sql.py` 第 548-554 行：grounding PromptSection `max_chars=2500`，预算器按 2500 截断；
  3. 结果：长期记忆占满配额，**知识内容被截断丢弃**，模型看不到业务口径。
- **影响**：知识库功能形同虚设（命中但无效），业务口径、规则类知识无法约束 SQL 生成，可能产出错误口径的数据。
- **修复建议**：merged_context 中知识优先于记忆；或 grounding 拆分为「业务规则/知识」与「记忆」两个 section 分别限制；或提高 grounding 配额。

---

### BUG-15【P2】意图分类：简单统计查询被识别为 chat，不查库且回答误导

- **触发条件**：短统计类问题（实测「测试取消功能，统计订单总数」，13 字符；对照「统计订单总金额是多少？」11 字符被正确路由）。
- **复现步骤**：
  1. 选择 mysql 数据源，发送「统计订单总数」；
  2. 后端日志：`意图路由输出 reason=chat target=llm_direct_answer`；
  3. 回答：「无法直接统计订单总数，因为**当前未连接数据源**或调用数据库工具...」——未执行任何 SQL，且与事实不符（已选择 mysql 数据源）。
- **影响**：简单统计问题可能被静默降级为闲聊回答，用户拿到错误结论；回答内容还误导用户「未连接数据源」。
- **建议**：chat 意图判定增加规则兜底（含「统计/多少/总数/汇总」等词时强制 query）；llm_direct_answer 的 prompt 补充用户已选数据源信息，避免误导性回答。

---

### BUG-16【P2】每轮对话固定延迟 10~16 秒（长期记忆 touch 写放大）

- **触发条件**：任何一轮对话（prepare_turn 阶段）。
- **复现**：日志显示 `用户偏好查询完成`(21:28:37.599) → `长期记忆检索完成`(21:28:53.490)，**间隔约 16 秒**；期间命中 5 条长期记忆并逐个执行 `_touch`。
- **根因**（代码确认）：`src/memory/long_term_store.py::search` 对每条命中记忆调用 `_touch()`（第 100-101 行），而 `_touch()`（第 411-440 行）对每条记忆执行 **PG UPDATE + VectorStore upsert**（Milvus/向量写入，含 embedding 重算），5 条 × 每条约 3 秒 ≈ 15 秒。该开销发生在**每轮对话入口**，与 LLM 调用耗时叠加。
- **影响**：单轮问答基础延迟 +10~16 秒（与 BUG-06 的 LLM 耗时叠加后单轮可达 30~60 秒+）。
- **建议**：_touch 异步化（后台任务）或降频（如 1 天一次）；或向量 upsert 跳过（PG 已记录访问时间）。

---

### BUG-14【P2】知识库自进化 SQL 观察恒为 0（多语句校验失败）

- **触发条件**：任何成功执行的查询（尤其多数据源/LLM 生成多条 SQL 时）。
- **复现步骤**：
  1. 执行一次成功查询；
  2. 后端日志出现 `成功 SQL 长期记忆写入完成` 后紧跟 `知识库自进化安全校验拒绝 dialect=mysql`；
  3. `knowledge_sql_observations` 表恒为 0 行。
- **根因**（代码+实测确认）：
  1. `src/graph/nodes/build_response.py` 第 413 行把 `gen_sql`（多语句拼接文本，含 `-- datasource: xxx` 注释头）传给 `observe_successful_sql`；
  2. `src/memory/knowledge_evolution.py` 第 81-84 行 `validate_sql(sql, dialect)` 对多语句文本校验失败（实测：`Invalid expression / Unexpected token. Line 5`），直接拒绝，不写入观察。
- **影响**：知识进化（成功 SQL → 模板晋升）在多数据源场景下完全失效；单条 SQL 场景未验证（但生成链路常输出多条）。
- **修复建议**：observe 前按 `sql_statements` 逐条校验/观察；或对拼接 SQL 先按注释头拆分再校验。

---

## 三、功能测试通过项汇总（测试范围与结论）

### 3.1 认证与登录（P0）✅
- 租户编码+用户名+密码登录/退出、HttpOnly Cookie 会话 ✅
- 登录失败提示（"租户编码、用户名或密码错误"）✅
- 登录频率限制（20 次/小时/IP+账号组合，429 提示「登录尝试过于频繁」）✅（注：浏览器与 API 共用同一 IP 限流，自动化测试时易互相影响，建议测试期提高阈值或按账号独立计数）
- 账号停用后登录被拒（401「账号停用」）✅（测试中创建的用户 qa_user 被状态开关停用后无法登录，验证了停用逻辑生效；已通过管理界面重置密码的流程也验证通过）
- 超管跨租户直接修改用户被拒（403「需要当前租户管理员权限」）✅（租户边界生效）
- 未登录访问受保护页面跳转 /login ✅
- 未配置 LLM 租户调用 chat 返回 409「当前租户未配置可用的 LLM，请先在管理后台配置并启用模型」✅

### 3.2 对话分析页（P0）✅（含发现 BUG-01/06/08）
- 示例问题点击回填、发送、SSE 流式节点进度（prepare_turn→意图识别→检索表结构→生成SQL→SQL校验→layer4→执行查询→分析结果→supplemental_query）✅
- 8+ 节点进度图标、会话 ID 展示、取消按钮出现/消失 ✅
- SQL 生成正确（本月各产品销售额排名，MySQL 方言）✅
- 证据补查机制（max_supplemental_query_rounds=2 限制生效，未无限循环）✅
- 分析结果 JSON 完整（summary/insights/data_quality/limitations/related_metrics/business_impacts/possible_causes/recommended_actions/evidence_status/supplemental_questions/follow_up_questions）✅
- 模型选择（deepseek/DeepSeek V4 Flash）、推理开关、推理深度下拉 ✅
- 多数据源选择（Select multiple）✅
- 附件上传按钮（未做端到端文件分析验证，见 3.8 assets API ✅）

### 3.3 数据源管理页（P1）✅
- 列表（名称/方言/版本/主机/端口/数据库/用户/描述）✅
- 添加（含错误密码也可入库——**设计如此**：POST /datasources 不强制连接测试）✅
- 测试连接（成功/失败 toast）✅
- 编辑：空密码沿用原凭证 ✅；连接测试失败时保存被阻断（400「连接测试失败，原配置未变更」）—— 错误提示文案为原始 JSON（BUG-04）
- 删除（Popconfirm 确认、toast「已删除」）✅
- 分页 ✅

### 3.4 表结构页（P1）✅（含 BUG-02）
- 表列表（10 表/2 页）、搜索框、刷新 Schema ✅
- 展开行字段列表（字段/类型/注释/可空/主键/索引）✅
- 表详情抽屉（描述/字段列表/编辑注释按钮）✅
- 列注释编辑 API（BUG-02）⚠️

### 3.5 查询历史页（P1）✅（含 BUG-07）
- 历史列表（时间/数据源/查询/SQL/行数/状态）、分页（4 页）、搜索过滤 ✅
- 数据源筛选下拉 ✅
- 回放功能缺失（BUG-07）❌

### 3.6 Skills 页（P1）✅
- 普通用户（tenant_admin）：列表为空（system 作用域仅超管可见，**设计如此**；建议空态加引导文案）
- 超管：34 个内置 Skill 列表、分页、启用/禁用切换（持久化确认）、范围徽标、触发词、工具数 ✅
- 详情弹窗（元数据 + SKILL.md 原文）✅
- 内置 Skill 删除按钮 disabled（title="内置不可删除"）✅
- 批量导入/单文件导入/Skill Registry 按钮存在（未做文件上传端到端验证）

### 3.7 知识库页（P1）✅（含 BUG-03/05）
- 条目列表（577 条）、范围筛选（系统/租户/个人）、类别筛选、搜索、分页（29 页）✅
- 已索引文档列表（3 份文档，仅用户上传可删）✅
- 文档上传（txt 走通，返回 task_id、轮询状态 done）—— 但短文本 0 分块（BUG-03）⚠️
- 文档/条目删除（权限控制：系统条目不可删）✅
- 超管视图多「标签治理」tab ✅

### 3.8 附件/文件分析（P1）✅
- `POST /assets/profile`：CSV 列画像（dtype/null/unique/sample/候选主键/预览）✅
- `POST /assets/query`：DuckDB 只读查询、聚合正确 ✅；写操作拦截（`DELETE FROM data` → 400「结构化资产只允许只读 SELECT」）✅
- 单文件表名固定为 `data`（调用方需知晓此约定）✅
- **聊天附件全链路**：`POST /chat/files` 上传（file_id 返回）→ `POST /chat` 携带 file_ids → `file_analysis` 子图正确解析 CSV（2 行 2 列）并按产品汇总（X=10/Y=20），回答带数据来源说明 ✅

### 3.9 自动化页（P2）✅
- 创建任务（Modal 表单：类型/名称/数据源/频率/SQL/阈值/通知渠道，默认值正确）✅
- 立即运行（后端 88ms 执行成功，row_count=1）✅
- **定时调度（cron 路径）**：将任务 next_run_at 改为过去后，调度器下一轮自动认领（SKIP LOCKED，claimed_count=1）并执行（kind=report，46ms，notified=True）✅
- 删除任务时运行记录/通知级联清理（`analysis_schedule_runs`/`analysis_notifications` ON DELETE CASCADE，确认表结构）✅
- 站内通知 tab、刷新按钮 ✅
- BUG-12（立即运行后上次运行列未刷新）⚠️

### 3.10 MCP 页（P2）✅（含 BUG-10）
- 添加弹窗（名称/范围/传输/命令/URL）✅
- SSE host allowlist 校验（400「MCP SSE host '127.0.0.1' 不在 allowlist」）✅
- 失败提示笼统（BUG-10）⚠️

### 3.11 管理工作区（P0）✅
- tenant_admin：用户列表/创建用户（qa_user 创建成功）/角色修改（PATCH 生效）/状态开关/重置密码 ✅
- tenant_admin：LLM 命名连接（deepseek 连接、API Key 已配置、默认连接/默认模型选择、新增弹窗）✅
- super_admin：租户管理（4 租户列表、展开行管理员信息、重置密码、创建租户弹窗、删除租户按钮）✅
- super_admin：LLM 厂商与模型（Anthropic/DeepSeek/OpenAI 厂商、模型目录、能力字段展示）✅
- super_admin：安全配置（运行环境/多租户/公开注册/登录锁定/频率/缓存/内置 MCP）✅
- 用户登录限流（429）✅

### 3.12 其他 API（P2/P3）✅
- `GET /sessions`（cursor 分页）、`GET /sessions/{id}`、`GET /sessions/{id}/turns`（逐轮 final_result 恢复，多数据源 SQL/图表/分析完整）✅
- `POST /optimization/advice`（SELECT_STAR/NO_LIMIT 诊断、候选索引、证据质量说明）✅
- `GET /health` ✅
- `POST /auth/logout` ✅

---

### 3.13 隐藏功能专项（记忆系统 / 知识库命中与生效 / 缓存 / 审计）✅（含 BUG-13/14）

- **多轮对话上下文**：第二轮追问「那平均每单金额是多少？」正确引用第一轮「订单总金额」上下文，SQL 正确（SUM/COUNT），日志确认 history_turns=1 传递 ✅
- **会话状态持久化**：LangGraph Checkpointer（PostgresSaver）正常，`checkpoints` 1515 行、`sessions` 42 行 ✅
- **长期记忆（SQL 模板）**：成功 SQL 自动写入 `long_term_memories`（12 行），每轮 prepare_turn 加载（memory_count=5）并注入 SQL 生成 prompt ✅
- **用户偏好记忆**：问「记住我的偏好：图表用柱状图」→ 意图路由 `save_preference` → 写入 `preference=chart_type`（PG 持久化）→ 后续请求自动加载（preference_count=1）✅
- **知识库语义检索**：`_load_knowledge_context` 向量检索命中（matched=5/top=3，926 条目）✅；**但注入被长期记忆挤占截断（BUG-13）** ❌
- **知识进化（SQL 观察）**：`knowledge_sql_observations` 恒为 0，多语句 SQL 校验失败被拒（BUG-14） ❌
- **数据源/Schema 缓存**：`解析数据源命中缓存`、枚举字典加载、schema 快照缓存 ✅
- **查询审计**：每次查询写 `query_audit_log`（80 行，含 tenant/user/sql_hash/row_count/duration/success）✅
- **会话归档**：`sessions_archive` 96 行，长会话压缩（≥50 轮触发 compact_turn_history）✅
- **PG 初始化缓存**：history_store/session_store 等组件 PG 就绪状态缓存 ✅
- **用户偏好展示**：llm_direct_answer 路径把偏好原始文本作为「知识库参考」展示在回答下方（`用户偏好: chart_type: line`），展示原始、不友好（轻微 UX 问题，未单列 bug）

---

## 四、非 Bug 类改进建议

1. **登录限流体验**：限流按「IP + 租户 + 用户名」组合计数，浏览器与 API 共用；开发/测试环境建议配置调高 `login_max_per_hour`，或对 127.0.0.1 放行。
2. **Skills 空态引导**：非超管用户无任何可见 Skill 时，页面显示「共 0 个」无说明，建议空态文案说明 system 仅管理员可见。
3. **数据源添加允许失败连接入库**：添加接口不校验连接（编辑强制校验），两者行为不一致。建议添加时也做连接探测（或明确标注「连接异常」状态），避免用户误以为配置正确（本测试中错误密码数据源添加成功后编辑永远失败）。
4. **补查链路耗时**：见 BUG-06，建议为 analyze_result LLM 调用设置超时与降级（processor 直出）路径。
5. **antd 升级**：见 BUG-09，建议随 antd v5→v6 升级时统一清理弃用 API。
6. **附件表名约定**：`/assets/query` 单文件固定表名 `data`，建议在 API 文档中明确，或返回 tables 映射供前端提示用户。

---

## 五、测试覆盖缺口（本次未覆盖）

- 历史会话抽屉 UI 打开（bsk 自动化对底部 Tooltip 按钮点击受限，代码审查正常，建议人工复核）
- 知识库 PDF/Word 上传（TXT/MD 已测；PDF/Word 解析依赖文件样本）
- Skills 批量导入（文件夹上传，webkitdirectory）
- MCP stdio 类型真实命令连接（仅验证了 SSE allowlist 拦截与表单校验）
- 推理开关开启时的 reasoning 行为、推理深度参数端到端验证
- 移动端/窄屏响应式布局（767px 断点）
- 会话归档压缩的 UI 侧验证（≥50 轮触发 compact_turn_history，代码确认）
- 多数据源并行查询（multi_source_dispatch 子图，历史数据中有成功记录但本次未端到端复测）

---

*报告生成：2026-08-23，测试人：AI 自动化测试代理*
