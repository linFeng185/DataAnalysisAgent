"""本地 ONNX Runtime 嵌入后端，避开 transformers 与 torch 的重量级导入。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import numpy as np

from src.logging_config import get_logger

logger = get_logger(__name__)

# all-MiniLM-L6-v2 的最大序列长度，与 sentence-transformers 默认一致。
MAX_SEQ_LENGTH = 256
# 默认模型目录名，与 sentence-transformers 的仓库名一致。
DEFAULT_MODEL_DIR = "all-MiniLM-L6-v2"


# 方法作用：解析模型目录中的 ONNX 图与分词器文件路径。
# Args: model_path - 模型目录，可为空。
# Returns: (onnx 图路径, 分词器路径)。
def resolve_model_files(model_path: str = "") -> tuple[str, str]:
    """定位 onnx/model.onnx 与 tokenizer.json，缺失时失败关闭。"""
    logger.debug("嵌入模型文件解析入口", model_path=model_path)
    base = Path(model_path or DEFAULT_MODEL_DIR)
    onnx_path = base / "onnx" / "model.onnx"
    tokenizer_path = base / "tokenizer.json"
    if not onnx_path.is_file():
        logger.error("嵌入模型 ONNX 图缺失", path=str(onnx_path))
        raise FileNotFoundError(
            f"未找到 ONNX 模型文件：{onnx_path}。请确认 EMBEDDING_MODEL_PATH 指向 "
            f"sentence-transformers 模型目录，且该目录包含 onnx/model.onnx。"
        )
    if not tokenizer_path.is_file():
        logger.error("嵌入模型分词器缺失", path=str(tokenizer_path))
        raise FileNotFoundError(
            f"未找到分词器文件：{tokenizer_path}。请确认模型目录来自 "
            f"sentence-transformers 导出且未被裁剪。"
        )
    logger.info("嵌入模型文件解析完成", onnx=str(onnx_path), tokenizer=str(tokenizer_path))
    return str(onnx_path), str(tokenizer_path)


# 方法作用：对最后一层隐状态做 mask 加权平均池化并 L2 归一化。
# Args: hidden_state - (batch, seq, dim) 隐状态；attention_mask - (batch, seq) 掩码。
# Returns: (batch, dim) 的 float32 归一化向量。
def mean_pool_normalize(hidden_state: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    """与 sentence-transformers 的 mean pooling + normalize 语义保持一致。"""
    mask = attention_mask[..., None].astype(np.float32)
    summed = (hidden_state.astype(np.float32) * mask).sum(axis=1)
    counts = np.clip(mask.sum(axis=1), 1e-9, None)
    pooled = summed / counts
    norms = np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
    return (pooled / norms).astype(np.float32)


# 方法作用：构造 ONNX Runtime 会话选项。
# Args: ort - 已导入的 onnxruntime 模块。
# Returns: 开启图优化的会话选项。
def _session_options(ort: Any) -> Any:
    """开启全部图优化，减小首次推理延迟。"""
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.log_severity_level = 3
    logger.debug("ONNX 会话选项构造完成", graph_optimization="ORT_ENABLE_ALL")
    return options


# 方法作用：加载 ONNX 嵌入模型并返回批量编码函数。
# Args: model_path - 模型目录；空值回退到 all-MiniLM-L6-v2。
# Returns: 接受文本列表、返回等长向量列表的可调用对象。
def load_embed_fn(model_path: str = "") -> Callable[[list[str]], list[list[float]]]:
    """用 onnxruntime + tokenizers 加载，避开 transformers 与 torch 的导入成本。"""
    logger.debug("ONNX 嵌入后端加载入口", model_path=model_path)
    import onnxruntime as ort
    from tokenizers import Tokenizer

    onnx_path, tokenizer_path = resolve_model_files(model_path)
    tokenizer = Tokenizer.from_file(tokenizer_path)
    # 与 sentence-transformers 一致：补齐到 batch 内最长序列并按 [PAD] 编号填充。
    tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
    tokenizer.enable_truncation(max_length=MAX_SEQ_LENGTH)
    session = ort.InferenceSession(
        onnx_path,
        sess_options=_session_options(ort),
        providers=["CPUExecutionProvider"],
    )
    input_names = {spec.name for spec in session.get_inputs()}
    logger.info(
        "ONNX 嵌入模型加载完成",
        backend="onnxruntime",
        inputs=sorted(input_names),
        max_seq_length=MAX_SEQ_LENGTH,
    )

    # 方法作用：把文本列表编码为归一化向量列表。
    # Args: texts - 待编码文本列表。
    # Returns: 与输入等长的向量列表；空输入返回空列表。
    def encode(texts: list[str]) -> list[list[float]]:
        if not texts:
            logger.debug("ONNX 编码跳过空批次")
            return []
        encodings = tokenizer.encode_batch(list(texts))
        feed: dict[str, Any] = {
            "input_ids": np.array([item.ids for item in encodings], dtype=np.int64),
            "attention_mask": np.array([item.attention_mask for item in encodings], dtype=np.int64),
            "token_type_ids": np.array([item.type_ids for item in encodings], dtype=np.int64),
        }
        # 不同导出版本的输入集不同，按实际图输入裁剪，避免喂入多余张量。
        feed = {key: value for key, value in feed.items() if key in input_names}
        hidden_state = session.run(None, feed)[0]
        vectors = mean_pool_normalize(hidden_state, feed["attention_mask"])
        logger.debug("ONNX 编码完成", count=len(texts), dim=int(vectors.shape[1]))
        return [vector.tolist() for vector in vectors]

    return encode
