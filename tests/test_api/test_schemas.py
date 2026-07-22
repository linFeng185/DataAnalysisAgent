"""API Pydantic Schema 默认值回归测试。"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


class TestSchemaMutableDefaults:
    """覆盖 list/dict 字段必须显式使用 default_factory。"""

    # 方法作用：验证所有可变默认字段均由 Pydantic 工厂创建。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_mutable_fields_use_default_factory(self) -> None:
        """模型定义不得依赖框架对共享可变默认值的隐式复制行为。"""
        logger.debug("test_mutable_fields_use_default_factory 入口")
        try:
            # Arrange
            from src.api.schemas import (
                ChatResponse,
                DataSourceCreateRequest,
                MCPServerCreate,
                TableInfo,
            )

            fields = [
                ChatResponse.model_fields["data"],
                ChatResponse.model_fields["analysis"],
                ChatResponse.model_fields["chart"],
                DataSourceCreateRequest.model_fields["tags"],
                DataSourceCreateRequest.model_fields["extra_params"],
                TableInfo.model_fields["columns"],
                MCPServerCreate.model_fields["env_vars"],
            ]

            # Act / Assert
            assert all(field.default_factory is not None for field in fields)
            logger.info("test_mutable_fields_use_default_factory 完成")
        except Exception as exc:
            logger.error(
                "test_mutable_fields_use_default_factory 异常: %s",
                exc,
                exc_info=True,
            )
            raise
