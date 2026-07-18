"""Phase B 结构化文件资产适配器测试。"""

from __future__ import annotations

import io

import pandas as pd
import pytest


class TestStructuredAssetAdapter:
    """覆盖 CSV/Excel/Parquet 的统一 profile 和资源边界。"""

    def test_inspect_csv_builds_profile_and_preview(self):
        """CSV 应生成行列、空值、唯一值、时间列和候选主键信息。"""
        # Arrange
        from src.knowledge.structured_assets import StructuredAssetAdapter

        content = (
            "order_id,order_date,amount,channel\n"
            "1,2026-01-01,10.5,web\n"
            "2,2026-01-02,20.0,app\n"
            "3,,15.0,web\n"
        ).encode("utf-8")

        # Act
        asset = StructuredAssetAdapter().inspect_bytes("orders.csv", content)

        # Assert
        assert asset.format == "csv"
        assert asset.row_count == 3
        assert asset.column_count == 4
        assert asset.preview[0]["order_id"] == 1
        assert asset.time_columns == ["order_date"]
        assert "order_id" in asset.candidate_primary_keys
        assert asset.columns["order_date"].null_count == 1

    def test_read_rows_respects_limit_and_rejects_empty(self):
        """读取行应受资源上限约束，空文件应给出可理解异常。"""
        # Arrange
        from src.knowledge.structured_assets import StructuredAssetAdapter, StructuredAssetError

        content = b"a,b\n1,x\n2,y\n3,z\n"

        # Act
        rows = StructuredAssetAdapter().read_rows("sample.csv", content, limit=2)

        # Assert
        assert len(rows) == 2
        assert rows[0] == {"a": 1, "b": "x"}
        with pytest.raises(StructuredAssetError, match="内容为空"):
            StructuredAssetAdapter().inspect_bytes("empty.csv", b"")

    def test_unsupported_format_and_optional_engine_error(self):
        """不支持的格式和缺失可选引擎必须明确失败。"""
        # Arrange
        from src.knowledge.structured_assets import StructuredAssetAdapter, StructuredAssetError

        adapter = StructuredAssetAdapter()

        # Act / Assert
        with pytest.raises(StructuredAssetError, match="不支持"):
            adapter.inspect_bytes("notes.txt", b"hello")
        with pytest.raises(StructuredAssetError, match="Parquet"):
            adapter.inspect_bytes("sample.parquet", b"not-parquet")

    def test_inspect_excel_returns_sheet_profiles_when_engine_available(self, tmp_path):
        """Excel 文件应按 sheet 返回独立 profile；环境缺少引擎时应明确提示。"""
        # Arrange
        from src.knowledge.structured_assets import StructuredAssetAdapter, StructuredAssetError

        buffer = io.BytesIO()
        try:
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                pd.DataFrame({"id": [1, 2], "value": [3.5, 4.5]}).to_excel(
                    writer, sheet_name="sales", index=False,
                )
        except (ImportError, ModuleNotFoundError):
            with pytest.raises(StructuredAssetError, match="Excel"):
                StructuredAssetAdapter().inspect_bytes("sales.xlsx", b"invalid")
            return

        # Act
        asset = StructuredAssetAdapter().inspect_bytes("sales.xlsx", buffer.getvalue())

        # Assert
        assert asset.format == "excel"
        assert asset.sheets["sales"].row_count == 2
        assert asset.columns["id"].numeric is True
