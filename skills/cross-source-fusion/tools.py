"""跨资产连接契约工具。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger
from src.tools.join_contract import build_join_contract

logger = get_logger(__name__)


class InspectJoinTool(BaseTool):
    """在连接前检查匹配率、基数与膨胀风险。"""

    name: str = "inspect_join"
    description: str = "构造 JoinContract；风险连接默认阻断，只有显式确认后才生成有界预览。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("inspect_join 只允许异步执行")

    async def _arun(
        self,
        left_asset_id: str,
        right_asset_id: str,
        left_rows: list[dict[str, Any]],
        right_rows: list[dict[str, Any]],
        left_key: str,
        right_key: str,
        confirmed: bool = False,
        preview_limit: int = 20,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """返回连接契约，并仅在满足安全条件时生成结果预览。"""
        del run_manager
        logger.info(
            "跨源连接检查开始",
            left_asset_id=left_asset_id,
            right_asset_id=right_asset_id,
            left_rows=len(left_rows),
            right_rows=len(right_rows),
            confirmed=confirmed,
        )
        if not left_asset_id.strip() or not right_asset_id.strip():
            raise ValueError("两侧资产 ID 不能为空")
        if not 1 <= preview_limit <= 100:
            raise ValueError("preview_limit 必须在 1 到 100 之间")
        contract = build_join_contract(
            left_asset_id,
            right_asset_id,
            left_rows,
            right_rows,
            left_key,
            right_key,
        )
        blocked = contract.requires_confirmation and not confirmed
        preview = (
            []
            if blocked
            else _inner_join_preview(
                left_rows,
                right_rows,
                left_key,
                right_key,
                preview_limit,
            )
        )
        decision = (
            "blocked" if blocked else "confirmed" if contract.requires_confirmation else "ready"
        )
        result = {
            "decision": decision,
            "contract": contract.to_dict(),
            "join_preview": preview,
            "preview_truncated": not blocked and contract.matched_rows > len(preview),
            "limitations": [
                "该工具只生成内连接预览，不写回任何资产。",
                "连接键的业务语义仍需由调用方确认。",
            ],
        }
        logger.info(
            "跨源连接检查完成",
            decision=decision,
            cardinality=contract.cardinality,
            match_rate=contract.match_rate,
            preview_rows=len(preview),
        )
        return result


def _inner_join_preview(
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    left_key: str,
    right_key: str,
    limit: int,
) -> list[dict[str, Any]]:
    """生成有界内连接预览，并为冲突的右侧字段加前缀。"""
    right_index: dict[Any, list[dict[str, Any]]] = {}
    for row in right_rows:
        key = row.get(right_key)
        if key is None:
            continue
        try:
            right_index.setdefault(key, []).append(row)
        except TypeError as exc:
            raise ValueError(f"右侧连接键不可哈希: {right_key}") from exc
    output: list[dict[str, Any]] = []
    for left in left_rows:
        key = left.get(left_key)
        try:
            matches = right_index.get(key, [])
        except TypeError as exc:
            raise ValueError(f"左侧连接键不可哈希: {left_key}") from exc
        for right in matches:
            merged = dict(left)
            for column, value in right.items():
                target = f"right.{column}" if column in merged and column != right_key else column
                merged[target] = value
            output.append(merged)
            if len(output) >= limit:
                return output
    return output


_TOOLS: dict[str, BaseTool] = {"inspect_join": InspectJoinTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
