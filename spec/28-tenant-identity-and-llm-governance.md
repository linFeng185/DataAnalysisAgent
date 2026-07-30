# 租户身份与 LLM 治理设计

## 目标

在多租户部署中使用稳定的租户编码定位账号命名空间，并让每个租户独立维护自己的 LLM
连接、凭证、默认模型和对话级选择。平台超级管理员只维护租户生命周期以及平台支持的
模型厂商和模型目录，不持有或下发租户 API Key。

## 租户身份

### 租户编码

- `tenants.code` 是全局唯一、创建后不可修改的登录标识。
- 编码在写入和登录时统一规范化为小写，格式为
  `^[a-z0-9][a-z0-9-]{0,31}$`；默认租户编码固定为 `default`。
- 租户显示名称允许修改和重复展示，但当前 API 创建时仍拒绝同名，避免管理后台混淆。
- 数据库通过唯一索引和不可变触发器同时保护租户编码。

### 用户名与登录

- 登录请求固定为 `{tenant_code, username, password}`，三项缺一不可。
- 用户名区分大小写，同一租户使用 `(tenant_id, username)` 唯一约束；不同租户以及同租户内
  大小写不同的用户名均可合法存在。
- 登录查询先按规范化后的 `tenant_code` 精确定位租户，再按原始 `username` 精确匹配用户。
- 登录限流键为 `(client_ip, tenant_code, username)`，禁止跨租户同名账号互相消耗限额。
- JWT 继续保存 `tenant_id/user_id/role`，登录和 `/auth/me` 响应增加 `tenant_code`。
- 超级管理员使用 `default + super_admin_username + password` 登录。
- 公开注册永久关闭；兼容端点返回 403，前端不展示注册入口。

## 用户管理权限

### 超级管理员

- 创建、查看、启停租户，并在创建租户的同一事务中创建首个 `tenant_admin`。
- 维护平台 LLM 厂商和模型目录。
- 不通过通用用户管理接口查看、创建、修改或重置租户用户。

### 租户管理员

- 只能列出当前租户用户，服务端忽略任何来自请求体的租户归属声明。
- 可创建 `tenant_admin`、`analyst`、`viewer`，也可修改角色、启停账号和重置密码。
- 不能操作其他租户用户，不能创建 `super_admin`，不能修改固定超级管理员。
- 任意修改不得使当前租户失去最后一个启用的 `tenant_admin`。

## 平台 LLM 目录

### 厂商目录

`llm_provider_catalog` 由超级管理员维护：

| 字段 | 说明 |
|------|------|
| `code` | 厂商稳定编码，全局唯一 |
| `display_name` | 展示名称 |
| `protocol` | `openai_compatible` 或 `anthropic` |
| `default_base_url` | 可选默认请求地址 |
| `capability_schema` | 厂商级模型能力动态表单定义，字段包含 key/label/type/default/options/范围 |
| `is_active` | 是否允许租户新增连接 |

平台目录不保存 API Key。新增兼容 OpenAI 或 Anthropic 协议的厂商只需新增目录记录；新增全新
协议时实现 `LLMProvider` 并通过 `register_provider()` 注册，不修改业务节点。

### 模型目录

`llm_model_catalog` 从属于厂商，保存厂商侧 `model_id`、展示名称、按厂商动态表单验证后的能力对象、
是否启用。`(provider_id, model_id)` 唯一，同一模型标识可以出现在不同厂商目录中。管理端禁止要求
用户直接输入 JSON；厂商编辑器使用字段列表维护 `capability_schema`，模型创建和编辑表单按字段类型
动态渲染开关、数字输入、文本、单选或多选控件，服务端再次执行类型、必填、枚举、范围和未知字段校验。

### 物理删除

- 超级管理员可以物理删除模型和厂商，启停仅用于临时阻断，不能替代删除。
- 模型仍被 `tenant_llm_connection_models` 或 `tenant_llm_defaults` 引用时返回 409，禁止破坏租户配置。
- 厂商仍存在 `tenant_llm_connections` 时返回 409；无租户引用时，在同一事务先删除该厂商模型，再删除厂商。
- 删除接口只接受 `super_admin`，不存在的目录项返回 404，删除操作记录不含凭证的结构化日志。

## 租户 LLM 配置

### 命名连接

`tenant_llm_connections` 保存租户自己的命名连接：

- 同一租户可以为同一厂商创建多个连接，也可以创建不同厂商的多个连接。
- 每个连接分别保存 `name/base_url/encrypted_api_key/provider_id/is_active`。
- API Key 使用 `CredentialManager` 加密；读取 API 永不返回密文或明文，只返回
  `api_key_configured`。
- `(tenant_id, name)` 唯一；查询、测试、更新和删除始终强制当前租户过滤。
- 连接通过 `tenant_llm_connection_models` 启用平台目录中的一个或多个模型。

### 默认值与对话选择

