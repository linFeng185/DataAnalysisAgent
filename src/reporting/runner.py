"""报表任务运行器：节点日志、失败隔离、版本发布和并发配额。"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from src.logging_config import get_logger
from src.reporting.models import UnitResult
from src.reporting.quota import QuotaExceededError, QuotaManager, describe_quota_error
from src.reporting.store import ReportStore, get_report_store

logger = get_logger(__name__)

# 报表分析单元路由任务标识，供 LLM 任务路由选择本地或租户配置的模型。
_ROUTE_LLM_TASK = "report_route"
# 每个数据源最多放进路由 Prompt 的表名数量。
# 取 200 以覆盖上百张表的库（实测财务库 125 张）；一旦超预算被截断，
# 排在后面的数据源会整段丢失，路由就必然漏判。
_CATALOG_TABLES_PER_SOURCE = 200
# 指标口径来源的标识前缀，由 resolve_metric_references 生成。
_METRIC_SOURCE_PREFIX = "metric:"
# 相对时间范围的单位中文名；无法识别时按天处理，与 TimeRange 默认值 90d 一致。
_TIME_RANGE_UNIT_NAMES = {"d": "天", "w": "周", "m": "个月", "y": "年"}


# 方法作用：把单元的时间范围契约渲染成中文时间说明。
# Args: time_range - AnalysisUnit.time_range 的契约值。
# Returns: 可直接拼进查询文本的时间说明；无法识别时回退到原值。
def _describe_time_range(time_range: Any) -> str:
    logger.debug("时间范围渲染入口", kind=getattr(time_range, "kind", ""), value=getattr(time_range, "value", ""))
    kind = str(getattr(time_range, "kind", "") or "")
    value = str(getattr(time_range, "value", "") or "").strip()
    if kind == "all":
        logger.debug("时间范围渲染完成", text="全部数据", kind=kind)
        return "全部数据"
    if kind == "absolute":
        # 绝对范围改写为「起 至 止」并对下游声明取数要求：主图「问题里是否已含
        # 时间范围」的规则靠「日」字等时间标记识别，必须同时保留原始串与中文说明；
        # 90 天窗口只落回一个月时同比/环比基期取不到（2026-09-14 事故），
        # 必须显式要求覆盖整段区间，不能只靠一段没有约束力的日期串。
        parts = [part.strip() for part in re.split(r"[/:]", value) if part.strip()]
        span = " 至 ".join(parts) if len(parts) == 2 else value
        text = f"{span}（绝对日期范围 {value}；查询必须覆盖该区间，需要同比/环比时同时取到基期）" if value else ""
        logger.debug("时间范围渲染完成", text=text, kind=kind)
        return text
    match = re.fullmatch(r"(\d+)\s*([A-Za-z]?)", value)
    if not match:
        logger.debug("时间范围渲染回退到原值", value=value, kind=kind)
        return value
    amount = int(match.group(1))
    unit = _TIME_RANGE_UNIT_NAMES.get(match.group(2).lower(), "天")
    text = f"最近 {amount} {unit}"
    logger.debug("时间范围渲染完成", text=text, kind=kind)
    return text


# 快照 params 里承载样本处置结论的键名；不新开数据库列，随既有 params_json 落库。
_SAMPLE_DECISION_PARAM_KEY = "sample_decision"


# 方法作用：从返回行里取列名并集，供样本处置决策判断可选分组键。
# Args: rows - 进入分析链路的行。
# Returns: 去重后的列名列表；非字典行跳过。
def _collect_row_columns(rows: list[Any]) -> list[str]:
    logger.debug("样本列清单收集入口", row_count=len(rows))
    columns: dict[str, None] = {}
    for row in rows:
        if isinstance(row, dict):
            for name in row:
                columns.setdefault(str(name), None)
    logger.info("样本列清单收集完成", row_count=len(rows), column_count=len(columns))
    return list(columns)


# 方法作用：读取生效的样本行数上限，供样本处置与截断原因共用同一处口径。
# Args: 无。
# Returns: 配置的样本上限；读取失败时抛出，由调用方决定降级方式。
def _sample_row_limit() -> int:
    """执行层（execute_sql 的 masked_rows[:sample_limit]）就是按这个上限先裁再回传的。"""
    from src.config import get_settings

    return int(getattr(get_settings(), "report_max_sample_rows", 0) or 0)


# 方法作用：按真实切割点生成截断原因，调用方不再自带上限。
# Args: sample_rows - 进入分析链路的样本行数；full_count - 查询返回的全量行数。
# Returns: 可读的截断说明；上限读不到时按「未知上限」措辞，不编造数字。
def _describe_sample_truncation(sample_rows: int, full_count: int) -> str:
    """上限只从配置取：写死一个与执行层切割点无关的数字会让原因自相矛盾。"""
    from src.reporting.snapshots import describe_truncation

    try:
        limit = _sample_row_limit()
    except Exception:
        # 配置读不到时宁可少写一个数字，也不要拿别的口径冒充切割点。
        logger.error("样本上限读取失败，截断原因按未知上限措辞", exc_info=True)
        limit = 0
    text = describe_truncation(sample_rows, full_count, limit)
    logger.info("报表单元截断原因生成完成", sample_rows=sample_rows, full_count=full_count, limit=limit, has_reason=bool(text))
    return text


# 方法作用：单元行数规模超限或上游裁剪过样本时产出处置建议，并把结论写进快照参数与单元披露文案。
# Args: unit - 分析单元；trace - 快照采集字典（就地写入 params）；rows - 进入分析链路的行；
#       full_count - 查询返回的全量行数；sample_capped - 执行层上报的样本裁剪标记。
# Returns: 供单元 warnings 使用的披露文案列表；判据未触发或决策失败时返回空列表。
async def _decide_sample_guidance(
    unit: Any,
    trace: dict[str, Any],
    rows: list[Any],
    full_count: int,
    sample_capped: bool,
) -> list[str]:
    """只登记处置建议并如实披露，不触发任何自动重跑；重跑统一由补查回路负责。"""
    unit_id = str(getattr(unit, "unit_id", "") or "")
    entering = len(rows)
    full = max(0, int(full_count or 0))
    logger.debug("样本处置接线入口", unit_id=unit_id, entering_rows=entering, full_count=full, sample_capped=bool(sample_capped))
    try:
        limit = _sample_row_limit()
    except Exception:
        logger.error("样本上限读取失败，跳过处置决策", unit_id=unit_id, exc_info=True)
        return []
    try:
        from src.reporting.sample_guard import decide_sample_action, describe_sample_decision, needs_sample_action

        # 快速闸门与 sample_guard 内部判据共用同一个谓词，两处不可能脱钩：
        # ① 真实行数规模 max(进入链路行数, 查询全量行数) 超过样本上限；② 执行层已上报
        # query_result_sample_capped（上游裁过样本）。执行层是按同一个上限先裁再回传的，
        # 只拿裁完的行数去比上限永远比不出超限，「样本被裁过」就再也没人披露。
        # 外层放行、内层卡死的组合已被消除：闸门只负责省掉不必要的判据计算，
        # 放行后 decide_sample_action 必然给出结论（含「上游裁过但未超限」的直接披露）。
        if not needs_sample_action(row_count=entering, full_count=full, limit=limit, sample_capped=bool(sample_capped)):
            logger.info(
                "样本处置判据未触发，跳过决策",
                unit_id=unit_id,
                entering_rows=entering,
                full_count=full,
                limit=limit,
                sample_capped=bool(sample_capped),
            )
            return []
        decision = await decide_sample_action(
            question=str(getattr(unit, "question", "") or ""),
            columns=_collect_row_columns(rows),
            rows=rows,
            # 两个计数如实分开传：row_count 是进入分析链路的行数，full_count 是查询全量。
            # 把 max() 的结果当 row_count 传下去，探针与披露会把全量行数冒充成进入链路的
            # 行数（模型拿到假证据、用户看到错数字）。
            row_count=entering,
            full_count=full,
            # 时间跨度用该单元已确认的取数窗口；取不到时留空串，不替模型编一个区间。
            time_span=_describe_time_range(getattr(unit, "time_range", None)),
            limit=limit,
            # 上游裁剪标记下沉到 sample_guard：未超限但被裁过时只披露、不调用模型。
            sample_capped=bool(sample_capped),
        )
        text = describe_sample_decision(decision, limit)
    except Exception:
        # 决策属观测与披露：失败不得影响单元产出，更不得让单元变 failed。
        logger.error("样本处置决策失败，跳过披露", unit_id=unit_id, exc_info=True)
        return []
    if not text:
        # 判据已放行却拿不到结论属异常形态（sample_guard 在放行分支必有返回），
        # 保持单元产出不受影响，但记 info 便于与「确实没触发判据」区分。
        logger.info(
            "样本处置未产出披露文案",
            unit_id=unit_id,
            entering_rows=entering,
            full_count=full,
            limit=limit,
        )
        return []
    params = trace.setdefault("params", {})
    if isinstance(params, dict):
        # 结构化字段供后续补查与重跑回路消费，text 供页面与人工阅读，两者同源。
        params[_SAMPLE_DECISION_PARAM_KEY] = {
            "action": str(getattr(decision, "action", "") or ""),
            "reason": str(getattr(decision, "reason", "") or ""),
            "suggested_group_by": [str(name) for name in list(getattr(decision, "suggested_group_by", []) or [])],
            "suggested_range": str(getattr(decision, "suggested_range", "") or ""),
            "text": text,
        }
    logger.info(
        "样本处置建议已登记",
        unit_id=unit_id,
        action=str(getattr(decision, "action", "") or ""),
        text_chars=len(text),
    )
    return [text]


# 报表取数要求：明细不该进报表。行数超限的根因往往是 SQL 没聚合，
# 把要求前置到查询文本里，比事后在截断处补救更靠近源头。
_UNIT_AGGREGATION_REQUIREMENT = (
    "报表取数要求：优先在数据库侧完成聚合（GROUP BY 与聚合函数），只返回分析需要的结果行，"
    "不要把明细行带回上层统计；多个指标尽量一次查询取回。"
    "确实需要明细核验时，也要限制在能说明问题的最小行数。"
)


# 方法作用：把计划确定的时间范围与取数要求并入单元查询文本。
# Args: unit - 待执行的 AnalysisUnit。
# Returns: 供主图执行的查询文本；无可用时间范围时仍带上取数要求。
def _compose_unit_query(unit: Any) -> str:
    unit_id = str(getattr(unit, "unit_id", "") or "")
    question = str(getattr(unit, "question", "") or "")
    logger.debug("报表单元查询拼装入口", unit_id=unit_id, question_chars=len(question))
    hint = _describe_time_range(getattr(unit, "time_range", None))
    if not hint:
        logger.info("报表单元无可用时间范围，按原问题执行", unit_id=unit_id)
        return f"{question}。{_UNIT_AGGREGATION_REQUIREMENT}"
    # 时间范围在预览阶段就已绑定，执行期必须显式带上：主图 classify_intent 会按
    # 问题文本重判 needs_time_range，不带就会被拦成「请指定查询的时间范围」，
    # 白白丢掉一个参数早就确定的单元。
    # 取数要求追加成独立句子放在时间范围之后：括号串保持原样，下游按
    # 「时间范围：」定位的解析不受影响。
    query = f"{question}（时间范围：{hint}）。{_UNIT_AGGREGATION_REQUIREMENT}"
    logger.info("报表单元查询拼装完成", unit_id=unit_id, time_hint=hint, query_chars=len(query))
    return query


# 方法作用：从旧工作流返回的知识来源里筛出指标口径引用，供页面做口径溯源。
# Args: activated - 工作流状态中的 activated_knowledge 列表。
# Returns: 去重后的口径快照列表，每项含 code、name、expression、version。
def _extract_metric_citations(activated: Any) -> list[dict[str, Any]]:
    citations: dict[str, dict[str, Any]] = {}
    for item in list(activated or []):
        if not isinstance(item, dict):
            continue
        if not str(item.get("source_id", "") or "").startswith(_METRIC_SOURCE_PREFIX):
            continue
        locator = item.get("locator")
        if not isinstance(locator, dict):
            continue
        code = str(locator.get("metric_code", "") or "")
        if not code:
            continue
        citations[code] = {
            "code": code,
            "name": str(locator.get("name", "") or ""),
            "expression": str(locator.get("expression", "") or ""),
            "unit": str(locator.get("unit", "") or ""),
            "version": int(locator.get("version", 0) or 0),
        }
    if citations:
        logger.info("报表单元指标口径引用已解析", count=len(citations), codes=sorted(citations))
    return list(citations.values())


# 方法作用：从旧工作流的相关表证据里抽取列备注。
# Args: tables - 工作流状态中的 relevant_tables 列表。
# Returns: 列名到备注的映射；无备注的列不进入映射。
def _extract_column_comments(tables: Any) -> dict[str, str]:
    comments: dict[str, str] = {}
    for table in list(tables or []):
        if not isinstance(table, dict):
            continue
        for column in list(table.get("columns", []) or []):
            if not isinstance(column, dict):
                continue
            name = str(column.get("name", "") or "")
            comment = str(column.get("comment", "") or "").strip()
            if name and comment:
                comments[name] = comment
    if comments:
        logger.info("报表列备注已收集", comment_count=len(comments))
    return comments


# 方法作用：从分析结果里抽取结果字段的中文说明。
# Args: analysis - 分析节点输出的结构化结果。
# Returns: 字段名到说明的映射；说明为空的条目被丢弃。
def _extract_field_descriptions(analysis: Any) -> dict[str, str]:
    raw = analysis.get("field_descriptions") if isinstance(analysis, dict) else None
    if not isinstance(raw, dict):
        return {}
    descriptions = {
        str(key): str(value).strip()
        for key, value in raw.items()
        if str(key).strip() and str(value).strip()
    }
    if descriptions:
        logger.info("报表结果字段说明已收集", field_count=len(descriptions))
    return descriptions


class ReportRunner:
    """执行报表工作台 Job，并把内部错误转换成用户可读状态。"""

    # 方法作用：初始化持久化 Store、配额服务和可注入单元执行器。
    # Args: store - 报表 Store；quota_manager - 配额服务；execute_unit - 现有 SQL 工作流适配器。
    # Returns: 无返回值。
    def __init__(self, store: ReportStore | None = None, quota_manager: QuotaManager | None = None, execute_unit: Any | None = None) -> None:
        self.store = store or get_report_store()
        self.quota = quota_manager or QuotaManager(store=self.store)
        self.execute_unit = execute_unit
        self._tasks: set[asyncio.Task] = set()

    # 方法作用：异步排队执行报表任务，立即返回后台 Task 引用。
    # Args: job_id - 任务 ID；identity - 当前身份；attempt - 尝试次数。
    # Returns: 已创建的 asyncio.Task。
    def enqueue(self, job_id: str, identity: Any, attempt: int = 1, authorized_datasources: dict[str, dict[str, Any]] | None = None, *, force: bool = False) -> asyncio.Task:
        if self.execute_unit is None and not self.store._memory:
            from src.reporting.scheduler import ReportScheduler

            execution = ReportScheduler(self.store, self).dispatch(job_id, identity, force=force)
        else:
            execution = self.run_job(job_id, identity, attempt=attempt, authorized_datasources=authorized_datasources)
        task = asyncio.create_task(execution, name=f"report-run-{job_id[:12]}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info("报表任务已排队", job_id=job_id, tenant_id=getattr(identity, "tenant_id", 0), user_id=getattr(identity, "user_id", 0))
        return task

    # 方法作用：执行一次报表任务并发布成功或部分成功页面版本。
    # Args: job_id - 任务 ID；identity - 当前身份；attempt - 运行尝试次数。
    # Returns: 运行状态、run_id、report_id 和 version 摘要。
    async def run_job(self, job_id: str, identity: Any, *, attempt: int = 1, authorized_datasources: dict[str, dict[str, Any]] | None = None, version_store: ReportStore | None = None, before_publish: Any = None) -> dict[str, Any]:
        """执行报表并在成功、失败或取消时完成状态和资源清理。"""
        logger.debug("报表运行入口", job_id=job_id, tenant_id=getattr(identity, "tenant_id", 0), user_id=getattr(identity, "user_id", 0), attempt=attempt)
        job = await self.store.get_job(job_id, identity)
        if job is None:
            raise LookupError("报表任务不存在")
        run = await self.store.create_run(job_id, identity, attempt=attempt)
        plan = job.plan
        reservation = None
        # 真实计数在运行作用域内累计；LLM 回调挂载时按引用捕获该对象，
        # 因此不需要依赖 contextvar 穿过 LangGraph 内部的 task。
        from src.reporting.run_stats import activate, deactivate

        stats, stats_token = activate()
        try:
            # 先按表结构判断每个单元命中的数据源：它决定真实的查询扇出，
            # 必须早于配额预占，否则预占拿到的仍是"只数单元数"的低报估算。
            unit_datasources = await self._route_units(
                plan,
                [str(item) for item in (plan.requested_datasources or [])],
                int(getattr(identity, "tenant_id", 0) or 0),
            )
            quota = await self.store.reserve_run_quota(
                run.id, identity, self._reserve_plan(plan, unit_datasources), self.quota
            )
            reservation = quota
            await self.store.update_run(run.id, identity, status="running", started_at=datetime.now(timezone.utc), last_node="report_load_policy")
            await self.store.update_job_status(job_id, identity, "running")

            # 方法作用：记录当前运行的节点进度，供恢复与审计使用。
            # Args: node - 节点名称；status - 当前状态；error - 用户可见错误。
            # Returns: 无返回值。
            async def step_logger(node: str, status: str, error: str = "") -> None:
                await self.store.append_step(run.id, node, status, error_code="NODE_FAILED" if status == "failed" else "", user_error=error, identity=identity)
                await self.store.update_run(run.id, identity, last_node=node)

            from src.graph.subgraphs.report_workbench import build_report_workbench_subgraph

            effective_authorized = authorized_datasources
            if effective_authorized is None and hasattr(identity, "authorized_datasources"):
                effective_authorized = identity.authorized_datasources
            logger.info(
                "报表子图授权参数写入",
                job_id=job_id,
                authorized_datasource_names=sorted((effective_authorized or {}).keys()),
                authorized_datasources_is_none=effective_authorized is None,
                injected_executor=self.execute_unit is not None,
            )
            graph = build_report_workbench_subgraph()
            graph_state: dict[str, Any] = {
                "plan": plan,
                "identity": {"tenant_id": int(getattr(identity, "tenant_id", 0) or 0), "user_id": int(getattr(identity, "user_id", 0) or 0), "role": str(getattr(identity, "role", "") or "")},
                "execute_unit": self.execute_unit or self._default_execute_unit,
                "step_logger": step_logger,
                "report_id": job_id,
                "job_id": job_id,
                "run_id": run.id,
                # 模板只影响呈现，随 Job 落库并原样透传给页面文档。
                "template_id": str(getattr(job, "template_id", "") or "insight"),
            }
            if effective_authorized is not None:
                graph_state["authorized_datasources"] = effective_authorized
            # 路由结果在预占前已算出，这里只把它传给子图，避免向无关数据源广播查询。
            if unit_datasources:
                graph_state["unit_datasources"] = unit_datasources
            result = await graph.ainvoke(graph_state)
            run_status = str(result.get("run_status", "failed"))
            unit_results = list(result.get("unit_results", []) or [])
            row_count = sum(len(item.rows) for item in unit_results if isinstance(item, UnitResult))
            if run_status == "failed":
                message = "所有分析单元执行失败，未发布报表页面"
                await self.store.update_run(run.id, identity, status="failed", partial=False, row_count=row_count, error_code="ALL_UNITS_FAILED", user_error_message=message, finished_at=datetime.now(timezone.utc))
                if version_store is None:
                    await self.store.update_job_status(job_id, identity, "failed")
                return {"status": "failed", "run_id": run.id, "error_code": "ALL_UNITS_FAILED", "error_message": message, "quota": quota}
            artifact = result["artifact"]
            expires_at = None if plan.permanent else datetime.now(timezone.utc) + timedelta(days=plan.page_ttl_days)
            artifact.document.expires_at = expires_at
            if before_publish is not None:
                await before_publish()
            writer = version_store or self.store
            version = await writer.persist_version(job_id, run.id, identity, artifact.document.model_dump(mode="json"), artifact.data_manifest, expires_at, require_active_job=version_store is not None)
            final_status = "partial" if run_status == "partial" else "succeeded"
            # query_count 保持配额计费口径的估算值；真实计数与 token 用量在 finally 中统一落库。
            await self.store.update_run(run.id, identity, status=final_status, partial=final_status == "partial", row_count=row_count, query_count=plan.estimated.query_count, finished_at=datetime.now(timezone.utc))
            if version_store is None:
                await self.store.update_job_status(job_id, identity, final_status)
            logger.info("报表运行完成", job_id=job_id, run_id=run.id, status=final_status, report_id=version.report_id, version=version.version)
            return {"status": final_status, "run_id": run.id, "report_id": version.report_id, "version": version.version, "quota": quota}
        except asyncio.CancelledError:
            logger.info("报表运行取消", job_id=job_id, run_id=run.id)
            await self.store.update_run(run.id, identity, status="cancelled", error_code="CANCELLED", user_error_message="报表运行已取消", finished_at=datetime.now(timezone.utc))
            if version_store is None:
                await self.store.update_job_status(job_id, identity, "cancelled")
            raise
        except QuotaExceededError as exc:
            # 带上维度、上限与本次需求：只写"配额不足"无法定位要调哪一项。
            message = describe_quota_error(exc)
            await self.store.update_run(run.id, identity, status="failed", error_code="QUOTA_EXCEEDED", internal_error_message=str(exc), user_error_message=message, finished_at=datetime.now(timezone.utc))
            if version_store is None:
                await self.store.update_job_status(job_id, identity, "failed")
            return {"status": "failed", "run_id": run.id, "error_code": "QUOTA_EXCEEDED", "error_message": message}
        except Exception as exc:
            logger.error("报表运行失败", job_id=job_id, run_id=run.id, error=str(exc), exc_info=True)
            message = "报表生成失败，请查看运行日志后重试"
            await self.store.append_step(run.id, "report_runner", "failed", error_code="INTERNAL_ERROR", internal_error=str(exc), user_error=message, identity=identity)
            await self.store.update_run(run.id, identity, status="failed", error_code="INTERNAL_ERROR", internal_error_message=str(exc), user_error_message=message, finished_at=datetime.now(timezone.utc))
            if version_store is None:
                await self.store.update_job_status(job_id, identity, "failed")
            return {"status": "failed", "run_id": run.id, "error_code": "INTERNAL_ERROR", "error_message": message}
        finally:
            # 真实计数覆盖成功、部分成功、失败、取消与配额不足全部出口：
            # 已经发生的调用不应因为运行失败而丢失。
            try:
                await self.store.update_run(
                    run.id,
                    identity,
                    llm_call_count=stats.llm_call_count,
                    llm_failure_count=stats.llm_failure_count,
                    token_usage=stats.llm_token_usage,
                    sql_query_count=stats.sql_query_count,
                    sql_success_count=stats.sql_success_count,
                    sql_failure_count=stats.sql_failure_count,
                )
            except Exception:
                # 统计是观测数据，落库失败不得影响报表发布。
                logger.error("报表运行计数落库失败", job_id=job_id, run_id=run.id, exc_info=True)
            deactivate(stats_token)
            if reservation is not None:
                try:
                    await self.store.release_run_quota(run.id, identity, self.quota)
                except Exception:
                    logger.error("报表并发配额释放失败", job_id=job_id, run_id=run.id, exc_info=True)

    # 方法作用：关闭请求触发的后台任务，确保退出前释放运行资源。
    # Args: self - 当前运行器。
    # Returns: 无返回值。
    async def close(self) -> None:
        """AppContext 关闭时取消并等待全部本进程报表任务。"""
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("报表运行器已关闭", task_count=len(tasks))

    # 方法作用：通过现有 Chat LangGraph 执行单个分析单元，并为其落执行快照。
    # Args: unit - 分析单元；state - 报表子图状态和身份摘要。
    # Returns: UnitResult 或失败结果。
    async def _default_execute_unit(self, unit: Any, state: dict[str, Any]) -> UnitResult:
        started = time.monotonic()
        # 提示词现场只组装一次：工作流入参与快照共用同一文本，避免两次日志与两处漂移。
        trace: dict[str, Any] = {"prompt_text": _compose_unit_query(unit)}
        try:
            result = await self._execute_unit_workflow(unit, state, trace)
        except Exception as exc:
            # 异常路径也留快照：单元崩在半路时现场比堆栈更直接。
            await self._persist_unit_snapshot(unit, state, trace, status="failed", error_code="EXECUTION_ERROR", error_message=str(exc), elapsed_ms=round((time.monotonic() - started) * 1000))
            raise
        status = str(result.status or "success")
        await self._persist_unit_snapshot(unit, state, trace, status=status, error_code=result.error_code, error_message=result.user_error_message if status != "success" else "", elapsed_ms=round((time.monotonic() - started) * 1000))
        return result

    # 方法作用：执行单元工作流并把调试现场写入 trace，供外层组装快照。
    # Args: unit - 分析单元；state - 报表子图状态；trace - 过程采集字典（prompt_text/sql/dialect/行数等）。
    # Returns: UnitResult 或失败结果。
    async def _execute_unit_workflow(self, unit: Any, state: dict[str, Any], trace: dict[str, Any]) -> UnitResult:
        plan = state.get("plan")
        requested = [str(item) for item in (getattr(plan, "requested_datasources", []) or [])]
        routed = (state.get("unit_datasources") or {}).get(str(unit.unit_id))
        if unit.datasource:
            # 单元点名了数据源：单源执行，不扩大范围。
            selected_sources = [str(unit.datasource)]
        elif routed is None:
            # 未取得路由结果（模型不可用）：退回全部已选源，保持多源检索的既有行为。
            selected_sources = requested
        else:
            # 路由命中：只打命中的源；空列表表示没有源能支撑该问题。
            selected_sources = [str(item) for item in routed]
        if not selected_sources:
            # “没有源能支撑这个问题”是可解释的结论，不是失败，不计入失败单元。
            logger.info("报表分析单元无命中数据源", unit_id=unit.unit_id)
            trace["datasource"] = ""
            trace["params"] = {"selected_datasources": []}
            return UnitResult(unit_id=unit.unit_id, datasource="", status="partial", warnings=["已选数据源中没有与该问题匹配的表，未执行查询"], user_error_message="该分析单元在已选数据源中未找到相关数据")
        datasource = str(unit.datasource or selected_sources[0])
        trace["datasource"] = datasource
        trace["params"] = {"selected_datasources": selected_sources}
        from src.graph.workflow import app

        if app is None:
            return UnitResult(unit_id=unit.unit_id, datasource=datasource, status="partial", warnings=["缺少已授权数据源或 SQL 工作流尚未启动"], user_error_message="请先选择一个有权限的数据源")
        identity = dict(state.get("identity", {}) or {})
        authorized = dict(state.get("authorized_datasources", {}) or {})
        access = authorized.get(datasource, {})
        run_id = str(state.get("run_id", ""))
        if not run_id:
            raise ValueError("报表执行缺少独立运行标识")
        session_id = f"report-{run_id}-{unit.unit_id}"
        thread_id = f"tenant:{int(identity.get('tenant_id', 0))}:user:{int(identity.get('user_id', 0))}:session:{session_id}"
        result = await app.ainvoke({
            "user_query": str(trace.get("prompt_text", "") or ""),
            "datasource": datasource,
            "selected_datasources": selected_sources,
            "datasource_access": authorized,
            "allowed_columns": list(access.get("allowed_columns", []) or []),
            "row_filter_sql": str(access.get("row_filter_sql", "") or ""),
            "tenant_id": int(identity.get("tenant_id", 0) or 0),
            "user_id": int(identity.get("user_id", 0) or 0),
            "user_role": str(identity.get("role", "") or ""),
            "session_id": session_id,
            # 显式抑制旧的自主补查回环（workflow.py:237-281）。该回环没有配额预占
            # 与档位控制，与报表自身的受控发散回路叠加会让成本失控。
            # 不能用 capability="report" 实现：_infer_capability 只按问题文本推断、
            # 会覆盖预置值；而且它真生效时 after_analyze_result 会把单元路由到
            # report_subgraph（生成报告而不是取数），后果比原问题更严重。
            "suppress_supplemental_query": True,
            "request_rate_limit_checked": True,
        }, {"configurable": {"thread_id": thread_id}})
        final = dict(result.get("final_response", {}) or {})
        rows = final.get("data", result.get("query_result_sample", []))
        if not isinstance(rows, list):
            rows = []
        # SQL、方言与行数在成功和失败路径都先进快照：失败单元的现场同样要靠它反推。
        sql = str(result.get("generated_sql", "") or "").strip()
        dialect = str(result.get("dialect", "") or access.get("dialect", ""))
        full_count = int(result.get("query_result_full_count") or 0)
        trace["sql"] = sql
        trace["dialect"] = dialect
        trace["row_count"] = len(rows)
        trace["full_count"] = full_count
        if final.get("success", result.get("sql_valid", True)) is False:
            return UnitResult(unit_id=unit.unit_id, datasource=datasource, status="failed", user_error_message=str(final.get("error_message", "查询失败")), error_code=str(final.get("error_code", "QUERY_FAILED")))
        live_query_id = ""
        if sql and rows:
            from src.security.tenant_policy import RequestIdentity

            owner = RequestIdentity(tenant_id=int(identity.get("tenant_id", 0)), user_id=int(identity.get("user_id", 0)), role=str(identity.get("role", "")))
            live_query_id = await self.store.save_live_query(run_id, owner, datasource, dialect, sql)
        # 只看报表自己展示的行集是否被截断：上游的 query_result_truncated 还包含
        # "给模型的样本被裁到 50/30 行"这类与用户可见行集无关的标记，透传会让
        # "共 60 行、全部展示"的单元被判成"样本被截断"（实机页面上逐条刷屏）。
        from src.reporting.models import display_truncated

        truncated = display_truncated(len(rows), full_count)

        trace["truncated"] = truncated
        # 截断原因只由本层生成，且上限取真实切割点（配置的样本上限）：上游的
        # query_result_truncated 口径与本层无关，页面渲染另有自己的行数口径。
        trace["truncated_reason"] = _describe_sample_truncation(len(rows), full_count) if truncated else ""
        analysis = final.get("analysis") or {}
        # 直答类单元（元数据说明、知识直答等）产出的是文字结论，没有数据行是正常形态；
        # 标成 text 后，成功单元判据才不会把它们误算成部分完成。
        from src.graph.contracts import DIRECT_ANSWER_SOURCES

        answer_kind = "text" if str(final.get("source", "") or "") in DIRECT_ANSWER_SOURCES else "data"
        # 样本超限时的处置建议：只登记与披露，不做任何自动重跑。决策必须在单元返回前
        # 完成，结论才会随本次 trace 一起进快照；失败的单元（上面已返回）不决策。
        sample_guidance = await _decide_sample_guidance(
            unit,
            trace,
            rows,
            full_count,
            bool(result.get("query_result_sample_capped", False)),
        )
        limitations = list(dict.fromkeys(
            item for item in (analysis.get("limitations", []) or [])
            if isinstance(item, str) and item.strip()
        ))
        # 处置结论排在最前：warnings 会被页面质量块按前 20 条裁剪，排在分析模型给出的
        # 限制之后时，限制一多就把披露挤掉，等于没披露。
        warnings = list(dict.fromkeys([*sample_guidance, *limitations]))[:20]
        return UnitResult(
            unit_id=unit.unit_id,
            datasource=datasource,
            live_query_id=live_query_id,
            answer_kind=answer_kind,
            rows=[dict(row) for row in rows if isinstance(row, dict)],
            truncated=truncated,
            metric_citations=_extract_metric_citations(result.get("activated_knowledge")),
            column_comments=_extract_column_comments(result.get("relevant_tables")),
            field_descriptions=_extract_field_descriptions(analysis),
            summary=str(analysis.get("summary", "") or ""),
            warnings=warnings,
        )

    # 方法作用：把单元执行现场组装成快照并落库，失败只记告警不影响报表生成。
    # Args: unit - 分析单元；state - 报表子图状态；trace - 执行过程采集字段；status - 单元状态；error_code/error_message - 失败现场；elapsed_ms - 执行耗时毫秒。
    # Returns: 无返回值。
    async def _persist_unit_snapshot(self, unit: Any, state: dict[str, Any], trace: dict[str, Any], *, status: str, error_code: str = "", error_message: str = "", elapsed_ms: int = 0) -> None:
        unit_id = str(getattr(unit, "unit_id", "") or "")
        run_id = str(state.get("run_id", "") or "")
        if not run_id:
            # 没有运行归属的快照无法与页面版本关联，写进去只会成为孤儿记录。
            logger.debug("报表单元快照跳过：缺少运行标识", unit_id=unit_id)
            return
        try:
            from src.reporting.snapshots import build_snapshot, resolve_model_id
            from src.security.tenant_policy import RequestIdentity

            identity = dict(state.get("identity", {}) or {})
            owner = RequestIdentity(tenant_id=int(identity.get("tenant_id", 0) or 0), user_id=int(identity.get("user_id", 0) or 0), role=str(identity.get("role", "") or ""))
            snapshot = build_snapshot(
                run_id=run_id,
                report_id=str(state.get("report_id", "") or ""),
                tenant_id=owner.tenant_id,
                user_id=owner.user_id,
                unit_id=unit_id,
                status=status if status in {"success", "partial", "failed"} else "failed",
                datasource=str(trace.get("datasource", "") or ""),
                dialect=str(trace.get("dialect", "") or ""),
                sql=str(trace.get("sql", "") or ""),
                prompt_text=str(trace.get("prompt_text", "") or ""),
                model_id=resolve_model_id(),
                params=dict(trace.get("params", {}) or {}),
                row_count=int(trace.get("row_count", 0) or 0),
                full_count=int(trace.get("full_count", 0) or 0),
                truncated=bool(trace.get("truncated", False)),
                truncated_reason=str(trace.get("truncated_reason", "") or ""),
                elapsed_ms=int(elapsed_ms),
                error_code=error_code,
                error_message=error_message,
            )
            await self.store.save_unit_snapshot(snapshot, owner)
            logger.info("报表单元快照已保存", run_id=run_id, unit_id=unit_id, status=snapshot.status, snapshot_id=snapshot.id)
        except Exception as exc:
            # 快照是观测数据：落库失败不得影响报表生成（与运行计数落库同策略）。
            logger.warning("报表单元快照保存失败", run_id=run_id, unit_id=unit_id, error_type=type(exc).__name__, error=str(exc), exc_info=True)

    # 方法作用：读取各数据源的表名清单，供路由模型与计划期发散判断命中关系。
    # Args: requested - 已授权数据源名称；tenant_id - 当前租户。
    # Returns: 每个数据源一段紧凑表清单的 Prompt 文本。
    @staticmethod
    async def _datasource_table_catalog(requested: list[str], tenant_id: int) -> str:
        from src.knowledge.schema_manager import get_schema_manager

        manager = get_schema_manager()
        blocks: list[str] = []
        for name in requested:
            try:
                # 不传 user_query：路由需要完整表清单，而不是按问题预筛后的子集。
                schema = await manager.get_or_fetch_schema(name, tenant_id=tenant_id)
                tables = [str(table.name) for table in (schema.tables or [])][:_CATALOG_TABLES_PER_SOURCE]
            except Exception as exc:
                logger.warning("报表路由读取表清单失败", datasource=name, error_type=type(exc).__name__)
                tables = []
            blocks.append(f"### {name}\n" + ("、".join(tables) if tables else "（未获取到表结构）"))
        return "\n".join(blocks)

    # 方法作用：判断每个分析单元应命中哪些数据源，避免把问题广播给无关数据源。
    # Args: plan - 报表计划；requested - 已授权数据源；tenant_id - 当前租户。
    # Returns: unit_id 到命中数据源列表的映射；无法判定时返回空字典表示沿用全源检索。
    async def _route_units(self, plan: Any, requested: list[str], tenant_id: int) -> dict[str, list[str]]:
        units = [unit for unit in plan.analysis_units if not unit.datasource]
        # 单元已点名数据源时不需要路由。候选源只有一个时仍必须路由：
        # 路由不只是“选哪个源”，还负责判定“这个单元在候选源里有没有支撑表”。
        # 单源短路会让缺表单元照样发查询，最后只在 SQL 生成阶段变成一句“查询失败”。
        if not units or not requested:
            return {}
        try:
            from src.llm.client import is_task_llm_available

            if not is_task_llm_available(_ROUTE_LLM_TASK):
                raise RuntimeError("报表路由模型不可用")
            from src.llm.invocation import invoke_structured
            from src.llm.output_contracts import ReportUnitRoutesOutput
            from src.llm.prompt_budget import PromptSection

            catalog = await self._datasource_table_catalog(requested, tenant_id)
            output = await invoke_structured(
                "report.route",
                [
                    PromptSection("catalog", f"## 数据源表清单\n{catalog}", priority=100, min_chars=100, max_chars=30000),
                    PromptSection(
                        "units",
                        "## 分析单元\n" + "\n".join(f"- {unit.unit_id}: {unit.question}" for unit in units),
                        priority=90,
                        min_chars=50,
                        max_chars=2000,
                    ),
                ],
                output_model=ReportUnitRoutesOutput,
                task=_ROUTE_LLM_TASK,
                temperature=0.0,
                metadata={"node": "report_runner"},
            )
            allowed = set(requested)
            routes: dict[str, list[str]] = {}
            for route in output.routes:
                # 只接受已授权数据源，模型越权返回一律忽略。
                routes[str(route.unit_id)] = [name for name in route.datasources if name in allowed]
            matched = sum(1 for names in routes.values() if names)
            logger.info("报表分析单元路由完成", unit_count=len(units), routed_count=len(routes), matched_count=matched)
            return routes
        except Exception as exc:
            # 路由失败允许退回全源检索，但必须保留堆栈以便与“确实没有命中”区分。
            logger.warning("报表分析单元路由失败，退回全源检索", error_type=type(exc).__name__, error=str(exc), exc_info=True)
            return {}

    # 方法作用：按路由命中数重算预占用计划，避免按“单元数”低报真实查询扇出。
    # Args: plan - 原始报表计划；routed - unit_id 到命中数据源的映射，None 表示未路由。
    # Returns: estimated.query_count / llm_tokens 已按真实扇出修正的计划副本。
    @staticmethod
    def _reserve_plan(plan: Any, routed: dict[str, list[str]] | None) -> Any:
        if not routed:
            return plan
        fallback = max(1, len(getattr(plan, "requested_datasources", []) or []))
        actual = 0
        for unit in plan.analysis_units:
            if unit.datasource:
                actual += 1
                continue
            matched = routed.get(str(unit.unit_id))
            # 未出现在路由结果里的单元会退回全源检索，按 fallback 计入才不会低报。
            actual += len(matched) if matched is not None else fallback
        # 运行期发散回路是数据驱动的，预占时无法知道会不会发生、会加几个单元，
        # 因此按额度上限预留。禁止"先跑再按实际补扣"，那会绕过配额。
        from src.reporting.models import divergence_unit_budget, max_plan_queries

        level = str(getattr(plan, "divergence", "standard") or "standard")
        # 档位是计划期与运行期**共享**的总授权：计划期已经追加的发散单元必须扣减，
        # 否则会把已经花掉的额度再预留一遍（与子图 _remaining_budget 同口径）。
        plan_diverged = sum(1 for unit in plan.analysis_units if getattr(unit, "origin", "user") == "diverged")
        runtime_budget = max(0, divergence_unit_budget(level) - plan_diverged)
        actual += runtime_budget * fallback
        actual = max(1, actual)
        # 与计划期同源读配置：两处各自写死的钳位一旦脱钩，预占就会越过字段契约。
        reserved_query_count = min(max_plan_queries(), actual)
        estimated = plan.estimated.model_copy(update={
            "query_count": reserved_query_count,
            "llm_tokens": min(2_000_000, max(1000, actual * 1200)),
        })
        # 两个数都要打：只打 actual 会让日志里的数字与实际预占不一致（本次事故的排查坑）。
        logger.info(
            "报表配额按路由扇出修正",
            actual_query_count=actual,
            reserved_query_count=reserved_query_count,
            unit_count=len(plan.analysis_units),
        )
        return plan.model_copy(update={"estimated": estimated})


# 方法作用：从 AppContext 获取共享 ReportRunner。
# Args: 无。
# Returns: ReportRunner 单例。
def get_report_runner() -> ReportRunner:
    from src.app_context import get_app_context

    return get_app_context().get_or_create("report_runner", ReportRunner, closer=ReportRunner.close)
