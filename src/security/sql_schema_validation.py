"""SQL Schema 校验层 — 生成后、打库前的表与列引用校验。

设计依据：spec/39-sql-schema-validation.md
依赖方向：本模块不依赖 src.graph，入参为纯数据，可被 graph / reporting / api 复用。
"""

from __future__ import annotations

import re
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
    "type_mismatch",
]
Confidence = Literal["high", "low"]

# 回喂重试时单表最多携带的可用列数，避免撑爆 Prompt 预算。
MAX_AVAILABLE_COLUMNS = 40

# 字符串型列的类型基名（剥离长度/精度后精确匹配）。日期函数（TRUNC/ADD_MONTHS 等）
# 打在任一此类列上都会被 Oracle 按 NUMBER 或依赖 NLS 隐式转换，属确定性缺陷。
_STRING_TYPE_BASES = frozenset(
    {
        "CHAR",
        "NCHAR",
        "VARCHAR",
        "VARCHAR2",
        "NVARCHAR",
        "NVARCHAR2",
        "CLOB",
        "NCLOB",
        "TEXT",
        "STRING",
    }
)


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
    # type_mismatch 专用的可操作修法提示（直接回喂给模型，须自包含）；
    # 其他类型留空，走 describe 的固定文案分支。
    note: str = ""
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
        if self.type == "type_mismatch":
            # note 由生成处填好完整可操作提示；缺失时给降级文案兜底。
            return self.note or f"列 '{reference}' 的类型与函数用法不匹配"
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


# 启用「日期函数 × 字符串列」检查的方言集合。
# 各库函数语义与隐式转换规则不同，只在已实测确认的方言上启用，避免误伤。
_TYPE_CHECK_DIALECTS = frozenset({"oracle"})

# 各库的系统伪表：不登记在业务快照中，但在 SQL 里是合法引用。
# 判为「幻觉表」会把正确 SQL 拦下并触发无谓重试（如 Oracle 的 SELECT ... FROM dual），
# 重试次数耗尽后单元直接失败。白名单按方言隔离，避免跨库误放行。
_SYSTEM_TABLES_BY_DIALECT: dict[str, frozenset[str]] = {
    "oracle": frozenset({"dual"}),
    "mysql": frozenset({"dual"}),
}


# 方法作用：判断表名是否属于该方言的系统伪表。
# Args: table_name - 表名（大小写不敏感）；dialect - SQL 方言名。
# Returns: 是系统伪表返回 True。
def is_system_table(table_name: str, dialect: str) -> bool:
    """Oracle/MySQL 的 dual 等：合法引用但不在业务快照里，不得判为幻觉。"""
    normalized = str(table_name or "").strip().lower()
    if not normalized:
        return False
    from src.security.sql_execution import normalize_sql_dialect

    key = normalize_sql_dialect(dialect) if dialect else ""
    return normalized in _SYSTEM_TABLES_BY_DIALECT.get(key, frozenset())


# 方法作用：把权威快照整理成 (数据源, 表) -> {列名: 属性值} 的通用索引。
# Args: tables - 权威快照；datasources - 允许的数据源白名单；field - 列属性字段名。
# Returns: 属性索引；未携带该属性的列不进索引，查不到即视为「属性未知」并降级放行。
def _build_column_attr_index(
    tables: Sequence[Mapping[str, Any]],
    datasources: Sequence[str],
    field: str,
) -> dict[tuple[str, str], dict[str, str]]:
    """类型与值格式两个索引共用同一遍历逻辑，避免两处实现漂移。"""
    logger.debug("建列属性索引入口", field=field, table_count=len(tables))
    allowed = _normalize_datasources(datasources)
    allowed_set = set(allowed)
    default_source = allowed[0] if allowed else ""
    index: dict[tuple[str, str], dict[str, str]] = {}
    filled_columns = 0
    for table in tables:
        # 与 build_table_index 相同的健壮性约定：畸形条目静默丢弃。
        # 本索引缺失只会让对应检查降级放行，不改变表/列存在性判定。
        if not isinstance(table, Mapping):
            continue
        name = str(table.get("name", "") or "").strip().lower()
        if not name:
            continue
        source = str(table.get("datasource", "") or "").strip() or default_source
        if allowed_set and source not in allowed_set:
            continue
        column_values: dict[str, str] = {}
        for column in _safe_columns_container(table, name):
            if not isinstance(column, Mapping):
                continue
            column_name = str(column.get("name", "") or "").strip().lower()
            column_value = str(column.get(field, "") or "").strip()
            if column_name and column_value:
                column_values[column_name] = column_value
                filled_columns += 1
        if column_values:
            index[(source, name)] = column_values
    logger.info(
        "建列属性索引完成", field=field, tables=len(index), filled_columns=filled_columns
    )
    return index


