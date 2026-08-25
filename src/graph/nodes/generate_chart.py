"""4.9 generate_chart Node — 根据数据和分析结果生成 ECharts 配置。"""

from __future__ import annotations

import time
from decimal import Decimal

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)
_NUMERIC_TYPES = (int, float, Decimal)


# 方法作用：判断结果行是否具备至少一个维度和一个业务数值列。
# Args: rows - 待检查的查询结果行。
# Returns: 可形成确定性图表时返回 True，否则返回 False。
def _has_chart_candidate(rows: list[dict]) -> bool:
    """复用确定性资格规则，避免把单值或纯明细错误当成图表候选。"""
    return bool(_deterministic_chart_type(rows))


# 方法作用：在主查询和成功补查结果之间选择图表数据源。
# Args: state - 当前分析状态；analysis - 当前分析结果。
# Returns: 图表候选行和来源标识（primary/supplemental/none）。
def _select_chart_rows(state: AnalysisState, analysis: dict) -> tuple[list[dict], str]:
    """主查询优先；主查询仅为单值或不具备维度时才采用成功补查分组结果。"""
    primary = list(state.get("query_result_sample", []) or [])
    if _has_chart_candidate(primary):
        return primary, "primary"
    supplemental_candidates: list[tuple[list[dict], int]] = []
    for index, item in enumerate(state.get("supplemental_evidence", []) or []):
        if not isinstance(item, dict) or not item.get("success"):
            continue
        rows = list(item.get("data", []) or [])
        if _has_chart_candidate(rows):
            supplemental_candidates.append((rows, index))
    if supplemental_candidates:
        rows, index = supplemental_candidates[-1]
        logger.info(
            "图表候选切换到补查证据",
            primary_rows=len(primary),
            supplemental_candidate_count=len(supplemental_candidates),
            selected_evidence_index=index,
            selected_rows=len(rows),
        )
        return rows, "supplemental"
    return primary, "primary" if primary else "none"


# 方法作用：从结果行中寻找指定列首个非布尔数值。
# Args: rows - 查询结果行；column - 待检查列名。
# Returns: 首个数值；不存在时返回 None。
def _first_numeric_value(rows: list[dict], column: str):
    """忽略前置空值识别实际数值列。"""
    logger.debug("图表数值样本查找入口", column=column, row_count=len(rows))
    for row in rows:
        value = row.get(column)
        if isinstance(value, _NUMERIC_TYPES) and not isinstance(value, bool):
            logger.info("图表数值样本查找完成", column=column, found=True)
            return value
    logger.info("图表数值样本查找完成", column=column, found=False)
    return None


