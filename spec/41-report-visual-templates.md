# 报表视觉模板体系设计（配色 · 字体 · 密度）

> 状态：设计待评审
> 关联：spec/30-report-workbench.md（报表工作台）、spec/34-report-phase2-implementation.md（页面版本与发布）
> 关联功能点：features/23-report-workbench.md（23.15 报表视觉模板与生成期选择）、features/18-frontend.md（18.13 报表模板渲染与图表基线）
> 后续独立设计（不在本文范围）：报表智能发散分析（主动扩单元 / 深挖已有数据 / 跨单元关联与影响判定 / 业务基准对照）

---

## 1. 背景与问题

### 1.1 诉求

报表已能按自然语言生成，但存在两个表现层问题：

1. **只有一套视觉，且这套视觉没有设计体系**。用户要求"设计得高级一点"，并"提供多种风格模板供用户选择"。
2. **观感差的根因不是配色，是缺层次**。所有页面块都是同一个带硬边框的 antd `Card`；KPI 是裸 `Statistic`；section 间距固定；图表是硬编码 `bar` 且没有统一配色。

本设计只解决表现层与"生成时/查看时选择模板"，**不改变分析内容**。分析内容的主动扩展另出一份设计。

### 1.2 三个可直接复用的既有事实（决定架构走向）

| # | 事实 | 位置 | 对本设计的意义 |
|---|---|---|---|
| 1 | `ReportDocument.theme` **已经在发但前端完全没读** | 后端默认值 `src/reporting/document.py:132`；前端类型 `frontend/src/types/report.ts:183`；`ReportDocumentRenderer.tsx` 中 grep `theme` 零命中 | 模板标识的载体已存在，**不需要新增文档字段** |
| 2 | 后端 ECharts option 白名单根键只有 `{xAxis, yAxis, series, tooltip, legend, dataZoom}`，**不含 `color` 与 `grid`** | `src/reporting/document.py:21`（`_ECHARTS_ROOT_KEYS`） | 后端不可能下发配色；同一份报表内图表配色分裂的根因在此。前端注入 `color` 是**唯一可行且安全**的统一手段 |
| 3 | 生成页与分享页共用同一个渲染器 | `frontend/src/pages/ReportViewerPage.tsx` 与 `SharedReportPage.tsx` 都渲染 `ReportDocumentRenderer` | "查看时可切"只需在一处实现，两个页面同时生效 |

### 1.3 现状约束（必须遵守）

- **前端没有 JS 测试框架**：`frontend/package.json` 的 devDependencies 只有 `@types/react`、`@types/react-dom`、`@vitejs/plugin-react`、`typescript`、`vite`，无 vitest/jest。项目既有的前端验证方式是 `tests/test_frontend/` 下的 **Python 源码静态契约测试**（读 `.tsx` 文本断言关键符号），另有 `pytest.mark.browser` 的真实浏览器测试（`tests/test_frontend/test_report_browser.py` 为范例，由 `RUN_BROWSER_TESTS=1` 显式启用）。
- **样式无体系**：`frontend/src/index.css` 仅 82 行，报表相关的 13 个类全部硬编码色值（`#1677ff`、`#f5f5f5`、`#f0f0f0`、`#fafafa` 等），无 `:root` 变量、无暗色模式。
- **i18n 是"中文原文即键"**：`frontend/src/i18n/index.ts:20` 的 `t(source)` 以中文原文为键，`en.ts` 提供映射，缺失时回退原文。模板名称**不需要新增 key 体系**，直接写中文并补 `en.ts` 条目即可。
- **全局主题不能被污染**：`frontend/src/App.tsx:257` 的 `<ConfigProvider theme={{ algorithm: theme.defaultAlgorithm }}>` 是应用级配置，模板作用域必须严格限制在报表容器内。

---

## 2. 目标与非目标

### 2.1 目标

1. 建立一套设计 token 体系，消灭报表样式的硬编码，同一份 token 同时驱动 CSS、antd 组件与 ECharts 图表。
2. 内置 4 套模板，覆盖专业分析、深色看板、长文阅读、企业简报四类场景。
3. **生成时可选**：模板在创建报表任务时选定，存于 Job，发布时落进页面文档，**不影响计划哈希**。
4. **查看时可切**：阅读端与分享页都能临时切换模板，不改变报表版本。
5. 新增第 5 套模板的成本降到"改一个前端文件、写约 10 行 token"。

### 2.2 非目标

见第 15 节。

---

## 3. 总体架构

三层，每层职责单一：

```text
后端 ── 只存一个格式合法的字符串，不做任何视觉决策
  ReportJobCreateRequest.template_id
        ↓
  report_jobs.template_id   ←── 独立列，不进 ReportPlan，不进 canonical_hash()
        ↓
  document.theme.template_id

前端 ── 模板 = 设计 token 包，前端是视觉唯一真源
  frontend/src/report/templates.ts        （注册表：4 套 token）
        ↓
  frontend/src/components/report/TemplateScope.tsx
        ├─ CSS 变量注入（--report-*，作用域限定）
        ├─ 嵌套 antd ConfigProvider（token + algorithm）
        └─ 图表基线（withChartBaseline）
        ↓
  frontend/src/components/report/ReportDocumentRenderer.tsx
        （读 document.theme.template_id；本地 state 可覆盖）
```

**关键取舍 1：模板不进 `ReportPlan`，不进 `canonical_hash()`。**

