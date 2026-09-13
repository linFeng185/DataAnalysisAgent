"""SQL 方言重写器 — 修复 LLM 常见的方言语法错误。

代码级修正（非 LLM call），执行前自动运行。
每个方言有独立的规则集，按需扩展。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.datasource.schema_snapshot import SchemaSnapshot

from src.logging_config import get_logger

logger = get_logger(__name__)

_MYSQL_FIXES: list[tuple[str, str, str]] = [
    # INTERVAL 'N' UNIT → INTERVAL N UNIT（去掉 LLM 误加的引号）
    (r"\bINTERVAL\s+'(\d+)'\s+(\w+)",
     r"INTERVAL \1 \2"),
    # CURDATE() - INTERVAL → DATE_SUB(CURDATE(), INTERVAL ...)
    (r"\bCURDATE\(\)\s*-\s*INTERVAL\s+(\d+)\s+(\w+)",
     r"DATE_SUB(CURDATE(), INTERVAL \1 \2)"),
    # NOW() - INTERVAL → DATE_SUB(NOW(), INTERVAL ...)
    (r"\bNOW\(\)\s*-\s*INTERVAL\s+(\d+)\s+(\w+)",
     r"DATE_SUB(NOW(), INTERVAL \1 \2)"),
    # DATE(col) - INTERVAL → DATE_SUB(DATE(col), INTERVAL ...)
    (r"\bDATE\((\w+)\)\s*-\s*INTERVAL\s+(\d+)\s+(\w+)",
     r"DATE_SUB(DATE(\1), INTERVAL \2 \3)"),
    # col - INTERVAL → DATE_SUB(col, INTERVAL ...)
    (r"(\w+)\s*-\s*INTERVAL\s+(\d+)\s+(\w+)",
     r"DATE_SUB(\1, INTERVAL \2 \3)"),
    # CURDATE() + INTERVAL → DATE_ADD (避免歧义)
    (r"\bCURDATE\(\)\s*\+\s*INTERVAL\s+(\d+)\s+(\w+)",
     r"DATE_ADD(CURDATE(), INTERVAL \1 \2)"),
    # STRING_AGG → GROUP_CONCAT (MySQL 无 STRING_AGG)
    (r"\bSTRING_AGG\(([^,]+),\s*([^)]+)\)",
     r"GROUP_CONCAT(\1 SEPARATOR \2)"),
    # ILIKE → LIKE
    (r"\bILIKE\b", r"LIKE"),
]

_CLICKHOUSE_FIXES: list[tuple[str, str, str]] = [
    (r"\bGROUP_CONCAT\(([^)]+)\)", r"arrayStringConcat(groupArray(\1), ',')"),
    (r"\bIFNULL\(([^,]+),\s*([^)]+)\)", r"ifNull(\1, \2)"),
    # ClickHouse 的 dateTrunc 只接受 DateTime，Date32 列会直接抛 code 43
    # （Illegal type Date32 of 2nd argument）。toStartOf* 接受 Date/Date32/DateTime，
    # 是等价且更稳的写法，这里按粒度确定性改写，不依赖模型是否遵守方言提示。
    (r"\bdate_?trunc\(\s*'year'\s*,\s*([^)]+)\)", r"toStartOfYear(\1)"),
    (r"\bdate_?trunc\(\s*'quarter'\s*,\s*([^)]+)\)", r"toStartOfQuarter(\1)"),
    (r"\bdate_?trunc\(\s*'month'\s*,\s*([^)]+)\)", r"toStartOfMonth(\1)"),
    (r"\bdate_?trunc\(\s*'week'\s*,\s*([^)]+)\)", r"toStartOfWeek(\1)"),
    (r"\bdate_?trunc\(\s*'day'\s*,\s*([^)]+)\)", r"toStartOfDay(\1)"),
    (r"\bdate_?trunc\(\s*'hour'\s*,\s*([^)]+)\)", r"toStartOfHour(\1)"),
]

_POSTGRES_FIXES: list[tuple[str, str, str]] = [
    (r"\bGROUP_CONCAT\(([^,]+),\s*SEPARATOR\s+([^)]+)\)",
     r"STRING_AGG(\1, \2)"),
    (r"\bIFNULL\(([^,]+),\s*([^)]+)\)", r"COALESCE(\1, \2)"),
    (r"\bDATE_FORMAT\(([^,]+),\s*'([^']+)'\)", r"TO_CHAR(\1, '\2')"),
]

_ORACLE_FIXES: list[tuple[str, str, str]] = [
    (r"\bIFNULL\(([^,]+),\s*([^)]+)\)", r"NVL(\1, \2)"),
    (r"\bGROUP_CONCAT\(([^,]+),\s*SEPARATOR\s+([^)]+)\)",
     r"LISTAGG(\1, \2) WITHIN GROUP (ORDER BY \1)"),
]

_MSSQL_FIXES: list[tuple[str, str, str]] = [
    (r"\bIFNULL\(([^,]+),\s*([^)]+)\)", r"ISNULL(\1, \2)"),
    (r"\bGROUP_CONCAT\(([^,]+),\s*SEPARATOR\s+([^)]+)\)",
     r"STRING_AGG(\1, \2)"),
]

# sqlglot 的 ClickHouse 生成器会把 toStartOfMonth(x) 渲染回 dateTrunc('MONTH', x)，
# 而 dateTrunc 只接受 DateTime，Date32 列直接抛 code 43。Layer 2 之后必须再撤一次，
# 否则我们等于把模型写对的函数亲手改成会报错的写法。
_CLICKHOUSE_DATETRUNC_REVERSAL: list[tuple[str, str]] = [
    (r"\bdate_?trunc\(\s*'year'\s*,\s*([^)]+)\)", r"toStartOfYear(\1)"),
    (r"\bdate_?trunc\(\s*'quarter'\s*,\s*([^)]+)\)", r"toStartOfQuarter(\1)"),
    (r"\bdate_?trunc\(\s*'month'\s*,\s*([^)]+)\)", r"toStartOfMonth(\1)"),
    (r"\bdate_?trunc\(\s*'week'\s*,\s*([^)]+)\)", r"toStartOfWeek(\1)"),
    (r"\bdate_?trunc\(\s*'day'\s*,\s*([^)]+)\)", r"toStartOfDay(\1)"),
    (r"\bdate_?trunc\(\s*'hour'\s*,\s*([^)]+)\)", r"toStartOfHour(\1)"),
]

# sqlglot 的 identify 会给所有标识符加引号，包括 Oracle 伪表 dual。但 dual 是
# 公共同义词 DUAL（大写），带引号的小写 "dual" 按大小写敏感解析匹配不到对象，
# EXPLAIN 与执行都直接 ORA-00942。这与 ClickHouse 的 dateTrunc 是同一类问题：
# Layer 2 会把正确的写法改错，必须在它之后撤销。注意只撤 dual——小写业务表
# （"orders"）必须保留引号，否则会命中大写老表。
_ORACLE_DUAL_REVERSAL: list[tuple[str, str]] = [
    # 收尾不能写 \b：引号是非单词字符，`"` 后接空格/行尾不构成单词边界，
    # 加了 \b 反而一条都匹配不到（曾因此让整个回退静默失效）。
    # 闭合引号本身已保证标识符完整，`"duality"` 不会被误伤。
    (r'\bFROM\s+"dual"', "FROM dual"),
    (r'\bJOIN\s+"dual"', "JOIN dual"),
]

# Oracle 的 CTE 名不能与表名同名：`WITH "inventory" AS (SELECT ... FROM "inventory")`
# 会被当成递归 CTE，直接抛 ORA-32039（recursive WITH clause must have column alias list）。
# PostgreSQL / ClickHouse 等方言允许 CTE 遮蔽同名表，语义完全不同，所以这条只在
# Oracle 生效。改名策略是「只改定义名与对 CTE 的引用」，CTE 体内对同名表的引用
# 必须原样保留——那是真实表，改了等于把语义反转成自引用。
_ORACLE_CTE_RENAME_SUFFIX = "_src"

_RULES: dict[str, list[tuple[str, str, str]]] = {
    "mysql": _MYSQL_FIXES,
    "clickhouse": _CLICKHOUSE_FIXES,
    "postgres": _POSTGRES_FIXES,
    "oracle": _ORACLE_FIXES,
    "mssql": _MSSQL_FIXES,
}


# 方法作用：重命名 Oracle 中与表同名的 CTE，规避 ORA-32039 递归 CTE 解析。
# Args: sql - 已经过标识符加引号处理的 Oracle SQL。
# Returns: (修正后 SQL, 改名的 CTE 数量)；无冲突时原样返回。
def _oracle_rename_shadowing_ctes(sql: str) -> tuple[str, int]:
    """同名 CTE 只改定义名与对 CTE 的引用，CTE 体内的表引用保持不动。"""
    logger.debug("Oracle CTE 同名重命名入口", sql_chars=len(sql))
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql, read="oracle")
    except Exception as exc:
        # 解析失败不阻断执行：Layer 1/2 的成果照常返回，由下游报更具体的错
        logger.error("Oracle CTE 同名重命名解析失败", error=str(exc), exc_info=True)
        return sql, 0

    # 已占用的标识符：新名字不得再撞上别的表或 CTE
    taken = {table.name for table in tree.find_all(exp.Table)}
    taken |= {
        cte.alias_or_name
        for with_node in tree.find_all(exp.With)
        for cte in with_node.expressions
    }

    renamed = 0
    for with_node in tree.find_all(exp.With):
        for cte in with_node.expressions:
            name = cte.alias_or_name
            body = cte.this
            if not name or body is None:
                continue
            # 冲突判据：CTE 体内引用了同名标识符 —— Oracle 正是这样判定递归的
            if not any(table.name == name for table in body.find_all(exp.Table)):
                continue
            new_name = f"{name}{_ORACLE_CTE_RENAME_SUFFIX}"
            while new_name in taken:
                new_name += "_"
            taken.add(new_name)
            # 定义名与引用名必须同样带引号，否则渲染后大小写不一致、引用落空
            quoted_name = exp.to_identifier(new_name, quoted=True)
            cte.set("alias", exp.TableAlias(this=quoted_name))
            scope = with_node.parent if with_node.parent is not None else tree
            for table in scope.find_all(exp.Table):
                if table.name != name:
                    continue
                # 位于「自己的 body」内 → 那是真实表，跳过，否则语义反转
                cursor, inside_own_body = table.parent, False
                while cursor is not None:
                    if cursor is body:
                        inside_own_body = True
                        break
                    cursor = cursor.parent
                if inside_own_body:
                    continue
                table.set("this", exp.to_identifier(new_name, quoted=True))
            renamed += 1

    if renamed <= 0:
        logger.debug("Oracle CTE 同名重命名跳过", reason="无与表同名的 CTE")
        return sql, 0
    rewritten = tree.sql(dialect="oracle")
    logger.info(
        "Oracle CTE 同名重命名完成",
        renamed=renamed,
        sql_chars=len(rewritten),
    )
    return rewritten, renamed


# 根据权威列类型，只统一 Oracle 集合运算中明确冲突的字符集。
# Args: sql - 已完成基础重写的 SQL；schema - 当前数据源快照或其序列化字典。
# Returns: 修正后的 SQL 和新增转换数量；证据不足或分析失败时保留输入。
def _oracle_normalize_set_charsets(
    sql: str, schema: SchemaSnapshot | dict[str, Any],
) -> tuple[str, int]:
    """只转换已知普通字符集输出，保留国家字符集和所有业务表达式。"""
    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.scope import Scope, traverse_scope

    # 兼容运行时快照和序列化快照，不修改任何 Schema 对象。
    # Args: value - 快照、表或列；key - 属性名。
    # Returns: 属性值；不存在时返回 None。
    def field(value: Any, key: str) -> Any:
        return value.get(key) if isinstance(value, dict) else getattr(value, key, None)

    # 只识别明确的字符类型，其他类型不得被猜测为字符串。
    # Args: type_name - Oracle 类型名称。
    # Returns: 数据库字符集、国家字符集或未知类型标识。
    def charset(type_name: str) -> str:
        name = re.split(r"[\s(]", str(type_name).strip().upper(), maxsplit=1)[0]
        if name in {"CHAR", "VARCHAR", "VARCHAR2"}:
            return "database"
        if name in {"NCHAR", "NVARCHAR", "NVARCHAR2"}:
            return "national"
        return "unknown"

    try:
        tree = sqlglot.parse_one(sql, read="oracle")
        if tree is None or not tree.find(exp.SetOperation):
            return sql, 0
        table_types: dict[str, dict[str, str] | None] = {}
        for table in field(schema, "tables") or []:
            table_name = field(table, "name")
            if not isinstance(table_name, str) or not table_name:
                continue
            if table_name in table_types:
                table_types[table_name] = None
                continue
            columns: dict[str, str] = {}
            for column in field(table, "columns") or []:
                column_name = field(column, "name")
                if isinstance(column_name, str) and column_name:
                    columns[column_name] = (
                        "unknown" if column_name in columns else charset(field(column, "type"))
                    )
            table_types[table_name] = columns
        if not table_types:
            return sql, 0
        outputs: dict[Scope, list[tuple[str, str]]] = {}

        # 在实际 FROM/JOIN 来源中解析列，拒绝跨命名空间猜测和未限定歧义列。
        # Args: column - 列引用；scope - 当前 SELECT 作用域。
        # Returns: 已确认的列字符集，否则为 unknown。
        def column_charset(column: exp.Column, scope: Scope) -> str:
            sources = scope.selected_sources
            if column.db or column.catalog:
                return "unknown"
            if column.table:
                selected = sources.get(column.table)
            else:
                selected = next(iter(sources.values())) if len(sources) == 1 else None
            if selected is None:
                return "unknown"
            source = selected[1]
            if isinstance(source, exp.Table):
                if source.db or source.catalog:
                    return "unknown"
                return (table_types.get(source.name) or {}).get(column.name, "unknown")
            matches = [kind for name, kind in outputs.get(source, []) if name == column.name]
            return matches[0] if len(matches) == 1 else "unknown"

        # 追踪明确保留字符集的表达式，不把一般函数或数值运算当成文本。
        # Args: expression - 输出表达式；scope - 所属作用域。
        # Returns: 输出字符集、null 或 unknown。
        def expression_charset(expression: exp.Expression, scope: Scope) -> str:
            if isinstance(expression, (exp.Alias, exp.Paren, exp.Substring)):
                return expression_charset(expression.this, scope)
            if isinstance(expression, exp.Column):
                return column_charset(expression, scope)
            if isinstance(expression, exp.Cast):
                return charset(expression.args["to"].sql(dialect="oracle"))
            if isinstance(expression, exp.ToChar):
                return "database"
            if isinstance(expression, exp.Anonymous) and expression.name.upper() == "TO_NCHAR":
                return "national"
            if isinstance(expression, exp.National):
                return "national"
            if isinstance(expression, exp.Literal) and expression.is_string:
                return "database"
            if isinstance(expression, exp.Null):
                return "null"
            return "unknown"

        # 只在集合分支的投影处转换，避免改动源 CTE 的分组、过滤和其他消费者。
        # Args: scope - 已确认普通字符集的分支；position - 输出列位置。
        # Returns: 新增的 TO_NCHAR 数量。
        def promote_output(scope: Scope, position: int) -> int:
            output_name, kind = outputs[scope][position]
            if kind != "database":
                return 0
            if scope.union_scopes:
                count = sum(promote_output(child, position) for child in scope.union_scopes)
            else:
                projection = scope.expression.selects[position]
                value = projection.this if isinstance(projection, exp.Alias) else projection
                converted = exp.Anonymous(this="TO_NCHAR", expressions=[value.copy()])
                if isinstance(projection, exp.Alias):
                    projection.set("this", converted)
                else:
                    projection.replace(
                        exp.alias_(converted, output_name, quoted=True) if output_name else converted
                    )
                count = 1
            outputs[scope][position] = (output_name, "national")
            return count

        fixes = 0
        # 子作用域先处理，让 CTE 与多层集合操作的真实输出类型逐级向外传递。
        for scope in traverse_scope(tree):
            if isinstance(scope.expression, exp.Select):
                projections = scope.expression.selects
                outputs[scope] = [] if any(item.is_star for item in projections) else [
                    (
                        scope.outer_columns[position]
                        if position < len(scope.outer_columns) else projection.alias_or_name,
                        expression_charset(projection, scope),
                    )
                    for position, projection in enumerate(projections)
                ]
            elif scope.union_scopes:
                branches = [outputs.get(child, []) for child in scope.union_scopes]
                if not branches[0] or any(len(branch) != len(branches[0]) for branch in branches):
                    outputs[scope] = []
                    continue
                combined: list[tuple[str, str]] = []
                for position, (name, _) in enumerate(branches[0]):
                    kinds = {branch[position][1] for branch in branches} - {"null"}
                    if kinds == {"database", "national"}:
                        fixes += sum(promote_output(child, position) for child in scope.union_scopes)
                        kind = "national"
                    else:
                        kind = next(iter(kinds)) if len(kinds) == 1 else "unknown"
                    if position < len(scope.outer_columns):
                        name = scope.outer_columns[position]
                    combined.append((name, kind))
                outputs[scope] = combined
        logger.debug("Oracle 集合运算字符集分析完成", fixes=fixes)
        return (tree.sql(dialect="oracle"), fixes) if fixes else (sql, 0)
    except Exception as exc:
        # 保留排障堆栈，但解析器异常正文可能包含 SQL，不能直接写入日志。
        safe_error = RuntimeError("Oracle 集合运算字符集分析失败，保留原 SQL 校验路径")
        logger.error(
            "Oracle 集合运算字符集分析回退",
            error_type=type(exc).__name__,
            exc_info=(type(safe_error), safe_error, exc.__traceback__),
        )
        return sql, 0


# 修正常见跨方言 SQL，并用目标方言重新渲染标识符。
# Args: sql - 待修正的 SQL；dialect - 目标数据库方言；schema - 可选权威数据源快照。
# Returns: 可交给目标数据库执行的修正后 SQL。
def rewrite_sql(
    sql: str, dialect: str, *, schema: SchemaSnapshot | dict[str, Any] | None = None,
) -> str:
    """两层修正：① 正则替换跨方言函数 ② sqlglot 标识符加引号。

    Layer 2 解决 year_month / rank / status 等别名撞保留字的问题。
    """
    logger.debug("rewrite_sql 入口", dialect=dialect, sql=sql[:150])
    rules = _RULES.get(dialect.lower(), [])
    applied = 0

    # Layer 1: 正则替换
    if rules:
        for pattern, replacement in rules:
            new_sql, count = re.subn(pattern, replacement, sql, flags=re.IGNORECASE)
            if count > 0:
                sql = new_sql
                applied += count

    # PostgreSQL 两参数 ROUND 仅接受 numeric，显式 CAST 避免 double precision 运行时失败。
    if dialect.lower() == "postgres":
        try:
            import sqlglot
            from sqlglot import exp

            tree = sqlglot.parse_one(sql, read="postgres")
            round_fixes = 0
            round_skips = 0
            for round_node in tree.find_all(exp.Round):
                if round_node.args.get("decimals") is None:
                    continue
                round_input = round_node.this
                cast_type = (
                    round_input.args.get("to")
                    if isinstance(round_input, exp.Cast)
                    else None
                )
                if (
                    isinstance(cast_type, exp.DataType)
                    and cast_type.this == exp.DataType.Type.DECIMAL
                ):
                    round_skips += 1
                    continue
                round_node.set(
                    "this",
                    exp.Cast(
                        this=round_input.copy(),
                        to=exp.DataType.build("DECIMAL"),
                    ),
                )
                round_fixes += 1
            if round_fixes:
                sql = tree.sql(dialect="postgres")
                applied += round_fixes
                logger.info("PostgreSQL ROUND 类型修正完成", fixes=round_fixes)
            if round_skips:
                logger.info(
                    "PostgreSQL ROUND 类型修正跳过",
                    reason="输入已是 DECIMAL",
                    skips=round_skips,
                )
        except Exception as exc:
            logger.error(
                "PostgreSQL ROUND 类型修正失败",
                error=str(exc),
                exc_info=True,
            )

    # Layer 2: 所有标识符统一加引号（防撞保留字 + 避免正则遗漏）
    # sqlglot 是 AST 解析器，能区分 列名/表名/别名 vs 函数名/关键字——
    # 不会给 NOW() 或 AS/FROM 加引号。方言自动选择引号类型:
    #   MySQL/ClickHouse → `backtick`
    #   PostgreSQL/Oracle  → "double-quote"
    #   MSSQL              → [bracket]
    try:
        import sqlglot

        # sqlglot 的 SQL Server 方言名是 tsql，必须走统一映射，
        # 否则直接传 mssql 会报 Unknown dialect 'mssql'，整条重写静默失效。
        from src.security.sql_execution import normalize_sql_dialect

        sqlglot_dialect = normalize_sql_dialect(dialect)
        quoted = sqlglot.transpile(sql, read=sqlglot_dialect, write=sqlglot_dialect, identify=True)
        if quoted and quoted[0] != sql:
            sql = quoted[0]
            applied += 1
    except Exception as exc:
        logger.error("SQL 标识符引用重写失败", error=str(exc), exc_info=True)

    # Layer 3: 撤销 sqlglot 对 ClickHouse 日期函数的归一化（见常量处说明）。
    if dialect.lower() in {"clickhouse", "ch"}:
        for pattern, replacement in _CLICKHOUSE_DATETRUNC_REVERSAL:
            sql, count = re.subn(pattern, replacement, sql, flags=re.IGNORECASE)
            if count > 0:
                applied += count
                logger.info("ClickHouse 日期函数归一化回退", count=count)

    # Layer 4: 撤销 sqlglot 给 Oracle 伪表 dual 加的引号（见常量处说明）。
    if dialect.lower() in {"oracle", "oracle_xe"}:
        for pattern, replacement in _ORACLE_DUAL_REVERSAL:
            sql, count = re.subn(pattern, replacement, sql, flags=re.IGNORECASE)
            if count > 0:
                applied += count
                logger.info("Oracle dual 引号回退", count=count)

    # Layer 5: 重命名 Oracle 中与表同名的 CTE（见常量处说明）。
    if dialect.lower() in {"oracle", "oracle_xe"}:
        sql, count = _oracle_rename_shadowing_ctes(sql)
        if count > 0:
            applied += count

    # 同一权威快照同时服务 EXPLAIN 和执行，不依赖 LLM 是否遵守字符集提示。
    if dialect.lower() in {"oracle", "oracle_xe"} and schema is not None:
        sql, count = _oracle_normalize_set_charsets(sql, schema)
        applied += count

    if applied > 0:
        # 每次执行 SQL 都会触发；重写结论只在排查方言问题时需要
        logger.debug("SQL 方言重写", dialect=dialect, fixes=applied,
                     result=sql[:150])
    logger.debug("rewrite_sql 完成", dialect=dialect, fixes=applied, sql_chars=len(sql))
    return sql
