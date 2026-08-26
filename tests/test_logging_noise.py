"""请求链路日志噪声回归测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs


class TestRequestLoggingNoise:
    """确保 INFO 仅保留具有运维价值的请求级业务摘要。"""

    # 方法作用：验证已知 Milvus 客户端降级噪音被精确过滤而其他警告保留。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言过滤器契约。
    def test_milvus_mvcc_warning_filter_is_precise(self) -> None:
        """只屏蔽 mvccTs fallback 文案，不能吞掉真正的 Milvus 故障。"""
        # Arrange
        import logging

        from src.logging_config import _MilvusNoiseFilter

        noise = logging.LogRecord(
            "pymilvus.orm.iterator", logging.WARNING, __file__, 1,
            "failed to get mvccTs from milvus server, use client-side ts instead",
            (), None,
        )
        real_failure = logging.LogRecord(
            "pymilvus.orm.iterator", logging.WARNING, __file__, 1,
            "failed to connect to milvus server", (), None,
        )
        unrelated_logger = logging.LogRecord(
            "src.memory.vector_store", logging.WARNING, __file__, 1,
            "failed to get mvccTs from milvus server, use client-side ts instead",
            (), None,
        )

        # Act / Assert
        noise_filter = _MilvusNoiseFilter()
        assert noise_filter.filter(noise) is False
        assert noise_filter.filter(real_failure) is True
        assert noise_filter.filter(unrelated_logger) is True

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
        monkeypatch.setattr(
            auth.AuthMiddleware,
            "_load_current_identity",
            AsyncMock(return_value=(1, 1, "super_admin", "default", "")),
        )
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
        assert info_events == ["Skill 列表完成", "API 访问完成"]

    # 方法作用：验证处理器注册阶段只保留 DEBUG 明细，避免启动 INFO 被逐项淹没。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_processor_registration_does_not_emit_info_per_item(self) -> None:
        """处理器清单应由工作流聚合日志报告，而不是逐个输出 INFO。"""
        # Arrange
        from src.tools.data_processor import DataProcessor, ProcessorResult, register

        class LoggingNoiseProcessor(DataProcessor):
            """用于日志级别断言的最小处理器。"""

            name = "logging-noise-test"
            intents = ["test"]

            # 方法作用：返回固定测试处理结果。
            # Args: self - 测试处理器；rows - 输入行；params - 调用参数。
            # Returns: 固定 ProcessorResult。
            def process(self, rows: list[dict], params: dict) -> ProcessorResult:
                del params
                return ProcessorResult("ok", [], "table", rows)

        # Act
        with capture_logs() as logs:
            register(LoggingNoiseProcessor)

        # Assert
        assert not [entry for entry in logs if entry.get("log_level") == "info"]
