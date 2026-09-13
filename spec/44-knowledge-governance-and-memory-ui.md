# 知识库治理、上传交互与长期记忆管理设计

本设计收敛知识库页面的 5 项用户反馈，并新增「我的长期记忆」管理页。不引入新依赖，不改变 Agent 检索链路的知识召回口径。

## 1. 实测依据

2026-09-13 对生产库（Milvus `data_agent_knowledge` + PostgreSQL `data_agent`）只读实测：

| 范围 | 来源 | 有源文档 | 租户 | 条数 |
|------|------|----------|------|------|
| tenant | `auto_introspect` | 否 | 3 | 5662 |
| tenant | `user_upload` | 是 | 3 | 632 |
| tenant | `auto_introspect` | 否 | 1 | 127 |
| private | 来源缺失（`resource_kind=long_term_memory`） | 否 | 3 | 36 |
| private | `user_upload` | 是 | 1 | 20 |

`visibility` 分布中 `system` 为 0 条（平台知识尚未索引）。`long_term_memories` 表 36 条（SQL 模板 32 / 用户偏好 2 / 学习模式 2）。

单次 `GET /api/v1/knowledge` 耗时分解（日志实测 3.9s）：

| 阶段 | 耗时 |
|------|------|
| Milvus 三组全量扫描（`limit=10000` + `metadata like '%…%'` 中缀匹配） | 0.78s |
| Python 逐条构造 entries（6330 条） | **3.21s** |
| 其中 `can_manage_knowledge_resource()` 逐条调用（内部 8 次 structlog 调用） | 2.42s |
| 其中 ContextVar 读取 ×3 | 0.62s |

## 2. 知识条目治理

### 2.1 管理页可见范围

管理 API 的可见性**不复用检索链路的过滤器**。检索链路（`src/knowledge/retrieval.py` 的 `build_accessible_knowledge_filters`，被 `search_knowledge` 调用）中「super_admin 只召回 system」是刻意设计——`super_admin` 是平台操作身份而非租户业务身份，该行为不得改动。

新增管理视角过滤器 `build_admin_visible_filters(category: str = "")`，仅供 `list_knowledge` 与 `delete_knowledge_doc` 使用。列表与文档接口新增查询参数 `include_system`（默认 `false`）：

| 角色 | 默认可见范围 | `include_system=true` 时 |
|------|--------------|--------------------------|
| `super_admin`（`is_platform_super_admin()` 为真） | `system` + 本租户 `tenant` + 本人 `private` | 同左（默认已含 system） |
| `tenant_admin` / `analyst` / `viewer` | 本租户 `tenant` + 本人 `private` | 追加 `system`，**只读** |

**系统知识默认隐藏、可主动勾选查看，但绝不允许非超管编辑或修改。**

- `include_system` 只放宽**可见性**，不放宽**可管理性**：非超管所见 `system` 条目的 `can_delete` 恒为 `false`，且删除接口由 `can_manage_knowledge_resource` 的「非超管 + system → False」分支硬拒绝，不依赖前端置灰。
- 展示风险可控的依据：`system` 范围按既有约束不得存放租户业务口径、租户 Schema、凭证或个人数据（见 `spec/23` §3.7.1），其内容为平台通用方言资料、分析方法、数据质量规则与产品文档。
- 本条是**对 `spec/08` §7.3.10「`system` 仅超级管理员可通过管理 API 查看」的口径修订**：由「仅超管可见」改为「默认隐藏、可只读查看、仅超管可管理」。修订动因是普通用户排查知识召回问题时需要确认系统知识的存在与内容，需同步更新 `spec/08`。
- 文档侧同步放宽：`list_knowledge_docs`、`get_doc_content`、`get_doc_raw` 在 `include_system=true` 时允许非超管读取 `system` 文档内容；删除路径维持既有超管独占校验，不放宽。

「编辑与修改」的当前落点：知识库尚无条目编辑能力，可管理性实际体现为**删除**。`system` 的删除权由 `can_manage_knowledge_resource` 判定——`super_admin` 为真，其余角色恒为假。该判定同时意味着 **`super_admin` 只能管理 `system` 条目，不可删除租户业务知识**，这是「超管是平台操作身份而非租户业务身份」边界的既有设计，本设计不改变。

