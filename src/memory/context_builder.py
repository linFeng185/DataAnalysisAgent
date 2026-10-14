"""7.5 上下文裁剪 — 按模型上下文窗口动态控制会话历史。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from src.config import get_settings
from src.logging_config import get_logger
from src.memory.models import ConversationTurn

logger = get_logger(__name__)

DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_TRIGGER_RATIO = 0.80
DEFAULT_TARGET_RATIO = 0.55
DEFAULT_HARD_LIMIT_RATIO = 0.92
DEFAULT_PROMPT_RESERVE = 12_000
DEFAULT_OUTPUT_RESERVE = 8_192
DEFAULT_SUMMARY_RESERVE = 1_024
_TRUNCATION_MARKER = "\n...[已截断]"


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """当前模型一次会话可使用的输入预算和压缩阈值。"""

    model_id: str
    context_window: int
    prompt_reserve_tokens: int
    output_reserve_tokens: int
    usable_input_tokens: int
    trigger_tokens: int
    target_tokens: int
    hard_limit_tokens: int
    summary_reserve_tokens: int


# 方法作用：兼容 ConversationTurn、字典和旧 checkpoint 对象读取轮次字段。
# Args: turn - 单轮会话对象；key - 要读取的字段名。
# Returns: 字段值；缺失或空值返回空字符串。
def _g(turn: Any, key: str) -> Any:
    """兼容不同 checkpoint 序列化形态，避免上下文构建因类型差异失败。"""
    if hasattr(turn, "get"):
        return turn.get(key, "") or ""
    return getattr(turn, key, "") or ""


# 方法作用：把模型能力字段安全转换成正整数。
# Args: value - 模型目录或配置中的能力值；field_name - 日志字段名。
# Returns: 有效正整数；非法值返回 None。
def _positive_int(value: Any, field_name: str = "value") -> int | None:
    """拒绝空值、布尔值和非正数，防止错误能力配置放大上下文预算。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        logger.warning("模型能力值无效", field_name=field_name, value=str(value)[:80])
        return None
    return number if number > 0 else None


# 方法作用：从字典或能力对象读取模型能力字段。
# Args: capabilities - 字典或 SupportedFeatures 类似对象；key - 字段名。
# Returns: 原始能力值；缺失返回 None。
def _capability(capabilities: Any, key: str) -> Any:
    """统一处理租户 JSONB 能力和内置 dataclass 能力。"""
    if isinstance(capabilities, dict):
        return capabilities.get(key)
    return getattr(capabilities, key, None)


# 方法作用：解析当前请求可用的模型标识与能力声明。
# Args: model_id - 显式模型标识；model - 可选已创建的 ChatModel。
# Returns: 模型标识和能力字典二元组。
def _resolve_model_identity(
    model_id: str | None = None,
    model: Any | None = None,
) -> tuple[str, dict[str, Any]]:
    """优先使用租户授权模型，之后回退到模型注册表和本地配置。"""
    selected_id = str(model_id or "").strip()
    capabilities: dict[str, Any] = {}

    try:
        from src.llm.tenant_config import get_current_tenant_llm_selection

        selection = get_current_tenant_llm_selection()
    except Exception as exc:
        logger.warning("读取租户 LLM 选择失败，使用本地模型能力", error=str(exc))
        selection = None
    if selection is not None:
        if not selected_id:
            selected_id = str(getattr(selection, "model_id", "") or "").strip()
        selected_capabilities = getattr(selection, "capabilities", {})
        if isinstance(selected_capabilities, dict):
            capabilities.update(selected_capabilities)

    if model is not None:
        model_capabilities = getattr(model, "capabilities", None)
        for key in ("context_window", "max_tokens_limit"):
            value = _capability(model_capabilities, key)
            if value is None:
                value = getattr(model, key, None)
            if value is not None:
                capabilities.setdefault(key, value)
        if not selected_id:
            selected_id = str(
                getattr(model, "model", None)
                or getattr(model, "model_name", None)
                or ""
            ).strip()

    settings = get_settings()
    if not selected_id:
        selected_id = str(
            getattr(settings, "local_llm_model", "")
            or getattr(settings, "llm_model", "")
            or ""
        ).strip()

    if selected_id:
        try:
            from src.llm.model_registry import get_model_registry

            info = get_model_registry().get(selected_id)
            if info is not None:
                registry_capabilities = info.capabilities
                for key in ("context_window", "max_tokens_limit"):
                    value = _capability(registry_capabilities, key)
                    if value is not None:
                        capabilities.setdefault(key, value)
        except Exception as exc:
            logger.warning(
                "读取模型注册表能力失败，使用默认上下文窗口",
                model_id=selected_id,
                error=str(exc),
            )
        if not _positive_int(capabilities.get("context_window"), "context_window"):
            try:
                from src.llm.adapters.registry import get_adapter

                adapter = get_adapter(selected_id)
                value = _capability(adapter.supported_features, "context_window")
                if value is not None:
                    capabilities.setdefault("context_window", value)
                value = _capability(adapter.supported_features, "max_tokens_limit")
                if value is not None:
                    capabilities.setdefault("max_tokens_limit", value)
            except Exception as exc:
                logger.debug(
                    "模型适配器未声明上下文能力",
                    model_id=selected_id,
                    error=str(exc),
                )
    return selected_id, capabilities


