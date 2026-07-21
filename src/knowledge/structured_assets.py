"""CSV、Excel、Parquet 的结构化资产读取与质量 profile。"""

from __future__ import annotations

import io
import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from src.logging_config import get_logger

logger = get_logger(__name__)


class StructuredAssetError(ValueError):
    """结构化资产无法解析或违反资源边界时抛出的异常。"""


@dataclass
class ColumnProfile:
    """单列数据质量和类型概览。"""

    name: str
    dtype: str
    null_count: int
    null_rate: float
    unique_count: int
    sample_values: list[Any] = field(default_factory=list)
    numeric: bool = False
    datetime: bool = False


@dataclass
class SheetProfile:
    """Excel sheet 或单个表文件的 profile。"""

    name: str
    row_count: int
    column_count: int
    columns: dict[str, ColumnProfile]
    preview: list[dict[str, Any]] = field(default_factory=list)
    time_columns: list[str] = field(default_factory=list)
    candidate_primary_keys: list[str] = field(default_factory=list)


@dataclass
class StructuredAssetProfile:
    """结构化文件统一资产描述，可作为 DataAsset 的 provenance。"""

    asset_id: str
    source_file: str
    format: str
    checksum: str
    row_count: int
    column_count: int
    columns: dict[str, ColumnProfile]
    preview: list[dict[str, Any]] = field(default_factory=list)
    sheets: dict[str, SheetProfile] = field(default_factory=dict)
    time_columns: list[str] = field(default_factory=list)
    candidate_primary_keys: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False

    # 方法作用：把 profile 转为 API 和审计日志可序列化的字典。
    # Args: self - 结构化资产 profile。
    # Returns: 不含 pandas/numpy 对象的字典。
    def to_dict(self) -> dict[str, Any]:
        logger.debug("结构化资产 profile 序列化入口", asset_id=self.asset_id)
        result = asdict(self)
        logger.info("结构化资产 profile 序列化完成", asset_id=self.asset_id)
        return result


