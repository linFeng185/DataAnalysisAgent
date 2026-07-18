"""
知识条目数据模型 — KnowledgeSource 枚举 + KnowledgeEntry 数据结构。

KnowledgeEntry 是整个知识库系统的原子单元：表结构、字段语义、
业务规则、历史 SQL 模板、用户修正等所有知识都以统一格式存储于 ChromaDB。

优先级别（数值越小优先级越高）：
  1  MANUAL_DOC       — 人工编写文档
  2  USER_CORRECTION  — 用户纠正确认
  3  ORM_MODEL        — ORM 模型定义
  4  DB_COMMENT       — 数据库 COMMENT 字段
  5  AUTO_INTROSPECT  — 系统表自动拉取（兜底）
  6  SYSTEM_WARNING   — 权限/连接告警（不参与检索排序）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum


class KnowledgeSource(str, Enum):
    """知识来源枚举。"""

    MANUAL_DOC = "manual_doc"           # 人工编写文档（docs/metrics/ 目录）
    ORM_MODEL = "orm_model"             # Django/SQLAlchemy ORM 模型定义
    DB_COMMENT = "db_comment"           # 数据库 COMMENT 字段
    AUTO_INTROSPECT = "auto_introspect"  # DB 系统表自动拉取（source=auto）
    USER_CORRECTION = "user_correction"  # 用户手动修正
    SYSTEM_WARNING = "system_warning"    # 系统权限/连接告警


# 优先级映射（数值越小越优先）
_SOURCE_PRIORITY: dict[KnowledgeSource, int] = {
    KnowledgeSource.MANUAL_DOC: 1,
    KnowledgeSource.USER_CORRECTION: 2,
    KnowledgeSource.ORM_MODEL: 3,
    KnowledgeSource.DB_COMMENT: 4,
    KnowledgeSource.AUTO_INTROSPECT: 5,
    KnowledgeSource.SYSTEM_WARNING: 99,
}


def source_priority(source: KnowledgeSource) -> int:
    """返回知识来源的优先级数值（越小越优先）。"""
    return _SOURCE_PRIORITY.get(source, 50)


# 自动拉取的默认 TTL（7 天）
AUTO_TTL_SECONDS = int(timedelta(days=7).total_seconds())  # 604800


@dataclass
class KnowledgeEntry:
    """
    知识库中的一条原子记录。

    支持表级索引（category="table"）和字段级索引（category="column"），
    每条 entry 独立存储在 ChromaDB 中。
    """

    id: str
    """唯一标识: "table:datasource.orders" 或 "column:datasource.orders.amount" """

    content: str
    """自然语言描述文本，用于向量检索和 LLM Prompt 注入"""

    source: KnowledgeSource
    """知识来源，决定优先级和 TTL"""

    category: str
    """知识类型: "table" | "column" | "business_rule" | "sql_template" """

    table_name: str = ""
    """关联的表名"""

    column_name: str = ""
    """关联的字段名（仅字段级必填）"""

    tags: list[str] = field(default_factory=list)
    """标签列表，用于关键词过滤"""

    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    """创建时间（UTC）"""

    ttl: int = 0
    """生存时间（秒）。0=永不过期；auto 来源默认 604800（7天）"""

    metadata: dict = field(default_factory=dict)
    """扩展元数据（如字段类型、是否主键、枚举值列表等）"""

    asset_id: str = ""
    """关联的统一 DataAsset ID。"""

    source_file: str = ""
    """原始文件名或文档 URI。"""

    document_version: str = "v1"
    """原始文档或知识版本。"""

    locator: dict = field(default_factory=dict)
    """页码、段落、Sheet、单元格或结果行定位。"""

    checksum: str = ""
    """原始资产 checksum，用于幂等摄取。"""

    embedding_version: str = ""
    """向量模型/版本标识。"""

    def is_expired(self) -> bool:
        """判断该条目是否已过期。"""
        if self.ttl == 0:
            return False
        return datetime.now(timezone.utc) > self.created_at + timedelta(seconds=self.ttl)

    def to_dict(self) -> dict:
        """转为 ChromaDB metadata dict（不支持嵌套dict，metadata 序列化为 JSON 字符串）。"""
        import json as _json
        tags = self.tags if self.tags else [""]
        return {
            "id": self.id,
            "content": self.content,
            "source": self.source.value,
            "category": self.category,
            "table_name": self.table_name,
            "column_name": self.column_name,
            "tags": tags,
            "created_at": self.created_at.isoformat(),
            "ttl": str(self.ttl),
            "meta_json": _json.dumps(self.metadata, ensure_ascii=False) if self.metadata else "{}",
            "tenant_id": int(self.metadata.get("tenant_id", 1)),
            "owner_user_id": int(self.metadata.get("owner_user_id", 0)),
            "visibility": str(self.metadata.get("visibility", "tenant")),
            "asset_id": self.asset_id or str(self.metadata.get("asset_id", "")),
            "source_file": self.source_file or str(self.metadata.get("source_file", "")),
            "document_version": self.document_version,
            "locator_json": _json.dumps(self.locator, ensure_ascii=False),
            "checksum": self.checksum,
            "embedding_version": self.embedding_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> KnowledgeEntry:
        """从 dict 恢复 KnowledgeEntry（ChromaDB 读取路径）。"""
        import json as _json

        created_at = d.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif created_at is None:
            created_at = datetime.now(timezone.utc)

        source_str = d.get("source", "")
        if isinstance(source_str, KnowledgeSource):
            source = source_str
        elif isinstance(source_str, str) and source_str:
            try:
                source = KnowledgeSource(source_str)
            except ValueError:
                source = KnowledgeSource.AUTO_INTROSPECT
        else:
            source = KnowledgeSource.AUTO_INTROSPECT

        # ChromaDB 不支持嵌套 dict，metadata 以 JSON 字符串存储
        extra = {}
        meta_json = d.get("meta_json", "{}")
        try:
            extra = _json.loads(meta_json) if isinstance(meta_json, str) else {}
        except (_json.JSONDecodeError, TypeError):
            extra = {}
        for key in ("tenant_id", "owner_user_id", "visibility"):
            if key in d and key not in extra:
                extra[key] = d[key]

        ttl_str = d.get("ttl", "0")
        try:
            ttl = int(ttl_str) if isinstance(ttl_str, str) else int(ttl_str)
        except (ValueError, TypeError):
            ttl = 0

        tags = d.get("tags", [])
        if tags == [""] or tags is None:
            tags = []

        locator = d.get("locator", {})
        if not locator and isinstance(d.get("locator_json"), str):
            try:
                locator = _json.loads(d["locator_json"])
            except (_json.JSONDecodeError, TypeError):
                locator = {}

        return cls(
            id=d.get("id", ""),
            content=d.get("content", ""),
            source=source,
            category=d.get("category", ""),
            table_name=d.get("table_name", ""),
            column_name=d.get("column_name", ""),
            tags=list(tags),
            created_at=created_at,
            ttl=ttl,
            metadata=extra,
            asset_id=str(d.get("asset_id", extra.get("asset_id", "")) or ""),
            source_file=str(d.get("source_file", extra.get("source_file", "")) or ""),
            document_version=str(d.get("document_version", "v1") or "v1"),
            locator=locator if isinstance(locator, dict) else {},
            checksum=str(d.get("checksum", "") or ""),
            embedding_version=str(d.get("embedding_version", "") or ""),
        )
