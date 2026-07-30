"""主动洞察与定时报告的确定性执行器。"""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any

from src.automation.models import InsightEvent, ScheduleDefinition
from src.logging_config import get_logger

logger = get_logger(__name__)


# 方法作用：把查询结果中的每个数值列聚合为稳定指标总和。
# Args: rows - 已脱敏查询结果行。
# Returns: 按列名排序的数值指标。
def collect_numeric_metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, Decimal] = {}
    for row in rows:
        for key, value in row.items():
            if value is None or isinstance(value, bool):
                continue
            try:
                number = value if isinstance(value, Decimal) else Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                continue
            totals[str(key)] = totals.get(str(key), Decimal(0)) + number
    return {key: float(totals[key]) for key in sorted(totals)}


# 方法作用：比较两次成功运行指标并筛选超过阈值的增长或下降。
# Args: current - 当前指标；previous - 上次指标；threshold_pct - 绝对变化率阈值。
# Returns: 按指标名排序的洞察事件。
def detect_insights(
    current: dict[str, float],
    previous: dict[str, float],
    *,
    threshold_pct: float,
) -> list[InsightEvent]:
    threshold = max(0.0, float(threshold_pct))
    events: list[InsightEvent] = []
    for metric in sorted(set(current) & set(previous)):
        current_value = float(current[metric])
        previous_value = float(previous[metric])
        change = current_value - previous_value
        if previous_value == 0:
            change_pct = 0.0 if current_value == 0 else 100.0
        else:
            change_pct = change / abs(previous_value) * 100
        if abs(change_pct) < threshold:
            continue
        events.append(InsightEvent(
            metric=metric,
            current=round(current_value, 6),
            previous=round(previous_value, 6),
            change=round(change, 6),
            change_pct=round(change_pct, 2),
            direction="up" if change > 0 else "down" if change < 0 else "flat",
        ))
    return events


# 方法作用：用固定模板渲染包含指标摘要和最多 50 行数据的 Markdown 报告。
# Args: title - 报告标题；metrics - 汇总指标；rows - 已脱敏查询结果。
# Returns: Markdown 报告正文。
def render_markdown_report(
    title: str,
    metrics: dict[str, float],
    rows: list[dict[str, Any]],
) -> str:
    sections = [f"# {title}", "", "## 指标摘要"]
    if metrics:
        sections.extend(f"- {key}: {_format_number(value)}" for key, value in metrics.items())
    else:
        sections.append("- 无数值指标")
    sections.extend(["", "## 数据"])
    sample = rows[:50]
    if not sample:
        sections.append("无数据")
        return "\n".join(sections)
    columns = list(sample[0])
    sections.append("| " + " | ".join(columns) + " |")
    sections.append("| " + " | ".join("---" for _ in columns) + " |")
    for row in sample:
        sections.append("| " + " | ".join(
            str(row.get(column, "")).replace("|", "\\|").replace("\n", " ")
            for column in columns
        ) + " |")
    if len(rows) > len(sample):
        sections.extend(["", f"> 仅展示前 {len(sample)} 行，共 {len(rows)} 行。"])
    return "\n".join(sections)


# 方法作用：把浮点指标格式化为无冗余小数的报告文本。
# Args: value - 指标值。
# Returns: 紧凑十进制字符串。
def _format_number(value: float) -> str:
    return format(float(value), ".12g")


