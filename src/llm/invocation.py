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


def prepare_invocation(
    prompt_id: str,
    sections: list[PromptSection],
    *,
    task: str | None = None,
    temperature: float = 0.0,
    reasoning: bool = False,
    system_values: dict[str, object] | None = None,
    config: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    model: Any | None = None,
) -> PreparedInvocation:
    """统一准备模型、消息、Prompt 预算和 LangSmith 追踪元数据。"""
    definition = get_prompt_definition(prompt_id)
    effective_task = task or definition.task
    budgeted = build_budgeted_prompt(
        prompt_id,
        sections,
        system_values=system_values,
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
    logger.info(
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
    )


async def invoke_structured(
    prompt_id: str,
    sections: list[PromptSection],
    *,
    output_model: type[ModelT] | None = None,
    task: str | None = None,
    temperature: float = 0.0,
    reasoning: bool = False,
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
    response = await prepared.model.ainvoke(prepared.messages)
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
    prepared = prepare_invocation(prompt_id, sections, **kwargs)
    response = await prepared.model.ainvoke(prepared.messages)
    return str(getattr(response, "content", response) or "").strip()


async def stream_prompt(
    prompt_id: str,
    sections: list[PromptSection],
    **kwargs: Any,
) -> AsyncIterator[Any]:
    """通过注册 Prompt 流式返回模型 chunk。"""
    prepared = prepare_invocation(prompt_id, sections, **kwargs)
    async for chunk in prepared.model.astream(prepared.messages):
        yield chunk
