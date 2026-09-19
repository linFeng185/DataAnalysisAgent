# 报表呈现体系改版设计（hero 横幅 · 图文分栏 · 全图表类型）

> 状态：设计待评审
> 关联：spec/30-report-workbench.md（报表页面 DSL 与布局器）、spec/41-report-visual-templates.md（视觉模板体系）、spec/42-report-divergent-analysis.md（发散分析）
> 关联功能点：features/23-report-workbench.md、features/18-frontend.md、features/14-visualization.md
> 本文只做**呈现层与图表类型覆盖**，不改分析内容

---

## 1. 背景与问题

### 1.1 诉求

spec/41 落地后，报表已有 4 套视觉模板与 token 体系，但观感仍不达预期。用户提出三条：

1. **样式要再「高级」一档**，并明确选定「首屏横幅 + 高对比」方向（下称**方向 C**）。
2. **需要预留分析的文本空间，而不是单纯的图表**——图表与分析结论必须成对呈现。
3. **图表类型太少**，要求「各种类型的图表都要有，对话可以生成的图表都要兼容」。

### 1.2 根因（均为代码实测，非推断）

| # | 现象 | 位置 | 性质 |
|---|---|---|---|
| 1 | **卡片墙**：每个块（KPI / 图表 / 表格 / 文字）都套同一个 `<Card size="small" bordered>`，长得一模一样，无层级 | `ReportDocumentRenderer.tsx:210-215` | 违反 `spec/30:338`「前端禁止用嵌套卡片堆叠页面；区段为无框布局」的既有约束 |
| 2 | **图表类型写死为 `bar`**，且标题硬编码为「趋势与分布」（柱状图并没有趋势） | `document.py:331-336` | 功能缺口 |
| 3 | **两套并行的图表实现**：对话路径有完整的类型推断与构建（`_classify_chart_type` / `_build_option`），报表路径完全不用 | `chart_generator.py:82,109` vs `document.py:331` | 重复实现，必然分叉 |
| 4 | **白名单挡住热力图**：`_ECHARTS_ROOT_KEYS` 不含 `visualMap`，而热力图必需 `visualMap` | `document.py:26` | 功能缺口 + 安全边界需要论证 |
| 5 | **`ink` 模板图表配色实测 FAIL**：8 个分类色**全部**超出深色亮度带（实测 L 0.709–0.849，带内为 0.48–0.67）——深色底挂一排粉彩 | `templates.ts:98` | **缺陷**，非审美偏好 |
| 6 | 数字字形无区分：表格列、坐标轴、hero 大数字全部用同一字形 | `index.css` / 渲染器 | 观感 |

> 说明：`insight` / `paper` / `business` 三套的 `chartPalette` 经校验**全部通过**（含色盲分离与对比度），属「能用但不惊艳」，是审美问题，**不在本文范围**。

### 1.3 可直接复用的既有事实（决定架构走向）

| # | 事实 | 位置 | 对本设计的意义 |
|---|---|---|---|
| 1 | **一个 section = 一个分析单元**：`build_report_document` 对每个 `UnitResult` 追加一个 `ReportSection`，段内至多一个 `kpi`、一个 `chart`、一个 `narrative`、一个 `table` | `document.py:305,352,363,393,410` | **图文配对不需要新增 DSL 字段**，section 本身即配对边界 |
| 2 | 对话路径的图表推断与构建**已存在且已验收**（含 `resolve_chart_type_instruction` 的自然语言指令解析） | `chart_generator.py:32,82,109`；调用方 `src/api/routes/visualization.py:9` | 报表路径应**复用**而非重写；改公开函数即可 |
| 3 | `build_report_document` **只有一个调用点** | `src/graph/subgraphs/report_workbench.py:339` | 后端改动影响面可控 |
| 4 | 前端已有「按亮度判定深色」的既有模式（`isDarkSurface`，带单测） | `chartBase.ts:19-28` | hero 横幅的文字色可复用同一模式，无需新增成对的明暗 token |

---

## 2. 目标与非目标

### 2.1 目标

1. 报表正文换成**方向 C** 视觉：hero 横幅 + 无框区段 + 图文分栏；**去掉卡片墙**。
2. 报表图表类型从「写死 bar」扩到**对话可生成的全集**：`line` / `bar` / `pie` / `scatter` / `heatmap` / `table`。
3. 图表与分析文本**成对呈现**（变体 1 为主，宽图自动走变体 2）。
4. **修掉 `ink` 的配对缺陷**（第 1.2 节第 5 条）。
5. 图表推断逻辑**单一真源**，消除两套实现。

