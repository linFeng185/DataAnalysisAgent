"""30.2/30.3 键列保护与画像证据注入的整合测试（spec/55 §6、§14）。

覆盖点：
1. 超过 32 列时授权键列仍被保留，未授权列名不能进入必保集合；
2. 启用时键列候选与画像证据进入生成 Prompt，探针作用域只围绕本次 prepare/stream；
3. 关闭标记/身份不符时零模型查询、零画像查询，原 Prompt 与 Schema 不变；
4. 多数据源时只使用当前数据源的权限快照，不混用第一源；显式未授权源安全拒绝、未指定时只接受唯一授权源；
5. 画像失败降级、取消透传；最多 6 表画像；
6. 两种聊天入口都写入服务端标记，报表入口默认关闭，请求正文不可覆写。
"""

from __future__ import annotations

import asyncio
import traceback
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.graph.nodes.generate_sql as generate_module
from src.api.auth import use_identity_context
from src.api.schemas import ChatRequest
from src.graph.nodes.generate_sql import _current_datasource_access, _prune_prompt_table_columns
from src.llm.schema_keys import SQL_INTELLIGENCE_CONFIG_KEY

_MARKER_CONFIG = {"configurable": {"thread_id": "t-1", SQL_INTELLIGENCE_CONFIG_KEY: True}}


# 方法作用：构造一张宽表，键列放在很靠后的位置以模拟 32 列额度挤压。
# Args: column_count - 列数；key_column - 需要保护的键列名；key_at - 键列插入位置。
# Returns: 与 relevant_tables 形状一致的表字典。
def _wide_table(column_count: int = 40, key_column: str = "workshop_code", key_at: int = 35) -> dict:
    names = [f"metric_{index:02d}" for index in range(column_count)]
    names.insert(key_at, key_column)
    return {
        "name": "t_workshop",
        "description": "车间表",
        "columns": [
            {
                "name": name,
                "type": "Int64",
                "comment": "",
                "is_nullable": True,
                "is_primary_key": name == "id",
                "is_indexed": name == "id",
            }
            for name in ["id", *names]
        ],
        "relations": [],
    }


# 方法作用：构造含键列/展示名/外键的最小表。
# Args: name - 表名。
# Returns: 表结构字典。
def _small_table(name: str = "t_workshop") -> dict:
    return {
        "name": name,
        "description": "",
        "columns": [
            {"name": "id", "type": "Int64", "comment": "", "is_primary_key": True, "is_indexed": True, "is_nullable": False, "sample": "样例值-禁止外传"},
            {"name": "workshop_code", "type": "String", "comment": "车间编码", "is_primary_key": False, "is_indexed": False, "is_nullable": False, "sample": "样例值-禁止外传"},
            {"name": "workshop_name", "type": "String", "comment": "车间名称", "is_primary_key": False, "is_indexed": False, "is_nullable": False},
            {"name": "factory_id", "type": "Int64", "comment": "所属工厂", "is_primary_key": False, "is_indexed": False, "is_nullable": True},
        ],
        "relations": [{"relation_type": "many_to_one", "target_table": "t_factory", "join_key": "factory_id"}],
    }


# 方法作用：构造节点状态（扁平兼容形态，由 read_contexts 归一）。
# Args: tables - relevant_tables；datasource_access - 授权快照；datasource - 当前数据源。
# Returns: 可直接传入 generate_sql_node 的状态字典。
def _state(
    tables: list[dict],
    *,
    datasource_access: dict[str, dict] | None = None,
    datasource: str = "ds_main",
) -> dict:
    return {
        "user_query": "各车间的产量分别是多少？",
        "relevant_tables": tables,
        "dialect": "mysql",
        "datasource": datasource,
        "datasource_access": datasource_access or {
            "ds_main": {
                "name": "ds_main",
                "allowed_columns": ["workshop_code", "workshop_name"],
                "row_filter_sql": "region = '华东'",
            },
        },
        "tenant_id": 1,
        "user_id": 7,
        "user_role": "analyst",
    }


# 方法作用：构造可记录的假画像 API（ProfilingContext/get_data_profiler/render_profile）。
# Args: profile_text - render_profile 的返回文本；profile_error - 画像调用抛出的异常。
# Returns: (假模块对象, 调用记录字典)。
def _fake_profiling_api(
    profile_text: str = "t_workshop（约 120 行）\n  - workshop_code: 唯一（120 个值）",
    profile_error: Exception | None = None,
):
    calls: dict = {"context_kwargs": None, "columns_by_table": None, "profile_calls": 0, "render_calls": 0}

    # 方法作用：记录上下文构造参数并返回可辨识的上下文替身。
    # Args: **kwargs - ProfilingContext 构造参数。
    # Returns: 上下文替身对象。
    def context_factory(**kwargs):
        calls["context_kwargs"] = kwargs
        return SimpleNamespace(**kwargs)

    # 方法作用：返回可记录的 DataProfiler 替身。
    # Args: 无。
    # Returns: 具备异步 profile 的替身。
    class _Profiler:
        # 方法作用：记录画像请求并返回快照替身。
        # Args: context - 已构造上下文；columns_by_table - 每表候选列。
        # Returns: 快照替身对象。
        async def profile(self, context, columns_by_table):
            calls["profile_calls"] += 1
            calls["columns_by_table"] = columns_by_table
            if profile_error is not None:
                raise profile_error
            return SimpleNamespace(tables={}, query_count=1, errors=[])

    # 方法作用：记录渲染调用并返回画像文本。
    # Args: snapshot - 快照替身。
    # Returns: 画像文本。
    def render_profile(snapshot):
        calls["render_calls"] += 1
        return profile_text

    api = SimpleNamespace(
        ProfilingContext=context_factory,
        get_data_profiler=lambda: _Profiler(),
        render_profile=render_profile,
    )
    return api, calls


