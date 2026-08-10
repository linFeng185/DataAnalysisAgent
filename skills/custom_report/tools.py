"""确定性周期报告渲染工具。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_TEMPLATE_FILES = {
    "weekly_report": "weekly_report.jinja2",
    "monthly_report": "monthly_report.jinja2",
}


class RenderReportTool(BaseTool):
    """使用仓库内置 Jinja2 模板渲染周期数据报告。"""

    name: str = "render_report"
    description: str = "将已验证的摘要、洞察和指标渲染为周报或月报 Markdown 文本。"

    def _run(self, *args: Any, **kwargs: Any) -> str:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("render_report 只允许异步执行")

    async def _arun(
        self,
        template: str = "weekly_report",
        data: dict[str, Any] | str = "",
        run_manager: Any = None,
    ) -> str:
        """返回渲染后的 Markdown 文本，不写入文件系统。"""
        del run_manager
        normalized_template = template.strip()
        normalized_data = _normalize_data(data)
        logger.info(
            "报告渲染开始",
            template=normalized_template,
            metric_count=len(normalized_data["metrics"]),
            insight_count=len(normalized_data["insights"]),
        )
        template_name = _TEMPLATE_FILES.get(normalized_template)
        if template_name is None:
            raise ValueError(f"不支持的报告模板: {normalized_template}")

        try:
            from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

            environment = Environment(
                loader=FileSystemLoader(_TEMPLATE_DIR, encoding="utf-8"),
                autoescape=False,
                undefined=StrictUndefined,
            )
            rendered = environment.get_template(template_name).render(**normalized_data)
        except ImportError:
            logger.warning("Jinja2 未安装，使用最小 Markdown 回退")
            rendered = _render_fallback(normalized_data)
        except TemplateError as exc:
            logger.warning("报告模板渲染失败，使用最小 Markdown 回退", error=str(exc))
            rendered = _render_fallback(normalized_data)

        result = rendered.strip() + "\n"
        logger.info("报告渲染完成", template=normalized_template, report_chars=len(result))
        return result


def _normalize_data(data: dict[str, Any] | str) -> dict[str, Any]:
    """解析兼容字符串输入并补齐模板使用的可选字段。"""
    parsed: Any = data
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError("data 必须是合法 JSON 对象") from exc
    if not isinstance(parsed, dict):
        raise ValueError("data 必须是对象")
    normalized = dict(parsed)
    normalized.setdefault("title", "数据报告")
    normalized.setdefault("summary", "")
    normalized.setdefault("insights", [])
    normalized.setdefault("metrics", {})
    normalized.setdefault("period", "")
    normalized.setdefault("generated_at", "")
    normalized.setdefault("recommendations", [])
    normalized.setdefault("changes", {})
    normalized.setdefault("next_steps", [])
    return normalized


def _render_fallback(data: dict[str, Any]) -> str:
    """在模板引擎不可用时生成字段稳定的最小 Markdown。"""
    lines = [f"# {data['title']}", "", "## 摘要", str(data["summary"]), ""]
    if data["insights"]:
        lines.extend(["## 洞察", ""])
        lines.extend(f"- {item}" for item in data["insights"])
        lines.append("")
    if data["metrics"]:
        lines.extend(["## 关键指标", ""])
        if data.get("changes"):
            lines.extend(["| 指标 | 数值 | 变化 |", "| --- | ---: | --- |"])
            lines.extend(
                f"| {key} | {value} | {data['changes'].get(key, '')} |"
                for key, value in data["metrics"].items()
            )
        else:
            lines.extend(["| 指标 | 数值 |", "| --- | ---: |"])
            lines.extend(f"| {key} | {value} |" for key, value in data["metrics"].items())
        lines.append("")
    recommendations = list(data.get("recommendations", []) or [])
    next_steps = list(data.get("next_steps", []) or [])
    if recommendations:
        lines.extend(["## 后续行动", ""])
        lines.extend(f"- {item}" for item in recommendations)
    if next_steps:
        lines.extend(["## 下月关注", ""])
        lines.extend(f"- {item}" for item in next_steps)
    return "\n".join(lines).rstrip()


def get_tool(name: str = "render_report") -> BaseTool | None:
    """按名称返回已注册工具。"""
    return RenderReportTool() if name == "render_report" else None


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return [RenderReportTool()]
