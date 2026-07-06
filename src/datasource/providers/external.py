"""外挂模式 Provider — YAML/API 手动注册数据源，独立连接池。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from src.datasource.config import DataSourceConfig
from src.datasource.credential_manager import CredentialManager
from src.datasource.introspection import introspect_database
from src.datasource.providers.base import DataSourceProvider
from src.datasource.schema_snapshot import SchemaSnapshot
from src.logging_config import get_logger

logger = get_logger(__name__)
_DIALECT_DEFAULTS = {"clickhouse": 9000, "mysql": 3306, "postgres": 5432}


class ExternalDataSourceProvider(DataSourceProvider):
    """外挂模式 — 独立部署，一个 Agent 连接多个项目的数据源。

    配置来源: YAML 文件 | 管理 API | 配置数据库(远期)
    """

    def __init__(self) -> None:
        self._sources: dict[str, DataSourceConfig] = {}

    @classmethod
    def from_yaml(cls, yaml_path: str = "config/datasources.yaml") -> "ExternalDataSourceProvider":
        """2.3.6 从 YAML 加载并返回已填充的 Provider。"""
        provider = cls()
        provider.load_yaml(yaml_path)
        return provider

    def load_yaml(self, yaml_path: str = "config/datasources.yaml") -> list[DataSourceConfig]:
        """2.3.6 解析 YAML 配置文件。"""
        path = Path(yaml_path)
        if not path.exists():
            logger.warning("配置文件不存在", path=yaml_path)
            return []

        with open(path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f) or {}

        sources = []
        cred_mgr = CredentialManager()
        for name, cfg in config_data.get("datasources", {}).items():
            raw_pw = cred_mgr.resolve_env_ref(str(cfg.get("password", "")))
            ds = DataSourceConfig(
                name=name, mode="external",
                dialect=cfg.get("dialect", "postgres"),
                version=cfg.get("version", ""),
                host=cfg.get("host", "localhost"),
                port=cfg.get("port", 0) or _DIALECT_DEFAULTS.get(cfg.get("dialect", ""), 0),
                database=cfg.get("database", ""),
                username=cfg.get("username", ""),
                password=cred_mgr.encrypt(raw_pw),
                description=cfg.get("description", ""),
                tags=cfg.get("tags", []),
                extra_params=cfg.get("extra_params", {}),
            )
            self._register(ds)
            sources.append(ds)
        logger.info("YAML 配置加载完成", count=len(sources))
        return sources

    async def register(self, req: "DataSourceCreateRequest") -> DataSourceConfig:  # noqa: F821
        """2.3.2 注册新数据源: 加密凭证 → 后台预采集 Schema。"""
        cred_mgr = CredentialManager()
        ds = DataSourceConfig(
            name=req.name, mode="external", dialect=req.dialect,
            version=req.version,
            host=req.host, port=req.port or _DIALECT_DEFAULTS.get(req.dialect, 0),
            database=req.database, username=req.username,
            password=cred_mgr.encrypt(req.password),
            description=req.description, tags=req.tags, extra_params=req.extra_params,
        )
        self._register(ds)
        asyncio.create_task(self._prefetch_schema(ds))
        return ds

    async def unregister(self, name: str) -> None:
        """2.3.3 移除数据源，关闭连接池。"""
        if name in self._sources:
            ds = self._sources.pop(name)
            if ds.engine:
                await ds.engine.dispose()
            logger.info("数据源已移除", name=name)

    async def _prefetch_schema(self, ds: DataSourceConfig) -> None:
        """后台预采集 Schema。"""
        try:
            ds.schema = await self.extract_schema(ds)
            logger.info("Schema 预采集完成", name=ds.name)
        except Exception as e:
            logger.warning("Schema 预采集失败", name=ds.name, error=str(e))

    async def extract_schema(self, ds: DataSourceConfig) -> SchemaSnapshot:
        """2.3.5 纯 DB 内省提取 Schema。"""
        async def _executor(ds_cfg, sql, params):
            import sqlalchemy as sa
            async with ds_cfg.engine.connect() as conn:
                result = await conn.execute(sa.text(sql), params)
                return [dict(row._mapping) for row in result]
        return await introspect_database(ds, _executor)

    def _register(self, ds: DataSourceConfig) -> None:
        self._sources[ds.name] = ds

    async def lookup(self, name: str) -> DataSourceConfig | None:
        return self._sources.get(name)

    async def list_all(self) -> list[DataSourceConfig]:
        return list(self._sources.values())

    async def test_connection(self, ds: DataSourceConfig) -> bool:
        """2.3.4 测试连通性。"""
        try:
            import sqlalchemy as sa
            async with ds.engine.connect() as conn:
                await conn.execute(sa.text("SELECT 1"))
            return True
        except Exception:
            return False


class DataSourceCreateRequest:
    """2.1.5 外挂模式注册数据源请求体 (临时，后续迁移到 api/schemas.py)。"""

    def __init__(
        self, name: str, dialect: str,
        host: str = "localhost", port: int = 0,
        database: str = "", username: str = "", password: str = "",
        description: str = "",
        tags: list[str] | None = None,
        extra_params: dict | None = None,
    ) -> None:
        self.name = name
        self.dialect = dialect
        self.host = host
        self.port = port
        self.database = database
        self.username = username
        self.password = password
        self.description = description
        self.tags = tags or []
        self.extra_params = extra_params or {}