# 方法作用：构建 (数据源, 表) -> {列名: 类型} 索引，只收录快照中携带类型的列。
# Args: tables - 权威快照；datasources - 允许的数据源白名单。
# Returns: 类型索引；未携带 type 的列不进索引，查不到即视为「类型未知」并降级放行。
def build_column_type_index(
    tables: Sequence[Mapping[str, Any]],
    datasources: Sequence[str],
) -> dict[tuple[str, str], dict[str, str]]:
    """列名到类型声明的索引；本索引是类型提示的增强层，缺失不影响主校验。"""
    return _build_column_attr_index(tables, datasources, "type")


# 方法作用：构建 (数据源, 表) -> {列名: 值格式} 索引，只收录识别出格式的列。
# Args: tables - 权威快照；datasources - 允许的数据源白名单。
# Returns: 值格式索引；未识别格式的列不进索引，查不到即视为「格式未知」并跳过比对。
def build_column_format_index(
    tables: Sequence[Mapping[str, Any]],
    datasources: Sequence[str],
) -> dict[tuple[str, str], dict[str, str]]:
    """列名到值格式的索引；用于比对 TO_DATE 格式串，缺失不影响主校验。"""
    return _build_column_attr_index(tables, datasources, "value_format")


# 方法作用：剥离类型声明的长度/精度后缀，返回基类型名（大写）。
# Args: column_type - 快照中的类型字符串，如 "VARCHAR2(50)"、"TIMESTAMP WITH TIME ZONE"。
# Returns: 基类型名；空输入返回空串。
def _base_type_name(column_type: str) -> str:
    """VARCHAR2(50) -> VARCHAR2；TIMESTAMP WITH TIME ZONE -> TIMESTAMP。"""
    normalized = str(column_type or "").strip().upper()
    if not normalized:
        return ""
    return re.split(r"[( ]", normalized, maxsplit=1)[0]


# 方法作用：判断类型是否属于字符串族（日期函数打在它上面会隐式转 NUMBER 或依赖 NLS）。
# Args: column_type - 快照中的类型字符串。
# Returns: 是字符串族返回 True。
def _is_string_type(column_type: str) -> bool:
    """VARCHAR2 / CHAR / CLOB 等字符串族判定。"""
    return _base_type_name(column_type) in _STRING_TYPE_BASES


