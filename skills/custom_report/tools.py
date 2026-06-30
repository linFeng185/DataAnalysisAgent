"""9.3.4 报告渲染工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"


class RenderReportTool(BaseTool):
    """9.3.4 Jinja2 模板渲染数据报告。"""
    name: str = "render_report"
    description: str = (
        "将分析结果渲染为报告。输入: {\"template\": \"weekly_report\", \"data\": {...}}。"
        "template: weekly_report / monthly_report。"
    )

    def _run(self, template: str = "weekly_report", data: dict | str = "",
             run_manager: Any = None) -> str:
        import json
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                data = {"content": data}

        logger.info("报告渲染", template=template)

        template_file = _TEMPLATE_DIR / f"{template}.jinja2"
        if template_file.exists():
            try:
                from jinja2 import Template
                return Template(template_file.read_text(encoding="utf-8")).render(**data)
            except ImportError:
                logger.warning("jinja2 未安装，回退")
            except Exception as e:
                logger.warning("模板渲染失败", error=str(e))

        # 回退 Markdown
        lines = [f"# {data.get('title', '数据报告')}", ""]
        if data.get("summary"):
            lines.append(f"## 摘要\n{data['summary']}\n")
        if data.get("insights"):
            lines.append("## 洞察")
            for i in data["insights"]:
                lines.append(f"- {i}")
        if data.get("metrics"):
            lines.append("\n## 关键指标")
            for k, v in data["metrics"].items():
                lines.append(f"- {k}: {v}")
        return "\n".join(lines)


def get_tool(name: str = "render_report") -> BaseTool | None:
    return RenderReportTool() if name == "render_report" else None


def get_tools() -> list[BaseTool]:
    return [RenderReportTool()]