# 方法作用：构造可记录的探针上下文管理器工厂。
# Args: recorder - 记录字典。
# Returns: 工厂函数，返回记录 enter/exit 的上下文管理器。
def _fake_probe_scope_factory(recorder: dict):
    # 方法作用：返回记录生命周期事件的上下文管理器。
    # Args: context - 探针上下文。
    # Returns: 上下文管理器对象。
    def factory(context):
        recorder["context"] = context
        recorder.setdefault("events", [])
        recorder["events"].append("enter")

        # 方法作用：记录退出事件的作用域对象。
        # Args: 无。
        # Returns: 无返回值。
        class _Scope:
            # 方法作用：记录进入。
            # Args: 无。
            # Returns: 自身。
            def __enter__(self_inner):
                return self_inner

            # 方法作用：记录退出，不吞异常。
            # Args: exc_type/exc/tb - 异常上下文。
            # Returns: False，异常继续传播。
            def __exit__(self_inner, exc_type, exc, tb):
                recorder["events"].append("exit")
                return False

        return _Scope()

    return factory


# 方法作用：构造记录 _llm_generate 入参的替身。
# Args: captured - 记录字典。
# Returns: 异步函数，返回固定 SQL。
def _llm_recorder(captured: dict):
    # 方法作用：记录节点传入的生成参数。
    # Args: **kwargs - _llm_generate 的关键字参数。
    # Returns: (sql, reasoning, explanation)。
    async def fake_llm(**kwargs):
        captured.update(kwargs)
        return "SELECT workshop_code, SUM(qty) FROM t_workshop GROUP BY workshop_code", "", "ok"

    return fake_llm


class TestKeyColumnProtection:
    """覆盖 30.2：超过 32 列时授权键列仍被保留。"""

    # 方法作用：>32 列的表里，_profile_key_columns 标记的键列不被配额挤掉。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_key_column_beyond_32_columns_survives(self) -> None:
        table = _wide_table()
        # 反证：无键列提示时，位置靠后的键列会被每表 32 列额度挤掉（42 列只留 32）。
        plain = _prune_prompt_table_columns(dict(table), "各车间产量")
        assert "workshop_code" not in {column["name"] for column in plain["columns"]}
        table["_profile_key_columns"] = ["workshop_code"]

        pruned = _prune_prompt_table_columns(table, "各车间产量")

        names = {column["name"] for column in pruned["columns"]}
        assert "workshop_code" in names

    # 方法作用：未授权列名写进 _profile_key_columns 也不能进入必保集合。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_unauthorized_key_names_cannot_be_protected(self) -> None:
        table = _wide_table()
        table["_profile_key_columns"] = ["not_authorized_column", "workshop_code"]

        pruned = _prune_prompt_table_columns(table, "各车间产量")

        names = {column["name"] for column in pruned["columns"]}
        assert "not_authorized_column" not in names


