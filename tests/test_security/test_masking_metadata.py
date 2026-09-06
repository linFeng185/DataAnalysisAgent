"""覆盖 18.8.1 的字段级脱敏元数据和向后兼容。"""

from copy import deepcopy

import pytest

from src.security.data_masker import (
    mask_sensitive_data,
    mask_sensitive_data_with_metadata,
    merge_masking_metadata,
)


class TestMaskingMetadata:
    """覆盖功能 18.8.1：真实脱敏事件、空输入、无敏感信息与重复处理。"""

    # 方法作用：验证字段和内容规则均产出不含原始值的元数据。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_masking_metadata_records_fields(self) -> None:
        """脱敏结果兼容旧接口，计数和类别仅反映实际处理的单元格。"""
        rows = [{"phone": "13812345678", "note": "user@example.com", "value": 3}]
        before = deepcopy(rows)
        masked, metadata = mask_sensitive_data_with_metadata(rows)
        assert masked == mask_sensitive_data(rows)
        assert masked[0]["phone"] != rows[0]["phone"]
        assert rows == before
        assert metadata["applied"] is True
        assert {field["column"] for field in metadata["fields"]} == {"phone", "note"}
        assert all(field["masked_count"] == 1 for field in metadata["fields"])
        assert "13812345678" not in str(metadata)
        assert "user@example.com" not in str(metadata)

    # 方法作用：验证空值、普通字符串和星号文本不会被误报。
    # Args: self - 实例；rows - 不触发脱敏规则的数据。
    # Returns: 无返回值。
    @pytest.mark.parametrize("rows", [[], [{"phone": None}], [{"note": "hello***", "count": 0}]])
    def test_non_sensitive_and_empty_no_badge(self, rows) -> None:
        """前端标志必须来自处理事件，不能从星号猜测。"""
        masked, metadata = mask_sensitive_data_with_metadata(rows)
        assert masked == rows
        assert metadata == {"applied": False, "fields": []}

    # 方法作用：验证同一列的多个敏感单元格被聚合。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_multiple_rows_aggregate_counts(self) -> None:
        """元数据按列聚合，不携带逐行身份或敏感值。"""
        rows = [{"email": "one@example.com"}, {"email": "two@example.com"}, {"email": None}]
        _, metadata = mask_sensitive_data_with_metadata(rows)
        assert len(metadata["fields"]) == 1
        assert metadata["fields"][0]["masked_count"] == 2

    # 方法作用：验证异常输入继续按显式异常处理，不伪造正常脱敏结果。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_invalid_rows_rejected(self) -> None:
        """行必须是映射，非法输入不静默丢弃。"""
        with pytest.raises((TypeError, AttributeError)):
            mask_sensitive_data_with_metadata([None])

    # 方法作用：验证不同来源的同名敏感字段能够聚合而不丢类别。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_merge_sources_preserves_counts(self) -> None:
        """多源只合并元数据，输入为空时维持无标识状态。"""
        first = mask_sensitive_data_with_metadata([{"email": "one@example.com"}])[1]
        second = mask_sensitive_data_with_metadata([{"email": "two@example.com"}])[1]
        merged = merge_masking_metadata([first, second])
        assert merged["fields"][0]["masked_count"] == 2
        assert merged["applied"] is True
        assert merge_masking_metadata([]) == {"applied": False, "fields": []}