前端控件：「知识条目」与「已索引文档」两个页签的筛选栏各增加复选框「显示系统知识」，**默认不勾选**。该状态仅存于组件内存，不写入 `localStorage` 或用户偏好，刷新页面即回到默认隐藏。勾选后 system 条目沿用既有的金色「系统知识」范围标签作视觉区分。

### 2.2 来源分层

`GET /api/v1/knowledge` 新增查询参数 `entry_kind`：

| 取值 | 语义 |
|------|------|
| `document`（默认） | 排除 `source=auto_introspect`，即上传切块、系统目录、手工文档等有来源的知识 |
| `introspect` | 仅 `source=auto_introspect`（Schema 自动内省条目） |
| `all` | 全部 |

**`resource_kind=long_term_memory` 的条目在所有取值下一律排除**。长期记忆属于记忆模块，混入知识库列表属于数据污染，其查看与管理迁移至第 5 节的独立页面。

内省条目在 UI 上标注「随表结构自动维护」，删除按钮禁用并说明原因——不再出现「灰按钮无解释」。

`entry_kind` 与 `include_system` 是两个**正交**维度：前者按**来源**切分，后者按**归属范围**切分。勾选「显示系统知识」后，`system` 范围内的内省条目仍受 `entry_kind` 约束。

### 2.3 创建人

`list_knowledge` 与 `list_knowledge_docs` 响应新增 `owner_username` 字段。

实现约束：**只对当前页记录**收集 `owner_user_id`，一次 `SELECT id, username FROM users WHERE id = ANY($1)` 映射，禁止逐条查询。`owner_user_id = 0` 或无匹配时返回空字符串，前端显示「系统」。

**数据模型注意（实现期实测发现）**：`tenant` 范围的条目在 `upload_manager.py` 写入时把上传者放在 `uploaded_by_user_id` 键下，而 `private` 与 `system` 范围用 `owner_user_id`。该键在修复前**只被写入、从未被读取**（全项目 Grep 仅命中写入点），这正是「知识条目没有创建人信息」的根因。管理 API 解析创建人时必须以 `owner_user_id` 优先、缺失或为 0 时回退到 `uploaded_by_user_id`（实现为 `_resolve_owner_user_id`）。该回退不影响删除权判定：`tenant` 范围的授权走 `is_tenant_admin` 分支，不比较属主。

### 2.4 删除权限收口

修复一处实际缺陷：列表接口与删除接口对缺失 `visibility` 的条目采用不同默认值（列表为 `"system" if not is_user else "tenant"`，删除为 `"private"`），会导致「按钮可点、点击后 403」。

统一为失败关闭语义：`visibility = str(meta.get("visibility") or "private")`，列表的 `can_delete` 与删除接口的授权判定使用同一表达式。

内省条目仍不可通过 API 删除（`source != "user_upload"` 拒绝），该行为符合设计，仅需在 UI 上给出明确说明。

## 3. 上传表单交互

### 3.1 切块与重叠

`frontend/src/pages/KnowledgePage.tsx` 上传配置 Modal：

| 字段 | 控件 | 范围 | 步长 | 默认值 |
|------|------|------|------|--------|
| 块大小 `chunkSize` | `Slider` + `InputNumber` 双向联动 | 200–4000 | 100 | 800 |
| 重叠 `chunkOverlap` | `Slider` + `InputNumber` 双向联动 | 0–500 | 10 | 100 |

保留手动输入能力（`InputNumber` 与 `Slider` 共享同一状态，双向同步）。约束 `chunkOverlap < chunkSize`：当 `chunkSize` 下调时自动收窄 `chunkOverlap` 的上限与当前值。

### 3.2 数据源

上传 Modal 的「数据源」由自由文本 `Input` 改为 `Select`（`showSearch` 单选、`allowClear`），候选来自既有 `fetchDatasourceCatalog()`（自动翻页，不漏第二页以后的数据源）。

**保持可选（非必填）**：现有上传接口签名为 `datasource: str = ""`，无数据源绑定是合法用法；改为必填会破坏既有调用方与已上传文档的语义。

