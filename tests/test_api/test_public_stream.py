"""覆盖 28.2、28.3：公开正文提取与全局过程安全边界。"""

import pytest


class TestPublicStream:
    """原始推理、工具参数和结构化内部字段不得进入公开正文。"""

    # 验证 JSON summary 分片在完整对象结束前可提取。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_summary_is_incremental_and_decodes_escapes(self) -> None:
        """不完整转义不能提前显示，后续分片必须恢复正确文字。"""
        from src.api.public_stream import public_text

        # Arrange / Act
        first = public_text('{"summary":"销售\\n增长\\u', "analyze_result")
        second = public_text(
            '{"summary":"销售\\n增长\\u0032","insights":["不展示"]}', "analyze_result"
        )
        # Assert
        assert first == "销售\n增长"
        assert second == "销售\n增长2"

    # 验证只消费顶层公开字段，不能从 SQL 或嵌套对象抽取内容。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    @pytest.mark.parametrize(
        "node,text,expected",
        [
            ("generate_sql", '{"summary":"内部"}', ""),
            ("analyze_result", '{"nested":{"summary":"内部"}}', ""),
            ("llm_direct_answer", '{"answer":"你好"}', "你好"),
            ("analyze_result", '{"summary":null}', ""),
        ],
    )
    def test_only_public_fields_are_extracted(self, node: str, text: str, expected: str) -> None:
        """节点与字段双白名单隔离原始模型输出。"""
        from src.api.public_stream import public_text

        # Arrange / Act
        result = public_text(text, node)
        # Assert
        assert result == expected

    # 验证摘要开关默认关闭且平台热配置可管理。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_process_setting_is_off_by_default_and_live(self) -> None:
        """关闭过程摘要不能影响正常节点进度。"""
        from src.config import Settings
        from src.runtime.settings import LIVE_FIELDS

        # Arrange / Act
        settings = Settings()
        # Assert
        assert settings.chat_show_analysis_process is False
        assert "chat_show_analysis_process" in LIVE_FIELDS

    # 验证洞察和建议也可以逐项公开，不能携带工具和内部推理字段。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_public_sections_stream_partial_list_items(self) -> None:
        """summary 后面的洞察无需等待整个 JSON 结束才显示。"""
        from src.api.public_stream import public_sections

        # Arrange
        text = '{"summary":"结论","reasoning_content":"内部","insights":["已核实","趋势仍'
        # Act
        fields = public_sections(text, "analyze_result")
        # Assert
        assert fields == {"insights": ["已核实", "趋势仍"]}
        assert public_sections(text, "generate_sql") == {}

    # 验证过程摘要开关只能由固定平台管理员修改。
    # Args: self - 实例；monkeypatch - 身份补丁；role - 角色；user_id - 用户编号；expected - 状态码。
    # Returns: 无返回值。
    @pytest.mark.parametrize(
        "role,user_id,expected",
        [
            ("analyst", 7, 403),
            ("tenant_admin", 1, 403),
            ("super_admin", 7, 403),
            ("super_admin", 1, 200),
        ],
    )
    def test_process_setting_requires_platform_admin(
        self, monkeypatch, role, user_id, expected
    ) -> None:
        """租户管理员及非保留平台账号不能读取或修改全局摘要配置。"""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import src.api.auth as auth
        import src.api.routes.runtime_controls as route

        # Arrange
        monkeypatch.setattr(auth, "get_current_role", lambda: role)
        monkeypatch.setattr(auth, "get_current_user_id", lambda: user_id)
        monkeypatch.setattr(route, "get_current_user_id", lambda: user_id)
        service = SimpleNamespace(
            refresh=AsyncMock(),
            describe=lambda: {"revision": 2},
            save=AsyncMock(return_value={"revision": 3}),
        )
        monkeypatch.setattr(route, "get_runtime_settings", lambda: service)
        app = FastAPI()
        app.include_router(route.router)
        client = TestClient(app)

        # Act
        read = client.get("/admin/runtime-settings")
        write = client.put(
            "/admin/runtime-settings",
            json={
                "revision": 2,
                "values": {"chat_show_analysis_process": True},
            },
        )

        # Assert
        assert read.status_code == write.status_code == expected
        if expected == 200:
            request, actor = service.save.await_args.args
            assert request.values == {"chat_show_analysis_process": True}
            assert actor == 1
            service.refresh.assert_awaited_once()
        else:
            service.save.assert_not_awaited()
            service.refresh.assert_not_awaited()
