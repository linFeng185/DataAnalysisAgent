# 36. 大规模多业务域测试数据

## 范围与兼容

2026-09-06 用户澄清：要求是**每个数据源超过 100 张表**，不是五源合计。
当前目标为五源各 125 张专属业务表，共 625 张；下文旧 25 表/源验收记录仅为历史阶段。
每源保留原 25 表，新增 100 张有独立含义和字段的业务表，不用分表、复制表或空表凑数。
旧 `get_tables/build_ddl/generate_table_file` 保留用于历史测试；
`tests/import_test_data.py` 默认入口使用新生成器，不改业务查询链路。

| 数据源 | 引擎 | 业务所有权 |
|---|---|---|
| retail | MySQL | 客户、商品、交易、门店、会员、促销和售后 |
| finance | PostgreSQL | 科目、发票、收付款、总账、预算、税务与资产 |
| supply | SQL Server | 供应商、采购、仓储、物流、生产与质量 |
| workforce | Oracle | 组织、员工、薪资、招聘、培训、项目与合规 |
| insight | ClickHouse | 会话、漏斗、广告、实验、设备、天气与市场指标 |

## 接口与数据契约

- `GenerationConfig`：Pydantic 校验每源基础池、扩展池各自目标行数 `scale`（默认各 1,000,000）、
  `anchor_year`（默认当年）、前后各十年、`seed` 和 `batch_size`。
- `build_catalog()`：中文业务含义、字段类型、生成规则和引用目标。
- `row_counts(config)`：按权重分配，每表至少两行，日期维表覆盖完整窗口。
- `iter_batches(table, config)`：固定种子、有界内存，与批大小及生成顺序无关。
- `export_dataset()`：CSV/JSONL/SQL、五方言 DDL、manifest、关联及问答样例；
  清单含实际行数、SHA256、日期范围、空值计数、金额汇总，失败无成功标记。
- `import_dataset()`：经 `src/connectors/` 工厂连接，分批提交；只创建新表，
  不执行 DROP/TRUNCATE，不覆盖已有表；失败退出非零并清理连接。
- 不调用 LLM、不新增重量级依赖，不继承旧脚本硬编码服务器或密码。

### 每源百表增量扩容契约

- `expanded_catalog.py` 按五源、每源十个新业务主题组织 500 张新表；新增业务字段显式附中文释义。
- 旧 125 表的权重、计数及行内容保持字节级确定性兼容；新增表按独立权重池分配。
  `scale` 是每源基础池与扩展池分别的目标行数，默认每源合计至少 200 万行、全局千万级。
- `--extend-from <旧产物目录>` 仅与显式入库同用：先验证旧 manifest 的文件完整性与
  seed/时间/scale 契约，再在所有目标数据库核对已存在旧表、计数、主键、时间、金额和样本。
  任一不一致或新增表已存在，必须在任何新增写入前停止。旧表不执行 DDL/INSERT/UPDATE/DELETE。
- 只新增目录差集；不支持自动删除、盲目跳过或自动续传半张表。失败保留已提交批次供人工审计。
- 完成后对 625 表逐表重新查询数据库核验；明确报告“原有保留行数/新增行数/最终总行数”。
- 文档自动扩展到每源 125 份表结构，补充每源十个业务主题说明及新表问答，保留六份私有资料隔离。

## 时间、金额和关联

2026 年默认 **2016-01-01 至 2036-12-31**，首末日期确定性覆盖。
事实区分“历史模拟 / 当年模拟 / 未来情景”；未来值只是模拟情景。
指标包含趋势、季节、周末、促销、退款、零值、长尾与空备注；金额使用 Decimal。

强关联：客户→订单→明细/发票/发货/数字触点；员工→薪资/项目；商品→采购/库存。
引用根据全目录计数计算，单源导出也保持跨源 ID 一致。核心订单客户、日期、区域
与金额快照由相同订单键推导。跨库不创建物理外键，关系清单注明基数与先聚合要求。
弱关联：日期或月份 + 区域 + 渠道/品类的天气、宏观、竞品、舆情与广告对照，
不伪造实体外键，不把相关性描述为因果。

