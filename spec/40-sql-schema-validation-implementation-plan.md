# SQL Schema 校验层实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立唯一的"生成后 · 打库前"SQL ↔ Schema 校验实现，用作用域感知解析替代全局 `find_all`，校验基准按数据源取权威快照，消除问答/多源/报表三条链路的漏报与误杀。

**Architecture:** 新建 `src/security/sql_schema_validation.py`（纯函数、不依赖 `src.graph`）；`layer3_validate`（主防线）与 `execute_sql`（最后防线）改为薄调用；`generate_sql` 的表级校验与重试回喂契约收敛到同一实现。用户已确认决策 D1-D5。

**Tech Stack:** Python 3.14.0（项目 `.venv`，`pyproject.toml` 要求 `requires-python = "==3.14.0"`）、sqlglot 30.9.0（`sqlglot.optimizer.scope.traverse_scope`）、pytest、prometheus_client（经 `src/observability.py`）

**依据文档:** `spec/39-sql-schema-validation.md`

---

## Global Constraints

- **提交策略（注意：与撰写本计划时的约定已不同）**：撰写时项目规则禁止自动提交，故本计划正文不含提交步骤。**执行时用户于 2026-09-12 显式授权本地 commit**（仍**禁止 push**）；提交信息**不得出现任何 Claude/Anthropic 署名**（用户明确要求）。逐任务执行与提交记录见 `.superpowers/sdd/progress.md`。
- **每个方法必须有中文注释三要素**（作用 / Args / Returns），紧邻定义上方。
- **每个方法必须打印日志**：入口 `debug`（方法名 + 关键参数）、出口 `info`（结果摘要）、异常 `error`（含 `exc_info=True`）、分支回退 `info`/`warning`。循环内用 `debug`，禁用 `print()`。
- **必须使用项目 `.venv` 解释器**：所有命令一律写 `.venv/Scripts/python.exe`（Python **3.14.0**）。**禁止**用系统 `python`——它指向 `D:\python3.10.6`，缺 `StrEnum` 等 3.11+ 特性，会让 `src/failure_policy.py` 直接 `ImportError`，产生**假故障**（实测：同一套测试在 3.10 下 `7 failed`，在 `.venv` 下 `7 passed`）。
- 类型注解使用 `from __future__ import annotations`；语法按 Python 3.14 编写。
- **校验器内部必须用 `sqlglot.parse_one`**（不是 `sqlglot.parse`）：`tests/test_graph/test_execute_column_validation.py::test_mssql_uses_tsql_parser` monkeypatch 的是 `parse_one` 并断言 `read == "tsql"`。
- **单元测试禁止调用真实模型**；不得依赖数据库连接。
- **测试文件路径与源码一一对应**。
- **错误消息必须包含的固定字样**（回归用例断言）：`不属于表`、`解析失败`。
- 统一测试命令：在 `D:\work\dataAnalysisAgent` 下执行 `.venv/Scripts/python.exe -m pytest <path> -v`。

---

## 实测证据（实现前必读）

以下为 sqlglot **30.9.0**（项目 `.venv` 实际版本）实跑结论，直接决定实现写法。已在 30.9.0 上逐条复验通过：

### 证据 1：`traverse_scope` 的作用域解析能力

| 场景 | 实测结论 |
|------|----------|
| 表别名 `orders AS o` | `scope.sources == {'o': Table(orders)}`，列归属正确 |
| CTE | 外层 `scope.sources == {'t': Scope}`；CTE 内部 scope 正确解析出物理表 `orders` |
| 派生表 `FROM (SELECT a FROM orders) x` | `scope.sources == {'x': Scope}`；内部 `orders` 正确 |
| 窗口函数 `ROW_NUMBER() OVER (PARTITION BY o.customer_id ORDER BY o.created_at)` | PARTITION / ORDER BY 内的列**全部出现在 `scope.columns`，无遗漏** |
| 嵌套函数 `DATE_TRUNC('MONTH', o.created_at)` | 只提取 `o.created_at`，字符串 `'MONTH'` 未被误当列 |
| 聚合别名 `SUM(o.amount) AS total ... ORDER BY total` | 只提取 `o.amount`，别名 `total` 未被误当列 |
| 未限定列 `SELECT amount FROM orders` | `scope.columns` 给出 `Column(table='', name='amount')`，**无归属**，需自行判定 |
| 引号标识符 `"o"."Order Date"` | 正确保留大小写与空格 |

**推论**：CTE 与派生表在 `sources` 中是 `Scope` 类型（非 `exp.Table`），用 `isinstance(node, exp.Table)` 过滤即可精确排除，不需要手工比对 CTE 名。

### 证据 2：星号判定不能用 `find_all(exp.Star)`

| SQL | 投影 AST | `find_all(Star)` 命中 |
|-----|----------|----------------------|
| `SELECT *` | `Star()` | 1 |
| `SELECT t.*` | `Column(this=Star(), table=t)` | 1 |
| **`SELECT COUNT(*)`** | `Count(this=Star())` | **1** |

**推论**：星号可能位于函数参数内（`COUNT(*)` 合法），必须**只看投影表达式顶层**是否为 `Star`，或用 `Column` 且其 `this` 是 `Star`。用 `find_all(Star)` 会把 `COUNT(*)` 误报为"无法校验"。

### 证据 3：CTE 名会出现在 `find_all(exp.Table)` 中

`WITH x AS (SELECT id FROM orders) SELECT x.id FROM x` → `find_all(Table)` 返回 `['x', 'orders']`。

**推论**：表级校验（`_collect_unknown_tables`）必须用 `find_all(exp.CTE)` 收集 CTE 名并排除，否则 CTE 名会被误判为不存在的表。

### 证据 4：`SELECT *` 时 `scope.columns` 为空

`SELECT * FROM orders` → `scope.columns == []`。

**推论**：星号检测不能依赖 `scope.columns`，必须遍历 `scope.expression` 的 SELECT 投影，并使用证据 2 的顶层判定。

---

## 阶段划分

| 阶段 | Task | 内容 | 风险 |
|------|------|------|------|
| **A** | 1-3 | 新建模块 + 单测 + 指标接入，**不改任何调用方** | 对现网零影响 |
| **B** | 4-8 | 接入 `layer3_validate` / `execute_sql` / `generate_sql`，收敛重试契约 | 改变现网行为 |

阶段 A 完成后先跑一次全量回归再进 B 阶段：

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph/ tests/test_security/ -v
```

---

## 文件结构

| 文件 | 职责 | 动作 |
|------|------|------|
| `src/security/sql_schema_validation.py` | 唯一校验实现 | 新建 |
| `tests/test_security/test_sql_schema_validation.py` | 校验器单测 | 新建 |
| `tests/test_graph/test_layer3_schema_validation.py` | 两阶段取数测试 | 新建 |
| `tests/test_graph/test_generate_sql_retry_context.py` | 回喂契约测试 | 新建 |
| `src/graph/nodes/layer3_validate.py` | 主防线：接入 + 两阶段 | 改造 `:47-61` |
| `src/graph/nodes/execute_sql.py` | 最后防线：适配器 | 改造 `:263-442` |
| `src/graph/nodes/generate_sql.py` | 表级校验收敛 + 回喂契约 | 改造 `:356-391`、`:846-905` |
| `src/observability.py` | 指标计数 | 改造 `:27-111` |
| `src/graph/nodes/supplemental_query.py` | **不改动**（语义不同：含描述/别名，用于模糊匹配） | 保持 |

---

## Task 1: 数据结构与表索引工具

**Files:**
- Create: `src/security/sql_schema_validation.py`
- Test: `tests/test_security/test_sql_schema_validation.py`

**Interfaces:**
- Consumes: `src.logging_config.get_logger`
- Produces:
  - `ValidationIssue(type, datasource, table="", column="", qualifier="", provided_by=(), available_columns=(), confidence="high")` — frozen dataclass，含 `describe() -> str`
  - `build_table_index(tables, datasources) -> dict[tuple[str, str], frozenset[str]]`
  - `resolve_table_owner(index, table_name) -> str | None`（返回 `"ambiguous"` 表示多源同名）

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_security/test_sql_schema_validation.py`：