# 方法作用：在 (数据源, 表) -> {列: 值} 索引里解析 AST 列节点。
# Args: column - sqlglot Column 节点；local_tables - 别名 -> 真实表；
#       owners - 真实表 -> 归属源；index - 任意列属性索引（类型 / 值格式）。
# Returns: (真实表名, 索引值)；无法唯一判定（CTE 限定、来源歧义、值缺失）返回 None。
def _resolve_column_attr(
    column: Any,
    local_tables: Mapping[str, str],
    owners: Mapping[str, str | None],
    index: Mapping[tuple[str, str], Mapping[str, str]],
) -> tuple[str, str] | None:
    """仅当列归属与索引值都唯一可确定时才返回值，否则 None 走降级放行。"""
    column_name = str(column.name or "").strip().lower()
    if not column_name:
        return None
    qualifier = str(column.table or "").strip().lower()
    if qualifier:
        real_table = local_tables.get(qualifier)
        if real_table is None:
            # CTE / 派生表限定符：权威索引里没有它的属性信息
            return None
        owner = owners.get(real_table)
        if owner is None or owner == "ambiguous":
            return None
        value = index.get((owner, real_table), {}).get(column_name)
        if not value:
            return None
        return real_table, value
    # 未限定列：仅当全 scope 物理表中唯一提供该列、且该列属性已知时才判定
    hits = [
        (table, index[(owner, table)][column_name])
        for table, owner in owners.items()
        if owner
        and owner != "ambiguous"
        and column_name in index.get((owner, table), {})
    ]
    if len(hits) != 1:
        return None
    return hits[0]


# 方法作用：枚举本 scope 内「输入必须是日期类型」的函数参数节点。
# Args: select - 本 scope 的 Select 节点。
# Returns: (函数显示名, 参数节点) 列表；TO_CHAR 仅在带 format 时纳入。
def _iter_date_func_args(select: Any) -> list[tuple[str, Any]]:
    """收集日期语义函数的输入参数；不带 format 的 TO_CHAR 是恒等转换，不纳入。"""
    from sqlglot import exp

    pairs: list[tuple[str, Any]] = []
    for node in select.find_all(exp.DateTrunc):
        # Oracle 下即 TRUNC(date, fmt)。两条参数都检查：this 是目标输入。
        pairs.append(("TRUNC", node.this))
    for node in select.find_all(exp.AddMonths):
        pairs.append(("ADD_MONTHS", node.this))
    for node in select.find_all(exp.LastDay):
        pairs.append(("LAST_DAY", node.this))
    for node in select.find_all(exp.Extract):
        pairs.append(("EXTRACT", node.expression))
    for node in select.find_all(exp.MonthsBetween):
        pairs.append(("MONTHS_BETWEEN", node.this))
        pairs.append(("MONTHS_BETWEEN", node.expression))
    for node in select.find_all(exp.ToChar):
        # 带 format 且输入是字符串时 format 被静默忽略 → 结果错误但 DB 不报错，
        # 比显式报错更危险，必须与 TRUNC 一视同仁。
        if node.args.get("format") is not None:
            pairs.append(("TO_CHAR", node.this))
    for node in select.find_all(exp.Anonymous):
        # 单参数形态 TRUNC(col) 会被 sqlglot 解析为 Anonymous：this 是函数名字符串，
        # 参数在 expressions 里（取 this 会拿到字符串而被裸列过滤静默漏检）。
        if str(node.name or "").strip().upper() == "TRUNC":
            first_arg = node.expressions[0] if node.expressions else None
            if first_arg is not None:
                pairs.append(("TRUNC", first_arg))
    return pairs