## 安全与验收

默认只导出到 Git 忽略的 `data/generated/`；输出目录不得复用，避免混入旧种子。
入库须显式 `--import-db`、专用测试库环境变量和 `--confirm-test-database`；
禁止 SQL Server master、PostgreSQL postgres 等系统库。日志复用七天轮转配置。
先写单测，覆盖每源 125 表、全局千万级、21 年窗口、确定性、FK、跨库订单一致性、
边界样本、SQL 方言解析、文件清单与失败路径；Fake/SQLite 验证写入边界。
默认不连接真实数据库，五库真实验收须另行显式执行。

## 中文知识库配套资料

业务名称、状态、标签与正文均使用中文，技术标识符、币种代码保留原值。
每源把 125 张表的结构文档（字段含义、类型、空值、主键、强关联与粒度）合并为单份
`表结构总览.md`，每张表以行首的 `### 表: <表名> — <中文名>` 分节，按域生成业务词典，
另提供跨源关系、指标口径、问答样例和上传清单。文档来自同一目录而非手写两份结构。
上传该文档时自动检测会按 `### 表:` 标记选用 table 策略（2026-09-10 修正了此前被
反引号计数抢先判为 reference、整篇塌成单块的问题）；写入前另有超长块兜底切分，
保证单块不超过向量库字段上限。
共享知识只建议上传到 `knowledge_scope=tenant`，不自动发布到 system。
私有资料分别归属于财务、人力和经营负责人，使用模拟的薪资政策、授信规则与经营情景，
只允许通过对应拥有者账号以 `knowledge_scope=private` 上传；目录名不是权限机制。
上传清单不包含私有正文，不在租户共享文件中复制私有阈值或私有检索标记。
默认只生成文件，不使用当前登录身份自动上传，不读取真实员工或客户敏感数据。

## 操作方式

```powershell
# 默认五源、每源 125 表且至少两百万行；目录必须是新目录
.venv/Scripts/python.exe tests/import_test_data.py --db all --scale 1000000 --anchor-year 2026 --output-dir data/generated/multidomain_2026_seed42

# 小样本功能验证，仍保留每源 125 表和完整日期维表
.venv/Scripts/python.exe tests/import_test_data.py --db all --scale 1000 --output-dir data/generated/small_multidomain

# 只生成 DDL 和全部知识资料，不生成数据行
.venv/Scripts/python.exe tests/import_test_data.py --ddl-only --output-dir data/generated/schema_and_knowledge

# 独立检查数据、DDL 和知识文件的校验和
.venv/Scripts/python.exe tests/import_test_data.py --verify-only --output-dir data/generated/multidomain_2026_seed42
```

`--format csv|jsonl|sql` 控制压缩文件格式；默认 CSV。CSV 空值是 `\N`，金额保持
两位小数，换行/逗号/引号由 CSV 标准转义；读取时需要正确识别空值标记。
`--scale` 表示**每业务源基础池与扩展池各自的目标行数**，默认每池百万、每源合计至少两百万，
不是每张表的行数；基础池单独分配以保持已导入旧表的计数和引用键兼容。
`--batch-size` 默认 5000；`--anchor-year` 默认当前年；`--years-before/--years-after`
默认各十年。固定种子与时间参数可重现，改变 batch_size 不改变数据。

### 显式导入专用测试库

先用数据库管理工具创建空的专用测试库/账号，脚本不创建数据库，不删除旧表。
每种引擎使用以下环境变量前缀，并配置 `HOST`、`PORT`、`DATABASE`、`USER`、`PASSWORD`：

