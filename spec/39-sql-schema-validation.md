# SQL Schema 校验层设计（生成后 · 打库前）

> 状态：设计待评审
> 关联：spec/03-core-components.md:1000-1065（Layer3/Layer4）、spec/12-langgraph-integration.md:72、spec/21-defect-remediation-design.md:62-65、spec/24-architecture-remediation.md:86、spec/30-report-workbench.md:109
> 关联功能点：features/04-graph.md:72（4.5.1）、features/04-graph.md:90（4.7.3）

---

## 1. 背景与问题

### 1.1 现状：校验不是不存在，而是有空窗

"生成后、打库前"的列校验**已经存在**，且已前移到主防线：

- `src/graph/nodes/layer3_validate.py:47-61` — 已调用 `_validate_column_references`，失败写出 `{"type": "column_reference"}`，由 `after_layer3`（`src/graph/workflow.py:92-98`）路由回 `generate_sql` 重试。
- `src/graph/nodes/execute_sql.py:128-142` — 同一函数作为最后防线再跑一次。
- **报表链路不是独立执行器**：`src/reporting/runner.py:207-219` 的 `_default_execute_unit()` 直接 `app.ainvoke(...)` 跑主图，因此报表单元的 SQL 同样经过 `layer3_validate`。spec/30-report-workbench.md:109 亦明文禁止为报表另写绕过 ACL 的执行器。

因此"报表里仍出现不存在的表/字段"的根因，**不在链路缺失校验，而在校验函数自身的空窗**。

### 1.2 实测确认的空窗清单

| # | 空窗 | 位置 | 后果 |
|---|------|------|------|
| 1 | `tables` 为空时直接 `return None` | `src/graph/nodes/execute_sql.py:346-348` | 静默跳过，校验形同不存在 |
| 2 | 基准是"所有表列名并集"`valid_cols` | `src/graph/nodes/execute_sql.py:353-366` | 列只要在任意一张表存在即通过，形成表盲 |
| 3 | 跨表错配仅在 `resolved in columns_by_table` 时生效 | `src/graph/nodes/execute_sql.py:311` | **不带限定符**的幻觉列若与别表列名同名，被放过 |
| 4 | 别名收集只扫 `exp.Select` 直接表达式 | `src/graph/nodes/execute_sql.py:382-387` | 手工排除 SELECT 别名，会**误伤**真实同名列；CTE 名/派生列未覆盖 |
| 5 | 使用 `find_all(exp.Column)` 而非作用域解析 | `src/graph/nodes/execute_sql.py:402` | 不区分 scope，CTE / 派生表列无法判定归属 |
| 6 | 基准取自 `relevant_tables`（单源问答链路上被裁剪） | 见 1.3 | 合法 SQL 被**误杀** |
| 7 | 方言映射内联，与 `normalize_sql_dialect` 重复 | `src/graph/nodes/execute_sql.py:371-375` vs `src/security/sql_execution.py:124` | 两处不一致风险 |
| 8 | 只回注 `errors[:3]` | `src/graph/nodes/execute_sql.py:418` | 列多时模型改不全 |

### 1.3 两条链路的基准宽窄不一致（关键机制）

`src/graph/nodes/retrieve_schema.py:35` 存在短路：

```python
schema = state.get("resolved_schema")          # ← 短路点
if schema is None:
    schema = await manager.get_or_fetch_schema(
        datasource_name,
        user_query=contexts.request.user_query,  # ← 非空 → 触发 _filter_relevant_tables
        ...
```

- **多源 worker / 报表**：`to_worker_state()`（`src/graph/contracts.py:133-159`）传入了 `resolved_schema` → 使用 Registry 全量快照 → **不裁剪**。
- **单源问答**：`resolved_schema` 为 None → 走 `get_or_fetch_schema(user_query=用户问题)` → `src/knowledge/schema_manager.py:104,130` 的 `if user_query:` 命中 → `_filter_relevant_tables` **裁剪**。