`ReportPlan` 的每个字段都被 runner 用来决定"查什么、查几次、扣多少配额"，`canonical_hash()`（`models.py:160-163`）的职责是防篡改**执行范围**。`template_id` 一个都不影响，属于纯表现设置，因此它的载体是 **Job** 而不是 **Plan**。

直接收益：改模板不影响 `plan_hash`，**不触发重新预览、不触发重新确认**。若把它放进 plan，改一次配色就要重新跑一次 `report.plan` 的 LLM 调用，纯属浪费。

**关键取舍 2：后端不持有模板清单，只做 `^[a-z0-9_-]{1,32}$` 格式校验。**

若后端也维护一份 id 白名单，就会与前端注册表形成双写、必然脱钩。模板 id 只影响视觉、不影响授权与数据范围，未知 id 由前端静默回退默认模板即可，无安全后果。

**关键取舍 3：不存在"LLM 覆写模板"这个威胁。**

`ReportPlanUnitsOutput`（`src/llm/output_contracts.py:161-164`）**只有 `analysis_units` 一个字段**，且 `StrictOutputModel` 是 `extra="ignore"`（同文件 `:11-14`）。模型在结构上没有地方输出 `template_id`，因此不需要任何"强制回滚"防御逻辑。把字段放进 plan 会制造一个虚假威胁，再为它写无用的防御代码与测试。

---

## 4. 后端契约

模板的载体是 **Job**，不是 Plan。后端改动 5 个文件 + 1 个迁移。

### 4.1 迁移 `migrations/027_report_job_template.sql`（新增）

```sql
ALTER TABLE report_jobs ADD COLUMN IF NOT EXISTS template_id VARCHAR(32);
```

- **可空、无 DB 默认值**。回退到默认模板的逻辑放在应用层（runner），避免 DB 默认值把历史任务钉死，将来若要调整默认模板不必改数据。
- 应用层约束：非空时必须匹配 `^[a-z0-9_-]{1,32}$`。
- `report_jobs` 现有 DDL 见 `migrations/018_report_workbench.sql:4-25`，已有 `plan_json JSONB` 与 `plan_hash`；本设计**不把模板合并进 `plan_json`**——理由见第 3 节关键取舍 1。
- SQLite 测试走 store 的应用层模拟（与既有做法一致）。

### 4.2 `src/reporting/models.py`

**`ReportPlan` 不加任何字段**，`canonical_hash()` 不动。

```python
class ReportJobCreateRequest(BaseModel):
    ...
    template_id: str | None = Field(default=None, max_length=32, pattern=r"^[a-z0-9_-]{1,32}$")
```

- 该模型是 `models.py:187-194`。
- 用 `pattern` 做格式约束，**非法格式由 FastAPI 直接转 422**，不静默降级。这是内部 API，调用方 bug 要吵出来。
- `ReportPlanRequest`（`models.py:166-184`）**不加** `template_id`：预览阶段不需要知道模板，模板在创建任务时才确定。

`ReportJob` 响应模型同步加 `template_id: str | None = None`，供 `GET /jobs/{id}` 回显。

### 4.3 `src/reporting/store.py`

- `create_job_with_quota()` 落库时写入 `template_id`。
- 读取 Job 的地方带回该列（含调度恢复扫描路径 `scheduler.py`，保证定时任务每次发布都沿用创建时选定的模板）。
- 字符校验放在应用层：非空且不匹配 `^[a-z0-9_-]{1,32}$` 时抛 `ValueError`（防御性双保险，主校验在 4.2 的 `pattern`）。

### 4.4 `src/reporting/runner.py`

- `run_job()` 读取 `job.template_id`，`None` 时取应用默认值 `"insight"`。
- 传入 `build_report_document()`，写入 `document.theme`。
- **不写 `warnings`**：模板为 `None` 是正常路径，不是降级。

### 4.5 `src/reporting/document.py`

- `build_report_document()` 新增 `template_id: str = "insight"` 参数并写入 `theme`：

```python
theme = {"template_id": template_id}
```

- `ReportDocument.theme` 保持 `dict[str, str]`（`document.py:132`），仅调整默认值，不新增字段。
- **后端不写 `density`**：密度是模板的内部属性，由前端注册表定义。后端若也写一份，就又变成"后端持有模板知识"，与第 3 节的取舍矛盾。旧记录里遗留的 `density` 键前端直接忽略。
- **向后兼容**：旧版本记录的 `document_json` 里 `theme` 没有 `template_id` 键。Pydantic 的 `default_factory` 只在构造新对象时生效，反序列化旧 JSON 不会补该键，因此**前端必须容忍 `theme.template_id` 缺失**（见第 10 节）。不对旧记录做数据回填。

### 4.6 `src/api/routes/report_workbench.py`

- `POST /report-workbench/jobs`（`:234`）把 `request.template_id` 传给 `store.create_job_with_quota()`。
- `POST /report-workbench/plans`（`:210`）**不变**。
- 其余端点不变。`GET /reports/{id}`、`GET /shared/reports/{token}` 返回的 `document.theme` 已含模板标识，无需额外字段。

---

## 5. 前端模板注册表

新增 `frontend/src/report/templates.ts`。

### 5.1 类型定义

