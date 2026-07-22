"""
生成电商测试数据 SQL 文件
支持 MySQL 8.0, PostgreSQL 16, Oracle 21c, SQL Server 2019
输出独立文件，直接执行即可插入数据
"""
import random
import datetime
from faker import Faker

from src.logging_config import get_logger


logger = get_logger(__name__)

fake = Faker('zh_CN')

# ============ 配置参数，可按需修改 ============
USER_COUNT = 5000            # 用户数
CATEGORY_COUNT = 10          # 分类数
PRODUCT_COUNT = 200          # 商品数
ORDER_COUNT = 50000          # 订单数
LOG_COUNT = 2000             # 等级变更记录数

START_DATE = datetime.date(2024, 7, 1)
END_DATE   = datetime.date(2028, 6, 30)

OUTPUT_FILE = 'test_data.sql'  # 输出文件名
# =============================================

# 方法作用：生成给定日期闭区间内的随机日期。
# Args: start - 起始日期；end - 结束日期。
# Returns: 闭区间内的随机日期。
def random_date(start, end):
    """生成 start~end 之间的随机日期"""
    logger.debug("生成随机日期入口", start=str(start), end=str(end))
    delta = end - start
    result = start + datetime.timedelta(days=random.randint(0, delta.days))
    logger.debug("生成随机日期完成", result=str(result))
    return result

# 方法作用：按配置数量生成多数据库兼容的电商测试 SQL 文件。
# Args: 无，使用模块级数量和输出路径配置。
# Returns: 无返回值。
def main():
    logger.debug("生成测试数据入口", output=OUTPUT_FILE)
    random.seed(42)
    fake.seed_instance(42)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        # 关闭外键检查的预处理（各数据库稍后统一加上）
        f.write("-- 关闭外键约束 (MySQL/PostgreSQL/Oracle/SQL Server)\n")
        f.write("-- 如需直接执行，请根据数据库类型取消对应注释\n\n")

        # ------------------ 1. categories ------------------
        categories = []
        f.write("-- 商品分类\n")
        for i in range(1, CATEGORY_COUNT+1):
            name = fake.unique.word().capitalize() + "类"
            categories.append((i, name))
            f.write(f"INSERT INTO categories (category_id, name) VALUES ({i}, '{name}');\n")
        f.write("\n")

        # ------------------ 2. products ------------------
        products = []
        f.write("-- 商品\n")
        for i in range(1, PRODUCT_COUNT+1):
            cat_id = random.randint(1, CATEGORY_COUNT)
            # 价格左偏：10% 高价值 (500-5000)，90% 中低价值 (10-500)
            if random.random() < 0.1:
                price = round(random.uniform(500, 5000), 2)
            else:
                price = round(random.uniform(10, 500), 2)
            name = fake.unique.catch_phrase()
            products.append((i, cat_id, name, price))
            f.write(f"INSERT INTO products (product_id, category_id, name, price) VALUES ({i}, {cat_id}, '{name}', {price});\n")
        f.write("\n")

        # ------------------ 3. users ------------------
        users = []
        f.write("-- 用户\n")
        for i in range(1, USER_COUNT+1):
            name = fake.name()
            # VIP等级分布：0-60%, 1-25%, 2-10%, 3-5%
            r = random.random()
            if r < 0.60:
                vip = 0
            elif r < 0.85:
                vip = 1
            elif r < 0.95:
                vip = 2
            else:
                vip = 3
            reg_date = random_date(START_DATE - datetime.timedelta(days=365), END_DATE)  # 注册可能早于下单时间
            users.append((i, name, vip, reg_date))
            f.write(f"INSERT INTO users (user_id, name, vip_level, register_date) VALUES ({i}, '{name}', {vip}, '{reg_date.isoformat()}');\n")
        f.write("\n")

        # ------------------ 4. orders ------------------
        orders = []
        f.write("-- 订单\n")
        for i in range(1, ORDER_COUNT+1):
            user_id = random.randint(1, USER_COUNT)
            order_date = random_date(START_DATE, END_DATE)
            # 状态分布
            r = random.random()
            if r < 0.85:
                status = 'completed'
            elif r < 0.95:
                status = 'cancelled'
            else:
                status = 'refunded'
            orders.append((i, user_id, order_date, status))
            f.write(f"INSERT INTO orders (order_id, user_id, order_date, status) VALUES ({i}, {user_id}, '{order_date.isoformat()}', '{status}');\n")
        f.write("\n")

        # ------------------ 5. order_items ------------------
        f.write("-- 订单明细\n")
        item_id = 1
        for order in orders:
            order_id, user_id, order_date, status = order
            # 每个订单 1~5 件商品，平均 3 件
            num_items = random.choices([1,2,3,4,5], weights=[10,30,35,20,5], k=1)[0]
            # 防止重复商品（可选）
            product_ids = random.sample(range(1, PRODUCT_COUNT+1), min(num_items, PRODUCT_COUNT))
            for pid in product_ids:
                product = products[pid-1]  # pid 从1开始，products下标从0开始
                unit_price = product[3]    # 使用商品原价作为快照
                quantity = random.randint(1, 5)
                f.write(f"INSERT INTO order_items (item_id, order_id, product_id, quantity, unit_price) VALUES ({item_id}, {order_id}, {pid}, {quantity}, {unit_price});\n")
                item_id += 1
        f.write("\n")

        # ------------------ 6. user_level_log ------------------
        f.write("-- 用户等级变更记录\n")
        for i in range(1, LOG_COUNT+1):
            user_id = random.randint(1, USER_COUNT)
            user = users[user_id-1]
            reg_date = user[3]
            # 变更时间在注册之后，且不超过2026-06-30
            change_date = random_date(reg_date, END_DATE)
            # 新等级必须 >= 旧等级，且大多数升级合理（0->1,1->2,2->3）
            old = random.choices([0,1,2], weights=[70,20,10])[0]  # 多数从0或1升级
            new = random.randint(old+1, 3) if old < 3 else old
            f.write(f"INSERT INTO user_level_log (log_id, user_id, old_level, new_level, change_date) VALUES ({i}, {user_id}, {old}, {new}, '{change_date.isoformat()}');\n")

    logger.info("生成测试数据完成", output=OUTPUT_FILE)

if __name__ == '__main__':
    main()