# 方法作用：解析当前模型的完整上下文窗口。
# Args: context_window - 显式窗口覆盖；model_id - 模型标识；model - 可选已创建模型。
# Returns: 正整数 Token 窗口；配置异常时回退到 128K。
def resolve_context_window(
    context_window: int | None = None,
    *,
    model_id: str | None = None,
    model: Any | None = None,
) -> int:
    """从显式值、租户能力、模型注册表依次解析上下文限制。"""
    explicit = _positive_int(context_window, "context_window")
    if explicit is not None:
        return explicit

    selected_id, capabilities = _resolve_model_identity(model_id, model)
    resolved = _positive_int(capabilities.get("context_window"), "context_window")
    if resolved is not None:
        return resolved

    settings = get_settings()
    configured_default = _positive_int(
        getattr(settings, "context_default_window", DEFAULT_CONTEXT_WINDOW),
        "context_default_window",
    )
    if configured_default is None:
        logger.warning(
            "默认上下文窗口配置无效，回退 128K",
            model_id=selected_id,
        )
        return DEFAULT_CONTEXT_WINDOW
    logger.warning(
        "未找到模型上下文能力，使用保守默认窗口",
        model_id=selected_id,
        context_window=configured_default,
    )
    return configured_default


# 方法作用：读取当前模型声明的提示词预算换算系数。
# Args: model_id - 显式模型标识；model - 可选模型实例。
# 说明：能力链对模型对象/注册表/适配器只读取 context_window/max_tokens_limit，
#   本系数当前唯一有效来源是租户 LLM 选择链（缺失时由调用方回退全局设置）。
# Returns: 正数系数；未声明或非法时返回 None（由调用方回退全局默认）。
def resolve_prompt_budget_factor(
    *,
    model_id: str | None = None,
    model: Any | None = None,
) -> float | None:
    """供提示词预算动态化读取供应商级 token→字符折算系数（spec/53）。"""
    _, capabilities = _resolve_model_identity(model_id, model)
    raw = capabilities.get("prompt_budget_char_factor")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