async def generate_chart_node(state: AnalysisState) -> dict:
    from src.graph.context import read_contexts

    contexts = read_contexts(state)
    _start = time.monotonic()
    logger.info(
        "节点开始",
        node="generate_chart",
        intent=contexts.routing.intent,
    )

    analysis = state.get("analysis_result", {}) or {}
    data, data_source = _select_chart_rows(state, analysis)
    logger.info(
        "图表候选选择完成",
        primary_rows=len(state.get("query_result_sample", []) or []),
        selected_rows=len(data),
        data_source=data_source,
        supplemental_count=len(state.get("supplemental_evidence", []) or []),
    )
    attribution = analysis.get("attribution", {}) or {}
    if contexts.routing.intent in {"metadata", "chat"}:
        return {"chart_config": _no_chart("元数据或直接问答不生成图表")}
    if attribution.get("status") == "insufficient":
        return {"chart_config": _no_chart("触点归因资格不足，不生成归因图表")}
    if attribution.get("status") == "applied":
        attribution_rows = list(attribution.get("results", []) or [])
        if not attribution_rows:
            return {"chart_config": _failed_chart("bar", "触点归因结果为空")}
        try:
            option = _build_attribution_option(attribution_rows)
        except Exception as exc:
            logger.error("触点归因图表配置生成失败", error=str(exc), exc_info=True)
            return {"chart_config": _failed_chart("bar", f"触点归因图表生成失败：{exc}")}
        return {"chart_config": {
            "type": "bar",
            "option": option,
            "status": "success",
            "reason": "",
            "data_source": "attribution",
        }}
    if not data:
        logger.info("节点完成（无数据）", node="generate_chart",
                    elapsed_ms=round((time.monotonic() - _start) * 1000))
        return {"chart_config": _no_chart("查询没有返回数据，无法生成图表")}
    core_value = _extract_core_value(data)
    if len(data) == 1 and core_value:
        logger.info("节点完成（核心数值）", node="generate_chart", column=core_value["column"])
        return {"chart_config": {
            **_no_chart("单行单值结果使用核心数值展示"),
            "core_value": core_value,
        }}

    chart_type = _pick_type(analysis, data)
    if chart_type == "table":
        return {"chart_config": _no_chart("当前结果缺少可形成图表的维度与数值组合")}

    try:
        option = _build_echarts_option(data, chart_type)
    except Exception as exc:
        logger.error("图表配置生成失败", chart_type=chart_type, error=str(exc), exc_info=True)
        return {"chart_config": _failed_chart(chart_type, f"图表配置生成失败：{exc}")}
    if not option:
        logger.warning("图表配置生成失败", chart_type=chart_type, reason="配置为空")
        return {"chart_config": _failed_chart(chart_type, "图表配置为空，请检查维度与数值字段")}

    elapsed = round((time.monotonic() - _start) * 1000)
    logger.info("节点完成", node="generate_chart", elapsed_ms=elapsed,
                chart_type=chart_type, data_rows=len(data))
    return {"chart_config": {
        "type": chart_type,
        "option": option,
        "status": "success",
        "reason": "",
        "data_source": data_source,
    }}


_SUPPORTED_TYPES = ("bar", "line", "pie", "scatter", "table")


# 根据分析推荐和数据列类型选择图表类型。
# Args: analysis - 分析节点输出；data - 查询结果数据。
# Returns: 受支持的 ECharts 图表类型。
def _pick_type(analysis: dict, data: list[dict]) -> str:
    rec = analysis.get("recommended_chart_type", "")
    deterministic = _deterministic_chart_type(data)
    logger.debug(
        "图表类型选择入口",
        recommended=rec,
        deterministic=deterministic,
        data_rows=len(data),
    )
    if rec == "table" and deterministic:
        logger.info(
            "图表类型选择完成",
            chart_type=deterministic,
            source="deterministic_override_table",
        )
        return deterministic
    if rec in _SUPPORTED_TYPES and rec != "table":
        logger.info("图表类型选择完成", chart_type=rec, source="analysis")
        return rec
    if deterministic:
        logger.info("图表类型选择完成", chart_type=deterministic, source="data_semantics")
        return deterministic
    if not data:
        logger.info("图表类型选择完成", chart_type="table", source="empty_data")
        return "table"
    cols = list(data[0].keys())
    if len(cols) >= 2:
        for col in cols[1:]:
            value = _first_numeric_value(data, col)
            if value is not None:
                low = col.lower()
                if any(w in low for w in ("phone", "tel", "mobile", "手机", "电话")):
                    logger.info("图表数值列跳过", column=col, reason="联系方式")
                    continue
                if (any(w in low for w in ("id", "no", "编号", "序号"))
                        and isinstance(value, int) and value > 999):
                    logger.info("图表数值列跳过", column=col, reason="标识符")
                    continue
                logger.info("图表类型选择完成", chart_type="bar", source="numeric_column")
                return "bar"
    logger.info("图表类型选择完成", chart_type="table", source="no_numeric_column")
    return "table"


# 方法作用：生成明确表示不适用的图表契约。
# Args: reason - 不生成图表的业务原因。
# Returns: 可直接进入公开响应的图表配置。
def _no_chart(reason: str) -> dict:
    return {"type": "none", "option": {}, "status": "not_applicable", "reason": reason}