```python
"""SQL Schema 校验层单元测试。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class TestTableIndex:
    """覆盖表/列索引与表归属解析（spec/39 §4.2、§5.1）。"""

    # 方法作用：验证索引键为 (数据源, 表名) 且缺省源回落到白名单首项。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_index_uses_datasource_and_table_key(self) -> None:
        """单源场景下表归属缺省回落到 datasources[0]。"""
        logger.debug("test_index_uses_datasource_and_table_key 入口")
        from src.security.sql_schema_validation import build_table_index

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}, {"name": "amount"}]}]

        # Act
        index = build_table_index(tables, ["pg_main"])

        # Assert
        assert index[("pg_main", "orders")] == frozenset({"id", "amount"})
        logger.info("test_index_uses_datasource_and_table_key 完成")

    # 方法作用：验证未授权源的表被整体跳过，不作为校验证据。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_index_skips_unauthorized_datasource(self) -> None:
        """datasource 不在白名单内的表必须整体跳过。"""
        logger.debug("test_index_skips_unauthorized_datasource 入口")
        from src.security.sql_schema_validation import build_table_index

        # Arrange
        tables = [
            {"name": "orders", "datasource": "pg_main", "columns": [{"name": "id"}]},
            {"name": "secret", "datasource": "pg_other", "columns": [{"name": "token"}]},
        ]

        # Act
        index = build_table_index(tables, ["pg_main"])

        # Assert
        assert ("pg_main", "orders") in index
        assert ("pg_other", "secret") not in index
        logger.info("test_index_skips_unauthorized_datasource 完成")

    # 方法作用：验证单源归属唯一、多源同名判为 ambiguous、不存在返回 None。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_resolve_owner_single_and_ambiguous(self) -> None:
        """归属解析三分支必须正确。"""
        logger.debug("test_resolve_owner_single_and_ambiguous 入口")
        from src.security.sql_schema_validation import build_table_index, resolve_table_owner

        # Arrange
        single = build_table_index(
            [{"name": "orders", "columns": [{"name": "id"}]}], ["pg_main"]
        )
        multi = build_table_index(
            [
                {"name": "orders", "datasource": "pg_a", "columns": [{"name": "id"}]},
                {"name": "orders", "datasource": "pg_b", "columns": [{"name": "amount"}]},
            ],
            ["pg_a", "pg_b"],
        )

        # Act & Assert
        assert resolve_table_owner(single, "orders") == "pg_main"
        assert resolve_table_owner(single, "missing") is None
        assert resolve_table_owner(multi, "orders") == "ambiguous"
        logger.info("test_resolve_owner_single_and_ambiguous 完成")


class TestValidationIssue:
    """覆盖 ValidationIssue 描述渲染（spec/39 §4.3）。"""

    # 方法作用：验证跨表错配描述包含「不属于表」与真正提供该列的表名。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_describe_cross_table_column(self) -> None:
        """describe() 必须点明真实归属，供模型改正。"""
        from src.security.sql_schema_validation import ValidationIssue

        # Arrange
        issue = ValidationIssue(
            type="cross_table_column",
            datasource="pg_main",
            table="finance_invoices",
            column="created_at",
            qualifier="i",
            provided_by=("orders",),
        )

        # Act
        text = issue.describe()

        # Assert
        assert "不属于表" in text
        assert "orders" in text
        assert "i.created_at" in text

    # 方法作用：验证解析失败描述包含「解析失败」字样。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_describe_parse_error(self) -> None:
        """parse_error 必须可被既有回归用例识别。"""
        from src.security.sql_schema_validation import ValidationIssue

        # Arrange & Act
        text = ValidationIssue(type="parse_error", datasource="pg_main").describe()

        # Assert
        assert "解析失败" in text
```

