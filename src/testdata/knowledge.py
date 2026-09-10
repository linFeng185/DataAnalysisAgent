"""从表目录生成中文知识资料，私有文档不自动发布。"""

from pathlib import Path
from typing import Any

from src.logging_config import get_logger
from src.testdata.catalog import SOURCES, ColumnSpec, TableSpec, build_catalog
from src.testdata.generator import CHANNELS, CITIES, REGIONS, GenerationConfig, row_counts

logger = get_logger(__name__)

LABELS = {
    "id": "本表唯一主键",
    "event_date": "业务日期",
    "year_month": "业务年月（YYYY-MM）",
    "region_code": "地区技术编码",
    "channel_code": "渠道名称",
    "scenario": "模拟数据时态",
    "notes": "可空中文备注，含引号、逗号和换行边界",
    "fiscal_year": "财年",
    "quarter": "季度",
    "is_weekend": "是否周末",
    "net_amount": "订单净额（人民币，退款为负）",
    "gross_amount": "订单原始金额",
    "discount_amount": "折扣金额",
    "line_amount": "订单行净额",
    "invoice_amount": "发票人民币金额，与订单净额一致",
    "order_status": "订单状态",
    "quantity": "数量",
    "unit_price": "单价",
    "unit_cost": "单位成本",
    "segment": "客户分群",
    "age": "年龄",
    "acquisition": "获客来源",
    "positioning": "品牌定位",
    "area_sqm": "面积（平方米）",
    "tier": "会员等级",
    "redeemed": "是否已核销",
    "rating": "评分（1—5）",
    "sentiment": "情绪倾向",
    "abandoned": "是否弃购",
    "priority": "优先级",
    "frequency": "订阅周期",
    "active": "是否有效",
    "activated": "是否激活",
    "satisfaction": "满意度（1—5）",
    "converted": "是否转化",
    "purpose": "授权用途",
    "granted": "是否授权",
    "currency": "币种编码",
    "posted": "是否记账",
    "approval": "审核状态",
    "direction": "资金方向",
    "version": "预算版本",
    "method": "计提方法",
    "confidence": "情景置信系数（模拟）",
    "severity": "严重程度",
    "resolved": "是否已解决",
    "country": "国家",
    "accepted": "是否验收通过",
    "on_hand": "在库数量",
    "stockout": "是否缺货",
    "movement": "移动类型",
    "on_time": "是否准时",
    "criticality": "关键程度（1—5）",
    "renewed": "是否续签",
    "deductions": "薪资扣款",
    "approved": "是否批准",
    "compensation": "加班补偿",
    "proficiency": "能力等级（1—5）",
    "completed": "是否完成",
    "valid": "是否有效",
    "openings": "招聘人数",
    "stage": "业务阶段",
    "score": "评分",
    "recommended": "是否推荐",
    "objective": "目标",
    "viewable": "是否可见",
    "bounced": "是否跳出",
    "variant": "实验分组",
    "action": "事件动作",
    "delivered": "是否送达",
    "opened": "是否打开",
    "topic": "主题",
    "mentions": "提及次数",
    "sector": "行业",
    "endpoint": "接口技术名称",
    "temperature_c": "气温（摄氏度）",
    "rainfall_mm": "降雨量（毫米）",
}
WORDS = dict(
    pair.split(":")
    for pair in (
        "customer:客户 product:商品 category:品类 brand:品牌 store:门店 channel:渠道 membership:会员 "
        "promotion:促销 annual:年度 face:面值 return:退货 cart:购物车 points:积分 delta:变动 "
        "old:原 previous:原 new:新 price:价格 reason:原因 target:目标 units:数量 balance:余额 "
        "issue:问题 resolution:解决 dwell:停留 city:城市 address:地址 tag:标签 name:名称 "
        "type:类型 rate:比率 amount:金额 value:数值 cost:成本 count:数量 hours:小时 minutes:分钟 "
        "seconds:秒 family:系列 group:分组 account:账户 center:中心 commission:佣金 discount:折扣 "
        "margin:毛利 decimal:小数 places:位数 exchange:汇率 base:基准 receipt:收款 payment:支付 "
        "method:方式 refunded:退款 refund:退款 journal:凭证 allocated:分配 debit:借方 credit:贷方 "
        "outstanding:未结 overdue:逾期 days:天数 provision:准备 payable:应付 bank:银行 flow:流水 "
        "fee:手续费 difference:差异 reconciled:已对账 budget:预算 actual:实际 variance:偏差 tax:税 "
        "taxable:应税 asset:资产 original:原始 residual:残值 depreciation:折旧 expense:费用 "
        "expected:预期 inflow:流入 outflow:流出 limit:额度 risk:风险 grade:等级 exposure:风险敞口 "
        "supplier:供应商 warehouse:仓库 capacity:容量 carrier:承运商 transport:运输 mode:方式 "
        "origin:始发 destination:目的 distance:距离 km:公里 purchase:采购 status:状态 received:收货 "
        "reserved:预留 transfer:调拨 lead:提前 shipping:运输 delay:延迟 event:事件 elapsed:经过 "
        "claim:理赔 fuel:燃油 surcharge:附加费 quality:质量 delivery:交付 sample:样本 defect:缺陷 "
        "rework:返工 material:物料 required:需求 planned:计划 machine:设备 yield:良品 "
        "downtime:停机 maintenance:维护 demand:需求 safety:安全 stock:库存 reorder:补货 "
        "point:阈值 lot:批量 size:大小 emission:排放 kg:千克 accounting:核算 scope:范围 "
        "department:部门 function:职能 position:岗位 job:岗位 employee:员工 employment:用工 "
        "contract:合同 change:变更 work:工作 late:迟到 leave:请假 overtime:加班 gross:税前 "
        "salary:工资 net:净 component:构成 benefit:福利 achievement:达成 skill:技能 "
        "course:课程 exam:考试 recruiting:招聘 experience:经验 years:年 candidate:候选人 "
        "project:项目 allocation:投入 logged:登记 labor:人力 lost:损失 campaign:活动 "
        "creative:素材 media:媒体 impression:曝光 click:点击 device:设备 session:会话 "
        "landing:落地 page:页面 duration:持续 search:搜索 term:词 result:结果 attributed:归因 "
        "revenue:收入 attribution:归因 model:模型 operating:操作 system:系统 browser:浏览器 "
        "latency:延迟 ms:毫秒 experiment:实验 metric:指标 conversion:转化 response:响应 "
        "market:市场 index:指数 growth:增长 weather:天气 holiday:假日 flag:标志 competitor:竞品 "
        "indicator:指标 sentiment:情绪 mention:提及 error:错误 energy:能耗 kwh:千瓦时 "
        "electricity:电力 renewable:可再生 share:占比"
    ).split()
)


