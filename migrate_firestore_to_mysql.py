"""
一次性遷移腳本：把 Firestore 舊資料搬到 MySQL

使用方式：
    pip install firebase-admin pymysql python-dotenv --break-system-packages
    python migrate_firestore_to_mysql.py

前置需求：
    1. firebase-adminsdk.json 憑證檔要放在跟這支腳本同一層目錄
    2. .env 要有 MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE
    3. MySQL 那邊的資料表要先用 schema.sql 建好（空表即可）

⚠️ 注意：這支腳本沒有做「重複執行防呆」，expenses / orders / order_items /
settlements 這幾張表沒有天然的唯一鍵可以比對 Firestore 文件 ID，
若不小心跑第二次會造成資料重複。建議只執行一次；如果需要重跑，
請先手動清空對應資料表（TRUNCATE）再執行。
"""

import os
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, firestore
import pymysql
from pymysql.cursors import DictCursor
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# 初始化 Firebase
# ==========================================
cred = credentials.Certificate("firebase-adminsdk.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# ==========================================
# 初始化 MySQL
# ==========================================
MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER"),
    "password": os.getenv("MYSQL_PASSWORD"),
    "database": os.getenv("MYSQL_DATABASE"),
    "charset": "utf8mb4",
    "cursorclass": DictCursor,
    "autocommit": True,
}
conn = pymysql.connect(**MYSQL_CONFIG)
cur = conn.cursor()

# ==========================================
# 工具函式：統一把 Firestore 的各種時間格式轉成 MySQL 可接受的 naive datetime
# ==========================================
def to_mysql_datetime(value):
    if value is None:
        return datetime.utcnow()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return datetime.utcnow()
    if hasattr(value, "astimezone"):
        try:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            return value.replace(tzinfo=None) if getattr(value, "tzinfo", None) else value
    return datetime.utcnow()


counters = {
    "groups": 0, "group_members": 0, "expenses": 0,
    "orders": 0, "order_items": 0, "order_items_temp": 0, "settlements": 0,
}