# 方法作用：生成不会中断主查询的图表失败契约。
# Args: chart_type - 原计划图表类型；reason - 失败原因。
# Returns: 带失败状态和原因的图表配置。
def _failed_chart(chart_type: str, reason: str) -> dict:
    return {"type": chart_type, "option": {}, "status": "failed", "reason": reason[:300]}


# 方法作用：识别结果中所有真实数值列。
# Args: rows - 查询结果行。
# Returns: 至少存在一个非空数值的列名列表。
def _numeric_columns(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    return [
        str(column)
        for column in rows[0]
        if _first_numeric_value(rows, str(column)) is not None
        and not _is_identifier_column(str(column))
    ]


# 方法作用：判断数值列是否更可能是标识符而不是可视化指标。
# Args: column - 字段名。
# Returns: 主键、编码和序号类字段返回 True。
def _is_identifier_column(column: str) -> bool:
    normalized = column.lower().removeprefix("f_")
    if normalized in {"id", "sort", "sequence", "seq"}:
        return True
    return any(marker in normalized for marker in ("_id", "_code", "编号", "序号"))


# 方法作用：按数据列语义确定最小可用图表类型。
# Args: rows - 至少两行查询结果。
# Returns: line/bar，缺少维度数值组合时返回空字符串。
def _deterministic_chart_type(rows: list[dict]) -> str:
    if len(rows) < 2:
        return ""
    numeric = _numeric_columns(rows)
    if not numeric:
        return ""
    dimensions = [column for column in rows[0] if column not in numeric]
    time_column = next(
        (
            column for column in dimensions
            if any(marker in str(column).lower() for marker in (
                "date", "time", "day", "month", "year", "dt", "日期", "时间", "月份",
            ))
        ),
        "",
    )
    if time_column:
        return "line"
    if any(
        any(isinstance(row.get(column), str) for row in rows)
        for column in dimensions
    ):
        return "bar"
    return ""


# 方法作用：从单行结果提取唯一业务数值供核心数值组件展示。
# Args: rows - 查询结果行。
# Returns: label/value/column；不满足单值条件时返回空字典。
def _extract_core_value(rows: list[dict]) -> dict:
    if len(rows) != 1:
        return {}
    numeric = _numeric_columns(rows)
    if len(numeric) != 1:
        return {}
    column = numeric[0]
    return {
        "label": column,
        "value": rows[0].get(column),
        "column": column,
    }


def _build_echarts_option(rows: list[dict], chart_type: str) -> dict:
    if not rows:
        return {}
    keys = list(rows[0].keys())
    if chart_type in ("pie",):
        return _pie_option(rows, keys)
    if chart_type in ("line", "bar", "scatter"):
        return _axis_option(rows, keys, chart_type)
    return {}


# 方法作用：根据确定性触点归因结果生成固定柱状图配置。
# Args: rows - touchpoint/attributed_value/attribution_pct 结果行。
# Returns: 不重新计算归因数值的 ECharts 配置。
def _build_attribution_option(rows: list[dict]) -> dict:
    """图表只消费规则引擎结果，避免从原始路径行重新猜测字段。"""
    categories = [str(row.get("touchpoint", "") or "") for row in rows]
    values = [_to_num(row.get("attributed_value")) for row in rows]
    percentages = [_to_num(row.get("attribution_pct")) for row in rows]
    return {
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "category", "data": categories},
        "yAxis": {"type": "value", "name": "归因价值"},
        "series": [{
            "name": "归因价值",
            "type": "bar",
            "data": values,
            "attribution_pct": percentages,
        }],
    }


def _pie_option(rows: list[dict], keys: list[str]) -> dict:
    name_col = keys[0]
    val_col = keys[1] if len(keys) > 1 else None
    if not val_col:
        return {}
    items = [{"name": str(r.get(name_col, "")), "value": _to_num(r.get(val_col))}
             for r in rows[:30]]
    return {
        "tooltip": {"trigger": "item"},
        "series": [{"type": "pie", "radius": "60%", "data": items}],
    }


