"""ONNX Runtime 嵌入后端测试，覆盖 spec/46。

事实依据：pytest 经 tests/conftest.py 与 tests/test_memory/__init__.py 收集；
tests/test_memory/ 既有文件覆盖 long_term_store / vector_store_milvus 等主题，
无嵌入后端主题；本文件只做 numpy 数学断言与路径解析断言，不读写数据文件，
不加载真实大模型（真实模型一致性由本地模型可用时另行验证）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.memory.embedding_backend import (  # noqa: E402
    mean_pool_normalize,
    resolve_model_files,
)


class TestMeanPoolNormalize:
    """验证池化与归一化的数学行为。"""

    # 方法作用：验证池化只对未被 mask 的位置求平均。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_pooling_ignores_masked_positions(self) -> None:
        """padding 位置必须被排除，否则短句向量会被拉偏。"""
        # Arrange
        hidden = np.array([[[1.0, 1.0], [3.0, 3.0], [99.0, 99.0]]], dtype=np.float32)
        mask = np.array([[1, 1, 0]], dtype=np.int64)

        # Act
        result = mean_pool_normalize(hidden, mask)

        # Assert
        expected = np.array([2.0, 2.0]) / np.linalg.norm([2.0, 2.0])
        assert np.allclose(result[0], expected, atol=1e-6)

    # 方法作用：验证输出已做 L2 归一化。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_output_is_l2_normalized(self) -> None:
        """归一化后模长必须为 1，否则余弦检索会失真。"""
        # Arrange
        hidden = np.random.default_rng(0).random((3, 5, 8)).astype(np.float32)
        mask = np.ones((3, 5), dtype=np.int64)

        # Act
        result = mean_pool_normalize(hidden, mask)

        # Assert
        norms = np.linalg.norm(result, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-5)

    # 方法作用：验证 padding 长度不影响结果。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_padding_length_does_not_change_result(self) -> None:
        """同一条文本在不同 batch 里长度不同，向量必须一致。"""
        # Arrange
        core = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], dtype=np.float32)
        short_hidden = core[None, :, :]
        short_mask = np.array([[1, 1]], dtype=np.int64)
        long_hidden = np.concatenate(
            [core, np.zeros((2, 3), dtype=np.float32)], axis=0)[None, :, :]
        long_mask = np.array([[1, 1, 0, 0]], dtype=np.int64)

        # Act
        short_result = mean_pool_normalize(short_hidden, short_mask)
        long_result = mean_pool_normalize(long_hidden, long_mask)

        # Assert
        assert np.allclose(short_result, long_result, atol=1e-6)

    # 方法作用：验证全零 mask 不产生除零异常。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_all_zero_mask_is_safe(self) -> None:
        """脏输入不得让检索链路崩溃。"""
        # Arrange
        hidden = np.zeros((1, 3, 4), dtype=np.float32)
        mask = np.zeros((1, 3), dtype=np.int64)

        # Act
        result = mean_pool_normalize(hidden, mask)

        # Assert
        assert result.shape == (1, 4)
        assert np.all(np.isfinite(result))

    # 方法作用：验证输出形状与 dtype 契约。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_output_contract(self) -> None:
        """编码结果形状与 dtype 必须供 VectorEntry 直接消费。"""
        # Arrange
        hidden = np.random.default_rng(1).random((2, 4, 6)).astype(np.float32)
        mask = np.ones((2, 4), dtype=np.int64)

        # Act
        result = mean_pool_normalize(hidden, mask)

        # Assert
        assert isinstance(result, np.ndarray)
        assert result.shape == (2, 6)
        assert result.dtype == np.float32


class TestResolveModelFiles:
    """验证模型文件定位与错误提示。"""

    # 方法作用：验证完整目录能解析出两个必需文件。
    # Args: self - 测试实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_resolves_onnx_and_tokenizer(self, tmp_path: Path) -> None:
        """标准 sentence-transformers 目录结构必须被识别。"""
        # Arrange
        (tmp_path / "onnx").mkdir()
        (tmp_path / "onnx" / "model.onnx").write_bytes(b"stub")
        (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")

        # Act
        onnx_path, tokenizer_path = resolve_model_files(str(tmp_path))

        # Assert
        assert onnx_path.endswith("model.onnx")
        assert tokenizer_path.endswith("tokenizer.json")

    # 方法作用：验证 ONNX 文件缺失时给出可操作的错误。
    # Args: self - 测试实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_missing_onnx_raises_actionable_error(self, tmp_path: Path) -> None:
        """缺失必须显式失败并指出期望路径，不能静默回退。"""
        # Act / Assert
        with pytest.raises(FileNotFoundError) as caught:
            resolve_model_files(str(tmp_path))
        assert "onnx" in str(caught.value).lower()

    # 方法作用：验证 tokenizer 缺失时同样报错。
    # Args: self - 测试实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_missing_tokenizer_raises(self, tmp_path: Path) -> None:
        """缺少分词器无法编码，必须失败关闭。"""
        # Arrange
        (tmp_path / "onnx").mkdir()
        (tmp_path / "onnx" / "model.onnx").write_bytes(b"stub")

        # Act / Assert
        with pytest.raises(FileNotFoundError) as caught:
            resolve_model_files(str(tmp_path))
        assert "tokenizer" in str(caught.value).lower()
