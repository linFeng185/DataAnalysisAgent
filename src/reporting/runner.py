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
        try:
            quota = await self.store.reserve_run_quota(run.id, identity, plan, self.quota)
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
            await self.store.update_run(run.id, identity, status=final_status, partial=final_status == "partial", row_count=row_count, query_count=plan.estimated.query_count, token_usage=plan.estimated.llm_tokens, finished_at=datetime.now(timezone.utc))
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
        datasource = str(unit.datasource or (state.get("plan").requested_datasources or [""])[0])
        from src.graph.workflow import app

        if app is None or not datasource:
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
            "selected_datasources": [datasource],
            "datasource_access": authorized,
            "allowed_columns": list(access.get("allowed_columns", []) or []),
            "row_filter_sql": str(access.get("row_filter_sql", "") or ""),
            "tenant_id": int(identity.get("tenant_id", 0) or 0),
            "user_id": int(identity.get("user_id", 0) or 0),
            "user_role": str(identity.get("role", "") or ""),
            "session_id": session_id,
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
        return UnitResult(unit_id=unit.unit_id, datasource=datasource, live_query_id=live_query_id, rows=[dict(row) for row in rows if isinstance(row, dict)], summary=str((final.get("analysis") or {}).get("summary", "") or ""))


# 方法作用：从 AppContext 获取共享 ReportRunner。
# Args: 无。
# Returns: ReportRunner 单例。
def get_report_runner() -> ReportRunner:
    from src.app_context import get_app_context

    return get_app_context().get_or_create("report_runner", ReportRunner, closer=ReportRunner.close)