class TestIntelligenceEnabled:
    """覆盖 30.2：启用时键列候选与画像证据进入 Prompt。"""

    # 方法作用：键列候选与画像文本进入 data_profile，探针上下文随调用下发。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_profile_and_hints_reach_generate_prompt(self, monkeypatch) -> None:
        import src.llm.schema_keys as schema_keys_module

        schema_keys_module.clear_schema_key_cache()
        monkeypatch.setattr(schema_keys_module, "_is_task_llm_available", lambda task: False)
        api, calls = _fake_profiling_api()
        monkeypatch.setattr(generate_module, "_load_profiling_api", lambda: api)
        probe_recorder: dict = {}
        monkeypatch.setattr(
            generate_module, "_data_probe_context_scope",
            _fake_probe_scope_factory(probe_recorder),
        )
        captured: dict = {}
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(generate_module, "_llm_generate", _llm_recorder(captured))
        state = _state([_small_table()])

        with use_identity_context(SimpleNamespace(tenant_id=1, user_id=7, role="analyst")):
            await generate_module.generate_sql_node(state, _MARKER_CONFIG)

        data_profile = captured["data_profile"]
        # 键列候选（确定性回退）与画像文本都必须进入画像证据段。
        assert "workshop_code" in data_profile
        assert "workshop_name" in data_profile
        assert "唯一（120 个值）" in data_profile
        # 样例值只保留在原有 Schema 段；键列/画像段不得携带列样例。
        assert "样例值-禁止外传" not in data_profile
        # 探针上下文按本次调用下发。
        assert captured["probe_context"] is not None
        assert calls["profile_calls"] == 1
        assert calls["render_calls"] == 1
        # 画像候选列来自键列判断结果，且只含授权列。
        columns = calls["columns_by_table"]["t_workshop"]
        assert 0 < len(columns) <= 6
        assert set(columns) <= {"id", "workshop_code", "workshop_name", "factory_id"}

    # 方法作用：画像上下文使用当前数据源的权限快照，不混用第一源。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_current_datasource_permissions_used(self, monkeypatch) -> None:
        import src.llm.schema_keys as schema_keys_module

        schema_keys_module.clear_schema_key_cache()
        monkeypatch.setattr(schema_keys_module, "_is_task_llm_available", lambda task: False)
        api, calls = _fake_profiling_api()
        monkeypatch.setattr(generate_module, "_load_profiling_api", lambda: api)
        monkeypatch.setattr(generate_module, "_data_probe_context_scope", lambda context: nullcontext())
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(generate_module, "_llm_generate", _llm_recorder({}))
        state = _state(
            [_small_table()],
            datasource="ds_b",
            datasource_access={
                "ds_a": {"name": "ds_a", "allowed_columns": ["wrong_column"], "row_filter_sql": "a = 1"},
                "ds_b": {"name": "ds_b", "allowed_columns": ["workshop_code"], "row_filter_sql": "b = 2"},
            },
        )

        with use_identity_context(SimpleNamespace(tenant_id=1, user_id=7, role="analyst")):
            await generate_module.generate_sql_node(state, _MARKER_CONFIG)

        kwargs = calls["context_kwargs"]
        assert kwargs["datasource"] == "ds_b"
        assert kwargs["allowed_columns"] == ["workshop_code"]
        assert kwargs["row_filter_sql"] == "b = 2"

    # 方法作用：画像表数受上限约束（默认最多 6 表）。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_profile_table_cap(self, monkeypatch) -> None:
        import src.llm.schema_keys as schema_keys_module

        schema_keys_module.clear_schema_key_cache()
        monkeypatch.setattr(schema_keys_module, "_is_task_llm_available", lambda task: False)
        api, calls = _fake_profiling_api()
        monkeypatch.setattr(generate_module, "_load_profiling_api", lambda: api)
        monkeypatch.setattr(generate_module, "_data_probe_context_scope", lambda context: nullcontext())
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(generate_module, "_llm_generate", _llm_recorder({}))
        state = _state([_small_table(f"t_workshop_{index}") for index in range(8)])

        with use_identity_context(SimpleNamespace(tenant_id=1, user_id=7, role="analyst")):
            await generate_module.generate_sql_node(state, _MARKER_CONFIG)

        assert calls["columns_by_table"] is not None
        assert len(calls["columns_by_table"]) <= 6

    # 方法作用：画像失败时降级为无画像继续生成，不抛错。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_profile_failure_degrades(self, monkeypatch) -> None:
        import src.llm.schema_keys as schema_keys_module

        schema_keys_module.clear_schema_key_cache()
        monkeypatch.setattr(schema_keys_module, "_is_task_llm_available", lambda task: False)
        api, calls = _fake_profiling_api(profile_error=RuntimeError("db down"))
        monkeypatch.setattr(generate_module, "_load_profiling_api", lambda: api)
        monkeypatch.setattr(generate_module, "_data_probe_context_scope", lambda context: nullcontext())
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        captured: dict = {}
        monkeypatch.setattr(generate_module, "_llm_generate", _llm_recorder(captured))
        state = _state([_small_table()])

        with use_identity_context(SimpleNamespace(tenant_id=1, user_id=7, role="analyst")):
            result = await generate_module.generate_sql_node(state, _MARKER_CONFIG)

        assert result["generated_sql"]
        assert calls["profile_calls"] == 1
        assert "workshop_code" in captured["data_profile"]
        assert "唯一（120 个值）" not in captured["data_profile"]

    # 方法作用：与真实画像契约接线——原始表结构必须归一为 TableSchema 并通过真实校验。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_real_profiling_contract_wiring(self, monkeypatch) -> None:
        import src.datasource.profiling as profiling_module
        import src.llm.schema_keys as schema_keys_module

        schema_keys_module.clear_schema_key_cache()
        monkeypatch.setattr(schema_keys_module, "_is_task_llm_available", lambda task: False)
        recorded: dict = {}

        # 方法作用：真实契约下的画像替身，只记录请求不访问数据库。
        # Args: 无。
        # Returns: 具备异步 profile 的替身。
        class _Profiler:
            # 方法作用：记录真实 ProfilingContext 与候选列并返回空快照。
            # Args: context - 真实 ProfilingContext；columns_by_table - 候选列。
            # Returns: 真实 ProfileSnapshot。
            async def profile(self, context, columns_by_table):
                recorded["context"] = context
                recorded["columns_by_table"] = columns_by_table
                return profiling_module.ProfileSnapshot(tables=[], query_count=1, errors=[])

        monkeypatch.setattr(profiling_module, "get_data_profiler", lambda: _Profiler())
        captured: dict = {}
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(generate_module, "_llm_generate", _llm_recorder(captured))

        with use_identity_context(SimpleNamespace(tenant_id=1, user_id=7, role="analyst")):
            await generate_module.generate_sql_node(_state([_small_table()]), _MARKER_CONFIG)

        context = recorded["context"]
        # 原始字典必须已归一为 TableSchema，不能靠契约校验失败后降级；
        # 且上下文只携带授权列：id/factory_id 不在本次 allowed_columns 白名单内。
        assert isinstance(context, profiling_module.ProfilingContext)
        assert context.datasource == "ds_main"
        assert context.role == "analyst"
        assert [column.name for column in context.tables["t_workshop"].columns] == [
            "workshop_code", "workshop_name",
        ]
        assert recorded["columns_by_table"]["t_workshop"]
        assert "workshop_code" in captured["data_profile"]

    # 方法作用：画像模块未就绪（并行任务尚未落盘）时整体降级为原路径。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_profile_module_unavailable_degrades(self, monkeypatch) -> None:
        # 方法作用：模拟画像模块缺失的加载失败。
        # Args: 无。
        # Returns: 永不返回。
        def missing_module():
            raise ImportError("src.datasource.profiling 未就绪")

        monkeypatch.setattr(generate_module, "_load_profiling_api", missing_module)
        captured: dict = {}
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(generate_module, "_llm_generate", _llm_recorder(captured))

        with use_identity_context(SimpleNamespace(tenant_id=1, user_id=7, role="analyst")):
            result = await generate_module.generate_sql_node(_state([_small_table()]), _MARKER_CONFIG)

        assert result["generated_sql"]
        assert captured["data_profile"] == ""
        assert captured["probe_context"] is None

    # 方法作用：键列判断被取消时必须向上传播，不能吞掉取消。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_detection_cancellation_propagates(self, monkeypatch) -> None:
        api, _calls = _fake_profiling_api()
        monkeypatch.setattr(generate_module, "_load_profiling_api", lambda: api)
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)

        # 方法作用：模拟取消。
        # Args: context - 授权上下文。
        # Returns: 永不返回。
        async def cancelled(context):
            raise asyncio.CancelledError()

        monkeypatch.setattr(generate_module, "detect_schema_keys", cancelled)

        with pytest.raises(asyncio.CancelledError):
            with use_identity_context(SimpleNamespace(tenant_id=1, user_id=7, role="analyst")):
                await generate_module.generate_sql_node(_state([_small_table()]), _MARKER_CONFIG)


