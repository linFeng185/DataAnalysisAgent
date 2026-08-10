"""Skill Manifest v2 离线校验工具。"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator
from langchain_core.tools import BaseTool
from pydantic import ValidationError

from src.logging_config import get_logger
from src.skill_runtime import SkillRuntimeLimits

logger = get_logger(__name__)
_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_]*)+$")
_ASSET_KINDS = {"database", "table_file", "document", "timeseries", "api", "stream"}
_FILE_PERMISSIONS = {"none", "read_asset_only"}
_DATASOURCE_PERMISSIONS = {"none", "read_only"}


class ValidateSkillManifestTool(BaseTool):
    """校验调用方提供的清单对象和内联 JSON Schema。"""

    name: str = "validate_skill_manifest"
    description: str = (
        "离线检查 Manifest v2、触发规则、权限、资源预算、工具 Schema 和评测配置，不读取文件系统。"
    )

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("validate_skill_manifest 只允许异步执行")

    async def _arun(
        self,
        manifest: dict[str, Any],
        instructions: str,
        schemas: dict[str, dict[str, Any]],
        existing_skills: list[dict[str, Any]] | None = None,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """执行完整静态校验并返回错误、警告和检查项。"""
        del run_manager
        logger.info(
            "Skill 清单校验开始",
            manifest_fields=sorted(manifest),
            schema_count=len(schemas),
            existing_skill_count=len(existing_skills or []),
        )
        errors: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        checks: list[dict[str, Any]] = []

        required = {
            "name",
            "version",
            "api_version",
            "description",
            "triggers",
            "capabilities",
            "accepts",
            "permissions",
            "resources",
            "tools",
            "evaluation",
        }
        missing = sorted(required - set(manifest))
        _record(
            checks,
            errors,
            "required_fields",
            not missing,
            f"缺少必填字段: {', '.join(missing)}" if missing else "必填字段完整",
        )

        name = str(manifest.get("name", ""))
        _record(
            checks,
            errors,
            "name",
            bool(_NAME_PATTERN.fullmatch(name)) and len(name) <= 64,
            "name 必须为不超过 64 字符的连字符小写名称",
        )
        version = str(manifest.get("version", ""))
        _record(
            checks,
            errors,
            "version",
            bool(_VERSION_PATTERN.fullmatch(version)),
            "version 必须符合语义版本格式",
        )
        _record(
            checks,
            errors,
            "api_version",
            manifest.get("api_version") == "data-agent/v2",
            "api_version 必须为 data-agent/v2",
        )
        _record(
            checks,
            errors,
            "description",
            len(str(manifest.get("description", "")).strip()) >= 10,
            "description 至少需要 10 个字符",
        )
        _record(
            checks,
            errors,
            "instructions",
            len(instructions.strip()) >= 20,
            "技能正文至少需要 20 个字符",
        )

        _validate_triggers(
            manifest.get("triggers"), checks, errors, warnings, existing_skills or [], name
        )
        _validate_capabilities(manifest.get("capabilities"), checks, errors)
        _validate_accepts(manifest.get("accepts"), checks, errors)
        _validate_permissions(manifest.get("permissions"), checks, errors)
        _validate_resources(manifest.get("resources"), checks, errors)
        _validate_tools(manifest.get("tools"), schemas, checks, errors)
        _validate_evaluation(manifest.get("evaluation"), checks, errors)

        result = {
            "valid": not errors,
            "score": max(0, round(100 - len(errors) * 15 - len(warnings) * 3)),
            "errors": errors,
            "warnings": warnings,
            "checks": checks,
            "filesystem_accessed": False,
        }
        logger.info(
            "Skill 清单校验完成",
            valid=result["valid"],
            score=result["score"],
            error_count=len(errors),
            warning_count=len(warnings),
        )
        return result


def _record(
    checks: list[dict[str, Any]],
    errors: list[dict[str, str]],
    check_id: str,
    passed: bool,
    message: str,
) -> None:
    """记录检查结果，并把失败项加入错误列表。"""
    checks.append({"id": check_id, "passed": passed, "message": message})
    if not passed:
        errors.append({"code": check_id.upper(), "message": message})


def _validate_triggers(
    triggers: Any,
    checks: list[dict[str, Any]],
    errors: list[dict[str, str]],
    warnings: list[dict[str, str]],
    existing_skills: list[dict[str, Any]],
    name: str,
) -> None:
    """校验触发结构并检查跨 Skill 关键词冲突。"""
    valid = isinstance(triggers, dict) and all(
        isinstance(triggers.get(key, []), list) for key in ("keywords", "intents", "tables")
    )
    _record(
        checks, errors, "triggers", valid, "triggers 必须包含数组形式的 keywords、intents 和 tables"
    )
    if not valid:
        return
    keywords = {
        str(item).strip().lower() for item in triggers.get("keywords", []) if str(item).strip()
    }
    if not keywords and not triggers.get("intents") and not triggers.get("tables"):
        warnings.append(
            {"code": "NO_AUTO_TRIGGER", "message": "没有自动触发条件，只能显式选择该 Skill"}
        )
    for existing in existing_skills:
        other_name = str(existing.get("name", ""))
        if other_name == name:
            continue
        overlap = keywords & {
            str(item).strip().lower() for item in existing.get("keywords", []) if str(item).strip()
        }
        if overlap:
            warnings.append(
                {
                    "code": "TRIGGER_COLLISION",
                    "message": f"与 {other_name} 存在重复关键词: {', '.join(sorted(overlap))}",
                }
            )


def _validate_capabilities(
    capabilities: Any,
    checks: list[dict[str, Any]],
    errors: list[dict[str, str]],
) -> None:
    """校验能力标识列表。"""
    valid = (
        isinstance(capabilities, list)
        and bool(capabilities)
        and len(capabilities) == len(set(capabilities))
        and all(
            isinstance(item, str) and _CAPABILITY_PATTERN.fullmatch(item) for item in capabilities
        )
    )
    _record(
        checks, errors, "capabilities", valid, "capabilities 必须是非空、无重复的点分小写标识列表"
    )


def _validate_accepts(
    accepts: Any,
    checks: list[dict[str, Any]],
    errors: list[dict[str, str]],
) -> None:
    """校验资产类型白名单。"""
    valid = isinstance(accepts, list) and bool(accepts) and set(accepts) <= _ASSET_KINDS
    _record(checks, errors, "accepts", valid, "accepts 必须使用受支持的数据资产类型")


def _validate_permissions(
    permissions: Any,
    checks: list[dict[str, Any]],
    errors: list[dict[str, str]],
) -> None:
    """校验网络、文件和数据源权限。"""
    valid = (
        isinstance(permissions, dict)
        and isinstance(permissions.get("network"), list)
        and permissions.get("files") in _FILE_PERMISSIONS
        and permissions.get("datasources") in _DATASOURCE_PERMISSIONS
    )
    _record(
        checks,
        errors,
        "permissions",
        valid,
        "permissions 必须声明 network 数组及受支持的 files、datasources 权限",
    )


def _validate_resources(
    resources: Any,
    checks: list[dict[str, Any]],
    errors: list[dict[str, str]],
) -> None:
    """复用运行时模型校验资源边界。"""
    valid = isinstance(resources, dict)
    message = "资源预算有效"
    if valid:
        try:
            accepted = set(SkillRuntimeLimits.model_fields)
            SkillRuntimeLimits.model_validate(
                {key: value for key, value in resources.items() if key in accepted}
            )
            max_calls = int(resources.get("max_tool_calls", 0))
            if not 1 <= max_calls <= 100:
                raise ValueError("max_tool_calls 必须在 1 到 100 之间")
        except (ValidationError, TypeError, ValueError) as exc:
            valid = False
            message = f"资源预算无效: {exc}"
    else:
        message = "resources 必须是对象"
    _record(checks, errors, "resources", valid, message)


def _validate_tools(
    tools: Any,
    schemas: dict[str, dict[str, Any]],
    checks: list[dict[str, Any]],
    errors: list[dict[str, str]],
) -> None:
    """校验工具名称、Schema 路径及 Draft 2020-12 结构。"""
    if not isinstance(tools, list) or not tools:
        _record(checks, errors, "tools", False, "tools 必须是非空数组")
        return
    names: set[str] = set()
    valid = True
    messages: list[str] = []
    for index, tool in enumerate(tools, start=1):
        if not isinstance(tool, dict) or not str(tool.get("name", "")).strip():
            valid = False
            messages.append(f"第 {index} 个工具缺少名称")
            continue
        tool_name = str(tool["name"])
        if tool_name in names:
            valid = False
            messages.append(f"工具名重复: {tool_name}")
        names.add(tool_name)
        for field in ("input_schema", "output_schema"):
            path = str(tool.get(field, ""))
            if not _safe_schema_path(path):
                valid = False
                messages.append(f"工具 {tool_name} 的 {field} 路径不安全")
                continue
            schema = schemas.get(path)
            if not isinstance(schema, dict):
                valid = False
                messages.append(f"未提供工具 {tool_name} 的 Schema: {path}")
                continue
            try:
                Draft202012Validator.check_schema(schema)
            except Exception as exc:
                valid = False
                messages.append(f"工具 {tool_name} 的 Schema 无效: {exc}")
    _record(checks, errors, "tools", valid, "；".join(messages) if messages else "工具契约有效")


def _safe_schema_path(path: str) -> bool:
    """只允许 Skill 根目录下 schemas/ 中的相对 JSON 路径。"""
    normalized = path.replace("\\", "/")
    pure = PurePosixPath(normalized)
    return (
        bool(path)
        and not pure.is_absolute()
        and ".." not in pure.parts
        and pure.parts[:1] == ("schemas",)
        and pure.suffix == ".json"
    )


def _validate_evaluation(
    evaluation: Any,
    checks: list[dict[str, Any]],
    errors: list[dict[str, str]],
) -> None:
    """校验评测集路径和最低得分。"""
    valid = isinstance(evaluation, dict)
    if valid:
        dataset = str(evaluation.get("dataset", ""))
        minimum_score = evaluation.get("minimum_score")
        path = PurePosixPath(dataset.replace("\\", "/"))
        valid = (
            not path.is_absolute()
            and ".." not in path.parts
            and path.parts[:1] == ("eval",)
            and path.suffix in {".yaml", ".yml", ".json"}
            and isinstance(minimum_score, (int, float))
            and 0 <= float(minimum_score) <= 1
        )
    _record(
        checks,
        errors,
        "evaluation",
        valid,
        "evaluation 必须声明 eval/ 下的评测集和 0 到 1 的 minimum_score",
    )


_TOOLS: dict[str, BaseTool] = {"validate_skill_manifest": ValidateSkillManifestTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