# 生成字段中文解释，技术列名保留用于 SQL。
# Args: column - 字段定义。
# Returns: 中文字段含义。
def column_description(column: ColumnSpec) -> str:
    """主外键与业务字段统一生成中文释义。"""
    if column.description:
        return column.description
    if column.reference:
        return f"关联{build_catalog()[column.reference].description.split('，')[0]}的主键"
    return LABELS.get(
        column.name, "".join(WORDS.get(part, part) for part in column.name.split("_"))
    )


# 输出可供程序或知识文档共用的强弱关系定义。
# Args: 无。
# Returns: 强关联和弱关联列表。
def relationship_manifest() -> dict[str, Any]:
    """强关联保证键存在，弱关联只允许聚合后对照。"""
    catalog = build_catalog()
    strong = []
    for table in catalog.values():
        for column in table.columns:
            if column.reference:
                strong.append(
                    {
                        "from_source": table.source,
                        "from_table": table.name,
                        "from_column": column.name,
                        "to_source": catalog[column.reference].source,
                        "to_table": column.reference,
                        "to_column": "id",
                        "cardinality": "一对一"
                        if table.name in {"finance_invoices", "supply_shipments"}
                        and column.name == "order_id"
                        else "多对一",
                        "join_rule": "先按关联键聚合事实表，再关联；不可直接相乘明细行。",
                    }
                )
    weak = [
        {
            "from_table": f"insight_{suffix}",
            "to_table": "retail_orders",
            "keys": ["year_month", "region_code"],
            "grain": "地区×月份",
            "warning": "仅做趋势相关对照，不能推出因果；缺失月份保留为空，不填成零。",
        }
        for suffix in [
            "weather_daily",
            "market_indices",
            "competitor_prices",
            "economic_indicators",
            "social_mentions",
            "energy_readings",
        ]
    ]
    weak.extend(
        {
            "from_table": table.name,
            "to_table": "retail_orders",
            "keys": ["year_month", "region_code"],
            "grain": "地区×月份",
            "warning": "仅做趋势相关对照，不能推出因果；缺失月份保留为空，不填成零。",
        }
        for table in catalog.values()
        if table.subject and "弱关联" in table.description
    )
    return {"strong": strong, "weak": weak}


