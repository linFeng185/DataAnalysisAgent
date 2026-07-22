"""AppContext 应用级依赖容器契约测试。"""

from __future__ import annotations

import asyncio
import ast
import logging
from pathlib import Path
from types import SimpleNamespace

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


class TestAppContextResources:
    """覆盖功能 20.15：资源隔离、并发初始化和关闭。"""

    # 方法作用：验证两个 Context 的同名依赖互不污染。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_context_instances_isolate_resources(self) -> None:
        """不同 FastAPI 应用不得共享 Registry 或存储实例。"""
        logger.debug("test_context_instances_isolate_resources 入口")
        from src.app_context import AppContext

        first = AppContext(SimpleNamespace(multi_tenant=False))
        second = AppContext(SimpleNamespace(multi_tenant=True))
        first_resource = first.get_or_create("resource", object)
        second_resource = second.get_or_create("resource", object)

        assert first_resource is first.get_or_create("resource", object)
        assert second_resource is second.get_or_create("resource", object)
        assert first_resource is not second_resource
        logger.info("test_context_instances_isolate_resources 完成")

    # 方法作用：验证异步资源并发请求只执行一次工厂。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_async_factory_runs_once_under_concurrency(self) -> None:
        """并发首请求不得重复创建连接池或 VectorStore。"""
        logger.debug("test_async_factory_runs_once_under_concurrency 入口")
        from src.app_context import AppContext

        context = AppContext(SimpleNamespace(multi_tenant=False))
        calls = 0

        # 方法作用：模拟存在调度切换的异步资源初始化。
        # Args: 无。
        # Returns: 新建资源对象。
        async def factory() -> object:
            nonlocal calls
            logger.debug("测试异步资源工厂入口")
            calls += 1
            await asyncio.sleep(0)
            result = object()
            logger.info("测试异步资源工厂完成")
            return result

        first, second = await asyncio.gather(
            context.get_or_create_async("async_resource", factory),
            context.get_or_create_async("async_resource", factory),
        )

        assert first is second
        assert calls == 1
        logger.info("test_async_factory_runs_once_under_concurrency 完成")

    # 方法作用：验证资源按初始化逆序关闭且每个 closer 只调用一次。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_close_releases_resources_once_in_reverse_order(self) -> None:
        """应用退出必须先关闭后创建的依赖，重复 close 应幂等。"""
        logger.debug("test_close_releases_resources_once_in_reverse_order 入口")
        from src.app_context import AppContext

        context = AppContext(SimpleNamespace(multi_tenant=False))
        closed: list[str] = []

        # 方法作用：记录资源关闭顺序。
        # Args: resource - 待关闭资源名称。
        # Returns: 无返回值。
        async def closer(resource: str) -> None:
            logger.debug("测试资源关闭入口", extra={"resource": resource})
            closed.append(resource)
            logger.info("测试资源关闭完成", extra={"resource": resource})

        context.set_resource("first", "first", closer=closer)
        context.set_resource("second", "second", closer=closer)

        await context.close()
        await context.close()

        assert closed == ["second", "first"]
        assert context.closed is True
        logger.info("test_close_releases_resources_once_in_reverse_order 完成")

    # 方法作用：验证临时 Context 覆盖退出后精确恢复原值。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_use_app_context_restores_previous_context(self) -> None:
        """嵌套测试和并发任务不得永久覆盖调用方 Context。"""
        logger.debug("test_use_app_context_restores_previous_context 入口")
        from src.app_context import AppContext, get_app_context, use_app_context

        outer = AppContext(SimpleNamespace(multi_tenant=False))
        inner = AppContext(SimpleNamespace(multi_tenant=True))

        with use_app_context(outer):
            assert get_app_context() is outer
            with use_app_context(inner):
                assert get_app_context() is inner
            assert get_app_context() is outer
        logger.info("test_use_app_context_restores_previous_context 完成")


