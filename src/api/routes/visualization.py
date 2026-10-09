"""查询结果图表重生成路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.api.schemas import ChartAdjustRequest
from src.logging_config import get_logger
from src.tools.chart_generator import ChartGeneratorTool, resolve_chart_type_instruction

logger = get_logger(__name__)
router = APIRouter()


@router.post("/charts/adjust")
# 方法作用：根据白名单类型指令复用已有数据重生成 ECharts 配置。
# Args: req - 原始查询结果行和图表调整指令。
# Returns: type/option 图表配置。
async def adjust_chart(req: ChartAdjustRequest):
    logger.debug(
        "图表调整路由入口",
        row_count=len(req.rows),
        instruction_chars=len(req.instruction),
    )
    try:
        chart_type = resolve_chart_type_instruction(req.instruction)
        if req.chart_plan or req.column_labels:
            from src.tools.chart_generator import classify_chart_type
            from src.tools.chart_presentation import adapt_plan, build_option

            selected_type = classify_chart_type(req.rows) if chart_type == "auto" else chart_type
            effective_plan = adapt_plan(req.rows, selected_type, req.chart_plan)
            result = {"recommended_chart_type": selected_type,
                      "option": build_option(req.rows, selected_type, effective_plan, req.column_labels)}
        else:
            effective_plan = {}
            result = ChartGeneratorTool()._run(req.rows, chart_type=chart_type)
        if result.get("error"):
            raise ValueError(str(result["error"]))
    except ValueError as exc:
        logger.warning("图表调整请求无效", error=str(exc))
        raise HTTPException(422, str(exc)) from exc
    response = {
        "type": str(result["recommended_chart_type"]),
        "option": dict(result["option"]),
        "chart_plan": effective_plan,
        "column_labels": req.column_labels,
    }
    logger.info(
        "图表调整路由完成",
        row_count=len(req.rows),
        chart_type=response["type"],
    )
    return response
