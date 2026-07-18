"""结构化资产只读 SQL 执行层，优先使用 DuckDB。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import sqlglot
from sqlglot import exp

from src.knowledge.structured_assets import StructuredAssetAdapter
from src.logging_config import get_logger

logger = get_logger(__name__)


class StructuredQueryError(ValueError):
    """结构化 SQL 不合法、依赖缺失或超过资源上限时抛出的异常。"""


@dataclass
class StructuredQueryResult:
    """结构化 SQL 的结果、表映射和截断状态。"""

    rows: list[dict[str, Any]]
    columns: list[str]
    row_count: int
    truncated: bool
    engine: str
    sql: str
    tables: dict[str, str] = field(default_factory=dict)

    # 方法作用：把查询结果转换成 API 和分析节点可用的字典。
    # Args: self - 查询结果对象。
    # Returns: JSON 兼容的查询结果字典。
    def to_dict(self) -> dict[str, Any]:
        logger.debug("结构化查询结果序列化入口", row_count=self.row_count)
        result = {
            "rows": self.rows,
            "columns": self.columns,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "engine": self.engine,
            "sql": self.sql,
            "tables": self.tables,
        }
        logger.info("结构化查询结果序列化完成", row_count=self.row_count)
        return result


class StructuredQueryEngine:
    """把 CSV/Excel/Parquet 注册为临时表并执行受控只读 SQL。"""

    # 方法作用：初始化查询执行器并区分扫描行数与返回行数上限。
    # Args: max_rows - 单次查询最多返回的行数；max_bytes - 文件读取上限；max_scan_rows - 单次最多扫描行数。
    # Returns: 无返回值。
    def __init__(self, max_rows: int = 100_000, max_bytes: int = 100 * 1024 * 1024,
                 max_scan_rows: int = 1_000_000) -> None:
        logger.debug("结构化查询执行器初始化入口", max_rows=max_rows, max_bytes=max_bytes,
                     max_scan_rows=max_scan_rows)
        if max_rows <= 0 or max_bytes <= 0 or max_scan_rows <= 0:
            raise ValueError("结构化查询资源上限必须大于零")
        self.max_rows = max_rows
        self.max_scan_rows = max_scan_rows
        self.adapter = StructuredAssetAdapter(max_bytes=max_bytes, max_rows=max_scan_rows)
        logger.info("结构化查询执行器初始化完成", max_rows=max_rows, max_scan_rows=max_scan_rows)

    # 方法作用：校验 SQL 为单条只读查询，且只引用已注册的资产表。
    # Args: sql - 待执行 SQL；allowed_tables - 当前资产注册的表名集合。
    # Returns: 去除末尾分号的原始 SQL。
    def validate_sql(self, sql: str, allowed_tables: set[str]) -> str:
        logger.debug("结构化 SQL 校验入口", sql_preview=sql[:160], allowed_tables=sorted(allowed_tables))
        clean = (sql or "").strip().rstrip(";").strip()
        if not clean:
            raise StructuredQueryError("SQL 不能为空")
        if ";" in clean:
            raise StructuredQueryError("只允许执行单条只读 SQL")
        lowered = clean.lower()
        if re.search(r"\b(insert|update|delete|drop|alter|create|copy|attach|install|load)\b", lowered):
            raise StructuredQueryError("结构化资产只允许只读 SELECT")
        if re.search(r"\b(read_csv|read_json|read_parquet|parquet_scan|httpfs)\s*\(", lowered):
            raise StructuredQueryError("禁止通过 SQL 访问资产之外的文件或网络")
        try:
            trees = sqlglot.parse(clean, read="duckdb")
        except Exception as exc:
            logger.error("结构化 SQL 解析失败", error=str(exc), exc_info=True)
            raise StructuredQueryError(f"SQL 语法无效: {exc}") from exc
        if len(trees) != 1:
            raise StructuredQueryError("只允许执行单条只读 SQL")
        tree = trees[0]
        if tree.find(exp.Select) is None:
            raise StructuredQueryError("结构化资产只允许只读 SELECT")
        cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
        referenced = {table.name for table in tree.find_all(exp.Table)}
        unknown = referenced - set(allowed_tables) - cte_names
        if unknown:
            raise StructuredQueryError(f"SQL 引用了未注册表: {', '.join(sorted(unknown))}")
        logger.info("结构化 SQL 校验完成", referenced=sorted(referenced))
        return clean

    # 方法作用：加载文件表、注册 DuckDB 临时表并执行受控 SQL。
    # Args: file_name - 文件名；content - 文件字节；sql - 只读查询；sheet_name - 可选 Excel sheet。
    # Returns: StructuredQueryResult 查询结果。
    async def execute(self, file_name: str, content: bytes, sql: str,
                      sheet_name: str | None = None) -> StructuredQueryResult:
        logger.debug("结构化 SQL 执行入口", file_name=file_name, sql_preview=sql[:160])
        duckdb = _load_duckdb()
        if duckdb is None:
            raise StructuredQueryError("结构化 SQL 执行需要安装 DuckDB，请安装 structured 可选依赖")
        try:
            fmt = self.adapter._detect_format(file_name)  # noqa: SLF001
            frames = self.adapter._load_frames(fmt, content, sheet_name=sheet_name,
                                               row_limit=self.max_scan_rows)  # noqa: SLF001
            table_frames = _build_table_frames(frames)
            normalized_sql = self.validate_sql(sql, set(table_frames))
            connection = duckdb.connect(database=":memory:")
            try:
                for table_name, (_original, frame) in table_frames.items():
                    connection.register(table_name, frame)
                limited_sql = f"SELECT * FROM ({normalized_sql}) AS _data_agent_result LIMIT {self.max_rows + 1}"
                result_frame = connection.execute(limited_sql).fetchdf()
            finally:
                connection.close()
            truncated = len(result_frame) > self.max_rows
            result_frame = result_frame.head(self.max_rows)
            rows = [
                {str(key): self.adapter._json_value(value) for key, value in row.items()}  # noqa: SLF001
                for row in result_frame.to_dict(orient="records")
            ]
            result = StructuredQueryResult(
                rows=rows,
                columns=[str(column) for column in result_frame.columns],
                row_count=len(rows),
                truncated=truncated,
                engine="duckdb",
                sql=normalized_sql,
                tables={name: original for name, (original, _frame) in table_frames.items()},
            )
            logger.info("结构化 SQL 执行完成", file_name=file_name, rows=len(rows), truncated=truncated)
            return result
        except StructuredQueryError:
            logger.warning("结构化 SQL 执行拒绝", file_name=file_name, exc_info=True)
            raise
        except Exception as exc:
            logger.error("结构化 SQL 执行失败", file_name=file_name, error=str(exc), exc_info=True)
            raise StructuredQueryError(f"结构化 SQL 执行失败: {exc}") from exc


# 方法作用：延迟加载可选 DuckDB 依赖，使 profile 功能不被执行引擎阻断。
# Args: 无。
# Returns: duckdb 模块；未安装时返回 None。
def _load_duckdb():
    logger.debug("加载 DuckDB 入口")
    try:
        import duckdb
        logger.info("加载 DuckDB 完成", version=getattr(duckdb, "__version__", "unknown"))
        return duckdb
    except ImportError:
        logger.warning("DuckDB 未安装，结构化 SQL 暂不可用")
        return None


# 方法作用：为每个 DataFrame 生成稳定且安全的 DuckDB 表别名。
# Args: frames - 原始表名到 DataFrame 的映射。
# Returns: DuckDB 表别名到 (原始表名, DataFrame) 的映射。
def _build_table_frames(frames: dict[str, pd.DataFrame]) -> dict[str, tuple[str, pd.DataFrame]]:
    logger.debug("构造结构化临时表入口", frame_count=len(frames))
    result: dict[str, tuple[str, pd.DataFrame]] = {}
    used: set[str] = set()
    for index, (original, frame) in enumerate(frames.items()):
        if len(frames) == 1:
            alias = "data"
        else:
            alias = re.sub(r"\W+", "_", str(original).strip().lower()).strip("_") or f"sheet_{index}"
            if alias[0].isdigit():
                alias = f"sheet_{alias}"
        if alias in used:
            alias = f"{alias}_{index}"
        used.add(alias)
        result[alias] = (str(original), frame)
    logger.info("构造结构化临时表完成", tables=sorted(result))
    return result