| 业务域 | 变量前缀 | 专用目标示例 |
|---|---|---|
| retail | `TESTDATA_MYSQL_` | database=`daa_test_retail` |
| finance | `TESTDATA_POSTGRES_` | database=`daa_test_finance` |
| supply | `TESTDATA_MSSQL_` | database=`daa_test_supply` |
| workforce | `TESTDATA_ORACLE_` | user=`DAA_TEST_WORKFORCE`，DATABASE 填实际 service_name |
| insight | `TESTDATA_CLICKHOUSE_` | database=`daa_test_insight` |

用户名/密码只从环境或本地 `.env` 读取，勿写入知识文件；按需安装项目已有
`connectors` 可选依赖。然后执行：

```powershell
.venv/Scripts/python.exe tests/import_test_data.py --db all --import-db --confirm-test-database --output-dir data/generated/import_run
```

原共享表不会被删除。注册五个新数据源后绑定相应知识文件；单独导出/导入一个业务域时，
跨域外键仍按全目录计算，缺少的依赖列于 `manifest.json.external_dependencies`，
没有导入依赖源时只能进行单域或部分跨域测试。
所有引擎先检查已有表冲突，再开始写入；每批提交，失败可能保留已提交批次。
失败后先检查目标库与已提交批次，不支持自动续传半表或盲目追加。
Oracle/SQL Server/MySQL 等 DDL 的自动提交差异不视为跨库原子事务保证。

### 保留旧表的每源百表扩容

```powershell
.venv/Scripts/python.exe tests/import_test_data.py --db all --scale 1000000 --anchor-year 2026 --seed 42 --batch-size 5000 --import-db --confirm-test-database --extend-from data/generated/multidomain_import_20260906 --output-dir data/generated/multidomain_625_20260906
```

`--extend-from` 必须指向旧完整产物，且只允许与显式入库同用。参数和旧文件校验通过后，
在全部目标库核验旧表并排除新增表冲突，再写入差集；不删除、覆盖或重复导入旧数据。
该命令记录本次已完成的操作，不可直接重复执行：新输出目录或新表已存在均会拒绝覆盖。
完成后生成 `import_report.json` 和独立查询得到的 `database_verification.json`。

### 知识资料产物

- `knowledge/tenant/<业务域>/`：每源 1 份合并表结构总览（内含该源 125 张表）、1 份业务词典和 10 份业务主题文档，合计 5 份表结构、5 份业务词典和 50 份业务主题文档。
- `knowledge/tenant/`：跨源业务关系、指标口径及问答验收样例。
- `knowledge/private/<拥有者>/`：财务、人力、经营负责人各 2 份私有模拟资料。
- `knowledge/上传操作说明.md`：按拥有者登录、权限范围、数据源绑定和隔离验收步骤。
- `knowledge_upload_manifest.json`：70 份文档的路径、分类、scope、拥有者和摘要；表结构条目用 `tables[]` 列出所含的 125 张表。
- `relationships.json`：可机读的强关联键、基数和弱关联粒度。
- `questions.json`：65 个中文分析问题、参考 SQL 与验收口径。
- `manifest.json`：逐表实测行数、日期、金额合计、空值计数与文件 SHA256。

上传 API 为 `POST /api/v1/knowledge/docs/upload`，文件放在 `files` 表单字段，
`knowledge_scope` 和 `datasource` 等为查询参数；表结构总览必须带 `strategy=table`，
否则整篇会塌成一个分块。私有文件须由其拥有者账号上传，
共享关系文档不要绑定单一数据源；普通用户不能自行发布 tenant 或 system 资料。
私有阈值和私有检索校验词不进入共享正文；这些是虚构的权限测试素材，不是真实商业秘密。

## 2026-09-06 历史基线本地验收记录（25 表/源）

固定参数 `seed=42 / anchor_year=2026 / scale=1000000`，本地产物位于
`data/generated/multidomain_2026_seed42/`（Git 忽略，不提交大文件）。