全仓库排查结论：仅此一处使用文本输入数据源，其余页面（对话、历史、自动化、指标、表结构、报表工作台、数据源管理）均已使用下拉选择。**新增涉及数据源的表单一律使用下拉单选或多选，禁止文本输入数据源名称**（数据源管理页新建数据源本身输入的是名称，属定义而非选择，不在此列）。

### 3.3 标签多选控件

`Select mode="multiple"` 的逐行下拉改为**点击弹出面板 + 标签网格**：

- 面板顶部保留搜索框（远程搜索，防抖 250ms）
- 标签以方角圆角标签框平铺排列（`flex-wrap`），点击切换选中
- 选中态：背景色变浅（`#e6f4ff`）、边框转蓝、右下角显示勾选图标
- 面板底部保留「新建个人标签」入口
- 已选标签仍以 Tag 形式回显在输入框内
- 键盘可达：标签块支持 Tab 聚焦与 Enter 切换

选择范围规则不变：`private` 范围可选全局标签与个人标签，非 `private` 范围仅可选全局标签；切换范围时剔除不合法已选项。

## 4. 查询性能

### 4.1 根因

`list_knowledge` 对结果集中的**每一条**记录调用 `can_manage_knowledge_resource()`。该函数内部串行执行 `normalize_knowledge_scope` + `is_super_admin` + `is_tenant_admin` + 自身共 4 层函数、8 次 structlog 调用。6330 条 × 8 ≈ 5 万次日志调用，占 3.9s 中的 2.42s。

### 4.2 L1 修循环热点

1. **授权判定按组合缓存**：以 `(visibility, resource_tenant_id, owner_user_id)` 为键，在一次请求内缓存判定结果。同一租户的条目组合数极少（个位数），授权调用次数从 6330 降到个位数。**`src/knowledge/governance.py` 不改动**，日志规范保持完整。
2. **ContextVar 提取到循环外**：`get_current_role()` / `get_current_tenant_id()` / `get_current_user_id()` 在循环前读取一次存入局部变量。
3. **`can_delete` 只算当前页**：搜索过滤与总数统计不依赖 `can_delete`，因此先分页、再对当前页记录补齐该字段。

预期：3.9s → 0.8s 以内（剩余为 Milvus 扫描成本）。

### 4.3 L2 服务端缓存

在 `src/api/routes/knowledge.py` 引入进程内 TTL 缓存：

- **缓存键**：`(role, tenant_id, user_id, category, knowledge_scope, entry_kind, include_system)`，**不含 `search`**——若把关键词并入键，每个搜索词都会缓存一份全量列表，内存与命中率双输；关键词过滤改为在缓存值上于内存完成（实测 6330 条全量过滤仅 0.01s）
- **`include_system` 必须进键**（实现期由测试发现并修正）：否则勾选查看与默认隐藏共用同一份缓存，用户取消勾选后仍会从缓存返回系统知识，导致平台知识在默认视图下泄漏
- **缓存值**：该身份可见的**完整条目列表**（不含关键词过滤，不含分页切片）
- **TTL**：30 秒
- **主动失效**：上传任务完成、删除条目、删除文档、Schema 内省刷新后，按 `tenant_id` 清除该租户全部缓存键
- **多 Worker 说明**：各 Worker 独立缓存，写操作只失效本进程；其他 Worker 依赖 TTL 过期，因此 TTL 不得放宽超过 30 秒

分页与关键词搜索在缓存命中时于内存完成，预期 < 100ms。

### 4.4 暂不实施项

Milvus 集合重建（将 `visibility` / `tenant_id` / `owner_user_id` / `source` 提升为标量字段并建倒排索引）可消除全表扫描，但服务端为 Milvus v2.4.23，**不支持在线加字段**，需要停机窗口重建集合并回填 6477 条。本轮不实施，待数据量或并发进一步增长后单独立项。

## 5. 我的长期记忆

### 5.1 权威数据源

长期记忆以 PostgreSQL `long_term_memories` 表为权威（`migrations/008_memory_runtime.sql`），已有索引 `ix_long_term_memories_visible (visibility, tenant_id, owner_user_id, memory_type)`。列表、筛选、搜索、分页**全部走 SQL，不访问向量库**。