class StructuredAssetAdapter:
    """读取结构化文件，并生成可复用的列级质量 profile。"""

    _EXTENSIONS = {".csv": "csv", ".xlsx": "excel", ".xls": "excel", ".parquet": "parquet"}

    # 方法作用：初始化文件适配器并设置单次解析的资源上限。
    # Args: max_bytes - 允许读取的最大文件字节数；max_rows - 单 sheet 最大读取行数；preview_rows - 预览行数。
    # Returns: 无返回值。
    def __init__(self, max_bytes: int = 100 * 1024 * 1024,
                 max_rows: int = 1_000_000, preview_rows: int = 20) -> None:
        logger.debug(
            "结构化资产适配器初始化入口",
            max_bytes=max_bytes,
            max_rows=max_rows,
            preview_rows=preview_rows,
        )
        if max_bytes <= 0 or max_rows <= 0 or preview_rows <= 0:
            raise ValueError("结构化资产资源上限必须大于零")
        self.max_bytes = max_bytes
        self.max_rows = max_rows
        self.preview_rows = min(preview_rows, max_rows)
        logger.info("结构化资产适配器初始化完成", max_rows=self.max_rows)

    # 方法作用：解析文件字节并生成统一 profile，Excel 会额外保留 sheet 级信息。
    # Args: file_name - 原始文件名；content - 文件二进制内容。
    # Returns: StructuredAssetProfile 结构化资产描述。
    def inspect_bytes(self, file_name: str, content: bytes) -> StructuredAssetProfile:
        logger.debug("结构化资产检查入口", file_name=file_name, content_size=len(content))
        try:
            fmt = self._detect_format(file_name)
            self._check_size(content)
            if not content:
                raise StructuredAssetError("文件内容为空")
            frames = self._load_frames(fmt, content)
            if not frames:
                raise StructuredAssetError("文件未包含可分析的数据表")

            sheets: dict[str, SheetProfile] = {}
            for name, frame in frames.items():
                sheets[name] = self._profile_frame(name, frame)

            primary_name, primary = next(iter(sheets.items()))
            warnings = []
            truncated = any(len(frame) >= self.max_rows for frame in frames.values())
            if truncated:
                warnings.append(f"数据超过单表 {self.max_rows} 行上限，profile 已截断")
            asset = StructuredAssetProfile(
                asset_id=f"file:{hashlib.sha256(content).hexdigest()[:24]}",
                source_file=file_name,
                format=fmt,
                checksum=hashlib.sha256(content).hexdigest(),
                row_count=primary.row_count,
                column_count=primary.column_count,
                columns=primary.columns,
                preview=primary.preview,
                sheets=sheets if fmt == "excel" else {},
                time_columns=primary.time_columns,
                candidate_primary_keys=primary.candidate_primary_keys,
                warnings=warnings,
                truncated=truncated,
            )
            logger.info(
                "结构化资产检查完成",
                file_name=file_name,
                format=fmt,
                rows=asset.row_count,
                columns=asset.column_count,
                sheets=len(sheets),
            )
            return asset
        except StructuredAssetError:
            logger.error("结构化资产检查失败", file_name=file_name, exc_info=True)
            raise
        except Exception as exc:
            logger.error("结构化资产检查异常", file_name=file_name, error=str(exc), exc_info=True)
            raise StructuredAssetError(f"{file_name} 解析失败: {exc}") from exc

    # 方法作用：读取文件前若干行，供分析计划和预览节点使用。
    # Args: file_name - 原始文件名；content - 文件二进制内容；limit - 返回行数；sheet_name - Excel sheet 名称。
    # Returns: JSON 兼容的行字典列表。
    def read_rows(self, file_name: str, content: bytes, limit: int = 1000,
                  sheet_name: str | None = None) -> list[dict[str, Any]]:
        logger.debug("结构化资产读取行入口", file_name=file_name, limit=limit, sheet_name=sheet_name)
        if limit <= 0 or limit > self.max_rows:
            raise StructuredAssetError(f"行数上限必须在 1 到 {self.max_rows} 之间")
        fmt = self._detect_format(file_name)
        self._check_size(content)
        try:
            frames = self._load_frames(fmt, content, sheet_name=sheet_name, row_limit=limit)
            if not frames:
                return []
            frame = next(iter(frames.values())).head(limit)
            rows = [
                {str(key): self._json_value(value) for key, value in row.items()}
                for row in frame.to_dict(orient="records")
            ]
            logger.info("结构化资产读取行完成", file_name=file_name, count=len(rows))
            return rows
        except StructuredAssetError:
            raise
        except Exception as exc:
            logger.error("结构化资产读取行失败", file_name=file_name, error=str(exc), exc_info=True)
            raise StructuredAssetError(f"{file_name} 读取失败: {exc}") from exc

    # 方法作用：根据扩展名确定解析器，避免让调用方传入不可信的格式标识。
    # Args: file_name - 原始文件名。
    # Returns: csv、excel 或 parquet 格式名。
    @classmethod
    def _detect_format(cls, file_name: str) -> str:
        logger.debug("识别结构化文件格式入口", file_name=file_name)
        suffix = "." + file_name.lower().rsplit(".", 1)[-1] if "." in file_name else ""
        fmt = cls._EXTENSIONS.get(suffix)
        if not fmt:
            raise StructuredAssetError(f"不支持的结构化文件格式: {suffix or file_name}")
        logger.info("识别结构化文件格式完成", file_name=file_name, format=fmt)
        return fmt

    # 方法作用：检查文件大小，防止一次解析消耗不可控内存。
    # Args: content - 文件二进制内容。
    # Returns: 无返回值；超限时抛出 StructuredAssetError。
    def _check_size(self, content: bytes) -> None:
        logger.debug("检查结构化文件大小入口", content_size=len(content), max_bytes=self.max_bytes)
        if len(content) > self.max_bytes:
            raise StructuredAssetError(f"文件大小超过限制 {self.max_bytes} 字节")
        logger.info("检查结构化文件大小完成", content_size=len(content))

    # 方法作用：调用 pandas 读取一个或多个逻辑表，并统一包装可选依赖错误。
    # Args: fmt - 解析格式；content - 文件二进制内容；sheet_name - 可选 sheet；row_limit - 读取行数上限。
    # Returns: sheet 名称到 DataFrame 的映射。
    def _load_frames(self, fmt: str, content: bytes, sheet_name: str | None = None,
                     row_limit: int | None = None) -> dict[str, pd.DataFrame]:
        logger.debug("加载结构化数据帧入口", format=fmt, sheet_name=sheet_name, row_limit=row_limit)
        rows = row_limit or self.max_rows
        stream = io.BytesIO(content)
        try:
            if fmt == "csv":
                return {"data": pd.read_csv(stream, nrows=rows)}
            if fmt == "excel":
                selected = sheet_name if sheet_name else None
                loaded = pd.read_excel(stream, sheet_name=selected, nrows=rows)
                if isinstance(loaded, pd.DataFrame):
                    return {sheet_name or "sheet1": loaded}
                return {str(name): frame for name, frame in loaded.items()}
            if fmt == "parquet":
                return {"data": pd.read_parquet(stream).head(rows)}
        except ImportError as exc:
            dependency = "Excel" if fmt == "excel" else "Parquet"
            raise StructuredAssetError(f"{dependency} 解析需要安装可选引擎: {exc}") from exc
        except ValueError as exc:
            dependency = "Excel" if fmt == "excel" else "Parquet" if fmt == "parquet" else "CSV"
            raise StructuredAssetError(f"{dependency} 文件无效: {exc}") from exc
        except Exception as exc:
            dependency = "Excel" if fmt == "excel" else "Parquet" if fmt == "parquet" else "CSV"
            raise StructuredAssetError(f"{dependency} 读取失败: {exc}") from exc
        raise StructuredAssetError(f"未实现的解析格式: {fmt}")

    # 方法作用：生成单个表的列级 profile、时间列和候选主键。
    # Args: name - 表或 sheet 名称；frame - pandas DataFrame。
    # Returns: SheetProfile。
    def _profile_frame(self, name: str, frame: pd.DataFrame) -> SheetProfile:
        logger.debug("生成表 profile 入口", name=name, rows=len(frame), columns=len(frame.columns))
        columns: dict[str, ColumnProfile] = {}
        time_columns: list[str] = []
        candidate_keys: list[str] = []
        row_count = len(frame)
        for raw_name in frame.columns:
            col = str(raw_name)
            series = frame[raw_name]
            null_count = int(series.isna().sum())
            unique_count = int(series.nunique(dropna=True))
            numeric = bool(pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series))
            datetime_col = self._is_datetime_column(series)
            if datetime_col:
                time_columns.append(col)
            if row_count > 0 and null_count == 0 and unique_count == row_count:
                candidate_keys.append(col)
            samples = [self._json_value(value) for value in series.dropna().head(5).tolist()]
            columns[col] = ColumnProfile(
                name=col,
                dtype=str(series.dtype),
                null_count=null_count,
                null_rate=round(null_count / row_count, 6) if row_count else 0.0,
                unique_count=unique_count,
                sample_values=samples,
                numeric=numeric,
                datetime=datetime_col,
            )
        preview = [
            {str(key): self._json_value(value) for key, value in row.items()}
            for row in frame.head(self.preview_rows).to_dict(orient="records")
        ]
        result = SheetProfile(
            name=name,
            row_count=row_count,
            column_count=len(frame.columns),
            columns=columns,
            preview=preview,
            time_columns=time_columns,
            candidate_primary_keys=candidate_keys,
        )
        logger.info("生成表 profile 完成", name=name, rows=row_count, time_columns=time_columns)
        return result

    # 方法作用：识别可用于时间对齐的列，避免把数值 ID 误判成日期。
    # Args: series - 待检测的 pandas Series。
    # Returns: 是否为日期时间列。
    @staticmethod
    def _is_datetime_column(series: pd.Series) -> bool:
        logger.debug("检测时间列入口", dtype=str(series.dtype))
        if pd.api.types.is_datetime64_any_dtype(series):
            return True
        if pd.api.types.is_numeric_dtype(series):
            return False
        non_null = series.dropna()
        if non_null.empty:
            return False
        parsed = pd.to_datetime(non_null, errors="coerce", utc=True, format="mixed")
        result = float(parsed.notna().mean()) >= 0.8
        logger.info("检测时间列完成", result=result, parsed_ratio=float(parsed.notna().mean()))
        return result

    # 方法作用：将 pandas、numpy 和时间对象转换为 JSON 可编码的基础类型。
    # Args: value - DataFrame 单元格值。
    # Returns: None、字符串、数字或布尔值。
    @staticmethod
    def _json_value(value: Any) -> Any:
        logger.debug("规范化结构化值入口", value_type=type(value).__name__)
        if value is None:
            return None
        if not isinstance(value, (list, dict)):
            try:
                missing = pd.isna(value)
                if isinstance(missing, bool) and missing:
                    return None
            except (TypeError, ValueError) as exc:
                logger.debug(
                    "结构化值缺失检测跳过",
                    value_type=type(value).__name__,
                    error=str(exc),
                )
        if hasattr(value, "isoformat") and not isinstance(value, (str, bytes)):
            return value.isoformat()
        if hasattr(value, "item"):
            try:
                return value.item()
            except (ValueError, TypeError) as exc:
                logger.debug(
                    "结构化标量提取跳过",
                    value_type=type(value).__name__,
                    error=str(exc),
                )
        return value
