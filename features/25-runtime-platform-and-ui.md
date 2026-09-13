# 25. 平台运行时与界面增强

依赖：既有认证、PostgreSQL、连接器注册、SchemaManager、指标目录、应急授权。设计见 spec/37-runtime-platform-and-ui.md。

| 编号 | 功能 | 状态 |
|---|---|---|
| 25.1 | 平台动态配置覆盖、校验、重置、版本冲突和跨 Worker 热刷新 | 单测完成 |
| 25.2 | 应急授权平台账号模糊下拉选择 | 集成测试完成 |
| 25.3 | 指标数据源下拉与权威方言带出 | 集成测试完成 |
| 25.4 | 数据库插件目录、热发现、持久化启停和执行撤权 | 集成测试完成 |
| 25.5 | 默认中文、英文界面、右上角切换与持久化 | 集成测试完成 |
| 25.6 | 表结构同步命名、共享状态、互斥与手动终止 | 集成测试完成 |
| 25.7 | 数据源 JSON 导入导出、无凭证导出、按名称单选/多选导出、整批事务与重名随机后缀改名 | 集成测试完成 |
| 25.8 | 移除业务数据源 YAML/环境加载与演示自动注册，仅恢复应用配置 | 单测完成 |
| 25.9 | 认证等待可见、请求超时释放、暂时不可用与未登录区分及原路由重试 | 集成测试完成 |
| 25.10 | 数据源页统一同步/终止/共享状态，表结构菜单只读 | 集成测试完成 |

本模块功能总数 10，已开发 10，待开发 0；不包含真实生产迁移、在线安装驱动依赖或 19.6 的模型回答翻译。单元测试不调用真实 LLM。

状态说明：25.1 已通过共享存储替身和两个应用上下文的单元测试；25.2～25.6 的集成状态指真实路由/前端/本地插件与隔离依赖的交互验证，不代表真实生产 PostgreSQL 和全部数据库驱动已经验收。

## 2026-09-06 验证结果

- 非 live 回归：`pytest -q -m "not live_llm and not local_llm and not live_datasource" --ignore=tests/test_testdata --tb=short --disable-warnings --maxfail=5`，结果 **1790 passed，12 skipped，1 deselected**。未覆盖独立的大型造数模块。
- 浏览器回归：`RUN_BROWSER_TESTS=1` 后执行 `pytest tests/test_frontend -q -m browser`，结果 **12 passed，59 deselected**。
- 前端 `npm --prefix frontend run build` 通过；构建仍提示 Ant Design 公共块大于 500 kB，不影响本次构建成功。
- 新增运行时/插件/同步模块及对应测试定向 Ruff 检查通过；`compileall` 检查通过。
- 修复规范快照命中时跳过文档备注的回归；探针记录修复前文档加载次数为 0，修复后回归用例验证当前请求带文档覆盖且规范快照未被污染。
- 对现有服务的 curl 探测：前端 `localhost:5173` 返回 200；`127.0.0.1:8000/api/v1/schema/sync?datasource=sales` 在 10 秒内未响应，未擅自重启既有后端。因此不能声称本次版本已在现有运行服务中完成验收。

## 测试入口

### 2026-09-07 补充验收

- 完整非实时套件（含造数模块）：`pytest -q -m "not live_llm and not local_llm and not live_datasource and not browser" --tb=short --disable-warnings --maxfail=4`：**1922 passed，16 deselected，1 warning**。
- 浏览器：`RUN_BROWSER_TESTS=1`，`pytest tests/test_frontend -q -m browser`：**15 passed，59 deselected**。
- 25.7：`tests/test_datasource/test_transfer.py` 和 `tests/test_api/test_datasource_import_export.py` 验证动态方言、脱敏导出、按名称导出与去重、未知名称 404、multipart 上传、当前租户限定、403、格式/大小边界、重名随机后缀改名与改名映射回传、`updated_at` 倒序且不进导出；浏览器实际选择文件、勾选导出与下载。
- 25.8：Provider、启动编排、配置安全和 Compose 测试验证旧变量不产生数据源，移除 YAML 模板及挂载；不删除用户本地被忽略的历史文件。
- 25.9：此前仅验证加载提示与超时结束，但把认证超时当作未登录的处理不正确；已由下面的 2026-09-08 故障修复替代，不再跳登录页。
- 25.10：列表共享状态正常/缺失/异常回退；浏览器验证同步中、终止中禁重复操作和语言持久化；只读浏览通过 `cached_only=true` 读取已发布快照，不创建连接或触发内省。
- `CODE_GUIDE.md` 已补充迁移模块与数据源来源；本模块 10 项均已开发，待开发 0 项。真实 PostgreSQL 多 Worker、外部数据库驱动和现有服务部署不计入本次隔离验收。