# 方法作用：对本 scope 的日期函数参数做类型检查，收集类型不匹配问题。
# Args: select - 本 scope 的 Select 节点；scope_column_keys - 本 scope 列键（qualifier, name）；
#       local_tables - 别名 -> 真实表；owners - 真实表 -> 归属源；
#       type_index - 列类型索引；default_source - issue 归属源。
# Returns: 类型不匹配问题列表；类型未知或来源不定时跳过（降级放行）。
def _collect_type_issues_in_scope(
    select: Any,
    scope_column_keys: set[tuple[str, str]],
    local_tables: Mapping[str, str],
    owners: Mapping[str, str | None],
    type_index: Mapping[tuple[str, str], Mapping[str, str]],
    default_source: str,
) -> list[ValidationIssue]:
    """日期函数打在字符串列上 → high 拦截并给出可操作修法。"""
    from sqlglot import exp

    issues: list[ValidationIssue] = []
    seen: set[tuple[str, str, str]] = set()
    for func_name, arg in _iter_date_func_args(select):
        # 参数不是裸列（TO_DATE(...) 包裹、字面量、子查询）时无从判定或已被正确转换
        if not isinstance(arg, exp.Column):
            continue
        column_name = str(arg.name or "").strip().lower()
        qualifier = str(arg.table or "").strip().lower()
        if not column_name or (qualifier, column_name) not in scope_column_keys:
            # 不属于本 scope 的列由更深层 scope 检查，避免同一错误重复上报
            continue
        resolved = _resolve_column_attr(arg, local_tables, owners, type_index)
        if resolved is None:
            continue
        table, column_type = resolved
        if not _is_string_type(column_type):
            continue
        if (table, column_name, func_name) in seen:
            continue
        seen.add((table, column_name, func_name))
        reference = f"{qualifier}.{column_name}" if qualifier else column_name
        logger.warning(
            "列类型与日期函数不匹配",
            table=table,
            column=column_name,
            column_type=column_type,
            func=func_name,
        )
        issues.append(
            ValidationIssue(
                type="type_mismatch",
                datasource=owners.get(table) or default_source,
                table=table,
                column=column_name,
                qualifier=qualifier,
                note=(
                    f"列 '{reference}' 的实际类型是 {column_type}（日期以文本存储），"
                    f"不能直接用于 {func_name}。请先用 TO_DATE 转换，"
                    f"例如 TO_CHAR(TO_DATE({column_name}, 'YYYY-MM-DD'), 'YYYY-MM')。"
                ),
            )
        )
    if issues:
        logger.info("列类型检查完成", issue_count=len(issues))
    return issues


# 方法作用：规范化日期格式标签，供跨来源比对（大小写与空白差异不影响相等性）。
# Args: label - 格式标签或 SQL 中的格式串。
# Returns: 大写、单空格分隔的规范形式。
def _normalize_format_label(label: str) -> str:
    """'yyyy-mm-dd hh24:mi:ss' 与 'YYYY-MM-DD   HH24:MI:SS' 归一为同一形式。"""
    return " ".join(str(label or "").strip().upper().split())


# 列值格式标签（Oracle 风格）到 sqlglot 内部格式（Python strftime）的对照。
# sqlglot 解析 TO_DATE 时会把 Oracle 格式串转成内部风格（'YYYY-MM-DD' → '%Y-%m-%d'），
# 比对前必须把列格式映射到同一体系，否则恒不相等导致误报。
# 必须与 src/datasource/introspection.py 的 _VALUE_FORMAT_PATTERNS 标签集保持一致。
_ORACLE_FORMAT_TO_INTERNAL: dict[str, str] = {
    "YYYY-MM-DD HH24:MI:SS": "%Y-%m-%d %H:%M:%S",
    "YYYY-MM-DD": "%Y-%m-%d",
    "YYYY/MM/DD": "%Y/%m/%d",
    "YYYY-MM": "%Y-%m",
    "HH24:MI:SS": "%H:%M:%S",
}
# 反查表：回喂文案要展示 Oracle 风格格式串而非内部 strftime 风格
_INTERNAL_TO_ORACLE_FORMAT: dict[str, str] = {
    internal: oracle for oracle, internal in _ORACLE_FORMAT_TO_INTERNAL.items()
}


# 方法作用：把 sqlglot 解析出的格式串归一为 Oracle 风格标签。
# Args: declared_raw - fmt 节点里的原始字符串。
# Returns: Oracle 风格标签；无法可靠归一（含未知 strftime 片段）时返回空串。
def _to_oracle_format(declared_raw: str) -> str:
    """sqlglot 只转换全大写格式串；小写保持原样，混合大小写会产生 % 残片。

    - 命中反查表：完整内部格式 → 翻回 Oracle 风格
    - 不含 %：未被转换（如全小写书写）→ 大写化即 Oracle 风格
    - 其余（含 % 残片）：无法可靠判定 → 空串，调用方跳过检查
    """
    text = str(declared_raw or "").strip()
    if not text:
        return ""
    if text in _INTERNAL_TO_ORACLE_FORMAT:
        return _INTERNAL_TO_ORACLE_FORMAT[text]
    if "%" in text:
        return ""
    return _normalize_format_label(text)