- [ ] **Step 2: 运行测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest tests/test_security/test_sql_schema_validation.py -v
```
预期：`ModuleNotFoundError: No module named 'src.security.sql_schema_validation'`

- [ ] **Step 3: 写最小实现**

创建 `src/security/sql_schema_validation.py`：

```python
"""SQL Schema 校验层 — 生成后、打库前的表与列引用校验。

设计依据：spec/39-sql-schema-validation.md
依赖方向：本模块不依赖 src.graph，入参为纯数据，可被 graph / reporting / api 复用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from src.logging_config import get_logger

logger = get_logger(__name__)

IssueType = Literal[
    "unknown_table",
    "unknown_column",
    "cross_table_column",
    "ambiguous_column",
    "parse_error",
]
Confidence = Literal["high", "low"]

# 回喂重试时单表最多携带的可用列数，避免撑爆 Prompt 预算。
MAX_AVAILABLE_COLUMNS = 40


# 方法作用：承载单条 Schema 校验问题，供调用方拦截与结构化回喂重试。
# Args: type - 问题类型；datasource - 归属数据源；table - 解析别名后的真实表名；
#       column - 列名；qualifier - SQL 中写的限定符；provided_by - 真正提供该列的表；
#       available_columns - 该表实际可用列；confidence - 置信度。
# Returns: 不可变的问题记录。
@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """单条 Schema 校验问题。"""

    type: IssueType
    datasource: str
    table: str = ""
    column: str = ""
    qualifier: str = ""
    provided_by: tuple[str, ...] = ()
    available_columns: tuple[str, ...] = ()
    confidence: Confidence = "high"

    # 方法作用：把问题渲染成可读中文描述，供 validation_errors.message 与回喂使用。
    # Args: self - 当前问题记录。
    # Returns: 一句话中文描述。
    def describe(self) -> str:
        """渲染可读描述。"""
        reference = f"{self.qualifier}.{self.column}" if self.qualifier else self.column
        if self.type == "parse_error":
            return "列名校验解析失败"
        if self.type == "unknown_table":
            return f"表 '{self.table}' 不在可用表结构中"
        if self.type == "ambiguous_column":
            return f"列 '{self.column}' 在多张表中同时存在，必须加表限定符"
        if self.type == "cross_table_column":
            providers = ", ".join(self.provided_by[:5]) or "未知"
            return f"列 '{reference}' 不属于表 {self.table}（该列属于: {providers}）"
        return f"列 '{reference}' 不在表 {self.table} 的可用列中"


# 方法作用：把权威快照整理成 (数据源, 表名) -> 列名集合 的索引。
# Args: tables - 权威快照，每张表可带可选 datasource 字段；datasources - 允许的数据源白名单。
# Returns: 索引字典；datasource 不在白名单内的表被整体跳过。
def build_table_index(
    tables: Sequence[Mapping[str, Any]],
    datasources: Sequence[str],
) -> dict[tuple[str, str], frozenset[str]]:
    """构建 (datasource, table) -> columns 索引，越界源整体跳过。"""
    logger.debug("建表索引入口", table_count=len(tables), datasource_count=len(datasources))
    allowed = [str(item).strip() for item in datasources if str(item).strip()]
    allowed_set = set(allowed)
    default_source = allowed[0] if allowed else ""
    index: dict[tuple[str, str], frozenset[str]] = {}
    skipped = 0
    for table in tables:
        if not isinstance(table, Mapping):
            continue
        name = str(table.get("name", "") or "").strip().lower()
        if not name:
            continue
        source = str(table.get("datasource", "") or "").strip() or default_source
        if allowed_set and source not in allowed_set:
            logger.warning("表索引跳过未授权源", table=name, datasource=source)
            skipped += 1
            continue
        columns = frozenset(
            str(column.get("name", "") or "").strip().lower()
            for column in (table.get("columns") or [])
            if isinstance(column, Mapping) and str(column.get("name", "") or "").strip()
        )
        index[(source, name)] = columns
    logger.info("建表索引完成", entries=len(index), skipped=skipped)
    return index


# 方法作用：解析某个表名归属的数据源，多源同名时判为歧义。
# Args: index - build_table_index 的输出；table_name - SQL 中引用的表名。
# Returns: 归属数据源名称；表不存在返回 None；多源同名返回 "ambiguous"。
def resolve_table_owner(
    index: Mapping[tuple[str, str], frozenset[str]],
    table_name: str,
) -> str | None:
    """返回表归属源；不存在返回 None，多源同名返回 "ambiguous"。"""
    target = str(table_name or "").strip().lower()
    if not target:
        return None
    owners = sorted(source for (source, name) in index if name == target)
    if not owners:
        return None
    if len(owners) > 1:
        logger.warning("多源同名表归属歧义", table=target, owners=owners)
        return "ambiguous"
    return owners[0]
```

- [ ] **Step 4: 运行测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest tests/test_security/test_sql_schema_validation.py -v
```
预期：`5 passed`

---

## Task 2: 作用域感知校验主函数

**Files:**
- Modify: `src/security/sql_schema_validation.py`（追加 `validate_sql_schema` 与全部子函数）
- Test: `tests/test_security/test_sql_schema_validation.py`

**Interfaces:**
- Consumes: Task 1 的 `ValidationIssue`、`build_table_index`、`resolve_table_owner`、`MAX_AVAILABLE_COLUMNS`
- Produces: `validate_sql_schema(sql, *, datasources, dialect="", tables=()) -> list[ValidationIssue]`

- [ ] **Step 1: 写失败的测试（表级 + 限定列）**

追加到 `tests/test_security/test_sql_schema_validation.py`：

```python
class TestUnknownTable:
    """覆盖表级校验（spec/39 §5.1 步骤 4、证据 3）。"""

    # 方法作用：验证引用不存在的表被拦为高置信。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_unknown_table_blocked(self) -> None:
        """不存在的表 → unknown_table，high 置信。"""
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT id FROM ghost_table",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].type == "unknown_table"
        assert issues[0].table == "ghost_table"
        assert issues[0].confidence == "high"

    # 方法作用：验证 CTE 名不被误判为不存在的表。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_cte_name_not_treated_as_unknown_table(self) -> None:
        """CTE 名会出现在 find_all(Table) 中，必须排除。"""
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]
        sql = "WITH x AS (SELECT id FROM orders) SELECT x.id FROM x"

        # Act
        issues = validate_sql_schema(
            sql, datasources=["pg_main"], dialect="postgres", tables=tables
        )

        # Assert
        assert issues == []

    # 方法作用：验证派生表别名不被误判。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_derived_table_alias_not_treated_as_table(self) -> None:
        """派生表别名不出现在 find_all(Table) 中。"""
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "a"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT d.a FROM (SELECT a FROM orders) d",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert issues == []


class TestQualifiedColumn:
    """覆盖带限定符的列引用校验（spec/39 §5.1 步骤 3、§5.4）。"""

    # 方法作用：验证限定符指向真实表时能查出该表缺失的列。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_unknown_column_on_known_table(self) -> None:
        """表存在但列不存在 → unknown_column，high 置信。"""
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]

        # Act
        issues = validate_sql_schema(
            'SELECT "o"."bogus" FROM "orders" AS "o"',
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].type == "unknown_column"
        assert issues[0].column == "bogus"
        assert issues[0].table == "orders"
        assert issues[0].confidence == "high"

    # 方法作用：验证列名在别的表存在但不属于被引用表时判为跨表错配。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_cross_table_column_detected(self) -> None:
        """created_at 只属于 audit_log，却挂在 finance_invoices 的别名上。"""
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [
            {"name": "finance_invoices", "columns": [{"name": "event_date"}]},
            {"name": "audit_log", "columns": [{"name": "created_at"}]},
        ]

        # Act
        issues = validate_sql_schema(
            "SELECT DATE_TRUNC('MONTH', \"i\".\"created_at\") "
            'FROM "finance_invoices" AS "i"',
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].type == "cross_table_column"
        assert issues[0].table == "finance_invoices"
        assert issues[0].provided_by == ("audit_log",)
        assert "不属于表" in issues[0].describe()

    # 方法作用：验证列真属于被引用表时不误报。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_valid_qualified_column_passes(self) -> None:
        """合法列必须放行。"""
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "finance_invoices", "columns": [{"name": "year_month"}]}]

        # Act
        issues = validate_sql_schema(
            'SELECT "i"."year_month" FROM "finance_invoices" AS "i"',
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert issues == []

    # 方法作用：验证限定符指向 CTE 时判为无法判定而非幻觉。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_cte_qualifier_is_not_blocked(self) -> None:
        """CTE 列不在物理表快照中，不得判为幻觉。"""
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]
        sql = "WITH t AS (SELECT id FROM orders) SELECT t.id FROM t"

        # Act
        issues = validate_sql_schema(
            sql, datasources=["pg_main"], dialect="postgres", tables=tables
        )

        # Assert
        assert [item for item in issues if item.confidence == "high"] == []
```

- [ ] **Step 2: 运行测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest tests/test_security/test_sql_schema_validation.py -v -k "UnknownTable or Qualified"
```
预期：FAIL，`ImportError: cannot import name 'validate_sql_schema'`

- [ ] **Step 3: 实现表级与限定列校验**

**先修改文件顶部 import 区**，加入 `import time`（与 `from __future__ import annotations` 之间，标准库组）：

```python
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence
```

然后在 `src/security/sql_schema_validation.py` 末尾追加：

```python
# 方法作用：作用域感知地校验 SQL 引用的表与列是否存在于权威快照。
# Args: sql - 待校验 SQL；datasources - 权威数据源白名单；dialect - 解析方言；tables - 权威快照。
# Returns: 结构化问题清单；空列表表示高置信通过。
def validate_sql_schema(
    sql: str,
    *,
    datasources: Sequence[str],
    dialect: str = "",
    tables: Sequence[Mapping[str, Any]] = (),
) -> list[ValidationIssue]:
    """逐 scope 校验表与列引用；无法判定的场景给 low 置信而不是拦截。"""
    logger.debug(
        "Schema 校验入口",
        sql_chars=len(sql),
        datasource_count=len(datasources),
        table_count=len(tables),
        dialect=dialect,
    )
    _start = time.monotonic()
    default_source = next(
        (str(item).strip() for item in datasources if str(item).strip()), ""
    )
    index = build_table_index(tables, datasources)
    try:
        import sqlglot

        from src.security.sql_execution import normalize_sql_dialect

        # 必须用 parse_one：execute_sql 的方言回归用例 monkeypatch 的即此函数。
        tree = sqlglot.parse_one(sql, read=normalize_sql_dialect(dialect) or None)
    except Exception as exc:
        logger.error("Schema 校验解析失败", error=str(exc), exc_info=True)
        return [ValidationIssue(type="parse_error", datasource=default_source)]
    if tree is None:
        logger.error("Schema 校验解析失败", error="解析结果为空")
        return [ValidationIssue(type="parse_error", datasource=default_source)]

    issues = _collect_unknown_tables(tree, index, default_source)
    issues.extend(_collect_column_issues(tree, index, default_source))
    high_count = sum(1 for item in issues if item.confidence == "high")
    logger.info(
        "Schema 校验完成",
        result="拦截" if high_count else "通过",
        high_count=high_count,
        low_count=len(issues) - high_count,
        elapsed_ms=round((time.monotonic() - _start) * 1000),
    )
    return issues


# 方法作用：收集 SQL 中引用了未知表的问题。
# Args: tree - 已解析语法树；index - 权威索引；default_source - issue 归属源。
# Returns: unknown_table 问题列表（多源同名表为 low 置信）。
def _collect_unknown_tables(
    tree: Any,
    index: Mapping[tuple[str, str], frozenset[str]],
    default_source: str,
) -> list[ValidationIssue]:
    """校验 FROM / JOIN 引用的物理表是否存在；CTE 名必须排除。"""
    from sqlglot import exp

    issues: list[ValidationIssue] = []
    # 证据 3：CTE 名会出现在 find_all(exp.Table) 中，必须显式排除，否则被误判为幻觉表。
    cte_names = {
        str(cte.alias_or_name).strip().lower()
        for cte in tree.find_all(exp.CTE)
        if cte.alias_or_name
    }
    seen: set[str] = set()
    for node in tree.find_all(exp.Table):
        name = str(node.name or "").strip().lower()
        if not name or name in cte_names or name in seen:
            continue
        seen.add(name)
        owner = resolve_table_owner(index, name)
        if owner == "ambiguous":
            logger.warning("表引用无法判定", reason="ambiguous_table", table=name)
            issues.append(
                ValidationIssue(
                    type="unknown_table",
                    datasource=default_source,
                    table=name,
                    confidence="low",
                )
            )
        elif owner is None:
            logger.warning("表引用不存在", table=name)
            issues.append(
                ValidationIssue(type="unknown_table", datasource=default_source, table=name)
            )
    return issues


# 方法作用：逐作用域校验列引用的归属。
# Args: tree - 已解析语法树；index - 权威索引；default_source - issue 归属源。
# Returns: 列级问题列表。
def _collect_column_issues(
    tree: Any,
    index: Mapping[tuple[str, str], frozenset[str]],
    default_source: str,
) -> list[ValidationIssue]:
    """按 scope 校验限定列与未限定列，并标记 SELECT * 盲区。"""
    from sqlglot import exp
    from sqlglot.optimizer.scope import traverse_scope

    issues: list[ValidationIssue] = []
    for scope in traverse_scope(tree):
        # 证据 1：CTE 与派生表在 sources 中是 Scope 类型，天然被排除，不会误判为物理表。
        local_tables = {
            str(alias).lower(): str(node.name or "").lower()
            for alias, node in scope.sources.items()
            if isinstance(node, exp.Table)
        }
        has_cte_or_derived = len(local_tables) < len(scope.sources)
        owners = {
            table: resolve_table_owner(index, table) for table in local_tables.values()
        }
        resolved = {
            table: owner
            for table, owner in owners.items()
            if owner is not None and owner != "ambiguous"
        }
        # 证据 2 + 4：星号不进入 scope.columns，必须直接检查 SELECT 投影顶层，
        # 且不能用 find_all(exp.Star)（会把 COUNT(*) 误报为无法校验）。
        for select in scope.expression.find_all(exp.Select):
            for projection in select.expressions:
                if _is_star_projection(projection):
                    logger.warning("列引用无法判定", reason="select_star")
                    issues.append(
                        ValidationIssue(
                            type="unknown_column",
                            datasource=default_source,
                            column="*",
                            confidence="low",
                        )
                    )
        seen: set[tuple[str, str]] = set()
        for column in scope.columns:
            column_name = str(column.name or "").strip().lower()
            qualifier = str(column.table or "").strip().lower()
            if not column_name or (qualifier, column_name) in seen:
                continue
            seen.add((qualifier, column_name))
            if qualifier:
                issues.extend(
                    _check_qualified_column(
                        column_name, qualifier, local_tables, owners, index
                    )
                )
            else:
                issues.extend(
                    _check_unqualified_column(
                        column_name, resolved, len(local_tables), has_cte_or_derived, index
                    )
                )
    return issues


# 方法作用：判断投影表达式是否为星号（SELECT * 或 SELECT t.*），排除函数参数内的星号。
# Args: node - SELECT 投影表达式节点。
# Returns: 是星号投影返回 True。
def _is_star_projection(node: Any) -> bool:
    """只认投影顶层星号，COUNT(*) 属函数参数不算。"""
    from sqlglot import exp

    if isinstance(node, exp.Star):
        return True
    if isinstance(node, exp.Alias):
        return _is_star_projection(node.this)
    if isinstance(node, exp.Column) and isinstance(node.this, exp.Star):
        return True
    return False


# 方法作用：校验带限定符的列是否属于限定符指向的真实表。
# Args: column_name - 列名（小写）；qualifier - 限定符（小写）；local_tables - 别名 -> 真实表；
#       owners - 真实表 -> 归属源；index - 权威索引。
# Returns: 问题列表；限定符指向 CTE 或归属不明时返回空。
def _check_qualified_column(
    column_name: str,
    qualifier: str,
    local_tables: Mapping[str, str],
    owners: Mapping[str, str | None],
    index: Mapping[tuple[str, str], frozenset[str]],
) -> list[ValidationIssue]:
    """按表归属校验限定列，跨表错配与未知列分别成 issue。"""
    real_table = local_tables.get(qualifier)
    if real_table is None:
        # 限定符指向 CTE 或派生表：无法判定，放行并记 warning。
        logger.warning("列引用无法判定", reason="cte_qualifier", qualifier=qualifier)
        return []
    owner = owners.get(real_table)
    if owner is None or owner == "ambiguous":
        return []
    columns = index.get((owner, real_table), frozenset())
    if column_name in columns:
        return []
    providers = sorted(
        table
        for (source, table) in index
        if source == owner and column_name in index[(source, table)]
    )
    available = tuple(sorted(columns)[:MAX_AVAILABLE_COLUMNS])
    if providers:
        logger.warning(
            "列归属错配拦截",
            table=real_table,
            column=column_name,
            qualifier=qualifier,
            provided_by=providers[:5],
        )
        return [
            ValidationIssue(
                type="cross_table_column",
                datasource=owner,
                table=real_table,
                column=column_name,
                qualifier=qualifier,
                provided_by=tuple(providers[:10]),
                available_columns=available,
            )
        ]
    logger.warning("列引用不存在", table=real_table, column=column_name, qualifier=qualifier)
    return [
        ValidationIssue(
            type="unknown_column",
            datasource=owner,
            table=real_table,
            column=column_name,
            qualifier=qualifier,
            available_columns=available,
        )
    ]


# 方法作用：校验未限定列的归属，区分唯一命中、歧义与真幻觉。
# Args: column_name - 列名（小写）；resolved - 归属已确定的表 -> 源；
#       total_table_count - 本 scope 物理表总数；has_cte_or_derived - 是否存在 CTE 或派生表；
#       index - 权威索引。
# Returns: 问题列表；唯一命中或无法判定时返回空。
def _check_unqualified_column(
    column_name: str,
    resolved: Mapping[str, str],
    total_table_count: int,
    has_cte_or_derived: bool,
    index: Mapping[tuple[str, str], frozenset[str]],
) -> list[ValidationIssue]:
    """未限定列按本 scope 物理表查找归属。"""
    hits = sorted(
        table
        for table, owner in resolved.items()
        if column_name in index.get((owner, table), frozenset())
    )
    if len(hits) == 1:
        return []
    if len(hits) > 1:
        logger.warning("列引用歧义拦截", column=column_name, candidate_tables=hits[:5])
        return [
            ValidationIssue(
                type="ambiguous_column",
                datasource=resolved[hits[0]],
                table=", ".join(hits[:5]),
                column=column_name,
            )
        ]
    # 零命中：只有本 scope 全是物理表、且归属全部确定时，才能判为真幻觉。
    # 否则列可能来自 CTE / 派生表，或来自归属不明的表 → 无法判定。
    if has_cte_or_derived or len(resolved) != total_table_count or not resolved:
        logger.warning(
            "列引用无法判定",
            reason="cte_or_unresolved",
            column=column_name,
            resolved_count=len(resolved),
            total_table_count=total_table_count,
        )
        return []
    table, owner = next(iter(resolved.items()))
    logger.warning("列引用不存在", table=table, column=column_name, unqualified=True)
    return [
        ValidationIssue(
            type="unknown_column",
            datasource=owner,
            table=table,
            column=column_name,
            available_columns=tuple(
                sorted(index.get((owner, table), frozenset()))[:MAX_AVAILABLE_COLUMNS]
            ),
        )
    ]
```

- [ ] **Step 4: 运行测试确认通过（表级 + 限定列）**

```bash
.venv/Scripts/python.exe -m pytest tests/test_security/test_sql_schema_validation.py -v
```
预期：`13 passed`

- [ ] **Step 5: 写失败的测试（未限定列 + 多源隔离）**

追加到 `tests/test_security/test_sql_schema_validation.py`：

```python
class TestUnqualifiedColumn:
    """覆盖未限定列的归属判定（spec/39 §5.3）。"""

    # 方法作用：验证单表场景下未限定列能正确匹配。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_unqualified_single_table_passes(self) -> None:
        """单表未限定列命中即通过。"""
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "amount"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT amount FROM orders",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert issues == []

    # 方法作用：验证未限定列在多表同时存在时判为歧义列。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_unqualified_ambiguous_column(self) -> None:
        """id 在 orders 与 customers 同时存在 → ambiguous_column。"""
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [
            {"name": "orders", "columns": [{"name": "id"}, {"name": "cid"}]},
            {"name": "customers", "columns": [{"name": "id"}, {"name": "name"}]},
        ]

        # Act
        issues = validate_sql_schema(
            "SELECT id FROM orders o JOIN customers c ON o.cid = c.id",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].type == "ambiguous_column"
        assert issues[0].column == "id"

    # 方法作用：验证未限定列在纯物理表作用域中零命中时判为幻觉。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_unqualified_zero_hit_is_hallucination(self) -> None:
        """全物理表 scope 中找不到该列 → unknown_column，high 置信。"""
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "amount"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT bogus FROM orders",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].type == "unknown_column"
        assert issues[0].confidence == "high"
        assert issues[0].column == "bogus"

    # 方法作用：验证含 CTE 时未限定列零命中降级为低置信，不拦截。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_zero_hit_with_cte_is_low_confidence(self) -> None:
        """含 CTE 时零命中可能来自派生列，不得拦截。"""
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "amount"}]}]
        sql = "WITH t AS (SELECT amount AS derived FROM orders) SELECT derived FROM t"

        # Act
        issues = validate_sql_schema(
            sql, datasources=["pg_main"], dialect="postgres", tables=tables
        )

        # Assert
        assert [item for item in issues if item.confidence == "high"] == []


class TestKnownBlindSpots:
    """覆盖 spec/39 §10 盲区必须显式标记而非静默通过。"""

    # 方法作用：验证 SELECT * 产出低置信标记而不是「校验通过」。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_select_star_is_marked_low_confidence(self) -> None:
        """星号无法展开，必须留痕。"""
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT * FROM orders",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].confidence == "low"
        assert issues[0].column == "*"

    # 方法作用：验证 COUNT(*) 不被误判为星号盲区（证据 2）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_count_star_is_not_flagged_as_blind_spot(self) -> None:
        """COUNT(*) 的星号在函数参数内，属合法用法。"""
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT COUNT(*) FROM orders",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert issues == []

    # 方法作用：验证 SELECT t.* 被标记。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_qualified_star_is_marked(self) -> None:
        """t.* 同样是无法展开的星号投影。"""
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [{"name": "orders", "columns": [{"name": "id"}]}]

        # Act
        issues = validate_sql_schema(
            "SELECT t.* FROM orders t",
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].column == "*"
        assert issues[0].confidence == "low"


class TestMultiSourceIsolation:
    """覆盖多源隔离契约（spec/39 §4.4）。"""

    # 方法作用：验证跨源同名表不串味，判为不可判定而非幻觉。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_same_table_name_across_sources(self) -> None:
        """a 源 orders 只有 id；b 源 orders 只有 amount。"""
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [
            {"name": "orders", "datasource": "pg_a", "columns": [{"name": "id"}]},
            {"name": "orders", "datasource": "pg_b", "columns": [{"name": "amount"}]},
        ]

        # Act
        issues = validate_sql_schema(
            'SELECT "o"."amount" FROM "orders" AS "o"',
            datasources=["pg_a", "pg_b"],
            dialect="postgres",
            tables=tables,
        )

        # Assert：同名歧义 → 归属不可判定 → 低置信，不得拦成幻觉
        assert [item for item in issues if item.confidence == "high"] == []

    # 方法作用：验证未授权源的表不参与校验，不作为「列存在」的证据。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_unauthorized_source_column_not_evidence(self) -> None:
        """pg_other 的列不得让 pg_main 的引用通过校验。"""
        from src.security.sql_schema_validation import validate_sql_schema

        # Arrange
        tables = [
            {"name": "orders", "datasource": "pg_main", "columns": [{"name": "id"}]},
            {"name": "orders", "datasource": "pg_other", "columns": [{"name": "secret_col"}]},
        ]

        # Act
        issues = validate_sql_schema(
            'SELECT "o"."secret_col" FROM "orders" AS "o"',
            datasources=["pg_main"],
            dialect="postgres",
            tables=tables,
        )

        # Assert
        assert len(issues) == 1
        assert issues[0].type == "unknown_column"
        assert issues[0].datasource == "pg_main"
```

- [ ] **Step 6: 运行测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest tests/test_security/test_sql_schema_validation.py -v
```
预期：`22 passed`。若任一失败，按“实测证据”核对实现写法（最常见错误是星号判定与 CTE 排除）。

- [ ] **Step 7: 跑一次方言与解析失败回归**

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph/test_execute_column_validation.py -v
```
预期：`7 passed`（新模块尚未接入，仅确认未被破坏）

---

## Task 3: 指标计数接入

**Files:**
- Modify: `src/observability.py:27-111`
- Modify: `src/security/sql_schema_validation.py`
- Test: `tests/test_security/test_sql_schema_validation.py`、`tests/test_observability.py`

**Interfaces:**
- Consumes: `src.observability.get_metrics_registry`
- Produces: `MetricsRegistry.record_schema_validation(issue_type, confidence, dialect, stage) -> None`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_security/test_sql_schema_validation.py`：

```python
class TestValidationMetrics:
    """覆盖校验指标计数（spec/39 §9）。"""

    # 方法作用：验证指标方法存在且标签为低基数类型。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_record_schema_validation(self) -> None:
        """标签仅允许 issue_type / confidence / dialect / stage。"""
        from src.observability import MetricsRegistry

        # Arrange
        registry = MetricsRegistry()

        # Act
        registry.record_schema_validation(
            issue_type="unknown_column",
            confidence="high",
            dialect="postgres",
            stage="stage1",
        )

        # Assert
        assert b"data_agent_sql_schema_validation_total" in registry.render()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest tests/test_security/test_sql_schema_validation.py -v -k Metrics
```
预期：FAIL，`AttributeError: 'MetricsRegistry' object has no attribute 'record_schema_validation'`

- [ ] **Step 3: 写最小实现**

在 `src/observability.py` 的 `MetricsRegistry.__init__` 中，`self.schema_warnings` 附近追加：

```python
        self.sql_schema_validation = Counter(
            "data_agent_sql_schema_validation_total",
            "SQL Schema 校验问题总数",
            ("issue_type", "confidence", "dialect", "stage"),
            registry=self.registry,
        )
```

在 `record_schema_warning`（`:107-111`）之后追加：

```python
    # 方法作用：记录一次 SQL Schema 校验问题，标签严格限制为低基数字段。
    # Args: self - 当前注册器；issue_type - 问题类型；confidence - 置信度；
    #       dialect - 数据源方言；stage - 校验阶段。
    # Returns: 无返回值。
    def record_schema_validation(
        self,
        issue_type: str,
        confidence: str,
        dialect: str,
        stage: str,
    ) -> None:
        self.sql_schema_validation.labels(
            str(issue_type or "unknown")[:32],
            str(confidence or "unknown")[:16],
            str(dialect or "unknown")[:32],
            str(stage or "unknown")[:16],
        ).inc()
```

在 `src/security/sql_schema_validation.py` 的 `validate_sql_schema` 中，`high_count = ...` 之前插入上报：

```python
    for item in issues:
        try:
            from src.observability import get_metrics_registry

            get_metrics_registry().record_schema_validation(
                issue_type=item.type,
                confidence=item.confidence,
                dialect=dialect,
                stage="stage1",
            )
        except Exception as exc:
            # 指标不可用不得影响校验主流程
            logger.debug("校验指标上报跳过", error=str(exc))
```

- [ ] **Step 4: 运行测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest tests/test_security/test_sql_schema_validation.py tests/test_observability.py -v
```
预期：全部 passed

- [ ] **Step 5: 阶段 A 收尾回归**

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph/ tests/test_security/ -v
```
预期：全部 passed。此时**未改动任何调用方**，现网行为与改动前一致。

---

## Task 4: `layer3_validate` 接入 + 两阶段取数

**Files:**
- Modify: `src/graph/nodes/layer3_validate.py:47-61`（并新增 `_recheck_with_full_snapshot`）
- Test: `tests/test_graph/test_layer3_schema_validation.py`（新建）、既有 `tests/test_graph/test_execute_column_validation.py`

**Interfaces:**
- Consumes: `validate_sql_schema`、`ValidationIssue.describe()`
- Produces: `validation_errors = [{"type": "column_reference", "message": str, "issues": list[dict]}]`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_graph/test_layer3_schema_validation.py`：

```python
"""layer3 两阶段 Schema 校验回归测试（spec/39 §6）。"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class TestLayer3TwoStageValidation:
    """覆盖两阶段取数与阶段二短路条件。"""

    # 方法作用：验证无数据源时阶段二被短路，不尝试连库。
    # Args: self - pytest 用例实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_stage_two_skipped_without_datasource(self, monkeypatch) -> None:
        """最小 state（无数据源）必须快速返回，不调用 SchemaManager。"""
        logger.debug("test_stage_two_skipped_without_datasource 入口")
        from src.graph.nodes.layer3_validate import layer3_validate_node

        # Arrange：若阶段二未被短路，下面的替身会让用例失败
        def _boom(*args, **kwargs):
            raise AssertionError("阶段二不应在无数据源时被调用")

        monkeypatch.setattr(
            "src.knowledge.schema_manager.get_schema_manager", _boom, raising=False
        )
        state = {
            "generated_sql": 'SELECT "i"."created_at" FROM "finance_invoices" AS "i"',
            "relevant_tables": [
                {"name": "finance_invoices", "columns": [{"name": "year_month"}]}
            ],
        }

        # Act
        result = asyncio.run(layer3_validate_node(state))

        # Assert
        assert result["sql_valid"] is False
        assert result["validation_errors"][0]["type"] == "column_reference"
        assert "created_at" in result["validation_errors"][0]["message"]
        logger.info("test_stage_two_skipped_without_datasource 完成")

    # 方法作用：验证拦截结果携带结构化 issues 供回喂重试使用。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_validation_errors_carry_structured_issues(self) -> None:
        """issues 必须含 table / available_columns，否则无法生成有效回喂。"""
        from src.graph.nodes.layer3_validate import layer3_validate_node

        # Arrange
        state = {
            "generated_sql": 'SELECT "i"."created_at" FROM "finance_invoices" AS "i"',
            "relevant_tables": [
                {
                    "name": "finance_invoices",
                    "columns": [{"name": "event_date"}, {"name": "year_month"}],
                }
            ],
        }

        # Act
        result = asyncio.run(layer3_validate_node(state))

        # Assert
        issues = result["validation_errors"][0]["issues"]
        assert issues[0]["table"] == "finance_invoices"
        assert "event_date" in issues[0]["available_columns"]
