"""样本超限时的处置决策：把"行数 + 时间跨度 + 是否聚合"交给模型判断。

设计边界：本模块只产出**可解释的处置建议**，不做自动重跑。建议写入单元快照与页面
披露信息，真正的重跑统一由补查回路处理，避免两条自动回路叠加互相打架。
"""

from __future__ import annotations

from typing import Any

# 输出契约统一放在 src/llm/output_contracts.py：prompts.py 注册 report.sample_guard
# 时要引用同一个类，留在 reporting 层会让 llm → reporting 反向依赖。
# 这里重导出，`from src.reporting.sample_guard import SampleDecision` 继续可用。
from src.llm.output_contracts import SampleDecision as SampleDecision
from src.logging_config import get_logger

logger = get_logger(__name__)

# 提示词标识与模型任务标识：项目约定 prompt_id 用点号、task 用下划线
# （如 report.route ↔ report_route），两者不可混用，否则任务级模型可用性判定失配。
_PROMPT_ID = "report.sample_guard"
_PROMPT_TASK = "report_sample_guard"
# 只喂少量样例行：超限现场本身很可能很大，探针不能反过来撑爆上下文。
_PROBE_ROW_LIMIT = 3
# 每次决策允许纳入的列名上限，防止宽表把提示词挤满。
_COLUMN_LIMIT = 40


# 方法作用：把列名与样例行渲染成提示词可读的探针文本。
# Args: question - 单元问题；columns - 列名清单；rows - 样例行；row_count/full_count -
#       进入分析链路与查询返回的行数；time_span - 本次查询覆盖的时间跨度。
# Returns: 探针文本；列名超过上限时只保留前 _COLUMN_LIMIT 个并标注省略。
def _render_probe(
    *,
    question: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    row_count: int,
    full_count: int,
    time_span: str,
) -> str:
    shown_columns = list(columns)[:_COLUMN_LIMIT]
    omitted = len(columns) - len(shown_columns)
    logger.debug(
        "样本探针渲染入口",
        column_count=len(columns),
        shown_columns=len(shown_columns),
        row_count=row_count,
        full_count=full_count,
    )
    lines = [
        f"## 单元问题\n{question}",
        f"## 返回行数\n进入分析链路 {row_count} 行；查询全量 {full_count} 行",
        f"## 时间跨度\n{time_span or '未知'}",
        f"## 列清单\n{', '.join(str(name) for name in shown_columns) or '（无）'}",
    ]
    if omitted > 0:
        lines.append(f"（另有 {omitted} 列未展示）")
    sample = list(rows)[:_PROBE_ROW_LIMIT]
    if sample:
        lines.append("## 样例行\n" + "\n".join(str(row) for row in sample))
    text = "\n".join(lines)
    logger.info("样本探针渲染完成", probe_chars=len(text), sample_rows=len(sample))
    return text


# 方法作用：调用模型判断超限样本应当聚合、缩小范围还是原样保留。
# Args: question - 单元问题；columns - 列名清单；rows - 样例行；row_count/full_count -
#       进入分析链路与查询返回的行数；time_span - 本次查询覆盖的时间跨度；limit - 样本上限。
# Returns: 模型的处置建议；模型不可用或决策失败时返回 None。
async def _invoke_sample_decision(
    *,
    question: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    row_count: int,
    full_count: int,
    time_span: str,
    limit: int,
) -> SampleDecision | None:
    from src.llm.client import is_task_llm_available

    if not is_task_llm_available(_PROMPT_TASK):
        logger.info("样本处置模型不可用，保留原始行集", task=_PROMPT_TASK)
        return None
    from src.llm.invocation import invoke_structured
    from src.llm.prompt_budget import PromptSection

    probe = _render_probe(
        question=question,
        columns=columns,
        rows=rows,
        row_count=row_count,
        full_count=full_count,
        time_span=time_span,
    )
    output = await invoke_structured(
        _PROMPT_ID,
        [
            PromptSection(
                "sample",
                f"## 样本上限\n{limit} 行\n\n{probe}",
                priority=100,
                min_chars=50,
                max_chars=6000,
            ),
        ],
        output_model=SampleDecision,
        task=_PROMPT_TASK,
        temperature=0.0,
        metadata={"node": "report_sample_guard"},
    )
    logger.info("样本处置决策完成", action=output.action, has_group_by=bool(output.suggested_group_by))
    return output


# 方法作用：判断是否需要进入样本处置决策（外层快速闸门与内层决策共用同一判据）。
# Args: row_count - 进入分析链路的行数；full_count - 查询返回的全量行数；
#       limit - 配置的样本上限；sample_capped - 执行层是否已上报样本被裁剪。
# Returns: 需要决策返回 True；上限未配置、或既未超限也未上报裁剪时返回 False。
def needs_sample_action(
    *,
    row_count: int,
    full_count: int,
    limit: int,
    sample_capped: bool = False,
) -> bool:
    """真实行数规模 max(进入链路, 查询全量) 超过上限，或执行层已上报样本裁剪。"""
    effective_limit = int(limit or 0)
    if effective_limit <= 0:
        # 上限没配好时宁可不决策：把 0 当上限会把所有单元一律判成超限。
        logger.debug("样本上限未配置，不进入处置决策", row_count=row_count, full_count=full_count)
        return False
    if sample_capped:
        # 上游裁剪事实必须有人披露：到手行数往往是被裁过的那一份，只比它永远比不出超限。
        logger.debug(
            "执行层已上报样本裁剪，进入处置决策",
            row_count=row_count,
            full_count=full_count,
            limit=effective_limit,
        )
        return True
    # 执行层是按同一个上限先裁再回传的：判定超限必须用「真实行数规模」，即进入链路行数
    # 与查询全量行数的较大者，不能只拿裁完的行数去比上限。
    overflow = max(int(row_count or 0), int(full_count or 0)) > effective_limit
    logger.debug(
        "样本处置判据求值",
        row_count=row_count,
        full_count=full_count,
        limit=effective_limit,
        overflow=overflow,
    )
    return overflow


