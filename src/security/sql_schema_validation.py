"""SQL Schema 校验层 — 生成后、打库前的表与列引用校验。

设计依据：spec/39-sql-schema-validation.md
依赖方向：本模块不依赖 src.graph，入参为纯数据，可被 graph / reporting / api 复用。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from src.logging_config import get_logger

logger = get_logger(__name__)

IssueType = Literal[
    "unknown_table",
    "unknown_column",
    "cross_table_column",
    "ambiguous_column",
    "invalid_qualifier",
    "parse_error",
]
Confidence = Literal["high", "low"]

# 回喂重试时单表最多携带的可用列数，避免撑爆 Prompt 预算。
MAX_AVAILABLE_COLUMNS = 40


# 方法作用：承载单条 Schema 校验问题，供调用方拦截与结构化回喂重试。
# Args: type - 问题类型；datasource - 归属数据源；table - 解析别名后的真实表名；
#       column - 列名；qualifier - SQL 中写的限定符；provided_by - 真正提供该列的表；
#       available_columns - 该表实际可用列（可能被 MAX_AVAILABLE_COLUMNS 截断）；
#       available_columns_total - 截断前的真实列数；
#       confidence - 置信度。
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
    # 该表列的真实总数：available_columns 被 MAX_AVAILABLE_COLUMNS 截断时，
    # 回喂文案必须报真实总数，否则「共 N 个」会把截断后的条数说成全部，误导模型。
    available_columns_total: int = 0
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
        if self.type == "invalid_qualifier":
            return (
                f"列限定符 '{self.qualifier}' 不是有效的表名或别名（它看起来是 schema 名）。"
                "无别名时表别名就是表名，请改用表名限定，或先给表起别名再用该别名限定。"
            )
        return f"列 '{reference}' 不在表 {self.table} 的可用列中"


# 方法作用：把数据源白名单归一化为去空白的字符串列表，过滤非字符串项。
# Args: datasources - 调用方传入的数据源集合。
# Returns: 归一化后的非空字符串列表，保持原顺序。
def _normalize_datasources(datasources: Sequence[str]) -> list[str]:
    """只保留非空白的字符串项，避免 None / 数字被字符串化成伪源名。"""
    result = [item.strip() for item in datasources if isinstance(item, str) and item.strip()]
    logger.debug("数据源白名单归一化完成", input_count=len(datasources), kept=len(result))
    return result


# 方法作用：安全抽取单表条目的列清单，畸形 columns 容器降级为空列表。
# Args: table - 快照中的单表条目（Mapping）；table_name - 表名，仅用于日志定位。
# Returns: 列条目列表；容器不可迭代（如 columns=5）时记 warning 并返回空列表。
def _safe_columns_container(table: Mapping[str, Any], table_name: str) -> list[Any]:
    """columns 容器畸形时降级为空列表，避免异常击穿校验主流程。"""
    logger.debug("列清单抽取入口", table=table_name)
    raw_columns = table.get("columns")
    try:
        columns = list(raw_columns or [])
    except TypeError:
        # layer3 的调用点没有 try/except，这里不降级就会直接炸掉图节点；
        # 与「非 Mapping 列条目被跳过」的处置保持一致：畸形数据只丢列集不抛异常。
        logger.warning(
            "表索引跳过畸形列容器",
            table=table_name,
            container_type=type(raw_columns).__name__,
        )
        columns = []
    logger.debug("列清单抽取完成", table=table_name, column_count=len(columns))
    return columns


# 方法作用：把权威快照整理成 (数据源, 表名) -> 列名集合 的索引。
# Args: tables - 权威快照，每张表可带可选 datasource 字段；datasources - 允许的数据源白名单。
# Returns: 索引字典；非空白名单时 datasource 不在白名单内的表被整体跳过。
# 契约：空白名单表示不做数据源过滤，调用方须自行保证传入的快照只含单一数据源；
#       非空白名单时越界源的表整体跳过。
def build_table_index(
    tables: Sequence[Mapping[str, Any]],
    datasources: Sequence[str],
) -> dict[tuple[str, str], frozenset[str]]:
    """构建 (datasource, table) -> columns 索引。

    空白名单表示不做数据源过滤，调用方须自行保证传入的快照只含单一数据源；
    非空白名单时越界源的表整体跳过。
    """
    logger.debug("建表索引入口", table_count=len(tables), datasource_count=len(datasources))
    # 白名单只接受真实字符串项，避免 None / 数字被字符串化成 "None" / "5" 之类的伪源名
    allowed = _normalize_datasources(datasources)
    allowed_set = set(allowed)
    default_source = allowed[0] if allowed else ""
    index: dict[tuple[str, str], frozenset[str]] = {}
    skipped = 0
    skipped_malformed = 0
    empty_column_tables = 0
    for table in tables:
        # 非 Mapping 条目取不出字段，记 warning 后丢弃，避免上游畸形快照无声消失
        if not isinstance(table, Mapping):
            logger.warning("表索引跳过非法表条目", entry_type=type(table).__name__)
            skipped_malformed += 1
            continue
        name = str(table.get("name", "") or "").strip().lower()
        # 表名为空的条目无法建键，记 warning 后丢弃
        if not name:
            logger.warning("表索引跳过空表名条目")
            skipped_malformed += 1
            continue
        declared_source = str(table.get("datasource", "") or "").strip()
        # 多源白名单下缺省归属不可靠（同名表可能被归到错误的源），必须留痕说明回落依据
        if not declared_source and len(allowed) > 1:
            logger.warning(
                "表未声明数据源，按白名单首项回落",
                table=name,
                default_source=default_source,
            )
        source = declared_source or default_source
        if allowed_set and source not in allowed_set:
            logger.warning("表索引跳过未授权源", table=name, datasource=source)
            skipped += 1
            continue
        columns = frozenset(
            str(column.get("name", "") or "").strip().lower()
            for column in _safe_columns_container(table, name)
            if isinstance(column, Mapping) and str(column.get("name", "") or "").strip()
        )
        # 列全部被丢弃（缺失 / 非 Mapping / 空名）时按表留 debug 痕迹，表仍录入但列集为空
        if not columns:
            empty_column_tables += 1
            logger.debug("表索引录入空列集", table=name, datasource=source)
        index[(source, name)] = columns
    # 空白名单是合法调用模式（调用方自带单一数据源快照），但必须留痕说明按源过滤被跳过
    if not allowed_set:
        logger.warning(
            "未提供数据源白名单，跳过按源过滤",
            table_count=len(tables),
            entries=len(index),
        )
    logger.info(
        "建表索引完成",
        entries=len(index),
        skipped=skipped,
        skipped_malformed=skipped_malformed,
        empty_column_tables=empty_column_tables,
    )
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


# 方法作用：作用域感知地校验 SQL 引用的表与列是否存在于权威快照。
# Args: sql - 待校验 SQL；datasources - 权威数据源白名单；dialect - 解析方言；
#       tables - 权威快照；stage - 校验阶段（stage1 常规 / stage2 权威复核），仅用于指标标签。
# Returns: 结构化问题清单；空列表表示高置信通过。
def validate_sql_schema(
    sql: str,
    *,
    datasources: Sequence[str],
    dialect: str = "",
    tables: Sequence[Mapping[str, Any]] = (),
    stage: str = "stage1",
) -> list[ValidationIssue]:
    """逐 scope 校验表与列引用；无法判定的场景给 low 置信而不是拦截。"""
    logger.debug(
        "Schema 校验入口",
        sql_chars=len(sql),
        datasource_count=len(datasources),
        table_count=len(tables),
        dialect=dialect,
        stage=stage,
    )
    _start = time.monotonic()
    allowed = _normalize_datasources(datasources)
    default_source = allowed[0] if allowed else ""
    index = build_table_index(tables, datasources)
    try:
        import sqlglot

        from src.security.sql_execution import normalize_sql_dialect

        # 必须用 parse_one：execute_sql 的方言回归用例 monkeypatch 的即此函数。
        tree = sqlglot.parse_one(sql, read=normalize_sql_dialect(dialect) or None)
    except Exception as exc:
        logger.error("Schema 校验解析失败", error=str(exc), exc_info=True)
        issues = [ValidationIssue(type="parse_error", datasource=default_source)]
        _record_issues(issues, dialect, stage)
        return issues
    if tree is None:
        logger.error("Schema 校验解析失败", error="解析结果为空")
        issues = [ValidationIssue(type="parse_error", datasource=default_source)]
        _record_issues(issues, dialect, stage)
        return issues

    issues = _collect_unknown_tables(tree, index, default_source)
    issues.extend(_collect_column_issues(tree, index, default_source))
    _record_issues(issues, dialect, stage)
    high_count = sum(1 for item in issues if item.confidence == "high")
    logger.info(
        "Schema 校验完成",
        result="拦截" if high_count else "通过",
        high_count=high_count,
        low_count=len(issues) - high_count,
        elapsed_ms=round((time.monotonic() - _start) * 1000),
    )
    return issues


# 方法作用：把本次校验的问题清单上报到指标计数器，失败不得影响校验主流程。
# Args: issues - 本次产生的问题清单；dialect - 解析方言；stage - 校验阶段。
# Returns: 无返回值。
def _record_issues(
    issues: Sequence[ValidationIssue],
    dialect: str,
    stage: str,
) -> None:
    """逐条上报指标；指标不可用时只记 debug，不中断校验。"""
    logger.debug("校验指标上报入口", issue_count=len(issues), dialect=dialect, stage=stage)
    if not issues:
        # 无问题属高频正常路径，只记 debug，避免每次校验多一行 INFO 噪音
        logger.debug("校验指标上报跳过", reason="no_issues")
        return
    reported = 0
    for item in issues:
        try:
            from src.observability import get_metrics_registry

            get_metrics_registry().record_schema_validation(
                issue_type=item.type,
                confidence=item.confidence,
                dialect=dialect,
                stage=stage,
            )
            reported += 1
        except Exception as exc:
            # 指标不可用不得影响校验主流程
            logger.debug("校验指标上报跳过", error=str(exc))
    logger.info("校验指标上报完成", reported=reported, total=len(issues))


# 方法作用：收集 SQL 中引用了未知表的问题。
# Args: tree - 已解析语法树；index - 权威索引；default_source - issue 归属源。
# Returns: unknown_table 问题列表（多源同名表与空基准均为 low 置信）。
def _collect_unknown_tables(
    tree: Any,
    index: Mapping[tuple[str, str], frozenset[str]],
    default_source: str,
) -> list[ValidationIssue]:
    """校验 FROM / JOIN 引用的物理表是否存在；CTE 名必须排除。"""
    from sqlglot import exp

    issues: list[ValidationIssue] = []
    # 索引为空说明本次没有任何权威基准可比对：此时「表不存在」不是可确认的结论，
    # 按 low 置信放行（设计 §5.4「无法判定不拦截」），但仍记 warning 留痕，避免静默。
    has_baseline = bool(index)
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
            if not has_baseline:
                # 空基准下的「表不存在」不可确认，只降级留痕，不产出 high 置信拦截
                logger.warning("表引用无法判定", reason="empty_snapshot", table=name)
                issues.append(
                    ValidationIssue(
                        type="unknown_table",
                        datasource=default_source,
                        table=name,
                        confidence="low",
                    )
                )
            else:
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
        # 本 scope 各表节点携带的 schema 限定名。列限定符若等于它，属「用 schema 名限定列」
        # 的无效形态（无别名时表别名就是表名），必须拦截而非当作 CTE 限定符静默放行。
        scope_schemas = {
            str(node.db).strip().lower()
            for node in scope.sources.values()
            if isinstance(node, exp.Table) and str(node.db or "").strip()
        }
        owners = {
            table: resolve_table_owner(index, table) for table in local_tables.values()
        }
        resolved = {
            table: owner
            for table, owner in owners.items()
            if owner is not None and owner != "ambiguous"
        }
        # 证据 2 + 4：星号不进入 scope.columns，必须直接检查本 scope 的 SELECT 投影顶层；
        # 且不能用 find_all(exp.Star)（会把 COUNT(*) 误报为无法校验）。
        # 也不用 find_all(exp.Select)——它会下探子查询，使同一星号被多个 scope 重复留痕。
        select = scope.expression
        if isinstance(select, exp.Select):
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
        # GROUP BY <输出别名> 会被 sqlglot 当作普通列收进 scope.columns，必须按别名放行。
        projection_aliases = (
            _collect_projection_aliases(select) if isinstance(select, exp.Select) else set()
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
                        column_name,
                        qualifier,
                        local_tables,
                        owners,
                        scope_schemas,
                        default_source,
                        index,
                    )
                )
            else:
                issues.extend(
                    _check_unqualified_column(
                        column_name,
                        resolved,
                        len(local_tables),
                        has_cte_or_derived,
                        projection_aliases,
                        index,
                    )
                )
    return issues


# 方法作用：收集本 scope SELECT 投影中 AS 定义的输出别名。
# Args: select - 本 scope 的 Select 节点。
# Returns: 小写别名集合。
def _collect_projection_aliases(select: Any) -> set[str]:
    """收集投影别名：未限定列零命中但命中别名时，它是引用输出列而非幻觉。"""
    from sqlglot import exp

    aliases = {
        str(projection.alias).strip().lower()
        for projection in select.expressions
        if isinstance(projection, exp.Alias) and str(projection.alias or "").strip()
    }
    logger.debug("投影别名收集完成", alias_count=len(aliases))
    return aliases


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
#       owners - 真实表 -> 归属源；scope_schemas - 本 scope 各表携带的 schema 限定名；
#       default_source - issue 归属源；index - 权威索引。
# Returns: 问题列表；限定符指向 CTE、归属不明或该表列信息为空时返回空。
def _check_qualified_column(
    column_name: str,
    qualifier: str,
    local_tables: Mapping[str, str],
    owners: Mapping[str, str | None],
    scope_schemas: set[str],
    default_source: str,
    index: Mapping[tuple[str, str], frozenset[str]],
) -> list[ValidationIssue]:
    """按表归属校验限定列，跨表错配与未知列分别成 issue。"""
    real_table = local_tables.get(qualifier)
    if real_table is None:
        if qualifier in scope_schemas:
            # 限定符等于某张表的 schema 名：这是无效限定符（无别名时表别名就是表名，
            # 不能用 schema 名限定列），DB 会报 missing FROM-clause entry。
            # 必须拦截并给出可操作提示，否则该列会被当作 CTE 限定符而完全不参与校验。
            logger.warning(
                "列限定符无效",
                qualifier=qualifier,
                column=column_name,
                reason="schema_qualifier",
            )
            return [
                ValidationIssue(
                    type="invalid_qualifier",
                    datasource=default_source,
                    column=column_name,
                    qualifier=qualifier,
                )
            ]
        # 限定符指向 CTE 或派生表：无法判定，放行并记 warning。
        logger.warning("列引用无法判定", reason="cte_qualifier", qualifier=qualifier)
        return []
    owner = owners.get(real_table)
    if owner is None or owner == "ambiguous":
        return []
    columns = index.get((owner, real_table), frozenset())
    if column_name in columns:
        return []
    # 表在索引中但列集为空（快照只证明表存在、未携带列信息）：没有任何列可比对，
    # 「该列不属于此表」无法确认。此时必须降级放行并留痕，否则该表上所有合法列
    # 都会被判为幻觉列，误导重试去改本来正确的列名。
    if not columns:
        logger.warning(
            "列引用无法判定",
            reason="table_has_no_columns",
            table=real_table,
            column=column_name,
            qualifier=qualifier,
        )
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
                available_columns_total=len(columns),
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
            available_columns_total=len(columns),
        )
    ]


# 方法作用：校验未限定列的归属，区分唯一命中、歧义与真幻觉。
# Args: column_name - 列名（小写）；resolved - 归属已确定的表 -> 源；
#       total_table_count - 本 scope 物理表总数；has_cte_or_derived - 是否存在 CTE 或派生表；
#       projection_aliases - 本 scope SELECT 投影别名集合；index - 权威索引。
# Returns: 问题列表；唯一命中、命中投影别名或无法判定时返回空。
def _check_unqualified_column(
    column_name: str,
    resolved: Mapping[str, str],
    total_table_count: int,
    has_cte_or_derived: bool,
    projection_aliases: set[str],
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
    # 零命中的列可能是 GROUP BY / 其它子句对 SELECT 输出别名的引用（sqlglot 会把
    # GROUP BY <别名> 当作普通列收进 scope.columns）。别名是 SQL 自己定义的输出列，
    # 不是幻觉列，必须放行，否则合法的聚合查询会被高置信误拦并误导重试。
    if column_name in projection_aliases:
        logger.debug("列引用命中投影别名", column=column_name, scope_alias=True)
        return []
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
    # 本 scope 所有候选表的列集都为空（快照未携带列信息）：零命中不是可确认的结论，
    # 降级放行并留痕。只要有一张候选表列信息已知，就保留原有 high 置信判定。
    if not any(index.get((owner, table), frozenset()) for table, owner in resolved.items()):
        logger.warning(
            "列引用无法判定",
            reason="tables_have_no_columns",
            column=column_name,
            candidate_tables=sorted(resolved)[:5],
        )
        return []
    table, owner = next(iter(resolved.items()))
    table_columns = index.get((owner, table), frozenset())
    logger.warning("列引用不存在", table=table, column=column_name, unqualified=True)
    return [
        ValidationIssue(
            type="unknown_column",
            datasource=owner,
            table=table,
            column=column_name,
            available_columns=tuple(sorted(table_columns)[:MAX_AVAILABLE_COLUMNS]),
            available_columns_total=len(table_columns),
        )
    ]
