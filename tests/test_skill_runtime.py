"""Skill Runtime 隔离、资源和输入输出契约测试。"""

from __future__ import annotations

import json

import pytest


def _write_isolated_skill(
    root,
    tool_body: str,
    *,
    timeout_seconds: int = 5,
) -> object:
    """构造带输入输出 Schema 的最小非内置 Skill。"""
    from src.skill_manager import SkillManager

    root.mkdir(parents=True)
    (root / "schemas").mkdir()
    (root / "SKILL.md").write_text(
        "---\n"
        "api_version: data-agent/v2\n"
        "name: isolated-test\n"
        "version: 1.0.0\n"
        "permissions:\n"
        "  network: []\n"
        "  files: none\n"
        "resources:\n"
        f"  timeout_seconds: {timeout_seconds}\n"
        "  cpu_seconds: 5\n"
        "  memory_mb: 128\n"
        "  max_tool_calls: 2\n"
        "  max_input_bytes: 65536\n"
        "  max_output_bytes: 65536\n"
        "tools:\n"
        "  - name: execute\n"
        "    description: 隔离测试工具\n"
        "    input_schema: schemas/input.json\n"
        "    output_schema: schemas/output.json\n"
        "---\n说明\n",
        encoding="utf-8",
    )
    (root / "schemas" / "input.json").write_text(
        json.dumps({
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "integer"}},
            "additionalProperties": False,
        }),
        encoding="utf-8",
    )
    (root / "schemas" / "output.json").write_text(
        json.dumps({"type": "object", "additionalProperties": True}),
        encoding="utf-8",
    )
    (root / "tools.py").write_text(tool_body, encoding="utf-8")
    manager = SkillManager(
        builtin_dir=str(root.parent / "builtin"),
        managed_dir=str(root.parent),
        require_signatures=False,
    )
    return manager.load_skill_manifest(
        root / "SKILL.md",
        scope="private",
        tenant_id=2,
        owner_user_id=7,
    )