# 渲染单张表的字段、粒度与口径正文，供合并文档按表分节复用。
# Args: table - 表规格；config - 生成配置；count - 目标行数。
# Returns: 不含顶层标题的中文 Markdown 正文。
def _schema_body(table: TableSpec, config: GenerationConfig, count: int) -> str:
    lines = [
        f"数据源：{SOURCES[table.source][1]}（{table.source} / {SOURCES[table.source][0]}）",
        f"业务主题：{table.subject or '基础经营'}。业务粒度：{table.description}。",
        f"模拟行数：{count:,}。主键：`id`。时间：{config.start_date} 至 {config.end_date}。",
        "所有内容均为合成测试数据；未来情景不是实际经营结果。",
        "",
        "## 表结构与字段含义",
        "",
        "| 字段 | 类型 | 字段含义 | 可空 | 生成/枚举规则 |",
        "|---|---|---|---|---|",
    ]
    types = {
        "integer": "整数",
        "decimal": "定点数（18,2）",
        "date": "日期",
        "text": "中文文本/技术编码",
    }
    for column in table.columns:
        rule = (
            column.recipe[5:].replace("|", "、")
            if column.recipe.startswith("enum.")
            else (f"`{column.reference}.id`" if column.reference else "固定种子确定性模拟")
        )
        lines.append(
            f"| `{column.name}` | {types[column.kind]} | {column_description(column)} | {'是' if column.nullable else '否'} | {rule} |"
        )
    lines += [
        "",
        "## 业务粒度与关联",
        "",
        "每个 id 为一条该表业务记录；同一天可有多条记录，不能按日期直接拼接两个事实表。",
    ]
    if table.name == "retail_order_items":
        lines.append(
            "每订单固定三条明细，SUM(line_amount) 等于订单 net_amount；退款的行金额为负，quantity=1，unit_price 为行金额绝对值。"
        )
    if table.name in {"finance_invoices", "supply_shipments"}:
        lines.append(
            "每个订单恰有一条对应记录，order_id 唯一；业务日期采用订单日期，而非独立开票/签收时间。"
        )
    for column in table.columns:
        if column.reference:
            lines.append(
                f"- 强关联：`{table.name}.{column.name}` → `{column.reference}.id`。跨源分别查询并先聚合。"
            )
    lines += [
        "",
        "## 口径限制",
        "",
        "`scenario` 分为历史模拟、当年模拟、未来情景；每个自然年都不等于真实数据。",
        "除明确的订单头—三明细—单发票对账外，其他金额列是独立模拟指标，不能假定所有账务明细自动闭合。",
        "比率为 0—1；评分为 1—5；备注 CSV 空值使用 \\N；空备注与空字符串不等价。",
    ]
    return "\n".join(lines) + "\n"