class TestIntelligenceFallbackLogObservability:
    """覆盖 30.2 画像接入回退边界的可观测性：一次脱敏堆栈，秘密不回显。"""

    # 方法作用：画像模块缺失回退只记录一次脱敏堆栈，模拟秘密不入日志。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_module_unavailable_logs_once_with_sanitized_stack(self, monkeypatch) -> None:
        """导入失败正文可能含路径等敏感信息，必须用固定中文文案承载原始堆栈。"""
        # Arrange
        from structlog.testing import capture_logs

        secret = "SECRET-模拟凭证-未就绪"

        # 方法作用：模拟画像模块加载失败并携带模拟秘密正文。
        # Args: 无。
        # Returns: 永不返回。
        def missing_module():
            """抛出携带模拟秘密正文的导入异常，用于验证日志脱敏。"""
            raise ImportError(f"src.datasource.profiling 未就绪: {secret}")

        monkeypatch.setattr(generate_module, "_load_profiling_api", missing_module)
        captured: dict = {}
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(generate_module, "_llm_generate", _llm_recorder(captured))

        # Act
        with capture_logs() as events:
            with use_identity_context(SimpleNamespace(tenant_id=1, user_id=7, role="analyst")):
                result = await generate_module.generate_sql_node(_state([_small_table()]), _MARKER_CONFIG)

        # Assert
        assert result["generated_sql"]
        assert captured["data_profile"] == ""
        fallback_events = [event for event in events if event.get("reason") == "profiling_unavailable"]
        assert len(fallback_events) == 1
        event = fallback_events[0]
        assert event["event"] == "SQL 智能降级"
        assert event["error_type"] == "ImportError"
        stack_text = "".join(traceback.format_exception(*event["exc_info"]))
        assert "missing_module" in stack_text
        assert "画像模块未就绪" in stack_text
        assert secret not in stack_text
        assert secret not in repr(events)

    # 方法作用：非法上下文回退只记录一次脱敏堆栈，校验正文不回显。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_context_build_failure_logs_once_with_sanitized_stack(self, monkeypatch) -> None:
        """契约校验失败正文可能回显字段值，必须替换为固定中文文案后落盘。"""
        # Arrange
        from structlog.testing import capture_logs

        secret = "SECRET-模拟凭证-非法上下文"

        # 方法作用：无论参数如何都抛出含模拟秘密的校验异常。
        # Args: **kwargs - 画像上下文构造参数。
        # Returns: 永不返回。
        def raising_context(**kwargs):
            """抛出携带模拟秘密的上下文校验异常，用于验证日志脱敏。"""
            del kwargs
            raise ValueError(f"上下文校验失败: {secret}")

        api = SimpleNamespace(
            ProfilingContext=raising_context,
            get_data_profiler=lambda: None,
            render_profile=lambda snapshot: "",
        )
        monkeypatch.setattr(generate_module, "_load_profiling_api", lambda: api)
        captured: dict = {}
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(generate_module, "_llm_generate", _llm_recorder(captured))

        # Act
        with capture_logs() as events:
            with use_identity_context(SimpleNamespace(tenant_id=1, user_id=7, role="analyst")):
                result = await generate_module.generate_sql_node(_state([_small_table()]), _MARKER_CONFIG)

        # Assert
        assert result["generated_sql"]
        assert captured["data_profile"] == ""
        fallback_events = [event for event in events if event.get("reason") == "context_build_failed"]
        assert len(fallback_events) == 1
        event = fallback_events[0]
        assert event["error_type"] == "ValueError"
        stack_text = "".join(traceback.format_exception(*event["exc_info"]))
        assert "raising_context" in stack_text
        assert "画像上下文构造失败" in stack_text
        assert secret not in stack_text
        assert secret not in repr(events)


