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
        """解析数据源 → 注入 engine → 缓存。

        schema 延迟加载：不在 resolve 时 introspect，
        交由 SchemaManager（ChromaDB 缓存优先）统一管理。
        避免每次服务重启都全量 INFORMATION_SCHEMA 查询。
        """
        if name in self._cache:
            return self._cache[name]

        for provider in self._providers.values():
            config = await provider.lookup(name)
            if config is None:
                continue

            if config.password:
                config.password = self._credential.resolve_env_ref(config.password)
                config.password = self._credential.decrypt(config.password)

            try:
                config.engine = await self._create_engine(config)

                if not await provider.test_connection(config):
                    raise ConnectionError(f"数据源 '{name}' 连接失败")

                # 不在 resolve 时做全量 introspect——
                # 如果 config 已有预设 schema 则保留，否则设为 None
                # SchemaManager.get_or_fetch_schema 会在需要时才从 ChromaDB 取
            except (ConnectionError, Exception) as e:
                logger.warning("数据源不可用，跳过", datasource=name, error=str(e)[:120])
                continue

            self._cache[name] = config
            return config

        raise DataSourceNotFoundError(name)

    async def _create_engine(self, ds: DataSourceConfig) -> AsyncEngine:
        from urllib.parse import quote_plus
        settings = get_settings()
        pwd = quote_plus(ds.password) if ds.password else ""
        sync_dialects = {"oracle", "mssql"}
        try:
            return self._build_engine(ds, pwd, sync_dialects)
        except Exception as e:
            raise ConnectionError(f"数据源 '{ds.name}' 方言驱动不可用: {ds.dialect} — {e}")

    def _build_engine(self, ds, pwd, sync_dialects):
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
        seen: set[str] = set()
        for provider in self._providers.values():
            for ds in await provider.list_all():
                seen.add(ds.name)
                result.append({
                    "name": ds.name, "dialect": ds.dialect,
                    "mode": ds.mode, "host": ds.host,
                    "description": ds.description,
                    "connected": ds.name in self._cache,
                })
        # 包含直接注入缓存的 demo 数据源（不通过 Provider 注册）
        for name, config in self._cache.items():
            if name not in seen:
                result.append({
                    "name": config.name, "dialect": config.dialect,
                    "mode": config.mode, "host": config.host or "localhost",
                    "description": config.description or "",
                    "connected": True,
                })
        return result

    async def resolve_or_none(self, name: str) -> DataSourceConfig | None:
        try:
            return await self.resolve(name)
        except Exception:
            return None