# 把一个数据源的全部表结构合并为单份按表分节的文档，降低上传文件数。
# Args: source - 数据源标识；tables - 该源的全部表规格；config - 生成配置；counts - 逐表行数。
# Returns: 可直接上传的中文 Markdown，每张表以 "### 表: <表名> — <中文名>" 开头。
def _merged_schema_document(
    source: str,
    tables: list[TableSpec],
    config: GenerationConfig,
    counts: dict[str, int],
) -> str:
    logger.debug("合并数据源表结构文档入口", source=source, table_count=len(tables))
    lines = [
        f"# {SOURCES[source][1]}表结构总览",
        "",
        f"数据源：{SOURCES[source][1]}（{source} / {SOURCES[source][0]}）",
        f"本文件收录该源全部 {len(tables)} 张表的完整结构定义，每张表以 `### 表: <表名> — <中文名>` 开头。",
        "上传本文件必须显式指定 table 分块策略（strategy=table），否则整篇会被当作单个分块，语义检索失效。",
        "",
    ]
    for table in tables:
        lines.append(f"### 表: {table.name} — {table.description}")
        lines.append("")
        lines.append(_schema_body(table, config, counts[table.name]))
        lines.append("")
    content = "\n".join(lines)
    logger.info(
        "合并数据源表结构文档完成",
        source=source,
        table_count=len(tables),
        chars=len(content),
    )
    return content


