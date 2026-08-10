"""结构化分析产物文本导出工具。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_FORMATS = {"json", "csv", "markdown"}


class ExportArtifactTool(BaseTool):
    """将分析产物导出为可复现的内存文本。"""

    name: str = "export_artifact"
    description: str = (
        "把结构化分析产物导出为 JSON、CSV 或 Markdown，并返回媒体类型、摘要和校验值。"
    )

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("export_artifact 只允许异步执行")

    async def _arun(
        self,
        artifact: dict[str, Any] | list[Any],
        format: str,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """以稳定顺序导出内容，不写入文件系统。"""
        del run_manager
        export_format = format.strip().lower()
        logger.info("分析产物导出开始", format=export_format, artifact_type=type(artifact).__name__)
        if export_format not in _FORMATS:
            raise ValueError(f"不支持的导出格式: {export_format}")

        sanitized_cells = 0
        if export_format == "json":
            content = json.dumps(
                artifact, ensure_ascii=False, sort_keys=True, indent=2, default=str
            )
            media_type = "application/json; charset=utf-8"
            extension = ".json"
        elif export_format == "csv":
            rows = _extract_rows(artifact)
            content, sanitized_cells = _to_csv(rows)
            media_type = "text/csv; charset=utf-8"
            extension = ".csv"
        else:
            content = _to_markdown(artifact)
            media_type = "text/markdown; charset=utf-8"
            extension = ".md"

        encoded = content.encode("utf-8")
        result = {
            "format": export_format,
            "media_type": media_type,
            "extension": extension,
            "content": content,
            "byte_size": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "sanitized_formula_cells": sanitized_cells,
            "written_to_filesystem": False,
        }
        logger.info(
            "分析产物导出完成",
            format=export_format,
            byte_size=len(encoded),
            sanitized_formula_cells=sanitized_cells,
        )
        return result


def _extract_rows(artifact: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    """从通用产物或直接数组中提取行式数据。"""
    candidate: Any = artifact
    if isinstance(artifact, dict):
        candidate = artifact.get("data", artifact.get("rows"))
    if not isinstance(candidate, list) or any(not isinstance(row, dict) for row in candidate):
        raise ValueError("CSV 导出要求 artifact 本身或 data/rows 字段为对象数组")
    if not candidate:
        raise ValueError("CSV 导出不能处理空数据")
    return candidate


def _to_csv(rows: list[dict[str, Any]]) -> tuple[str, int]:
    """生成稳定 CSV，并中和电子表格公式前缀。"""
    columns = sorted({str(key) for row in rows for key in row})
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    sanitized = 0
    for row in rows:
        prepared: dict[str, str] = {}
        for column in columns:
            value = row.get(column)
            if isinstance(value, (dict, list)):
                text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            elif value is None:
                text = ""
            else:
                text = str(value)
            if text.lstrip().startswith(("=", "+", "-", "@")):
                text = f"'{text}"
                sanitized += 1
            prepared[column] = text
        writer.writerow(prepared)
    return output.getvalue(), sanitized


def _to_markdown(artifact: dict[str, Any] | list[Any]) -> str:
    """将通用产物渲染为不含业务模板的 Markdown。"""
    lines = ["# 分析产物", ""]
    data: Any = artifact
    if isinstance(artifact, dict):
        narrative = artifact.get("narrative", {})
        if isinstance(narrative, dict) and narrative.get("summary"):
            lines.extend(["## 摘要", "", str(narrative["summary"]), ""])
        insights = narrative.get("insights", []) if isinstance(narrative, dict) else []
        if isinstance(insights, list) and insights:
            lines.extend(["## 洞察", ""])
            lines.extend(f"- {item}" for item in insights)
            lines.append("")
        data = artifact.get("data", artifact.get("rows", artifact))
    if isinstance(data, list) and data and all(isinstance(row, dict) for row in data):
        columns = sorted({str(key) for row in data for key in row})
        lines.extend(
            [
                "## 数据",
                "",
                "| " + " | ".join(_escape_markdown(column) for column in columns) + " |",
            ]
        )
        lines.append("| " + " | ".join("---" for _ in columns) + " |")
        for row in data:
            lines.append(
                "| "
                + " | ".join(_escape_markdown(_scalar_text(row.get(column))) for column in columns)
                + " |"
            )
    else:
        lines.extend(
            [
                "## 数据",
                "",
                "```json",
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                "```",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _scalar_text(value: Any) -> str:
    """把嵌套值转换为稳定文本。"""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return "" if value is None else str(value)


def _escape_markdown(value: Any) -> str:
    """转义 Markdown 表格中的分隔符和换行。"""
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


_TOOLS: dict[str, BaseTool] = {"export_artifact": ExportArtifactTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