```

- [ ] **Step 2: 运行测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph/test_layer3_schema_validation.py -v
```
预期：FAIL，`KeyError: 'issues'`

- [ ] **Step 3: 写最小实现**

替换 `src/graph/nodes/layer3_validate.py` 的 `:47-61`：

```python
    # 列引用校验必须早于 layer4_explain：EXPLAIN 会直接把 SQL 打到数据库，
    # 幻觉列在那里只能拿到原始 DB 报错，模型看不到可用列清单，重试只能继续猜。
    if not errors:
        from src.security.sql_schema_validation import validate_sql_schema

        datasource = contexts.routing.datasource or ""
        dialect_value = contexts.execution.dialect or ""
        issues = validate_sql_schema(
            sql,
            datasources=[datasource] if datasource else [],
            dialect=dialect_value,
            tables=state.get("relevant_tables", []) or [],
        )
        high_issues = [item for item in issues if item.confidence == "high"]

        # 阶段二复核：阶段一的基准可能被 _filter_relevant_tables 裁剪过（单源问答链路必然裁剪），
        # high 置信问题不可直接上报，必须用权威全量快照再判一次，避免误杀合法 SQL。
        if high_issues and datasource:
            confirmed = await _recheck_with_full_snapshot(
                sql, datasource, dialect_value, high_issues
            )
            if confirmed is not None:
                high_issues = confirmed

        if high_issues:
            message = "; ".join(item.describe() for item in high_issues[:3])
            errors = [
                {
                    "type": "column_reference",
                    "message": message,
                    "issues": [
                        {
                            "type": item.type,
                            "datasource": item.datasource,
                            "table": item.table,
                            "column": item.column,
                            "qualifier": item.qualifier,
                            "provided_by": list(item.provided_by),
                            "available_columns": list(item.available_columns),
                        }
                        for item in high_issues
                    ],
                }
            ]
            logger.warning(
                "列名校验拦截",
                dialect=dialect_value,
                high_issue_count=len(high_issues),
                first_issue=high_issues[0].type,
            )
```

