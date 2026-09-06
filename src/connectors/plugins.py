"""可信安装的连接器插件目录，支持运行时发现和启停。"""

import inspect
import re
from functools import wraps
from importlib import invalidate_caches, metadata
from typing import Any

from src.logging_config import get_logger

logger = get_logger(__name__)
_policy: dict[str, bool] = {}
_external: dict[str, type] = {}
_errors: dict[str, str] = {}


# 应用共享数据库中的启停策略，不销毁在途查询使用的对象。
# Args: policy - 方言名到启用状态。
# Returns: 无返回值。
def apply_plugin_policy(policy: dict[str, bool]) -> None:
    """新执行每次检查状态，关闭操作始终允许。"""
    global _policy
    if any(not isinstance(value, bool) for value in policy.values()):
        raise ValueError("插件启停策略必须是布尔值")
    _policy = dict(policy)


# 检查插件是否仍然注册且启用，保护已经缓存的连接器对象。
# Args: dialect - 方言名。
# Returns: 通过返回 None，停用或卸载抛出 ValueError。
def require_plugin(dialect: str) -> None:
    """停用后不会因 Registry 缓存而继续执行新查询。"""
    from src.connectors.registry import _load_default_connectors, _registry
    _load_default_connectors()
    if dialect not in _registry:
        raise ValueError(f"不支持的方言: {dialect}")
    if not _policy.get(dialect, True):
        logger.warning("数据库插件执行阻断", dialect=dialect, registered=dialect in _registry)
        raise ValueError(f"数据库插件已停用或未安装: {dialect}")


# 在连接器实例的异步执行入口校验撤权，缓存不能绕过平台停用。
# Args: connector - 新创建实例；dialect - 注册名。
# Returns: 无返回值，关闭方法不受限制。
def protect_connector(connector: Any, dialect: str) -> None:
    """已开始的任务可以完成，卸载或替换后的旧对象不能接受新任务。"""
    for name in ("create_engine", "execute", "execute_bounded", "explain", "health_check", "introspect_schema"):
        method = getattr(connector, name, None)
        if method is not None:
            setattr(connector, name, _guard(method, connector.__class__, dialect))


# 保持方法签名并在实际调用时执行最新策略检查。
# Args: method - 绑定异步方法；connector_class - 原注册类；dialect - 注册名。
# Returns: 带撤权检查的异步方法。
def _guard(method: Any, connector_class: type, dialect: str) -> Any:
    # 在进入驱动前阻止已停用、删除或替换的插件。
    # Args: args/kwargs - 原方法参数。
    # Returns: 原方法执行结果。
    @wraps(method)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        from src.connectors.registry import _registry
        require_plugin(dialect)
        if _registry[dialect] is not connector_class:
            raise ValueError("数据库插件版本已变更，请重新解析数据源")
        return await method(*args, **kwargs)
    return guarded


# 验证插件声明，必须同时具备连接器、SQL 解析方言和内省接口。
# Args: connector - 已安装入口导出的类。
# Returns: 可注册的连接器类。
def validate_plugin(connector: Any) -> type:
    """不执行 HTTP 上传的任意模块，也不在服务器中自动安装依赖。"""
    import sqlglot

    from src.connectors.base import ConnectorBase
    if not inspect.isclass(connector) or not issubclass(connector, ConnectorBase):
        raise ValueError("插件必须提供 ConnectorBase 子类")
    if inspect.isabstract(connector):
        raise ValueError("插件不能是未实现驱动的抽象类")
    dialect = str(getattr(connector, "sqlglot_dialect", ""))
    if not dialect or not inspect.iscoroutinefunction(getattr(connector, "introspect_schema", None)):
        raise ValueError("插件必须声明 sqlglot_dialect 和异步 introspect_schema")
    sqlglot.parse_one("SELECT 1", read=dialect)
    fields = getattr(connector, "connection_fields", [])
    if not isinstance(fields, list) or any(not isinstance(field, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", field) for field in fields):
        raise ValueError("连接字段必须是安全标识符列表")
    return connector


# 发现新安装或已删除发行包的 entry point，坏插件不破坏现有目录。
# Args: 无。
# Returns: 当前插件目录。
def discover_plugins() -> list[dict]:
    """仅加载部署环境安装的可信 Python 发行包。"""
    from src.connectors.registry import _load_default_connectors, _registry
    _load_default_connectors()
    invalidate_caches()
    installed = {}
    _errors.clear()
    for entry in metadata.entry_points(group="data_analysis_agent.connectors"):
        try:
            name = entry.name.strip().lower()
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or name in installed:
                raise ValueError("插件标识无效或重复")
            if name in _registry and name not in _external:
                raise ValueError("外部插件不能覆盖内置方言")
            connector = validate_plugin(entry.load())
            installed[name] = connector
            _registry[name] = connector
            _errors.pop(name, None)
        except Exception:
            _errors[entry.name] = "插件加载失败，请检查驱动依赖与声明"
            logger.error("数据库插件发现失败", plugin=entry.name, exc_info=True)
    for name in set(_external) - set(installed):
        _registry.pop(name, None)
    _external.clear()
    _external.update(installed)
    logger.info("数据库插件目录已刷新", external_count=len(installed))
    return plugin_catalog()


# 返回数据库类型及表单字段元数据，避免前端维护固定方言枚举。
# Args: 无。
# Returns: 不含连接凭证的插件描述。
def plugin_catalog() -> list[dict]:
    """内置和外部插件共用同一目录。"""
    from src.connectors.registry import _load_default_connectors, _registry
    _load_default_connectors()
    builtin_fields = {
        "sqlite": ["file_path"],
        "oracle": ["host", "port", "service_name", "schema", "tablespace", "username", "password"],
        "mssql": ["host", "port", "instance", "database", "schema", "username", "password"],
        "postgres": ["host", "port", "database", "schema", "username", "password"],
    }
    items = [{"dialect": name, "label": getattr(connector, "display_name", name),
             "sqlglot_dialect": getattr(connector, "sqlglot_dialect", "tsql" if name == "mssql" else name),
             "enabled": _policy.get(name, True), "external": name in _external,
             "fields": getattr(connector, "connection_fields", builtin_fields.get(name, ["host", "port", "database", "username", "password"])),
             "error": _errors.get(name, "")}
            for name, connector in sorted(_registry.items())]
    return items + [{"dialect": name, "label": name, "sqlglot_dialect": "", "enabled": False, "external": True, "fields": [], "error": error} for name, error in _errors.items() if name not in _registry]


# 将插件方言映射到其权威声明的 SQLGlot 解析方言。
# Args: dialect - 注册方言标识。
# Returns: SQLGlot 方言。
def sql_dialect(dialect: str) -> str:
    """指标、SQL 校验及插件扩展共用同一个映射。"""
    require_plugin(dialect)
    return next(item["sqlglot_dialect"] for item in plugin_catalog() if item["dialect"] == dialect)
