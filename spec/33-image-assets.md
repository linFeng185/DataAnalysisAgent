# 图片资产分析

对应 19.4。复用 `ChatRequest.file_ids`、FileStore、文件分析子图和请求级附件工具，
不创建第二套上传服务。结构化文件继续使用已有临时查询/画像适配器，文档继续有界提取。

PNG/JPEG/WebP 图片作为私有聊天附件上传；拒绝 SVG、GIF、动画、伪装或损坏文件。
单张最大 5 MiB、原始最多 1600 万像素、最长边压缩至 2048；一次请求最多 3 张图片。
Pillow 为可选 `vision` 依赖。先核验格式/尺寸，再解码；重新编码为无 EXIF/XMP 的
JPEG 发送给模型。原图仅在既有 FileStore 中按上传身份存储，不进入 Graph state、
工具返回值、日志或向量索引。相同图片在同一请求内复用视觉结果，读取时仍重验可见性。

视觉调用要求已授权的 TenantLLMSelection 且 `capabilities.vision=true`，租户必须与
文件分析请求一致；不自动换模型或改用外部 OCR。模型走 `src/llm/client.py` 工厂，
Prompt 注册到集中注册表，并走统一文字预算准备；图片使用独立字节/像素预算。
调用超时 30 秒，输出最多 2048 tokens、结构化文本有界；图片调用临时关闭 LangSmith
追踪，不改变全局配置。单测只使用 Fake LLM，不调用远程付费模型。

输出包含 ImageAnalysisOutput（summary/observations/extracted_text/limitations）、
DataAsset(kind=image)、原图 SHA-256、file_id 及来源引用；注明视觉识别可能错误，
图中指令不被当成系统指令，不执行图中 SQL/脚本。不保证精确 OCR 或自动恢复原始表格。
聊天页面提示图片交给当前所选视觉模型分析；原有文件白名单和租户权限不被放宽。

模型能力目录属于平台基础配置：厂商能力表单新增 `vision` 布尔字段并给确实支持视觉
输入的模型赋 true；租户自行选择并授权连接。缺少能力声明时明确拒绝，不猜模型能力。
