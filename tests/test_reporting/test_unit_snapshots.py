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
        # 方法作用：忽略入参直接返回固定结果的浅拷贝。
        # Args: payload - 工作流状态；config - 线程配置。
        # Returns: 预置结果字典的浅拷贝。
        async def ainvoke(self, payload, config):
            # 真实 CompiledStateGraph 每次 ainvoke 都重建状态字典；替身反复返回同一个
            # 对象时，执行侧收尾释放状态引用会污染下一次调用（重查轮拿到空数据）。
            return dict(response)

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

    # 方法作用：验证截断原因生成可读文案，措辞描述真实发生的样本收敛。
    def test_describe_truncation_formats_readable_reason(self) -> None:
        from src.reporting.snapshots import describe_truncation

        assert describe_truncation(5000, 80000, 5000) == "进入分析链路 5000 行，查询全量 80000 行（生效样本上限 5000 行）"

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
            row_count=5, full_count=9, truncated=True, truncated_reason="进入分析链路 5 行，查询全量 9 行（生效样本上限 5000 行）",
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
        assert row["truncated_reason"] == "进入分析链路 5 行，查询全量 9 行（生效样本上限 5000 行）"
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
        assert "查询全量 9 行" in row["truncated_reason"]
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

    # 方法作用：验证多源单元落快照时 SQL 非空，覆盖实跑里跨源单元 sql_text 为空的回归。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_multisource_unit_snapshot_keeps_merged_sql(self, monkeypatch) -> None:
        """多源合并最终 return 里的 generated_sql 被删掉时，这条用例立刻变红。"""
        # Arrange：多源图最终状态由真实合并节点产出，只替身展示子图、LLM 与工作流入口。
        import src.graph.workflow as workflow_module
        from unittest.mock import Mock

        from src.graph.nodes.multi_source import merge_results_node
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        presenter = SimpleNamespace(ainvoke=AsyncMock(return_value={}))
        monkeypatch.setattr("src.graph.subgraphs.result_presentation.build_result_presentation_subgraph", Mock(return_value=presenter))
        monkeypatch.setattr("src.llm.client.is_task_llm_available", Mock(return_value=False))
        merged = await merge_results_node({
            "user_query": "对比 oracle 与 ch 的销售额",
            "multi_source_results": [
                {"datasource": "oracle", "success": True, "sql": "SELECT sales FROM orders", "data": [{"sales": 12}]},
                {"datasource": "ch", "success": True, "sql": "SELECT attributed FROM attribution", "data": [{"attributed": 18}]},
            ],
        })
        monkeypatch.setattr(workflow_module, "app", _fake_workflow(merged))
        store = ReportStore(memory=True)
        runner = ReportRunner(store=store)
        unit = AnalysisUnit(question="跨源销售额对比")
        plan = ReportPlan(requested_datasources=["oracle", "ch"], analysis_units=[unit])
        state = {
            "plan": plan, "run_id": "run-snap-multi", "report_id": "rep-snap-multi",
            "identity": {"tenant_id": 3, "user_id": 7, "role": "analyst"},
            "authorized_datasources": {"oracle": {}, "ch": {}},
            "unit_datasources": {str(unit.unit_id): ["oracle", "ch"]},
        }

        # Act
        result = await runner._default_execute_unit(unit, state)

        # Assert：快照 SQL 必须带各来源标签与语句。
        assert result.status == "success"
        rows = await store.list_unit_snapshots("run-snap-multi", SimpleNamespace(tenant_id=3, user_id=7, role="analyst"))
        assert len(rows) == 1
        assert rows[0]["datasource"] == "oracle"
        assert "-- oracle" in rows[0]["sql_text"] and "SELECT sales FROM orders" in rows[0]["sql_text"]
        assert "-- ch" in rows[0]["sql_text"] and "SELECT attributed FROM attribution" in rows[0]["sql_text"]