同一份校验代码，两条链路拿到的基准宽窄不同：宽的那条漏报，窄的那条误杀。这是"问答和报表都出问题、但形态不同"的机制。

### 1.4 为什么不能"从根上关掉裁剪"

`relevant_tables` 语义是**双重**的：

- 当"授权 Schema 基准"：`src/graph/nodes/generate_sql.py:851` 表名幻觉校验、`src/graph/nodes/execute_sql.py:131` 列校验
- 当"检索结果"：`src/graph/nodes/decompose_query.py:42`、`src/graph/nodes/analyze_result.py:145`、`src/graph/nodes/llm_answer.py:38` 的 prompt 上下文

注释中两种叫法都出现过（`src/graph/attribution.py:49` 写"当前授权 Schema"，`:341` 写"Schema 检索结果"）。若去掉 `retrieve_schema` 的 `user_query`，全量表结构会涌进 prompt 组装路径，`src/graph/nodes/generate_sql.py:44` 的 `_PROMPT_SCHEMA_MAX_CHARS=60000` 预算会直接溢出。

**结论**：校验基准必须**独立于 `relevant_tables`**，按数据源取权威快照。

---

## 2. 目标与非目标

### 2.1 目标

1. 建立**唯一**的 SQL ↔ Schema 校验实现，问答 / 多源 / 报表三条链路共用。
2. 校验基准**按数据源取权威全量快照**，消除两条链路的基准差异与误杀。
3. 用**作用域感知**解析替代全局 `find_all`，正确处理别名、CTE、派生表、子查询、窗口函数、聚合、UNION。
4. 幻觉 issue **结构化**回喂重试，且不回注失败 SQL。
5. 明确标注**无法校验**的场景，绝不伪装成"校验通过"。

### 2.2 非目标

- 不做类型检查、可空性检查、聚合语义检查（保留给 Layer4 EXPLAIN）。
- 不实现跨源关联查询（当前一条 SQL 只属于一个数据源）。
- 不修复 schema 快照与真实库的漂移（另一议题）。
- 不改变 `relevant_tables` 的现有语义与消费方。

---

## 3. 方案选型

| 方案 | 内容 | 结论 |
|------|------|------|
| A | 原地加固 `_validate_column_references` | 否决：继续堆复杂度进 ~440 行函数，且仍需跨层取快照，函数不再纯粹 |
| **B** | **新建独立校验服务，双防线薄调用** | **采纳**（用户确认） |
| C | 校验下沉到 `sql_execution.py` 执行边界 | 暂缓：破坏 Layer3/Layer4 分层语义，且拿不到 state。可在 B 完成后作为可选收尾 |

**选 B 的理由**：单一职责；同时解决"逻辑重复"（`execute_sql.py` 与 `layer3_validate.py` 已出现双实现分叉苗头）、"基准错误"、"报表覆盖"三件事；符合 spec/24-architecture-remediation.md:86 将 SQL 校验归入 `src/security/` 的既定边界。

---

## 4. 模块设计

### 4.1 位置与依赖方向

新建 `src/security/sql_schema_validation.py`，与 `src/security/sql_execution.py` 同目录。

**依赖方向（硬约束）**：`sql_schema_validation` **不依赖 `src.graph`**。入参是纯数据，出参是结构化 issue 列表。这样 reporting、api、以及未来的直连调用都可复用，不被 LangGraph state 绑定。

### 4.2 接口签名（显式支持多源）

```python
def validate_sql_schema(
    sql: str,
    *,
    datasources: Sequence[str],                 # 权威数据源集合；单源时长度 1
    dialect: str,                               # 解析方言（见 D1）
    tables: Sequence[Mapping[str, Any]],        # 快照；每张表可带可选 "datasource" / "schema"
) -> list[ValidationIssue]: ...
```

**多源语义**（用户确认"现在就显式支持多源列表"）：