# 方法作用：比对 TO_DATE 格式串与列的实际值格式，拦截不匹配的写法。
# Args: select - 本 scope 的 Select 节点；scope_column_keys - 本 scope 列键；
#       local_tables - 别名 -> 真实表；owners - 真实表 -> 归属源；
#       format_index - 列值格式索引；default_source - issue 归属源。
# Returns: 格式不匹配问题列表；格式未知或无法解析时跳过（降级放行）。
def _collect_format_issues_in_scope(
    select: Any,
    scope_column_keys: set[tuple[str, str]],
    local_tables: Mapping[str, str],
    owners: Mapping[str, str | None],
    format_index: Mapping[tuple[str, str], Mapping[str, str]],
    default_source: str,
) -> list[ValidationIssue]:
    """TO_DATE(文本日期列, fmt) 的 fmt 与列实际格式不符 → high 拦截（ORA-01830/01840 防线）。"""
    from sqlglot import exp

    issues: list[ValidationIssue] = []
    seen: set[tuple[str, str]] = set()
    for node in select.find_all(exp.StrToDate):
        arg = node.this
        fmt_node = node.args.get("format")
        # 只校验「格式串是字面量、目标是裸列」的形态：其余无从比对或已被正确包裹
        if not isinstance(arg, exp.Column) or not isinstance(fmt_node, exp.Literal):
            continue
        column_name = str(arg.name or "").strip().lower()
        qualifier = str(arg.table or "").strip().lower()
        if not column_name or (qualifier, column_name) not in scope_column_keys:
            continue
        resolved = _resolve_column_attr(arg, local_tables, owners, format_index)
        if resolved is None:
            continue
        table, actual_format = resolved
        # 声明侧与列标签都归一为 Oracle 风格再比对（sqlglot 的转换只覆盖全大写写法）
        declared_label = _to_oracle_format(str(fmt_node.this or ""))
        expected_label = _normalize_format_label(actual_format)
        if not declared_label or not expected_label or declared_label == expected_label:
            continue
        if (table, column_name) in seen:
            continue
        seen.add((table, column_name))
        reference = f"{qualifier}.{column_name}" if qualifier else column_name
        declared_display = declared_label
        logger.warning(
            "TO_DATE 格式串与列实际格式不匹配",
            table=table,
            column=column_name,
            actual_format=actual_format,
            declared_format=declared_display,
        )
        issues.append(
            ValidationIssue(
                type="type_mismatch",
                datasource=owners.get(table) or default_source,
                table=table,
                column=column_name,
                qualifier=qualifier,
                note=(
                    f"列 '{reference}' 的实际值格式是 {actual_format}，"
                    f"但 TO_DATE 的格式串是 '{declared_display}'，"
                    "两者不匹配会报 ORA-01830/01840。请改用 "
                    f"TO_DATE({column_name}, '{actual_format}')，或直接字符串比较"
                    f"（{column_name} >= TO_CHAR(SYSDATE-90,'YYYY-MM-DD')），"
                    f"按月分组用 SUBSTR({column_name}, 1, 7)。"
                ),
            )
        )
    return issues