```ts
export interface ReportTemplateTokens {
  accent: string;            // 主色：KPI 数值、强调元素、图表首色
  accentSoft: string;        // 主色浅底：标签、高亮行
  bg: string;                // 页面底色
  surface: string;           // 卡片 / 区段底色
  border: string;            // 分隔线与边框
  textPrimary: string;
  textSecondary: string;
  fontFamily: string;        // 正文与控件字体族
  headingFontFamily: string; // 标题字体族（纸感模板与正文不同）
  radius: string;            // 圆角，如 '10px'
  shadow: string;            // 卡片阴影；'none' 表示无阴影
  spaceUnit: string;         // 基础间距刻度，如 '16px'
  density: 'comfortable' | 'compact'; // 控件密度：决定 antd 组件 size 与图表高度
  chartPalette: string[];    // 图表分类色板
}

export interface ReportTemplate {
  id: string;
  name: string;                        // 中文原文，直接进 t()
  description: string;                 // 中文原文，直接进 t()
  scheme: 'light' | 'dark';            // 决定 antd algorithm
  tokens: Partial<ReportTemplateTokens>; // 只写差异，其余继承默认模板
}
```

### 5.2 默认模板（`insight`）的完整 token

```ts
const insightTokens: ReportTemplateTokens = {
  accent: '#2563eb',
  accentSoft: '#eff6ff',
  bg: '#f7f8fa',
  surface: '#ffffff',
  border: '#e8eaed',
  textPrimary: '#1f2329',
  textSecondary: '#646a73',
  fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif",
  headingFontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif",
  radius: '10px',
  shadow: '0 1px 2px rgba(15,23,42,0.04), 0 4px 12px rgba(15,23,42,0.06)',
  spaceUnit: '16px',
  density: 'comfortable',
  chartPalette: ['#2563eb', '#14b8a6', '#f59e0b', '#8b5cf6', '#ef4444', '#0ea5e9', '#84cc16', '#ec4899'],
};
```

### 5.3 四套模板与差异 token

| id | 名称 | scheme | 定位 | 差异 token（未列出的继承 `insight`） |
|---|---|---|---|---|
| `insight` | 洞察 | light | 专业分析风（默认） | 基准，无差异 |
| `ink` | 墨玉 | dark | 深色看板 / 投屏 | `accent: '#38bdf8'`、`accentSoft: '#0c4a6e'`、`bg: '#0b1220'`、`surface: '#111c2e'`、`border: '#1e293b'`、`textPrimary: '#e2e8f0'`、`textSecondary: '#94a3b8'`、`shadow: '0 1px 2px rgba(0,0,0,0.4), 0 8px 24px rgba(0,0,0,0.5)'`、`chartPalette: ['#38bdf8', '#34d399', '#fbbf24', '#a78bfa', '#f87171', '#22d3ee', '#a3e635', '#f472b6']` |
| `paper` | 纸感 | light | 长文阅读风 | `accent: '#9a3412'`、`accentSoft: '#fef6ee'`、`bg: '#faf8f3'`、`surface: '#fffdf9'`、`border: '#e7e0d3'`、`textPrimary: '#2b2620'`、`textSecondary: '#6b6357'`、`headingFontFamily: "Georgia, 'Songti SC', 'SimSun', serif"`、`radius: '4px'`、`shadow: 'none'`、`spaceUnit: '20px'`、`chartPalette: ['#9a3412', '#0f766e', '#b45309', '#4d7c0f', '#7c2d12', '#0369a1', '#a16207', '#be185d']` |
| `business` | 商务 | light | 企业简报风 | `accent: '#1e3a5f'`、`accentSoft: '#eef2f7'`、`bg: '#f4f5f7'`、`surface: '#ffffff'`、`border: '#dfe3e8'`、`textPrimary: '#1a1f27'`、`textSecondary: '#5c6672'`、`radius: '2px'`、`shadow: '0 1px 2px rgba(16,24,40,0.06)'`、`spaceUnit: '14px'`、`density: 'compact'`、`chartPalette: ['#1e3a5f', '#5c7a99', '#8aa4bf', '#c4d2e0', '#2f6f4e', '#7a9e7e', '#a8763e', '#c9a97a']` |

展示顺序即上表顺序，`insight` 恒为第一项与默认项。

### 5.4 解析函数

```ts
export interface ResolvedTemplate {
  id: string;                  // 实际生效的模板 id（回退后）
  fallback: boolean;           // 是否发生了回退，供开发期调试与测试断言使用，不用于界面提示
  scheme: 'light' | 'dark';
  tokens: ReportTemplateTokens; // 已补全，全部键必定存在
}

// 方法作用：把模板 id 解析为完整模板，未知 id 静默回退默认模板。
// Args: id - 页面文档或用户选择中的模板标识，可能为 undefined 或未知值。
// Returns: token 已补全的模板对象，绝不抛错。
export function resolveTemplate(id: string | null | undefined): ResolvedTemplate
```

实现要点：

1. 按 `id` 在注册表查找；未命中则取 `insight`。
2. token 合并：`{...insightTokens, ...found.tokens}`，保证**任何模板都不可能缺键**。
3. 注册表本身校验：`id` 唯一、`accent` 必填（模块加载期一次静态断言，重复或缺失直接抛错——这是开发期错误，必须吵）。

---

## 6. `TemplateScope` 组件

新增 `frontend/src/components/report/TemplateScope.tsx`。

### 6.1 职责

接收模板 id，内部解析后把 token 变成实际生效的样式。**三个注入面**：

```tsx
<div className="report-theme" style={cssVariables}>
  <ConfigProvider theme={antdTheme}>{children}</ConfigProvider>
</div>
```