### 2.2 非目标

见第 15 节。

---

## 3. 总体架构

### 3.1 单一真源：图表推断

```text
src/tools/chart_generator.py
  _classify_chart_type(rows)            [现状：模块私有]
  _build_option(rows, chart_type)       [现状：模块私有]
        ↓ 提升为公开函数，实现一字不改
  classify_chart_type(rows) -> str
  build_chart_option(rows, chart_type) -> dict
        ├──────────────────────────────┐
        ↓                              ↓
  ChartGeneratorTool               src/reporting/document.py
  （对话路径，行为不变）             （报表路径，本次新增调用）
        ↓                              ↓
  POST /visualization/...          ReportBlock(type="chart"|"table", echarts_option=...)
```

**关键取舍 1：报表路径不复制推断逻辑，只调用。**

`_classify_chart_type` 的启发式（时间列→line、分类列→bar/pie、双数值→scatter、兜底→table）是对话路径已验收的行为，报表路径必须与对话给出一致的图表类型，否则同一份数据在对话与报表里长得不一样。

实现方式：**只改函数可见性（去掉下划线）并保留原实现**，`ChartGeneratorTool._run` 改为调用公开名。**不重构 `_build_option` 的内部逻辑**——它的输出是已验收契约。

**关键取舍 2：`chart_type == "table"` 时建 `table` 块，不建 `chart` 块。**

`_build_option` 对 `table` 返回的是 `{"columns": [...], "rows": [...]}`，**不是合法 ECharts option**，放进 `echarts_option` 会被 `validate_echarts_option` 拒绝。报表已有 `table` 块类型，语义正确。

### 3.2 呈现层分层

```text
后端 ── 只表达「有什么内容、多宽」（既有职责，本次仅扩图表类型）
  ReportBlock.content / echarts_option / layout.col_span
        ↓
前端 ── 视觉唯一真源（spec/41 既有分工，本次扩展）
  report/templates.ts         token 注册表（+2 个 token）
  components/report/
    ├─ ReportHero.tsx         [新增] hero 横幅：标题 + 元信息 + KPI 概览
    ├─ ReportBlockPair.tsx    [新增] 图文配对容器（变体 1 / 2）
    ├─ ReportDocumentRenderer.tsx  [修改] 去卡片墙、配对编排、字形规则
    └─ chartBase.ts           [修改] 单色阶梯注入、十字准线
  index.css                   [修改] 去卡片样式，新增 hero / 配对 / 无框表格
```

---

## 4. 后端契约变更

本次后端共 3 处，全部在既有文件内。

### 4.1 `src/reporting/document.py:26` —— 白名单放宽，只加 `visualMap`

```python
_ECHARTS_ROOT_KEYS = {"xAxis", "yAxis", "series", "tooltip", "legend", "dataZoom", "visualMap"}
```

**只加这一个键**，逐项论证：

| 候选键 | 加不加 | 理由 |
|---|---|---|
| `visualMap` | **加** | 热力图必需；且 `_build_option` 产出的白名单外根键**只有这一个**（`heatmap` 分支 `chart_generator.py:171-178`） |
| `title` | 不加 | 标题已由 `ReportBlock.title` 承载，图表内再放标题会重复 |
| `grid` | 不加 | 由前端 `chartBase.ts:113-117` 负责，spec/41 已定 |
| `color` | 不加 | spec/41 关键取舍：后端不持有配色，必须由前端注入 |
| `dataset` / `toolbox` / `graphic` | 不加 | 本轮无需求；`graphic` 且已在 `_FORBIDDEN_OPTION_KEYS` |

**安全性**：`visualMap` 的值仍经 `_validate_option_value` **递归**校验（`document.py:42-65`）——函数、`formatter`、`dataView`、HTML、脚本表达式一律照旧被拒。放宽白名单只是允许这个**容器键**存在，不放宽其内部的值约束。同时 `validate_echarts_option` 的 512 KB 体积上限与 `_DANGEROUS_TEXT_RE` 不变。

### 4.2 `src/reporting/document.py:331-336` —— 写死的 bar 改为调用推断

现状是内联构造一个写死的 bar option：

