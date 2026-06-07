"""
Web 接口层 — HTTP 请求/响应处理。

业务职责：
  将用户的分析请求（自然语言 + 数据源名称）通过 REST API 接收，
  分发给 LangGraph 分析流水线，并以 JSON 或 SSE 流式格式返回结果。

核心模块：
  routes.py   — API 端点定义（/chat、/health、/schema、/datasources）
  schemas.py  — Pydantic 请求/响应模型
  streaming.py— SSE 流式输出，实时推送执行进度和 LLM token
  middleware.py— 自定义异常 → HTTP 状态码映射
"""
