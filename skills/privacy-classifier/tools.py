"""常见敏感字段与敏感值分类工具。"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections import Counter, defaultdict
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
_PHONE = re.compile(r"^(?:\+?86[- ]?)?1[3-9]\d{9}$")
_ID_CARD = re.compile(r"^\d{17}[0-9Xx]$")
_BANK_CARD = re.compile(r"^\d{13,19}$")
_NAME_HINTS: dict[str, tuple[str, ...]] = {
    "email": ("email", "e_mail", "mail", "邮箱", "电子邮件"),
    "phone": ("phone", "mobile", "tel", "手机号", "电话", "联系方式"),
    "id_card": ("id_card", "identity", "身份证", "证件号", "证件号码"),
    "bank_card": ("bank_card", "card_number", "银行卡", "银行卡号"),
    "ip_address": ("ip", "ip_address", "ip地址"),
    "credential": (
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "access_key",
        "密码",
        "密钥",
    ),
    "person_name": ("real_name", "full_name", "姓名", "客户名", "联系人"),
    "address": ("address", "住址", "地址", "收货地址"),
}
_SENSITIVITY = {
    "email": "high",
    "phone": "high",
    "id_card": "critical",
    "bank_card": "critical",
    "ip_address": "medium",
    "credential": "critical",
    "person_name": "high",
    "address": "high",
}


class ClassifyPrivacyTool(BaseTool):
    """只分类敏感信息并返回不可逆样例哈希。"""

    name: str = "classify_privacy"
    description: str = "按字段名和确定性格式识别敏感信息类别、数量和置信度，不执行脱敏。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("classify_privacy 只允许异步执行")

    async def _arun(
        self,
        rows: list[dict[str, Any]],
        columns: list[str] | None = None,
        max_sample_hashes: int = 3,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """分类字段和值且绝不在输出或日志中回显敏感原值。"""
        del run_manager
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("rows 中的每一项都必须是对象")
        if not 0 <= max_sample_hashes <= 10:
            raise ValueError("max_sample_hashes 必须在 0 到 10 之间")
        available = {str(key) for row in rows for key in row}
        selected = sorted(available) if columns is None else columns
        if len(set(selected)) != len(selected):
            raise ValueError("columns 不能包含重复字段")
        missing = [column for column in selected if column not in available]
        if missing:
            raise ValueError(f"待分类字段不存在: {', '.join(missing)}")
        logger.info(
            "隐私分类开始",
            row_count=len(rows),
            column_count=len(selected),
        )

        detections: list[dict[str, Any]] = []
        category_counts: Counter[str] = Counter()
        for column in selected:
            values = [row.get(column) for row in rows if row.get(column) not in (None, "")]
            hints = _name_categories(column)
            value_matches: dict[str, list[str]] = defaultdict(list)
            for value in values:
                for category in _value_categories(value):
                    value_matches[category].append(_hash_value(value))
            categories = sorted(set(hints) | set(value_matches))
            for category in categories:
                matched_hashes = sorted(set(value_matches.get(category, [])))
                name_match = category in hints
                value_count = len(value_matches.get(category, []))
                if not value_count and not name_match:
                    continue
                if category == "credential" and name_match:
                    value_count = len(values)
                    matched_hashes = sorted({_hash_value(value) for value in values})
                confidence = 0.99 if name_match and value_count else 0.9 if value_count else 0.75
                reasons: list[str] = []
                if name_match:
                    reasons.append("字段名命中敏感语义")
                if value_count:
                    reasons.append("字段值命中确定性格式")
                detections.append(
                    {
                        "column": column,
                        "category": category,
                        "sensitivity": _SENSITIVITY[category],
                        "confidence": confidence,
                        "match_count": value_count,
                        "non_null_count": len(values),
                        "reasons": reasons,
                        "sample_hashes": matched_hashes[:max_sample_hashes],
                    }
                )
                category_counts[category] += value_count

        critical_columns = sorted(
            {item["column"] for item in detections if item["sensitivity"] == "critical"}
        )
        sensitive_columns = sorted({item["column"] for item in detections})
        result = {
            "contains_sensitive_data": bool(detections),
            "classified_columns": len(selected),
            "sensitive_columns": sensitive_columns,
            "critical_columns": critical_columns,
            "detections": detections,
            "category_match_counts": dict(sorted(category_counts.items())),
            "raw_values_returned": False,
            "action": "仅完成分类；实际脱敏、阻断和授权判断必须交由核心安全层执行",
        }
        logger.info(
            "隐私分类完成",
            contains_sensitive_data=result["contains_sensitive_data"],
            sensitive_column_count=len(sensitive_columns),
            critical_column_count=len(critical_columns),
        )
        return result


def _name_categories(column: str) -> set[str]:
    """根据规范化字段名匹配敏感语义。"""
    normalized = column.strip().casefold()
    return {
        category
        for category, hints in _NAME_HINTS.items()
        if any(normalized == hint or normalized.endswith(f"_{hint}") for hint in hints)
    }


def _value_categories(value: Any) -> set[str]:
    """根据严格格式匹配敏感值类别。"""
    if not isinstance(value, str):
        return set()
    normalized = re.sub(r"[ -]", "", value.strip())
    categories: set[str] = set()
    if _EMAIL.fullmatch(value.strip()):
        categories.add("email")
    if _PHONE.fullmatch(normalized):
        categories.add("phone")
    if _ID_CARD.fullmatch(normalized) and _valid_cn_id_checksum(normalized):
        categories.add("id_card")
    if _BANK_CARD.fullmatch(normalized) and _valid_luhn(normalized):
        categories.add("bank_card")
    try:
        ipaddress.ip_address(value.strip())
        categories.add("ip_address")
    except ValueError:
        pass
    return categories


def _valid_cn_id_checksum(value: str) -> bool:
    """校验十八位中国居民身份证校验码。"""
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    checks = "10X98765432"
    total = sum(int(digit) * weight for digit, weight in zip(value[:17], weights, strict=True))
    return checks[total % 11] == value[-1].upper()


def _valid_luhn(value: str) -> bool:
    """使用 Luhn 算法校验银行卡号。"""
    total = 0
    parity = len(value) % 2
    for index, character in enumerate(value):
        digit = int(character)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _hash_value(value: Any) -> str:
    """返回不可逆且有界的敏感值样例指纹。"""
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return digest[:16]


_TOOLS: dict[str, BaseTool] = {"classify_privacy": ClassifyPrivacyTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
