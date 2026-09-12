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
    allowed = [item.strip() for item in datasources if isinstance(item, str) and item.strip()]
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