删除必须保持双后端一致：PostgreSQL 删除后调用既有 `_delete_vector_or_queue()` 清理向量库条目，失败时写入 `pending_vector_sync` 补偿队列，由既有 `reconcile_pending_sync()` 周期恢复。

### 5.2 API 契约

新增 `src/api/routes/memory.py`，前缀 `/api/v1`：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/memory` | GET | 分页列表，支持 `memory_type`、`search`、`page`、`page_size` |
| `/memory/{memory_id}` | GET | 单条详情，含 `payload` |
| `/memory/{memory_id}` | DELETE | 删除本人记忆 |

`GET /memory` 响应示例：

```json
{
  "items": [
    {
      "id": "6f560e11-cef0-4c1e-9d5a-1a2b3c4d5e6f",
      "memory_type": "sql_template",
      "content": "问题: 统计各月销售额\n数据源: unknown\nSQL: WITH order_monthly AS (...) ",
      "payload": {"question": "统计各月销售额", "sql": "WITH order_monthly AS (...)", "datasource": "unknown"},
      "scope": "private:3:3",
      "access_count": 4,
      "confidence": 0.5,
      "ttl_days": 180,
      "created_at": "2026-09-10T17:51:24+00:00",
      "last_accessed_at": "2026-09-12T09:03:11+00:00",
      "expires_at": "2027-03-09T17:51:24+00:00"
    }
  ],
  "total": 36,
  "page": 1,
  "page_size": 20
}
```

**归属强制**：查询条件恒含 `tenant_id = 当前租户 AND owner_user_id = 当前用户 AND visibility = 'private'`，请求参数不得覆盖。

**删除的越权响应**：访问或删除非本人记忆一律返回 `404 {"detail": "记忆不存在"}`，不使用 403，避免探测他人记忆的存在性。

`LongTermMemoryStore` 新增两个方法：

- `list_memories(memory_type: str = "", search: str = "", page: int = 1, page_size: int = 20) -> tuple[list[LongTermMemory], int]`
- `delete_memory(memory_id: str) -> bool`（内部校验归属，双后端删除）

### 5.3 前端页面

新建 `frontend/src/pages/MemoryPage.tsx`，路由 `/memory`，菜单项「我的记忆」置于「知识库」之后，图标 `BulbOutlined`。

**可见角色**：所有已登录用户（含 `viewer`）。记忆是个人数据，不涉及资源共享权限。

| 区域 | 内容 |
|------|------|
| 筛选栏 | 类型下拉（全部 / SQL 模板 / 用户偏好 / 学习模式 / 纠正记录 / 项目规则）+ 搜索框 |
| 列表列 | 类型 Tag、内容摘要（截断 80 字）、访问次数、置信度、创建时间、最后访问时间、操作 |
| 详情 Modal | 完整 `content`、SQL 代码高亮（复用既有 highlight.js）、`payload` 字段表 |
| 操作 | 删除（Popconfirm 二次确认），不支持在线编辑 |
| 空态 | 「暂无长期记忆。成功执行的 SQL 和你的偏好会自动沉淀到这里。」 |

**不支持在线编辑**是刻意约束：手工修改记忆内容会直接污染 Agent 后续检索上下文，且 `confidence` / `ttl_days` 由维护任务统一调度，人工改写会破坏衰减与清理语义。需要修正时删除后由正常使用重新沉淀。

## 6. 兼容性与回退

| 变更 | 兼容性 |
|------|--------|
| `entry_kind` 默认 `document` | 既有调用方不传该参数时，内省条目从列表中消失，属预期行为变更；需在 `features/` 状态中标注 |
| `owner_username` 新增字段 | 纯新增，前端旧版本忽略该字段不影响功能 |
| 数据源改为下拉 | 接口签名与语义不变，仅控件变更 |
| 查询缓存 | 缓存仅影响管理列表读路径，检索链路（`search_knowledge`）不经过缓存 |
| 长期记忆新页面 | 纯新增路由与端点，不影响既有链路 |

任何缓存或新端点的异常必须降级为直接查询或明确错误，不得返回空列表伪装成功。

## 7. 测试要求

| 测试文件 | 覆盖内容 |
|----------|----------|
| `tests/test_knowledge/test_admin_visibility.py` | 四种角色在管理列表中的默认可见范围；`include_system=true` 时非超管可见 system 条目但 `can_delete` 恒为 false；非超管直接调用删除 system 条目接口返回 403；超管默认可见 system + 本租户 + 本人数据 |
| `tests/test_knowledge/test_doc_include_system.py` | `include_system` 对文档列表与文档内容读取的放宽；非超管读取 system 文档内容成功，删除 system 文档仍被拒绝 |
| `tests/test_knowledge/test_entry_kind.py` | `document` / `introspect` / `all` 三种取值的过滤结果；`resource_kind=long_term_memory` 在所有取值下均被排除 |
| `tests/test_knowledge/test_knowledge_delete_permission.py` | 列表 `can_delete` 与删除接口判定一致；缺失 `visibility` 的条目两侧行为一致；内省条目删除返回 403 |
| `tests/test_knowledge/test_owner_username.py` | 创建人映射只查当页；`owner_user_id=0` 返回空字符串；用户不存在时不报错 |
| `tests/test_knowledge/test_list_cache.py` | 缓存命中不重复查询；写操作按租户失效；TTL 过期后回源 |
| `tests/test_knowledge/test_list_performance.py` | 构造 ≥6000 条记录，断言授权判定调用次数为个位数（不随条目数线性增长） |
| `tests/test_memory/test_memory_management_api.py` | 列表只返回本人 private；分页与类型筛选正确；删除本人记忆成功且同步向量库；删除他人记忆返回 404 |
| `tests/test_frontend/test_knowledge_upload_form.py` | 滑块与数字输入双向联动；`chunkOverlap` 上限随 `chunkSize` 收窄；数据源下拉候选来源；标签面板选中态 |
| `tests/test_frontend/test_memory_page.py` | 列表渲染、类型筛选、删除交互、空态文案 |

性能测试使用固定数据集，不依赖真实租户数据。缓存相关测试注入可控时间源，禁止依赖真实 30 秒等待。

## 8. 验收标准

1. 租户管理员的知识条目列表中，每一条都能看到创建人，且「文档知识」视图里的条目均有可解释的来源（源文件或明确的内省标注）。
2. `system` 范围条目默认不展示；非超管勾选「显示系统知识」后可只读查看，但删除入口不可用、且直接调用删除接口返回 403；超管默认即可见 `system` + 本租户 + 本人数据。
3. 租户管理员删除本租户的上传条目成功；内省条目按钮禁用且有明确说明；不再出现「按钮可点但返回 403」。
4. 上传文档时数据源为下拉选择，切块与重叠既可用滑块调整也可手动输入，标签通过弹出面板以标签块多选。
5. 知识条目列表首次加载 < 1s，翻页与搜索 < 100ms（6000 条量级，单机部署）。
6. 「我的长期记忆」页面可查看、筛选、删除本人记忆；删除后向量库同步移除；知识库列表中不再出现长期记忆条目。

## 9. 文档与功能清单更新

| 文件 | 更新内容 |
|------|----------|
| `spec/08-security.md` §7.3.10 | **修订** `system` 可见性口径：由「仅超级管理员可通过管理 API 查看」改为「默认隐藏、可只读查看、仅超管可管理」；补充管理 API 的可见范围与来源分层 |
| `spec/23-...-roadmap.md` §3.7.1 | 澄清「读取范围」描述的是检索链路，管理 API 可见性以本设计为准 |
| `features/06-knowledge.md` | 新增功能点：管理页可见范围、来源分层、创建人、删除权限一致性、列表缓存 |
| `features/07-memory.md` | 新增 7.6 长期记忆管理（列表 API、删除 API、双后端一致性） |
| `features/18-frontend.md` | 新增「我的记忆」页；更新 18.10.x 上传表单交互、「显示系统知识」复选框与来源分层筛选 |
| `spec/README.md` | 本文件加入索引表 |
| `frontend/src/i18n/en.ts` | 新增「我的记忆」「标记为系统维护」等新增界面文案的英文词条 |

文档只落盘，不提交。
