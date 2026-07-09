#!/usr/bin/env python3
"""将测试数据直接导入到4个数据库中（MySQL/PostgreSQL/MSSQL/Oracle）。

使用方式:
    python data/import_test_data.py --scale 200000 --db all
    python data/import_test_data.py --scale 50000 --db mysql
    python data/import_test_data.py --scale 10000 --table customers
    python data/import_test_data.py --verify-only  # 仅验证不导入
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from faker import Faker
except ImportError:
    print("请先安装: pip install faker")
    sys.exit(1)

fake = Faker("zh_CN")


def _pg_connect():
    """PostgreSQL连接 — 需要autocommit以确保DDL立即生效。"""
    conn = __import__("psycopg2").connect(
        host="192.168.195.133", port=5432, user="postgres",
        password="1Qaz@2wsx124", dbname="postgres", connect_timeout=10)
    conn.autocommit = True
    return conn


# ================================================================
# 数据库连接配置
# ================================================================

DB_CONFIGS = {
    "mysql": {
        "connect": lambda: __import__("pymysql").connect(
            host="192.168.195.133", port=3306, user="root",
            password="1Qaz@2wsx124", database="test", charset="utf8mb4",
            connect_timeout=10, autocommit=False),
        "placeholder": "%s",
        "quote": ("`", "`"),
    },
    "postgres": {
        "connect": lambda: _pg_connect(),
        "placeholder": "%s",
        "quote": ('"', '"'),
    },
    "mssql": {
        "connect": lambda: __import__("pymssql").connect(
            host="192.168.195.133", port=1433, user="sa",
            password="1Qaz@2wsx124", database="master", login_timeout=10,
            autocommit=False),
        "placeholder": "%s",
        "quote": ("[", "]"),
    },
    "oracle": {
        "connect": lambda: __import__("oracledb").connect(
            host="192.168.195.133", port=1521, user="TEST_USER",
            password="1Qaz@2wsx124", service_name="XEPDB1"),
        "placeholder": ":{0}",
        "quote": ('"', '"'),
    },
}

# ================================================================
# 中文数据生成函数
# ================================================================

PROVINCE_CITIES = {
    "北京市": ["朝阳区", "海淀区", "丰台区", "西城区", "东城区", "通州区", "大兴区"],
    "上海市": ["浦东新区", "黄浦区", "徐汇区", "静安区", "长宁区", "闵行区", "杨浦区"],
    "广东省": ["广州市", "深圳市", "东莞市", "佛山市", "珠海市", "惠州市", "中山市"],
    "浙江省": ["杭州市", "宁波市", "温州市", "嘉兴市", "湖州市", "绍兴市", "金华市"],
    "江苏省": ["南京市", "苏州市", "无锡市", "常州市", "南通市", "徐州市", "扬州市"],
    "四川省": ["成都市", "绵阳市", "德阳市", "宜宾市", "南充市", "泸州市", "乐山市"],
    "湖北省": ["武汉市", "宜昌市", "襄阳市", "荆州市", "黄石市", "十堰市", "鄂州市"],
    "山东省": ["济南市", "青岛市", "烟台市", "潍坊市", "临沂市", "淄博市", "济宁市"],
}

SURNAMES = ["王", "李", "张", "刘", "陈", "杨", "黄", "赵", "周", "吴",
            "徐", "孙", "马", "朱", "胡", "郭", "何", "高", "林", "罗",
            "郑", "梁", "谢", "宋", "唐", "韩", "曹", "许", "邓", "冯"]

GIVEN_NAMES = ["伟", "芳", "娜", "秀英", "敏", "静", "丽", "强", "磊", "洋",
               "勇", "艳", "杰", "军", "刚", "平", "明", "辉", "玲",
               "桂英", "文", "华", "建平", "志强", "海燕", "超", "小龙", "飞", "玉兰",
               "鑫", "鹏", "浩然", "子涵", "梓萱", "雨桐", "一鸣", "欣怡", "宇轩", "诗涵"]

CATEGORIES = [
    ("电子产品", ["智能手机", "平板电脑", "笔记本电脑", "智能手表", "蓝牙耳机", "数码相机", "移动电源", "数据线"]),
    ("服装鞋帽", ["T恤", "牛仔裤", "连衣裙", "运动鞋", "羽绒服", "衬衫", "休闲裤", "卫衣"]),
    ("食品饮料", ["有机大米", "龙井茶", "坚果礼盒", "进口红酒", "矿泉水", "方便面", "橄榄油", "蛋白粉"]),
    ("家居用品", ["乳胶枕", "蚕丝被", "空气炸锅", "扫地机器人", "净水器", "加湿器", "电磁炉", "电饭煲"]),
    ("美妆护肤", ["面膜", "防晒霜", "口红", "精华液", "洗面奶", "粉底液", "眼霜", "卸妆水"]),
    ("母婴用品", ["纸尿裤", "奶粉", "婴儿车", "儿童座椅", "益智玩具", "奶瓶", "爬行垫", "婴儿湿巾"]),
    ("运动户外", ["跑步机", "瑜伽垫", "登山包", "帐篷", "钓竿", "自行车", "篮球", "泳镜"]),
    ("图书文娱", ["小说", "编程书籍", "儿童绘本", "考研资料", "字帖", "拼图", "围棋", "手工材料"]),
]

PAYMENT_METHODS = ["微信支付", "支付宝", "银行卡", "信用卡", "花呗分期", "京东白条", "货到付款"]
SHIPPING_COMPANIES = ["顺丰速运", "中通快递", "圆通速递", "韵达快递", "京东物流", "极兔速递", "申通快递", "德邦物流"]
TICKET_STATUSES = ["待处理", "处理中", "已解决", "已关闭", "已升级"]
TICKET_TYPES = ["售前咨询", "订单问题", "退换货", "投诉建议", "技术故障", "物流查询"]
SUPPLIER_TYPES = ["生产商", "代理商", "进口商", "批发商", "代工厂", "品牌直供"]


def random_date(start: str, end: str) -> str:
    s = datetime.fromisoformat(start)
    e = datetime.fromisoformat(end)
    return (s + timedelta(days=random.randint(0, (e - s).days))).strftime("%Y-%m-%d")


def random_datetime(start: str, end: str) -> str:
    s = datetime.fromisoformat(start)
    e = datetime.fromisoformat(end)
    secs = random.randint(0, int((e - s).total_seconds()))
    return (s + timedelta(seconds=secs)).strftime("%Y-%m-%d %H:%M:%S")


def random_name() -> str:
    return random.choice(SURNAMES) + random.choice(GIVEN_NAMES)


def random_phone() -> str:
    p = ["138", "139", "150", "151", "152", "158", "159", "186", "187", "188", "189", "135", "136", "137"]
    return random.choice(p) + "".join(str(random.randint(0, 9)) for _ in range(8))


def random_city() -> tuple:
    prov = random.choice(list(PROVINCE_CITIES.keys()))
    return prov, random.choice(PROVINCE_CITIES[prov])


def gen_product_name() -> str:
    cat = random.choice(CATEGORIES)
    sub = random.choice(cat[1])
    adj = random.choice(["经典款", "新款", "限量版", "热销", "性价比", "高端", "实惠", "网红", "进口", "国产"])
    return f"{adj}{sub}{random.randint(1, 999):03d}"


def gen_brand() -> str:
    pre = ["华", "鑫", "鼎", "瑞", "恒", "盛", "龙", "鹏", "嘉", "博",
           "科", "智", "创", "卓", "信", "达", "通", "源", "金", "丰"]
    suf = ["科技", "集团", "实业", "商贸", "电子", "股份", "控股", "产业", "工贸", "发展"]
    return random.choice(pre) + random.choice(pre) + random.choice(suf)


def gen_company() -> str:
    cities = ["北京", "上海", "深圳", "广州", "杭州", "成都", "武汉", "南京", "重庆", "苏州"]
    return random.choice(cities) + gen_brand() + "有限公司"


def gen_review_text() -> str:
    return random.choice([
        "质量非常好，强烈推荐！", "性价比很高，满意", "物流很快，包装很用心",
        "一直用这个品牌，值得信赖", "客服态度很好", "颜色和图片一致",
        "用了几天了没问题", "比实体店便宜是正品", "还行吧凑合用",
        "一般般没什么特别的", "价格再便宜点就好了", "质量一般不值这个价",
        "快递太慢了", "和描述不太一样", "收到就发现问题了",
    ])


def gen_ticket_desc() -> str:
    return random.choice([
        f"{random.choice(['订单', '商品', '快递'])}出现问题需{random.choice(['退款', '换货', '维修'])}",
        f"购买的商品出现{random.choice(['质量问题', '型号不匹配', '使用故障'])}",
        f"咨询{random.choice(['会员权益', '优惠活动', '退换规则'])}",
        f"希望{random.choice(['催促发货', '更改地址', '开发票'])}",
        f"{random.choice(['投诉', '建议'])}：{random.choice(['客服态度差', '物流太慢', '商品很好'])}",
    ])


# ================================================================
# 表定义
# ================================================================

def get_tables(scale: int) -> dict:
    s = scale
    return {
        "customers": {
            "columns": [
                ("name", str, lambda i: random_name()),
                ("phone", str, lambda i: random_phone()),
                ("email", str, lambda i: f"user{random.randint(10000, 99999999)}@example.com"),
                ("gender", str, lambda i: random.choice(["男", "女"])),
                ("birth_date", str, lambda i: random_date("1960-01-01", "2005-12-31")),
                ("province", str, lambda i: random_city()[0]),
                ("city", str, lambda i: random_city()[1]),
                ("register_date", str, lambda i: random_date("2020-01-01", "2025-12-31")),
                ("customer_level", str, lambda i: random.choice(["普通会员", "银卡会员", "金卡会员", "钻石会员"])),
                ("total_spent", float, lambda i: round(random.uniform(0, 500000), 2)),
            ],
            "row_count": max(s // 2, 50000),
        },
        "products": {
            "columns": [
                ("product_name", str, lambda i: gen_product_name()),
                ("category_id", int, lambda i: random.randint(1, len(CATEGORIES))),
                ("supplier_id", int, lambda i: random.randint(1, 10000)),
                ("unit_price", float, lambda i: round(random.uniform(5, 50000), 2)),
                ("cost_price", float, lambda i: round(random.uniform(3, 30000), 2)),
                ("specification", str, lambda i: random.choice(["标准版", "豪华版", "经济版", "旗舰版", "mini", "Pro", "Max", "Lite"])),
                ("brand", str, lambda i: gen_brand()),
                ("weight_kg", float, lambda i: round(random.uniform(0.01, 50), 3)),
                ("is_active", int, lambda i: 1 if random.random() > 0.1 else 0),
            ],
            "row_count": min(s // 4, 30000),
        },
        "categories": {
            "columns": [
                ("category_name", str, lambda i: CATEGORIES[i % len(CATEGORIES)][0]),
                ("parent_id", int, lambda i: 0),
                ("sort_order", int, lambda i: i + 1),
                ("description", str, lambda i: f"{CATEGORIES[i % len(CATEGORIES)][0]}类商品，包含{', '.join(CATEGORIES[i % len(CATEGORIES)][1])}等"),
            ],
            "row_count": min(s // 200, 500),
        },
        "suppliers": {
            "columns": [
                ("supplier_name", str, lambda i: gen_company()),
                ("contact_person", str, lambda i: random_name()),
                ("contact_phone", str, lambda i: random_phone()),
                ("supplier_type", str, lambda i: random.choice(SUPPLIER_TYPES)),
                ("province", str, lambda i: random_city()[0]),
                ("city", str, lambda i: random_city()[1]),
                ("address", str, lambda i: f"{random_city()[0]}{random_city()[1]}{random.choice(['工业园', '开发区', '高新区'])}{random.randint(1,999)}号"),
                ("rating", float, lambda i: round(random.uniform(1, 5), 2)),
                ("cooperation_since", str, lambda i: random_date("2018-01-01", "2025-06-30")),
            ],
            "row_count": min(s // 10, 5000),
        },
        "employees": {
            "columns": [
                ("name", str, lambda i: random_name()),
                ("department", str, lambda i: random.choice(["销售部", "技术部", "财务部", "人事部", "运营部", "市场部", "客服部", "物流部"])),
                ("position", str, lambda i: random.choice(["经理", "主管", "高级工程师", "专员", "总监", "助理", "实习生", "组长"])),
                ("hire_date", str, lambda i: random_date("2018-01-01", "2025-06-30")),
                ("salary", float, lambda i: round(random.uniform(4000, 50000), 2)),
                ("store_id", int, lambda i: random.randint(1, 2000)),
                ("performance_score", float, lambda i: round(random.uniform(1, 5), 1)),
            ],
            "row_count": min(s // 20, 3000),
        },
        "stores": {
            "columns": [
                ("store_name", str, lambda i: f"{gen_brand()}门店({random_city()[1]}店)"),
                ("province", str, lambda i: random_city()[0]),
                ("city", str, lambda i: random_city()[1]),
                ("address", str, lambda i: f"{random_city()[0]}{random_city()[1]}{fake.street_name()}{random.randint(1,999)}号"),
                ("area_sqm", float, lambda i: round(random.uniform(50, 5000), 2)),
                ("staff_count", int, lambda i: random.randint(3, 200)),
                ("open_date", str, lambda i: random_date("2016-01-01", "2025-01-01")),
                ("store_type", str, lambda i: random.choice(["旗舰店", "标准店", "社区店", "奥莱店", "快闪店"])),
            ],
            "row_count": min(s // 50, 1000),
        },
        "orders": {
            "columns": [
                ("customer_id", int, lambda i: random.randint(1, max(s, 50000))),
                ("store_id", int, lambda i: random.randint(1, 1000)),
                ("order_date", str, lambda i: random_date("2024-01-01", "2025-07-09")),
                ("order_time", str, lambda i: f"{random.randint(0,23):02d}:{random.randint(0,59):02d}:{random.randint(0,59):02d}"),
                ("total_amount", float, lambda i: round(random.uniform(10, 200000), 2)),
                ("discount_amount", float, lambda i: round(random.uniform(0, 5000), 2)),
                ("payment_method", str, lambda i: random.choice(PAYMENT_METHODS)),
                ("order_status", str, lambda i: random.choice(["已完成", "待付款", "待发货", "已发货", "已取消", "退货中", "已退款"])),
                ("notes", str, lambda i: random.choice(["", "加急", "送礼包装", "发票抬头:个人", "周末配送"])),
            ],
            "row_count": max(s, 200000),
        },
        "order_items": {
            "columns": [
                ("order_id", int, lambda i: random.randint(1, max(s, 200000))),
                ("product_id", int, lambda i: random.randint(1, 50000)),
                ("quantity", int, lambda i: random.randint(1, 100)),
                ("unit_price", float, lambda i: round(random.uniform(5, 50000), 2)),
                ("discount", float, lambda i: round(random.uniform(0, 100), 2)),
                ("warehouse_id", int, lambda i: random.randint(1, 50)),
                ("subtotal", float, lambda i: 0),
            ],
            "row_count": max(s * 4 // 3, 500000),
            "post_generate": lambda row: {**row, "subtotal": round(row["quantity"] * row["unit_price"] - row["discount"], 2)},
        },
        "inventory": {
            "columns": [
                ("product_id", int, lambda i: random.randint(1, 50000)),
                ("warehouse_id", int, lambda i: random.randint(1, 50)),
                ("store_id", int, lambda i: random.randint(1, 2000)),
                ("quantity", int, lambda i: random.randint(0, 5000)),
                ("safety_stock", int, lambda i: random.randint(10, 200)),
                ("last_restock_date", str, lambda i: random_date("2025-01-01", "2025-07-09")),
                ("batch_number", str, lambda i: f"BN{random.randint(2024,2025)}{random.randint(1,12):02d}{random.randint(10000,99999)}"),
                ("expiry_date", str, lambda i: random_date("2025-08-01", "2027-12-31")),
            ],
            "row_count": max(s // 3, 50000),
        },
        "payments": {
            "columns": [
                ("order_id", int, lambda i: random.randint(1, max(s, 200000))),
                ("amount", float, lambda i: round(random.uniform(10, 200000), 2)),
                ("payment_method", str, lambda i: random.choice(PAYMENT_METHODS)),
                ("payment_time", str, lambda i: random_datetime("2024-01-01", "2025-07-09")),
                ("transaction_id", str, lambda i: f"TXN{random.randint(2024,2025)}{random.randint(100000000,999999999)}"),
                ("status", str, lambda i: random.choice(["成功", "失败", "退款", "处理中"])),
                ("failure_reason", str, lambda i: random.choice(["", "", "", "余额不足", "银行卡过期", "风控拦截"])),
            ],
            "row_count": max(s, 200000),
        },
        # MySQL专属
        "marketing_campaigns": {
            "columns": [
                ("campaign_name", str, lambda i: f"{random.choice(['双十一', '618', '年货节', '开学季', '女王节', '双十二', '国庆', '元旦'])}营销第{random.randint(1,10)}期"),
                ("start_date", str, lambda i: random_date("2024-01-01", "2025-06-30")),
                ("end_date", str, lambda i: random_date("2024-01-15", "2025-07-09")),
                ("budget", float, lambda i: round(random.uniform(10000, 5000000), 2)),
                ("actual_cost", float, lambda i: round(random.uniform(5000, 5500000), 2)),
                ("channel", str, lambda i: random.choice(["线上-百度", "线上-抖音", "线上-微信", "线下-门店", "线下-地推", "全渠道"])),
                ("target_audience", str, lambda i: random.choice(["年轻女性", "家庭用户", "商务人士", "学生群体", "银发族", "全部用户"])),
                ("conversion_count", int, lambda i: random.randint(100, 50000)),
                ("roi", float, lambda i: round(random.uniform(0.5, 10), 2)),
            ],
            "row_count": min(s // 100, 2000),
            "db_only": "mysql",
        },
        "product_reviews": {
            "columns": [
                ("product_id", int, lambda i: random.randint(1, 50000)),
                ("customer_id", int, lambda i: random.randint(1, 200000)),
                ("rating", int, lambda i: random.choices([1, 2, 3, 4, 5], weights=[2, 5, 15, 35, 43])[0]),
                ("review_text", str, lambda i: gen_review_text()),
                ("review_date", str, lambda i: random_date("2024-01-01", "2025-07-09")),
                ("useful_count", int, lambda i: random.randint(0, 500)),
                ("has_images", int, lambda i: 1 if random.random() > 0.8 else 0),
            ],
            "row_count": max(s // 4, 50000),
            "db_only": "mysql",
        },
        "sales_targets": {
            "columns": [
                ("store_id", int, lambda i: random.randint(1, 2000)),
                ("year", int, lambda i: random.choice([2024, 2025])),
                ("month", int, lambda i: random.randint(1, 12)),
                ("target_amount", float, lambda i: round(random.uniform(50000, 5000000), 2)),
                ("actual_amount", float, lambda i: round(random.uniform(30000, 6000000), 2)),
                ("completion_rate", float, lambda i: round(random.uniform(0.5, 1.5), 4)),
                ("updated_at", str, lambda i: random_date("2024-01-01", "2025-07-09")),
            ],
            "row_count": min(s // 50, 10000),
            "db_only": "mysql",
        },
        # PostgreSQL专属
        "financial_reports": {
            "columns": [
                ("report_type", str, lambda i: random.choice(["月度损益表", "季度资产负债表", "年度现金流量表", "部门费用明细", "项目成本核算"])),
                ("period_start", str, lambda i: random_date("2024-01-01", "2025-06-30")),
                ("period_end", str, lambda i: random_date("2024-02-01", "2025-07-09")),
                ("revenue", float, lambda i: round(random.uniform(100000, 50000000), 2)),
                ("cost", float, lambda i: round(random.uniform(50000, 30000000), 2)),
                ("gross_profit", float, lambda i: 0),
                ("expenses", float, lambda i: round(random.uniform(10000, 5000000), 2)),
                ("net_profit", float, lambda i: 0),
                ("created_by", str, lambda i: random_name()),
            ],
            "row_count": min(s // 100, 3000),
            "db_only": "postgres",
            "post_generate": lambda row: {
                **row, "gross_profit": round(row["revenue"] - row["cost"], 2),
                "net_profit": round(row["revenue"] - row["cost"] - row["expenses"], 2),
            },
        },
        "budget_plans": {
            "columns": [
                ("department", str, lambda i: random.choice(["销售部", "技术部", "市场部", "运营部", "人事部", "财务部"])),
                ("year", int, lambda i: random.choice([2024, 2025])),
                ("budget_amount", float, lambda i: round(random.uniform(50000, 10000000), 2)),
                ("spent_amount", float, lambda i: round(random.uniform(0, 12000000), 2)),
                ("category", str, lambda i: random.choice(["人力成本", "设备采购", "差旅费用", "培训费用", "办公支出", "营销费用"])),
                ("approver", str, lambda i: random_name()),
                ("status", str, lambda i: random.choice(["待审批", "已批准", "已执行", "已超额", "已关闭"])),
            ],
            "row_count": min(s // 200, 1000),
            "db_only": "postgres",
        },
        # MSSQL专属
        "shipping_records": {
            "columns": [
                ("order_id", int, lambda i: random.randint(1, 500000)),
                ("carrier", str, lambda i: random.choice(SHIPPING_COMPANIES)),
                ("tracking_number", str, lambda i: f"SF{random.randint(10000000000,99999999999)}"),
                ("ship_date", str, lambda i: random_date("2024-01-01", "2025-07-09")),
                ("delivery_date", str, lambda i: random_date("2024-01-02", "2025-07-09")),
                ("origin_city", str, lambda i: random_city()[1]),
                ("dest_city", str, lambda i: random_city()[1]),
                ("weight_kg", float, lambda i: round(random.uniform(0.1, 100), 3)),
                ("shipping_cost", float, lambda i: round(random.uniform(5, 500), 2)),
                ("status", str, lambda i: random.choice(["运输中", "已签收", "派送中", "已揽收", "异常"])),
            ],
            "row_count": max(s, 100000),
            "db_only": "mssql",
        },
        "customer_tickets": {
            "columns": [
                ("customer_id", int, lambda i: random.randint(1, 200000)),
                ("ticket_type", str, lambda i: random.choice(TICKET_TYPES)),
                ("status", str, lambda i: random.choice(TICKET_STATUSES)),
                ("priority", str, lambda i: random.choice(["低", "中", "高", "紧急"])),
                ("created_date", str, lambda i: random_date("2024-01-01", "2025-07-09")),
                ("resolved_date", str, lambda i: random_date("2024-01-02", "2025-07-09")),
                ("assigned_to", str, lambda i: random_name()),
                ("satisfaction_score", int, lambda i: random.choices([1, 2, 3, 4, 5], weights=[3, 5, 10, 40, 42])[0]),
                ("description", str, lambda i: gen_ticket_desc()),
            ],
            "row_count": max(s // 5, 30000),
            "db_only": "mssql",
        },
        # Oracle专属
        "hr_records": {
            "columns": [
                ("employee_id", int, lambda i: random.randint(1, 5000)),
                ("record_type", str, lambda i: random.choice(["入职", "转正", "调岗", "晋升", "离职", "薪资调整", "培训记录"])),
                ("record_date", str, lambda i: random_date("2022-01-01", "2025-07-09")),
                ("previous_value", str, lambda i: random.choice(["初级工程师", "主管", "8000", "销售部", ""])),
                ("new_value", str, lambda i: random.choice(["高级工程师", "经理", "12000", "技术部", ""])),
                ("reason", str, lambda i: random.choice(["年度晋升", "组织架构调整", "个人发展", "薪资对标", "人才引进"])),
                ("approved_by", str, lambda i: random_name()),
            ],
            "row_count": min(s // 50, 5000),
            "db_only": "oracle",
        },
        "contracts": {
            "columns": [
                ("contract_name", str, lambda i: f"{random.choice(['采购', '销售', '服务', '代理', '加盟'])}合同-{random.randint(2024,2025)}年第{random.randint(1,999)}号"),
                ("party_a", str, lambda i: gen_company()),
                ("party_b", str, lambda i: gen_company()),
                ("contract_amount", float, lambda i: round(random.uniform(10000, 10000000), 2)),
                ("sign_date", str, lambda i: random_date("2023-01-01", "2025-07-09")),
                ("start_date", str, lambda i: random_date("2023-01-15", "2025-07-09")),
                ("end_date", str, lambda i: random_date("2025-08-01", "2028-12-31")),
                ("status", str, lambda i: random.choice(["履行中", "已完成", "已终止", "续约中"])),
                ("payment_terms", str, lambda i: random.choice(["月结30天", "季结", "预付50%", "验收后付款", "分期付款"])),
            ],
            "row_count": min(s // 100, 3000),
            "db_only": "oracle",
        },
    }


# ================================================================
# 数据库操作
# ================================================================

def build_ddl(table_name: str, columns: list, db: str) -> str:
    """生成方言DDL。"""
    q1, q2 = DB_CONFIGS[db]["quote"]

    if db == "mysql":
        col_defs = [f"{q1}id{q2} INT AUTO_INCREMENT PRIMARY KEY"]
    elif db == "postgres":
        col_defs = [f"{q1}id{q2} SERIAL PRIMARY KEY"]
    elif db == "mssql":
        col_defs = [f"{q1}id{q2} INT IDENTITY(1,1) PRIMARY KEY"]
    elif db == "oracle":
        col_defs = [f"{q1}id{q2} NUMBER(10) GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"]

    for col_name, py_type, _ in columns:
        if db == "mysql":
            dt = "INT" if py_type is int else "DOUBLE" if py_type is float else "VARCHAR(500)"
        elif db == "postgres":
            dt = "INTEGER" if py_type is int else "DOUBLE PRECISION" if py_type is float else "VARCHAR(500)"
        elif db == "mssql":
            dt = "INT" if py_type is int else "FLOAT" if py_type is float else "NVARCHAR(500)"
        elif db == "oracle":
            dt = "NUMBER(10)" if py_type is int else "NUMBER(15,2)" if py_type is float else "VARCHAR2(500)"
        col_defs.append(f"{q1}{col_name}{q2} {dt}")

    extra = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" if db == "mysql" else ""
    return f"CREATE TABLE {q1}{table_name}{q2} (\n  " + ",\n  ".join(col_defs) + f"\n){extra}"


def drop_table(conn, table_name: str, db: str):
    """删除旧表。"""
    q1, q2 = DB_CONFIGS[db]["quote"]
    cur = conn.cursor()
    try:
        if db == "mssql":
            cur.execute(f"IF OBJECT_ID('{table_name}', 'U') IS NOT NULL DROP TABLE {q1}{table_name}{q2}")
        elif db == "oracle":
            try:
                cur.execute(f"DROP TABLE {q1}{table_name}{q2} CASCADE CONSTRAINTS")
            except Exception:
                pass
        else:
            cur.execute(f"DROP TABLE IF EXISTS {q1}{table_name}{q2}")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def insert_batch(conn, table_name: str, columns: list, rows: list[dict], db: str) -> int:
    """批量插入。"""
    if not rows:
        return 0
    q1, q2 = DB_CONFIGS[db]["quote"]
    col_names = [c[0] for c in columns]
    quoted_cols = [f"{q1}{c}{q2}" for c in col_names]

    if db == "oracle":
        ph = [f":{j}" for j in range(len(col_names))]
    else:
        ph = [DB_CONFIGS[db]["placeholder"]] * len(col_names)

    sql = f"INSERT INTO {q1}{table_name}{q2} ({', '.join(quoted_cols)}) VALUES ({', '.join(ph)})"
    data = [tuple(row.get(c, None) for c in col_names) for row in rows]

    cur = conn.cursor()
    try:
        cur.executemany(sql, data)
        conn.commit()
        return len(rows)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def import_table(conn, table_name: str, table_def: dict, db: str, batch_size: int = 5000) -> int:
    """导入单表数据。"""
    columns = table_def["columns"]
    row_count = table_def["row_count"]
    post_gen = table_def.get("post_generate")

    print(f"  [{db}] {table_name}: {row_count:,} 行...", end=" ", flush=True)
    start = time.time()

    drop_table(conn, table_name, db)
    cur = conn.cursor()
    cur.execute(build_ddl(table_name, columns, db))
    conn.commit()

    total = 0
    batch = []

    for i in range(row_count):
        row = {col[0]: col[2](i) for col in columns}
        if post_gen:
            row = post_gen(row)
        batch.append(row)

        if len(batch) >= batch_size:
            insert_batch(conn, table_name, columns, batch, db)
            total += len(batch)
            batch.clear()
            if total % (batch_size * 20) == 0:
                pct = total * 100 // row_count
                print(f"\n    ... {total:,}/{row_count:,} ({pct}%)", end=" ", flush=True)

    if batch:
        insert_batch(conn, table_name, columns, batch, db)
        total += len(batch)

    elapsed = time.time() - start
    rate = total / elapsed if elapsed > 0 else 0
    print(f"完成 ({elapsed:.1f}s, {rate:.0f}行/s)")
    return total


def import_database(database: str, tables: dict, batch_size: int) -> dict:
    """导入指定数据库的所有表。"""
    print(f"\n{'=' * 60}")
    print(f"数据库: {database}")
    print(f"{'=' * 60}")

    conn = DB_CONFIGS[database]["connect"]()
    results = {}
    try:
        for table_name, table_def in tables.items():
            db_only = table_def.get("db_only", "")
            if db_only and db_only != database:
                continue
            try:
                count = import_table(conn, table_name, table_def, database, batch_size)
                results[table_name] = count
            except Exception as e:
                print(f"\n    [ERROR] {table_name}: {e}")
    finally:
        conn.close()
    return results


def verify_data(database: str, tables: dict):
    """验证数据量。"""
    conn = DB_CONFIGS[database]["connect"]()
    q1, q2 = DB_CONFIGS[database]["quote"]
    print(f"\n[{database}] 数据验证:")
    total = 0
    try:
        cur = conn.cursor()
        for table_name, table_def in tables.items():
            db_only = table_def.get("db_only", "")
            if db_only and db_only != database:
                continue
            try:
                cur.execute(f"SELECT COUNT(*) FROM {q1}{table_name}{q2}")
                count = cur.fetchone()[0]
                expected = table_def["row_count"]
                flag = "OK" if count == expected else f"差{count - expected}"
                print(f"  {table_name}: {count:,} 行 (期望{expected:,}) {flag}")
                total += count
            except Exception as e:
                print(f"  {table_name}: 查询失败 - {e}")
        print(f"  {'─' * 25}")
        print(f"  总计: {total:,} 行")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="测试数据导入脚本")
    parser.add_argument("--db", default="all",
                        choices=["all", "mysql", "postgres", "mssql", "oracle"])
    parser.add_argument("--scale", type=int, default=200000,
                        help="数据量缩放因子 (默认: 200000)")
    parser.add_argument("--table", default="", help="仅导入指定表")
    parser.add_argument("--batch-size", type=int, default=5000, help="批量插入大小")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--verify-only", action="store_true", help="仅验证已有数据")

    args = parser.parse_args()
    random.seed(args.seed)
    Faker.seed(args.seed)

    databases = ["mysql", "postgres", "mssql", "oracle"] if args.db == "all" else [args.db]
    all_tables = get_tables(args.scale)

    if args.table:
        if args.table not in all_tables:
            print(f"错误: 表 '{args.table}' 不存在。可用: {', '.join(all_tables.keys())}")
            sys.exit(1)
        all_tables = {args.table: all_tables[args.table]}

    if args.verify_only:
        print("=" * 60)
        print("数据验证模式")
        print("=" * 60)
        for db in databases:
            seed_map = {"mysql": 0, "postgres": 1000, "mssql": 2000, "oracle": 3000}
            random.seed(args.seed + seed_map[db])
            verify_data(db, all_tables)
        return

    total_start = time.time()
    seed_map = {"mysql": 0, "postgres": 1000, "mssql": 2000, "oracle": 3000}
    grand_results = {}

    for db in databases:
        random.seed(args.seed + seed_map[db])
        try:
            grand_results[db] = import_database(db, all_tables, args.batch_size)
        except Exception as e:
            print(f"\n[ERROR] {db} 导入失败: {e}")
            import traceback
            traceback.print_exc()

    elapsed = time.time() - total_start
    grand_total = sum(sum(v.values()) for v in grand_results.values())

    print(f"\n{'=' * 60}")
    print("导入完成摘要")
    print(f"{'=' * 60}")
    for db, results in grand_results.items():
        db_total = sum(results.values())
        print(f"  {db}: {db_total:,} 行 ({len(results)}张表)")
    print(f"  {'─' * 32}")
    print(f"  总计: {grand_total:,} 行")
    print(f"  耗时: {elapsed:.1f}s ({elapsed/60:.1f}min)")

    # 验证
    print(f"\n{'=' * 60}")
    print("数据验证")
    print(f"{'=' * 60}")
    for db in databases:
        if db in grand_results:
            random.seed(args.seed + seed_map[db])
            verify_data(db, all_tables)


if __name__ == "__main__":
    main()