class TestAppContextIntegration:
    """覆盖 FastAPI Depends 和兼容 getter 的 Context 委托。"""

    # 方法作用：验证请求通过中间件和 Depends 获取应用所属 Context。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_fastapi_dependency_returns_application_context(self) -> None:
        """请求依赖必须取 app.state，而不是进程级其他应用实例。"""
        logger.debug("test_fastapi_dependency_returns_application_context 入口")
        from src.app_context import (
            AppContext,
            AppContextMiddleware,
            get_request_app_context,
        )

        context = AppContext(SimpleNamespace(multi_tenant=False, env="test"))
        app = FastAPI()
        app.state.app_context = context
        app.add_middleware(AppContextMiddleware, context=context)

        # 方法作用：返回当前请求 Context 的对象标识。
        # Args: current - FastAPI 注入的 AppContext。
        # Returns: 当前 Context 是否为应用实例。
        @app.get("/context")
        async def read_context(
            current: AppContext = Depends(get_request_app_context),
        ) -> dict[str, bool]:
            logger.debug("测试 Context 路由入口")
            result = {"same": current is context}
            logger.info("测试 Context 路由完成")
            return result

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/context")

        assert response.json() == {"same": True}
        logger.info("test_fastapi_dependency_returns_application_context 完成")

    # 方法作用：验证项目主应用创建时挂载独立 Context 和绑定中间件。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_main_application_owns_context(self) -> None:
        """FastAPI 主应用必须显式持有 Context，不能依赖默认全局入口。"""
        logger.debug("test_main_application_owns_context 入口")
        from src.app_context import AppContext, AppContextMiddleware
        from src.main import app

        assert isinstance(app.state.app_context, AppContext)
        assert AppContextMiddleware in [item.cls for item in app.user_middleware]
        logger.info("test_main_application_owns_context 完成")

    # 方法作用：验证 DataSourceRegistry 兼容 getter 委托给当前 Context。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_registry_getter_uses_current_context(self) -> None:
        """保留的 get_registry 不得维护模块级实例。"""
        logger.debug("test_registry_getter_uses_current_context 入口")
        from src.app_context import AppContext, use_app_context
        from src.datasource.registry import get_registry

        first = AppContext(SimpleNamespace(multi_tenant=False))
        second = AppContext(SimpleNamespace(multi_tenant=False))
        with use_app_context(first):
            first_registry = get_registry()
        with use_app_context(second):
            second_registry = get_registry()

        assert first_registry is not second_registry
        logger.info("test_registry_getter_uses_current_context 完成")

    # 方法作用：验证已迁移模块不再声明资源模块级单例变量。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_resource_modules_have_no_module_singleton_assignments(self) -> None:
        """所有应用级实例必须集中保存到 AppContext。"""
        logger.debug("test_resource_modules_have_no_module_singleton_assignments 入口")
        targets = {
            "src/datasource/registry.py": {"_registry"},
            "src/knowledge/datasource_cache.py": {"_cache_singleton"},
            "src/knowledge/file_store.py": {"_store"},
            "src/knowledge/schema_manager.py": {"_manager"},
            "src/knowledge/tag_store.py": {"_tag_store"},
            "src/knowledge/upload_manager.py": {"_manager"},
            "src/llm/model_registry.py": {"_registry"},
            "src/mcp_client/client_manager.py": {"_client_manager"},
            "src/memory/history_store.py": {"_store"},
            "src/memory/pg_pool.py": {"_pool"},
            "src/memory/session_store.py": {"_store"},
            "src/memory/vector_store.py": {"_store"},
            "src/memory/checkpointer.py": {
                "_pg_ctx",
                "_pg_checkpointer",
                "_mem_checkpointer",
            },
            "src/skill_manager.py": {"_skill_manager"},
        }
        offenders: list[str] = []
        for relative_path, forbidden_names in targets.items():
            tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
            for node in tree.body:
                names: set[str] = set()
                if isinstance(node, ast.Assign):
                    names = {
                        target.id for target in node.targets if isinstance(target, ast.Name)
                    }
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    names = {node.target.id}
                for name in names & forbidden_names:
                    offenders.append(f"{relative_path}:{node.lineno}:{name}")

        assert offenders == []
        logger.info("test_resource_modules_have_no_module_singleton_assignments 完成")
