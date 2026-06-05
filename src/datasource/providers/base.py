"""DataSourceProvider 抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.datasource.config import DataSourceConfig
from src.datasource.schema_snapshot import SchemaSnapshot


class DataSourceProvider(ABC):
    """内置/外挂模式的公共接口。"""

    @abstractmethod
    async def lookup(self, name: str) -> DataSourceConfig | None:
        """按名称查找数据源配置。"""

    @abstractmethod
    async def list_all(self) -> list[DataSourceConfig]:
        """列出所有数据源。"""

    @abstractmethod
    async def extract_schema(self, ds: DataSourceConfig) -> SchemaSnapshot:
        """提取 Schema。"""

    @abstractmethod
    async def test_connection(self, ds: DataSourceConfig) -> bool:
        """测试连接。"""