| 功能 | 主要测试与边界 |
|---|---|
| 25.1 | `tests/test_security/test_runtime_settings.py`：环境默认值、严格类型、未知/敏感字段、跨实例版本、冲突、重置与最后有效配置 |
| 25.2 | `tests/test_api/test_runtime_controls.py`、`tests/test_frontend/test_governance_browser.py`：启用平台用户搜索、通配符输入、认证与授权下拉 |
| 25.3 | `tests/test_api/test_metric_catalog.py`、`tests/test_frontend/test_metrics_browser.py`：租户可见数据源和服务端方言带出 |
| 25.4 | `tests/test_connectors/test_hot_plugins.py`、`tests/test_frontend/test_runtime_browser.py`：真实发行包元数据发现、本地驱动执行、停用/恢复/移除、动态扩展字段 |
| 25.5 | `tests/test_frontend/test_runtime_browser.py`：默认中文、英文切换、刷新后持久化，用户数据不翻译 |
| 25.6 | `tests/test_api/test_schema_sync.py`、`tests/test_knowledge/test_schema_sync.py`：新旧入口互斥、权限、取消清理、发布冲突、旧快照保留、文档覆盖与缓存替换 |

## 2026-09-08 同步卡死与误退出修复

- 实际证据：前端 HTTP 200，但后端健康接口 5 秒无响应；运行进程堆栈为 `SchemaSyncService._publish → SchemaManager.refresh_from_snapshot → MilvusVectorStore.get_by_filter → Collection.load → wait_for_loading_collection`，同步 SDK 在主事件循环无限等待。热重载父进程也在等待这个旧 Worker 退出。
- 浏览器原始复现：直接打开现有前端 `/datasource`，认证请求 10 秒超时后落到 `/login`，并非服务端确认 Cookie 失效。另一次真实冷启动堆栈确认 `sentence_transformers/transformers` 首次导入也会阻塞主线程。
- 修复：Milvus 同步 RPC 和模型导入移到工作线程、RPC 和索引刷新有界；写任务取消必须等待线程退出才释放互斥。前端保留原路由，提供“重试验证”，明确未认证的 200/401 仍正常跳登录，不绕过认证。
- 真实验收：只结束已确认卡死的旧调试子进程，保留 PyCharm、热重载器与前端；以已配置管理账号通过应用 API 创建临时空 SQLite 数据源，在当前真实 PostgreSQL/Milvus 链路点击同步。发布期间连续刷新 3 次均保留登录、页面非空且无浏览器异常；健康、认证、同步状态一组请求前三轮分别约 76/57/62 毫秒，最终 `succeeded`。
- 范围与清理：管理账号所属租户没有用户原数据源，因此没有冒用其他租户身份；本轮验证的是当前服务上的临时数据源，而非用户原业务数据库。三轮临时数据源配置已删除，3 条临时同步任务记录已按唯一名称清理。
- 未修复的环境问题：只读探测实际 Milvus 集合仍为 `Loading`、进度 `0%`。本轮证明加载失败不再拖死服务与登录，不代表 Milvus 的向量索引已经恢复健康；需另行检查该部署的加载资源、QueryNode 和服务日志。
- 回归：非实时套件 **1946 passed，21 deselected**；浏览器 **20 passed，59 deselected**；前端构建通过。`tests/test_memory/test_vector_store_milvus_async.py` 新增 13 项，覆盖 SDK/模型导入线程边界、超时参数、HTTP 并发、取消写锁和索引失败收束。
- 模块仍为 10 项已开发、0 项待开发；其他部署验收范围保持独立，不把本次受控单 Worker 验收扩大为生产全场景验收。

