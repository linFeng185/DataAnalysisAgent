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
        result = ChartGeneratorTool()._run(req.rows, chart_type=chart_type)
        if result.get("error"):
            raise ValueError(str(result["error"]))
    except ValueError as exc:
        logger.warning("图表调整请求无效", error=str(exc))
        raise HTTPException(422, str(exc)) from exc
    response = {
        "type": str(result["recommended_chart_type"]),
        "option": dict(result["option"]),
    }
    logger.info(
        "图表调整路由完成",
        row_count=len(req.rows),
        chart_type=response["type"],
    )
    return response
