"""工作流状态差异与持久化一致性检查工具。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)


class CompareWorkflowStateTool(BaseTool):
    """按嵌套字段路径比较执行前后状态和持久化状态。"""

    name: str = "compare_workflow_state"
    description: str = "比较工作流前后状态，识别字段新增、删除、覆盖、保护字段变化和持久化差异。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("compare_workflow_state 只允许异步执行")

    async def _arun(
        self,
        before_state: dict[str, Any],
        after_state: dict[str, Any],
        ignore_fields: list[str] | None = None,
        protected_fields: list[str] | None = None,
        required_fields_after: list[str] | None = None,
        expected_changes: list[str] | None = None,
        persisted_state: dict[str, Any] | None = None,
        persistence_fields: list[str] | None = None,
        max_differences: int = 200,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """返回有界状态差异和完整策略违规。"""
        del run_manager
        ignore_fields = ignore_fields or []
        protected_fields = protected_fields or []
        required_fields_after = required_fields_after or []
        expected_changes = expected_changes or []
        persistence_fields = persistence_fields or []
        _validate_paths(
            ignore_fields,
            protected_fields,
            required_fields_after,
            expected_changes,
            persistence_fields,
            max_differences,
        )
        logger.info(
            "工作流状态比较开始",
            before_top_level_fields=len(before_state),
            after_top_level_fields=len(after_state),
            persistence_enabled=persisted_state is not None,
        )
        before_flat = _flatten(before_state)
        after_flat = _flatten(after_state)
        ignored = set(ignore_fields)
        all_paths = sorted((set(before_flat) | set(after_flat)) - ignored)
        added = [path for path in all_paths if path not in before_flat]
        removed = [path for path in all_paths if path not in after_flat]
        changed_paths = [
            path
            for path in all_paths
            if path in before_flat
            and path in after_flat
            and _token(before_flat[path]) != _token(after_flat[path])
        ]
        unchanged_count = sum(
            path in before_flat
            and path in after_flat
            and _token(before_flat[path]) == _token(after_flat[path])
            for path in all_paths
        )
        changed_set = set(added) | set(removed) | set(changed_paths)
        protected_violations = sorted(changed_set & set(protected_fields))
        missing_required = sorted(path for path in required_fields_after if path not in after_flat)
        missing_expected_changes = sorted(set(expected_changes) - changed_set)

        persistence_mismatches: list[dict[str, Any]] = []
        if persisted_state is not None:
            persisted_flat = _flatten(persisted_state)
            fields_to_check = persistence_fields or sorted(after_flat)
            for path in fields_to_check:
                after_value = after_flat.get(path, _MISSING)
                persisted_value = persisted_flat.get(path, _MISSING)
                if _token(after_value) != _token(persisted_value):
                    persistence_mismatches.append(
                        {
                            "path": path,
                            "after": None if after_value is _MISSING else after_value,
                            "persisted": None if persisted_value is _MISSING else persisted_value,
                            "after_present": after_value is not _MISSING,
                            "persisted_present": persisted_value is not _MISSING,
                        }
                    )
        elif persistence_fields:
            raise ValueError("声明 persistence_fields 时必须提供 persisted_state")

        differences: list[dict[str, Any]] = []
        for path in added:
            differences.append(
                {"path": path, "change": "added", "before": None, "after": after_flat[path]}
            )
        for path in removed:
            differences.append(
                {"path": path, "change": "removed", "before": before_flat[path], "after": None}
            )
        for path in changed_paths:
            differences.append(
                {
                    "path": path,
                    "change": "changed",
                    "before": before_flat[path],
                    "after": after_flat[path],
                }
            )
        differences.sort(key=lambda item: (item["path"], item["change"]))
        total_differences = len(differences)
        valid = not protected_violations and not missing_required and not persistence_mismatches
        result = {
            "valid": valid,
            "identical": total_differences == 0,
            "summary": {
                "added": len(added),
                "removed": len(removed),
                "changed": len(changed_paths),
                "unchanged": unchanged_count,
                "total_differences": total_differences,
            },
            "differences": differences[:max_differences],
            "differences_truncated": total_differences > max_differences,
            "protected_violations": protected_violations,
            "missing_required_fields": missing_required,
            "missing_expected_changes": missing_expected_changes,
            "persistence_mismatches": persistence_mismatches[:max_differences],
            "before_fingerprint": _fingerprint(before_state),
            "after_fingerprint": _fingerprint(after_state),
        }
        logger.info(
            "工作流状态比较完成",
            valid=valid,
            total_differences=total_differences,
            protected_violation_count=len(protected_violations),
            persistence_mismatch_count=len(persistence_mismatches),
        )
        return result


class _MissingValue:
    """表示字段不存在且区别于显式空值。"""


_MISSING = _MissingValue()


def _validate_paths(*values: Any) -> None:
    """校验路径数组和差异上限。"""
    *path_groups, max_differences = values
    for group in path_groups:
        if len(set(group)) != len(group) or any(not str(path).strip() for path in group):
            raise ValueError("字段路径数组不能包含空值或重复项")
    if not 1 <= max_differences <= 1000:
        raise ValueError("max_differences 必须在 1 到 1000 之间")


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """将嵌套对象展平为点分字段路径，数组作为原子值。"""
    if not isinstance(value, dict):
        return {prefix: value} if prefix else {}
    result: dict[str, Any] = {}
    for key in sorted(value, key=str):
        path = f"{prefix}.{key}" if prefix else str(key)
        item = value[key]
        if isinstance(item, dict) and item:
            result.update(_flatten(item, path))
        else:
            result[path] = item
    return result


def _token(value: Any) -> str:
    """把状态值转换为稳定比较标记。"""
    if value is _MISSING:
        return "__MISSING__"
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    """计算稳定状态指纹。"""
    return hashlib.sha256(_token(value).encode("utf-8")).hexdigest()


_TOOLS: dict[str, BaseTool] = {"compare_workflow_state": CompareWorkflowStateTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