# ==========================================
# 1. 遷移 users/{id}/expenses → expenses (owner_type='user')
# ==========================================
print("🚀 開始遷移個人記帳資料 (users)...")
for user_doc in db.collection("users").stream():
    user_id = user_doc.id
    for exp_doc in user_doc.reference.collection("expenses").stream():
        d = exp_doc.to_dict()
        try:
            cur.execute(
                """INSERT INTO expenses
                   (owner_type, owner_id, record_type, amount, item, category,
                    created_by_uid, created_by_name, created_at)
                   VALUES ('user', %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    user_id,
                    d.get("type", "expense"),
                    d.get("amount", 0),
                    d.get("item", ""),
                    d.get("category", "生活雜費"),
                    d.get("created_by_uid", user_id),
                    d.get("created_by_name", ""),
                    to_mysql_datetime(d.get("timestamp")),
                )
            )
            counters["expenses"] += 1
        except Exception as e:
            print(f"⚠️ 個人記帳寫入失敗 user={user_id} doc={exp_doc.id}: {e}")

# ==========================================
# 2. 遷移 groups/{id} 及其子集合
# ==========================================
print("🚀 開始遷移群組資料 (groups)...")
for group_doc in db.collection("groups").stream():
    group_id = group_doc.id
    g_data = group_doc.to_dict()

    # 2-1. groups 主體
    try:
        cur.execute(
            """INSERT INTO `groups` (group_id, state, active_order_code, created_at)
               VALUES (%s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE
                   state = VALUES(state),
                   active_order_code = VALUES(active_order_code)""",
            (
                group_id,
                g_data.get("state", "normal"),
                g_data.get("active_order_code") or None,
                to_mysql_datetime(g_data.get("created_at")),
            )
        )
        counters["groups"] += 1
    except Exception as e:
        print(f"⚠️ 群組主體寫入失敗 group={group_id}: {e}")
        continue

    # 2-2. group_members 子集合
    for member_doc in group_doc.reference.collection("members").stream():
        m = member_doc.to_dict()
        try:
            cur.execute(
                """INSERT INTO group_members (group_id, user_id, display_name, updated_at)
                   VALUES (%s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE display_name = VALUES(display_name)""",
                (
                    group_id,
                    member_doc.id,
                    m.get("display_name", f"成員({member_doc.id[:4]})"),
                    to_mysql_datetime(m.get("updated_at")),
                )
            )
            counters["group_members"] += 1
        except Exception as e:
            print(f"⚠️ 成員寫入失敗 group={group_id} user={member_doc.id}: {e}")

    # 2-3. groups/{id}/expenses 子集合
    for exp_doc in group_doc.reference.collection("expenses").stream():
        d = exp_doc.to_dict()
        try:
            cur.execute(
                """INSERT INTO expenses
                   (owner_type, owner_id, record_type, amount, item, category,
                    created_by_uid, created_by_name, created_at)
                   VALUES ('group', %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    group_id,
                    d.get("type", "expense"),
                    d.get("amount", 0),
                    d.get("item", ""),
                    d.get("category", "生活雜費"),
                    d.get("created_by_uid", ""),
                    d.get("created_by_name", ""),
                    to_mysql_datetime(d.get("timestamp")),
                )
            )
            counters["expenses"] += 1
        except Exception as e:
            print(f"⚠️ 群組記帳寫入失敗 group={group_id} doc={exp_doc.id}: {e}")

    # 2-4. groups/{id}/orders 子集合（已結單）+ 其 items 陣列
    for order_doc in group_doc.reference.collection("orders").stream():
        o = order_doc.to_dict()
        try:
            cur.execute(
                """INSERT INTO orders
                   (group_id, order_code, order_date, total_amount,
                    master_payer_id, master_payer_name, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    group_id,
                    o.get("order_code", ""),
                    o.get("order_date") or datetime.utcnow().strftime("%Y-%m-%d"),
                    o.get("total_amount", 0),
                    o.get("master_payer_id", ""),
                    o.get("master_payer_name", ""),
                    to_mysql_datetime(o.get("timestamp")),
                )
            )
            new_order_id = cur.lastrowid
            counters["orders"] += 1
        except Exception as e:
            print(f"⚠️ 訂單寫入失敗 group={group_id} order={order_doc.id}: {e}")
            continue

        for item in o.get("items", []):
            try:
                cur.execute(
                    """INSERT INTO order_items
                       (group_id, order_code, order_id, buyer_id, buyer_name,
                        item_name, price, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        group_id,
                        o.get("order_code", ""),
                        new_order_id,
                        item.get("buyer_id", ""),
                        item.get("buyer", ""),
                        item.get("item", ""),
                        item.get("price", 0),
                        to_mysql_datetime(item.get("timestamp")),
                    )
                )
                counters["order_items"] += 1
            except Exception as e:
                print(f"⚠️ 訂單品項寫入失敗 group={group_id} order={order_doc.id}: {e}")

    # 2-5. groups 主體上尚未結單的暫存品項 order_items_temp（order_id 留 NULL）
    for item in g_data.get("order_items_temp", []):
        try:
            cur.execute(
                """INSERT INTO order_items
                   (group_id, order_code, order_id, buyer_id, buyer_name,
                    item_name, price, created_at)
                   VALUES (%s, %s, NULL, %s, %s, %s, %s, %s)""",
                (
                    group_id,
                    g_data.get("active_order_code", ""),
                    item.get("buyer_id", ""),
                    item.get("buyer", ""),
                    item.get("item", ""),
                    item.get("price", 0),
                    to_mysql_datetime(item.get("timestamp")),
                )
            )
            counters["order_items_temp"] += 1
        except Exception as e:
            print(f"⚠️ 暫存品項寫入失敗 group={group_id}: {e}")

    # 2-6. groups/{id}/settlements 子集合
    for settle_doc in group_doc.reference.collection("settlements").stream():
        s = settle_doc.to_dict()
        try:
            cur.execute(
                """INSERT INTO settlements
                   (group_id, order_code_ref, payer_id, payer_name,
                    receiver_id, receiver_name, amount, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    group_id,
                    s.get("order_code_ref", ""),
                    s.get("payer_id", ""),
                    s.get("payer_name", ""),
                    s.get("receiver_id", ""),
                    s.get("receiver_name", ""),
                    s.get("amount", 0),
                    to_mysql_datetime(s.get("timestamp")),
                )
            )
            counters["settlements"] += 1
        except Exception as e:
            print(f"⚠️ 核銷紀錄寫入失敗 group={group_id} settle={settle_doc.id}: {e}")

cur.close()
conn.close()

print("\n✅ 遷移完成！統計如下：")
for table, count in counters.items():
    print(f"   {table}: {count} 筆")