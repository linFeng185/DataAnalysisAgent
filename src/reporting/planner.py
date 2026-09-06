"""报表工作台自然语言计划解析器。"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone
from typing import Any
from zoneinfo import ZoneInfo

from src.logging_config import get_logger
from src.reporting.models import AnalysisUnit, EstimatedResources, ReportPlan, SharePlan

logger = get_logger(__name__)

_DATASOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_COUNT_RE = re.compile(r"(?:执行|运行|跑|共|连续)\s*(\d{1,4})\s*(?:次|回|天|周|月)?")
_TTL_RE = re.compile(r"(?:保留|有效期|保存)\s*(\d{1,3})\s*天")
_FREQUENCIES: tuple[tuple[str, str], ...] = (
    ("hourly", "每小时"),
    ("daily", "每天"),
    ("weekly", "每周"),
    ("monthly", "每月"),
)


class ReportPlanner:
    """按规则优先、可选结构化模型补充的计划解析器。"""

    # 方法作用：初始化可选的结构化 LLM 适配器，不在计划预览阶段强制访问远程模型。
    # Args: llm - 可选 Fake/本地结构化解析器。
    # Returns: 无返回值。
    def __init__(self, llm: Any | None = None) -> None:
        self._llm = llm

    # 方法作用：从用户自然语言生成可确认的 ReportPlan。
    # Args: prompt - 用户报表描述；datasources - 当前身份已选择的数据源；timezone - 租户时区。
    # Returns: 经过 Pydantic 校验的报表计划。
    async def parse(
        self,
        prompt: str,
        *,
        datasources: list[str] | None = None,
        timezone: str = "Asia/Shanghai",
    ) -> ReportPlan:
        logger.debug("报表计划解析入口", prompt_chars=len(prompt), datasource_count=len(datasources or []))
        normalized = " ".join(str(prompt).strip().split())
        if not normalized:
            raise ValueError("报表描述不能为空")
        requested = self._validate_datasources(datasources or [])
        frequency = self._parse_frequency(normalized)
        schedule_signal = any(word in normalized for word in ("定时", "以后", "之后", "周期", "每小时", "每天", "每周", "每月"))
        immediate_signal = any(word in normalized for word in ("立即", "现在", "立刻", "先跑一次", "执行一次"))
        warnings: list[str] = []
        execution_mode = "scheduled" if schedule_signal or frequency else "immediate"
        if execution_mode == "immediate" and not immediate_signal:
            warnings.append("未指定调度信息，按立即执行一次处理")
        count_match = _COUNT_RE.search(normalized)
        max_runs: int | None = int(count_match.group(1)) if count_match else (1 if execution_mode == "immediate" else None)
        if max_runs is not None and max_runs > 1000:
            raise ValueError("执行次数不能超过 1000 次")
        ttl_match = _TTL_RE.search(normalized)
        page_ttl_days = int(ttl_match.group(1)) if ttl_match else 7
        if not 1 <= page_ttl_days <= 90:
            raise ValueError("页面有效期必须在 1 到 90 天之间")
        permanent = any(word in normalized for word in ("永久保存", "永久有效", "永不过期"))
        share = self._parse_share(normalized)
        units = self._build_units(normalized)
        if execution_mode == "scheduled" and frequency is None:
            warnings.append("已识别定时意图，但缺少每小时/每天/每周/每月频率，需要确认")
        if permanent:
            warnings.append("永久有效需要 report.permanent 权限，服务端将再次校验")
        if share.scope == "user":
            warnings.append("团队成员授权首期仅支持指定用户或租户成员范围，不开放实时钻取")
        plan = ReportPlan(
            execution_mode=execution_mode,
            frequency=frequency,
            timezone=timezone,
            max_runs=max_runs,
            page_ttl_days=page_ttl_days,
            permanent=permanent,
            requested_datasources=requested,
            analysis_units=units,
            share=share,
            warnings=warnings,
            needs_confirmation=execution_mode == "scheduled" or permanent or share.enabled,
            estimated=EstimatedResources(
                query_count=min(16, max(1, len(units))),
                llm_tokens=min(2_000_000, max(1000, len(units) * 1200)),
                estimated_seconds=min(3600, max(5, len(units) * 8)),
            ),
        )
        if execution_mode == "scheduled" and frequency:
            plan.run_at, plan.schedule_day = self.parse_schedule_start(normalized, frequency, timezone)
        if self._llm is not None:
            plan = await self._merge_structured_llm(plan, normalized)
        logger.info(
            "报表计划解析完成",
            execution_mode=plan.execution_mode,
            frequency=plan.frequency or "",
            unit_count=len(plan.analysis_units),
            needs_confirmation=plan.needs_confirmation,
        )
        return plan

    # 方法作用：解析明确星期、时刻和月内日期，首次时间与锚点显示给用户确认。
    # Args: text - 用户描述；frequency - 已识别频率；timezone_name - IANA 时区；now - 可注入当前时间。
    # Returns: 首次 UTC 时间与可选月度锚点日。
    @staticmethod
    def parse_schedule_start(text: str, frequency: str, timezone_name: str, *, now: datetime | None = None) -> tuple[datetime, int | None]:
        """未指定时刻时首次立即执行；月末不足目标日时夹到该月末日。"""
        zone = ZoneInfo(timezone_name)
        current = (now or datetime.now(datetime_timezone.utc)).astimezone(zone)
        clock = re.search(r"(?<!\d)(\d{1,2})(?::(\d{2})|点(?:(\d{1,2})分?)?|时)(?!\d)", text)
        weekday = re.search(r"每周([一二三四五六日天])", text)
        month_day = re.search(r"每月\s*(\d{1,2})[日号]", text)
        if not any((clock, weekday, month_day)):
            return current.astimezone(datetime_timezone.utc), None
        hour = int(clock.group(1)) if clock else current.hour
        minute = int(clock.group(2) or clock.group(3) or 0) if clock else current.minute
        if clock and any(marker in text[:clock.start()] for marker in ("下午", "晚上")) and hour < 12:
            hour += 12
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("调度时刻必须在 00:00 至 23:59 之间")
        candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        anchor_day = int(month_day.group(1)) if month_day else None
        if frequency == "weekly":
            target = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}[weekday.group(1)] if weekday else current.weekday()
            candidate += timedelta(days=(target - current.weekday()) % 7)
            if candidate <= current:
                candidate += timedelta(days=7)
        elif frequency == "monthly":
            anchor_day = anchor_day or current.day
            if not 1 <= anchor_day <= 31:
                raise ValueError("月度调度日期必须在 1 至 31 日之间")
            candidate = candidate.replace(day=min(anchor_day, calendar.monthrange(current.year, current.month)[1]))
            if candidate <= current:
                year, month_zero = divmod(current.year * 12 + current.month, 12)
                candidate = candidate.replace(year=year, month=month_zero + 1, day=min(anchor_day, calendar.monthrange(year, month_zero + 1)[1]))
        elif frequency == "daily":
            if candidate <= current:
                candidate += timedelta(days=1)
        else:
            raise ValueError("每小时调度不能同时指定每天的固定时刻")
        return candidate.astimezone(datetime_timezone.utc), anchor_day

    # 方法作用：校验客户端提交的数据源候选，不接受 SQL 或路径表达式。
    # Args: datasources - 用户提交的数据源名称。
    # Returns: 去重后的数据源名称列表。
    @staticmethod
    def _validate_datasources(datasources: list[str]) -> list[str]:
        result: list[str] = []
        for value in datasources:
            name = str(value).strip()
            if not name:
                continue
            if not _DATASOURCE_RE.fullmatch(name):
                raise ValueError("数据源名称格式无效")
            if name not in result:
                result.append(name)
        return result

    # 方法作用：识别中文周期词并映射到数据库调度枚举。
    # Args: text - 已规范化的用户文本。
    # Returns: hourly/daily/weekly/monthly 或 None。
    @staticmethod
    def _parse_frequency(text: str) -> str | None:
        for value, keyword in _FREQUENCIES:
            if keyword in text:
                return value
        return None

    # 方法作用：把文本中的业务主题拆成独立分析单元。
    # Args: text - 用户报表描述。
    # Returns: 有界且唯一的 AnalysisUnit 列表。
    @staticmethod
    def _build_units(text: str) -> list[AnalysisUnit]:
        cleaned = re.sub(
            r"(?:立即|现在|立刻|先跑一次|以后|之后|定时|每小时|每天|每周[^，。 ]*|每月[^，。 ]*|执行\s*\d+\s*次|运行\s*\d+\s*次|保留\s*\d+\s*天|永久保存|永久有效|永不过期)",
            " ",
            text,
        )
        pieces = [
            re.sub(r"^[ ，,、和及以及]+|[ ，,、和及以及]+$", "", piece)
            for piece in re.split(r"[，,、；;]|以及|和|及", cleaned)
        ]
        pieces = [piece for piece in pieces if piece]
        if not pieces:
            pieces = [text[:4000]]
        units: list[AnalysisUnit] = []
        seen: set[str] = set()
        for index, piece in enumerate(pieces[:12], start=1):
            topic = piece[:4000]
            key = ReportPlanner._topic_key(topic, index)
            if key in seen:
                key = f"{key}_{index}"
            seen.add(key)
            metrics = ReportPlanner._infer_metrics(topic)
            dimensions = ReportPlanner._infer_dimensions(topic)
            units.append(AnalysisUnit(unit_id=key, question=topic, metrics=metrics, dimensions=dimensions))
        return units or [AnalysisUnit(unit_id="unit_1", question=text[:4000])]

    # 方法作用：为业务主题生成稳定的安全单元标识。
    # Args: topic - 业务主题；index - 主题序号。
    # Returns: 英文安全标识。
    @staticmethod
    def _topic_key(topic: str, index: int) -> str:
        mapping = (("销售", "sales"), ("订单", "orders"), ("库存", "inventory"), ("客户", "customers"), ("留存", "retention"), ("收入", "revenue"), ("成本", "cost"))
        for keyword, key in mapping:
            if keyword in topic:
                return key
        return f"unit_{index}"

    # 方法作用：根据关键词填充常见业务指标，供页面布局和分析提示使用。
    # Args: topic - 业务主题。
    # Returns: 去重后的指标名称。
    @staticmethod
    def _infer_metrics(topic: str) -> list[str]:
        mapping = {
            "销售": ["销售额", "订单数", "客单价"],
            "订单": ["订单数", "订单金额"],
            "库存": ["库存量", "库存周转"],
            "客户": ["客户数", "复购率"],
            "留存": ["留存率", "复购率"],
            "成本": ["成本", "毛利率"],
        }
        return list(dict.fromkeys(metric for keyword, values in mapping.items() if keyword in topic for metric in values))

    # 方法作用：根据关键词填充常见业务维度，供查询规划器使用。
    # Args: topic - 业务主题。
    # Returns: 去重后的维度名称。
    @staticmethod
    def _infer_dimensions(topic: str) -> list[str]:
        mapping = {"趋势": "日期", "按日": "日期", "按月": "月份", "品类": "品类", "地区": "地区", "客户": "客户分层"}
        return list(dict.fromkeys(value for keyword, value in mapping.items() if keyword in topic))

    # 方法作用：识别公开、团队或授权码分享意图。
    # Args: text - 用户报表描述。
    # Returns: 规范化分享计划。
    @staticmethod
    def _parse_share(text: str) -> SharePlan:
        if not any(word in text for word in ("分享", "公开", "团队成员", "同事查看")):
            return SharePlan()
        is_team = any(word in text for word in ("团队成员", "同事", "租户成员"))
        return SharePlan(
            enabled=True,
            scope="user" if is_team else "public",
            user_scope="tenant" if is_team else "none",
            require_code=any(word in text for word in ("授权码", "密码")),
        )

    # 方法作用：让可选结构化模型只补充业务单元，不得覆盖安全和规则字段。
    # Args: plan - 规则解析后的计划；prompt - 原始规范文本。
    # Returns: 保留规则约束的计划。
    async def _merge_structured_llm(self, plan: ReportPlan, prompt: str) -> ReportPlan:
        try:
            result = self._llm.ainvoke(prompt) if hasattr(self._llm, "ainvoke") else self._llm(prompt)
            if hasattr(result, "__await__"):
                result = await result
            if isinstance(result, dict) and isinstance(result.get("analysis_units"), list):
                proposed = [AnalysisUnit.model_validate(item) for item in result["analysis_units"][:12]]
                if proposed:
                    allowed_datasources = set(plan.requested_datasources)
                    if any(unit.datasource and unit.datasource not in allowed_datasources for unit in proposed):
                        raise ValueError("结构化模型尝试使用未授权数据源")
                    # 重新走完整计划校验，避免直接给 Pydantic 实例赋值绕过数据源和 unit_id 约束。
                    candidate_data = plan.model_dump(mode="json")
                    candidate_data["analysis_units"] = [unit.model_dump(mode="json") for unit in proposed]
                    candidate = ReportPlan.model_validate(candidate_data)
                    if set(candidate.requested_datasources) != allowed_datasources:
                        raise ValueError("结构化模型改变了已授权数据源集合")
                    plan = candidate
        except Exception as exc:
            logger.warning("报表结构化模型补充失败，回退规则计划", error_type=type(exc).__name__)
        return plan
