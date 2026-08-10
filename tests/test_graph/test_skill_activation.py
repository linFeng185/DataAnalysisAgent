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
        assert result["skill_activation_stage"] == "schema"
        assert result["skill_candidate_ids"] == ["table-skill"]

    # 方法作用：验证意图阶段只记录表触发候选，Schema 阶段才完成最终激活。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_table_only_skill_activates_after_schema_resolution(self, monkeypatch) -> None:
        """只声明表触发器的 Skill 不得在真实表名到达前提前激活。"""
        # Arrange
        import src.skill_manager as manager_module
        from src.graph.skill_activation import activate_skills
        from src.skill_manager import Skill, SkillManager

        manager = SkillManager()
        table_skill = Skill(
            name="order-audit",
            version="1.0.0",
            description="订单审计",
            triggers={"tables": ["orders"]},
            depends_on={},
            tools=[],
            system_prompt_override="检查订单",
            resource_id="system:order-audit",
        )
        manager.add_skill(table_skill)
        monkeypatch.setattr(manager_module, "get_skill_manager", lambda: manager)
        state = {
            "user_query": "分析业务数据",
            "intent": "query",
            "relevant_tables": [{"name": "orders"}],
        }

        # Act
        intent_result = activate_skills(state, stage="intent")
        schema_result = activate_skills(
            {**state, **intent_result},
            ["orders"],
            stage="schema",
        )

        # Assert
        assert intent_result["activated_skills"] == []
        assert intent_result["skill_candidate_ids"] == ["system:order-audit"]
        assert intent_result["skill_activation_stage"] == "intent"
        assert schema_result["activated_skills"] == ["order-audit"]
        assert schema_result["activated_skill_ids"] == ["system:order-audit"]
        assert schema_result["skill_activation_stage"] == "schema"

    # 方法作用：验证激活结果只包含可序列化描述而不携带运行时工具实例。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_activation_state_is_msgpack_serializable(self, monkeypatch) -> None:
        """SkillRuntimeTool 不得进入 LangGraph checkpoint 的状态增量。"""
        # Arrange
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        import src.skill_manager as manager_module
        from src.graph.skill_activation import activate_skills

        runtime_tool = SimpleNamespace(name="render_report")

        class FakeManager:
            # 方法作用：返回带不可序列化运行时工具的 Skill 替身。
            # Args: self - 管理器替身；query/intent/tables - 匹配输入；kwargs - 身份参数。
            # Returns: 包含运行时工具的 Skill 列表。
            def match_skills(self, query, intent, tables, **kwargs):
                del query, intent, tables, kwargs
                return [SimpleNamespace(
                    name="custom-report",
                    resource_id="system:custom-report",
                    tools=[runtime_tool],
                )]

            # 方法作用：返回空 Prompt，避免测试依赖真实 Skill 文件。
            # Args: self - 管理器替身；skills - 激活 Skill 列表。
            # Returns: 空 Prompt 文本。
            def build_skill_prompt(self, skills):
                del skills
                return ""

            # 方法作用：提供固定请求级工具预算。
            # Args: self - 管理器替身；skills - 激活 Skill 列表。
            # Returns: 工具调用预算。
            def get_tool_budget(self, skills):
                del skills
                return 1

        monkeypatch.setattr(manager_module, "get_skill_manager", lambda: FakeManager())

        # Act
        result = activate_skills({"user_query": "生成月报", "intent": "report"})

        # Assert
        assert result["skill_tools"] == []
        assert result["activated_skill_ids"] == ["system:custom-report"]
        JsonPlusSerializer().dumps_typed(result)

    # 方法作用：验证消费节点能按可序列化 Skill ID 恢复运行时工具。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_resolve_active_skill_tools_restores_runtime_tools(self, monkeypatch) -> None:
        """checkpoint 恢复后消费边界应重新加载当前身份可见的工具。"""
        # Arrange
        import src.skill_manager as manager_module
        from src.graph.skill_activation import resolve_active_skill_tools

        runtime_tool = SimpleNamespace(name="render_report")

        class FakeManager:
            # 方法作用：记录按资源 ID 的身份校验调用。
            # Args: self - 管理器替身；resource_ids - Skill 复合资源 ID；tenant_id/user_id - 当前身份。
            # Returns: 通过校验的 Skill 列表。
            def resolve_requested_skills(self, resource_ids, *, tenant_id, user_id):
                assert resource_ids == ["tenant:custom-report"]
                assert (tenant_id, user_id) == (2, 3)
                return [SimpleNamespace(name="custom-report")]

            # 方法作用：把校验后的 Skill 转换为运行时工具对象。
            # Args: self - 管理器替身；skills - 已校验 Skill 列表。
            # Returns: 工具对象列表。
            def get_active_tools(self, skills):
                assert len(skills) == 1
                return [runtime_tool]

        monkeypatch.setattr(manager_module, "get_skill_manager", lambda: FakeManager())

        # Act
        tools = resolve_active_skill_tools({
            "tenant_id": 2,
            "user_id": 3,
            "activated_skill_ids": ["tenant:custom-report"],
        })

        # Assert
        assert tools == [runtime_tool]
