"""人工确认、幂等和审计约束下的外部动作契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ActionRequest:
    """一次外部动作请求；默认不允许执行。"""

    action_name: str
    payload: dict[str, Any]
    idempotency_key: str
    confirmed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionResult:
    """外部动作结果及审计摘要。"""

    status: str
    action_name: str
    idempotency_key: str = ""
    result: Any = None
    message: str = ""
    executed_at: datetime | None = None


class ExternalAction(ABC):
    """外部动作实现；交易动作不属于本接口的默认能力。"""

    name: str

    # 方法作用：执行已通过注册、确认和幂等检查的外部动作。
    # Args: self - 动作实现；request - 已授权动作请求。
    # Returns: 动作执行结果。
    @abstractmethod
    async def execute(self, request: ActionRequest) -> Any:
        logger.debug("外部动作执行入口", action=getattr(self, "name", "unknown"))
        raise NotImplementedError


class ExternalActionRegistry:
    """动作注册表，默认只执行人工确认且带幂等键的动作。"""

    _TRADING_ACTION_NAMES = frozenset({"buy", "sell", "trade", "order", "place_order", "cancel_order"})

    # 方法作用：初始化动作注册表、幂等记录和审计日志。
    # Args: self - 动作注册表。
    # Returns: 无返回值。
    def __init__(
        self,
        max_idempotency_keys: int = 10000,
        audit_max_entries: int = 10000,
    ) -> None:
        """初始化有界动作注册表。

        Args:
            max_idempotency_keys: 最多保留的成功幂等键数量。
            audit_max_entries: 最多保留的内存审计记录数量。

        Returns:
            无返回值。
        """
        logger.debug(
            "初始化外部动作注册表入口",
            max_idempotency_keys=max_idempotency_keys,
            audit_max_entries=audit_max_entries,
        )
        if max_idempotency_keys <= 0 or audit_max_entries <= 0:
            logger.error("初始化外部动作注册表失败", error="容量必须大于 0")
            raise ValueError("外部动作记录容量必须大于 0")
        self._actions: dict[str, ExternalAction] = {}
        self._executed: dict[str, ActionResult] = {}
        self._audit: deque[dict[str, Any]] = deque(maxlen=audit_max_entries)
        self._max_idempotency_keys = max_idempotency_keys
        logger.info(
            "初始化外部动作注册表完成",
            max_idempotency_keys=max_idempotency_keys,
            audit_max_entries=audit_max_entries,
        )

    # 方法作用：注册一个允许被人工确认后调用的动作。
    # Args: self - 动作注册表；action - 动作实现。
    # Returns: 无返回值。
    def register(self, action: ExternalAction) -> None:
        logger.debug("注册外部动作入口", action_type=type(action).__name__)
        name = str(getattr(action, "name", "")).strip().lower()
        if not name:
            raise ValueError("外部动作必须提供非空 name")
        if not callable(getattr(action, "execute", None)):
            raise TypeError("外部动作必须实现 execute")
        self._actions[name] = action
        logger.info("注册外部动作完成", action=name, total=len(self._actions))

    # 方法作用：执行动作前校验确认、幂等键和注册状态，并记录审计。
    # Args: self - 动作注册表；request - 外部动作请求。
    # Returns: 包含状态的 ActionResult。
    async def dispatch(self, request: ActionRequest) -> ActionResult:
        logger.debug("分发外部动作入口", action=request.action_name, key=request.idempotency_key, confirmed=request.confirmed)
        name = request.action_name.strip().lower()
        key = request.idempotency_key.strip()
        if not request.confirmed:
            result = ActionResult("confirmation_required", name, key, message="需要人工确认后执行")
            self._record_audit(request, result)
            logger.info("外部动作等待确认", action=name)
            return result
        if not key:
            result = ActionResult("rejected", name, message="缺少幂等键")
            self._record_audit(request, result)
            logger.warning("外部动作被拒绝", action=name, reason="missing_idempotency_key")
            return result
        if key in self._executed:
            previous = self._executed[key]
            result = ActionResult("already_executed", name, key, result=previous.result, message="幂等键已执行")
            self._record_audit(request, result)
            logger.info("外部动作幂等返回", action=name, key=key)
            return result
        if name in self._TRADING_ACTION_NAMES:
            result = ActionResult("rejected", name, key, message="自动交易动作未启用")
            self._record_audit(request, result)
            logger.warning("外部动作被拒绝", action=name, reason="automated_trading_disabled")
            return result
        action = self._actions.get(name)
        if action is None:
            result = ActionResult("rejected", name, key, message="动作未注册")
            self._record_audit(request, result)
            logger.warning("外部动作被拒绝", action=name, reason="unregistered")
            return result
        if len(self._executed) >= self._max_idempotency_keys:
            result = ActionResult("rejected", name, key, message="幂等记录容量已满")
            self._record_audit(request, result)
            logger.error(
                "外部动作被拒绝",
                action=name,
                reason="idempotency_capacity_full",
                max_idempotency_keys=self._max_idempotency_keys,
            )
            return result
        try:
            output = await action.execute(request)
            result = ActionResult("executed", name, key, result=output, executed_at=datetime.now(timezone.utc))
            self._executed[key] = result
            self._record_audit(request, result)
            logger.info("外部动作执行完成", action=name, key=key)
            return result
        except Exception as exc:
            logger.error("外部动作执行失败", action=name, key=key, error=str(exc), exc_info=True)
            result = ActionResult("failed", name, key, message=str(exc))
            self._record_audit(request, result)
            return result

    # 方法作用：返回动作执行审计记录的只读副本。
    # Args: self - 动作注册表。
    # Returns: 审计记录列表。
    def audit_log(self) -> list[dict[str, Any]]:
        logger.debug("读取外部动作审计入口", count=len(self._audit))
        result = [dict(item) for item in self._audit]
        logger.info("读取外部动作审计完成", count=len(result))
        return result

    # 方法作用：记录请求结果，便于审计拒绝、等待确认和执行状态。
    # Args: self - 动作注册表；request - 原始请求；result - 处理结果。
    # Returns: 无返回值。
    def _record_audit(self, request: ActionRequest, result: ActionResult) -> None:
        logger.debug("写入外部动作审计入口", action=request.action_name, status=result.status)
        self._audit.append({"action_name": request.action_name, "idempotency_key": request.idempotency_key, "status": result.status, "timestamp": datetime.now(timezone.utc).isoformat()})
        logger.info("写入外部动作审计完成", action=request.action_name, status=result.status)
