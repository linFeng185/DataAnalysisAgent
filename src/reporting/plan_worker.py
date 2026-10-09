"""报表计划后台生成工作器：创建记录后异步解析计划、起标题并通知用户（设计 48 第 7.2 节）。"""

from __future__ import annotations

import asyncio
from typing import Any

from src.logging_config import get_logger
from src.reporting.planner import ReportPlanner
from src.reporting.store import ReportStore, get_report_store

logger = get_logger(__name__)

# 标题任务标识：与 prompts.py 的 report.title 定义、client 的任务路由名对齐。
_TITLE_LLM_TASK = "report_title"
# 标题长度上限：需求为「不超过 20 字的中文标题」。
_TITLE_MAX_CHARS = 20
# 计划失败时的兜底可读原因，避免把堆栈或连接细节暴露给用户。
_FAILURE_FALLBACK = "计划生成失败，请稍后重试"


# 方法作用：把后台异常转换为用户可读的失败原因。
# Args: exc - 后台规划抛出的异常。
# Returns: 可读原因文本；无法判定时返回通用兜底文案。
def readable_plan_error(exc: BaseException) -> str:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, str) and detail.strip():
        # fastapi.HTTPException（数据源授权拒绝、授权服务不可用）只取 detail，不带堆栈。
        return detail.strip()[:2000]
    if isinstance(exc, (ValueError, LookupError, RuntimeError)):
        message = str(exc).strip()
        if message:
            return message[:2000]
    return _FAILURE_FALLBACK


# 方法作用：调用 report.title 任务生成不超过 20 字的报表标题。
# Args: prompt - 用户原始描述。
# Returns: 模型标题；模型不可用或解析失败时回退为描述前 20 字。
async def generate_report_title(prompt: str) -> str:
    """标题只是展示信息，任何失败都必须回退而不能让整个规划失败。"""
    normalized = " ".join(str(prompt or "").split())
    fallback = normalized[:_TITLE_MAX_CHARS] or "未命名报表"
    logger.debug("报表标题生成入口", prompt_chars=len(normalized))
    try:
        from src.llm.client import is_task_llm_available

        if not is_task_llm_available(_TITLE_LLM_TASK):
            raise RuntimeError("报表标题模型不可用")
        from src.llm.invocation import invoke_structured
        from src.llm.output_contracts import ReportTitleOutput
        from src.llm.prompt_budget import PromptSection

        output = await invoke_structured(
            "report.title",
            [
                PromptSection(
                    "requirement",
                    f"## 报表需求\n{normalized}",
                    priority=100,
                    min_chars=10,
                    max_chars=4000,
                ),
            ],
            output_model=ReportTitleOutput,
            task=_TITLE_LLM_TASK,
            temperature=0.0,
            metadata={"node": "report_plan_worker"},
        )
        title = "".join(str(output.title).split())
        if not title:
            raise ValueError("标题为空")
        logger.info("报表标题生成完成", title_chars=len(title), fallback=False)
        return title[:_TITLE_MAX_CHARS]
    except Exception as exc:
        # 回退必须留完整堆栈，才能与「模型本来就没配」区分开。
        logger.warning("报表标题生成失败，回退描述前缀", error_type=type(exc).__name__, error=str(exc), exc_info=True)
        return fallback