| 注入面 | 实现 | 约束 |
|---|---|---|
| CSS 变量 | 由 token 映射生成 `--report-accent`、`--report-accent-soft`、`--report-bg`、`--report-surface`、`--report-border`、`--report-text-primary`、`--report-text-secondary`、`--report-font`、`--report-heading-font`、`--report-radius`、`--report-shadow`、`--report-space-unit` | 全部加 `--report-` 前缀，挂在 `.report-theme` 容器上，**不写入 `:root`**，不污染应用其余部分 |
| antd token | 嵌套 `<ConfigProvider theme={{ algorithm, token: {...}, components: {...} }}>` | `scheme === 'dark'` 用 `theme.darkAlgorithm`，否则 `theme.defaultAlgorithm`。antd5 官方支持嵌套 ConfigProvider，只影响子树；`App.tsx:257` 的全局配置一行不动 |
| 图表基线 | 由渲染器调用，见第 8 节 | 不在 `TemplateScope` 内实现，避免组件承担两种职责 |

### 6.2 Props

```ts
interface TemplateScopeProps {
  templateId: string | null | undefined;  // 未解析的原始 id
  children: ReactNode;
}
```

组件内部自行调用 `resolveTemplate`，调用方不需要预先解析，避免解析逻辑散落多处。

### 6.3 antd token 的具体映射

| antd 字段 | 取值 |
|---|---|
| `token.colorPrimary` | `accent` |
| `token.colorBgContainer` | `surface` |
| `token.colorBgLayout` | `bg` |
| `token.colorText` | `textPrimary` |
| `token.colorTextSecondary` | `textSecondary` |
| `token.colorBorder` / `token.colorBorderSecondary` | `border` |
| `token.borderRadius` | 解析 `radius` 为数值（`'10px'` → `10`） |
| `token.fontFamily` | `fontFamily` |
| `token.fontSize` | `density === 'compact' ? 13 : 14` |
| `components.Card.bodyPaddingLG` / `headerFontSize` / `headerBg` | 由 `density` 与 `surface` 推导 |
| `components.Table.cellPaddingBlock` / `headerBg` / `borderColor` | 由 `density` 与 `accentSoft` 推导 |
| `components.Statistic.contentFontSize` | 由 `density` 推导 |

只映射上表列出的字段。**不整体替换 antd 主题**，避免模板承担它没有设计过的组件（`Modal`、`Drawer`、`Form` 等）的样式责任。

---

## 7. 渲染层重塑

### 7.1 `index.css` 去硬编码

`frontend/src/index.css` 中**所有报表相关规则**（`.report-document`、`.report-document-header`、`.report-document-limitations`、`.report-section`、`.report-section-heading`、`.report-section-grid`、`.report-block`、`.report-block-{type}`、`.report-relationship-content`、`.report-share-url`、`.report-viewer-toolbar`、`.report-workbench-*`）改为消费 CSS 变量：

```css
.report-document {
  background: var(--report-bg);
  font-family: var(--report-font);
  color: var(--report-text-primary);
}
.report-section { margin-bottom: calc(var(--report-space-unit) * 2); }
.report-block .ant-card {
  background: var(--report-surface);
  border-color: var(--report-border);
  border-radius: var(--report-radius);
  box-shadow: var(--report-shadow);
}
```

**验收标准**：`index.css` 中报表类下不再出现 `#1677ff`、`#f5f5f5`、`#f0f0f0`、`#fafafa`、`#666` 等字面色值。

### 7.2 观感改进项（"不丑了"的实处）

| 项 | 现状 | 改为 |
|---|---|---|
| 页面块卡片 | antd `Card size="small" bordered`，硬边框 + 拥挤内边距，所有块长得一样 | 去硬边框，改用 token 驱动的柔和阴影 + 更大内边距；`size` 由 `density` 决定 |
| 区段层次 | 仅一个 `Title level={3}`，无视觉锚点 | 标题左侧加 accent 短竖条（或底部分隔线），建立层级 |
| 区段间距 | 固定 `30px`（`index.css:26`） | `calc(var(--report-space-unit) * 2)`，随模板节奏变化 |
| KPI 块 | 裸 `<Statistic>`，与正文无区分 | 网格化卡片：数值走 accent 大字号，单位弱化，块内边距收紧 |
| 块类型差异 | `report-block-{type}` 类名已存在但**没有任何对应样式** | `chart` 给更大内边距；`narrative` 行宽限到约 `72ch` 提升可读性；`kpi` 收紧内边距；`table` 跟随 `density` 调整行高 |
| 图表高度 | 硬编码 `320`（`ReportDocumentRenderer.tsx:99`） | 由 `density` 推导的 token 值 |

`row_span` 仍然不使用（`ReportDocumentRenderer.tsx:56-63` 只读 `col_span`），本设计不改布局算法。

---

## 8. 图表基线统一

新增 `frontend/src/components/report/chartBase.ts`。

```ts
// 方法作用：为后端下发的 ECharts option 补齐模板样式基线，不覆盖后端显式设置。
// Args: option - 后端校验过的 echarts_option，可能为空；tokens - 当前模板 token。
// Returns: 合并后的 option；入参为空或非对象时返回 undefined。
export function withChartBaseline(
  option: Record<string, unknown> | null | undefined,
  tokens: ReportTemplateTokens,
): EChartsOption | undefined
```

### 8.1 合并策略（原则：不覆盖后端意图）