# 方法作用：根据模型窗口计算输入预算、触发阈值、目标阈值和硬保护线。
# Args: context_window - 显式模型窗口；model_id - 模型标识；model - 可选模型实例。
# Returns: ContextBudget，供上下文构建和会话归档共同使用。
def get_context_budget(
    context_window: int | None = None,
    *,
    model_id: str | None = None,
    model: Any | None = None,
) -> ContextBudget:
    """扣除固定 Prompt 与输出空间后按滞回阈值生成动态预算。"""
    settings = get_settings()
    selected_id, capabilities = _resolve_model_identity(model_id, model)
    window = resolve_context_window(
        context_window,
        model_id=selected_id,
        model=model,
    )

    prompt_reserve = _positive_int(
        getattr(settings, "context_prompt_reserve_tokens", DEFAULT_PROMPT_RESERVE),
        "context_prompt_reserve_tokens",
    ) or 0
    configured_output_reserve = _positive_int(
        getattr(settings, "context_output_reserve_tokens", DEFAULT_OUTPUT_RESERVE),
        "context_output_reserve_tokens",
    ) or 0
    model_output_limit = _positive_int(
        capabilities.get("max_tokens_limit"),
        "max_tokens_limit",
    ) or 0
    output_reserve = max(configured_output_reserve, model_output_limit)

    # 至少给输入保留一个 Token；小窗口模型不能被错误的预留值算成负预算。
    reserve_limit = max(0, window - 1)
    prompt_reserve = min(prompt_reserve, reserve_limit)
    output_reserve = min(output_reserve, max(0, reserve_limit - prompt_reserve))
    usable_input = max(1, window - prompt_reserve - output_reserve)

    absolute_limit = _positive_int(
        getattr(settings, "context_max_tokens", 0),
        "context_max_tokens",
    )
    if absolute_limit is not None:
        usable_input = min(usable_input, absolute_limit)

    def _ratio(name: str, default: float) -> float:
        value = getattr(settings, name, default)
        try:
            value = float(value)
        except (TypeError, ValueError):
            logger.warning("上下文比例配置无效，使用默认值", field_name=name, value=str(value))
            return default
        return value if 0.0 < value < 1.0 else default

    trigger_ratio = _ratio("context_compression_trigger_ratio", DEFAULT_TRIGGER_RATIO)
    target_ratio = _ratio("context_compression_target_ratio", DEFAULT_TARGET_RATIO)
    hard_ratio = _ratio("context_hard_limit_ratio", DEFAULT_HARD_LIMIT_RATIO)
    trigger = max(1, min(usable_input, math.floor(usable_input * trigger_ratio)))
    hard_limit = max(trigger, min(usable_input, math.ceil(usable_input * hard_ratio)))
    target = max(1, min(trigger - 1 if trigger > 1 else 1, math.floor(usable_input * target_ratio)))
    summary_reserve = _positive_int(
        getattr(settings, "context_summary_reserve_tokens", DEFAULT_SUMMARY_RESERVE),
        "context_summary_reserve_tokens",
    ) or DEFAULT_SUMMARY_RESERVE
    summary_reserve = min(summary_reserve, max(1, target))
    budget = ContextBudget(
        model_id=selected_id,
        context_window=window,
        prompt_reserve_tokens=prompt_reserve,
        output_reserve_tokens=output_reserve,
        usable_input_tokens=usable_input,
        trigger_tokens=trigger,
        target_tokens=target,
        hard_limit_tokens=hard_limit,
        summary_reserve_tokens=summary_reserve,
    )
    logger.debug(
        "动态上下文预算解析完成",
        model_id=selected_id,
        context_window=window,
        usable_input_tokens=usable_input,
        trigger_tokens=trigger,
        target_tokens=target,
        hard_limit_tokens=hard_limit,
    )
    return budget


# 方法作用：估算文本所占 Token 数，兼容没有 tokenizer 依赖的部署环境。
# Args: text - 待估算文本。
# Returns: 保守的正整数 Token 估算；空文本返回 0。
def estimate_tokens(text: str) -> int:
    """中文按字符估算、拉丁字符按四字符一个 Token 估算，避免旧比例低估中文。"""
    if not text:
        return 0
    chinese = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other = len(text) - chinese
    return max(1, math.ceil(chinese + other * 0.25))


