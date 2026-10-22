import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB = Path(os.getenv('SHOPMATE_DB', str(Path(__file__).resolve().parents[1] / 'data/shopmate.db')))

@contextmanager
def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def initialize():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, owner TEXT NOT NULL, product TEXT, cents INTEGER, status TEXT, logistics TEXT);
        CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY, title TEXT UNIQUE, content TEXT);
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, owner TEXT NOT NULL, order_id TEXT, intent TEXT);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, session_id TEXT REFERENCES sessions(id), role TEXT, content TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS refunds(id INTEGER PRIMARY KEY, order_id TEXT UNIQUE REFERENCES orders(id), owner TEXT, reason TEXT, status TEXT DEFAULT 'pending', note TEXT DEFAULT '', created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor TEXT, action TEXT, detail TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS products(sku TEXT PRIMARY KEY, name TEXT NOT NULL, price_cents INTEGER NOT NULL, stock INTEGER NOT NULL, category TEXT NOT NULL, status TEXT NOT NULL DEFAULT '在售');
        CREATE TABLE IF NOT EXISTS ops_orders(id TEXT PRIMARY KEY, product_sku TEXT NOT NULL REFERENCES products(sku), quantity INTEGER NOT NULL, amount_cents INTEGER NOT NULL, status TEXT NOT NULL, buyer TEXT NOT NULL, address TEXT NOT NULL, tracking TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS aftersales(id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES ops_orders(id), kind TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS op_runs(id TEXT PRIMARY KEY, owner TEXT NOT NULL, intent TEXT NOT NULL, plan_json TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}', created TEXT DEFAULT CURRENT_TIMESTAMP, expires REAL NOT NULL);
        ''')
        db.executemany('INSERT OR IGNORE INTO orders VALUES(?,?,?,?,?,?)', [
            ('SM1001','buyer','云感降噪耳机 Pro',39900,'运输中','顺丰模拟运单 SF-DEMO-1001；今日 09:20 已到达上海转运中心，预计明日送达。'),
            ('SM1002','buyer','轻行保温杯 500ml',12900,'待发货','仓库正在打包，预计 24 小时内交给承运商。'),
            ('SM1003','buyer','桌面机械键盘 Air',26900,'已签收','昨日 14:30 已签收。'),
            ('SM2001','other','云感降噪耳机 Pro',39900,'运输中','其他顾客的私有订单。')])
        db.executemany('INSERT OR IGNORE INTO documents(title,content) VALUES(?,?)', [
            ('退换货政策','普通商品签收后 7 天内可申请无理由退货，需商品完好、配件和包装齐全。质量问题由商家承担退货运费；非质量问题由买家承担。退款须经人工审核；批准不代表款项已到账。定制商品不适用无理由退货。'),
            ('发货与配送','现货订单付款后通常 48 小时内发货，节假日以公告为准。默认顺丰配送，中国大陆普通地区满 99 元包邮，否则运费 8 元。物流具体进度请查询订单。'),
            ('云感降噪耳机 Pro · 产品说明','云感降噪耳机 Pro 售价 399 元，蓝牙 5.3，支持主动降噪和通透模式。单次续航约 8 小时，搭配充电盒约 32 小时，USB-C 充电。支持 IPX4 防泼溅，不支持游泳佩戴。质保 1 年。'),
            ('轻行保温杯 · 产品说明','轻行保温杯售价 129 元，容量 500ml，316L 不锈钢内胆，保温约 6 小时。不可放入微波炉，建议手洗。提供雾白、墨绿两种颜色。'),
            ('桌面机械键盘 Air · 产品说明','桌面机械键盘 Air 售价 269 元，84 键布局，支持蓝牙与 USB-C 有线连接，兼容 Windows 和 macOS。采用静音红轴，支持热插拔，质保 1 年。'),
            ('发票与售后','订单完成后可联系人工申请电子普通发票，需要提供抬头与税号。售后人工服务时间为工作日 09:00–18:00。当前演示系统不收集真实发票和身份信息。')])
        db.executemany('INSERT OR IGNORE INTO products(sku,name,price_cents,stock,category,status) VALUES(?,?,?,?,?,?)', [
            ('SKU1001','云感降噪耳机 Pro',39900,46,'数码影音','在售'),
            ('SKU1002','轻行保温杯 500ml',12900,128,'家居生活','在售'),
            ('SKU1003','桌面机械键盘 Air',26900,12,'数码影音','在售'),
            ('SKU1004','晨雾香氛礼盒',18900,0,'居家香氛','库存不足'),
            ('SKU1005','棉麻收纳托特包',7900,73,'服饰箱包','在售')])
        db.executemany('INSERT OR IGNORE INTO ops_orders(id,product_sku,quantity,amount_cents,status,buyer,address,tracking,created) VALUES(?,?,?,?,?,?,?,?,?)', [
            ('DD20261021001','SKU1001',1,39900,'待发货','林女士','浙江省杭州市余杭区','', '2026-10-21 09:24'),
            ('DD20261021002','SKU1002',2,25800,'待发货','陈先生','江苏省苏州市工业园区','', '2026-10-21 10:06'),
            ('DD20261021003','SKU1005',1,7900,'已发货','周女士','广东省深圳市南山区','SF-DEMO-2103','2026-10-21 10:32'),
            ('DD20261020098','SKU1003',1,26900,'已签收','赵先生','上海市浦东新区','YT-DEMO-2098','2026-10-20 16:40'),
            ('DD20261021004','SKU1004',1,18900,'待发货','黄女士','北京市朝阳区','', '2026-10-21 11:18'),
            ('DD20261021005','SKU1001',1,39900,'售后处理中','吴先生','四川省成都市武侯区','SF-DEMO-2105','2026-10-21 11:46')])
        db.executemany('INSERT OR IGNORE INTO aftersales(id,order_id,kind,reason,status,created) VALUES(?,?,?,?,?,?)', [
            ('AS1001','DD20261021005','退货退款','佩戴后耳机有杂音，申请退货','待审核','2026-10-21 12:03'),
            ('AS1002','DD20261020098','仅退款','外包装破损，顾客上传了照片','待补充材料','2026-10-21 13:21')])

def rows(sql, args=()):
    with connect() as db:
        return [dict(x) for x in db.execute(sql, args).fetchall()]