```python
chart_option: dict[str, Any] = {
    "tooltip": {"trigger": "axis"},
    "xAxis": {"type": "category", "data": [...]},
    "yAxis": {"type": "value"},
    "series": [{"type": "bar", "name": value_column, "data": [...]}],
}
```

改为：

```python
chart_type = classify_chart_type(safe_rows)
if chart_type == "table":
    # 分类为表格：只建 table 块，不建 chart 块
    chart_option = None
else:
    try:
        chart_option = build_chart_option(safe_rows, chart_type)
    except Exception:
        logger.error("报表图表构建失败，降级为明细表", unit_id=section_id, chart_type=chart_type, exc_info=True)
        chart_option = None
        fallback_warnings.append("该分析单元的图表未能生成，已降级为明细表")
```

要点：

1. **入参用 `safe_rows`**（已脱敏、已限 100 行，`document.py:308`），与对话路径传入原始 `rows` 的差别是**脱敏前置**，符合报表安全边界。
2. **图表标题不再硬编码**「趋势与分布」。改为按 `chart_type` 与列语义生成（如 `line` → 「{value_column} 趋势」、`pie` → 「{value_column} 构成」、`heatmap` → 「{value_column} 分布」）。
3. `chart_option is None` 时**不追加 chart 块**，该段自然只剩 `table` + `narrative`，走第 6 节的非配对分支。
4. **降级必须写 `warnings`**，不静默（第 9 节）。

### 4.3 `src/tools/chart_generator.py` —— 函数公开化

```python
# 方法作用：根据时间、文本和数值列组合推荐图表类型。
# Args: rows - 非空查询结果行。
# Returns: line/bar/pie/scatter/table 之一。
def classify_chart_type(rows: list[dict]) -> str: ...      # 原 _classify_chart_type

# 方法作用：根据查询行和图表类型构建 ECharts option。
# Args: rows - 非空查询结果行；chart_type - 已选择图表类型。
# Returns: ECharts 配置字典或 table 数据配置。
def build_chart_option(rows: list[dict], chart_type: str) -> dict: ...   # 原 _build_option
```

- `ChartGeneratorTool._run`（`:72-73`）改为调用公开名。
- **不留旧名别名**——两个名字会让「哪份是当前实现」无法判断。直接改名并同步更新全部调用点；实施时以 `grep` 确认私有名只被 `chart_generator.py` 自身与测试引用。

---

## 5. 前端视觉体系（方向 C）

### 5.1 去卡片墙

`ReportDocumentRenderer.tsx:210-215` 现在的结构是「每个块套一个 `Card`」。改为**无框块**：块标题独立成行，块内容直接铺陈。

层级由**字号、留白、1px 规则线**建立，不再由边框和阴影建立。这条同时把代码拉回 `spec/30:338` 的既有约束。

### 5.2 hero 横幅

文档头从「标题 + 副标题 + 时间」升级为 hero 横幅承载：

| 元素 | 内容 |
|---|---|
| eyebrow | 「经营分析报表」等文档类别小字（大写、字距放宽） |
| 标题 | `document.title` |
| 副标题 | `document.subtitle` |
| 元信息 | 生成时间 · 版本 |
| **KPI 概览** | 从正文提升上来的 KPI（见 5.3） |

**背景与文字色**：新增 token `heroBg`。文字色**按 `heroBg` 亮度推导**——复用 `chartBase.ts:19-28` 的既有 `isDarkSurface` 模式（该函数已有单测）。这样新增模板仍只需声明 1 个 token，不必再维护成对的 hero 明暗文字色。

### 5.3 KPI 提升规则

`kpi` 块现在散在各 section 内（`layout_for_block("kpi")` 占 3 列，`document.py:267`）。

提升规则：

- 按**文档顺序**取前 `HERO_KPI_LIMIT`（= **4**）个 `kpi` 块提升进 hero 横幅，构成一行概览；
- 被提升的 `kpi` 块**从其 section 中移除**——保证每个 KPI 在全页**只出现一次**；
- 超出 4 个的 `kpi` 块**留在原 section** 按原 `col_span` 渲染。

**取舍说明**：4 是「hero 一行放得下」的上限（12 列 ÷ 3 列 = 4）。单元数 > 4 的报表，前 4 个进 hero，其余留在各段——会出现「有的段有 KPI、有的段没有」的不齐。这是**有意的**取舍：优先保证 hero 高度可控。常量具名，将来要改只改一处。

