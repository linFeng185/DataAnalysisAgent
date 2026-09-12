"""报表任务运行器：节点日志、失败隔离、版本发布和并发配额。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from src.logging_config import get_logger
from src.reporting.models import UnitResult
from src.reporting.quota import QuotaExceededError, QuotaManager
from src.reporting.store import ReportStore, get_report_store

logger = get_logger(__name__)

# 报表分析单元路由任务标识，供 LLM 任务路由选择本地或租户配置的模型。
_ROUTE_LLM_TASK = "report_route"
# 每个数据源最多放进路由 Prompt 的表名数量。
# 取 200 以覆盖上百张表的库（实测财务库 125 张）；一旦超预算被截断，
# 排在后面的数据源会整段丢失，路由就必然漏判。
_CATALOG_TABLES_PER_SOURCE = 200


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
            message = "本周期报表配额不足，请联系租户管理员"
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

    # 方法作用：通过现有 Chat LangGraph 执行单个分析单元，保持 SQL/ACL 链路复用。
    # Args: unit - 分析单元；state - 报表子图状态和身份摘要。
    # Returns: UnitResult 或失败结果。
    async def _default_execute_unit(self, unit: Any, state: dict[str, Any]) -> UnitResult:
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
            return UnitResult(unit_id=unit.unit_id, datasource="", status="partial", warnings=["已选数据源中没有与该问题匹配的表，未执行查询"], user_error_message="该分析单元在已选数据源中未找到相关数据")
        datasource = str(unit.datasource or selected_sources[0])
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
            "user_query": unit.question,
            "datasource": datasource,
            "selected_datasources": selected_sources,
            "datasource_access": authorized,
            "allowed_columns": list(access.get("allowed_columns", []) or []),
            "row_filter_sql": str(access.get("row_filter_sql", "") or ""),
            "tenant_id": int(identity.get("tenant_id", 0) or 0),
            "user_id": int(identity.get("user_id", 0) or 0),
            "user_role": str(identity.get("role", "") or ""),
            "session_id": session_id,
            # 显式声明报表能力：否则 classify_intent 会把单元问句判成 sql_analysis，
            # 从而允许触发旧的自主补查回环（workflow.py:237-281）。该回环没有配额
            # 预占与档位控制，与报表自身的受控发散回路叠加会让成本失控。
            "capability": "report",
            "request_rate_limit_checked": True,
        }, {"configurable": {"thread_id": thread_id}})
        final = dict(result.get("final_response", {}) or {})
        rows = final.get("data", result.get("query_result_sample", []))
        if not isinstance(rows, list):
            rows = []
        if final.get("success", result.get("sql_valid", True)) is False:
            return UnitResult(unit_id=unit.unit_id, datasource=datasource, status="failed", user_error_message=str(final.get("error_message", "查询失败")), error_code=str(final.get("error_code", "QUERY_FAILED")))
        live_query_id = ""
        sql = str(result.get("generated_sql", "") or "").strip()
        if sql and rows:
            from src.security.tenant_policy import RequestIdentity

            owner = RequestIdentity(tenant_id=int(identity.get("tenant_id", 0)), user_id=int(identity.get("user_id", 0)), role=str(identity.get("role", "")))
            live_query_id = await self.store.save_live_query(run_id, owner, datasource, str(result.get("dialect", "") or access.get("dialect", "")), sql)
        # 透传旧工作流已有的截断信号，供确定性深挖披露样本局限。
        statistics = result.get("query_result_statistics")
        truncated = bool(
            (statistics.get("truncated") if isinstance(statistics, dict) else False)
            or result.get("query_result_truncated")
        )
        return UnitResult(unit_id=unit.unit_id, datasource=datasource, live_query_id=live_query_id, rows=[dict(row) for row in rows if isinstance(row, dict)], truncated=truncated, summary=str((final.get("analysis") or {}).get("summary", "") or ""))

    # 方法作用：读取各数据源的表名清单，供路由模型判断命中关系。
    # Args: requested - 已授权数据源名称；tenant_id - 当前租户。
    # Returns: 每个数据源一段紧凑表清单的 Prompt 文本。
    async def _datasource_table_catalog(self, requested: list[str], tenant_id: int) -> str:
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
        # 只有一个候选源时不存在选择问题；单元已点名数据源时不需要路由。
        if not units or len(requested) <= 1:
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
        actual = max(1, actual)
        estimated = plan.estimated.model_copy(update={
            "query_count": min(1000, actual),
            "llm_tokens": min(2_000_000, max(1000, actual * 1200)),
        })
        logger.info("报表配额按路由扇出修正", query_count=actual, unit_count=len(plan.analysis_units))
        return plan.model_copy(update={"estimated": estimated})


# 方法作用：从 AppContext 获取共享 ReportRunner。
# Args: 无。
# Returns: ReportRunner 单例。
def get_report_runner() -> ReportRunner:
    from src.app_context import get_app_context

    return get_app_context().get_or_create("report_runner", ReportRunner, closer=ReportRunner.close)