- `datasources` 是**允许出现的源白名单**。每条 SQL 引用的表，其归属源必须落在此集合内。
- `tables` 中每张表通过可选字段 `"datasource"` 声明归属；**缺省视为 `datasources[0]`**（单源场景全部缺省，行为与单源完全一致）。**注意：`datasources` 长度大于 1 时该回落不可靠**——多源下同名表的归属会被落到错误的源，因此多源调用方**必须**为每张表显式标注 `datasource`；未标注时校验器记 warning，同名表按 §5.1 的 `resolve_owner` 判为 `ambiguous` 并低置信放行。
- 校验器内部索引键为 `(datasource, table_name)`，结构上不存在"把多源快照混成一个表名空间"的路径。
- 当前所有调用方传长度为 1 的列表（原因见 4.4），多源形状是为跨源查询预留。

**`schema` 可选字段**：为未来的命名空间支持预留。缺省时只按表名匹配（见 §10 盲区 2）。

### 4.3 ValidationIssue 数据结构

```python
@dataclass(frozen=True, slots=True)
class ValidationIssue:
    type: Literal[
        "unknown_table",        # SQL 引用了快照中不存在的表
        "unknown_column",       # 列在该表（或本 scope 所有物理表）中不存在
        "cross_table_column",   # 列名在别的表存在，但不属于被引用表（跨表错配）
        "ambiguous_column",     # 未限定列在多张表中同时存在（DB 会报 ambiguous）
        "parse_error",          # SQL 解析失败
    ]
    datasource: str                        # 该引用所属数据源，跨源比较的唯一依据
    table: str = ""                        # 解析别名后的真实表名
    column: str = ""                       # 列名；未限定时为空
    qualifier: str = ""                    # SQL 中写的限定符原文
    provided_by: tuple[str, ...] = ()      # cross_table_column：真正提供该列的表
    available_columns: tuple[str, ...] = ()  # 该表实际可用列，用于回喂重试
    confidence: Literal["high", "low"] = "high"   # 见 §5.4
```

### 4.4 多源隔离契约

| 约束 | 落地方式 |
|------|----------|
| 单次调用处理一个源的判定 | 函数为纯函数，不读任何全局/state；快照只能由调用方传入 |
| 跨源同名表不串味 | 索引键为 `(datasource, table_name)`；表的归属由 `tables[*]["datasource"]` 显式声明，缺省回落 `datasources[0]` |
| 调用方不许混入未授权源 | `tables` 中 `datasource` 不在 `datasources` 内的表 → 记 warning 并**整体跳过该表**（不参与校验，不作为"不存在"的证据） |
| 方言与源绑定 | 由调用方按数据源取权威方言（`src/security/sql_execution.py:355` `_resolve_sql_target` 已有"不信调用方 dialect"的语义）；校验器内部统一走 `normalize_sql_dialect`（`src/security/sql_execution.py:124`），删除内联副本 |
| 当前实际调用形态 | `layer3_validate` 每次只处理一个源（多源 dispatch 是每源一个 worker，`src/graph/nodes/multi_source.py:121-249`），故调用时传单元素列表 |

---

## 5. 校验算法

### 5.1 作用域感知解析

用 `sqlglot.optimizer.scope.traverse_scope` 替代 `find_all(exp.Column)`。该 API 已在项目内使用：`src/security/permission_check.py:170`。

```python
# 方法作用：逐作用域校验 SQL 引用的表与列是否存在于权威快照。
# Args: sql - 待校验 SQL；datasources - 权威源集合；dialect - 解析方言；tables - 权威快照。
# Returns: 结构化问题清单，空列表表示高置信通过。
def validate_sql_schema(sql, *, datasources, dialect, tables): ...
```

算法骨架：