# 构建坐标轴图表配置，并支持 Decimal 数值及交叉分组数据。
# Args: rows - 查询结果行；keys - 数据列顺序；chart_type - 图表类型。
# Returns: 可直接传给 ECharts 的 option；无数值列时返回空字典。
def _axis_option(rows: list[dict], keys: list[str], chart_type: str) -> dict:
    logger.debug("坐标轴配置构建入口", chart_type=chart_type, row_count=len(rows), columns=keys)
    numeric_columns = _numeric_columns(rows)
    numeric_col = numeric_columns[0] if numeric_columns else None
    if not numeric_col:
        logger.warning("坐标轴配置回退", reason="无数值列", columns=keys)
        return {}
    dimension_columns = [column for column in keys if column not in numeric_columns]
    if not dimension_columns:
        logger.warning("坐标轴配置回退", reason="无维度列", columns=keys)
        return {}
    x_col = next(
        (
            column for column in dimension_columns
            if any(marker in column.lower() for marker in (
                "date", "time", "day", "month", "year", "dt", "日期", "时间", "月份",
            ))
        ),
        dimension_columns[0],
    )

    # 检测交叉透视：X 列值大量重复 → 应按第三列分组
    x_vals = [str(r.get(x_col, "")) for r in rows]
    uniq_x = len(set(x_vals))
    is_cross = uniq_x < len(rows) * 0.6 and uniq_x > 1 and len(keys) >= 3
    group_col = None
    if is_cross:
        logger.info(
            "交叉图分组选择边界",
            x_column=x_col,
            numeric_column=numeric_col,
            dimension_candidates=dimension_columns,
        )
        for col in dimension_columns:
            if col in {x_col, numeric_col}:
                continue
            if any(isinstance(row.get(col), str) for row in rows):
                group_col = col
                break
        logger.info("交叉图分组选择完成", group_column=group_col or "")

    if is_cross and group_col:
        # 交叉透视：按分组列拆多系列
        group_vals = sorted(set(str(r.get(group_col, "")) for r in rows))
        if len(group_vals) > 12:
            group_vals = group_vals[:12]
        categories = sorted(set(x_vals))
        series = []
        for gv in group_vals:
            data_map = {}
            for r in rows:
                if str(r.get(group_col, "")) == gv:
                    data_map[str(r.get(x_col, ""))] = _to_num(r.get(numeric_col))
            vals = [data_map.get(c, 0) for c in categories]
            series.append({"name": gv, "type": chart_type, "data": vals,
                           "smooth": chart_type == "line"})
        option = {
            "tooltip": {"trigger": "axis"},
            "legend": {"data": group_vals, "bottom": 0},
            "xAxis": {"type": "category", "data": categories,
                       "axisLabel": {"rotate": len(categories) > 8 and 45 or 0}},
            "yAxis": {"type": "value"},
            "series": series,
        }
        logger.info("坐标轴配置构建完成", mode="cross", series_count=len(series), category_count=len(categories))
        return option

    # 普通模式
    if len(rows) > 30:
        rows = rows[:25]
    categories = [str(r.get(x_col, "")) for r in rows]
    vals = [_to_num(r.get(numeric_col)) for r in rows]
    option = {
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "category", "data": categories,
                   "axisLabel": {"rotate": len(categories) > 8 and 45 or 0}},
        "yAxis": {"type": "value"},
        "series": [{"type": chart_type, "data": vals,
                     "smooth": chart_type == "line"}],
    }
    logger.info("坐标轴配置构建完成", mode="standard", series_count=1, category_count=len(categories))
    return option


def _to_num(val) -> float:
    if val is None:
        return 0
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    try:
        return float(str(val))
    except (ValueError, TypeError):
        return 0
