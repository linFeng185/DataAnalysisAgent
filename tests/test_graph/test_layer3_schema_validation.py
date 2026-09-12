"""layer3 两阶段 Schema 校验回归测试（spec/39 §6）。"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

logger = logging.getLogger(__name__)

# 阶段二复核用例共用的最小复现输入：created_at 被裁剪掉，阶段一必然报出 high issue。
STAGE_TWO_SQL = 'SELECT "i"."created_at" FROM "finance_invoices" AS "i"'
STAGE_TWO_DATASOURCE = "finance_db"
STAGE_TWO_DIALECT = "postgres"
STAGE_TWO_TRIMMED_TABLES = [
    {"name": "finance_invoices", "columns": [{"name": "event_date"}, {"name": "year_month"}]},
]


class TestLayer3TwoStageValidation:
    """覆盖两阶段取数与阶段二短路条件。"""

    # 方法作用：验证无数据源时阶段二被短路，不尝试连库。
    # Args: self - pytest 用例实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_stage_two_skipped_without_datasource(self, monkeypatch) -> None:
        """最小 state（无数据源）必须快速返回，不调用 SchemaManager。"""
        logger.debug("test_stage_two_skipped_without_datasource 入口")
        from src.graph.nodes.layer3_validate import layer3_validate_node

        # Arrange：若阶段二未被短路，下面的替身会让用例失败
        def _boom(*args, **kwargs):
            raise AssertionError("阶段二不应在无数据源时被调用")

        monkeypatch.setattr(
            "src.knowledge.schema_manager.get_schema_manager", _boom, raising=False
        )
        state = {
            "generated_sql": 'SELECT "i"."created_at" FROM "finance_invoices" AS "i"',
            "relevant_tables": [
                {"name": "finance_invoices", "columns": [{"name": "year_month"}]}
            ],
        }

        # Act
        result = asyncio.run(layer3_validate_node(state))

        # Assert
        assert result["sql_valid"] is False
        assert result["validation_errors"][0]["type"] == "column_reference"
        assert "created_at" in result["validation_errors"][0]["message"]
        logger.info("test_stage_two_skipped_without_datasource 完成")

    # 方法作用：验证拦截结果携带结构化 issues 供回喂重试使用。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_validation_errors_carry_structured_issues(self) -> None:
        """issues 必须含 table / available_columns，否则无法生成有效回喂。"""
        logger.debug("test_validation_errors_carry_structured_issues 入口")
        from src.graph.nodes.layer3_validate import layer3_validate_node

        # Arrange
        state = {
            "generated_sql": 'SELECT "i"."created_at" FROM "finance_invoices" AS "i"',
            "relevant_tables": [
                {
                    "name": "finance_invoices",
                    "columns": [{"name": "event_date"}, {"name": "year_month"}],
                }
            ],
        }

        # Act
        result = asyncio.run(layer3_validate_node(state))

        # Assert
        issues = result["validation_errors"][0]["issues"]
        assert issues[0]["table"] == "finance_invoices"
        assert "event_date" in issues[0]["available_columns"]
        logger.info("test_validation_errors_carry_structured_issues 完成")

    # 方法作用：验证节点采纳阶段二复核结果，假阳性被消除后最终放行。
    # Args: self - pytest 用例实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_layer3_stage_two_false_positive_is_cleared(self, monkeypatch) -> None:
        """阶段一在裁剪基准上报出的 high issue，经全量快照复核后必须被节点采纳清除。"""
        logger.debug("test_layer3_stage_two_false_positive_is_cleared 入口")
        from src.graph.nodes.layer3_validate import layer3_validate_node

        # Arrange：注入节点实际读取的上下文字段（routing.datasource / execution.dialect）
        contexts = SimpleNamespace(
            routing=SimpleNamespace(datasource=STAGE_TWO_DATASOURCE),
            execution=SimpleNamespace(dialect=STAGE_TWO_DIALECT),
        )
        monkeypatch.setattr(
            "src.graph.context.read_contexts", lambda state: contexts, raising=False
        )
        # 前置条件：阶段一基准是被裁剪过的表（缺 created_at），必然报出 high issue
        assert _stage_one_high_issues(STAGE_TWO_TRIMMED_TABLES), (
            "前置条件：裁剪快照必须让阶段一报出 high issue"
        )
        # 全量快照里 finance_invoices 含 created_at → 阶段二应消除该假阳性
        manager = _StubSchemaManager(
            _build_snapshot_stub(
                [("finance_invoices", ["event_date", "year_month", "created_at"])]
            )
        )
        monkeypatch.setattr(
            "src.knowledge.schema_manager.get_schema_manager",
            lambda: manager,
            raising=False,
        )
        state = {
            "generated_sql": STAGE_TWO_SQL,
            "relevant_tables": STAGE_TWO_TRIMMED_TABLES,
        }

        # Act
        result = asyncio.run(layer3_validate_node(state))

        # Assert：节点必须采纳复核结果（删掉采纳赋值这行，本用例会因 sql_valid 为 False 变红）
        assert result["sql_valid"] is True
        assert result["validation_errors"] == []
        logger.info("test_layer3_stage_two_false_positive_is_cleared 完成")

    # 方法作用：验证空基准时节点仍走阶段二，权威快照补回基准后合法 SQL 被放行。
    # Args: self - pytest 用例实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_layer3_rechecks_when_baseline_is_empty(self, monkeypatch) -> None:
        """relevant_tables 为空时不得静默放行，必须用权威快照复核（设计 §6）。"""
        logger.debug("test_layer3_rechecks_when_baseline_is_empty 入口")
        from src.graph.nodes.layer3_validate import layer3_validate_node

        # Arrange：阶段一无基准（relevant_tables 为空），但本轮有确定数据源
        contexts = SimpleNamespace(
            routing=SimpleNamespace(datasource=STAGE_TWO_DATASOURCE),
            execution=SimpleNamespace(dialect=STAGE_TWO_DIALECT),
        )
        monkeypatch.setattr(
            "src.graph.context.read_contexts", lambda state: contexts, raising=False
        )
        # 权威全量快照里有该表该列 → 复核后应放行
        manager = _StubSchemaManager(
            _build_snapshot_stub([("finance_invoices", ["created_at", "year_month"])])
        )
        monkeypatch.setattr(
            "src.knowledge.schema_manager.get_schema_manager",
            lambda: manager,
            raising=False,
        )
        state = {"generated_sql": STAGE_TWO_SQL, "relevant_tables": []}

        # Act
        result = asyncio.run(layer3_validate_node(state))

        # Assert：阶段二确实被触发（否则本用例空转），且合法 SQL 不被误杀
        assert manager.calls == [STAGE_TWO_DATASOURCE]
        assert result["sql_valid"] is True
        assert result["validation_errors"] == []
        logger.info("test_layer3_rechecks_when_baseline_is_empty 完成")

    # 方法作用：验证空基准 + 权威快照也无该表时拦截，空窗不再放行。
    # Args: self - pytest 用例实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_layer3_blocks_when_empty_baseline_recheck_finds_hallucination(
        self, monkeypatch
    ) -> None:
        """权威快照确认表不存在时，节点必须拦截（设计 §6 堵住空 tables 静默放行）。"""
        logger.debug(
            "test_layer3_blocks_when_empty_baseline_recheck_finds_hallucination 入口"
        )
        from src.graph.nodes.layer3_validate import layer3_validate_node

        # Arrange：阶段一无基准，权威快照里只有 orders，没有 SQL 引用的 finance_invoices
        contexts = SimpleNamespace(
            routing=SimpleNamespace(datasource=STAGE_TWO_DATASOURCE),
            execution=SimpleNamespace(dialect=STAGE_TWO_DIALECT),
        )
        monkeypatch.setattr(
            "src.graph.context.read_contexts", lambda state: contexts, raising=False
        )
        manager = _StubSchemaManager(_build_snapshot_stub([("orders", ["id"])]))
        monkeypatch.setattr(
            "src.knowledge.schema_manager.get_schema_manager",
            lambda: manager,
            raising=False,
        )
        state = {"generated_sql": STAGE_TWO_SQL, "relevant_tables": []}

        # Act
        result = asyncio.run(layer3_validate_node(state))

        # Assert：阶段二确认真幻觉 → 拦截走既有重试路由
        assert manager.calls == [STAGE_TWO_DATASOURCE]
        assert result["sql_valid"] is False
        assert result["validation_errors"][0]["type"] == "column_reference"
        logger.info(
            "test_layer3_blocks_when_empty_baseline_recheck_finds_hallucination 完成"
        )


# 方法作用：构造极简权威快照桩，只保留阶段二复核所需的 tables 属性。
# Args: tables - (表名, 列名序列) 二元组序列。
# Returns: 带 tables 属性的 SimpleNamespace 快照桩。
def _build_snapshot_stub(tables: list[tuple[str, list[str]]]) -> SimpleNamespace:
    logger.debug("构造快照桩入口", table_count=len(tables))
    result = SimpleNamespace(
        tables=[
            SimpleNamespace(
                name=name,
                columns=[SimpleNamespace(name=column) for column in columns],
            )
            for name, columns in tables
        ]
    )
    logger.info("构造快照桩完成", table_count=len(tables))
    return result


# 方法作用：在裁剪快照上复现阶段一校验，产出真实的 high 置信问题作为阶段二输入。
# Args: tables - 裁剪后的表结构（每张表含 name 与 columns）。
# Returns: 阶段一的 high 置信问题列表。
def _stage_one_high_issues(tables: list[dict]) -> list:
    from src.security.sql_schema_validation import validate_sql_schema

    logger.debug("复现阶段一校验入口", table_count=len(tables))
    issues = validate_sql_schema(
        STAGE_TWO_SQL,
        datasources=[STAGE_TWO_DATASOURCE],
        dialect=STAGE_TWO_DIALECT,
        tables=tables,
    )
    high = [item for item in issues if item.confidence == "high"]
    logger.info("复现阶段一校验完成", high_issue_count=len(high))
    return high


# 方法作用：提供阶段二复核用例专用的桩 SchemaManager，不触碰真实连接与缓存。
# Args: snapshot - 取数成功时返回的快照桩；error - 非空时改为抛出该异常。
# Returns: 具备异步 get_or_fetch_schema 的桩管理器实例。
class _StubSchemaManager:
    """阶段二复核用例专用桩，隔离真实 SchemaManager。"""

    # 方法作用：保存取数行为参数。
    # Args: snapshot - 成功时返回的快照桩；error - 需要模拟的取数异常。
    # Returns: 无返回值。
    def __init__(
        self, snapshot: SimpleNamespace | None = None, error: Exception | None = None
    ) -> None:
        self._snapshot = snapshot
        self._error = error
        # 记录每次取数的数据源名，供用例断言阶段二确实被触发
        self.calls: list[str] = []
        logger.debug(
            "桩 SchemaManager 构造",
            has_snapshot=snapshot is not None,
            error=type(error).__name__ if error else "",
        )

    # 方法作用：模拟 SchemaManager 取数，按构造参数返回快照或抛异常。
    # Args: datasource - 数据源名；user_query - 语义筛选问题（阶段二固定传空串）。
    # Returns: 快照桩；构造时给了异常则抛出。
    async def get_or_fetch_schema(
        self, datasource: str, user_query: str = ""
    ) -> SimpleNamespace | None:
        logger.debug("桩取数入口", datasource=datasource, user_query=user_query)
        self.calls.append(datasource)
        if self._error is not None:
            raise self._error
        logger.info("桩取数完成", datasource=datasource)
        return self._snapshot


class TestStageTwoRecheck:
    """覆盖阶段二复核的假阳性消除、确认与两条回落分支。"""

    # 方法作用：验证全量快照含该列时阶段一假阳性被消除（两阶段设计存在的理由）。
    # Args: self - pytest 用例实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_recheck_clears_false_positive(self, monkeypatch) -> None:
        """裁剪快照上的 high issue 必须被权威全量快照清零，避免误杀合法 SQL。"""
        logger.debug("test_recheck_clears_false_positive 入口")
        from src.graph.nodes.layer3_validate import _recheck_with_full_snapshot

        # Arrange：阶段一基准是被裁剪过的表（不含 created_at），故报出 high issue
        high_issues = _stage_one_high_issues(STAGE_TWO_TRIMMED_TABLES)
        assert high_issues, "前置条件：裁剪快照必须让阶段一报出 high issue"
        manager = _StubSchemaManager(
            _build_snapshot_stub(
                [("finance_invoices", ["event_date", "year_month", "created_at"])]
            )
        )
        monkeypatch.setattr(
            "src.knowledge.schema_manager.get_schema_manager",
            lambda: manager,
            raising=False,
        )

        # Act
        result = asyncio.run(
            _recheck_with_full_snapshot(
                STAGE_TWO_SQL, STAGE_TWO_DATASOURCE, STAGE_TWO_DIALECT, high_issues
            )
        )

        # Assert：全量快照确认该列存在，high 置信问题清零
        assert result is not None
        assert [item for item in result if item.confidence == "high"] == []
        logger.info("test_recheck_clears_false_positive 完成")

    # 方法作用：验证全量快照也没有该列时幻觉被确认保留。
    # Args: self - pytest 用例实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_recheck_confirms_real_hallucination(self, monkeypatch) -> None:
        """权威快照同样缺失该列时，high issue 必须原样保留，拦截不回退。"""
        logger.debug("test_recheck_confirms_real_hallucination 入口")
        from src.graph.nodes.layer3_validate import _recheck_with_full_snapshot

        # Arrange：全量快照里 finance_invoices 同样没有 created_at
        high_issues = _stage_one_high_issues(STAGE_TWO_TRIMMED_TABLES)
        manager = _StubSchemaManager(
            _build_snapshot_stub([("finance_invoices", ["event_date", "year_month"])])
        )
        monkeypatch.setattr(
            "src.knowledge.schema_manager.get_schema_manager",
            lambda: manager,
            raising=False,
        )

        # Act
        result = asyncio.run(
            _recheck_with_full_snapshot(
                STAGE_TWO_SQL, STAGE_TWO_DATASOURCE, STAGE_TWO_DIALECT, high_issues
            )
        )

        # Assert：幻觉被权威快照确认，问题保留且类型为未知列
        assert result is not None
        assert any(
            item.type == "unknown_column" and item.confidence == "high"
            for item in result
        )
        logger.info("test_recheck_confirms_real_hallucination 完成")

    # 方法作用：验证权威快照为空时返回 None，由调用方回落阶段一结果。
    # Args: self - pytest 用例实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_recheck_returns_none_on_empty_snapshot(self, monkeypatch) -> None:
        """快照为空时不得据此放行或拦截，必须返回 None 交由调用方回落。"""
        logger.debug("test_recheck_returns_none_on_empty_snapshot 入口")
        from src.graph.nodes.layer3_validate import _recheck_with_full_snapshot

        # Arrange：桩返回 tables=[] 的空快照
        high_issues = _stage_one_high_issues(STAGE_TWO_TRIMMED_TABLES)
        manager = _StubSchemaManager(_build_snapshot_stub([]))
        monkeypatch.setattr(
            "src.knowledge.schema_manager.get_schema_manager",
            lambda: manager,
            raising=False,
        )

        # Act
        result = asyncio.run(
            _recheck_with_full_snapshot(
                STAGE_TWO_SQL, STAGE_TWO_DATASOURCE, STAGE_TWO_DIALECT, high_issues
            )
        )

        # Assert
        assert result is None
        logger.info("test_recheck_returns_none_on_empty_snapshot 完成")

    # 方法作用：验证取数异常被吞掉并返回 None（fail-safe），不向上抛。
    # Args: self - pytest 用例实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_recheck_returns_none_on_exception(self, monkeypatch) -> None:
        """复核失败绝不能阻断链路：异常必须回落阶段一结果。"""
        logger.debug("test_recheck_returns_none_on_exception 入口")
        from src.graph.nodes.layer3_validate import _recheck_with_full_snapshot

        # Arrange：桩取数直接抛异常
        high_issues = _stage_one_high_issues(STAGE_TWO_TRIMMED_TABLES)
        manager = _StubSchemaManager(error=RuntimeError("db down"))
        monkeypatch.setattr(
            "src.knowledge.schema_manager.get_schema_manager",
            lambda: manager,
            raising=False,
        )

        # Act：若实现未吞异常，这里会直接抛 RuntimeError 让用例失败
        result = asyncio.run(
            _recheck_with_full_snapshot(
                STAGE_TWO_SQL, STAGE_TWO_DATASOURCE, STAGE_TWO_DIALECT, high_issues
            )
        )

        # Assert
        assert result is None
        logger.info("test_recheck_returns_none_on_exception 完成")