class ReportPlanWorker:
    """后台解析报表计划并把结果写回记录，成功与失败都发一条个人通知。"""

    # 方法作用：初始化工作器、Store 与计划解析器。
    # Args: store - 报表 Store；planner - 计划解析器（可注入 Fake）。
    # Returns: 无返回值。
    def __init__(self, store: ReportStore | None = None, planner: Any | None = None) -> None:
        self.store = store or get_report_store()
        self.planner = planner or ReportPlanner()
        self._tasks: set[asyncio.Task] = set()
        logger.debug("报表后台规划工作器初始化完成", memory=bool(getattr(self.store, "_memory", False)))

    # 方法作用：异步排队一次后台规划，立即返回任务引用供生命周期管理。
    # Args: report_id - 报表标识；identity - 记录拥有者身份；model_id - 显式选择的模型；connection_id - 显式选择的连接。
    # Returns: 已创建的 asyncio.Task。
    def enqueue(self, report_id: str, identity: Any, *, model_id: str = "", connection_id: int | None = None) -> asyncio.Task:
        task = asyncio.create_task(
            self.plan_report(report_id, identity, model_id=model_id, connection_id=connection_id),
            name=f"report-plan-{str(report_id)[:12]}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info("报表后台规划已排队", report_id=report_id, tenant_id=getattr(identity, "tenant_id", 0), user_id=getattr(identity, "user_id", 0))
        return task

    # 方法作用：解析计划、起标题并写回记录，异常一律转成 plan_failed 而不外抛。
    # Args: report_id - 报表标识；identity - 记录拥有者身份；model_id - 显式选择的模型；connection_id - 显式选择的连接。
    # Returns: 规划结果摘要（status/title/plan_hash 或失败原因）。
    async def plan_report(self, report_id: str, identity: Any, *, model_id: str = "", connection_id: int | None = None) -> dict[str, Any]:
        """异常不外抛：后台任务静默失败会让记录永远停在 planning。"""
        logger.debug("报表后台规划入口", report_id=report_id, tenant_id=getattr(identity, "tenant_id", 0), user_id=getattr(identity, "user_id", 0))
        prompt = ""
        try:
            report = await self.store.get_report_row(report_id, identity)
            if report is None:
                raise LookupError("报表记录不存在或已无权访问")
            prompt = str(report.get("prompt", "") or "")
            datasources = [str(item) for item in (report.get("datasources") or [])]
            title = ""
            selection = await self._resolve_llm_selection(identity, connection_id=connection_id, model_id=model_id)
            # 后台规划没有请求上下文，必须显式绑定记录拥有者的可信身份；
            # 无可信身份只禁用知识检索，不采用默认租户或平台身份。
            from src.llm.knowledge_loop import use_trusted_identity
            from src.llm.tenant_config import use_tenant_llm_selection

            background_datasource = datasources[0] if len(datasources) == 1 else ""
            # 与旧 POST /plans 一致：绑定租户模型后 Planner 才会用大模型做语义拆解。
            with use_tenant_llm_selection(selection), use_trusted_identity(
                identity, datasource=background_datasource,
            ):
                plan = await self.planner.parse(
                    prompt,
                    datasources=datasources,
                    timezone=str(report.get("timezone", "Asia/Shanghai") or "Asia/Shanghai"),
                    divergence=str(report.get("divergence", "standard") or "standard"),
                )
                # 计划期发散需要表名清单，与 report.route 共用同一份目录。
                from src.reporting.runner import ReportRunner

                catalog = await ReportRunner._datasource_table_catalog(
                    list(plan.requested_datasources), int(getattr(identity, "tenant_id", 0) or 0)
                )
                plan = await self.planner.diverge(plan, datasources=datasources, catalog=catalog)
                # 数据源授权必须在计划落库前重新解析，越权计划不能进入「未生成」状态。
                await self._resolve_access(identity, plan)
                title = await generate_report_title(prompt)
            if not await self.store.save_planned_job(report_id, identity, plan=plan, title=title):
                raise LookupError("报表记录已不可写，计划未落库")
        except Exception as exc:
            logger.error("报表后台规划失败", report_id=report_id, error_type=type(exc).__name__, exc_info=True)
            reason = readable_plan_error(exc)
            try:
                await self.store.mark_plan_failed(report_id, identity, reason=reason)
            except Exception:
                logger.error("报表计划失败状态写入失败", report_id=report_id, exc_info=True)
            await self._notify(identity, kind="personal", title="报表计划生成失败", body=reason, report_id=report_id)
            return {"report_id": report_id, "status": "plan_failed", "reason": reason}
        # 通知类型沿用「任务执行完毕的个人通知」这一类；「计划就绪 / 计划失败」的区分
        # 放在标题、正文与跳转链接里，不在用户定义的四类之外另造类型。
        await self._notify(
            identity,
            kind="personal",
            title="报表计划已生成",
            body=f"计划已生成：{title}",
            report_id=report_id,
        )
        logger.info("报表后台规划完成", report_id=report_id, status="draft", unit_count=len(plan.analysis_units), title=title)
        return {"report_id": report_id, "status": "draft", "title": title, "plan_hash": plan.canonical_hash()}

    # 方法作用：解析后台规划可用的租户模型，未配置时返回 None 交给规则回退。
    # Args: identity - 记录拥有者身份；model_id - 显式选择的模型；connection_id - 显式选择的连接。
    # Returns: 租户 LLM 选择或未配置时的 None。
    async def _resolve_llm_selection(self, identity: Any, *, model_id: str = "", connection_id: int | None = None) -> Any:
        from src.llm.tenant_config import resolve_tenant_llm_selection

        try:
            return await resolve_tenant_llm_selection(
                tenant_id=int(getattr(identity, "tenant_id", 0) or 0),
                connection_id=connection_id,
                model_id=model_id,
            )
        except (LookupError, ValueError) as exc:
            # 未配置模型是正常状态：规划会自动回退规则拆分，不能因此让后台任务失败。
            logger.info("报表后台规划未启用租户模型，按规则拆分", tenant_id=getattr(identity, "tenant_id", 0), reason=str(exc))
            return None
        except Exception:
            logger.error("报表后台规划租户模型解析失败", tenant_id=getattr(identity, "tenant_id", 0), exc_info=True)
            return None

    # 方法作用：复用报表路由的授权解析，确保后台计划与预览计划走同一套数据源校验。
    # Args: identity - 记录拥有者身份；plan - 待落库计划。
    # Returns: 无返回值；越权时抛出异常由调用方转成 plan_failed。
    @staticmethod
    async def _resolve_access(identity: Any, plan: Any) -> None:
        from src.api.routes.report_workbench import _resolve_plan_access

        await _resolve_plan_access(identity, plan)

    # 方法作用：发送个人通知；通知服务缺失或发送失败都不影响计划结果。
    # Args: identity - 记录拥有者身份；kind - 通知类型；title - 通知标题；body - 通知正文；report_id - 报表标识。
    # Returns: 无返回值。
    async def _notify(self, identity: Any, *, kind: str, title: str, body: str, report_id: str) -> None:
        try:
            from src.notifications.service import notify_user
        except ImportError:
            # 通知模块由另一条并行改动提供：缺失时只记 warning，不阻塞计划链路。
            logger.warning("通知服务尚未就绪，跳过报表计划通知", kind=kind, report_id=report_id)
            return
        try:
            await notify_user(
                int(getattr(identity, "tenant_id", 0) or 0),
                int(getattr(identity, "user_id", 0) or 0),
                kind,
                title,
                body,
                # 路由必须真实存在：这里曾写 /report-workbench/reports/{id}，
                # 而路由表里只有 /report-workbench/jobs，点击通知落到空匹配直接白屏。
                f"/report-workbench/jobs?report={report_id}",
            )
            logger.info("报表计划通知已发送", kind=kind, report_id=report_id)
        except Exception:
            logger.error("报表计划通知发送失败", kind=kind, report_id=report_id, exc_info=True)

    # 方法作用：关闭工作器注册的后台任务，确保退出前不再有规划在途。
    # Args: self - 当前工作器。
    # Returns: 无返回值。
    async def close(self) -> None:
        """AppContext 关闭时取消并等待全部本进程规划任务。"""
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("报表后台规划工作器已关闭", task_count=len(tasks))


# 方法作用：从 AppContext 获取共享后台规划工作器。
# Args: 无。
# Returns: ReportPlanWorker 单例。
def get_report_plan_worker() -> ReportPlanWorker:
    from src.app_context import get_app_context

    return get_app_context().get_or_create("report_plan_worker", ReportPlanWorker, closer=ReportPlanWorker.close)