# 方法作用：把单轮会话渲染成用于计数和 Prompt 的统一文本。
# Args: turn - 字典或 ConversationTurn 单轮记录。
# Returns: 只包含业务相关字段的文本。
def render_turn(turn: Any) -> str:
    """统一计入问题、SQL、分析结论和决策摘要，防止富历史绕过预算。"""
    fields = [f"用户: {_g(turn, 'user_query')}" ]
    generated_sql = str(_g(turn, "generated_sql") or "")
    if generated_sql:
        fields.append(f"执行的SQL: {generated_sql}")
    analysis_summary = str(_g(turn, "analysis_summary") or "")
    if analysis_summary:
        fields.append(f"分析结论: {analysis_summary}")
    decision_summary = str(_g(turn, "decision_summary") or "")
    if decision_summary and decision_summary != analysis_summary:
        fields.append(f"决策摘要: {decision_summary}")
    return "\n".join(fields)


# 方法作用：估算整个会话历史的 Token 数。
# Args: history - 会话轮次列表。
# Returns: 包含字段标签和分隔符的历史 Token 估算。
def estimate_history_tokens(history: list[Any] | None) -> int:
    """按实际注入字段估算历史大小，摘要轮次也会被计入。"""
    total = 0
    for turn in history or []:
        rendered = render_turn(turn)
        if rendered.strip() != "用户:":
            total += estimate_tokens(rendered) + 4
    return total


# 方法作用：按估算 Token 上限截断文本并保留统一标记。
# Args: text - 原始文本；max_tokens - 最大估算 Token 数。
# Returns: 不超过目标的文本；目标不足时返回尽可能短的前缀。
def truncate_text_to_tokens(text: str, max_tokens: int) -> str:
    """二分查找字符前缀，适应中文和英文混合内容。"""
    if not text or max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    marker = _TRUNCATION_MARKER
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle] + marker) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    if low <= 0:
        return text[: max(1, min(len(text), max_tokens))]
    return text[:low] + marker


# 方法作用：在当前模型预算内选择热轮次、温摘要和冷检索内容。
# Args: conversation_history - 历史轮次；user_query - 当前问题；long_term_store - 可选长期记忆；node_name - 节点名；context_window/model_id/model - 模型预算覆盖。
# Returns: 可直接注入 LLM Prompt 的上下文文本。
async def build_llm_context(
    conversation_history: list[ConversationTurn] | list[dict[str, Any]],
    user_query: str = "",
    long_term_store=None,
    node_name: str = "",
    *,
    context_window: int | None = None,
    model_id: str | None = None,
    model: Any | None = None,
) -> str:
    """统一构建动态预算的热/温/冷三层上下文。"""
    history = list(conversation_history or [])
    if not history:
        return ""
    settings = get_settings()
    budget = get_context_budget(context_window, model_id=model_id, model=model)
    history_tokens = estimate_history_tokens(history)
    if history_tokens >= budget.trigger_tokens:
        logger.info(
            "会话历史达到动态压缩阈值",
            node=node_name,
            model_id=budget.model_id,
            history_tokens=history_tokens,
            trigger_tokens=budget.trigger_tokens,
            hard_limit_tokens=budget.hard_limit_tokens,
        )

    hot_window = max(1, int(getattr(settings, "context_hot_turns", 5) or 5))
    warm_window = max(0, int(getattr(settings, "context_warm_turns", 20) or 20))
    hot = history[-hot_window:]
    warm_source = history[:-hot_window] if len(history) > hot_window else []
    warm = warm_source[-warm_window:] if warm_window else []
    parts: list[str] = [render_turn(turn) for turn in hot if render_turn(turn).strip() != "用户:"]

    if warm:
        summary = await _summarize_turns(warm)
        if summary:
            parts.insert(0, f"[前序对话摘要] {summary}")

    if (
        long_term_store
        and user_query
        and (len(history) > warm_window or history_tokens >= budget.trigger_tokens)
    ):
        try:
            hits = await long_term_store.search(user_query, top_k=3)
            if hits:
                questions = [
                    str(getattr(hit, "payload", {}).get("question", getattr(hit, "content", "")[:60]))
                    for hit in hits
                ]
                parts.insert(0, f"[历史相关经验] {', '.join(item for item in questions if item)}")
        except Exception as exc:
            logger.error("长期记忆检索跳过", error=str(exc), exc_info=True)

    if not parts:
        return ""
    latest = render_turn(history[-1])
    context = "\n---\n".join(parts)
    if estimate_tokens(context) > budget.target_tokens:
        logger.warning(
            "LLM 上下文超过目标预算，保留最新轮次",
            node=node_name,
            model_id=budget.model_id,
            estimated_tokens=estimate_tokens(context),
            target_tokens=budget.target_tokens,
        )
        summary_parts = [part for part in parts if part.startswith("[前序对话摘要]")]
        compact_parts = summary_parts + [latest]
        compact_context = "\n---\n".join(compact_parts)
        if estimate_tokens(compact_context) <= budget.target_tokens:
            context = compact_context
        else:
            # 最新一轮是理解追问的最低信息单元，即使极小的显式上限也不将其清空。
            context = (
                latest
                if budget.target_tokens <= 1
                else truncate_text_to_tokens(latest, budget.target_tokens)
            )
    logger.debug(
        "LLM 上下文构建完成",
        node=node_name,
        model_id=budget.model_id,
        history_tokens=history_tokens,
        rendered_tokens=estimate_tokens(context),
        history_turns=len(history),
    )
    return context


