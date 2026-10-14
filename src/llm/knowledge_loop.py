"""统一有界只读工具循环：知识检索与结构化探查共用同一套协议（设计 spec/51 第 3 节、spec/55 第 7 节）。"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Iterator

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from src.logging_config import get_logger
from src.security.tenant_policy import RequestIdentity
from src.tools.knowledge_search import (
    RESULT_TOTAL_CHARS as TOOL_RESULT_TOTAL_CHARS,
)
from src.tools.knowledge_search import (
    STATUS_INVALID_ARGUMENTS,
    STATUS_UNAVAILABLE,
    build_knowledge_tools,
    use_tool_identity,
)
from src.tools.knowledge_search import (
    TOOL_NAME as KNOWLEDGE_TOOL_NAME,
)

logger = get_logger(__name__)

# 中间工具回合的追踪标记：SSE 层据此推迟或丢弃该回合的增量。
TOOL_ROUND_TAG = "knowledge_tool_round"
# 有界循环上限：3 个工具回合，最多 6 次真实检索，之后最多 1 次禁用工具的最终回答。
MAX_TOOL_ROUNDS = 3
MAX_RETRIEVALS = 6
PER_RETRIEVAL_SECONDS = 3.0
MAX_TOTAL_RETRIEVAL_SECONDS = 8.0
# 预算耗尽时的最终回答约束，避免模型编造未获取的口径。
_BUDGET_NOTICE = (
    "工具调用预算已用尽。请只依据上文已经获得的工具结果或既有上下文作答，"
    "明确说明缺失的口径或字段，禁止编造未获取的枚举、指标定义或业务规则。"
)
_TRIMMED_TOOL_NOTE = json.dumps(
    {"status": "ok", "evidence": [], "citations": [], "truncated": True, "reason": "message_budget"},
    ensure_ascii=False,
)


@dataclass(frozen=True, slots=True)
class KnowledgeRoundReset:
    """标记此前流式内容属于被丢弃的中间工具回合，消费方必须清除已累积正文。"""

    round_index: int
    reason: str = TOOL_ROUND_TAG


@dataclass(slots=True)
class KnowledgeLoopConfig:
    """服务端提供的工具循环配置：身份、数据源、预算与超时。"""

    enabled: bool = True
    identity: RequestIdentity | None = None
    datasource: str = ""
    # None 表示未限定；显式空集合表示本次执行不允许任何具体数据源。
    allowed_datasources: frozenset[str] | None = None
    store_provider: Any | None = None
    per_retrieval_seconds: float = PER_RETRIEVAL_SECONDS
    max_total_retrieval_seconds: float = MAX_TOTAL_RETRIEVAL_SECONDS
    max_rounds: int = MAX_TOOL_ROUNDS
    max_retrievals: int = MAX_RETRIEVALS
    # 显式覆盖消息预算，主要供测试与特殊节点使用；为空时按 Prompt 预算推导。
    message_budget_chars: int | None = None


@dataclass(slots=True)
class _LoopBudget:
    """一次执行的模型消息预算快照。"""

    message_budget_chars: int
    # 工具 schema 的真实序列化字符数，由 _prepare_tool_budget 在首次调用前写入。
    tool_schema_chars: int = 0
    tool_rounds: int = 0
    tool_calls: int = 0
    # 知识工具专属计数：探查调用不得消耗知识检索额度（spec/55 §7）。
    knowledge_calls: int = 0


# 单次工具结果的有界预留：与工具自身的 RESULT_TOTAL_CHARS 同源，保证至少一条证据进得去。
TOOL_RESULT_RESERVE_CHARS = TOOL_RESULT_TOTAL_CHARS
# assistant tool_calls 消息、预算提示与分隔符的协议开销上限。
TOOL_PROTOCOL_RESERVE_CHARS = 800
# 工具 schema 预留字符的进程级缓存：schema 与身份无关，只需序列化一次。
_SCHEMA_RESERVE_CACHE: int | None = None
_config_var: ContextVar[KnowledgeLoopConfig | None] = ContextVar(
    "knowledge_loop_config",
    default=None,
)
_public_answer_stream: ContextVar[bool] = ContextVar("public_answer_stream", default=False)


# 为当前请求启用公开回答的真实流式调用，不影响后台任务和 SQL。
# Args: 无。
# Returns: 自动恢复请求作用域的上下文管理器。
@contextmanager
def use_public_answer_stream() -> Iterator[None]:
    token = _public_answer_stream.set(True)
    try:
        yield
    finally:
        _public_answer_stream.reset(token)


# 方法作用：绑定一次请求或后台任务的知识循环配置。
# Args: config - 服务端配置。
# Returns: 用于恢复的 Token。
def bind_knowledge_loop(config: KnowledgeLoopConfig | None) -> Token:
    return _config_var.set(config)


# 方法作用：恢复到绑定前的知识循环配置。
# Args: token - bind_knowledge_loop 返回的 Token。
# Returns: 无返回值。
def reset_knowledge_loop(token: Token) -> None:
    _config_var.reset(token)


# 方法作用：读取当前生效的知识循环配置，未绑定时返回安全默认值。
# Args: 无。
# Returns: KnowledgeLoopConfig。
def get_knowledge_loop_config() -> KnowledgeLoopConfig:
    bound = _config_var.get()
    return bound if bound is not None else KnowledgeLoopConfig()


# 方法作用：在指定配置下执行一段逻辑并保证恢复。
# Args: config - 本次执行的配置。
# Returns: 上下文管理器。
@contextmanager
def use_knowledge_loop(config: KnowledgeLoopConfig | None) -> Iterator[None]:
    token = bind_knowledge_loop(config)
    try:
        yield
    finally:
        reset_knowledge_loop(token)


# 方法作用：为后台任务绑定可信身份，无可信身份时只禁用检索而不采用平台身份。
# Args: identity - 记录拥有者身份；datasource - 默认数据源；allowed_datasources - 可选白名单。
# Returns: 上下文管理器。
@contextmanager
def use_trusted_identity(
    identity: RequestIdentity | None,
    *,
    datasource: str = "",
    allowed_datasources: frozenset[str] | None = None,
) -> Iterator[None]:
    from src.api.auth import use_identity_context
    from src.tools.knowledge_search import is_trusted_identity

    trusted = identity if is_trusted_identity(identity) else None
    if trusted is None:
        logger.warning("后台知识检索未绑定", reason="identity_not_trusted")
    config = KnowledgeLoopConfig(
        enabled=trusted is not None,
        identity=trusted,
        datasource=str(datasource or "").strip(),
        allowed_datasources=(
            None if allowed_datasources is None
            else frozenset(str(name).strip() for name in allowed_datasources if str(name).strip())
        ),
    )
    token = bind_knowledge_loop(config)
    try:
        if trusted is None:
            yield
        else:
            # 同时绑定真实认证上下文，使向量库 ACL 与工具复核看到同一身份。
            with use_identity_context(trusted):
                yield
    finally:
        reset_knowledge_loop(token)


# 方法作用：为普通请求绑定知识检索范围（数据源与请求白名单），不覆盖身份。
# Args: datasource - 本次请求已授权的默认数据源；allowed_datasources - 请求级数据源白名单。
# Returns: 用于恢复的 Token。
def bind_request_knowledge_scope(
    datasource: str = "",
    allowed_datasources: Iterable[str] | None = None,
) -> Token:
    """身份始终来自真实请求上下文，执行期漂移复核保持有效。"""
    config = KnowledgeLoopConfig(
        enabled=True,
        identity=None,
        datasource=str(datasource or "").strip(),
        # None 表示未限定；显式空集合表示本次请求不允许任何具体数据源。
        allowed_datasources=(
            None if allowed_datasources is None
            else frozenset(str(name).strip() for name in allowed_datasources if str(name).strip())
        ),
    )
    return bind_knowledge_loop(config)


# 方法作用：以上下文管理器方式绑定请求级知识检索范围。
# Args: datasource - 默认数据源；allowed_datasources - 请求级白名单。
# Returns: 上下文管理器。
@contextmanager
def use_request_knowledge_scope(
    datasource: str = "",
    allowed_datasources: Iterable[str] | None = None,
) -> Iterator[None]:
    token = bind_request_knowledge_scope(datasource, allowed_datasources)
    try:
        yield
    finally:
        reset_knowledge_loop(token)


# 方法作用：合并父级配置与调用方显式覆盖的配置。
# Args: base - prepared.config；override - 调用方覆盖。
# Returns: 合并后的 RunnableConfig 字典。
def _merge_config(base: dict | None, override: dict | None) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base or {})
    for key, value in dict(override or {}).items():
        if key in {"metadata", "tags"} and value:
            existing = merged.get(key) or ([] if key == "tags" else {})
            if key == "tags":
                merged[key] = list(dict.fromkeys([*list(existing or []), *list(value or [])]))
            else:
                merged[key] = {**dict(existing or {}), **dict(value or {})}
        else:
            merged[key] = value
    return merged


# 方法作用：为指定回合生成调用配置，中间回合附加可被 SSE 识别的标记。
# Args: base_config - 基础配置；intermediate - 是否为工具交互回合。
# Returns: 该回合使用的配置。
def _round_config(base_config: dict[str, Any], *, intermediate: bool) -> dict[str, Any]:
    if not intermediate:
        return dict(base_config)
    result = dict(base_config)
    result["tags"] = list(dict.fromkeys([*list(result.get("tags", []) or []), TOOL_ROUND_TAG]))
    return result


# 方法作用：判断事件或配置是否属于中间工具回合，供 SSE 过滤复用。
# Args: tags - 事件的 tags 列表。
# Returns: 属于中间工具回合时返回 True。
def is_tool_round_tags(tags: Any) -> bool:
    return TOOL_ROUND_TAG in [str(item) for item in (tags or [])]


# 方法作用：给模型绑定只读知识工具，并保留原有追踪配置。
# Args: model - 可能已被 with_config 包装的模型；tools - 待绑定工具。
# Returns: 可调用的绑定对象；模型明确不支持绑定时返回 None。
def _bind_tools(model: Any, tools: list[Any]) -> Any | None:
    binder = getattr(model, "bind_tools", None)
    if not callable(binder):
        return None
    try:
        bound = binder(tools)
    except (AttributeError, NotImplementedError):
        # 只有“能力缺失”才回退；认证、超时、5xx、解析错误都必须向上暴露。
        return None
    if bound is None:
        return None
    existing = getattr(model, "config", None)
    if isinstance(existing, dict) and existing and hasattr(bound, "with_config"):
        bound = bound.with_config(existing)
    return bound


# 方法作用：从响应或合并后的分片中提取工具调用列表。
# Args: message - AIMessage 或合并后的 AIMessageChunk。
# Returns: 工具调用字典列表。
def _tool_calls_from_message(message: Any) -> list[dict[str, Any]]:
    """损坏参数由 LangChain 放在 invalid_tool_calls，同样必须闭合调用 ID。"""
    if message is None:
        return []
    result: list[dict[str, Any]] = [
        dict(call) for call in (getattr(message, "tool_calls", None) or [])
    ]
    if not result:
        for chunk in getattr(message, "tool_call_chunks", None) or []:
            name = chunk.get("name")
            if not name:
                continue
            try:
                args = json.loads(chunk.get("args") or "{}")
            except (TypeError, json.JSONDecodeError):
                args = {}
            result.append({
                "name": name,
                "args": args if isinstance(args, dict) else {},
                "id": str(chunk.get("id") or ""),
            })
    for invalid in getattr(message, "invalid_tool_calls", None) or []:
        if not isinstance(invalid, dict):
            continue
        result.append({
            "name": str(invalid.get("name") or ""),
            # 参数无法解析：保留原始值让校验必然失败，绝不猜测模型意图。
            "args": invalid.get("args"),
            "id": str(invalid.get("id") or ""),
            "invalid": True,
        })
    return result


# 方法作用：按服务端工具自身的 args_schema 校验模型参数，杜绝自由输入模式。
# Args: tool - 服务端构建的工具；call - 模型的工具调用字典。
# Returns: 校验通过的参数字典；参数损坏、非法或工具缺少可校验契约时返回 None。
def _validated_tool_args(tool: Any, call: dict[str, Any]) -> dict[str, Any] | None:
    """损坏参数与未知字段一律在这里拒绝，调用方据此生成匹配的拒绝 ToolMessage。"""
    if call.get("invalid"):
        # 参数无法解析：必然失败，绝不按猜测值去执行。
        return None
    raw_args = call.get("args")
    if not isinstance(raw_args, dict):
        return None
    schema = getattr(tool, "args_schema", None)
    validator = getattr(schema, "model_validate", None)
    if not callable(validator):
        # 没有可校验 Pydantic 契约的工具不允许执行，避免放开未声明的输入模式。
        return None
    try:
        return validator(raw_args).model_dump()
    except ValidationError:
        return None


# 方法作用：按工具与参数生成匹配 tool_call_id 的 ToolMessage。
# Args: tools_by_name - 工具名到工具的映射；call - 模型的工具调用。
# Returns: 始终存在的 ToolMessage，未知工具与非法参数都不会留下孤立调用。
async def _resolve_tool_message(
    tools_by_name: dict[str, Any],
    call: dict[str, Any],
    identity: RequestIdentity | None = None,
) -> ToolMessage:
    call_id = str(call.get("id") or "")
    name = str(call.get("name") or "")
    tool = tools_by_name.get(name)
    if tool is None:
        logger.warning("工具循环拒绝未知工具", tool=name)
        return ToolMessage(
            content=json.dumps(
                {"status": STATUS_INVALID_ARGUMENTS, "evidence": [], "citations": [],
                 "truncated": False, "reason": "unknown_tool"},
                ensure_ascii=False,
            ),
            tool_call_id=call_id,
            name=name or None,
        )
    args = _validated_tool_args(tool, call)
    if args is None:
        logger.warning("工具循环拒绝非法参数", tool=name)
        return ToolMessage(
            content=json.dumps(
                {"status": STATUS_INVALID_ARGUMENTS, "evidence": [], "citations": [],
                 "truncated": False, "reason": "invalid_arguments"},
                ensure_ascii=False,
            ),
            tool_call_id=call_id,
            name=name,
        )
    try:
        # 显式身份（后台任务）与真实请求身份都走同一套执行期复核。
        with use_tool_identity(identity):
            result = await tool.ainvoke(args)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "工具执行失败",
            tool=name,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return ToolMessage(
            content=json.dumps(
                {"status": STATUS_UNAVAILABLE, "evidence": [], "citations": [],
                 "truncated": False, "reason": "tool_failed"},
                ensure_ascii=False,
            ),
            tool_call_id=call_id,
            name=name,
        )
    payload = result if isinstance(result, dict) else {"status": STATUS_UNAVAILABLE, "reason": "unexpected_result"}
    return ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        tool_call_id=call_id,
        name=name,
    )


# 方法作用：统计消息与工具 schema 的字符规模，作为统一消息预算输入。
# Args: messages - 当前完整消息序列；schema_chars - 工具 schema 预留字符。
# Returns: 估算字符数。
def _message_chars(messages: list[Any], schema_chars: int) -> int:
    total = max(0, int(schema_chars))
    for message in messages:
        total += len(str(getattr(message, "content", "") or ""))
        for call in getattr(message, "tool_calls", None) or []:
            total += len(json.dumps(call.get("args", {}), ensure_ascii=False, default=str))
    return total


# 方法作用：在总消息预算内裁减工具证据，保持系统、核心问题与 Schema 优先级。
# Args: messages - 当前消息序列；budget - 本次执行预算。
# Returns: (新消息序列, 裁减后是否仍超预算)。
def _fit_messages(messages: list[Any], budget: _LoopBudget) -> tuple[list[Any], bool]:
    fitted = list(messages)
    if _message_chars(fitted, budget.tool_schema_chars) <= budget.message_budget_chars:
        return fitted, False
    trimmed = 0
    for index, message in enumerate(fitted):
        if getattr(message, "type", "") != "tool":
            continue
        if _message_chars(fitted, budget.tool_schema_chars) <= budget.message_budget_chars:
            break
        fitted[index] = ToolMessage(
            content=_TRIMMED_TOOL_NOTE,
            tool_call_id=getattr(message, "tool_call_id", "") or "",
            name=getattr(message, "name", None),
        )
        trimmed += 1
    over = _message_chars(fitted, budget.tool_schema_chars) > budget.message_budget_chars
    if trimmed:
        logger.info(
            "知识工具证据按消息预算裁减",
            trimmed_count=trimmed,
            over_budget=over,
            budget_chars=budget.message_budget_chars,
        )
    return fitted, over


# 方法作用：解析本次调用的消息预算，显式配置优先，其次本次调用的动态预算，最后回退注册上限。
# Args: prepared - 已注册 Prompt 的调用快照；config - 当前循环配置。
# Returns: 消息字符预算。
def _resolve_message_budget(prepared: Any, config: KnowledgeLoopConfig) -> int:
    """spec/53 起 Prompt 注册值仅为设计上限，须优先用本次调用的动态预算收敛消息总量。"""
    if config.message_budget_chars is not None:
        return max(200, int(config.message_budget_chars))
    # 调用快照携带的动态预算与 Prompt 层同源；缺失（旧构造方）时回退注册上限保持兼容。
    call_budget = int(getattr(prepared, "budget_chars", 0) or 0)
    if call_budget > 0:
        return call_budget
    from src.llm.prompts import get_prompt_definition

    definition = get_prompt_definition(prepared.prompt_id)
    return int(definition.context_budget)


# 方法作用：判断当前消息加工具 schema 是否仍能放进总预算。
# Args: messages - 当前消息；budget - 本次执行预算。
# Returns: (是否可挂工具, schema 字符数)。
def _prepare_tool_budget(messages: list[Any], tools: list[Any], budget: _LoopBudget) -> tuple[bool, int]:
    """首次调用前就完成预算判定：连一条有用证据都放不下时直接不挂工具。"""
    try:
        schema_chars = _tool_schema_chars(tools)
    except Exception:
        logger.warning("知识工具 schema 无法序列化，跳过工具绑定", exc_info=True)
        return False, 0
    budget.tool_schema_chars = schema_chars
    # 不只判 schema：还要留得下至少一条结果与协议消息，否则证据必被裁空。
    return (
        _message_chars(messages, schema_chars) + TOOL_RESULT_RESERVE_CHARS
        + TOOL_PROTOCOL_RESERVE_CHARS
    ) <= budget.message_budget_chars, schema_chars


# 方法作用：按真实序列化结果统计工具 schema 占用的字符数。
# Args: tools - 已构建的工具列表。
# Returns: name/description/parameters 的完整 JSON 字符数。
def _tool_schema_chars(tools: list[Any]) -> int:
    """不靠固定常量估算，schema 超预算时由调用方决定不挂工具。"""
    total = 0
    for tool in tools:
        schema = getattr(tool, "args_schema", None)
        if isinstance(schema, dict):
            payload: Any = schema
        elif hasattr(schema, "model_json_schema"):
            payload = schema.model_json_schema()
        else:
            raise TypeError("工具缺少可序列化的 args_schema")
        total += len(json.dumps({
            "name": str(getattr(tool, "name", "")),
            "description": str(getattr(tool, "description", "")),
            "parameters": payload,
        }, ensure_ascii=False, default=str))
    return total


# 方法作用：把流式合并分片归一成带 tool_calls 的 AIMessage。
# Args: message - 模型响应或合并后的 AIMessageChunk。
# Returns: 可直接作为下一轮 assistant 消息的 AIMessage。
def _as_assistant_message(message: Any) -> Any:
    """provider 序列化依赖 AIMessage：补回 tool_calls，并把损坏调用归一成可闭合的调用。

    否则随后的 ToolMessage 会成为没有对应 assistant tool_call 的孤立消息而被供应商拒绝。
    """
    from langchain_core.messages import AIMessageChunk

    invalid = list(getattr(message, "invalid_tool_calls", None) or [])
    if not invalid and not isinstance(message, AIMessageChunk):
        return message
    tool_calls = [dict(call) for call in (getattr(message, "tool_calls", None) or [])]
    for item in invalid:
        if not isinstance(item, dict):
            continue
        tool_calls.append({
            "name": str(item.get("name") or ""),
            # 参数损坏时用空对象占位，真正的失败原因由 ToolMessage 回传模型。
            "args": {},
            "id": str(item.get("id") or ""),
        })
    return AIMessage(
        content=getattr(message, "content", "") or "",
        tool_calls=tool_calls,
        # 供应商续轮协议字段（如 reasoning_content）必须原样带回，否则续轮会被拒或丢推理。
        additional_kwargs=dict(getattr(message, "additional_kwargs", None) or {}),
        response_metadata=dict(getattr(message, "response_metadata", None) or {}),
    )


# 方法作用：按真实序列化结果计算知识工具 schema 的预留字符数。
# Args: 无。
# Returns: 知识 schema 字符数；未启用、身份不可信或无法计算时返回 0。
def _knowledge_schema_reserve_chars() -> int:
    """知识工具 schema 与身份无关，进程级只序列化一次并静态缓存。"""
    global _SCHEMA_RESERVE_CACHE
    config = get_knowledge_loop_config()
    if not config.enabled:
        return 0
    identity = config.identity
    if identity is None:
        try:
            from src.api.auth import get_current_identity

            identity = get_current_identity()
        except Exception:
            # 无请求上下文属预期路径（后台任务），保留堆栈便于区分真实故障。
            logger.debug("知识工具身份解析失败，按无预留处理", exc_info=True)
            return 0
    from src.tools.knowledge_search import is_trusted_identity

    if not is_trusted_identity(identity):
        return 0
    if _SCHEMA_RESERVE_CACHE is None:
        try:
            probe = build_knowledge_tools(identity=identity)
            _SCHEMA_RESERVE_CACHE = _tool_schema_chars(probe)
        except Exception:
            logger.warning("知识工具 schema 预留计算失败", exc_info=True)
            _SCHEMA_RESERVE_CACHE = 0
    return int(_SCHEMA_RESERVE_CACHE)


# 探查 schema 预留测量用的最小调用快照：只提供挂载判定所需的任务标识，不含身份与数据。
@dataclass(frozen=True, slots=True)
class _ProbeSchemaSentinel:
    """触发 build_data_probe_tools 的 sql.generate 判定；重复构建不重置探查预算。"""

    prompt_id: str = "sql.generate"
    prompt_version: str = ""
    task: str = "generate_sql"
    model: Any = None
    messages: tuple = ()
    config: dict = field(default_factory=dict)
    budget_chars: int = 0


# 方法作用：探查上下文已绑定时按真实序列化结果测量探查工具 schema 字符数。
# Args: 无。
# Returns: 探查 schema 字符数；未绑定、模块缺失或测量失败时返回 0。
def _probe_schema_reserve_chars() -> int:
    """探查工具带身份作用域，测量结果不进全局缓存，每次调用重新计算。"""
    tools = _build_probe_tools_for_call(_ProbeSchemaSentinel())
    if not tools:
        return 0
    try:
        return _tool_schema_chars(tools)
    except Exception:
        logger.warning("探查工具 schema 预留计算失败", exc_info=True)
        return 0


# 方法作用：按真实序列化结果给出工具交互所需的预留字符数（知识 + 已绑定探查）。
# Args: context_budget - 当前 Prompt 的总字符预算。
# Returns: 需要预留的字符数；两类工具都不可用时返回 0。
def knowledge_tool_reserve_chars(context_budget: int) -> int:
    """预留 = 每个可用工具集的 schema + 各自一条有界结果 + 协议开销，避免证据被裁空。"""
    knowledge_chars = _knowledge_schema_reserve_chars()
    probe_chars = _probe_schema_reserve_chars()
    groups = (1 if knowledge_chars > 0 else 0) + (1 if probe_chars > 0 else 0)
    if not groups:
        return 0
    # 分配上限由 Prompt 预算器统一裁剪；知识禁用不影响探查部分的预留。
    return (
        knowledge_chars
        + probe_chars
        + TOOL_RESULT_RESERVE_CHARS * groups
        + TOOL_PROTOCOL_RESERVE_CHARS
    )


# 方法作用：构造最终回答的消息序列，确保落在总消息预算内。
# Args: messages - 当前消息序列；budget - 本次执行预算。
# Returns: 不超预算的消息序列。
def _final_messages(messages: list[Any], budget: _LoopBudget) -> list[Any]:
    """预算不足时先裁减工具证据，仍超限则移除整段工具交换，绝不上送超大消息。"""
    fitted, over = _fit_messages(messages, budget)
    if not over:
        return fitted
    kept = [
        message for message in fitted
        if getattr(message, "type", "") != "tool" and not getattr(message, "tool_calls", None)
    ]
    logger.warning(
        "知识工具交换按预算移除",
        removed_messages=len(fitted) - len(kept),
        budget_chars=budget.message_budget_chars,
    )
    return kept


# 方法作用：构建本次调用的结构化探查工具，挂载判定完全由探查模块按上下文与任务裁决。
# Args: prepared - 已注册 Prompt 的调用快照。
# Returns: StructuredTool 列表；模块未部署、无已授权上下文或非 sql.generate 时为空列表。
def _build_probe_tools_for_call(prepared: Any) -> list[Any]:
    try:
        from src.tools.data_probe import build_data_probe_tools
    except ImportError as exc:
        # 探查模块未部署属于阶段性状态：知识工具不受影响，探查静默缺席。
        logger.debug("探查工具模块不可用，跳过探查挂载", error_type=type(exc).__name__)
        return []
    try:
        return list(build_data_probe_tools(prepared) or [])
    except Exception as exc:
        # 构建失败只降级探查能力，不阻塞本次生成；异常边界只记一次脱敏堆栈。
        logger.error("探查工具构建失败", error_type=type(exc).__name__, exc_info=True)
        return []


# 方法作用：按服务端配置构建本次执行的只读工具集（知识检索 + 结构化探查）。
# Args: prepared - 调用快照；探查工具据此判定任务与上下文，知识工具不读取它。
# Returns: StructuredTool 列表；知识禁用不阻止探查，探查未绑定时只返回知识工具。
def build_knowledge_tools_for_call(prepared: Any = None) -> list[Any]:
    tools: list[Any] = []
    config = get_knowledge_loop_config()
    if config.enabled:
        identity = config.identity
        if identity is None:
            from src.api.auth import get_current_identity

            identity = get_current_identity()
        tools.extend(build_knowledge_tools(
            identity=identity,
            default_datasource=config.datasource,
            allowed_datasources=config.allowed_datasources,
            store_provider=config.store_provider,
            per_retrieval_seconds=config.per_retrieval_seconds,
            max_retrievals=config.max_retrievals,
            max_total_seconds=config.max_total_retrieval_seconds,
            # 每次执行独立缓存，不跨请求也不跨并行节点共享。
            cache={},
        ))
    # 知识检索关闭或身份不可信都只影响知识工具，已绑定的探查能力独立生效。
    tools.extend(_build_probe_tools_for_call(prepared))
    return tools


# 方法作用：非流式统一调用入口，允许模型按需调用只读知识工具。
# Args: prepared - 已注册 Prompt 的调用快照；messages - 可选显式消息；config - 可选配置覆盖。
# Returns: 最终的模型响应。
async def invoke_with_knowledge(
    prepared: Any,
    messages: list[Any] | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> Any:
    """无 tool_call 时只有原有一次模型调用，也不触发任何检索。"""
    if _public_answer_stream.get() and getattr(prepared, "task", "") in {"analyze_result", "direct_answer"}:
        return await _invoke_public_stream(prepared, messages, config=config)
    call_messages = list(messages if messages is not None else prepared.messages)
    model_config = _merge_config(prepared.config, config)
    tools = build_knowledge_tools_for_call(prepared)
    loop_config = get_knowledge_loop_config()
    if not tools:
        return await prepared.model.ainvoke(call_messages, config=model_config)

    budget = _LoopBudget(
        message_budget_chars=_resolve_message_budget(prepared, loop_config),
    )
    # 首次调用前先判定 schema 与消息预算，放不下就不挂工具。
    allowed, _schema_chars = _prepare_tool_budget(call_messages, tools, budget)
    if not allowed:
        logger.info(
            "知识工具循环跳过",
            reason="tool_schema_over_budget",
            prompt_id=getattr(prepared, "prompt_id", ""),
        )
        return await prepared.model.ainvoke(call_messages, config=model_config)
    bound = _bind_tools(prepared.model, tools)
    if bound is None:
        logger.info(
            "知识工具循环回退",
            reason="bind_tools_unsupported",
            prompt_id=getattr(prepared, "prompt_id", ""),
        )
        return await prepared.model.ainvoke(call_messages, config=model_config)

    tools_by_name = {tool.name: tool for tool in tools}
    for _round_index in range(max(1, int(loop_config.max_rounds))):
        response = await bound.ainvoke(call_messages, config=model_config)
        tool_calls = _tool_calls_from_message(response)
        if not tool_calls:
            logger.debug(
                "知识工具循环完成",
                prompt_id=getattr(prepared, "prompt_id", ""),
                tool_rounds=budget.tool_rounds,
                tool_calls=budget.tool_calls,
                stop_reason="answered",
            )
            return response
        budget.tool_rounds += 1
        budget.tool_calls += len(tool_calls)
        call_messages.append(_as_assistant_message(response))
        for call in tool_calls:
            call_messages.append(await _resolve_tool_message(tools_by_name, call, loop_config.identity))
        call_messages, over_budget = _fit_messages(call_messages, budget)
        if over_budget:
            logger.warning("知识工具循环消息预算不足，提前收敛", tool_rounds=budget.tool_rounds)
            break
    logger.info(
        "知识工具循环达到上限",
        prompt_id=getattr(prepared, "prompt_id", ""),
        tool_rounds=budget.tool_rounds,
        tool_calls=budget.tool_calls,
        stop_reason="round_limit",
    )
    budget.tool_schema_chars = 0
    final_messages = _final_messages(
        [*call_messages, HumanMessage(content=_BUDGET_NOTICE)], budget,
    )
    return await prepared.model.ainvoke(final_messages, config=model_config)


# 先完成有界只读工具探索，再以无工具模型逐块生成公开回答。
# Args: prepared - 调用快照；messages - 可选消息；config - 调用覆盖。
# Returns: 合并后的最终模型响应；原始分片由 LangGraph 回调即时推送。
async def _invoke_public_stream(prepared: Any, messages: list[Any] | None = None, *, config: dict | None = None) -> Any:
    call_messages = list(messages if messages is not None else prepared.messages)
    model_config = _merge_config(prepared.config, config)
    tools = build_knowledge_tools_for_call(prepared)
    loop_config = get_knowledge_loop_config()
    if tools:
        budget = _LoopBudget(message_budget_chars=_resolve_message_budget(prepared, loop_config))
        allowed, _ = _prepare_tool_budget(call_messages, tools, budget)
        bound = _bind_tools(prepared.model, tools) if allowed else None
        if bound is not None:
            tools_by_name = {tool.name: tool for tool in tools}
            for _ in range(max(1, int(loop_config.max_rounds))):
                response = await bound.ainvoke(call_messages, config=_round_config(model_config, intermediate=True))
                calls = _tool_calls_from_message(response)
                if not calls:
                    break
                budget.tool_rounds += 1
                budget.tool_calls += len(calls)
                # 只有知识工具调用消耗 max_retrievals；探查调用沿用自身独立预算。
                budget.knowledge_calls += sum(
                    1 for call in calls
                    if str(call.get("name") or "") == KNOWLEDGE_TOOL_NAME
                )
                call_messages.append(_as_assistant_message(response))
                for call in calls:
                    call_messages.append(await _resolve_tool_message(tools_by_name, call, loop_config.identity))
                call_messages, over_budget = _fit_messages(call_messages, budget)
                if over_budget or budget.knowledge_calls >= max(1, int(loop_config.max_retrievals)):
                    break
            call_messages.append(HumanMessage(content="知识工具探索已结束。请依据已获得证据输出最终回答，继续严格遵循原输出契约；证据缺失应说明限制，不得编造。"))
    # 无工具的最终调用不可能再转为工具交互，增量可以安全即时公开。
    final_config = _merge_config(model_config, {"metadata": {"public_answer": True}})
    merged = None
    async for chunk in prepared.model.astream(call_messages, config=final_config):
        merged = chunk if merged is None else merged + chunk
    if merged is None:
        raise ValueError("公开回答流没有返回内容")
    return merged


# 方法作用：流式统一调用入口，中间工具回合的正文不作为最终答案增量。
# Args: prepared - 已注册 Prompt 的调用快照；messages - 可选显式消息；config - 可选配置覆盖。
# Returns: 异步分片生成器；中间工具回合前会产出 KnowledgeRoundReset。
async def stream_with_knowledge(
    prepared: Any,
    messages: list[Any] | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> AsyncIterator[Any]:
    """消费方收到 KnowledgeRoundReset 时必须丢弃本轮已累积的正文。"""
    call_messages = list(messages if messages is not None else prepared.messages)
    model_config = _merge_config(prepared.config, config)
    tools = build_knowledge_tools_for_call(prepared)
    loop_config = get_knowledge_loop_config()
    if not tools:
        async for chunk in prepared.model.astream(call_messages, config=model_config):
            yield chunk
        return

    budget = _LoopBudget(
        message_budget_chars=_resolve_message_budget(prepared, loop_config),
    )
    allowed, _schema_chars = _prepare_tool_budget(call_messages, tools, budget)
    if not allowed:
        logger.info(
            "知识工具循环跳过",
            reason="tool_schema_over_budget",
            prompt_id=getattr(prepared, "prompt_id", ""),
        )
        async for chunk in prepared.model.astream(call_messages, config=model_config):
            yield chunk
        return
    bound = _bind_tools(prepared.model, tools)
    if bound is None:
        logger.info(
            "知识工具循环回退",
            reason="bind_tools_unsupported",
            prompt_id=getattr(prepared, "prompt_id", ""),
        )
        async for chunk in prepared.model.astream(call_messages, config=model_config):
            yield chunk
        return

    tools_by_name = {tool.name: tool for tool in tools}
    max_rounds = max(1, int(loop_config.max_rounds))
    for round_index in range(max_rounds):
        # 挂了工具的回合一律带标记：只有辨别出完整回合后才能确定是否最终正文。
        round_config = _round_config(model_config, intermediate=True)
        merged: Any = None
        buffered: list[Any] = []
        async for chunk in bound.astream(call_messages, config=round_config):
            merged = chunk if merged is None else merged + chunk
            buffered.append(chunk)
        tool_calls = _tool_calls_from_message(merged)
        if not tool_calls:
            # 没有工具调用：本回合就是最终答案，此时才释放缓冲分片。
            logger.debug(
                "知识工具循环流式完成",
                prompt_id=getattr(prepared, "prompt_id", ""),
                tool_rounds=budget.tool_rounds,
                tool_calls=budget.tool_calls,
                stop_reason="answered",
            )
            for chunk in buffered:
                yield chunk
            return
        budget.tool_rounds += 1
        budget.tool_calls += len(tool_calls)
        # 工具回合正文一律不产出：消费方与 SSE 都看不到中间说明。
        yield KnowledgeRoundReset(round_index=round_index)
        call_messages.append(_as_assistant_message(merged))
        for call in tool_calls:
            call_messages.append(await _resolve_tool_message(tools_by_name, call, loop_config.identity))
        call_messages, over_budget = _fit_messages(call_messages, budget)
        if over_budget:
            logger.warning("知识工具循环消息预算不足，提前收敛", tool_rounds=budget.tool_rounds)
            break
    logger.info(
        "知识工具循环达到上限",
        prompt_id=getattr(prepared, "prompt_id", ""),
        tool_rounds=budget.tool_rounds,
        tool_calls=budget.tool_calls,
        stop_reason="round_limit",
    )
    yield KnowledgeRoundReset(round_index=max_rounds, reason="round_limit")
    # 最终回答禁用工具，逐块实时输出，保证 final/done 语义不变。
    budget.tool_schema_chars = 0
    final_messages = _final_messages(
        [*call_messages, HumanMessage(content=_BUDGET_NOTICE)], budget,
    )
    async for chunk in prepared.model.astream(final_messages, config=model_config):
        yield chunk