在文件末尾追加阶段二函数：

```python
# 方法作用：用权威全量快照复核阶段一的高置信问题，消除裁剪造成的假阳性。
# Args: sql - 待校验 SQL；datasource - 数据源名；dialect - 方言；high_issues - 阶段一问题。
# Returns: 复核后仍存在的问题列表；复核不可用时返回 None（回落阶段一结果）。
async def _recheck_with_full_snapshot(
    sql: str,
    datasource: str,
    dialect: str,
    high_issues: list,
) -> list | None:
    """失败安全：任何异常都回落阶段一结果，不因复核失败阻断链路。"""
    logger.debug("阶段二复核入口", datasource=datasource, high_issue_count=len(high_issues))
    try:
        from src.knowledge.schema_manager import get_schema_manager

        snapshot = await get_schema_manager().get_or_fetch_schema(datasource, user_query="")
        tables = [
            {
                "name": table.name,
                "columns": [{"name": column.name} for column in table.columns],
            }
            for table in (snapshot.tables if snapshot else [])
        ]
        if not tables:
            logger.warning("阶段二复核跳过", reason="全量快照为空", datasource=datasource)
            return None
        from src.security.sql_schema_validation import validate_sql_schema

        issues = validate_sql_schema(
            sql, datasources=[datasource], dialect=dialect, tables=tables
        )
        confirmed = [item for item in issues if item.confidence == "high"]
        logger.info(
            "阶段二复核完成",
            datasource=datasource,
            before=len(high_issues),
            after=len(confirmed),
        )
        return confirmed
    except Exception as exc:
        logger.warning(
            "阶段二复核失败，回落阶段一结果",
            datasource=datasource,
            error=str(exc),
            exc_info=True,
        )
        return None
```