| 字段 | 策略 | 理由 |
|---|---|---|
| `color` | **直接注入** `tokens.chartPalette`（后端设了也覆盖） | 后端白名单 `_ECHARTS_ROOT_KEYS`（`document.py:21`）**不允许**根键 `color`，后端永远设不了；这是配色不统一的根因 |
| `backgroundColor` | 仅在缺失时注入 `tokens.surface` | 后端可能有意留白 |
| `textStyle.fontFamily` | 仅在缺失时注入 `tokens.fontFamily` | 同上 |
| `grid` | **逐键仅补齐缺失** | 后端可能已设 `containLabel` 等 |
| `tooltip`、`legend` | **逐键仅补齐缺失**（`textStyle`、`borderColor`、`backgroundColor` 等样式键） | 后端已设的值代表明确意图 |
| `xAxis`、`yAxis` | 对单对象或数组逐项**逐键仅补齐缺失**（轴线色、标签色、分割线色） | 同上 |
| 其他键 | 原样保留 | — |

"逐键仅补齐缺失"指浅合并：目标对象中已存在的键保持不变，只补目标缺失且基线提供的键。数组元素按索引对齐合并，长度以目标为准。

**深色模板的例外（`scheme === 'dark'`）**：`tooltip`、`legend`、`xAxis`、`yAxis` 的**颜色类字段强制覆盖**，不再"仅补齐缺失"。

原因是这些字段由后端在**浅色假设下**生成（`generate_chart.py` 写死了浅色的文字色与边框色），深色底上会白底穿帮。强制覆盖清单：

| 字段 | 覆盖为 |
|---|---|
| `tooltip.backgroundColor` / `.borderColor` | `tokens.surface` / `tokens.border` |
| `tooltip.textStyle.color` | `tokens.textPrimary` |
| `legend.textStyle.color` | `tokens.textSecondary` |
| `xAxis` / `yAxis` 的 `axisLabel.color`、`axisLine.lineStyle.color`、`splitLine.lineStyle.color` | 依次为 `tokens.textSecondary` / `tokens.border` / `tokens.border` |

浅色模板（`scheme === 'light'`）**不适用**该例外，仍严格走"仅补齐缺失"。这条差异必须在代码里按 `scheme` 分支实现，不能统一处理。

### 8.2 调用点

`ReportDocumentRenderer.tsx:97` 改为：

```tsx
option={withChartBaseline(block.echarts_option, tokens)}
```

样式对象与高度改为 token 驱动，替换 `:99` 的 `style={{ height: 320, width: '100%' }}`。

**安全说明**：前端注入的全部是静态样式常量，不含函数、字符串表达式或外部资源引用，不会破坏后端 `validate_echarts_option`（`document.py:62-72`）所建立的 DSL 安全边界。注入发生在渲染期，结果不回写服务端、不被持久化。

### 8.3 含负值纯柱图的自动数值轴居中（2026-09-19 用户授权追加）

**问题（实机取证）**：分享页「未达成门店缺口金额」与「毛利告警商品分布」各 24 个全负柱值，
`yAxis.inverse=false`，零线 y=38 落在 grid 上沿（grid 上下 38/260），柱子从顶部往下垂。
根因是**自动范围在全负数据上退化成 `[min, 0]`**，不是符号写错，也不是轴反转。

**规则**：`withChartBaseline` 在完成样式合并后追加一次**渲染期**判定（`withCenteredZeroAxis`），
命中全部条件时把该数值轴改写为以 0 为对称中心：

| 条件 | 判定 |
|---|---|
| 系列 | `series` 非空且每项都是 `type === 'bar'`（复用 `isAllBarSeries`，判定依据是**实际生效的 series**） |
| 轴形状 | x/y 必须是**唯一 `value` + 唯一 `category`** 这一种组合；多元素轴数组、两条数值轴、`log` / `time` / 未声明 `type` 与数值轴搭配、另一条轴缺失，一律不动 |
| 系列绑轴 | 每个系列的 `xAxisIndex` / `yAxisIndex` 未声明或显式为 `0`；出现非 0 索引时不动 |
| 范围来源 | 该轴 `min` / `max` / `interval` **均未显式声明**（`null` 视为未声明） |
| 轴方向与类型 | `inverse !== true` 且非 `log` |
| 数据 | 取值能逐个解析为有限数值，且**至少一个负值** |

> **轴形状只数「实际数值轴个数」，不数「可改写候选数」**（2026-09-19 审查订正）：
> 早期实现把「筛掉显式范围后剩下的候选」当唯一性依据，于是
> `xAxis: { type: 'value', min: 0 }` + `yAxis: { type: 'value' }` 会只剩一个候选，
> 唯一那条被改的轴未必是 series 真正使用的轴。现在按轴类型计数，只有
> 「一条 value + 一条 category」才认为能可靠解读。

命中后写入 `min = -bound`、`max = bound`、`interval = bound / 2`，其中
`bound = niceCeil(max(|min|, |max|))`，`niceCeil` 向上取到 `1 / 2 / 2.5 / 5 × 10^k` 的整值
（2.5 档用于避免 2400 这类取值被粗放扩成 5000、柱子只占半格）。
`interval = bound / 2` 保证刻度固定落在 `-bound / -bound÷2 / 0 / bound÷2 / bound`。

