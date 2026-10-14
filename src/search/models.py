"""受控联网搜索数据模型与引用边界守卫（spec/58-controlled-web-search.md）。

本模块只依赖标准库与 Pydantic：引用 URL 仅做静态合法性判定，不抓取、
不联网解析、不发起 DNS 查询，避免引入任何新的网络出站面。
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 引用字段上限：标题 300 字符、摘要 2000 字符（spec/58 §3）。
MAX_TITLE_CHARS = 300
MAX_SNIPPET_CHARS = 2_000
# source_id 摘要长度：截取 sha256 前 16 位十六进制，足够稳定去重且不暴露完整 URL。
SOURCE_ID_CHARS = 16

# 固定错误码集合：全部脱敏，禁止携带正文、SQL 或底层异常原文（spec/58 §3）。
ERROR_DISABLED = "disabled"
ERROR_NO_EXPLICIT_INTENT = "no_explicit_intent"
ERROR_INVALID_QUERY = "invalid_query"
ERROR_UNAUTHORIZED = "unauthorized"
ERROR_NO_TOOL = "no_tool"
ERROR_TIMEOUT = "timeout"
ERROR_CALL_FAILED = "call_failed"
ERROR_RESPONSE_TOO_LARGE = "response_too_large"
ERROR_INVALID_RESPONSE = "invalid_response"
ERROR_NO_USABLE_SOURCES = "no_usable_sources"

# 请求位置的有限执行句式（spec/58 §3）：只在问法开头识别可下达的联网搜索动作，
# 可含有限礼貌前缀；句中任意位置出现关键词、未识别或仅提及的表述一律失败关闭。

# 有限礼貌前缀：中文请求敬语与英文请求框架，必须与执行核心直接相邻才算下达动作。
_REQUEST_LEAD_PATTERN = (
    r"(?:请(?:你|您)?(?:帮我)?|麻烦(?:你|您)?(?:帮我)?|帮我|帮忙|劳驾|有劳"
    r"|能否(?:帮我)?|能不能(?:帮我)?"
    r"|please|can\s+you|could\s+you|would\s+you|will\s+you|help\s+me"
    r"|i\s+want\s+you\s+to|i\s+need\s+you\s+to|i(?:'|’)d\s+like\s+you\s+to)"
)

# 中文执行核心：联网/网上/上网 限定 + 搜索类动词，以及「搜索网页」「网页搜索」。
_CHINESE_EXECUTION_CORE = (
    r"(?:联网(?:搜索|调研|查询|检索|搜|查)"
    r"|网上(?:搜索|查询|检索|搜|查)"
    r"|上网搜索|搜索网页|网页搜索)"
)

# 英文名词式核心（web search / online search）的余段边界断言：只在整句到此结束
# （可带结尾问号/句号）或紧跟空白 + for 词边界引入主题时才算下达动作。
_ENGLISH_NOUN_CORE_BOUNDARY = r"(?=\s*(?:[?？.。]?\s*$|\s+for\b))"

# 英文执行核心：search the web / search online 动词式与 (do|perform) a web search；
# 名词式 web search / online search 只认裸命令或 for 引入主题，其后未列出的谓语（is、
# means、returns 等）一律按陈述失败关闭，不用黑名单模拟授权（spec/58 §3）；
# 英文侧用词边界防止匹配词内子串。
_ENGLISH_EXECUTION_CORE = (
    r"(?:(?:do|perform)\s+(?:a\s+|an\s+)?web\s+search\b"
    r"|search\s+the\s+web\b|search\s+online\b"
    rf"|web\s+search\b{_ENGLISH_NOUN_CORE_BOUNDARY}"
    rf"|online\s+search\b{_ENGLISH_NOUN_CORE_BOUNDARY})"
)

# 请求位置执行句式：问法开头 = 可选礼貌前缀 + 执行核心；核心之前的任何其它文本
# （孤立单字、立场词或未识别前缀）都不构成授权，锚定串首即未知默认拒绝（spec/58 §3）。
_EXECUTION_SYNTAX_PATTERN = re.compile(
    rf"^(?:\s*(?:{_REQUEST_LEAD_PATTERN})\s*)?"
    rf"(?:{_CHINESE_EXECUTION_CORE}|{_ENGLISH_EXECUTION_CORE})"
)

# 执行核心之后的自指问法：把联网搜索本身当作询问对象（如何关闭/配置/使用、与其它方式
# 对比、询问含义与能力、只有疑问语气词收尾），不是下达搜索动作，命中即失败关闭。
_SELF_REFERENTIAL_TRAILING_PATTERN = re.compile(
    r"^\s*(?:的|个)?\s*(?:功能|能力|开关|选项|设置|参数|方式)?\s*"
    r"(?:"
    r"(?:如何|怎么|怎样|咋)(?:才能|能|才可以)?\s*"
    r"(?:关闭|禁用|停用|开启|启用|打开|设置|配置|使用|用|操作|实现|开通|接入)"
    r"(?:一下|下|好|掉)?\s*(?:这|那|它|这个|那个|该)?\s*"
    r"(?:功能|能力|开关|选项|设置|参数|方式)?\s*(?:呢|呀|啊)?\s*[。！？!?.，,]*\s*$"
    r"|(?:和|与|跟|vs\.?|versus)\s*[^。！？!?\n]{0,30}?"
    r"(?:有什么区别|有何区别|有什么不同|有何不同|有什么差别|有啥区别"
    r"|有区别|的区别|区别是什么|差异|差别|哪个好|哪个更好)"
    r"|(?:是什么|是啥|是什么意思|什么意思|指的是什么|指什么|是做什么用的|有什么用"
    r"|有什么作用|有什么用途|有什么功能|有什么限制|有什么优势|有什么特点|有什么能力"
    r"|有哪些作用|有哪些用途|有哪些功能|有哪些限制|有哪些用法)"
    r"|(?:吗|么|呢)\s*[？?。]?\s*$"
    r")"
)

# 明确否定与离线意图标记：用户明确禁止联网或要求仅离线时优先拒绝，
# 不能因为问法里同时出现正向关键词就放行外送（spec/58 §3）。
# 均为小写子串匹配；「仅离线/离线分析」等仅离线意图一并视为拒绝外送。
_NEGATED_INTENT_MARKERS = (
    "不要联网",
    "不要上网",
    "不要搜",
    "不联网",
    "不上网",
    "别联网",
    "别上网",
    "勿联网",
    "禁止联网",
    "不准联网",
    "无需联网",
    "不用联网",
    "不需要联网",
    "不访问网络",
    "不使用网络",
    "仅离线",
    "只离线",
    "纯离线",
    "离线分析",
    "do not search",
    "don't search",
    "don’t search",
    "dont search",
    "do not browse",
    "don't browse",
    "dont browse",
    "do not use the web",
    "don't use the web",
    "no web search",
    "no online search",
    "without web search",
    "without searching",
    "offline only",
    "offline analysis",
    "without internet",
    "no internet",
    "stay offline",
)

# 否定结构（中文 + 英文）：否定词后可隔空白与至多一个动作动词再接联网/搜索类词，
# 覆盖「不能联网搜索」「禁止使用 web search」等中英混合形态；英文侧覆盖
# must not / cannot 与 without + 动作 结构，兜住无法穷举的动词变体（spec/58 §3）。
_NEGATED_INTENT_PATTERN = re.compile(
    r"(?:不需要|不要|不用|无需|不需|不必|不许|不能|请勿|勿|禁止|不准)"
    r"(?:\s*(?:使用|进行|发起|调用|采用|执行|做|去|再))?"
    r"\s*(?:联网|上网|搜索|搜|查询|查|检索|浏览|网页"
    r"|web\s*search|online\s+search|search\s+the\s+web|search\s+online|brows(?:e|ing))"
    r"|(?:do not|must not|mustn't|mustn’t|cannot|can not|don't|don’t|dont|never)\s+"
    r"(?:(?:use|perform|run|make|do|initiate|execute)\s+)?"
    r"(?:any\s+|a\s+|the\s+)?"
    r"(?:web\s+search|search|browse|internet|web)"
    r"|without\s+"
    r"(?:(?:performing|doing|using|running|making|initiating|executing)\s+)?"
    r"(?:any\s+|a\s+|the\s+)?"
    r"(?:web\s+search|search|brows(?:e|ing)|internet|web)"
)

# 纯数字顶级域（含 0x 十六进制风格的最后一节）不是合法公网域名，按明显本地/混淆地址拒绝。
_NUMERIC_HOST_SUFFIX = re.compile(r"[0-9]+")
# 明显本地域名后缀：不做任何解析即拒绝。
_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")


# 方法作用：在请求位置匹配有限执行句式，返回执行核心之后的余段。
# Args: text - 已 trim 并转小写的用户问法。
# Returns: (是否命中执行句式, 核心之后的余段) 二元组；未命中时余段为空串。
def _match_execution_syntax(text: str) -> tuple[bool, str]:
    """只在问法开头（可含有限礼貌前缀）识别执行句式；核心之前的任意前缀都不授权。"""
    match = _EXECUTION_SYNTAX_PATTERN.match(text)
    if match is None:
        return False, ""
    return True, text[match.end():]


# 方法作用：判定执行核心之后的余段是否把联网搜索本身当作询问对象。
# Args: remainder - 执行核心之后的原问法余段。
# Returns: 命中自指问法句式时返回 True（应失败关闭）。
def _is_self_referential_tail(remainder: str) -> bool:
    """余段把搜索能力当作询问对象时不是执行请求，失败关闭（spec/58 §3）。"""
    return bool(_SELF_REFERENTIAL_TRAILING_PATTERN.match(remainder))


# 方法作用：判定问法是否包含明确联网搜索意图，作为外送授权的唯一依据。
# Args: query - 用户当前原始问法。
# Returns: 请求位置命中执行句式、无明确否定且非自指问法时返回 True。
def is_explicit_web_search(query: str) -> bool:
    """只在请求位置识别有限中英文执行句式；否定优先，未识别或仅提及的表述一律失败关闭。"""
    text = str(query or "").strip().lower()
    if not text:
        return False
    # 否定优先：用户明确禁止联网或要求仅离线时，任何执行句式都不构成授权。
    if any(marker in text for marker in _NEGATED_INTENT_MARKERS) or _NEGATED_INTENT_PATTERN.search(text):
        return False
    matched, remainder = _match_execution_syntax(text)
    if not matched:
        return False
    # 执行核心之后是自指问法或能力陈述时，用户是在谈论该能力而不是要求搜索。
    return not _is_self_referential_tail(remainder)


# 方法作用：按 URL 生成稳定摘要作为引用编号，保证去重与回指稳定。
# Args: url - 已通过合法性判定的引用 URL。
# Returns: sha256 前 16 位十六进制摘要。
def make_source_id(url: str) -> str:
    """同 URL 恒得同 source_id，不同 URL 视为不同引用。"""
    return hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:SOURCE_ID_CHARS]


# 方法作用：静态判定引用 URL 是否合法公共 HTTP(S) 链接，不做任何网络解析。
# Args: url - 工具返回的候选引用 URL。
# Returns: 无凭证、非本地、非字面私网、scheme 为 http/https 时返回 True。
def is_safe_public_url(url: str) -> bool:
    """SSRF 与凭证泄漏面在引用层直接拒绝，无 userinfo、无本地/私网主机。"""
    if not isinstance(url, str):
        return False
    text = url.strip()
    if not text:
        return False
    try:
        parsed = urlsplit(text)
        host = (parsed.hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    if parsed.username or parsed.password:
        return False
    if not host:
        return False
    # 字面 IP 只允许公网地址；回环、私网、链路本地和保留段全部拒绝。
    try:
        address = ipaddress.ip_address(host.split("%", maxsplit=1)[0])
    except ValueError:
        address = None
    if address is not None:
        return bool(address.is_global)
    # 明显本地主机名与单标签内网名不做解析即拒绝。
    if host == "localhost" or host.endswith(_LOCAL_HOST_SUFFIXES):
        return False
    if "." not in host:
        return False
    # 纯数字顶级域（如 0x7f.0.0.1 的最后一节）视为混淆地址拒绝。
    return not _NUMERIC_HOST_SUFFIX.fullmatch(host.rsplit(".", maxsplit=1)[1])


class SearchHit(BaseModel):
    """单条可引用搜索来源：只有工具实际返回且 URL 合法的条目才可构建。"""

    model_config = ConfigDict(extra="ignore")

    source_id: str = ""
    url: str = Field(..., min_length=1)
    title: str = ""
    snippet: str = ""
    provider: str = ""
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # 方法作用：按上限截断标题，防止单个来源撑爆证据预算。
    # Args: value - 原始标题文本。
    # Returns: 至多 300 字符的标题。
    @field_validator("title")
    @classmethod
    def _truncate_title(cls, value: str) -> str:
        return str(value or "")[:MAX_TITLE_CHARS]

    # 方法作用：按上限截断摘要，防止单个来源撑爆证据预算。
    # Args: value - 原始摘要文本。
    # Returns: 至多 2000 字符的摘要。
    @field_validator("snippet")
    @classmethod
    def _truncate_snippet(cls, value: str) -> str:
        return str(value or "")[:MAX_SNIPPET_CHARS]

    # 方法作用：未显式提供 source_id 时按 URL 稳定摘要补全。
    # Args: self - 已完成字段校验的条目。
    # Returns: 带 source_id 的条目自身。
    @model_validator(mode="after")
    def _ensure_source_id(self) -> "SearchHit":
        if not self.source_id:
            self.source_id = make_source_id(self.url)
        return self


class SearchResponse(BaseModel):
    """一次受控搜索的脱敏结果：失败时只有固定错误码，无正文或底层异常。"""

    model_config = ConfigDict(extra="ignore")

    success: bool = False
    hits: list[SearchHit] = Field(default_factory=list)
    error_code: str = ""
    truncated: bool = False
