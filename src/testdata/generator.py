"""确定性随机访问与分批造数，内存不随事实表总行数增长。"""

import math
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any, Self
from zlib import crc32

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.testdata.catalog import TableSpec, build_catalog

REGIONS = ("CN-BJ", "CN-SH", "CN-GD", "CN-ZJ", "CN-JS", "CN-SC", "CN-HB", "CN-SD")
CITIES = ("北京市", "上海市", "广州市", "杭州市", "南京市", "成都市", "武汉市", "济南市")
CHANNELS = ("官网", "移动应用", "线下门店", "合作伙伴", "社交渠道")
MONEY_SCALE = Decimal(100)


# 提供默认自然年，便于配置校验和测试固定时间。
# Args: 无。
# Returns: 本机当前年份。
def _current_year() -> int:
    return date.today().year


NAME_SAMPLES = {
    "category_name": (
        "数码家电",
        "家居日用",
        "服饰鞋包",
        "生鲜食品",
        "运动户外",
        "图书文创",
        "母婴玩具",
        "美妆个护",
    ),
    "product_name": (
        "降噪无线耳机",
        "不锈钢保温杯",
        "轻量通勤背包",
        "全麦早餐面包",
        "智能空气净化器",
        "儿童益智积木",
        "防晒运动外套",
        "山地露营帐篷",
    ),
    "brand_name": ("星河", "青禾", "云栖", "山岚", "锦程", "知木"),
    "supplier_name": (
        "华东智造有限公司",
        "岭南食品供应有限公司",
        "川蜀织造有限公司",
        "江南数字设备有限公司",
    ),
    "store_name": ("朝阳旗舰店", "滨江生活店", "天河社区店", "浦东体验店", "高新便利店"),
    "department_name": (
        "研发中心",
        "零售事业部",
        "供应链中心",
        "财务管理部",
        "客户服务部",
        "市场营销部",
    ),
    "position_name": ("数据分析师", "采购专员", "物流主管", "门店经理", "产品工程师", "客户顾问"),
    "project_name": ("智慧门店升级", "仓储自动化", "客户体验提升", "绿色运输试点", "财务共享建设"),
    "course_name": ("数据分析入门", "供应链协同", "客户沟通技巧", "安全生产培训", "财务合规基础"),
}


class GenerationConfig(BaseModel):
    """控制每源行数、时间窗口、种子及单批内存。"""

    model_config = ConfigDict(frozen=True)
    scale: int = Field(default=1_000_000, ge=100)
    anchor_year: int = Field(default_factory=_current_year, ge=1930, le=2269)
    years_before: int = Field(default=10, ge=0, le=30)
    years_after: int = Field(default=10, ge=0, le=30)
    seed: int = Field(default=42, ge=0, le=2**32 - 1)
    batch_size: int = Field(default=5000, ge=1, le=100_000)

    # 保证日期处于所有目标数据库共同支持的范围。
    # Args: self - 当前配置。
    # Returns: 校验后的当前配置。
    @model_validator(mode="after")
    def validate_window(self) -> Self:
        """防止 ClickHouse Date32 等日期类型溢出。"""
        if self.start_date.year < 1900 or self.end_date.year > 2299:
            raise ValueError("模拟日期必须处于 1900—2299 年")
        return self

    # 返回包含首年的完整自然年边界。
    # Args: self - 当前配置。
    # Returns: 首日。
    @property
    def start_date(self) -> date:
        """时间窗口起点。"""
        return date(self.anchor_year - self.years_before, 1, 1)

    # 返回包含末年的完整自然年边界。
    # Args: self - 当前配置。
    # Returns: 末日。
    @property
    def end_date(self) -> date:
        """时间窗口终点。"""
        return date(self.anchor_year + self.years_after, 12, 31)


# 为五个完整域分配规模，确保分源运行仍使用相同的主键空间。
# Args: config - 规模和时间配置。
# Returns: 所有表的实际目标行数。
def row_counts(config: GenerationConfig) -> dict[str, int]:
    """旧池与扩展池各至少 scale 行，保持已导入旧主键空间不变。"""
    catalog = {name: table for name, table in build_catalog().items() if not table.subject}
    weights: dict[str, int] = {}
    for table in catalog.values():
        weights[table.source] = weights.get(table.source, 0) + table.weight
    counts = {
        name: max(2, math.ceil(config.scale * table.weight / weights[table.source]))
        for name, table in catalog.items()
    }
    counts["retail_calendar"] = (config.end_date - config.start_date).days + 1
    counts["retail_order_items"] = counts["retail_orders"] * 3
    counts["finance_invoices"] = counts["retail_orders"]
    counts["supply_shipments"] = counts["retail_orders"]
    for source in weights:
        names = [name for name, table in catalog.items() if table.source == source]
        deficit = config.scale - sum(counts[name] for name in names)
        if deficit > 0:
            candidates = [
                name
                for name in names
                if name
                not in {
                    "retail_calendar",
                    "retail_orders",
                    "retail_order_items",
                    "finance_invoices",
                    "supply_shipments",
                }
            ]
            counts[max(candidates, key=counts.__getitem__)] += deficit
    expanded = {name: table for name, table in build_catalog().items() if table.subject}
    expanded_weights = {
        source: sum(table.weight for table in expanded.values() if table.source == source)
        for source in weights
    }
    counts.update(
        {
            name: max(2, math.ceil(config.scale * table.weight / expanded_weights[table.source]))
            for name, table in expanded.items()
        }
    )
    return counts


