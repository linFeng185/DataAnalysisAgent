"""知识标签规范化与初始化数据测试，覆盖 6.9.2 和 6.9.3。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

logger = logging.getLogger(__name__)


class TestKnowledgeTagDefinitions:
    """验证全局标签初始化集合与个人标签规范化。"""

    # 验证初始化标签只包含知识类型与技术平台，不预置业务领域。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_default_tags_exclude_business_domains(self) -> None:
        """业务领域标签应由超级管理员按实际业务维护。"""
        logger.debug("test_default_tags_exclude_business_domains 入口")
        try:
            # Arrange / Act：读取默认全局标签定义。
            from src.knowledge.tag_store import DEFAULT_GLOBAL_TAGS

            names = {tag["name"] for tag in DEFAULT_GLOBAL_TAGS}
            groups = {tag["tag_group"] for tag in DEFAULT_GLOBAL_TAGS}

            # Assert：覆盖日常知识和数据库技术，但不含订单/客户等业务标签。
            assert {"数据字典", "指标口径", "数据质量", "PostgreSQL", "ClickHouse"} <= names
            assert not ({"订单", "客户", "商品", "营销", "财务"} & names)
            assert groups == {"knowledge_type", "technology"}
            logger.info(
                "test_default_tags_exclude_business_domains 完成",
                extra={"count": len(names)},
            )
        except Exception as exc:
            logger.error(
                "test_default_tags_exclude_business_domains 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 验证中英文自定义标签可生成稳定 slug。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_normalize_tag_slug_is_stable(self) -> None:
        """相同标签经空白和大小写变化后应落到同一唯一键。"""
        logger.debug("test_normalize_tag_slug_is_stable 入口")
        try:
            # Arrange / Act：规范化常见中英文标签名称。
            from src.knowledge.tag_store import normalize_tag_slug

            first = normalize_tag_slug(" PostgreSQL 指南 ")
            second = normalize_tag_slug("postgresql   指南")

            # Assert：slug 稳定且不包含空白。
            assert first == second
            assert " " not in first
            logger.info("test_normalize_tag_slug_is_stable 完成", extra={"slug": first})
        except Exception as exc:
            logger.error("test_normalize_tag_slug_is_stable 异常: %s", exc, exc_info=True)
            raise


class TestKnowledgeTagStore:
    """验证个人标签、全局标签和标签搜索的持久化边界。"""

    # 验证标签搜索只返回全局标签和当前用户的个人标签。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_search_limits_personal_tags_to_current_user(self, monkeypatch) -> None:
        """搜索 SQL 必须同时约束全局范围和个人所有者。"""
        logger.debug("test_search_limits_personal_tags_to_current_user 入口")
        try:
            # Arrange：模拟数据库返回一个全局标签和一个个人标签。
            from src.knowledge.tag_store import KnowledgeTagStore

            conn = AsyncMock()
            conn.fetch.return_value = [
                {
                    "id": 1, "name": "PostgreSQL", "slug": "postgresql",
                    "tag_group": "technology", "aliases": [], "description": "",
                    "scope": "global", "tenant_id": None, "owner_user_id": None,
                    "is_active": True, "is_seed": True,
                },
                {
                    "id": 2, "name": "临时口径", "slug": "临时口径",
                    "tag_group": "custom", "aliases": [], "description": "",
                    "scope": "private", "tenant_id": 7, "owner_user_id": 9,
                    "is_active": True, "is_seed": False,
                },
            ]
            conn.close = AsyncMock()
            store = KnowledgeTagStore()
            monkeypatch.setattr(store, "_connect", AsyncMock(return_value=conn))

            # Act：以租户 7 用户 9 搜索标签。
            result = await store.search("口径", tenant_id=7, user_id=9)

            # Assert：返回模型正确，查询参数包含当前身份。
            assert [tag.scope for tag in result] == ["global", "private"]
            assert conn.fetch.await_args.args[1:3] == (7, 9)
            logger.info("test_search_limits_personal_tags_to_current_user 完成")
        except Exception as exc:
            logger.error(
                "test_search_limits_personal_tags_to_current_user 异常: %s", exc, exc_info=True,
            )
            raise

    # 验证用户自定义标签默认保存为仅本人可见。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_create_personal_tag_defaults_to_private(self, monkeypatch) -> None:
        """普通用户不能通过创建接口产生全局标签。"""
        logger.debug("test_create_personal_tag_defaults_to_private 入口")
        try:
            # Arrange：模拟插入后返回个人标签记录。
            from src.knowledge.tag_store import KnowledgeTagStore

            conn = AsyncMock()
            conn.fetchrow.return_value = {
                "id": 3, "name": "临时口径", "slug": "临时口径",
                "tag_group": "custom", "aliases": [], "description": "",
                "scope": "private", "tenant_id": 7, "owner_user_id": 9,
                "is_active": True, "is_seed": False,
            }
            conn.close = AsyncMock()
            store = KnowledgeTagStore()
            monkeypatch.setattr(store, "_connect", AsyncMock(return_value=conn))

            # Act：创建自定义标签。
            result = await store.create_personal(" 临时口径 ", tenant_id=7, user_id=9)

            # Assert：范围和所有者由服务端固定。
            assert result.scope == "private"
            assert result.tenant_id == 7
            assert result.owner_user_id == 9
            assert conn.fetchrow.await_args.args[3:5] == (7, 9)
            logger.info("test_create_personal_tag_defaults_to_private 完成")
        except Exception as exc:
            logger.error(
                "test_create_personal_tag_defaults_to_private 异常: %s", exc, exc_info=True,
            )
            raise

    # 验证租户管理员不能创建或提升全局标签。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_global_tag_management_requires_super_admin(self) -> None:
        """全局标签治理属于平台权限，不属于租户权限。"""
        logger.debug("test_global_tag_management_requires_super_admin 入口")
        try:
            # Arrange / Act / Assert：两个全局治理入口均拒绝租户管理员。
            from src.knowledge.tag_store import KnowledgeTagStore

            store = KnowledgeTagStore()
            with pytest.raises(PermissionError):
                await store.create_global("财务", actor_role="tenant_admin")
            with pytest.raises(PermissionError):
                await store.promote_to_global(3, actor_role="tenant_admin")
            logger.info("test_global_tag_management_requires_super_admin 完成")
        except Exception as exc:
            logger.error(
                "test_global_tag_management_requires_super_admin 异常: %s", exc, exc_info=True,
            )
            raise

    # 验证上传时只能解析当前用户可见且启用的标签。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_get_visible_by_ids_applies_owner_filter(self, monkeypatch) -> None:
        """标签 ID 校验不能让用户引用其他用户的个人标签。"""
        logger.debug("test_get_visible_by_ids_applies_owner_filter 入口")
        try:
            # Arrange：数据库只返回通过 ACL 的一条全局标签。
            from src.knowledge.tag_store import KnowledgeTagStore

            conn = AsyncMock()
            conn.fetch.return_value = [{
                "id": 1, "name": "数据字典", "slug": "数据字典",
                "tag_group": "knowledge_type", "aliases": [], "description": "",
                "scope": "global", "tenant_id": None, "owner_user_id": None,
                "is_active": True, "is_seed": True,
            }]
            conn.close = AsyncMock()
            store = KnowledgeTagStore()
            monkeypatch.setattr(store, "_connect", AsyncMock(return_value=conn))

            # Act：请求一个全局标签和一个不可见标签。
            result = await store.get_visible_by_ids([1, 99], tenant_id=7, user_id=9)

            # Assert：查询携带当前身份且不伪造缺失标签。
            assert [tag.id for tag in result] == [1]
            assert conn.fetch.await_args.args[2:4] == (7, 9)
            logger.info("test_get_visible_by_ids_applies_owner_filter 完成")
        except Exception as exc:
            logger.error(
                "test_get_visible_by_ids_applies_owner_filter 异常: %s", exc, exc_info=True,
            )
            raise

    # 验证超级管理员搜索可以覆盖所有个人标签以执行治理。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_super_admin_search_can_include_all_private_tags(self, monkeypatch) -> None:
        """平台管理员治理视图不应受当前租户和当前用户过滤。"""
        logger.debug("test_super_admin_search_can_include_all_private_tags 入口")
        try:
            # Arrange：模拟空搜索结果并捕获查询参数。
            from src.knowledge.tag_store import KnowledgeTagStore

            conn = AsyncMock()
            conn.fetch.return_value = []
            conn.close = AsyncMock()
            store = KnowledgeTagStore()
            monkeypatch.setattr(store, "_connect", AsyncMock(return_value=conn))

            # Act：启用全量个人标签治理开关。
            result = await store.search(
                "", tenant_id=7, user_id=9, include_all_private=True, include_inactive=True,
            )

            # Assert：第三个 SQL 参数明确启用跨所有者查询。
            assert result == []
            assert conn.fetch.await_args.args[3] is True
            logger.info("test_super_admin_search_can_include_all_private_tags 完成")
        except Exception as exc:
            logger.error(
                "test_super_admin_search_can_include_all_private_tags 异常: %s", exc, exc_info=True,
            )
            raise

    # 验证设置连接身份失败时，已建立的 PostgreSQL 连接仍会关闭。
    # Args: self - 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_connect_closes_connection_when_identity_setup_fails(self, monkeypatch) -> None:
        import sys
        from unittest.mock import AsyncMock

        import src.knowledge.tag_store as tag_module

        connection = SimpleNamespace(
            execute=AsyncMock(side_effect=RuntimeError("set_config failed")),
            close=AsyncMock(),
        )
        fake_asyncpg = SimpleNamespace(connect=AsyncMock(return_value=connection))
        monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)
        monkeypatch.setattr(tag_module, "get_settings", lambda: SimpleNamespace(
            database_url="postgresql+asyncpg://test",
        ))
        store = tag_module.KnowledgeTagStore()
        monkeypatch.setattr(store, "_ensure", AsyncMock())

        with pytest.raises(RuntimeError, match="set_config failed"):
            await store._connect()

        connection.close.assert_awaited_once()