### 5.4 区段标题

`index.css:73` 现在是 `border-left: 3px solid var(--report-accent)` 的竖条——这是仪表盘最通用的套路，也是「不高级」的来源之一。改为「编号 + 标题 + 副标题」三行结构：编号用 accent 色与等宽数字，标题靠字号分层。**移除竖条**。

### 5.5 字形规则

| 场景 | 字形 |
|---|---|
| 表格数据列、坐标轴刻度 | `font-variant-numeric: tabular-nums`（需纵向对齐） |
| **hero 大数字、KPI 数值** | **比例字形（默认）**——等宽只用于需对齐处，大数字用等宽会显得松散 |

这条同时修正了本轮视觉稿中的一处错误（曾给 hero 数字加了 `tabular-nums`）。

### 5.6 图表样式规格

统一到一套规格，六种图表因此成为一个体系：

| 项 | 规格 |
|---|---|
| 网格线 | 1px，极淡（`border` token） |
| 基线 / 轴线 | 1px，比网格线略深 |
| 柱状图圆角 | **4px，仅数据端（顶部）圆角，基线端为直角** |
| 相邻柱 / 堆叠段间隙 | **2px**（底色缝） |
| 折线宽度 | **2px** |
| 散点标记 | 直径 **≥ 8px**，带 2px 底色描边 |
| 单系列柱状图 | **全场一色**，只对研判点名的实体高亮 |
| 热力图 | 单色明度阶梯，**非彩虹** |
| 图例 | **只在 option 自己声明了图例时补样式，前端绝不凭空造图例**（延续 `chartBase.ts:104-112` 的既有规则）。本轮 `_build_option` 只产单系列，全部类型都不带图例，该规则不影响现状 |

> **单系列柱状图为什么不再做「按名次深浅渐变」**：柱长已经编码了数值大小，再用颜色重复编码一遍是冗余；且这是「颜色跟随名次而非实体」——一旦筛选，幸存的柱子会被重新上色。改为**全场一色 + 高亮研判点名的实体**，颜色跟随实体，图与文字结论也对得上。

---

## 6. 区块配对规则（变体 1 / 变体 2）

### 6.1 依据

由第 1.3 节事实 1：**section 即配对边界**。`build_report_document` 对每个 `UnitResult` 产出一个 section，段内至多一个 `chart` 与一个 `narrative`。因此配对是**纯渲染层决策，不新增 DSL 字段、不改后端布局器**。

### 6.2 规则表

| 条件 | 布局 |
|---|---|
| 段内有 `chart` 且**图表类型不是 `heatmap`**，且有 `narrative` | **变体 1**：配对容器占整行（12 列），内部 **62% 图表 ┃ 38% 研判栏** |
| 段内有 `chart` 且**图表类型是 `heatmap`** | **变体 2**：图表满宽，`narrative` 以「研判带」形式紧随其下（文本限宽 **72ch**） |
| 有 `chart` 无 `narrative`，或有 `narrative` 无 `chart` | **不配对**，退回原 `col_span` 流式布局 |
| 段内无 `chart`（取数失败 / 直答类单元） | 不配对，`narrative` / `callout` 按原 `col_span` 渲染 |

**热力图走变体 2 的理由**：热力图是二维矩阵，宽度直接决定单元格可读性，62% 宽会把标签挤到不可读。散点图可接受 62%，不需特殊处理。

### 6.3 与后端布局意图的关系

后端 `layout_for_block`（`document.py:265-285`）继续计算并下发 `col_span`，**职责不变**。配对生效时：

- 配对容器占**整行 12 列**，容器内部再按 62/38 分栏；
- 此时 `chart` 与 `narrative` 各自的 `col_span` **由容器接管**（不再单独生效）；
- 配对不生效时，`col_span` **按原样使用**，行为与本设计之前完全一致。

这条差异必须在代码里**显式分支**（配对容器组件内判断），**不做隐式覆盖**——隐式覆盖会让「为什么后端说 8 列却渲染成 62%」无法追查。

---

## 7. 图表基线扩展（`chartBase.ts`）