- [ ] **Step 4: 运行测试确认通过（含既有 7 个回归用例）**

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph/test_layer3_schema_validation.py tests/test_graph/test_execute_column_validation.py -v
```
预期：全部 passed

---

## Task 5: `execute_sql` 适配器薄调用

**Files:**
- Modify: `src/graph/nodes/execute_sql.py:263-442`
- Test: `tests/test_graph/test_execute_column_validation.py`

**Interfaces:**
- Consumes: `validate_sql_schema`、`build_table_index`
- Produces: `_validate_column_references(sql, tables, dialect) -> str | None`、`find_cross_table_columns(tree, tables, aliases) -> list[dict]`（签名与返回结构均不变）

- [ ] **Step 1: 确认既有测试当前通过（基线）**

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph/test_execute_column_validation.py -v
```
预期：`7 passed`

- [ ] **Step 2: 改造实现**

把 `src/graph/nodes/execute_sql.py` 的 `_validate_column_references` 函数体替换为委托（保留原注释与签名）：

```python
# 方法作用：执行前验证 SQL 中的列引用是否都存在于表结构中（最后防线，委托统一校验器）。
# Args: sql - 待验证 SQL；tables - relevant_tables 列表；dialect - 数据库方言。
# Returns: 错误消息字符串，None 表示通过。
def _validate_column_references(
    sql: str,
    tables: list[dict],
    dialect: str = "",
) -> str | None:
    """委托 src.security.sql_schema_validation，保留原签名与错误消息契约。"""
    logger.debug("列引用校验入口", dialect=dialect, table_count=len(tables))
    from src.security.sql_schema_validation import validate_sql_schema

    try:
        issues = validate_sql_schema(sql, datasources=[""], dialect=dialect, tables=tables)
    except Exception as exc:
        logger.error("列引用校验解析失败", error=str(exc), exc_info=True)
        return "列名校验解析失败"
    high_issues = [item for item in issues if item.confidence == "high"]
    if not high_issues:
        logger.info("列引用校验完成", result="通过")
        return None
    result = "列名校验失败: " + "; ".join(item.describe() for item in high_issues[:3])
    logger.info("列引用校验完成", result="失败", error_count=len(high_issues))
    return result
```