**0 参考线**：对称轴只保证「刻度里有一个 0」，零线与其它网格线没有区别。命中时给**首系列**
补一条 `markLine`：锚在数值轴的 0 位（`{ yAxis: 0 }` 或水平柱图下的 `{ xAxis: 0 }`），
`silent: true`、`symbol: 'none'`、`label.show: false`，线色取模板 `textSecondary`
（比 `splitLine` 的 `border` 清晰、又不到正文色那么重），因此浅色 / 深色模板各自有合适的零线。
首系列若**已自带 `markLine`**（后端意图），原样保留，不用零线顶掉它。

**取值解析与堆叠**：`series[i].data[j]` 只接受有限数值与 `{value: number}` 对象项；
`null` / `undefined` / 非有限数视为缺值（不落点、不参与累计）；字符串、`[x, y]` 数对等
**判不出的结构一律整体放弃**。带同名 `stack` 的系列按 ECharts 默认 `stackStrategy: 'samesign'`
口径取界——**正值与负值分别逐类目累计，相反符号不抵消**，非堆叠系列按单点取值计入。
`stackStrategy` 声明为其它取值（`all` / `positive` / `negative`）时判不出真实边界，整体放弃。

> **堆叠口径订正（2026-09-19，主代理浏览器审计）**：早期实现把同 stack 的正负值做**代数和**，
> 在 `[-900] / [-900] / [900]` 这类形状上得到 -900，而 ECharts 6.1 的真实
> `stackResultDimension` 是 `[-900, -1800, 900]`（负向边界 -1800），第二根负柱因此被轴截断
> （取证：`logs/zero-axis-audit-20260919/stacked-bar-audit.json`，`extent [-1000,1000]`、
> `clipped: true`）。现在按 samesign 分别累计，取界覆盖真实堆叠高度。

**明确不动的边界**：全正 / 全零 / 空数据 / 无有效数值、非柱图与混合系列（bar+line 等）、
显式范围（含 `min: 'dataMin'` 这类表达式）、反向轴、对数轴、`stackStrategy` 非默认值，
以及上面「轴形状」「系列绑轴」两行列出的全部形状，一律原样保留。
`dataset` 与 `encode` 场景下 `series.data` 不是数组，已被取值解析守卫拦下，无需额外分支。

**实现约束**：只发生在渲染期，旧快照分享页刷新即生效；**不改**数据、不取绝对值、不反转轴，
不修改入参 option 及其嵌套 `axis` / `series` / `data`（居中后的轴对象是新建的）；
不改后端 API / SQL / manifest / 持久化文档 / LLM prompt；不引入新依赖。

---

## 9. 查看时切换

### 9.1 Hook

新增 `frontend/src/hooks/useReportTemplate.ts`：

```ts
// 方法作用：解析页面文档记录的模板作为初始值，并允许本次浏览临时覆盖。
// Args: document - 当前报表页面文档。
// Returns: [当前模板 id, 切换函数]；初始值取 document.theme.template_id，缺失时回退默认模板。
export function useReportTemplate(document: ReportDocument): [string, (id: string) => void]
```

- **纯本地 state，不写回服务端**。切模板不改报表版本、不触发重新查询、不改 `document`。
- 刷新后回到 `document.theme.template_id` 记录的模板。
- 没有"记住上次选择"的持久化——模板是生成期决策，阅读期的临时切换不该跨越报表。

### 9.2 切换控件

`ReportViewerPage.tsx` 与 `SharedReportPage.tsx` 各加一个 antd `Segmented`，选项来自注册表（4 项），每项前带一个由 `accent` / `bg` / `surface` 三色构成的色卡圆点。

- 私有页放在现有 toolbar 区域（`.report-viewer-toolbar`）。
- 分享页放在页面顶部，`SharedReportPage.tsx` 现有布局内。
- 切换逻辑只在 hook 一处，两个页面只是渲染控件。

---

## 10. 错误处理与降级

| 场景 | 行为 |
|---|---|
| `theme.template_id` 缺失（旧版本记录） | 使用 `insight`，**无任何提示** |
| `theme.template_id` 是未知值 | 使用 `insight`，**无任何提示**（分享页面对外部用户，不暴露内部 id） |
| 模板 `tokens` 缺键 | `{...insightTokens, ...template.tokens}` 继承补齐；注册表加载期静态断言保证 `insight` 自身完整 |
| `POST /jobs` 传非法格式 `template_id` | 后端 422 拒绝，**吵出来**（内部 API，静默降级会掩盖调用方 bug） |
| `POST /jobs` 不传 `template_id` | 落库为 `NULL`；runner 发布时取应用默认值 `insight`，**不写 `warnings`**（正常路径，非降级） |
| 历史 Job 的 `template_id` 为 `NULL`（迁移前创建） | 同上，取 `insight` |
| 模板 id 影响 `plan_hash` | **不会**。`ReportPlan` 不含该字段，`canonical_hash()` 未改动 |
| `block.echarts_option` 为 `null` | `withChartBaseline` 返回 `undefined`，走既有 `Empty` 分支（`ReportDocumentRenderer.tsx:105`） |
| `block.echarts_option` 非对象 | 同上，返回 `undefined` |
| `document.theme` 整体缺失 | 视为 `template_id` 缺失，用 `insight` |

原则：**用户可见处静默回退，开发者可见处（内部 API）明确报错**。

---

## 11. 目录结构

