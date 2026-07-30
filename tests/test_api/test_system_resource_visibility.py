"""system Skill、知识和 MCP 管理内容不可见回归测试。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

logger = logging.getLogger(__name__)


class TestSystemResourceVisibility:
    """覆盖功能 21.3.5 的后端强制隔离。"""

    # 方法作用：验证普通用户的 Skill 列表排除 system 资源。
    # Args: self - pytest 测试类实例；monkeypatch - 依赖补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_regular_user_cannot_list_system_skills(self, monkeypatch) -> None:
        """内置 Skill 可供 Agent 内部使用，但不能暴露管理详情。"""
        logger.debug("test_regular_user_cannot_list_system_skills 入口")
        import src.api.auth as auth
        import src.skill_manager as skill_manager
        from src.api.routes.skills import list_skills

        system_skill = SimpleNamespace(
            name="system-skill", version="1", enabled=True, description="system",
            triggers={}, tools=[], depends_on={}, scope="system", tenant_id=0,
            owner_user_id=0,
        )
        private_skill = SimpleNamespace(
            name="private-skill", version="1", enabled=True, description="private",
            triggers={}, tools=[], depends_on={}, scope="private", tenant_id=2,
            owner_user_id=9,
        )
        manager = SimpleNamespace(
            get_visible_skills=lambda tenant_id, user_id: [system_skill, private_skill],
            is_builtin=lambda *args, **kwargs: args[0] == "system-skill",
        )
        monkeypatch.setattr(skill_manager, "get_skill_manager", lambda *args, **kwargs: manager)
        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 2)
        monkeypatch.setattr(auth, "get_current_user_id", lambda: 9)
        monkeypatch.setattr(auth, "get_current_role", lambda: "analyst")

        result = await list_skills()

        assert [item["name"] for item in result["skills"]] == ["private-skill"]
        logger.info("test_regular_user_cannot_list_system_skills 完成")

    # 方法作用：验证普通用户的知识管理查询不会请求 system 过滤组。
    # Args: self - pytest 测试类实例；monkeypatch - 依赖补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_regular_user_cannot_query_system_knowledge(self, monkeypatch) -> None:
        """system 知识原文不能进入普通用户的管理 API 查询。"""
        logger.debug("test_regular_user_cannot_query_system_knowledge 入口")
        import src.api.auth as auth
        import src.knowledge.retrieval as retrieval
        import src.memory.vector_store as vector_store
        from src.api.routes.knowledge import list_knowledge

        requested: list[str] = []

        # 方法作用：记录测试中实际触发的知识范围查询。
        # Args: filters - 向量存储过滤条件；limit - 最大结果数。
        # Returns: 空结果列表。
        async def get_by_filter(filters, limit):
            logger.debug(
                "知识范围测试探针入口",
                extra={"visibility": filters.get("visibility", "")},
            )
            requested.append(str(filters.get("visibility", "")))
            logger.info("知识范围测试探针完成", extra={"limit": limit})
            return []

        store = SimpleNamespace(get_by_filter=get_by_filter)
        monkeypatch.setattr(vector_store, "get_vector_store", lambda: _async_value(store))
        monkeypatch.setattr(
            retrieval,
            "build_accessible_knowledge_filters",
            lambda category="": [
                {"visibility": "system"},
                {"visibility": "private", "tenant_id": 2, "owner_user_id": 9},
            ],
        )
        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 2)
        monkeypatch.setattr(auth, "get_current_user_id", lambda: 9)
        monkeypatch.setattr(auth, "get_current_role", lambda: "analyst")

        result = await list_knowledge(
            category=None,
            search=None,
            knowledge_scope=None,
            page=1,
            page_size=20,
        )

        assert result["total"] == 0
        assert requested == ["private"]
        logger.info("test_regular_user_cannot_query_system_knowledge 完成")

    # 方法作用：验证普通用户的 MCP 列表既不合并运行时 system 配置也不查询数据库 system 行。
    # Args: self - pytest 测试类实例；monkeypatch - 依赖补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_regular_user_cannot_list_system_mcp(self, monkeypatch) -> None:
        """MCP 连接参数属于平台机密，不能通过普通账号枚举。"""
        logger.debug("test_regular_user_cannot_list_system_mcp 入口")
        import src.api.auth as auth
        import src.api.routes as routes_package
        import src.mcp_client.client_manager as client_manager
        from src.api.routes.mcp import list_mcp_servers

        queries: list[str] = []

        class FakeConnection:
            """记录 MCP 列表 SQL 的最小异步连接。"""

            # 方法作用：记录查询并返回空数据库配置。
            # Args: query - SQL；args - SQL 参数。
            # Returns: 空行列表。
            async def fetch(self, query: str, *args):
                logger.debug("MCP SQL 测试探针入口", extra={"arg_count": len(args)})
                queries.append(query)
                logger.info("MCP SQL 测试探针完成")
                return []

        @asynccontextmanager
        # 方法作用：向 MCP 路由提供可记录 SQL 的测试连接。
        # Args: 无。
        # Returns: 异步上下文中的 FakeConnection。
        async def fake_connection():
            logger.debug("MCP 测试连接入口")
            yield FakeConnection()
            logger.info("MCP 测试连接完成")

        manager = SimpleNamespace(
            list_system_servers=lambda: [{"name": "secret-system", "scope": "system"}],
        )
        monkeypatch.setattr(routes_package, "_connect_scoped_mcp_db", fake_connection)
        monkeypatch.setattr(client_manager, "get_mcp_client_manager", lambda: manager)
        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 2)
        monkeypatch.setattr(auth, "get_current_user_id", lambda: 9)
        monkeypatch.setattr(auth, "get_current_role", lambda: "analyst")

        result = await list_mcp_servers()

        assert result == {"servers": [], "total": 0}
        assert queries and "scope='system'" not in queries[0]
        logger.info("test_regular_user_cannot_list_system_mcp 完成")


# 方法作用：把同步值包装为可 await 的测试协程。
# Args: value - 待返回值。
# Returns: await 后得到原值。
async def _async_value(value):
    logger.debug("异步测试值入口")
    logger.info("异步测试值完成")
    return value
