"""请求链路日志噪声回归测试。"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs


class TestRequestLoggingNoise:
    """确保 INFO 仅保留具有运维价值的请求级业务摘要。"""

    # 方法作用：验证 Skills 查询不输出 getter、中间件和逐条判断成功日志。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具；tmp_path - 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_skills_request_keeps_info_at_business_boundary(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        """预热后的读取请求只保留一条 INFO 业务结果摘要。"""
        # Arrange
        import src.api.auth as auth
        import src.skill_manager as skill_module
        from src.main import create_app

        manager = skill_module.SkillManager(
            str(tmp_path / "builtin"),
            managed_dir=str(tmp_path / "managed"),
        )
        monkeypatch.setattr(skill_module, "get_skill_manager", lambda *args, **kwargs: manager)
        token = auth.create_access_token(1, 1, "super_admin")
        client = AsyncClient(
            transport=ASGITransport(app=create_app()),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        )
        await client.get("/api/v1/skills")

        # Act
        with capture_logs() as logs:
            response = await client.get("/api/v1/skills")
        await client.aclose()

        # Assert
        info_events = [
            entry["event"]
            for entry in logs
            if entry.get("log_level") == "info"
        ]
        assert response.status_code == 200
        assert info_events == ["Skill 列表完成"]