| 数据源 | 表数 | 实际行数 |
|---|---:|---:|
| retail | 25 | 1,000,000 |
| finance | 25 | 1,000,000 |
| supply | 25 | 1,019,576 |
| workforce | 25 | 1,000,009 |
| insight | 25 | 1,000,018 |
| 合计 | 125 | 5,019,603 |

- 从压缩 CSV 独立重读全部 5,019,603 行，检查连续主键、所有引用范围及逐表 21 个自然年覆盖。
- 订单、三明细和单发票净额合计均为 106,368,707.76；每条发票与订单的客户、日期、地区和金额一致性由单测覆盖。
- 完整文件/知识文档摘要校验通过；产物共 77,855,607 字节（约 74.25 MiB）。
- 140 份中文资料含 125 表结构、5 业务词典、3 通用关系/口径/问答文件、6 私有资料及 1 上传说明。
- 新旧相关 pytest 共 52 项通过；625 组表/方言 DDL 解析通过；Ruff 和 Python 3.14.0 语法检查通过。
- 入库使用 SQLite 实际同步/异步事务及 ClickHouse Fake 测试，真实五引擎尚未联机验收。
- 未修改现有数据库数据，未自动上传任何知识资料；私有文件须按对应拥有者身份单独上传。

## 2026-09-06 历史基线真实五库导入验收（25 表/源）

用户确认后执行真实导入，约 21:31 完成全部写入，21:32 完成独立数据库核验。

| 项目数据源名 | 实际数据库 / Schema | 新增表数 | 实测行数 |
|---|---|---:|---:|
| retail | MySQL `daa_test_retail` | 25 | 1,000,000 |
| finance | PostgreSQL `daa_test_finance` | 25 | 1,000,000 |
| supply | SQL Server `daa_test_supply` | 25 | 1,019,576 |
| workforce | Oracle `XEPDB1` / `TEST_USER` | 25 | 1,000,009 |
| insight | ClickHouse `daa_test_insight` | 25 | 1,000,018 |

前三库完成后，Oracle 首次建表遇到 ORA-00922。日志和只读双探针确认：
离线 SQL 末尾分号被直接传入 DBAPI；带分号的 SELECT 被拒绝，不带分号执行成功，
失败建表未产生 workforce 表。最小修复仅在 Oracle 建表驱动边界移除结尾分号，
离线 DDL 文件继续保留分号；新增回归测试后只继续 Oracle/ClickHouse，未重跑前三库。

最终直接从五个数据库重新验证 125 表：行数、主键连续性与唯一性、所有引用范围、
日期首末边界、全部定点字段合计、空值计数，以及包含中文、引号和换行的样本字段。
全部通过；订单、订单明细和发票净额合计仍为 106,368,707.76。
相关新旧回归 **53 项通过**，Ruff 与语法检查通过。

导入产物与报告在 `data/generated/multidomain_import_20260906/`：

- `import_report.json`：最终五库完成报告，包含分阶段恢复说明。
- `database_verification.json`：独立数据库验收总报告。
- `database_verification.<业务域>.json`：分源验收结果。
- `import_report.remaining.json`：Oracle/ClickHouse 继续导入的原始结果。

本地 `config/datasources.yaml` 已保留原五个源并追加 retail/finance/supply/workforce/insight，
供项目下次启动加载；未重启应用或修改已有会话。本地 `.env` 已追加 TESTDATA_* 配置，
密码仅引用原有环境变量。没有删除或覆盖任何原有表；没有自动上传知识资料。

## 2026-09-06 每源百表扩容验收

22:28 启动全量导出与安全增量入库，22:45 完成最终逐表核验。
固定参数为 `seed=42 / anchor_year=2026 / scale=1000000 / batch_size=5000`。
先在所有目标库检查历史基线，再只创建 500 张新表；原 125 表不执行写入。

