"""自动化调度、洞察和运行结果数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ScheduleDefinition:
    """租户和用户隔离的只读 SQL 自动化任务。"""

    id: str
    tenant_id: int
    user_id: int
    user_role: str
    name: str
    kind: str
    datasource: str
    sql: str
    dialect: str
    frequency: str
    threshold_pct: float = 10.0
    channels: list[str] = field(default_factory=lambda: ["in_app"])
    recipient_email: str = ""
    enabled: bool = True
    next_run_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_run_at: datetime | None = None


@dataclass(frozen=True)
class InsightEvent:
    """一个可审计指标相对上次成功运行的显著变化。"""

    metric: str
    current: float
    previous: float
    change: float
    change_pct: float
    direction: str

    # 方法作用：转换为可写入 JSONB 和 API 响应的普通字典。
    # Args: self - 洞察事件。
    # Returns: 包含指标、基准、变化和方向的字典。
    def to_dict(self) -> dict[str, str | float]:
        return {
            "metric": self.metric,
            "current": self.current,
            "previous": self.previous,
            "change": self.change,
            "change_pct": self.change_pct,
            "direction": self.direction,
        }
