"""
generate_sql 节点 — LLM 生成 SQL，支持错误回注重试。

这是流水线中最关键的节点。它负责：
  1. 将「表结构」+「方言参考」+「用户问题」组装成 LLM Prompt
  2. 调用 LLM 流式生成 SQL（通过 src/llm/client.py 工厂获取模型）
  3. 从 LLM 响应中鲁棒提取 SQL（支持 JSON / SQL 代码块 / 纯文本）
  4. 在重试模式下，将上一轮的 SQL 和错误信息注入 Prompt

数据流：
  输入:  relevant_tables（表结构）, user_query（用户问题）, dialect（方言）
        validation_errors + generated_sql（仅重试时）
  输出:  generated_sql（生成的 SQL）, retry_count

与条件路由的协作:
  after_layer3 检测到语法错误 → 回到 generate_sql 重试（retry_count+1）
  after_layer4 检测到 EXPLAIN 错误 → 同上
  should_retry 检测到执行错误 → 同上
  三类重试都回到这里，每次重试 error_context 会追加到 Prompt 中
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any

from langchain_core.runnables import RunnableConfig

from src.config import get_settings
from src.graph.state import AnalysisState
from src.llm.prompt_budget import PromptSection
from src.llm.prompts import get_dialect_cheatsheet
from src.llm.schema_keys import (
    SQL_INTELLIGENCE_CONFIG_KEY,
    apply_key_hints,
    authorized_structure_fingerprint,
    column_authorized,
    detect_schema_keys,
    match_identifier,
)
from src.logging_config import get_logger
from src.security.sql_execution import normalize_sql_dialect

logger = get_logger(__name__)

# Schema 段字符上限。原值 4400 会把上百张表的库裁到只剩几张，
# 目标表的列名进不了 Prompt，模型只能按惯例猜列名（曾导致 i.created_at 不存在）。
# 现代模型窗口足够大，这里按“能装下整库表结构”来设，不再是瓶颈。
_PROMPT_SCHEMA_MAX_CHARS = 60000
# 重试回喂预算：字段错误很多时 issues 量与每表可用列量都会线性放大 Prompt，
# 这里与 message 的 high_issues[:3] 约定保持一致（issue 上限 3），
# 并把每表可用列收紧到 10（模块内 MAX_AVAILABLE_COLUMNS=40 是入库上限，
# 重试 Prompt 不需要那么多）。
# 不要按「3 表 × 10 列 ≈ 1050 字符，仍有富余」估算后贸然上调这两个上限：
# tests/test_graph/test_generate_sql_retry_context.py 的
# test_retry_instructions_survive_section_budget_truncation 用极端 fixture
# （3 条 issue、每条 8 个长提供方表名 + 每表 25 个长列名）实测整段 3362 字符，
# 约为 1800 预算的 1.9 倍；契约靠「指令与截断提示前置」保住，而不是靠明细够短。
# 上调上限前必须重算该用例的可见前缀。
# 截断处必须显式提示数量，且提示前置在行首，避免与清单一起被尾部截断。
_RETRY_MAX_SCHEMA_ISSUES = 3
_RETRY_MAX_AVAILABLE_COLUMNS = 10
# EXPLAIN 错误回喂预算。layer4_explain 写入的 explain_errors 单条 message 最长
# 500 字符（连接器侧已 [:500]），多条叠加会把 1800 字符的 retry_error 段撑爆。
# 这里限条数（3 条，与 validation issues 上限一致）+ 限单条长度（400），
# 并通过「另有 N 条」提示未列出的数量，避免截断变成静默丢弃。
_RETRY_MAX_EXPLAIN_ERRORS = 3
_RETRY_MAX_EXPLAIN_MESSAGE_CHARS = 400
# 失败 SQL 回注预算。原值 3000 远超 retry_error 段的 1800 字符上限，
# 多出来的部分本来就会被尾部截断砍掉，却会把排在它后面的收尾指令一起顶出去。
# 收紧到 1000，让「错误原因 + 失败 SQL + 收尾指令」都落在可见前缀内。
_RETRY_MAX_PREVIOUS_SQL_CHARS = 1000
# 每张表进入 Prompt 的列数上限。命中列优先占位，其余按原列序补足——
# 这个上限必须同时作用于「命中」与「未命中」两条路径，见 _prune_prompt_table_columns。
_PROMPT_COLUMNS_PER_TABLE = 32
_COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "订单": ("order", "orders", "sale", "sales"),
    "金额": ("amount", "amt", "price", "revenue", "sales", "total"),
    "客单价": ("unit_price", "average_order_value", "aov", "price"),
    "用户": ("user", "users", "customer", "customers", "member"),
    "数量": ("count", "qty", "quantity", "number", "num"),
    "状态": ("status", "state"),
    "日期": ("date", "day", "time", "created", "updated"),
    "时间": ("time", "date", "created", "updated"),
    "产品": ("product", "sku", "item"),
    "库存": ("stock", "inventory", "qty", "quantity"),
}
_INTERNAL_COLUMN_MARKERS = {
    "internal", "remark", "remarks", "note", "notes", "debug", "trace",
    "created_by", "updated_by", "deleted_by", "private", "secret",
}


# 方法作用：延迟检查指定 LLM 任务是否可用，避免工作流装配时加载模型依赖。
# Args: task - LLM 任务标识。
# Returns: 指定任务存在可用模型时返回 True。
def _is_task_llm_available(task: str) -> bool:
    from src.llm.client import is_task_llm_available

    return is_task_llm_available(task)


# 方法作用：延迟创建指定任务模型并保留测试可替换的模块级边界。
# Args: task - LLM 任务标识；kwargs - 模型创建参数。
# Returns: 任务对应的 ChatModel。
def _get_task_llm(task: str, **kwargs: Any) -> Any:
    from src.llm.client import get_task_llm

    return get_task_llm(task, **kwargs)


# 方法作用：兼容旧测试和扩展点，同时按 generate_sql 任务检查模型可用性。
# Args: 无。
# Returns: 远程授权模型或本地模型可用于 SQL 生成时返回 True。
def is_llm_available() -> bool:
    """返回 SQL 生成任务的模型可用状态。"""
    logger.debug("SQL 任务模型可用性入口")
    available = _is_task_llm_available("generate_sql")
    logger.debug("SQL 任务模型可用性完成", available=available)
    return available


# 方法作用：兼容旧 Mock 接口并创建关闭 reasoning 的 SQL 任务模型。
# Args: temperature - 生成温度；stream - 是否创建流式模型。
# Returns: 按 generate_sql 任务策略创建的 ChatModel。
def get_llm(temperature: float = 0, *, stream: bool = True):
    """创建 SQL 生成模型，强制关闭推理模式以降低延迟。"""
    logger.debug("创建 SQL 任务模型入口", temperature=temperature, stream=stream)
    model = _get_task_llm(
        "generate_sql",
        temperature=temperature,
        reasoning=False,
        stream=stream,
    )
    logger.debug("创建 SQL 任务模型完成", stream=stream)
    return model


# 数据画像段的统一规则：只在启用 SQL 智能时随 data_profile 注入，
# 关闭特性不得改动全局 sql.generate 提示词。
_DATA_PROFILE_RULES = (
    "以下规则只在本次请求附带键列事实/数据画像时适用：\n"
    "1. 分组或展示维度单列不唯一时，SELECT 必须带出可区分的键列（下文标记唯一或编码的列），不得只输出展示名；\n"
    "2. 实体名过滤优先使用画像中的精确值；不存在精确匹配时，返回全部候选实体明细并说明歧义，禁止把同名实体静默合并；\n"
    "3. 需要确认具体实体、值域或时间边界而画像未覆盖时，先用探针工具（probe_data）确认，再生成正式 SQL；\n"
    "4. 画像或探查结论只在其声明的范围内成立：样本中未见重复或未命中的值，不得写成全表唯一或全表不存在。"
)
# data_profile 段上限（spec/55 §6）；键列与画像结论排在前缀，截断优先保留。
_DATA_PROFILE_SECTION_MAX_CHARS = 6000
# 画像预算默认值：字段由配置任务补入 src/config.py，缺失时取 spec/55 §9 默认。
_DEFAULT_PROFILING_MAX_TABLES = 6
_DEFAULT_PROFILING_MAX_COLUMNS_PER_TABLE = 6


@dataclass(frozen=True, slots=True)
class SqlIntelligence:
    """一次启用 SQL 智能的生成准备结果；全部为本次调用局部状态。"""

    prompt_tables: list[dict]
    data_profile: str = ""
    probe_context: Any | None = None


# 方法作用：读取服务端写入的启用标记与画像预取总开关，任一关闭即视为关闭。
# Args: config - 当前节点 RunnableConfig。
# Returns: 标记严格为 True 且总开关开启时返回 True。
def _sql_intelligence_enabled(config: RunnableConfig | None) -> bool:
    """标记只由服务端聊天入口写入，请求正文无法覆写；总开关关闭必须带来零新增准备。"""
    if isinstance(config, dict):
        configurable = config.get("configurable", {})
    else:
        configurable = getattr(config, "configurable", {})
    if not isinstance(configurable, dict):
        return False
    if configurable.get(SQL_INTELLIGENCE_CONFIG_KEY) is not True:
        return False
    # 总开关关闭时不进入键列判断/画像/探针准备；字段缺失按开启处理，保持既有路径不变。
    return bool(getattr(get_settings(), "sql_profiling_enabled", True))


# 方法作用：读取画像预取相关配置，字段缺失时使用 spec 默认值。
# Args: 无。
# Returns: enabled/max_tables/max_columns_per_table 配置快照。
def _profiling_settings() -> dict[str, Any]:
    """配置字段由配置任务统一添加；此处只读默认，不在本节点改配置模块。"""
    settings = get_settings()
    return {
        "enabled": bool(getattr(settings, "sql_profiling_enabled", True)),
        "max_tables": max(1, int(getattr(settings, "sql_profiling_max_tables", _DEFAULT_PROFILING_MAX_TABLES) or 0)
                          or _DEFAULT_PROFILING_MAX_TABLES),
        "max_columns_per_table": max(1, int(
            getattr(settings, "sql_profiling_max_columns_per_table", _DEFAULT_PROFILING_MAX_COLUMNS_PER_TABLE) or 0
        ) or _DEFAULT_PROFILING_MAX_COLUMNS_PER_TABLE),
    }


# 方法作用：校验真实认证身份与请求上下文身份一致，不信任 state 声称的身份。
# Args: contexts - 当前节点的只读上下文分组。
# Returns: 一致时返回 (tenant_id, user_id)；不一致返回 None。
def _verified_intelligence_identity(contexts: Any) -> tuple[int, int] | None:
    """真实身份以认证 ContextVar 为准；不一致时禁用智能能力且不查任何数据。"""
    from src.api.auth import get_current_tenant_id, get_current_user_id

    real_tenant = int(get_current_tenant_id())
    real_user = int(get_current_user_id())
    if (real_tenant, real_user) != (int(contexts.request.tenant_id), int(contexts.request.user_id)):
        logger.warning(
            "SQL 智能身份复核拒绝",
            real_tenant=real_tenant,
            real_user=real_user,
            state_tenant=int(contexts.request.tenant_id),
            state_user=int(contexts.request.user_id),
        )
        return None
    return real_tenant, real_user


# 方法作用：取当前数据源的授权快照，多源场景不混用第一源权限。
# Args: contexts - 当前节点的只读上下文分组。
# Returns: (数据源名, 授权条目)；无法唯一定位时返回 None。
def _current_datasource_access(contexts: Any) -> tuple[str, dict[str, Any]] | None:
    """显式数据源必须命中授权映射，未命中即安全拒绝；未指定时只接受唯一授权源的快照。"""
    access_map = contexts.permission.datasource_access or {}
    datasource = str(contexts.routing.datasource or "").strip()
    if datasource:
        entry = access_map.get(datasource)
        if isinstance(entry, dict):
            return datasource, entry
        # 显式数据源未命中授权映射：不得回退借用其他数据源的权限快照，直接安全拒绝。
        logger.info(
            "SQL 智能跳过",
            reason="explicit_datasource_not_authorized",
            datasource=datasource,
            authorized_count=len(access_map),
        )
        return None
    if len(access_map) == 1:
        only_name, only_entry = next(iter(access_map.items()))
        # 画像契约要求数据源名非空；空名无法证明数据源作用域，直接跳过。
        if isinstance(only_entry, dict) and str(only_name or "").strip():
            return str(only_name).strip(), only_entry
    logger.info(
        "SQL 智能跳过",
        reason="no_scoped_datasource_access",
        datasource=datasource,
        authorized_count=len(access_map),
    )
    return None


# 方法作用：延迟加载画像模块（接口由并行任务交付，不在本文件复制定义）。
# Args: 无。
# Returns: src.datasource.profiling 模块对象。
def _load_profiling_api() -> Any:
    from src.datasource import profiling

    return profiling


# 方法作用：把节点内的原始表结构转换为画像契约要求的 TableSchema 集合。
# Args: tables - 完整授权表结构列表（relevant_tables 形态）。
# Returns: 表名 -> TableSchema 字典；无合法表时返回空字典。
def _build_profiling_tables(tables: list[dict]) -> dict[str, Any]:
    """原始字典含样例值等扩展键，必须先归一为 TableSchema 才能通过画像契约校验。"""
    from src.datasource.schema_snapshot import ColumnInfo, TableRelation, TableSchema

    result: dict[str, Any] = {}
    for table in tables:
        if not isinstance(table, dict):
            continue
        name = str(table.get("name", "") or "").strip()
        if not name:
            continue
        columns: list[Any] = []
        for column in table.get("columns", []) or []:
            if not isinstance(column, dict):
                continue
            column_name = str(column.get("name", "") or "").strip()
            if not column_name:
                continue
            columns.append(ColumnInfo(
                name=column_name,
                type=str(column.get("type", "") or ""),
                comment=str(column.get("comment", "") or ""),
                is_nullable=bool(column.get("is_nullable", True)),
                is_primary_key=bool(column.get("is_primary_key", False)),
                is_indexed=bool(column.get("is_indexed", False)),
                enum_values=[
                    str(value) for value in (column.get("enum_values") or [])
                    if str(value or "").strip()
                ],
                value_format=str(column.get("value_format", "") or ""),
            ))
        relations: list[Any] = []
        for relation in table.get("relations", []) or []:
            if not isinstance(relation, dict):
                continue
            relations.append(TableRelation(
                target_table=str(relation.get("target_table", "") or ""),
                join_key=str(relation.get("join_key", "") or ""),
                relation_type=str(relation.get("relation_type", "") or ""),
            ))
        try:
            row_estimate = int(table.get("row_count_estimate") or table.get("row_estimate") or 0)
        except (TypeError, ValueError):
            row_estimate = 0
        result[name] = TableSchema(
            name=name,
            description=str(table.get("description", "") or ""),
            columns=columns,
            relations=relations,
            row_count_estimate=row_estimate,
            partition_key=str(table.get("partition_key", "") or ""),
            tags=[str(tag) for tag in (table.get("tags") or []) if str(tag or "").strip()],
        )
    return result


# 方法作用：创建探针上下文的请求作用域（由并行任务交付的接口）。
# Args: context - 已构造的 ProfilingContext。
# Returns: 同步上下文管理器；退出即复原。
def _data_probe_context_scope(context: Any):
    from src.tools.data_probe import use_data_probe_context

    return use_data_probe_context(context)


# 方法作用：键列保护可能让渲染重新超预算，从末位裁剪选中表。
# Args: tables - 已裁剪表列表；dialect - 方言；max_chars - 详细 Schema 上限。
# Returns: 不超过字符预算的表列表；至少保留第一张表。
def _fit_prompt_tables_budget(tables: list[dict], dialect: str, max_chars: int) -> list[dict]:
    selected = list(tables)
    while len(selected) > 1 and len(format_schema_for_prompt(selected, dialect)) > max_chars:
        selected.pop()
    return selected


# 方法作用：按键列判断结果与问题命中列选择单表画像候选列。
# Args: table - 带 `_profile_key_columns` 的表结构；hints_entry - 该表的键列提示；query - 用户问题；cap - 每表列数上限。
# Returns: 去重且不超过上限的授权列名列表。
def _profile_columns_for_table(
    table: dict,
    hints_entry: dict[str, list[str]],
    query: str,
    cap: int,
) -> list[str]:
    """键列判断结果优先；不足时按问题命中列补足（退化路径仍覆盖命名候选）。"""
    columns = [
        column for column in table.get("columns", []) or []
        if isinstance(column, dict) and str(column.get("name", "") or "").strip()
    ]
    # 提示列按物理身份匹配：精确优先，大小写歧义时宁缺勿借，不能借到变体列。
    column_names = [str(column.get("name", "") or "").strip() for column in columns]
    ordered: list[str] = []
    candidates: list[str] = []
    for field in ("profile_columns", "key_columns", "display_columns", "foreign_keys"):
        candidates.extend(hints_entry.get(field, []) or [])
    candidates.extend(table.get("_profile_key_columns", []) or [])
    for candidate in candidates:
        canonical = match_identifier(column_names, candidate)
        if canonical and canonical not in ordered:
            ordered.append(canonical)
    limit = max(1, int(cap))
    if len(ordered) >= limit:
        return ordered[:limit]
    table_name = str(table.get("name", "") or "")
    scored = sorted(
        (
            (_column_match_score(query, column, table_name), index, str(column.get("name", "") or "").strip())
            for index, column in enumerate(columns)
        ),
        key=lambda item: (-item[0], item[1]),
    )
    for score, _index, name in scored:
        if score <= 0:
            break
        if name not in ordered:
            ordered.append(name)
        if len(ordered) >= limit:
            break
    return ordered[:limit]


# 方法作用：把键列提示渲染为紧凑的语义事实行，供生成 Prompt 配对使用。
# Args: hints - detect_schema_keys 的候选字典。
# Returns: 键列候选文本；无可用候选时返回空串。
def _render_key_hint_text(hints: dict[str, dict[str, list[str]]]) -> str:
    lines: list[str] = []
    for name, entry in (hints or {}).items():
        parts: list[str] = []
        if entry.get("key_columns"):
            parts.append("键列候选 " + ", ".join(entry["key_columns"]))
        if entry.get("display_columns"):
            parts.append("展示名候选 " + ", ".join(entry["display_columns"]))
        if entry.get("foreign_keys"):
            parts.append("外键候选 " + ", ".join(entry["foreign_keys"]))
        if parts:
            lines.append(f"- {name}: " + "；".join(parts))
    if not lines:
        return ""
    return "### 键列候选（结构判断，未经数据验证）\n" + "\n".join(lines)


# 方法作用：组装 data_profile 段文本：规则 + 键列候选 + 画像证据。
# Args: hints - 键列提示；profile_text - render_profile 输出。
# Returns: 供 PromptSection 使用的文本；两者皆空时返回空串。
def _build_data_profile_text(hints: dict[str, dict[str, list[str]]], profile_text: str) -> str:
    """键列与画像结论排在前缀，预算截断优先保留。"""
    hint_text = _render_key_hint_text(hints)
    profile_text = str(profile_text or "").strip()
    if not hint_text and not profile_text:
        return ""
    sections = [_DATA_PROFILE_RULES]
    if hint_text:
        sections.append(hint_text)
    if profile_text:
        sections.append(profile_text)
    return "\n\n".join(sections)


# 方法作用：按授权列集合过滤表结构，生成 Prompt 与画像只使用授权列。
# Args: tables - 完整授权表结构列表；allowed_columns - 当前数据源授权列快照。
# Returns: 复制后只含授权列的表列表；无授权列的表被剔除。
def _filter_authorized_tables(tables: list[dict], allowed_columns: list[str]) -> list[dict]:
    """授权语义与画像模块一致：空白名单不限制；精确比较标识符，大小写变体不共享权限。"""
    result: list[dict] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        name = str(table.get("name", "") or "").strip()
        if not name:
            continue
        copied = copy.deepcopy(table)
        copied["columns"] = [
            column for column in copied.get("columns", []) or []
            if isinstance(column, dict)
            and str(column.get("name", "") or "").strip()
            and column_authorized(allowed_columns, name, str(column.get("name", "") or "").strip())
        ]
        if not copied["columns"]:
            logger.debug("SQL 智能跳过无授权列的表", table=name[:64])
            continue
        result.append(copied)
    return result


# 方法作用：启用标记下的生成准备：键列判断 → 裁剪保护 → 画像预取。
# Args: contexts - 只读上下文分组；tables - 完整授权表；prompt_tables - 原规则选中的表；query - 用户问题。
# Returns: SqlIntelligence；任何失败都降级为原始 Prompt。
async def _prepare_sql_intelligence(
    contexts: Any,
    tables: list[dict],
    prompt_tables: list[dict],
    query: str,
) -> SqlIntelligence:
    """只在真实身份一致且能定位当前数据源授权时启用；取消向上传播。"""
    unchanged = SqlIntelligence(prompt_tables=prompt_tables)
    if not prompt_tables:
        return unchanged
    identity = _verified_intelligence_identity(contexts)
    if identity is None:
        return unchanged
    tenant_id, user_id = identity
    access_target = _current_datasource_access(contexts)
    if access_target is None:
        return unchanged
    datasource, access = access_target
    full_by_name = {
        str(table.get("name", "") or "").strip(): table
        for table in tables
        if isinstance(table, dict) and str(table.get("name", "") or "").strip()
    }
    selected_full = [
        full_by_name[str(table.get("name", "") or "").strip()]
        for table in prompt_tables
        if isinstance(table, dict) and str(table.get("name", "") or "").strip() in full_by_name
    ]
    if not selected_full:
        return unchanged
    # 生成接入只使用授权过滤后的结构：未授权列不得进入键列判断、画像、裁剪保护与 Prompt。
    allowed_columns = [str(column) for column in (access.get("allowed_columns") or [])]
    authorized_full = _filter_authorized_tables(selected_full, allowed_columns)
    if not authorized_full:
        logger.info("SQL 智能降级", reason="no_authorized_columns", datasource=datasource)
        return unchanged
    try:
        profiling_api = _load_profiling_api()
    except Exception as exc:
        # 画像模块尚未就绪（并行交付）属预期降级路径，不阻塞 SQL 生成；
        # 导入失败正文可能夹带环境信息，用固定中文文案承载原始 traceback，
        # 且只在本回退边界记录一次。
        safe_error = RuntimeError("画像模块未就绪，SQL 智能降级为原始 Prompt")
        logger.info(
            "SQL 智能降级",
            reason="profiling_unavailable",
            error_type=type(exc).__name__,
            exc_info=(type(safe_error), safe_error, exc.__traceback__),
        )
        return unchanged
    authorized = _build_profiling_tables(authorized_full)
    if not authorized:
        return unchanged
    dialect = str(contexts.execution.dialect or "")
    settings = _profiling_settings()
    context_kwargs: dict[str, Any] = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "datasource": datasource,
        "dialect": dialect,
        "tables": authorized,
        "allowed_columns": allowed_columns,
        "row_filter_sql": str(access.get("row_filter_sql") or ""),
        "schema_fingerprint": authorized_structure_fingerprint(authorized),
    }
    # role 参与行列策略解析与探针绑定；契约无该字段时不传，保持 §14 兼容。
    if "role" in (getattr(profiling_api.ProfilingContext, "model_fields", {}) or {}):
        context_kwargs["role"] = str(contexts.request.user_role or "")
    try:
        profile_context = profiling_api.ProfilingContext(**context_kwargs)
    except Exception as exc:
        # 非法上下文属预期降级路径；契约校验正文可能回显字段值，
        # 用固定中文文案承载原始 traceback，且只在本回退边界记录一次。
        safe_error = RuntimeError("画像上下文构造失败，SQL 智能降级为原始 Prompt")
        logger.warning(
            "SQL 智能降级",
            reason="context_build_failed",
            error_type=type(exc).__name__,
            exc_info=(type(safe_error), safe_error, exc.__traceback__),
        )
        return unchanged
    hints: dict[str, dict[str, list[str]]] = {}
    try:
        hints = await detect_schema_keys(profile_context)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("键列判断失败，按无提示继续", error_type=type(exc).__name__)
        hints = {}
    hinted_tables = apply_key_hints(authorized_full, hints or {})
    pruned = [_prune_prompt_table_columns(table, query) for table in hinted_tables]
    pruned = _fit_prompt_tables_budget(pruned, dialect, _PROMPT_SCHEMA_MAX_CHARS)
    profile_text = ""
    if settings["enabled"]:
        kept_names = {str(table.get("name", "") or "").strip() for table in pruned}
        columns_by_table: dict[str, list[str]] = {}
        for table in hinted_tables:
            table_name = str(table.get("name", "") or "").strip()
            if table_name not in kept_names or len(columns_by_table) >= settings["max_tables"]:
                continue
            columns = _profile_columns_for_table(
                table,
                (hints or {}).get(table_name, {}),
                query,
                settings["max_columns_per_table"],
            )
            if columns:
                columns_by_table[table_name] = columns
        if columns_by_table:
            try:
                snapshot = await profiling_api.get_data_profiler().profile(
                    profile_context, columns_by_table,
                )
                profile_text = str(profiling_api.render_profile(snapshot) or "")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 画像失败不阻塞生成：无画像照常出 SQL。
                logger.warning(
                    "画像预取降级",
                    error_type=type(exc).__name__,
                    table_count=len(columns_by_table),
                )
                profile_text = ""
    data_profile = _build_data_profile_text(hints or {}, profile_text)
    logger.info(
        "SQL 智能准备完成",
        datasource=datasource,
        prompt_tables=len(pruned),
        hint_tables=len(hints or {}),
        profile_chars=len(profile_text),
    )
    return SqlIntelligence(
        prompt_tables=pruned,
        data_profile=data_profile,
        probe_context=profile_context,
    )


async def generate_sql_node(state: AnalysisState, config: RunnableConfig) -> dict:
    """
    SQL 生成节点的主入口。

    读取 state 中的用户查询、表结构、方言信息，
    优先调用 LLM 流式生成 SQL，LLM 不可用时退回模板。

    返回: {"generated_sql": str, "retry_count": int}
    """
    from src.graph.context import read_contexts

    contexts = read_contexts(state)
    _start = time.monotonic()
    logger.debug("节点开始", node="generate_sql")
    tables = state.get("relevant_tables", [])
    dialect = contexts.execution.dialect or "clickhouse"
    query = contexts.request.user_query
    retry = contexts.execution.retry_count
    task_plan = contexts.routing.task_plan

    # 指标目录先按结构化计划处理，禁止把目录问题交给 LLM 编造跨表 SQL。
    if task_plan.get("operation") == "indicator_catalog":
        from src.graph.indicator_catalog import (
            build_indicator_catalog_plan,
            build_indicator_catalog_sql,
            build_indicator_catalog_summary,
        )

        catalog_plan = build_indicator_catalog_plan(tables)
        sql = build_indicator_catalog_sql(catalog_plan, dialect)
        logger.info(
            "指标目录 SQL 规划完成",
            mode=catalog_plan.mode,
            requires_data_query=catalog_plan.requires_data_query,
            selected_table=catalog_plan.selected_table,
            sql_present=bool(sql),
        )
        return {
            "generated_sql": sql,
            "retry_count": retry + 1,
            "needs_input": catalog_plan.needs_input,
            "input_explanation": build_indicator_catalog_summary(catalog_plan),
            "needs_time_range": False,
            "time_range_explanation": "",
            "validation_errors": [],
            "explain_errors": [],
            "execution_error": "",
            "execution_error_type": "",
            "execution_retry_count": 0,
        }

    # 时间补参只接受 classify_intent 写入的结构化任务计划。
    if bool(task_plan.get("needs_time_range", False)):
        logger.info("需要用户指定时间范围", query=query[:80], source="task_plan")
        return {
            "generated_sql": "",
            "needs_input": True,
            "input_explanation": "请指定查询的时间范围（最近一周/一月/一年/两年/三年/五年 或 全部数据）",
            "needs_time_range": True,
            "time_range_explanation": "请指定查询的时间范围（最近一周/一月/一年/两年/三年/五年 或 全部数据）",
            "retry_count": retry + 1,
            "validation_errors": [],
            "execution_error": "",
            "execution_error_type": "",
        }

    # 表结构 → Markdown 格式（用于 Prompt）；白名单使用完整授权表，详细 Schema 按问题裁剪。
    prompt_tables = select_prompt_tables(
        tables,
        query,
        dialect=dialect,
        max_chars=_PROMPT_SCHEMA_MAX_CHARS,
    )
    # 启用 SQL 智能时：先按原规则确定选中表，再取完整列做键列判断与画像，最后重新裁剪。
    # 未写入服务端标记的调用（报表/后台/直接节点调用）完全走原路径，零新增调用。
    data_profile_text = ""
    probe_context: Any | None = None
    if _sql_intelligence_enabled(config):
        intelligence = await _prepare_sql_intelligence(contexts, tables, prompt_tables, query)
        prompt_tables = intelligence.prompt_tables
        data_profile_text = intelligence.data_profile
        probe_context = intelligence.probe_context
    schema_text = format_schema_for_prompt(prompt_tables, dialect)
    table_whitelist = build_table_whitelist_context(tables)
    logger.debug(
        "SQL Schema Prompt 裁剪完成",
        total_tables=len(tables),
        prompt_tables=len(prompt_tables),
        schema_chars=len(schema_text),
        whitelist_chars=len(table_whitelist),
    )
    # 获取该方言的 SQL 语法速查表
    dialect_hint = get_dialect_cheatsheet(dialect)
    # 技能系统注入的额外 Prompt（Phase 2）
    skill_prompt = state.get("skill_prompt_override", "")
    # 业务规则、知识命中、枚举和已验证示例统一组装为证据块
    grounding_context = build_sql_grounding_context(state)

    # ── 重试时的错误上下文 ──
    error_context = build_retry_context(state, retry, tables)
    if retry > 0:
        prev_sql = str(state.get("generated_sql", "") or "")
        exec_err = str(state.get("execution_error", "") or "")
        val_errors = state.get("validation_errors", []) or []
        logger.info(
            "SQL 重试上下文边界",
            retry=retry,
            previous_sql_chars=len(prev_sql),
            validation_error_types=[
                str(error.get("type", "")) for error in val_errors if isinstance(error, dict)
            ],
            has_execution_error=bool(exec_err),
            # 探针补全：EXPLAIN 失败只落 explain_errors，不打印这一项时日志会显示
            # 「validation_error_types=[] has_execution_error=False」，看起来像没有任何
            # 错误被回喂，实际原因只是打错了字段。
            explain_error_count=len(state.get("explain_errors", []) or []),
        )

    # 新轮次的 retry_count 由 prepare_turn 统一清零；节点内再次按历史判断会把同轮重试误判为新问题。
    history = state.get("conversation_history", []) or []
    # 回退：如果 conversation_history 为空，从 messages 字段提取上下文
    if not history:
        msgs = state.get("messages", []) or []
        if msgs:
            # 从 LangChain messages 重建简化的对话历史
            for msg in msgs:
                if hasattr(msg, 'content') and msg.content:
                    role = 'user' if msg.__class__.__name__ == 'HumanMessage' else 'assistant'
                    history.append({
                        "turn_id": len(history) + 1,
                        "user_query": msg.content if role == 'user' else '',
                        "analysis_summary": msg.content if role == 'assistant' else '',
                        "generated_sql": '',
                        "execution_success": True,
                        "chart_type": '',
                    })
        if history:
            logger.debug("对话上下文（从 messages 复原）", turns=len(history))
    if history:
        logger.debug("对话上下文", turns=len(history),
                    types=[type(t).__name__ for t in history])
    else:
        logger.debug("对话上下文为空（可能是首轮或 checkpointer 反序列化失败）")
    # ── 主路径：LLM 流式生成 ──
    explanation = ""
    fallback_error = ""
    if is_llm_available():
        logger.info("SQL 生成: 调用 LLM", query=query[:80], dialect=dialect, retry=retry)
        sql, reasoning, explanation = await _llm_generate(
            schema_text=schema_text,
            table_whitelist=table_whitelist,
            dialect_hint=dialect_hint,
            dialect=dialect,
            query=query,
            error_ctx=error_context,
            skill_prompt=skill_prompt,
            grounding_context=grounding_context,
            config=config,
            conversation_history=history,
            data_profile=data_profile_text,
            probe_context=probe_context,
        )
        if sql.startswith("-- LLM"):
            fallback_error = sql.removeprefix("-- ").strip()
            sql = ""
    else:
        sql, reasoning = _template_generate(tables, retry, state), ""
        if not sql:
            fallback_error = "LLM 不可用，当前问题无法通过确定性规则生成 SQL"
            logger.warning("SQL 确定性回退不可用", query=query[:80], retry=retry)

    logger.info("SQL 生成完成", sql=sql[:200], reasoning_chars=len(reasoning))

    logger.debug(
        "SQL 时间补参判定边界",
        task_plan_needs_time_range=bool(contexts.routing.task_plan.get("needs_time_range", False)),
        sql_present=bool(sql.strip()),
        explanation_chars=len(explanation),
        explanation_triggered=False,
    )

    if not sql.strip() and not fallback_error:
        input_explanation = explanation.strip() or "请补充具体业务目标或目标表后重试。"
        logger.info("SQL 生成需要补充输入", explanation_chars=len(input_explanation))
        result = {
            "generated_sql": "",
            "needs_input": True,
            "input_explanation": input_explanation,
            "needs_time_range": False,
            "time_range_explanation": "",
            "retry_count": retry + 1,
            "validation_errors": [],
            "execution_error": "",
            "execution_error_type": "",
        }
        if reasoning:
            result["sql_reasoning_content"] = reasoning
        return result

    # 12.1.6 LLM 输出二次校验 — 拦截表名幻觉
    if sql.strip() and not sql.startswith("-- "):  # 跳过空回退和错误占位符
        hallucination = _check_table_hallucination(sql, tables, dialect)
        if hallucination:
            logger.warning("LLM 幻觉拦截", sql=sql[:200], unknown_tables=hallucination)
            result = {"generated_sql": sql, "retry_count": retry + 1,
                      "validation_errors": [{"type": "hallucination",
                          "message": f"SQL 引用了不存在的表: {hallucination}",
                          "unknown_tables": hallucination}]}
            if reasoning:
                result["sql_reasoning_content"] = reasoning
            logger.debug("节点完成", node="generate_sql",
                       elapsed_ms=round((time.monotonic() - _start) * 1000))
            return result

    logger.debug("节点完成", node="generate_sql", elapsed_ms=round((time.monotonic() - _start) * 1000))
    result = {
        "generated_sql": sql,
        "retry_count": retry + 1,
        "needs_input": False,
        "input_explanation": "",
        "needs_time_range": False,
        "time_range_explanation": "",
        "validation_errors": [],
        "explain_errors": [],
        "execution_error": "",
        "execution_error_type": "",
        "execution_retry_count": 0,
    }
    if fallback_error:
        result["execution_error"] = fallback_error
        result["execution_error_type"] = "llm_unavailable"
        logger.info(
            "SQL 生成失败边界写回",
            error_type="llm_unavailable",
            generated_sql=False,
            retry_count=result["retry_count"],
        )
    if reasoning:
        result["sql_reasoning_content"] = reasoning
    return result


# 方法作用：构造不依赖详细字段预算的完整合法表名白名单。
# Args: tables - retrieve_schema 返回的相关表结构。
# Returns: 每个合法物理表独占一行的紧凑 Prompt 文本。
def build_table_whitelist_context(tables: list[dict]) -> str:
    """白名单只包含 Schema 已确认的表名，CTE 仍可由 SQL 自行定义。"""
    names = [
        str(table.get("name", "") or "").strip()
        for table in tables
        if isinstance(table, dict) and str(table.get("name", "") or "").strip()
    ]
    unique_names = list(dict.fromkeys(names))
    if not unique_names:
        return "合法物理表白名单为空，不得猜测任何表名。"
    return "合法物理表白名单（只能使用以下物理表或 SQL 内定义的 CTE）：\n" + "\n".join(
        f"- {name}" for name in unique_names
    )


# 方法作用：把单条结构化 schema 问题渲染成与问题类型相符的重试指令行。
# Args: issue - layer3 写入 validation_errors 的单条 issue 字典。
# Returns: 该问题的指令行列表；结构化字段不足时回退到 issue 自带的 message 文案。
def _render_schema_issue_lines(issue: dict) -> list[str]:
    """按问题类型分派渲染，避免把「真实存在的列」谎报成「不存在的字段」。"""
    issue_type = str(issue.get("type", "") or "").strip()
    datasource = str(issue.get("datasource", "") or "").strip()
    table = str(issue.get("table", "") or "").strip()
    column = str(issue.get("column", "") or "").strip()
    qualifier = str(issue.get("qualifier", "") or "").strip()
    reference = f"{qualifier}.{column}" if qualifier else column
    key = f"{datasource}.{table}" if datasource and table else (table or datasource)
    message = str(issue.get("message", "") or "").strip()
    if issue_type == "type_mismatch":
        return [message or f"列 {reference} 存在类型或日期格式不匹配，必须依据 Schema 使用显式转换，不得改用不存在的列。"]
    if issue_type == "ambiguous_column":
        # 歧义列的 table 字段是多张候选表名的逗号列表，且 provided_by / available_columns
        # 均为空：绝不能套用「引用了不存在的字段」模板，否则模型会去改一个本来正确的列名。
        # 正确指令是让模型补表限定符，并把候选表原样告知。
        candidates = table or "多张表"
        logger.debug("重试上下文渲染歧义列", table=table, column=column)
        return [
            f"上一轮未加表限定符引用列 '{column}'，该列在以下候选表中同时存在：{candidates}。"
            "必须为该列补上表限定符（表名.列名）后重新生成，不得修改列名。"
        ]
    if issue_type == "unknown_table":
        logger.debug("重试上下文渲染未知表", table=table)
        return [
            f"上一轮引用了不存在的表：{key or table or '未知'}，"
            "必须改用下方白名单中的物理表。"
        ]
    if issue_type == "parse_error":
        logger.debug("重试上下文渲染解析失败", datasource=datasource)
        return [f"上一轮 SQL 无法解析：{message or '解析失败'}，必须重新生成语法正确的 SQL。"]
    if issue_type == "cross_table_column":
        # 该列真实存在但属于别的表：文案必须写明「实际属于」，不能笼统说列不存在
        provided = ", ".join(str(item) for item in (issue.get("provided_by") or [])) or "未知"
        logger.debug("重试上下文渲染跨表列", table=table, column=column, provided_by=provided)
        return [
            f"上一轮引用的字段 {key}.{reference} 不属于表 {table}"
            f"（该列实际属于: {provided}），必须改用它真正所属的表或修正别名。"
        ]
    if issue_type == "unknown_column":
        logger.debug("重试上下文渲染未知列", table=table, column=column)
        return [f"上一轮引用了不存在的字段：{key}.{reference}"]
    # 未知类型：结构化字段不足，回退到上游已算好的准确文案（message 携带描述）
    logger.warning(
        "重试上下文遇到未知问题类型",
        issue_type=issue_type,
        table=table,
        column=column,
    )
    return [
        message
        or f"上一轮 SQL 的字段引用有误（类型: {issue_type or '未知'}），必须按可用列清单重新生成。"
    ]


# 方法作用：按错误类型构造 SQL 重试上下文，防止幻觉 SQL 被完整回注强化。
# Args: state - 当前执行状态；retry - 当前重试序号；tables - Schema 合法表列表。
# Returns: 首次调用返回空文本；表名幻觉仅返回未知表和白名单，字段引用错误
#          仅返回按表分组的可用列清单，其他错误返回受限 SQL 上下文。
def build_retry_context(state: AnalysisState, retry: int, tables: list[dict]) -> str:
    """表名幻觉、字段引用错误与普通语义错误使用不同回注策略。"""
    raw_errors = state.get("validation_errors", []) or []
    logger.debug(
        "SQL 重试上下文入口",
        retry=retry,
        validation_errors=len(raw_errors) if isinstance(raw_errors, (list, tuple)) else 0,
        table_count=len(tables),
    )
    if retry <= 0:
        return ""
    # 上一轮直接没输出 SQL（模型自称缺表/缺字段而拒绝生成）：把它的自述缺口原样回注，
    # 并明确要求基于已给 Schema 重试。实机里多数"缺失"其实在 Schema 内——总账与人力
    # 单元都出现过模型宣称没有借贷/工时表，而表就在检索结果里。
    if not str(state.get("generated_sql", "") or "").strip():
        previous = str(state.get("input_explanation", "") or "").strip()
        return (
            f"## 第 {retry} 次生成：上一轮没有输出 SQL\n"
            f"你上一轮给出的理由：{previous or '（未说明）'}\n"
            "请据此重新生成一条可执行 SQL：优先用下方 Schema 中真实存在的表与列表达同一业务口径，"
            "不要因为找不到字面同名的表就放弃；跨表关联可以借助 Schema 给出的外键。"
            "确实无法用任何真实表表达该口径时，才继续返回空 SQL，并写明缺哪张表、哪个字段。"
        )
    validation_errors = state.get("validation_errors", []) or []
    hallucinations = [
        error for error in validation_errors
        if isinstance(error, dict) and error.get("type") == "hallucination"
    ]
    if hallucinations:
        unknown_tables = list(dict.fromkeys(
            str(name)
            for error in hallucinations
            for name in (error.get("unknown_tables", []) or [])
            if str(name).strip()
        ))
        return (
            f"## 第 {retry} 次生成：纠正表名幻觉\n"
            f"上一轮引用了不存在的表：{', '.join(unknown_tables) or '未知'}。\n"
            f"{build_table_whitelist_context(tables)}\n"
            "禁止复用上一轮 SQL，必须仅基于合法表名和字段 Schema 重新生成。"
        )
    # schema 幻觉（字段引用错误）：与表名幻觉同策略——不回注失败 SQL，
    # 只回喂「该表实际可用的列」，避免模型照抄错误 SQL 就地打补丁。
    # 回喂预算：issue 只渲染前 _RETRY_MAX_SCHEMA_ISSUES 条，每张表最多列出
    # _RETRY_MAX_AVAILABLE_COLUMNS 个可用列；两处截断都显式提示数量，
    # 防止模型把「没有列出来」误判成「该列不存在」。
    schema_issues = [
        issue
        for error in validation_errors
        if isinstance(error, dict) and error.get("type") == "column_reference"
        for issue in (error.get("issues") or [])
        if isinstance(issue, dict)
    ]
    if schema_issues:
        rendered_issues = schema_issues[:_RETRY_MAX_SCHEMA_ISSUES]
        # 指令必须前置：retry_error section 的预算是「硬上限 + 尾部截断」
        # （prompt_budget._truncate_text 保留前缀），放在尾部时一旦超预算
        # 会被整行砍掉，模型就看不到「禁止复用上一轮 SQL」这条核心契约。
        # 因此把必须送达的指令紧跟标题，可截断的字段证据放在后面，
        # 截断代价只是信息变少，而不是契约失效。
        # 有可用列清单时才说「仅基于下列可用列」；歧义列等问题场景没有列清单，
        # 该表述会误导模型（列清单为空却让它照清单改），改为「逐条修正」。
        has_column_lists = any(
            (issue.get("available_columns") or []) for issue in rendered_issues
        )
        lines = [
            f"## 第 {retry} 次生成：纠正字段引用错误",
            "禁止复用上一轮 SQL，必须仅基于下列可用列重新生成。"
            if has_column_lists
            else "禁止复用上一轮 SQL，必须按下列问题逐条修正后重新生成。",
        ]
        if retry >= 2:
            # 最后通牒行与首指令行同源：只有真的渲染了可用列清单才说「使用下列
            # 明确列出的标识符」；歧义列等问题场景下方没有清单，沿用该表述会
            # 指向不存在的内容（悬挂引用），与前波修复的缺陷同类。
            lines.append(
                "这是最后一次尝试，必须使用下列明确列出的标识符。"
                if has_column_lists
                else "这是最后一次尝试，必须严格按上述问题逐条修正后重新生成。"
            )
        # 每表收集：可用列清单 + 真实总数（available_columns 可能被校验器
        # 按 MAX_AVAILABLE_COLUMNS 截断，报「共 N 个」必须用真实总数）。
        grouped: dict[str, tuple[list[str], int]] = {}
        for issue in rendered_issues:
            table = str(issue.get("table", "") or "").strip()
            datasource = str(issue.get("datasource", "") or "").strip()
            key = f"{datasource}.{table}" if datasource else table
            lines.extend(_render_schema_issue_lines(issue))
            available = [str(item) for item in (issue.get("available_columns") or [])]
            if available and key not in grouped:
                try:
                    total = int(issue.get("available_columns_total") or 0)
                except (TypeError, ValueError):
                    total = 0
                # 总数缺失时退化为清单长度；不得把未知总数谎报成更小的数
                grouped[key] = (available, max(total, len(available)))
        for key, (available, total) in grouped.items():
            shown = available[:_RETRY_MAX_AVAILABLE_COLUMNS]
            if total > _RETRY_MAX_AVAILABLE_COLUMNS:
                # 截断提示必须与表名同行前置：section 是尾部截断，提示若挂在
                # 清单末尾会与清单一起被砍，模型只能拿到「无标记的残缺列清单」，
                # 进而把没列出的列当成不存在。提示与事实同生共死才安全。
                lines.append(
                    f"表 {key} 实际可用列（仅列出前 {_RETRY_MAX_AVAILABLE_COLUMNS} 个，"
                    f"共 {total} 个）：{', '.join(shown)}"
                )
            else:
                lines.append(f"表 {key} 实际可用列：{', '.join(shown)}")
        if len(schema_issues) > _RETRY_MAX_SCHEMA_ISSUES:
            lines.append(
                f"（另有 {len(schema_issues) - _RETRY_MAX_SCHEMA_ISSUES} 个字段引用错误未列出，"
                "请按可用列清单自行核对）"
            )
        logger.info(
            "SQL 重试上下文 schema 分支完成",
            retry=retry,
            total_issues=len(schema_issues),
            rendered_issues=len(rendered_issues),
            tables=len(grouped),
            context_chars=len("\n".join(lines)),
        )
        return "\n".join(lines)
    previous_sql = str(state.get("generated_sql", "") or "")[:_RETRY_MAX_PREVIOUS_SQL_CHARS]
    execution_error = str(state.get("execution_error", "") or "")
    # EXPLAIN 校验失败（layer4_explain）只写 explain_errors，不写 validation_errors，
    # 也不写 execution_error。不回注这一段，重试段就会渲染成「错误原因: []」，
    # 模型看不到数据库报错原文，只能照着失败的 SQL 再抄一遍——实测三次重试产出的
    # SQL 逐字节相同（ClickHouse code 184、Oracle ORA-00942 两条链路均命中）。
    raw_explain_errors = [
        error
        for error in (state.get("explain_errors", []) or [])
        if isinstance(error, dict)
    ]
    explain_errors, omitted_explain = _bounded_explain_errors(raw_explain_errors)
    errors: list[object] = list(validation_errors) + explain_errors
    if execution_error:
        errors.append(execution_error)
    if omitted_explain:
        errors.append(f"（另有 {omitted_explain} 条 EXPLAIN 错误未列出）")
    reason = (
        json.dumps(errors, ensure_ascii=False)
        if errors
        else "（上一轮未记录结构化错误，请按当前 Schema 逐列复核列名、函数与数据类型）"
    )
    lines = [
        f"## 第 {retry} 次重试：修正上一轮 SQL",
        # 契约句必须前置：retry_error 段是「硬上限 + 尾部截断」，放在末尾
        # 一旦超预算会被整行砍掉，模型就失去「不得照抄」这条核心约束。
        "禁止逐字复用上一轮 SQL，必须针对下列错误原因修改后重新生成。",
    ]
    if retry >= 2:
        lines.append("这是最后一次尝试，必须使用 Schema 中明确列出的标识符。")
    # 错误原因排在失败 SQL 之前：截断先砍尾部，原因被砍掉等价于回到「错误原因: []」。
    lines.append(f"错误原因: {reason}")
    lines.append(f"失败的 SQL: {previous_sql}")
    lines.append("请严格检查列名、函数和数据类型是否来自当前 Schema。")
    logger.info(
        "SQL 重试上下文通用分支完成",
        retry=retry,
        error_count=len(errors),
        explain_error_count=len(explain_errors),
        omitted_explain=omitted_explain,
        context_chars=len("\n".join(lines)),
    )
    return "\n".join(lines)


# 方法作用：把 EXPLAIN 校验错误压成有界的回注条目。
# Args: explain_errors - layer4_explain 写入的结构化错误列表（元素为 dict）。
# Returns: (回注用错误列表, 未列出的条数)。
def _bounded_explain_errors(explain_errors: list[dict]) -> tuple[list[dict], int]:
    """EXPLAIN 错误逐条限长、整体限条数，防止重试段被自身证据撑爆。"""
    logger.debug("EXPLAIN 错误回注入口", total=len(explain_errors))
    rendered: list[dict] = []
    for error in explain_errors[:_RETRY_MAX_EXPLAIN_ERRORS]:
        item = dict(error)
        message = item.get("message")
        if isinstance(message, str) and len(message) > _RETRY_MAX_EXPLAIN_MESSAGE_CHARS:
            item["message"] = message[:_RETRY_MAX_EXPLAIN_MESSAGE_CHARS] + "（已截断）"
        rendered.append(item)
    omitted = max(0, len(explain_errors) - _RETRY_MAX_EXPLAIN_ERRORS)
    logger.debug("EXPLAIN 错误回注完成", rendered=len(rendered), omitted=omitted)
    return rendered, omitted


# 将 state 中分散的业务证据组装为 SQL 模型可审计的上下文块。
# Args: state - 当前 LangGraph 分析状态。
# Returns: 按证据类型分段的文本；无证据时返回明确占位。
def build_sql_grounding_context(state: AnalysisState) -> str:
    """组装意图、业务规则、知识命中、枚举字典和已验证 SQL 示例。"""
    from src.graph.context import read_contexts

    contexts = read_contexts(state)
    logger.debug(
        "组装 SQL 证据上下文入口",
        intent=contexts.routing.intent,
        rule_chars=len(state.get("business_rules_text", "") or ""),
        knowledge_chars=len(state.get("long_term_memories_text", "") or ""),
    )
    try:
        sections: list[str] = []
        intent = contexts.routing.intent
        if intent:
            sections.append(f"### 查询意图\n{intent}")
        if state.get("needs_decompose"):
            steps = state.get("decompose_steps", []) or []
            formatted_steps = [
                f"{step.get('step')}. {step.get('question', '')}"
                + (
                    f"（依赖步骤: {step.get('depends_on')}）"
                    if step.get("depends_on")
                    else ""
                )
                for step in steps
                if isinstance(step, dict) and step.get("question")
            ]
            if formatted_steps:
                sections.append(
                    "### 查询规划\n"
                    "将以下步骤合并为一条可审计的只读 SQL，优先使用 CTE 表达中间结果；"
                    "若无法在单条 SQL 中证明语义正确，则返回空 SQL 并说明原因。\n"
                    + "\n".join(formatted_steps)
                )
        business_rules = str(state.get("business_rules_text", "") or "").strip()
        if business_rules:
            sections.append(f"### 业务规则与指标口径\n{business_rules}")
        knowledge = str(state.get("long_term_memories_text", "") or "").strip()
        if knowledge:
            sections.append(f"### 知识库相关内容\n{knowledge}")
        enum_dictionary = state.get("enum_dictionary", {}) or {}
        if enum_dictionary:
            sections.append(
                "### 字段合法枚举\n" + json.dumps(
                    enum_dictionary, ensure_ascii=False, sort_keys=True,
                )
            )
        examples = state.get("few_shot_examples", []) or []
        if examples:
            formatted_examples: list[str] = []
            for index, example in enumerate(examples[:3], start=1):
                if not isinstance(example, dict):
                    continue
                question = str(example.get("question", "") or "").strip()
                sql = str(example.get("sql", "") or "").strip()
                if question and sql:
                    formatted_examples.append(
                        f"示例 {index}\n问题: {question}\nSQL: {sql}"
                    )
            if formatted_examples:
                sections.append("### 已验证 SQL 示例\n" + "\n\n".join(formatted_examples))
        result = "\n\n".join(sections) if sections else "(无额外业务证据，以 Schema 为准)"
        logger.debug(
            "组装 SQL 证据上下文完成",
            section_count=len(sections),
            chars=len(result),
        )
        return result
    except Exception as exc:
        logger.error("组装 SQL 证据上下文失败", error=str(exc), exc_info=True)
        raise


async def _llm_generate(
    schema_text: str,
    dialect_hint: str,
    dialect: str,
    query: str,
    error_ctx: str,
    skill_prompt: str,
    grounding_context: str,
    config: RunnableConfig,
    conversation_history: list | None = None,
    table_whitelist: str = "",
    data_profile: str = "",
    probe_context: Any | None = None,
) -> tuple[str, str, str]:
    """
    调用 LLM 流式生成 SQL 的核心逻辑，返回 (sql, reasoning_content, explanation)。

    使用 astream + config 实现真流式：
    - DeepSeek 的 reasoning_content 仅用于服务端诊断计数，不进入响应或历史
    - content token 逐 chunk 推送 → 前端实时打字机效果

    支持的响应格式（按优先级）：
      - `` ```json {"sql": "..."} ``` `` → 提取 sql 字段
      - `` ```sql ... ``` `` → 直接返回 SQL 代码块内容
      - `` ``` ... ``` `` → 尝试 JSON 解析
      - `SELECT ...;` → 正则提取第一个 SELECT 语句
      - 纯文本 → 直接返回
    """
    logger.debug(
        "LLM SQL 生成入口",
        dialect=dialect,
        query=query[:80],
        schema_chars=len(schema_text),
        grounding_chars=len(grounding_context),
    )
    # 在线程池构造同步 ChatModel，避免多源并发时阻塞事件循环。
    llm = await asyncio.to_thread(get_llm, 0)

    # 7.5.3 注入对话上下文（热/温/冷三层裁剪）
    context_text = ""
    if conversation_history:
        from src.memory.context_builder import build_llm_context
        context_text = await build_llm_context(
            conversation_history,
            query,
            node_name="generate_sql",
            model=llm,
        )

    from datetime import datetime, timezone
    _utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    _current_time_functions = {
        "clickhouse": "now() / today()",
        "mysql": "NOW() / CURDATE()",
        "postgres": "CURRENT_TIMESTAMP / CURRENT_DATE",
        "sqlite": "datetime('now') / date('now')",
        "oracle": "SYSTIMESTAMP / SYSDATE",
        "mssql": "SYSUTCDATETIME() / GETDATE()",
    }
    _time_function = _current_time_functions.get(
        dialect.lower(), "CURRENT_TIMESTAMP / CURRENT_DATE",
    )
    _now_info = (
        f"UTC: {_utc}。{dialect} 当前时间函数: {_time_function}；"
        "用户指定具体日期时使用明确边界，不混用其他方言函数。"
    )
    whitelist_prompt = f"## 物理表强约束\n{table_whitelist}" if table_whitelist else ""
    prompt_sections = [
            PromptSection(
                "skill",
                skill_prompt,
                priority=75,
                max_chars=1500,
                target="system",
            ),
            PromptSection(
                "table_whitelist",
                whitelist_prompt,
                priority=110,
                min_chars=len(whitelist_prompt),
                max_chars=len(whitelist_prompt),
            ),
            PromptSection(
                "query",
                f"## 用户问题\n{query}\n请生成 SQL:",
                priority=105,
                min_chars=600,
                max_chars=2500,
            ),
            PromptSection(
                "retry_error",
                error_ctx,
                priority=90,
                min_chars=300,
                max_chars=1800,
            ),
            PromptSection(
                "schema",
                f"## 数据库表结构\n{schema_text}",
                priority=100,
                min_chars=1800,
                max_chars=_PROMPT_SCHEMA_MAX_CHARS,
            ),
            PromptSection(
                "data_profile",
                data_profile,
                priority=95,
                min_chars=200,
                max_chars=_DATA_PROFILE_SECTION_MAX_CHARS,
            ),
            PromptSection(
                "dialect",
                f"## 方言参考\n{dialect_hint}",
                priority=90,
                min_chars=300,
                max_chars=1200,
            ),
            PromptSection(
                "current_time",
                f"## 当前时间\n{_now_info}",
                priority=85,
                min_chars=150,
                max_chars=500,
            ),
            PromptSection(
                "grounding",
                f"## 业务与知识上下文\n{grounding_context}",
                priority=60,
                min_chars=300,
                max_chars=10000,
            ),
            PromptSection(
                "history",
                f"## 对话历史\n{context_text}" if context_text else "",
                priority=30,
                max_chars=1500,
            ),
        ]

    # 探针作用域只围绕本次 prepare/stream，finally 复原，不污染并行请求；
    # 探针上下文为空（未启用或降级）时完全不进入该作用域。
    probe_stack = ExitStack()
    if probe_context is not None:
        try:
            probe_stack.enter_context(_data_probe_context_scope(probe_context))
        except Exception as exc:
            # 探查工具尚未就绪或作用域绑定失败：按无探针继续，不阻塞生成。
            logger.info("探针作用域未绑定，按无探针继续", reason=type(exc).__name__)

    try:
        from src.llm.invocation import prepare_invocation

        prepared = prepare_invocation(
            "sql.generate",
            prompt_sections,
            task="generate_sql",
            system_values={"dialect": dialect, "skill_instructions": ""},
            config=dict(config or {}),
            metadata={"node": "generate_sql", "dialect": dialect},
            model=llm,
        )

        # ── 流式消费：逐 chunk 累积，LangGraph 自动捕获事件推送到前端 ──
        stream_started_at = time.monotonic()
        logger.debug(
            "SQL LLM 流式调用边界输入",
            model_type=type(prepared.model).__name__,
            streaming=bool(getattr(prepared.model, "streaming", False)),
            stream_chunk_timeout=getattr(prepared.model, "stream_chunk_timeout", None),
            message_count=len(prepared.messages),
            prompt_chars=sum(len(str(getattr(message, "content", "") or "")) for message in prepared.messages),
        )
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        chunk_count = 0
        try:
            # 统一有界工具循环；中间工具回合的正文以重置标记隔离，不进入 SQL JSON。
            from src.llm.knowledge_loop import KnowledgeRoundReset, stream_with_knowledge

            async for chunk in stream_with_knowledge(prepared):
                if isinstance(chunk, KnowledgeRoundReset):
                    logger.info(
                        "SQL 生成丢弃中间工具回合正文",
                        round_index=chunk.round_index,
                        reason=chunk.reason,
                        discarded_chars=sum(len(part) for part in content_parts),
                    )
                    content_parts.clear()
                    reasoning_parts.clear()
                    continue
                chunk_count += 1
                if chunk_count == 1:
                    logger.debug(
                        "SQL LLM 首个流式块到达",
                        elapsed_ms=round((time.monotonic() - stream_started_at) * 1000),
                        chunk_type=type(chunk).__name__,
                    )
                # 提取 content（兼容 AIMessageChunk.content 和 ChatGenerationChunk.text）
                chunk_content = ""
                if hasattr(chunk, "content") and chunk.content:
                    chunk_content = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                elif hasattr(chunk, "text") and chunk.text:
                    chunk_content = chunk.text if isinstance(chunk.text, str) else str(chunk.text)
                if chunk_content:
                    content_parts.append(chunk_content)

                # 提取 reasoning_content（DeepSeek 思考链）
                reasoning = _extract_chunk_reasoning(chunk)
                if reasoning:
                    reasoning_parts.append(reasoning)
        except Exception as stream_exc:
            from langchain_openai import StreamChunkTimeoutError

            received_chunks = max(
                chunk_count,
                int(getattr(stream_exc, "chunks_received", 0) or 0),
            )
            if not isinstance(stream_exc, StreamChunkTimeoutError) or received_chunks > 0:
                raise
            logger.warning(
                "SQL LLM 流式首块超时，切换非流式回退",
                timeout_s=getattr(stream_exc, "timeout_s", None),
                chunks_received=received_chunks,
                prompt_id=prepared.prompt_id,
            )
            fallback_llm = await asyncio.to_thread(get_llm, 0, stream=False)
            # 回退同样走统一循环，保持工具与身份边界一致；工具不再可用时退化为原一次调用。
            from dataclasses import replace as _replace

            from src.llm.knowledge_loop import invoke_with_knowledge

            response = await invoke_with_knowledge(_replace(prepared, model=fallback_llm))
            fallback_content = getattr(response, "content", response)
            if fallback_content:
                content_parts.append(
                    fallback_content
                    if isinstance(fallback_content, str)
                    else str(fallback_content)
                )
            fallback_reasoning = _extract_chunk_reasoning(response)
            if fallback_reasoning:
                reasoning_parts.append(fallback_reasoning)
            logger.info(
                "SQL LLM 非流式回退完成",
                content_chars=sum(len(part) for part in content_parts),
                reasoning_chars=sum(len(part) for part in reasoning_parts),
            )

        raw = "".join(content_parts)
        reasoning_text = "".join(reasoning_parts)
        if reasoning_parts:
            logger.debug(
                "LLM 推理链已收集",
                reasoning_chunks=len(reasoning_parts),
                reasoning_chars=len(reasoning_text),
            )
        logger.debug("LLM 原始响应", raw=raw[:500], content_chunks=len(content_parts))

        # ── 响应内容为空时的回退 ──
        if not raw or raw.strip() == "":
            logger.error("LLM 返回空内容")
            return "-- LLM 返回空内容", reasoning_text, ""

        text = raw.strip()

        # ── 格式解析 ──
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```sql" in text:
            return text.split("```sql")[1].split("```")[0].strip(), reasoning_text, ""
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        try:
            from src.llm.output_contracts import SQLGenerationOutput, parse_json_model

            data = parse_json_model(text, SQLGenerationOutput)
            explanation = data.explanation
            sql = data.sql.strip()
            logger.info(
                "LLM SQL 生成完成",
                dialect=dialect,
                sql_chars=len(sql),
                explanation_chars=len(explanation),
            )
            return sql, reasoning_text, explanation
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug(
                "LLM SQL JSON 解析回退",
                error=str(exc),
                text_chars=len(text),
                exc_info=True,
            )

        if "SELECT" in text.upper():
            match = re.search(r"SELECT[\s\S]*?(?:;|$)", text, re.IGNORECASE)
            if match:
                return match.group(0).strip().rstrip(";"), reasoning_text, ""

        return text.strip(), reasoning_text, ""

    except Exception as e:
        logger.error("LLM 调用失败", error=str(e), exc_info=True)
        return "-- LLM 调用失败，请检查 API Key 配置", "", ""
    finally:
        # 无论成功、失败还是取消，都精确复原探针作用域。
        probe_stack.close()


def _extract_chunk_reasoning(chunk) -> str:
    """从流式 chunk 中提取 reasoning_content，兼容多种 LangChain 版本。

    支持 AIMessageChunk 和 ChatGenerationChunk（通过 .message 解包）。
    """
    target = chunk
    if hasattr(chunk, "message") and not hasattr(chunk, "additional_kwargs"):
        target = chunk.message

    if hasattr(target, "additional_kwargs") and isinstance(target.additional_kwargs, dict):
        r = target.additional_kwargs.get("reasoning_content", "")
        if r:
            return r if isinstance(r, str) else str(r)

    if hasattr(target, "response_metadata") and isinstance(target.response_metadata, dict):
        choices = target.response_metadata.get("choices", [])
        if choices and isinstance(choices, list):
            delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else {}
            r = delta.get("reasoning_content", "") if isinstance(delta, dict) else ""
            if r:
                return r if isinstance(r, str) else str(r)

    if hasattr(target, "reasoning_content") and target.reasoning_content:
        rc = target.reasoning_content
        return rc if isinstance(rc, str) else str(rc)

    return ""


def _template_generate(tables: list[dict], retry: int, state: AnalysisState) -> str:
    """在无 LLM 时仅生成语义可证明的数量查询。

    Args:
        tables: 已解析的候选表结构。
        retry: 当前重试次数。
        state: LangGraph 当前状态。

    Returns:
        可证明正确的 SQL；无法确定语义时返回空字符串。
    """
    from src.graph.context import read_contexts

    query = read_contexts(state).request.user_query.strip()
    logger.debug("SQL 模板回退入口", query=query[:80], retry=retry, table_count=len(tables))
    if retry > 0 or not tables:
        logger.info("SQL 模板回退停止", reason="重试不可修复或无候选表")
        return ""

    count_keywords = ("数量", "总数", "多少", "几条", "条数", "个数", "计数", "行数", "记录数")
    is_count_query = any(keyword in query for keyword in count_keywords) or query.endswith("数")
    if is_count_query:
        table_name = tables[0].get("name", "")
        if table_name:
            sql = f"SELECT COUNT(*) AS count FROM {table_name}"
            logger.info("SQL 模板回退完成", mode="count", table=table_name)
            return sql

    logger.info("SQL 模板回退停止", reason="语义不确定")
    return ""


def _missing_time_filter(sql: str, query: str) -> bool:
    """SQL 有聚合 + 含日期列但无日期过滤 + 非累计查询 → 需要时间范围。不依赖具体表名。"""
    import re
    sql_upper = sql.upper()

    # 非聚合查询（无 SUM/COUNT/AVG/GROUP BY）不需要时间过滤
    if not re.search(r'\b(SUM|COUNT|AVG|GROUP\s+BY)\b', sql_upper):
        return False

    # SQL 中引用的列名是否包含日期特征（任何含 date/time 的列名）
    cols = set(re.findall(r'[a-z_]{3,}', sql_upper))
    date_cols = {c for c in cols if any(w in c for w in ('DATE', 'TIME'))}
    if not date_cols:
        return False

    # 日期列已在 WHERE 中做了过滤
    where_match = re.search(r'\bWHERE\b(.+?)(?:\bGROUP\b|\bORDER\b|\bLIMIT\b|$)', sql_upper, re.DOTALL)
    where_clause = where_match.group(1) if where_match else ""
    if any(dc in where_clause for dc in date_cols):
        return False

    # 累计/排名/总量类不需要时间过滤
    cumulative = ("累计", "总量", "排行", "排名", "榜", "历史", "总共",
                  "全部客户", "所有客户", "每个客户", "一共有多少")
    if any(w in query for w in cumulative):
        return False

    # 用户明确指定了时间
    user_time = ("最近", "去年", "今年", "本月", "上周", "本周", "今天", "昨天",
                 "202", "全部数据", "所有时间", "不限时间", "不限制时间")
    if any(w in query for w in user_time):
        return False

    return True


# 方法作用：按真实数据库方言解析 SQL，并拦截 Schema 或 CTE 之外的表引用。
# Args: sql - 待校验 SQL；tables - Schema 已知表；dialect - 数据源真实方言。
# Returns: 未知表名列表；解析失败时返回失败关闭标记。
def _check_table_hallucination(
    sql: str,
    tables: list[dict],
    dialect: str = "",
) -> list[str]:
    """12.1.6 检查 SQL 中引用的表名是否在 relevant_tables 中存在。

    使用 sqlglot 提取 FROM/JOIN 子句中的表引用，与已知表名比对，拦截 LLM 幻觉。
    表名判定复用 src.security.sql_schema_validation 的表索引与归属解析，
    与 layer3 / execute_sql 共用同一实现，避免多处判定各自漂移。
    """
    if not tables:
        logger.info("表名幻觉校验跳过", reason="Schema 表为空")
        return []
    # 表名判定统一走共享校验模块，避免与 layer3 / execute_sql 的表名判定各自漂移
    from src.security.sql_schema_validation import (
        build_table_index,
        is_system_table,
        resolve_table_owner,
    )

    # 单源快照统一落到默认源名下：白名单真实生效（越界源的表被跳过，不再 fail-open），
    # 同时避开空白名单在热路径上每次校验都打「未提供数据源白名单」告警的噪音。
    # 与 execute_sql._validate_column_references 保持同一约定，不得改回 [""]。
    index = build_table_index(tables, ["__local__"])
    known_names = sorted({t["name"].lower() for t in tables if t.get("name")})
    unknown: list[str] = []
    parser_dialect = normalize_sql_dialect(dialect)
    logger.debug(
        "表名幻觉校验边界输入",
        known_tables=known_names,
        parser_dialect=parser_dialect or "default",
        sql=sql,
    )
    try:
        import sqlglot
        from sqlglot import exp

        parsed = sqlglot.parse(sql, read=parser_dialect or None)
        logger.debug(
            "表名幻觉校验解析完成",
            parsed_type=type(parsed).__name__,
            statement_count=len(parsed),
            parser_dialect=parser_dialect or "default",
        )
        for statement in parsed:
            if statement is None:
                continue
            # CTE 名会出现在 find_all(exp.Table) 中，必须显式排除，否则 CTE 查询全被误拦
            cte_names = {
                str(cte.alias_or_name).lower()
                for cte in statement.find_all(exp.CTE)
                if cte.alias_or_name
            }
            for table in statement.find_all(exp.Table):
                table_name = str(table.name or "").strip().lower()
                if not table_name or table_name in cte_names or table_name in unknown:
                    continue
                # 系统伪表（如 Oracle 的 dual）合法但不在业务快照，判为幻觉会误拦正确 SQL、
                # 触发无谓重试，重试次数耗尽后整个分析单元失败（线上实证）
                if is_system_table(table_name, dialect):
                    logger.debug("表名幻觉校验命中系统伪表", table=table_name, dialect=dialect)
                    continue
                # 统一判定：归属唯一即合法；不存在才算幻觉候选
                if resolve_table_owner(index, table_name) is None:
                    unknown.append(table_name)
    except Exception as exc:
        logger.error(
            "表名幻觉校验异常",
            error=str(exc),
            parser_dialect=parser_dialect or "default",
            known_tables=known_names,
            exc_info=True,
        )
        # 失败关闭：解析异常不得当作「无幻觉」放行，调用方见非空结果即拦截并触发重试
        return ["SQL 解析失败，已阻断执行"]
    logger.debug("表名幻觉校验完成", unknown_tables=unknown, unknown_count=len(unknown))
    return unknown


def format_schema_for_prompt(tables: list[dict], dialect: str = "") -> str:
    """将表结构列表格式化为 Markdown 表格，含精确列名、样本值、方言约束。

    Args:
        tables - relevant_tables 列表
        dialect - 数据源方言，用于追加引用约束
    """
    if not tables:
        return "(无可用表结构)"
    lines = []
    # 追加列名列白名单，方便 LLM 精确引用
    all_columns: list[str] = []
    for t in tables:
        for c in t.get("columns", []):
            name = c.get("name", "")
            if name and name not in all_columns:
                all_columns.append(name)

    lines.append("## 重要约束")
    lines.append(f"- 本数据源方言: {dialect or 'SQL'}")
    lines.append(f"- 所有可用列名（白名单）: {', '.join(all_columns)}")
    lines.append("- **必须使用上述列名，禁止编造或猜测任何列名**")
    lines.append("- **禁止使用不存在的函数，参考下方方言参考**")
    lines.append("")

    for t in tables:
        lines.append(f"### 表: {t['name']} — {t.get('description', '')}")
        lines.append("| 字段 | 类型 | 约束 | 说明 |")
        lines.append("|------|------|------|------|")
        for c in t.get("columns", []):
            sample = c.get("sample", "")
            comment = c.get("comment", "") or ""
            if sample:
                comment = f"{comment}（示例: {sample}）" if comment else f"示例: {sample}"
            enum_values = c.get("enum_values", []) or []
            if enum_values:
                enum_text = ", ".join(str(value) for value in enum_values[:20])
                comment = f"{comment}；枚举: {enum_text}" if comment else f"枚举: {enum_text}"
            constraints: list[str] = []
            if c.get("is_primary_key"):
                constraints.append("PK")
            elif c.get("is_indexed"):
                constraints.append("INDEX")
            constraints.append("NULL" if c.get("is_nullable", True) else "NOT NULL")
            lines.append(
                f"| {c['name']} | {c['type']} | {', '.join(constraints)} | {comment} |"
            )
        relations = t.get("relations", []) or []
        if relations:
            lines.append("\n关联关系（只能基于这些关系 JOIN）:")
            for relation in relations:
                lines.append(
                    f"- {relation.get('relation_type', '')}: {t['name']} -> "
                    f"{relation.get('target_table', '')} ON {relation.get('join_key', '')}"
                )
        # 追加行数提示
        row_est = t.get("row_count_estimate") or t.get("row_estimate")
        if row_est:
            lines.append(f"\n（约 {row_est} 行）")
        partition_key = t.get("partition_key", "")
        if partition_key:
            lines.append(f"分区键: {partition_key}")
        lines.append("")
    return "\n".join(lines)


# 方法作用：把文本切成可用于字段匹配的中英文标识符片段。
# Args: value - 表名、列名或业务说明文本。
# Returns: 去重后的低成本匹配片段集合。
def _schema_match_tokens(value: Any) -> set[str]:
    text = str(value or "").strip().lower()
    if not text:
        return set()
    tokens = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", text))
    for token in list(tokens):
        if re.fullmatch(r"[a-z0-9]+", token):
            tokens.update(part for part in token.split("_") if part)
    return tokens


# 方法作用：提取列的物理名称和业务别名，供问题相关性评分复用。
# Args: column - Schema 中的列字典。
# Returns: 小写别名集合。
def _column_aliases(column: dict[str, Any]) -> set[str]:
    aliases: list[Any] = [
        column.get("name", ""),
        column.get("comment", ""),
        column.get("description", ""),
        column.get("business_name", ""),
    ]
    raw_aliases = column.get("aliases", []) or []
    aliases.extend(raw_aliases if isinstance(raw_aliases, (list, tuple, set)) else [raw_aliases])
    return {
        str(alias).strip().lower()
        for alias in aliases
        if str(alias or "").strip()
    }


# 方法作用：根据中英文业务词汇判断列是否与用户问题相关。
# Args: query - 用户原始问题；column - 待评估列；table_name - 所属表名。
# Returns: 大于零表示应优先保留，数值越大相关性越强。
def _column_match_score(query: str, column: dict[str, Any], table_name: str = "") -> int:
    query_text = str(query or "").strip().lower()
    if not query_text:
        return 0
    aliases = _column_aliases(column)
    score = 0
    for alias in aliases:
        if any(ord(char) > 127 for char in alias):
            if alias in query_text:
                score += 6
        else:
            alias_tokens = _schema_match_tokens(alias)
            query_tokens = _schema_match_tokens(query_text)
            if alias in query_tokens:
                score += 8
            score += min(3, len(alias_tokens & query_tokens))
    # 常见中文业务词与物理列名之间没有显式注释时，使用受控同义词兜底。
    name_text = " ".join(aliases)
    name_tokens = _schema_match_tokens(name_text)
    table_tokens = _schema_match_tokens(table_name)
    for chinese, english_terms in _COLUMN_SYNONYMS.items():
        matched_terms = name_tokens.intersection(english_terms)
        # orders.order_status 中的 order 只是表级公共前缀，不能让所有列都被判为相关。
        if chinese in query_text and matched_terms.difference(table_tokens):
            score += 4
    return score


# 方法作用：从索引、主键和关系表达式中提取必须保留的列名。
# Args: table - 单张表的授权 Schema 字典。
# Returns: 小写物理列名集合。
def _required_schema_columns(table: dict[str, Any]) -> set[str]:
    required: set[str] = set()
    columns = [
        column for column in table.get("columns", []) or []
        if isinstance(column, dict)
    ]
    known = {
        str(column.get("name", "") or "").strip().lower()
        for column in columns
        if str(column.get("name", "") or "").strip()
    }
    for column in columns:
        name = str(column.get("name", "") or "").strip().lower()
        if name and (column.get("is_primary_key") or column.get("is_indexed")):
            required.add(name)
    # 键列判断结果（apply_key_hints 写入）并入必保集合。提示列先按物理身份精确匹配：
    # 未知与未授权列被过滤；同表存在大小写变体（Secret/secret）时匹配不唯一，
    # 宁可放弃该提示保护，也不能把大小写不同的未授权变体列重新保留。
    exact_names = [
        str(column.get("name", "") or "").strip()
        for column in columns
        if str(column.get("name", "") or "").strip()
    ]
    for column_name in table.get("_profile_key_columns", []) or []:
        matched = match_identifier(exact_names, column_name)
        if matched is not None:
            required.add(matched.lower())
    for index in table.get("indexes", []) or []:
        if not isinstance(index, dict):
            continue
        index_columns = index.get("columns", []) or []
        if isinstance(index_columns, str):
            index_columns = [index_columns]
        required.update(
            str(name).strip().lower()
            for name in index_columns
            if str(name or "").strip()
        )
    relation_texts = [
        str(relation.get("join_key", "") or "")
        for relation in table.get("relations", []) or []
        if isinstance(relation, dict)
    ]
    relation_texts.append(str(table.get("partition_key", "") or ""))
    for relation_text in relation_texts:
        for identifier in re.findall(r"(?:[A-Za-z_][\w$]*\.)?([A-Za-z_][\w$]*)", relation_text):
            normalized = identifier.strip().lower()
            if normalized in known:
                required.add(normalized)
    return required


# 方法作用：判断列名是否明显属于内部备注或审计字段。
# Args: column - Schema 中的列字典。
# Returns: 属于低价值内部字段时返回 True。
def _is_internal_schema_column(column: dict[str, Any]) -> bool:
    tokens = _schema_match_tokens(column.get("name", ""))
    return bool(tokens & _INTERNAL_COLUMN_MARKERS)


# 方法作用：复制并按问题相关性裁剪单张表的列，避免修改授权 Schema。
# Args: table - 完整授权表；query - 用户问题。
# Returns: 可安全放入 Prompt 的独立表字典。
def _prune_prompt_table_columns(table: dict[str, Any], query: str) -> dict[str, Any]:
    copied = copy.deepcopy(table)
    columns = [
        column for column in copied.get("columns", []) or []
        if isinstance(column, dict) and str(column.get("name", "") or "").strip()
    ]
    if not columns:
        copied["columns"] = []
        return copied
    table_name = str(copied.get("name", "") or "")
    required = _required_schema_columns(copied)
    scored = [
        (_column_match_score(query, column, table_name), index, column)
        for index, column in enumerate(columns)
    ]
    # 命中列优先，其余列按原列序补足到每表上限。命中与未命中两条路径必须都保留
    # 「未命中但可用」的列：只保留 required|relevant 会让没有中文同义词的英文业务列
    # （year_month、region_code、channel_code、scenario、net_amount）整体消失。
    # 实测 125 张表的 retail 库被裁到只剩 278 列（retail_orders 仅 id/event_date/
    # order_status），模型据此判定「字段不存在」并拒绝生成 SQL。
    # 明显内部备注仍要排除，避免它们占用每表额度。
    ranked_optional = sorted(
        (
            (
                score,
                -index,
                str(column.get("name", "") or "").strip().lower(),
            )
            for score, index, column in scored
            if str(column.get("name", "") or "").strip().lower() not in required
            and not _is_internal_schema_column(column)
        ),
        reverse=True,
    )
    selected_names = required | {
        name
        for _, _, name in ranked_optional[: max(0, _PROMPT_COLUMNS_PER_TABLE - len(required))]
    }
    copied["columns"] = [
        column for column in columns
        if str(column.get("name", "") or "").strip().lower() in selected_names
    ]
    return copied


# 方法作用：按用户问题相关性裁剪详细 Schema，同时保留至少一张可用表。
# Args: tables - 完整授权表结构；query - 用户问题；dialect - 数据库方言；max_chars - 详细 Schema 上限。
# Returns: 不超过字符预算的独立表列表；空输入返回空列表。
def select_prompt_tables(
    tables: list[dict],
    query: str,
    *,
    dialect: str = "",
    max_chars: int = _PROMPT_SCHEMA_MAX_CHARS,
) -> list[dict]:
    if max_chars <= 0:
        raise ValueError("max_chars 必须大于零")
    if not tables:
        return []
    query_tokens = _schema_match_tokens(query)
    ranked: list[tuple[int, int, dict]] = []
    for index, table in enumerate(tables):
        if not isinstance(table, dict):
            continue
        searchable = " ".join([
            str(table.get("name", "") or ""),
            str(table.get("description", "") or ""),
            " ".join(
                " ".join(sorted(_column_aliases(column)))
                for column in table.get("columns", []) or []
                if isinstance(column, dict)
            ),
        ]).lower()
        tokens = _schema_match_tokens(searchable)
        overlap = len(query_tokens & tokens) if query_tokens else 0
        # 列级评分可以识别中文业务别名，作为表排序的补充信号。
        overlap += sum(
            min(3, _column_match_score(query, column, str(table.get("name", ""))))
            for column in table.get("columns", []) or []
            if isinstance(column, dict)
        )
        ranked.append((overlap, -index, table))
    ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
    if not ranked:
        return []

    selected: list[dict] = []
    for _, _, table in ranked:
        prompt_table = _prune_prompt_table_columns(table, query)
        candidate = selected + [prompt_table]
        rendered = format_schema_for_prompt(candidate, dialect)
        if len(rendered) <= max_chars or not selected:
            selected.append(prompt_table)
        if selected and len(rendered) > max_chars and len(selected) > 1:
            selected.pop()
            break
    logger.debug(
        "详细 Schema 按表按列裁剪完成",
        query=query[:80],
        total=len(tables),
        selected=len(selected),
        selected_columns=sum(len(table.get("columns", []) or []) for table in selected),
        matched=[table.get("name", "") for _, _, table in ranked[:len(selected)]],
        max_chars=max_chars,
        rendered_chars=len(format_schema_for_prompt(selected, dialect)),
    )
    return selected