| 项 | 现状 | 改为 |
|---|---|---|
| 分类板 | `color = chartPalette` 直接注入（`:92`） | 不变 |
| **单色阶梯** | 无 | **新增**：option 含 `visualMap` 时，注入 `visualMap.inRange.color = sequentialPalette`。`visualMap` 为**数组**时逐项处理（ECharts 允许 `visualMap` 是数组） |
| 十字准线 | 无 | 新增：`tooltip.axisPointer` 十字准线（line / bar 的 axis 触发场景） |
| 图例 | 只在 option 自己声明了图例时补样式，**绝不凭空造图例**（`:104-112`） | **规则保留不变**。本轮不产生多系列图，该规则不受影响 |
| 深色强制覆盖 | `scheme === 'dark'` 时强制覆盖 tooltip / legend / 轴的颜色类字段（`:93,99-112,126`） | 不变 |
| 网格与留白 | `mergeMissing` 补齐 `containLabel` 与边距（`:113-117`） | 不变 |

**合并原则不变**：逐键「仅补齐缺失」，深色模板下颜色类字段强制覆盖的既有例外不变。

---

## 8. 模板 token 变更

### 8.1 新增 2 个 token

```ts
export interface ReportTemplateTokens {
  // ...既有 15 个字段不变

  /** 单色明度阶梯：热力图等连续量级编码用。5 阶，按量级递增排列 */
  sequentialPalette: string[];

  /** hero 横幅底色；文字色按此底色亮度推导，不另设 token */
  heroBg: string;
}
```

**`sequentialPalette` 必须存在的原因**：`chartPalette` 是**分类板**（categorical，用于身份区分），热力图需要的是**单色阶梯**（sequential，用于连续量级）。两者不可互相替代——用分类板画热力图就是彩虹热力图，正是要避免的。

**方向性要求**：`sequentialPalette` 按**量级递增**排列（`[0]` = 最低值）。在**浅色模板**下排列为**浅 → 深**；在**深色模板**下低值应「退向底色」，排列为**深 → 亮**。实施时必须按亮度单调性校验并写入测试。

### 8.2 各模板取值

| token | insight / business（浅底） | paper（米底） | ink（深底） |
|---|---|---|---|
| `sequentialPalette` | `['#cde2fb','#86b6ef','#3987e5','#1c5cab','#0d366b']` | 同左（**有意的简化**，见下方说明） | **反向**：`['#0d366b','#1c5cab','#3987e5','#86b6ef','#cde2fb']` |
| `heroBg` | `'#0f1a2e'`（深色横幅，方向 C 的标志） | `'#332b23'`（深暖褐；米底 + 衬线标题配深横幅如书封） | `'#0b1220'`（与 `bg` 一致，hero 融入页面） |

**`paper` 的 `sequentialPalette` 是取舍，不是遗漏**：`paper` 的分类板是暖色系（首色 `#9a3412`），而此处直接复用了蓝色阶梯，热力图在米底上会与页面色调相冲。之所以这样定：蓝色阶梯是**已文档化且单调性可验证**的一套取值，而手工推导一套暖色阶梯无法在缺少验证手段的情况下保证亮度单调与色盲可读——**宁可用一套和谐的、已知正确的蓝阶，也不用一套凭感觉调的暖阶**。若后续要换成暖阶，必须先有可验证的阶梯生成与单调性校验手段。已记入第 16 节遗留。

### 8.3 `ink` 缺陷修复

`templates.ts:98` 的 `chartPalette` 8 个颜色**全部超出深色亮度带**，实测：

| | 实测亮度（OKLab L） | 判定 |
|---|---|---|
| 现网 `ink` 8 色 | 0.709 – 0.849 | **FAIL**（带内为 0.48–0.67）——深色底挂粉彩 |
| 替换后 8 色 | 全在 0.48 – 0.67 带内 | **PASS**，且对比度 8 色全 ≥ 3:1 |

替换值（在 `#111c2e` 底上实测通过）：

```ts
chartPalette: ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'],
```

**槽序即色盲安全机制，不可重排**：该顺序是穷举候选后仅存的、在浅深两档都通过相邻对色盲分离门槛的排序。将来新增模板时若换色，必须重跑验证脚本，且**只在通过的排序里挑**，不得手工排。

`ink` 的 `accent`（`#38bdf8`）**不动**——它承载 KPI 数值与高亮，是前景强调色而非序列色，亮度带门槛不适用于它；在 `#111c2e` 上对比度充足。

---

## 9. 错误处理与降级