# 方法作用：生成多轮会话摘要并在 LLM 不可用时安全回退。
# Args: turns - 待摘要轮次。
# Returns: 摘要文本。
async def _summarize_turns(turns: list[ConversationTurn] | list[dict[str, Any]]) -> str:
    """LLM 优先生成摘要，失败时使用规则摘要。"""
    if not turns:
        return ""
    llm_summary = await _summarize_turns_llm(turns)
    if llm_summary:
        return llm_summary
    return _summarize_turns_rule(turns)


# 方法作用：通过受控摘要模型压缩历史轮次。
# Args: turns - 待摘要轮次。
# Returns: LLM 摘要；不可用或失败返回空字符串。
async def _summarize_turns_llm(turns: list[ConversationTurn] | list[dict[str, Any]]) -> str:
    """摘要输入先限长，避免摘要请求本身吞掉主会话预算。"""
    from src.llm.client import is_task_llm_available

    if not is_task_llm_available("context_summary"):
        return ""
    raw_text = "\n".join(
        render_turn(turn)
        for turn in turns
        if render_turn(turn).strip() != "用户:"
    )
    turns_text = truncate_text_to_tokens(raw_text, 2_000)
    if not turns_text:
        return ""
    try:
        from src.llm.invocation import invoke_text
        from src.llm.prompt_budget import PromptSection

        summary = await invoke_text(
            "context.summary",
            [
                PromptSection(
                    "conversation",
                    f"## 对话\n{turns_text}\n\n请生成摘要。",
                    priority=100,
                    min_chars=1000,
                    max_chars=2800,
                ),
            ],
            task="context_summary",
        )
        if summary:
            logger.info("LLM 摘要生成成功", turns=len(turns), chars=len(summary))
        return summary
    except Exception as exc:
        logger.error("LLM 摘要生成失败，回退到规则", error=str(exc), exc_info=True)
        return ""


# 方法作用：在无摘要模型时拼接可读的规则摘要。
# Args: turns - 待摘要轮次。
# Returns: 包含问题和失败次数的摘要文本。
def _summarize_turns_rule(turns: list[ConversationTurn] | list[dict[str, Any]]) -> str:
    """规则摘要只保留问题预览和执行状态，不引入新事实。"""
    queries = [str(_g(turn, "user_query"))[:80] for turn in turns if _g(turn, "user_query")]
    errors = sum(
        1
        for turn in turns
        if not bool(_g(turn, "execution_success")) and _g(turn, "generated_sql")
    )
    return (
        f"前 {len(turns)} 轮对话涵盖: {'; '.join(queries)}。"
        f"其中 {errors} 次查询需要重试或纠正。"
    )