class ScheduledAnalysisRunner:
    """重新授权并执行一个调度任务，生成洞察或报告并记录运行。"""

    # 方法作用：注入持久化 Store、SQL executor 和通知分发器。
    # Args: self - Runner；store - 调度存储；executor - 可选 SQL 执行函数；dispatcher - 可选通知分发器。
    # Returns: 无返回值。
    def __init__(self, store: Any, *, executor: Any | None = None, dispatcher: Any | None = None) -> None:
        self._store = store
        self._executor = executor or execute_schedule_query
        if dispatcher is None:
            from src.automation.delivery import NotificationDispatcher

            dispatcher = NotificationDispatcher()
        self._dispatcher = dispatcher

    # 方法作用：执行单个任务并保证成功、失败和通知状态都持久化。
    # Args: self - Runner；schedule - 到期任务定义。
    # Returns: 运行结果摘要。
    async def run(self, schedule: ScheduleDefinition) -> dict[str, Any]:
        started_at = time.monotonic()
        logger.debug(
            "自动化任务执行入口",
            schedule_id=schedule.id,
            tenant_id=schedule.tenant_id,
            user_id=schedule.user_id,
            kind=schedule.kind,
            datasource=schedule.datasource,
        )
        try:
            rows = await self._executor(schedule)
            metrics = collect_numeric_metrics(rows)
            previous = await self._store.latest_success_payload(schedule.id)
            payload: dict[str, Any] = {
                "success": True,
                "row_count": len(rows),
                "metrics": metrics,
                "events": [],
                "report": "",
                "notification": None,
                "delivery": {},
            }
            if schedule.kind == "insight":
                previous_metrics = dict((previous or {}).get("metrics", {}) or {})
                events = detect_insights(
                    metrics,
                    previous_metrics,
                    threshold_pct=schedule.threshold_pct,
                ) if previous_metrics else []
                payload["events"] = [event.to_dict() for event in events]
                if events:
                    body = "\n".join(
                        f"- {event.metric}: {event.previous} -> {event.current} "
                        f"({event.change_pct:+.2f}%)"
                        for event in events
                    )
                    payload["notification"] = {
                        "kind": "insight",
                        "title": f"{schedule.name} 发现 {len(events)} 项变化",
                        "body": body,
                    }
            elif schedule.kind == "report":
                report = render_markdown_report(schedule.name, metrics, rows)
                payload["report"] = report
                payload["notification"] = {
                    "kind": "report",
                    "title": schedule.name,
                    "body": report,
                }
            else:
                raise ValueError(f"不支持的自动化任务类型: {schedule.kind}")

            if payload["notification"] is not None:
                payload["delivery"] = await self._dispatcher.deliver(
                    schedule,
                    payload["notification"]["title"],
                    payload["notification"]["body"],
                )
                failed_channels = [
                    channel for channel, status in payload["delivery"].items()
                    if status != "success"
                ]
                if failed_channels:
                    payload["success"] = False
                    payload["error"] = "通知投递失败"
                    payload["delivery_error_channels"] = failed_channels
                    await self._store.record_failure(
                        schedule,
                        f"通知渠道失败: {', '.join(failed_channels)}",
                    )
                    logger.warning(
                        "自动化任务通知失败，未记录成功基线",
                        schedule_id=schedule.id,
                        failed_channels=failed_channels,
                    )
                    return payload
            await self._store.record_success(schedule, payload, payload["notification"])
            logger.info(
                "自动化任务执行完成",
                schedule_id=schedule.id,
                kind=schedule.kind,
                row_count=len(rows),
                notified=payload["notification"] is not None,
                elapsed_ms=round((time.monotonic() - started_at) * 1000),
            )
            return payload
        except Exception as exc:
            logger.error(
                "自动化任务执行失败",
                schedule_id=schedule.id,
                kind=schedule.kind,
                error=str(exc)[:500],
                exc_info=True,
            )
            await self._store.record_failure(schedule, str(exc)[:500])
            return {"success": False, "error": "自动化任务执行失败"}


# 方法作用：按任务身份重新授权、注入行列策略并通过统一服务执行只读 SQL。
# Args: schedule - 调度任务定义。
# Returns: 已脱敏的有界查询结果。
async def execute_schedule_query(schedule: ScheduleDefinition) -> list[dict[str, Any]]:
    from src.app_context import get_tenant_policy
    from src.datasource.registry import get_registry
    from src.security.data_masker import mask_sensitive_data
    from src.security.permission_check import (
        check_column_whitelist,
        inject_row_filter,
        resolve_datasource_access,
    )
    from src.security.sql_execution import validate_and_execute_sql

    registry = get_registry()
    available = await registry.list_all()
    policy = get_tenant_policy()
    if policy.datasource_isolation_enabled:
        access = await resolve_datasource_access(
            available,
            [schedule.datasource],
            tenant_id=schedule.tenant_id,
            user_id=schedule.user_id,
            role=schedule.user_role,
            tenant_policy=policy,
        )
        permission = access[schedule.datasource]
    else:
        permission = {"allowed_columns": [], "row_filter_sql": ""}
    sql = schedule.sql
    allowed_columns = list(permission.get("allowed_columns", []) or [])
    if allowed_columns:
        error = check_column_whitelist(sql, allowed_columns)
        if error:
            raise PermissionError(error)
    row_filter = str(permission.get("row_filter_sql", "") or "")
    if row_filter:
        sql = inject_row_filter(sql, row_filter)
    result = await validate_and_execute_sql(
        sql,
        schedule.datasource,
        schedule.dialect,
        explain=True,
    )
    if not result.success:
        raise RuntimeError(result.error or "定时 SQL 执行失败")
    return mask_sensitive_data(result.data)


# 方法作用：获取当前 AppContext 共享的自动化单任务 Runner。
# Args: 无。
# Returns: ScheduledAnalysisRunner 单例。
def get_scheduled_analysis_runner() -> ScheduledAnalysisRunner:
    from src.app_context import get_app_context
    from src.automation.store import get_automation_store

    return get_app_context().get_or_create(
        "automation_runner",
        lambda: ScheduledAnalysisRunner(get_automation_store()),
    )