```
1. 解析
   tree = sqlglot.parse_one(sql, read=normalize_sql_dialect(dialect))
   异常或结果为空 → [ValidationIssue(type="parse_error", confidence="high")]
   （与 spec/21-defect-remediation-design.md:62-65 的 fail-closed 一致）

2. 建权威索引 + 表归属解析
   index: dict[tuple[str, str], set[str]]   # (datasource, table) -> {column}
   跳过 datasource 不在 datasources 内的表（记 warning）

   # 表归属源解析：多源下同一表名可能同时存在于多个源，绝不能默认取某一个
   resolve_owner(table_name) -> str | None | "ambiguous"
       owners = [ds for (ds, t) in index if t == table_name]
       len(owners) == 1 → owners[0]
       len(owners) == 0 → None          （表不存在，交给第 4 步报 unknown_table）
       len(owners) > 1  → "ambiguous"   （多源同名表，基准不可判定 → 低置信放行 + warning）

3. 逐 scope 校验
   for scope in traverse_scope(tree):
       # scope.sources: alias -> Table 节点（物理表）或 Scope 节点（CTE / 派生表）
       local_tables = {
           alias: node.name.lower()
           for alias, node in scope.sources.items()
           if isinstance(node, exp.Table)
       }   # CTE / 派生表天然是 Scope 类型 → 自动排除，不会误判为物理表
       has_cte_or_derived = len(local_tables) < len(scope.sources)
       # 不假设本 scope 所有表同源：逐表解析归属源
       owners = {tbl: resolve_owner(tbl) for tbl in local_tables.values()}

       for col in dedupe(scope.columns):        # 实测存在重复（如 o.id 出现两次）
           if isinstance(col, exp.Star):        # SELECT * / t.*
               记低置信 warning，跳过          # 见 §10 盲区 1
               continue
           if col.table:                        # 带限定符
               real = local_tables.get(col.table.lower())
               if real is None:                 # 限定符指向 CTE / 派生表
                   放行 + 低置信 warning; continue
               ds = owners[real]
               if ds is None or ds == "ambiguous":
                   continue                     # 表不存在/归属不明 → 由第 4 步或低置信处置
               if col.name not in index[(ds, real)]:
                   providers = [
                       t for (d, t) in index
                       if d == ds and col.name in index[(d, t)]
                   ]
                   → cross_table_column（providers 非空）/ unknown_column（providers 为空）
           else:                                # 未限定列
               # 只在本 scope 中「归属已确定」的表里查找；归属不明的表不作为证据
               resolved = {
                   tbl: owners[tbl]
                   for tbl in local_tables.values()
                   if owners[tbl] is not None and owners[tbl] != "ambiguous"
               }
               hits = [t for t, ds in resolved.items() if col.name in index[(ds, t)]]
               if len(hits) == 1: 通过
               elif len(hits) > 1: → ambiguous_column（高置信）
               elif not has_cte_or_derived and len(resolved) == len(local_tables):
                   # 本 scope 全为物理表且归属全部确定，列却不在任何一张里 → 真幻觉
                   → unknown_column（高置信）
               else:
                   # 可能来自 CTE / 派生表，或存在归属不明的表 → 无法判定
                   放行 + 低置信 warning

4. 表级校验
   所有 scope 的 exp.Table 节点：
       resolve_owner(name) is None       → unknown_table（高置信）
       resolve_owner(name) == "ambiguous" → 低置信 warning（多源同名表）
```

### 5.2 实测覆盖矩阵（sqlglot 30.12.0）

17 个用例实跑结果，作为准确度依据：

| 场景 | 实测结论 |
|------|----------|
| 表别名 `orders AS o` | `o → PHYS:orders`，列归属正确 |
| CTE `WITH t AS (SELECT ... FROM orders) SELECT ... FROM t` | CTE 内部正确解析出物理表 `orders`；外层 `t` 为 `Scope` 类型 → 可精确排除 |
| 派生表 `FROM (SELECT a FROM orders) x` | `x` 为 `Scope` → 可排除；内部 `orders` 正确 |
| 窗口函数 `ROW_NUMBER() OVER (PARTITION BY o.customer_id ORDER BY o.created_at)` | PARTITION / ORDER BY 内的列**全部提取，无遗漏** |
| 嵌套函数 `DATE_TRUNC('MONTH', o.created_at)` | 只提取 `o.created_at`；字符串 `'MONTH'` 未被误当列 |
| 聚合 + 列别名 `SUM(o.amount) AS total ... ORDER BY total` | 只提取 `o.amount`；别名 `total` 未被误当列 |
| CTE 内幻觉列 `WITH t AS (SELECT o.bogus_col FROM orders o)` | **可检出**（现有实现漏检的形态） |
| JOIN 多表 | 各列归属正确 |
| UNION / WHERE 子查询 / 引号标识符 `"Order Date"` | 分 scope 各自正确 |
| 未限定列 `SELECT amount FROM orders` | AST 给出 `-.amount`（无归属）→ **需按 §5.1 第 3 步自行判定** |
| `SELECT *` | `columns=[]` → **无法校验**，必须显式识别 |
| 全限定表 `analytics.orders` | `node.name` 仅 `orders`，`analytics` 落在 `node.db` 被丢弃 |

