"""覆盖功能 31.1/31.2：统一只读工具循环中的结构化探查挂载、异构参数校验、独立额度与预算。

GateGuard 事实（写入前确认）：
1) 调用方：pytest 收集本文件；本文件从 tests/test_llm/test_knowledge_loop.py 复用
   _FakeToolModel/_bind_loop/_prepared/_tool_round_chunks/_evidence/_identity，不改其内容。
2) 无既有同用途文件：Glob tests/**/*data_probe* 无结果；tests/test_llm/ 下现有
   test_knowledge_loop*.py 与 test_public_answer_stream.py 均只覆盖知识检索循环，不含探查工具。
3) 本文件不读写数据文件，只构造内存中的 Fake LLM 与离线结构化工具（合成值如 "一号车间"）。
4) 用户当前指令节选（逐字）："你的写集严格限 src/llm/knowledge_loop.py、
   tests/test_llm/test_data_probe_loop.py（新增）、必要最少既有 knowledge_loop 测试修正、
   features/31-sql-probe-loop.md"；"TDD 用真实 StructuredTool/ProbeRequest 或严格兼容假工具"。

离线替身严格镜像 spec/55 §14 的 ProbeRequest 契约（operation + 五原语字段 + extra=forbid），
真实探查工具的端到端挂载由 TestRealProbeToolIntegration 在依赖生成后验证。
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from tests.test_llm.test_knowledge_loop import (
    _bind_loop,
    _evidence,
    _FakeToolModel,
    _identity,
    _prepared,
    _tool_round_chunks,
)

PROBE_TOOL_NAME = "probe_data"
KNOWLEDGE_TOOL_NAME = "search_knowledge"

# value_domain 的原语入参示例：只填固定字段，不携带自由 SQL。
_PROBE_VALUE_DOMAIN_ARGS = {
    "operation": "value_domain",
    "table": "t_workshop",
    "column": "workshop_name",
}


class _OfflineProbeRequest(BaseModel):
    """离线严格替身：镜像 spec/55 §14 ProbeRequest 的字段与拒绝策略，不接收自由 SQL。"""

    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "entity_lookup", "value_domain", "column_repeat", "time_range", "table_sample",
    ]
    table: str = Field(min_length=1, max_length=128)
    column: str = Field(default="", max_length=128)
    pattern: str = Field(default="", max_length=256)
    columns: list[str] = Field(default_factory=list)
    limit: int = Field(default=50, ge=1, le=200)


# 方法作用：构造与 spec/55 §7 返回契约一致的探查结果字典。
# Args: overrides - 需要覆盖的字段。
# Returns: 含 success/columns/rows/row_count/truncated/scope/error/error_code 的结果。
def _probe_result(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": True,
        "columns": ["workshop_name", "row_count"],
        "rows": [["一号车间", 15], ["二号车间", 15]],
        "row_count": 2,
        "truncated": False,
        "scope": "sample",
        "error": None,
        "error_code": None,
    }
    payload.update(overrides)
    return payload


# 方法作用：向 sys.modules 注入严格兼容 data_probe 契约的离线假模块。
# Args: monkeypatch - pytest 补丁工具；gate - 可选挂载判定函数（替换默认 sql.generate 契约）；
#       result_fn - 可选结果工厂，抛异常可模拟工具故障。
# Returns: 含真实 StructuredTool、执行记录与构建函数的命名空间。
def _install_fake_probe_module(
    monkeypatch,
    *,
    gate: Any = None,
    result_fn: Any = None,
) -> SimpleNamespace:
    executions: list[dict[str, Any]] = []

    # 方法作用：离线探查工具入口，按 args_schema 校验后记录参数并返回结构化结果。
    # Args: kwargs - 经 _OfflineProbeRequest 校验后的原语参数。
    # Returns: 探查结果字典。
    async def _probe(**kwargs: Any) -> dict[str, Any]:
        executions.append(dict(kwargs))
        return result_fn(**kwargs) if result_fn is not None else _probe_result()

    tool = StructuredTool.from_function(
        coroutine=_probe,
        name=PROBE_TOOL_NAME,
        description="结构化探查：只接受固定原语与授权表列，不接受自由 SQL。",
        args_schema=_OfflineProbeRequest,
    )

    # 方法作用：镜像主代理确认的挂载契约：仅已绑定上下文的 sql.generate 返回探查工具。
    # Args: prepared - 调用快照。
    # Returns: StructuredTool 列表或空列表。
    def build_data_probe_tools(prepared: Any = None) -> list[Any]:
        if gate is not None:
            return [tool] if gate(prepared) else []
        prompt_id = str(getattr(prepared, "prompt_id", "") or "")
        task = str(getattr(prepared, "task", "") or "")
        return [tool] if prompt_id == "sql.generate" or task == "generate_sql" else []

    monkeypatch.setitem(
        sys.modules,
        "src.tools.data_probe",
        SimpleNamespace(build_data_probe_tools=build_data_probe_tools),
    )
    return SimpleNamespace(tool=tool, executions=executions, build=build_data_probe_tools)


# 方法作用：构造集成用例的离线探查环境：临时 SQLite 小样本 + 授权/配置替身。
# Args: monkeypatch - pytest 补丁工具。
# Returns: (真实 ProfilingContext, engine)；调用方负责 dispose engine。
async def _build_offline_probe_context(monkeypatch) -> tuple[Any, Any]:
    """只用内存 SQLite 小样本，不访问任何业务数据库；身份须与循环绑定一致。"""
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    import src.datasource.profiling as profiling_module
    import src.tools.data_probe as data_probe_module
    from src.connectors.sqlite import SQLiteConnector
    from src.datasource.config import DataSourceConfig
    from src.datasource.profiling import ProfilingContext
    from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=sa.pool.StaticPool)
    async with engine.begin() as connection:
        await connection.execute(sa.text(
            "CREATE TABLE t_workshop (id INTEGER PRIMARY KEY, workshop_name TEXT NOT NULL, region TEXT)"
        ))
        await connection.execute(sa.text(
            "INSERT INTO t_workshop (id, workshop_name, region) VALUES "
            "(1,'冲压车间','华东'),(2,'冲压车间','华东'),(3,'焊接车间','华北')"
        ))
    tables = {
        "t_workshop": TableSchema(name="t_workshop", columns=[
            ColumnInfo("id", "INTEGER", is_primary_key=True),
            ColumnInfo("workshop_name", "TEXT"),
            ColumnInfo("region", "TEXT"),
        ], row_count_estimate=3),
    }
    datasource = DataSourceConfig(
        name="probe-int-sqlite", dialect="sqlite", mode="embedded",
        database=":memory:", engine=engine,
    )
    datasource.connector = SQLiteConnector(datasource).attach_engine(engine)
    datasource.schema = SchemaSnapshot(tables=list(tables.values()))
    registry = SimpleNamespace(
        resolve=AsyncMock(return_value=datasource),
        list_all=AsyncMock(return_value=[{"name": datasource.name, "dialect": datasource.dialect}]),
    )
    monkeypatch.setattr("src.datasource.registry.get_registry", lambda: registry)
    settings = SimpleNamespace(sql_profiling_enabled=True, sql_probe_limit=50)
    monkeypatch.setattr(profiling_module, "get_settings", lambda: settings)
    monkeypatch.setattr(data_probe_module, "get_settings", lambda: settings)
    context = ProfilingContext(
        tenant_id=2,
        user_id=7,
        role="analyst",
        datasource=datasource.name,
        dialect="sqlite",
        tables=tables,
    )
    return context, engine


# 方法作用：为真实探查回合绑定执行期真实认证上下文（生产身份复核读取该通道）。
# Args: user_id - 真实认证用户 ID，默认与画像上下文一致；负例传不同值制造漂移。
# Returns: 上下文管理器，退出时按既有 Token 精确复原，不泄漏身份到后续协程。
def _bind_probe_real_identity(user_id: int = 7) -> Any:
    """画像与探查服务执行前复核 src.api.auth 的 ContextVar，夹具必须绑定同一身份。"""
    from src.api.auth import use_identity_context
    from src.security.tenant_policy import RequestIdentity

    return use_identity_context(
        RequestIdentity(tenant_id=2, user_id=user_id, role="analyst"),
    )


# 方法作用：提取某次模型调用实际绑定的工具名列表。
# Args: call - _FakeToolModel 记录的调用字典。
# Returns: 工具名列表。
def _bound_tool_names(call: dict[str, Any]) -> list[str]:
    return [str(getattr(tool, "name", "")) for tool in call.get("tools", [])]


# 方法作用：读取某次模型调用消息序列中的工具回复，按 tool_call_id 建索引。
# Args: call - _FakeToolModel 记录的调用字典。
# Returns: tool_call_id 到 ToolMessage 的映射。
def _tool_replies(call: dict[str, Any]) -> dict[str, Any]:
    return {
        str(getattr(message, "tool_call_id", "")): message
        for message in call.get("messages", [])
        if getattr(message, "type", "") == "tool"
    }


class TestProbeLoopInvoke:
    """覆盖功能 31.1 的非流式异构校验与挂载边界。"""

    # 方法作用：probe_data 必须按自身 schema 校验并执行，不能被知识参数契约误杀。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_probe_call_uses_own_schema_and_closes_tool_message(self, monkeypatch) -> None:
        """模型只填原语参数时，探查工具必须真实执行而非返回 invalid_arguments。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        probe = _install_fake_probe_module(monkeypatch)
        model = _FakeToolModel([
            AIMessage(content="", tool_calls=[{
                "name": PROBE_TOOL_NAME, "args": dict(_PROBE_VALUE_DOMAIN_ARGS), "id": "probe-1",
            }]),
            AIMessage(content="SELECT workshop_name FROM t_workshop"),
        ])

        # Act
        with _bind_loop(monkeypatch):
            response = await loop_module.invoke_with_knowledge(_prepared(model))

        # Assert
        assert response.content == "SELECT workshop_name FROM t_workshop"
        assert len(model.calls) == 2
        assert {KNOWLEDGE_TOOL_NAME, PROBE_TOOL_NAME} <= set(_bound_tool_names(model.calls[0]))
        assert probe.executions == [{
            "operation": "value_domain",
            "table": "t_workshop",
            "column": "workshop_name",
            "pattern": "",
            "columns": [],
            "limit": 50,
        }]
        replies = _tool_replies(model.calls[1])
        assert set(replies) == {"probe-1"}
        payload = json.loads(replies["probe-1"].content)
        assert payload["success"] is True
        assert payload["row_count"] == 2
        assert replies["probe-1"].name == PROBE_TOOL_NAME

    # 方法作用：关闭知识检索不能阻断已绑定的探查能力。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_knowledge_disabled_still_allows_probe(self, monkeypatch) -> None:
        """知识禁用时绑定列表只剩探查工具，探查调用仍可执行。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        probe = _install_fake_probe_module(monkeypatch)
        model = _FakeToolModel([
            AIMessage(content="", tool_calls=[{
                "name": PROBE_TOOL_NAME, "args": dict(_PROBE_VALUE_DOMAIN_ARGS), "id": "probe-disabled-1",
            }]),
            AIMessage(content="完成"),
        ])

        # Act
        with _bind_loop(monkeypatch, enabled=False):
            await loop_module.invoke_with_knowledge(_prepared(model))

        # Assert
        assert _bound_tool_names(model.calls[0]) == [PROBE_TOOL_NAME]
        assert len(probe.executions) == 1

    # 方法作用：普通分析任务不得暴露探查工具。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_probe_not_exposed_for_non_sql_generate(self, monkeypatch) -> None:
        """analysis 任务即使有探查模块也不得挂载 probe_data，其调用按未知工具闭合。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        probe = _install_fake_probe_module(monkeypatch)
        model = _FakeToolModel([
            AIMessage(content="", tool_calls=[{
                "name": PROBE_TOOL_NAME, "args": dict(_PROBE_VALUE_DOMAIN_ARGS), "id": "analysis-probe",
            }]),
            AIMessage(content="无探查结果可用"),
        ])
        prepared = _prepared(model)
        prepared.task = "analyze_result"
        prepared.prompt_id = "analysis.result"

        # Act
        with _bind_loop(monkeypatch):
            response = await loop_module.invoke_with_knowledge(prepared)

        # Assert
        assert response.content == "无探查结果可用"
        assert PROBE_TOOL_NAME not in _bound_tool_names(model.calls[0])
        assert KNOWLEDGE_TOOL_NAME in _bound_tool_names(model.calls[0])
        assert probe.executions == []
        payload = json.loads(_tool_replies(model.calls[1])["analysis-probe"].content)
        assert payload["reason"] == "unknown_tool"

    # 方法作用：探查参数必须拒绝自由 SQL 与额外字段，且不执行任何查询。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_probe_arguments_reject_free_sql_and_extra_fields(self, monkeypatch) -> None:
        """sql 字段、未知原语、缺失 operation 与类型错误都必须以 invalid_arguments 闭合。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        probe = _install_fake_probe_module(monkeypatch)
        model = _FakeToolModel([
            AIMessage(content="", tool_calls=[
                {
                    "name": PROBE_TOOL_NAME,
                    "args": {**_PROBE_VALUE_DOMAIN_ARGS, "sql": "SELECT * FROM t_workshop"},
                    "id": "probe-extra",
                },
                {
                    "name": PROBE_TOOL_NAME,
                    "args": {"operation": "raw_select", "table": "t_workshop"},
                    "id": "probe-op",
                },
                {
                    "name": PROBE_TOOL_NAME,
                    "args": {"table": "t_workshop"},
                    "id": "probe-missing-op",
                },
                {
                    "name": PROBE_TOOL_NAME,
                    "args": {"operation": "table_sample", "table": "t_workshop", "limit": "many"},
                    "id": "probe-limit-type",
                },
            ]),
            AIMessage(content="参数非法"),
        ])

        # Act
        with _bind_loop(monkeypatch):
            await loop_module.invoke_with_knowledge(_prepared(model))

        # Assert
        replies = _tool_replies(model.calls[1])
        assert set(replies) == {"probe-extra", "probe-op", "probe-missing-op", "probe-limit-type"}
        for reply in replies.values():
            assert "invalid_arguments" in str(reply.content)
        assert probe.executions == []

    # 方法作用：供应商解析为 invalid_tool_calls 的损坏探查参数同样获得匹配回复。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_provider_invalid_probe_call_gets_matching_reply(self, monkeypatch) -> None:
        """损坏 JSON 绝不按猜测值执行探查，同时不留下孤立 tool_call。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        probe = _install_fake_probe_module(monkeypatch)
        malformed = AIMessage(
            content="",
            invalid_tool_calls=[{
                "name": PROBE_TOOL_NAME,
                "args": '{"operation":"value_domain","table":',
                "id": "probe-bad-json",
                "error": "synthetic invalid JSON",
            }],
        )
        model = _FakeToolModel([malformed, AIMessage(content="继续生成")])

        # Act
        with _bind_loop(monkeypatch):
            response = await loop_module.invoke_with_knowledge(_prepared(model))

        # Assert
        assert response.content == "继续生成"
        assert probe.executions == []
        replies = _tool_replies(model.calls[1])
        assert set(replies) == {"probe-bad-json"}
        assert "invalid_arguments" in str(replies["probe-bad-json"].content)

    # 方法作用：同一回合混合探查、知识、未知工具与非法参数时必须一一闭合。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_mixed_round_closes_every_tool_call(self, monkeypatch) -> None:
        """四个调用按顺序获得四条匹配 ToolMessage，合法调用只执行各自工具。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        probe = _install_fake_probe_module(monkeypatch)
        search = AsyncMock(return_value=[_evidence()])
        model = _FakeToolModel([
            AIMessage(content="", tool_calls=[
                {"name": PROBE_TOOL_NAME, "args": {"operation": "column_repeat", "table": "t_workshop", "column": "workshop_name"}, "id": "mix-probe"},
                {"name": "mystery_tool", "args": {}, "id": "mix-unknown"},
                {"name": KNOWLEDGE_TOOL_NAME, "args": {"query": "车间口径"}, "id": "mix-knowledge"},
                {"name": PROBE_TOOL_NAME, "args": {**_PROBE_VALUE_DOMAIN_ARGS, "sql": "SELECT 1"}, "id": "mix-bad"},
            ]),
            AIMessage(content="完成"),
        ])

        # Act
        with _bind_loop(monkeypatch, search=search):
            await loop_module.invoke_with_knowledge(_prepared(model))

        # Assert
        replies = _tool_replies(model.calls[1])
        assert list(replies) == ["mix-probe", "mix-unknown", "mix-knowledge", "mix-bad"]
        assert "success" in str(replies["mix-probe"].content)
        assert "unknown_tool" in str(replies["mix-unknown"].content)
        assert '"status": "ok"' in str(replies["mix-knowledge"].content)
        assert "invalid_arguments" in str(replies["mix-bad"].content)
        assert len(probe.executions) == 1
        assert search.await_count == 1

    # 方法作用：探查工具故障必须返回脱敏结构化结果并继续收敛到最终回答。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_probe_failure_returns_structured_unavailable(self, monkeypatch) -> None:
        """工具执行异常不向上抛出，ToolMessage 闭合后循环继续。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        # 方法作用：模拟探查工具执行期故障。
        # Args: kwargs - 已校验原语参数。
        # Returns: 永不返回，抛出运行时异常。
        def _boom(**kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("synthetic probe failure")

        probe = _install_fake_probe_module(monkeypatch, result_fn=_boom)
        model = _FakeToolModel([
            AIMessage(content="", tool_calls=[{
                "name": PROBE_TOOL_NAME, "args": dict(_PROBE_VALUE_DOMAIN_ARGS), "id": "probe-fail",
            }]),
            AIMessage(content="探查不可用，按既有上下文作答"),
        ])

        # Act
        with _bind_loop(monkeypatch):
            response = await loop_module.invoke_with_knowledge(_prepared(model))

        # Assert
        assert response.content == "探查不可用，按既有上下文作答"
        assert len(probe.executions) == 1
        payload = json.loads(_tool_replies(model.calls[1])["probe-fail"].content)
        assert payload["status"] == "unavailable"
        assert "synthetic probe failure" not in str(_tool_replies(model.calls[1])["probe-fail"].content)

    # 方法作用：取消必须在探查执行期原样向上传播。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_cancelled_error_from_probe_propagates(self, monkeypatch) -> None:
        """CancelledError 不能被通用异常处理转成 unavailable。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        # 方法作用：模拟探查执行期被取消。
        # Args: kwargs - 已校验原语参数。
        # Returns: 永不返回，抛出 CancelledError。
        def _cancel(**kwargs: Any) -> dict[str, Any]:
            raise asyncio.CancelledError()

        _install_fake_probe_module(monkeypatch, result_fn=_cancel)
        model = _FakeToolModel([
            AIMessage(content="", tool_calls=[{
                "name": PROBE_TOOL_NAME, "args": dict(_PROBE_VALUE_DOMAIN_ARGS), "id": "probe-cancel",
            }]),
        ])

        # Act + Assert
        with _bind_loop(monkeypatch):
            try:
                await loop_module.invoke_with_knowledge(_prepared(model))
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("CancelledError 必须向上传播")

    # 方法作用：模型不支持 bind_tools 时探查沿用既有收敛行为，不冒充失败。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_bind_tools_unsupported_falls_back_with_probe_only(self, monkeypatch) -> None:
        """仅探查工具可用时，不支持工具绑定的模型退回一次普通调用。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        probe = _install_fake_probe_module(monkeypatch)
        model = _FakeToolModel([AIMessage(content="直接回答")], supports_tools=False)

        # Act
        with _bind_loop(monkeypatch, enabled=False):
            response = await loop_module.invoke_with_knowledge(_prepared(model))

        # Assert
        assert response.content == "直接回答"
        assert len(model.calls) == 1
        assert model.calls[0]["tools"] == []
        assert probe.executions == []


class TestProbeLoopStream:
    """覆盖功能 31.2 的流式回合收敛、公开流隔离与取消传播。"""

    # 方法作用：探查回合的中间内容与结果不得作为公开增量输出。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_stream_probe_round_resets_and_hides_payload(self, monkeypatch) -> None:
        """探查回合产出重置标记，正文与结果都不进公开流，最终分片照常释放。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        probe = _install_fake_probe_module(monkeypatch)
        model = _FakeToolModel([
            lambda: _tool_round_chunks(
                PROBE_TOOL_NAME,
                "probe-stream-1",
                ['{"operation":"value_domain",', '"table":"t_workshop","column":"workshop_name"}'],
            ),
            lambda: [AIMessageChunk(content="SELECT "), AIMessageChunk(content="1")],
        ])

        # Act
        yielded = []
        with _bind_loop(monkeypatch):
            async for chunk in loop_module.stream_with_knowledge(_prepared(model)):
                yielded.append(chunk)

        # Assert
        resets = [
            index for index, item in enumerate(yielded)
            if isinstance(item, loop_module.KnowledgeRoundReset)
        ]
        assert len(resets) == 1
        visible = "".join(str(getattr(item, "content", "")) for item in yielded[resets[0] + 1:])
        assert visible == "SELECT 1"
        assert "一号车间" not in "".join(str(getattr(item, "content", "")) for item in yielded)
        # 分片合并后的原语参数必须完整下推到探查工具。
        assert probe.executions == [{
            "operation": "value_domain",
            "table": "t_workshop",
            "column": "workshop_name",
            "pattern": "",
            "columns": [],
            "limit": 50,
        }]
        replies = _tool_replies(model.calls[1])
        assert set(replies) == {"probe-stream-1"}
        assert json.loads(replies["probe-stream-1"].content)["success"] is True

    # 方法作用：探查预算耗尽的结构化结果不中断循环，仍收敛到最终回答。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_stream_probe_budget_exhausted_still_finalizes(self, monkeypatch) -> None:
        """工具返回预算耗尽结果时按普通证据闭合，最终回合无工具并产出答案。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        # 方法作用：模拟探查次数耗尽后的结构化结果。
        # Args: kwargs - 已校验原语参数。
        # Returns: 预算耗尽的探查结果。
        def _exhausted(**kwargs: Any) -> dict[str, Any]:
            return _probe_result(
                success=False, rows=[], row_count=0,
                error="探查调用预算已耗尽", error_code="budget_exhausted",
            )

        _install_fake_probe_module(monkeypatch, result_fn=_exhausted)
        model = _FakeToolModel([
            lambda: _tool_round_chunks(
                PROBE_TOOL_NAME, "probe-exhausted-1", ['{"operation":"time_range","table":"t_workshop","column":"created_at"}'],
            ),
            lambda: [AIMessageChunk(content="最终回答")],
        ])

        # Act
        yielded = []
        with _bind_loop(monkeypatch):
            async for chunk in loop_module.stream_with_knowledge(_prepared(model)):
                yielded.append(chunk)

        # Assert
        replies = _tool_replies(model.calls[1])
        payload = json.loads(replies["probe-exhausted-1"].content)
        assert payload["error_code"] == "budget_exhausted"
        # 预算耗尽结果按普通证据闭合后，循环在下一回合正常收敛出最终回答。
        assert len(model.calls) == 2
        assert len([item for item in yielded if isinstance(item, loop_module.KnowledgeRoundReset)]) == 1
        assert getattr(yielded[-1], "content", "") == "最终回答"

    # 方法作用：流式探查回合必须保留供应商续轮协议字段。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_stream_assistant_conversion_keeps_reasoning_content(self, monkeypatch) -> None:
        """合并分片转 AIMessage 时 reasoning_content 与探查调用 ID 都不能丢。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        probe = _install_fake_probe_module(monkeypatch)
        model = _FakeToolModel([
            lambda: [AIMessageChunk(
                content="",
                additional_kwargs={"reasoning_content": "synthetic-protocol-marker"},
                tool_call_chunks=[{
                    "name": PROBE_TOOL_NAME,
                    "args": json.dumps(_PROBE_VALUE_DOMAIN_ARGS),
                    "id": "probe-reasoning-1",
                    "index": 0,
                }],
            )],
            lambda: [AIMessageChunk(content="SELECT 1")],
        ])

        # Act
        with _bind_loop(monkeypatch):
            _ = [chunk async for chunk in loop_module.stream_with_knowledge(_prepared(model))]

        # Assert
        assistant = [
            message for message in model.calls[1]["messages"]
            if getattr(message, "type", "") == "ai" and getattr(message, "tool_calls", None)
        ][-1]
        assert assistant.additional_kwargs.get("reasoning_content") == "synthetic-protocol-marker"
        assert assistant.tool_calls[0]["id"] == "probe-reasoning-1"
        assert probe.executions
        assert set(_tool_replies(model.calls[1])) == {"probe-reasoning-1"}

    # 方法作用：流式探查执行期的取消同样必须传播。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_stream_cancelled_error_from_probe_propagates(self, monkeypatch) -> None:
        """流式回合里探查被取消时不得被吞掉。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        # 方法作用：模拟探查执行期被取消。
        # Args: kwargs - 已校验原语参数。
        # Returns: 永不返回，抛出 CancelledError。
        def _cancel(**kwargs: Any) -> dict[str, Any]:
            raise asyncio.CancelledError()

        _install_fake_probe_module(monkeypatch, result_fn=_cancel)
        model = _FakeToolModel([
            lambda: _tool_round_chunks(
                PROBE_TOOL_NAME, "probe-stream-cancel", ['{"operation":"value_domain","table":"t_workshop","column":"workshop_name"}'],
            ),
        ])

        # Act + Assert
        with _bind_loop(monkeypatch):
            try:
                async for _ in loop_module.stream_with_knowledge(_prepared(model)):
                    pass
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("CancelledError 必须向上传播")


class TestProbeLoopQuota:
    """覆盖功能 31.1 的独立额度：探查调用不消耗知识检索配额。"""

    # 方法作用：探查调用不得占用 max_retrievals，知识调用在后续回合仍可执行。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_probe_call_does_not_consume_knowledge_quota(self, monkeypatch) -> None:
        """max_retrievals=1 时，第一回合的探查不能让第二回合的知识检索被提前截断。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        probe = _install_fake_probe_module(monkeypatch, gate=lambda prepared: True)
        search = AsyncMock(return_value=[_evidence()])
        model = _FakeToolModel([
            AIMessage(content="", tool_calls=[{
                "name": PROBE_TOOL_NAME, "args": dict(_PROBE_VALUE_DOMAIN_ARGS), "id": "quota-probe",
            }]),
            AIMessage(content="", tool_calls=[{
                "name": KNOWLEDGE_TOOL_NAME, "args": {"query": "车间口径"}, "id": "quota-knowledge",
            }]),
            lambda: [AIMessageChunk(content="最终回答")],
        ])
        prepared = _prepared(model)
        prepared.task = "analyze_result"
        prepared.prompt_id = "analysis.result"

        # 该用例以假模块放行探查，专门验证循环层计数不把探查算进知识额度。
        # Act
        with _bind_loop(monkeypatch, search=search, max_retrievals=1), loop_module.use_public_answer_stream():
            response = await loop_module.invoke_with_knowledge(prepared)

        # Assert
        assert response.content == "最终回答"
        assert len(probe.executions) == 1
        assert search.await_count == 1
        assert len(model.calls) == 3

    # 方法作用：知识检索配额本身仍按既有语义终止探索回合。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_knowledge_quota_still_converges_exploration(self, monkeypatch) -> None:
        """max_retrievals=1 且首回合命中知识工具时，探索不进入第二回合。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        probe = _install_fake_probe_module(monkeypatch)
        search = AsyncMock(return_value=[_evidence()])
        model = _FakeToolModel([
            AIMessage(content="", tool_calls=[{
                "name": KNOWLEDGE_TOOL_NAME, "args": {"query": "车间口径"}, "id": "quota-knowledge-only",
            }]),
            lambda: [AIMessageChunk(content="最终回答")],
        ])
        prepared = _prepared(model)
        prepared.task = "analyze_result"
        prepared.prompt_id = "analysis.result"

        # Act
        with _bind_loop(monkeypatch, search=search, max_retrievals=1), loop_module.use_public_answer_stream():
            response = await loop_module.invoke_with_knowledge(prepared)

        # Assert
        assert response.content == "最终回答"
        assert search.await_count == 1
        assert probe.executions == []
        assert len(model.calls) == 2


class TestProbeBudgetReserve:
    """覆盖功能 31.2 的预算：真实 schema 字符数与动态 Prompt 预留一致。"""

    # 方法作用：探查上下文已绑定时预留真实 schema 与一条有界结果。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    def test_reserve_includes_real_probe_schema_and_bounded_result(self, monkeypatch) -> None:
        """知识禁用但探查可用时，预留等于探查 schema + 一条结果 + 协议开销。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        probe = _install_fake_probe_module(monkeypatch)
        monkeypatch.setattr(loop_module, "_SCHEMA_RESERVE_CACHE", None)
        schema_chars = loop_module._tool_schema_chars([probe.tool])

        # Act
        with _bind_loop(monkeypatch, enabled=False):
            reserve = loop_module.knowledge_tool_reserve_chars(85000)

        # Assert
        assert reserve == (
            schema_chars
            + loop_module.TOOL_RESULT_RESERVE_CHARS
            + loop_module.TOOL_PROTOCOL_RESERVE_CHARS
        )
        # 探查 schema 属身份作用域测量，不得进入全局知识 schema 缓存。
        assert loop_module._SCHEMA_RESERVE_CACHE is None

    # 方法作用：未绑定探查上下文时知识禁用不产生任何预留。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    def test_reserve_is_zero_without_probe_context(self, monkeypatch) -> None:
        """无上下文时假模块返回空列表，预留为 0。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        _install_fake_probe_module(monkeypatch, gate=lambda prepared: False)

        # Act
        with _bind_loop(monkeypatch, enabled=False):
            reserve = loop_module.knowledge_tool_reserve_chars(85000)

        # Assert
        assert reserve == 0

    # 方法作用：知识工具启用的预留口径保持既有契约。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    def test_reserve_for_knowledge_only_is_unchanged(self, monkeypatch) -> None:
        """无探查上下文时预留仍等于知识 schema + 一条结果 + 协议开销。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module
        from src.tools.knowledge_search import build_knowledge_tools

        _install_fake_probe_module(monkeypatch, gate=lambda prepared: False)
        monkeypatch.setattr(loop_module, "_SCHEMA_RESERVE_CACHE", None)
        knowledge_schema = loop_module._tool_schema_chars(build_knowledge_tools(
            identity=_identity(),
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        ))

        # Act
        with _bind_loop(monkeypatch):
            reserve = loop_module.knowledge_tool_reserve_chars(85000)

        # Assert
        assert reserve == (
            knowledge_schema
            + loop_module.TOOL_RESULT_RESERVE_CHARS
            + loop_module.TOOL_PROTOCOL_RESERVE_CHARS
        )

    # 方法作用：循环首次调用前的门槛与预留口径一致，探查工具不会被预算误杀。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_message_budget_boundary_matches_real_schema(self, monkeypatch) -> None:
        """预算恰好容纳 schema + 一条结果 + 协议时挂载工具，少 1 字符即整体跳过。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        probe = _install_fake_probe_module(monkeypatch)
        probe_schema = loop_module._tool_schema_chars([probe.tool])
        model = _FakeToolModel([AIMessage(content="完成"), AIMessage(content="完成")])
        prepared = _prepared(model)
        base_chars = loop_module._message_chars(prepared.messages, probe_schema)
        exact_budget = (
            base_chars + loop_module.TOOL_RESULT_RESERVE_CHARS + loop_module.TOOL_PROTOCOL_RESERVE_CHARS
        )

        # Act：恰好预算与差 1 字符两种边界。
        with _bind_loop(monkeypatch, enabled=False, message_budget_chars=exact_budget):
            await loop_module.invoke_with_knowledge(prepared)
        with _bind_loop(monkeypatch, enabled=False, message_budget_chars=exact_budget - 1):
            await loop_module.invoke_with_knowledge(prepared)

        # Assert
        assert _bound_tool_names(model.calls[0]) == [PROBE_TOOL_NAME]
        assert model.calls[1]["tools"] == []


    # 方法作用：真实探查模块落地后，预留按真实 probe_data schema 与有界结果计算。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_reserve_measures_real_probe_schema_when_context_bound(self, monkeypatch) -> None:
        """sentinel 测量必须与真实 build_data_probe_tools 的序列化结果一致。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module
        from src.tools.data_probe import build_data_probe_tools, use_data_probe_context

        context, engine = await _build_offline_probe_context(monkeypatch)
        try:
            prepared = _prepared(_FakeToolModel([]))
            with use_data_probe_context(context):
                probe_schema = loop_module._tool_schema_chars(build_data_probe_tools(prepared))

                # Act
                measured = loop_module._probe_schema_reserve_chars()
                with _bind_loop(monkeypatch, enabled=False):
                    reserve = loop_module.knowledge_tool_reserve_chars(85000)

            # Assert
            assert measured == probe_schema
            assert reserve == (
                probe_schema
                + loop_module.TOOL_RESULT_RESERVE_CHARS
                + loop_module.TOOL_PROTOCOL_RESERVE_CHARS
            )
            assert loop_module._SCHEMA_RESERVE_CACHE is None
        finally:
            await engine.dispose()


class TestRealProbeToolIntegration:
    """功能 31.2 集成：真实探查工具经统一循环完成一次探查并返回最终回答。

    依赖 src/tools/data_probe.py 与 src/datasource/profiling.py 生成后启用；
    依赖未就绪时本类用例失败（旧失败回归），不允许用临时 stub 伪造真实接线。
    """

    # 方法作用：真实模块按挂载契约提供探查工具，并完成一次真实工具回合。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_real_probe_tool_round_trip(self, monkeypatch) -> None:
        """Fake LLM 发出 probe_data → 真实工具在已授权小样本上执行 → 最终响应返回。"""
        # Arrange：依赖未生成时此处直接失败，等待工具接口落地。
        import src.llm.knowledge_loop as loop_module
        from src.api.auth import get_current_identity
        from src.tools.data_probe import build_data_probe_tools, use_data_probe_context

        context, engine = await _build_offline_probe_context(monkeypatch)
        try:
            # 未绑定上下文时不得返回任何探查工具；非 sql.generate 同样不放行。
            unbound = _prepared(_FakeToolModel([]))
            assert build_data_probe_tools(unbound) == []
            with use_data_probe_context(context):
                assert build_data_probe_tools(unbound)
                assert build_data_probe_tools(SimpleNamespace(prompt_id="analysis.result")) == []

            model = _FakeToolModel([
                AIMessage(content="", tool_calls=[
                    {"name": PROBE_TOOL_NAME, "args": dict(_PROBE_VALUE_DOMAIN_ARGS), "id": "real-probe-1"},
                    {
                        "name": PROBE_TOOL_NAME,
                        "args": {**_PROBE_VALUE_DOMAIN_ARGS, "sql": "DROP TABLE t_workshop"},
                        "id": "real-probe-free-sql",
                    },
                ]),
                AIMessage(content="SELECT workshop_name, COUNT(*) FROM t_workshop GROUP BY workshop_name"),
            ])
            prepared = _prepared(model)

            # Act：真实认证身份与已授权小样本上下文一并绑定后走统一循环入口，
            # 知识禁用不影响探查；生产身份复核读取的就是 src.api.auth 通道。
            before_identity = get_current_identity()
            with (
                _bind_probe_real_identity(),
                use_data_probe_context(context),
                _bind_loop(monkeypatch, enabled=False),
            ):
                response = await loop_module.invoke_with_knowledge(prepared)

            # Assert：退出后真实认证上下文必须精确复原，身份不得泄漏给后续协程。
            assert get_current_identity() == before_identity
            assert response.content == "SELECT workshop_name, COUNT(*) FROM t_workshop GROUP BY workshop_name"
            replies = _tool_replies(model.calls[1])
            assert set(replies) == {"real-probe-1", "real-probe-free-sql"}
            payload = json.loads(replies["real-probe-1"].content)
            assert payload["success"] is True
            assert payload["rows"][0] == {"value": "冲压车间", "count": 2}
            assert replies["real-probe-1"].name == PROBE_TOOL_NAME
            # 自由 SQL 字段被真实 ProbeRequest 拒绝，探查结果不进入公开正文。
            rejected = json.loads(replies["real-probe-free-sql"].content)
            assert "invalid_arguments" in json.dumps(rejected, ensure_ascii=False)
        finally:
            await engine.dispose()

    # 方法作用：真实认证身份缺失或漂移时，真实画像服务必须失败关闭而不是执行探查。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_real_probe_rejects_identity_mismatch(self, monkeypatch) -> None:
        """未绑定真实 auth 身份（匿名默认）与同租户身份漂移都必须被真实服务拒绝。"""
        # Arrange：真实模块与真实 SQLite 小样本；循环的 use_tool_identity 旁路
        # 只能服务知识/探查工具的自身校验，不能替代生产身份复核通道。
        import src.llm.knowledge_loop as loop_module
        from src.tools.data_probe import use_data_probe_context

        context, engine = await _build_offline_probe_context(monkeypatch)
        try:
            # 场景 A：未绑定真实认证身份——执行期读到匿名默认身份（user_id=0）。
            anonymous_model = _FakeToolModel([
                AIMessage(content="", tool_calls=[{
                    "name": PROBE_TOOL_NAME, "args": dict(_PROBE_VALUE_DOMAIN_ARGS),
                    "id": "real-probe-anonymous",
                }]),
                AIMessage(content="无探查结果，按既有上下文生成"),
            ])
            with use_data_probe_context(context), _bind_loop(monkeypatch, enabled=False):
                response = await loop_module.invoke_with_knowledge(_prepared(anonymous_model))
            assert response.content == "无探查结果，按既有上下文生成"
            anonymous_payload = json.loads(
                _tool_replies(anonymous_model.calls[1])["real-probe-anonymous"].content
            )
            assert anonymous_payload["success"] is False
            assert anonymous_payload["error_code"] == "forbidden"
            assert anonymous_payload["rows"] == []

            # 场景 B：真实认证身份漂移到同租户另一用户（8 ≠ 上下文 7）。
            drifted_model = _FakeToolModel([
                AIMessage(content="", tool_calls=[{
                    "name": PROBE_TOOL_NAME, "args": dict(_PROBE_VALUE_DOMAIN_ARGS),
                    "id": "real-probe-drifted",
                }]),
                AIMessage(content="无探查结果，按既有上下文生成"),
            ])
            with (
                _bind_probe_real_identity(user_id=8),
                use_data_probe_context(context),
                _bind_loop(monkeypatch, enabled=False),
            ):
                response = await loop_module.invoke_with_knowledge(_prepared(drifted_model))
            assert response.content == "无探查结果，按既有上下文生成"
            drifted_payload = json.loads(
                _tool_replies(drifted_model.calls[1])["real-probe-drifted"].content
            )
            assert drifted_payload["success"] is False
            assert drifted_payload["error_code"] == "forbidden"
            # 拒绝体不携带样本行，也不回显任何样本原值。
            assert drifted_payload["rows"] == []
            assert drifted_payload["columns"] == []
            assert "冲压车间" not in json.dumps(drifted_payload, ensure_ascii=False)
        finally:
            await engine.dispose()