| 场景 | 行为 |
|---|---|
| `classify_chart_type` 返回 `table` | 只建 `table` 块，不建 `chart` 块。**正常路径，不写 `warnings`** |
| `build_chart_option` 抛错（如热力图维度不足 `chart_generator.py:149`） | 降级为只建 `table` 块，**追加 `warnings`**：「该分析单元的图表未能生成，已降级为明细表」 |
| `safe_rows` 为空 | 沿用现状：走 `callout` 分支（`document.py:377-391`），与图表类型无关 |
| 未知 `chart_type` 字符串 | `classify_chart_type` 自身保证只返回白名单值；调用侧再断言一次（防御性双保险） |
| `sequentialPalette` / `heroBg` 缺键 | 注册表合并 `{...insightTokens, ...template.tokens}` 继承补齐，既有机制保证不缺键 |
| 旧版本 `document_json` 无新 token | token 在前端注册表，不在 `document_json` 里；旧记录不受影响 |
| `visualMap` 为数组 | `chartBase` 逐项注入；非对象项原样保留 |
| hero 的 `heroBg` 亮度无法解析（非 `#rrggbb`） | `isDarkSurface` 返回 `false` → 按浅底取文字色。**与该函数既有行为一致** |
| 段内 KPI 全部被 hero 提升后段内无块 | 不会发生：段内必有 `narrative`（`document.py:393` 无条件插入） |

**原则**：用户可见处静默回退（图表降级），但**降级原因写进 `warnings` 并在页面可见**；开发者可见处（内部 API 参数格式）明确报错。

---

## 10. 安全与边界

| 边界 | 处理 |
|---|---|
| `visualMap` 放宽引入的注入面 | **不放宽值约束**：仍经 `_validate_option_value` 递归校验，函数 / `formatter` / `dataView` / `graphic` / HTML / 脚本表达式一律照旧被拒 |
| ECharts option 体积 | 512 KB 上限不变（`document.py:79-80`） |
| 后端配色边界 | **不变**：`color` 仍不在白名单，配色仍只能前端注入 |
| XSS | 渲染器继续不使用 `dangerouslySetInnerHTML`（既有断言 `tests/test_frontend/test_report_workbench.py`） |
| 计划防篡改 | `ReportPlan` 与 `canonical_hash()` **完全不动** |
| 脱敏 | `classify_chart_type` / `build_chart_option` 的入参是 **`safe_rows`**（已脱敏、限 100 行），图表数据不绕过脱敏 |
| 全局主题污染 | hero / 配对容器的样式全部走 `--report-*` 变量，作用域仍在 `.report-theme` 内；`App.tsx` 全局配置一行不动 |
| 前端注入回写 | 图表基线注入发生在渲染期，结果**不回写服务端、不被持久化** |

---

## 11. 目录结构

```text
frontend/src/
├── report/
│   └── templates.ts                              [修改] +sequentialPalette +heroBg；修 ink chartPalette
├── components/report/
│   ├── ReportHero.tsx                            [新增] hero 横幅（标题 + 元信息 + KPI 概览）
│   ├── ReportBlockPair.tsx                       [新增] 图文配对容器（变体 1 / 变体 2 分支）
│   ├── ReportDocumentRenderer.tsx                [修改] 去卡片墙、配对编排、KPI 提升、字形规则
│   └── chartBase.ts                              [修改] 单色阶梯注入、十字准线
├── pages/
│   ├── ReportViewerPage.tsx                      [修改] 改用 ReportHero
│   └── SharedReportPage.tsx                      [修改] 改用 ReportHero
└── index.css                                     [修改] 去卡片样式，新增 hero / 配对 / 无框表格 / 字形规则

src/
├── tools/chart_generator.py                      [修改] 两个私有函数公开化，tool 改调公开名
└── reporting/document.py                         [修改] 白名单 +visualMap；图表类型推断接入；标题去硬编码
```

---

## 12. 测试

### 12.1 后端 `tests/test_reporting/test_chart_types.py`（新增）

类 docstring 注明覆盖的功能编号。