# 方法作用：收集本 scope 各 CTE / 派生表来源的输出列名。
# Args: scope - sqlglot traverse_scope 产出的单个 scope。
# Returns: 别名 -> (输出列名集合, 是否可完全枚举)；含 SELECT * 或解析失败时标记不可枚举。
def _collect_cte_outputs(scope: Any) -> dict[str, tuple[set[str], bool]]:
    """CTE 输出列索引；不可枚举的来源退回「无法判定不拦截」。"""
    from sqlglot import exp

    outputs: dict[str, tuple[set[str], bool]] = {}
    # 只保留 FROM/JOIN 实际引用的来源：sqlglot 会把 WITH 里所有 CTE 都挂到外层
    # scope.sources，但未被引用的上游 CTE 输出列对外层不可见。若不做这层过滤，
    # 越界引用会被上游 CTE 意外「救活」（ORA-00904 漏检）。
    # selected_sources 的 value 是 sqlglot 内部元组（版本相关），只取 key 过滤，
    # 节点仍从 scope.sources 取，避免依赖未文档化的结构。
    selected_keys = set(getattr(scope, "selected_sources", None) or scope.sources)
    for alias, node in scope.sources.items():
        if alias not in selected_keys:
            continue
        if isinstance(node, exp.Table):
            continue
        key = str(alias).strip().lower()
        inner = getattr(node, "expression", None)
        if not isinstance(inner, exp.Select):
            outputs[key] = (set(), False)
            continue
        columns: set[str] = set()
        complete = True
        for projection in inner.expressions:
            if _is_star_projection(projection):
                complete = False
                continue
            name = str(projection.alias_or_name or "").strip().lower()
            if name:
                columns.add(name)
            else:
                complete = False
        outputs[key] = (columns, complete)
    logger.debug("CTE 输出列收集完成", source_count=len(outputs))
    return outputs


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

    issues = _collect_unknown_tables(tree, index, default_source, dialect)
    # 类型索引与列名索引分开构建：类型检查是本轮新增的增强层，
    # 其缺失或降级不得影响既有表/列存在性判定。
    type_index = build_column_type_index(tables, datasources)
    format_index = build_column_format_index(tables, datasources)
    issues.extend(
        _collect_column_issues(
            tree,
            index,
            default_source,
            type_index=type_index,
            format_index=format_index,
            dialect=dialect,
        )
    )
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
    dialect: str = "",
) -> list[ValidationIssue]:
    """校验 FROM / JOIN 引用的物理表是否存在；CTE 名与系统伪表必须排除。"""
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
        # 系统伪表（如 Oracle 的 dual）合法但不在业务快照，判为幻觉会误拦正确 SQL
        if is_system_table(name, dialect):
            logger.debug("表引用命中系统伪表", table=name, dialect=dialect)
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