class TestIntelligenceDisabled:
    """覆盖 30.3：关闭或身份不符时零新增调用、原 Prompt 不变。"""

    # 方法作用：无标记时键列判断、画像与探针全部零调用，且 data_profile 为空。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_disabled_marker_makes_zero_new_calls(self, monkeypatch) -> None:
        detect = AsyncMock(return_value={})
        monkeypatch.setattr(generate_module, "detect_schema_keys", detect)
        monkeypatch.setattr(
            generate_module, "_load_profiling_api",
            AsyncMock(side_effect=AssertionError("画像不得加载")),
        )
        monkeypatch.setattr(
            generate_module, "_data_probe_context_scope",
            AsyncMock(side_effect=AssertionError("探针作用域不得创建")),
        )
        captured: dict = {}
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(generate_module, "_llm_generate", _llm_recorder(captured))
        state = _state([_small_table()])

        with use_identity_context(SimpleNamespace(tenant_id=1, user_id=7, role="analyst")):
            await generate_module.generate_sql_node(state, {"configurable": {"thread_id": "t-1"}})

        assert detect.await_count == 0
        assert captured["data_profile"] == ""
        assert captured["probe_context"] is None
        # Schema 与禁用前完全一致：等于原始裁剪与渲染结果。
        from src.graph.nodes.generate_sql import (
            _PROMPT_SCHEMA_MAX_CHARS,
            format_schema_for_prompt,
            select_prompt_tables,
        )

        expected = format_schema_for_prompt(
            select_prompt_tables(
                state["relevant_tables"], state["user_query"],
                dialect="mysql", max_chars=_PROMPT_SCHEMA_MAX_CHARS,
            ),
            "mysql",
        )
        assert captured["schema_text"] == expected

    # 方法作用：真实身份与请求上下文身份不一致时零查询（不信任 state 伪造身份）。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_identity_mismatch_makes_zero_queries(self, monkeypatch) -> None:
        detect = AsyncMock(return_value={})
        monkeypatch.setattr(generate_module, "detect_schema_keys", detect)
        monkeypatch.setattr(
            generate_module, "_load_profiling_api",
            AsyncMock(side_effect=AssertionError("画像不得加载")),
        )
        captured: dict = {}
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(generate_module, "_llm_generate", _llm_recorder(captured))
        state = _state([_small_table()])
        state["user_id"] = 99  # state 声称的身份与真实认证身份不一致

        with use_identity_context(SimpleNamespace(tenant_id=1, user_id=7, role="analyst")):
            await generate_module.generate_sql_node(state, _MARKER_CONFIG)

        assert detect.await_count == 0
        assert captured["data_profile"] == ""
        assert captured["probe_context"] is None

    # 方法作用：多源且无法唯一定位当前源时不得猜用第一源权限，零查询。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_ambiguous_datasource_makes_zero_queries(self, monkeypatch) -> None:
        detect = AsyncMock(return_value={})
        monkeypatch.setattr(generate_module, "detect_schema_keys", detect)
        monkeypatch.setattr(
            generate_module, "_load_profiling_api",
            AsyncMock(side_effect=AssertionError("画像不得加载")),
        )
        captured: dict = {}
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(generate_module, "_llm_generate", _llm_recorder(captured))
        state = _state(
            [_small_table()],
            datasource="",
            datasource_access={
                "ds_a": {"name": "ds_a", "allowed_columns": ["a"], "row_filter_sql": ""},
                "ds_b": {"name": "ds_b", "allowed_columns": ["b"], "row_filter_sql": ""},
            },
        )

        with use_identity_context(SimpleNamespace(tenant_id=1, user_id=7, role="analyst")):
            await generate_module.generate_sql_node(state, _MARKER_CONFIG)

        assert detect.await_count == 0
        assert captured["data_profile"] == ""
        assert captured["probe_context"] is None


class TestExplicitDatasourceAccess:
    """覆盖 30.3：显式数据源必须命中授权，未指定时只接受唯一授权源。"""

    # 方法作用：构造数据源选择只读上下文的最小形态。
    # Args: datasource - 路由显式数据源；access_map - 授权快照映射。
    # Returns: 含 routing/permission 两组的上下文替身。
    @staticmethod
    def _contexts(datasource: str, access_map: dict[str, dict]) -> SimpleNamespace:
        return SimpleNamespace(
            routing=SimpleNamespace(datasource=datasource),
            permission=SimpleNamespace(datasource_access=access_map),
        )

    # 方法作用：显式未授权源即使仅有另一个源可用也必须拒绝，不得回退借用其权限快照。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_explicit_unauthorized_datasource_rejected(self) -> None:
        """routing=secret 而仅有 public 权限时返回 None，不落入唯一源回退。"""
        contexts = self._contexts(
            "secret",
            {"public": {"allowed_columns": ["orders.id"], "row_filter_sql": ""}},
        )
        assert _current_datasource_access(contexts) is None

    # 方法作用：显式数据源存在但条目不是快照字典时同样安全拒绝。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_explicit_datasource_with_invalid_entry_rejected(self) -> None:
        """授权条目非字典（契约外形态）时返回 None，不借用其他源。"""
        contexts = self._contexts(
            "secret",
            {
                "public": {"allowed_columns": ["orders.id"], "row_filter_sql": ""},
                "secret": "not-a-snapshot",
            },
        )
        assert _current_datasource_access(contexts) is None

    # 方法作用：显式数据源命中授权映射时返回该源自身的条目。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_explicit_authorized_datasource_selected(self) -> None:
        """routing=secret 且 secret 在授权映射中时返回 secret 自身的快照。"""
        access_map = {
            "public": {"allowed_columns": ["orders.id"], "row_filter_sql": ""},
            "secret": {"allowed_columns": ["secret.id"], "row_filter_sql": "level <= 3"},
        }
        contexts = self._contexts("secret", access_map)
        assert _current_datasource_access(contexts) == ("secret", access_map["secret"])

    # 方法作用：未指定数据源且仅有一个授权源时保留合理回退。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_unspecified_single_source_fallback_kept(self) -> None:
        """routing 为空而仅有一个授权源时仍返回该唯一源。"""
        access_map = {"public": {"allowed_columns": ["orders.id"], "row_filter_sql": ""}}
        contexts = self._contexts("", access_map)
        assert _current_datasource_access(contexts) == ("public", access_map["public"])

    # 方法作用：未指定数据源且多源无法唯一定位时拒绝，不猜用第一源。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_unspecified_multiple_sources_rejected(self) -> None:
        """routing 为空且授权映射含多源时返回 None。"""
        contexts = self._contexts(
            "",
            {
                "ds_a": {"allowed_columns": ["a"], "row_filter_sql": ""},
                "ds_b": {"allowed_columns": ["b"], "row_filter_sql": ""},
            },
        )
        assert _current_datasource_access(contexts) is None

    # 方法作用：节点层：显式未授权源时零键列模型、零画像、零探针绑定，原路径照常出 SQL。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_explicit_unauthorized_datasource_makes_zero_queries(self, monkeypatch) -> None:
        """secret 未授权时不得借用 public 快照触发键列判断、画像或探针绑定。"""
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        import src.llm.invocation as invocation_module
        from src.llm.knowledge_loop import KnowledgeLoopConfig, use_knowledge_loop

        detect = AsyncMock(return_value={})
        monkeypatch.setattr(generate_module, "detect_schema_keys", detect)
        # 记录式替身：若错误回退发生，画像加载与画像调用会被记录而不是静默降级。
        api, calls = _fake_profiling_api()
        monkeypatch.setattr(generate_module, "_load_profiling_api", lambda: api)
        probe_recorder: dict = {}
        monkeypatch.setattr(
            generate_module, "_data_probe_context_scope",
            _fake_probe_scope_factory(probe_recorder),
        )
        fake_model = FakeListChatModel(responses=[
            '{"sql": "SELECT 1", "explanation": "ok", "assumptions": [], "confidence": "high"}',
        ])
        monkeypatch.setattr(generate_module, "get_llm", lambda temperature=0, **kwargs: fake_model)
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        captured: dict = {}
        real_build = invocation_module.build_budgeted_prompt

        # 方法作用：记录预算器收到的 Section 后委托真实实现。
        # Args: prompt_id - Prompt 标识；sections - 动态片段；kwargs - 其余参数。
        # Returns: 真实预算结果。
        def spy_build(prompt_id, sections, **kwargs):
            captured["sections"] = {section.name: section for section in sections}
            return real_build(prompt_id, sections, **kwargs)

        monkeypatch.setattr(invocation_module, "build_budgeted_prompt", spy_build)
        state = _state(
            [_small_table()],
            datasource="secret",
            datasource_access={
                "public": {"name": "public", "allowed_columns": ["workshop_code"], "row_filter_sql": ""},
            },
        )

        with use_knowledge_loop(KnowledgeLoopConfig(enabled=False)):
            with use_identity_context(SimpleNamespace(tenant_id=1, user_id=7, role="analyst")):
                result = await generate_module.generate_sql_node(state, _MARKER_CONFIG)

        # 错误回退（借用 public 快照）会让以下每一项都非零/非空：全部必须保持为零。
        assert detect.await_count == 0
        assert calls["context_kwargs"] is None
        assert calls["profile_calls"] == 0
        assert calls["render_calls"] == 0
        assert probe_recorder == {}
        assert captured["sections"]["data_profile"].content == ""
        # 降级后原路径照常生成 SQL，且不写回任何画像/探针状态。
        assert result["generated_sql"] == "SELECT 1"
        assert "data_profile" not in result
        assert "_profile_key_columns" not in str(result)