| 测试函数 | 场景 |
|---|---|
| `test_classify_chart_type_is_single_source` | 报表路径与 `ChartGeneratorTool` 调用**同一个函数对象**（防再次分叉） |
| `test_report_uses_line_for_time_series` | 含时间列 + 数值列 → `chart` 块的 `series[0].type == "line"` |
| `test_report_uses_bar_for_category` | 分类列（>8 类）+ 数值列 → `bar` |
| `test_report_uses_pie_for_few_categories` | 分类列（≤8 类）→ `pie` |
| `test_report_uses_scatter_for_two_numeric` | 两个数值列、无文本列、无时间列 → `scatter` |
| `test_report_uses_heatmap_for_two_dimensions` | 两维度列 + 一数值列 → `heatmap`，且 option 含 `visualMap` |
| `test_report_creates_table_block_not_chart_for_table_type` | 分类结果为 `table` → 建 `table` 块，**不建** `chart` 块 |
| `test_heatmap_visualmap_passes_whitelist` | `validate_echarts_option` 接受含 `visualMap` 的 option |
| `test_visualmap_still_rejects_forbidden_values` | 含 `visualMap.inRange.color` 中混入函数 / `dataView` / `<script>` 仍被拒 |
| `test_chart_option_keys_stay_within_whitelist` | 六种类型产出的 option 根键全部落在白名单内（防将来 `_build_option` 新增根键时静默失败） |
| `test_chart_failure_degrades_to_table_with_warning` | 图表构建抛错 → 只建 `table` 块，且 `warnings` 含降级说明 |
| `test_chart_title_not_hardcoded` | 不再出现字面量「趋势与分布」 |

### 12.2 前端契约 `tests/test_frontend/test_report_presentation.py`（新增）

沿用既有 Python 静态断言模式（`Path(...).read_text()` + 符号断言）。

| 测试函数 | 断言 |
|---|---|
| `test_no_card_wrapper_in_report_blocks` | 渲染器中图表 / 表格 / 叙事块**不再**被 `Card` 包裹（防卡片墙回归） |
| `test_pair_layout_branches_on_heatmap` | 配对容器含 heatmap 分支（变体 2）与非 heatmap 分支（变体 1） |
| `test_hero_promotes_kpi_with_limit_constant` | hero 组件存在，且 KPI 提升数量由具名常量控制 |
| `test_chart_base_injects_sequential_for_visualmap` | `chartBase.ts` 含 `visualMap` → `sequentialPalette` 注入分支 |
| `test_tabular_nums_only_on_table_columns` | `tabular-nums` 不出现在 hero / KPI 数字样式上 |
| `test_ink_palette_replaced` | `templates.ts` 的 `ink` 模板 `chartPalette` 不再含 `#38bdf8` 等旧值 |
| `test_every_template_has_sequential_palette` | 4 套模板解析后 `sequentialPalette` 均为 5 阶且非空 |

### 12.3 调色板单调性 / 安全性校验

| 检查 | 方式 |
|---|---|
| `sequentialPalette` 亮度单调 | 后端测试中对每套模板的 5 阶做**亮度单调**断言（深色模板反向） |
| `ink` 新 `chartPalette` 合规 | 以验证脚本口径复算：8 色亮度落在暗色带内、相邻对色盲分离达标 |

> 后一条**必须在实施时真跑验证脚本**，不接受「看起来没问题」。脚本本体不随仓库分发，实施时以等价断言固化到测试里。

### 12.4 浏览器视觉验证 `tests/test_frontend/test_report_presentation_browser.py`（新增）

`pytest.mark.browser`，`RUN_BROWSER_TESTS=1` 显式启用，复用 `test_report_browser.py` 的离线前端启动 fixture（不访问业务后端）。

| 测试函数 | 验证 |
|---|---|
| `test_hero_band_renders_with_document_title` | hero 存在且含文档标题 |
| `test_pair_layout_applied_when_chart_and_narrative_coexist` | 段内同时有 chart 与 narrative 时，两者处于同一配对容器内（`getBoundingClientRect` 横向并列） |
| `test_no_card_class_on_report_blocks` | 报表块节点上不存在 antd `Card` 类名 |
| `test_heatmap_renders_with_sequential_ramp` | 热力图存在且色阶非彩虹 |

**这四个用例是唯一能验证配对与去卡片真正生效的手段**——纯源码静态断言测不出布局是否真的并列。

### 12.5 手工验收

需人工确认：hero 与 KPI 概览在 1440px / 1100px / 767px 三个断点下的观感；4 套模板下 hero 文字色的可读性（尤其 `paper` 的米底）；六种图表在同一模板下的观感一致性；深色模板下表格无「白底穿帮」。

---

## 13. 实施顺序

