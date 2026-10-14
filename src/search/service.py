"""受控联网搜索服务：确定性选择已授权的已加载 MCP 工具执行一次有界搜索（spec/58）。

执行顺序（不可调换）：
    总开关与明确意图 → 输入长度校验 → 真实认证身份 → 已加载工具枚举 →
    精确白名单与参数 Schema → 再次核对真实身份 → 一次有超时的工具调用 →
    有界解析与引用清洗。

边界约定：
    - 出站文本只能是用户当前原始问题（trim 后）与服务端限额，模型无法改写；
    - 不调用 ensure_scoped_servers，不为搜索请求建立新的 MCP 连接；
    - 单次失败不改用其他工具，取消原样传播；
    - 日志只含身份、工具名、字符数、耗时、条目数与固定错误码。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
from src.config import get_settings
from src.logging_config import get_logger
from src.mcp_client.client_manager import get_mcp_client_manager
from src.search.models import (
    ERROR_CALL_FAILED,
    ERROR_DISABLED,
    ERROR_INVALID_QUERY,
    ERROR_INVALID_RESPONSE,
    ERROR_NO_EXPLICIT_INTENT,
    ERROR_NO_TOOL,
    ERROR_NO_USABLE_SOURCES,
    ERROR_RESPONSE_TOO_LARGE,
    ERROR_TIMEOUT,
    ERROR_UNAUTHORIZED,
    SearchHit,
    SearchResponse,
    is_explicit_web_search,
    is_safe_public_url,
)

logger = get_logger(__name__)

# 允许出现在搜索工具 Schema 中的可选限额字段（有且至多其一）。
_ALLOWED_LIMIT_FIELDS = ("max_results", "limit")


# 方法作用：解析精确工具名白名单，跳过空白项与通配符项。
# Args: settings - 应用配置。
# Returns: 去重保序的精确完整工具名列表；全部无效时返回空列表。
def _allowed_tool_names(settings: Any) -> list[str]:
    names: list[str] = []
    for raw in str(getattr(settings, "web_search_mcp_tools", "") or "").split(","):
        name = raw.strip()
        if not name or "*" in name or "?" in name:
            continue
        if name not in names:
            names.append(name)
    return names


# 方法作用：从 Pydantic 参数模型判定兼容性，返回可选限额字段名。
# Args: schema - 工具 args_schema 对应的 Pydantic 模型类。
# Returns: "max_results"/"limit"/""（无限额字段）；不兼容时返回 None。
def _limit_field_from_model(schema: type[BaseModel]) -> str | None:
    fields = schema.model_fields
    query_field = fields.get("query")
    if (
        query_field is None
        or query_field.annotation is not str
        or not query_field.is_required()
    ):
        return None
    extras = set(fields) - {"query"}
    if not extras:
        return ""
    if len(extras) != 1:
        return None
    limit_field = next(iter(extras))
    if limit_field not in _ALLOWED_LIMIT_FIELDS:
        return None
    limit = fields[limit_field]
    if limit.is_required() or limit.annotation not in (int, int | None):
        return None
    return limit_field


# 方法作用：从等价 JSON Schema 字典判定兼容性，返回可选限额字段名。
# Args: schema - 工具 args_schema 对应的 JSON Schema 字典。
# Returns: "max_results"/"limit"/""（无限额字段）；不兼容时返回 None。
def _limit_field_from_json_schema(schema: dict) -> str | None:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    required = schema.get("required")
    required = required if isinstance(required, list) else []
    query_schema = properties.get("query")
    if not isinstance(query_schema, dict) or query_schema.get("type") != "string":
        return None
    if "query" not in required:
        return None
    extras = set(properties) - {"query"}
    if not extras:
        return ""
    if len(extras) != 1:
        return None
    limit_field = next(iter(extras))
    if limit_field not in _ALLOWED_LIMIT_FIELDS:
        return None
    limit_schema = properties[limit_field]
    if not isinstance(limit_schema, dict) or limit_schema.get("type") != "integer":
        return None
    if limit_field in required:
        return None
    return limit_field


# 方法作用：检查工具参数 Schema 是否只含必需 query:string 与可选限额字段。
# Args: tool - 已加载的 LangChain 工具。
# Returns: 限额字段名（"" 表示无）；Schema 缺失、歧义或不兼容时返回 None。
def _limit_field_of(tool: BaseTool) -> str | None:
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        return None
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return _limit_field_from_model(schema)
    if isinstance(schema, dict):
        return _limit_field_from_json_schema(schema)
    return None


# 方法作用：按配置顺序选择第一个兼容且当前可见的工具并组装服务端参数。
# Args: tools - 当前身份可见的已加载工具；allowed_names - 精确白名单；max_results - 服务端条数限额；query_text - trim 后的原始问法。
# Returns: (tool, arguments) 二元组；无兼容工具时返回 None。
def _select_tool(
    tools: list[BaseTool],
    allowed_names: list[str],
    max_results: int,
    query_text: str,
) -> tuple[BaseTool, dict] | None:
    by_name: dict[str, BaseTool] = {}
    for tool in tools:
        name = str(getattr(tool, "name", "") or "")
        if name and name not in by_name:
            by_name[name] = tool
    for name in allowed_names:
        tool = by_name.get(name)
        if tool is None:
            continue
        limit_field = _limit_field_of(tool)
        if limit_field is None:
            continue
        arguments: dict[str, Any] = {"query": query_text}
        if limit_field:
            arguments[limit_field] = max_results
        return tool, arguments
    return None


# 方法作用：读取当前请求的真实认证身份快照。
# Args: 无。
# Returns: (user_id, tenant_id, 归一化 role) 三元组。
def _read_identity() -> tuple[int, int, str]:
    return (
        int(get_current_user_id() or 0),
        int(get_current_tenant_id() or 0),
        str(get_current_role() or "").strip().lower(),
    )


# 方法作用：判定身份快照是否为匿名，匿名不构成任何外送授权。
# Args: identity - _read_identity 返回的三元组。
# Returns: user_id<=0 或 role=anonymous 时返回 True。
def _is_anonymous(identity: tuple[int, int, str]) -> bool:
    return identity[0] <= 0 or identity[2] == "anonymous"


# 方法作用：构造只含固定错误码的脱敏失败响应。
# Args: error_code - models 中的固定错误码常量。
# Returns: success=False 且无条目的 SearchResponse。
def _failure(error_code: str) -> SearchResponse:
    return SearchResponse(success=False, hits=[], error_code=error_code, truncated=False)


# 方法作用：从条目字典提取引用 URL（url 或 locator.url/locator 字符串）。
# Args: item - 候选条目字典。
# Returns: trim 后的 URL 字符串；缺失时返回空字符串。
def _extract_url(item: dict) -> str:
    url = item.get("url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    locator = item.get("locator")
    if isinstance(locator, str) and locator.strip():
        return locator.strip()
    if isinstance(locator, dict):
        nested = locator.get("url")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


# 方法作用：把单个候选条目转为可引用 SearchHit，非法 URL 直接丢弃。
# Args: item - 候选条目（任意类型）；provider - 实际执行搜索的工具完整名。
# Returns: 合法 SearchHit；条目不合法或 URL 未通过清洗时返回 None。
def _build_hit(item: Any, provider: str) -> SearchHit | None:
    if not isinstance(item, dict):
        return None
    url = _extract_url(item)
    if not is_safe_public_url(url):
        return None
    title = item.get("title")
    snippet = item.get("snippet")
    if not isinstance(snippet, str):
        content = item.get("content")
        snippet = content if isinstance(content, str) else ""
    return SearchHit(
        url=url,
        title=title if isinstance(title, str) else "",
        snippet=snippet,
        provider=provider,
    )


# 方法作用：从有界载荷中提取候选条目列表，兼容 results/items 与单条 locator 形态。
# Args: payload - 已解析的 JSON 载荷（字典或列表）。
# Returns: 候选条目列表；结构无法识别时返回 None。
def _extract_items(payload: Any) -> list | None:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in ("results", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    if _extract_url(payload):
        return [payload]
    return None


# 方法作用：用有界分块序列化计数，判定字典/列表载荷是否超出统一字符预算。
# Args: payload - 工具返回的等价 JSON 载荷；max_chars - 统一响应字符上限。
# Returns: 未超限返回 True；累计一超限立即返回 False；不可序列化或循环结构按原异常抛出。
def _payload_within_budget(payload: Any, max_chars: int) -> bool:
    """分块计数、尽早截断：不先复制整段序列化文本，也不为计数触碰预算之外的内容。"""
    total = 0
    for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(payload):
        total += len(chunk)
        if total > max_chars:
            return False
    return True


# 方法作用：对工具原始响应做有界解析与引用清洗，产出脱敏响应。
# Args: raw - 工具返回值（JSON 文本或等价字典/列表）；settings - 应用配置；provider - 工具完整名。
# Returns: SearchResponse（固定错误码或合法引用条目）。
def _parse_bounded_response(raw: Any, settings: Any, provider: str) -> SearchResponse:
    if isinstance(raw, str):
        if len(raw) > settings.web_search_max_response_chars:
            return _failure(ERROR_RESPONSE_TOO_LARGE)
        text = raw.strip()
        if not text:
            return _failure(ERROR_INVALID_RESPONSE)
        try:
            payload: Any = json.loads(text)
        except (TypeError, ValueError):
            # 纯文本不构成来源，返回固定失败码而不是尝试猜引用。
            return _failure(ERROR_NO_USABLE_SOURCES)
    elif isinstance(raw, (dict, list)):
        # 等价 JSON 载荷与原始文本共用同一字符预算：先有界计数，再进入结构识别与条目遍历。
        try:
            within_budget = _payload_within_budget(raw, int(settings.web_search_max_response_chars))
        except Exception as exc:
            # 异常边界：保留排障堆栈，但异常正文可能间接携带响应内容，用脱敏异常承载原始堆栈。
            safe_error = RuntimeError("联网搜索响应结构无法纳入有界计数，细节已脱敏")
            logger.error(
                "联网搜索响应结构无效",
                error_type=type(exc).__name__,
                exc_info=(type(safe_error), safe_error, exc.__traceback__),
            )
            return _failure(ERROR_INVALID_RESPONSE)
        if not within_budget:
            return _failure(ERROR_RESPONSE_TOO_LARGE)
        payload = raw
    else:
        return _failure(ERROR_INVALID_RESPONSE)
    items = _extract_items(payload)
    if items is None:
        return _failure(ERROR_INVALID_RESPONSE)
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for item in items:
        hit = _build_hit(item, provider)
        if hit is None or hit.url in seen:
            continue
        seen.add(hit.url)
        hits.append(hit)
    if not hits:
        if not items:
            # 空结果是正常结果，由研究层说明未检索到证据。
            return SearchResponse(success=True, hits=[], error_code="", truncated=False)
        return _failure(ERROR_NO_USABLE_SOURCES)
    limit = int(settings.web_search_max_results)
    truncated = len(hits) > limit
    return SearchResponse(success=True, hits=hits[:limit], error_code="", truncated=truncated)


# 方法作用：执行一次受控联网搜索，不调用任何模型。
# Args: query - 用户当前原始问题（唯一允许出站的自然语言文本）。
# Returns: SearchResponse；失败时只有固定错误码与空条目，取消原样传播。
async def search_public_web(query: str) -> SearchResponse:
    """确定性调用一个已加载且已授权的搜索 MCP 工具，单次调用、有界解析。"""
    settings = get_settings()
    query_text = str(query or "").strip()
    # 第一道：总开关与明确联网意图，关闭或模糊问法时零工具、零连接。
    if not bool(getattr(settings, "web_search_enabled", False)):
        logger.info("联网搜索已关闭", error_code=ERROR_DISABLED)
        return _failure(ERROR_DISABLED)
    if not is_explicit_web_search(query_text):
        logger.info(
            "联网搜索缺少明确意图",
            error_code=ERROR_NO_EXPLICIT_INTENT,
            query_chars=len(query_text),
        )
        return _failure(ERROR_NO_EXPLICIT_INTENT)
    # 第二道：输入长度校验，超限拒绝而不是截断用户意图。
    if not query_text or len(query_text) > int(settings.web_search_max_query_chars):
        logger.info(
            "联网搜索问题长度无效",
            error_code=ERROR_INVALID_QUERY,
            query_chars=len(query_text),
        )
        return _failure(ERROR_INVALID_QUERY)
    # 第三道：真实认证身份，匿名（含角色为 anonymous）一律拒绝。
    identity = _read_identity()
    logger.debug(
        "联网搜索身份读取完成",
        user_id=identity[0],
        tenant_id=identity[1],
        query_chars=len(query_text),
    )
    if _is_anonymous(identity):
        logger.info(
            "联网搜索拒绝匿名身份",
            error_code=ERROR_UNAUTHORIZED,
            user_id=identity[0],
            tenant_id=identity[1],
        )
        return _failure(ERROR_UNAUTHORIZED)
    # 第四道：精确白名单，为空则不为搜索请求建立任何 MCP 连接。
    allowed_names = _allowed_tool_names(settings)
    if not allowed_names:
        logger.info(
            "联网搜索白名单为空",
            error_code=ERROR_NO_TOOL,
            user_id=identity[0],
            tenant_id=identity[1],
        )
        return _failure(ERROR_NO_TOOL)
    # 只读取当前身份已加载的工具，不调用 ensure_scoped_servers 建立新连接。
    manager = get_mcp_client_manager(settings.mcp_config_path)
    visible_tools = manager.get_all_tools(tenant_id=identity[1], user_id=identity[0])
    selection = _select_tool(
        visible_tools,
        allowed_names,
        int(settings.web_search_max_results),
        query_text,
    )
    if selection is None:
        logger.info(
            "联网搜索无兼容工具",
            error_code=ERROR_NO_TOOL,
            user_id=identity[0],
            tenant_id=identity[1],
        )
        return _failure(ERROR_NO_TOOL)
    tool, arguments = selection
    # 第五道：出站前再次核对身份，防止枚举与调用之间发生漂移。
    if _read_identity() != identity:
        logger.info(
            "联网搜索身份漂移拒绝",
            error_code=ERROR_UNAUTHORIZED,
            user_id=identity[0],
            tenant_id=identity[1],
        )
        return _failure(ERROR_UNAUTHORIZED)
    started = time.monotonic()
    try:
        async with asyncio.timeout(float(settings.web_search_timeout_seconds)):
            raw = await tool.ainvoke(arguments)
    except asyncio.CancelledError:
        # 取消必须原样传播，不转换为失败响应。
        raise
    except TimeoutError:
        logger.info(
            "联网搜索超时",
            error_code=ERROR_TIMEOUT,
            tool=str(getattr(tool, "name", "") or ""),
            user_id=identity[0],
            tenant_id=identity[1],
            query_chars=len(query_text),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        return _failure(ERROR_TIMEOUT)
    except Exception as exc:
        # 异常边界：保留排障堆栈，但底层异常正文可能含凭证或响应内容，不写入日志。
        safe_error = RuntimeError("联网搜索工具调用失败，细节已脱敏")
        logger.error(
            "联网搜索工具调用异常",
            tool=str(getattr(tool, "name", "") or ""),
            error_type=type(exc).__name__,
            user_id=identity[0],
            tenant_id=identity[1],
            exc_info=(type(safe_error), safe_error, exc.__traceback__),
        )
        return _failure(ERROR_CALL_FAILED)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    response = _parse_bounded_response(raw, settings, provider=str(getattr(tool, "name", "") or ""))
    if response.success:
        # 高频成功路径只记 debug，避免正常密集调用产生日志噪声。
        logger.debug(
            "联网搜索完成",
            tool=str(getattr(tool, "name", "") or ""),
            user_id=identity[0],
            tenant_id=identity[1],
            query_chars=len(query_text),
            hit_count=len(response.hits),
            truncated=response.truncated,
            elapsed_ms=elapsed_ms,
        )
    else:
        logger.info(
            "联网搜索未产出证据",
            error_code=response.error_code,
            tool=str(getattr(tool, "name", "") or ""),
            user_id=identity[0],
            tenant_id=identity[1],
            query_chars=len(query_text),
            hit_count=len(response.hits),
            elapsed_ms=elapsed_ms,
        )
    return response
