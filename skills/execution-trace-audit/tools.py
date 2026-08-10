"""工作流执行轨迹审计工具。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_STATUSES = {"success", "error", "cancelled"}


class AuditExecutionTraceTool(BaseTool):
    """审计节点跨度的顺序、耗时、重试、错误恢复和状态写回。"""

    name: str = "audit_execution_trace"
    description: str = "检查工作流节点跨度的顺序、耗时、重试、错误恢复和状态写回完整性。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("audit_execution_trace 只允许异步执行")

    async def _arun(
        self,
        spans: list[dict[str, Any]],
        expected_nodes: list[str] | None = None,
        required_state_fields: list[str] | None = None,
        max_node_duration_ms: float = 30000.0,
        max_retries: int = 3,
        max_findings: int = 200,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """返回轨迹完整性结论和有界问题明细。"""
        del run_manager
        expected_nodes = expected_nodes or []
        required_state_fields = required_state_fields or []
        _validate_options(
            expected_nodes, required_state_fields, max_node_duration_ms, max_retries, max_findings
        )
        logger.info(
            "执行轨迹审计开始",
            span_count=len(spans),
            expected_node_count=len(expected_nodes),
            required_state_field_count=len(required_state_fields),
        )
        findings: list[dict[str, Any]] = []
        total_findings = 0
        total_errors = 0
        total_warnings = 0

        def add(code: str, severity: str, path: str, message: str) -> None:
            nonlocal total_findings, total_errors, total_warnings
            total_findings += 1
            total_errors += severity == "error"
            total_warnings += severity == "warning"
            if len(findings) < max_findings:
                findings.append(
                    {
                        "code": code,
                        "severity": severity,
                        "path": path,
                        "message": message,
                    }
                )

        normalized: list[dict[str, Any]] = []
        sequence_seen: set[int] = set()
        span_ids: set[str] = set()
        for index, span in enumerate(spans):
            if not isinstance(span, dict):
                raise ValueError(f"spans 第 {index} 项必须是对象")
            normalized_span = _normalize_span(span, index)
            sequence = normalized_span["sequence"]
            span_id = normalized_span["span_id"]
            if sequence in sequence_seen:
                add("DUPLICATE_SEQUENCE", "error", f"spans[{index}].sequence", "执行序号重复")
            sequence_seen.add(sequence)
            if span_id in span_ids:
                add("DUPLICATE_SPAN_ID", "error", f"spans[{index}].span_id", "跨度标识重复")
            span_ids.add(span_id)
            normalized.append(normalized_span)

        original_sequences = [span["sequence"] for span in normalized]
        if original_sequences != sorted(original_sequences):
            add("OUT_OF_ORDER", "error", "spans", "跨度没有按执行序号排列")
        if original_sequences and sorted(original_sequences) != list(
            range(min(original_sequences), max(original_sequences) + 1)
        ):
            add("SEQUENCE_GAP", "error", "spans", "执行序号存在断档")

        by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
        state_fields_written: set[str] = set()
        total_duration = 0.0
        for index, span in enumerate(normalized):
            by_node[span["node"]].append(span)
            duration = span["ended_ms"] - span["started_ms"]
            total_duration += max(duration, 0)
            if duration < 0:
                add("NEGATIVE_DURATION", "error", f"spans[{index}]", "结束时间早于开始时间")
            elif duration > max_node_duration_ms:
                add("DURATION_EXCEEDED", "warning", f"spans[{index}]", "节点耗时超过预算")
            state_fields_written.update(span["state_writes"])

        node_results: list[dict[str, Any]] = []
        for node in sorted(by_node):
            node_spans = sorted(by_node[node], key=lambda item: (item["attempt"], item["sequence"]))
            attempts = [item["attempt"] for item in node_spans]
            unique_attempts = sorted(set(attempts))
            expected_attempts = list(range(1, max(unique_attempts, default=0) + 1))
            if unique_attempts != expected_attempts:
                add("ATTEMPT_GAP", "error", f"nodes.{node}", "节点尝试次数不连续")
            retries = max(unique_attempts, default=1) - 1
            if retries > max_retries:
                add("RETRY_LIMIT_EXCEEDED", "error", f"nodes.{node}", "节点重试次数超过预算")
            successes = [item for item in node_spans if item["status"] == "success"]
            terminal_success = bool(successes) and successes[-1] == node_spans[-1]
            if not terminal_success:
                add("UNRECOVERED_NODE", "error", f"nodes.{node}", "节点没有以成功状态结束")
            elif any(item["status"] == "error" for item in node_spans[:-1]):
                add("RECOVERED_ERROR", "warning", f"nodes.{node}", "节点发生错误后重试恢复")
            node_results.append(
                {
                    "node": node,
                    "attempts": len(unique_attempts),
                    "retries": retries,
                    "terminal_status": node_spans[-1]["status"],
                    "duration_ms": round(
                        sum(max(item["ended_ms"] - item["started_ms"], 0) for item in node_spans),
                        6,
                    ),
                    "state_fields_written": sorted(
                        {field for item in node_spans for field in item["state_writes"]}
                    ),
                }
            )

        missing_nodes = sorted(set(expected_nodes) - set(by_node))
        for node in missing_nodes:
            add("EXPECTED_NODE_MISSING", "error", f"nodes.{node}", "预期节点没有执行记录")
        missing_state_fields = sorted(set(required_state_fields) - state_fields_written)
        for field in missing_state_fields:
            add("STATE_WRITE_MISSING", "error", f"state.{field}", "必需状态字段没有写回记录")

        trace_source = json.dumps(
            spans, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
        )
        result = {
            "valid": total_errors == 0,
            "summary": {
                "span_count": len(spans),
                "node_count": len(by_node),
                "errors": total_errors,
                "warnings": total_warnings,
                "total_findings": total_findings,
            },
            "total_duration_ms": round(total_duration, 6),
            "node_results": node_results,
            "missing_nodes": missing_nodes,
            "missing_state_fields": missing_state_fields,
            "findings": findings,
            "findings_truncated": total_findings > len(findings),
            "trace_fingerprint": hashlib.sha256(trace_source.encode("utf-8")).hexdigest(),
        }
        logger.info(
            "执行轨迹审计完成",
            valid=result["valid"],
            error_count=total_errors,
            warning_count=total_warnings,
            findings_truncated=result["findings_truncated"],
        )
        return result


def _validate_options(
    expected_nodes: list[str],
    required_state_fields: list[str],
    max_node_duration_ms: float,
    max_retries: int,
    max_findings: int,
) -> None:
    """校验轨迹审计参数。"""
    if len(set(expected_nodes)) != len(expected_nodes):
        raise ValueError("expected_nodes 不能包含重复节点")
    if len(set(required_state_fields)) != len(required_state_fields):
        raise ValueError("required_state_fields 不能包含重复字段")
    if max_node_duration_ms <= 0:
        raise ValueError("max_node_duration_ms 必须大于零")
    if not 0 <= max_retries <= 20:
        raise ValueError("max_retries 必须在 0 到 20 之间")
    if not 1 <= max_findings <= 1000:
        raise ValueError("max_findings 必须在 1 到 1000 之间")


def _normalize_span(span: dict[str, Any], index: int) -> dict[str, Any]:
    """校验并规范化单条节点跨度。"""
    span_id = str(span.get("span_id", "")).strip()
    node = str(span.get("node", "")).strip()
    status = str(span.get("status", "")).strip()
    if not span_id or not node:
        raise ValueError(f"spans 第 {index} 项必须提供 span_id 和 node")
    if status not in _STATUSES:
        raise ValueError(f"spans 第 {index} 项包含不支持的状态: {status}")
    sequence = span.get("sequence")
    attempt = span.get("attempt", 1)
    started_ms = span.get("started_ms")
    ended_ms = span.get("ended_ms")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ValueError(f"spans 第 {index} 项的 sequence 必须是正整数")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValueError(f"spans 第 {index} 项的 attempt 必须是正整数")
    if not _is_number(started_ms) or not _is_number(ended_ms):
        raise ValueError(f"spans 第 {index} 项的时间必须是数值")
    state_writes = span.get("state_writes") or []
    if not isinstance(state_writes, list) or any(not str(item).strip() for item in state_writes):
        raise ValueError(f"spans 第 {index} 项的 state_writes 必须是字段名数组")
    return {
        "span_id": span_id,
        "node": node,
        "sequence": sequence,
        "attempt": attempt,
        "status": status,
        "started_ms": float(started_ms),
        "ended_ms": float(ended_ms),
        "state_writes": [str(item) for item in state_writes],
    }


def _is_number(value: Any) -> bool:
    """排除布尔值后判断数值。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


_TOOLS: dict[str, BaseTool] = {"audit_execution_trace": AuditExecutionTraceTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
