"""两个早期内置 Skill 的标准包、契约和确定性执行测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = PROJECT_ROOT / "skills"


def _load_skill_and_tools(skill_dir_name: str) -> tuple[Any, dict[str, Any]]:
    """通过正式 SkillManager 加载指定内置 Skill 及其工具。"""
    from src.skill_manager import SkillManager

    manager = SkillManager(builtin_dir=str(SKILLS_ROOT))
    skill = manager.load_skill_manifest(
        SKILLS_ROOT / skill_dir_name / "SKILL.md",
        scope="system",
    )
    tools = {tool.name: tool for tool in manager.get_active_tools([skill])}
    return skill, tools


def _read_manifest(skill_dir_name: str) -> tuple[dict[str, Any], str]:
    """读取 Skill Manifest 和中文指令正文。"""
    content = (SKILLS_ROOT / skill_dir_name / "SKILL.md").read_text(
        encoding="utf-8",
        errors="strict",
    )
    parts = content.split("---", 2)
    return yaml.safe_load(parts[1]), parts[2].strip()


class TestEarlySkillPackages:
    """覆盖功能 9.2.1、9.3.1：早期 Skill 标准包改造。"""

    def test_data_quality_package_has_standard_utf8_structure(self) -> None:
        """数据质量 Skill 应具备标准清单、界面配置、契约和评测集。"""
        # 准备阶段
        skill_dir = SKILLS_ROOT / "data_quality_check"
        expected_files = {
            "SKILL.md",
            "agents/openai.yaml",
            "eval/cases.yaml",
            "schemas/quality_input.json",
            "schemas/quality_output.json",
            "tools.py",
        }

        # 执行阶段
        actual_files = {
            path.relative_to(skill_dir).as_posix()
            for path in skill_dir.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        manifest, instructions = _read_manifest("data_quality_check")
        agent_text = (skill_dir / "agents" / "openai.yaml").read_text(
            encoding="utf-8",
            errors="strict",
        )
        evaluation = yaml.safe_load(
            (skill_dir / "eval" / "cases.yaml").read_text(encoding="utf-8", errors="strict")
        )

        # 断言阶段
        assert actual_files == expected_files
        assert manifest["api_version"] == "data-agent/v2"
        assert manifest["input_schema"] == "schemas/quality_input.json"
        assert manifest["output_schema"] == "schemas/quality_output.json"
        assert manifest["evaluation"] == {
            "dataset": "eval/cases.yaml",
            "minimum_score": 0.95,
        }
        assert manifest["resources"]["max_tool_calls"] == 50
        assert "$data-quality-check" in agent_text
        assert len(evaluation["cases"]) == 4
        assert "\ufffd" not in instructions + agent_text
        assert "TODO" not in instructions + agent_text

    def test_custom_report_package_keeps_templates_and_adds_standard_files(self) -> None:
        """报告 Skill 应保留两个模板并补齐标准界面配置和评测集。"""
        # 准备阶段
        skill_dir = SKILLS_ROOT / "custom_report"
        expected_files = {
            "SKILL.md",
            "agents/openai.yaml",
            "eval/cases.yaml",
            "schemas/render_report_input.json",
            "schemas/render_report_output.json",
            "templates/monthly_report.jinja2",
            "templates/weekly_report.jinja2",
            "tools.py",
        }

        # 执行阶段
        actual_files = {
            path.relative_to(skill_dir).as_posix()
            for path in skill_dir.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        manifest, instructions = _read_manifest("custom_report")
        agent_text = (skill_dir / "agents" / "openai.yaml").read_text(
            encoding="utf-8",
            errors="strict",
        )
        evaluation = yaml.safe_load(
            (skill_dir / "eval" / "cases.yaml").read_text(encoding="utf-8", errors="strict")
        )

        # 断言阶段
        assert actual_files == expected_files
        assert manifest["api_version"] == "data-agent/v2"
        assert manifest["input_schema"] == "schemas/render_report_input.json"
        assert manifest["output_schema"] == "schemas/render_report_output.json"
        assert manifest["evaluation"] == {
            "dataset": "eval/cases.yaml",
            "minimum_score": 0.95,
        }
        assert manifest["resources"]["max_tool_calls"] == 1
        assert "$custom-report" in agent_text
        assert len(evaluation["cases"]) == 3
        assert "\ufffd" not in instructions + agent_text
        assert "TODO" not in instructions + agent_text

    @pytest.mark.parametrize("skill_dir_name", ["data_quality_check", "custom_report"])
    async def test_early_skill_manifest_passes_project_validator(
        self,
        skill_dir_name: str,
    ) -> None:
        """两个改造后的 Manifest 应通过项目校验器且没有警告。"""
        # 准备阶段
        manifest, instructions = _read_manifest(skill_dir_name)
        schema_refs = {
            str(tool[field])
            for tool in manifest["tools"]
            for field in ("input_schema", "output_schema")
        }
        schemas = {
            schema_ref: json.loads(
                (SKILLS_ROOT / skill_dir_name / schema_ref).read_text(
                    encoding="utf-8",
                    errors="strict",
                )
            )
            for schema_ref in schema_refs
        }
        for schema in schemas.values():
            Draft202012Validator.check_schema(schema)
        _, validator_tools = _load_skill_and_tools("skill-validator")

        # 执行阶段
        result = await validator_tools["validate_skill_manifest"].ainvoke(
            {
                "manifest": manifest,
                "instructions": instructions,
                "schemas": schemas,
                "existing_skills": [],
            }
        )

        # 断言阶段
        assert result["valid"] is True
        assert result["score"] == 100
        assert result["errors"] == []
        assert result["warnings"] == []


class TestDataQualityCheckSkill:
    """覆盖功能 9.2.2 至 9.2.4：数据质量检查工具。"""

    async def test_null_rate_counts_missing_and_explicit_null(self) -> None:
        """字段缺失与显式空值应共同计入空值率。"""
        # 准备阶段
        _, tools = _load_skill_and_tools("data_quality_check")
        rows = [{"amount": 10}, {"amount": None}, {"id": 3}]

        # 执行阶段
        result = await tools["check_null_rate"].ainvoke({"rows": rows, "column": "amount"})

        # 断言阶段
        assert result == {
            "column": "amount",
            "total": 3,
            "null_count": 2,
            "null_rate": pytest.approx(2 / 3),
        }

    async def test_duplicates_ignore_null_and_return_stable_counts(self) -> None:
        """重复检查应忽略空值并按稳定顺序返回重复值。"""
        # 准备阶段
        _, tools = _load_skill_and_tools("data_quality_check")
        rows = [{"code": "B"}, {"code": "A"}, {"code": "B"}, {"code": None}]

        # 执行阶段
        result = await tools["check_duplicates"].ainvoke({"rows": rows, "column": "code"})

        # 断言阶段
        assert result["total"] == 4
        assert result["unique"] == 2
        assert result["duplicated"] == 1
        assert result["duplicates"] == {"B": 2}

    async def test_outlier_detection_returns_original_row_index(self) -> None:
        """超过阈值的数值应返回对应的原始行索引。"""
        # 准备阶段
        _, tools = _load_skill_and_tools("data_quality_check")
        rows = [{"value": value} for value in [10, 10, 10, 10, 10, 10, 10, 100]]

        # 执行阶段
        result = await tools["detect_outliers"].ainvoke(
            {"rows": rows, "column": "value", "threshold": 2}
        )

        # 断言阶段
        assert result["total"] == 8
        assert result["outlier_count"] == 1
        assert result["outlier_indices"] == [7]

    async def test_outlier_detection_reports_insufficient_sample(self) -> None:
        """少于八行的输入应返回明确错误而不是异常值结论。"""
        # 准备阶段
        _, tools = _load_skill_and_tools("data_quality_check")

        # 执行阶段
        result = await tools["detect_outliers"].ainvoke(
            {"rows": [{"value": 1}, {"value": 2}], "column": "value"}
        )

        # 断言阶段
        assert result == {"error": "至少需要 8 行数据"}

    async def test_quality_runtime_rejects_empty_rows(self) -> None:
        """空数组应在正式运行时输入契约边界被拒绝。"""
        # 准备阶段
        from src.skill_runtime import SkillRuntime, SkillRuntimeError

        skill, _ = _load_skill_and_tools("data_quality_check")

        # 执行与断言阶段
        with pytest.raises(SkillRuntimeError, match="输入不符合声明契约"):
            await SkillRuntime().execute(
                skill,
                "check_null_rate",
                {"rows": [], "column": "amount"},
                trusted_builtin=True,
            )


class TestCustomReportSkill:
    """覆盖功能 9.3.2 至 9.3.4：周期报告模板和渲染工具。"""

    async def test_weekly_report_renders_metrics_and_recommendations(self) -> None:
        """周报应渲染标题、指标、洞察和行动建议。"""
        # 准备阶段
        _, tools = _load_skill_and_tools("custom_report")
        payload = {
            "template": "weekly_report",
            "data": {
                "title": "订单周报",
                "summary": "订单量保持增长",
                "insights": ["华东区域增幅最高"],
                "metrics": {"订单量": 120},
                "recommendations": ["复核华东区域库存"],
            },
        }

        # 执行阶段
        result = await tools["render_report"].ainvoke(payload)

        # 断言阶段
        assert "# 订单周报" in result
        assert "| 订单量 | 120 |" in result
        assert "华东区域增幅最高" in result
        assert "复核华东区域库存" in result

    async def test_monthly_report_renders_changes_and_next_steps(self) -> None:
        """月报应渲染环比数据和下月关注事项。"""
        # 准备阶段
        _, tools = _load_skill_and_tools("custom_report")
        payload = {
            "template": "monthly_report",
            "data": {
                "title": "订单月报",
                "summary": "月度成交额提升",
                "insights": [],
                "metrics": {"成交额": 180000},
                "changes": {"成交额": "+12%"},
                "next_steps": ["跟踪重点客户复购"],
            },
        }

        # 执行阶段
        result = await tools["render_report"].ainvoke(payload)

        # 断言阶段
        assert "| 成交额 | 180000 | +12% |" in result
        assert "## 下月关注" in result
        assert "跟踪重点客户复购" in result

    async def test_report_runtime_rejects_unknown_template(self) -> None:
        """未登记模板应在正式运行时输入契约边界被拒绝。"""
        # 准备阶段
        from src.skill_runtime import SkillRuntime, SkillRuntimeError

        skill, _ = _load_skill_and_tools("custom_report")
        payload = {
            "template": "unknown_report",
            "data": {"title": "非法报告", "summary": "", "insights": [], "metrics": {}},
        }

        # 执行与断言阶段
        with pytest.raises(SkillRuntimeError, match="输入不符合声明契约"):
            await SkillRuntime().execute(
                skill,
                "render_report",
                payload,
                trusted_builtin=True,
            )
