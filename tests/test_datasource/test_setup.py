"""演示数据源启动初始化测试。"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


class TestDemoDatasourceSetup:
    """覆盖演示库建表、种子数据和 Registry 注册。"""

    # 方法作用：验证演示数据源通过当前 Context 注册并可真实查询。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_ensure_demo_datasource_registers_queryable_database(self) -> None:
        """初始化后 Registry 必须解析同一实例，订单种子数固定为 15。"""
        logger.debug("test_ensure_demo_datasource_registers_queryable_database 入口")
        from src.app_context import AppContext, use_app_context
        from src.datasource.registry import get_registry
        from src.datasource.setup import ensure_demo_datasource

        context = AppContext(SimpleNamespace(multi_tenant=False))
        with use_app_context(context):
            datasource = await ensure_demo_datasource()
            resolved = await get_registry().resolve("demo")
            async with resolved.engine.connect() as connection:
                count = await connection.scalar(sa.text("SELECT COUNT(*) FROM orders"))

        assert resolved is datasource
        assert count == 15
        assert [table.name for table in datasource.schema.tables] == ["orders", "users"]
        await datasource.engine.dispose()
        logger.info("test_ensure_demo_datasource_registers_queryable_database 完成")

    # 方法作用：验证启动模块不再写 Registry 私有缓存。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_setup_uses_registry_public_api(self) -> None:
        """跨模块写 `_cache` 会绕过日志和未来校验，必须禁止。"""
        logger.debug("test_setup_uses_registry_public_api 入口")
        source = (ROOT / "src/datasource/setup.py").read_text(encoding="utf-8")
        assert "._cache" not in source
        logger.info("test_setup_uses_registry_public_api 完成")
