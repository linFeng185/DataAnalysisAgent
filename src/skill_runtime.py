"""Skill 工具统一执行、隔离、资源治理和输出校验。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, create_model

from src.logging_config import get_logger

if TYPE_CHECKING:
    from src.skill_manager import Skill


logger = get_logger(__name__)
_SENSITIVE_KEYS = re.compile(r"password|passwd|secret|token|api[_-]?key|authorization", re.I)
_SENSITIVE_TEXT = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization)\s*[:=]\s*([^\s,;]+)"
)


class SkillRuntimeLimits(BaseModel):
    """限制单次 Skill 工具调用可消耗的资源。"""

    model_config = ConfigDict(extra="forbid")

    timeout_seconds: int = Field(default=30, ge=1, le=300)
    cpu_seconds: int = Field(default=10, ge=1, le=120)
    memory_mb: int = Field(default=256, ge=64, le=2048)
    max_input_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    max_output_bytes: int = Field(default=512 * 1024, ge=1024, le=10 * 1024 * 1024)


class SkillToolPayload(BaseModel):
    """强制工具输入是可验证的 JSON 对象。"""

    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any]


class SkillRuntimeError(RuntimeError):
    """表示 Skill 运行时的受控失败。"""


class SkillRuntime:
    """统一执行可信内置工具和隔离的受管 Skill 工具。"""

    async def execute(
        self,
        skill: Skill,
        tool_name: str,
        payload: dict[str, Any],
        *,
        trusted_builtin: bool,
        allowed_asset_paths: list[str] | None = None,
    ) -> Any:
        """校验输入，执行工具，再校验大小、脱敏和引用契约。"""
        normalized_payload = _to_json_value(payload)
        validated_payload = SkillToolPayload(payload=normalized_payload).payload
        logger.info(
            "Skill Runtime 输入规范化边界",
            skill=skill.name,
            tool=tool_name,
            fields=sorted(validated_payload),
            value_types={key: type(value).__name__ for key, value in validated_payload.items()},
        )
        limits = _runtime_limits(skill.resources)
        input_bytes = _json_bytes(validated_payload)
        if len(input_bytes) > limits.max_input_bytes:
            raise SkillRuntimeError("Skill 工具输入超过大小限制")
        input_schema = _load_tool_schema(skill, tool_name, "input")
        _validate_json_schema(validated_payload, input_schema, "输入")
        logger.info(
            "Skill Runtime 执行开始",
            skill=skill.name,
            tool=tool_name,
            isolated=not trusted_builtin,
            timeout_seconds=limits.timeout_seconds,
            input_bytes=len(input_bytes),
        )
        if trusted_builtin:
            async with asyncio.timeout(limits.timeout_seconds):
                output = await self._execute_in_process(skill, tool_name, validated_payload)
        else:
            output = await self._execute_isolated(
                skill,
                tool_name,
                validated_payload,
                limits,
                allowed_asset_paths or [],
            )
        sanitized = _sanitize_output(output)
        output_schema = _load_tool_schema(skill, tool_name, "output")
        _validate_json_schema(sanitized, output_schema, "输出")
        _validate_citations(sanitized)
        output_bytes = _json_bytes(sanitized)
        if len(output_bytes) > limits.max_output_bytes:
            raise SkillRuntimeError("Skill 工具输出超过大小限制")
        logger.info(
            "Skill Runtime 执行完成",
            skill=skill.name,
            tool=tool_name,
            output_bytes=len(output_bytes),
        )
        return sanitized

    async def _execute_in_process(
        self,
        skill: Skill,
        tool_name: str,
        payload: dict[str, Any],
    ) -> Any:
        """仅为仓库内置可信 Skill 在当前进程加载工具。"""
        tools_path = Path(skill.source_path).resolve() / "tools.py"
        if not tools_path.is_file():
            raise SkillRuntimeError("Skill 缺少 tools.py")
        module_name = f"skills.runtime_{skill.package_digest[:16] or skill.name.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(module_name, tools_path)
        if spec is None or spec.loader is None:
            raise SkillRuntimeError("Skill 工具模块无法加载")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        tool = module.get_tool(tool_name) if hasattr(module, "get_tool") else None
        if tool is None:
            raise SkillRuntimeError(f"Skill 未导出工具: {tool_name}")
        return await tool.ainvoke(payload)

    async def _execute_isolated(
        self,
        skill: Skill,
        tool_name: str,
        payload: dict[str, Any],
        limits: SkillRuntimeLimits,
        allowed_asset_paths: list[str],
    ) -> Any:
        """使用独立解释器执行非内置 Skill，并有界读取标准输出。"""
        worker_path = Path(__file__).with_name("skill_worker.py").resolve()
        request = {
            "skill_root": str(Path(skill.source_path).resolve()),
            "project_src_root": str(Path(__file__).resolve().parent),
            "tool_name": tool_name,
            "payload": payload,
            "permissions": dict(skill.permissions or {}),
            "limits": limits.model_dump(),
            "allowed_asset_paths": [str(Path(path).resolve()) for path in allowed_asset_paths],
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(worker_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert process.stdin is not None
            process.stdin.write(_json_bytes(request))
            await process.stdin.drain()
            process.stdin.close()
            async with asyncio.timeout(limits.timeout_seconds):
                stdout_task = asyncio.create_task(
                    _read_bounded(process.stdout, limits.max_output_bytes + 16 * 1024)
                )
                stderr_task = asyncio.create_task(_read_bounded(process.stderr, 64 * 1024))
                stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
                return_code = await process.wait()
        except (TimeoutError, SkillRuntimeError):
            process.kill()
            await process.wait()
            logger.error("隔离 Skill 被资源策略终止", skill=skill.name, tool=tool_name)
            raise
        if return_code != 0:
            logger.error(
                "隔离 Skill 执行失败",
                skill=skill.name,
                tool=tool_name,
                return_code=return_code,
                stderr=stderr.decode("utf-8", errors="replace")[:500],
            )
            raise SkillRuntimeError("隔离 Skill 工具执行失败")
        try:
            response = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SkillRuntimeError("隔离 Skill 返回了非法 JSON") from exc
        if not isinstance(response, dict) or not response.get("ok"):
            raise SkillRuntimeError(str(response.get("error") or "隔离 Skill 执行失败"))
        return response.get("result")


class SkillRuntimeTool(BaseTool):
    """将 Skill Runtime 暴露为 LangChain 可调用工具。"""

    name: str
    description: str
    args_schema: type[BaseModel] | None = None
    _skill: Any = PrivateAttr()
    _runtime: SkillRuntime = PrivateAttr()
    _trusted_builtin: bool = PrivateAttr(default=False)

    def __init__(
        self,
        *,
        skill: Skill,
        tool_name: str,
        description: str,
        trusted_builtin: bool,
        runtime: SkillRuntime | None = None,
    ) -> None:
        args_schema = _build_tool_args_schema(skill, tool_name)
        super().__init__(
            name=tool_name,
            description=description,
            args_schema=args_schema,
        )
        self._skill = skill
        self._runtime = runtime or SkillRuntime()
        self._trusted_builtin = trusted_builtin
        visible_schema = self.get_input_schema()
        logger.info(
            "Skill Runtime 工具契约暴露完成",
            skill=skill.name,
            tool=tool_name,
            fields=sorted(visible_schema.model_fields),
        )

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        """同步入口显式拒绝，避免在事件循环外绕过异步治理。"""
        del args, kwargs
        raise SkillRuntimeError("Skill 工具只允许异步执行")

    async def _arun(self, run_manager: Any = None, **kwargs: Any) -> Any:
        """把 LangChain 工具参数交给统一 Skill Runtime。"""
        del run_manager
        logger.info(
            "Skill Runtime 工具调用边界输入",
            skill=self._skill.name,
            tool=self.name,
            fields=sorted(kwargs),
            value_types={key: type(value).__name__ for key, value in kwargs.items()},
        )
        return await self._runtime.execute(
            self._skill,
            self.name,
            kwargs,
            trusted_builtin=self._trusted_builtin,
        )


def _runtime_limits(resources: dict[str, Any] | None) -> SkillRuntimeLimits:
    """从 Manifest 构造有上限的运行资源策略。"""
    values = dict(resources or {})
    accepted = set(SkillRuntimeLimits.model_fields)
    return SkillRuntimeLimits.model_validate({key: value for key, value in values.items() if key in accepted})


def _build_tool_args_schema(skill: Skill, tool_name: str) -> type[BaseModel] | None:
    """把 Manifest 输入 JSON Schema 转换为 Agent 可见的 Pydantic 参数模型。"""
    schema = _load_tool_schema(skill, tool_name, "input")
    if schema is None:
        return None
    if schema.get("type") != "object":
        raise SkillRuntimeError("Skill 工具输入 Schema 顶层必须是对象")
    model_name = re.sub(r"[^0-9A-Za-z_]", "_", f"{skill.name}_{tool_name}_Input")
    return _object_model_from_schema(schema, model_name)


def _object_model_from_schema(schema: dict[str, Any], model_name: str) -> type[BaseModel]:
    """递归构造对象 Schema，并保留必填字段和额外字段策略。"""
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise SkillRuntimeError("Skill 工具输入 Schema 的 properties 必须是对象")
    required = {
        str(item)
        for item in (schema.get("required", []) or [])
        if isinstance(item, str)
    }
    fields: dict[str, tuple[Any, Any]] = {}
    for field_name, raw_field_schema in properties.items():
        if not isinstance(raw_field_schema, dict):
            raise SkillRuntimeError(f"Skill 工具字段 Schema 无效: {field_name}")
        field_schema = dict(raw_field_schema)
        annotation = _annotation_from_schema(
            field_schema,
            f"{model_name}_{str(field_name).title()}",
        )
        default = ... if field_name in required else field_schema.get("default", None)
        fields[str(field_name)] = (
            annotation,
            Field(default, **_field_constraints(field_schema)),
        )
    extra_policy = "forbid" if schema.get("additionalProperties") is False else "allow"
    return create_model(
        model_name,
        __config__=ConfigDict(extra=extra_policy),
        **fields,
    )


def _annotation_from_schema(schema: dict[str, Any], model_name: str) -> Any:
    """将常用 JSON Schema 类型映射为 Pydantic 可校验的类型注解。"""
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return Literal.__getitem__(tuple(enum_values))
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        annotations = [
            type(None) if item == "null" else _annotation_from_schema({**schema, "type": item}, model_name)
            for item in schema_type
        ]
        result = annotations[0] if annotations else Any
        for annotation in annotations[1:]:
            result = result | annotation
        return result
    if schema_type == "object":
        properties = schema.get("properties")
        if isinstance(properties, dict) and properties:
            return _object_model_from_schema(schema, model_name)
        additional = schema.get("additionalProperties")
        value_type = (
            _annotation_from_schema(additional, f"{model_name}_Value")
            if isinstance(additional, dict)
            else Any
        )
        return dict[str, value_type]
    if schema_type == "array":
        item_schema = schema.get("items", {})
        item_type = (
            _annotation_from_schema(item_schema, f"{model_name}_Item")
            if isinstance(item_schema, dict)
            else Any
        )
        return list[item_type]
    return {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "null": type(None),
    }.get(schema_type, Any)


def _field_constraints(schema: dict[str, Any]) -> dict[str, Any]:
    """把 JSON Schema 的常用约束转换为 Pydantic Field 参数。"""
    constraints: dict[str, Any] = {}
    mappings = {
        "description": "description",
        "title": "title",
        "minimum": "ge",
        "maximum": "le",
        "exclusiveMinimum": "gt",
        "exclusiveMaximum": "lt",
        "multipleOf": "multiple_of",
        "pattern": "pattern",
    }
    for source, target in mappings.items():
        if source in schema:
            constraints[target] = schema[source]
    schema_type = schema.get("type")
    if schema_type == "string":
        if "minLength" in schema:
            constraints["min_length"] = schema["minLength"]
        if "maxLength" in schema:
            constraints["max_length"] = schema["maxLength"]
    elif schema_type == "array":
        if "minItems" in schema:
            constraints["min_length"] = schema["minItems"]
        if "maxItems" in schema:
            constraints["max_length"] = schema["maxItems"]
    return constraints


def _load_tool_schema(skill: Skill, tool_name: str, kind: str) -> dict[str, Any] | None:
    """读取工具级或 Skill 级输入输出 JSON Schema。"""
    key = f"{kind}_schema"
    schema_ref = ""
    for tool in skill.tools or []:
        if str(tool.get("name", "")) == tool_name:
            schema_ref = str(tool.get(key, "") or "")
            break
    schema_ref = schema_ref or str(getattr(skill, key, "") or "")
    if not schema_ref:
        return None
    root = Path(skill.source_path).resolve()
    schema_path = (root / schema_ref).resolve()
    if root != schema_path and root not in schema_path.parents:
        raise SkillRuntimeError("Skill Schema 路径越界")
    if not schema_path.is_file():
        raise SkillRuntimeError(f"Skill 声明的 {kind} Schema 不存在")
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SkillRuntimeError(f"Skill {kind} Schema 不是合法 JSON") from exc
    if not isinstance(schema, dict):
        raise SkillRuntimeError(f"Skill {kind} Schema 必须是 JSON 对象")
    return schema


def _validate_json_schema(value: Any, schema: dict[str, Any] | None, label: str) -> None:
    """使用 JSON Schema 校验工具输入或输出。"""
    if schema is None:
        return
    try:
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value)
    except Exception as exc:
        raise SkillRuntimeError(f"Skill {label}不符合声明契约") from exc


def _sanitize_output(value: Any) -> Any:
    """递归脱敏 Skill 输出并限制为 JSON 可表达值。"""
    if isinstance(value, BaseModel):
        return _sanitize_output(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {
            str(key): "***" if _SENSITIVE_KEYS.search(str(key)) else _sanitize_output(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_output(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        return _SENSITIVE_TEXT.sub(lambda match: f"{match.group(1)}=***", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _to_json_value(value: Any) -> Any:
    """把 Pydantic 工具参数递归还原为 JSON Schema 可校验的普通值。"""
    if isinstance(value, BaseModel):
        return _to_json_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_value(item) for item in value]
    return value


def _validate_citations(value: Any) -> None:
    """存在 citations 字段时强制每条引用具有来源和定位。"""
    if not isinstance(value, dict) or "citations" not in value:
        return
    citations = value.get("citations")
    if not isinstance(citations, list):
        raise SkillRuntimeError("Skill 输出 citations 必须是数组")
    for citation in citations:
        if not isinstance(citation, dict) or not citation.get("source_id") or not citation.get("locator"):
            raise SkillRuntimeError("Skill 输出包含不可追溯引用")


def _json_bytes(value: Any) -> bytes:
    """按稳定 JSON 编码计算输入输出边界大小。"""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=lambda item: str(item) if isinstance(item, Decimal) else str(item),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SkillRuntimeError("Skill 输入输出不是可序列化 JSON") from exc


async def _read_bounded(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    """有界读取子进程输出，超过上限立即失败。"""
    if stream is None:
        return b""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise SkillRuntimeError("隔离 Skill 输出超过大小限制")
        chunks.append(chunk)