**结论**：别名、CTE、派生表、子查询、窗口函数、聚合、嵌套函数、UNION、引号标识符均可准确解析；未限定列需补一步判定；`SELECT *` 与命名空间为已知盲区。

### 5.3 未限定列的归属判定

未限定列（`-.amount`）从 AST 无法直接得到归属表，按 §5.1 第 3 步在本 scope 的**物理表**列集合中查找：唯一命中 → 归属确定；多表命中 → 歧义列（DB 同样会报错，属提前拦截的增益）；零命中且无 CTE/派生表 → 高置信幻觉。

### 5.4 置信度分层与处置（用户确认）

| 置信度 | 触发条件 | 处置 |
|--------|----------|------|
| **high** | 限定符表明确不含该列；列在本 scope 所有物理表中均不存在且无 CTE/派生表；未限定列多表命中；表不存在；解析失败 | **拦截**，写入 `validation_errors`，回喂重试 |
| **low** | `SELECT *` / `t.*`；限定符指向 CTE / 派生表；存在 CTE/派生表时未限定列零命中；命名空间不明 | **放行 + 结构化 warning**，不阻断 |

**该分层是避免误杀的关键**：对"无法判定"的情况不做拦截，否则合法 SQL 会被频繁打回，体验劣于现状。

---

## 6. 两阶段取数（免费 + 复核）

放在 `layer3_validate_node`（async，天然可 await）。**校验器本身保持零 IO 纯函数**。

```
阶段一（零 IO）：用 state["relevant_tables"] 校验
  ├─ 无 high 置信 issue → 通过，结束（绝大多数合法 SQL 走此路径）
  └─ 出现 high 置信 issue → 不可直接上报：该快照可能是裁剪过的
        ↓
阶段二（缓存查询；按 high 置信 issue 涉及的源逐个取，当前调用方只传 1 个源故实为 1 次）：
  # 短路条件：无法从 state 解析出数据源时（如单测构造的最小 state）跳过阶段二，
  # 直接用阶段一结果。否则会退化为尝试连库，拖慢甚至挂住不涉及数据源的调用方。
  if not datasource:
      记录 debug 日志("阶段二跳过", reason="无数据源")；直接用阶段一结果
  else:
      for ds in {issue.datasource for issue in high_issues}:
          tables_full[ds] = await get_or_fetch_schema(ds, user_query="")   # 空 query 不过滤
      用全量快照重校验
  整个阶段二用 try/except 包裹：任何异常 → 记 warning 并回落阶段一结果（fail-safe，不因复核失败阻断链路）
  ├─ 无 high 置信 issue → 判为裁剪假阳性，丢弃，通过（记 counter）
  └─ 仍有 high 置信 issue → 确认幻觉，上报
```

**依据**：`src/knowledge/schema_manager.py:104,130` 确认 `user_query` 为空时不触发 `_filter_relevant_tables`；`get_or_fetch_schema`（`src/knowledge/schema_manager.py:69`）有四级回退，且带 7 天 TTL 的连接级共享缓存（`src/knowledge/datasource_cache.py`），通常命中内存或本地 JSON，不打库。

**收益**：合法 SQL 零额外开销；消除两条链路的基准差异与误杀；同时修掉空窗 #1：