```text
frontend/src/
├── report/
│   └── templates.ts                                     [新增] 模板注册表 + resolveTemplate
├── components/report/
│   ├── TemplateScope.tsx                                [新增] token → CSS 变量 + antd token
│   ├── chartBase.ts                                     [新增] withChartBaseline
│   └── ReportDocumentRenderer.tsx                       [修改] 包 TemplateScope、消费 theme、图表基线
├── hooks/
│   └── useReportTemplate.ts                             [新增] 解析 + 本地覆盖
├── pages/
│   ├── ReportViewerPage.tsx                             [修改] 加模板切换控件
│   ├── ReportWorkbenchPage.tsx                          [修改] 生成面板加模板选择器
│   └── SharedReportPage.tsx                             [修改] 加模板切换控件
├── i18n/en.ts                                           [修改] 补模板名称与描述的英文条目
└── index.css                                            [修改] 报表类改用 CSS 变量

src/reporting/
├── models.py                                            [修改] ReportJobCreateRequest/ReportJob 加 template_id
├── store.py                                             [修改] Job 落库与读取带 template_id（含调度恢复路径）
├── runner.py                                            [修改] 读 job.template_id（NULL 回退 insight）传给 document
├── document.py                                          [修改] build_report_document 写入 theme
└── ../api/routes/report_workbench.py                    [修改] POST /jobs 透传

migrations/
└── 027_report_job_template.sql                          [新增] ALTER TABLE report_jobs ADD COLUMN template_id
```

生成面板落点：`ReportWorkbenchPage.tsx` 在数据源选择器旁新增模板选择器（带色卡预览），选中值作为客户端 state 保存，**在点击"创建任务"时随 `POST /jobs` 提交**。

交互要点：
- **预览计划（`POST /plans`）不携带模板**，因此切换模板**不触发重新预览**、不消耗 LLM 调用。
- 已创建任务的模板由 `GET /jobs/{id}` 回显（Job 响应模型的 `template_id`）。
- 定时任务的每次运行沿用创建时选定的模板（由 4.3 的调度恢复路径保证）。

---

## 12. 安全与边界

| 边界 | 处理 |
|---|---|
| 全局主题污染 | CSS 变量只挂 `.report-theme` 容器；antd 用嵌套 `ConfigProvider`；`App.tsx:257` 全局配置不改 |
| XSS | 模板只控制样式，不引入 HTML 注入；渲染器继续不使用 `dangerouslySetInnerHTML`（现有约束，`tests/test_frontend/test_report_workbench.py` 已断言） |
| ECharts DSL 边界 | 前端注入的只有静态样式常量，不含函数与外部引用；注入不回写、不持久化 |
| 计划防篡改 | `ReportPlan` 与 `canonical_hash()` **完全不动**，模板不参与执行范围的防篡改校验，既有安全语义零变更 |
| 模型越权 | 不适用。`ReportPlanUnitsOutput` 无 `template_id` 字段且 `StrictOutputModel` 为 `extra="ignore"`，模型结构上无法输出模板选择 |
| 越权选择他人模板 | 模板 id 不含租户/用户信息，且只影响视觉，无越权面 |
| 公开分享面 | 分享页对未知 id 静默回退，不泄露内部模板命名 |
| 数据范围 | 模板与数据源授权、脱敏、配额完全无关，不引入新的越权面 |

---

## 13. 测试

### 13.1 后端 `tests/test_reporting/test_report_template.py`（新增）

类 docstring 注明覆盖的功能编号。

| 测试函数 | 场景 |
|---|---|
| `test_job_template_defaults_to_null` | 创建任务不传 `template_id` → 落库为 `NULL`，不写 `warnings` |
| `test_runner_falls_back_to_insight_for_null_template` | Job 的 `template_id` 为 `NULL` → 发布版本 `theme["template_id"] == "insight"` |
| `test_plan_hash_unaffected_by_template_id` | 同一计划两次创建任务、模板分别为 `insight` 与 `ink` → `plan_hash` **相同**，两次都通过防篡改校验 |
| `test_job_rejects_malformed_template_id` | 边界值表格：空串 / 33 字符超长 / 大写字母 / 含空格 / 含 `/` 均 422；32 字符合法值通过 |
| `test_document_theme_carries_template_id` | `build_report_document()` 产出的 `theme["template_id"]` 等于入参 |
| `test_document_accepts_legacy_theme_without_template_id` | `theme` 仅有 `accent`/`density` 的旧 JSON 反序列化成功，不抛错 |
| `test_scheduled_run_inherits_job_template` | 定时任务的第二次运行发布的版本仍带创建时选定的模板 |
| `test_report_job_endpoint_persists_template_id` | `POST /report-workbench/jobs` 携带 `template_id` → 落库，且 `GET /jobs/{id}` 回显该值 |
| `test_plan_request_has_no_template_field` | `ReportPlanRequest` 不含 `template_id`（防止后续有人"顺手加回去"） |

### 13.2 前端契约 `tests/test_frontend/test_report_templates.py`（新增）

沿用既有 Python 静态断言模式（`Path(...).read_text()` + 符号断言）。