class TestDataProfilePromptSection:
    """覆盖 30.2：data_profile PromptSection 的位置与预算契约。"""

    # 方法作用：data_profile 段优先级 95、上限 6000，内容进入生成消息，探针作用域自动复原。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_section_priority_and_probe_scope_restored(self, monkeypatch) -> None:
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        import src.llm.invocation as invocation_module
        from src.llm.knowledge_loop import KnowledgeLoopConfig, use_knowledge_loop

        fake_model = FakeListChatModel(responses=[
            '{"sql": "SELECT 1", "explanation": "ok", "assumptions": [], "confidence": "high"}',
        ])
        monkeypatch.setattr(generate_module, "get_llm", lambda temperature=0, **kwargs: fake_model)
        captured: dict = {}
        real_build = invocation_module.build_budgeted_prompt

        # 方法作用：记录预算器收到的 Section 后委托真实实现。
        # Args: prompt_id - Prompt 标识；sections - 动态片段；kwargs - 其余参数。
        # Returns: 真实预算结果。
        def spy_build(prompt_id, sections, **kwargs):
            captured["sections"] = {section.name: section for section in sections}
            return real_build(prompt_id, sections, **kwargs)

        monkeypatch.setattr(invocation_module, "build_budgeted_prompt", spy_build)
        probe_recorder: dict = {}
        monkeypatch.setattr(
            generate_module, "_data_probe_context_scope",
            _fake_probe_scope_factory(probe_recorder),
        )

        with use_knowledge_loop(KnowledgeLoopConfig(enabled=False)):
            sql, _reasoning, explanation = await generate_module._llm_generate(
                schema_text="## 表结构\n- t_workshop",
                table_whitelist="",
                dialect_hint="",
                dialect="sqlite",
                query="各车间产量",
                error_ctx="",
                skill_prompt="",
                grounding_context="",
                config={"configurable": {}},
                data_profile="键列候选: t_workshop.workshop_code（未经数据验证）",
                probe_context=object(),
            )

        section = captured["sections"]["data_profile"]
        assert section.priority == 95
        assert section.max_chars == 6000
        assert "workshop_code" in section.content
        assert sql == "SELECT 1"
        # 探针作用域围绕本次 prepare/stream，结束后复原。
        assert probe_recorder["events"] == ["enter", "exit"]

    # 方法作用：无探针上下文时不创建作用域，兼容原有调用签名。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_no_probe_scope_without_context(self, monkeypatch) -> None:
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        from src.llm.knowledge_loop import KnowledgeLoopConfig, use_knowledge_loop

        fake_model = FakeListChatModel(responses=[
            '{"sql": "SELECT 2", "explanation": "ok", "assumptions": [], "confidence": "high"}',
        ])
        monkeypatch.setattr(generate_module, "get_llm", lambda temperature=0, **kwargs: fake_model)
        probe_recorder: dict = {}
        monkeypatch.setattr(
            generate_module, "_data_probe_context_scope",
            _fake_probe_scope_factory(probe_recorder),
        )

        with use_knowledge_loop(KnowledgeLoopConfig(enabled=False)):
            sql, _reasoning, _explanation = await generate_module._llm_generate(
                schema_text="## 表结构\n- t_workshop",
                table_whitelist="",
                dialect_hint="",
                dialect="sqlite",
                query="各车间产量",
                error_ctx="",
                skill_prompt="",
                grounding_context="",
                config={"configurable": {}},
            )

        assert probe_recorder == {}
        assert sql == "SELECT 2"


