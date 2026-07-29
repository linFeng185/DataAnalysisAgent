"""统一 Prompt 字符预算分配，覆盖固定 System 与动态上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.logging_config import get_logger


logger = get_logger(__name__)
_TRUNCATION_MARKER = "\n...[已截断]"
PromptTarget = Literal["system", "human"]


@dataclass(frozen=True, slots=True)
class PromptSection:
    """一个可按优先级分配的动态 Prompt 片段。"""

    name: str
    content: str
    priority: int = 50
    min_chars: int = 0
    max_chars: int | None = None
    target: PromptTarget = "human"


@dataclass(frozen=True, slots=True)
class BudgetedPrompt:
    """完成统一预算后的 System/Human 文本和诊断摘要。"""

    system: str
    human: str
    budget: int
    used_chars: int
    truncated_sections: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PreparedSection:
    """预算器内部使用的规范化 section。"""

    index: int
    section: PromptSection
    original: str
    capped: str
    capped_by_max: bool


# 方法作用：在严格字符上限内截断文本并尽可能附加可见标记。
# Args: text - 原始文本；limit - 最大字符数。
# Returns: 长度不超过 limit 的文本。
def _truncate_text(text: str, limit: int) -> str:
    """短上限保留纯前缀，空间足够时附加统一截断标记。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATION_MARKER):
        return text[:limit]
    return text[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


# 方法作用：校验并规范化待分配的动态 Prompt section。
# Args: sections - 调用节点声明的动态 section 列表。
# Returns: 已移除空内容并应用单节上限的内部 section 列表。
def _prepare_sections(sections: list[PromptSection]) -> list[_PreparedSection]:
    """拒绝非法名称、目标和负预算，避免裁剪行为静默失真。"""
    prepared: list[_PreparedSection] = []
    seen_names: set[str] = set()
    for index, section in enumerate(sections):
        name = str(section.name or "").strip()
        if not name:
            raise ValueError("Prompt section 名称不能为空")
        if name in seen_names:
            raise ValueError(f"Prompt section 名称重复: {name}")
        if section.target not in {"system", "human"}:
            raise ValueError(f"Prompt section 目标非法: {section.target}")
        if section.min_chars < 0:
            raise ValueError(f"Prompt section 最低字符数不能为负数: {name}")
        if section.max_chars is not None and section.max_chars < section.min_chars:
            raise ValueError(f"Prompt section 上限小于最低字符数: {name}")
        original = str(section.content or "").strip()
        if not original:
            continue
        seen_names.add(name)
        max_chars = len(original) if section.max_chars is None else section.max_chars
        capped = _truncate_text(original, max_chars)
        prepared.append(_PreparedSection(
            index=index,
            section=PromptSection(
                name=name,
                content=original,
                priority=section.priority,
                min_chars=section.min_chars,
                max_chars=section.max_chars,
                target=section.target,
            ),
            original=original,
            capped=capped,
            capped_by_max=len(capped) < len(original),
        ))
    return prepared


# 方法作用：按最低保留量和优先级分配单次 Prompt 的统一字符预算。
# Args: prompt_id - 注册 Prompt ID；sections - 动态上下文；system_values - System 模板变量。
# Returns: 总长度不超过 PromptDefinition.context_budget 的 System/Human 文本。
def build_budgeted_prompt(
    prompt_id: str,
    sections: list[PromptSection],
    *,
    system_values: dict[str, object] | None = None,
) -> BudgetedPrompt:
    """固定 System 不截断，动态 section 先保底再按优先级扩展。"""
    from src.llm.prompts import get_prompt_definition

    definition = get_prompt_definition(prompt_id)
    budget = int(definition.context_budget)
    if budget <= 0:
        raise ValueError(f"Prompt {prompt_id} 的 context_budget 必须大于零")
    fixed_system = definition.render(**dict(system_values or {}))
    if len(fixed_system) > budget:
        raise ValueError(
            f"Prompt {prompt_id} 固定 System Prompt 已超过预算: "
            f"{len(fixed_system)}/{budget}",
        )

    prepared = _prepare_sections(sections)
    separator_reserve = len(prepared) * 2
    available = max(0, budget - len(fixed_system) - separator_reserve)
    allocations = {item.section.name: 0 for item in prepared}
    ordered = sorted(prepared, key=lambda item: (-item.section.priority, item.index))

    # 先覆盖各 section 的最低保留量，确保低优先级证据不会被完全挤出。
    for item in ordered:
        required = min(len(item.capped), item.section.min_chars)
        granted = min(required, available)
        allocations[item.section.name] = granted
        available -= granted

    # 剩余字符按优先级补足，高优先级内容获得更完整上下文。
    for item in ordered:
        current = allocations[item.section.name]
        granted = min(len(item.capped) - current, available)
        allocations[item.section.name] += granted
        available -= granted
        if available <= 0:
            break

    system_parts: list[str] = []
    human_parts: list[str] = []
    truncated: list[str] = []
    for item in sorted(prepared, key=lambda value: value.index):
        granted = allocations[item.section.name]
        rendered = _truncate_text(item.capped, granted)
        if item.capped_by_max or granted < len(item.capped):
            truncated.append(item.section.name)
        if not rendered:
            continue
        target = system_parts if item.section.target == "system" else human_parts
        target.append(rendered)

    dynamic_system = "\n\n".join(system_parts)
    system = f"{fixed_system}\n\n{dynamic_system}" if dynamic_system else fixed_system
    human = "\n\n".join(human_parts)
    used_chars = len(system) + len(human)
    if used_chars > budget:
        raise RuntimeError(f"Prompt {prompt_id} 预算器输出超限: {used_chars}/{budget}")
    logger.info(
        "Prompt 统一预算完成",
        prompt_id=prompt_id,
        budget=budget,
        used_chars=used_chars,
        section_count=len(prepared),
        truncated_sections=truncated,
    )
    return BudgetedPrompt(
        system=system,
        human=human,
        budget=budget,
        used_chars=used_chars,
        truncated_sections=tuple(truncated),
    )