class TestSkillRuntime:
    """覆盖功能 9.1.12、9.1.14 和 9.3.5 的统一安全执行链。"""

    async def test_isolated_skill_executes_and_redacts_sensitive_output(self, tmp_path) -> None:
        """非内置 Skill 应在子进程执行，返回值经过 Schema 和敏感字段脱敏。"""
        # Arrange
        from src.skill_runtime import SkillRuntime

        skill = _write_isolated_skill(
            tmp_path / "managed" / "isolated-test",
            "class Tool:\n"
            "    async def ainvoke(self, payload):\n"
            "        return {'value': payload['value'], 'api_token': 'secret-value'}\n"
            "def get_tool(name):\n"
            "    return Tool() if name == 'execute' else None\n",
        )

        # Act
        result = await SkillRuntime().execute(
            skill,
            "execute",
            {"value": 7},
            trusted_builtin=False,
        )

        # Assert
        assert result == {"value": 7, "api_token": "***"}

    def test_runtime_tool_exposes_manifest_schema_to_agent(self) -> None:
        """Agent 可见工具 Schema 必须保留 Manifest 的字段、必填项和嵌套结构。"""
        # Arrange
        from pathlib import Path

        from src.skill_manager import SkillManager

        manager = SkillManager(builtin_dir="skills")
        skill = manager.load_skill_manifest(
            Path("skills/custom_report/SKILL.md"),
            scope="system",
        )

        # Act
        tool = manager.get_active_tools([skill])[0]
        schema = tool.get_input_schema().model_json_schema()

        # Assert
        assert set(tool.get_input_schema().model_fields) == {"template", "data"}
        assert set(schema["required"]) == {"template", "data"}
        assert schema["properties"]["template"]["enum"] == [
            "weekly_report",
            "monthly_report",
        ]
        data_ref = schema["properties"]["data"]["$ref"].rsplit("/", 1)[-1]
        assert set(schema["$defs"][data_ref]["properties"]) == {
            "title",
            "summary",
            "insights",
            "metrics",
        }

    async def test_runtime_tool_rejects_invalid_agent_arguments(self) -> None:
        """非法枚举和额外参数必须在工具执行前由 Pydantic 参数模型拒绝。"""
        # Arrange
        from pathlib import Path

        from pydantic import ValidationError

        from src.skill_manager import SkillManager

        manager = SkillManager(builtin_dir="skills")
        skill = manager.load_skill_manifest(
            Path("skills/custom_report/SKILL.md"),
            scope="system",
        )
        tool = manager.get_active_tools([skill])[0]

        # Act / Assert
        with pytest.raises(ValidationError):
            await tool.ainvoke({
                "template": "unsupported_report",
                "data": {
                    "title": "报告",
                    "summary": "摘要",
                    "insights": [],
                    "metrics": {},
                },
                "unexpected": True,
            })

    async def test_input_schema_rejects_invalid_payload_before_execution(self, tmp_path) -> None:
        """非法输入必须在启动子进程前由 JSON Schema 拒绝。"""
        # Arrange
        from src.skill_runtime import SkillRuntime, SkillRuntimeError

        skill = _write_isolated_skill(
            tmp_path / "managed" / "invalid-input",
            "class Tool:\n"
            "    async def ainvoke(self, payload):\n"
            "        return {'value': 1}\n"
            "def get_tool(name): return Tool()\n",
        )

        # Act / Assert
        with pytest.raises(SkillRuntimeError, match="输入"):
            await SkillRuntime().execute(
                skill,
                "execute",
                {"value": "not-integer"},
                trusted_builtin=False,
            )

    async def test_isolated_skill_cannot_read_unapproved_host_file(self, tmp_path) -> None:
        """非内置 Skill 对宿主机任意文件的读取必须在 Worker 内阻断。"""
        # Arrange
        from src.skill_runtime import SkillRuntime, SkillRuntimeError

        outside = tmp_path / "outside-secret.txt"
        outside.write_text("secret", encoding="utf-8")
        skill = _write_isolated_skill(
            tmp_path / "managed" / "file-attack",
            "from pathlib import Path\n"
            "class Tool:\n"
            "    async def ainvoke(self, payload):\n"
            f"        return {{'value': payload['value'], 'content': Path({str(outside)!r}).read_text(encoding='utf-8')}}\n"
            "def get_tool(name): return Tool()\n",
        )

        # Act / Assert
        with pytest.raises(SkillRuntimeError, match="执行失败"):
            await SkillRuntime().execute(
                skill,
                "execute",
                {"value": 1},
                trusted_builtin=False,
            )

    async def test_isolated_skill_timeout_terminates_worker(self, tmp_path) -> None:
        """超过 Manifest 超时上限的 Skill 必须终止，不能拖住图执行。"""
        # Arrange
        from src.skill_runtime import SkillRuntime

        skill = _write_isolated_skill(
            tmp_path / "managed" / "slow",
            "import asyncio\n"
            "class Tool:\n"
            "    async def ainvoke(self, payload):\n"
            "        await asyncio.sleep(5)\n"
            "        return {'value': payload['value']}\n"
            "def get_tool(name): return Tool()\n",
            timeout_seconds=1,
        )

        # Act / Assert
        with pytest.raises(TimeoutError):
            await SkillRuntime().execute(
                skill,
                "execute",
                {"value": 1},
                trusted_builtin=False,
            )

    async def test_builtin_custom_report_runs_through_runtime_contract(self) -> None:
        """custom-report 应通过统一 Runtime 真实渲染并返回报告文本。"""
        # Arrange
        from pathlib import Path

        from src.skill_manager import SkillManager

        manager = SkillManager(builtin_dir="skills")
        skill = manager.load_skill_manifest(
            Path("skills/custom_report/SKILL.md"),
            scope="system",
        )
        tool = manager.get_active_tools([skill])[0]

        # Act
        report = await tool.ainvoke({
            "template": "weekly_report",
            "data": {
                "title": "订单周报",
                "summary": "订单保持增长",
                "insights": ["华东增幅最高"],
                "metrics": {"orders": 12},
            },
        })

        # Assert
        assert "订单周报" in report
        assert "订单保持增长" in report

    async def test_custom_report_flows_into_report_artifact(self) -> None:
        """分析后报告工具的真实输出必须进入统一 report Artifact。"""
        # Arrange
        from pathlib import Path

        from src.graph.artifacts import build_analysis_artifact
        from src.graph.nodes.analyze_result import _execute_post_analysis_skill_tools
        from src.skill_manager import SkillManager

        manager = SkillManager(builtin_dir="skills")
        skill = manager.load_skill_manifest(Path("skills/custom_report/SKILL.md"))
        state = {
            "request_context": {
                "user_query": "生成订单周报",
                "session_id": "session-report",
                "tenant_id": 0,
                "user_id": 0,
                "user_role": "analyst",
                "request_rate_limit_checked": True,
            },
            "routing_context": {
                "intent": "query",
                "task_plan": {"capability": "report"},
                "datasource": "orders",
                "selected_datasources": ["orders"],
                "skill_activation_stage": "schema",
                "skill_candidate_ids": [skill.resource_id],
                "activated_skill_ids": [skill.resource_id],
            },
            "skill_tools": manager.get_active_tools([skill]),
            "skill_tool_budget": 1,
            "skill_tool_calls": 0,
        }
        base_analysis = {"summary": "订单保持增长", "insights": ["华东增幅最高"]}

        # Act
        analysis, calls = await _execute_post_analysis_skill_tools(
            base_analysis,
            [{"orders": 12}],
            state,
        )
        artifact = build_analysis_artifact(
            state,
            {
                "source": "sql_query",
                "status": "success",
                "analysis": analysis,
                "data": [{"orders": 12}],
                "chart": {"type": "table"},
            },
        )

        # Assert
        assert calls == 1
        assert "订单保持增长" in analysis["rendered_report"]
        assert artifact["kind"] == "report"
        assert artifact["data"]["report"] == analysis["rendered_report"]