class TestApiEntryMarkers:
    """覆盖 30.3：两种聊天入口写入服务端标记，报表默认关闭，请求不可覆写。"""

    # 方法作用：非流式 /chat 的服务端 RunnableConfig 携带启用标记。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_non_stream_chat_sets_marker(self, monkeypatch) -> None:
        import importlib

        import src.api.routes as routes_package

        # 包命名空间里的 `chat` 是路由函数，必须用 importlib 取到真正的路由模块。
        chat_module = importlib.import_module("src.api.routes.chat")

        monkeypatch.setattr(chat_module, "_enforce_chat_request_quota", AsyncMock())
        monkeypatch.setattr(chat_module, "_validated_skill_ids", lambda req: [])
        monkeypatch.setattr(
            chat_module, "_resolve_chat_access_for_request", AsyncMock(return_value={}),
        )
        monkeypatch.setattr(chat_module, "_resolve_chat_llm_selection", AsyncMock(return_value=None))
        monkeypatch.setattr("src.api.background_tasks.create_background_task", lambda *a, **k: None)
        monkeypatch.setattr(
            "src.memory.session_store.get_session_store",
            lambda: SimpleNamespace(touch=lambda *a, **k: None, create=lambda *a, **k: None),
        )
        ainvoke = AsyncMock(return_value={"final_response": {}})
        monkeypatch.setattr(
            routes_package, "_app", lambda: SimpleNamespace(ainvoke=ainvoke),
        )

        await chat_module.chat(ChatRequest(query="各车间产量", stream=False))

        cfg = ainvoke.await_args.args[1]
        assert cfg["configurable"][SQL_INTELLIGENCE_CONFIG_KEY] is True

    # 方法作用：流式入口构建的工作流配置携带启用标记，且 done 事件行为不变。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_stream_entry_sets_marker(self, monkeypatch) -> None:
        import src.api.streaming as streaming_module
        import src.graph.workflow as workflow_module

        captured: dict = {}

        # 方法作用：记录工作流配置并产出空事件流。
        # Args: input_state - 初始状态；config - 工作流配置；version - 事件版本。
        # Returns: 空异步生成器。
        async def fake_astream_events(input_state, config, version="v2"):
            captured["config"] = config
            captured["input_state"] = input_state
            if False:
                yield None

        monkeypatch.setattr(
            workflow_module, "app", SimpleNamespace(astream_events=fake_astream_events),
        )
        monkeypatch.setattr("src.api.background_tasks.create_background_task", lambda *a, **k: None)
        # 后台任务被替换为 no-op 后，协程工厂同样要替换，避免“未 await”告警。
        monkeypatch.setattr(streaming_module, "_touch_session_meta", lambda *a, **k: None)
        monkeypatch.setattr(streaming_module, "_save_session_meta", lambda *a, **k: None)

        chunks = [
            chunk async for chunk in streaming_module.stream_analysis(
                "各车间产量", "ds_main", "sess-1",
                tenant_id=1, user_id=7, user_role="analyst",
                datasource_access={"ds_main": {"name": "ds_main", "allowed_columns": [], "row_filter_sql": ""}},
            )
        ]

        assert captured["config"]["configurable"][SQL_INTELLIGENCE_CONFIG_KEY] is True
        assert any('"type": "done"' in chunk for chunk in chunks)

    # 方法作用：报表入口（与 runner 相同的 config 形状）不携带标记，默认关闭。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    async def test_report_entry_defaults_to_disabled(self) -> None:
        runner_config = {"configurable": {"thread_id": "report-thread"}}

        assert generate_module._sql_intelligence_enabled(runner_config) is False

        runner_source = Path("src/reporting/runner.py").read_text(encoding="utf-8")
        assert SQL_INTELLIGENCE_CONFIG_KEY not in runner_source

    # 方法作用：ChatRequest 不接受请求参数覆写标记。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_request_body_cannot_override_marker(self) -> None:
        assert SQL_INTELLIGENCE_CONFIG_KEY not in ChatRequest.model_fields

        request = ChatRequest.model_validate({"query": "各车间产量", SQL_INTELLIGENCE_CONFIG_KEY: False})

        assert not hasattr(request, SQL_INTELLIGENCE_CONFIG_KEY)


# 方法作用：构造带大小写变体与未授权列的表，验证生成接入的授权边界。
# Args: 无。
# Returns: 与 relevant_tables 形状一致的表字典。
def _identity_boundary_table() -> dict:
    return {
        "name": "orders",
        "description": "订单测试表",
        "columns": [
            {"name": "id", "type": "INTEGER", "comment": "订单编号", "is_nullable": False, "is_primary_key": True, "is_indexed": True},
            {"name": "customer", "type": "TEXT", "comment": "客户名称", "is_nullable": False, "is_primary_key": False, "is_indexed": False},
            {"name": "amount", "type": "REAL", "comment": "订单金额", "is_nullable": False, "is_primary_key": False, "is_indexed": False},
            {"name": "Status", "type": "TEXT", "comment": "订单状态", "is_nullable": True, "is_primary_key": False, "is_indexed": False},
            {"name": "status", "type": "TEXT", "comment": "小写状态", "is_nullable": True, "is_primary_key": False, "is_indexed": False},
        ],
        "relations": [],
    }


