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

本轮六项代码均已实现，功能总数 6，已开发 6，待开发 0；不包含真实生产迁移、在线安装驱动依赖或 19.6 的模型回答翻译。单元测试不调用真实 LLM。

状态说明：25.1 已通过共享存储替身和两个应用上下文的单元测试；25.2～25.6 的集成状态指真实路由/前端/本地插件与隔离依赖的交互验证，不代表真实生产 PostgreSQL 和全部数据库驱动已经验收。

## 2026-09-06 验证结果

- 非 live 回归：`pytest -q -m "not live_llm and not local_llm and not live_datasource" --ignore=tests/test_testdata --tb=short --disable-warnings --maxfail=5`，结果 **1790 passed，12 skipped，1 deselected**。未覆盖独立的大型造数模块。
- 浏览器回归：`RUN_BROWSER_TESTS=1` 后执行 `pytest tests/test_frontend -q -m browser`，结果 **12 passed，59 deselected**。
- 前端 `npm --prefix frontend run build` 通过；构建仍提示 Ant Design 公共块大于 500 kB，不影响本次构建成功。
- 新增运行时/插件/同步模块及对应测试定向 Ruff 检查通过；`compileall` 检查通过。
- 修复规范快照命中时跳过文档备注的回归；探针记录修复前文档加载次数为 0，修复后回归用例验证当前请求带文档覆盖且规范快照未被污染。
- 对现有服务的 curl 探测：前端 `localhost:5173` 返回 200；`127.0.0.1:8000/api/v1/schema/sync?datasource=sales` 在 10 秒内未响应，未擅自重启既有后端。因此不能声称本次版本已在现有运行服务中完成验收。

## 测试入口

| 功能 | 主要测试与边界 |
|---|---|
| 25.1 | `tests/test_security/test_runtime_settings.py`：环境默认值、严格类型、未知/敏感字段、跨实例版本、冲突、重置与最后有效配置 |
| 25.2 | `tests/test_api/test_runtime_controls.py`、`tests/test_frontend/test_governance_browser.py`：启用平台用户搜索、通配符输入、认证与授权下拉 |
| 25.3 | `tests/test_api/test_metric_catalog.py`、`tests/test_frontend/test_metrics_browser.py`：租户可见数据源和服务端方言带出 |
| 25.4 | `tests/test_connectors/test_hot_plugins.py`、`tests/test_frontend/test_runtime_browser.py`：真实发行包元数据发现、本地驱动执行、停用/恢复/移除、动态扩展字段 |
| 25.5 | `tests/test_frontend/test_runtime_browser.py`：默认中文、英文切换、刷新后持久化，用户数据不翻译 |
| 25.6 | `tests/test_api/test_schema_sync.py`、`tests/test_knowledge/test_schema_sync.py`：新旧入口互斥、权限、取消清理、发布冲突、旧快照保留、文档覆盖与缓存替换 |

## 部署验收边界

以下属于部署验收，不计为本模块未开发功能；不得把隔离测试通过描述成生产数据库已经迁移。

| 待验收项 | 本轮未执行原因 | 可验收条件 | 预计时机 |
|---|---|---|---|
| PostgreSQL 025/026 迁移及真实 RLS | 未连接或修改用户状态数据库 | 备份完成并确认目标状态库、迁移身份与运行身份 | 本次版本部署时 |
| 多 Worker 同步互斥和崩溃恢复 | 当前测试使用共享 Fake 存储，未运行真实 PostgreSQL 多进程 | 隔离 PostgreSQL 环境及至少两个 Worker | 上线前预发布验收 |
| 各实际数据库驱动的取消行为 | 只验收本地驱动和受控异步清理，不接触业务数据库 | 各数据库只读账号与可中断的测试查询 | 对应数据库插件上线前 |
| 外部插件生产发行包 | 真实 entry point + SQLite 测试已通过，尚无用户指定的生产插件包 | 审核后的发行包及各 Worker 一致的驱动依赖 | 新数据库类型接入时 |
