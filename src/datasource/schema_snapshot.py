"""Schema 数据结构 — 表结构、字段信息、关系图谱的统一定义。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ColumnInfo:
    """字段信息。"""

    name: str
    type: str
    comment: str = ""
    is_nullable: bool = True
    is_primary_key: bool = False
    is_indexed: bool = False  # 主键/唯一/普通索引
    enum_values: list[str] = field(default_factory=list)


@dataclass
class TableRelation:
    """表关系。"""

    target_table: str
    join_key: str
    relation_type: str  # "many_to_one" | "one_to_one" | "one_to_many"


@dataclass
class TableSchema:
    """表结构。"""

    name: str
    description: str = ""
    columns: list[ColumnInfo] = field(default_factory=list)
    relations: list[TableRelation] = field(default_factory=list)
    row_count_estimate: int = 0
    partition_key: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class SchemaSnapshot:
    """检索结果统一封装 — 无论知识来自文档还是自动拉取，格式一致。"""

    tables: list[TableSchema] = field(default_factory=list)
    field_semantics: list[dict] = field(default_factory=list)
    business_rules: list[dict] = field(default_factory=list)
    sql_templates: list[dict] = field(default_factory=list)

    def merge(self, other: "SchemaSnapshot") -> "SchemaSnapshot":
        """合并另一个 SchemaSnapshot。同名表以合并 columns，后者优先。"""
        existing_names = {t.name for t in self.tables}
        for table in other.tables:
            if table.name not in existing_names:
                self.tables.append(table)
            else:
                target = next(t for t in self.tables if t.name == table.name)
                existing_cols = {c.name for c in target.columns}
                for col in table.columns:
                    if col.name not in existing_cols:
                        target.columns.append(col)
                target.relations.extend(table.relations)
                if table.description and not target.description:
                    target.description = table.description
                if table.row_count_estimate and not target.row_count_estimate:
                    target.row_count_estimate = table.row_count_estimate
        self.field_semantics.extend(other.field_semantics)
        self.business_rules.extend(other.business_rules)
        self.sql_templates.extend(other.sql_templates)
        return self

    def to_prompt_text(self) -> str:
        """格式化为 LLM Prompt 可用的 Markdown 文本。"""
        sections: list[str] = []

        if self.tables:
            sections.append("## 数据库表结构\n")
            for t in self.tables:
                sections.append(f"### {t.name}")
                if t.description:
                    sections.append(f"{t.description}\n")
                if t.row_count_estimate:
                    sections.append(f"估算行数: {t.row_count_estimate:,}")
                sections.append("| 字段 | 类型 | 说明 |")
                sections.append("|------|------|------|")
                for c in t.columns:
                    pk = " PK" if c.is_primary_key else ""
                    nullable = "" if c.is_nullable else " NOT NULL"
                    sections.append(
                        f"| {c.name}{pk} | {c.type}{nullable} | {c.comment} |"
                    )
                if t.relations:
                    sections.append("\n**关联**:")
                    for r in t.relations:
                        sections.append(
                            f"- {r.relation_type} → {r.target_table} ON {r.join_key}"
                        )
                sections.append("")

        if self.field_semantics:
            sections.append("## 关键字段说明")
            for fs in self.field_semantics:
                sections.append(f"- {fs.get('content', '')}")
            sections.append("")

        if self.business_rules:
            sections.append("## 业务规则与指标口径")
            for br in self.business_rules:
                sections.append(f"- {br.get('content', '')}")
            sections.append("")

        if self.sql_templates:
            sections.append("## 相似问题参考")
            for tpl in self.sql_templates:
                sections.append(f"- 问题: {tpl.get('content', '')}")
                if sql := tpl.get("sql"):
                    sections.append(f"  SQL: {sql}")
            sections.append("")

        return "\n".join(sections)
