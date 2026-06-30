---
name: systematic-debugging
description: Use when encountering ANY bug, test failure, unexpected behavior, or production issue. Mandatory debugging protocol — apply BEFORE writing any fix code. Covers data pipeline, API, frontend, database, LLM, workflow, and serialization bugs.
---

# 系统调试协议

## 核心铁律

**禁止在未定位根因之前写任何修复代码。** 本次会话暴露的典型反模式：「扫文档 → 猜测根因 → 大规模重写 → 发现不对 → 再猜 → 再改」，浪费多轮对话。必须改为：「加诊断探针 → 跑一次 → 读日志 → 精准定位消失点 → 最小修复」。

## 强制流程（所有 bug 适用）

```
收到 bug 报告
  │
  ├─ Step 1: 拿到复现数据
  │   前端 bug → curl + 浏览器控制台日志
  │   后端 bug → 完整服务器日志 + 请求体
  │   数据 bug → 输入数据 + 期望输出 + 实际输出
  │
  ├─ Step 2: 加诊断探针（在怀疑链路的每一层边界上加 log）
  │   原则：宁可多加 10 行 log 也不要少加 1 行
  │   探针格式：logger.info("位置描述", 关键变量=值, 状态=值)
  │
  ├─ Step 3: 跑一次复现
  │   观察：数据在哪一步从"正确"变成了"错误"？
  │   观察：哪个中间状态和预期不符？
  │
  ├─ Step 4: 定位消失点后，在该层检查 4 个常见根因
  │   a) 输入是否正确到达？（参数传递 / 序列化 / 默认值覆盖）
  │   b) 执行路径是否符合预期？（条件路由 / 并行冲突 / 异常被吞）
  │   c) 输出是否正确写回？（字段覆盖 / 类型不匹配 / reducer 行为）
  │   d) 下次读取是否正确恢复？（反序列化丢失 / 缓存 / 单例状态）
  │
  └─ Step 5: 最小修复 + 日志确认修复生效
```

## 本项目常见问题速查

| 症状 | 先检查 | 常见根因 |
|------|--------|---------|
| 多轮对话上下文丢失 | `classify_intent` 入口打 `conversation_history` 长度 | MemorySaver 不持久化普通字段 → 转 `messages` |
| SQL 执行返回空 | `execute_sql` 打 `resolve_or_none` 返回值 | 连接配置不可达 / `resolve_or_none` 只捕特定异常 |
| 节点被执行多次 | `chain_start/end` 计数 | workflow 中固定边 + 条件边冲突 |
| SSE 事件前端没收 | `streaming.py` 打事件 type | 事件类型未在 `switch-case` 中处理 |
| 数据源列表为空 | `list_all` 打 provider 名称 | 直接注入 cache 的源不被 `list_all` 返回 |
| checkpoint 警告 | 搜 `Deserializing unregistered type` | 自定义类型需注册到 serde 或改用 dict |
| LLM 输出与上下文无关 | `generate_sql` 打 prompt 最后 200 字符 | `context_text` 为空 → `conversation_history` 未传入 |
| 会话总是新建 | `streaming.py` 入口打 session_id | 前端 session_id 为空字符串 |

## 诊断日志模板

```python
# 在怀疑链路的每一层加：

# API 入口
logger.info("请求到达", session_id=x, query=q[:60], datasource=d)

# 节点入口
logger.info("节点开始", node=__name__)
ch = state.get("conversation_history", []) or []
logger.info("状态快照", keys=list(state.keys())[:10], history_turns=len(ch))

# 关键操作后
logger.info("写入完成", field_name="xxx", value_preview=str(val)[:100])

# 链路结束
logger.info("流式完成", chain_start=S, chain_end=E)
```

## 禁止清单

| 反模式 | 后果                  | 本次实例 |
|--------|---------------------|---------|
| 先大规模重构再诊断 | 浪费时间，引入新 bug        | 重写了整个前端才发现 bug 在后端 |
| 猜测根因直接改代码 | 改了 几十个 个地方都不是根因     | 先改 `resolve_or_none`，再改 `ConversationTurn`，再改 workflow |
| 不加日志就改第二轮 | 无法确认修改是否生效          | 每次改完等用户重启看日志 |
| 只修一个层面不查其他 | 修完 A 层发现 B 层也坏了     | 修了序列化发现并行分叉也导致覆盖 |
| 信任框架的"应该会持久化" | MemorySaver 选择性丢弃字段 | conversation_history 写入成功但加载为空 |

## 完成标准

修复声称完成前必须满足：
- 诊断日志显示数据在每一层都正确传递
- 复现路径跑通且结果符合预期
- 修改的文件数 ≤ 该层需要改的最少文件数（如果 > 5 个文件，说明你在重构而非修复）