- **阶段一为空（无基准）→ 仍然进入阶段二复核**。触发条件是「有 high 置信 issue **或** 阶段一没有基准可比对」（`need_recheck = bool(high_issues) or not tables_hint`），不是只看 high issue——否则空基准会直接放行，空窗依旧。
- **阶段二也拿不到权威快照 → 降级放行 + warning 留痕**。

> 语义边界（避免误读为硬保证）：「空 `tables` 不再静默放行」只在权威快照**可得**时成立。
> 若快照也不可得（`get_or_fetch_schema` 返回空或异常），校验结果是**降级放行**——有 warning 留痕可追溯，但该 SQL 会通过。这是刻意的：无基准时把「表不存在」判为幻觉就是误杀，与 §5.4「无法判定不拦截」冲突。执行层（`execute_sql` 的第二道防线）仍会兜底。

> 注：此处**不**采用 fail-closed 报 `unknown_table`。空基准下「表不存在」不是可确认的结论——若按 high 置信上报，会对 SQL 里的**每一张表**都给出误导性诊断（模型据此改表名只会越改越错），且违背 §5.4「无法判定 → 不拦截」的分层原则。`_collect_unknown_tables` 以 `has_baseline = bool(index)` 区分「无基准」与「表确实不在基准里」，前者降 low、后者仍 high。

**待验证假设**：阶段二的缓存命中率。实现时必须加日志确认，不允许该假设默默存在。

---

## 7. 调用方改造

### 7.1 `src/graph/nodes/layer3_validate.py:47-61`

改为：阶段一 → 阶段二 → 将 high 置信 issue 映射为 `{"type": "column_reference", "issues": [...], "message": <摘要>}` 写入 `validation_errors`。保留现有 `column_reference` 类型名以兼容 `after_layer3` 路由。

### 7.2 `src/graph/nodes/execute_sql.py:128-142`

改为薄调用（最后防线）。`_validate_column_references` 与 `find_cross_table_columns` 均作为兼容适配器保留（签名与返回结构不变，内部委托新模块），避免破坏既有调用方与 7 个回归用例。

### 7.3 `src/graph/nodes/generate_sql.py:851` 表级校验收敛（决策 D3）

`_check_table_hallucination` 的表名比对逻辑改为调用新模块导出的表名比对函数，**保留其"生成后立即校验"的早失败位置**。避免表级判定在两处出现不一致。

### 7.4 重复实现收敛（决策 D5）

| 现有实现 | 处置 |
|----------|------|
| `src/graph/nodes/execute_sql.py:263-275` `_columns_by_table` | 迁入新模块，唯一导出 |
| `src/graph/nodes/execute_sql.py:281-290` `_alias_to_table` | 被 scope 解析取代，删除 |
| `src/graph/nodes/execute_sql.py:296-321` `find_cross_table_columns` | 逻辑并入新模块的 `cross_table_column` 判定 |
| `src/graph/nodes/supplemental_query.py:73` `_schema_table_columns` | **不收敛**。它额外纳入列注释、表描述、业务别名（`aliases`/`tags`）用于自然语言模糊匹配，与校验器需要的"严格表名→真实列名"语义不同；强行合并会削弱 `supplemental_query` 的匹配能力 |

---

## 8. 重试回喂契约（决策 D4）

现状 `build_retry_context`（`src/graph/nodes/generate_sql.py:356-391`）：仅 `type == "hallucination"` 不回注失败 SQL；其余分支在 `:378-391` **回注失败 SQL 前 3000 字符** + `validation_errors` 的 JSON。列幻觉目前走的是后一条路径，等于把幻觉 SQL 完整喂回模型，与 spec/12-langgraph-integration.md:72 的精神相悖。

**改造**：新增"schema 幻觉"分支，覆盖 `column_reference`（含 `unknown_column` / `cross_table_column` / `ambiguous_column` / `unknown_table`）：

