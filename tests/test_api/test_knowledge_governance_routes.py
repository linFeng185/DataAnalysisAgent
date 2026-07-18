"""知识范围上传与标签治理 API 测试，覆盖 11.1.35 和 11.1.36。"""

from __future__ import annotations

import io
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile

logger = logging.getLogger(__name__)


class TestKnowledgeScopeRoutes:
    """验证上传范围在保存原文件之前完成授权。"""

    # 验证租户管理员不能上传系统知识。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_tenant_admin_cannot_upload_system_knowledge(self, monkeypatch) -> None:
        """平台权限校验失败后不得产生文件或向量副作用。"""
        logger.debug("test_tenant_admin_cannot_upload_system_knowledge 入口")
        try:
            # Arrange：构造租户管理员身份和文件存储探针。
            import src.api.routes as routes
            import src.api.auth as auth
            import src.config as config_module
            import src.knowledge.file_store as file_store_module

            file_store = SimpleNamespace(save=AsyncMock())
            monkeypatch.setattr(auth, "get_current_role", lambda: "tenant_admin")
            monkeypatch.setattr(auth, "get_current_user_id", lambda: 7)
            monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(
                max_upload_bytes=1024, multi_tenant=True,
            ))
            monkeypatch.setattr(file_store_module, "get_file_store", lambda: file_store)
            upload = UploadFile(filename="system.md", file=io.BytesIO(b"content"))

            # Act / Assert：系统范围上传返回 403，且未保存原文件。
            with pytest.raises(HTTPException) as caught:
                await routes.upload_knowledge_docs(
                    [upload], strategy="auto", chunk_size=800, chunk_overlap=100,
                    category="", knowledge_scope="system", tag_ids="", datasource="",
                )
            assert caught.value.status_code == 403
            file_store.save.assert_not_awaited()
            logger.info("test_tenant_admin_cannot_upload_system_knowledge 完成")
        except Exception as exc:
            logger.error(
                "test_tenant_admin_cannot_upload_system_knowledge 异常: %s", exc, exc_info=True,
            )
            raise

    # 验证公共知识不能绑定仅创建者可见的个人标签。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_tenant_upload_rejects_private_tag(self, monkeypatch) -> None:
        """公共文档的标签定义必须对所有文档读者可见。"""
        logger.debug("test_tenant_upload_rejects_private_tag 入口")
        try:
            # Arrange：构造租户管理员和一个个人标签。
            import src.api.routes as routes
            import src.api.auth as auth
            import src.config as config_module
            import src.knowledge.file_store as file_store_module
            import src.knowledge.tag_store as tag_store_module

            file_store = SimpleNamespace(save=AsyncMock())
            tag_store = SimpleNamespace(get_visible_by_ids=AsyncMock(return_value=[
                SimpleNamespace(id=3, name="个人标签", scope="private"),
            ]))
            monkeypatch.setattr(auth, "get_current_role", lambda: "tenant_admin")
            monkeypatch.setattr(auth, "get_current_user_id", lambda: 7)
            monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 4)
            monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(
                max_upload_bytes=1024, multi_tenant=True,
            ))
            monkeypatch.setattr(file_store_module, "get_file_store", lambda: file_store)
            monkeypatch.setattr(tag_store_module, "get_knowledge_tag_store", lambda: tag_store)
            upload = UploadFile(filename="tenant.md", file=io.BytesIO(b"content"))

            # Act / Assert：标签范围检查在文件保存前返回 400。
            with pytest.raises(HTTPException) as caught:
                await routes.upload_knowledge_docs(
                    [upload], strategy="auto", chunk_size=800, chunk_overlap=100,
                    category="", knowledge_scope="tenant", tag_ids="3", datasource="demo",
                )
            assert caught.value.status_code == 400
            file_store.save.assert_not_awaited()
            logger.info("test_tenant_upload_rejects_private_tag 完成")
        except Exception as exc:
            logger.error("test_tenant_upload_rejects_private_tag 异常: %s", exc, exc_info=True)
            raise


class TestKnowledgeTagRoutes:
    """验证个人标签创建与全局标签治理 API。"""

    # 验证普通用户创建标签时服务端固定为个人范围。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_create_tag_uses_current_owner(self, monkeypatch) -> None:
        """请求体不能覆盖 tenant_id、owner_user_id 或 scope。"""
        logger.debug("test_create_tag_uses_current_owner 入口")
        try:
            # Arrange：模拟当前身份与标签存储。
            import src.api.routes as routes
            import src.api.auth as auth
            import src.knowledge.tag_store as tag_store_module
            from src.api.schemas import KnowledgeTagCreateRequest

            tag = SimpleNamespace(model_dump=lambda: {
                "id": 3, "name": "临时口径", "scope": "private",
                "tenant_id": 7, "owner_user_id": 9,
            })
            store = SimpleNamespace(create_personal=AsyncMock(return_value=tag))
            monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 7)
            monkeypatch.setattr(auth, "get_current_user_id", lambda: 9)
            monkeypatch.setattr(tag_store_module, "get_knowledge_tag_store", lambda: store)

            # Act：创建个人自定义标签。
            result = await routes.create_knowledge_tag(
                KnowledgeTagCreateRequest(name="临时口径"),
            )

            # Assert：存储调用只使用服务端身份。
            assert result["scope"] == "private"
            assert store.create_personal.await_args.kwargs["tenant_id"] == 7
            assert store.create_personal.await_args.kwargs["user_id"] == 9
            logger.info("test_create_tag_uses_current_owner 完成")
        except Exception as exc:
            logger.error("test_create_tag_uses_current_owner 异常: %s", exc, exc_info=True)
            raise

    # 验证租户管理员不能创建全局标签。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_tenant_admin_cannot_create_global_tag(self, monkeypatch) -> None:
        """租户管理员只能消费全局标签，不能维护全局固定数据。"""
        logger.debug("test_tenant_admin_cannot_create_global_tag 入口")
        try:
            # Arrange：设置租户管理员角色。
            import src.api.routes as routes
            import src.api.auth as auth
            from src.api.schemas import KnowledgeTagCreateRequest

            monkeypatch.setattr(auth, "get_current_role", lambda: "tenant_admin")

            # Act / Assert：全局创建端点返回 403。
            with pytest.raises(HTTPException) as caught:
                await routes.create_global_knowledge_tag(
                    KnowledgeTagCreateRequest(name="财务", tag_group="business_domain"),
                )
            assert caught.value.status_code == 403
            logger.info("test_tenant_admin_cannot_create_global_tag 完成")
        except Exception as exc:
            logger.error(
                "test_tenant_admin_cannot_create_global_tag 异常: %s", exc, exc_info=True,
            )
            raise


