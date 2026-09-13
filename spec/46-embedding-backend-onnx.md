# 嵌入后端切换 ONNX Runtime 设计

把本地句向量推理从 `sentence-transformers`（PyTorch 后端）切到 `onnxruntime` + `tokenizers`，消除服务启动时由 `transformers` 依赖链导入造成的固定延迟。不改变向量语义，不新增依赖。

## 1. 问题与实测根因

服务启动日志中「VectorStore 类型: Milvus」到「嵌入模型加载完成」之间有 **27.3 秒**固定延迟（`src/memory/vector_store_milvus.py` 的 `_warmup_knowledge` 阶段）。

分离实测（同一台机器，`all-MiniLM-L6-v2`）：

| 阶段 | 耗时 |
|------|------|
| `SentenceTransformer(path)` 真正加载模型权重 | **0.11s** |
| `import sentence_transformers` | **5.42s** |
| ├─ `transformers` 依赖链 | ~3.0s |
| └─ `torch` | ~1.1s |

结论：**慢的不是模型加载，而是 HuggingFace `transformers` 的顶层 `__init__` eager 导入**，属该库的已知行为。

PyCharm 调试模式（pydevd 对每次模块导入加 hook）把 5.5 秒放大到 27 秒，约 5 倍。这是**一次性启动成本**，服务就绪后模型常驻内存，不影响单次查询耗时。

## 2. 设计

新增 `src/memory/embedding_backend.py` 作为**两个向量后端共享**的嵌入模块（`MilvusVectorStore` 与 `PgVectorStore` 原先各自实现同一段 `SentenceTransformer` 加载逻辑）：

```
resolve_model_files(model_path) -> (onnx_path, tokenizer_path)
mean_pool_normalize(hidden_state, attention_mask) -> np.ndarray
load_embed_fn(model_path) -> Callable[[list[str]], list[list[float]]]
```

**实现要点**：

1. 用 `tokenizers.Tokenizer` 读取模型目录的 `tokenizer.json`，启用 `enable_padding(pad_id=0, pad_token="[PAD]")` 与 `enable_truncation(max_length=256)`，与 sentence-transformers 的默认行为对齐。
2. 用 `onnxruntime.InferenceSession` 加载 `onnx/model.onnx`，开启 `ORT_ENABLE_ALL` 图优化。
3. 按 ONNX 图实际声明的输入名裁剪喂入张量（不同导出版本输入集不同），避免多余张量报错。
4. 池化与归一化：`attention_mask` 加权平均池化后做 L2 归一化，与 sentence-transformers 的 `mean pooling + normalize_embeddings=True` 语义一致。
5. **失败关闭**：`onnx/model.onnx` 或 `tokenizer.json` 缺失时抛 `FileNotFoundError`，错误信息包含期望路径与修复提示，不静默回退到旧后端。
6. `onnxruntime` 与 `tokenizers` 仍在函数内惰性导入，保持「重活不占事件循环」的既有约定。

**编码接口契约**（两个后端一致）：接受 `list[str]`，返回等长的 `list[list[float]]`。单条编码通过 `encode([text])[0]` 完成，不再传递 `show_progress_bar` 参数（ONNX 路径不存在 tqdm 输出）。

## 3. 数值一致性验证

同一份文本分别经新旧后端编码（`all-MiniLM-L6-v2`，3 条中英文样本）：

| 指标 | 结果 |
|------|------|
| 余弦相似度 | `1.00000012` / `1.00000007` / `1.00000007` |
| 最大绝对差 | `1.71e-07` |
| 新后端向量模长 | `1.000000` |
| 维度 | `384` |

差异为 float32 累加顺序导致的浮点噪声，**检索语义不变**。

## 4. 性能收益

| 指标 | 旧（sentence-transformers） | 新（ONNX Runtime） |
|------|------------------------------|---------------------|
| 模块导入 | 5.42s | **0.12s** |
| 模型加载 | 0.11s | 0.19s |
| **启动合计** | **5.53s** | **0.31s** |
| PyCharm 调试模式估算 | 27s | 约 2s |
| 批量编码 3 条 | — | 4.7ms |

## 5. 依赖

`onnxruntime`、`tokenizers` 与 ONNX 模型文件（`onnx/model.onnx`）**均已存在于当前部署环境**，本次改动不新增任何依赖声明。`sentence-transformers` 仍保留在 `pyproject.toml` 中——测试与数值比对脚本仍需它作为参照实现。

## 6. 测试

| 测试文件 | 覆盖内容 |
|----------|----------|
| `tests/test_memory/test_embedding_backend.py` | 池化忽略 padding 位；输出 L2 归一化；padding 长度不影响结果；全零 mask 不除零；输出形状与 dtype 契约；模型文件解析成功路径与两类缺失路径的报错 |
| `tests/test_memory/test_vector_store_milvus.py` | 单条编码走批量接口且只调用一次；upsert 缺失向量合并为一次 encode |
| `tests/test_memory/test_vector_store_pg.py` | 模型初始化和 CPU 推理均离开事件循环 |
| `tests/test_memory/test_vector_store_milvus_async.py` | 嵌入后端导入发生在工作线程，且并发首次请求只初始化一次 |

测试替身由 `SentenceTransformer` 改为 `embedding_backend.load_embed_fn`，断言意图（批量、离线程）保持不变。

## 7. 回退

若需退回 PyTorch 后端：把 `MilvusVectorStore._get_embed_fn` 与 `PgVectorStore._get_embed_fn` 的 `load_embed_fn(path)` 调用换回 `SentenceTransformer(path).encode`，并恢复编码接口的 `.tolist()` 消费方式即可。**已在 Milvus 中写入的向量无需重算**——新旧后端输出数值等价（见第 3 节）。