# 使用稳定整数散列，不依赖进程随机哈希或全局 random 状态。
# Args: value - 输入整数。
# Returns: 无符号确定性整数。
def _mix(value: int) -> int:
    value = (value ^ (value >> 16)) * 0x45D9F3B & 0xFFFFFFFF
    value = (value ^ (value >> 16)) * 0x45D9F3B & 0xFFFFFFFF
    return value ^ (value >> 16)


# 缓存小型固定目录的盐值，不缓存事实行。
# Args: name - 表名。
# Returns: 稳定表盐值。
@lru_cache(maxsize=256)
def _salt(name: str) -> int:
    return crc32(name.encode("utf-8"))


# 将单调行号映射到父表空间，并覆盖首末主键。
# Args: row_id - 子表行号；count - 子表大小；parent_count - 父表大小。
# Returns: 合法父表主键。
def _parent_id(row_id: int, count: int, parent_count: int) -> int:
    return 1 + (row_id - 1) * (parent_count - 1) // max(1, count - 1)


# 按日、季节、趋势与特殊订单构造可精确对账的金额。
# Args: key - 业务键；event_date - 日期；config - 生成配置。
# Returns: 含金额与状态的业务上下文。
def _amounts(key: int, event_date: date, config: GenerationConfig) -> dict[str, Any]:
    random_value = _mix(key + config.seed)
    trend = 100 + (event_date.year - config.start_date.year) * 3
    seasonal = (90, 85, 95, 100, 110, 130, 105, 100, 115, 125, 160, 140)[event_date.month - 1]
    weekend = 115 if event_date.weekday() >= 5 else 100
    gross_cents = (2000 + random_value % 98000) * trend * seasonal * weekend // 1_000_000
    if key % 101 == 0:
        gross_cents *= 30
    discount_cents = gross_cents * (5 + random_value % 16) // 100
    net_cents = gross_cents - discount_cents
    status = "已完成"
    if key % 31 == 0:
        net_cents = 0
        status = "已取消"
    elif key % 17 == 0:
        net_cents = -net_cents
        status = "已退款"
    return {
        "gross": Decimal(gross_cents) / MONEY_SCALE,
        "discount": Decimal(discount_cents) / MONEY_SCALE,
        "net": Decimal(net_cents) / MONEY_SCALE,
        "cost": Decimal(gross_cents * 60 // 100) / MONEY_SCALE,
        "price": Decimal(gross_cents // 3) / MONEY_SCALE,
        "tax": Decimal(gross_cents * 13 // 100) / MONEY_SCALE,
        "refund": -Decimal(abs(net_cents)) / MONEY_SCALE,
        "order_status": status,
        "net_cents": net_cents,
    }


# 按主键构造单行，核心订单的跨库快照共享业务上下文。
# Args: table - 表规格；row_id - 一基主键；config - 生成配置；counts - 全目录计数。
# Returns: 类型稳定、可直接批量写入的字段字典。
def make_row(
    table: TableSpec, row_id: int, config: GenerationConfig, counts: dict[str, int]
) -> dict[str, Any]:
    """随机访问一行，不需要先生成其父表。"""
    count = counts[table.name]
    if not 1 <= row_id <= count:
        raise ValueError(f"{table.name} 主键超出范围")
    order_linked = table.name == "retail_orders" or any(
        column.reference == "retail_orders" for column in table.columns
    )
    order_id = row_id
    if table.name == "retail_order_items":
        order_id = (row_id - 1) // 3 + 1
    elif table.name != "retail_orders" and order_linked:
        order_id = _parent_id(row_id, count, counts["retail_orders"])
    time_key = order_id if order_linked else row_id
    time_count = counts["retail_orders"] if order_linked else count
    day_offset = (
        (time_key - 1) * (config.end_date - config.start_date).days // max(1, time_count - 1)
    )
    event_date = config.start_date + timedelta(days=day_offset)
    business_key = order_id if order_linked else row_id + _salt(table.name)
    random_value = _mix(business_key + config.seed)
    values = _amounts(business_key, event_date, config)
    region_index = random_value % len(REGIONS)
    scenario = (
        "历史模拟"
        if event_date.year < config.anchor_year
        else ("未来情景" if event_date.year > config.anchor_year else "当年模拟")
    )
    row: dict[str, Any] = {}
    for column in table.columns:
        recipe = column.recipe
        column_random = _mix(random_value + _salt(column.name))
        if column.reference:
            if column.reference == "retail_orders":
                value = order_id
            elif order_linked and column.reference == "retail_customers":
                value = 1 + _mix(order_id + config.seed + 71) % counts[column.reference]
            else:
                value = 1 + column_random % counts[column.reference]
        elif recipe in values:
            value = values[recipe]
        elif recipe.startswith("enum."):
            choices = recipe[5:].split("|")
            value = choices[column_random % len(choices)]
        elif recipe == "pk":
            value = row_id
        elif recipe == "date":
            value = event_date
        elif recipe == "month":
            value = event_date.strftime("%Y-%m")
        elif recipe == "scenario":
            value = scenario
        elif recipe == "region":
            value = REGIONS[region_index]
        elif recipe == "city":
            value = CITIES[region_index]
        elif recipe == "channel":
            value = CHANNELS[(random_value // 8) % len(CHANNELS)]
        elif recipe == "currency":
            value = ("CNY", "USD", "EUR", "JPY", "GBP")[column_random % 5]
        elif recipe == "label":
            samples = NAME_SAMPLES.get(column.name)
            if column.name in {"customer_name", "employee_name", "candidate_name"}:
                surname = ("赵", "钱", "孙", "李", "周", "吴", "郑", "王")[column_random % 8]
                given = ("明轩", "雨桐", "子涵", "晓晨", "思远", "若宁")[(column_random // 8) % 6]
                label = surname + given
            else:
                label = (
                    samples[column_random % len(samples)]
                    if samples
                    else table.description.split("，")[0]
                )
            value = f"模拟{label}-{row_id:08d}"
        elif recipe == "notes":
            value = (
                None
                if row_id % 13 == 0
                else ("模拟边界：O'Reilly，逗号,换行\n测试" if row_id % 19 == 0 else "合成测试数据")
            )
        elif recipe == "line":
            base_cents, remainder = divmod(values["net_cents"], 3)
            value = Decimal(base_cents + (remainder if row_id % 3 == 0 else 0)) / MONEY_SCALE
        elif recipe == "quantity":
            value = 1 + column_random % 8
        elif recipe == "rating":
            value = 1 + column_random % 5
        elif recipe == "flag":
            value = int(column_random % 10 < 7)
        elif recipe == "int":
            value = column_random % 90
        elif recipe == "signed":
            value = (column_random % 201) - 100
        elif recipe == "hours":
            value = column_random % 9
        elif recipe == "ratio":
            value = Decimal(column_random % 101) / MONEY_SCALE
        elif recipe == "delta":
            value = Decimal(column_random % 20001 - 10000) / MONEY_SCALE
        elif recipe == "temperature":
            value = Decimal((event_date.month * 37 + region_index * 11) % 550 - 100) / 10
        elif recipe == "rain":
            value = Decimal(column_random % 2000) / MONEY_SCALE
        elif recipe in {"money", "metric"}:
            value = Decimal(column_random % 1_000_000) / MONEY_SCALE
        else:
            raise ValueError(f"未知生成规则: {recipe}")
        row[column.name] = value
    if table.name == "retail_calendar":
        row.update(
            fiscal_year=event_date.year,
            quarter=(event_date.month - 1) // 3 + 1,
            is_weekend=int(event_date.weekday() >= 5),
        )
    if table.name == "retail_order_items":
        row.update(quantity=1, unit_price=abs(row["line_amount"]))
    return row


# 逐批生成，避免旧脚本将整张事实表驻留内存。
# Args: table - 表规格；config - 生成配置。
# Returns: 每批最多 batch_size 行的迭代器。
def iter_batches(table: TableSpec, config: GenerationConfig) -> Iterator[list[dict[str, Any]]]:
    """流式输出，可被文件与数据库写入共用。"""
    counts = row_counts(config)
    for start in range(1, counts[table.name] + 1, config.batch_size):
        yield [
            make_row(table, row_id, config, counts)
            for row_id in range(start, min(start + config.batch_size, counts[table.name] + 1))
        ]