# 方法作用：构造只用于 ContextVar 断言的假租户模型选择，不读取任何真实凭证。
# Args: tenant_id - 选择归属租户。
# Returns: 字段完整但不连接任何真实服务的 TenantLLMSelection。
def _fake_tenant_selection(tenant_id: int = 3):
    from src.llm.tenant_config import TenantLLMSelection

    return TenantLLMSelection(
        tenant_id=tenant_id,
        connection_id=2,
        connection_name="默认连接",
        provider_code="deepseek",
        protocol="openai",
        model_catalog_id=11,
        model_id="deepseek-v4-flash",
        base_url="http://127.0.0.1:1/v1",
        api_key="fake-key",
    )


# 方法作用：准备内存 Store 中的报表、计划、运行记录与路由替身。
# Args: monkeypatch - pytest 补丁工具。
# Returns: (store, identity, job_id, unit)。
async def _prepare_rerun_endpoint(monkeypatch):
    import src.api.routes.report_workbench as route
    import src.graph.workflow as workflow_module
    import src.llm.client as llm_client
    import src.llm.tenant_config as tenant_config
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
    # 真实解析要连 PostgreSQL 并解密凭证；单测只替身解析结果，避免任何外部依赖。
    monkeypatch.setattr(tenant_config, "resolve_tenant_llm_selection", AsyncMock(return_value=_fake_tenant_selection()))
    runner = ReportRunner(store=store)
    monkeypatch.setattr(runner_module, "get_report_runner", lambda: runner)
    monkeypatch.setattr(route, "get_report_store", lambda: store)
    monkeypatch.setattr(route, "get_current_identity", lambda: identity)
    monkeypatch.setattr(route, "_require_capability", AsyncMock())
    monkeypatch.setattr(route, "_has_view_all", AsyncMock(return_value=False))
    monkeypatch.setattr(route, "_resolve_plan_access", AsyncMock(return_value={}))
    return store, identity, job.id, unit


class TestRerunAndSnapshotApi:
    """覆盖单单元重跑与快照查询端点契约。"""

    # 方法作用：复用模块级夹具，保证重跑相关用例的准备方式完全一致。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: (store, identity, job_id, unit)。
    async def _prepare(self, monkeypatch):
        return await _prepare_rerun_endpoint(monkeypatch)

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