## 2026-09-10 Milvus 集合加载卡死根因定位与客户端熔断

- 承接 09-08 遗留的「只读探测实际 Milvus 集合仍为 `Loading`、进度 `0%`」环境问题，本轮完成根因分层定位。该状态是**升级前**的测量结果，下文升级复核已将其取代。
- 实际证据（**升级前**对远端 v2.4.0 实例的只读实测，彼时 `server_version` = `v2.4.0`）：`has_collection=True`；索引存在（`embedding` / IVF_FLAT / COSINE / nlist=128）；分区仅 `_default`；实例内仅此一个集合——排除缺索引、多分区与跨集合资源争抢。`load_state` 恒为 `Loading`、`loading_progress` 恒为 `0%`，手动 `load(timeout=120)` 仍然失败，排除「超时设得太短」，判定为永久卡死而非加载缓慢。
- 根因层（服务端，应用代码无法修复）：load 任务永久卡死。对照 Milvus 官方 [issue #35630](https://github.com/milvus-io/milvus/issues/35630) 与 [issue #35441](https://github.com/milvus-io/milvus/issues/35441)（关联 bug #35008，2.4.9 修复、2.4.23 稳定），判定为 v2.4.0 已知缺陷。
- 放大因素层（客户端）：`_get_collection()` 每次读写操作都无条件调用 `Collection.load()`，且失败后无熔断，导致集合不可用时每个请求各白等一次完整 10 秒超时——09-08 日志中一次同步即在读、写两处各空转 10 秒。
- 客户端修复：确认加载后复用本地标记，不再每次操作重发 `LoadCollection`；加载失败进入 60 秒熔断窗口，窗口内直接抛 `MilvusCollectionUnavailable` 且不发起 RPC；操作报「未加载」类错误时失效标记，支持服务端驱逐或人工 release 后自愈；熔断期内健康检查直接判不健康。
- 回归：`tests/test_memory/test_vector_store_milvus.py` 新增 10 项（加载只做一次、熔断快速失败、窗口过期重试、未加载错误自愈、错误分类边界、熔断期健康检查、空批次零 RPC），与既有 23 项合计 33 项通过；`tests/test_memory` 与 `tests/test_knowledge` 合计 296 项通过。
- 范围与清理：诊断用的一次性探针脚本 `tmp_milvus_probe.py`、`tmp_milvus_probe2.py` 在结论确认后已删除，未进入代码库；全程对远端 Milvus 仅做只读探测，未执行 `release`、写入或删除。
- **已处置并复核**：部署侧已按上述处置方案将该实例升级到 2.4.23。升级后对同一实例只读复核：`server_version` = `v2.4.23`，`load_state` = `Loaded`，`loading_progress` = `100%`，`count(*)` = 931。集合加载卡死已解除，向量缓存读写恢复，不再降级到 DB 内省，19.17 的向量检索在该实例上已可用。
- **升级后遗留（独立问题）**：Milvus 容器持续刷 `[INFO] [msgstream/mq_msgstream.go] ["skip msg"] [type=TimeTick]`。官方 [issue #36569](https://github.com/milvus-io/milvus/issues/36569) 归因于 rocksmq 消费者 seek 到过早位置，需要消费并跳过 2.4.0 时期写入的大量陈旧消息；修复 PR #36617 / #36618 已含在 2.4.21+。本实例升级后可服务性正常（集合 `Loaded`），刷屏属恢复期日志噪声，主要风险是 Docker 日志无上限增长撑爆磁盘（该实例数据在 `/data/milvus/data` 下）。建议为容器配置日志轮转上限，并观察陈旧消息消费完毕后刷屏是否自行消退。

## 部署验收边界

以下属于部署验收，不计为本模块未开发功能；不得把隔离测试通过描述成生产数据库已经迁移。

| 待验收项 | 本轮未执行原因 | 可验收条件 | 预计时机 |
|---|---|---|---|
| PostgreSQL 025/026 迁移及真实 RLS | 本轮仅验收本机已迁移状态库的受控同步读写，未执行全新库迁移与多租户 RLS 矩阵 | 备份完成并确认目标状态库、迁移身份与运行身份 | 本次版本部署时 |
| 多 Worker 同步互斥和崩溃恢复 | 当前测试使用共享 Fake 存储，未运行真实 PostgreSQL 多进程 | 隔离 PostgreSQL 环境及至少两个 Worker | 上线前预发布验收 |
| 各实际数据库驱动的取消行为 | 只验收本地驱动和受控异步清理，不接触业务数据库 | 各数据库只读账号与可中断的测试查询 | 对应数据库插件上线前 |
| 外部插件生产发行包 | 真实 entry point + SQLite 测试已通过，尚无用户指定的生产插件包 | 审核后的发行包及各 Worker 一致的驱动依赖 | 新数据库类型接入时 |

## 2026-09-13 同步超限改为持久化排队（需求 8）

原实现在本 Worker 运行中任务数达到硬编码 `3` 时直接抛 429 拒绝，该限制值既不在配置里也无法调整；用户反馈「超过数量的同步任务被直接中断」。现改为排队，扩展 25.6 与 25.10 的语义（**不新增独立功能编号**，两项状态仍为集成测试完成）。

- `schema_sync_jobs.status` 新增 `queued`（迁移 `031_schema_sync_queue.sql`：按 `pg_constraint` 实际名重建 CHECK 约束，并新增 `(started_at, datasource) WHERE status='queued'` 部分索引）。
- 新增配置 `max_schema_sync_concurrent`（默认 3，范围 1-100）。以 `max_` 开头，因此自动进入 `src/runtime/settings.py` 的 `LIVE_FIELDS` 白名单，可通过 `PUT /admin/runtime-settings` 热改。
- 调度：`SchemaSyncService` 新增 `start_scheduler`/`stop_scheduler`（`close()` 内调用），每 2 秒按 `(started_at, datasource)` 先进先出认领 `queued` 行；认领复用与直接启动**相同的会话级 advisory lock**，并以 `status='queued' AND run_id` 匹配的条件更新保证「取消与认领只有一个胜出，不复活已取消行」。
- 崩溃回收只处理 `running`/`cancelling`，`queued` 行不持有 advisory lock、天然可被重新认领，**不会被误判为 failed**。
- 排队中可直接取消（不再 409）；`publishing` 阶段取消仍返回 409。同一数据源重复启动仍是 409。
- 排队位置按**同租户**口径计算（用户只能看见本租户数据源；全局口径会显示与感知不符的数字，也会泄露其他租户的排队规模），下发为 `queue_position`，界面显示「排队中（前面还有 N 个）」，`0` 时显示「排队中（即将开始）」。
- 已知边界：排队触发条件是「**本 Worker** 运行中任务数 ≥ 上限」，因此本 Worker 空闲时新启动的任务不会排在别处排队任务之后；被排队任务会在 ≤2 秒内被任意空闲 Worker 认领，单 Worker 部署下无实际饥饿。若要全局严格 FIFO，需改为「队列非空即排队」。
- 测试：`tests/test_knowledge/test_schema_sync_queue.py`（13 例：并发满返回 `queued` 而不抛 429、FIFO 认领顺序、租户口径位置、崩溃回收不误伤排队行、排队 cancel 成功 / publishing 仍 409、认领跳过被占用源、调度循环异常不致死、启停幂等）与 `tests/test_frontend/test_schema_sync_queue.py`（4 例静态契约）。真实 PostgreSQL 迁移在**回滚事务内**验证了幂等性、约束名与索引；多 Worker 竞争仍属上面的部署验收边界。
