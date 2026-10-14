"""统一的 Prompt 准备、普通调用、结构化调用和流式调用入口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, TypeVar

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from src.llm.output_contracts import parse_json_model
from src.llm.prompt_budget import PromptSection, build_budgeted_prompt
from src.llm.prompts import get_prompt_definition
from src.logging_config import get_logger

logger = get_logger(__name__)
ModelT = TypeVar("ModelT", bound=BaseModel)


def get_task_llm(*args: Any, **kwargs: Any) -> Any:
    """运行时委托任务模型工厂，避免缓存跨 Context 的旧引用。"""
    from src.llm.client import get_task_llm as factory

    return factory(*args, **kwargs)


@dataclass(slots=True)
class PreparedInvocation:
    """一次已经完成注册、预算和追踪配置的模型调用。"""

    prompt_id: str
    prompt_version: str
    task: str
    model: Any
    messages: list[Any]
    config: dict[str, Any]
    # 本次调用的实际预算（spec/53 动态推导值）；0 表示调用方未提供，消费方回退注册上限。
    budget_chars: int = 0


def prepare_invocation(
    prompt_id: str,
    sections: list[PromptSection],
    *,
    task: str | None = None,
    temperature: float = 0.0,
    reasoning: bool | None = None,
    system_values: dict[str, object] | None = None,
    config: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    model: Any | None = None,
) -> PreparedInvocation:
    """统一准备模型、消息、Prompt 预算和 LangSmith 追踪元数据。"""
    definition = get_prompt_definition(prompt_id)
    effective_task = task or definition.task
    # 可信身份下为工具定义、一条有界工具结果与协议开销预留空间。
    from src.llm.knowledge_loop import knowledge_tool_reserve_chars
    from src.llm.prompt_budget import resolve_dynamic_budget

    # spec/53：预算按当前模型窗口动态推导，注册值仅作设计上限。
    # 预算按会话选择链推导，与 get_task_llm 创建的模型同源；显式传入的 model 实例不参与窗口推导。
    effective_budget = resolve_dynamic_budget(definition)
    budgeted = build_budgeted_prompt(
        prompt_id,
        sections,
        system_values=system_values,
        reserve_chars=knowledge_tool_reserve_chars(definition.context_budget),
        budget_override=effective_budget,
    )
    selected_model = model or get_task_llm(
        effective_task,
        temperature=temperature,
        reasoning=reasoning,
    )
    call_config = dict(config or {})
    call_metadata = dict(call_config.get("metadata", {}) or {})
    call_metadata.update({
        "prompt_id": prompt_id,
        "prompt_version": definition.version,
        "llm_task": effective_task,
        **dict(metadata or {}),
    })
    call_config["metadata"] = call_metadata
    call_config["tags"] = list(dict.fromkeys([
        *(call_config.get("tags", []) or []),
        f"prompt:{prompt_id}",
        f"prompt-version:{definition.version}",
        f"task:{effective_task}",
    ]))
    if hasattr(selected_model, "with_config"):
        selected_model = selected_model.with_config(call_config)
    # 每次 LLM 调用都会触发，属于调用内基础设施细节；
    # 对回测推理有价值的是 Prompt 统一预算完成（含 truncated_sections）与调用完成。
    logger.debug(
        "统一 LLM 调用准备完成",
        prompt_id=prompt_id,
        prompt_version=definition.version,
        task=effective_task,
        used_chars=budgeted.used_chars,
        truncated_sections=list(budgeted.truncated_sections),
    )
    return PreparedInvocation(
        prompt_id=prompt_id,
        prompt_version=definition.version,
        task=effective_task,
        model=selected_model,
        messages=[
            SystemMessage(content=budgeted.system),
            HumanMessage(content=budgeted.human),
        ],
        config=call_config,
        budget_chars=budgeted.budget,
    )


async def invoke_structured(
    prompt_id: str,
    sections: list[PromptSection],
    *,
    output_model: type[ModelT] | None = None,
    task: str | None = None,
    temperature: float = 0.0,
    reasoning: bool | None = None,
    system_values: dict[str, object] | None = None,
    config: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    model: Any | None = None,
) -> ModelT:
    """按注册 Prompt、统一预算和 Pydantic 契约完成一次 LLM 调用。"""
    definition = get_prompt_definition(prompt_id)
    model_type = output_model or definition.output_model
    if model_type is None:
        raise ValueError(f"Prompt {prompt_id} 未声明 output_model")
    prepared = prepare_invocation(
        prompt_id,
        sections,
        task=task,
        temperature=temperature,
        reasoning=reasoning,
        system_values=system_values,
        config=config,
        metadata=metadata,
        model=model,
    )
    logger.info(
        "统一结构化 LLM 调用开始",
        prompt_id=prompt_id,
        prompt_version=prepared.prompt_version,
        task=prepared.task,
    )
    # 统一走有界工具循环：模型不请求工具时仍是原有一次调用、零检索。
    from src.llm.knowledge_loop import invoke_with_knowledge

    response = await invoke_with_knowledge(prepared)
    parsed = parse_json_model(getattr(response, "content", response), model_type)
    logger.info(
        "统一结构化 LLM 调用完成",
        prompt_id=prompt_id,
        prompt_version=definition.version,
        output_model=model_type.__name__,
    )
    return parsed


async def invoke_text(
    prompt_id: str,
    sections: list[PromptSection],
    **kwargs: Any,
) -> str:
    """通过注册 Prompt 返回原始正文，供显式兼容场景使用。"""
    from src.llm.knowledge_loop import invoke_with_knowledge

    prepared = prepare_invocation(prompt_id, sections, **kwargs)
    response = await invoke_with_knowledge(prepared)
    return str(getattr(response, "content", response) or "").strip()


async def stream_prompt(
    prompt_id: str,
    sections: list[PromptSection],
    **kwargs: Any,
) -> AsyncIterator[Any]:
    """通过注册 Prompt 流式返回模型 chunk，中间工具回合以重置标记隔离。"""
    from src.llm.knowledge_loop import stream_with_knowledge

    prepared = prepare_invocation(prompt_id, sections, **kwargs)
    async for chunk in stream_with_knowledge(prepared):
        yield chunk