- **不回注失败 SQL**
- 回注结构化事实：按表分组输出"该表实际可用列"（来自 `ValidationIssue.available_columns`）
- 多源时按 `issue.datasource` 分组标注
- 保留 `retry >= 2` 的"最后一次尝试"提示

---

## 9. 观测（用户确认：结构化日志 + 指标计数）

**结构化日志**（`src/logging_config.get_logger`）：

- 入口 `debug`：`sql_chars`、`datasource_count`、`table_count`、`dialect`
- 拦截 `warning`：`issue_type`、`table`、`column`、`qualifier`、`provided_by`
- 无法判定 `warning`：`reason`（`select_star` / `cte_qualifier` / `namespace_unknown`）
- 出口 `info`：`result`、`high_count`、`low_count`、`stage`（`stage1` / `stage2`）

**指标计数**（接入 `src/observability.py:27` 的 `MetricsRegistry`，仿 `:107-111` 的 `record_schema_warning` 先例）：

- 新增 Counter `data_agent_sql_schema_validation_total`
- 标签：`issue_type`、`confidence`、`dialect`、`stage`
- **不使用数据源名做标签**：数据源数量随租户增长，会造成高基数（现有约定见 `record_schema_warning` 的 `[:32]` 截断）

---

## 10. 已知限制与盲区

必须显式记录，且在实现中以 warning 呈现，**不得静默当作"校验通过"**：

1. **`SELECT *` / `SELECT t.*`**：AST 不展开星号（实测 `columns=[]`）。无法校验列。
2. **命名空间**：`TableSchema.name`（`src/datasource/schema_snapshot.py:30-40`）不含 schema/database；实跑 `analytics.orders` 时 `analytics` 落在 `node.db` 被丢弃。**当前仅按表名匹配**，多 schema 同名表无法区分。为此在 `tables` 上预留可选 `schema` 字段。
3. **方言特有表函数 / `UNNEST` / `LATERAL`**：需按方言实测补充，不在本设计承诺范围内。
4. **快照与真实库漂移**：校验基准是各级缓存拼出的快照，若快照本身与真实库不一致，校验无法发现。属独立议题。
5. **未限定列的 CTE 歧义**：scope 内存在 CTE/派生表时，未限定列零命中只能判为低置信。

---

## 11. 测试策略

新建 `tests/test_security/test_sql_schema_validation.py`（路径与源码一一对应，遵循项目规范）。单元测试禁止调用真实模型。

**必须覆盖**：

| 类别 | 用例 |
|------|------|
| 正常路径 | 简单别名、JOIN、窗口函数、嵌套函数、聚合别名 + ORDER BY 别名、CTE、派生表、UNION、引号标识符 —— 全部通过 |
| 高置信拦截 | 限定符表不含该列；未限定列在纯物理表 scope 中零命中；表不存在；解析失败 |
| 跨表错配 | `i.created_at` 实际属于 `orders` → `cross_table_column` 且 `provided_by` 正确 |
| 歧义列 | `SELECT id FROM orders o JOIN customers c` 两表都有 `id` → `ambiguous_column` |
| 低置信放行 | `SELECT *`；限定符指向 CTE；含 CTE 时未限定列零命中 |
| 多源隔离 | 传 `datasources=["a"]` 但 `tables` 掺入 b 源专有列 → 该列**不作为 a 的证据**参与判定；`tables` 中 `datasource` 不在 `datasources` 内 → 整体跳过该表 |
| 跨源同名表 | a 源与 b 源各有 `orders` 且列不同 → 按 `(datasource, table)` 索引后各自判定正确 |
| 方言兼容 | `mssql → tsql`、`postgresql → postgres` 的归一化；按方言分别验证解析 |
| 边界 | 空 `tables`；空 SQL；`tables` 无 `columns`；重复列；`ORDER BY 1` 序号 |
| 回退路径 | 阶段二（裁剪假阳性消除）单独测试；解析异常 fail-closed |

**回喂契约测试**：`tests/test_graph/test_generate_sql_retry_context.py` 验证 schema 幻觉分支不回注失败 SQL 且包含可用列清单。