把 `find_cross_table_columns` 替换为保留 dict 契约的适配器（下方刻意复用新模块的 `_collect_column_issues`，以便同一套作用域解析逻辑只存在一份；这是项目内模块间复用，不是对外 API）：

```python
# 方法作用：找出「列名在全局白名单存在、却不属于本语句引用表」的错配列（兼容适配器）。
# Args: tree - 已解析的 SQL 语法树；tables - 运行时表结构；aliases - SELECT 中定义的别名。
# Returns: 每个错配列的 column / referenced_table / provided_by 摘要；结构与旧实现一致。
def find_cross_table_columns(
    tree: Any, tables: list[dict], aliases: set[str]
) -> list[dict[str, Any]]:
    """保留旧的 dict 契约，判定逻辑委托新模块。"""
    from src.security.sql_schema_validation import (
        build_table_index,
        _collect_column_issues,
    )

    logger.debug("跨表列错配检测入口", table_count=len(tables))
    index = build_table_index(tables, [""])
    by_table: dict[str, int] = {}
    for (_source, name) in index:
        by_table[name] = by_table.get(name, 0) + 1
    result: list[dict[str, Any]] = []
    for issue in _collect_column_issues(tree, index, ""):
        if issue.type != "cross_table_column":
            continue
        qualifier = issue.qualifier
        providers = list(issue.provided_by)
        result.append(
            {
                "column": f"{qualifier}.{issue.column}" if qualifier else issue.column,
                "referenced_table": issue.table,
                "provided_by": providers[:10],
                "provider_count": len(providers),
                "schema_table_count": len(by_table),
            }
        )
    logger.info("跨表列错配检测完成", mismatch_count=len(result))
    return result
```

删除 `_columns_by_table` 与 `_alias_to_table`，并确认无残留引用：

```bash
grep -rn "_columns_by_table\|_alias_to_table" src/ tests/
```
预期：无输出（若仍有引用，改为调用 `src.security.sql_schema_validation.build_table_index`）

- [ ] **Step 3: 运行既有回归测试**

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph/test_execute_column_validation.py -v
```
预期：`7 passed`。重点核对：
- `test_mssql_uses_tsql_parser` — 校验器内部使用 `parse_one` 且传 `read="tsql"`
- `test_cross_table_column_probe` — `find_cross_table_columns` 返回 `column == "i.created_at"`、`provided_by == ["audit_log"]`
- `test_cross_table_column_is_rejected` — 消息含 `不属于表` 与 `orders`

- [ ] **Step 4: 跑 graph 与 security 全量回归**

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph/ tests/test_security/ -v
```
预期：全部 passed

---

## Task 6: 重试回喂契约改造（不回注失败 SQL）

**Files:**
- Modify: `src/graph/nodes/generate_sql.py:356-391`
- Test: `tests/test_graph/test_generate_sql_retry_context.py`（新建）

