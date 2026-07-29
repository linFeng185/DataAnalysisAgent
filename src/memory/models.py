"""7.1+7.2+7.3 记忆系统数据模型。

依据: SPEC §3.8 记忆系统
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


# ── 7.3.1 长期记忆类型 ──────────────────────────────

class MemoryType(Enum):
    USER_PREFERENCE = "user_preference"
    SQL_TEMPLATE = "sql_template"
    LEARNED_PATTERN = "learned_pattern"
    CORRECTION = "correction"
    PROJECT_RULE = "project_rule"


# ── 7.3.2 长期记忆条目 ──────────────────────────────

@dataclass
class LongTermMemory:
    """长期记忆条目 — 跨会话持久化的可复用知识。"""

    id: str
    memory_type: MemoryType
    scope: str                       # "user:{id}" | "project:{id}"
    content: str                     # 自然语言描述，用于向量检索
    payload: dict                    # 结构化数据 (SQL 模板、参数等)
    embedding: list[float] | None = None
    created_at: datetime = field(default_factory=datetime.now)
    last_accessed_at: datetime = field(default_factory=datetime.now)
    access_count: int = 0
    confidence: float = 1.0
    ttl_days: int | None = None
    visibility: str = "private"
    tenant_id: int = 1
    owner_user_id: int = 0

    # 方法作用：记录一次有效召回并刷新最近访问时间。
    # Args: self - 当前长期记忆。
    # Returns: 无返回值。
    def touch(self) -> None:
        self.access_count += 1
        self.last_accessed_at = datetime.now(timezone.utc)

    # 方法作用：判断记忆在指定时间是否已经超过 TTL。
    # Args: self - 当前长期记忆；now - 比较时间，缺省为当前 UTC 时间。
    # Returns: 已过期返回 True，永久记忆返回 False。
    def is_expired(self, now: datetime | None = None) -> bool:
        if self.ttl_days is None:
            return False
        current = now or datetime.now(timezone.utc)
        created = _as_utc(self.created_at)
        return created + timedelta(days=self.ttl_days) <= _as_utc(current)

    # 方法作用：序列化长期记忆为 PostgreSQL 结构化字段。
    # Args: self - 当前长期记忆。
    # Returns: 包含身份、生命周期和业务载荷的字典。
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "memory_type": self.memory_type.value,
            "scope": self.scope,
            "content": self.content,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
            "last_accessed_at": self.last_accessed_at.isoformat(),
            "access_count": self.access_count,
            "confidence": self.confidence,
            "ttl_days": self.ttl_days,
            "visibility": self.visibility,
            "tenant_id": self.tenant_id,
            "owner_user_id": self.owner_user_id,
        }

    # 方法作用：从持久化字典恢复长期记忆并兼容旧 scope 数据。
    # Args: cls - LongTermMemory 类；data - 持久化字段。
    # Returns: 恢复后的长期记忆。
    @classmethod
    def from_dict(cls, data: dict) -> LongTermMemory:
        return cls(
            id=data["id"],
            memory_type=MemoryType(data["memory_type"]),
            scope=data.get("scope", ""),
            content=data["content"],
            payload=data.get("payload", {}),
            created_at=_parse_dt(data.get("created_at")),
            last_accessed_at=_parse_dt(data.get("last_accessed_at")),
            access_count=data.get("access_count", 0),
            confidence=data.get("confidence", 1.0),
            ttl_days=data.get("ttl_days"),
            visibility=data.get("visibility", "private"),
            tenant_id=int(data.get("tenant_id", 1) or 0),
            owner_user_id=int(data.get("owner_user_id", 0) or 0),
        )


# ── 7.2.2 单轮对话 ─────────────────────────────────

@dataclass
class ConversationTurn:
    """单轮对话记录。"""

    turn_id: int
    user_query: str
    generated_sql: str | None = None
    execution_success: bool = False
    analysis_summary: str | None = None
    chart_type: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)


# ── 7.2.1 会话上下文 ────────────────────────────────

@dataclass
class SessionContext:
    """会话上下文 — 从 Checkpointer 恢复的当前会话状态。"""

    session_id: str
    thread_id: str
    user_id: str = "anonymous"
    created_at: datetime = field(default_factory=datetime.now)
    last_active_at: datetime = field(default_factory=datetime.now)
    conversation_history: list[ConversationTurn] = field(default_factory=list)
    current_datasource: str | None = None
    current_tables: list[str] = field(default_factory=list)
    last_sql: str | None = None
    last_result_summary: str | None = None


def _parse_dt(val: Any) -> datetime:
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        return _as_utc(datetime.fromisoformat(val.replace("Z", "+00:00")))
    return datetime.now(timezone.utc)


# 方法作用：把有无时区的 datetime 统一为 UTC，避免 TTL 比较类型冲突。
# Args: value - 待规范化时间。
# Returns: 带 UTC 时区的 datetime。
def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
