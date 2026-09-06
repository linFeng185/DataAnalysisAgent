"""五个数据源的专属业务表与字段目录，不复制共享表。"""

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict

SOURCES = {
    "retail": ("mysql", "零售交易与客户"),
    "finance": ("postgres", "财务核算与资金"),
    "supply": ("mssql", "供应链与制造物流"),
    "workforce": ("oracle", "组织人力与项目"),
    "insight": ("clickhouse", "数字营销与外部环境"),
}


class ColumnSpec(BaseModel):
    """单列逻辑类型、生成配方和可审计引用。"""

    model_config = ConfigDict(frozen=True)
    name: str
    kind: Literal["integer", "decimal", "date", "text"]
    recipe: str
    reference: str | None = None
    nullable: bool = False
    description: str = ""


class TableSpec(BaseModel):
    """具有独立业务含义的表，权重决定数据规模。"""

    model_config = ConfigDict(frozen=True)
    name: str
    source: str
    description: str
    weight: int
    columns: tuple[ColumnSpec, ...]
    subject: str = ""


# 每条声明均对应真实业务粒度，引用跨域主数据而非复制同名维表。
DEFINITIONS = {
    "retail": [
        (
            "calendar",
            "自然日历，含财年、季度和节假日",
            1,
            "fiscal_year:int quarter:int is_weekend:flag",
        ),
        (
            "customers",
            "客户主档与会员分群",
            5,
            "customer_name:label segment:enum.普通|银卡|金卡|企业 age:int acquisition:enum.自然|推荐|广告",
        ),
        ("categories", "商品品类及毛利目标", 1, "category_name:label margin_target:ratio"),
        ("brands", "品牌定位及品牌等级", 1, "brand_name:label positioning:enum.经济|主流|高端"),
        (
            "products",
            "商品主档与成本价格",
            3,
            "category_id:ref.retail_categories brand_id:ref.retail_brands product_name:label unit_price:price unit_cost:cost",
        ),
        (
            "stores",
            "门店面积与经营业态",
            1,
            "store_name:label store_type:enum.旗舰|社区|加盟 area_sqm:metric",
        ),
        ("channels", "销售渠道字典与费率", 1, "channel_name:label commission_rate:ratio"),
        (
            "memberships",
            "会员订阅与权益",
            2,
            "customer_id:ref.retail_customers tier:enum.基础|专业|尊享 annual_fee:money",
        ),
        (
            "addresses",
            "客户脱敏配送地址",
            2,
            "customer_id:ref.retail_customers city_name:city address_tag:enum.家庭|办公|自提",
        ),
        (
            "promotions",
            "促销计划与折扣策略",
            1,
            "promotion_name:label discount_rate:ratio campaign_type:enum.满减|折扣|赠品",
        ),
        (
            "coupons",
            "优惠券发放及核销",
            3,
            "promotion_id:ref.retail_promotions customer_id:ref.retail_customers face_value:money redeemed:flag",
        ),
        (
            "orders",
            "订单头，净额等于明细合计，退款为负",
            15,
            "customer_id:ref.retail_customers store_id:ref.retail_stores order_status:order_status gross_amount:gross discount_amount:discount net_amount:net",
        ),
        (
            "order_items",
            "订单明细，每单三行，行净额可汇总",
            45,
            "order_id:ref.retail_orders product_id:ref.retail_products quantity:quantity unit_price:price line_amount:line",
        ),
        (
            "returns",
            "退货原因与损失",
            3,
            "order_id:ref.retail_orders return_reason:enum.质量|不适合|误购 return_amount:refund",
        ),
        (
            "reviews",
            "购买评价与情绪标签",
            3,
            "order_id:ref.retail_orders rating:rating sentiment:enum.正面|中性|负面",
        ),
        (
            "carts",
            "购物车留存与弃购",
            4,
            "customer_id:ref.retail_customers product_id:ref.retail_products abandoned:flag cart_value:money",
        ),
        (
            "wishlists",
            "收藏偏好",
            2,
            "customer_id:ref.retail_customers product_id:ref.retail_products priority:int",
        ),
        (
            "loyalty_moves",
            "会员积分流水",
            2,
            "customer_id:ref.retail_customers points_delta:signed movement_type:enum.获取|兑换|到期",
        ),
        (
            "price_history",
            "商品价格变更历史",
            2,
            "product_id:ref.retail_products previous_price:price new_price:money reason:enum.调价|清仓|新品",
        ),
        (
            "sales_targets",
            "门店渠道销售目标",
            1,
            "store_id:ref.retail_stores target_amount:money target_units:metric",
        ),
        (
            "subscriptions",
            "周期购订阅",
            1,
            "customer_id:ref.retail_customers product_id:ref.retail_products frequency:enum.周|月|季 active:flag",
        ),
        (
            "gift_cards",
            "礼品卡余额与激活",
            1,
            "customer_id:ref.retail_customers face_amount:money balance_amount:cost activated:flag",
        ),
        (
            "service_cases",
            "零售售后工单与满意度",
            2,
            "order_id:ref.retail_orders issue_type:enum.商品|支付|配送 resolution_hours:metric satisfaction:rating",
        ),
        (
            "store_visits",
            "线下门店到访与转化",
            3,
            "store_id:ref.retail_stores customer_id:ref.retail_customers dwell_minutes:metric converted:flag",
        ),
        (
            "consents",
            "客户授权与撤回记录",
            1,
            "customer_id:ref.retail_customers purpose:enum.营销|分析|通知 granted:flag",
        ),
    ],
    "finance": [
        (
            "accounts",
            "会计科目及账户类别",
            1,
            "account_name:label account_type:enum.资产|负债|收入|费用",
        ),
        (
            "cost_centers",
            "成本中心与责任组织",
            1,
            "center_name:label department_id:ref.workforce_departments",
        ),
        (
            "currencies",
            "币种字典及精度",
            1,
            "currency:currency currency_name:label decimal_places:int",
        ),
        (
            "fx_rates",
            "每日币种汇率",
            2,
            "currency:currency exchange_rate:ratio base_currency:enum.CNY",
        ),
        (
            "invoices",
            "一单一票的人民币净额快照",
            15,
            "order_id:ref.retail_orders customer_id:ref.retail_customers invoice_amount:net currency:enum.CNY",
        ),
        (
            "invoice_lines",
            "发票科目分摊明细",
            15,
            "invoice_id:ref.finance_invoices account_id:ref.finance_accounts allocated_amount:money",
        ),
        (
            "receipts",
            "客户收款及支付方式",
            10,
            "order_id:ref.retail_orders receipt_amount:net payment_method:enum.银行卡|电子钱包|对公",
        ),
        (
            "refunds",
            "退款资金流水",
            3,
            "order_id:ref.retail_orders refunded_amount:refund refund_reason:enum.取消|售后|差价",
        ),
        (
            "journals",
            "会计凭证头及审核状态",
            4,
            "journal_name:label posted:flag approval:enum.待审|通过|退回",
        ),
        (
            "journal_lines",
            "借贷分录，单行借贷平衡",
            12,
            "journal_id:ref.finance_journals account_id:ref.finance_accounts debit_amount:gross credit_amount:gross",
        ),
        (
            "receivables",
            "应收账龄与坏账准备",
            4,
            "customer_id:ref.retail_customers outstanding_amount:money overdue_days:int provision_amount:cost",
        ),
        (
            "payables",
            "供应商应付及到期情况",
            4,
            "supplier_id:ref.supply_suppliers payable_amount:money overdue_days:int",
        ),
        (
            "bank_accounts",
            "脱敏银行账户与币种",
            1,
            "bank_name:label currency:currency account_type:enum.基本|一般|外币",
        ),
        (
            "bank_flows",
            "银行资金流与手续费",
            6,
            "bank_account_id:ref.finance_bank_accounts flow_amount:net fee_amount:discount direction:enum.收入|支出",
        ),
        (
            "reconciliations",
            "交易对账差异",
            3,
            "order_id:ref.retail_orders difference_amount:delta reconciled:flag",
        ),
        (
            "budgets",
            "责任中心预算",
            2,
            "cost_center_id:ref.finance_cost_centers budget_amount:money version:enum.初版|调整|终版",
        ),
        (
            "budget_actuals",
            "预算执行与偏差",
            2,
            "budget_id:ref.finance_budgets actual_amount:money variance_amount:delta",
        ),
        ("tax_rates", "税种与适用税率", 1, "tax_type:enum.增值税|所得税|附加税 tax_rate:ratio"),
        (
            "tax_filings",
            "纳税申报和应纳税额",
            2,
            "tax_rate_id:ref.finance_tax_rates taxable_amount:gross tax_amount:tax",
        ),
        (
            "fixed_assets",
            "固定资产原值与残值",
            2,
            "cost_center_id:ref.finance_cost_centers asset_name:label original_value:gross residual_value:cost",
        ),
        (
            "depreciation",
            "资产折旧记录",
            3,
            "asset_id:ref.finance_fixed_assets depreciation_amount:cost method:enum.直线|年限总和",
        ),
        (
            "expenses",
            "员工费用报销",
            3,
            "employee_id:ref.workforce_employees cost_center_id:ref.finance_cost_centers expense_amount:money expense_type:enum.差旅|办公|招待",
        ),
        (
            "cash_forecasts",
            "资金预测情景，非真实预测",
            1,
            "bank_account_id:ref.finance_bank_accounts expected_inflow:gross expected_outflow:cost confidence:ratio",
        ),
        (
            "credit_limits",
            "客户信用额度及评级",
            1,
            "customer_id:ref.retail_customers credit_limit:money risk_grade:enum.A|B|C|D",
        ),
        (
            "audit_findings",
            "财务审计发现及整改",
            1,
            "account_id:ref.finance_accounts severity:enum.低|中|高 exposure_amount:money resolved:flag",
        ),
    ],
    "supply": [
        (
            "suppliers",
            "供应商资质及等级",
            2,
            "supplier_name:label supplier_tier:enum.战略|优选|普通 country:enum.中国|德国|越南",
        ),
        (
            "warehouses",
            "仓库容量与设施类型",
            1,
            "warehouse_name:label capacity_units:metric warehouse_type:enum.中心|区域|前置",
        ),
        (
            "carriers",
            "承运商及运输方式",
            1,
            "carrier_name:label transport_mode:enum.公路|铁路|航空|海运",
        ),
        (
            "routes",
            "运输线路与里程",
            1,
            "carrier_id:ref.supply_carriers origin_city:city destination_city:city distance_km:metric",
        ),
        (
            "purchase_orders",
            "采购订单与供应商报价",
            8,
            "supplier_id:ref.supply_suppliers purchase_amount:money purchase_status:enum.新建|在途|完成",
        ),
        (
            "purchase_lines",
            "采购商品数量与成本",
            12,
            "purchase_id:ref.supply_purchase_orders product_id:ref.retail_products quantity:quantity unit_cost:cost",
        ),
        (
            "goods_receipts",
            "采购到货与验收",
            6,
            "purchase_id:ref.supply_purchase_orders warehouse_id:ref.supply_warehouses received_units:metric accepted:flag",
        ),
        (
            "inventory_daily",
            "商品仓库库存快照",
            15,
            "warehouse_id:ref.supply_warehouses product_id:ref.retail_products on_hand:metric reserved_units:quantity stockout:flag",
        ),
        (
            "stock_moves",
            "库存出入库流水",
            10,
            "warehouse_id:ref.supply_warehouses product_id:ref.retail_products quantity_delta:signed movement:enum.入库|出库|调整",
        ),
        (
            "transfers",
            "仓间调拨记录",
            3,
            "warehouse_id:ref.supply_warehouses product_id:ref.retail_products transfer_units:metric lead_hours:metric",
        ),
        (
            "shipments",
            "订单发运及配送绩效",
            15,
            "order_id:ref.retail_orders carrier_id:ref.supply_carriers shipping_cost:cost delay_days:int on_time:flag",
        ),
        (
            "shipment_events",
            "配送节点轨迹",
            20,
            "shipment_id:ref.supply_shipments event_type:enum.揽收|中转|派送|签收 elapsed_hours:metric",
        ),
        (
            "delivery_claims",
            "配送理赔和责任",
            2,
            "shipment_id:ref.supply_shipments claim_amount:money claim_type:enum.破损|丢失|延误",
        ),
        (
            "freight_rates",
            "线路运价与燃油附加",
            1,
            "route_id:ref.supply_routes base_rate:money fuel_surcharge:discount",
        ),
        (
            "supplier_scores",
            "供应商履约质量评分",
            2,
            "supplier_id:ref.supply_suppliers quality_score:rating delivery_score:rating",
        ),
        (
            "quality_checks",
            "商品批次抽检",
            4,
            "product_id:ref.retail_products supplier_id:ref.supply_suppliers sample_units:quantity defect_rate:ratio",
        ),
        (
            "defects",
            "质量缺陷及返工成本",
            2,
            "product_id:ref.retail_products defect_type:enum.外观|性能|包装 rework_cost:cost",
        ),
        (
            "bills_of_material",
            "产品物料清单",
            2,
            "product_id:ref.retail_products material_name:label required_units:quantity",
        ),
        (
            "production_orders",
            "生产工单及完工量",
            4,
            "product_id:ref.retail_products planned_units:metric completed_units:quantity",
        ),
        (
            "production_runs",
            "产线工时与良率",
            4,
            "production_id:ref.supply_production_orders machine_hours:metric yield_rate:ratio",
        ),
        (
            "machines",
            "生产设备与维护等级",
            1,
            "machine_name:label machine_type:enum.组装|包装|检测 criticality:rating",
        ),
        (
            "maintenance",
            "设备保养与停机",
            2,
            "machine_id:ref.supply_machines downtime_hours:metric maintenance_cost:cost",
        ),
        (
            "demand_plans",
            "商品需求计划",
            2,
            "product_id:ref.retail_products planned_demand:metric safety_stock:quantity",
        ),
        (
            "reorder_rules",
            "商品补货阈值",
            1,
            "product_id:ref.retail_products warehouse_id:ref.supply_warehouses reorder_point:metric lot_size:quantity",
        ),
        (
            "carbon_emissions",
            "物流碳排放核算",
            2,
            "shipment_id:ref.supply_shipments emission_kg:metric accounting_scope:enum.范围一|范围二|范围三",
        ),
    ],
    "workforce": [
        (
            "departments",
            "组织部门与职能",
            1,
            "department_name:label function_type:enum.销售|研发|运营|支持",
        ),
        (
            "positions",
            "岗位序列及级别",
            1,
            "position_name:label job_family:enum.技术|业务|管理 job_grade:int",
        ),
        (
            "employees",
            "合成员工档案，不含真实个人信息",
            5,
            "department_id:ref.workforce_departments position_id:ref.workforce_positions employee_name:label employment_type:enum.全职|兼职|外包",
        ),
        (
            "contracts",
            "员工合同及续签状态",
            3,
            "employee_id:ref.workforce_employees contract_type:enum.固定|长期|劳务 renewed:flag",
        ),
        (
            "org_changes",
            "员工调岗与组织调整",
            2,
            "employee_id:ref.workforce_employees department_id:ref.workforce_departments change_type:enum.入职|调动|晋升|离职",
        ),
        (
            "attendance",
            "日出勤与迟到",
            20,
            "employee_id:ref.workforce_employees work_hours:hours late_minutes:int",
        ),
        (
            "leave_requests",
            "休假类型与审批",
            3,
            "employee_id:ref.workforce_employees leave_type:enum.年假|事假|病假 leave_hours:hours approved:flag",
        ),
        (
            "overtime",
            "加班时长与补偿",
            6,
            "employee_id:ref.workforce_employees overtime_hours:hours compensation:money",
        ),
        (
            "payroll",
            "员工月薪及扣款",
            15,
            "employee_id:ref.workforce_employees gross_salary:gross deductions:discount net_salary:net",
        ),
        (
            "payroll_items",
            "薪资构成明细",
            15,
            "payroll_id:ref.workforce_payroll component:enum.基本|奖金|补贴|扣款 component_amount:money",
        ),
        (
            "benefits",
            "员工福利计划",
            2,
            "employee_id:ref.workforce_employees benefit_type:enum.保险|餐补|交通 benefit_amount:money",
        ),
        (
            "performance",
            "员工绩效及目标达成",
            4,
            "employee_id:ref.workforce_employees rating:rating achievement_rate:ratio",
        ),
        ("skills", "能力技能目录", 1, "skill_name:label skill_group:enum.技术|通用|管理"),
        (
            "employee_skills",
            "员工技能等级",
            3,
            "employee_id:ref.workforce_employees skill_id:ref.workforce_skills proficiency:rating",
        ),
        ("courses", "培训课程目录", 1, "course_name:label course_hours:hours course_cost:money"),
        (
            "training",
            "员工学习与考试",
            4,
            "employee_id:ref.workforce_employees course_id:ref.workforce_courses completed:flag exam_score:metric",
        ),
        (
            "certifications",
            "技能认证及有效状态",
            2,
            "employee_id:ref.workforce_employees skill_id:ref.workforce_skills valid:flag",
        ),
        (
            "job_openings",
            "招聘需求与预算",
            1,
            "department_id:ref.workforce_departments position_id:ref.workforce_positions openings:int budget_salary:money",
        ),
        (
            "candidates",
            "合成候选人与招聘渠道",
            3,
            "candidate_name:label recruiting_channel:enum.内推|招聘站|校招 experience_years:int",
        ),
        (
            "applications",
            "岗位投递及筛选",
            4,
            "candidate_id:ref.workforce_candidates opening_id:ref.workforce_job_openings stage:enum.筛选|面试|录用|拒绝",
        ),
        (
            "interviews",
            "面试评价与录用建议",
            3,
            "application_id:ref.workforce_applications interviewer_id:ref.workforce_employees score:rating recommended:flag",
        ),
        (
            "projects",
            "项目预算与交付状态",
            2,
            "department_id:ref.workforce_departments project_name:label budget_amount:money delivery_status:enum.进行中|延期|完成",
        ),
        (
            "assignments",
            "员工项目投入配置",
            4,
            "employee_id:ref.workforce_employees project_id:ref.workforce_projects allocation_rate:ratio",
        ),
        (
            "timesheets",
            "项目工时与成本",
            15,
            "employee_id:ref.workforce_employees project_id:ref.workforce_projects logged_hours:hours labor_cost:cost",
        ),
        (
            "safety_incidents",
            "安全事件与整改",
            1,
            "department_id:ref.workforce_departments severity:enum.轻微|一般|重大 lost_hours:hours resolved:flag",
        ),
    ],
    "insight": [
        (
            "campaigns",
            "数字广告活动主档",
            1,
            "campaign_name:label objective:enum.拉新|转化|留存 budget_amount:money",
        ),
        (
            "ad_creatives",
            "广告素材与形式",
            1,
            "campaign_id:ref.insight_campaigns creative_name:label media_type:enum.图文|视频|直播",
        ),
        (
            "impressions",
            "广告曝光事件",
            20,
            "campaign_id:ref.insight_campaigns creative_id:ref.insight_ad_creatives impression_cost:cost viewable:flag",
        ),
        (
            "clicks",
            "广告点击事件",
            12,
            "campaign_id:ref.insight_campaigns click_cost:money device_type:enum.手机|桌面|平板",
        ),
        (
            "sessions",
            "数字会话与入口",
            10,
            "customer_id:ref.retail_customers session_seconds:metric landing_page:enum.首页|商品|活动 bounced:flag",
        ),
        (
            "pageviews",
            "页面访问及停留",
            20,
            "session_id:ref.insight_sessions page_type:enum.首页|详情|购物车|支付 duration_seconds:metric",
        ),
        (
            "searches",
            "站内搜索与无结果率",
            8,
            "customer_id:ref.retail_customers search_term:label result_count:int",
        ),
        (
            "funnel_events",
            "转化漏斗阶段事件",
            12,
            "customer_id:ref.retail_customers stage:enum.浏览|加购|结算|支付 event_count:quantity",
        ),
        (
            "order_attribution",
            "订单归因触点与渠道",
            8,
            "order_id:ref.retail_orders campaign_id:ref.insight_campaigns attributed_revenue:net attribution_model:enum.末次|首次|线性",
        ),
        (
            "devices",
            "设备画像和操作系统",
            2,
            "device_name:label operating_system:enum.Android|iOS|Windows browser_family:enum.Chrome|Safari|Edge",
        ),
        (
            "app_events",
            "应用功能使用事件",
            10,
            "device_id:ref.insight_devices event_name:enum.启动|登录|分享|收藏 latency_ms:metric",
        ),
        (
            "experiments",
            "实验定义及目标指标",
            1,
            "experiment_name:label target_metric:enum.转化|留存|客单价",
        ),
        (
            "assignments",
            "客户实验分组",
            4,
            "experiment_id:ref.insight_experiments customer_id:ref.retail_customers variant:enum.对照组|实验组",
        ),
        (
            "conversions",
            "实验转化及价值",
            4,
            "experiment_id:ref.insight_experiments variant:enum.对照组|实验组 converted:flag conversion_value:money",
        ),
        (
            "email_events",
            "邮件送达打开及点击",
            4,
            "campaign_id:ref.insight_campaigns customer_id:ref.retail_customers action:enum.送达|打开|点击|退订",
        ),
        (
            "push_events",
            "推送触达与唤回",
            4,
            "customer_id:ref.retail_customers delivered:flag opened:flag response_seconds:metric",
        ),
        (
            "social_mentions",
            "地区社媒热度，仅弱关联",
            3,
            "topic:enum.品质|服务|价格|品牌 sentiment:enum.正面|中性|负面 mentions:metric",
        ),
        (
            "market_indices",
            "地区市场景气指数，仅弱关联",
            2,
            "sector:enum.消费|制造|服务 market_index:metric growth_rate:delta",
        ),
        (
            "weather_daily",
            "地区天气日指标，仅弱关联",
            2,
            "temperature_c:temperature rainfall_mm:rain weather_type:enum.晴|阴|雨|雪",
        ),
        (
            "holidays",
            "地区假日与消费主题",
            1,
            "holiday_name:enum.春节|国庆|周末|工作日 holiday_flag:flag",
        ),
        (
            "competitor_prices",
            "竞品价位指数，仅弱关联",
            2,
            "category_group:enum.数码|家居|服饰|食品 competitor_name:label price_index:metric",
        ),
        (
            "economic_indicators",
            "地区宏观经济指标，仅弱关联",
            1,
            "indicator_name:enum.CPI|就业|收入|信心 indicator_value:metric",
        ),
        (
            "support_sentiment",
            "服务舆情趋势，仅弱关联",
            2,
            "topic:enum.配送|售后|产品 sentiment_score:ratio mention_count:metric",
        ),
        (
            "api_latency",
            "应用性能与可用性",
            5,
            "endpoint:enum.search|checkout|catalog|account latency_ms:metric error_rate:ratio",
        ),
        (
            "energy_readings",
            "地区能耗与电价，仅弱关联",
            3,
            "energy_kwh:metric electricity_price:money renewable_share:ratio",
        ),
    ],
}


