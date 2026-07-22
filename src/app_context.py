"""应用级依赖容器、请求绑定和兼容上下文入口。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from src.logging_config import get_logger
from src.security.tenant_policy import TenantPolicy


logger = get_logger(__name__)

T = TypeVar("T")
ResourceCloser = Callable[[Any], Awaitable[None] | None]
_MISSING = object()


@dataclass(slots=True)
class AppContext:
    """持有单个应用实例的配置、租户策略和共享资源。"""

    settings: Any
    tenant_policy: TenantPolicy | None = None
    _resources: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _closers: dict[str, ResourceCloser] = field(default_factory=dict, init=False, repr=False)
    _resource_order: list[str] = field(default_factory=list, init=False, repr=False)
    _async_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    # 方法作用：根据 Settings 初始化当前应用的租户策略。
    # Args: self - 当前 AppContext。
    # Returns: 无返回值。
    def __post_init__(self) -> None:
        logger.debug("AppContext.__post_init__ 入口")
        if self.tenant_policy is None:
            self.tenant_policy = TenantPolicy(
                multi_tenant=bool(getattr(self.settings, "multi_tenant", False)),
            )
        logger.info(
            "AppContext.__post_init__ 完成",
            multi_tenant=self.tenant_policy.multi_tenant,
        )

    @property
    def closed(self) -> bool:
        """返回 Context 是否已经完成关闭。"""
        logger.debug("读取 AppContext 关闭状态入口")
        result = self._closed
        logger.info("读取 AppContext 关闭状态完成", closed=result)
        return result

    # 方法作用：确保关闭后的 Context 不再创建或替换资源。
    # Args: self - 当前 AppContext。
    # Returns: 无返回值，已关闭时抛出 RuntimeError。
    def _ensure_open(self) -> None:
        logger.debug("校验 AppContext 可用状态入口", closed=self._closed)
        if self._closed:
            logger.error("校验 AppContext 可用状态失败", reason="Context 已关闭")
            raise RuntimeError("AppContext 已关闭")
        logger.info("校验 AppContext 可用状态完成")

    # 方法作用：读取已创建资源，不触发工厂。
    # Args: self - 当前 Context；name - 资源名；default - 不存在时返回值。
    # Returns: 已存在资源或 default。
    def get_resource(self, name: str, default: T | None = None) -> Any | T | None:
        logger.debug("读取 AppContext 资源入口", resource=name)
        result = self._resources.get(name, default)
        logger.info("读取 AppContext 资源完成", resource=name, found=name in self._resources)
        return result

    # 方法作用：显式注册资源和可选关闭器。
    # Args: self - 当前 Context；name - 资源名；value - 实例；closer - 关闭函数；replace - 是否允许覆盖。
    # Returns: 注册后的资源实例。
    def set_resource[T](
        self,
        name: str,
        value: T,
        *,
        closer: ResourceCloser | None = None,
        replace: bool = False,
    ) -> T:
        logger.debug("注册 AppContext 资源入口", resource=name, replace=replace)
        self._ensure_open()
        if not name.strip():
            logger.error("注册 AppContext 资源失败", reason="资源名为空")
            raise ValueError("资源名不能为空")
        if name in self._resources and not replace:
            if self._resources[name] is value:
                logger.info("注册 AppContext 资源完成", resource=name, reused=True)
                return value
            logger.error("注册 AppContext 资源失败", resource=name, reason="资源已存在")
            raise ValueError(f"资源已存在: {name}")
        if name not in self._resources:
            self._resource_order.append(name)
        self._resources[name] = value
        if closer is not None:
            self._closers[name] = closer
        logger.info("注册 AppContext 资源完成", resource=name, reused=False)
        return value

    # 方法作用：同步惰性创建资源并保证单个 Context 内只执行一次工厂。
    # Args: self - 当前 Context；name - 资源名；factory - 同步工厂；closer - 可选关闭器。
    # Returns: 已存在或新创建的资源。
    def get_or_create[T](
        self,
        name: str,
        factory: Callable[[], T],
        *,
        closer: ResourceCloser | None = None,
    ) -> T:
        logger.debug("同步获取 AppContext 资源入口", resource=name)
        self._ensure_open()
        existing = self._resources.get(name, _MISSING)
        if existing is not _MISSING:
            logger.info("同步获取 AppContext 资源完成", resource=name, reused=True)
            return cast(T, existing)
        try:
            value = factory()
            result = self.set_resource(name, value, closer=closer)
        except Exception as exc:
            logger.error(
                "同步获取 AppContext 资源失败",
                resource=name,
                error=str(exc),
                exc_info=True,
            )
            raise
        logger.info("同步获取 AppContext 资源完成", resource=name, reused=False)
        return result

    # 方法作用：并发安全地惰性创建异步资源。
    # Args: self - 当前 Context；name - 资源名；factory - 异步工厂；closer - 可选关闭器。
    # Returns: 已存在或新创建的资源。
    async def get_or_create_async[T](
        self,
        name: str,
        factory: Callable[[], Awaitable[T]],
        *,
        closer: ResourceCloser | None = None,
    ) -> T:
        logger.debug("异步获取 AppContext 资源入口", resource=name)
        self._ensure_open()
        existing = self._resources.get(name, _MISSING)
        if existing is not _MISSING:
            logger.info("异步获取 AppContext 资源完成", resource=name, reused=True)
            return cast(T, existing)
        lock = self._async_locks.setdefault(name, asyncio.Lock())
        async with lock:
            self._ensure_open()
            existing = self._resources.get(name, _MISSING)
            if existing is not _MISSING:
                logger.info("异步获取 AppContext 资源完成", resource=name, reused=True)
                return cast(T, existing)
            try:
                value = await factory()
                result = self.set_resource(name, value, closer=closer)
            except Exception as exc:
                logger.error(
                    "异步获取 AppContext 资源失败",
                    resource=name,
                    error=str(exc),
                    exc_info=True,
                )
                raise
        logger.info("异步获取 AppContext 资源完成", resource=name, reused=False)
        return result

    # 方法作用：关闭并移除单个资源。
    # Args: self - 当前 Context；name - 资源名。
    # Returns: 实际移除资源时返回 True。
    async def close_resource(self, name: str) -> bool:
        logger.debug("关闭 AppContext 资源入口", resource=name)
        if name not in self._resources:
            logger.info("关闭 AppContext 资源完成", resource=name, existed=False)
            return False
        value = self._resources.pop(name)
        closer = self._closers.pop(name, None)
        self._async_locks.pop(name, None)
        if name in self._resource_order:
            self._resource_order.remove(name)
        if closer is not None:
            try:
                result = closer(value)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.error(
                    "关闭 AppContext 资源失败",
                    resource=name,
                    error=str(exc),
                    exc_info=True,
                )
                raise
        logger.info("关闭 AppContext 资源完成", resource=name, existed=True)
        return True

    # 方法作用：按资源创建逆序关闭当前应用全部依赖。
    # Args: self - 当前 Context。
    # Returns: 无返回值。
    async def close(self) -> None:
        logger.debug("关闭 AppContext 入口", resource_count=len(self._resources))
        async with self._close_lock:
            if self._closed:
                logger.info("关闭 AppContext 完成", already_closed=True)
                return
            self._closed = True
            errors: list[BaseException] = []
            for name in list(reversed(self._resource_order)):
                try:
                    await self.close_resource(name)
                except Exception as exc:
                    errors.append(exc)
            self._resources.clear()
            self._closers.clear()
            self._resource_order.clear()
            self._async_locks.clear()
            if errors:
                logger.error("关闭 AppContext 失败", error_count=len(errors), exc_info=True)
                raise ExceptionGroup("AppContext 资源关闭失败", errors)
        logger.info("关闭 AppContext 完成", already_closed=False)


_current_app_context: ContextVar[AppContext | None] = ContextVar(
    "current_app_context",
    default=None,
)
_default_app_context: AppContext | None = None


# 方法作用：创建使用指定 Settings 的独立应用 Context。
# Args: settings - 当前应用配置。
# Returns: 新建 AppContext。
def create_app_context(settings: Any) -> AppContext:
    logger.debug("创建 AppContext 入口")
    result = AppContext(settings=settings)
    logger.info("创建 AppContext 完成")
    return result


# 方法作用：读取当前请求 Context，缺失时惰性创建兼容 Context。
# Args: 无。
# Returns: 当前或默认 AppContext。
def get_app_context() -> AppContext:
    logger.debug("获取 AppContext 入口")
    current = _current_app_context.get()
    if current is not None:
        logger.info("获取 AppContext 完成", source="current")
        return current
    global _default_app_context
    if _default_app_context is None or _default_app_context.closed:
        from src.config import Settings

        _default_app_context = AppContext(Settings())
    logger.info("获取 AppContext 完成", source="default")
    return _default_app_context


# 方法作用：读取当前 AppContext 持有的集中租户策略。
# Args: 无。
# Returns: 当前应用唯一的 TenantPolicy 实例。
def get_tenant_policy() -> TenantPolicy:
    logger.debug("获取 TenantPolicy 入口")
    policy = get_app_context().tenant_policy
    if policy is None:
        logger.error("获取 TenantPolicy 失败", reason="AppContext 未初始化租户策略")
        raise RuntimeError("AppContext 未初始化 TenantPolicy")
    logger.info("获取 TenantPolicy 完成")
    return policy


# 方法作用：替换 CLI/兼容路径使用的默认应用 Context。
# Args: context - 新默认 Context。
# Returns: 原默认 Context，可能为 None。
def set_default_app_context(context: AppContext) -> AppContext | None:
    logger.debug("设置默认 AppContext 入口")
    global _default_app_context
    previous = _default_app_context
    _default_app_context = context
    logger.info("设置默认 AppContext 完成", replaced=previous is not None)
    return previous


# 方法作用：临时绑定当前协程 AppContext 并在退出时恢复。
# Args: context - 临时绑定 Context。
# Returns: 绑定期间的 Context。
@contextmanager
def use_app_context(context: AppContext) -> Iterator[AppContext]:
    logger.debug("绑定 AppContext 入口")
    token = _current_app_context.set(context)
    try:
        yield context
        logger.info("绑定 AppContext 使用完成")
    except Exception as exc:
        logger.error("绑定 AppContext 使用失败", error=str(exc), exc_info=True)
        raise
    finally:
        _current_app_context.reset(token)
        logger.info("绑定 AppContext 已恢复")


# 方法作用：异步临时绑定当前协程 AppContext 并在退出时恢复。
# Args: context - 临时绑定 Context。
# Returns: 绑定期间的 Context。
@asynccontextmanager
async def use_app_context_async(context: AppContext) -> AsyncIterator[AppContext]:
    logger.debug("异步绑定 AppContext 入口")
    with use_app_context(context):
        yield context
    logger.info("异步绑定 AppContext 完成")


# 方法作用：从 FastAPI app.state 获取当前应用 Context。
# Args: request - 当前 FastAPI 请求。
# Returns: 请求所属 AppContext。
def get_request_app_context(request: Request) -> AppContext:
    logger.debug("FastAPI AppContext 依赖入口", path=request.url.path)
    context = getattr(request.app.state, "app_context", None)
    if not isinstance(context, AppContext):
        logger.error("FastAPI AppContext 依赖失败", path=request.url.path)
        raise RuntimeError("FastAPI 应用未配置 AppContext")
    logger.info("FastAPI AppContext 依赖完成", path=request.url.path)
    return context


class AppContextMiddleware:
    """把应用级 Context 绑定到完整 ASGI 请求生命周期。"""

    # 方法作用：保存下游应用和当前 FastAPI 应用的 Context。
    # Args: app - 下游 ASGI 应用；context - 应用 Context。
    # Returns: 无返回值。
    def __init__(self, app: ASGIApp, *, context: AppContext) -> None:
        logger.debug("AppContextMiddleware.__init__ 入口")
        self.app = app
        self.context = context
        logger.info("AppContextMiddleware.__init__ 完成")

    # 方法作用：在 HTTP/WebSocket 请求期间绑定应用 Context。
    # Args: scope - ASGI 作用域；receive - 接收通道；send - 发送通道。
    # Returns: 无返回值。
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = str(scope.get("type", ""))
        logger.debug("AppContextMiddleware 调用入口", scope_type=scope_type)
        if scope_type not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            logger.info("AppContextMiddleware 调用完成", mode="passthrough")
            return
        async with use_app_context_async(self.context):
            await self.app(scope, receive, send)
        logger.info("AppContextMiddleware 调用完成", mode="bound")
