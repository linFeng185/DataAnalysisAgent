"""每源十个业务主题、每主题十张独立表的扩展目录。"""

# 每行依次为表后缀、中文业务粒度、规模权重及带中文释义的字段。
EXPANSIONS = {
    "retail": {
        "企业销售": """
business_accounts;企业采购账户与规模;1;customer_id:ref.retail_customers:客户主键 industry:enum.制造|教育|医疗|服务:所属行业 annual_spend:money:年度采购额
account_contacts;企业账户联系职责;1;account_id:ref.retail_business_accounts:企业账户 contact_name:label:模拟联系人 contact_role:enum.采购|财务|审批:联系职责
sales_leads;企业销售线索评分;3;account_id:ref.retail_business_accounts:企业账户 lead_score:rating:线索评分 lead_origin:enum.会展|转介绍|外呼:线索渠道
opportunities;商机阶段与预计成交;3;lead_id:ref.retail_sales_leads:线索主键 deal_stage:enum.发现|方案|谈判|赢单|失单:商机阶段 expected_value:money:预计成交金额
quotations;商机报价及有效状态;3;opportunity_id:ref.retail_opportunities:商机主键 quoted_total:money:报价总额 quote_status:enum.草稿|送审|已发|过期:报价状态
quotation_lines;报价商品与供货数量;4;quotation_id:ref.retail_quotations:报价主键 product_id:ref.retail_products:商品主键 quoted_units:quantity:报价数量 negotiated_price:price:协商单价
sales_agreements;企业销售框架协议;1;account_id:ref.retail_business_accounts:企业账户 agreement_value:money:协议金额 renewal_option:flag:是否自动续约
agreement_terms;协议交付与支付条款;2;agreement_id:ref.retail_sales_agreements:协议主键 term_type:enum.付款|交付|质保:条款分类 tolerance_days:int:宽限天数
account_approvals;企业采购审批路径;2;account_id:ref.retail_business_accounts:企业账户 approval_limit:money:审批金额上限 approver_role:enum.部门主管|财务|总经理:审批角色
contract_releases;框架协议分次提货;4;agreement_id:ref.retail_sales_agreements:协议主键 order_id:ref.retail_orders:订单主键 release_status:enum.预约|出库|结清:提货状态
""",
        "全渠道履约": """
pickup_points;门店自提点服务能力;1;store_id:ref.retail_stores:门店主键 point_name:label:自提点名称 daily_capacity:metric:每日处理能力
pickup_bookings;订单自提预约;4;order_id:ref.retail_orders:订单主键 point_id:ref.retail_pickup_points:自提点主键 pickup_slot:enum.上午|下午|晚间:预约时段
pickup_handoffs;自提交接及核销;4;booking_id:ref.retail_pickup_bookings:预约主键 waiting_minutes:int:等待分钟 identity_checked:flag:是否完成核验
store_ship_tasks;门店发货任务分配;4;order_id:ref.retail_orders:订单主键 store_id:ref.retail_stores:门店主键 handling_minutes:metric:处理分钟
delivery_slots;区域配送时段配额;2;slot_label:enum.早间|午间|晚间:配送时段 reserved_capacity:metric:预留运力 slot_fee:money:时段服务费
delivery_promises;订单时效承诺;4;order_id:ref.retail_orders:订单主键 slot_id:ref.retail_delivery_slots:时段主键 promised_hours:hours:承诺小时数
split_allocations;订单拆分履约分配;4;order_id:ref.retail_orders:订单主键 warehouse_id:ref.supply_warehouses:仓库主键 allocated_units:quantity:分配数量
substitutions;缺货替代商品确认;2;order_id:ref.retail_orders:订单主键 product_id:ref.retail_products:替代商品 customer_accepted:flag:客户是否接受
delivery_exceptions;订单交付异常原因;3;order_id:ref.retail_orders:订单主键 exception_type:enum.缺货|地址错误|拒收|天气:异常原因 recovery_hours:hours:恢复小时数
fulfillment_charges;订单履约附加费用;3;order_id:ref.retail_orders:订单主键 charge_type:enum.加急|包装|上楼:费用类型 charge_amount:money:附加费用
""",
        "商品经营": """
assortment_plans;门店商品组合规划;1;store_id:ref.retail_stores:门店主键 category_id:ref.retail_categories:品类主键 shelf_share:ratio:货架份额
assortment_lines;商品组合上架清单;3;plan_id:ref.retail_assortment_plans:组合计划 product_id:ref.retail_products:商品主键 display_priority:rating:陈列优先级
seasonal_ranges;季节商品系列;1;range_name:label:系列名称 season:enum.春|夏|秋|冬:销售季节 target_margin:ratio:目标毛利率
range_members;季节系列商品归属;2;range_id:ref.retail_seasonal_ranges:系列主键 product_id:ref.retail_products:商品主键 launch_wave:int:上市批次
product_attributes;商品扩展属性值;4;product_id:ref.retail_products:商品主键 attribute_name:enum.颜色|材质|尺寸|产地:属性名称 attribute_value:label:属性取值
product_variants;商品规格与型号;3;product_id:ref.retail_products:商品主键 variant_name:label:规格名称 variant_price:price:规格价格
product_bundles;组合商品定价;1;bundle_name:label:组合名称 bundle_price:money:组合价格 bundle_type:enum.套餐|礼盒|配件包:组合类型
bundle_components;组合商品构成;3;bundle_id:ref.retail_product_bundles:组合主键 product_id:ref.retail_products:组成商品 component_units:quantity:组成数量
launch_checklists;新品上市准备检查;2;product_id:ref.retail_products:商品主键 check_type:enum.图片|定价|库存|资质:检查项 ready_flag:flag:是否就绪
delisting_decisions;商品退市原因与回收;2;product_id:ref.retail_products:商品主键 delist_reason:enum.滞销|换代|质量|合同到期:退市原因 recovery_value:money:回收金额
""",
        "动态定价": """
price_zones;区域定价策略;1;zone_name:label:价格区名称 reference_index:metric:参考指数 zone_strategy:enum.平价|渗透|溢价:价格策略
zone_assignments;门店价格区分配;2;store_id:ref.retail_stores:门店主键 zone_id:ref.retail_price_zones:价格区主键 override_allowed:flag:允许覆盖标志
price_tests;商品价格测试方案;2;product_id:ref.retail_products:商品主键 test_price:price:测试价格 test_objective:enum.销量|收入|利润:测试目标
price_test_results;价格测试观察结果;4;test_id:ref.retail_price_tests:价格测试 observed_units:metric:观察销量 conversion_rate:ratio:转化率
markdown_plans;清仓降价计划;1;category_id:ref.retail_categories:品类主键 markdown_rate:ratio:降价比例 clearance_target:money:清仓目标金额
markdown_items;清仓计划商品明细;3;plan_id:ref.retail_markdown_plans:降价计划 product_id:ref.retail_products:商品主键 clearance_units:quantity:清仓数量
price_overrides;门店临时调价审批;3;store_id:ref.retail_stores:门店主键 product_id:ref.retail_products:商品主键 override_amount:delta:价格调整量
competitor_matches;商品竞价匹配申请;2;product_id:ref.retail_products:商品主键 claimed_price:price:竞品声称价格 match_approved:flag:是否批准比价
margin_alerts;商品毛利异常告警;3;product_id:ref.retail_products:商品主键 observed_margin:ratio:观察毛利率 alert_level:enum.提示|预警|严重:告警级别
price_elasticities;品类价格弹性估计;2;category_id:ref.retail_categories:品类主键 elasticity:delta:弹性模拟值 sample_orders:metric:样本订单数
""",
        "客户生命周期": """
customer_cohorts;客户群组定义;1;cohort_name:label:群组名称 cohort_rule:enum.首购|复购|高价值|沉睡:分组规则
cohort_members;客户群组归属记录;4;cohort_id:ref.retail_customer_cohorts:群组主键 customer_id:ref.retail_customers:客户主键 membership_score:ratio:归属评分
rfm_scores;客户近频额分层评分;4;customer_id:ref.retail_customers:客户主键 recency_score:rating:近度分 frequency_score:rating:频度分 monetary_score:rating:金额分
churn_signals;客户流失风险信号;4;customer_id:ref.retail_customers:客户主键 churn_probability:ratio:流失概率 signal_type:enum.降频|投诉|价格敏感:风险信号
retention_offers;客户挽留权益发放;3;customer_id:ref.retail_customers:客户主键 offer_cost:money:挽留成本 offer_type:enum.赠券|积分|专属服务:权益类型
retention_outcomes;挽留策略结果反馈;3;offer_id:ref.retail_retention_offers:挽留记录 returned_flag:flag:是否回购 retained_value:money:保留价值
referral_codes;客户推荐邀请码;2;customer_id:ref.retail_customers:客户主键 referral_code:label:模拟邀请码 reward_value:money:推荐奖励
referral_conversions;推荐客户成交结果;3;referral_id:ref.retail_referral_codes:邀请码记录 order_id:ref.retail_orders:订单主键 reward_paid:flag:是否发奖
winback_contacts;沉睡客户唤回联系;3;customer_id:ref.retail_customers:客户主键 contact_method:enum.短信|电话|邮件:联系方式 contacted_flag:flag:是否触达
lifetime_values;客户生命周期价值情景;2;customer_id:ref.retail_customers:客户主键 expected_lifetime_value:money:预期生命周期价值 uncertainty:ratio:不确定度
""",
        "门店营运": """
store_openings;门店开业筹备;1;store_id:ref.retail_stores:门店主键 opening_budget:money:开业预算 preparation_score:rating:筹备评分
store_leases;门店租赁协议;1;store_id:ref.retail_stores:门店主键 monthly_rent:money:月租金额 deposit_amount:money:押金金额
store_layouts;门店布局与面积规划;1;store_id:ref.retail_stores:门店主键 selling_area:metric:营业面积 layout_type:enum.网格|环形|自由:布局类型
shelf_positions;门店货架位置;3;store_id:ref.retail_stores:门店主键 shelf_code:label:货架编码 facing_capacity:quantity:陈列容量
planogram_items;货架商品陈列方案;4;shelf_id:ref.retail_shelf_positions:货架主键 product_id:ref.retail_products:商品主键 facing_units:quantity:陈列数量
display_audits;门店陈列巡检;4;store_id:ref.retail_stores:门店主键 compliance_score:rating:陈列合规评分 audit_result:enum.合格|整改|复查:巡检结果
store_shifts;门店员工排班;4;store_id:ref.retail_stores:门店主键 employee_id:ref.workforce_employees:员工主键 shift_name:enum.早班|中班|晚班:班次
cash_drawers;收银钱箱交接;3;store_id:ref.retail_stores:门店主键 opening_cash:money:期初现金 shortage_amount:delta:长短款金额
store_utilities;门店水电能源费用;2;store_id:ref.retail_stores:门店主键 utility_type:enum.水|电|燃气:能源类型 utility_cost:money:费用金额
store_incidents;门店营运突发事件;2;store_id:ref.retail_stores:门店主键 incident_kind:enum.停电|设备|客诉|安全:事件类型 interruption_hours:hours:中断小时
""",
        "会员权益": """
benefit_catalog;会员权益产品目录;1;benefit_name:label:权益名称 benefit_category:enum.折扣|配送|体验:权益类型 cost_per_use:cost:单次成本
benefit_rules;会员权益适用规则;2;benefit_id:ref.retail_benefit_catalog:权益主键 required_tier:enum.基础|金卡|尊享:所需等级 usage_limit:int:使用次数上限
benefit_grants;客户权益授予记录;4;benefit_id:ref.retail_benefit_catalog:权益主键 customer_id:ref.retail_customers:客户主键 granted_units:quantity:授予数量
benefit_redemptions;客户权益兑换流水;4;grant_id:ref.retail_benefit_grants:权益授予 order_id:ref.retail_orders:订单主键 redeemed_units:quantity:兑换数量
tier_assessments;会员等级评估记录;3;customer_id:ref.retail_customers:客户主键 assessment_score:metric:评估分数 promoted_flag:flag:是否升级
points_expiry;积分到期提醒及失效;3;customer_id:ref.retail_customers:客户主键 expiring_points:metric:到期积分 notified_flag:flag:是否提醒
member_events;会员专属活动安排;1;event_name:label:活动名称 capacity_seats:metric:活动名额 event_cost:money:活动成本
event_enrollments;会员活动报名与到场;3;event_id:ref.retail_member_events:活动主键 customer_id:ref.retail_customers:客户主键 attended_flag:flag:是否到场
partner_benefits;合作伙伴联合权益;1;partner_name:label:合作方名称 benefit_id:ref.retail_benefit_catalog:权益主键 settlement_rate:ratio:结算比例
benefit_complaints;会员权益履约投诉;2;benefit_id:ref.retail_benefit_catalog:权益主键 customer_id:ref.retail_customers:客户主键 complaint_reason:enum.不可用|未到账|过期:投诉原因
""",
        "售后维修": """
warranty_plans;商品保修计划;1;product_id:ref.retail_products:商品主键 warranty_months:int:保修月数 coverage_type:enum.整机|核心部件|意外:保障范围
warranty_registrations;订单保修登记;3;order_id:ref.retail_orders:订单主键 plan_id:ref.retail_warranty_plans:保修计划 registration_valid:flag:登记是否有效
repair_centers;维修服务网点;1;center_name:label:服务点名称 daily_repairs:metric:日处理能力 certification:enum.厂家|授权|合作:网点资质
repair_jobs;客户商品维修工单;4;order_id:ref.retail_orders:订单主键 center_id:ref.retail_repair_centers:维修点主键 repair_cost:money:维修费用
repair_parts;维修备件耗用;4;job_id:ref.retail_repair_jobs:维修工单 product_id:ref.retail_products:备件商品 part_units:quantity:备件数量
repair_diagnoses;维修故障诊断记录;3;job_id:ref.retail_repair_jobs:维修工单 fault_code:enum.电源|屏幕|机械|软件:故障类型 diagnosis_hours:hours:诊断小时
repair_status_logs;维修进度状态流转;4;job_id:ref.retail_repair_jobs:维修工单 repair_stage:enum.接收|检测|维修|返还:维修阶段 elapsed_days:int:经过天数
replacement_requests;商品换货申请;3;order_id:ref.retail_orders:订单主键 product_id:ref.retail_products:换货商品 exchange_approved:flag:是否批准
service_compensation;售后补偿处理;3;case_id:ref.retail_service_cases:售后工单 compensation_amount:money:补偿金额 compensation_method:enum.现金|积分|换券:补偿方式
repair_feedback;维修完成满意度;2;job_id:ref.retail_repair_jobs:维修工单 service_rating:rating:服务评分 repeat_fault:flag:是否重复故障
""",
        "销售风控": """
fraud_rules;交易欺诈识别规则;1;rule_name:label:规则名称 risk_type:enum.盗刷|套利|刷单|虚假退货:风险类型 risk_threshold:ratio:风险阈值
fraud_alerts;订单风险预警;4;order_id:ref.retail_orders:订单主键 rule_id:ref.retail_fraud_rules:风险规则 risk_score:ratio:风险评分
fraud_reviews;风险订单人工复核;3;alert_id:ref.retail_fraud_alerts:告警主键 review_outcome:enum.放行|阻断|补证:复核结果 review_minutes:int:复核分钟
device_risk_links;客户设备风险关联;3;customer_id:ref.retail_customers:客户主键 device_id:ref.insight_devices:设备主键 shared_accounts:int:关联账户数
payment_disputes;客户支付争议;3;order_id:ref.retail_orders:订单主键 dispute_amount:money:争议金额 dispute_cause:enum.未授权|重复扣款|未收到:争议原因
chargeback_cases;银行卡拒付处理;2;dispute_id:ref.retail_payment_disputes:争议主键 chargeback_loss:money:拒付损失 defended_flag:flag:是否申诉
return_abuse;异常退货识别;2;customer_id:ref.retail_customers:客户主键 suspicious_returns:int:可疑退货次数 abuse_score:ratio:异常评分
identity_checks;企业及客户身份核验;2;customer_id:ref.retail_customers:客户主键 check_method:enum.资料|人工|授权接口:核验方式 verified_flag:flag:是否通过
compliance_holds;订单合规冻结;2;order_id:ref.retail_orders:订单主键 hold_reason:enum.资质|风险|司法模拟:冻结原因 held_hours:hours:冻结小时
loss_recoveries;交易损失追偿;2;order_id:ref.retail_orders:订单主键 recovered_amount:money:追回金额 recovery_channel:enum.客户|银行|保险:追偿途径
""",
        "客户体验": """
survey_templates;客户调研问卷模板;1;survey_name:label:问卷名称 survey_goal:enum.满意度|新品|价格|服务:调研目标
survey_questions;调研问卷问题项;2;survey_id:ref.retail_survey_templates:问卷主键 question_text:label:问题文本 question_kind:enum.评分|单选|开放:问题类型
survey_responses;客户问卷答卷;4;survey_id:ref.retail_survey_templates:问卷主键 customer_id:ref.retail_customers:客户主键 completed_minutes:int:完成分钟
survey_answers;调研问题回答明细;4;response_id:ref.retail_survey_responses:答卷主键 question_id:ref.retail_survey_questions:问题主键 answer_score:rating:回答评分
nps_observations;客户净推荐度观察;3;customer_id:ref.retail_customers:客户主键 recommendation_score:rating:推荐评分 feedback_topic:enum.价格|服务|品质:反馈主题
journey_maps;客户体验旅程定义;1;journey_name:label:旅程名称 journey_goal:enum.发现|购买|复购:旅程目标
journey_steps;客户旅程步骤配置;2;journey_id:ref.retail_journey_maps:旅程主键 step_label:label:步骤名称 expected_minutes:int:预期耗时
journey_observations;客户旅程行为观察;4;step_id:ref.retail_journey_steps:旅程步骤 customer_id:ref.retail_customers:客户主键 friction_score:rating:阻碍程度
experience_actions;体验改善措施;2;journey_id:ref.retail_journey_maps:旅程主键 action_name:label:措施名称 action_budget:money:改善预算
experience_results;体验改善效果评估;2;action_id:ref.retail_experience_actions:改善措施 satisfaction_delta:delta:满意度变化 observed_customers:metric:观察客户数
""",
    },
    "finance": {
        "资金交易": """
treasury_pools;集团资金池主档;1;pool_name:label:资金池名称 currency:currency:币种 target_balance:money:目标余额
pool_members;资金池银行账户成员;2;pool_id:ref.finance_treasury_pools:资金池 bank_account_id:ref.finance_bank_accounts:银行账户 participation_rate:ratio:参与比例
cash_sweeps;资金池归集流水;4;pool_id:ref.finance_treasury_pools:资金池 sweep_amount:money:归集金额 sweep_direction:enum.上收|下拨:归集方向
funding_requests;部门资金申请;3;cost_center_id:ref.finance_cost_centers:成本中心 requested_amount:money:申请金额 urgency:enum.普通|紧急:紧急程度
funding_approvals;资金申请审批记录;2;request_id:ref.finance_funding_requests:资金申请 approved_amount:money:批准金额 approval_level:enum.经理|总监|董事会:审批层级
money_transfers;内部账户资金划拨;4;bank_account_id:ref.finance_bank_accounts:银行账户 transfer_amount:money:划拨金额 transfer_status:enum.申请|执行|完成:划拨状态
liquidity_limits;资金池流动性限额;1;pool_id:ref.finance_treasury_pools:资金池 minimum_cash:money:最低现金 warning_ratio:ratio:告警比例
liquidity_alerts;现金流动性缺口预警;3;pool_id:ref.finance_treasury_pools:资金池 cash_gap:delta:现金缺口 alert_severity:enum.一般|紧急|危急:告警程度
treasury_positions;资金交易头寸快照;4;pool_id:ref.finance_treasury_pools:资金池 available_funds:money:可用资金 locked_funds:money:冻结资金
bank_fee_plans;银行账户收费方案;1;bank_account_id:ref.finance_bank_accounts:银行账户 monthly_fee:money:月费 transaction_rate:ratio:交易费率
""",
        "融资债务": """
lenders;融资机构及评级;1;lender_name:label:融资机构 lender_type:enum.银行|租赁|基金:机构类型 credit_rating:enum.A|B|C:信用评级
loan_facilities;贷款授信协议;1;lender_id:ref.finance_lenders:融资机构 facility_limit:money:授信限额 interest_rate:ratio:利率
loan_drawdowns;贷款提款记录;3;facility_id:ref.finance_loan_facilities:贷款协议 drawdown_amount:money:提款金额 tenor_days:int:期限天数
loan_repayments;贷款还本付息;3;facility_id:ref.finance_loan_facilities:贷款协议 principal_paid:money:偿还本金 interest_paid:money:支付利息
interest_accruals;贷款利息计提;4;facility_id:ref.finance_loan_facilities:贷款协议 accrued_interest:money:计提利息 accrual_days:int:计息天数
loan_covenants;融资财务约束条款;1;facility_id:ref.finance_loan_facilities:贷款协议 covenant_name:label:约束条款 threshold_ratio:ratio:约束阈值
covenant_tests;融资约束达标检测;3;covenant_id:ref.finance_loan_covenants:约束条款 actual_ratio:ratio:实际比率 breached_flag:flag:是否违约
debt_maturities;到期债务计划;2;facility_id:ref.finance_loan_facilities:贷款协议 maturity_amount:money:到期金额 refinancing_needed:flag:是否需续贷
collateral_assets;融资抵押资产;2;facility_id:ref.finance_loan_facilities:贷款协议 asset_id:ref.finance_fixed_assets:资产主键 pledged_value:money:抵押价值
guarantees;企业融资担保;1;lender_id:ref.finance_lenders:融资机构 guaranteed_amount:money:担保金额 guarantee_type:enum.连带|一般|质押:担保方式
""",
        "投资管理": """
investment_products;投资产品目录;1;product_name:label:投资产品 risk_level:rating:风险等级 currency:currency:币种
investment_accounts;投资账户管理;1;bank_account_id:ref.finance_bank_accounts:银行账户 mandate_type:enum.稳健|平衡|进取:授权策略
investment_trades;投资申购赎回交易;4;product_id:ref.finance_investment_products:投资产品 trade_amount:money:交易金额 trade_side:enum.申购|赎回:交易方向
investment_holdings;投资组合持仓;4;product_id:ref.finance_investment_products:投资产品 holding_units:metric:持有份额 book_value:money:账面价值
market_valuations;投资公允价值估值;4;product_id:ref.finance_investment_products:投资产品 fair_value:money:公允价值 valuation_source:enum.市场|模型|第三方:估值来源
investment_income;投资收益确认;3;product_id:ref.finance_investment_products:投资产品 income_amount:money:确认收益 income_type:enum.分红|利息|价差:收益类型
investment_limits;投资产品风险限额;1;product_id:ref.finance_investment_products:投资产品 concentration_limit:ratio:集中度上限 stop_loss_rate:ratio:止损比例
investment_breaches;投资限额超标记录;2;product_id:ref.finance_investment_products:投资产品 excess_amount:money:超标金额 remediation_status:enum.未处理|减仓|关闭:整改状态
hedge_contracts;汇率套保合约;2;currency:currency:套保币种 notional_amount:money:名义本金 hedge_ratio:ratio:套保比例
hedge_settlements;套保合约结算;3;hedge_id:ref.finance_hedge_contracts:套保合约 settlement_result:delta:结算损益 settlement_method:enum.现金|实物:结算方式
""",
        "费用管控": """
expense_policies;报销政策目录;1;policy_name:label:政策名称 expense_category:enum.交通|住宿|招待|通讯:费用类别 daily_limit:money:每日限额
expense_claims;员工报销申请单;3;employee_id:ref.workforce_employees:员工主键 policy_id:ref.finance_expense_policies:费用政策 claimed_amount:money:申请金额
expense_claim_lines;报销申请费用明细;4;claim_id:ref.finance_expense_claims:申请主键 expense_detail:label:费用说明 line_value:money:明细金额
expense_approvals;报销审核处理;3;claim_id:ref.finance_expense_claims:申请主键 approved_value:money:审核通过金额 rejected_reason:enum.无|超标|缺票|重复:驳回原因
travel_requests;出差预算申请;2;employee_id:ref.workforce_employees:员工主键 destination_city:city:出差城市 travel_budget:money:差旅预算
travel_bookings;出差交通住宿预订;3;request_id:ref.finance_travel_requests:出差申请 booking_type:enum.机票|车票|酒店:预订类型 booking_cost:money:预订成本
travel_settlements;差旅费用结算;3;request_id:ref.finance_travel_requests:出差申请 settled_cost:money:结算费用 advance_offset:money:预支抵扣
employee_advances;员工费用预支;2;employee_id:ref.workforce_employees:员工主键 advance_amount:money:预支金额 outstanding_days:int:未核销天数
corporate_cards;企业公务卡信息;1;employee_id:ref.workforce_employees:员工主键 card_limit:money:公务卡限额 card_state:enum.启用|暂停|注销:卡状态
card_transactions;公务卡消费记录;4;card_id:ref.finance_corporate_cards:公务卡主键 merchant_category:enum.住宿|交通|餐饮|办公:商户类别 transaction_amount:money:消费金额
""",
        "收入确认": """
revenue_contracts;收入确认合同;1;customer_id:ref.retail_customers:客户主键 contract_value:money:合同对价 recognition_policy:enum.时点|时段:收入确认方式
performance_obligations;合同履约义务拆分;2;contract_id:ref.finance_revenue_contracts:收入合同 obligation_name:label:履约义务 allocated_price:money:分摊价格
revenue_schedules;履约收入确认计划;4;obligation_id:ref.finance_performance_obligations:履约义务 scheduled_revenue:money:计划确认收入 completion_ratio:ratio:完成比例
revenue_recognitions;履约收入确认流水;4;obligation_id:ref.finance_performance_obligations:履约义务 recognized_revenue:money:已确认收入 evidence_type:enum.签收|进度|验收:确认依据
deferred_revenue;递延收入余额;3;contract_id:ref.finance_revenue_contracts:收入合同 deferred_amount:money:递延余额 released_amount:money:本期释放
contract_assets;合同资产计量;2;contract_id:ref.finance_revenue_contracts:收入合同 asset_amount:money:合同资产 impairment_amount:money:减值金额
contract_liabilities;合同负债计量;2;contract_id:ref.finance_revenue_contracts:收入合同 liability_amount:money:合同负债 refundable_amount:money:可退还金额
rebate_programs;客户销售返利政策;1;customer_id:ref.retail_customers:客户主键 rebate_rate:ratio:返利比例 volume_threshold:metric:销量门槛
rebate_accruals;销售返利预计计提;3;program_id:ref.finance_rebate_programs:返利政策 accrued_rebate:money:预计返利 qualifying_sales:money:合格销售额
rebate_settlements;客户返利实际结算;3;program_id:ref.finance_rebate_programs:返利政策 settled_rebate:money:实际结算金额 settlement_status:enum.待核|通过|完成:结算状态
""",
        "税务筹划": """
tax_jurisdictions;纳税辖区及征管机关;1;jurisdiction_name:label:纳税辖区 authority_type:enum.市级|省级|国家:征管层级
tax_registrations;经营主体税务登记;1;jurisdiction_id:ref.finance_tax_jurisdictions:税务辖区 taxpayer_type:enum.一般|小规模:纳税人类别 active_registration:flag:登记有效标志
vat_inputs;增值税进项凭证;4;supplier_id:ref.supply_suppliers:供应商主键 input_tax:money:进项税额 deductible_flag:flag:是否可抵扣
vat_outputs;增值税销项记录;4;invoice_id:ref.finance_invoices:发票主键 output_tax:money:销项税额 exempt_flag:flag:是否免税
vat_reconciliations;增值税申报对账;3;jurisdiction_id:ref.finance_tax_jurisdictions:税务辖区 input_total:money:进项总额 output_total:money:销项总额 reconciliation_gap:delta:对账差额
tax_incentives;税收优惠资格;1;incentive_name:label:优惠名称 incentive_rate:ratio:优惠比例 eligible_flag:flag:是否符合条件
incentive_claims;税收优惠申请;2;incentive_id:ref.finance_tax_incentives:优惠项目 claimed_relief:money:申请减免金额 approved_relief:money:批准减免金额
withholding_records;代扣代缴税费;3;employee_id:ref.workforce_employees:员工主键 taxable_income:money:应税收入 withheld_tax:money:代扣税额
tax_payments;税款实际缴纳;3;jurisdiction_id:ref.finance_tax_jurisdictions:税务辖区 paid_tax:money:已缴税额 payment_state:enum.成功|处理中|退回:支付状态
tax_audit_cases;税务稽查事项;2;jurisdiction_id:ref.finance_tax_jurisdictions:税务辖区 assessed_tax:money:评估补税额 case_status:enum.立案|核查|结案:案件状态
""",
        "资产租赁": """
asset_categories;固定资产分类政策;1;category_name:label:资产分类 useful_life:int:使用年限 residual_rate:ratio:残值率
asset_locations;固定资产物理位置;1;location_name:label:位置名称 department_id:ref.workforce_departments:部门主键
asset_acquisitions;固定资产购入验收;3;asset_id:ref.finance_fixed_assets:资产主键 supplier_id:ref.supply_suppliers:供应商主键 acquisition_cost:money:购入成本
asset_transfers;资产部门调拨;3;asset_id:ref.finance_fixed_assets:资产主键 location_id:ref.finance_asset_locations:目标位置 transfer_cost:money:调拨费用
asset_counts;固定资产盘点;3;asset_id:ref.finance_fixed_assets:资产主键 present_flag:flag:实物存在标志 condition_score:rating:资产状况评分
asset_impairments;资产减值测试;2;asset_id:ref.finance_fixed_assets:资产主键 recoverable_value:money:可收回金额 impairment_loss:money:减值损失
asset_disposals;固定资产处置;2;asset_id:ref.finance_fixed_assets:资产主键 disposal_proceeds:money:处置收入 disposal_reason:enum.报废|出售|置换:处置原因
lease_contracts;租赁准则合同;1;cost_center_id:ref.finance_cost_centers:成本中心 lease_payment:money:租赁付款 discount_rate:ratio:折现率
lease_liabilities;租赁负债摊销计划;3;lease_id:ref.finance_lease_contracts:租赁合同 liability_balance:money:负债余额 interest_expense:money:利息费用
right_of_use_assets;使用权资产折旧;3;lease_id:ref.finance_lease_contracts:租赁合同 carrying_value:money:账面价值 amortization_charge:money:摊销金额
""",
        "预算预测": """
planning_cycles;预算编制周期;1;cycle_name:label:编制周期 planning_method:enum.零基|增量|滚动:编制方法
budget_versions;预算版本及发布状态;1;cycle_id:ref.finance_planning_cycles:编制周期 version_label:label:版本名称 publication_state:enum.草稿|审核|生效:发布状态
budget_submissions;责任中心预算提报;3;version_id:ref.finance_budget_versions:预算版本 cost_center_id:ref.finance_cost_centers:成本中心 submitted_total:money:提报金额
budget_line_items;预算科目明细;4;submission_id:ref.finance_budget_submissions:预算提报 account_id:ref.finance_accounts:会计科目 planned_amount:money:计划金额
budget_transfers;成本中心预算调剂;2;budget_id:ref.finance_budgets:预算主键 transfer_amount:money:调剂金额 transfer_reason:enum.节余|新增|应急:调剂原因
forecast_scenarios;经营预测情景设置;1;scenario_name:label:情景名称 growth_assumption:ratio:增长假设 inflation_assumption:ratio:通胀假设
forecast_drivers;预测业务驱动因素;2;scenario_id:ref.finance_forecast_scenarios:预测情景 driver_name:enum.销量|价格|人头|汇率:驱动因素 assumed_value:metric:假设数值
forecast_results;情景预测结果指标;4;scenario_id:ref.finance_forecast_scenarios:预测情景 cost_center_id:ref.finance_cost_centers:成本中心 forecast_value:money:预测指标金额
variance_explanations;预算差异原因分析;3;budget_id:ref.finance_budgets:预算主键 variance_reason:enum.价格|数量|时点|范围:差异原因 explained_amount:delta:解释差额
capex_requests;资本开支立项申请;2;cost_center_id:ref.finance_cost_centers:成本中心 requested_capex:money:申请资本开支 payback_years:int:回收年限
""",
        "合并报表": """
legal_entities;集团法人主体;1;entity_name:label:法人主体 entity_type:enum.母公司|子公司|联营:主体类别
ownership_links;法人股权持有关系;1;entity_id:ref.finance_legal_entities:被投资主体 ownership_percentage:ratio:持股比例 control_method:enum.控制|共同控制|重大影响:控制方式
reporting_periods;集团报告期间状态;1;period_label:label:报告期间 close_status:enum.开放|预关账|已关闭:关账状态
trial_balances;法人科目试算平衡;4;entity_id:ref.finance_legal_entities:法人主体 account_id:ref.finance_accounts:会计科目 debit_balance:money:借方余额 credit_balance:money:贷方余额
intercompany_flows;集团内部交易流水;4;entity_id:ref.finance_legal_entities:法人主体 flow_category:enum.销售|借款|服务|股利:内部交易类别 flow_value:money:交易金额
elimination_entries;合并抵销分录;3;entity_id:ref.finance_legal_entities:法人主体 account_id:ref.finance_accounts:会计科目 eliminated_amount:money:抵销金额
consolidation_runs;合并报表执行批次;2;period_id:ref.finance_reporting_periods:报告期间 execution_state:enum.运行|成功|失败:执行状态 entity_count:int:参与主体数
consolidated_metrics;集团合并指标结果;4;run_id:ref.finance_consolidation_runs:合并批次 metric_name:enum.收入|利润|资产|负债:指标名称 metric_amount:money:指标金额
disclosure_notes;财务报表附注事项;2;period_id:ref.finance_reporting_periods:报告期间 disclosure_title:label:附注标题 materiality_amount:money:重要性金额
closing_checklists;财务关账任务清单;3;period_id:ref.finance_reporting_periods:报告期间 task_name:label:关账任务 task_completed:flag:是否完成
""",
        "内控审计": """
control_catalog;财务内部控制点;1;control_name:label:控制点名称 control_frequency:enum.每日|每月|每季:执行频率
control_owners;控制点责任人;1;control_id:ref.finance_control_catalog:控制点 employee_id:ref.workforce_employees:责任员工
control_tests;内控有效性测试;3;control_id:ref.finance_control_catalog:控制点 tested_samples:metric:测试样本量 failed_samples:quantity:失败样本数
control_exceptions;内控例外事项;3;control_id:ref.finance_control_catalog:控制点 exception_value:money:例外涉及金额 severity_grade:rating:严重程度
remediation_plans;内控缺陷整改计划;2;exception_id:ref.finance_control_exceptions:例外事项 remediation_cost:money:整改成本 progress_ratio:ratio:整改进度
audit_engagements;审计项目立项;1;engagement_name:label:审计项目 audit_scope:enum.资金|税务|采购|收入:审计范围
audit_samples;审计抽样交易;4;engagement_id:ref.finance_audit_engagements:审计项目 journal_id:ref.finance_journals:凭证主键 sample_risk:ratio:抽样风险
audit_evidence;审计证据登记;3;engagement_id:ref.finance_audit_engagements:审计项目 evidence_type:enum.凭证|访谈|函证|实物:证据类别 reliable_flag:flag:是否可靠
audit_adjustments;审计调整分录;3;engagement_id:ref.finance_audit_engagements:审计项目 account_id:ref.finance_accounts:会计科目 adjustment_amount:delta:调整金额
audit_followups;审计建议落实跟踪;2;engagement_id:ref.finance_audit_engagements:审计项目 completion_rate:ratio:落实比例 followup_result:enum.未落实|部分|全部:跟进结果
""",
    },
    "supply": {
        "战略寻源": """
sourcing_projects;采购寻源立项;1;project_name:label:寻源项目 category_id:ref.retail_categories:品类主键 target_savings:money:节约目标
rfq_headers;供应商询价单;2;project_id:ref.supply_sourcing_projects:寻源项目 rfq_title:label:询价主题 invited_count:int:邀请供应商数
rfq_items;询价商品需求明细;3;rfq_id:ref.supply_rfq_headers:询价单 product_id:ref.retail_products:商品主键 requested_units:quantity:需求数量
supplier_bids;供应商投标报价;3;rfq_id:ref.supply_rfq_headers:询价单 supplier_id:ref.supply_suppliers:供应商主键 bid_amount:money:投标金额
bid_evaluations;采购投标综合评分;2;bid_id:ref.supply_supplier_bids:投标记录 technical_score:rating:技术评分 commercial_score:rating:商务评分
sourcing_awards;采购中标结果;2;project_id:ref.supply_sourcing_projects:寻源项目 supplier_id:ref.supply_suppliers:中标供应商 awarded_amount:money:中标金额
framework_contracts;采购框架协议;1;supplier_id:ref.supply_suppliers:供应商主键 committed_value:money:承诺采购额 payment_days:int:账期天数
contract_price_lines;采购协议价目明细;3;contract_id:ref.supply_framework_contracts:框架协议 product_id:ref.retail_products:商品主键 contracted_price:price:协议价格
supplier_capacity;供应商产能承诺;3;supplier_id:ref.supply_suppliers:供应商主键 monthly_capacity:metric:月度产能 utilized_ratio:ratio:利用比例
savings_tracking;采购节约效果追踪;3;project_id:ref.supply_sourcing_projects:寻源项目 baseline_cost:money:基准成本 realized_savings:money:实际节约
""",
        "供应商治理": """
supplier_onboarding;供应商准入申请;1;supplier_id:ref.supply_suppliers:供应商主键 onboarding_stage:enum.申请|审核|试供|准入:准入阶段 qualification_score:rating:资质评分
supplier_documents;供应商资质证照;2;supplier_id:ref.supply_suppliers:供应商主键 document_kind:enum.营业执照|认证|许可:证照类型 document_valid:flag:证照有效标志
supplier_audits;供应商现场审核;2;supplier_id:ref.supply_suppliers:供应商主键 audit_score:rating:审核评分 corrective_actions:int:整改数量
supplier_audit_items;供应商审核检查项;3;audit_id:ref.supply_supplier_audits:审核记录 check_area:enum.质量|环境|劳工|安全:检查领域 compliant_flag:flag:是否合规
supplier_risk_events;供应商经营风险事件;3;supplier_id:ref.supply_suppliers:供应商主键 risk_category:enum.财务|交付|合规|灾害:风险类别 impact_value:money:影响金额
supplier_certificates;供应商体系认证;1;supplier_id:ref.supply_suppliers:供应商主键 certificate_name:label:认证名称 certification_status:enum.有效|复审|失效:认证状态
supplier_improvements;供应商改善计划;2;supplier_id:ref.supply_suppliers:供应商主键 improvement_goal:label:改善目标 completion_ratio:ratio:完成比例
supplier_meetings;供应商业务评审会;2;supplier_id:ref.supply_suppliers:供应商主键 meeting_topic:enum.质量|产能|成本|合作:评审议题 action_count:int:行动项数
supplier_penalties;供应商履约处罚;2;supplier_id:ref.supply_suppliers:供应商主键 penalty_amount:money:处罚金额 penalty_reason:enum.延期|缺陷|违约:处罚原因
supplier_exit_cases;供应商退出评估;1;supplier_id:ref.supply_suppliers:供应商主键 exit_reason:enum.质量|风险|替代|停业:退出原因 transition_cost:money:切换成本
""",
        "仓内作业": """
warehouse_zones;仓库功能分区;1;warehouse_id:ref.supply_warehouses:仓库主键 zone_name:label:分区名称 zone_type:enum.收货|存储|拣选|发货:分区用途
bin_locations;仓库储位容量;2;zone_id:ref.supply_warehouse_zones:仓库分区 bin_code:label:储位编号 max_units:metric:最大容量
putaway_tasks;到货上架任务;4;warehouse_id:ref.supply_warehouses:仓库主键 product_id:ref.retail_products:商品主键 putaway_units:quantity:上架数量
putaway_confirmations;上架作业确认;4;task_id:ref.supply_putaway_tasks:上架任务 bin_id:ref.supply_bin_locations:目标储位 elapsed_minutes:int:上架耗时
pick_waves;仓库拣选波次;2;warehouse_id:ref.supply_warehouses:仓库主键 wave_priority:rating:波次优先级 expected_picks:metric:预期拣选量
pick_tasks;订单拣货作业;4;wave_id:ref.supply_pick_waves:拣选波次 order_id:ref.retail_orders:订单主键 picked_units:quantity:拣货数量
pack_tasks;订单包装作业;4;order_id:ref.retail_orders:订单主键 packaging_type:enum.纸箱|袋装|托盘:包装类型 packing_minutes:int:包装分钟
loading_tasks;车辆装车作业;3;shipment_id:ref.supply_shipments:发运记录 warehouse_id:ref.supply_warehouses:仓库主键 loaded_weight:metric:装载重量
cycle_counts;仓库循环盘点;3;bin_id:ref.supply_bin_locations:储位主键 product_id:ref.retail_products:商品主键 counted_units:metric:实盘数量
count_adjustments;库存盘点差异调整;3;count_id:ref.supply_cycle_counts:盘点记录 adjustment_units:signed:调整数量 adjustment_reason:enum.损耗|错拣|漏记:调整原因
""",
        "运输管理": """
fleet_vehicles;运输车辆主档;1;carrier_id:ref.supply_carriers:承运商主键 vehicle_code:label:模拟车辆编号 payload_tons:metric:载重吨数
fleet_drivers;运输司机资质;1;employee_id:ref.workforce_employees:员工主键 license_class:enum.重型|中型|轻型:驾驶资格 safety_score:rating:安全评分
transport_plans;运输调度计划;2;route_id:ref.supply_routes:线路主键 planned_trips:metric:计划车次 planned_distance:metric:计划里程
trip_assignments;运输车次分配;3;plan_id:ref.supply_transport_plans:运输计划 vehicle_id:ref.supply_fleet_vehicles:车辆主键 driver_id:ref.supply_fleet_drivers:司机主键
trip_stops;运输车次停靠点;4;trip_id:ref.supply_trip_assignments:车次主键 stop_city:city:停靠城市 stop_minutes:int:停靠分钟
gps_observations;运输车辆定位观察;5;vehicle_id:ref.supply_fleet_vehicles:车辆主键 speed_kmh:metric:速度公里每小时 distance_delta:metric:区间距离
fuel_transactions;车队燃油补给;3;vehicle_id:ref.supply_fleet_vehicles:车辆主键 fuel_liters:metric:加油升数 fuel_cost:money:燃油成本
toll_charges;运输道路通行费用;3;trip_id:ref.supply_trip_assignments:车次主键 toll_amount:money:通行费 road_type:enum.高速|桥梁|隧道:道路类型
vehicle_inspections;运输车辆安全检查;2;vehicle_id:ref.supply_fleet_vehicles:车辆主键 inspection_score:rating:检查评分 repair_needed:flag:是否需维修
transport_invoices;承运商运费结算单;3;carrier_id:ref.supply_carriers:承运商主键 invoiced_freight:money:结算运费 disputed_freight:money:争议运费
""",
        "国际贸易": """
trade_lanes;国际运输航线;1;lane_name:label:航线名称 origin_country:enum.中国|德国|越南:始发国家 destination_country:enum.中国|法国|泰国:目的国家
export_declarations;出口报关单;3;shipment_id:ref.supply_shipments:发运记录 declared_value:money:申报货值 customs_status:enum.申报|查验|放行:通关状态
import_declarations;进口报关单;3;purchase_id:ref.supply_purchase_orders:采购订单 import_value:money:进口货值 inspection_required:flag:是否查验
customs_line_items;报关商品明细;4;declaration_id:ref.supply_import_declarations:进口报关单 product_id:ref.retail_products:商品主键 declared_units:quantity:申报数量
duty_assessments;进口关税评估;3;declaration_id:ref.supply_import_declarations:进口报关单 duty_rate:ratio:关税税率 duty_amount:money:应纳关税
customs_inspections;海关查验记录;2;declaration_id:ref.supply_export_declarations:出口报关单 inspection_hours:hours:查验耗时 findings_count:int:发现问题数
certificates_origin;货物原产地证明;1;supplier_id:ref.supply_suppliers:供应商主键 origin_region:label:原产地区 certified_flag:flag:是否认证
ocean_bookings;海运舱位预订;2;lane_id:ref.supply_trade_lanes:航线主键 booked_teu:quantity:预订标准箱数 ocean_freight:money:海运费用
container_events;集装箱流转事件;4;booking_id:ref.supply_ocean_bookings:海运预订 container_stage:enum.提箱|装箱|装船|卸船|还箱:流转节点 dwell_hours:hours:停留小时
trade_compliance;进出口贸易合规检查;2;shipment_id:ref.supply_shipments:发运记录 restricted_flag:flag:是否受限 compliance_reason:enum.正常|许可证|产品管制:合规原因
""",
        "精益制造": """
production_lines;生产线能力主档;1;line_name:label:产线名称 nominal_capacity:metric:标称产能 automation_rate:ratio:自动化比例
work_centers;制造工作中心;1;line_id:ref.supply_production_lines:生产线 center_name:label:工作中心 labor_slots:int:人工工位数
process_routes;商品工艺路线;1;product_id:ref.retail_products:商品主键 route_version:label:工艺版本 standard_minutes:metric:标准工时
process_steps;工艺路线加工步骤;2;route_id:ref.supply_process_routes:工艺路线 center_id:ref.supply_work_centers:工作中心 cycle_seconds:metric:节拍秒数
work_orders;生产执行作业单;4;production_id:ref.supply_production_orders:生产订单 line_id:ref.supply_production_lines:生产线 planned_quantity:metric:计划数量
work_order_outputs;工单产出报工;4;work_order_id:ref.supply_work_orders:作业单 good_units:metric:合格数量 rejected_units:quantity:不合格数量
setup_changes;产线换型作业;3;line_id:ref.supply_production_lines:生产线 changeover_minutes:metric:换型分钟 setup_loss:money:换型损失
line_downtime;生产线停机事件;3;line_id:ref.supply_production_lines:生产线 downtime_minutes:metric:停机分钟 stop_reason:enum.故障|缺料|换型|计划:停机原因
oee_observations;设备综合效率观察;3;line_id:ref.supply_production_lines:生产线 availability:ratio:时间开动率 performance_ratio:ratio:性能效率 quality_ratio:ratio:合格率
lean_actions;精益改善活动;2;center_id:ref.supply_work_centers:工作中心 improvement_name:label:改善名称 saved_minutes:metric:节省工时
""",
        "质量追溯": """
quality_standards;商品质量检验标准;1;product_id:ref.retail_products:商品主键 standard_name:label:检验标准 acceptance_rate:ratio:验收阈值
inspection_lots;质量检验批次;3;product_id:ref.retail_products:商品主键 supplier_id:ref.supply_suppliers:供应商主键 lot_units:metric:批次数量
inspection_results;检验特性测量结果;4;lot_id:ref.supply_inspection_lots:检验批次 characteristic:enum.尺寸|重量|强度|外观:检验特性 measured_value:metric:测量结果
nonconformances;质量不合格报告;3;lot_id:ref.supply_inspection_lots:检验批次 severity_level:enum.轻微|主要|致命:缺陷等级 affected_units:quantity:受影响数量
capa_requests;纠正预防措施申请;2;nonconformance_id:ref.supply_nonconformances:不合格报告 root_cause:enum.材料|工艺|设备|人员:根因类别
capa_actions;纠正预防措施执行;3;request_id:ref.supply_capa_requests:措施申请 action_cost:money:执行成本 effectiveness:ratio:有效性
batch_genealogy;生产批次物料追溯;4;lot_id:ref.supply_inspection_lots:检验批次 product_id:ref.retail_products:追溯商品 trace_depth:int:追溯层数
recall_campaigns;商品召回行动;1;product_id:ref.retail_products:商品主键 recall_reason:enum.安全|性能|法规:召回原因 target_units:metric:目标召回量
recall_returns;召回商品回收记录;3;campaign_id:ref.supply_recall_campaigns:召回行动 order_id:ref.retail_orders:订单主键 recovered_units:quantity:回收数量
lab_calibrations;实验室设备校准;2;machine_id:ref.supply_machines:设备主键 calibration_error:delta:校准偏差 passed_flag:flag:是否合格
""",
        "备件与设备": """
spare_parts;设备维修备件目录;1;part_name:label:备件名称 part_price:price:备件单价 critical_part:flag:是否关键备件
spare_stock;设备备件库存;3;part_id:ref.supply_spare_parts:备件主键 warehouse_id:ref.supply_warehouses:仓库主键 stock_units:metric:库存数量
spare_requisitions;维修备件领用申请;3;part_id:ref.supply_spare_parts:备件主键 machine_id:ref.supply_machines:设备主键 requested_parts:quantity:申请数量
preventive_plans;设备预防维护计划;1;machine_id:ref.supply_machines:设备主键 interval_hours:metric:维护间隔 planned_cost:money:计划费用
maintenance_orders;设备维修执行工单;3;machine_id:ref.supply_machines:设备主键 work_type:enum.预防|故障|改善:工单类型 actual_hours:hours:实际工时
maintenance_tasks;设备维修任务明细;4;order_id:ref.supply_maintenance_orders:维修工单 task_label:label:任务名称 task_minutes:int:任务耗时
failure_modes;设备故障模式目录;1;machine_id:ref.supply_machines:设备主键 failure_name:label:故障模式 severity_score:rating:严重度
failure_occurrences;设备故障发生记录;3;mode_id:ref.supply_failure_modes:故障模式 production_loss:money:生产损失 repair_hours:hours:修复小时
condition_readings;设备状态监测读数;5;machine_id:ref.supply_machines:设备主键 vibration_value:metric:振动幅度 temperature_value:temperature:设备温度
reliability_metrics;设备可靠性指标;3;machine_id:ref.supply_machines:设备主键 mtbf_hours:metric:平均故障间隔 mttr_hours:hours:平均修复时间
""",
        "计划协同": """
sales_ops_cycles;产销协同计划周期;1;cycle_name:label:协同周期 demand_version:label:需求版本 consensus_reached:flag:是否达成共识
demand_signals;市场需求信号记录;4;product_id:ref.retail_products:商品主键 signal_source:enum.订单|促销|渠道|预测:信号来源 demand_units:metric:需求数量
consensus_forecasts;商品共识预测;3;cycle_id:ref.supply_sales_ops_cycles:协同周期 product_id:ref.retail_products:商品主键 consensus_units:metric:共识预测量
forecast_errors;需求预测偏差观察;3;product_id:ref.retail_products:商品主键 forecast_units:metric:预测数量 actual_units:metric:实际数量 bias_units:delta:预测偏差
material_requirements;物料需求计算;4;product_id:ref.retail_products:商品主键 required_quantity:metric:需求数量 shortage_quantity:quantity:缺料数量
capacity_plans;产能平衡计划;2;line_id:ref.supply_production_lines:生产线 required_hours:metric:需求工时 available_hours:metric:可用工时
allocation_priorities;紧缺商品分配优先级;2;product_id:ref.retail_products:商品主键 store_id:ref.retail_stores:门店主键 priority_score:rating:优先评分
safety_stock_reviews;安全库存参数评审;2;product_id:ref.retail_products:商品主键 warehouse_id:ref.supply_warehouses:仓库主键 revised_stock:metric:调整库存
replenishment_orders;自动补货建议单;4;product_id:ref.retail_products:商品主键 warehouse_id:ref.supply_warehouses:仓库主键 suggested_units:metric:建议数量
planning_exceptions;供应计划异常处理;3;cycle_id:ref.supply_sales_ops_cycles:协同周期 exception_label:enum.产能|物料|交付|预测:异常类别 impact_units:metric:影响数量
""",
        "绿色供应链": """
emission_factors;物流排放因子字典;1;transport_mode:enum.公路|铁路|航空|海运:运输方式 factor_value:metric:排放系数 factor_unit:enum.千克每吨公里:计量单位
energy_meters;仓库能源计量表;1;warehouse_id:ref.supply_warehouses:仓库主键 meter_name:label:计量表名称 energy_type:enum.电力|燃气|热力:能源类型
energy_consumption;仓库能源消耗记录;4;meter_id:ref.supply_energy_meters:能源表 consumption_kwh:metric:耗能千瓦时 energy_cost:money:能源费用
waste_streams;生产废弃物分类;1;stream_name:label:废物流名称 hazardous_flag:flag:是否危废 recycling_target:ratio:回收目标比例
waste_collections;生产废弃物收集;3;stream_id:ref.supply_waste_streams:废物流 collected_kg:metric:收集千克数 disposal_cost:money:处置费用
recycling_batches;物料回收处理批次;3;stream_id:ref.supply_waste_streams:废物流 recycled_kg:metric:回收千克数 recovery_rate:ratio:回收率
packaging_materials;绿色包装材料目录;1;material_name:label:包装材料 reusable_flag:flag:是否可重复使用 unit_weight:metric:单位重量
packaging_usage;运输包装耗用记录;4;material_id:ref.supply_packaging_materials:包装材料 shipment_id:ref.supply_shipments:发运记录 used_packages:quantity:使用包装数
supplier_esg_scores;供应商可持续评分;2;supplier_id:ref.supply_suppliers:供应商主键 environmental_score:rating:环境评分 social_score:rating:社会评分 governance_score:rating:治理评分
carbon_reduction_plans;供应链减碳项目;1;warehouse_id:ref.supply_warehouses:仓库主键 reduction_target:metric:减排目标 project_investment:money:项目投入
""",
    },
    "workforce": {
        "组织规划": """
org_units;组织经营单元;1;unit_name:label:经营单元 unit_type:enum.事业群|事业部|区域:单元类型
org_unit_links;组织单元上下级关系;1;unit_id:ref.workforce_org_units:组织单元 department_id:ref.workforce_departments:部门主键 reporting_level:int:汇报层级
headcount_plans;部门编制预算;2;department_id:ref.workforce_departments:部门主键 authorized_positions:metric:核定编制 labor_budget:money:人工预算
headcount_requests;新增编制申请;2;department_id:ref.workforce_departments:部门主键 requested_heads:quantity:申请人数 request_reason:enum.扩张|替补|转型:申请原因
headcount_approvals;编制申请审批;2;request_id:ref.workforce_headcount_requests:编制申请 approved_heads:quantity:批准人数 approval_status:enum.通过|驳回|暂缓:审批状态
position_budgets;岗位薪酬预算;2;position_id:ref.workforce_positions:岗位主键 salary_budget:money:薪资预算 planned_heads:int:计划人数
span_of_control;管理幅度观察;2;employee_id:ref.workforce_employees:管理者主键 direct_reports:int:直接下属数 indirect_reports:int:间接下属数
org_design_changes;组织架构调整项目;1;department_id:ref.workforce_departments:部门主键 change_name:label:调整项目 change_cost:money:调整成本
position_evaluations;岗位价值评估;2;position_id:ref.workforce_positions:岗位主键 responsibility_score:rating:职责评分 complexity_score:rating:复杂度评分
workforce_scenarios;人力供需情景;2;department_id:ref.workforce_departments:部门主键 demand_heads:metric:需求人数 supply_heads:metric:供给人数
""",
        "招聘运营": """
recruiting_agencies;招聘合作机构;1;agency_name:label:机构名称 agency_type:enum.猎头|校招|平台:机构类别 service_rate:ratio:服务费率
agency_contracts;招聘供应商合同;1;agency_id:ref.workforce_recruiting_agencies:招聘机构 contract_fee:money:合同费用 replacement_days:int:替换保障天数
recruiting_campaigns;招聘营销活动;2;opening_id:ref.workforce_job_openings:职位需求 campaign_name:label:活动名称 promotion_spend:money:推广费用
candidate_sources;候选人渠道归因;3;candidate_id:ref.workforce_candidates:候选人主键 agency_id:ref.workforce_recruiting_agencies:招聘机构 source_quality:rating:渠道质量
talent_pools;候选人人才库;1;pool_name:label:人才库名称 specialty:enum.研发|销售|运营|职能:专业方向
pool_candidates;人才库成员及活跃度;3;pool_id:ref.workforce_talent_pools:人才库 candidate_id:ref.workforce_candidates:候选人主键 engagement_score:rating:活跃评分
interview_panels;面试评委组;2;opening_id:ref.workforce_job_openings:职位需求 employee_id:ref.workforce_employees:评委主键 panel_role:enum.专业|文化|终面:评委职责
assessment_tests;招聘测评结果;3;candidate_id:ref.workforce_candidates:候选人主键 assessment_type:enum.技能|认知|性格:测评类型 test_score:metric:测评分数
job_offers;候选人录用邀约;2;candidate_id:ref.workforce_candidates:候选人主键 offered_salary:money:邀约薪资 accepted_offer:flag:是否接受
offer_negotiations;录用条件协商;2;offer_id:ref.workforce_job_offers:录用邀约 requested_salary:money:期望薪资 negotiation_round:int:协商轮次
""",
        "入职与离职": """
onboarding_plans;新员工入职计划;1;employee_id:ref.workforce_employees:员工主键 mentor_id:ref.workforce_employees:导师主键 plan_duration:int:计划天数
onboarding_tasks;入职办理任务;3;plan_id:ref.workforce_onboarding_plans:入职计划 task_type:enum.证件|设备|培训|介绍:办理事项 completed_flag:flag:是否完成
probation_reviews;员工试用期考核;2;employee_id:ref.workforce_employees:员工主键 probation_score:rating:试用评分 confirmed_flag:flag:是否转正
buddy_assignments;新员工伙伴安排;2;employee_id:ref.workforce_employees:新员工 buddy_id:ref.workforce_employees:伙伴主键 meeting_count:int:辅导次数
equipment_issues;员工办公设备领用;3;employee_id:ref.workforce_employees:员工主键 asset_id:ref.finance_fixed_assets:固定资产 issued_quantity:quantity:领用数量
access_provisioning;员工业务访问开通;3;employee_id:ref.workforce_employees:员工主键 system_name:label:系统名称 access_state:enum.申请|开通|回收:访问状态
exit_requests;员工离职申请;2;employee_id:ref.workforce_employees:员工主键 departure_reason:enum.发展|薪酬|家庭|组织:离职原因 voluntary_flag:flag:是否主动离职
exit_interviews;离职访谈反馈;2;request_id:ref.workforce_exit_requests:离职申请 management_rating:rating:管理评分 rehire_eligible:flag:是否可再雇佣
handover_tasks;员工离职交接;3;request_id:ref.workforce_exit_requests:离职申请 handover_area:enum.客户|项目|资产|权限:交接范围 cleared_flag:flag:是否结清
final_settlements;离职薪酬结算;2;employee_id:ref.workforce_employees:员工主键 final_pay:money:最终薪酬 severance_pay:money:离职补偿
""",
        "职业发展": """
career_paths;岗位职业发展路径;1;position_id:ref.workforce_positions:岗位主键 path_name:label:路径名称 path_type:enum.专业|管理|跨职能:发展方向
career_steps;职业路径能力阶段;2;path_id:ref.workforce_career_paths:职业路径 step_name:label:阶段名称 required_level:rating:所需能力
development_plans;员工个人发展计划;2;employee_id:ref.workforce_employees:员工主键 career_goal:label:发展目标 investment_budget:money:培养预算
development_actions;个人发展行动项;3;plan_id:ref.workforce_development_plans:发展计划 action_type:enum.学习|轮岗|导师|项目:行动类型 progress_score:ratio:完成进度
mentoring_pairs;员工导师配对;2;employee_id:ref.workforce_employees:学员主键 mentor_id:ref.workforce_employees:导师主键 fit_score:rating:匹配评分
mentoring_sessions;导师辅导记录;3;pair_id:ref.workforce_mentoring_pairs:辅导配对 session_hours:hours:辅导小时 feedback_score:rating:反馈评分
succession_pools;关键岗位继任池;1;position_id:ref.workforce_positions:关键岗位 pool_label:label:继任池 vacancy_risk:ratio:空缺风险
successor_candidates;岗位继任候选人;2;pool_id:ref.workforce_succession_pools:继任池 employee_id:ref.workforce_employees:候选员工 readiness:enum.立即|一年|两年:准备周期
promotion_reviews;员工晋升评审;2;employee_id:ref.workforce_employees:员工主键 target_grade:int:目标职级 promotion_passed:flag:是否通过
internal_moves;员工内部转岗申请;2;employee_id:ref.workforce_employees:员工主键 position_id:ref.workforce_positions:目标岗位 mobility_status:enum.申请|匹配|转岗:流转状态
""",
        "绩效激励": """
goal_cycles;绩效目标周期;1;cycle_name:label:绩效周期 evaluation_method:enum.目标|关键成果|平衡计分:考核方法
employee_goals;员工绩效目标;3;employee_id:ref.workforce_employees:员工主键 cycle_id:ref.workforce_goal_cycles:考核周期 target_value:metric:目标值
goal_checkins;员工目标进度跟进;4;goal_id:ref.workforce_employee_goals:绩效目标 progress_value:metric:进度值 confidence_level:rating:达成信心
peer_feedback;员工同事反馈;3;employee_id:ref.workforce_employees:被评员工 reviewer_id:ref.workforce_employees:评价员工 collaboration_score:rating:协作评分
manager_reviews;管理者绩效评价;3;employee_id:ref.workforce_employees:员工主键 delivery_score:rating:交付评分 growth_score:rating:成长评分
calibration_sessions;绩效校准会议;1;cycle_id:ref.workforce_goal_cycles:考核周期 department_id:ref.workforce_departments:部门主键 reviewed_people:int:校准人数
calibration_results;员工绩效校准结果;2;session_id:ref.workforce_calibration_sessions:校准会议 employee_id:ref.workforce_employees:员工主键 calibrated_rating:rating:校准评分
bonus_plans;绩效奖金分配方案;1;department_id:ref.workforce_departments:部门主键 bonus_pool:money:奖金池 payout_factor:ratio:发放系数
bonus_awards;员工奖金授予;3;plan_id:ref.workforce_bonus_plans:奖金方案 employee_id:ref.workforce_employees:员工主键 awarded_bonus:money:授予奖金
recognition_events;员工表彰奖励;2;employee_id:ref.workforce_employees:员工主键 recognition_type:enum.创新|服务|协作|贡献:表彰类型 reward_value:money:奖励价值
""",
        "薪酬福利": """
salary_bands;岗位薪酬带宽;1;position_id:ref.workforce_positions:岗位主键 minimum_salary:money:最低薪资 midpoint_salary:money:薪资中位数 maximum_salary:money:最高薪资
salary_reviews;员工调薪评审;2;employee_id:ref.workforce_employees:员工主键 increase_rate:ratio:调薪比例 review_reason:enum.绩效|晋升|市场|保留:调薪原因
salary_changes;员工薪资变更;3;employee_id:ref.workforce_employees:员工主键 prior_salary:money:原薪资 revised_salary:money:新薪资
allowance_plans;津贴补贴项目;1;allowance_name:label:津贴名称 allowance_type:enum.交通|住房|通讯|餐饮:补贴类型 standard_amount:money:标准金额
allowance_enrollments;员工津贴参与;3;employee_id:ref.workforce_employees:员工主键 plan_id:ref.workforce_allowance_plans:津贴计划 entitled_amount:money:可享金额
insurance_plans;员工保险保障计划;1;plan_name:label:保险计划 coverage_amount:money:保障额度 premium_amount:money:保费
insurance_enrollments;员工保险参保记录;3;employee_id:ref.workforce_employees:员工主键 plan_id:ref.workforce_insurance_plans:保险计划 dependent_count:int:附属人数
insurance_claims;员工保险理赔;3;enrollment_id:ref.workforce_insurance_enrollments:参保记录 claimed_benefit:money:申请理赔 approved_benefit:money:批准理赔
retirement_accounts;员工补充养老账户;2;employee_id:ref.workforce_employees:员工主键 employee_contribution:money:个人缴纳 employer_contribution:money:企业缴纳
equity_awards;员工长期激励授予;2;employee_id:ref.workforce_employees:员工主键 granted_shares:metric:授予份额 vested_ratio:ratio:归属比例
""",
        "学习培训": """
learning_paths;员工学习路径;1;path_name:label:学习路径 target_audience:enum.新人|专业|管理:适用人群
path_courses;学习路径课程安排;2;path_id:ref.workforce_learning_paths:学习路径 course_id:ref.workforce_courses:课程主键 mandatory_course:flag:是否必修
learning_enrollments;员工学习计划报名;3;employee_id:ref.workforce_employees:员工主键 path_id:ref.workforce_learning_paths:学习路径 completion_percentage:ratio:完成比例
learning_sessions;培训课程开班;2;course_id:ref.workforce_courses:课程主键 instructor_id:ref.workforce_employees:讲师主键 seat_capacity:int:班级容量
session_attendance;培训课堂出勤;4;session_id:ref.workforce_learning_sessions:培训班次 employee_id:ref.workforce_employees:员工主键 attended_hours:hours:出席小时
learning_assessments;培训学习测评;3;employee_id:ref.workforce_employees:员工主键 course_id:ref.workforce_courses:课程主键 knowledge_score:rating:知识评分
course_feedback;培训课程评价;3;course_id:ref.workforce_courses:课程主键 employee_id:ref.workforce_employees:员工主键 relevance_score:rating:相关性评分
training_vendors;外部培训供应商;1;vendor_name:label:培训供应商 specialty_area:enum.技术|销售|管理|合规:专业领域 daily_fee:money:每日培训费用
training_invoices;外部培训费用结算;2;vendor_id:ref.workforce_training_vendors:培训供应商 course_id:ref.workforce_courses:课程主键 invoice_value:money:结算金额
learning_transfers;培训成果应用评估;2;employee_id:ref.workforce_employees:员工主键 course_id:ref.workforce_courses:课程主键 applied_flag:flag:是否应用 benefit_value:money:应用收益
""",
        "劳动力调度": """
shift_templates;工作班次模板;1;template_name:label:班次名称 shift_hours:hours:班次小时 night_shift:flag:是否夜班
shift_requirements;部门排班需求;3;department_id:ref.workforce_departments:部门主键 template_id:ref.workforce_shift_templates:班次模板 required_workers:int:需求人数
shift_assignments;员工班次分配;4;employee_id:ref.workforce_employees:员工主键 template_id:ref.workforce_shift_templates:班次模板 scheduled_hours:hours:计划工时
shift_swaps;员工换班请求;3;employee_id:ref.workforce_employees:申请员工 partner_id:ref.workforce_employees:换班员工 swap_approved:flag:是否批准
clock_events;员工考勤打卡事件;5;employee_id:ref.workforce_employees:员工主键 clock_type:enum.上班|下班|外出|返回:打卡类型 late_flag:flag:是否迟到
attendance_exceptions;员工考勤异常;3;employee_id:ref.workforce_employees:员工主键 exception_kind:enum.缺卡|迟到|旷工|重复:异常类型 exception_minutes:int:异常分钟
leave_balances;员工假期余额;3;employee_id:ref.workforce_employees:员工主键 leave_category:enum.年假|调休|福利假:假期类别 balance_hours:hours:剩余小时
leave_accruals;员工假期累计流水;4;employee_id:ref.workforce_employees:员工主键 accrued_hours:hours:新增小时 expired_hours:hours:失效小时
capacity_allocations;项目人员产能分配;3;project_id:ref.workforce_projects:项目主键 employee_id:ref.workforce_employees:员工主键 available_hours:hours:可用工时
contingent_workers;外包劳动力使用;2;department_id:ref.workforce_departments:部门主键 supplier_id:ref.supply_suppliers:供应商主键 contractor_hours:hours:外包工时 hourly_rate:money:小时费率
""",
        "员工关系": """
engagement_surveys;员工敬业度调查;1;survey_name:label:调查名称 survey_dimension:enum.认同|支持|成长|协作:调查维度
engagement_responses;员工敬业度答卷;4;survey_id:ref.workforce_engagement_surveys:调查主键 employee_id:ref.workforce_employees:员工主键 engagement_rating:rating:敬业度评分
wellbeing_programs;员工健康关怀项目;1;program_name:label:关怀项目 program_category:enum.运动|心理|健康|家庭:项目类别
wellbeing_enrollments;健康关怀参与记录;3;program_id:ref.workforce_wellbeing_programs:关怀项目 employee_id:ref.workforce_employees:员工主键 participation_hours:hours:参与小时
grievance_cases;员工申诉案件;2;employee_id:ref.workforce_employees:员工主键 grievance_kind:enum.薪酬|管理|环境|公平:申诉类别 case_state:enum.受理|调查|调解|结案:处理状态
grievance_actions;员工申诉处理行动;3;case_id:ref.workforce_grievance_cases:申诉案件 action_kind:enum.访谈|核查|协商|整改:处理行动 effort_hours:hours:处理工时
employee_suggestions;员工改善建议;3;employee_id:ref.workforce_employees:员工主键 suggestion_title:label:建议标题 expected_savings:money:预期节约
suggestion_reviews;员工建议评审结果;2;suggestion_id:ref.workforce_employee_suggestions:建议主键 adopted_flag:flag:是否采纳 awarded_value:money:奖励金额
union_activities;员工组织活动;1;activity_name:label:活动名称 participant_count:metric:参与人数 activity_expense:money:活动支出
employee_relations;员工关系沟通记录;3;employee_id:ref.workforce_employees:员工主键 communication_topic:enum.发展|薪酬|家庭|工作:沟通主题 satisfaction_after:rating:沟通后满意度
""",
        "合规与安全": """
policy_catalog;员工制度目录;1;policy_name:label:制度名称 policy_category:enum.行为|安全|隐私|利益冲突:制度类别
policy_acknowledgments;员工制度签收;4;policy_id:ref.workforce_policy_catalog:制度主键 employee_id:ref.workforce_employees:员工主键 acknowledged_flag:flag:是否签收
compliance_training;员工合规学习记录;3;employee_id:ref.workforce_employees:员工主键 policy_id:ref.workforce_policy_catalog:制度主键 passed_assessment:flag:考核通过标志
conflict_disclosures;员工利益冲突申报;2;employee_id:ref.workforce_employees:员工主键 disclosure_type:enum.投资|亲属|兼职|无冲突:申报类型 reviewed_flag:flag:是否审核
health_checks;员工职业健康检查;2;employee_id:ref.workforce_employees:员工主键 fitness_status:enum.适任|限制|复查:模拟适岗状态 followup_required:flag:是否复查
safety_observations;工作环境安全观察;3;department_id:ref.workforce_departments:部门主键 observation_area:enum.设备|防护|操作|环境:观察领域 hazard_score:rating:危险评分
hazard_reports;员工隐患报告;3;employee_id:ref.workforce_employees:员工主键 hazard_description:label:隐患描述 urgency_level:rating:紧急程度
hazard_corrections;安全隐患治理;3;report_id:ref.workforce_hazard_reports:隐患报告 correction_cost:money:整改费用 eliminated_flag:flag:是否消除
emergency_drills;应急演练活动;1;department_id:ref.workforce_departments:部门主键 drill_type:enum.消防|疏散|急救|应急:演练类别 response_minutes:int:响应分钟
drill_participants;应急演练员工表现;3;drill_id:ref.workforce_emergency_drills:演练主键 employee_id:ref.workforce_employees:员工主键 drill_score:rating:演练评分
""",
    },
    "insight": {
        "广告投放": """
media_platforms;广告媒体平台目录;1;platform_name:label:媒体平台 platform_type:enum.搜索|社交|视频|资讯:平台类型
media_accounts;广告主平台账户;1;platform_id:ref.insight_media_platforms:媒体平台 account_name:label:广告账户 spend_limit:money:消费限额
ad_groups;营销活动广告组;2;campaign_id:ref.insight_campaigns:营销活动 targeting_mode:enum.兴趣|人群|关键词:定向模式 group_budget:money:广告组预算
ad_keywords;搜索广告关键词;3;group_id:ref.insight_ad_groups:广告组 keyword_text:label:关键词 max_bid:money:最高出价
bid_adjustments;广告出价调整记录;4;group_id:ref.insight_ad_groups:广告组 bid_change:delta:出价变化 adjustment_reason:enum.竞争|转化|预算:调整原因
ad_placements;广告版位资源;1;platform_id:ref.insight_media_platforms:媒体平台 placement_name:label:版位名称 visibility_rate:ratio:可见率
placement_delivery;广告版位投放绩效;5;placement_id:ref.insight_ad_placements:版位资源 campaign_id:ref.insight_campaigns:营销活动 delivered_impressions:metric:投放曝光量 delivery_cost:money:投放成本
creative_tests;广告素材测试配置;2;creative_id:ref.insight_ad_creatives:广告素材 hypothesis:label:测试假设 minimum_samples:metric:最小样本量
creative_performance;广告素材表现指标;4;creative_id:ref.insight_ad_creatives:广告素材 clicks_observed:metric:观察点击量 conversions_observed:quantity:观察转化数
media_invoices;广告媒体费用账单;3;account_id:ref.insight_media_accounts:广告账户 billed_spend:money:账单消费 service_fee:money:媒体服务费
""",
        "搜索内容": """
content_pages;内容页面目录;1;page_title:label:页面标题 content_category:enum.攻略|评测|教程|资讯:内容类别
content_authors;内容作者画像;1;author_name:label:模拟作者 specialty_topic:enum.数码|家居|食品|运动:擅长主题
content_publications;文章发布及渠道;2;page_id:ref.insight_content_pages:内容页面 author_id:ref.insight_content_authors:内容作者 publication_channel:enum.官网|社区|公众号:发布渠道
content_engagement;内容互动表现;5;page_id:ref.insight_content_pages:内容页面 likes_count:metric:点赞量 shares_count:metric:分享量 reading_minutes:metric:阅读分钟
search_keywords;自然搜索关键词;1;keyword_name:label:关键词名称 search_intent:enum.信息|比较|购买:搜索意图
keyword_rankings;搜索排名观察;4;keyword_id:ref.insight_search_keywords:关键词主键 page_id:ref.insight_content_pages:内容页面 ranking_position:int:排名位置
organic_landings;自然搜索落地访问;5;keyword_id:ref.insight_search_keywords:关键词主键 page_id:ref.insight_content_pages:内容页面 landing_sessions:metric:落地会话数
internal_search_terms;站内搜索词字典;1;term_text:label:搜索词 category_group:enum.商品|服务|订单|帮助:词语类别
search_zero_results;搜索无结果事件;4;term_id:ref.insight_internal_search_terms:搜索词 customer_id:ref.retail_customers:客户主键 reformulated_flag:flag:是否改写搜索
content_conversions;内容引导订单转化;4;page_id:ref.insight_content_pages:内容页面 order_id:ref.retail_orders:订单主键 content_revenue:net:内容关联订单净额
""",
        "产品使用": """
app_versions;应用版本发布目录;1;version_name:label:版本名称 release_channel:enum.稳定|灰度|测试:发布通道
feature_catalog;数字产品功能目录;1;feature_name:label:功能名称 feature_area:enum.搜索|购物|支付|服务:功能领域
feature_rollouts;产品功能灰度发布;2;feature_id:ref.insight_feature_catalog:功能主键 version_id:ref.insight_app_versions:应用版本 rollout_rate:ratio:灰度比例
feature_usage;客户产品功能使用;5;feature_id:ref.insight_feature_catalog:功能主键 customer_id:ref.retail_customers:客户主键 usage_seconds:metric:使用秒数
onboarding_steps;产品新手引导步骤;1;step_name:label:引导步骤 mandatory_step:flag:是否必须 step_order:int:步骤次序
onboarding_events;客户新手引导行为;4;step_id:ref.insight_onboarding_steps:引导步骤 customer_id:ref.retail_customers:客户主键 completed_step:flag:是否完成
app_crashes;应用崩溃事件;4;version_id:ref.insight_app_versions:应用版本 device_id:ref.insight_devices:设备主键 crash_signature:label:崩溃特征
app_startups;应用启动性能记录;5;version_id:ref.insight_app_versions:应用版本 device_id:ref.insight_devices:设备主键 startup_ms:metric:启动毫秒
notification_preferences;客户通知偏好;2;customer_id:ref.retail_customers:客户主键 notification_kind:enum.促销|物流|服务:通知类别 enabled_flag:flag:是否启用
uninstall_signals;客户应用卸载信号;3;customer_id:ref.retail_customers:客户主键 version_id:ref.insight_app_versions:应用版本 uninstall_reason:enum.性能|空间|价值|未知:卸载原因
""",
        "实验科学": """
experiment_metrics;实验指标配置;1;experiment_id:ref.insight_experiments:实验主键 metric_name:enum.收入|留存|耗时|转化:指标名称 guardrail_flag:flag:是否护栏指标
experiment_variants;实验变体定义;1;experiment_id:ref.insight_experiments:实验主键 variant_name:enum.对照|方案甲|方案乙:变体名称 allocation_share:ratio:分配比例
experiment_exposures;客户实验曝光;5;experiment_id:ref.insight_experiments:实验主键 customer_id:ref.retail_customers:客户主键 exposure_count:quantity:曝光次数
experiment_events;实验目标事件;5;experiment_id:ref.insight_experiments:实验主键 customer_id:ref.retail_customers:客户主键 event_value:money:事件价值
metric_observations;实验指标观测;4;metric_id:ref.insight_experiment_metrics:实验指标 measured_metric:metric:观测值 sample_size:metric:样本量
experiment_readouts;实验统计解读;2;experiment_id:ref.insight_experiments:实验主键 effect_estimate:delta:效应估计 uncertainty_rate:ratio:不确定度
sample_ratio_checks;实验分组比例检测;3;experiment_id:ref.insight_experiments:实验主键 observed_share:ratio:观察分配比例 mismatch_flag:flag:是否分流异常
guardrail_alerts;实验护栏指标告警;3;metric_id:ref.insight_experiment_metrics:实验指标 breach_value:delta:越界幅度 paused_flag:flag:是否暂停
experiment_decisions;实验上线决策;1;experiment_id:ref.insight_experiments:实验主键 decision:enum.上线|停止|延长|复测:决策结果 business_rationale:label:业务理由
holdout_observations;长期保留组观察;4;customer_id:ref.retail_customers:客户主键 experiment_id:ref.insight_experiments:实验主键 retained_flag:flag:是否留存 revenue_value:money:收入值
""",
        "社交传播": """
social_accounts;品牌社交账号;1;account_name:label:社交账号 platform_category:enum.短视频|社区|微博|论坛:平台类别
social_posts;社交内容帖子;3;account_id:ref.insight_social_accounts:社交账号 post_title:label:帖子标题 post_format:enum.图文|短视频|直播:内容形式
social_reactions;社交内容互动;5;post_id:ref.insight_social_posts:帖子主键 reaction_kind:enum.赞|评|转|收藏:互动类型 reaction_count:metric:互动数量
influencers;达人合作资源;1;influencer_name:label:模拟达人 audience_size:metric:受众规模 niche:enum.美妆|生活|科技|食品:垂直领域
influencer_contracts;达人营销合作合同;2;influencer_id:ref.insight_influencers:达人主键 campaign_id:ref.insight_campaigns:营销活动 cooperation_fee:money:合作费用
influencer_deliveries;达人合作内容交付;3;contract_id:ref.insight_influencer_contracts:合作合同 delivered_posts:quantity:交付篇数 verified_reach:metric:核验触达量
community_groups;用户兴趣社群;1;group_name:label:社群名称 community_topic:enum.新品|优惠|体验|售后:社群主题
community_members;客户社群成员关系;4;group_id:ref.insight_community_groups:社群主键 customer_id:ref.retail_customers:客户主键 active_days:int:活跃天数
community_messages;社群内容主题统计;5;group_id:ref.insight_community_groups:社群主键 message_topic:enum.咨询|评价|分享|投诉:消息主题 message_volume:metric:消息量
social_conversions;社交内容订单归因;4;post_id:ref.insight_social_posts:帖子主键 order_id:ref.retail_orders:订单主键 social_revenue:net:关联订单净额
""",
        "用户画像": """
audience_segments;营销受众分群定义;1;segment_name:label:受众分群 segment_basis:enum.兴趣|行为|价值|阶段:分群依据
audience_members;客户受众分群成员;5;segment_id:ref.insight_audience_segments:受众分群 customer_id:ref.retail_customers:客户主键 affinity_score:ratio:亲和评分
interest_topics;客户兴趣主题字典;1;topic_name:label:兴趣主题 topic_group:enum.生活|科技|娱乐|教育:主题类别
customer_interests;客户兴趣偏好强度;4;customer_id:ref.retail_customers:客户主键 topic_id:ref.insight_interest_topics:兴趣主题 interest_strength:ratio:兴趣强度
propensity_scores;客户行为倾向评分;4;customer_id:ref.retail_customers:客户主键 propensity_type:enum.购买|流失|升级|复购:倾向类型 probability_score:ratio:概率评分
identity_graph;客户跨设备身份匹配;4;customer_id:ref.retail_customers:客户主键 device_id:ref.insight_devices:设备主键 match_confidence:ratio:匹配置信度
consent_events;数字隐私授权事件;3;customer_id:ref.retail_customers:客户主键 consent_purpose:enum.营销|个性化|分析:授权目的 opted_in:flag:是否同意
audience_exports;受众分群平台推送;2;segment_id:ref.insight_audience_segments:受众分群 platform_id:ref.insight_media_platforms:媒体平台 exported_count:metric:推送人数
audience_match_rates;受众平台匹配率;3;segment_id:ref.insight_audience_segments:受众分群 matched_users:metric:匹配人数 match_rate:ratio:匹配比例
segment_migrations;客户分群迁移记录;3;customer_id:ref.retail_customers:客户主键 segment_id:ref.insight_audience_segments:目标分群 migration_reason:enum.活跃|购买|偏好|规则:迁移原因
""",
        "推荐搜索": """
recommendation_models;推荐模型版本;1;model_name:label:模型名称 model_family:enum.协同|内容|排序|混合:模型类型
recommendation_requests;客户推荐请求;5;customer_id:ref.retail_customers:客户主键 model_id:ref.insight_recommendation_models:推荐模型 requested_items:quantity:请求推荐数
recommendation_items;推荐商品候选集;5;request_id:ref.insight_recommendation_requests:推荐请求 product_id:ref.retail_products:商品主键 ranking_score:ratio:排序评分
recommendation_clicks;推荐商品点击;5;request_id:ref.insight_recommendation_requests:推荐请求 product_id:ref.retail_products:商品主键 click_position:int:点击位置
recommendation_sales;推荐商品成交归因;4;model_id:ref.insight_recommendation_models:推荐模型 order_id:ref.retail_orders:订单主键 recommendation_revenue:net:推荐关联净额
search_models;搜索排序模型目录;1;model_name:label:搜索模型 ranking_strategy:enum.相关性|销量|个性化:排序策略
search_requests;客户搜索请求质量;5;customer_id:ref.retail_customers:客户主键 model_id:ref.insight_search_models:搜索模型 response_latency:metric:响应延迟
search_clicks;搜索结果商品点击;5;request_id:ref.insight_search_requests:搜索请求 product_id:ref.retail_products:商品主键 result_position:int:结果位置
relevance_judgments;搜索相关性人工评估;3;model_id:ref.insight_search_models:搜索模型 product_id:ref.retail_products:商品主键 relevance_grade:rating:相关性等级
ranking_metrics;推荐搜索离线指标;3;model_id:ref.insight_recommendation_models:推荐模型 precision_score:ratio:精确率 diversity_score:ratio:多样性评分
""",
        "服务可靠性": """
service_catalog;数字服务目录;1;service_name:label:服务名称 criticality_level:rating:关键等级
service_dependencies;数字服务依赖关系;1;service_id:ref.insight_service_catalog:服务主键 dependency_label:label:依赖名称 dependency_type:enum.数据库|接口|缓存:依赖类型
service_requests;应用接口请求指标;5;service_id:ref.insight_service_catalog:服务主键 request_count:metric:请求数 success_count:metric:成功数
service_errors;应用服务错误事件;5;service_id:ref.insight_service_catalog:服务主键 error_category:enum.超时|参数|资源|依赖:错误类别 affected_requests:metric:受影响请求数
latency_histograms;服务响应延迟分桶;4;service_id:ref.insight_service_catalog:服务主键 latency_bucket:enum.低延迟|一般|慢|超时:延迟分桶 bucket_requests:metric:分桶请求数
availability_windows;服务可用性时间窗;4;service_id:ref.insight_service_catalog:服务主键 uptime_ratio:ratio:可用性比例 downtime_minutes:metric:不可用分钟
incident_records;数字服务故障工单;2;service_id:ref.insight_service_catalog:服务主键 incident_level:enum.一级|二级|三级:故障等级 recovery_minutes:metric:恢复分钟
incident_impacts;服务故障业务影响;3;incident_id:ref.insight_incident_records:故障工单 lost_orders:metric:受影响订单量 estimated_loss:money:估计损失
deployment_events;数字服务部署发布;3;service_id:ref.insight_service_catalog:服务主键 version_id:ref.insight_app_versions:应用版本 rollback_flag:flag:是否回滚
capacity_metrics;数字服务资源容量;5;service_id:ref.insight_service_catalog:服务主键 cpu_utilization:ratio:计算利用率 memory_utilization:ratio:内存利用率
""",
        "外部市场": """
regional_population;地区人口结构指标，仅弱关联;2;age_group:enum.青年|中年|老年:年龄分组 population_count:metric:人口指标 urbanization_rate:ratio:城市化率
regional_income;地区居民收入指标，仅弱关联;2;income_group:enum.低收入|中收入|高收入:收入分组 disposable_income:money:可支配收入 savings_rate:ratio:储蓄比例
regional_consumption;地区消费结构指标，仅弱关联;3;consumption_category:enum.食品|服饰|居住|服务:消费类别 spending_index:metric:消费指数
regional_employment;地区就业结构指标，仅弱关联;2;industry_group:enum.制造|服务|科技|农业:行业类别 employment_index:metric:就业指数 unemployment_ratio:ratio:失业比率
retail_confidence;地区消费者信心，仅弱关联;3;confidence_dimension:enum.就业|收入|消费|预期:信心维度 confidence_index:metric:信心指数
category_demand;外部品类需求指数，仅弱关联;3;category_group:enum.数码|家居|服饰|食品:品类分组 demand_index:metric:需求指数 search_growth:delta:搜索增速
competitor_campaigns;竞品营销活动观察，仅弱关联;2;competitor_brand:label:竞品品牌 campaign_kind:enum.折扣|联名|直播|新品:活动类型 estimated_spend:money:模拟投放费用
competitor_assortment;竞品品类结构观察，仅弱关联;3;category_group:enum.数码|家居|服饰|食品:品类分组 assortment_width:metric:商品宽度 new_product_share:ratio:新品比例
market_price_indices;地区市场价格指数，仅弱关联;3;commodity_group:enum.能源|食品|原料|服务:价格类别 price_level:metric:价格水平 price_change:delta:价格变化
consumer_trends;地区消费热点趋势，仅弱关联;3;trend_topic:enum.健康|智能|绿色|悦己:趋势主题 popularity_index:metric:热度指数 sentiment_balance:delta:情绪净值
""",
        "环境与地理": """
weather_stations;地区气象观测点，仅弱关联;1;station_name:label:气象站 altitude_meters:metric:海拔米数 station_type:enum.城市|郊区|山区:站点类型
weather_forecasts;地区天气模拟预报，仅弱关联;3;forecast_temperature:temperature:预报气温 precipitation_probability:ratio:降水概率 forecast_horizon:int:提前天数
extreme_weather;地区极端天气事件，仅弱关联;2;hazard_type:enum.暴雨|高温|寒潮|台风:天气灾害 severity_index:rating:严重等级 affected_days:int:影响天数
air_quality;地区空气质量观察，仅弱关联;3;air_quality_index:metric:空气质量指数 pollutant_type:enum.颗粒物|臭氧|二氧化氮:污染物类别
traffic_conditions;地区交通拥堵指标，仅弱关联;4;congestion_index:metric:拥堵指数 average_speed:metric:平均速度 road_category:enum.主干道|快速路|高速:道路类别
public_events;地区公共活动记录，仅弱关联;2;event_name:label:活动名称 event_category:enum.展会|赛事|演出|节庆:活动类别 estimated_attendance:metric:预计参加人数
tourism_flows;地区旅游客流指数，仅弱关联;3;visitor_origin:enum.本地|国内|国际:客源类型 visitor_index:metric:客流指数 overnight_share:ratio:过夜比例
commercial_rents;地区商圈租金指数，仅弱关联;2;commercial_grade:enum.核心|次级|社区:商圈等级 rental_index:metric:租金指数 vacancy_rate:ratio:空置率
transport_access;地区交通可达性指数，仅弱关联;2;transit_mode:enum.地铁|公交|驾车|步行:交通方式 access_score:rating:可达性评分 average_minutes:int:平均耗时
regional_disruptions;地区经营中断事件，仅弱关联;2;disruption_type:enum.停电|交通|通信|施工:中断类型 duration_hours:hours:持续小时 economic_impact:money:模拟经济影响
""",
    },
}
