"""报告渲染前的确定性指标适配。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


# 方法作用：把全部查询结果行转换为报告模板允许的扁平指标映射。
# Args: rows - 查询结果行；max_metrics - 输出指标数量上限。
# Returns: 保留全部可容纳数值行且具有稳定标签的指标字典。
def build_report_metrics(
    rows: list[dict[str, Any]],
    *,
    max_metrics: int = 500,
) -> dict[str, int | float | Decimal]:
    """单行保留原列名，多行使用维度值标识每一行，避免只消费 rows[0]。"""
    if max_metrics <= 0:
        return {}
    if len(rows) == 1:
        return {
            str(key): value
            for key, value in rows[0].items()
            if not isinstance(value, bool) and isinstance(value, (int, float, Decimal))
        }

    metrics: dict[str, int | float | Decimal] = {}
    for row_index, row in enumerate(rows, start=1):
        labels = [
            str(value).strip()
            for value in row.values()
            if value not in (None, "")
            and not isinstance(value, bool)
            and not isinstance(value, (int, float, Decimal))
        ]
        row_label = " / ".join(labels[:3]) or f"第 {row_index} 行"
        for key, value in row.items():
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                continue
            metric_name = f"{row_label} / {key}"
            if metric_name in metrics:
                metric_name = f"{metric_name} [{row_index}]"
            metrics[metric_name] = value
            if len(metrics) >= max_metrics:
                return metrics
    return metrics