class TestRerunUnitTenantLlmScope:
    """覆盖实机取证（features/23 的 23.41）：重跑必须像调度器一样绑定租户模型。

    没有该绑定时 `is_task_llm_available("report.route")` 会落到 `resolve_llm_task_target`
    的进程默认配置上；租户 3 的 report_route 恰为 none，于是被判定"路由模型不可用"、
    退回全源检索，把五个无关数据源各查了一遍 COUNT(*)，还如实返回了 success。
    """

    # 方法作用：验证数据源路由与单元执行看到同一个租户模型选择，且退出后复位。
    @pytest.mark.asyncio
    async def test_rerun_binds_same_selection_for_route_and_execute(self, monkeypatch) -> None:
        # Arrange：替身解析结果，并把 ContextVar 取值点插在两条真实调用路径的入口。
        import src.api.routes.report_workbench as route
        import src.llm.tenant_config as tenant_config
        import src.reporting.runner as runner_module

        _, _, job_id, unit = await _prepare_rerun_endpoint(monkeypatch)
        selection = _fake_tenant_selection()
        seen: list[tuple[str, object]] = []
        runner = runner_module.get_report_runner()

        async def fake_route_units(plan, requested, tenant_id):
            seen.append(("route", tenant_config.get_current_tenant_llm_selection()))
            return {str(unit.unit_id): ["oracle"]}

        async def fake_execute_unit(unit_arg, state):
            seen.append(("execute", tenant_config.get_current_tenant_llm_selection()))
            return SimpleNamespace(status="success")

        monkeypatch.setattr(runner, "_route_units", fake_route_units)
        monkeypatch.setattr(runner, "_default_execute_unit", fake_execute_unit)
        monkeypatch.setattr(tenant_config, "resolve_tenant_llm_selection", AsyncMock(return_value=selection))

        # Act
        payload = await route.rerun_report_unit(job_id, unit.unit_id)

        # Assert：两条路径都在作用域内，且拿到的是同一个对象而不是各自解析一次。
        assert [name for name, _ in seen] == ["route", "execute"]
        assert all(seen_selection is selection for _, seen_selection in seen)
        assert payload["status"] == "success"
        # 离开作用域必须复位：残留的选择会污染同一 Worker 的下一次请求。
        assert tenant_config.get_current_tenant_llm_selection() is None

    # 方法作用：验证单元执行抛错时租户模型上下文同样复位，不随异常泄漏。
    @pytest.mark.asyncio
    async def test_rerun_resets_selection_when_execute_raises(self, monkeypatch) -> None:
        import src.api.routes.report_workbench as route
        import src.llm.tenant_config as tenant_config
        import src.reporting.runner as runner_module

        _, _, job_id, unit = await _prepare_rerun_endpoint(monkeypatch)
        runner = runner_module.get_report_runner()

        async def failing_execute_unit(unit_arg, state):
            raise RuntimeError("单元执行中断")

        monkeypatch.setattr(runner, "_route_units", AsyncMock(return_value={}))
        monkeypatch.setattr(runner, "_default_execute_unit", failing_execute_unit)

        with pytest.raises(RuntimeError):
            await route.rerun_report_unit(job_id, unit.unit_id)

        assert tenant_config.get_current_tenant_llm_selection() is None

    # 方法作用：验证租户未配置可用模型时按规则回退继续重跑，与既有报表入口口径一致。
    @pytest.mark.asyncio
    async def test_rerun_without_configured_model_still_runs(self, monkeypatch) -> None:
        import src.api.routes.report_workbench as route
        import src.llm.tenant_config as tenant_config
        import src.reporting.runner as runner_module

        store, identity, job_id, unit = await _prepare_rerun_endpoint(monkeypatch)
        seen: list[object] = []
        runner = runner_module.get_report_runner()
        real_route_units = runner._route_units

        async def spy_route_units(plan, requested, tenant_id):
            seen.append(tenant_config.get_current_tenant_llm_selection())
            return await real_route_units(plan, requested, tenant_id)

        monkeypatch.setattr(runner, "_route_units", spy_route_units)
        monkeypatch.setattr(
            tenant_config,
            "resolve_tenant_llm_selection",
            AsyncMock(side_effect=LookupError("当前租户未配置可用的 LLM 连接和模型")),
        )

        payload = await route.rerun_report_unit(job_id, unit.unit_id)

        # 未配置是正常状态：绑定的是 None，而不是让整个重跑失败。
        assert seen == [None]
        assert payload["status"] == "success"
        assert payload["snapshot_id"]
        runs = await store.list_runs(job_id, identity, limit=1)
        assert len(await store.list_unit_snapshots(runs[0].id, identity)) == 1

    # 方法作用：验证模型配置查询异常返回 503，且不执行单元、不写成功快照。
    @pytest.mark.asyncio
    async def test_rerun_resolution_error_is_503_and_skips_execution(self, monkeypatch) -> None:
        import src.api.routes.report_workbench as route
        import src.llm.tenant_config as tenant_config
        import src.reporting.runner as runner_module
        from fastapi import HTTPException

        store, identity, job_id, unit = await _prepare_rerun_endpoint(monkeypatch)
        executed: list[str] = []
        runner = runner_module.get_report_runner()

        async def spy_execute_unit(unit_arg, state):
            executed.append(str(unit_arg.unit_id))
            return SimpleNamespace(status="success")

        monkeypatch.setattr(runner, "_default_execute_unit", spy_execute_unit)
        monkeypatch.setattr(
            tenant_config,
            "resolve_tenant_llm_selection",
            AsyncMock(side_effect=RuntimeError("pg pool unavailable")),
        )

        with pytest.raises(HTTPException) as excinfo:
            await route.rerun_report_unit(job_id, unit.unit_id)

        # 把基础设施异常降级成"全源检索的成功"正是本次实机事故的形态，必须报错而不是报成功。
        assert excinfo.value.status_code == 503
        assert executed == []
        runs = await store.list_runs(job_id, identity, limit=1)
        assert await store.list_unit_snapshots(runs[0].id, identity) == []
        assert tenant_config.get_current_tenant_llm_selection() is None
