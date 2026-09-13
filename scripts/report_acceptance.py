"""通过正式认证与 API 运行真实报表验收，不替换模型、数据库或权限边界。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

PROMPT = """立即生成一份《2026 年经营全景报表》，
不要定时，页面保留 7 天：

1. 销售：按年月、地区、渠道统计订单净销售额、订单数、客单价，显式区分历史模拟/当年/未来情景，
   给出同比、季节性与地区 Top/Bottom；
2. 财务：按年月核对订单净额与发票金额的差额，统计退款规模（金额为负），
   并给出成本中心的预算 vs 实际费用执行率；
3. 履约：对比配送费用、采购加权单位成本、库存水平与订单量的走势；
4. 营销：广告归因收入与订单净销售额的对比（归因记录多对一，不能等同唯一订单收入）；
   市场景气指数、日均气温与地区销售的统计相关（弱关联，标注相关不等于因果）；
5. 数据质量：说明时间覆盖范围、被截断或被权限过滤的部分，以及未能建立的关系。"""


# 方法作用：通过真实应用认证、规划和生成深度报表，记录可追溯且不含凭证的验收摘要。
# Args: 无；凭证从 ACCEPTANCE_TENANT/USER/PASSWORD 环境变量读取。
# Returns: 无返回值；失败通过异常和日志报告。
async def main() -> None:
    if os.environ.get("RUN_LIVE_LLM_TESTS") != "1":
        raise RuntimeError("真实模型验收必须显式设置 RUN_LIVE_LLM_TESTS=1")
    os.environ.setdefault("LOG_FORMAT", "json")
    os.environ.setdefault("LOG_FILE", "logs/report-acceptance-20260913.jsonl")
    os.environ["AUTOMATION_ENABLED"] = "false"
    os.environ["REPORT_SCHEDULER_ENABLED"] = "false"
    from src.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        logger = logging.getLogger("report_acceptance")
        logger.info("真实报表验收入口 divergence=deep")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost", timeout=600) as client:
            login = await client.post("/api/v1/auth/login", json={
                "tenant_code": os.environ["ACCEPTANCE_TENANT"],
                "username": os.environ["ACCEPTANCE_USER"],
                "password": os.environ["ACCEPTANCE_PASSWORD"],
            })
            login.raise_for_status()
            sources = await client.get("/api/v1/datasources")
            sources.raise_for_status()
            names = [item["name"] for item in sources.json()["datasources"]]
            started = datetime.now(timezone.utc).isoformat()
            preview = await client.post("/api/v1/report-workbench/plans", json={"prompt": PROMPT, "datasources": names, "divergence": "deep"})
            preview.raise_for_status()
            payload = preview.json()
            plan = payload["plan"]
            assert plan["divergence"] == "deep" and plan["execution_mode"] == "immediate"
            assert plan["page_ttl_days"] == 7 and not plan["permanent"]
            logger.info("深度计划验收 unit_count=%s time_ranges=%s", len(plan["analysis_units"]), [unit["time_range"] for unit in plan["analysis_units"]])
            created = await client.post("/api/v1/report-workbench/jobs", json={"prompt": PROMPT, "plan_hash": payload["plan_hash"], "confirm": True, "idempotency_key": f"acceptance-{uuid4().hex}"})
            created.raise_for_status()
            job_id = created.json()["job_id"]
            logger.info("真实验收任务已创建 job_id=%s", job_id)
            detail = {}
            for _attempt in range(180):
                await asyncio.sleep(5)
                response = await client.get(f"/api/v1/report-workbench/jobs/{job_id}")
                response.raise_for_status()
                detail = response.json()
                run = detail.get("latest_run") or {}
                if _attempt % 6 == 0:
                    logger.info("真实报表验收进度 job_id=%s status=%s last_node=%s", job_id, run.get("status"), run.get("last_node"))
                if run.get("status") in {"succeeded", "partial", "failed", "cancelled"}:
                    break
            else:
                raise TimeoutError("真实报表运行超过十五分钟")
            run_id = detail["latest_run"]["id"]
            run_response = await client.get(f"/api/v1/report-workbench/runs/{run_id}")
            run_response.raise_for_status()
            report_response = await client.get(f"/api/v1/report-workbench/reports/{job_id}")
            report = report_response.json() if report_response.status_code == 200 else {}
            identity_after = await client.get("/api/v1/auth/me")
            identity_after.raise_for_status()
            output = {"started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(), "job_id": job_id, "plan": plan, "run": run_response.json(), "report": report, "identity_status": identity_after.status_code}
            path = Path(f"logs/report-acceptance-{job_id}.json")
            path.write_text(json.dumps(output, ensure_ascii=False, default=str), encoding="utf-8")
            logger.info("真实报表验收完成 job_id=%s status=%s report_status=%s sections=%s artifact=%s", job_id, detail["latest_run"]["status"], report_response.status_code, len(report.get("document", {}).get("sections", [])), str(path))


if __name__ == "__main__":
    from src.main import selector_event_loop_factory

    with asyncio.Runner(loop_factory=selector_event_loop_factory) as runner:
        runner.run(main())
