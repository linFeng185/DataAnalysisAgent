# tests/test_reporting/test_unit_snapshots.py
"""报表单元执行快照与单单元重跑契约。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class TestUnitSnapshotContract:
    """覆盖快照必须覆盖的字段与迁移契约。"""

    # 方法作用：验证快照字段覆盖反推排查所需的最小集合。
    def test_snapshot_carries_debug_fields(self) -> None:
        """缺 unit_id 的 SQL 无法定位到单元，缺提示词无法解释模型为何这样写 SQL。"""
        # Arrange / Act
        from src.reporting.snapshots import UnitSnapshot

        fields = set(UnitSnapshot.model_fields)

        # Assert
        assert {"unit_id", "sql_text", "prompt_text", "model_id", "row_count",
                "full_count", "truncated", "truncated_reason", "elapsed_ms", "error_message"} <= fields

    # 方法作用：验证迁移包含 RLS 与单元维度唯一键。
    def test_migration_enforces_rls_and_uniqueness(self) -> None:
        # Arrange / Act
        sql = Path("migrations/037_report_unit_snapshots.sql").read_text(encoding="utf-8")

        # Assert
        assert "FORCE ROW LEVEL SECURITY" in sql
        assert "UNIQUE (run_id, unit_id)" in sql


# 方法作用：构造只返回固定结果的工作流替身，避免真实 LangGraph 与数据库依赖。
# Args: response - ainvoke 返回的结果字典。
# Returns: 提供 await ainvoke 的假工作流对象。
def _fake_workflow(response: dict):
    class FakeApp:
        # 方法作用：忽略入参直接返回固定结果。
        # Args: payload - 工作流状态；config - 线程配置。
        # Returns: 预置结果字典。
        async def ainvoke(self, payload, config):
            return response

    return FakeApp()


class TestBuildSnapshotNormalization:
    """覆盖 build_snapshot 的字段归一与截断原因可读化。"""

    # 方法作用：验证超长 SQL 在入库前被截到上限，避免撑爆文本列。
    def test_long_sql_is_clipped_to_limit(self) -> None:
        from src.reporting.snapshots import SQL_TEXT_LIMIT, build_snapshot

        snapshot = build_snapshot(unit_id="unit_1", sql="SELECT 1 " * 20_000)

        assert SQL_TEXT_LIMIT == 100_000
        assert len(snapshot.sql_text) == SQL_TEXT_LIMIT

    # 方法作用：验证敏感参数键被剔除，普通键原样保留。
    def test_sensitive_params_are_dropped(self) -> None:
        from src.reporting.snapshots import build_snapshot

        snapshot = build_snapshot(
            unit_id="unit_1",
            params={"api_key": "k", "Access_Token": "t", "db_password": "p", "client_secret": "s", "timeout": 3},
        )

        assert snapshot.params == {"timeout": 3}
        # 值里出现 token 字样但键不敏感时不能误删（否则丢掉正常参数）
        assert build_snapshot(unit_id="unit_1", params={"note": "token"}).params == {"note": "token"}

    # 方法作用：验证 truncated_reason 由调用方给出，build_snapshot 不自行推断。
    def test_truncated_reason_is_not_inferred(self) -> None:
        from src.reporting.snapshots import build_snapshot

        assert build_snapshot(unit_id="unit_1", truncated=True).truncated_reason == ""
        assert build_snapshot(unit_id="unit_1", truncated=True, truncated_reason="上游样本被裁").truncated_reason == "上游样本被裁"

    # 方法作用：验证按迁移列宽裁剪超长字段，避免快照写入被列宽拒绝。
    def test_column_widths_are_clipped(self) -> None:
        from src.reporting.snapshots import build_snapshot

        snapshot = build_snapshot(unit_id="unit_1", model_id="m" * 300, truncated_reason="r" * 300, error_code="e" * 100)

        assert len(snapshot.model_id) == 128
        assert len(snapshot.truncated_reason) == 200
        assert len(snapshot.error_code) == 64

    # 方法作用：验证截断原因生成可读文案。
    def test_describe_truncation_formats_readable_reason(self) -> None:
        from src.reporting.snapshots import describe_truncation

        assert describe_truncation(5000, 80000, 5000) == "查询返回 80000 行，页面展示 5000 行（上限 5000）"

    # 方法作用：验证未发生截断时返回空串，不产生噪音文案。
    def test_describe_truncation_empty_when_not_truncated(self) -> None:
        from src.reporting.snapshots import describe_truncation

        assert describe_truncation(200, 200, 200) == ""
        assert describe_truncation(200, 0, 200) == ""
        assert describe_truncation(100, 50, 200) == ""


class TestUnitSnapshotStoreMemory:
    """覆盖内存分支的保存、覆盖、排序与租户隔离。"""

    # 方法作用：验证保存后能按运行读回全部调试字段。
    @pytest.mark.asyncio
    async def test_save_and_list_roundtrip(self) -> None:
        from src.reporting.snapshots import build_snapshot
        from src.reporting.store import ReportStore

        store = ReportStore(memory=True)
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        snapshot = build_snapshot(
            run_id="run-a", report_id="rep-a", tenant_id=3, user_id=7, unit_id="unit_1",
            status="success", datasource="oracle", dialect="mysql", sql="SELECT 1",
            prompt_text="销售趋势", model_id="qwen3", params={"selected_datasources": ["oracle"]},
            row_count=5, full_count=9, truncated=True, truncated_reason="查询返回 9 行，页面展示 5 行",
            elapsed_ms=120,
        )

        await store.save_unit_snapshot(snapshot, identity)
        rows = await store.list_unit_snapshots("run-a", identity)

        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == snapshot.id
        assert row["unit_id"] == "unit_1"
        assert row["sql_text"] == "SELECT 1"
        assert row["prompt_text"] == "销售趋势"
        assert row["model_id"] == "qwen3"
        assert row["params"] == {"selected_datasources": ["oracle"]}
        assert row["row_count"] == 5
        assert row["full_count"] == 9
        assert row["truncated"] is True
        assert row["truncated_reason"] == "查询返回 9 行，页面展示 5 行"
        assert row["elapsed_ms"] == 120
        assert row["datasource"] == "oracle"
        assert row["dialect"] == "mysql"
        assert row["tenant_id"] == 3

    # 方法作用：验证重跑覆盖同一 (run_id, unit_id) 的快照且保持快照标识稳定。
    @pytest.mark.asyncio
    async def test_rerun_overwrites_same_unit_snapshot(self) -> None:
        from src.reporting.snapshots import build_snapshot
        from src.reporting.store import ReportStore

        store = ReportStore(memory=True)
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        first = build_snapshot(run_id="run-a", unit_id="unit_1", sql="SELECT 1")
        await store.save_unit_snapshot(first, identity)
        second = build_snapshot(run_id="run-a", unit_id="unit_1", sql="SELECT 2")

        await store.save_unit_snapshot(second, identity)
        rows = await store.list_unit_snapshots("run-a", identity)

        assert len(rows) == 1
        assert rows[0]["sql_text"] == "SELECT 2"
        # 快照标识跨重跑保持稳定，与 PG 的 ON CONFLICT DO UPDATE 语义一致
        assert rows[0]["id"] == first.id

    # 方法作用：验证列表按 unit_id 排序，页面按单元查看时顺序稳定。
    @pytest.mark.asyncio
    async def test_list_sorted_by_unit_id(self) -> None:
        from src.reporting.snapshots import build_snapshot
        from src.reporting.store import ReportStore

        store = ReportStore(memory=True)
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        await store.save_unit_snapshot(build_snapshot(run_id="run-b", unit_id="unit_b", sql="SELECT 1"), identity)
        await store.save_unit_snapshot(build_snapshot(run_id="run-b", unit_id="unit_a", sql="SELECT 2"), identity)

        rows = await store.list_unit_snapshots("run-b", identity)

        assert [row["unit_id"] for row in rows] == ["unit_a", "unit_b"]

    # 方法作用：验证其他租户读不到不属于自己的快照。
    @pytest.mark.asyncio
    async def test_other_tenant_lists_nothing(self) -> None:
        from src.reporting.snapshots import build_snapshot
        from src.reporting.store import ReportStore

        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        stranger = SimpleNamespace(tenant_id=4, user_id=7, role="analyst")
        await store.save_unit_snapshot(build_snapshot(run_id="run-c", unit_id="unit_1", sql="SELECT 1"), owner)

        assert await store.list_unit_snapshots("run-c", stranger) == []

    # 方法作用：验证归属取执行身份而不是调用方传入的字段，防止伪造查看范围。
    @pytest.mark.asyncio
    async def test_save_uses_identity_for_ownership(self) -> None:
        from src.reporting.snapshots import build_snapshot
        from src.reporting.store import ReportStore

        store = ReportStore(memory=True)
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        spoofed = build_snapshot(run_id="run-d", unit_id="unit_1", sql="SELECT 1", tenant_id=99, user_id=99)

        await store.save_unit_snapshot(spoofed, identity)
        rows = await store.list_unit_snapshots("run-d", identity)

        assert rows[0]["tenant_id"] == 3
        assert rows[0]["user_id"] == 7


class TestRunnerUnitSnapshot:
    """覆盖运行器在成功、失败与持久化异常下的快照行为。"""

    # 方法作用：验证成功单元在返回前落快照，且 SQL、行数、截断原因完整。
    @pytest.mark.asyncio
    async def test_success_run_persists_snapshot(self, monkeypatch) -> None:
        import src.graph.workflow as workflow_module
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        monkeypatch.setattr(workflow_module, "app", _fake_workflow({
            "final_response": {"success": True, "data": [{"v": 1}]},
            "generated_sql": "SELECT v FROM t",
            "dialect": "mysql",
            "query_result_full_count": 9,
        }))
        store = ReportStore(memory=True)
        runner = ReportRunner(store=store)
        unit = AnalysisUnit(question="销售明细")
        plan = ReportPlan(requested_datasources=["oracle"], analysis_units=[unit])
        state = {
            "plan": plan, "run_id": "run-snap-1", "report_id": "rep-snap-1",
            "identity": {"tenant_id": 3, "user_id": 7, "role": "analyst"},
            "authorized_datasources": {"oracle": {}},
        }

        result = await runner._default_execute_unit(unit, state)

        assert result.status == "success"
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        rows = await store.list_unit_snapshots("run-snap-1", identity)
        assert len(rows) == 1
        row = rows[0]
        assert row["unit_id"] == unit.unit_id
        assert row["status"] == "success"
        assert row["report_id"] == "rep-snap-1"
        assert row["sql_text"] == "SELECT v FROM t"
        assert row["dialect"] == "mysql"
        assert row["datasource"] == "oracle"
        assert row["row_count"] == 1
        assert row["full_count"] == 9
        assert row["truncated"] is True
        assert "查询返回 9 行" in row["truncated_reason"]
        assert row["elapsed_ms"] >= 0
        assert "销售明细" in row["prompt_text"]

    # 方法作用：验证失败单元也落快照，错误码与错误信息可反查。
    @pytest.mark.asyncio
    async def test_failed_run_persists_error_snapshot(self, monkeypatch) -> None:
        import src.graph.workflow as workflow_module
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        monkeypatch.setattr(workflow_module, "app", _fake_workflow({
            "final_response": {"success": False, "error_message": "SQL 生成失败", "error_code": "SQL_INVALID"},
        }))
        store = ReportStore(memory=True)
        runner = ReportRunner(store=store)
        unit = AnalysisUnit(question="销售明细")
        plan = ReportPlan(requested_datasources=["oracle"], analysis_units=[unit])
        state = {
            "plan": plan, "run_id": "run-snap-2",
            "identity": {"tenant_id": 3, "user_id": 7, "role": "analyst"},
            "authorized_datasources": {"oracle": {}},
        }

        result = await runner._default_execute_unit(unit, state)

        assert result.status == "failed"
        rows = await store.list_unit_snapshots("run-snap-2", SimpleNamespace(tenant_id=3, user_id=7, role="analyst"))
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["error_code"] == "SQL_INVALID"
        assert rows[0]["error_message"] == "SQL 生成失败"

    # 方法作用：验证快照写入失败只记告警，不阻断报表生成。
    @pytest.mark.asyncio
    async def test_snapshot_write_failure_does_not_break_run(self, monkeypatch) -> None:
        import src.graph.workflow as workflow_module
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        monkeypatch.setattr(workflow_module, "app", _fake_workflow({
            "final_response": {"success": True, "data": [{"v": 1}]},
        }))
        store = ReportStore(memory=True)
        monkeypatch.setattr(store, "save_unit_snapshot", AsyncMock(side_effect=RuntimeError("pg down")))
        runner = ReportRunner(store=store)
        unit = AnalysisUnit(question="销售明细")
        plan = ReportPlan(requested_datasources=["oracle"], analysis_units=[unit])
        state = {
            "plan": plan, "run_id": "run-snap-3",
            "identity": {"tenant_id": 3, "user_id": 7, "role": "analyst"},
            "authorized_datasources": {"oracle": {}},
        }

        result = await runner._default_execute_unit(unit, state)

        assert result.status == "success"


class TestRerunAndSnapshotApi:
    """覆盖单单元重跑与快照查询端点契约。"""

    # 方法作用：准备内存 Store 中的报表、计划、运行记录与路由替身。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: (store, identity, job_id, unit)。
    async def _prepare(self, monkeypatch):
        import src.api.routes.report_workbench as route
        import src.graph.workflow as workflow_module
        import src.llm.client as llm_client
        import src.reporting.runner as runner_module
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        store = ReportStore(memory=True)
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        unit = AnalysisUnit(question="销售明细")
        plan = ReportPlan(requested_datasources=["oracle"], analysis_units=[unit])
        job, _ = await store.create_planning_job(identity, "销售报表", datasources=["oracle"])
        await store.update_report_plan(job.id, identity, plan=plan, columns={})
        await store.create_run(job.id, identity)

        monkeypatch.setattr(workflow_module, "app", _fake_workflow({
            "final_response": {"success": True, "data": [{"v": 1}]},
            "generated_sql": "SELECT v FROM t",
            "dialect": "mysql",
        }))
        # 路由模型不可用时退回全源检索，测试不发真实 LLM 请求
        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)
        runner = ReportRunner(store=store)
        monkeypatch.setattr(runner_module, "get_report_runner", lambda: runner)
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "_require_capability", AsyncMock())
        monkeypatch.setattr(route, "_has_view_all", AsyncMock(return_value=False))
        monkeypatch.setattr(route, "_resolve_plan_access", AsyncMock(return_value={}))
        return store, identity, job.id, unit

    # 方法作用：验证重跑端点执行单单元并返回新快照标识。
    @pytest.mark.asyncio
    async def test_rerun_returns_snapshot_id(self, monkeypatch) -> None:
        import src.api.routes.report_workbench as route

        store, identity, job_id, unit = await self._prepare(monkeypatch)

        payload = await route.rerun_report_unit(job_id, unit.unit_id)

        assert payload["unit_id"] == unit.unit_id
        assert payload["status"] == "success"
        assert payload["snapshot_id"]
        runs = await store.list_runs(job_id, identity, limit=1)
        rows = await store.list_unit_snapshots(runs[0].id, identity)
        assert len(rows) == 1
        assert rows[0]["sql_text"] == "SELECT v FROM t"
        assert rows[0]["id"] == payload["snapshot_id"]

    # 方法作用：验证计划中不存在的单元返回 404，不能凭空构造执行范围。
    @pytest.mark.asyncio
    async def test_rerun_unknown_unit_is_404(self, monkeypatch) -> None:
        import src.api.routes.report_workbench as route
        from fastapi import HTTPException

        _, _, job_id, _ = await self._prepare(monkeypatch)

        with pytest.raises(HTTPException) as excinfo:
            await route.rerun_report_unit(job_id, "unit_missing")

        assert excinfo.value.status_code == 404

    # 方法作用：验证没有运行记录的报表禁止重跑，避免无 run 归属的快照。
    @pytest.mark.asyncio
    async def test_rerun_without_run_is_409(self, monkeypatch) -> None:
        import src.api.routes.report_workbench as route
        from fastapi import HTTPException
        from src.reporting.models import AnalysisUnit, ReportPlan

        store, identity, _, _ = await self._prepare(monkeypatch)
        unit = AnalysisUnit(question="库存明细")
        job, _ = await store.create_planning_job(identity, "库存报表", datasources=["oracle"])
        await store.update_report_plan(job.id, identity, plan=ReportPlan(requested_datasources=["oracle"], analysis_units=[unit]), columns={})

        with pytest.raises(HTTPException) as excinfo:
            await route.rerun_report_unit(job.id, unit.unit_id)

        assert excinfo.value.status_code == 409

    # 方法作用：验证快照查询端点返回最近一次执行的快照。
    @pytest.mark.asyncio
    async def test_snapshot_endpoint_returns_latest(self, monkeypatch) -> None:
        import src.api.routes.report_workbench as route

        _, _, job_id, unit = await self._prepare(monkeypatch)
        await route.rerun_report_unit(job_id, unit.unit_id)

        payload = await route.get_unit_snapshot(job_id, unit.unit_id)

        assert payload["snapshot"] is not None
        assert payload["snapshot"]["sql_text"] == "SELECT v FROM t"
        assert payload["snapshot"]["unit_id"] == unit.unit_id

    # 方法作用：验证从未执行过的单元查询返回 null 而不是报错。
    @pytest.mark.asyncio
    async def test_snapshot_endpoint_null_when_missing(self, monkeypatch) -> None:
        import src.api.routes.report_workbench as route

        _, _, job_id, _ = await self._prepare(monkeypatch)

        payload = await route.get_unit_snapshot(job_id, "unit_missing")

        assert payload == {"snapshot": None}
