"""十个内置治理 Skill 的清单、契约和确定性执行测试。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = PROJECT_ROOT / "skills"
GOVERNANCE_SKILLS = {
    "data-contract": "validate_data_contract",
    "schema-drift": "compare_schema_drift",
    "dataset-diff": "compare_datasets",
    "sampling-audit": "sample_with_audit",
    "distribution-drift": "measure_distribution_drift",
    "semantic-normalization": "normalize_semantics",
    "provenance-audit": "audit_provenance",
    "result-verification": "verify_result",
    "privacy-classifier": "classify_privacy",
    "skill-evaluator": "evaluate_skill_runs",
}


def _load_runtime_tool(skill_name: str):
    """通过正式 SkillManager 加载内置工具及其输入输出契约。"""
    from src.skill_manager import SkillManager

    manager = SkillManager(builtin_dir=str(SKILLS_ROOT))
    skill = manager.load_skill_manifest(SKILLS_ROOT / skill_name / "SKILL.md", scope="system")
    tools = manager.get_active_tools([skill])
    assert len(tools) == 1
    return tools[0]


class TestBuiltinGovernanceSkills:
    """覆盖功能 9.7.1 至 9.7.10 的正常、边界和错误路径。"""

    async def test_data_contract_reports_bounded_violations(self) -> None:
        """9.7.1：有效数据应通过，多条违规应保留总数并限制明细。"""
        # Arrange
        tool = _load_runtime_tool("data-contract")
        contract = {
            "columns": [
                {
                    "name": "id",
                    "type": "integer",
                    "required": True,
                    "nullable": False,
                    "unique": True,
                },
                {"name": "amount", "type": "number", "minimum": 0},
            ],
            "primary_key": ["id"],
        }

        # Act
        valid = await tool.ainvoke({"rows": [{"id": 1, "amount": 10}], "contract": contract})
        invalid = await tool.ainvoke(
            {
                "rows": [{"id": None, "amount": -1}, {"id": None, "amount": -2}],
                "contract": contract,
                "max_violations": 2,
            }
        )

        # Assert
        assert valid["valid"] is True
        assert invalid["valid"] is False
        assert invalid["total_violations"] > len(invalid["violations"])
        assert invalid["violations_truncated"] is True
        assert len(invalid["contract_fingerprint"]) == 64

    async def test_schema_drift_classifies_breaking_and_compatible_changes(self) -> None:
        """9.7.2：删除字段与新增可空字段应进入不同兼容性类别。"""
        # Arrange
        tool = _load_runtime_tool("schema-drift")
        baseline = {
            "tables": [
                {
                    "name": "orders",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "amount", "type": "number"},
                    ],
                    "primary_key": ["id"],
                }
            ]
        }
        current = {
            "tables": [
                {
                    "name": "orders",
                    "columns": [
                        {"name": "id", "type": "number", "nullable": False},
                        {"name": "note", "type": "string", "nullable": True},
                    ],
                    "primary_key": ["id"],
                }
            ]
        }

        # Act
        result = await tool.ainvoke({"baseline": baseline, "current": current})

        # Assert
        assert result["compatible"] is False
        assert {item["code"] for item in result["breaking_changes"]} == {"COLUMN_REMOVED"}
        assert {item["code"] for item in result["compatible_changes"]} == {
            "COLUMN_ADDED",
            "TYPE_CHANGED",
        }

    async def test_dataset_diff_applies_tolerance_and_rejects_duplicate_keys(self) -> None:
        """9.7.3：数值容差应生效，重复主键必须阻断比较。"""
        # Arrange
        tool = _load_runtime_tool("dataset-diff")
        payload = {
            "baseline_rows": [{"id": 1, "amount": 10}, {"id": 2, "amount": 20}],
            "current_rows": [{"id": 1, "amount": 10.001}, {"id": 3, "amount": 30}],
            "primary_key": ["id"],
            "numeric_tolerance": 0.01,
        }

        # Act
        result = await tool.ainvoke(payload)

        # Assert
        assert result["summary"] == {
            "baseline_rows": 2,
            "current_rows": 2,
            "added": 1,
            "removed": 1,
            "modified": 0,
            "unchanged": 1,
        }
        with pytest.raises(ValueError, match="重复主键"):
            await tool.ainvoke(
                {
                    "baseline_rows": [{"id": 1}, {"id": 1}],
                    "current_rows": [],
                    "primary_key": ["id"],
                }
            )

    @pytest.mark.parametrize("method", ["random", "systematic", "stratified", "time_window"])
    async def test_sampling_audit_supports_reproducible_methods(self, method: str) -> None:
        """9.7.4：四种抽样方法使用相同参数重复执行时必须返回相同索引。"""
        # Arrange
        tool = _load_runtime_tool("sampling-audit")
        rows = [
            {"id": index, "group": "甲" if index < 6 else "乙", "time": f"2026-01-{index + 1:02d}"}
            for index in range(10)
        ]
        payload = {"rows": rows, "sample_size": 4, "method": method, "seed": 17}
        if method == "stratified":
            payload["strata_column"] = "group"
        if method == "time_window":
            payload.update(
                {
                    "time_column": "time",
                    "start_time": "2026-01-02",
                    "end_time": "2026-01-09",
                }
            )

        # Act
        first = await tool.ainvoke(payload)
        second = await tool.ainvoke(payload)

        # Assert
        assert first["selected_indices"] == second["selected_indices"]
        assert first["actual_sample_size"] == 4
        assert first["sample_rows"] == second["sample_rows"]

    async def test_sampling_audit_rejects_missing_strata_column(self) -> None:
        """9.7.4：分层抽样缺少分层字段时必须明确拒绝。"""
        # Arrange
        tool = _load_runtime_tool("sampling-audit")

        # Act / Assert
        with pytest.raises(ValueError, match="strata_column"):
            await tool.ainvoke({"rows": [{"id": 1}], "sample_size": 1, "method": "stratified"})

    async def test_distribution_drift_measures_numeric_and_categorical_changes(self) -> None:
        """9.7.5：明显数值平移和类别占比变化都应被识别。"""
        # Arrange
        tool = _load_runtime_tool("distribution-drift")
        baseline = [{"value": value, "group": "甲" if value < 4 else "乙"} for value in range(1, 6)]
        current = [{"value": value, "group": "乙"} for value in range(101, 106)]

        # Act
        result = await tool.ainvoke(
            {
                "baseline_rows": baseline,
                "current_rows": current,
                "columns": ["value", "group"],
                "psi_threshold": 0.1,
                "categorical_threshold": 0.1,
            }
        )

        # Assert
        by_column = {item["column"]: item for item in result["columns"]}
        assert result["drift_detected"] is True
        assert by_column["value"]["data_type"] == "numeric"
        assert by_column["value"]["metrics"]["ks_statistic"] == 1.0
        assert by_column["group"]["data_type"] == "categorical"
        assert by_column["group"]["metrics"]["total_variation"] == 0.6

    async def test_semantic_normalization_is_non_destructive_and_audited(self) -> None:
        """9.7.6：显式规则应规范化副本，未知枚举应保留并记录问题。"""
        # Arrange
        tool = _load_runtime_tool("semantic-normalization")
        rows = [
            {"enabled": "是", "distance": 1.5, "status": "完成", "note": "N/A"},
            {"enabled": "否", "distance": 2, "status": "未知", "note": "保留"},
        ]
        original = deepcopy(rows)
        rules = [
            {"column": "enabled", "operation": "boolean"},
            {"column": "distance", "operation": "scale", "factor": 1000},
            {"column": "status", "operation": "enum", "enum_mapping": {"完成": "completed"}},
            {"column": "note", "operation": "null", "null_values": ["N/A"]},
        ]

        # Act
        result = await tool.ainvoke({"rows": rows, "rules": rules})

        # Assert
        assert rows == original
        assert result["rows"][0] == {
            "enabled": True,
            "distance": 1500.0,
            "status": "completed",
            "note": None,
        }
        assert result["rows"][1]["status"] == "未知"
        assert result["summary"]["issue_count"] == 1
        assert result["issues"][0]["code"] == "NORMALIZATION_FAILED"

    async def test_provenance_audit_validates_complete_and_broken_chains(self) -> None:
        """9.7.7：完整链应通过，尚未产生的输入资产引用必须失败。"""
        # Arrange
        tool = _load_runtime_tool("provenance-audit")
        assets = [
            {
                "asset_id": "source",
                "fingerprint": "a" * 64,
                "source_id": "orders",
                "locator": {"table": "orders"},
            },
            {"asset_id": "middle", "fingerprint": "b" * 64},
            {"asset_id": "result", "fingerprint": "c" * 64},
        ]
        complete = [
            {
                "step_id": "step-1",
                "input_asset_ids": ["source"],
                "output_asset_id": "middle",
                "tool": "query",
                "parameters": {"limit": 100},
                "code_version": "abc123",
            },
            {
                "step_id": "step-2",
                "input_asset_ids": ["middle"],
                "output_asset_id": "result",
                "tool": "aggregate",
                "parameters": {},
                "code_version": "abc123",
            },
        ]

        # Act
        valid = await tool.ainvoke(
            {
                "assets": assets,
                "transforms": complete,
                "expected_output_asset_id": "result",
            }
        )
        invalid = await tool.ainvoke(
            {
                "assets": assets,
                "transforms": [complete[1], complete[0]],
                "expected_output_asset_id": "result",
            }
        )

        # Assert
        assert valid["valid"] is True
        assert valid["terminal_asset_ids"] == ["result"]
        assert valid["filesystem_accessed"] is False
        assert invalid["valid"] is False
        assert any(item["code"] == "CHAIN_DISCONTINUITY" for item in invalid["findings"])

    async def test_result_verification_checks_aggregate_and_completeness(self) -> None:
        """9.7.8：汇总一致时内容校验通过，但截断结果不能标记为已验证。"""
        # Arrange
        tool = _load_runtime_tool("result-verification")
        payload = {
            "rows": [{"id": 1, "amount": 10}, {"id": 2, "amount": 20}],
            "summary": {"total_amount": 30},
            "checks": [
                {"operation": "non_empty"},
                {"operation": "unique", "columns": ["id"]},
                {
                    "operation": "aggregate",
                    "column": "amount",
                    "summary_field": "total_amount",
                    "aggregation": "sum",
                },
            ],
            "truncated": True,
        }

        # Act
        result = await tool.ainvoke(payload)

        # Assert
        assert result["valid"] is True
        assert result["complete"] is False
        assert result["verified"] is False
        assert result["summary"]["failed_checks"] == 0
        assert result["warnings"]

    async def test_privacy_classifier_never_returns_sensitive_values(self) -> None:
        """9.7.9：常见敏感信息应分类，输出只能包含不可逆哈希。"""
        # Arrange
        tool = _load_runtime_tool("privacy-classifier")
        rows = [
            {
                "email": "user@example.com",
                "mobile": "13800138000",
                "api_key": "secret-value",
                "metric": 42,
            }
        ]

        # Act
        result = await tool.ainvoke({"rows": rows})
        serialized = json.dumps(result, ensure_ascii=False)

        # Assert
        assert result["contains_sensitive_data"] is True
        assert {"email", "mobile", "api_key"} <= set(result["sensitive_columns"])
        assert result["raw_values_returned"] is False
        assert "user@example.com" not in serialized
        assert "13800138000" not in serialized
        assert "secret-value" not in serialized
        assert all(
            len(value) == 16
            for detection in result["detections"]
            for value in detection["sample_hashes"]
        )

    async def test_skill_evaluator_applies_contract_determinism_and_time_gates(self) -> None:
        """9.7.10：合规记录应通过，结构错误、非确定性和超时记录应失败。"""
        # Arrange
        tool = _load_runtime_tool("skill-evaluator")
        schema = {
            "type": "object",
            "required": ["ok", "count"],
            "properties": {"ok": {"type": "boolean"}, "count": {"type": "integer"}},
            "additionalProperties": False,
        }
        passing_case = {
            "case_id": "passing",
            "output": {"ok": True, "count": 2},
            "output_schema": schema,
            "expected_subset": {"ok": True},
            "repeat_outputs": [{"ok": True, "count": 2}],
            "duration_ms": 10,
        }
        failing_case = {
            "case_id": "failing",
            "output": {"ok": True, "count": "two"},
            "output_schema": schema,
            "expected_subset": {"ok": True},
            "repeat_outputs": [{"ok": False, "count": "two"}],
            "duration_ms": 200,
        }

        # Act
        passed = await tool.ainvoke({"cases": [passing_case], "maximum_duration_ms": 100})
        failed = await tool.ainvoke(
            {"cases": [failing_case], "minimum_score": 0.95, "maximum_duration_ms": 100}
        )

        # Assert
        assert passed["gate_passed"] is True
        assert passed["average_score"] == 1.0
        assert failed["gate_passed"] is False
        assert failed["schema_failures"] == ["failing"]
        assert failed["nondeterministic_cases"] == ["failing"]
        assert failed["slow_cases"] == ["failing"]
        assert failed["filesystem_accessed"] is False
        assert failed["untrusted_code_executed"] is False


class TestBuiltinGovernanceSkillPackages:
    """覆盖十个治理 Skill 的中文内容、清单、结构定义和评测包完整性。"""

    def test_all_governance_skill_packages_are_complete_utf8_and_chinese(self) -> None:
        """十个包必须严格使用 UTF-8，且不存在占位符、乱码或缺失资源。"""
        # Arrange
        from jsonschema import Draft202012Validator

        from src.skill_manager import SkillManager

        manager = SkillManager(builtin_dir=str(SKILLS_ROOT))

        # Act / Assert
        for skill_name, tool_name in GOVERNANCE_SKILLS.items():
            skill_dir = SKILLS_ROOT / skill_name
            skill_content = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="strict")
            agent_content = (skill_dir / "agents" / "openai.yaml").read_text(
                encoding="utf-8",
                errors="strict",
            )
            assert "TODO" not in skill_content
            assert "Overview" not in skill_content
            assert "�" not in skill_content + agent_content
            assert any("\u4e00" <= char <= "\u9fff" for char in skill_content)
            assert any("\u4e00" <= char <= "\u9fff" for char in agent_content)

            skill = manager.load_skill_manifest(skill_dir / "SKILL.md", scope="system")
            assert skill.api_version == "data-agent/v2"
            assert skill.tools[0]["name"] == tool_name
            assert skill.permissions == {"network": [], "files": "none", "datasources": "none"}
            assert skill.resources["max_tool_calls"] == 1
            assert skill.evaluation["minimum_score"] >= 0.95

            for schema_name in (skill.input_schema, skill.output_schema):
                schema_path = skill_dir / schema_name
                schema = json.loads(schema_path.read_text(encoding="utf-8", errors="strict"))
                Draft202012Validator.check_schema(schema)

            cases = yaml.safe_load(
                (skill_dir / skill.evaluation["dataset"]).read_text(
                    encoding="utf-8",
                    errors="strict",
                )
            )
            assert len(cases["cases"]) >= 3
            assert str(cases["feature_id"]).startswith("9.7.")

    async def test_all_governance_manifests_pass_project_validator(self) -> None:
        """十个清单必须通过项目 v2 校验器，不能只满足通用极简清单格式。"""
        # Arrange
        validator = _load_runtime_tool("skill-validator")

        # Act / Assert
        for skill_name in GOVERNANCE_SKILLS:
            skill_dir = SKILLS_ROOT / skill_name
            content = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="strict")
            _, frontmatter, instructions = content.split("---", 2)
            manifest = yaml.safe_load(frontmatter)
            schemas: dict[str, dict] = {}
            for tool in manifest["tools"]:
                for key in ("input_schema", "output_schema"):
                    schema_ref = tool[key]
                    schemas[schema_ref] = json.loads(
                        (skill_dir / schema_ref).read_text(encoding="utf-8", errors="strict")
                    )

            result = await validator.ainvoke(
                {
                    "manifest": manifest,
                    "instructions": instructions.strip(),
                    "schemas": schemas,
                }
            )

            assert result["valid"] is True, (skill_name, result["errors"])
            assert result["score"] == 100
            assert result["errors"] == []

    async def test_discovery_loads_all_governance_skills_with_unique_triggers(self) -> None:
        """正式发现流程应加载全部治理 Skill，且治理 Skill 之间不得重复关键词。"""
        # Arrange
        from src.skill_manager import SkillManager

        manager = SkillManager(builtin_dir=str(SKILLS_ROOT))

        # Act
        await manager.discover()
        discovered = {
            skill.name: skill for skill in manager.get_visible_skills(tenant_id=1, user_id=1)
        }

        # Assert
        assert GOVERNANCE_SKILLS.keys() <= discovered.keys()
        seen: dict[str, str] = {}
        for skill_name in GOVERNANCE_SKILLS:
            for keyword in discovered[skill_name].triggers.get("keywords", []):
                normalized = str(keyword).strip().lower()
                assert normalized not in seen, (
                    f"{skill_name} 与 {seen.get(normalized)} 重复触发词 {keyword}"
                )
                seen[normalized] = skill_name
