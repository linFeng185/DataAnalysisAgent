"""30.1 键列结构判断 — 有界模型判断 + 确定性回退（spec/55 §4 与 §14）。

判断输入只包含授权表结构（表名/列名/类型/注释/主键标记），不含用户问题与样例值；
模型不可用、超时、失败或输出非法时回退到确定性结构启发式（主键、索引、关系 join_key、
`*_id`/`*_code` 命名），回退只是语义候选，绝不宣称真实唯一。结果按
（tenant_id, user_id, datasource, 授权结构/权限指纹, 模型指纹, Prompt 版本）缓存，
TTL 与容量有界，不跨身份复用。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import threading
import time
from typing import Any

from src.config import get_settings
from src.llm.output_contracts import SchemaKeyDetectionOutput
from src.llm.prompt_budget import PromptSection
from src.llm.prompts import get_prompt_definition
from src.logging_config import get_logger

logger = get_logger(__name__)

# 服务端运行配置中启用 SQL 智能（键列判断/画像/探查）的标记键；
# 只由服务端聊天入口写入 RunnableConfig.configurable，不接受请求正文覆写。
SQL_INTELLIGENCE_CONFIG_KEY = "enable_sql_intelligence"
# 键列判断的任务标识与注册 Prompt ID。
SCHEMA_KEY_TASK = "schema_key_detection"
SCHEMA_KEY_PROMPT_ID = "schema.keys"
# 每表固定四键：键列 / 展示列 / 外键 / 画像候选列。
_HINT_FIELDS = ("key_columns", "display_columns", "foreign_keys", "profile_columns")
# 单条注释进入结构文本的长度上限，避免超长注释挤占判断预算。
_MAX_COMMENT_CHARS = 60
# 结构文本整体长度上限；超出部分由 Prompt 预算器的尾部截断兜底。
_MAX_STRUCTURE_CHARS = 12000
# 默认配置值：与 spec/55 §9 一致；字段由配置任务补入 src/config.py，读取时缺失即取默认。
_DEFAULT_SETTINGS = {
    "enabled": True,
    "max_tokens": 800,
    "timeout": 3.0,
    "cache_ttl": 300.0,
    "cache_entries": 128,
}

# 进程内键列缓存：key -> (过期单调时间, 结果副本)。
_CACHE: dict[tuple, tuple[float, dict[str, dict[str, list[str]]]]] = {}
_CACHE_LOCK = threading.Lock()


# 方法作用：读取键列判断相关配置，字段缺失时使用 spec 默认值。
# Args: 无。
# Returns: enabled/max_tokens/timeout/cache_ttl/cache_entries 配置快照。
def _schema_key_settings() -> dict[str, Any]:
    """配置字段由配置任务添加；此处只在缺失时取 spec 默认，不改配置模块。"""
    settings = get_settings()
    return {
        "enabled": bool(getattr(settings, "schema_key_detection_enabled", _DEFAULT_SETTINGS["enabled"])),
        "max_tokens": int(getattr(settings, "schema_key_detection_max_tokens", _DEFAULT_SETTINGS["max_tokens"]) or 0)
        or int(_DEFAULT_SETTINGS["max_tokens"]),
        "timeout": float(getattr(settings, "schema_key_detection_timeout_seconds", _DEFAULT_SETTINGS["timeout"]) or 0)
        or float(_DEFAULT_SETTINGS["timeout"]),
        "cache_ttl": float(getattr(settings, "sql_profiling_ttl_seconds", _DEFAULT_SETTINGS["cache_ttl"]) or 0)
        or float(_DEFAULT_SETTINGS["cache_ttl"]),
        "cache_entries": int(getattr(settings, "sql_profiling_cache_entries", _DEFAULT_SETTINGS["cache_entries"]) or 0)
        or int(_DEFAULT_SETTINGS["cache_entries"]),
    }


# 方法作用：读取单调时钟，保留测试可替换的模块级边界。
# Args: 无。
# Returns: 当前单调秒数。
def _monotonic() -> float:
    return time.monotonic()


# 方法作用：延迟检查键列判断任务是否存在可用模型。
# Args: task - 任务标识。
# Returns: 有可用模型时返回 True。
def _is_task_llm_available(task: str) -> bool:
    from src.llm.client import is_task_llm_available

    return is_task_llm_available(task)


# 方法作用：延迟创建键列判断模型，保留测试可替换的模块级边界。
# Args: task - 任务标识；kwargs - 模型创建参数。
# Returns: 任务对应的 ChatModel。
def _get_task_llm(task: str, **kwargs: Any) -> Any:
    from src.llm.client import get_task_llm

    return get_task_llm(task, **kwargs)


# 方法作用：解析当前生效的模型指纹，供缓存键与身份隔离使用。
# Args: 无。
# Returns: 租户选择或本地模型配置的稳定指纹字符串。
def _model_fingerprint() -> str:
    try:
        from src.llm.tenant_config import get_current_tenant_llm_selection

        selection = get_current_tenant_llm_selection()
    except Exception:
        selection = None
    if selection is not None:
        return f"tenant:{selection.tenant_id}:conn:{selection.connection_id}:model:{selection.model_id}"
    settings = get_settings()
    return "local:{provider}:{model}:{local}".format(
        provider=getattr(settings, "llm_provider", ""),
        model=getattr(settings, "llm_model", ""),
        local=getattr(settings, "local_llm_model", ""),
    )


# 方法作用：读取当前注册的键列判断 Prompt 版本，供缓存键使用。
# Args: 无。
# Returns: Prompt 版本串；未注册时返回 "0"。
def _prompt_version() -> str:
    try:
        return get_prompt_definition(SCHEMA_KEY_PROMPT_ID).version
    except KeyError:
        logger.warning("键列判断 Prompt 未注册", prompt_id=SCHEMA_KEY_PROMPT_ID)
        return "0"


# 方法作用：清空进程内键列缓存（测试与权限变更场景使用）。
# Args: 无。
# Returns: 无返回值。
def clear_schema_key_cache() -> None:
    """清空缓存不影响任何持久状态；正常请求不需要调用。"""
    with _CACHE_LOCK:
        _CACHE.clear()


# 方法作用：按 TTL 读取缓存副本，过期条目即时剔除。
# Args: key - 缓存键。
# Returns: 结果深拷贝；未命中或已过期时返回 None。
def _cache_get(key: tuple) -> dict[str, dict[str, list[str]]] | None:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if _monotonic() >= expires_at:
            _CACHE.pop(key, None)
            return None
        return copy.deepcopy(value)


# 方法作用：写入缓存并按容量上限淘汰最旧条目。
# Args: key - 缓存键；value - 判断结果；ttl - 有效期秒数；capacity - 容量上限。
# Returns: 无返回值。
def _cache_set(key: tuple, value: dict[str, dict[str, list[str]]], ttl: float, capacity: int) -> None:
    if ttl <= 0:
        return
    with _CACHE_LOCK:
        _CACHE[key] = (_monotonic() + ttl, copy.deepcopy(value))
        limit = max(1, int(capacity))
        while len(_CACHE) > limit:
            oldest = next(iter(_CACHE))
            _CACHE.pop(oldest, None)


# 方法作用：读取字典或对象上的同名字段，兼容原始表结构与 TableSchema 契约。
# Args: value - 字典或对象；field - 字段名；default - 缺省值。
# Returns: 字段值；缺失时返回 default。
def _field(value: Any, field: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(field, default)
    return getattr(value, field, default)


# 方法作用：把单列结构归一为不含样例值的结构字典。
# Args: column - ColumnInfo 或原始列字典。
# Returns: {name,type,comment,is_nullable,is_primary_key,is_indexed}；无列名时返回 None。
def _column_mapping(column: Any) -> dict[str, Any] | None:
    name = str(_field(column, "name", "") or "").strip()
    if not name:
        return None
    return {
        "name": name,
        "type": str(_field(column, "type", "") or ""),
        "comment": str(_field(column, "comment", "") or ""),
        "is_nullable": bool(_field(column, "is_nullable", True)),
        "is_primary_key": bool(_field(column, "is_primary_key", False)),
        "is_indexed": bool(_field(column, "is_indexed", False)),
    }


# 方法作用：把单表结构归一为不含样例值的结构字典。
# Args: table - TableSchema 或原始表字典。
# Returns: {name,description,columns,relations} 结构字典。
def _table_mapping(table: Any) -> dict[str, Any]:
    columns = [
        mapping
        for mapping in (
            _column_mapping(column)
            for column in (_field(table, "columns", None) or [])
        )
        if mapping is not None
    ]
    relations = [
        {
            "relation_type": str(_field(relation, "relation_type", "") or ""),
            "target_table": str(_field(relation, "target_table", "") or ""),
            "join_key": str(_field(relation, "join_key", "") or ""),
        }
        for relation in (_field(table, "relations", None) or [])
    ]
    return {
        "name": str(_field(table, "name", "") or ""),
        "description": str(_field(table, "description", "") or ""),
        "columns": columns,
        "relations": relations,
    }


# 方法作用：判断列是否在授权列集合内（空白名单表示不额外限制）。
# Args: allowed_columns - 授权列集合，支持裸列名或 table.column；table_name - 表名；column_name - 列名。
# Returns: 授权时返回 True；大小写不同的物理列不共享权限。
def column_authorized(allowed_columns: list[str] | None, table_name: str, column_name: str) -> bool:
    """与画像模块的列白名单判定保持同一语义：精确比较标识符，禁止大小写变体借用权限。"""
    allowed = {str(item).strip() for item in (allowed_columns or []) if str(item).strip()}
    if not allowed:
        return True
    return column_name in allowed or f"{table_name}.{column_name}" in allowed


# 方法作用：把物理标识符匹配到候选中的规范拼写，大小写歧义时宁缺勿借。
# Args: candidates - 候选物理名序列（保持原始大小写）；raw_name - 待匹配的名称。
# Returns: 唯一匹配到的候选名；无匹配或大小写歧义时返回 None。
def match_identifier(candidates: list[str] | tuple[str, ...], raw_name: Any) -> str | None:
    """先精确匹配；大小写不敏感回退只在候选唯一时生效，多个变体说明物理身份不唯一。"""
    text = str(raw_name or "").strip()
    if not text:
        return None
    if text in candidates:
        return text
    folded = [candidate for candidate in candidates if candidate.casefold() == text.casefold()]
    return folded[0] if len(folded) == 1 else None


# 方法作用：把上下文中的授权表结构归一为 表名 -> 结构字典（兼容 TableSchema）。
# Args: context - 携带 tables 字段的授权上下文（ProfilingContext 或等价对象）。
# Returns: 归一后只含授权列的表结构字典；无可用表时返回空字典。
def _authorized_tables(context: Any) -> dict[str, dict]:
    """授权过滤发生在模型输入、结构指纹、回退、缓存与裁剪保护之前，未授权列不进入判断链路。"""
    raw = getattr(context, "tables", None)
    allowed_columns = list(getattr(context, "allowed_columns", None) or [])
    items: list[tuple[Any, Any]] = []
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        items = [(None, table) for table in raw]
    result: dict[str, dict] = {}
    for name, table in items:
        mapping = _table_mapping(table)
        table_name = str(name or mapping["name"] or "").strip()
        if not table_name:
            continue
        mapping["name"] = table_name
        mapping["columns"] = [
            column for column in mapping["columns"]
            if column_authorized(allowed_columns, table_name, str(column.get("name", "")))
        ]
        if not mapping["columns"]:
            logger.debug("键列判断跳过无授权列的表", table=table_name[:64])
            continue
        result[table_name] = mapping
    return result


# 方法作用：计算授权表结构的稳定指纹，覆盖列名/类型/注释与主键索引标记。
# Args: tables - 表名 -> 表结构字典。
# Returns: 16 位十六进制指纹。
def authorized_structure_fingerprint(tables: dict[str, Any]) -> str:
    """结构指纹参与缓存键与画像上下文，结构变化必须换键；兼容原始字典与 TableSchema。"""
    payload: list[list[Any]] = []
    for name in sorted(tables):
        mapping = _table_mapping(tables[name])
        columns = [
            [
                column["name"],
                column["type"],
                str(column["comment"])[:120],
                bool(column["is_primary_key"]),
                bool(column["is_indexed"]),
            ]
            for column in mapping["columns"]
        ]
        # 表名保留原始大小写：大小写不同的物理表是不同对象，指纹不能合并。
        payload.append([str(name), columns])
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


# 方法作用：计算当前请求授权列与行过滤策略的指纹。
# Args: context - 授权上下文。
# Returns: 16 位十六进制指纹。
def _permission_fingerprint(context: Any) -> str:
    # 授权标识符保留原始大小写：Secret 与 secret 是不同物理列，缓存必须分开。
    allowed = sorted(
        str(column).strip()
        for column in (getattr(context, "allowed_columns", []) or [])
        if str(column or "").strip()
    )
    payload = json.dumps(
        [allowed, str(getattr(context, "row_filter_sql", "") or "")],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# 方法作用：渲染只含授权结构的判断输入，不含样例值与任何用户问题。
# Args: tables - 表名 -> 表结构字典。
# Returns: 紧凑的结构文本；超出上限时尾部截断。
def render_structure_input(tables: dict[str, dict]) -> str:
    """样例值属于数据事实而非结构，禁止进入键列判断输入。"""
    lines: list[str] = []
    for name in sorted(tables):
        table = tables[name] if isinstance(tables[name], dict) else {}
        description = str(table.get("description", "") or "").strip()[:_MAX_COMMENT_CHARS]
        header = f"表 {name}" + (f"（{description}）" if description else "")
        lines.append(header)
        for column in table.get("columns", []) or []:
            if not isinstance(column, dict):
                continue
            column_name = str(column.get("name", "") or "").strip()
            if not column_name:
                continue
            marks: list[str] = []
            if column.get("is_primary_key"):
                marks.append("PK")
            if column.get("is_indexed"):
                marks.append("INDEX")
            marks.append("NULL" if column.get("is_nullable", True) else "NOT NULL")
            comment = str(column.get("comment", "") or "").strip()[:_MAX_COMMENT_CHARS]
            line = (
                f"- {column_name} {str(column.get('type', '') or '')} "
                f"[{', '.join(marks)}]"
            )
            if comment:
                line += f" {comment}"
            lines.append(line)
        relations = table.get("relations", []) or []
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            lines.append(
                f"  关联: {relation.get('relation_type', '')} -> "
                f"{relation.get('target_table', '')} ON {relation.get('join_key', '')}"
            )
        lines.append("")
    text = "\n".join(lines).strip()
    if len(text) > _MAX_STRUCTURE_CHARS:
        text = text[:_MAX_STRUCTURE_CHARS] + "\n...[结构已截断]"
    return text


# 方法作用：判断列名/注释是否属于编码或编号类候选。
# Args: column - 列结构字典。
# Returns: 命中编码命名特征时返回 True。
def _is_code_like(column: dict) -> bool:
    name = str(column.get("name", "") or "").strip().lower()
    if name.endswith("_code") or name.endswith("_no") or name in {"code", "no"}:
        return True
    comment = str(column.get("comment", "") or "")
    return "编码" in comment or "编号" in comment


# 方法作用：判断列名/注释是否属于实体展示名类候选。
# Args: column - 列结构字典。
# Returns: 命中展示名命名特征时返回 True。
def _is_display_like(column: dict) -> bool:
    name = str(column.get("name", "") or "").strip().lower()
    if name in {"name", "title", "label", "display_name"} or name.endswith("_name"):
        return True
    if _is_code_like(column):
        return False
    comment = str(column.get("comment", "") or "")
    return any(word in comment for word in ("名称", "名字", "标题", "标签"))


# 方法作用：从关系表达式中提取命中本表列名的关联列。
# Args: table - 表结构字典；candidates - 本表物理列名序列（保留原始大小写）。
# Returns: 关系 join_key 中出现的关联列名列表；大小写歧义列不借用。
def _relation_columns(table: dict, candidates: list[str]) -> list[str]:
    found: list[str] = []
    for relation in table.get("relations", []) or []:
        if not isinstance(relation, dict):
            continue
        join_key = str(relation.get("join_key", "") or "")
        for identifier in re.findall(r"(?:[A-Za-z_][\w$]*\.)?([A-Za-z_][\w$]*)", join_key):
            canonical = match_identifier(candidates, identifier)
            if canonical and canonical not in found:
                found.append(canonical)
    return found


# 方法作用：按列序过滤候选集合，输出稳定且与结构一致的列名列表。
# Args: columns - 表列结构列表；candidates - 候选列名集合。
# Returns: 按表内列序排列的列名列表。
def _ordered_subset(columns: list[dict], candidates: set[str]) -> list[str]:
    return [
        str(column.get("name", "") or "").strip()
        for column in columns
        if str(column.get("name", "") or "").strip() in candidates
    ]


# 方法作用：确定性结构回退：主键/索引/关系键/`*_id`/`*_code` 命名候选。
# Args: tables - 表名 -> 表结构字典。
# Returns: 每表四键的候选列；只是语义候选，不代表真实唯一。
def fallback_key_hints(tables: dict[str, dict]) -> dict[str, dict[str, list[str]]]:
    """回退只依据授权结构与命名约定，不查询数据、不下唯一性结论。"""
    result: dict[str, dict[str, list[str]]] = {}
    for name, table in tables.items():
        columns = [
            column for column in table.get("columns", []) or []
            if isinstance(column, dict) and str(column.get("name", "") or "").strip()
        ]
        # 候选集合一律使用物理列原始拼写：大小写不同的列是不同对象，不能归一后合并。
        named = [
            (str(column.get("name", "") or "").strip(), column)
            for column in columns
        ]
        primary = {name for name, column in named if column.get("is_primary_key")}
        indexed = {name for name, column in named if column.get("is_indexed")}
        code_like = {name for name, column in named if _is_code_like(column)}
        id_like = {name for name in (item[0] for item in named) if name.lower().endswith("_id")}
        display_like = {name for name, column in named if _is_display_like(column)}
        relation_columns = set(_relation_columns(table, [name for name, _ in named]))
        foreign = (relation_columns | id_like) - primary
        key_candidates = (primary | code_like | indexed | id_like) - display_like
        key_columns = _ordered_subset(columns, key_candidates)
        display_columns = _ordered_subset(columns, display_like)
        foreign_keys = _ordered_subset(columns, foreign)
        profile_columns = list(dict.fromkeys([*key_columns, *display_columns, *foreign_keys]))
        result[name] = {
            "key_columns": key_columns,
            "display_columns": display_columns,
            "foreign_keys": foreign_keys,
            "profile_columns": profile_columns,
        }
    return result


# 方法作用：读取模型输出条目中的列名列表，兼容字典与 Pydantic 两种形态。
# Args: item - 模型输出的单表条目；field - 字段名。
# Returns: 原始列名列表。
def _item_list(item: Any, field: str) -> list[str]:
    value = item.get(field) if isinstance(item, dict) else getattr(item, field, None)
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    try:
        return [str(entry) for entry in value]
    except TypeError:
        return []


# 方法作用：校验模型输出：非法表丢弃，未知列拒绝，列名归一为授权拼写。
# Args: output - 模型结构化输出；tables - 授权表结构字典。
# Returns: 表名 -> 四键候选；不含任何未授权表或列。
def normalize_model_hints(
    output: Any,
    tables: dict[str, dict],
) -> dict[str, dict[str, list[str]]]:
    """未知列/伪造表一律拒绝，绝不把模型编造的列写进裁剪保护集合。"""
    table_names = list(tables)
    result: dict[str, dict[str, list[str]]] = {}
    items = getattr(output, "tables", None)
    if items is None and isinstance(output, dict):
        items = output.get("tables")
    for item in list(items or []):
        raw_name = item.get("table") if isinstance(item, dict) else getattr(item, "table", "")
        # 表名先精确匹配；大小写不敏感回退只在授权表唯一时生效，歧义表整体拒绝。
        canonical_table = match_identifier(table_names, raw_name)
        if canonical_table is None:
            logger.debug("键列判断拒绝伪造表或歧义表", table=str(raw_name or "")[:64])
            continue
        columns = [
            column for column in tables[canonical_table].get("columns", []) or []
            if isinstance(column, dict) and str(column.get("name", "") or "").strip()
        ]
        # 列名同样保留物理身份：Secret 与 secret 并存时不得凭模糊匹配借用另一列。
        column_names = [str(column.get("name", "") or "").strip() for column in columns]
        entry: dict[str, list[str]] = {}
        for field in _HINT_FIELDS:
            normalized: list[str] = []
            for column_name in _item_list(item, field):
                canonical = match_identifier(column_names, column_name)
                if canonical and canonical not in normalized:
                    normalized.append(canonical)
            entry[field] = normalized
        result[canonical_table] = entry
    return result


# 方法作用：把模型候选与确定性回退合并，保证每表四键齐全且不丢结构候选。
# Args: fallback - 回退结果；model_hints - 已校验的模型结果。
# Returns: 每表固定四键的最终候选。
def _merge_hints(
    fallback: dict[str, dict[str, list[str]]],
    model_hints: dict[str, dict[str, list[str]]],
) -> dict[str, dict[str, list[str]]]:
    merged: dict[str, dict[str, list[str]]] = {}
    for name, base in fallback.items():
        model = model_hints.get(name, {})
        entry: dict[str, list[str]] = {}
        for field in _HINT_FIELDS:
            combined = list(dict.fromkeys([*model.get(field, []), *base.get(field, [])]))
            entry[field] = combined
        merged[name] = entry
    return merged


# 方法作用：调用模型完成一次键列判断（含工厂创建、工具禁用与结构化解析）。
# Args: context - 授权上下文；tables - 授权表结构；settings - 配置快照。
# Returns: 已通过 Pydantic 校验的模型输出。
async def _invoke_key_model(context: Any, tables: dict[str, dict], settings: dict[str, Any]) -> SchemaKeyDetectionOutput:
    """调用期禁用知识/探查工具，避免键列判断递归探查、消耗业务预算。"""
    from src.llm.invocation import invoke_structured
    from src.llm.knowledge_loop import KnowledgeLoopConfig, use_knowledge_loop

    llm = await asyncio.to_thread(
        _get_task_llm,
        SCHEMA_KEY_TASK,
        temperature=0,
        reasoning=False,
        max_tokens=int(settings["max_tokens"]),
        stream=False,
    )
    structure_text = render_structure_input(tables)
    sections = [
        PromptSection(
            "schema_structures",
            structure_text,
            priority=100,
            min_chars=200,
            max_chars=_MAX_STRUCTURE_CHARS,
        ),
        PromptSection(
            "output_spec",
            "只对上面列出的表逐表输出 JSON；没有把握的表给空列表。",
            priority=90,
            max_chars=300,
        ),
    ]
    logger.debug(
        "键列判断模型调用入口",
        datasource=str(getattr(context, "datasource", "") or ""),
        table_count=len(tables),
        structure_chars=len(structure_text),
        max_tokens=int(settings["max_tokens"]),
    )
    with use_knowledge_loop(KnowledgeLoopConfig(enabled=False)):
        return await invoke_structured(
            SCHEMA_KEY_PROMPT_ID,
            sections,
            output_model=SchemaKeyDetectionOutput,
            task=SCHEMA_KEY_TASK,
            temperature=0,
            reasoning=False,
            config={},
            metadata={
                "node": "schema_keys",
                "datasource": str(getattr(context, "datasource", "") or ""),
            },
            model=llm,
        )


# 方法作用：键列结构判断主入口（30.1），有界模型 + 确定性回退 + 身份隔离缓存。
# Args: context - 授权上下文（tenant_id/user_id/datasource/tables/allowed_columns/row_filter_sql）。
# Returns: 每表固定四键 key_columns/display_columns/foreign_keys/profile_columns 的候选字典副本。
async def detect_schema_keys(context: Any) -> dict[str, dict[str, list[str]]]:
    """只输入授权表结构；未知列/伪造表拒绝；取消必须向上传播。"""
    tables = _authorized_tables(context)
    if not tables:
        logger.debug("键列判断跳过", reason="no_authorized_tables")
        return {}
    settings = _schema_key_settings()
    cache_key = (
        int(getattr(context, "tenant_id", 0) or 0),
        int(getattr(context, "user_id", 0) or 0),
        str(getattr(context, "datasource", "") or ""),
        authorized_structure_fingerprint(tables),
        _permission_fingerprint(context),
        _model_fingerprint(),
        _prompt_version(),
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.debug("键列判断缓存命中", datasource=cache_key[2], table_count=len(cached))
        return cached
    fallback = fallback_key_hints(tables)
    result = fallback
    llm_available = False
    if settings["enabled"]:
        llm_available = _is_task_llm_available(SCHEMA_KEY_TASK)
    if settings["enabled"] and llm_available:
        try:
            async with asyncio.timeout(float(settings["timeout"])):
                output = await _invoke_key_model(context, tables, settings)
            model_hints = normalize_model_hints(output, tables)
            result = _merge_hints(fallback, model_hints) if model_hints else fallback
            if not model_hints:
                logger.info("键列判断输出非法，已回退确定性规则", table_count=len(tables))
        except asyncio.CancelledError:
            # 取消不降级：向上传播，让上游按取消语义收尾。
            raise
        except Exception as exc:
            logger.warning(
                "键列判断降级为确定性规则",
                reason=type(exc).__name__,
                table_count=len(tables),
            )
            result = fallback
    else:
        logger.info(
            "键列判断确定性回退",
            enabled=bool(settings["enabled"]),
            llm_available=llm_available,
            table_count=len(tables),
        )
    _cache_set(cache_key, result, float(settings["cache_ttl"]), int(settings["cache_entries"]))
    return copy.deepcopy(result)


# 方法作用：把键列提示写入表结构副本的 `_profile_key_columns`，原对象保持不动。
# Args: tables - 表结构列表；hints - detect_schema_keys 的候选字典。
# Returns: 复制并写入键候选的表结构列表；非法列名不会写入。
def apply_key_hints(tables: list[dict], hints: dict[str, dict[str, list[str]]]) -> list[dict]:
    """只写合法 key_columns；未授权或未知列名一律丢弃。"""
    hint_names = [str(name).strip() for name in (hints or {}) if str(name or "").strip()]
    result: list[dict] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        copied = copy.deepcopy(table)
        name = str(copied.get("name", "") or "").strip()
        # 表名与列名都按物理身份匹配：精确优先，大小写歧义时宁缺勿借。
        hint_name = match_identifier(hint_names, name)
        entry = (hints or {}).get(hint_name, {}) if hint_name else {}
        column_names = [
            str(column.get("name", "") or "").strip()
            for column in copied.get("columns", []) or []
            if isinstance(column, dict) and str(column.get("name", "") or "").strip()
        ]
        key_columns: list[str] = []
        for column_name in entry.get("key_columns", []) or []:
            canonical = match_identifier(column_names, column_name)
            if canonical and canonical not in key_columns:
                key_columns.append(canonical)
        copied["_profile_key_columns"] = key_columns
        result.append(copied)
    return result
