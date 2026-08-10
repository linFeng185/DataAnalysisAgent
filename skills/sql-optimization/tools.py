"""SQL 优化 Skill 工具。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger
from src.optimization.advisor import OptimizationAdvisor

logger = get_logger(__name__)


class OptimizeSqlTool(BaseTool):
    """生成 SQL 性能优化候选，不访问数据库。"""

    name: str = "optimize_sql"
    description: str = "基于静态规则、Schema 和索引元数据生成 SQL 优化建议，绝不执行 SQL。"

    # 方法作用：拒绝同步调用，统一 Skill 资源预算下的异步入口。
    # Args: args/kwargs - 兼容 BaseTool 的同步参数。
    # Returns: 永不返回，抛出 RuntimeError。
    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise RuntimeError("optimize_sql 只允许异步执行")

    # 方法作用：调用确定性建议器并返回 JSON 兼容结果。
    # Args: sql/dialect - SQL 与数据库方言；schema/indexes/statistics/execution_ms - 可选证据。
    # Returns: OptimizationReport 字典，executed 永远为 False。
    async def _arun(
        self,
        sql: str,
        dialect: str,
        schema: dict[str, Any] | None = None,
        indexes: dict[str, Any] | None = None,
        statistics: dict[str, Any] | None = None,
        execution_ms: float | None = None,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        del run_manager
        logger.info("SQL 优化 Skill 开始", dialect=dialect, sql_chars=len(sql or ""))
        report = OptimizationAdvisor().advise(
            sql,
            dialect,
            schema=schema,
            indexes=indexes,
            statistics=statistics,
            execution_ms=execution_ms,
        )
        result = report.model_dump(mode="json")
        result["executed"] = False
        logger.info("SQL 优化 Skill 完成", valid=report.valid, issue_count=len(report.issues))
        return result


_TOOLS: dict[str, BaseTool] = {"optimize_sql": OptimizeSqlTool()}


# 方法作用：按名称读取 SQL 优化 Skill 工具。
# Args: name - 工具名称。
# Returns: 找到的 BaseTool；不存在返回 None。
def get_tool(name: str) -> BaseTool | None:
    return _TOOLS.get(name)


# 方法作用：返回 SQL 优化 Skill 暴露的工具列表。
# Args: 无。
# Returns: BaseTool 列表。
def get_tools() -> list[BaseTool]:
    return list(_TOOLS.values())
