# 密钥托管与凭证轮换

## 范围与选型

对应 2.4.3、19.20。用户于 2026-09-06 授权自行选择密钥托管方案，DDL/CDC
变更通知暂不开发。选用与云厂商无关的 Vault KV v2；另提供只读 JSON 文件注入，
可对接 K8s Secret 或 Vault Agent。默认仍使用现有环境配置，不自动部署外部服务。

这里采用“托管主密钥 + 应用内 Fernet 加解密”，主密钥会进入应用内存；不是
Vault Transit/HSM 的不可导出密钥模式，也不代替数据库用户密码的服务端轮换。
Vault 的部署、解封、备份、审计及 Agent 认证由运维管理。应用只获得指定 KV 路径的
read 权限，不持有 root token，不授予写入或删除 Vault 密钥的能力。

## 配置及接口

- `secret_provider=environment|file|vault`，默认 `environment`。
- `secret_file`：只读 UTF-8 JSON 文件，内容与 Vault KV data 对象一致。
- `vault_addr`：生产强制 HTTPS，禁止 URL 凭证、查询参数、片段和重定向。
- `vault_token_file`：Agent 写入的令牌文件，每次读取重新打开，禁止写入日志。
- `vault_mount`、`vault_secret_path`：只允许非空路径段，不接受 `.`、`..`。
- `vault_namespace`、`vault_ca_file`：可选；TLS 校验不可关闭。
- `vault_secret_version`：0 读取最新版，大于 0 锁定 KV 版本。
- `vault_timeout_seconds`：连接与读取的有限超时。
- `load_secret_settings(settings)`：异步读取、白名单校验并返回新的 Settings；不修改
  原对象、不写入环境变量、不持久化明文。托管源拥有敏感字段的最高优先级。

托管文件必须包含非空 `credential_encryption_key`；允许 `credential_key_id`、
`credential_previous_keys`、`jwt_secret`、`database_url`、`admin_api_key`、
`super_admin_password`。不接受任意配置覆盖，尤其不能关闭认证、修改角色或开启调试。
字段、类型、密钥强度和完整生产配置在创建任何数据库池/后台服务前一起校验。
启用托管源后读取/解析失败在所有环境阻断启动，禁止回退本地旧密钥。

## 密文与轮换

- 旧数据继续读取固定 salt Fernet 和 `v2:salt:token`。
- 配置 `credential_key_id` 后，新写入 `v3:key_id:salt:token`。
- `credential_previous_keys` 是最多 5 个旧 key_id 到主密钥的映射，不允许复用当前 ID。
- v3 只按 ID 选择密钥，未知 ID 或格式损坏直接失败，禁止当明文使用。
- v2/旧 Fernet 按当前及保留旧密钥尝试读取，支持从现有部署平滑迁移。
- `python -m src.datasource.rotate_credentials` 默认仅核验；`--apply --offline-confirmed` 才写入。
  轮换 `datasource_configs.encrypted_password` 与
  `tenant_llm_connections.encrypted_api_key`，全事务、互斥锁、逐行锁、失败回滚、
  不输出密文或明文。维护数据库账号必须可以读取全部租户记录，否则拒绝执行。

轮换步骤：备份 → 停止所有应用/后台 Worker 的写入 → 在托管源发布新 ID/新密钥并
保留旧密钥 → dry-run → apply → 重启全部 Worker → 业务验证 → 保留旧密钥直至
数据库、备份和所有历史密文都超出恢复窗口，再由运维删除旧版本。工具不会自动
修改 Vault 或删除旧密钥，不在在线请求中执行跨租户轮换。

## 验证与权限

使用 httpx MockTransport 验证 KV v2 协议、HTTPS、拒绝重定向、超时/403/空数据、
令牌重新读取及无敏感日志。使用 pytest 验证新旧密文、未知 key_id、轮换原子性与
dry-run；真实 Vault 和真实 PG 验收单独说明，不以 Mock 验收替代部署验收。

密钥托管和存储加密轮换属于平台运维；租户的数据源密码及模型 API Key 更新仍由
原有租户权限控制，不新增超管读取租户凭证接口。

## 部署示例

非敏感环境配置示例（应用不自行创建 Vault）：

```dotenv
SECRET_PROVIDER=vault
VAULT_ADDR=https://vault.internal.example
VAULT_MOUNT=secret
VAULT_SECRET_PATH=data-agent/production
VAULT_TOKEN_FILE=/run/secrets/vault-token
VAULT_SECRET_VERSION=0
VAULT_CA_FILE=/run/secrets/vault-ca.pem
```

指定路径的 KV data 或 `SECRET_PROVIDER=file` 对应 `SECRET_FILE` 文件示例：

```json
{
  "credential_encryption_key": "<至少32字符的独立随机主密钥>",
  "credential_key_id": "release-2026-09",
  "credential_previous_keys": {},
  "jwt_secret": "<至少32字符的独立JWT签名密钥>",
  "database_url": "postgresql+asyncpg://<user>:<password>@<host>:5432/<database>"
}
```

占位符不可原样部署。Secret 文件和 token 文件必须只允许应用账号读取；不得提交
到 Git。Vault 使用 Agent/工作负载身份提供短期 token；应用只读该文件，不自行续租。
JWT 签名密钥改变会使旧 token 失效，因此不要将它和存储凭证密钥随意同时轮换。
