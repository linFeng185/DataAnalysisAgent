"""DataSourceRegistry — 数据源统一注册与解析入口。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.config import get_settings
from src.datasource.config import DataSourceConfig
from src.datasource.credential_manager import CredentialManager
from src.datasource.providers.base import DataSourceProvider
from src.exceptions import DataSourceNotFoundError
from src.logging_config import get_logger

logger = get_logger(__name__)

# 模块级单例 — 启动时填充，请求时复用
_registry: DataSourceRegistry | None = None


def get_registry() -> "DataSourceRegistry":
    global _registry
    if _registry is None:
        _registry = DataSourceRegistry()
    return _registry


class DataSourceRegistry:
    """统一入口。内置模式启动时自动填充，外挂模式由 API 动态填充。"""

    def __init__(self) -> None:
        self._providers: dict[str, DataSourceProvider] = {}
        self._cache: dict[str, DataSourceConfig] = {}
        self._credential = CredentialManager()

    def register_provider(self, name: str, provider: DataSourceProvider) -> None:
        self._providers[name] = provider

    async def resolve(self, name: str) -> DataSourceConfig:
        """解析数据源 → 注入 engine + schema → 缓存。"""
        if name in self._cache:
            return self._cache[name]

        for provider in self._providers.values():
            config = await provider.lookup(name)
            if config is None:
                continue

            if config.password:
                config.password = self._credential.decrypt(config.password)
            config.engine = await self._create_engine(config)

            if not await provider.test_connection(config):
                raise ConnectionError(f"数据源 '{name}' 连接失败")

            try:
                config.schema = await provider.extract_schema(config)
            except Exception as e:
                logger.warning("Schema 提取失败", error=str(e))
                from src.datasource.schema_snapshot import SchemaSnapshot
                config.schema = SchemaSnapshot()

            self._cache[name] = config
            return config

        raise DataSourceNotFoundError(name)

    async def _create_engine(self, ds: DataSourceConfig) -> AsyncEngine:
        from urllib.parse import quote_plus
        settings = get_settings()
        pwd = quote_plus(ds.password) if ds.password else ""
        sync_dialects = {"oracle", "mssql"}
        if ds.dialect in sync_dialects:
            from sqlalchemy import create_engine
            url_map = {
                "oracle": f"oracle+oracledb://{ds.username}:{pwd}@{ds.host}:{ds.port}/?service_name={ds.database}",
                "mssql": f"mssql+pymssql://{ds.username}:{pwd}@{ds.host}:{ds.port}/{ds.database}",
            }
            url = url_map.get(ds.dialect, "")
            return create_engine(
                url,
                pool_size=2, max_overflow=5,
                pool_pre_ping=True, pool_recycle=1800,
                echo=False,  # SQL 日志统一走 structlog，避免 echo=True 导致双份输出
            )
        scheme_map = {
            "clickhouse": "clickhouse+asynch",
            "mysql": "mysql+aiomysql",
            "postgres": "postgresql+asyncpg",
        }
        scheme = scheme_map.get(ds.dialect, "postgresql+asyncpg")
        # MySQL 需要 charset=utf8mb4 参数
        extra = "?charset=utf8mb4" if ds.dialect == "mysql" else ""
        url = f"{scheme}://{ds.username}:{pwd}@{ds.host}:{ds.port}/{ds.database}{extra}"
        pool_size = ds.extra_params.get("pool_size", 5)
        return create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=ds.extra_params.get("max_overflow", 10),
            pool_pre_ping=True,
            echo=False,  # SQL 日志统一走 structlog，避免 echo=True 导致双份输出
        )

    async def list_all(self) -> list[dict]:
        result = []
        for provider in self._providers.values():
            for ds in await provider.list_all():
                result.append({
                    "name": ds.name, "dialect": ds.dialect,
                    "mode": ds.mode, "host": ds.host,
                    "description": ds.description,
                    "connected": ds.name in self._cache,
                })
        return result

    async def resolve_or_none(self, name: str) -> DataSourceConfig | None:
        try:
            return await self.resolve(name)
        except DataSourceNotFoundError:
            return None
