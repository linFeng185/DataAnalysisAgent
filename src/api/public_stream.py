"""从受约束结构化输出中提取可公开正文，不透传原始推理。"""

from __future__ import annotations

import json

PUBLIC_SECTION_FIELDS = frozenset(
    {
        "insights",
        "related_metrics",
        "business_impacts",
        "possible_causes",
        "recommended_actions",
        "limitations",
        "data_quality",
    }
)


# 提取已开始生成的字符串数组，未完成的末项仅公开可解码前缀。
# Args: text - JSON 数组起始位置后的文本。
# Returns: 已完成项和当前末项，不接受对象或内部结构。
def _partial_list(text: str) -> list[str]:
    decoder = json.JSONDecoder()
    index = 1
    items = []
    while index < len(text) and len(items) < 30:
        while index < len(text) and text[index] in " ,\r\n\t":
            index += 1
        if text[index : index + 1] != '"':
            break
        try:
            value, index = decoder.raw_decode(text, index)
            items.append(value)
        except ValueError:
            partial = _partial_string(text[index + 1 :])
            if partial:
                items.append(partial)
            break
    return items


# 从分析 JSON 的顶层白名单读取正在生成的洞察与建议。
# Args: text - 累计模型正文；node - 所属业务节点。
# Returns: 可合并到分析结果的公开章节，不含 summary 或内部字段。
def public_sections(text: str, node: str) -> dict[str, list[str]]:
    if node != "analyze_result":
        return {}
    source = text.lstrip()
    if not source.startswith("{"):
        return {}
    decoder = json.JSONDecoder()
    index = 1
    result = {}
    while index < len(source):
        while index < len(source) and source[index] in " ,\r\n\t":
            index += 1
        try:
            key, index = decoder.raw_decode(source, index)
        except ValueError:
            break
        while index < len(source) and source[index].isspace():
            index += 1
        if source[index : index + 1] != ":":
            break
        index += 1
        while index < len(source) and source[index].isspace():
            index += 1
        if key in PUBLIC_SECTION_FIELDS and source[index : index + 1] == "[":
            result[key] = _partial_list(source[index:])
        try:
            _, index = decoder.raw_decode(source, index)
        except ValueError:
            break
    return result


# 读取尚未闭合的 JSON 字符串，完整转义才可进入正文。
# Args: text - 从字符串开引号之后开始的内容。
# Returns: 当前可解码的字符串前缀。
def _partial_string(text: str) -> str:
    output = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == '"':
            break
        if char != "\\":
            if ord(char) < 32:
                break
            output.append(char)
            index += 1
            continue
        if index + 1 >= len(text):
            break
        length = 6 if text[index + 1] == "u" else 2
        if index + length > len(text):
            break
        try:
            value = json.loads('"' + text[index : index + length] + '"')
            if value and 0xD800 <= ord(value[0]) <= 0xDBFF:
                if index + 12 > len(text):
                    break
                value = json.loads('"' + text[index : index + 12] + '"')
                length = 12
            if any(0xD800 <= ord(part) <= 0xDFFF for part in value):
                break
            output.append(value)
        except ValueError, json.JSONDecodeError:
            break
        index += length
    return "".join(output)


# 从公开业务节点的顶层 summary/answer 字符串提取当前正文。
# Args: text - 当前模型调用累计正文；node - 业务节点。
# Returns: 已解码正文；内部节点、嵌套字段和非法前缀返回空。
def public_text(text: str, node: str) -> str:
    """顶层键解析避免误把工具参数或嵌套 summary 当成用户回答。"""
    field = {"analyze_result": "summary", "llm_direct_answer": "answer"}.get(node)
    if not field:
        return ""
    source = text.lstrip()
    if not source.startswith("{"):
        return ""
    decoder = json.JSONDecoder()
    index = 1
    while index < len(source):
        while index < len(source) and source[index] in " \r\n\t,":
            index += 1
        try:
            key, index = decoder.raw_decode(source, index)
        except ValueError:
            return ""
        while index < len(source) and source[index].isspace():
            index += 1
        if index >= len(source) or source[index] != ":":
            return ""
        index += 1
        while index < len(source) and source[index].isspace():
            index += 1
        if key == field:
            return _partial_string(source[index + 1 :]) if source[index : index + 1] == '"' else ""
        try:
            _, index = decoder.raw_decode(source, index)
        except ValueError:
            return ""
    return ""
