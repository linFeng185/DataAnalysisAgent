"""30.1 键列结构判断的契约测试（spec/55 §4 与 §14）。

覆盖点：
1. 无模型/失败/超时/非法输出时使用确定性回退，且回退只保留结构候选、不宣称真实唯一；
2. 只输入授权表结构（不含用户问题与样例值），未知列与伪造表被拒绝；
3. 模型调用走 client 工厂 + 注册 Prompt `schema.keys`，输出 800 token、调用期禁用知识/探查工具；
4. 缓存按 tenant/user/授权结构/权限/模型指纹/Prompt 版本隔离，TTL 与容量有界；
5. `apply_key_hints` 只复制不改原对象，非法列名不写入 `_profile_key_columns`。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.llm import schema_keys
from src.llm.output_contracts import SchemaKeyDetectionOutput, SchemaKeyTable


# 方法作用：构造一张含主键、编码、展示名与外键的最小授权表结构。
# Args: extra_columns - 追加的列名列表；relations - 关联关系列表。
# Returns: 与 relevant_tables 形状一致的表字典。
def _table(extra_columns: list[str] | None = None, relations: list[dict] | None = None) -> dict:
    specs = [
        ("id", "Int64", "主键", True, True),
        ("workshop_code", "String", "车间编码", False, False),
        ("workshop_name", "String", "车间名称", False, False),
        ("factory_id", "Int64", "所属工厂", False, False),
        ("remark", "String", "内部备注", False, False),
    ]
    specs.extend((name, "String", "", False, False) for name in (extra_columns or []))
    return {
        "name": "t_workshop",
        "description": "车间表",
        "columns": [
            {
                "name": name,
                "type": data_type,
                "comment": comment,
                "is_nullable": True,
                "is_primary_key": is_pk,
                "is_indexed": is_indexed,
                "sample": "样例值-禁止外传",
            }
            for name, data_type, comment, is_pk, is_indexed in specs
        ],
        "relations": relations or [{"relation_type": "many_to_one", "target_table": "t_factory", "join_key": "factory_id"}],
    }


# 方法作用：构造一次键列判断的授权上下文替身（形状对齐 ProfilingContext）。
# Args: tables - 表结构字典；user_id/tenant_id/datasource - 身份与数据源；allowed_columns/row_filter_sql - 权限指纹输入。
# Returns: 具备 detect_schema_keys 所需字段的轻量上下文。
def _context(
    tables: dict[str, dict] | None = None,
    *,
    tenant_id: int = 1,
    user_id: int = 7,
    datasource: str = "ds_main",
    allowed_columns: list[str] | None = None,
    row_filter_sql: str = "",
) -> SimpleNamespace:
    table = _table()
    return SimpleNamespace(
        tenant_id=tenant_id,
        user_id=user_id,
        datasource=datasource,
        dialect="mysql",
        tables=tables if tables is not None else {table["name"]: table},
        allowed_columns=list(allowed_columns or []),
        row_filter_sql=row_filter_sql,
        schema_fingerprint="",
    )


# 方法作用：把键列判断固定到“无模型/有模型”分支，并清空跨用例缓存。
# Args: monkeypatch - pytest 补丁工具；available - LLM 可用性。
# Returns: 无返回值。
def _patch_llm_availability(monkeypatch, *, available: bool) -> None:
    schema_keys.clear_schema_key_cache()
    monkeypatch.setattr(schema_keys, "_is_task_llm_available", lambda task: available)


class TestDeterministicFallback:
    """覆盖 30.1：无模型/失败/超时/非法输出时的确定性回退。"""

    # 方法作用：无模型时回退按结构启发式给出四键，不宣称真实唯一。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_no_model_uses_deterministic_structure_fallback(self, monkeypatch) -> None:
        _patch_llm_availability(monkeypatch, available=False)

        result = await schema_keys.detect_schema_keys(_context())

        assert set(result) == {"t_workshop"}
        assert set(result["t_workshop"]) == {
            "key_columns", "display_columns", "foreign_keys", "profile_columns",
        }
        assert "id" in result["t_workshop"]["key_columns"]
        assert "workshop_code" in result["t_workshop"]["key_columns"]
        assert "workshop_name" in result["t_workshop"]["display_columns"]
        assert "factory_id" in result["t_workshop"]["foreign_keys"]
        # 结构候选绝不能自称唯一：输出里不能出现任何唯一性断言字段。
        assert "unique" not in str(result).lower()

    # 方法作用：模型失败（异常）时回退确定性规则，不向上抛错。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_model_failure_falls_back(self, monkeypatch) -> None:
        _patch_llm_availability(monkeypatch, available=True)
        monkeypatch.setattr(
            schema_keys, "_invoke_key_model", AsyncMock(side_effect=RuntimeError("boom")),
        )

        result = await schema_keys.detect_schema_keys(_context())

        assert "workshop_code" in result["t_workshop"]["key_columns"]

    # 方法作用：模型超时（超过配置超时）时回退，不阻塞主流程。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_model_timeout_falls_back(self, monkeypatch) -> None:
        _patch_llm_availability(monkeypatch, available=True)
        monkeypatch.setattr(
            schema_keys, "_schema_key_settings",
            lambda: {
                "enabled": True, "max_tokens": 800, "timeout": 0.02,
                "cache_ttl": 300.0, "cache_entries": 128,
            },
        )

        # 方法作用：模拟超时模型调用。
        # Args: context - 上下文；tables - 表结构；settings - 配置快照。
        # Returns: 永不返回（被超时取消）。
        async def slow_invoke(context, tables, settings):
            await asyncio.sleep(5)

        monkeypatch.setattr(schema_keys, "_invoke_key_model", slow_invoke)

        result = await schema_keys.detect_schema_keys(_context())

        assert "id" in result["t_workshop"]["key_columns"]

    # 方法作用：取消必须原样向上传播，不能降级成确定性回退。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_cancelled_error_propagates(self, monkeypatch) -> None:
        _patch_llm_availability(monkeypatch, available=True)
        monkeypatch.setattr(
            schema_keys, "_invoke_key_model",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )

        with pytest.raises(asyncio.CancelledError):
            await schema_keys.detect_schema_keys(_context())

    # 方法作用：开关关闭时不调用模型，直接使用确定性回退。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_disabled_switch_skips_model(self, monkeypatch) -> None:
        _patch_llm_availability(monkeypatch, available=True)
        invoke = AsyncMock()
        monkeypatch.setattr(schema_keys, "_invoke_key_model", invoke)
        monkeypatch.setattr(
            schema_keys, "_schema_key_settings",
            lambda: {
                "enabled": False, "max_tokens": 800, "timeout": 3.0,
                "cache_ttl": 300.0, "cache_entries": 128,
            },
        )

        await schema_keys.detect_schema_keys(_context())

        assert invoke.await_count == 0

    # 方法作用：空表结构返回空结果，不触发模型调用。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_empty_tables_returns_empty(self, monkeypatch) -> None:
        _patch_llm_availability(monkeypatch, available=True)
        invoke = AsyncMock()
        monkeypatch.setattr(schema_keys, "_invoke_key_model", invoke)

        assert await schema_keys.detect_schema_keys(_context(tables={})) == {}
        assert invoke.await_count == 0


class TestModelOutputValidation:
    """覆盖 30.1：模型输出的未知列/伪造表拒绝与回退合并。"""

    # 方法作用：模型给出的非法列被拒绝，伪造表被丢弃，合法列与回退候选合并。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_unknown_columns_and_forged_tables_rejected(self, monkeypatch) -> None:
        _patch_llm_availability(monkeypatch, available=True)
        monkeypatch.setattr(
            schema_keys, "_invoke_key_model",
            AsyncMock(return_value=SchemaKeyDetectionOutput(tables=[
                SchemaKeyTable(
                    table="t_workshop",
                    key_columns=["id", "ghost_column"],
                    display_columns=["workshop_name"],
                ),
                SchemaKeyTable(table="forged_table", key_columns=["secret_id"]),
            ])),
        )

        result = await schema_keys.detect_schema_keys(_context())

        assert set(result) == {"t_workshop"}
        assert "ghost_column" not in result["t_workshop"]["key_columns"]
        assert "id" in result["t_workshop"]["key_columns"]
        # 回退的结构候选仍然保留，避免模型少给列时失去裁剪保护。
        assert "workshop_code" in result["t_workshop"]["key_columns"]
        assert "secret_id" not in str(result)

    # 方法作用：模型回表的列名大小写归一为授权结构里的真实拼写。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_model_columns_canonicalized(self, monkeypatch) -> None:
        _patch_llm_availability(monkeypatch, available=True)
        monkeypatch.setattr(
            schema_keys, "_invoke_key_model",
            AsyncMock(return_value=SchemaKeyDetectionOutput(tables=[
                SchemaKeyTable(table="T_WORKSHOP", key_columns=["WORKSHOP_CODE"]),
            ])),
        )

        result = await schema_keys.detect_schema_keys(_context())

        assert "workshop_code" in result["t_workshop"]["key_columns"]

    # 方法作用：非法输出（全部表都不在授权结构内）时整体回退。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_fully_invalid_output_falls_back(self, monkeypatch) -> None:
        _patch_llm_availability(monkeypatch, available=True)
        monkeypatch.setattr(
            schema_keys, "_invoke_key_model",
            AsyncMock(return_value=SchemaKeyDetectionOutput(tables=[])),
        )

        result = await schema_keys.detect_schema_keys(_context())

        assert "workshop_code" in result["t_workshop"]["key_columns"]


class TestInvocationContract:
    """覆盖 30.1：模型调用契约（工厂、Prompt、token 上限、禁用工具）。"""

    # 方法作用：键列判断走 client 工厂 + `schema.keys` Prompt，800 token 且调用期禁用知识工具。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_invocation_uses_factory_prompt_and_disables_tools(self, monkeypatch) -> None:
        from src.llm.knowledge_loop import get_knowledge_loop_config

        _patch_llm_availability(monkeypatch, available=True)
        created: dict = {}
        captured: dict = {}

        # 方法作用：记录工厂调用参数并返回可辨识的假模型。
        # Args: task - 任务标识；kwargs - 模型创建参数。
        # Returns: 假模型对象。
        def fake_factory(task, **kwargs):
            created["task"] = task
            created.update(kwargs)
            return Mock(name="fake-key-model")

        monkeypatch.setattr(schema_keys, "_get_task_llm", fake_factory)

        # 方法作用：以结构化调用替身捕获 Prompt 与调用参数，并记录调用期的工具开关。
        # Args: prompt_id - Prompt 标识；sections - 动态片段；kwargs - 调用参数。
        # Returns: 空表输出。
        async def fake_invoke(prompt_id, sections, **kwargs):
            captured["prompt_id"] = prompt_id
            captured["sections"] = [(s.name, s.priority, s.max_chars, s.content) for s in sections]
            captured["kwargs"] = kwargs
            captured["tools_enabled_during_call"] = get_knowledge_loop_config().enabled
            return SchemaKeyDetectionOutput(tables=[])

        monkeypatch.setattr("src.llm.invocation.invoke_structured", fake_invoke)

        await schema_keys.detect_schema_keys(_context())

        assert created["task"] == "schema_key_detection"
        assert created["max_tokens"] == 800
        assert created["stream"] is False
        assert created["reasoning"] is False
        assert captured["prompt_id"] == "schema.keys"
        assert captured["tools_enabled_during_call"] is False
        # 调用结束必须恢复知识工具作用域。
        assert get_knowledge_loop_config().enabled is True
        # 结构输入只含授权结构，不能带样例值或用户问题。
        joined = "\n".join(content for _, _, _, content in captured["sections"])
        assert "样例值-禁止外传" not in joined
        assert "t_workshop" in joined


class TestHintsCache:
    """覆盖 30.1：缓存身份隔离、TTL 与容量边界。"""

    # 方法作用：不同用户/权限/结构/模型指纹各自独立缓存，不跨身份复用。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_cache_isolated_by_identity_structure_and_permissions(self, monkeypatch) -> None:
        _patch_llm_availability(monkeypatch, available=True)
        invoke = AsyncMock(side_effect=RuntimeError("fallback"))
        monkeypatch.setattr(schema_keys, "_invoke_key_model", invoke)

        await schema_keys.detect_schema_keys(_context(user_id=7))
        await schema_keys.detect_schema_keys(_context(user_id=7))
        assert invoke.await_count == 1

        await schema_keys.detect_schema_keys(_context(user_id=8))
        assert invoke.await_count == 2

        await schema_keys.detect_schema_keys(_context(user_id=7, tenant_id=2))
        assert invoke.await_count == 3

        await schema_keys.detect_schema_keys(_context(user_id=7, allowed_columns=["workshop_code"]))
        assert invoke.await_count == 4

        await schema_keys.detect_schema_keys(_context(user_id=7, row_filter_sql="region = '华东'"))
        assert invoke.await_count == 5

        other_table = _table(extra_columns=["added_column"])
        await schema_keys.detect_schema_keys(_context({"t_workshop": other_table}))
        assert invoke.await_count == 6

        monkeypatch.setattr(schema_keys, "_model_fingerprint", lambda: "model-2")
        await schema_keys.detect_schema_keys(_context(user_id=7))
        assert invoke.await_count == 7

        monkeypatch.setattr(schema_keys, "_prompt_version", lambda: "9.9.9")
        await schema_keys.detect_schema_keys(_context(user_id=7))
        assert invoke.await_count == 8

    # 方法作用：缓存过期后重新判断；容量上限触发淘汰。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_cache_ttl_and_capacity(self, monkeypatch) -> None:
        _patch_llm_availability(monkeypatch, available=True)
        invoke = AsyncMock(side_effect=RuntimeError("fallback"))
        monkeypatch.setattr(schema_keys, "_invoke_key_model", invoke)
        clock = {"now": 1000.0}
        monkeypatch.setattr(schema_keys, "_monotonic", lambda: clock["now"])
        monkeypatch.setattr(
            schema_keys, "_schema_key_settings",
            lambda: {
                "enabled": True, "max_tokens": 800, "timeout": 3.0,
                "cache_ttl": 10.0, "cache_entries": 1,
            },
        )

        await schema_keys.detect_schema_keys(_context(user_id=7))
        await schema_keys.detect_schema_keys(_context(user_id=7))
        assert invoke.await_count == 1

        # 未过期但容量为 1：新键写入淘汰旧键。
        await schema_keys.detect_schema_keys(_context(user_id=8))
        await schema_keys.detect_schema_keys(_context(user_id=7))
        assert invoke.await_count == 3

        # 过期后必须重新判断。
        clock["now"] += 11.0
        await schema_keys.detect_schema_keys(_context(user_id=7))
        assert invoke.await_count == 4


class TestApplyKeyHints:
    """覆盖 30.1：`apply_key_hints` 的复制语义与合法列过滤。"""

    # 方法作用：只返回复制后的表结构，原对象不被修改，非法列名不写入。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_copies_and_filters_legal_columns(self) -> None:
        table = _table()
        hints = {
            "t_workshop": {
                "key_columns": ["workshop_code", "ghost_column"],
                "display_columns": ["workshop_name"],
                "foreign_keys": ["factory_id"],
                "profile_columns": ["workshop_code"],
            },
        }

        result = schema_keys.apply_key_hints([table], hints)

        assert "_profile_key_columns" not in table
        assert result[0]["_profile_key_columns"] == ["workshop_code"]
        assert result[0] is not table
        result[0]["columns"][0]["name"] = "mutated"
        assert table["columns"][0]["name"] == "id"

    # 方法作用：无提示或未知表时写入空列表，不抛错。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_missing_hints_write_empty_list(self) -> None:
        table = _table()

        result = schema_keys.apply_key_hints([table], {})

        assert result[0]["_profile_key_columns"] == []

    # 方法作用：表名大小写不敏感匹配提示键。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_table_name_match_is_case_insensitive(self) -> None:
        table = _table()
        table["name"] = "T_Workshop"

        result = schema_keys.apply_key_hints(
            [table], {"t_workshop": {"key_columns": ["id"]}},
        )

        assert result[0]["_profile_key_columns"] == ["id"]


class TestStructureInputIsolation:
    """覆盖 30.1：判断输入只含授权结构，不含用户问题与样例。"""

    # 方法作用：结构渲染排除样例值，只保留表名、列名、类型、注释与主键标记。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_structure_render_excludes_samples(self) -> None:
        rendered = schema_keys.render_structure_input({"t_workshop": _table()})

        assert "样例值-禁止外传" not in rendered
        # 结构信息必须齐全：表名、列名、注释、主键标记。
        assert "t_workshop" in rendered
        assert "workshop_code" in rendered
        assert "PK" in rendered


class TestPhysicalIdentityAuthorization:
    """覆盖 30.1：授权列过滤与大小写精确的物理身份（spec/55 §4）。"""

    # 方法作用：真实 ProfilingContext 下未授权列在模型输入与确定性回退前被过滤。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_authorized_columns_filtered_before_model_and_fallback(self, monkeypatch) -> None:
        from src.datasource.profiling import ProfilingContext
        from src.datasource.schema_snapshot import ColumnInfo, TableSchema

        context = ProfilingContext(
            tenant_id=1,
            user_id=7,
            datasource="ds_main",
            dialect="postgres",
            tables={"orders": TableSchema(name="orders", description="订单表", columns=[
                ColumnInfo("id", "INTEGER", "订单编号", is_nullable=False, is_primary_key=True),
                ColumnInfo("customer", "TEXT", "客户名称", is_nullable=False),
                ColumnInfo("amount", "REAL", "订单金额", is_nullable=False),
            ])},
            allowed_columns=["orders.id", "orders.customer"],
        )

        # 确定性回退：四键候选里不得出现未授权的 amount。
        _patch_llm_availability(monkeypatch, available=False)
        fallback = await schema_keys.detect_schema_keys(context)

        assert set(fallback) == {"orders"}
        assert all("amount" not in columns for columns in fallback["orders"].values())

        # 模型输入：结构文本只含授权列，未授权列不进入提示词。
        captured: list[dict] = []

        # 方法作用：记录进入模型的表结构并返回空输出触发回退。
        # Args: context - 授权上下文；tables - 传入模型的结构；settings - 配置快照。
        # Returns: 空表结构合法输出。
        async def capture(context, tables, settings):
            captured.append(tables)
            return SchemaKeyDetectionOutput(tables=[])

        schema_keys.clear_schema_key_cache()
        monkeypatch.setattr(schema_keys, "_is_task_llm_available", lambda task: True)
        monkeypatch.setattr(schema_keys, "_invoke_key_model", capture)

        await schema_keys.detect_schema_keys(context)

        assert captured
        assert [column["name"] for column in captured[0]["orders"]["columns"]] == ["id", "customer"]

    # 方法作用：模型伪造未授权列或大小写变体列时不能扩大授权范围。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_model_cannot_borrow_case_variant_or_unauthorized_column(self, monkeypatch) -> None:
        from src.datasource.profiling import ProfilingContext
        from src.datasource.schema_snapshot import ColumnInfo, TableSchema

        context = ProfilingContext(
            tenant_id=1,
            user_id=7,
            datasource="ds_main",
            dialect="postgres",
            tables={"orders": TableSchema(name="orders", columns=[
                ColumnInfo("Secret", "TEXT", "大写密钥"),
                ColumnInfo("secret", "TEXT", "小写密钥"),
                ColumnInfo("amount", "REAL", "订单金额"),
            ])},
            allowed_columns=["orders.Secret"],
        )
        _patch_llm_availability(monkeypatch, available=True)
        monkeypatch.setattr(
            schema_keys, "_invoke_key_model",
            AsyncMock(return_value=SchemaKeyDetectionOutput(tables=[
                SchemaKeyTable(
                    table="orders",
                    key_columns=["secret", "SECRET", "amount"],
                    display_columns=["Secret"],
                ),
            ])),
        )

        result = await schema_keys.detect_schema_keys(context)

        assert set(result) == {"orders"}
        # 未授权 amount 一律拒绝；“secret”只能归一为已授权的物理列 Secret。
        assert "amount" not in str(result)
        assert result["orders"]["display_columns"] == ["Secret"]
        assert "secret" not in result["orders"]["key_columns"]
        assert all(name == "Secret" for name in result["orders"]["key_columns"])

    # 方法作用：无列级策略时同表大小写变体并存，歧义名称拒绝、精确拼写照常接受。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_ambiguous_case_names_are_rejected(self) -> None:
        tables = {
            "orders": {
                "name": "orders",
                "columns": [
                    {"name": "Secret", "type": "TEXT"},
                    {"name": "secret", "type": "TEXT"},
                ],
                "relations": [],
            },
            "Orders": {
                "name": "Orders",
                "columns": [{"name": "id", "type": "INTEGER"}],
                "relations": [],
            },
        }
        output = SchemaKeyDetectionOutput(tables=[
            SchemaKeyTable(table="ORDERS", key_columns=["id"]),
            SchemaKeyTable(table="orders", key_columns=["SECRET", "secret"]),
        ])

        normalized = schema_keys.normalize_model_hints(output, tables)

        # 表名大小写歧义（Orders/orders）整体拒绝；列名歧义拒绝、精确保留。
        assert set(normalized) == {"orders"}
        assert normalized["orders"]["key_columns"] == ["secret"]

        # 回退的主键标记只落在真实拼写的那一列上，不合并大小写变体。
        tables["orders"]["columns"][0]["is_primary_key"] = True
        hints = schema_keys.fallback_key_hints(tables)

        assert hints["orders"]["key_columns"] == ["Secret"]

    # 方法作用：权限与结构指纹保留大小写，Secret/secret 策略不能共用缓存。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_case_declared_permissions_do_not_share_cache(self, monkeypatch) -> None:
        upper = SimpleNamespace(allowed_columns=["orders.Secret"], row_filter_sql="")
        lower = SimpleNamespace(allowed_columns=["orders.secret"], row_filter_sql="")

        assert schema_keys._permission_fingerprint(upper) != schema_keys._permission_fingerprint(lower)

        case_table = {
            "name": "t_workshop",
            "description": "",
            "columns": [
                {"name": "Secret", "type": "TEXT"},
                {"name": "secret", "type": "TEXT"},
            ],
            "relations": [],
        }
        assert schema_keys.authorized_structure_fingerprint(
            {"Orders": case_table},
        ) != schema_keys.authorized_structure_fingerprint({"orders": case_table})

        _patch_llm_availability(monkeypatch, available=True)
        invoke = AsyncMock(side_effect=RuntimeError("fallback"))
        monkeypatch.setattr(schema_keys, "_invoke_key_model", invoke)

        await schema_keys.detect_schema_keys(
            _context({"t_workshop": case_table}, allowed_columns=["Secret"]),
        )
        await schema_keys.detect_schema_keys(
            _context({"t_workshop": case_table}, allowed_columns=["secret"]),
        )

        # 仅授权列大小写不同也必须各自判断，不能命中同一缓存条目。
        assert invoke.await_count == 2