class TestKnowledgeGovernanceAsgi:
    """通过真实 ASGI 中间件验证知识治理身份透传。"""

    # 验证租户管理员 JWT 不能通过 HTTP 上传系统知识。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_tenant_admin_http_upload_system_scope_returns_403(self, monkeypatch) -> None:
        """HTTP 层必须在读取和保存文件前拒绝平台权限越界。"""
        logger.debug("test_tenant_admin_http_upload_system_scope_returns_403 入口")
        try:
            # Arrange：创建启用多租户认证的 ASGI 应用和租户管理员令牌。
            from httpx import ASGITransport, AsyncClient
            import src.api.auth as auth
            import src.main as main_module

            settings = SimpleNamespace(
                env="test", multi_tenant=True, admin_api_key="",
                jwt_secret="k" * 32, jwt_access_token_expire_hours=24,
            )
            monkeypatch.setattr(auth, "get_settings", lambda: settings)
            monkeypatch.setattr(main_module, "get_settings", lambda: settings)
            monkeypatch.setattr(auth, "_secret_cache", None)
            token = auth.create_access_token(7, 4, "tenant_admin")
            client = AsyncClient(
                transport=ASGITransport(app=main_module.create_app()),
                base_url="http://test",
                headers={"Authorization": f"Bearer {token}"},
            )

            # Act：发起真实 multipart 上传请求。
            response = await client.post(
                "/api/v1/knowledge/docs/upload?knowledge_scope=system",
                files={"files": ("system.md", b"content", "text/markdown")},
            )
            await client.aclose()

            # Assert：中间件身份进入路由后被范围权限拒绝。
            assert response.status_code == 403
            logger.info("test_tenant_admin_http_upload_system_scope_returns_403 完成")
        except Exception as exc:
            logger.error(
                "test_tenant_admin_http_upload_system_scope_returns_403 异常: %s", exc,
                exc_info=True,
            )
            raise

    # 验证普通用户 JWT 身份通过 HTTP 传给个人标签存储。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_user_http_create_tag_uses_jwt_identity(self, monkeypatch) -> None:
        """客户端不能在请求体中伪造标签所有者和租户。"""
        logger.debug("test_user_http_create_tag_uses_jwt_identity 入口")
        try:
            # Arrange：模拟标签存储并创建分析师令牌。
            from httpx import ASGITransport, AsyncClient
            import src.api.auth as auth
            import src.knowledge.tag_store as tag_store_module
            import src.main as main_module
            from src.knowledge.tag_store import KnowledgeTag

            settings = SimpleNamespace(
                env="test", multi_tenant=True, admin_api_key="",
                jwt_secret="m" * 32, jwt_access_token_expire_hours=24,
            )
            tag = KnowledgeTag(
                id=5, name="个人口径", slug="个人口径", scope="private",
                tenant_id=4, owner_user_id=9,
            )
            store = SimpleNamespace(create_personal=AsyncMock(return_value=tag))
            monkeypatch.setattr(auth, "get_settings", lambda: settings)
            monkeypatch.setattr(main_module, "get_settings", lambda: settings)
            monkeypatch.setattr(tag_store_module, "get_knowledge_tag_store", lambda: store)
            monkeypatch.setattr(auth, "_secret_cache", None)
            token = auth.create_access_token(9, 4, "analyst")
            client = AsyncClient(
                transport=ASGITransport(app=main_module.create_app()),
                base_url="http://test",
                headers={"Authorization": f"Bearer {token}"},
            )

            # Act：通过真实 HTTP 路由创建个人标签。
            response = await client.post("/api/v1/knowledge/tags", json={"name": "个人口径"})
            await client.aclose()

            # Assert：服务端使用 JWT 身份调用存储。
            assert response.status_code == 201
            assert response.json()["scope"] == "private"
            assert store.create_personal.await_args.kwargs["tenant_id"] == 4
            assert store.create_personal.await_args.kwargs["user_id"] == 9
            logger.info("test_user_http_create_tag_uses_jwt_identity 完成")
        except Exception as exc:
            logger.error("test_user_http_create_tag_uses_jwt_identity 异常: %s", exc, exc_info=True)
            raise
