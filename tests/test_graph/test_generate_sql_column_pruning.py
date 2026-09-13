"""generate_sql 表列裁剪的 Prompt 契约测试。

覆盖 2026-09-13 报表失败：问题命中一张表的任意一列后，旧实现只保留
required（主键/索引/关系键）| relevant（命中列），其余业务列被整体丢弃。
retail_orders 因此只剩下 id/event_date/order_status，模型据此判定
「白名单列中不存在 year_month、region_code、channel_code、scenario、net_amount」
并拒绝生成 SQL，整个销售单元失败。

契约要点：
1. 命中与未命中两条路径都必须保留「未命中但可用」的列；
2. 每表列数仍受上限约束，大表不能重新撑爆 Prompt；
3. 内部备注列不占用每表额度；
4. 裁剪不得修改传入的授权 Schema。
"""

from __future__ import annotations

from src.graph.nodes.generate_sql import (
    _PROMPT_COLUMNS_PER_TABLE,
    _prune_prompt_table_columns,
)

# 零售库的典型维度列，中文问题里没有对应词，旧实现会把它们整批裁掉。
_DIMENSION_COLUMNS = ("year_month", "region_code", "channel_code", "scenario", "net_amount")


# 方法作用：构造一张含主键与业务列的零售订单表。
# Args: extra_columns - 追加的列名列表。
# Returns: 表结构字典，字段形状与 relevant_tables 一致。
def _table(extra_columns: list[str] | None = None) -> dict:
    names = [
        "id",
        "event_date",
        "year_month",
        "region_code",
        "channel_code",
        "scenario",
        "gross_amount",
        "discount_amount",
        "net_amount",
        "order_status",
    ]
    names.extend(extra_columns or [])
    return {
        "name": "retail_orders",
        "description": "",
        "columns": [
            {
                "name": name,
                "type": "Int64",
                "comment": "",
                "is_nullable": True,
                "is_primary_key": name == "id",
                "is_indexed": name == "id",
            }
            for name in names
        ],
        "relations": [],
    }


class TestPromptColumnPruning:
    """覆盖表列裁剪必须保留未命中的业务列。"""

    # 方法作用：问题只命中少数列时，未命中的维度列不得被裁掉。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_unmatched_business_columns_survive(self) -> None:
        pruned = _prune_prompt_table_columns(_table(), "订单状态分布")
        names = {column["name"] for column in pruned["columns"]}
        assert set(_DIMENSION_COLUMNS) <= names

    # 方法作用：主键在裁剪后必须保留，供 JOIN 与去重使用。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_primary_key_always_kept(self) -> None:
        pruned = _prune_prompt_table_columns(_table(), "订单状态分布")
        assert "id" in {column["name"] for column in pruned["columns"]}

    # 方法作用：单表列数不得超过上限，避免宽表重新撑爆 Prompt。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_column_count_respects_cap(self) -> None:
        wide = _table([f"metric_{index}" for index in range(80)])
        pruned = _prune_prompt_table_columns(wide, "订单状态分布")
        assert len(pruned["columns"]) <= _PROMPT_COLUMNS_PER_TABLE

    # 方法作用：内部备注与调试列不得占用每表额度。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_internal_columns_excluded(self) -> None:
        pruned = _prune_prompt_table_columns(_table(["notes", "debug_trace"]), "订单状态分布")
        names = {column["name"] for column in pruned["columns"]}
        assert "notes" not in names
        assert "debug_trace" not in names

    # 方法作用：完全没有列命中时仍返回可解释的列集合，不产生空 Schema。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_no_match_keeps_visible_columns(self) -> None:
        pruned = _prune_prompt_table_columns(_table(), "zzz_qqq_unmatched")
        assert len(pruned["columns"]) >= 9

    # 方法作用：裁剪不得修改传入的授权 Schema（深拷贝契约）。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_source_table_not_mutated(self) -> None:
        source = _table()
        before = len(source["columns"])

        _prune_prompt_table_columns(source, "订单状态分布")

        assert len(source["columns"]) == before
