"""5.1.6 DataAnalyzerTool — 封装统计分析逻辑供 Agent 调用。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)


class DataAnalyzerTool(BaseTool):
    """数据分析工具 — 对查询结果执行统计分析。"""

    name: str = "data_analyzer"
    description: str = (
        "对 SQL 查询返回的数据进行统计分析。"
        "输入: {\"rows\": [{\"col\": val, ...}], \"intent\": \"趋势分析\"}。"
        "返回: statistics/trend/outliers/concentration 分析结果。"
    )

    def _run(
        self,
        rows: list[dict] | str,
        intent: str = "query",
        run_manager: Any = None,
    ) -> dict:
        import json
        if isinstance(rows, str):
            try:
                rows = json.loads(rows)
            except json.JSONDecodeError:
                return {"error": "rows 参数需为 JSON 数组"}

        logger.info("数据分析工具调用", row_count=len(rows), intent=intent)

        try:
            from src.tools.analyzer import (
                compute_concentration,
                compute_statistics,
                compute_trend,
                detect_outliers_zscore,
            )

            stats = compute_statistics(rows)
            numeric_cols = stats.get("numeric_columns", [])
            result: dict[str, Any] = {"statistics": stats}

            if intent in ("trend", "aggregation"):
                time_col = _find_time_col(rows)
                if time_col and numeric_cols:
                    t = compute_trend(rows, time_col, numeric_cols[0])
                    result["trend"] = {"direction": t["trend"], "change_pct": t["change_pct"]}

            if numeric_cols and len(rows) >= 8:
                vals = [float(r.get(numeric_cols[0], 0) or 0) for r in rows]
                outliers = detect_outliers_zscore(vals)
                result["outliers"] = {"count": len(outliers), "indices": outliers}

            cat_col = _find_category_col(rows)
            if cat_col and numeric_cols and len(rows) > 1:
                grouped: dict[str, float] = {}
                for r in rows:
                    k = str(r.get(cat_col, "?"))
                    v = float(r.get(numeric_cols[0], 0) or 0)
                    grouped[k] = grouped.get(k, 0) + v
                vals = [v for _, v in sorted(grouped.items(), key=lambda x: x[1], reverse=True)]
                c = compute_concentration(vals, min(5, len(vals)))
                result["concentration"] = c

            return result
        except Exception as e:
            logger.error("数据分析失败", error=str(e))
            return {"error": str(e)}


def _find_time_col(rows: list[dict]) -> str | None:
    for k in rows[0]:
        if any(w in k.lower() for w in ("date", "time", "day", "month", "year", "dt", "created", "updated")):
            return k
    return None


def _find_category_col(rows: list[dict]) -> str | None:
    for k in rows[0]:
        if all(isinstance(r.get(k), str) for r in rows if r.get(k) is not None):
            return k
    return None