# 解析紧凑字段声明，保持类型与生成规则只有一个来源。
# Args: token - 字段名与生成配方。
# Returns: 已校验的列规格。
def _column(token: str) -> ColumnSpec:
    name, recipe = token.split(":", 1)
    reference = recipe.removeprefix("ref.") if recipe.startswith("ref.") else None
    integer_recipes = {"pk", "int", "flag", "quantity", "rating", "signed", "hours"}
    text_recipes = {"label", "region", "channel", "scenario", "notes", "month", "city", "currency"}
    kind = "decimal"
    if reference or recipe in integer_recipes:
        kind = "integer"
    elif recipe in text_recipes or recipe.startswith("enum.") or recipe == "order_status":
        kind = "text"
    elif recipe == "date":
        kind = "date"
    return ColumnSpec(
        name=name, kind=kind, recipe=recipe, reference=reference, nullable=recipe == "notes"
    )


# 构建所有业务域的唯一目录供生成、导入和数据字典共用。
# Args: 无。
# Returns: 表名到不可变表规格的映射。
@lru_cache(maxsize=1)
def build_catalog() -> dict[str, TableSpec]:
    """返回五源各 125 张业务表，旧表定义保持兼容。"""
    from src.testdata.expanded_catalog import EXPANSIONS

    common = "id:pk event_date:date year_month:month region_code:region channel_code:channel scenario:scenario notes:notes"
    catalog = {}
    for source, definitions in DEFINITIONS.items():
        for suffix, description, weight, fields in definitions:
            name = f"{source}_{suffix}"
            catalog[name] = TableSpec(
                name=name,
                source=source,
                description=description,
                weight=weight,
                columns=tuple(_column(token) for token in f"{common} {fields}".split()),
            )
    for source, subjects in EXPANSIONS.items():
        if len(subjects) != 10:
            raise ValueError(f"{source} 必须包含十个扩展主题")
        for subject, definitions in subjects.items():
            lines = definitions.strip().splitlines()
            if len(lines) != 10:
                raise ValueError(f"{source}/{subject} 必须包含十张独立业务表")
            for line in lines:
                suffix, description, weight, fields = line.split(";", 3)
                columns = list(_column(token) for token in common.split())
                for token in fields.split():
                    name, recipe, meaning = token.split(":", 2)
                    columns.append(
                        _column(f"{name}:{recipe}").model_copy(update={"description": meaning})
                    )
                name = f"{source}_{suffix}"
                if name in catalog:
                    raise ValueError(f"重复表名: {name}")
                catalog[name] = TableSpec(
                    name=name,
                    source=source,
                    description=description,
                    weight=int(weight),
                    columns=tuple(columns),
                    subject=subject,
                )
    for table in catalog.values():
        for column in table.columns:
            if column.reference and column.reference not in catalog:
                raise ValueError(f"未知引用: {table.name}.{column.name}")
    return catalog
