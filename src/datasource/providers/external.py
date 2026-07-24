"""外挂模式 Provider — YAML/API 手动注册数据源，独立连接池。"""

from __future__ import annotations

import inspect
import json
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
            username = cred_mgr.resolve_env_ref(str(cfg.get("username", "")))
            ds = DataSourceConfig(
                name=name, mode="external",
                dialect=cfg.get("dialect", "postgres"),
                version=cfg.get("version", ""),
                host=cfg.get("host", "localhost"),
                port=cfg.get("port", 0) or _DIALECT_DEFAULTS.get(cfg.get("dialect", ""), 0),
                database=cfg.get("database", ""),
                username=username,
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
        """注册新数据源并加密凭证。

        Args:
            req: 数据源创建请求。

        Returns:
            已持久在当前 Provider 中的数据源配置。
        """
        logger.debug("外部数据源注册入口", datasource=req.name, dialect=req.dialect)
        cred_mgr = CredentialManager()
        database = req.database
        if req.dialect == "sqlite":
            database = getattr(req, "file_path", "") or req.database or ":memory:"
        ds = DataSourceConfig(
            name=req.name, mode="external", dialect=req.dialect,
            version=req.version,
            host=req.host, port=req.port or _DIALECT_DEFAULTS.get(req.dialect, 0),
            database=database, username=req.username,
            password=cred_mgr.encrypt(req.password),
            description=req.description, tags=req.tags, extra_params=req.extra_params,
        )
        self._register(ds)
        logger.info("外部数据源注册完成", datasource=ds.name, dialect=ds.dialect)
        return ds

    # 方法作用：把页面创建的数据源及加密凭证持久化到状态数据库。
    # Args: ds - 已加密密码的数据源配置；tenant_id - 所属租户；owner_user_id - 创建者。
    # Returns: 持久化成功时无返回值。
    async def persist(
        self,
        ds: DataSourceConfig,
        *,
        tenant_id: int,
        owner_user_id: int,
    ) -> None:
        logger.debug(
            "页面数据源持久化入口",
            datasource=ds.name,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        from src.memory.pg_pool import get_pg_pool

        ds.tenant_id = tenant_id
        ds.owner_user_id = owner_user_id
        try:
            pool = await get_pg_pool()
            async with pool.acquire() as connection:
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO datasource_configs "
                        "(name, tenant_id, owner_user_id, dialect, version, host, port, "
                        "database_name, username, encrypted_password, description, extra_params) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb) "
                        "ON CONFLICT (name) DO UPDATE SET tenant_id=EXCLUDED.tenant_id, "
                        "owner_user_id=EXCLUDED.owner_user_id, dialect=EXCLUDED.dialect, "
                        "version=EXCLUDED.version, host=EXCLUDED.host, port=EXCLUDED.port, "
                        "database_name=EXCLUDED.database_name, username=EXCLUDED.username, "
                        "encrypted_password=EXCLUDED.encrypted_password, "
                        "description=EXCLUDED.description, extra_params=EXCLUDED.extra_params, "
                        "updated_at=NOW()",
                        ds.name, tenant_id, owner_user_id, ds.dialect, ds.version,
                        ds.host, ds.port, ds.database, ds.username, ds.password,
                        ds.description, json.dumps(ds.extra_params, ensure_ascii=False),
                    )
                    await connection.execute(
                        "INSERT INTO datasource_permissions "
                        "(datasource_name, tenant_id, owner_user_id, visibility, access_level) "
                        "VALUES ($1,$2,$3,'tenant','read') "
                        "ON CONFLICT (datasource_name, tenant_id) DO UPDATE SET "
                        "owner_user_id=EXCLUDED.owner_user_id, visibility='tenant', access_level='read'",
                        ds.name, tenant_id, owner_user_id,
                    )
        except Exception:
            logger.error("页面数据源持久化失败", datasource=ds.name, exc_info=True)
            raise
        logger.info("页面数据源持久化完成", datasource=ds.name, tenant_id=tenant_id)

    # 方法作用：从状态数据库恢复页面维护的数据源配置。
    # Args: 无。
    # Returns: 本次加载的数据源数量。
    async def load_persisted(self) -> int:
        logger.debug("持久化数据源加载入口")
        from src.memory.pg_pool import get_pg_pool

        try:
            pool = await get_pg_pool()
            async with pool.acquire() as connection:
                rows = await connection.fetch(
                    "SELECT name, tenant_id, owner_user_id, dialect, version, host, port, "
                    "database_name, username, encrypted_password, description, extra_params "
                    "FROM datasource_configs ORDER BY id",
                )
            for row in rows:
                ds = DataSourceConfig(
                    name=str(row["name"]),
                    mode="external",
                    dialect=str(row["dialect"]),
                    version=str(row["version"] or ""),
                    host=str(row["host"] or "localhost"),
                    port=int(row["port"] or 0),
                    database=str(row["database_name"] or ""),
                    username=str(row["username"] or ""),
                    password=str(row["encrypted_password"] or ""),
                    description=str(row["description"] or ""),
                    extra_params=dict(row["extra_params"] or {}),
                    tenant_id=int(row["tenant_id"]),
                    owner_user_id=int(row["owner_user_id"]),
                )
                self._register(ds)
        except Exception:
            logger.error("持久化数据源加载失败", exc_info=True)
            raise
        logger.info("持久化数据源加载完成", count=len(rows))
        return len(rows)

    # 方法作用：删除页面持久化的数据源和对应权限记录。
    # Args: name - 数据源名称；tenant_id - 当前租户；platform_admin - 是否平台超级管理员。
    # Returns: 存在并删除时返回 True。
    async def delete_persisted(
        self,
        name: str,
        *,
        tenant_id: int,
        platform_admin: bool,
    ) -> bool:
        logger.debug(
            "持久化数据源删除入口",
            datasource=name,
            tenant_id=tenant_id,
            platform_admin=platform_admin,
        )
        from src.memory.pg_pool import get_pg_pool

        try:
            pool = await get_pg_pool()
            async with pool.acquire() as connection:
                async with connection.transaction():
                    row = await connection.fetchrow(
                        "DELETE FROM datasource_configs WHERE name=$1 "
                        "AND ($2::bool OR tenant_id=$3) RETURNING tenant_id",
                        name, platform_admin, tenant_id,
                    )
                    if row is not None:
                        await connection.execute(
                            "DELETE FROM datasource_permissions WHERE datasource_name=$1 "
                            "AND tenant_id=$2",
                            name, int(row["tenant_id"]),
                        )
        except Exception:
            logger.error("持久化数据源删除失败", datasource=name, exc_info=True)
            raise
        removed = row is not None
        logger.info("持久化数据源删除完成", datasource=name, removed=removed)
        return removed

    async def unregister(self, name: str) -> None:
        """移除数据源并兼容关闭同步/异步连接池。

        Args:
            name: 数据源名称。

        Returns:
            无返回值。
        """
        logger.debug("外部数据源注销入口", datasource=name)
        if name in self._sources:
            ds = self._sources.pop(name)
            if ds.connector:
                await ds.connector.close()
            elif ds.engine:
                dispose_result = ds.engine.dispose()
                if inspect.isawaitable(dispose_result):
                    await dispose_result
            ds.engine = None
            ds.connector = None
            logger.info("数据源已移除", name=name)
        else:
            logger.info("外部数据源注销跳过", datasource=name, reason="不存在")

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
        """2.3.4 测试数据源连通性。

        Args:
            ds: 已创建 SQLAlchemy engine 的数据源配置。

        Returns:
            探针 SQL 成功返回 True，否则返回 False。
        """
        connector = ds.connector
        if connector is None:
            from src.connectors.registry import create_connector

            connector = create_connector(ds)
            connector.attach_engine(ds.engine)
        probe_sql = connector.probe_sql
        logger.debug(
            "数据源连通性探针入口",
            datasource=ds.name,
            dialect=ds.dialect,
            probe_sql=probe_sql,
        )
        try:
            healthy = await connector.health_check()
            if not healthy:
                raise ConnectionError(f"数据源 '{ds.name}' 探针失败")
            logger.info(
                "数据源连通性探针完成",
                datasource=ds.name,
                dialect=ds.dialect,
                success=True,
            )
            return True
        except Exception as exc:
            logger.error(
                "数据源连通性探针失败",
                datasource=ds.name,
                dialect=ds.dialect,
                probe_sql=probe_sql,
                error=str(exc)[:500],
                exc_info=True,
            )
            return False


class DataSourceCreateRequest:
    """2.1.5 外挂模式注册数据源请求体 (临时，后续迁移到 api/schemas.py)。"""

    def __init__(
        self, name: str, dialect: str,
        host: str = "localhost", port: int = 0,
        database: str = "", username: str = "", password: str = "",
        description: str = "", version: str = "",
        tags: list[str] | None = None,
        extra_params: dict | None = None,
    ) -> None:
        """初始化向后兼容的数据源注册请求。

        Args:
            name: 数据源名称。
            dialect: 数据库方言。
            host: 主机地址。
            port: 端口。
            database: 数据库名。
            username: 用户名。
            password: 密码。
            description: 描述。
            version: 数据库版本。
            tags: 标签列表。
            extra_params: 扩展连接参数。

        Returns:
            无返回值。
        """
        logger.debug("构建兼容注册请求入口", datasource=name, dialect=dialect)
        self.name = name
        self.dialect = dialect
        self.version = version
        self.host = host
        self.port = port
        self.database = database
        self.username = username
        self.password = password
        self.description = description
        self.tags = tags or []
        self.extra_params = extra_params or {}
        logger.info("构建兼容注册请求完成", datasource=name, dialect=dialect)
