"""对话图表的受约束语义构型与独立展示标签。"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChartPlan(BaseModel):
    """模型只选择真实字段的角色和明确聚合口径，不生成可执行代码。"""

    model_config = ConfigDict(extra="forbid")
    x: str = Field(default="", max_length=128)
    y: str = Field(default="", max_length=128)
    series: list[str] = Field(default_factory=list, max_length=8)
    facets: list[str] = Field(default_factory=list, max_length=8)
    metrics: list[str] = Field(default_factory=list, max_length=12)
    aggregation: Literal["none", "sum", "avg"] = "none"


_LABELS = {
    "date": "日期",
    "order_date": "订单日期",
    "order_month": "订单月份",
    "month": "月份",
    "year": "年份",
    "day": "日期",
    "time": "时间",
    "region": "地区",
    "region_code": "地区编码",
    "region_name": "地区名称",
    "product": "产品",
    "category": "类别",
    "category_name": "类别",
    "sales": "销售额",
    "total_sales": "销售总额",
    "sales_amount": "销售金额",
    "net_sales_amount": "净销售金额",
    "amount": "金额",
    "total_amount": "总金额",
    "revenue": "收入",
    "profit": "利润",
    "orders": "订单数",
    "order_count": "订单数",
    "total_orders": "订单总数",
    "count": "数量",
    "quantity": "数量",
    "users": "用户数",
    "user_count": "用户数",
    "period_type": "对比周期",
    "_datasource": "数据来源",
    "touchpoint": "触点",
    "attributed_value": "归因价值",
    "attribution_pct": "归因占比",
}
_LANGUAGES = {
    "zh-CN": ("中文", "汉语", "Chinese"),
    "en-US": ("英文", "英语", "English"),
    "ja-JP": ("日文", "日语", "日本語", "Japanese"),
    "ko-KR": ("韩语", "韩文", "Korean"),
    "fr-FR": ("法语", "法文", "French"),
    "de-DE": ("德语", "德文", "German"),
    "es-ES": ("西班牙语", "Spanish"),
}


# 按明确要求、问题文字、历史及界面默认值选择语言。
# Args: query - 用户问题；history - 既有轮次；fallback - 界面偏好。
# Returns: 展示语言代码，不修改用户原文。
def resolve_language(query: str, history: list[dict] | None = None, fallback: str = "zh-CN") -> str:
    """历史仅用于短追问，不让界面语言覆盖明确的自然语言请求。"""
    for language, names in _LANGUAGES.items():
        for name in names:
            if re.search(
                r"(?:用|使用|以|换成|切换到|in\s+|into\s+)" + re.escape(name), query, re.I
            ):
                return language
    if re.search(r"[\u3040-\u30ff]", query):
        return "ja-JP"
    if re.search(r"[\uac00-\ud7af]", query):
        return "ko-KR"
    if re.search(r"[\u4e00-\u9fff]", query):
        return "zh-CN"
    if re.search(r"[a-zA-Z]{2,}", query):
        return "en-US"
    for turn in reversed(history or []):
        previous = str(turn.get("user_query", "") or "")
        if previous:
            return resolve_language(previous, fallback=fallback)
    return fallback or "zh-CN"


# 将模型标签限制在真实结果字段内，缺失标签按语言兜底。
# Args: rows - 查询结果；descriptions - 模型展示名；language - 展示语言。
# Returns: 原始字段名到展示名的映射。
def column_labels(
    rows: list[dict], descriptions: dict | None = None, language: str = "zh-CN"
) -> dict[str, str]:
    """映射与数据分离，未知业务字段保留原名而非编造含义。"""
    columns = dict.fromkeys(column for row in rows for column in row)
    labels = {}
    for column in columns:
        supplied = str((descriptions or {}).get(column, "") or "").strip()[:80]
        default = (
            _LABELS.get(column.lower(), column)
            if language.startswith("zh")
            else column.replace("_", " ").strip().capitalize()
        )
        # 模型或历史中的原键名占位不应压过已有可靠中文映射；未知含义仍保留。
        if language.startswith("zh") and column.lower() in _LABELS:
            normalized = supplied.lower().replace("_", " ").strip()
            if normalized == column.lower().replace("_", " ").strip():
                supplied = ""
        labels[column] = supplied or default
    return labels


# 识别真实数值指标，排除布尔、日期整数和标识符。
# Args: rows - 查询结果。
# Returns: 保持输入顺序的数值指标列。
def numeric_columns(rows: list[dict]) -> list[str]:
    columns = dict.fromkeys(column for row in rows for column in row)
    return [
        column
        for column in columns
        if not re.search(r"(?:^id$|_id$|_code$|^seq$|^sort$|编号|序号|phone|mobile)", column, re.I)
        and not is_time_column(column, rows)
        and any(
            isinstance(row.get(column), (int, float, Decimal))
            and not isinstance(row.get(column), bool)
            for row in rows
        )
    ]


# 判断字段语义或实际值是否为时间，避免子串误判。
# Args: column - 字段名；rows - 查询结果。
# Returns: 是否可作为时间维度。
def is_time_column(column: str, rows: list[dict]) -> bool:
    markers = r"(?:^|_)(?:date|time|day|month|year|dt|timestamp|created_at|updated_at)(?:$|_)|日期|时间|月份|年份"
    return bool(re.search(markers, column, re.I)) or any(
        isinstance(row.get(column), (date, datetime)) for row in rows
    )


# 将数据转换为有限数值，缺失值保持空。
# Args: value - 查询值。
# Returns: 有限浮点数或 None。
def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except TypeError, ValueError, OverflowError:
        return None


# 将一组明确需要聚合的值按指定口径归并。
# Args: values - 同坐标数据；aggregation - 聚合方式。
# Returns: 数值或 None；未指定聚合而重复则拒绝。
def _reduce(values: list[Any], aggregation: str) -> float | None:
    if len(values) > 1 and aggregation == "none":
        raise ValueError("重复坐标缺少明确聚合口径，请增加系列/分面维度或指定聚合")
    numbers = [number for value in values if (number := _number(value)) is not None]
    if not numbers:
        return None
    if aggregation == "avg":
        return sum(numbers) / len(numbers)
    return sum(numbers)


# 构造不混淆空值、数值与文字的维度标识。
# Args: row - 查询行；columns - 维度字段。
# Returns: 可哈希的原始类型标识元组。
def _key(row: dict, columns: list[str]) -> tuple:
    return tuple((type(row.get(column)).__name__, str(row.get(column))) for column in columns)


# 构造维度展示标签而不改写业务值。
# Args: row - 查询行；columns - 维度字段；labels - 展示标签。
# Returns: 面板、系列或类别的说明。
def _name(row: dict, columns: list[str], labels: dict[str, str]) -> str:
    values = ["—" if row.get(column) is None else str(row[column]) for column in columns]
    if len(columns) > 1:
        return " · ".join(
            f"{labels.get(column, column)}: {value}"
            for column, value in zip(columns, values, strict=True)
        )
    return values[0] if values else ""


# 构建单个分面，所有数据角色由已校验方案提供。
# Args: rows - 分面数据；kind - 图表类型；plan - 方案；metrics/dimensions - 指标/维度；labels - 展示标签。
# Returns: ECharts 配置。
def _panel(
    rows: list[dict],
    kind: str,
    plan: ChartPlan,
    metrics: list[str],
    dimensions: list[str],
    labels: dict[str, str],
) -> dict:
    x_column = plan.x or next(
        (column for column in dimensions if is_time_column(column, rows)),
        dimensions[0] if dimensions else "",
    )
    groups = list(
        dict.fromkeys(
            plan.series
            + ([plan.y] if kind == "heatmap" and plan.y else [])
            + [column for column in dimensions if column != x_column]
        )
    )
    if kind == "scatter":
        x_column = plan.x or metrics[0]
        y_column = plan.y or (metrics[1] if len(metrics) > 1 else "")
        if x_column not in metrics or y_column not in metrics or x_column == y_column:
            raise ValueError("散点图需要两个不同的数值指标")
        groups = list(
            dict.fromkeys(
                plan.series + [column for column in dimensions if not is_time_column(column, rows)]
            )
        )
        grouped: dict[tuple, tuple[str, list]] = {}
        for row in rows:
            group = _key(row, groups)
            grouped.setdefault(group, (_name(row, groups, labels) or labels[y_column], []))
            x_value, y_value = _number(row.get(x_column)), _number(row.get(y_column))
            if x_value is not None and y_value is not None:
                grouped[group][1].append([x_value, y_value])
        if not any(values for _, values in grouped.values()):
            raise ValueError("散点图没有成对有效数值")
        return {
            "tooltip": {"trigger": "item"},
            "legend": {"type": "scroll"},
            "xAxis": {"type": "value", "name": labels[x_column], "scale": True},
            "yAxis": {"type": "value", "name": labels[y_column], "scale": True},
            "series": [
                {"name": name, "type": "scatter", "symbolSize": 9, "data": values}
                for name, values in grouped.values()
            ],
        }
    if not metrics or not x_column or x_column not in dimensions:
        raise ValueError("图表需要真实分类/时间维度和数值指标")
    x_rows = {_key(row, [x_column]): row for row in rows}
    categories = list(x_rows)
    if is_time_column(x_column, rows):
        categories.sort(key=lambda key: str(x_rows[key].get(x_column, "")))
    category_labels = [_name(x_rows[key], [x_column], labels) for key in categories]
    if kind == "pie":
        if len(metrics) != 1:
            raise ValueError("饼图每个分面只能使用一个指标")
        # 多维饼图按完整类别组合，绝不以同名类别覆盖原始记录。
        pie_dimensions = list(dict.fromkeys([x_column] + groups))
        values: dict[tuple, list] = defaultdict(list)
        names = {}
        for row in rows:
            key = _key(row, pie_dimensions)
            values[key].append(row.get(metrics[0]))
            names[key] = _name(row, pie_dimensions, labels)
        items = [
            {"name": names[key], "value": _reduce(value, plan.aggregation)}
            for key, value in values.items()
        ]
        if any(item["value"] is not None and item["value"] < 0 for item in items):
            raise ValueError("饼图不支持负值，请使用柱状图")
        items = [item for item in items if item["value"] is not None]
        if not items or not any(item["value"] > 0 for item in items):
            raise ValueError("饼图需要至少一个正数值")
        return {
            "tooltip": {"trigger": "item"},
            "legend": {"type": "scroll", "bottom": 0},
            "series": [
                {"name": labels[metrics[0]], "type": "pie", "radius": ["25%", "65%"], "data": items}
            ],
        }
    grouped_rows: dict[tuple, dict] = {}
    values = defaultdict(list)
    for row in rows:
        group, category = _key(row, groups), _key(row, [x_column])
        grouped_rows[group] = row
        for metric in metrics:
            values[(group, category, metric)].append(row.get(metric))
    group_keys = sorted(grouped_rows, key=lambda key: _name(grouped_rows[key], groups, labels))
    if kind == "heatmap":
        if not groups or len(metrics) != 1:
            raise ValueError("热力图每个分面需要两个维度和一个指标")
        points = []
        for group_index, group in enumerate(group_keys):
            for category_index, category in enumerate(categories):
                number = _reduce(values.get((group, category, metrics[0]), []), plan.aggregation)
                if number is not None:
                    points.append([category_index, group_index, number])
        if not points:
            raise ValueError("热力图没有有效数值")
        return {
            "tooltip": {"trigger": "item"},
            "xAxis": {"type": "category", "data": category_labels, "name": labels[x_column]},
            "yAxis": {
                "type": "category",
                "data": [_name(grouped_rows[key], groups, labels) for key in group_keys],
            },
            "visualMap": {
                "min": min(point[2] for point in points),
                "max": max(point[2] for point in points),
                "calculable": True,
                "orient": "horizontal",
                "bottom": 0,
            },
            "series": [{"name": labels[metrics[0]], "type": "heatmap", "data": points}],
        }
    series = []
    for group in group_keys:
        for metric in metrics:
            name = _name(grouped_rows[group], groups, labels)
            name = (
                f"{name} · {labels[metric]}"
                if name and len(metrics) > 1
                else name or labels[metric]
            )
            item = {
                "name": name,
                "type": kind,
                "data": [
                    _reduce(values.get((group, category, metric), []), plan.aggregation)
                    for category in categories
                ],
            }
            if kind == "line":
                item.update(
                    {"connectNulls": True, "smooth": False, "showSymbol": len(categories) <= 40}
                )
            series.append(item)
    option = {
        "tooltip": {"trigger": "axis" if kind == "line" else "item"},
        "legend": {"type": "scroll", "bottom": 0},
        "grid": {"left": 55, "right": 30, "top": 35, "bottom": 65, "containLabel": True},
        "xAxis": {
            "type": "category",
            "data": category_labels,
            "name": labels[x_column],
            "axisLabel": {"hideOverlap": True},
        },
        "yAxis": {"type": "value", "name": labels[metrics[0]]},
        "series": series,
    }
    if len(categories) > 20:
        option["dataZoom"] = [{"type": "inside"}, {"type": "slider", "bottom": 25}]
    return option


# 统一构建初始和手动切换图表，分面与多指标均保持独立轴。
# Args: rows - 原始数据；kind - 图表类型；plan - 模型角色方案；labels - 字段展示名。
# Returns: 可 JSON 序列化的 ECharts option；语义不适用时抛出 ValueError。
def build_option(
    rows: list[dict],
    kind: str,
    plan: dict | ChartPlan | None = None,
    labels: dict[str, str] | None = None,
) -> dict:
    """所有转换只读输入；未知字段拒绝，重复坐标不静默覆盖。"""
    if not rows:
        return {}
    if kind == "table":
        return {
            "columns": list(dict.fromkeys(column for row in rows for column in row)),
            "rows": rows,
        }
    if kind not in {"bar", "line", "pie", "scatter", "heatmap"}:
        raise ValueError("不支持的图表类型")
    selected = plan if isinstance(plan, ChartPlan) else ChartPlan.model_validate(plan or {})
    columns = list(dict.fromkeys(column for row in rows for column in row))
    referenced = [selected.x, selected.y, *selected.series, *selected.facets, *selected.metrics]
    if any(column and column not in columns for column in referenced):
        raise ValueError("图表方案引用不存在的字段")
    numeric = numeric_columns(rows)
    metrics = selected.metrics or (
        [selected.y] if selected.y and kind in {"line", "bar", "pie"} else numeric
    )
    if not metrics or any(column not in numeric for column in metrics):
        raise ValueError("图表指标必须是非标识符的数值列")
    # 地区或品类编码不是数值指标，但仍是有效的分类分组维度。
    dimensions = [
        column
        for column in columns
        if column not in numeric
        and not re.search(r"(?:^id$|_id$|^seq$|^sort$|编号|序号|phone|mobile)", column, re.I)
        and not (column.startswith("_") and len({_key(row, [column]) for row in rows}) <= 1)
    ]
    if any(column not in dimensions for column in [*selected.series, *selected.facets]):
        raise ValueError("系列与分面必须是维度字段")
    if selected.x and (selected.x in selected.series or selected.x in selected.facets):
        raise ValueError("横轴不能同时作为系列或分面")
    if selected.y and kind == "heatmap" and selected.y not in dimensions:
        raise ValueError("热力图纵轴必须是维度字段")
    if len(set(metrics)) != len(metrics) or set(selected.series) & set(selected.facets):
        raise ValueError("指标不能重复，系列与分面不能重叠")
    effective_labels = {column: (labels or {}).get(column, column) for column in columns}
    facets: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        facets[_key(row, selected.facets)].append(row)
    panels = []
    for facet_rows in facets.values():
        panel_metrics = [metrics] if kind == "scatter" else [[metric] for metric in metrics]
        for metric_group in panel_metrics:
            remaining = [column for column in dimensions if column not in selected.facets]
            option = _panel(facet_rows, kind, selected, metric_group, remaining, effective_labels)
            caption = _name(facet_rows[0], selected.facets, effective_labels)
            if len(metrics) > 1 and kind != "scatter":
                caption = " · ".join(filter(None, [caption, effective_labels[metric_group[0]]]))
            panels.append((caption, option))
    if len(panels) > 8:
        raise ValueError("分面过多，请筛选维度或指标后展示；完整数据仍保留在表格")
    if len(panels) == 1:
        return panels[0][1]
    result: dict[str, Any] = {
        "tooltip": panels[0][1]["tooltip"],
        "legend": {"type": "scroll", "bottom": 0},
        "title": [],
        "grid": [],
        "xAxis": [],
        "yAxis": [],
        "series": [],
    }
    height = 85 / len(panels)
    for index, (caption, option) in enumerate(panels):
        top = 3 + index * height
        result["title"].append(
            {"text": caption, "left": "center", "top": f"{top}%", "textStyle": {"fontSize": 12}}
        )
        if kind == "pie":
            for series in option["series"]:
                result["series"].append(
                    {
                        **series,
                        "center": ["50%", f"{top + height / 2}%"],
                        "radius": [0, f"{height * 0.35}%"],
                    }
                )
        else:
            result["grid"].append(
                {
                    "top": f"{top + 5}%",
                    "height": f"{max(5, height - 10)}%",
                    "left": 60,
                    "right": 45,
                    "containLabel": True,
                }
            )
            result["xAxis"].append({**option["xAxis"], "gridIndex": index})
            result["yAxis"].append({**option["yAxis"], "gridIndex": index})
            start = len(result["series"])
            result["series"].extend(
                {**series, "xAxisIndex": index, "yAxisIndex": index} for series in option["series"]
            )
            if "visualMap" in option:
                result.setdefault("visualMap", []).append(
                    {
                        **option["visualMap"],
                        "seriesIndex": list(range(start, len(result["series"]))),
                        "show": False,
                    }
                )
    return result


# 在手动切换类型时仅调整不兼容角色，保留维度、指标和聚合口径。
# Args: rows - 原始结果；kind - 新类型；plan - 旧图表方案。
# Returns: 适配后的真实字段方案；不存在的字段仍拒绝。
def adapt_plan(rows: list[dict], kind: str, plan: dict | None = None) -> dict:
    selected = ChartPlan.model_validate(plan or {})
    columns = list(dict.fromkeys(column for row in rows for column in row))
    referenced = [selected.x, selected.y, *selected.series, *selected.facets, *selected.metrics]
    if any(column and column not in columns for column in referenced):
        raise ValueError("图表方案引用不存在的字段")
    numeric = numeric_columns(rows)
    if kind == "scatter":
        if len(numeric) < 2:
            raise ValueError("散点图需要两个不同的数值指标")
        selected.x = (
            selected.x if selected.x in numeric else next(iter(selected.metrics or numeric))
        )
        selected.y = (
            selected.y
            if selected.y in numeric and selected.y != selected.x
            else next(column for column in numeric if column != selected.x)
        )
        selected.metrics = list(dict.fromkeys([selected.x, selected.y]))
    elif kind != "table":
        if selected.x in numeric:
            selected.x = ""
        if kind == "heatmap" and selected.y in numeric:
            selected.y = ""
        elif kind in {"line", "bar", "pie"} and selected.y not in numeric:
            selected.y = ""
    return selected.model_dump()