# 生成不依赖自然语言输出的跨域测试问题和可执行聚合 SQL。
# Args: manifest - 当前导出的实际汇总。
# Returns: 中文问题及验收规则。
def _questions(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    cases = [
        (
            "按地区和月份统计净销售额，并区分历史模拟与未来情景",
            ["retail"],
            "retail_orders",
            "net_amount",
            "按年月、地区、scenario 分组",
        ),
        (
            "核对订单净额和财务发票金额是否一致",
            ["retail", "finance"],
            "finance_invoices",
            "invoice_amount",
            "每单一票，与 retail_orders 按 order_id=id 对账，差额应为零",
        ),
        (
            "分析地区订单销售额和配送费用",
            ["retail", "supply"],
            "supply_shipments",
            "shipping_cost",
            "每单一次发运，先按订单对齐",
        ),
        (
            "比较广告归因收入与订单净销售额",
            ["insight", "retail"],
            "insight_order_attribution",
            "attributed_revenue",
            "归因记录可能多对一，不能等同唯一订单收入",
        ),
        (
            "比较不同部门员工薪资与报销费用",
            ["workforce", "finance"],
            "finance_expenses",
            "expense_amount",
            "先按 employee_id 聚合，再关联员工部门",
        ),
        (
            "分析商品采购成本和库存水平",
            ["retail", "supply"],
            "supply_purchase_lines",
            "unit_cost",
            "单位成本不直接相加，应按采购数量加权",
        ),
        (
            "比较门店销售目标与实际净销售额",
            ["retail"],
            "retail_sales_targets",
            "target_amount",
            "按门店和年月分别聚合，不拼接原始事实",
        ),
        (
            "分析年度成本中心预算与实际费用",
            ["finance"],
            "finance_budget_actuals",
            "actual_amount",
            "按 budget_id 聚合；不同版本预算不得重复累计",
        ),
        (
            "分析天气变化与地区销售趋势相关性",
            ["insight", "retail"],
            "insight_weather_daily",
            "temperature_c",
            "按地区月份取平均气温并对照净销售额，相关不等于因果",
        ),
        (
            "比较市场景气指数和地区订单走势",
            ["insight", "retail"],
            "insight_market_indices",
            "market_index",
            "指数取平均；仅弱关联",
        ),
        (
            "分析员工项目投入工时和人力成本",
            ["workforce"],
            "workforce_timesheets",
            "labor_cost",
            "按员工项目和月份聚合，避免 assignments 导致扇出",
        ),
        (
            "统计各渠道退款规模和退款订单数",
            ["retail", "finance"],
            "finance_refunds",
            "refunded_amount",
            "退款金额为非正；资金退款表是独立模拟，不宣称与订单全集闭合",
        ),
        (
            "比较未来十年物流碳排放情景",
            ["supply"],
            "supply_carbon_emissions",
            "emission_kg",
            "仅筛选未来情景，不标为真实预测",
        ),
        (
            "检查会计分录是否借贷平衡",
            ["finance"],
            "finance_journal_lines",
            "debit_amount",
            "本样本每行 debit_amount=credit_amount，差额零",
        ),
        (
            "分析实验组与对照组转化率",
            ["insight"],
            "insight_conversions",
            "conversion_value",
            "转化率为 SUM(converted)/COUNT(*)，不能相加比率",
        ),
    ]
    summaries = {table["name"]: table for table in manifest["tables"]}
    questions = []
    for index, (question, sources, table, measure, rule) in enumerate(cases, 1):
        aggregate = "AVG" if measure in {"unit_cost", "temperature_c", "market_index"} else "SUM"
        questions.append(
            {
                "id": f"多域-{index:02d}",
                "question": question,
                "sources": sources,
                "table": table,
                "acceptance": rule,
                "example_sql": f"SELECT year_month, region_code, {aggregate}({measure}) AS metric_value FROM {table} GROUP BY year_month, region_code",
                "available": set(sources) <= set(manifest["sources"]),
                "expected_table_sum": summaries.get(table, {}).get("sums", {}).get(measure),
            }
        )
    return questions


# 为每个扩展业务主题提供可执行查询及口径限制。
# Args: manifest - 当前导出清单。
# Returns: 五十个独立主题的中文问答验收案例。
def _subject_questions(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    from src.testdata.sql import quote_identifier

    questions = []
    seen = set()
    for table in build_catalog().values():
        key = (table.source, table.subject)
        if table.source not in manifest["sources"] or not table.subject or key in seen:
            continue
        seen.add(key)
        dialect = SOURCES[table.source][0]
        name = quote_identifier(table.name, dialect)
        month = quote_identifier("year_month", dialect)
        region = quote_identifier("region_code", dialect)
        scenario = quote_identifier("scenario", dialect)
        questions.append(
            {
                "id": f"subject_{table.source}_{len(questions) + 1:02d}",
                "question": f"{table.subject}中，{table.description}按年月、地区和历史/未来情景的记录数有什么差异？",
                "sources": [table.source],
                "tables": [table.name],
                "subject": table.subject,
                "example_sql": f"SELECT {month}, {region}, {scenario}, COUNT(*) AS record_count FROM {name} GROUP BY {month}, {region}, {scenario}",
                "available": True,
                "acceptance": "分组计数之和等于该表行数；未来情景独立展示，比例列不能相加。",
                "expected_rows": next(
                    item["rows"] for item in manifest["tables"] if item["name"] == table.name
                ),
            }
        )
    return questions


# 生成分级知识资料及显式上传清单，不接触登录身份或网络。
# Args: output - 产物根目录；config - 配置；manifest - 已生成表汇总。
# Returns: 上传清单与公共问答样例。
def generate_knowledge(
    output: Path, config: GenerationConfig, manifest: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """生成租户资料和按拥有者分目录的完全虚构私有资料。"""
    from src.testdata.exporter import file_digest

    logger.debug("生成中文分级知识资料入口", sources=manifest["sources"])
    catalog = build_catalog()
    counts = row_counts(config)
    documents: list[dict[str, Any]] = []

    # 统一落盘并显式标注权限，不将目录名当作权限控制。
    # Args: relative - 相对路径；body - 中文正文；scope - 上传范围；category - 文档分类；metadata - 辅助信息。
    # Returns: 无返回值。
    def save(relative: str, body: str, scope: str, category: str, **metadata: Any) -> None:
        logger.debug("写入模拟知识文件", scope=scope, category=category)
        path = output / "knowledge" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        documents.append(
            {
                "file": path.relative_to(output).as_posix(),
                "knowledge_scope": scope,
                "category": category,
                "sha256": file_digest(path),
                **metadata,
            }
        )

    for source in manifest["sources"]:
        tables = [table for table in catalog.values() if table.source == source]
        # 每源合并为单份表结构文档，625 份逐表文件降为 5 份，向量分块粒度不变。
        save(
            f"tenant/{source}/表结构总览.md",
            _merged_schema_document(source, tables, config, counts),
            "tenant",
            "表结构",
            datasource=source,
            tables=[table.name for table in tables],
        )
        overview = [
            f"# {SOURCES[source][1]}业务词典",
            "",
            "此文件只含共享业务定义，不含私有审批阈值。",
            "",
        ]
        overview.extend(
            f"- `{table.name}`：{table.description}；{counts[table.name]:,} 行。"
            for table in tables
        )
        save(
            f"tenant/{source}/业务词典.md",
            "\n".join(overview),
            "tenant",
            "业务词典",
            datasource=source,
        )
        for subject in dict.fromkeys(table.subject for table in tables if table.subject):
            members = [table for table in tables if table.subject == subject]
            lines = [
                f"# {SOURCES[source][1]}：{subject}",
                "",
                "所有业务记录均为中文合成数据；历史和未来情景不可混作真实经营数据。",
                "",
                "## 业务流程与表粒度",
                "",
                *[
                    f"- `{table.name}`：{table.description}；{counts[table.name]:,} 行。"
                    for table in members
                ],
                "",
                "## 关联和分析口径",
                "",
                "强关联按表结构文档中的实体键联结；多事实表必须先聚合再联结，避免金额放大。",
                "弱关联只按地区、年月、渠道等共同维度进行趋势对照，不把相关性解释为因果。",
                "金额按字段口径求和，评分和比例按明确分母计算或展示平均值，不能直接相加。",
                "除订单头—明细—发票外，独立模拟金额不构成会计勾稽关系。",
                "",
                "## 可测试问题",
                "",
                f"{subject}相关业务按月份、区域、渠道和情景的规模如何变化？",
                "关联实体有哪些地区差异？哪些月份出现零值、负值或空备注？",
                "未来十年的情景指标与历史十年如何分开比较？",
                "",
                "## 权限",
                "",
                "本文只包含租户可共享的合成业务口径；私有审批、人员薪酬策略见对应拥有者私有文档，须单独授权。",
            ]
            save(
                f"tenant/{source}/主题_{subject}.md",
                "\n".join(lines),
                "tenant",
                "业务主题",
                datasource=source,
                subject=subject,
            )
    relationships = relationship_manifest()
    relationship_lines = [
        "# 跨数据源业务关系",
        "",
        "## 强关联",
        "",
        "事实表先按连接键聚合；不能把两个一对多表直接相乘。",
        "",
    ]
    relationship_lines.extend(
        f"- `{item['from_table']}.{item['from_column']}` → `{item['to_table']}.id`：{item['cardinality']}。"
        for item in relationships["strong"]
    )
    relationship_lines += [
        "",
        "## 弱关联",
        "",
        "按地区×月份分别聚合后对照；相关不代表因果，缺失不代表零。",
    ]
    relationship_lines.extend(
        f"- `{item['from_table']}` 与 `{item['to_table']}`：地区编码 `region_code`、年月 `year_month`。"
        for item in relationships["weak"]
    )
    save("tenant/跨源业务关系.md", "\n".join(relationship_lines), "tenant", "业务关系")
    definitions = [
        "# 指标口径与模拟边界",
        "",
        f"固定 seed={config.seed}，日期 {config.start_date} 至 {config.end_date}。",
        "业务状态使用中文；表名、列名和币种为技术标识。所有人名、公司和政策均非真实资料。",
        "净销售额=SUM(retail_orders.net_amount)，退款为负、取消为零；每单三明细合计与净额一致。",
        "finance_invoices 每单一票，invoice_amount 与订单 net_amount 完全一致，币种固定 CNY。",
        "非取消订单净额绝对值=原始金额-折扣；退款代表负向收入。取消订单净额为零。",
        "其他业务表是独立模拟的运营指标，不应假定所有子账、余额、预算和费用自动对账。",
        "期间、实体和状态为合成快照，不等于现实主数据生命周期；不作为真实财务或劳动决策依据。",
        "历史模拟、当年模拟、未来情景需要分开查询；未来十年只是模拟情景，不是预测承诺。",
        f"渠道：{'、'.join(CHANNELS)}。",
        "地区映射："
        + "；".join(f"{code}={city}" for code, city in zip(REGIONS, CITIES, strict=True)),
        "收入/成本总额使用 SUM；单价与指数不可直接累计，比率必须用分子分母重算。",
    ]
    save("tenant/指标口径与模拟边界.md", "\n\n".join(definitions), "tenant", "指标口径")
    questions = _questions(manifest) + _subject_questions(manifest)
    logger.info(
        "主题问答渲染边界",
        questions=len(questions),
        subjects=sum("subject" in question for question in questions),
    )
    save(
        "tenant/跨源问答验收样例.md",
        "# 中文问答验收\n\n"
        + "\n\n".join(
            f"## {item['id']} {item['question']}\n\n涉及：{'、'.join(item['sources'])}。\n\n验收：{item['acceptance']}。\n\n```sql\n{item['example_sql']}\n```"
            for item in questions
        ),
        "tenant",
        "问答样例",
    )
    private_materials = {
        "财务负责人": [
            (
                "客户授信审批细则",
                "私有检索校验词：青岚授信样本。完全虚构：C 级客户单笔信用额度 36000 元，超额转人工审核。使用 finance_credit_limits 与 retail_customers 按 customer_id 关联；不得宣称这些阈值是公共政策。",
            ),
            (
                "资金压力情景备忘",
                "私有检索校验词：玉衡资金样本。完全虚构：保守情景下预留 18% 流动性缓冲；finance_cash_forecasts 只作为模拟输入。查询未来情景时明确不是真实预测。",
            ),
        ],
        "人力负责人": [
            (
                "薪酬绩效保密规则",
                "私有检索校验词：秘密薪酬样本。完全虚构：绩效五星的模拟年度奖金上限为基本薪资的 22%，四星为 12%。使用 workforce_payroll 与 workforce_performance，按 employee_id、year_month 聚合后关联。",
            ),
            (
                "组织调整模拟方案",
                "私有检索校验词：紫桐组织样本。完全虚构：未来情景中的研发岗位预算上浮 9%，并为培训保留 6% 预算；不涉及真实员工去留决定。关联 workforce_departments、workforce_projects 和 finance_budgets。",
            ),
        ],
        "经营负责人": [
            (
                "区域经营策略草案",
                "私有检索校验词：玄武经营样本。完全虚构：华南地区未来情景的目标增长率设为 16%，目标毛利率 31%。共享经营目标不包含这份私有草案，回答时不能替换正式口径。",
            ),
            (
                "竞品价格应对预案",
                "私有检索校验词：白鹭竞价样本。完全虚构：竞品价格指数连续三期低于 92 时建议人工评审，而非自动降价。insight_competitor_prices 与 retail_orders 只能按地区月份弱关联，不能推断因果。",
            ),
        ],
    }
    for owner, materials in private_materials.items():
        for title, body in materials:
            save(
                f"private/{owner}/{title}.md",
                f"# {title}\n\n级别：私有，仅限{owner}。\n\n必须以{owner}账号登录，使用 knowledge_scope=private 上传。不得上传至 tenant 或 system。\n\n{body}\n\n权限验收：拥有者可以检索；同租户其他用户及其他租户均不得检索本文件或检索校验词。目录名称本身不提供访问控制。\n",
                "private",
                "私有业务规则",
                intended_owner=owner,
            )
    save(
        "上传操作说明.md",
        "# 知识资料上传操作\n\n所有正文均为模拟资料，不含真实敏感信息。\n\n1. 按 knowledge_upload_manifest.json 的 knowledge_scope 分批处理，不要批量混传全部目录。\n2. tenant 文件用租户管理员账号上传；private 文件必须切换到 intended_owner 对应账号，选择私有范围。私有归属由服务端当前身份决定，不由文件名决定。\n3. 使用知识库页面或 POST /api/v1/knowledge/docs/upload，并设置 knowledge_scope=tenant 或 private；datasource 绑定为本租户已注册的数据源实际名称，不能盲用域别名。\n4. 每个源的 125 张表结构已合并为 tenant/<源>/表结构总览.md 单份文档，上传时必须选择 table 分块策略（strategy=table），保证每张表各占一个分块；跨源关系、通用口径与问答样例不绑定单一数据源。\n5. 上传后分别用拥有者、同租户非拥有者和其他租户身份测试私有召回隔离。不要把私有文件合并进公共词典。\n6. 不要将百万行数据文件上传到知识库；数据库导入用数据文件，知识库只上传此清单中的 Markdown。\n7. 本脚本不执行自动上传，也不提升到 system 级别。\n",
        "tenant",
        "使用说明",
    )
    logger.info("中文分级知识资料生成完成", documents=len(documents), private_documents=6)
    return documents, questions
