"""Skill 两阶段激活回归测试。"""

from __future__ import annotations

from types import SimpleNamespace


class TestSkillActivation:
    """覆盖意图阶段和 Schema 阶段的表名触发。"""

    # 方法作用：验证 Schema 阶段把真实表名传给 SkillManager。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_schema_stage_passes_resolved_table_names(self, monkeypatch) -> None:
        """Schema 解析后的真实表名应到达 SkillManager，而不是空列表。"""
        # Arrange
        import src.graph.skill_activation as activation

        calls: list[tuple[list[str], str]] = []

        class FakeManager:
            # 方法作用：记录 Skill 匹配收到的表名和意图。
            # Args: self - 管理器替身；query/intent/tables - 匹配输入；kwargs - 身份参数。
            # Returns: 固定 Skill 列表。
            def match_skills(self, query, intent, tables, **kwargs):
                del query, kwargs
                calls.append((list(tables), intent))
                return [SimpleNamespace(name="table-skill")]

            # 方法作用：模拟构建激活 Skill Prompt。
            # Args: self - 管理器替身；skills - 激活 Skill。
            # Returns: 有 Skill 时返回固定 Prompt。
            def build_skill_prompt(self, skills):
                return "prompt" if skills else ""

            # 方法作用：模拟加载激活 Skill 工具。
            # Args: self - 管理器替身；skills - 激活 Skill。
            # Returns: 空工具列表。
            def get_active_tools(self, skills):
                return []

            # 方法作用：模拟汇总 Skill 请求级工具预算。
            # Args: self - 管理器替身；skills - 激活 Skill。
            # Returns: 固定预算 3。
            def get_tool_budget(self, skills):
                return 3

        monkeypatch.setattr(activation, "get_skill_manager", lambda: FakeManager(), raising=False)
        # helper imports from module at call time, so patch the source module too
        import src.skill_manager as manager_module
        monkeypatch.setattr(manager_module, "get_skill_manager", lambda: FakeManager())

        # Act
        result = activation.activate_skills(
            {"user_query": "查询订单", "intent": "query"},
            ["orders"],
        )

        # Assert
        assert calls == [(["orders"], "query")]
        assert result["activated_skills"] == ["table-skill"]
        assert result["skill_tool_budget"] == 3
