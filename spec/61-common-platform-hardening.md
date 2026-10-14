# 61. 通用平台加固（工具输出、认证故障、日志作用域、迁移前序与知识版本读取）

> 本文件记录 2026-10-14 经审计通过的通用能力迁入 main 的具体接口与边界；
> 不含筛选/交易理论/市场路由、股票通知、上传发布 API/前端等专用能力。
> 功能台账见 features/36-common-platform-hardening.md。

## 1. 目的与范围

本轮把审计底座的五类通用能力迁入 main：工具输出兼容规则、认证依赖故障 503 边界、依赖异常日志 ContextVar 作用域、迁移默认排除 900+ 与 connection_setup 前序回调、知识文件版本存储与精确原文读取。迁入实现与该底座逐行一致（仅换行归一），main 原有代码完整保留。

## 2. 工具输出兼容（展示层规则）

- 规则条款写入 `AGENTS.md`《工具输出兼容规则》：任何文件、日志、测试金标与子代理回执等输出前先做 JSON 安全编码，并把真实 U+2028/U+2029 替换为字面转义序列（反斜杠加 u2028/u2029 的纯 ASCII 文本形式）；禁止原样整文件转储，不输出凭证、完整配置或业务正文。
- 仅改变展示层：不修改源码字符、测试金标、golden vectors、哈希或已落盘契约。
- 未经原始故障（Failed to parse item）复验，不得宣称已根治。

## 3. 认证依赖故障边界（src/api/auth.py）

- `AuthMiddleware._authenticate_request`：数据库身份复核依赖 `load_current_identity` 异常（超时等）时返回 HTTP 503「认证服务暂不可用」，不视为登录失效；503 响应不下发删除 access_token 的 Set-Cookie，浏览器会话保留。
- `asyncio.CancelledError` 原样传播，不吞、不转换为 503/401。
- 明确无身份（库返回 None）或库内身份与 JWT 声明不一致必须 401「令牌身份已失效」。
- 五个身份 ContextVar（user_id / tenant_id / role / tenant_code / username）在请求进入时注入、请求结束后精确 reset；503/401 拒绝路径不注入身份。
- 覆盖测试：`tests/test_api/test_auth_timeout_logging.py`（Core）、`tests/test_api/test_auth_dependency_http.py`（Acceptance；含 127.0.0.1 随机端口 uvicorn + curl.exe 的 503→恢复 200）。

## 4. 依赖异常日志作用域（src/log_scopes.py）

- `defer_dependency_error_logging()`：显式 opt-in 的 ContextVar 作用域，默认 False；进入最常见安全边界（精确原文读取）时启用；期间资源建立/健康/重建/关闭/解析异常栈不在内层重复输出；退出（含异常/取消）finally 恢复 token。
- `dependency_error_logging_deferred()`：只读探测，不读配置、不触 I/O、无日志副作用。
- 定点守卫：`src/memory/pg_pool.py`（池建立/健康/重建/关闭）、`src/app_context.py`（异步资源获取/关闭）、`src/db/utils.py`（URL 解析边界：仅接受 postgresql / postgresql+asyncpg 且要求 netloc，失败不回显原始 URL）。
- 不全局禁 logger、不改 loglevel/filter；旧调用与常规路径完全不受影响。
- 覆盖测试：`tests/test_log_scopes.py`、`tests/test_db/test_pg_error_logging_scope.py`（Core）。

## 5. 迁移默认排除与 connection_setup（src/db/migrations.py）

- 签名：`run_migrations(database_url, *, migrations_dir=None, connection_setup=None, include_branch=False)`。
- 默认只应用编号小于 900 的主库迁移；`include_branch` 必须为布尔值（非布尔一律 MigrationError 且不建立连接），True 才按数值序纳入 900+ 分库分支。
- `connection_setup`：可选异步回调，在连接建立后、advisory lock 前于同一会话恰执行一次（含零待应用迁移场景）；其异常按失败关闭处理（MigrationError），finally 关闭连接，不应用任何迁移 SQL；None 与旧调用完全一致。
- 覆盖测试：`tests/test_db/test_migrations.py`、`tests/test_db/test_migration_connection_setup.py`（Acceptance）。

## 6. 知识文件版本存储与精确原文读取（src/knowledge/file_store.py）

- `save(..., publication=..., require_durable=...)` 出版路径：调用方须先验校验 publication 完整性——非空 `source_version` 检查位于 `_save_publication_row`，其 ValueError 会被公开 save 的广义异常分支记录为「PG 保存失败」并转为返回 None，不属于公开 save 的保证抛错行为；`source_revision_hash` 非空无人强制，精确读取再做 64 位小写 hex 格式校验与字节 sha256 自证；同范围/身份/文件名/版本重复提交抛 `FileStoreDuplicateVersionError`（携带既有 file_id 与 index_state，不静默复用）；新行 `supersedes_file_id` 指向已提交前版本；出版表未就绪抛 `FileStoreUnavailableError`，不降级落盘。
- 旧普通上传路径（publication=None）行为不变：`source_version` 为空，不进入版本查询与精确读取通道。
- `get_exact_version(*, doc_id, source_version, source_revision_hash, knowledge_scope="")`：I/O 前严格校验（非空去空白、长度上限、64 位小写 hex、scope 白名单 system/tenant/private/空）；只按当前身份可见性（system；同租户 tenant；本人 private）；多匹配/元数据不符/字节 sha256 或长度不符抛 `FileStoreIntegrityError`（reason=multi_match/metadata/bytes），0 行返回 None；不回退最新同名、不读磁盘。
- `get_latest_versions(filename, *, knowledge_scope="", limit=2)`：按已提交追加序（id 倒序）返回 `source_version` 非空且当前身份可见的行；查询失败抛 `FileStoreUnavailableError`。
- `mark_index_state(file_id, state, *, knowledge_scope="")`：仅接受 pending/ready/failed，未知状态 ValueError；按 id 与当前身份精确回写，未命中返回 False。
- 覆盖测试：`tests/test_knowledge/test_file_store_publication.py`、`tests/test_knowledge/test_file_store_exact_version.py`、`tests/test_knowledge/test_file_store_exact_version_acceptance.py`（Core）；`tests/test_db/test_knowledge_version_main_postgresql.py`（真 PG 沙箱验收：2026-10-14 受控槽 9 passed、未过滤/未 skip，含真实终止后端故障→同三元组恢复）。

## 7. 兼容与边界

- 不引入新依赖；不重排/格式化无关既有文件。
- 默认语义不变：log_scopes 默认关闭；迁移默认排除 900+；save 无 publication 走旧路径；精确读取为新增读取口，不替代旧 get/get_by_name。

## 8. 测试与运行门

- Core 209 passed；Compat 456 passed；Acceptance 14 passed；Check 0（2026-10-14 受控隔离环境，原红证保留）。
- PG（2026-10-14 已执行；受控槽 holder_lane=main-common，仅本测试文件）：9 passed、0 failed/skip；真实断连用例以 pg_terminate_backend 终止读取持有的真实连接，真实 get_exact_version 路径收敛为 FileStoreUnavailableError，撤除连接边界门控后同三元组恢复成功；随机 mainkv_ 库/角色/会话逐例清理收据硬零（8 条收据），末例前缀核对 0/0/0、正对照 1/1，独立复核 audit-main-common-pg-20261014.json 记录 (0,0,0)。运行门仍为 `RUN_MAIN_COMMON_PG_TESTS=1` 且 `MAIN_COMMON_PG_CONFIG` 指向受控 test-environment.json（127.0.0.1:55491）。
