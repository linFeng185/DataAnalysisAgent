"""管理端、MCP 与会话持久化故障可见性回归测试。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

logger = logging.getLogger(__name__)


class TestManagementInputValidation:
    """覆盖模型测试端点的结构化输入边界。"""

    # 方法作用：验证模型 ID 为空、过长或含控制字符时被 Pydantic 拒绝。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_model_test_request_rejects_invalid_id(self) -> None:
        """裸 dict 不得把任意大输入传到模型工厂。"""
        logger.debug("test_model_test_request_rejects_invalid_id 入口")
        from src.api.schemas import ModelTestRequest

        for model_id in ("", "x" * 129, "model\nname"):
            with pytest.raises(ValidationError):
                ModelTestRequest(model_id=model_id)
        logger.info("test_model_test_request_rejects_invalid_id 完成")


class TestMCPFailureVisibility:
    """覆盖 MCP 管理数据库故障的 HTTP 状态。"""

    # 方法作用：验证 MCP 数据库查询失败返回 503 而不是 HTTP 200 空列表。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_list_mcp_servers_database_failure_is_503(self, monkeypatch) -> None:
        """管理员必须能区分空配置和数据库故障。"""
        logger.debug("test_list_mcp_servers_database_failure_is_503 入口")
        import src.api.auth as auth_module
        import src.api.routes as routes_package
        import src.api.routes.mcp as mcp_routes
        import src.mcp_client.client_manager as manager_module

        # 方法作用：模拟 MCP 数据库连接失败。
        # Args: 无。
        # Returns: 不返回连接，进入时抛出异常。
        @asynccontextmanager
        async def broken_connection():
            raise RuntimeError("database unavailable")
            yield  # pragma: no cover

        monkeypatch.setattr(auth_module, "get_current_tenant_id", lambda: 4)
        monkeypatch.setattr(auth_module, "get_current_user_id", lambda: 7)
        monkeypatch.setattr(auth_module, "get_current_role", lambda: "tenant_admin")
        monkeypatch.setattr(routes_package, "_connect_scoped_mcp_db", broken_connection)
        monkeypatch.setattr(
            manager_module,
            "get_mcp_client_manager",
            lambda: SimpleNamespace(list_system_servers=lambda: []),
        )

        with pytest.raises(HTTPException) as caught:
            await mcp_routes.list_mcp_servers()
        assert caught.value.status_code == 503
        assert caught.value.detail == "MCP Server 配置存储不可用"
        logger.info("test_list_mcp_servers_database_failure_is_503 完成")

    # 方法作用：验证 MCP 创建失败不会向客户端暴露数据库连接串。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_create_mcp_server_failure_hides_internal_detail(self, monkeypatch) -> None:
        """内部存储错误应保留在日志中，HTTP 响应只返回稳定错误文案。"""
        # Arrange
        import src.api.routes as routes_package
        import src.api.routes.mcp as mcp_routes
        from src.api.schemas import MCPServerCreate

        secret = "postgresql://admin:secret@db.internal/app"

        # 方法作用：模拟创建 MCP 配置时数据库连接失败。
        # Args: 无。
        # Returns: 不返回连接，进入时抛出带敏感信息的异常。
        @asynccontextmanager
        async def broken_connection():
            logger.info("MCP 创建失败测试探针", extra={"boundary": "database"})
            raise RuntimeError(secret)
            yield  # pragma: no cover

        monkeypatch.setattr(
            mcp_routes,
            "_authorize_extension_scope",
            lambda scope: ("private", 4, 7, "tenant_admin"),
        )
        monkeypatch.setattr(mcp_routes, "_validate_managed_mcp_request", lambda *args: None)
        monkeypatch.setattr(routes_package, "_connect_scoped_mcp_db", broken_connection)
        request = MCPServerCreate(
            name="internal", scope="private", transport="sse",
            url="https://mcp.example.com/sse",
        )

        # Act
        with pytest.raises(HTTPException) as caught:
            await mcp_routes.create_mcp_server(request)

        # Assert
        assert caught.value.status_code == 500
        assert caught.value.detail == "创建 MCP Server 失败"
        assert secret not in caught.value.detail

    # 方法作用：验证 MCP 删除失败不会向客户端暴露数据库内部信息。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_delete_mcp_server_failure_hides_internal_detail(self, monkeypatch) -> None:
        """删除边界的非 HTTP 异常必须转换为稳定且脱敏的 500 响应。"""
        # Arrange
        import src.api.auth as auth_module
        import src.api.routes as routes_package
        import src.api.routes.mcp as mcp_routes

        secret = r"permission denied: C:\\service\\mcp-secrets.json"

        # 方法作用：模拟删除 MCP 配置时数据库连接失败。
        # Args: 无。
        # Returns: 不返回连接，进入时抛出带内部路径的异常。
        @asynccontextmanager
        async def broken_connection():
            logger.info("MCP 删除失败测试探针", extra={"boundary": "database"})
            raise RuntimeError(secret)
            yield  # pragma: no cover

        monkeypatch.setattr(auth_module, "get_current_tenant_id", lambda: 4)
        monkeypatch.setattr(auth_module, "get_current_user_id", lambda: 7)
        monkeypatch.setattr(auth_module, "get_current_role", lambda: "tenant_admin")
        monkeypatch.setattr(routes_package, "_connect_scoped_mcp_db", broken_connection)

        # Act
        with pytest.raises(HTTPException) as caught:
            await mcp_routes.delete_mcp_server("internal", scope="private")

        # Assert
        assert caught.value.status_code == 500
        assert caught.value.detail == "删除 MCP Server 失败"
        assert secret not in caught.value.detail

    # 方法作用：验证 MCP 连通测试失败结果不会返回底层网络异常。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_mcp_connection_test_hides_internal_detail(self, monkeypatch) -> None:
        """连通测试保持 ok=false 协议，但 error 字段不得包含目标网络细节。"""
        # Arrange
        import src.api.auth as auth_module
        import src.api.routes.mcp as mcp_routes
        import src.mcp_client.client_manager as manager_module

        secret = "connection refused at 10.20.30.40:9000"
        manager = SimpleNamespace(
            ensure_scoped_servers=AsyncMock(side_effect=RuntimeError(secret)),
        )
        monkeypatch.setattr(auth_module, "get_current_tenant_id", lambda: 4)
        monkeypatch.setattr(auth_module, "get_current_user_id", lambda: 7)
        monkeypatch.setattr(manager_module, "get_mcp_client_manager", lambda: manager)

        # Act
        result = await mcp_routes.test_mcp_server("internal", scope="private")

        # Assert
        assert result == {
            "name": "internal",
            "ok": False,
            "error": "MCP Server 连通性测试失败",
        }
        assert secret not in result["error"]


class TestSkillFailureVisibility:
    """覆盖 Skill 管理与上传失败响应的异常脱敏。"""

    # 方法作用：验证 Skill 刷新失败不会暴露受管目录内部路径。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_refresh_skills_failure_hides_internal_detail(self, monkeypatch) -> None:
        """扫描异常只记录到服务端日志，客户端收到稳定 500 文案。"""
        # Arrange
        import src.api.routes.skills as skill_routes
        import src.skill_manager as manager_module

        secret = r"invalid manifest at C:\\data\\private\\SKILL.md"
        manager = SimpleNamespace(discover=AsyncMock(side_effect=RuntimeError(secret)))
        monkeypatch.setattr(manager_module, "get_skill_manager", lambda: manager)

        # Act
        with pytest.raises(HTTPException) as caught:
            await skill_routes.refresh_skills()

        # Assert
        assert caught.value.status_code == 500
        assert caught.value.detail == "Skill 刷新失败"
        assert secret not in caught.value.detail

    # 方法作用：验证 Skill 启停失败不会暴露内部异常。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_toggle_skill_failure_hides_internal_detail(self, monkeypatch) -> None:
        """启停持久化或查找失败时应返回稳定错误文案。"""
        # Arrange
        import src.api.auth as auth_module
        import src.api.routes.skills as skill_routes
        import src.skill_manager as manager_module

        secret = "state write failed: api_key=internal-secret"
        manager = SimpleNamespace(get_skill=lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError(secret),
        ))
        monkeypatch.setattr(auth_module, "get_current_tenant_id", lambda: 4)
        monkeypatch.setattr(auth_module, "get_current_user_id", lambda: 7)
        monkeypatch.setattr(manager_module, "get_skill_manager", lambda: manager)

        # Act
        with pytest.raises(HTTPException) as caught:
            await skill_routes.toggle_skill("quality", enabled=False, skill_scope="private")

        # Assert
        assert caught.value.status_code == 500
        assert caught.value.detail == "Skill 启停失败"
        assert secret not in caught.value.detail

    # 方法作用：验证上传后的内部清单加载错误只返回安全分类信息。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具；tmp_path - 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_uploaded_skill_cache_error_hides_internal_detail(
        self, monkeypatch, tmp_path,
    ) -> None:
        """缓存注入异常不得通过批量上传 errors 数组泄露服务端路径。"""
        # Arrange
        import src.api.routes.skills as skill_routes
        import src.skill_manager as manager_module

        secret = r"signature service failed at C:\\keys\\root.pem"
        skill_dir = tmp_path / "quality"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: quality\n---\n", encoding="utf-8")
        manager = SimpleNamespace(
            load_skill_manifest=lambda *args, **kwargs: (_ for _ in ()).throw(
                ValueError(secret),
            ),
        )
        monkeypatch.setattr(manager_module, "get_skill_manager", lambda: manager)

        # Act
        errors = skill_routes._cache_uploaded_skills(
            [{"name": "quality", "file": "quality/SKILL.md"}],
            str(tmp_path),
            "private",
            4,
            7,
        )

        # Assert
        assert errors == [{
            "file": "quality/SKILL.md",
            "error": "Skill 包校验失败",
        }]
        assert secret not in errors[0]["error"]


class TestSessionFailureVisibility:
    """覆盖会话状态加载故障不得伪装为空历史。"""

    # 方法作用：验证最新状态加载故障向上抛出。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_latest_state_failure_is_not_silenced(self, monkeypatch) -> None:
        """Checkpointer 故障不得返回 None 冒充无最新状态。"""
        logger.debug("test_latest_state_failure_is_not_silenced 入口")
        import src.api.routes as routes_package
        import src.api.routes.session as session_routes

        monkeypatch.setattr(
            routes_package,
            "_load_checkpoint_tuple",
            AsyncMock(side_effect=RuntimeError("checkpoint unavailable")),
        )
        with pytest.raises(RuntimeError, match="checkpoint unavailable"):
            await session_routes._load_latest_state("session-1")
        logger.info("test_latest_state_failure_is_not_silenced 完成")

    # 方法作用：验证会话轮次加载故障向上抛出。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_turn_history_failure_is_not_silenced(self, monkeypatch) -> None:
        """持久化故障不得返回空列表冒充无历史记录。"""
        logger.debug("test_turn_history_failure_is_not_silenced 入口")
        import src.api.routes as routes_package
        import src.api.routes.session as session_routes

        monkeypatch.setattr(
            routes_package,
            "_load_checkpoint_tuple",
            AsyncMock(side_effect=RuntimeError("checkpoint unavailable")),
        )
        with pytest.raises(RuntimeError, match="checkpoint unavailable"):
            await session_routes._load_session_turns("session-1")
        logger.info("test_turn_history_failure_is_not_silenced 完成")


class TestHistoryWriteDurability:
    """覆盖查询历史写入必须由调用链 await。"""

    # 方法作用：验证 add 等待 PostgreSQL 写入完成。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_add_awaits_postgres_insert(self, monkeypatch) -> None:
        """写入任务不得以无引用 fire-and-forget 方式静默丢失。"""
        logger.debug("test_add_awaits_postgres_insert 入口")
        import src.api.auth as auth_module
        from src.memory.history_store import HistoryStore

        monkeypatch.setattr(auth_module, "get_current_tenant_id", lambda: 4)
        monkeypatch.setattr(auth_module, "get_current_user_id", lambda: 7)
        store = HistoryStore()
        insert = AsyncMock()
        monkeypatch.setattr(store, "_pg_insert", insert)

        await store.add("query", "demo", "session-1")

        insert.assert_awaited_once()
        logger.info("test_add_awaits_postgres_insert 完成")
