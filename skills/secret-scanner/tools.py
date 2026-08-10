"""工程文本敏感凭据扫描工具。"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_PATTERNS: list[tuple[str, str, re.Pattern[str], int]] = [
    (
        "private_key",
        "critical",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]{0,8192}?-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
        ),
        0,
    ),
    (
        "jwt",
        "high",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        0,
    ),
    ("cloud_access_key", "critical", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), 0),
    ("source_control_token", "critical", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,255}\b"), 0),
    (
        "connection_uri",
        "critical",
        re.compile(
            r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s/'\"]+:[^\s@/'\"]+@[^\s'\"]+"
        ),
        0,
    ),
    (
        "assigned_credential",
        "high",
        re.compile(
            r"(?i)\b(?:api[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token|password|passwd)\b\s*[:=]\s*['\"]?([A-Za-z0-9_./+=:-]{8,})"
        ),
        1,
    ),
]


class ScanSecretsTool(BaseTool):
    """扫描代码、配置和日志文本且绝不返回凭据原值。"""

    name: str = "scan_secrets"
    description: str = (
        "扫描代码、配置和日志中的私钥、令牌、连接串和凭据赋值特征，仅返回不可逆指纹。"
    )

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("scan_secrets 只允许异步执行")

    async def _arun(
        self,
        documents: list[dict[str, Any]],
        allowlist_hashes: list[str] | None = None,
        max_findings: int = 200,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """返回有界凭据特征位置和截断哈希。"""
        del run_manager
        allowlist = set(allowlist_hashes or [])
        if any(not re.fullmatch(r"[0-9a-f]{16}", item) for item in allowlist):
            raise ValueError("allowlist_hashes 必须是 16 位小写十六进制指纹")
        if not 1 <= max_findings <= 1000:
            raise ValueError("max_findings 必须在 1 到 1000 之间")
        logger.info(
            "工程凭据扫描开始",
            document_count=len(documents),
            allowlist_count=len(allowlist),
            max_findings=max_findings,
        )
        findings: list[dict[str, Any]] = []
        suppressed_count = 0
        scanned_characters = 0
        for index, document in enumerate(documents):
            if not isinstance(document, dict):
                raise ValueError(f"documents 第 {index} 项必须是对象")
            source_id = str(document.get("source_id", "")).strip()
            text = document.get("text")
            locator = document.get("locator") or {}
            if not source_id or not isinstance(text, str):
                raise ValueError(f"documents 第 {index} 项必须提供 source_id 和文本 text")
            if not isinstance(locator, dict):
                raise ValueError(f"documents 第 {index} 项的 locator 必须是对象")
            scanned_characters += len(text)
            seen: set[tuple[int, int, str]] = set()
            for category, severity, pattern, group_index in _PATTERNS:
                for match in pattern.finditer(text):
                    start, end = match.span(group_index)
                    key = (start, end, category)
                    if key in seen:
                        continue
                    seen.add(key)
                    value = match.group(group_index)
                    fingerprint = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
                    if fingerprint in allowlist:
                        suppressed_count += 1
                        continue
                    findings.append(
                        {
                            "source_id": source_id,
                            "locator": locator,
                            "category": category,
                            "severity": severity,
                            "start": start,
                            "end": end,
                            "fingerprint": fingerprint,
                            "reason": _reason(category),
                        }
                    )

        findings.sort(key=lambda item: (item["source_id"], item["start"], item["category"]))
        counts = Counter(item["category"] for item in findings)
        result = {
            "contains_sensitive_material": bool(findings),
            "summary": {
                "document_count": len(documents),
                "scanned_characters": scanned_characters,
                "total_findings": len(findings),
                "suppressed_findings": suppressed_count,
            },
            "category_counts": dict(sorted(counts.items())),
            "findings": findings[:max_findings],
            "findings_truncated": len(findings) > max_findings,
            "raw_values_returned": False,
            "action": "立即撤销并轮换真实凭据；本工具只负责检测，不修改原始文本或配置",
        }
        logger.info(
            "工程凭据扫描完成",
            contains_sensitive_material=result["contains_sensitive_material"],
            finding_count=len(findings),
            suppressed_count=suppressed_count,
        )
        return result


def _reason(category: str) -> str:
    """返回敏感材料类别的中文原因。"""
    return {
        "private_key": "文本包含私钥块边界和内容",
        "jwt": "文本包含三段式签名令牌特征",
        "cloud_access_key": "文本包含云访问密钥标识特征",
        "source_control_token": "文本包含源码平台访问令牌特征",
        "connection_uri": "连接地址包含内联用户名和凭据",
        "assigned_credential": "敏感配置字段被直接赋予高风险字符串",
    }[category]


_TOOLS: dict[str, BaseTool] = {"scan_secrets": ScanSecretsTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
