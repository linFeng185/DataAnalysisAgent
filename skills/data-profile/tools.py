"""通用行式数据画像工具。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)


class ProfileDataTool(BaseTool):
    """对内存中的行式数据生成可审计画像。"""

    name: str = "profile_data"
    description: str = "检查行数、字段类型、空值、唯一性、候选主键、时间列和重复记录。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口，统一通过受治理的异步运行时执行。"""
        del args, kwargs
        raise RuntimeError("profile_data 只允许异步执行")

    async def _arun(
        self,
        rows: list[dict[str, Any]],
        sample_size: int = 5,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """生成不修改原始数据的字段级画像。"""
        del run_manager
        logger.info("数据画像开始", row_count=len(rows), sample_size=sample_size)
        if not 1 <= sample_size <= 20:
            raise ValueError("sample_size 必须在 1 到 20 之间")
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("rows 中的每一项都必须是对象")

        columns = sorted({str(key) for row in rows for key in row})
        row_tokens = [_stable_token(row) for row in rows]
        duplicate_row_count = len(row_tokens) - len(set(row_tokens))
        profiles: list[dict[str, Any]] = []
        warnings: list[str] = []

        for column in columns:
            values = [row.get(column) for row in rows]
            present = [value for value in values if value is not None]
            types = sorted({_value_type(value) for value in present})
            unique_tokens = {_stable_token(value) for value in present}
            null_count = len(rows) - len(present)
            null_rate = round(null_count / len(rows), 6) if rows else 0.0
            numeric = bool(present) and all(_is_number(value) for value in present)
            datetime_column = _is_datetime_column(present)
            candidate_key = bool(rows) and null_count == 0 and len(unique_tokens) == len(rows)
            if len(types) > 1:
                warnings.append(f"字段 {column} 包含混合类型: {', '.join(types)}")
            if null_rate > 0.5:
                warnings.append(f"字段 {column} 空值率超过 50%")
            profiles.append(
                {
                    "name": column,
                    "observed_types": types,
                    "null_count": null_count,
                    "null_rate": null_rate,
                    "unique_count": len(unique_tokens),
                    "sample_values": [_json_value(value) for value in present[:sample_size]],
                    "numeric": numeric,
                    "datetime": datetime_column,
                    "candidate_key": candidate_key,
                }
            )

        if not rows:
            warnings.append("输入数据为空，无法推断字段结构")
        fingerprint_source = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
        result = {
            "row_count": len(rows),
            "column_count": len(columns),
            "duplicate_row_count": duplicate_row_count,
            "columns": profiles,
            "candidate_primary_keys": [item["name"] for item in profiles if item["candidate_key"]],
            "time_columns": [item["name"] for item in profiles if item["datetime"]],
            "warnings": warnings,
            "fingerprint": hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest(),
        }
        logger.info(
            "数据画像完成",
            row_count=len(rows),
            column_count=len(columns),
            duplicate_row_count=duplicate_row_count,
            warning_count=len(warnings),
        )
        return result


def _stable_token(value: Any) -> str:
    """把任意 JSON 兼容值转换为稳定比较标记。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _value_type(value: Any) -> str:
    """返回与 JSON 语义一致的值类型。"""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, (float, Decimal)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_number(value: Any) -> bool:
    """排除布尔值后判断数值。"""
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _is_datetime_column(values: list[Any]) -> bool:
    """仅在至少八成非空文本可解析时识别时间列。"""
    if not values or any(not isinstance(value, str) for value in values):
        return False
    parsed = 0
    for value in values:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            parsed += 1
        except ValueError:
            continue
    return parsed / len(values) >= 0.8


def _json_value(value: Any) -> Any:
    """把特殊标量转换为 JSON 可表达值。"""
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    return value


_TOOLS: dict[str, BaseTool] = {"profile_data": ProfileDataTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