# 方法作用：逐作用域校验列引用的归属与类型用法。
# Args: tree - 已解析语法树；index - 权威索引；default_source - issue 归属源；
#       type_index - 列类型索引（可选，缺省跳过类型检查）；
#       dialect - SQL 方言（可选，仅已启用的方言做类型检查）。
# Returns: 列级问题列表。
def _collect_column_issues(
    tree: Any,
    index: Mapping[tuple[str, str], frozenset[str]],
    default_source: str,
    type_index: Mapping[tuple[str, str], Mapping[str, str]] | None = None,
    format_index: Mapping[tuple[str, str], Mapping[str, str]] | None = None,
    dialect: str = "",
) -> list[ValidationIssue]:
    """按 scope 校验限定列、未限定列与日期函数参数类型，并标记 SELECT * 盲区。"""
    from sqlglot import exp
    from sqlglot.optimizer.scope import traverse_scope

    issues: list[ValidationIssue] = []
    type_check_enabled = bool(
        type_index and str(dialect or "").strip().lower() in _TYPE_CHECK_DIALECTS
    )
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
        # CTE / 派生表输出列：外层引用 CTE 输出是合法写法，越界引用才是可确认的缺陷。
        cte_outputs = _collect_cte_outputs(scope)
        # 类型检查只对已启用方言执行；列类型缺失时逐条降级放行（见 _resolve_column_type）。
        if type_check_enabled and isinstance(select, exp.Select):
            scope_column_keys = {
                (str(column.table or "").strip().lower(), str(column.name or "").strip().lower())
                for column in scope.columns
            }
            issues.extend(
                _collect_type_issues_in_scope(
                    select,
                    scope_column_keys,
                    local_tables,
                    owners,
                    type_index or {},
                    default_source,
                )
            )
            # 格式比对依赖采样识别的 value_format：索引为空时整体跳过（降级放行）
            if format_index:
                issues.extend(
                    _collect_format_issues_in_scope(
                        select,
                        scope_column_keys,
                        local_tables,
                        owners,
                        format_index,
                        default_source,
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
                        column_name,
                        qualifier,
                        local_tables,
                        owners,
                        scope_schemas,
                        default_source,
                        index,
                        cte_outputs,
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
                        cte_outputs,
                        default_source,
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
    cte_outputs: Mapping[str, tuple[set[str], bool]] | None = None,
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
        # 限定符指向 CTE / 派生表：输出列可完全枚举且不含该列时，是可确认的越界引用
        # （ORA-00904 的确定性形态）；否则无法判定，放行并记 warning。
        cte_entry = (cte_outputs or {}).get(qualifier)
        if cte_entry is not None:
            cte_columns, cte_complete = cte_entry
            if cte_complete and column_name not in cte_columns:
                logger.warning(
                    "列引用不存在",
                    reason="cte_qualifier_exhaustive",
                    qualifier=qualifier,
                    column=column_name,
                )
                return [
                    ValidationIssue(
                        type="unknown_column",
                        datasource=default_source,
                        table=qualifier,
                        column=column_name,
                        qualifier=qualifier,
                        available_columns=tuple(sorted(cte_columns)[:MAX_AVAILABLE_COLUMNS]),
                        available_columns_total=len(cte_columns),
                    )
                ]
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
    cte_outputs: Mapping[str, tuple[set[str], bool]] | None = None,
    default_source: str = "",
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
    cte_columns: set[str] = set()
    # 没有任何 CTE 来源时 cte_complete 保持 False：不允许走「CTE 穷举」拦截分支。
    cte_complete = bool(cte_outputs)
    for columns, complete in (cte_outputs or {}).values():
        cte_columns |= columns
        if not complete:
            cte_complete = False
    # 命中 CTE / 派生表输出列：外层引用 CTE 输出是合法写法
    if column_name in cte_columns:
        logger.debug("列引用命中 CTE 输出列", column=column_name)
        return []
    if has_cte_or_derived:
        # 所有来源（物理表 + CTE）的列集都能完全枚举时，「不在任何来源列集中」
        # 是可确认的结论 → 拦截（ORA-00904 的确定性形态）。任一来源不可枚举即退回放行。
        physical_exhaustive = len(resolved) == total_table_count and all(
            index.get((owner, table), frozenset()) for table, owner in resolved.items()
        )
        if cte_complete and physical_exhaustive:
            logger.warning(
                "列引用不存在",
                column=column_name,
                reason="cte_outputs_exhaustive",
                candidate_sources=sorted((cte_outputs or {}))[:5],
            )
            return [
                ValidationIssue(
                    type="unknown_column",
                    datasource=next(iter(resolved.values()), default_source) or default_source,
                    # 无物理表可归属时，把 CTE 来源名写进 table 字段：
                    # 回喂文案「不在表 X 的可用列中」必须能指出该改哪个来源。
                    table=", ".join(sorted(cte_outputs or {}))[:200],
                    column=column_name,
                    available_columns=tuple(sorted(cte_columns)[:MAX_AVAILABLE_COLUMNS]),
                    available_columns_total=len(cte_columns),
                )
            ]
        logger.warning(
            "列引用无法判定",
            reason="cte_or_unresolved",
            column=column_name,
            resolved_count=len(resolved),
            total_table_count=total_table_count,
        )
        return []
    # 零命中：只有本 scope 全是物理表、且归属全部确定时，才能判为真幻觉。
    # 否则列可能来自归属不明的表 → 无法判定。
    if len(resolved) != total_table_count or not resolved:
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
