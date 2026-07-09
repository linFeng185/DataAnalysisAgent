#!/usr/bin/env python3
"""全流程测试数据生成脚本 — 22张表、中文内容、5种数据库引擎、百万级数据。

使用方式:
    python data/generate_test_data.py --db all --scale 1000000 --output-dir data/generated
    python data/generate_test_data.py --db postgres --scale 100000 --format csv
    python data/generate_test_data.py --db clickhouse --scale 5000000 --table website_traffic
    python data/generate_test_data.py --db all --ddl-only  # 仅生成建表语句

依赖: pip install faker
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from faker import Faker
except ImportError:
    print("请先安装 faker: pip install faker")
    sys.exit(1)

# ================================================================
# 全局配置
# ================================================================

fake = Faker("zh_CN")

# 数据库方言SQL引号
DIALECT_QUOTES = {
    "clickhouse": ('"', '"'),
    "mysql": ("`", "`"),
    "postgres": ('"', '"'),
    "oracle": ('"', '"'),
    "mssql": ("[", "]"),
}

ALL_DATABASES = ["clickhouse", "mysql", "postgres", "oracle", "mssql"]

# 中文省份城市映射
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
            "郑", "梁", "谢", "宋", "唐", "韩", "曹", "许", "邓", "冯",
            "彭", "曾", "肖", "田", "董", "潘", "袁", "蔡", "蒋", "余"]

GIVEN_NAMES = ["伟", "芳", "娜", "秀英", "敏", "静", "丽", "强", "磊", "洋",
               "勇", "艳", "杰", "军", "秀兰", "刚", "平", "明", "辉", "玲",
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

SUPPLIER_TYPES = ["生产商", "代理商", "进口商", "批发商", "代工厂", "品牌直供"]
PAYMENT_METHODS = ["微信支付", "支付宝", "银行卡", "信用卡", "花呗分期", "京东白条", "货到付款"]
SHIPPING_COMPANIES = ["顺丰速运", "中通快递", "圆通速递", "韵达快递", "京东物流", "极兔速递", "申通快递", "德邦物流"]
TICKET_STATUSES = ["待处理", "处理中", "已解决", "已关闭", "已升级"]
TICKET_TYPES = ["售前咨询", "订单问题", "退换货", "投诉建议", "技术故障", "物流查询"]


def random_date(start: str, end: str) -> str:
    """生成指定范围内的随机日期。"""
    s = datetime.fromisoformat(start)
    e = datetime.fromisoformat(end)
    delta = (e - s).days
    d = s + timedelta(days=random.randint(0, delta))
    return d.strftime("%Y-%m-%d")


def random_datetime(start: str, end: str) -> str:
    """生成指定范围内的随机日期时间。"""
    s = datetime.fromisoformat(start)
    e = datetime.fromisoformat(end)
    delta = (e - s).total_seconds()
    d = s + timedelta(seconds=random.randint(0, int(delta)))
    return d.strftime("%Y-%m-%d %H:%M:%S")


def random_chinese_name() -> str:
    """生成随机中文姓名。"""
    return random.choice(SURNAMES) + random.choice(GIVEN_NAMES)


def random_phone() -> str:
    """生成随机手机号。"""
    prefixes = ["138", "139", "150", "151", "152", "158", "159", "186", "187", "188", "189", "135", "136", "137"]
    return random.choice(prefixes) + "".join(str(random.randint(0, 9)) for _ in range(8))


def random_city() -> tuple:
    """返回 (省份, 城市) 元组。"""
    province = random.choice(list(PROVINCE_CITIES.keys()))
    city = random.choice(PROVINCE_CITIES[province])
    return province, city


def esc(v: Any) -> str:
    """对值进行SQL转义。"""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    return f"'{str(v)}'"


# ================================================================
# 辅助生成函数
# ================================================================

def _gen_product_name() -> str:
    """生成中文商品名。"""
    cat_idx = random.randint(0, len(CATEGORIES) - 1)
    sub_item = random.choice(CATEGORIES[cat_idx][1])
    adj = random.choice(["经典款", "新款", "限量版", "热销", "性价比", "高端", "实惠", "网红", "进口", "国产"])
    return f"{adj}{sub_item}{random.randint(1,999):03d}"


def _gen_brand() -> str:
    """生成中文品牌名。"""
    pre = ["华", "鑫", "鼎", "瑞", "恒", "盛", "龙", "鹏", "嘉", "博",
           "科", "智", "创", "卓", "信", "达", "通", "源", "金", "丰"]
    suf = ["科技", "集团", "实业", "商贸", "电子", "股份", "控股", "产业", "工贸", "发展"]
    return random.choice(pre) + random.choice(pre) + random.choice(suf)


def _gen_company_name() -> str:
    """生成中文公司名。"""
    cities = ["北京", "上海", "深圳", "广州", "杭州", "成都", "武汉", "南京", "重庆", "苏州"]
    return random.choice(cities) + _gen_brand() + "有限公司"


def _gen_review_text() -> str:
    """生成中文评价文本。"""
    texts = [
        "质量非常好，强烈推荐！", "性价比很高，满意的一次购物", "物流很快，包装很用心",
        "一直在用这个品牌，值得信赖", "客服态度很好，问题及时解决",
        "颜色和图片一致，没有色差", "用了几天了，没什么问题", "比实体店便宜，是正品",
        "还行吧，凑合用", "一般般，没什么特别的", "价格要是再便宜点就好了",
        "质量一般，不值这个价", "快递太慢了", "和描述不太一样", "收到就发现有问题",
    ]
    return random.choice(texts)


def _gen_ticket_desc() -> str:
    """生成中文工单描述。"""
    templates = [
        f"{random.choice(['订单', '商品', '快递'])}出现问题，需要{random.choice(['退款', '换货', '维修', '咨询'])}处理",
        f"购买的商品出现{random.choice(['质量问题', '型号不匹配', '使用故障', '外观损坏'])}",
        f"咨询关于{random.choice(['会员权益', '优惠活动', '退换规则', '保修政策'])}的相关事宜",
        f"希望{random.choice(['催促发货', '更改收货地址', '修改订单', '开发票'])}",
        f"{random.choice(['投诉', '建议', '反馈'])}：{random.choice(['客服态度差', '物流太慢', '商品很好', '希望增加配送区域'])}",
    ]
    return random.choice(templates)


# ================================================================
# 表Schema定义
# ================================================================

def build_schemas(scale: int) -> dict:
    """构建所有表的schema定义，scale控制生成行数。"""
    s = scale
    return {
        # ===== A. 共享表(10张) =====
        "customers": {
            "columns": [
                ("customer_id", "INTEGER", lambda i: i + 1),
                ("name", "VARCHAR(100)", lambda i: random_chinese_name()),
                ("phone", "VARCHAR(20)", lambda i: random_phone()),
                ("email", "VARCHAR(200)", lambda i: f"user{random.randint(10000,99999999)}@example.com"),
                ("gender", "VARCHAR(4)", lambda i: random.choice(["男", "女"])),
                ("birth_date", "DATE", lambda i: random_date("1960-01-01", "2005-12-31")),
                ("province", "VARCHAR(50)", lambda i: random_city()[0]),
                ("city", "VARCHAR(50)", lambda i: random_city()[1]),
                ("register_date", "DATE", lambda i: random_date("2020-01-01", "2025-12-31")),
                ("customer_level", "VARCHAR(20)", lambda i: random.choice(["普通会员", "银卡会员", "金卡会员", "钻石会员"])),
                ("total_spent", "DECIMAL(15,2)", lambda i: round(random.uniform(0, 500000), 2)),
            ],
            "row_count": max(s, 200000),
            "pk": "customer_id",
        },
        "products": {
            "columns": [
                ("product_id", "INTEGER", lambda i: i + 1),
                ("product_name", "VARCHAR(200)", lambda i: _gen_product_name()),
                ("category_id", "INTEGER", lambda i: random.randint(1, len(CATEGORIES))),
                ("supplier_id", "INTEGER", lambda i: random.randint(1, 10000)),
                ("unit_price", "DECIMAL(10,2)", lambda i: round(random.uniform(5, 50000), 2)),
                ("cost_price", "DECIMAL(10,2)", lambda i: round(random.uniform(3, 30000), 2)),
                ("specification", "VARCHAR(100)", lambda i: random.choice(["标准版", "豪华版", "经济版", "旗舰版", "mini", "Pro", "Max", "Lite"])),
                ("brand", "VARCHAR(100)", lambda i: _gen_brand()),
                ("weight_kg", "DECIMAL(8,3)", lambda i: round(random.uniform(0.01, 50), 3)),
                ("is_active", "BOOLEAN", lambda i: random.random() > 0.1),
            ],
            "row_count": min(s // 4, 50000),
            "pk": "product_id",
        },
        "categories": {
            "columns": [
                ("category_id", "INTEGER", lambda i: i + 1),
                ("category_name", "VARCHAR(100)", lambda i: CATEGORIES[i % len(CATEGORIES)][0]),
                ("parent_id", "INTEGER", lambda i: 0),
                ("sort_order", "INTEGER", lambda i: i + 1),
                ("description", "VARCHAR(500)", lambda i: f"{CATEGORIES[i % len(CATEGORIES)][0]}类商品，包含{', '.join(CATEGORIES[i % len(CATEGORIES)][1])}等"),
            ],
            "row_count": min(s // 200, 500),
            "pk": "category_id",
        },
        "suppliers": {
            "columns": [
                ("supplier_id", "INTEGER", lambda i: i + 1),
                ("supplier_name", "VARCHAR(200)", lambda i: _gen_company_name()),
                ("contact_person", "VARCHAR(50)", lambda i: random_chinese_name()),
                ("contact_phone", "VARCHAR(20)", lambda i: random_phone()),
                ("supplier_type", "VARCHAR(50)", lambda i: random.choice(SUPPLIER_TYPES)),
                ("province", "VARCHAR(50)", lambda i: random_city()[0]),
                ("city", "VARCHAR(50)", lambda i: random_city()[1]),
                ("address", "VARCHAR(300)", lambda i: f"{random_city()[0]}{random_city()[1]}{random.choice(['工业园', '开发区', '高新区'])}{random.randint(1,999)}号"),
                ("rating", "DECIMAL(3,2)", lambda i: round(random.uniform(1, 5), 2)),
                ("cooperation_since", "DATE", lambda i: random_date("2018-01-01", "2025-06-30")),
            ],
            "row_count": min(s // 20, 10000),
            "pk": "supplier_id",
        },
        "employees": {
            "columns": [
                ("employee_id", "INTEGER", lambda i: i + 1),
                ("name", "VARCHAR(50)", lambda i: random_chinese_name()),
                ("department", "VARCHAR(50)", lambda i: random.choice(["销售部", "技术部", "财务部", "人事部", "运营部", "市场部", "客服部", "物流部"])),
                ("position", "VARCHAR(50)", lambda i: random.choice(["经理", "主管", "高级工程师", "专员", "总监", "助理", "实习生", "组长"])),
                ("hire_date", "DATE", lambda i: random_date("2018-01-01", "2025-06-30")),
                ("salary", "DECIMAL(10,2)", lambda i: round(random.uniform(4000, 50000), 2)),
                ("store_id", "INTEGER", lambda i: random.randint(1, 2000)),
                ("performance_score", "DECIMAL(3,1)", lambda i: round(random.uniform(1, 5), 1)),
            ],
            "row_count": min(s // 40, 5000),
            "pk": "employee_id",
        },
        "stores": {
            "columns": [
                ("store_id", "INTEGER", lambda i: i + 1),
                ("store_name", "VARCHAR(200)", lambda i: f"{_gen_brand()}门店({random_city()[1]}店)"),
                ("province", "VARCHAR(50)", lambda i: random_city()[0]),
                ("city", "VARCHAR(50)", lambda i: random_city()[1]),
                ("address", "VARCHAR(300)", lambda i: f"{random_city()[0]}{random_city()[1]}{fake.street_name()}{random.randint(1,999)}号"),
                ("area_sqm", "DECIMAL(10,2)", lambda i: round(random.uniform(50, 5000), 2)),
                ("staff_count", "INTEGER", lambda i: random.randint(3, 200)),
                ("open_date", "DATE", lambda i: random_date("2016-01-01", "2025-01-01")),
                ("store_type", "VARCHAR(30)", lambda i: random.choice(["旗舰店", "标准店", "社区店", "奥莱店", "快闪店"])),
            ],
            "row_count": min(s // 100, 2000),
            "pk": "store_id",
        },
        "orders": {
            "columns": [
                ("order_id", "INTEGER", lambda i: i + 1),
                ("customer_id", "INTEGER", lambda i: random.randint(1, max(s, 200000))),
                ("store_id", "INTEGER", lambda i: random.randint(1, 2000)),
                ("order_date", "DATE", lambda i: random_date("2024-01-01", "2025-07-09")),
                ("order_time", "VARCHAR(10)", lambda i: f"{random.randint(0,23):02d}:{random.randint(0,59):02d}:{random.randint(0,59):02d}"),
                ("total_amount", "DECIMAL(15,2)", lambda i: round(random.uniform(10, 200000), 2)),
                ("discount_amount", "DECIMAL(10,2)", lambda i: round(random.uniform(0, 5000), 2)),
                ("payment_method", "VARCHAR(30)", lambda i: random.choice(PAYMENT_METHODS)),
                ("order_status", "VARCHAR(20)", lambda i: random.choice(["已完成", "待付款", "待发货", "已发货", "已取消", "退货中", "已退款"])),
                ("notes", "VARCHAR(500)", lambda i: random.choice(["", "加急", "送礼包装", "发票抬头:个人", "周末配送"])),
            ],
            "row_count": max(s, 500000),
            "pk": "order_id",
        },
        "order_items": {
            "columns": [
                ("item_id", "INTEGER", lambda i: i + 1),
                ("order_id", "INTEGER", lambda i: random.randint(1, max(s, 500000))),
                ("product_id", "INTEGER", lambda i: random.randint(1, 50000)),
                ("quantity", "INTEGER", lambda i: random.randint(1, 100)),
                ("unit_price", "DECIMAL(10,2)", lambda i: round(random.uniform(5, 50000), 2)),
                ("discount", "DECIMAL(10,2)", lambda i: round(random.uniform(0, 100), 2)),
                ("warehouse_id", "INTEGER", lambda i: random.randint(1, 50)),
                ("subtotal", "DECIMAL(15,2)", lambda i: 0),
            ],
            "row_count": max(s * 4, 2000000),
            "pk": "item_id",
            "post_process": lambda row: {**row, "subtotal": round(row["quantity"] * row["unit_price"] - row["discount"], 2)},
        },
        "inventory": {
            "columns": [
                ("inventory_id", "INTEGER", lambda i: i + 1),
                ("product_id", "INTEGER", lambda i: random.randint(1, 50000)),
                ("warehouse_id", "INTEGER", lambda i: random.randint(1, 50)),
                ("store_id", "INTEGER", lambda i: random.randint(1, 2000)),
                ("quantity", "INTEGER", lambda i: random.randint(0, 5000)),
                ("safety_stock", "INTEGER", lambda i: random.randint(10, 200)),
                ("last_restock_date", "DATE", lambda i: random_date("2025-01-01", "2025-07-09")),
                ("batch_number", "VARCHAR(50)", lambda i: f"BN{random.randint(2024,2025)}{random.randint(1,12):02d}{random.randint(10000,99999)}"),
                ("expiry_date", "DATE", lambda i: random_date("2025-08-01", "2027-12-31")),
            ],
            "row_count": max(s // 2, 100000),
            "pk": "inventory_id",
        },
        "payments": {
            "columns": [
                ("payment_id", "INTEGER", lambda i: i + 1),
                ("order_id", "INTEGER", lambda i: random.randint(1, max(s, 500000))),
                ("amount", "DECIMAL(15,2)", lambda i: round(random.uniform(10, 200000), 2)),
                ("payment_method", "VARCHAR(30)", lambda i: random.choice(PAYMENT_METHODS)),
                ("payment_time", "VARCHAR(30)", lambda i: random_datetime("2024-01-01", "2025-07-09")),
                ("transaction_id", "VARCHAR(100)", lambda i: f"TXN{random.randint(2024,2025)}{random.randint(100000000,999999999)}"),
                ("status", "VARCHAR(20)", lambda i: random.choice(["成功", "失败", "退款", "处理中"])),
                ("failure_reason", "VARCHAR(200)", lambda i: random.choice(["", "", "", "余额不足", "银行卡过期", "风控拦截"])),
            ],
            "row_count": max(s, 500000),
            "pk": "payment_id",
        },

        # ===== B. 专属表(12张) =====
        "website_traffic": {
            "columns": [
                ("log_id", "INTEGER", lambda i: i + 1),
                ("visit_time", "VARCHAR(30)", lambda i: random_datetime("2024-01-01", "2025-07-09")),
                ("user_id", "INTEGER", lambda i: random.randint(1, 1000000)),
                ("page_url", "VARCHAR(500)", lambda i: random.choice(["/", "/products", "/cart", "/checkout", "/user/profile", "/search", "/category/电子产品", "/promo"])),
                ("source", "VARCHAR(100)", lambda i: random.choice(["百度搜索", "微信", "抖音", "今日头条", "小红书", "直接访问", "淘宝客", "邮件营销"])),
                ("device", "VARCHAR(30)", lambda i: random.choice(["iPhone", "Android", "Windows PC", "Mac", "iPad", "华为手机"])),
                ("duration_seconds", "INTEGER", lambda i: random.randint(1, 3600)),
                ("bounce", "BOOLEAN", lambda i: random.random() > 0.6),
                ("ip_address", "VARCHAR(45)", lambda i: f"{random.randint(1,255)}.{random.randint(0,255)}.{random.randint(1,254)}.{random.randint(1,254)}"),
            ],
            "row_count": max(s * 3, 3000000),
            "pk": "log_id",
            "db_only": "clickhouse",
        },
        "ad_impressions": {
            "columns": [
                ("impression_id", "INTEGER", lambda i: i + 1),
                ("campaign_id", "INTEGER", lambda i: random.randint(1, 5000)),
                ("ad_name", "VARCHAR(200)", lambda i: f"广告-{_gen_product_name()}-{random.choice(['A','B','C'])}版"),
                ("impression_time", "VARCHAR(30)", lambda i: random_datetime("2024-01-01", "2025-07-09")),
                ("user_id", "INTEGER", lambda i: random.randint(1, 1000000)),
                ("clicked", "BOOLEAN", lambda i: random.random() > 0.9),
                ("converted", "BOOLEAN", lambda i: random.random() > 0.95),
                ("cost_cents", "INTEGER", lambda i: random.randint(1, 5000)),
                ("platform", "VARCHAR(30)", lambda i: random.choice(["百度", "抖音", "微信", "今日头条", "小红书", "B站"])),
            ],
            "row_count": max(s * 2, 2000000),
            "pk": "impression_id",
            "db_only": "clickhouse",
        },
        "user_behavior_logs": {
            "columns": [
                ("behavior_id", "INTEGER", lambda i: i + 1),
                ("user_id", "INTEGER", lambda i: random.randint(1, 1000000)),
                ("event_type", "VARCHAR(50)", lambda i: random.choice(["浏览", "点击", "加购", "收藏", "分享", "搜索", "下单", "支付", "评价", "退货"])),
                ("event_time", "VARCHAR(30)", lambda i: random_datetime("2024-01-01", "2025-07-09")),
                ("product_id", "INTEGER", lambda i: random.randint(1, 50000)),
                ("page", "VARCHAR(200)", lambda i: random.choice(["/首页", "/商品详情", "/搜索", "/购物车", "/订单", "/我的"])),
                ("session_id", "VARCHAR(64)", lambda i: f"sess_{random.randint(100000000,999999999)}_{random.randint(10000,99999)}"),
                ("stay_duration_ms", "INTEGER", lambda i: random.randint(100, 1800000)),
            ],
            "row_count": max(s * 5, 5000000),
            "pk": "behavior_id",
            "db_only": "clickhouse",
        },
        "marketing_campaigns": {
            "columns": [
                ("campaign_id", "INTEGER", lambda i: i + 1),
                ("campaign_name", "VARCHAR(200)", lambda i: f"{random.choice(['双十一', '618', '年货节', '开学季', '女王节', '双十二', '国庆', '元旦'])}营销活动第{random.randint(1,10)}期"),
                ("start_date", "DATE", lambda i: random_date("2024-01-01", "2025-06-30")),
                ("end_date", "DATE", lambda i: random_date("2024-01-15", "2025-07-09")),
                ("budget", "DECIMAL(12,2)", lambda i: round(random.uniform(10000, 5000000), 2)),
                ("actual_cost", "DECIMAL(12,2)", lambda i: round(random.uniform(5000, 5500000), 2)),
                ("channel", "VARCHAR(50)", lambda i: random.choice(["线上-百度", "线上-抖音", "线上-微信", "线下-门店", "线下-地推", "全渠道"])),
                ("target_audience", "VARCHAR(100)", lambda i: random.choice(["年轻女性", "家庭用户", "商务人士", "学生群体", "银发族", "全部用户"])),
                ("conversion_count", "INTEGER", lambda i: random.randint(100, 50000)),
                ("roi", "DECIMAL(5,2)", lambda i: round(random.uniform(0.5, 10), 2)),
            ],
            "row_count": min(s // 200, 5000),
            "pk": "campaign_id",
            "db_only": "mysql",
        },
        "product_reviews": {
            "columns": [
                ("review_id", "INTEGER", lambda i: i + 1),
                ("product_id", "INTEGER", lambda i: random.randint(1, 50000)),
                ("customer_id", "INTEGER", lambda i: random.randint(1, 200000)),
                ("rating", "INTEGER", lambda i: random.choices([1, 2, 3, 4, 5], weights=[2, 5, 15, 35, 43])[0]),
                ("review_text", "VARCHAR(1000)", lambda i: _gen_review_text()),
                ("review_date", "DATE", lambda i: random_date("2024-01-01", "2025-07-09")),
                ("useful_count", "INTEGER", lambda i: random.randint(0, 500)),
                ("has_images", "BOOLEAN", lambda i: random.random() > 0.8),
            ],
            "row_count": max(s // 5, 200000),
            "pk": "review_id",
            "db_only": "mysql",
        },
        "sales_targets": {
            "columns": [
                ("target_id", "INTEGER", lambda i: i + 1),
                ("store_id", "INTEGER", lambda i: random.randint(1, 2000)),
                ("year", "INTEGER", lambda i: random.choice([2024, 2025])),
                ("month", "INTEGER", lambda i: random.randint(1, 12)),
                ("target_amount", "DECIMAL(15,2)", lambda i: round(random.uniform(50000, 5000000), 2)),
                ("actual_amount", "DECIMAL(15,2)", lambda i: round(random.uniform(30000, 6000000), 2)),
                ("completion_rate", "DECIMAL(5,4)", lambda i: round(random.uniform(0.5, 1.5), 4)),
                ("updated_at", "DATE", lambda i: random_date("2024-01-01", "2025-07-09")),
            ],
            "row_count": min(s // 50, 20000),
            "pk": "target_id",
            "db_only": "mysql",
        },
        "financial_reports": {
            "columns": [
                ("report_id", "INTEGER", lambda i: i + 1),
                ("report_type", "VARCHAR(50)", lambda i: random.choice(["月度损益表", "季度资产负债表", "年度现金流量表", "部门费用明细", "项目成本核算"])),
                ("period_start", "DATE", lambda i: random_date("2024-01-01", "2025-06-30")),
                ("period_end", "DATE", lambda i: random_date("2024-02-01", "2025-07-09")),
                ("revenue", "DECIMAL(18,2)", lambda i: round(random.uniform(100000, 50000000), 2)),
                ("cost", "DECIMAL(18,2)", lambda i: round(random.uniform(50000, 30000000), 2)),
                ("expenses", "DECIMAL(18,2)", lambda i: round(random.uniform(10000, 5000000), 2)),
                ("created_by", "VARCHAR(50)", lambda i: random_chinese_name()),
                ("gross_profit", "DECIMAL(18,2)", lambda i: 0),
                ("net_profit", "DECIMAL(18,2)", lambda i: 0),
            ],
            "row_count": min(s // 200, 5000),
            "pk": "report_id",
            "db_only": "postgres",
            "post_process": lambda row: {
                **row,
                "gross_profit": round(row["revenue"] - row["cost"], 2),
                "net_profit": round(row["revenue"] - row["cost"] - row["expenses"], 2),
            },
        },
        "budget_plans": {
            "columns": [
                ("budget_id", "INTEGER", lambda i: i + 1),
                ("department", "VARCHAR(50)", lambda i: random.choice(["销售部", "技术部", "市场部", "运营部", "人事部", "财务部"])),
                ("year", "INTEGER", lambda i: random.choice([2024, 2025])),
                ("budget_amount", "DECIMAL(15,2)", lambda i: round(random.uniform(50000, 10000000), 2)),
                ("spent_amount", "DECIMAL(15,2)", lambda i: round(random.uniform(0, 12000000), 2)),
                ("category", "VARCHAR(50)", lambda i: random.choice(["人力成本", "设备采购", "差旅费用", "培训费用", "办公支出", "营销费用"])),
                ("approver", "VARCHAR(50)", lambda i: random_chinese_name()),
                ("status", "VARCHAR(20)", lambda i: random.choice(["待审批", "已批准", "已执行", "已超额", "已关闭"])),
            ],
            "row_count": min(s // 500, 2000),
            "pk": "budget_id",
            "db_only": "postgres",
        },
        "hr_records": {
            "columns": [
                ("record_id", "INTEGER", lambda i: i + 1),
                ("employee_id", "INTEGER", lambda i: random.randint(1, 5000)),
                ("record_type", "VARCHAR(30)", lambda i: random.choice(["入职", "转正", "调岗", "晋升", "离职", "薪资调整", "培训记录"])),
                ("record_date", "DATE", lambda i: random_date("2022-01-01", "2025-07-09")),
                ("previous_value", "VARCHAR(200)", lambda i: random.choice(["初级工程师", "主管", "8000", "销售部", "", ""])),
                ("new_value", "VARCHAR(200)", lambda i: random.choice(["高级工程师", "经理", "12000", "技术部", "", ""])),
                ("reason", "VARCHAR(500)", lambda i: random.choice(["年度晋升", "组织架构调整", "个人发展", "薪资对标", "人才引进"])),
                ("approved_by", "VARCHAR(50)", lambda i: random_chinese_name()),
            ],
            "row_count": min(s // 100, 10000),
            "pk": "record_id",
            "db_only": "oracle",
        },
        "contracts": {
            "columns": [
                ("contract_id", "INTEGER", lambda i: i + 1),
                ("contract_name", "VARCHAR(200)", lambda i: f"{random.choice(['采购', '销售', '服务', '代理', '加盟'])}合同-{random.randint(2024,2025)}年第{random.randint(1,999)}号"),
                ("party_a", "VARCHAR(200)", lambda i: _gen_company_name()),
                ("party_b", "VARCHAR(200)", lambda i: _gen_company_name()),
                ("contract_amount", "DECIMAL(15,2)", lambda i: round(random.uniform(10000, 10000000), 2)),
                ("sign_date", "DATE", lambda i: random_date("2023-01-01", "2025-07-09")),
                ("start_date", "DATE", lambda i: random_date("2023-01-15", "2025-07-09")),
                ("end_date", "DATE", lambda i: random_date("2025-08-01", "2028-12-31")),
                ("status", "VARCHAR(20)", lambda i: random.choice(["履行中", "已完成", "已终止", "续约中"])),
                ("payment_terms", "VARCHAR(100)", lambda i: random.choice(["月结30天", "季结", "预付50%", "验收后付款", "分期付款"])),
            ],
            "row_count": min(s // 200, 5000),
            "pk": "contract_id",
            "db_only": "oracle",
        },
        "shipping_records": {
            "columns": [
                ("shipping_id", "INTEGER", lambda i: i + 1),
                ("order_id", "INTEGER", lambda i: random.randint(1, 500000)),
                ("carrier", "VARCHAR(50)", lambda i: random.choice(SHIPPING_COMPANIES)),
                ("tracking_number", "VARCHAR(50)", lambda i: f"SF{random.randint(10000000000,99999999999)}"),
                ("ship_date", "DATE", lambda i: random_date("2024-01-01", "2025-07-09")),
                ("delivery_date", "DATE", lambda i: random_date("2024-01-02", "2025-07-09")),
                ("origin_city", "VARCHAR(50)", lambda i: random_city()[1]),
                ("dest_city", "VARCHAR(50)", lambda i: random_city()[1]),
                ("weight_kg", "DECIMAL(8,3)", lambda i: round(random.uniform(0.1, 100), 3)),
                ("shipping_cost", "DECIMAL(10,2)", lambda i: round(random.uniform(5, 500), 2)),
                ("status", "VARCHAR(20)", lambda i: random.choice(["运输中", "已签收", "派送中", "已揽收", "异常"])),
            ],
            "row_count": max(s, 500000),
            "pk": "shipping_id",
            "db_only": "mssql",
        },
        "customer_tickets": {
            "columns": [
                ("ticket_id", "INTEGER", lambda i: i + 1),
                ("customer_id", "INTEGER", lambda i: random.randint(1, 200000)),
                ("ticket_type", "VARCHAR(30)", lambda i: random.choice(TICKET_TYPES)),
                ("status", "VARCHAR(20)", lambda i: random.choice(TICKET_STATUSES)),
                ("priority", "VARCHAR(10)", lambda i: random.choice(["低", "中", "高", "紧急"])),
                ("created_date", "DATE", lambda i: random_date("2024-01-01", "2025-07-09")),
                ("resolved_date", "DATE", lambda i: random_date("2024-01-02", "2025-07-09")),
                ("assigned_to", "VARCHAR(50)", lambda i: random_chinese_name()),
                ("satisfaction_score", "INTEGER", lambda i: random.choices([1, 2, 3, 4, 5], weights=[3, 5, 10, 40, 42])[0]),
                ("description", "VARCHAR(1000)", lambda i: _gen_ticket_desc()),
            ],
            "row_count": max(s // 10, 100000),
            "pk": "ticket_id",
            "db_only": "mssql",
        },
    }


# ================================================================
# DDL生成 — 为每种方言生成建表语句
# ================================================================

def generate_ddl(schema: dict, table_name: str, dialect: str) -> str:
    """为指定方言生成CREATE TABLE语句。"""
    q1, q2 = DIALECT_QUOTES.get(dialect, ('"', '"'))
    cols = schema["columns"]
    pk = schema.get("pk", "")
    col_defs = []

    for col_name, col_type, _ in cols:
        dt = col_type
        if dialect == "clickhouse":
            if "BOOLEAN" in dt:
                dt = "UInt8"
            elif "VARCHAR" in dt:
                dt = "String"
            elif "INTEGER" in dt:
                dt = "Int64"
            elif "DECIMAL" in dt:
                dt = dt.replace("DECIMAL", "Decimal")
            elif "TIME" in dt or "DATE" in dt:
                dt = "String" if "VARCHAR" in col_type else ("Date" if "DATE" in dt else "String")
        elif dialect == "oracle":
            if "BOOLEAN" in dt:
                dt = "NUMBER(1)"
            elif "VARCHAR" in dt:
                dt = dt.replace("VARCHAR", "VARCHAR2")
            elif "INTEGER" in dt:
                dt = "NUMBER(10)"
            elif "DECIMAL" in dt:
                dt = dt.replace("DECIMAL", "NUMBER")
            elif "TIME" in dt:
                dt = "VARCHAR2(8)"
        elif dialect == "mssql":
            if "BOOLEAN" in dt:
                dt = "BIT"

        col_defs.append(f"    {q1}{col_name}{q2} {dt}")

    if pk:
        col_defs.append(f"    PRIMARY KEY ({q1}{pk}{q2})")

    engine = ""
    if dialect == "clickhouse":
        has_order_date = any(c[0] == "order_date" for c in cols)
        if has_order_date:
            engine = f"\nENGINE = MergeTree()\nORDER BY ({q1}{pk}{q2})\nPARTITION BY toYYYYMM({q1}order_date{q2})"
        else:
            engine = f"\nENGINE = MergeTree()\nORDER BY ({q1}{pk}{q2})"
    elif dialect == "mysql":
        engine = "\nENGINE = InnoDB DEFAULT CHARSET = utf8mb4"

    return f"CREATE TABLE IF NOT EXISTS {q1}{table_name}{q2} (\n" + ",\n".join(col_defs) + f"\n){engine};\n"


# ================================================================
# 数据写入器 — 批量写入
# ================================================================

class DataWriter:
    """批量写入数据到文件，支持SQL/CSV/JSONL格式。"""

    def __init__(self, output_dir: Path, table_name: str, dialect: str, fmt: str):
        self.table_name = table_name
        self.fmt = fmt
        self.batch_size = 5000
        self.buffer: list[dict] = []
        db_dir = output_dir / dialect
        db_dir.mkdir(parents=True, exist_ok=True)

        if fmt == "sql":
            self.file = gzip.open(db_dir / f"{table_name}.sql.gz", "wt", encoding="utf-8")
            self.file.write(f"-- 测试数据: {table_name} — 数据库: {dialect}\n")
            q1, q2 = DIALECT_QUOTES.get(dialect, ('"', '"'))
            schema = build_schemas(1)[table_name]
            col_names = [f"{q1}{c[0]}{q2}" for c in schema["columns"]]
            self.insert_prefix = f"INSERT INTO {q1}{table_name}{q2} ({', '.join(col_names)}) VALUES\n"
        elif fmt == "csv":
            self.file = gzip.open(db_dir / f"{table_name}.csv.gz", "wt", encoding="utf-8", newline="")
            self.writer = csv.writer(self.file)
            schema = build_schemas(1)[table_name]
            self.writer.writerow([c[0] for c in schema["columns"]])
        elif fmt == "jsonl":
            self.file = gzip.open(db_dir / f"{table_name}.jsonl.gz", "wt", encoding="utf-8")

    def add(self, row: dict):
        """添加一行到缓冲区，满了自动flush。"""
        self.buffer.append(row)
        if len(self.buffer) >= self.batch_size:
            self._flush()

    def _flush(self):
        """将缓冲区写入文件。"""
        if not self.buffer:
            return
        if self.fmt == "sql":
            rows = [f"({', '.join(esc(v) for v in row.values())})" for row in self.buffer]
            self.file.write(self.insert_prefix + ",\n".join(rows) + ";\n")
        elif self.fmt == "csv":
            for row in self.buffer:
                self.writer.writerow(list(row.values()))
        elif self.fmt == "jsonl":
            for row in self.buffer:
                self.file.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.buffer.clear()

    def close(self):
        """关闭文件，写入剩余数据。"""
        self._flush()
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ================================================================
# 主生成逻辑
# ================================================================

def generate_table(table_name: str, schema: dict, dialect: str, output_dir: Path, fmt: str) -> int:
    """生成单张表的数据，返回行数。"""
    row_count = schema["row_count"]
    db_only = schema.get("db_only", "")
    if db_only and dialect != db_only:
        return 0

    columns = schema["columns"]
    post_process = schema.get("post_process")

    print(f"  [{dialect}] {table_name}: {row_count:,} 行...", end=" ", flush=True)
    start = time.time()

    with DataWriter(output_dir, table_name, dialect, fmt) as writer:
        for i in range(row_count):
            row = {col[0]: col[2](i) for col in columns}
            if post_process:
                row = post_process(row)
            writer.add(row)
            if (i + 1) % 500000 == 0:
                print(f"\n    ... {i + 1:,}/{row_count:,}", end=" ", flush=True)

    elapsed = time.time() - start
    print(f"完成 ({elapsed:.1f}s)")
    return row_count


def main():
    parser = argparse.ArgumentParser(description="全流程测试数据生成脚本")
    parser.add_argument("--db", default="all",
                        choices=["all", "clickhouse", "mysql", "postgres", "oracle", "mssql"])
    parser.add_argument("--scale", type=int, default=1000000, help="数据量缩放因子")
    parser.add_argument("--table", default="", help="仅生成指定表")
    parser.add_argument("--format", default="sql", choices=["sql", "csv", "jsonl"])
    parser.add_argument("--output-dir", default="data/generated")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--ddl-only", action="store_true", help="仅生成DDL，不生成数据")

    args = parser.parse_args()

    random.seed(args.seed)
    Faker.seed(args.seed)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    databases = ALL_DATABASES if args.db == "all" else [args.db]
    schemas = build_schemas(args.scale)

    if args.table:
        if args.table not in schemas:
            print(f"错误: 表 '{args.table}' 不存在。可用: {', '.join(schemas.keys())}")
            sys.exit(1)
        schemas = {args.table: schemas[args.table]}

    if args.ddl_only:
        print("=" * 60)
        print("生成 DDL（建表语句）")
        print("=" * 60)
        ddl_dir = output_dir / "ddl"
        ddl_dir.mkdir(parents=True, exist_ok=True)
        for db in databases:
            db_ddl = ""
            for table_name, schema in schemas.items():
                if schema.get("db_only", "") and db != schema["db_only"]:
                    continue
                db_ddl += f"-- 表: {table_name}\n"
                db_ddl += generate_ddl(schema, table_name, db) + "\n"
            fpath = ddl_dir / f"{db}_ddl.sql"
            fpath.write_text(db_ddl, encoding="utf-8")
            print(f"  {db}: {fpath} ({len(db_ddl)} bytes)")
        return

    print("=" * 60)
    print("Data Analysis Agent — 测试数据生成器")
    print(f"缩放: {args.scale:,} | 格式: {args.format} | 数据库: {args.db}")
    print(f"输出: {output_dir.absolute()}")
    print("=" * 60)

    total_start = time.time()
    totals = {}

    for db_idx, db in enumerate(databases):
        db_seed = args.seed + db_idx * 1000
        random.seed(db_seed)
        print(f"\n{'='*60}")
        print(f"数据库: {db} (seed={db_seed})")
        print(f"{'='*60}")

        db_total = sum(
            generate_table(tn, sc, db, output_dir, args.format)
            for tn, sc in schemas.items()
            if not sc.get("db_only", "") or sc["db_only"] == db
        )
        totals[db] = db_total
        print(f"  {db} 总计: {db_total:,} 行")

    total_elapsed = time.time() - total_start
    grand_total = sum(totals.values())

    print(f"\n{'='*60}")
    print("生成完成摘要")
    print(f"{'='*60}")
    for db, count in totals.items():
        print(f"  {db:15s}: {count:>15,} 行")
    print(f"  {'─' * 32}")
    print(f"  {'总计':15s}: {grand_total:>15,} 行")
    print(f"  耗时: {total_elapsed:.1f}s ({total_elapsed/60:.1f}min)")

    meta = {
        "generated_at": datetime.now().isoformat(),
        "scale": args.scale,
        "format": args.format,
        "databases": databases,
        "tables": {k: {"row_count": v["row_count"], "db_only": v.get("db_only", "")}
                    for k, v in schemas.items()},
        "row_totals": totals,
        "grand_total": grand_total,
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n元数据: {output_dir / 'metadata.json'}")


if __name__ == "__main__":
    main()
