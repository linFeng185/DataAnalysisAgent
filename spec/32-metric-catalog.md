# 租户指标目录与口径治理

对应 11.1.13、18.6.1、18.6.2，扩展 §3.2.5 的文档型业务规则，不替换已有知识检索。
业务 API 使用 `/api/v1/metric-catalog`，不占用 Prometheus `/metrics`。

## 数据与权限

`MetricDefinition` 包含 code/name/datasource/dialect/expression/description/unit。
expression 是单个 SQL 表达式，按 ClickHouse/MySQL/PostgreSQL 方言由 sqlglot 校验，
不得携带查询、DML、DDL 或多条语句；仅作为生成 SQL 的口径证据，不直接执行。

PG `business_metrics` 按 tenant_id/code/version 唯一；每次保存产生新的不可变定义版本。
新建提交 expected_version=0；更新必须携带读到的最新版本，否则 409，不覆盖并发修改。
生命周期 draft → pending → approved/rejected；已有已批准版本不可修改，需另建版本。
审核可指定带时区的 effective_at，未到生效时间不参与查询；旧已批准版本保持生效，
直到更新的已批准版本到达生效时间。被新草稿取代的旧待审版本不能批准。

租户成员读取本租户已生效版本；租户管理员可以查看草稿、全部历史、创建版本、提交
审核及批准/驳回。审核记录保存操作者、时间与意见；不强制双人审核。超级管理员不
代替租户管理业务口径。RLS 和每条查询均绑定 tenant_id；没有跨租户全局指标写入口。

## API 与引用

- GET `/metric-catalog?query=&limit=20&offset=0`：返回最新可见版本及 total。
- GET `/metric-catalog/{code}/versions`：按版本倒序返回可见历史。
- POST `/metric-catalog`：`{definition:{...},expected_version:0}` 保存新版本。
- POST `/metric-catalog/{id}/transition`：`{action:"submit|approve|reject",effective_at:null,note:""}`。
- GET `/metric-catalog/{code}/effective`：返回当前已生效版本与固定版本引用。

示例定义：

```json
{"definition":{"code":"gmv","name":"支付金额","datasource":"sales","dialect":"postgres","expression":"SUM(paid_amount)","description":"仅统计已支付订单，按支付时间归属","unit":"元"},"expected_version":0}
```

成功响应包含 id/tenant_id/code/version/status/definition/created_by/reviewed_by/
created_at/effective_at/review_note；冲突 409、无权限 403、不可见或未生效 404。

查询显式使用 `@metric(gmv)` 或 `@metric(gmv@2)` 引用；后端只解析当前租户已生效
且数据源一致的已批准版本。不存在、未批准、未生效、跨数据源引用失败关闭，不
允许忽略引用继续生成另一种口径的 SQL。每个问题最多引用 10 个指标。
来源写入 activated_knowledge，包含版本和公式快照，保证历史可追溯。
没有显式引用的旧查询不访问指标存储，不改变原知识检索或增加数据库依赖。

## 界面与验收

指标列表/搜索/分页、编辑为新版本、版本历史、提交及审核、生效时间和复制固定
版本引用均在租户指标页。迁移只新增文件，不修改旧迁移 checksum。
单测覆盖隔离、乐观冲突、审核状态、未来生效、非法表达式、跨方言、引用；
前端通过构建与独立浏览器验证，真实生产 PG 迁移另行验收。
