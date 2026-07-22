"""生产 OpenAPI 元数据暴露回归测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace


logger = logging.getLogger(__name__)


class TestOpenApiExposure:
    """覆盖生产环境 API 文档与 schema 同时关闭。"""

    # 方法作用：验证生产应用不会挂载 /openapi.json。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_production_disables_openapi_schema(self, monkeypatch) -> None:
        """关闭 Swagger UI 时不能继续公开完整接口清单。"""
        logger.debug("test_production_disables_openapi_schema 入口")
        import src.config as config_module
        import src.main as main_module

        settings = SimpleNamespace(env="prod")
        monkeypatch.setattr(main_module, "get_settings", lambda: settings)
        monkeypatch.setattr(config_module, "validate_production_settings", lambda value: None)

        app = main_module.create_app()

        assert app.docs_url is None
        assert app.openapi_url is None
        logger.info("test_production_disables_openapi_schema 完成")
