"""文本日期列的值格式采样与 Prompt 渲染测试。

覆盖功能：内省阶段的值格式采样（格式识别、候选列筛选、方言 SQL、失败降级）
以及 SchemaSnapshot 的「值格式」标注渲染。

背景（回归来源）：Oracle 库中 orders.order_date 存 'YYYY-MM-DD'、payments.payment_time
存 'YYYY-MM-DD HH24:MI:SS'，模型统一用 TO_DATE(col,'YYYY-MM-DD') 时对后者报 ORA-01830。
采样识别的格式会写入 ColumnInfo.value_format 并出现在给模型的 schema 文本里。
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from src.datasource.introspection import (
    _base_column_type,
    _build_value_format_query,
    detect_value_format,
    is_date_like_text_column,
    sample_value_formats,
)
from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema

logger = logging.getLogger(__name__)


# 方法作用：构造带 engine.dialect 的最小数据源替身，供采样函数取方言用。
# Args: dialect_name - SQLAlchemy 方言名，如 "oracle"。
# Returns: 具备 engine.dialect.identifier_preparer.quote_identifier 的简单对象。
def _fake_ds(dialect_name: str = "oracle"):
    """只需 identifier_preparer 与 name 两个能力。"""
    preparer = SimpleNamespace(quote_identifier=lambda name: f'"{name}"')
    dialect = SimpleNamespace(name=dialect_name, identifier_preparer=preparer)
    return SimpleNamespace(engine=SimpleNamespace(dialect=dialect))


# 方法作用：构造返回固定行的异步执行器替身。
# Args: rows - 执行器每次调用返回的行列表。
# Returns: 符合 executor(ds, sql, params) 契约的异步函数，附带 calls 记录。
def _executor_returning(rows):
    """记录调用次数以便断言候选筛选是否生效。"""
    calls: list[str] = []

    async def _run(ds, sql, params):
        calls.append(sql)
        return rows

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


class TestValueFormatDetection:
    """覆盖 detect_value_format 的格式识别（正常路径 / 混合 / 空值 / 脏数据）。"""

    # 方法作用：验证纯日期格式识别。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_detect_plain_date(self) -> None:
        """'2026-09-13' → YYYY-MM-DD。"""
        logger.debug("test_detect_plain_date 入口")
        assert detect_value_format(["2026-09-13"]) == "YYYY-MM-DD"
        logger.info("test_detect_plain_date 完成")

    # 方法作用：验证带时间的格式优先于纯日期模式命中。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_detect_datetime(self) -> None:
        """'2026-09-13 14:30:00' → YYYY-MM-DD HH24:MI:SS（长模式优先，不被短模式抢占）。"""
        logger.debug("test_detect_datetime 入口")
        assert detect_value_format(["2026-09-13 14:30:00"]) == "YYYY-MM-DD HH24:MI:SS"
        logger.info("test_detect_datetime 完成")

    # 方法作用：验证纯时间列识别。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_detect_time_only(self) -> None:
        """'14:30:00' → HH24:MI:SS。"""
        logger.debug("test_detect_time_only 入口")
        assert detect_value_format(["14:30:00"]) == "HH24:MI:SS"
        logger.info("test_detect_time_only 完成")

    # 方法作用：验证年月格式识别。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_detect_year_month(self) -> None:
        """'2016-01' → YYYY-MM。"""
        logger.debug("test_detect_year_month 入口")
        assert detect_value_format(["2016-01"]) == "YYYY-MM"
        logger.info("test_detect_year_month 完成")

    # 方法作用：验证多行样例一致时正常识别。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_detect_consistent_samples(self) -> None:
        """全部样例同一格式 → 正常识别。"""
        logger.debug("test_detect_consistent_samples 入口")
        assert detect_value_format(["2026-09-13", "2027-01-01"]) == "YYYY-MM-DD"
        logger.info("test_detect_consistent_samples 完成")

    # 方法作用：验证格式混杂时返回空串（宁可不标注也不标错）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_detect_mixed_returns_empty(self) -> None:
        """同一列出现两种格式 → 空串，避免错误标注误导模型。"""
        logger.debug("test_detect_mixed_returns_empty 入口")
        assert detect_value_format(["2026-09-13", "2026-09-13 14:30:00"]) == ""
        logger.info("test_detect_mixed_returns_empty 完成")

    # 方法作用：验证全空样例返回空串。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_detect_all_empty_returns_empty(self) -> None:
        """全 NULL / 空串 → 空串。"""
        logger.debug("test_detect_all_empty_returns_empty 入口")
        assert detect_value_format([None, "", "   "]) == ""
        logger.info("test_detect_all_empty_returns_empty 完成")

    # 方法作用：验证非日期文本返回空串。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_detect_non_date_returns_empty(self) -> None:
        """普通文本 → 空串。"""
        logger.debug("test_detect_non_date_returns_empty 入口")
        assert detect_value_format(["华东区", "华南区"]) == ""
        logger.info("test_detect_non_date_returns_empty 完成")


class TestDateLikeTextColumn:
    """覆盖候选列筛选：类型必须是字符串族，列名必须含日期/时间提示。"""

    # 方法作用：验证典型文本日期列命中候选。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_text_date_column_is_candidate(self) -> None:
        """VARCHAR2 + order_date → 候选。"""
        logger.debug("test_text_date_column_is_candidate 入口")
        assert is_date_like_text_column("order_date", "VARCHAR2") is True
        assert is_date_like_text_column("payment_time", "VARCHAR2(19)") is True
        logger.info("test_text_date_column_is_candidate 完成")

    # 方法作用：验证 NVARCHAR2 年月列命中候选（跨域对齐场景）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_nvarchar2_year_month_is_candidate(self) -> None:
        """NVARCHAR2 + year_month → 候选。"""
        logger.debug("test_nvarchar2_year_month_is_candidate 入口")
        assert is_date_like_text_column("year_month", "NVARCHAR2") is True
        logger.info("test_nvarchar2_year_month_is_candidate 完成")

    # 方法作用：验证真实 DATE 类型不是候选（无需采样）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_real_date_type_not_candidate(self) -> None:
        """DATE 类型列不该被采样。"""
        logger.debug("test_real_date_type_not_candidate 入口")
        assert is_date_like_text_column("event_date", "DATE") is False
        logger.info("test_real_date_type_not_candidate 完成")

    # 方法作用：验证无日期提示的文本列不是候选（控制采样成本）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_plain_text_column_not_candidate(self) -> None:
        """VARCHAR2 + notes → 非候选。"""
        logger.debug("test_plain_text_column_not_candidate 入口")
        assert is_date_like_text_column("notes", "VARCHAR2") is False
        logger.info("test_plain_text_column_not_candidate 完成")

    # 方法作用：验证基类型剥离长度与精度。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_base_type_strips_length(self) -> None:
        """VARCHAR2(50) → varchar2；DECIMAL(18,2) → decimal。"""
        logger.debug("test_base_type_strips_length 入口")
        assert _base_column_type("VARCHAR2(50)") == "varchar2"
        assert _base_column_type("DECIMAL(18,2)") == "decimal"
        assert _base_column_type("") == ""
        logger.info("test_base_type_strips_length 完成")


class TestSampleQueryBuilding:
    """覆盖各方言采样 SQL 的行数限制语法。"""

    # 方法作用：验证 Oracle 使用 FETCH FIRST。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_oracle_uses_fetch_first(self) -> None:
        """Oracle 不支持 LIMIT。"""
        logger.debug("test_oracle_uses_fetch_first 入口")
        sql = _build_value_format_query("oracle", '"orders"', '"order_date"')
        assert "FETCH FIRST" in sql and "LIMIT" not in sql
        logger.info("test_oracle_uses_fetch_first 完成")

    # 方法作用：验证 MSSQL 使用 TOP。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_mssql_uses_top(self) -> None:
        """SQL Server 用 TOP n。"""
        logger.debug("test_mssql_uses_top 入口")
        sql = _build_value_format_query("mssql", "[orders]", "[order_date]")
        assert "TOP 5" in sql
        logger.info("test_mssql_uses_top 完成")

    # 方法作用：验证 MySQL/PG/ClickHouse 使用 LIMIT。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_limit_dialects(self) -> None:
        """常见方言用 LIMIT 5。"""
        logger.debug("test_limit_dialects 入口")
        for name in ("mysql", "postgres", "clickhouse"):
            assert "LIMIT 5" in _build_value_format_query(name, "t", "c")
        logger.info("test_limit_dialects 完成")

    # 方法作用：验证未知方言返回空串（调用方据此跳过采样）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_unknown_dialect_returns_empty(self) -> None:
        """未知方言不生成 SQL。"""
        logger.debug("test_unknown_dialect_returns_empty 入口")
        assert _build_value_format_query("duckdb", "t", "c") == ""
        logger.info("test_unknown_dialect_returns_empty 完成")


class TestSampleValueFormats:
    """覆盖采样的正常路径与降级路径。"""

    # 方法作用：验证采样识别出格式并写回结果字典。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_sample_returns_detected_format(self) -> None:
        """Oracle 采样 payment_time 的 19 位值 → YYYY-MM-DD HH24:MI:SS。"""
        logger.debug("test_sample_returns_detected_format 入口")
        # Arrange
        columns = [ColumnInfo(name="payment_time", type="VARCHAR2")]
        executor = _executor_returning([{"payment_time": "2026-09-13 14:30:00"}])

        # Act
        formats = asyncio.run(
            sample_value_formats(_fake_ds("oracle"), "payments", columns, executor)
        )

        # Assert
        assert formats == {"payment_time": "YYYY-MM-DD HH24:MI:SS"}
        logger.info("test_sample_returns_detected_format 完成")

    # 方法作用：验证无候选列时不发起任何查询。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_no_candidates_skips_query(self) -> None:
        """非日期文本列与数值列都不触发采样。"""
        logger.debug("test_no_candidates_skips_query 入口")
        columns = [
            ColumnInfo(name="notes", type="VARCHAR2"),
            ColumnInfo(name="total_amount", type="NUMBER"),
        ]
        executor = _executor_returning([{"x": 1}])

        formats = asyncio.run(
            sample_value_formats(_fake_ds("oracle"), "orders", columns, executor)
        )

        assert formats == {}
        assert executor.calls == []  # type: ignore[attr-defined]
        logger.info("test_no_candidates_skips_query 完成")

    # 方法作用：验证采样查询失败时降级为空字典，不向上抛异常。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_query_failure_degrades(self) -> None:
        """采样失败不能阻塞内省主流程。"""
        logger.debug("test_query_failure_degrades 入口")

        async def _failing(ds, sql, params):
            raise RuntimeError("ORA-00942: table or view does not exist")

        columns = [ColumnInfo(name="order_date", type="VARCHAR2")]
        formats = asyncio.run(
            sample_value_formats(_fake_ds("oracle"), "orders", columns, _failing)
        )

        assert formats == {}
        logger.info("test_query_failure_degrades 完成")

    # 方法作用：验证方言不可得时跳过采样。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_missing_dialect_skips(self) -> None:
        """延迟创建 Engine 的场景下无方言可取 → 跳过。"""
        logger.debug("test_missing_dialect_skips 入口")
        ds = SimpleNamespace(engine=None, connector=None)
        columns = [ColumnInfo(name="order_date", type="VARCHAR2")]
        executor = _executor_returning([{"order_date": "2026-09-13"}])

        formats = asyncio.run(sample_value_formats(ds, "orders", columns, executor))

        assert formats == {}
        assert executor.calls == []  # type: ignore[attr-defined]
        logger.info("test_missing_dialect_skips 完成")

    # 方法作用：验证格式混杂时该列不进入结果（不标注胜过标错）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_mixed_samples_not_reported(self) -> None:
        """同一列两种格式 → 不标注。"""
        logger.debug("test_mixed_samples_not_reported 入口")
        columns = [ColumnInfo(name="order_date", type="VARCHAR2")]
        executor = _executor_returning(
            [{"order_date": "2026-09-13"}, {"order_date": "2026-09-13 14:30:00"}]
        )

        formats = asyncio.run(
            sample_value_formats(_fake_ds("oracle"), "orders", columns, executor)
        )

        assert formats == {}
        logger.info("test_mixed_samples_not_reported 完成")


class TestPromptRendering:
    """覆盖 SchemaSnapshot 渲染时的值格式标注。"""

    # 方法作用：验证 value_format 出现在 Prompt 文本中。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_value_format_rendered_in_prompt(self) -> None:
        """带格式的列在说明列前置「值格式 X；」。"""
        logger.debug("test_value_format_rendered_in_prompt 入口")
        snapshot = SchemaSnapshot(
            tables=[
                TableSchema(
                    name="payments",
                    columns=[
                        ColumnInfo(
                            name="payment_time",
                            type="VARCHAR2",
                            comment="支付时间",
                            value_format="YYYY-MM-DD HH24:MI:SS",
                        )
                    ],
                )
            ]
        )

        text = snapshot.to_prompt_text()

        assert "值格式 YYYY-MM-DD HH24:MI:SS；" in text
        assert "支付时间" in text
        logger.info("test_value_format_rendered_in_prompt 完成")

    # 方法作用：验证无格式的列不产生多余标注（避免 Prompt 噪音）。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_absent_format_adds_no_note(self) -> None:
        """未识别格式的列保持原样。"""
        logger.debug("test_absent_format_adds_no_note 入口")
        snapshot = SchemaSnapshot(
            tables=[
                TableSchema(
                    name="orders",
                    columns=[ColumnInfo(name="total_amount", type="NUMBER")],
                )
            ]
        )

        text = snapshot.to_prompt_text()

        assert "值格式" not in text
        logger.info("test_absent_format_adds_no_note 完成")
