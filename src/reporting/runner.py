"""报表任务运行器：节点日志、失败隔离、版本发布和并发配额。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from src.api.auth import scope_thread_id
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
    def enqueue(self, job_id: str, identity: Any, attempt: int = 1, authorized_datasources: dict[str, dict[str, Any]] | None = None) -> asyncio.Task:
        task = asyncio.create_task(self.run_job(job_id, identity, attempt=attempt, authorized_datasources=authorized_datasources), name=f"report-run-{job_id[:12]}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info("报表任务已排队", job_id=job_id, tenant_id=getattr(identity, "tenant_id", 0), user_id=getattr(identity, "user_id", 0))
        return task

    # 方法作用：执行一次报表任务并发布成功或部分成功页面版本。
    # Args: job_id - 任务 ID；identity - 当前身份；attempt - 运行尝试次数。
    # Returns: 运行状态、run_id、report_id 和 version 摘要。
    async def run_job(self, job_id: str, identity: Any, *, attempt: int = 1, authorized_datasources: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        logger.debug("报表运行入口", job_id=job_id, tenant_id=getattr(identity, "tenant_id", 0), user_id=getattr(identity, "user_id", 0), attempt=attempt)
        job = await self.store.get_job(job_id, identity)
        if job is None:
            raise LookupError("报表任务不存在")
        run = await self.store.create_run(job_id, identity, attempt=attempt)
        plan = job.plan
        try:
            quota = await self.quota.reserve(
                identity,
                runs=1,
                concurrent=1,
                units=len(plan.analysis_units),
                queries=plan.estimated.query_count,
                tokens=plan.estimated.llm_tokens,
                timezone_name=plan.timezone,
            )
            await self.store.update_run(run.id, identity, status="running", started_at=datetime.now(timezone.utc), last_node="report_load_policy")
            await self.store.update_job_status(job_id, identity, "running")

            async def step_logger(node: str, status: str, error: str = "") -> None:
                await self.store.append_step(run.id, node, status, error_code="NODE_FAILED" if status == "failed" else "", user_error=error)
                await self.store.update_run(run.id, identity, last_node=node)

            from src.graph.subgraphs.report_workbench import build_report_workbench_subgraph

            graph = build_report_workbench_subgraph()
            result = await graph.ainvoke({
                "plan": plan,
                "identity": {"tenant_id": int(getattr(identity, "tenant_id", 0) or 0), "user_id": int(getattr(identity, "user_id", 0) or 0), "role": str(getattr(identity, "role", "") or "")},
                "authorized_datasources": authorized_datasources or getattr(identity, "authorized_datasources", {}) or {},
                "execute_unit": self.execute_unit or self._default_execute_unit,
                "step_logger": step_logger,
                "report_id": None,
            })
            run_status = str(result.get("run_status", "failed"))
            unit_results = list(result.get("unit_results", []) or [])
            row_count = sum(len(item.rows) for item in unit_results if isinstance(item, UnitResult))
            if run_status == "failed":
                message = "所有分析单元执行失败，未发布报表页面"
                await self.store.update_run(run.id, identity, status="failed", partial=False, row_count=row_count, error_code="ALL_UNITS_FAILED", user_error_message=message, finished_at=datetime.now(timezone.utc))
                await self.store.update_job_status(job_id, identity, "failed")
                await self.quota.release_concurrent(identity, timezone_name=plan.timezone)
                return {"status": "failed", "run_id": run.id, "error_code": "ALL_UNITS_FAILED", "error_message": message, "quota": quota}
            artifact = result["artifact"]
            expires_at = None if plan.permanent else datetime.now(timezone.utc) + timedelta(days=plan.page_ttl_days)
            artifact.document.expires_at = expires_at
            version = await self.store.persist_version(job_id, run.id, identity, artifact.document.model_dump(mode="json"), artifact.data_manifest, expires_at)
            final_status = "partial" if run_status == "partial" else "succeeded"
            await self.store.update_run(run.id, identity, status=final_status, partial=final_status == "partial", row_count=row_count, query_count=plan.estimated.query_count, token_usage=plan.estimated.llm_tokens, finished_at=datetime.now(timezone.utc))
            await self.store.update_job_status(job_id, identity, final_status)
            await self.quota.release_concurrent(identity, timezone_name=plan.timezone)
            logger.info("报表运行完成", job_id=job_id, run_id=run.id, status=final_status, report_id=version.report_id, version=version.version)
            return {"status": final_status, "run_id": run.id, "report_id": version.report_id, "version": version.version, "quota": quota}
        except QuotaExceededError as exc:
            message = "本周期报表配额不足，请联系租户管理员"
            await self.store.update_run(run.id, identity, status="failed", error_code="QUOTA_EXCEEDED", internal_error_message=str(exc), user_error_message=message, finished_at=datetime.now(timezone.utc))
            await self.store.update_job_status(job_id, identity, "failed")
            return {"status": "failed", "run_id": run.id, "error_code": "QUOTA_EXCEEDED", "error_message": message}
        except Exception as exc:
            logger.error("报表运行失败", job_id=job_id, run_id=run.id, error=str(exc), exc_info=True)
            message = "报表生成失败，请查看运行日志后重试"
            await self.store.append_step(run.id, "report_runner", "failed", error_code="INTERNAL_ERROR", internal_error=str(exc), user_error=message)
            await self.store.update_run(run.id, identity, status="failed", error_code="INTERNAL_ERROR", internal_error_message=str(exc), user_error_message=message, finished_at=datetime.now(timezone.utc))
            await self.store.update_job_status(job_id, identity, "failed")
            try:
                await self.quota.release_concurrent(identity, timezone_name=plan.timezone)
            except Exception:
                logger.error("报表并发配额释放失败", job_id=job_id, exc_info=True)
            return {"status": "failed", "run_id": run.id, "error_code": "INTERNAL_ERROR", "error_message": message}

    # 方法作用：通过现有 Chat LangGraph 执行单个分析单元，保持 SQL/ACL 链路复用。
    # Args: unit - 分析单元；state - 报表子图状态和身份摘要。
    # Returns: UnitResult 或失败结果。
    async def _default_execute_unit(self, unit: Any, state: dict[str, Any]) -> UnitResult:
        datasource = str((state.get("plan").requested_datasources or [""])[0])
        from src.graph.workflow import app

        if app is None or not datasource:
            return UnitResult(unit_id=unit.unit_id, datasource=datasource, status="partial", warnings=["缺少已授权数据源或 SQL 工作流尚未启动"], user_error_message="请先选择一个有权限的数据源")
        identity = dict(state.get("identity", {}) or {})
        authorized = dict(state.get("authorized_datasources", {}) or {})
        access = authorized.get(datasource, {})
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
            "session_id": f"report-{unit.unit_id}",
            "request_rate_limit_checked": True,
        }, {"configurable": {"thread_id": scope_thread_id(f"report-{unit.unit_id}")}})
        final = dict(result.get("final_response", {}) or {})
        rows = final.get("data", result.get("query_result_sample", []))
        if not isinstance(rows, list):
            rows = []
        if final.get("success", result.get("sql_valid", True)) is False:
            return UnitResult(unit_id=unit.unit_id, datasource=datasource, status="failed", user_error_message=str(final.get("error_message", "查询失败")), error_code=str(final.get("error_code", "QUERY_FAILED")))
        return UnitResult(unit_id=unit.unit_id, datasource=datasource, rows=[dict(row) for row in rows if isinstance(row, dict)], summary=str((final.get("analysis") or {}).get("summary", "") or ""))


# 方法作用：从 AppContext 获取共享 ReportRunner。
# Args: 无。
# Returns: ReportRunner 单例。
def get_report_runner() -> ReportRunner:
    from src.app_context import get_app_context

    return get_app_context().get_or_create("report_runner", ReportRunner)