| 数据源 | 表数 | 最终行数 | 保留行数 | 新增行数 |
|---|---:|---:|---:|---:|
| retail / MySQL | 125 | 2,000,058 | 1,000,000 | 1,000,058 |
| finance / PostgreSQL | 125 | 2,000,076 | 1,000,000 | 1,000,076 |
| supply / SQL Server | 125 | 2,019,576 | 1,019,576 | 1,000,000 |
| workforce / Oracle | 125 | 2,000,058 | 1,000,009 | 1,000,049 |
| insight / ClickHouse | 125 | 2,000,074 | 1,000,018 | 1,000,056 |
| 合计 | 625 | 10,019,842 | 5,019,603 | 5,000,239 |

- 每张表的实际日期首末均为 2016-01-01 和 2036-12-31，未来数据显式标识为“未来情景”。
- 625 表全量计数、唯一连续主键、外键范围、日期边界、定点金额合计、空值计数及每表 6 个中文样本全部通过。
- 再独立重读全部 10,019,842 行压缩 CSV，逐行验证主键连续、引用范围和每表 21 个自然年均有数据；原 125 表解压后的 SHA256 与旧基线逐表一致。
- 50 个新增业务主题覆盖不同粒度；678 条强关联与 26 条弱关联，不采用五库复制相同表的方式凑数。
- 690 份中文知识文档包含 625 表结构、50 业务主题、5 词典、3 通用文档、6 私有资料和 1 上传说明；另有 65 个中文问答样例。
- 测试数据相关回归共 110 项通过；五方言共 3,125 组 DDL 解析通过；Ruff 与 Python 3.14 语法检查通过。

本轮产物位于 `data/generated/multidomain_625_20260906/`：

- `manifest.json`：全量文件指标与校验和。
- `import_report.json`：保留/新增逐表动作和实际行数。
- `database_verification.json`：真实五库逐表验收指标与源级汇总。
- `file_audit.json`：全量文件逐行核验、逐年覆盖和原表内容一致性报告。
- `knowledge_upload_manifest.json`：资料分类、权限与拥有者清单。

本地数据源说明已同步为每源 125 表；原五个遗留配置和原有表保持不变。
未重启应用、未自动同步运行中 Schema 缓存、未自动上传知识文件；私有文件须按清单用对应拥有者账号上传，
随后用拥有者、同租户其他用户、其他租户三种身份验证召回隔离。

## 2026-09-10 表结构文档按源合并

降低知识库上传文件数量，把逐表结构文档合并为每源一份，向量分块粒度保持不变。

- 生成入口 `src/testdata/knowledge.py` 改为产出 `tenant/<源>/表结构总览.md`，每张表以行首
  `### 表: <表名> — <中文名>` 分节；原 `_schema_document` 拆出 `_schema_body` 供分节复用。
- 分块策略：table 下每源 126 块 = 1 个文档头块 + 125 个表块，表名由
  `_extract_table_name` 正确写入 metadata。
- 上传时发现并修复缺陷：`_auto_detect` 原先把 REFERENCE 排在 TABLE 之前，数据字典正文
  反引号密集必然先命中 REFERENCE，而该策略找不到 `####` 锚点会把整篇当成单块。实测
  `insight/表结构总览.md` 因此产出 137003 字符单块，超出 Milvus `content` 字段
  `VARCHAR(65535)` 上限，上传以 ParamError 失败。修复：TABLE 判定提前到 REFERENCE 之前，
  并新增 `enforce_chunk_limit` 在写入前按 `MAX_CHUNK_CHARS` 兜底切分超长块。
- 清单表结构条目由 625 条并为 5 条，`table` 单值改为 `tables[]` 列表；条目总数 690 → 70。
- `data/generated/multidomain_625_20260906/` 已另行产出 5 份合并文档并重建清单；
  原 625 份逐表文件保留原地留底，不再作为上传目标。
- 覆盖测试：`tests/test_testdata/test_knowledge.py` 改为校验每源 125 节、字段齐全且表名无重复。