class TestAuthorizedStructureBoundary:
    """覆盖 30.2：生成接入只使用授权过滤后的结构（spec/55 §6）。"""

    # 方法作用：启用智能时未授权列与大小写变体列不进入 Prompt、画像请求与裁剪保护。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_only_authorized_columns_reach_prompt_and_profile(self, monkeypatch) -> None:
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        import src.datasource.profiling as profiling_module
        import src.llm.invocation as invocation_module
        import src.llm.schema_keys as schema_keys_module
        from src.llm.knowledge_loop import KnowledgeLoopConfig, use_knowledge_loop

        schema_keys_module.clear_schema_key_cache()
        monkeypatch.setattr(schema_keys_module, "_is_task_llm_available", lambda task: False)
        recorded: dict = {}

        # 方法作用：真实契约的画像替身：记录真实上下文并返回真实快照，不访问数据库。
        # Args: 无。
        # Returns: 具备异步 profile 的替身。
        class _Profiler:
            # 方法作用：记录真实 ProfilingContext 与候选列并返回含唯一性结论的真实快照。
            # Args: context - 真实 ProfilingContext；columns_by_table - 候选列。
            # Returns: 真实 ProfileSnapshot。
            async def profile(self, context, columns_by_table):
                recorded["context"] = context
                recorded["columns_by_table"] = columns_by_table
                return profiling_module.ProfileSnapshot(tables=[
                    profiling_module.TableProfile(
                        name="orders", scope="complete", sampled_rows=3,
                        columns=[profiling_module.ColumnProfile(
                            name="id", scope="complete", non_null_count=3,
                            distinct_count=3, uniqueness="unique",
                        )],
                    ),
                ], query_count=1, errors=[])

        monkeypatch.setattr(profiling_module, "get_data_profiler", lambda: _Profiler())
        fake_model = FakeListChatModel(responses=[
            '{"sql": "SELECT id, customer FROM orders", "explanation": "ok", "assumptions": [], "confidence": "high"}',
        ])
        monkeypatch.setattr(generate_module, "get_llm", lambda temperature=0, **kwargs: fake_model)
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        captured: dict = {}
        real_build = invocation_module.build_budgeted_prompt

        # 方法作用：记录预算器收到的 Section 后委托真实实现。
        # Args: prompt_id - Prompt 标识；sections - 动态片段；kwargs - 其余参数。
        # Returns: 真实预算结果。
        def spy_build(prompt_id, sections, **kwargs):
            captured.setdefault("sections", {}).update({section.name: section for section in sections})
            return real_build(prompt_id, sections, **kwargs)

        monkeypatch.setattr(invocation_module, "build_budgeted_prompt", spy_build)
        state = _state(
            [_identity_boundary_table()],
            datasource_access={"ds_main": {
                "name": "ds_main",
                "allowed_columns": ["orders.id", "orders.customer", "orders.Status"],
                "row_filter_sql": "",
            }},
        )
        state["user_query"] = "查询订单明细"

        with use_knowledge_loop(KnowledgeLoopConfig(enabled=False)):
            with use_identity_context(SimpleNamespace(tenant_id=1, user_id=7, role="analyst")):
                result = await generate_module.generate_sql_node(state, _MARKER_CONFIG)

        assert result["generated_sql"]
        schema_text = captured["sections"]["schema"].content
        # 结构段只允许授权列的物理拼写出现；未授权列与大小写变体列都不在。
        assert "| id |" in schema_text
        assert "| customer |" in schema_text
        assert "| Status |" in schema_text
        assert "| amount |" not in schema_text
        assert "| status |" not in schema_text
        # 画像上下文与请求列同样只来自授权结构（真实 ProfilingContext/TableSchema）。
        context = recorded["context"]
        assert isinstance(context, profiling_module.ProfilingContext)
        assert [column.name for column in context.tables["orders"].columns] == [
            "id", "customer", "Status",
        ]
        assert recorded["columns_by_table"]["orders"]
        assert set(recorded["columns_by_table"]["orders"]) <= {"id", "customer", "Status"}
        # 真实 render_profile 的证据与键列提示进入画像段，且不含未授权列名。
        data_profile = captured["sections"]["data_profile"].content
        assert "唯一" in data_profile
        assert "id" in data_profile
        assert "amount" not in data_profile
        # 画像/键列提示/结构样本只存在于本次调用的 Prompt 组装，不写回节点结果（state）。
        assert "data_profile" not in result
        assert "_profile_key_columns" not in str(result)
        assert "amount" not in str(result)

    # 方法作用：授权过滤后的大小写变体列不会被键列提示保护重新保留。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_case_variant_hint_cannot_restore_unauthorized_column(self) -> None:
        from src.llm.schema_keys import apply_key_hints

        table = _identity_boundary_table()
        filtered = generate_module._filter_authorized_tables(
            [table], ["orders.id", "orders.customer", "orders.Status"],
        )
        hinted = apply_key_hints(filtered, {"orders": {"key_columns": ["status", "STATUS", "amount"]}})

        # “status”大小写不敏感唯一命中已授权的 Status；“STATUS/amount”不落保护。
        assert hinted[0]["_profile_key_columns"] == ["Status"]

        pruned = _prune_prompt_table_columns(hinted[0], "查询订单明细")
        names = {column["name"] for column in pruned["columns"]}
        assert names == {"id", "customer", "Status"}

        # 无列级策略时大小写变体并存：歧义提示不保护任何列，不能借道恢复未授权拼写。
        both = _identity_boundary_table()
        both["columns"] = [
            column for column in both["columns"] if column["name"] in {"Status", "status"}
        ]
        ambiguous = apply_key_hints([both], {"orders": {"key_columns": ["STATUS"]}})
        assert ambiguous[0]["_profile_key_columns"] == []