| 测试函数 | 断言 |
|---|---|
| `test_template_registry_has_four_unique_templates` | `templates.ts` 含 4 个 id 且互不相同；含 `insight` |
| `test_resolve_template_falls_back_for_unknown_id` | `resolveTemplate` 返回 `fallback: true` 且 `id` 为默认模板；实现含注册表未命中分支 |
| `test_renderer_consumes_document_theme` | `ReportDocumentRenderer.tsx` 引用 `theme` 与 `TemplateScope`；不再出现硬编码高度 `320` |
| `test_css_uses_report_variables` | `index.css` 报表类使用 `var(--report-`。**断言必须按选择器分块切片后再检查**（提取以 `.report-` 开头的规则块），因为 `#1677ff` 在同文件的 `.node-progress.running:3` 与 `.thinking-block:6` 仍在合法使用，全局 grep 会产生误报 |
| `test_chart_baseline_injects_palette_and_preserves_backend_keys` | `chartBase.ts` 含 `chartPalette` 注入与"仅补齐缺失"逻辑 |
| `test_dark_scheme_forces_color_override` | `chartBase.ts` 按 `scheme === 'dark'` 分支强制覆盖 `tooltip`/`legend`/轴的颜色类字段（第 8.1 节的例外规则），浅色分支不覆盖 |
| `test_viewer_and_shared_page_have_template_switch` | `ReportViewerPage.tsx` 与 `SharedReportPage.tsx` 均引用 `useReportTemplate` |
| `test_workbench_passes_template_id` | `ReportWorkbenchPage.tsx` 与 `api/reportWorkbench.ts` 传递 `template_id` |
| `test_template_scope_does_not_touch_root_variables` | `TemplateScope.tsx` 不出现 `:root`、不出现 `document.documentElement` |

### 13.3 浏览器视觉验证 `tests/test_frontend/test_report_theme_browser.py`（新增）

`pytest.mark.browser`，`RUN_BROWSER_TESTS=1` 显式启用，复用 `test_report_browser.py` 的离线前端启动 fixture（不访问业务后端）。

| 测试函数 | 验证 |
|---|---|
| `test_switching_template_changes_computed_background` | 打开分享页 → 读 `.report-document` 的 `getComputedStyle().backgroundColor` → 切到另一套模板 → 断言颜色**确实变化** |
| `test_dark_template_applies_dark_surface` | 切到 `ink` → 断言 `.report-block .ant-card` 的背景色亮度低于阈值 |

**这两个用例是本设计唯一能真正验证"换皮肤生效"的手段**——纯源码静态断言测不出 CSS 变量是否连通、嵌套 `ConfigProvider` 是否生效。

### 13.4 手工验收

`RUN_BROWSER_TESTS` 之外，需人工确认：4 套模板在 1440px / 1100px / 767px 三个断点下的观感；深色模板下 antd `Table`、`Alert`、`List`、`Empty` 无"白底穿帮"；`paper` 模板的衬线标题在中文下的渲染效果。

---

## 14. 实施顺序

| 阶段 | 内容 | 依赖 |
|---|---|---|
| A | 迁移 `027` + `models.py` / `store.py` / `runner.py` / `document.py` / API + `tests/test_reporting/test_report_template.py` | 无 |
| B | `templates.ts` 注册表 + `TemplateScope` + `index.css` 去硬编码 | A 可并行（仅需 `theme.template_id` 契约） |
| C | `chartBase.ts` + 渲染器接入 + 生成面板/查看页/分享页控件 | B |
| D | `en.ts` 英文条目 + 前端契约测试 | C |
| E | 浏览器视觉验证 + 三断点手工验收 | D |

阶段 A 与 B 可并行推进。

---

## 15. 明确不做（YAGNI）

| 不做 | 原因 |
|---|---|
| PDF / 打印样式 | 当前项目**完全没有导出功能**（`package.json` 无 `html2canvas`/`jsPDF`/`file-saver`；`ReportViewerPage.tsx:146` 创建分享时硬编码 `allow_download: false`）。这等于并排一个新模块，需单独排期 |
| 版式级模板（仪表盘型 / 杂志型 / 简报型） | 本设计只做配色 / 字体 / 密度层。版式模板需后端 `layout_for_block` 与前端 grid 都按模板分支，工作量大一档，收益待验证 |
| 用户自定义 token 编辑器 | 无产品诉求，且引入 token 持久化与校验成本 |
| 后端模板清单 API | 与"前端是视觉唯一真源"的取舍冲突，会造成双写 |
| 修改 `App.tsx` 全局主题 | 模板作用域严格限制在报表容器 |
| 消费 `document.navigation` / `document.sources` 幽灵字段 | 这是内容呈现问题，与视觉模板正交，留给后续 |
| 图表类型推断（解除硬编码 `bar`） | 属"分析内容"范畴，纳入后续的发散分析设计 |
| 旧记录 `theme` 数据回填 | 前端已能容忍缺失；回填是无收益的迁移风险 |

---

## 16. 遗留与后续

1. **报表智能发散分析**（已与用户确认的四项能力）：主动扩单元、深挖已有数据（同环比 / 趋势 / 异常 / 贡献度 / 分布）、跨单元关联与影响判定、业务基准对照。**已出设计文档：`spec/42-report-divergent-analysis.md`**。
2. `document.navigation`、`document.sources`、`permissions.download` 三个字段已下发但前端未消费，可作为独立小批次处理。
3. 既有规格落差（本设计不处理，已在 `spec/42` 第 18 节处理）：`spec/30:217` 的 `report_analyze_units` 节点在代码中不存在；`spec/30:215` 的 `report_profile_results` 实际只统计行数；`spec/30:264` 声称报表路径不进入补查回环，但 `runner.py:207-219` 未注入 `suppress_supplemental_query` 标志，回环实际可达。`spec/30:264` 已由 `spec/42` 第 18.1 节同步修订。