**兼容测试**：`tests/test_graph/test_execute_column_validation.py` 现有 7 个用例必须继续通过。

- `_validate_column_references` 保留为包装层。错误消息必须包含 `不属于表` 与 `orders` 字样（`test_cross_table_column_is_rejected` 的断言）。
- `find_cross_table_columns` 保留为**适配器**（签名与 dict 返回结构不变，内部委托新模块）：`test_cross_table_column_probe` 与 `test_cross_table_probe_ignores_matching_column` 直接依赖其返回格式（`column == "i.created_at"`、`provided_by == ["audit_log"]`）。判定逻辑仍只有一份，适配器只做格式转换。
- 校验器内部必须使用 `sqlglot.parse_one`（**不是** `sqlglot.parse`）：`test_mssql_uses_tsql_parser` monkeypatch 的是 `parse_one` 并断言 `read == "tsql"`；若改用复数 `parse`（`_check_table_hallucination` 即为此写法），该用例会静默失效。

---

## 12. 实施步骤

1. 新建 `src/security/sql_schema_validation.py`：`ValidationIssue` + `validate_sql_schema` + 表名/列名索引工具。
2. 建 `tests/test_security/test_sql_schema_validation.py`，覆盖 §11 全部分类。
3. 改造 `layer3_validate`：接入校验器 + 两阶段取数 + issue → `validation_errors` 映射。
4. 改造 `execute_sql._validate_column_references` 为兼容包装（薄调用）。
5. 收敛 `generate_sql._check_table_hallucination` 与 `supplemental_query._schema_table_columns`。
6. 改造 `build_retry_context`：新增 schema 幻觉分支（不回注失败 SQL）。
7. 接入 `MetricsRegistry` 计数器与结构化日志。
8. 更新 `features/04-graph.md` 4.5.1 / 4.7.3 状态与描述；新增本设计对应功能点。
9. 判断并更新 `CODE_GUIDE.md`（新增模块 → 目录结构；校验节点行为变化 → 数据流图）。

**范围与拆分建议**：本设计含 1 个新模块 + 4 处现有文件改造 + 1 个指标 + 3 个测试文件，边界清晰但体量偏大。建议按两个可独立验收的阶段推进——**阶段 A**（步骤 1-2、7）：新模块 + 单测 + 指标接入，此时尚未改动任何调用方，对现网零影响；**阶段 B**（步骤 3-6、8-9）：接入 `layer3_validate` / `execute_sql` / `generate_sql` 并收敛重试契约。

**约束**：设计文档与代码改动**只落盘，不提交**（项目规则）。

---

## 13. 待校验决策（自主决断项）

以下为本设计自主决断、需在评审时一并确认的点：

| 编号 | 决策 | 理由 | 备选 |
|------|------|------|------|
| D1 | `dialect` 保持单一字符串，多源不逐源解析 | 一条 SQL 文本只能用一个方言解析；跨源关联查询落地时若使用联邦/统一方言，由调用方指定即可 | `dialects: Mapping[datasource, dialect]` |
| D2 | `datasources` 为白名单 + `tables[*]["datasource"]` 声明归属 | 显式多源列表（用户已定），且单源时零额外复杂度 | 仅 `datasource: str` |
| D3 | 表级校验收敛为单一实现，但保留 `generate_sql` 早失败位置 | 避免两处判定不一致；保留生成后立即失败的响应速度 | 两者并存 |
| D4 | `column_reference` 类 issue 不回注失败 SQL | 与 spec/12-langgraph-integration.md:72 一致；回注幻觉 SQL 会强化错误 | 沿用现行为 |
| D5 | 旧实现改兼容包装，不保留双实现 | 防止分叉（现有 `execute_sql.py` 与 `layer3_validate.py` 已有此苗头）。`_validate_column_references` 与 `find_cross_table_columns` 均保留为适配器（后者被 2 个回归用例直接依赖其 dict 返回格式） | 直接删除旧函数 |
