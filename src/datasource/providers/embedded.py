"""内置模式 Provider — 从环境变量自发现数据源。"""

from __future__ import annotations

import os

from src.datasource.config import DataSourceConfig
from src.datasource.introspection import introspect_database
from src.datasource.providers.base import DataSourceProvider
from src.datasource.schema_snapshot import SchemaSnapshot
from src.logging_config import get_logger

logger = get_logger(__name__)

_DIALECT_DEFAULTS = {"clickhouse": 9000, "mysql": 3306, "postgres": 5432}


class EmbeddedDataSourceProvider(DataSourceProvider):
    """从环境变量自动发现数据源。"""

    def __init__(self) -> None:
        self._sources: dict[str, DataSourceConfig] = {}
        self._load_from_env()

    def _load_from_env(self) -> None:
        # 单数据源: DATASOURCE_NAME / DATASOURCE_DIALECT / ...
        if name := os.getenv("DATASOURCE_NAME"):
            self._register(_parse_ds_from_prefix("", name))

        # 多数据源: DATASOURCE_0_NAME / DATASOURCE_1_NAME / ...
        for n in range(5):
            name = os.getenv(f"DATASOURCE_{n}_NAME")
            if not name:
                break
            self._register(_parse_ds_from_prefix(f"DATASOURCE_{n}_", name))

    def _register(self, ds: DataSourceConfig) -> None:
        self._sources[ds.name] = ds
        logger.info("发现数据源", name=ds.name, dialect=ds.dialect)

    async def lookup(self, name: str) -> DataSourceConfig | None:
        return self._sources.get(name)

    async def list_all(self) -> list[DataSourceConfig]:
        return list(self._sources.values())

    async def extract_schema(self, ds: DataSourceConfig) -> SchemaSnapshot:
        async def _executor(ds_cfg, sql, params):
            import sqlalchemy as sa
            async with ds_cfg.engine.connect() as conn:
                result = await conn.execute(sa.text(sql), params)
                return [dict(row._mapping) for row in result]
        return await introspect_database(ds, _executor)

    async def test_connection(self, ds: DataSourceConfig) -> bool:
        try:
            import sqlalchemy as sa
            async with ds.engine.connect() as conn:
                await conn.execute(sa.text("SELECT 1"))
            return True
        except Exception:
            return False


def _parse_ds_from_prefix(prefix: str, name: str) -> DataSourceConfig:
    dialect = os.getenv(f"{prefix}DIALECT", "clickhouse")
    return DataSourceConfig(
        name=name,
        mode="embedded",
        dialect=dialect,
        host=os.getenv(f"{prefix}HOST", "localhost"),
        port=int(os.getenv(f"{prefix}PORT", "0")) or _DIALECT_DEFAULTS.get(dialect, 0),
        database=os.getenv(f"{prefix}DATABASE", ""),
        username=os.getenv(f"{prefix}USERNAME", ""),
        password=os.getenv(f"{prefix}PASSWORD", ""),
    )