**Interfaces:**
- Consumes: `validation_errors[*]["issues"]`（Task 4 产出）
- Produces: `build_retry_context(state, retry, tables) -> str`，对 `column_reference` 不回注失败 SQL

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_graph/test_generate_sql_retry_context.py`：

```python
"""重试上下文回喂契约测试（spec/39 §8）。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class TestRetryContextSchemaHallucination:
    """覆盖 schema 幻觉不回注失败 SQL 的契约。"""

    # 方法作用：验证列幻觉分支不回注失败 SQL，且给出可用列清单。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_column_reference_does_not_echo_failed_sql(self) -> None:
        """失败的 SQL 不得被回注，否则会强化幻觉。"""
        logger.debug("test_column_reference_does_not_echo_failed_sql 入口")
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange
        failed_sql = 'SELECT "i"."created_at" FROM "finance_invoices" AS "i"'
        state = {
            "generated_sql": failed_sql,
            "validation_errors": [
                {
                    "type": "column_reference",
                    "message": "列 'i.created_at' 不属于表 finance_invoices",
                    "issues": [
                        {
                            "type": "cross_table_column",
                            "datasource": "pg_main",
                            "table": "finance_invoices",
                            "column": "created_at",
                            "qualifier": "i",
                            "provided_by": ["orders"],
                            "available_columns": ["event_date", "year_month"],
                        }
                    ],
                }
            ],
        }

        # Act
        context = build_retry_context(state, 1, [{"name": "finance_invoices"}])

        # Assert
        assert failed_sql not in context
        assert "event_date" in context
        assert "year_month" in context
        logger.info("test_column_reference_does_not_echo_failed_sql 完成")

    # 方法作用：验证无 issues 结构时退回原有行为，不破坏既有调用方。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_legacy_column_reference_without_issues_falls_back(self) -> None:
        """旧格式 validation_errors 不崩溃。"""
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange
        state = {
            "generated_sql": "SELECT amount FROM orders",
            "validation_errors": [
                {"type": "column_reference", "message": "列名校验失败: 列 'x' 不在可用列中"}
            ],
        }

        # Act
        context = build_retry_context(state, 1, [{"name": "orders"}])

        # Assert
        assert isinstance(context, str)
        assert context
```

- [ ] **Step 2: 运行测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph/test_generate_sql_retry_context.py -v
```
预期：FAIL，`assert failed_sql not in context`（当前走 `:378-391` 分支会回注 SQL）

- [ ] **Step 3: 写最小实现**

在 `src/graph/nodes/generate_sql.py` 的 `build_retry_context` 中，在 hallucination 分支（`return (...)` 结束处）之后、`previous_sql = ...` 之前插入：

```python
    # schema 幻觉（字段引用错误）：与表名幻觉同策略——不回注失败 SQL，
    # 只回喂「该表实际可用的列」，避免模型照抄错误 SQL 就地打补丁。
    schema_issues = [
        issue
        for error in validation_errors
        if isinstance(error, dict) and error.get("type") == "column_reference"
        for issue in (error.get("issues") or [])
        if isinstance(issue, dict)
    ]
    if schema_issues:
        lines = [f"## 第 {retry} 次生成：纠正字段引用错误"]
        grouped: dict[str, list[str]] = {}
        for issue in schema_issues:
            table = str(issue.get("table", "") or "").strip()
            datasource = str(issue.get("datasource", "") or "").strip()
            key = f"{datasource}.{table}" if datasource else table
            column = str(issue.get("column", "") or "").strip()
            qualifier = str(issue.get("qualifier", "") or "").strip()
            reference = f"{qualifier}.{column}" if qualifier else column
            provided = ", ".join(issue.get("provided_by") or []) or "未知"
            lines.append(f"上一轮引用了不存在的字段：{key}.{reference}（该列属于: {provided}）")
            available = [str(item) for item in (issue.get("available_columns") or [])]
            if available and key not in grouped:
                grouped[key] = available
        for key, available in grouped.items():
            lines.append(f"表 {key} 实际可用列：{', '.join(available)}")
        lines.append("禁止复用上一轮 SQL，必须仅基于上述列重新生成。")
        if retry >= 2:
            lines.append("这是最后一次尝试，必须使用上述明确列出的标识符。")
        return "\n".join(lines)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph/test_generate_sql_retry_context.py -v
```
预期：`2 passed`

- [ ] **Step 5: 跑重试相关回归**

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph/ -v -k "generate_sql or retry or hallucination"
```
预期：全部 passed

---

## Task 7: `_check_table_hallucination` 收敛到统一实现

**Files:**
- Modify: `src/graph/nodes/generate_sql.py:846-905`
- Test: 既有覆盖表名幻觉的用例

**Interfaces:**
- Consumes: `build_table_index`、`resolve_table_owner`
- Produces: `_check_table_hallucination(sql, tables, dialect) -> list[str]`（签名与返回类型不变）

- [ ] **Step 1: 记录当前行为基线**

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph/ -v -k "hallucination or 幻觉" 2>&1 | tail -5
```
记录通过数量；改造后必须一致。

- [ ] **Step 2: 改造实现**

把 `_check_table_hallucination` 中「构建 `known` / `known_bases` + 逐表比对」的循环体替换为统一判定（保留函数签名、`if not tables: return []` 短路、以及解析失败返回 `["SQL 解析失败，已阻断执行"]` 的失败关闭语义）：

```python
    from src.security.sql_schema_validation import build_table_index, resolve_table_owner

    index = build_table_index(tables, [""])
    unknown: list[str] = []
    parser_dialect = normalize_sql_dialect(dialect)
    logger.debug(
        "表名幻觉校验边界输入",
        known_tables=sorted({t["name"].lower() for t in tables if t.get("name")}),
        parser_dialect=parser_dialect or "default",
        sql=sql,
    )
    try:
        import sqlglot
        from sqlglot import exp

        parsed = sqlglot.parse(sql, read=parser_dialect or None)
        for statement in parsed:
            if statement is None:
                continue
            cte_names = {
                str(cte.alias_or_name).lower()
                for cte in statement.find_all(exp.CTE)
                if cte.alias_or_name
            }
            for node in statement.find_all(exp.Table):
                name = str(node.name or "").strip().lower()
                if not name or name in cte_names or name in unknown:
                    continue
                # 统一判定：归属唯一即合法；不存在或多源歧义才算幻觉候选
                if resolve_table_owner(index, name) is None:
                    unknown.append(name)
    except Exception as exc:
        logger.error("表名幻觉校验解析失败", error=str(exc), exc_info=True)
        return ["SQL 解析失败，已阻断执行"]
    logger.info("表名幻觉校验完成", unknown_count=len(unknown), unknown=unknown[:10])
    return unknown
```

> **注意**：此函数保持 `sqlglot.parse`（复数）不变——它的既有测试不依赖 `parse_one`；`parse_one` 的要求仅适用于 `src/security/sql_schema_validation.py`。
>
> **不要"恢复"旧哨兵值**：本计划早期版本此处写的是 `__parse_failed__`，该字符串在仓库中**并不存在**；真实失败关闭哨兵是 `["SQL 解析失败，已阻断执行"]`（`src/graph/nodes/generate_sql.py`，由 `tests/test_graph/test_generate_sql_hallucination.py::test_parser_failure_is_closed` 以 `"解析失败" in result[0]` 断言）。改回 `__parse_failed__` 会让失败关闭语义失效、该用例变红。
>
> **白名单入参同理**：片段里的 `build_table_index(tables, [""])` 是计划初稿写法，实装已收敛为 `build_table_index(tables, ["__local__"])`（与 `execute_sql._validate_column_references` 同一约定）——空白名单会在每次 SQL 生成时打「未提供数据源白名单」告警，不要改回 `[""]`。

- [ ] **Step 3: 运行回归确认行为一致**

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph/ -v -k "hallucination or 幻觉"
```
预期：与 Step 1 记录的数量一致。

- [ ] **Step 4: 跑 graph 全量回归**

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph/ -v
```
预期：全部 passed

---

## Task 8: 文档与功能清单同步

**Files:**
- Modify: `features/26-sql-schema-validation.md`
- Modify: `features/04-graph.md`（4.5.1、4.7.3）
- Modify: `CODE_GUIDE.md`（视 Step 1 判断）

- [ ] **Step 1: 判断是否需要更新 CODE_GUIDE.md**

按项目规则：本次**新增模块**（`src/security/sql_schema_validation.py`）→ 需更新目录结构与业务含义；layer3 行为变化（两阶段复核）→ 需更新节点说明。

```bash
grep -n "security/" CODE_GUIDE.md | head -20
```

- [ ] **Step 2: 更新 features/26-sql-schema-validation.md**

把 26.1.1 - 26.3.5 的状态从「待开发」改为「单测完成」。

- [ ] **Step 3: 更新 features/04-graph.md**

在 4.5.1 与 4.7.3 的描述中补充：列校验已收敛到 `src/security/sql_schema_validation.py`；layer3 采用两阶段取数（阶段一用 `relevant_tables` 零 IO 校验，出现 high 置信问题才用全量快照复核）；`execute_sql` 保留兼容适配器（`_validate_column_references`、`find_cross_table_columns`）。

- [ ] **Step 4: 更新 CODE_GUIDE.md**

按 Step 1 的判断，在目录结构中补充新模块，并在节点说明中更新 layer3 的行为描述。

- [ ] **Step 5: 最终全量回归**

```bash
.venv/Scripts/python.exe -m pytest tests/test_graph/ tests/test_security/ tests/test_reporting/ -v
```
预期：全部 passed

---

## 遗留与未覆盖项（不在本计划范围）

| 项 | 原因 |
|----|------|
| 命名空间（schema/database）支持 | 快照 `TableSchema.name` 不含命名空间；`tables` 已预留可选 `schema` 字段 |
| 方言特有表函数 / `UNNEST` / `LATERAL` | 需按方言实测补充 |
| 快照与真实库漂移 | 独立议题 |
| 方案 C（执行边界兜底） | 待本计划完成后评估 |
| 跨源关联查询 | `datasources` 多源形状已预留；实施时需确认联邦方言策略（决策 D1 备选） |

---

## Self-Review 记录

- **Spec 覆盖**：spec/39 §4.2（Task 1）、§4.3（Task 1）、§4.4（Task 2 Step 5）、§5.1（Task 2）、§5.3（Task 2 Step 5）、§5.4（Task 2 各置信度断言）、§6（Task 4）、§7.1（Task 4）、§7.2（Task 5）、§7.3（Task 7）、§7.4（Task 5 删除两个工具函数；`_schema_table_columns` 明确不改）、§8（Task 6）、§9（Task 3）、§10（Task 2 Step 5 的 `TestKnownBlindSpots`）、§11（各 Task 测试）
- **实测证据已内联**：星号判定（证据 2）、CTE 排除（证据 3）、`scope.columns` 为空（证据 4）、`parse_one` 强制要求（Global Constraints）
- **无占位符**：所有代码步骤均给出完整可运行实现；无 TBD / TODO / "稍后补充"
- **类型一致性**：`ValidationIssue` 字段名（`type`/`datasource`/`table`/`column`/`qualifier`/`provided_by`/`available_columns`/`confidence`）在 Task 1 定义，Task 2、4、5、6 一致引用；`MAX_AVAILABLE_COLUMNS` 在 Task 1 定义并被 Task 2 使用