| 阶段 | 内容 | 依赖 |
|---|---|---|
| A | `chart_generator.py` 公开化 + `document.py` 白名单与推断接入 + `tests/test_reporting/test_chart_types.py` | 无 |
| B | `templates.ts` 两个新 token + `ink` 修复 + `chartBase.ts` 单色阶梯与十字准线 | 无（与 A 并行） |
| C | `ReportHero` + `ReportBlockPair` + 渲染器去卡片与配对编排 + `index.css` | A、B |
| D | 前端契约测试 + 浏览器视觉验证 | C |

阶段 A 与 B **完全并行**，无共享文件。

---

## 14. 与既有设计的差异（需同步修订）

| 文件 | 章节 | 现状表述 | 本文的修订 |
|---|---|---|---|
| `spec/41` | §15 明确不做 | 「图表类型推断（解除硬编码 `bar`）——属分析内容范畴，纳入后续的发散分析设计」 | **该条移出非目标**：本文实施图表类型推断。它确实是呈现层能力（决定用哪种图呈现已有数据），不改变取数与分析范围 |
| `spec/30` | §7.4 图表白名单 | 「只允许普通 JSON 值、白名单 axis/series/tooltip/legend/dataZoom 配置」 | 白名单**增加 `visualMap`**；值级约束不变 |
| `spec/41` | §5.1 `ReportTemplateTokens` | 15 个字段 | **增加 `sequentialPalette`、`heroBg`** 共 17 个 |

以上三处修订**随本文一并评审**，不改动其结论性内容。

---

## 15. 明确不做（YAGNI）

| 不做 | 原因 |
|---|---|
| **多系列图表**（一条 line 多条线、堆叠柱） | `_build_option` 目前每个类型只产**单系列**（`chart_generator.py:126,130,142`）。扩到多系列要改 `_build_option` 的已验收契约与图例规则，是独立一档工作量；本轮图表**类型**覆盖已达标，**系列数**留待后续 |
| **PDF / 打印样式** | 项目完全没有导出功能（`permissions.download` 恒 `false`）。并排一个新模块，需单独排期 |
| **模板审美升级**（insight / paper / business 换色） | 用户明确「后面可以换」。本文只修 `ink` 的**实测缺陷**，审美另开一轮——两个目标混在一个计划里，做完无法判断哪部分算成功 |
| **KPI 的 change / status 内容扩展** | `build_report_document` 现在只写 `value` 与 `unit`，不产环比与状态。属**内容**范畴，与呈现层正交 |
| **图表内嵌标题（`title` 白名单）** | 标题已由 `ReportBlock.title` 承载，图内再加会重复 |
| **版式级模板**（仪表盘型 / 杂志型） | spec/41 §15 既有结论，本文不变 |
| **用户自定义 token 编辑器** | spec/41 §15 既有结论，本文不变 |
| **`document.navigation` / `sources` 消费** | 与呈现层正交，spec/41 §16 既有遗留 |

---

## 16. 遗留与后续

1. **多系列图表**：`_build_option` 的单系列限制解除后，`chartBase.ts` 的图例规则（≥2 系列必有图例）才会真正生效。届时需一并设计图例位置与 `reserveLegendSpace` 的既有逻辑（`chartBase.ts:60-81`）。
2. **`paper` 模板的 hero 实际观感**：`heroBg` 已定为 `#332b23`（第 8.2 节），**不存在待定项**；但米底 + 衬线标题配深色横幅的实际效果仍需人工看过——这是第 12.5 节手工验收清单中的一项。
3. **`heroBg` 与 `spec/41` §12「全局主题污染」的边界**：hero 走 `--report-*` 变量，作用域仍限 `.report-theme`，未越界；但未来若把 hero 提到应用级共用，需重新评估。
4. **图表类型推断的可解释性**：`classify_chart_type` 是启发式，判断错了用户无法在界面纠正。后续可考虑把「这张图为什么是饼图」暴露给用户，或提供手动改图表类型的入口（对话路径已有 `resolve_chart_type_instruction`，可考虑透传到报表）。
5. **`paper` 的暖色热力阶梯**：第 8.2 节说明的取舍。要换成暖阶，前提是先有**可验证的阶梯生成与亮度单调性校验手段**，不能凭感觉调。
6. **`ink` 的 hero 与页面同色**（`heroBg == bg`）：深色模板下 hero 不显边界，是刻意的（避免深浅两层深色打架）。若验收时觉得「hero 看不出是个横幅」，需要的是**加规则线或留白**而不是换底色——换底色会让 ink 出现三层深色。
