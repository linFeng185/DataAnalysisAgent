"""报表工作台自然语言计划解析器。"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone
from typing import Any
from zoneinfo import ZoneInfo

from src.logging_config import get_logger
from src.reporting.models import (
    _CAUSAL_PHRASE_RE,
    AnalysisUnit,
    EstimatedResources,
    ReportPlan,
    SharePlan,
    TimeRange,
    divergence_unit_budget,
    max_plan_queries,
    max_plan_units,
)

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
# 报表规划任务标识，供 LLM 任务路由选择本地或租户配置的模型。
_PLAN_LLM_TASK = "report_plan"
# 计划期发散任务标识，与需求拆解分离，使其可独立降级与独立评测。
_DIVERGE_LLM_TASK = "report_diverge"
# 调度噪声词，否定前缀与词一起删除，避免把“不要定时”的残留“不要”当成分析单元。
# 否定前缀只收多字词：单字“别/不”会误伤“类别每月”“区分每周”这类常见业务表述。
_SCHEDULE_NOISE_RE = re.compile(
    r"(?:(?:不要|不需要|无需|不用|禁止)\s*)?"
    r"(?:立即|现在|立刻|先跑一次|以后|之后|定时|每小时|每天|每周[^，。 ]*|每月[^，。 ]*"
    r"|页面\s*保留\s*\d{1,3}\s*天|保留\s*\d{1,3}\s*天|执行\s*\d+\s*次|运行\s*\d+\s*次"
    r"|永久保存|永久有效|永不过期)"
)
# 用户书写的章节编号，支持 1./1、/1) 与 一、/一. 两类。
_SECTION_MARK_RE = re.compile(r"(?<![\d.])(?:\d{1,2}\s*[.、)](?!\d)|[一二三四五六七八九十]{1,2}\s*[、.])\s*")
# 片段首尾的并列连接词与标点，仅用于清理边界，不作为切分依据。
_PIECE_TRIM_RE = re.compile(r"^[\s，,、；;。：:和及以及]+|[\s，,、；;。：:和及以及]+$")
# 单元标识允许的字符集，与 models.AnalysisUnit 的校验保持一致。
_UNIT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# 时间范围单位换算，用于比较各单元跨度并让发散单元继承最长者。
_SPAN_UNIT_DAYS: dict[str, int] = {"d": 1, "w": 7, "m": 30, "q": 90, "y": 365}


# 方法作用：把时间范围值换算成天数，用于比较跨度长短。
# Args: time_range - 单元的 TimeRange。
# Returns: 估算天数；无法解析时返回 0。
def _span_days(time_range: Any) -> int:
    if getattr(time_range, "kind", "") == "absolute":
        try:
            start, end = str(time_range.value).split("/")
            return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days + 1
        except (ValueError, TypeError):
            return 0
    match = re.match(r"^(\d+)\s*([a-zA-Z]?)", str(getattr(time_range, "value", "") or "").strip())
    if not match:
        return 0
    return int(match.group(1)) * _SPAN_UNIT_DAYS.get(match.group(2).lower(), 1)
# 被否定的调度词，用户说“不要定时”时不能按定时意图处理；同样只认多字否定词。
_NEGATED_SCHEDULE_RE = re.compile(
    r"(?:不要|不需要|无需|不用|禁止)\s*(?:定时|以后|之后|周期|每小时|每天|每周|每月)"
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
        divergence: str = "standard",
    ) -> ReportPlan:
        logger.debug("报表计划解析入口", prompt_chars=len(prompt), datasource_count=len(datasources or []))
        normalized = " ".join(str(prompt).strip().split())
        if not normalized:
            raise ValueError("报表描述不能为空")
        requested = self._validate_datasources(datasources or [])
        # 先剔除被否定的调度词，"不要定时" 不能算作定时意图。
        schedule_text = _NEGATED_SCHEDULE_RE.sub(" ", normalized)
        frequency = self._parse_frequency(schedule_text)
        schedule_signal = any(word in schedule_text for word in ("定时", "以后", "之后", "周期", "每小时", "每天", "每周", "每月"))
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
        # 未绑定数据源的单元按“每个已选源各一次”计上界；已绑定源按 1 次计。
        source_upper_bound = max(1, len(requested))
        # 发散单元尚未生成，但成本必须按最坏情况预估：额度用满即为上界。
        divergent_upper_bound = min(divergence_unit_budget(divergence), max_plan_units() - len(units))
        fanout = sum(1 if unit.datasource else source_upper_bound for unit in units)
        fanout += max(0, divergent_upper_bound) * source_upper_bound
        plan = ReportPlan(
            execution_mode=execution_mode,
            frequency=frequency,
            timezone=timezone,
            max_runs=max_runs,
            page_ttl_days=page_ttl_days,
            permanent=permanent,
            requested_datasources=requested,
            analysis_units=units,
            divergence=divergence,
            share=share,
            warnings=warnings,
            needs_confirmation=execution_mode == "scheduled" or permanent or share.enabled,
            estimated=EstimatedResources(
                # 多源扇出：未绑定数据源的单元会在每个已选数据源上各跑一次查询，
                # 估算按 单元数 × 源数 的上界计算。只数单元数会低报约 N 倍，
                # 既误导预览，也让配额预占失真。实际命中数由执行期路由收窄。
                # 钳位必须读系统配置：写死常量会与 report_max_queries_per_run 脱钩。
                query_count=min(max_plan_queries(), max(1, fanout)),
                llm_tokens=min(2_000_000, max(1000, fanout * 1200)),
                estimated_seconds=min(3600, max(5, fanout * 8)),
            ),
        )
        if execution_mode == "scheduled" and frequency:
            plan.run_at, plan.schedule_day = self.parse_schedule_start(normalized, frequency, timezone)
        # 业务语义拆解优先交给结构化模型；无注入适配器时走统一 LLM 调用，两者失败都保留规则计划。
        if self._llm is not None:
            plan = await self._merge_structured_llm(plan, normalized)
        else:
            plan = await self._plan_units_with_llm(plan, normalized)
        # 年度标题属于全局分析范围，模型拆分后的问题不再含标题，必须显式保留。
        heading = _SECTION_MARK_RE.split(normalized, maxsplit=1)[0]
        year_match = re.search(r"(?<!\d)((?:19|20)\d{2})\s*年(?:度)?[^，。；:：]{0,40}(?:报表|报告|分析)", heading)
        if year_match:
            year = int(year_match.group(1))
            time_range = TimeRange(kind="absolute", value=f"{year}-01-01/{year}-12-31")
            for unit in plan.analysis_units:
                if unit.time_range == TimeRange():
                    unit.time_range = time_range.model_copy()
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

    # 方法作用：按用户书写的章节结构拆分业务主题，缺少章节结构时回退到分句切分。
    # Args: text - 用户报表描述。
    # Returns: 有界且唯一的 AnalysisUnit 列表。
    @staticmethod
    def _build_units(text: str) -> list[AnalysisUnit]:
        cleaned = _SCHEDULE_NOISE_RE.sub(" ", text)
        # 用户显式写了编号章节时，章节就是分析单元边界；章节内部的并列词不再切分。
        pieces = ReportPlanner._split_sections(cleaned)
        if len(pieces) < 2:
            pieces = ReportPlanner._split_clauses(cleaned)
        pieces = [piece for piece in pieces if piece and not ReportPlanner._is_fragment(piece)]
        if not pieces:
            pieces = [text[:4000]]
        units: list[AnalysisUnit] = []
        seen: set[str] = set()
        for index, piece in enumerate(pieces[:max_plan_units()], start=1):
            topic = piece[:4000]
            key = ReportPlanner._topic_key(topic, index)
            if key in seen:
                key = f"{key}_{index}"
            seen.add(key)
            units.append(
                AnalysisUnit(
                    unit_id=key,
                    question=topic,
                    metrics=ReportPlanner._infer_metrics(topic),
                    dimensions=ReportPlanner._infer_dimensions(topic),
                )
            )
        return units or [AnalysisUnit(unit_id="unit_1", question=text[:4000])]

    # 方法作用：按编号章节切分文本，并丢弃编号之前的标题前言。
    # Args: text - 已去除调度噪声的描述。
    # Returns: 章节正文列表；识别到的章节不足两段时返回空列表。
    @staticmethod
    def _split_sections(text: str) -> list[str]:
        marks = list(_SECTION_MARK_RE.finditer(text))
        if len(marks) < 2:
            return []
        blocks: list[str] = []
        for index, mark in enumerate(marks):
            end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
            block = ReportPlanner._trim_piece(text[mark.end():end])
            if block:
                blocks.append(block)
        return blocks

    # 方法作用：无章节结构时按分句标点切分，作为旧行为的兼容回退。
    # Args: text - 已去除调度噪声的描述。
    # Returns: 分句片段列表。
    @staticmethod
    def _split_clauses(text: str) -> list[str]:
        return [ReportPlanner._trim_piece(piece) for piece in re.split(r"[，,、；;]|以及|和|及", text)]

    # 方法作用：去掉片段首尾的连接词与标点，避免残留“、”“和”等边界字符。
    # Args: piece - 原始片段。
    # Returns: 清理并折叠空白后的片段。
    @staticmethod
    def _trim_piece(piece: str) -> str:
        return " ".join(_PIECE_TRIM_RE.sub("", piece).split())

    # 方法作用：识别只由调度噪声残留构成的无意义片段。
    # Args: piece - 已清理的片段。
    # Returns: 片段不含业务含义时返回 True。
    @staticmethod
    def _is_fragment(piece: str) -> bool:
        compact = re.sub(r"\s+", "", piece)
        if len(compact) >= 4:
            return False
        return not (ReportPlanner._infer_metrics(compact) or ReportPlanner._infer_dimensions(compact))

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

    # 方法作用：让注入的结构化适配器补充业务单元，不得覆盖安全和规则字段。
    # Args: plan - 规则解析后的计划；prompt - 原始规范文本。
    # Returns: 采用模型单元或保留规则单元并标注降级的计划。
    async def _merge_structured_llm(self, plan: ReportPlan, prompt: str) -> ReportPlan:
        try:
            result = self._llm.ainvoke(prompt) if hasattr(self._llm, "ainvoke") else self._llm(prompt)
            if hasattr(result, "__await__"):
                result = await result
            if not isinstance(result, dict):
                raise ValueError("结构化模型未返回计划对象")
            raw_units = result.get("analysis_units")
            if not isinstance(raw_units, list):
                raise ValueError("结构化模型未返回分析单元列表")
            proposed = self._coerce_llm_units(
                [item for item in raw_units if isinstance(item, dict)],
                allowed_datasources=set(plan.requested_datasources),
                prompt=prompt,
            )
            plan = self._apply_proposed_units(plan, proposed)
        except Exception as exc:
            logger.warning("报表结构化模型补充失败，回退规则计划", error_type=type(exc).__name__, error=str(exc))
            plan = self._with_planner_warning(plan)
        return plan

    # 方法作用：调用统一结构化 LLM 完成报表需求的业务语义拆解。
    # Args: plan - 规则计划；prompt - 用户原始描述。
    # Returns: 采用模型单元或保留规则单元并标注降级的计划。
    async def _plan_units_with_llm(self, plan: ReportPlan, prompt: str) -> ReportPlan:
        try:
            from src.llm.client import is_task_llm_available

            if not is_task_llm_available(_PLAN_LLM_TASK):
                raise RuntimeError("报表规划模型不可用")
            from src.llm.invocation import invoke_structured
            from src.llm.output_contracts import ReportPlanUnitsOutput
            from src.llm.prompt_budget import PromptSection

            allowed = plan.requested_datasources
            output = await invoke_structured(
                "report.plan",
                [
                    PromptSection(
                        "requirement",
                        f"## 报表需求\n{prompt}",
                        priority=100,
                        min_chars=100,
                        max_chars=4000,
                    ),
                    PromptSection(
                        "scope",
                        "## 可用数据源\n" + ("、".join(allowed) if allowed else "未指定，datasource 留空"),
                        priority=90,
                        min_chars=20,
                        max_chars=600,
                    ),
                ],
                output_model=ReportPlanUnitsOutput,
                task=_PLAN_LLM_TASK,
                temperature=0.0,
                # 模板里的 {max_units} 是 System Prompt 变量，必须走 system_values；
                # 放进 sections 会在 render 时抛 KeyError 并整批降级（report.diverge 踩过）。
                # 值取自配置，避免提示词里再写死一个与 report_max_units_per_run 脱钩的数字。
                system_values={"max_units": str(max_plan_units())},
                metadata={"node": "report_planner"},
            )
            proposed = self._coerce_llm_units(
                [unit.model_dump() for unit in output.analysis_units],
                allowed_datasources=set(allowed),
                prompt=prompt,
            )
            plan = self._apply_proposed_units(plan, proposed)
            logger.info("报表规划模型完成拆解", unit_count=len(plan.analysis_units))
        except Exception as exc:
            logger.warning("报表规划模型不可用，回退规则拆分", error_type=type(exc).__name__, error=str(exc))
            plan = self._with_planner_warning(plan)
        return plan

    # 方法作用：把模型建议的分析单元合并进已通过规则校验的计划。
    # Args: plan - 规则计划；proposed - 已规范化的分析单元。
    # Returns: 保留规则安全字段、替换业务单元的计划。
    @staticmethod
    def _apply_proposed_units(plan: ReportPlan, proposed: list[AnalysisUnit]) -> ReportPlan:
        if not proposed:
            raise ValueError("结构化模型未给出可用分析单元")
        allowed_datasources = set(plan.requested_datasources)
        if any(unit.datasource and unit.datasource not in allowed_datasources for unit in proposed):
            raise ValueError("结构化模型尝试使用未授权数据源")
        # 重新走完整计划校验，避免直接给 Pydantic 实例赋值绕过数据源和 unit_id 约束。
        candidate_data = plan.model_dump(mode="json")
        candidate_data["analysis_units"] = [unit.model_dump(mode="json") for unit in proposed]
        candidate = ReportPlan.model_validate(candidate_data)
        if set(candidate.requested_datasources) != allowed_datasources:
            raise ValueError("结构化模型改变了已授权数据源集合")
        return candidate

    # 方法作用：在计划期按档位额度提出补充分析单元。
    # Args: plan - 已拆解的计划；datasources - 已授权数据源清单；catalog - 可选的每源表名清单文本。
    # Returns: 追加了发散单元的计划；额度不足、模型不可用或调用失败时原样返回。
    async def diverge(
        self,
        plan: ReportPlan,
        *,
        datasources: list[str] | None = None,
        catalog: str = "",
    ) -> ReportPlan:
        divergence = str(plan.divergence)
        user_units = [unit for unit in plan.analysis_units if unit.origin == "user"]
        limit = min(divergence_unit_budget(divergence), max_plan_units() - len(user_units))
        if limit <= 0:
            # 额度为 0 或全局单元上限已满是正常路径，不是降级。
            logger.info("报表计划期发散跳过", divergence=divergence, user_units=len(user_units), limit=limit)
            return plan
        from src.llm.client import is_task_llm_available

        if not is_task_llm_available(_DIVERGE_LLM_TASK):
            logger.info("报表计划期发散跳过", reason="无可用模型", divergence=divergence)
            return plan
        summary = "\n".join(f"- {unit.question}" for unit in user_units)
        try:
            from src.llm.invocation import invoke_structured
            from src.llm.output_contracts import ReportDivergeOutput
            from src.llm.prompt_budget import PromptSection

            sections = [
                PromptSection("requirement", f"已确认的分析单元：\n{summary}", priority=100, min_chars=20, max_chars=4000),
                PromptSection("limit", f"最多提出 {limit} 个建议。", priority=90, min_chars=10, max_chars=200),
            ]
            if catalog:
                sections.append(PromptSection("catalog", f"各数据源表名清单：\n{catalog}", priority=80, min_chars=20, max_chars=30000))
            output = await invoke_structured(
                "report.diverge",
                sections,
                output_model=ReportDivergeOutput,
                task=_DIVERGE_LLM_TASK,
                temperature=0.0,
                # 模板里的 {limit} 必须走 system_values：它是 System Prompt 的变量，
                # 不是 user 段落。只放进 sections 会在 render 时抛 KeyError 并整批降级。
                system_values={"limit": str(limit)},
                metadata={"node": "report_planner_diverge"},
            )
        except Exception:
            # 发散失败只降级留痕，主链路必须继续。
            logger.warning("报表计划期发散失败，按原需求继续", divergence=divergence, exc_info=True)
            return self._with_warnings(plan, ["发散分析不可用，已按原需求生成计划"])
        proposed = self._coerce_diverge_units(
            [item.model_dump(mode="python") for item in output.proposed_units],
            allowed_datasources=set(datasources or plan.requested_datasources),
            decided_by="plan",
            limit=limit,
            reference_units=user_units,
        )
        if not proposed:
            logger.info("报表计划期发散无建议", divergence=divergence)
            return plan
        return self._with_warnings(
            plan.model_copy(update={"analysis_units": [*plan.analysis_units, *proposed]}),
            [f"系统按{divergence}档追加了 {len(proposed)} 个补充分析单元，可在预览中取消"],
        )

    # 方法作用：把发散建议规范化为受控的 AnalysisUnit。
    # Args: raw_units - 模型输出字典；allowed_datasources - 已授权数据源；decided_by - plan 或 runtime；limit - 本次采纳上限；reference_units - 用于继承时间范围的主单元。
    # Returns: 通过契约校验的发散单元列表。
    @staticmethod
    def _coerce_diverge_units(
        raw_units: list[dict[str, Any]],
        *,
        allowed_datasources: set[str],
        decided_by: str,
        limit: int,
        reference_units: list[AnalysisUnit],
    ) -> list[AnalysisUnit]:
        time_range = ReportPlanner._inherit_time_range(reference_units)
        units: list[AnalysisUnit] = []
        for raw in raw_units[:limit]:
            question = str(raw.get("question") or "").strip()
            rationale = str(raw.get("rationale") or "").strip()
            if not question:
                continue
            # 提示词只是软约束，因果措辞必须服务端硬校验。
            if _CAUSAL_PHRASE_RE.search(rationale):
                logger.warning("发散建议含因果措辞，已丢弃", question=question[:60])
                continue
            datasource = str(raw.get("datasource") or "").strip()
            if datasource not in allowed_datasources:
                # 越权或猜测的数据源清空，交给 report.route 重新定位。
                datasource = ""
            payload = {
                "question": question,
                "datasource": datasource,
                "rationale": rationale or "系统建议的补充分析方向",
                "origin": "diverged",
                "decided_by": decided_by,
                "priority": "secondary",
                "time_range": time_range.model_dump(mode="python"),
            }
            try:
                units.append(AnalysisUnit.model_validate(payload))
            except ValueError:
                logger.warning("发散建议未通过校验，已跳过", question=question[:60])
        return units

    # 方法作用：从主单元里挑出时间跨度最长的时间范围，供发散单元继承。
    # Args: units - 参考单元列表。
    # Returns: 选中的 TimeRange；没有主单元时返回默认值。
    @staticmethod
    def _inherit_time_range(units: list[AnalysisUnit]) -> Any:
        primaries = [unit for unit in units if unit.priority == "primary"] or list(units)
        if not primaries:
            from src.reporting.models import TimeRange

            return TimeRange()
        return max(primaries, key=lambda unit: _span_days(unit.time_range)).time_range

    # 方法作用：在计划上追加可读提示，保持原有字段不变。
    # Args: plan - 原计划；messages - 追加的提示语。
    # Returns: 追加提示后的计划。
    @staticmethod
    def _with_warnings(plan: ReportPlan, messages: list[str]) -> ReportPlan:
        merged = list(dict.fromkeys([*plan.warnings, *[item for item in messages if item.strip()]]))
        return plan.model_copy(update={"warnings": merged})

    # 方法作用：把模型给出的原始单元规范化为通过契约校验的 AnalysisUnit。
    # Args: raw_units - 模型输出的单元字典列表；allowed_datasources - 计划已授权数据源；prompt - 用户原始描述。
    # Returns: 跳过空问题与无法校验条目的分析单元列表。
    @staticmethod
    def _coerce_llm_units(
        raw_units: list[dict[str, Any]],
        allowed_datasources: set[str] | None = None,
        prompt: str = "",
    ) -> list[AnalysisUnit]:
        units: list[AnalysisUnit] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_units[:max_plan_units()], start=1):
            question = str(raw.get("question") or "").strip()
            if not question:
                continue
            key = str(raw.get("unit_id") or "").strip()
            if not _UNIT_KEY_RE.fullmatch(key):
                # 模型标识缺失或不合规时退回语义主题键，避免整批计划被拒绝。
                key = ReportPlanner._topic_key(question, index)
            if key in seen:
                key = f"{key}_{index}"
            seen.add(key)
            payload = {name: value for name, value in raw.items() if name != "unit_id"}
            payload.update({"unit_id": key, "question": question})
            # 模型没有 schema 依据：集合内的数据源若未在用户原文点名，只是猜测，清空后交给执行期定位；
            # 集合外的数据源保持原样，仍由授权校验拒绝，不能让清理动作绕过安全闸。
            datasource = str(payload.get("datasource") or "").strip()
            if datasource and datasource not in prompt and (allowed_datasources is None or datasource in allowed_datasources):
                payload["datasource"] = ""
            try:
                units.append(AnalysisUnit.model_validate(payload))
            except ValueError:
                logger.warning("报表规划模型给出的单元未通过校验，已跳过", unit_index=index)
        return units

    # 方法作用：在降级为规则拆分时补充可读提示，让用户知道当前单元来源。
    # Args: plan - 保留规则单元的计划。
    # Returns: 追加降级提示后的计划。
    @staticmethod
    def _with_planner_warning(plan: ReportPlan) -> ReportPlan:
        message = "模型规划不可用，已按规则拆分，建议确认分析单元"
        if message not in plan.warnings and len(plan.warnings) < 20:
            plan.warnings.append(message)
        return plan
