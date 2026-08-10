"""OpenAPI 接口契约漂移比较工具。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


class CompareApiContractsTool(BaseTool):
    """比较两版 OpenAPI 路径、操作、参数、请求体和响应结构。"""

    name: str = "compare_api_contracts"
    description: str = "比较两版 OpenAPI 契约并区分破坏性、兼容性和信息性变化。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("compare_api_contracts 只允许异步执行")

    async def _arun(
        self,
        baseline: dict[str, Any],
        current: dict[str, Any],
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """按客户端兼容性视角比较两个内联 OpenAPI 文档。"""
        del run_manager
        baseline_paths = _paths(baseline, "baseline")
        current_paths = _paths(current, "current")
        logger.info(
            "接口契约漂移比较开始",
            baseline_path_count=len(baseline_paths),
            current_path_count=len(current_paths),
        )
        breaking: list[dict[str, Any]] = []
        compatible: list[dict[str, Any]] = []
        informational: list[dict[str, Any]] = []

        def add(
            target: list[dict[str, Any]],
            code: str,
            path: str,
            before: Any,
            after: Any,
            reason: str,
        ) -> None:
            target.append(
                {
                    "code": code,
                    "path": path,
                    "before": before,
                    "after": after,
                    "reason": reason,
                }
            )

        for path in sorted(set(baseline_paths) - set(current_paths)):
            add(breaking, "PATH_REMOVED", path, "present", "missing", "删除路径会破坏现有客户端")
        for path in sorted(set(current_paths) - set(baseline_paths)):
            add(compatible, "PATH_ADDED", path, "missing", "present", "新增路径不影响现有客户端")

        for path in sorted(set(baseline_paths) & set(current_paths)):
            old_path = baseline_paths[path]
            new_path = current_paths[path]
            old_operations = _operations(old_path, path, "baseline")
            new_operations = _operations(new_path, path, "current")
            for method in sorted(set(old_operations) - set(new_operations)):
                add(
                    breaking,
                    "OPERATION_REMOVED",
                    f"{method.upper()} {path}",
                    "present",
                    "missing",
                    "删除操作会破坏现有调用",
                )
            for method in sorted(set(new_operations) - set(old_operations)):
                add(
                    compatible,
                    "OPERATION_ADDED",
                    f"{method.upper()} {path}",
                    "missing",
                    "present",
                    "新增操作兼容现有调用",
                )
            for method in sorted(set(old_operations) & set(new_operations)):
                operation_path = f"{method.upper()} {path}"
                old_operation = old_operations[method]
                new_operation = new_operations[method]
                _compare_operation(
                    baseline,
                    current,
                    old_path,
                    new_path,
                    old_operation,
                    new_operation,
                    operation_path,
                    breaking,
                    compatible,
                    informational,
                    add,
                )

        for collection in (breaking, compatible, informational):
            collection.sort(key=lambda item: (item["path"], item["code"]))
        result = {
            "compatible": not breaking,
            "breaking_changes": breaking,
            "compatible_changes": compatible,
            "informational_changes": informational,
            "summary": {
                "breaking": len(breaking),
                "compatible": len(compatible),
                "informational": len(informational),
                "total": len(breaking) + len(compatible) + len(informational),
            },
        }
        logger.info(
            "接口契约漂移比较完成",
            compatible=result["compatible"],
            breaking_change_count=len(breaking),
            total_change_count=result["summary"]["total"],
        )
        return result


def _compare_operation(
    baseline: dict[str, Any],
    current: dict[str, Any],
    old_path: dict[str, Any],
    new_path: dict[str, Any],
    old_operation: dict[str, Any],
    new_operation: dict[str, Any],
    operation_path: str,
    breaking: list[dict[str, Any]],
    compatible: list[dict[str, Any]],
    informational: list[dict[str, Any]],
    add: Any,
) -> None:
    """比较单个接口操作。"""
    old_security = old_operation.get("security")
    new_security = new_operation.get("security")
    if not old_security and new_security:
        add(
            breaking,
            "SECURITY_REQUIRED",
            operation_path,
            old_security,
            new_security,
            "新增认证要求会拒绝原有匿名调用",
        )
    old_parameters = _parameter_map(
        [*(old_path.get("parameters") or []), *(old_operation.get("parameters") or [])],
        baseline,
        operation_path,
    )
    new_parameters = _parameter_map(
        [*(new_path.get("parameters") or []), *(new_operation.get("parameters") or [])],
        current,
        operation_path,
    )
    for key in sorted(set(old_parameters) - set(new_parameters)):
        add(
            breaking,
            "PARAMETER_REMOVED",
            f"{operation_path}.parameters.{key}",
            old_parameters[key],
            None,
            "删除已接受参数可能破坏现有请求",
        )
    for key in sorted(set(new_parameters) - set(old_parameters)):
        parameter = new_parameters[key]
        target = breaking if parameter.get("required", False) else compatible
        add(
            target,
            "PARAMETER_ADDED",
            f"{operation_path}.parameters.{key}",
            None,
            parameter,
            "新增必填参数会破坏现有请求" if target is breaking else "新增可选参数兼容现有请求",
        )
    for key in sorted(set(old_parameters) & set(new_parameters)):
        old_parameter = old_parameters[key]
        new_parameter = new_parameters[key]
        parameter_path = f"{operation_path}.parameters.{key}"
        if not old_parameter.get("required", False) and new_parameter.get("required", False):
            add(
                breaking,
                "PARAMETER_REQUIRED",
                parameter_path,
                False,
                True,
                "可选参数改为必填会破坏现有请求",
            )
        _compare_schema(
            _resolve_schema(baseline, old_parameter.get("schema") or {}),
            _resolve_schema(current, new_parameter.get("schema") or {}),
            parameter_path,
            "request",
            breaking,
            compatible,
            add,
        )

    old_request = _resolve_schema(baseline, old_operation.get("requestBody") or {})
    new_request = _resolve_schema(current, new_operation.get("requestBody") or {})
    if not old_request and new_request:
        target = breaking if new_request.get("required", False) else compatible
        add(
            target,
            "REQUEST_BODY_ADDED",
            f"{operation_path}.requestBody",
            None,
            new_request.get("required", False),
            "新增必填请求体会破坏现有请求" if target is breaking else "新增可选请求体兼容现有请求",
        )
    elif old_request and not new_request:
        add(
            breaking,
            "REQUEST_BODY_REMOVED",
            f"{operation_path}.requestBody",
            "present",
            "missing",
            "删除已接受请求体可能破坏现有请求",
        )
    elif old_request and new_request:
        if not old_request.get("required", False) and new_request.get("required", False):
            add(
                breaking,
                "REQUEST_BODY_REQUIRED",
                f"{operation_path}.requestBody",
                False,
                True,
                "请求体改为必填会破坏现有请求",
            )
        _compare_schema(
            _resolve_schema(baseline, _content_schema(old_request)),
            _resolve_schema(current, _content_schema(new_request)),
            f"{operation_path}.requestBody.schema",
            "request",
            breaking,
            compatible,
            add,
        )

    old_responses = old_operation.get("responses") or {}
    new_responses = new_operation.get("responses") or {}
    if not isinstance(old_responses, dict) or not isinstance(new_responses, dict):
        raise ValueError(f"{operation_path} 的 responses 必须是对象")
    for status in sorted(set(old_responses) - set(new_responses)):
        add(
            breaking,
            "RESPONSE_REMOVED",
            f"{operation_path}.responses.{status}",
            "present",
            "missing",
            "删除响应状态会破坏依赖该响应的客户端",
        )
    for status in sorted(set(new_responses) - set(old_responses)):
        add(
            compatible,
            "RESPONSE_ADDED",
            f"{operation_path}.responses.{status}",
            "missing",
            "present",
            "新增响应状态不改变既有响应结构",
        )
    for status in sorted(set(old_responses) & set(new_responses)):
        _compare_schema(
            _resolve_schema(
                baseline,
                _content_schema(_resolve_schema(baseline, old_responses[status])),
            ),
            _resolve_schema(
                current,
                _content_schema(_resolve_schema(current, new_responses[status])),
            ),
            f"{operation_path}.responses.{status}.schema",
            "response",
            breaking,
            compatible,
            add,
        )
    for field in ("summary", "description"):
        if old_operation.get(field) != new_operation.get(field):
            add(
                informational,
                "DOCUMENTATION_CHANGED",
                f"{operation_path}.{field}",
                old_operation.get(field),
                new_operation.get(field),
                "接口说明发生变化",
            )


def _compare_schema(
    old_schema: dict[str, Any],
    new_schema: dict[str, Any],
    path: str,
    direction: str,
    breaking: list[dict[str, Any]],
    compatible: list[dict[str, Any]],
    add: Any,
) -> None:
    """递归比较请求或响应对象 Schema 的关键兼容性。"""
    old_type = _schema_type(old_schema)
    new_type = _schema_type(new_schema)
    if old_type != new_type:
        add(
            breaking,
            "SCHEMA_TYPE_CHANGED",
            path,
            old_type,
            new_type,
            "数据类型变化会破坏序列化或解析",
        )
        return
    if old_type == "array":
        _compare_schema(
            old_schema.get("items") or {},
            new_schema.get("items") or {},
            f"{path}.items",
            direction,
            breaking,
            compatible,
            add,
        )
        return
    if old_type != "object":
        old_enum = old_schema.get("enum")
        new_enum = new_schema.get("enum")
        if isinstance(old_enum, list) and isinstance(new_enum, list):
            removed = [item for item in old_enum if item not in new_enum]
            if removed:
                add(
                    breaking,
                    "ENUM_VALUES_REMOVED",
                    path,
                    old_enum,
                    new_enum,
                    "删除允许值会破坏既有数据",
                )
        return
    old_properties = old_schema.get("properties") or {}
    new_properties = new_schema.get("properties") or {}
    if not isinstance(old_properties, dict) or not isinstance(new_properties, dict):
        raise ValueError(f"{path} 的 properties 必须是对象")
    for name in sorted(set(old_properties) - set(new_properties)):
        add(
            breaking,
            f"{direction.upper()}_PROPERTY_REMOVED",
            f"{path}.{name}",
            old_properties[name],
            None,
            "删除字段会破坏既有请求或响应",
        )
    for name in sorted(set(new_properties) - set(old_properties)):
        required = name in set(new_schema.get("required") or [])
        target = breaking if direction == "request" and required else compatible
        add(
            target,
            f"{direction.upper()}_PROPERTY_ADDED",
            f"{path}.{name}",
            None,
            new_properties[name],
            "请求新增必填字段会破坏现有客户端" if target is breaking else "新增字段保持向后兼容",
        )
    old_required = set(old_schema.get("required") or [])
    new_required = set(new_schema.get("required") or [])
    newly_required = new_required - old_required
    if direction == "request":
        for name in sorted(newly_required & set(old_properties)):
            add(
                breaking,
                "REQUEST_PROPERTY_REQUIRED",
                f"{path}.{name}",
                False,
                True,
                "请求字段改为必填会破坏现有客户端",
            )
    else:
        for name in sorted((old_required - new_required) & set(new_properties)):
            add(
                breaking,
                "RESPONSE_PROPERTY_OPTIONAL",
                f"{path}.{name}",
                True,
                False,
                "响应字段不再保证存在会破坏现有客户端",
            )
    for name in sorted(set(old_properties) & set(new_properties)):
        _compare_schema(
            old_properties[name],
            new_properties[name],
            f"{path}.{name}",
            direction,
            breaking,
            compatible,
            add,
        )


def _paths(document: dict[str, Any], label: str) -> dict[str, Any]:
    """读取并校验 OpenAPI paths 对象。"""
    if not isinstance(document, dict):
        raise ValueError(f"{label} 必须是对象")
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise ValueError(f"{label}.paths 必须是对象")
    return paths


def _operations(path_item: Any, path: str, label: str) -> dict[str, dict[str, Any]]:
    """提取路径下的 HTTP 操作。"""
    if not isinstance(path_item, dict):
        raise ValueError(f"{label}.paths.{path} 必须是对象")
    result: dict[str, dict[str, Any]] = {}
    for method, operation in path_item.items():
        normalized = str(method).lower()
        if normalized not in _HTTP_METHODS:
            continue
        if not isinstance(operation, dict):
            raise ValueError(f"{label} 的 {normalized.upper()} {path} 必须是对象")
        result[normalized] = operation
    return result


def _parameter_map(
    parameters: Any, document: dict[str, Any], path: str
) -> dict[str, dict[str, Any]]:
    """将参数数组规范化为位置和名称联合索引。"""
    if not isinstance(parameters, list):
        raise ValueError(f"{path} 的 parameters 必须是数组")
    result: dict[str, dict[str, Any]] = {}
    for parameter in parameters:
        resolved = _resolve_schema(document, parameter)
        if not isinstance(resolved, dict):
            raise ValueError(f"{path} 包含无效参数")
        name = str(resolved.get("name", "")).strip()
        location = str(resolved.get("in", "")).strip()
        if not name or location not in {"query", "header", "path", "cookie"}:
            raise ValueError(f"{path} 包含缺少 name 或 in 的参数")
        key = f"{location}:{name}"
        if key in result:
            raise ValueError(f"{path} 包含重复参数 {key}")
        result[key] = resolved
    return result


def _content_schema(container: Any) -> dict[str, Any]:
    """从请求体或响应对象提取首选媒体类型 Schema。"""
    if not isinstance(container, dict):
        return {}
    content = container.get("content") or {}
    if not isinstance(content, dict) or not content:
        return {}
    media = content.get("application/json")
    if media is None:
        media = content[sorted(content)[0]]
    return media.get("schema") or {} if isinstance(media, dict) else {}


def _resolve_schema(document: dict[str, Any], value: Any) -> dict[str, Any]:
    """递归展开仅限当前文档的本地 JSON Pointer 引用。"""
    resolved = _expand_value(document, value, set(), 0)
    return resolved if isinstance(resolved, dict) else {}


def _expand_value(
    document: dict[str, Any],
    value: Any,
    reference_stack: set[str],
    depth: int,
) -> Any:
    """递归展开对象和数组中的本地引用。"""
    if depth > 40:
        raise ValueError("OpenAPI 引用或嵌套深度超过限制")
    if isinstance(value, list):
        return [_expand_value(document, item, reference_stack, depth + 1) for item in value]
    if not isinstance(value, dict):
        return value
    reference = value.get("$ref")
    if isinstance(reference, str):
        if not reference.startswith("#/") or reference in reference_stack:
            raise ValueError("只支持无循环的本地 OpenAPI 引用")
        target: Any = document
        for token in reference[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or token not in target:
                raise ValueError(f"OpenAPI 引用不存在: {reference}")
            target = target[token]
        if not isinstance(target, dict):
            raise ValueError(f"OpenAPI 引用目标不是对象: {reference}")
        siblings = {key: item for key, item in value.items() if key != "$ref"}
        merged = {**target, **siblings}
        return _expand_value(
            document,
            merged,
            reference_stack | {reference},
            depth + 1,
        )
    return {
        key: _expand_value(document, item, reference_stack, depth + 1)
        for key, item in value.items()
    }


def _schema_type(schema: dict[str, Any]) -> str:
    """返回 Schema 显式或可推断类型。"""
    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        return schema_type
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return "unknown"


_TOOLS: dict[str, BaseTool] = {"compare_api_contracts": CompareApiContractsTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
