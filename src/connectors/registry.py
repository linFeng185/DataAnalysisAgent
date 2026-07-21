"""数据库连接器注册表，按方言创建运行时连接器。"""

from __future__ import annotations

from src.connectors.base import ConnectorBase
from src.datasource.config import DataSourceConfig
from src.logging_config import get_logger


logger = get_logger(__name__)
_registry: dict[str, type[ConnectorBase]] = {}
_defaults_loaded = False


# 方法作用：以装饰器形式注册一个数据库方言连接器。
# Args: dialect - 规范化数据库方言名。
# Returns: 接收 ConnectorBase 子类并完成注册的装饰器。
def register_connector(dialect: str):
    """注册方言连接器，插件只需导入模块即可完成扩展。"""
    normalized = dialect.strip().lower()
    logger.debug("注册连接器装饰器入口", dialect=normalized)

    # 方法作用：把连接器类写入方言注册表。
    # Args: connector_class - ConnectorBase 子类。
    # Returns: 原连接器类。
    def decorator(connector_class: type[ConnectorBase]) -> type[ConnectorBase]:
        logger.debug(
            "连接器类注册入口",
            dialect=normalized,
            connector_class=connector_class.__name__,
        )
        if not normalized:
            logger.error("连接器类注册失败", reason="方言为空")
            raise ValueError("数据库方言不能为空")
        _registry[normalized] = connector_class
        logger.info(
            "连接器类注册完成",
            dialect=normalized,
            connector_class=connector_class.__name__,
        )
        return connector_class

    logger.info("注册连接器装饰器完成", dialect=normalized)
    return decorator


# 方法作用：加载项目内置连接器模块以触发装饰器注册。
# Args: 无。
# Returns: 无返回值。
def _load_default_connectors() -> None:
    """延迟导入内置连接器，避免模块初始化循环依赖。"""
    global _defaults_loaded
    logger.debug("加载默认连接器入口", already_loaded=_defaults_loaded)
    if _defaults_loaded:
        logger.info("加载默认连接器跳过", reason="已加载")
        return
    _defaults_loaded = True
    try:
        import src.connectors.clickhouse  # noqa: F401
        import src.connectors.mssql  # noqa: F401
        import src.connectors.mysql  # noqa: F401
        import src.connectors.oracle  # noqa: F401
        import src.connectors.postgres  # noqa: F401
        import src.connectors.sqlite  # noqa: F401
    except Exception as exc:
        _defaults_loaded = False
        logger.error("加载默认连接器失败", error=str(exc), exc_info=True)
        raise
    logger.info("加载默认连接器完成", dialects=sorted(_registry))


# 方法作用：根据数据源方言创建已注册连接器。
# Args: datasource - 已归一化的数据源配置。
# Returns: 对应方言的 ConnectorBase 实例。
def create_connector(datasource: DataSourceConfig) -> ConnectorBase:
    """创建连接器，不包含任何具体方言条件分支。"""
    _load_default_connectors()
    dialect = datasource.dialect.strip().lower()
    logger.debug("创建连接器入口", datasource=datasource.name, dialect=dialect)
    connector_class = _registry.get(dialect)
    if connector_class is None:
        logger.error("创建连接器失败", datasource=datasource.name, dialect=dialect)
        raise ValueError(f"不支持的方言: {dialect}")
    connector = connector_class(datasource)
    logger.info(
        "创建连接器完成",
        datasource=datasource.name,
        dialect=dialect,
        connector_class=connector_class.__name__,
    )
    return connector


# 方法作用：移除方言注册项，供测试和插件卸载使用。
# Args: dialect - 数据库方言名。
# Returns: 存在并移除时返回 True。
def unregister_connector(dialect: str) -> bool:
    """注销单个连接器，不影响其他方言。"""
    normalized = dialect.strip().lower()
    logger.debug("注销连接器入口", dialect=normalized)
    removed = _registry.pop(normalized, None) is not None
    logger.info("注销连接器完成", dialect=normalized, removed=removed)
    return removed


# 方法作用：列出当前可用数据库方言。
# Args: 无。
# Returns: 排序后的方言名列表。
def list_dialects() -> list[str]:
    """返回稳定的已注册方言清单。"""
    _load_default_connectors()
    result = sorted(_registry)
    logger.info("列出连接器方言完成", count=len(result), dialects=result)
    return result
