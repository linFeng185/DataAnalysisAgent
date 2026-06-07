"""
LLM 调用层 — 大模型客户端 + 模型适配。

业务职责：
  封装所有与大模型的交互，提供统一的调用接口。
  通过「适配器模式」处理不同模型的参数差异：
  - DeepSeek：思考模式（reasoning_effort + extra_body）
  - OpenAI：标准 Chat Completions

核心模块：
  client.py         — LLM 工厂（get_llm / get_cheap_llm）
  prompts.py        — Prompt 模板（SQL 生成 / 分析 / 方言速查表）
  adapters/         — 模型适配器子包
"""