- `tenant_llm_defaults` 保存当前租户默认连接和默认对话模型。
- `GET /models` 只返回当前租户已启用连接下的可用模型以及默认选择。
- `ChatRequest` 使用 `llm_connection_id + model_id` 表示对话级选择；两者都为空时使用租户默认值。
- `reasoning_enabled + reasoning_effort` 表示当前对话请求的统一推理偏好；关闭推理时禁止单独提交深度。
- 服务端只允许在模型 `capabilities.reasoning=true` 时开启推理，深度必须存在于
  `capabilities.reasoning_efforts`；前端禁用控件不能替代该授权校验。
- `analyst/viewer/tenant_admin` 都可在当前对话选择本租户可见模型，但不能修改连接配置。
- 显式选择不存在、已停用、跨租户或模型不属于连接时失败关闭，不回退到其他凭证。
- 多租户模式不存在平台全局 API Key 回退；单租户模式在未配置租户连接时保留 Settings 兼容路径。

## 统一调用链

1. API/SSE 边界根据认证身份、模型选择和推理偏好解析 `TenantLLMSelection`。
2. 解析结果只在请求级 ContextVar 和 AppContext 缓存中保存，不写入 LangGraph checkpoint，
   不记录 API Key。
3. `src/llm/invocation.py` 仍是 Prompt、结构化调用和流式调用的统一入口。
4. `get_task_llm()` 从当前租户选择创建 Provider；Provider 缓存键包含连接 ID、地址和凭证摘要。
5. `provider_registry` 只按协议创建实现，`ModelAdapter` 将统一推理偏好转换为厂商参数，业务节点禁止判断具体厂商或直接调用 SDK。
6. 租户连接变更后刷新当前 AppContext 缓存；单 worker 约束下立即生效。

### DeepSeek V4

- 平台初始目录只包含 `deepseek-v4-pro` 和 `deepseek-v4-flash`，旧 `deepseek-chat` 在迁移中将连接绑定和默认值转到 Pro 后物理删除。
- 官方地址为 `https://api.deepseek.com`；两个模型声明 1,000,000 Token 上下文、流式、JSON、工具调用和思考模式。
- 开启思考时 OpenAI-compatible 请求发送 `reasoning_effort=high|max` 与
  `extra_body.thinking.type=enabled`；关闭时显式发送 `disabled`。
- `low/medium` 兼容映射为 `high`，`xhigh` 映射为 `max`；管理端只展示实际有效的 `high/max`。
- 思考模式不发送 `temperature/top_p/presence_penalty/frequency_penalty` 等无效采样参数。
- 普通多轮不需要回传历史 `reasoning_content`；同一工具调用链以及后续用户轮次必须完整回传包含
  `reasoning_content/tool_calls` 的 assistant 消息，否则 DeepSeek 返回 400。
- 原始推理只用于协议续接和运行时解析，不进入公开响应、历史或 checkpoint。

## API

### 平台超级管理员

| 方法 | 端点 | 作用 |
|------|------|------|
| GET/POST | `/api/v1/admin/llm/providers` | 列出或创建厂商 |
| PATCH/DELETE | `/api/v1/admin/llm/providers/{provider_id}` | 修改或物理删除厂商 |
| GET/POST | `/api/v1/admin/llm/providers/{provider_id}/models` | 列出或创建模型 |
| PATCH/DELETE | `/api/v1/admin/llm/models/{model_id}` | 修改或物理删除模型 |

### 租户管理员

| 方法 | 端点 | 作用 |
|------|------|------|
| GET/POST | `/api/v1/admin/llm/connections` | 列出或创建当前租户连接 |
| PATCH/DELETE | `/api/v1/admin/llm/connections/{connection_id}` | 更新或删除当前租户连接 |
| POST | `/api/v1/admin/llm/connections/{connection_id}/test` | 最小请求测试连接 |
| PUT | `/api/v1/admin/llm/default` | 设置当前租户默认连接与模型 |

## 测试

- 租户编码全局唯一、不可修改、默认租户回填和用户名大小写敏感唯一约束；
- 同名用户跨租户登录、同租户大小写不同用户登录、限流键隔离和公开注册阻断；
- 租户管理员用户 CRUD、跨租户越权、角色边界和最后管理员保护；
- 平台厂商/模型目录的超级管理员边界和动态 OpenAI-compatible 模型；
- 厂商/模型物理删除、租户引用冲突、JSONB 字符串归一化和动态能力表单校验；
- DeepSeek V4 Pro/Flash 开关、深度映射、流式/非流式推理字段和工具调用回传；
- 对话推理开关、模型能力失败关闭、请求级 ContextVar 隔离和前端参数透传；
- 同厂商多连接、跨厂商连接、凭证加密/脱敏、默认值、对话覆盖和跨租户拒绝；
- 所有 LLM 节点仍经过 `src/llm/invocation.py`，单元测试统一使用 Fake LLM。
