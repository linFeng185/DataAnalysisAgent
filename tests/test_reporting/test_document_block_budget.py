"""报表页面块预算与超限裁剪测试。

覆盖 2026-09-13 线上故障：9 个分析单元全部取数成功、确定性深挖产出 31 条发现，
页面块数 53 + 31 = 84 撞上 ReportDocument 里硬编码的 80 块上限，
report_validate_document 抛 ValidationError，把整份已成功取数的报表判为 failed。

契约要点：
1. 块预算必须随 report_max_units_per_run 缩放，不能写死；
2. 超预算时按追加顺序裁剪增量区段，而不是丢弃整份报表；
3. 裁空的区段连同导航锚点一并摘除，但 overview 首屏锚点必须保留。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.reporting.document import (
    ReportBlock,
    ReportDocument,
    ReportSection,
    truncate_document_to_block_budget,
)
from src.reporting.models import max_document_blocks, max_plan_units

# 单单元块预算与固定区段预留，与 models 内部取值保持一致，用于断言缩放公式。
_UNIT_BLOCK_BUDGET = 10
_FIXED_BLOCK_BUDGET = 40


# 方法作用：把分析单元数上限替换为指定值，隔离配置文件实际取值对断言的干扰。
# Args: monkeypatch - pytest 补丁夹具；units - 期望的单元数上限。
# Returns: 无返回值。
def _set_unit_limit(monkeypatch: pytest.MonkeyPatch, units: int) -> None:
    monkeypatch.setattr(
        "src.config.get_settings",
        lambda: SimpleNamespace(report_max_units_per_run=units),
    )


# 方法作用：构造包含指定块数的区段。
# Args: section_id - 区段标识；count - 块数量。
# Returns: 组装好的区段。
def _section(section_id: str, count: int) -> ReportSection:
    return ReportSection(
        id=section_id,
        title=section_id,
        blocks=[
            ReportBlock(section_id=section_id, type="narrative", content={"summary": "摘要"})
            for _ in range(count)
        ],
    )


# 方法作用：按各区段块数构造报表文档。
# Args: sizes - 各区段块数，列表索引即区段序号。
# Returns: 通过校验的报表文档。
def _document(sizes: list[int]) -> ReportDocument:
    sections = [_section(f"sec{index}", size) for index, size in enumerate(sizes)]
    return ReportDocument(
        report_id="11111111-1111-1111-1111-111111111111",
        sections=sections,
        navigation=["overview"] + [section.id for section in sections],
    )


class TestBlockBudgetScaling:
    """覆盖块预算随单元上限缩放的契约。"""

    # 方法作用：单元上限变化时块预算必须同步变化，且不低于历史下限 80。
    # Args: self - 测试实例；monkeypatch - 补丁夹具。
    # Returns: 断言通过时无返回值。
    def test_budget_scales_with_unit_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_unit_limit(monkeypatch, 1)
        assert max_plan_units() == 1
        assert max_document_blocks() == 80

        _set_unit_limit(monkeypatch, 36)
        assert max_document_blocks() == 36 * _UNIT_BLOCK_BUDGET + _FIXED_BLOCK_BUDGET
        assert max_document_blocks() > 80

    # 方法作用：回归故障场景——84 块（53 个单元块 + 31 条深挖发现）必须能通过校验。
    # Args: self - 测试实例；monkeypatch - 补丁夹具。
    # Returns: 断言通过时无返回值。
    def test_document_accepts_blocks_above_legacy_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_unit_limit(monkeypatch, 12)
        document = _document([53, 31])
        assert sum(len(section.blocks) for section in document.sections) == 84


class TestTruncateDocumentToBlockBudget:
    """覆盖超预算裁剪的顺序、计数与区段清理。"""

    # 方法作用：预算内裁剪必须是空操作，块数与区段数都不变。
    # Args: self - 测试实例；monkeypatch - 补丁夹具。
    # Returns: 断言通过时无返回值。
    def test_noop_within_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_unit_limit(monkeypatch, 1)
        document = _document([40, 40])

        assert truncate_document_to_block_budget(document) == 0
        assert [len(section.blocks) for section in document.sections] == [40, 40]
        assert len(document.navigation) == 3

    # 方法作用：超预算时从后往前裁，保留先出现的块（即线上场景裁掉末尾几条深挖发现）。
    # Args: self - 测试实例；monkeypatch - 补丁夹具。
    # Returns: 断言通过时无返回值。
    def test_truncates_tail_blocks_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_unit_limit(monkeypatch, 12)
        document = _document([53, 31])
        # 预算收紧到 80，需裁掉 4 块。
        _set_unit_limit(monkeypatch, 1)

        removed = truncate_document_to_block_budget(document)

        assert removed == 4
        assert [len(section.blocks) for section in document.sections] == [53, 27]
        assert len(document.navigation) == 3
        assert any("页面块" in item and "4" in item for item in document.limitations)

    # 方法作用：被裁空的区段连同导航锚点一并摘除，overview 首屏锚点必须保留。
    # Args: self - 测试实例；monkeypatch - 补丁夹具。
    # Returns: 断言通过时无返回值。
    def test_emptied_sections_dropped_and_navigation_pruned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_unit_limit(monkeypatch, 12)
        # 首段就吃掉全部预算，尾部的两段会被整段裁空。
        document = _document([90, 5, 5])
        _set_unit_limit(monkeypatch, 1)

        removed = truncate_document_to_block_budget(document)

        assert removed == 20
        assert [section.id for section in document.sections] == ["sec0"]
        assert len(document.sections[0].blocks) == 80
        assert document.navigation == ["overview", "sec0"]

    # 方法作用：裁剪结果必须仍满足文档自身的块数校验，不会留下二次失败。
    # Args: self - 测试实例；monkeypatch - 补丁夹具。
    # Returns: 断言通过时无返回值。
    def test_truncated_document_passes_validation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_unit_limit(monkeypatch, 12)
        document = _document([53, 31])
        _set_unit_limit(monkeypatch, 1)

        truncate_document_to_block_budget(document)

        revalidated = ReportDocument.model_validate(document.model_dump(mode="json"))
        assert sum(len(section.blocks) for section in revalidated.sections) == 80