# 方法作用：把「进入分析链路行数 + 查询全量行数」渲染成如实的计数说明。
# Args: entering - 进入分析链路的行数；full - 查询返回的全量行数。
# Returns: 计数说明文本；全量为 0 时写「不可得」，不拿进入链路行数冒充全量。
def _describe_counts(entering: int, full: int) -> str:
    full_text = f"查询全量 {full} 行" if full > 0 else "全量行数不可得（上游未上报）"
    return f"本次进入分析链路 {entering} 行，{full_text}"


# 方法作用：渲染「上游裁剪过、但真实行数未超限」这一分支的披露文案。
# Args: entering - 进入分析链路的行数；full - 查询返回的全量行数；limit - 样本上限。
# Returns: 基于真实计数的一句话披露文案。
def _describe_sample_capped_reason(entering: int, full: int, limit: int) -> str:
    return (
        f"上游执行层已对样本做裁剪；{_describe_counts(entering, full)}；"
        f"真实行数未超过样本上限 {limit} 行，保留当前样本并如实披露，未做任何额外裁剪。"
    )


# 方法作用：样本超限时给出处置建议，未超限时不消耗模型调用。
# Args: question - 单元问题；columns - 列名清单；rows - 样例行（可选）；row_count - 进入
#       分析链路的行数；full_count - 查询返回的全量行数；time_span - 可读时间跨度；
#       limit - 配置的样本上限；sample_capped - 执行层是否已上报样本被裁剪。
# Returns: SampleDecision；判据未触发时返回 None。
async def decide_sample_action(
    *,
    question: str,
    columns: list[str],
    rows: list[dict[str, Any]] | None = None,
    row_count: int,
    full_count: int,
    time_span: str = "",
    limit: int,
    sample_capped: bool = False,
) -> SampleDecision | None:
    """未超限返回 None；超限但模型不可用时返回保守的 keep 并写明行数。"""
    effective_limit = int(limit or 0)
    entering = int(row_count or 0)
    full = int(full_count or 0)
    # 与外部快速闸门共用 needs_sample_action，两处判据不可能脱钩：外层放行而内层卡死
    # 会让「样本被裁过」变成静默通过，真实行数在上游丢失后就再也没人披露。
    if not needs_sample_action(
        row_count=entering,
        full_count=full,
        limit=effective_limit,
        sample_capped=bool(sample_capped),
    ):
        logger.debug(
            "样本未超限，跳过处置决策",
            row_count=entering,
            full_count=full,
            limit=effective_limit,
        )
        return None
    if max(entering, full) <= effective_limit:
        # 只有「上游裁剪过」这一条成立：事实必须披露，但真实行数没超限，
        # 不值得为此白烧一次模型调用，直接给保守结论。
        logger.info(
            "样本被上游裁剪但未超限，直接登记披露",
            row_count=entering,
            full_count=full,
            limit=effective_limit,
        )
        return SampleDecision(
            action="keep",
            reason=_describe_sample_capped_reason(entering, full, effective_limit),
        )
    logger.info(
        "样本超限，请求模型决策",
        row_count=entering,
        full_count=full,
        limit=effective_limit,
        time_span=time_span,
    )
    try:
        decision = await _invoke_sample_decision(
            question=question,
            columns=columns,
            rows=list(rows or []),
            row_count=entering,
            full_count=full,
            time_span=time_span,
            limit=effective_limit,
        )
    except Exception:
        # 决策失败不得影响单元产出；保守保留原行集并如实披露。
        logger.error("样本处置决策失败，保留原始行集", exc_info=True)
        decision = None
    if decision is not None:
        return decision
    # 不得静默：模型不可用时也要留下"确实超限、暂未处理"的可解释记录。
    # 文字里的两个计数必须各自如实：进入链路行数可能远小于全量行数（执行层先裁再回传），
    # 把全量行数冒充成进入链路行数会让用户看到错数字。
    return SampleDecision(
        action="keep",
        reason=(
            f"{_describe_counts(entering, full)}，真实行数规模超过样本上限 {effective_limit} 行；"
            "模型不可用，保留原始行集并如实披露，未做任何裁剪。"
        ),
    )


# 方法作用：把处置建议渲染成页面披露用的一句话。
# Args: decision - 处置建议；limit - 样本上限。
# Returns: 可读文案；decision 为 None 时返回空串。
def describe_sample_decision(decision: SampleDecision | None, limit: int) -> str:
    logger.debug("样本处置文案渲染入口", action=getattr(decision, "action", ""))
    if decision is None:
        return ""
    label = {
        "aggregate": "建议在 SQL 层补充聚合",
        "narrow_range": "建议缩小取数范围",
        "keep": "保留原始行集",
    }.get(str(decision.action), str(decision.action))
    parts = [f"{label}（样本上限 {int(limit or 0)} 行）"]
    if decision.suggested_group_by:
        parts.append("建议分组：" + "、".join(str(name) for name in decision.suggested_group_by))
    if decision.suggested_range:
        parts.append("建议范围：" + str(decision.suggested_range))
    if decision.reason:
        parts.append(str(decision.reason))
    text = "；".join(parts)
    logger.info("样本处置文案渲染完成", text=text[:80])
    return text
