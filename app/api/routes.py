"""
監控後台（index.html）呼叫的 REST API：記帳查詢/編輯、群組狀態、
團單核銷、旅行規劃的查詢與增刪修。全部掛在同一個 APIRouter，
main.py 用 app.include_router(router) 掛載即可。
"""
from datetime import datetime
from typing import Literal, Optional
from dateutil.relativedelta import relativedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import db_cursor, is_db_ready
from app.geo import geocode_location
from app.logging_utils import log_error
from app.line_client import get_full_group_member_list

router = APIRouter()

# ==========================================
class ExpenseUpdate(BaseModel):
    item: str
    amount: int

class GroupStateUpdate(BaseModel):
    state: Literal["normal", "order", "settle"]

class OrderItemUpdate(BaseModel):
    buyer_id: str
    buyer_name: str
    item_name: str
    price: int

class OrderItemCreate(BaseModel):
    buyer_id: str
    buyer_name: str
    item_name: str
    price: int

class OrderMasterPayerUpdate(BaseModel):
    master_payer_id: str
    master_payer_name: str


def _require_db():
    if not is_db_ready():
        raise HTTPException(status_code=503, detail="資料庫尚未就緒")


@router.get("/api/expenses")
def api_list_expenses(
    owner_type: Literal["user", "group"],
    owner_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    record_type: Optional[str] = None,
    payment_method: Optional[str] = None,
):
    """個人/群組首頁流水帳、進階查詢共用：依日期區間、收支類型、支付方式篩選"""
    _require_db()
    sql = "SELECT id, record_type, amount, item, category, payment_method, created_by_name, created_at FROM expenses WHERE owner_type=%s AND owner_id=%s"
    params = [owner_type, owner_id]
    if start:
        sql += " AND created_at >= %s"
        params.append(f"{start} 00:00:00")
    if end:
        sql += " AND created_at <= %s"
        params.append(f"{end} 23:59:59")
    if record_type and record_type != "all":
        sql += " AND record_type = %s"
        params.append(record_type)
    if payment_method and payment_method != "all":
        sql += " AND payment_method = %s"
        params.append(payment_method)
    sql += " ORDER BY created_at DESC"
    try:
        with db_cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        for r in rows:
            r["created_at"] = r["created_at"].isoformat()
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/payment-method-summary")
def api_payment_method_summary(owner_id: str, start: Optional[str] = None, end: Optional[str] = None):
    """個人版限定：依支付方式加總支出金額，供首頁圖表與「現金/最常用支付工具」欄位使用"""
    _require_db()
    sql = "SELECT payment_method, COALESCE(SUM(amount),0) AS total FROM expenses WHERE owner_type='user' AND owner_id=%s AND record_type='expense'"
    params = [owner_id]
    if start:
        sql += " AND created_at >= %s"
        params.append(f"{start} 00:00:00")
    if end:
        sql += " AND created_at <= %s"
        params.append(f"{end} 23:59:59")
    sql += " GROUP BY payment_method ORDER BY total DESC"
    try:
        with db_cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/api/expenses/{expense_id}")
def api_update_expense(expense_id: int, body: ExpenseUpdate):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE expenses SET item=%s, amount=%s WHERE id=%s",
                (body.item, body.amount, expense_id)
            )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/expenses/{expense_id}")
def api_delete_expense(expense_id: int):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM expenses WHERE id=%s", (expense_id,))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/groups/{group_id}")
def api_get_group(group_id: str):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT group_id, state, active_order_code FROM `groups` WHERE group_id=%s", (group_id,))
            row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="找不到此群組")
        return row
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/api/groups/{group_id}/state")
def api_update_group_state(group_id: str, body: GroupStateUpdate):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("UPDATE `groups` SET state=%s WHERE group_id=%s", (body.state, group_id))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/groups/{group_id}/payer-summary")
def api_payer_summary(group_id: str):
    """群組成員歷史累計墊付排行（管理頁的甜甜圈圖用）"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                """SELECT created_by_name, COALESCE(SUM(amount),0) AS total
                   FROM expenses
                   WHERE owner_type='group' AND owner_id=%s AND record_type != 'income'
                   GROUP BY created_by_name
                   ORDER BY total DESC""",
                (group_id,)
            )
            return cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/groups/{group_id}/orders")
def api_list_orders(group_id: str):
    """歷史揪團訂單清單，並附上每個訂單的成員應付/已付明細（後端算好，前端不用再算）"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                """SELECT id, order_code, order_name, order_date, total_amount, master_payer_id, master_payer_name, created_at
                   FROM orders WHERE group_id=%s ORDER BY created_at DESC""",
                (group_id,)
            )
            orders = cur.fetchall()

            cur.execute(
                "SELECT payer_name, order_code_ref, amount FROM settlements WHERE group_id=%s",
                (group_id,)
            )
            settlements = cur.fetchall()

            for o in orders:
                cur.execute(
                    "SELECT id, buyer_id, buyer_name, item_name, price FROM order_items WHERE order_id=%s ORDER BY id ASC",
                    (o["id"],)
                )
                o["items"] = cur.fetchall()
                o["created_at"] = o["created_at"].isoformat()
                if hasattr(o["order_date"], "isoformat"):
                    o["order_date"] = o["order_date"].isoformat()

                expected = {}
                for item in o["items"]:
                    expected[item["buyer_name"]] = expected.get(item["buyer_name"], 0) + item["price"]
                actual = {}
                for s in settlements:
                    if s["order_code_ref"] == o["order_code"]:
                        actual[s["payer_name"]] = actual.get(s["payer_name"], 0) + s["amount"]
                o["expected"] = expected
                o["actual"] = actual

        return orders
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/api/order-items/{item_id}")
def api_update_order_item(item_id: int, body: OrderItemUpdate):
    """修改團單品項：務必連 buyer_id 一起改，因為 LINE 對話端的核銷比對是用 buyer_id
    去查應付金額，只改 buyer_name（純文字）的話，核銷永遠對不上正確的人。"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT order_id FROM order_items WHERE id=%s", (item_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="找不到此品項")
            cur.execute(
                "UPDATE order_items SET buyer_id=%s, buyer_name=%s, item_name=%s, price=%s WHERE id=%s",
                (body.buyer_id, body.buyer_name, body.item_name, body.price, item_id)
            )
            if row["order_id"]:
                cur.execute(
                    "UPDATE orders SET total_amount=(SELECT COALESCE(SUM(price),0) FROM order_items WHERE order_id=%s) WHERE id=%s",
                    (row["order_id"], row["order_id"])
                )
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/order-items/{item_id}")
def api_delete_order_item(item_id: int):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT order_id FROM order_items WHERE id=%s", (item_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="找不到此品項")
            cur.execute("DELETE FROM order_items WHERE id=%s", (item_id,))
            if row["order_id"]:
                cur.execute(
                    "UPDATE orders SET total_amount=(SELECT COALESCE(SUM(price),0) FROM order_items WHERE order_id=%s) WHERE id=%s",
                    (row["order_id"], row["order_id"])
                )
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/groups/{group_id}/orders/{order_id}")
def api_delete_order(group_id: str, order_id: int):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM order_items WHERE order_id=%s AND group_id=%s", (order_id, group_id))
            cur.execute("DELETE FROM orders WHERE id=%s AND group_id=%s", (order_id, group_id))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/groups/{group_id}/members")
def api_list_group_members(group_id: str):
    """群組完整成員清單（優先呼叫LINE官方API拿真實全體成員），供監控後台下拉選單使用，
    確保選到的是正確的 user_id，不會因為手動輸入名字打錯字而讓核銷對不上人。"""
    try:
        return get_full_group_member_list(group_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/groups/{group_id}/orders/{order_id}/items")
def api_add_order_item(group_id: str, order_id: int, body: OrderItemCreate):
    """幫團單新增一位付款人（分攤品項）"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT order_code FROM orders WHERE id=%s AND group_id=%s", (order_id, group_id))
            order = cur.fetchone()
            if not order:
                raise HTTPException(status_code=404, detail="找不到此團單")
            cur.execute(
                """INSERT INTO order_items (group_id, order_code, order_id, buyer_id, buyer_name, item_name, price)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (group_id, order["order_code"], order_id, body.buyer_id, body.buyer_name, body.item_name, body.price)
            )
            cur.execute(
                "UPDATE orders SET total_amount=(SELECT COALESCE(SUM(price),0) FROM order_items WHERE order_id=%s) WHERE id=%s",
                (order_id, order_id)
            )
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/api/groups/{group_id}/orders/{order_id}")
def api_update_order_master_payer(group_id: str, order_id: int, body: OrderMasterPayerUpdate):
    """修改團單的墊付人（誰先代墊了這筆錢）"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT id FROM orders WHERE id=%s AND group_id=%s", (order_id, group_id))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="找不到此團單")
            cur.execute(
                "UPDATE orders SET master_payer_id=%s, master_payer_name=%s WHERE id=%s",
                (body.master_payer_id, body.master_payer_name, order_id)
            )
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 🧳 旅行功能 REST API（BETA，供 index.html 顯示/增減修正旅行規劃）
# ==========================================
class TripCreate(BaseModel):
    owner_type: Literal["user", "group"]
    owner_id: str
    departure_at: str  # "YYYY-MM-DD HH:MM"
    return_at: str

class TripTimesUpdate(BaseModel):
    departure_at: str
    return_at: str

class ItineraryItemCreate(BaseModel):
    scheduled_at: str  # "YYYY-MM-DD HH:MM"
    location_name: str

class ItineraryItemUpdate(BaseModel):
    scheduled_at: str
    location_name: str

def _parse_dt(s: str) -> datetime:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        raise HTTPException(status_code=400, detail="日期時間格式錯誤，請用 YYYY-MM-DD HH:MM")

@router.get("/api/trips")
def api_list_trips(owner_type: Literal["user", "group"], owner_id: str):
    """列出該使用者/群組的所有旅行（含已完成規劃與進行中的草案），並附上每趟旅行的行程項目明細"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                """SELECT id, trip_code, departure_at, return_at, status, ai_route_summary, created_at
                   FROM trips WHERE owner_type=%s AND owner_id=%s ORDER BY departure_at DESC""",
                (owner_type, owner_id)
            )
            trips = cur.fetchall()
            for t in trips:
                cur.execute(
                    "SELECT id, scheduled_at, location_name, notified FROM itineraries WHERE trip_id=%s ORDER BY scheduled_at ASC",
                    (t["id"],)
                )
                items = cur.fetchall()
                for it in items:
                    it["scheduled_at"] = it["scheduled_at"].isoformat()
                t["items"] = items
                t["departure_at"] = t["departure_at"].isoformat()
                t["return_at"] = t["return_at"].isoformat()
                t["created_at"] = t["created_at"].isoformat()
        return trips
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/trips")
def api_create_trip(body: TripCreate):
    """中控後台直接新增一趟旅行（不經過 LINE 對話流程），建立時即視為已確認並產生旅行單號"""
    _require_db()
    departure_at = _parse_dt(body.departure_at)
    return_at = _parse_dt(body.return_at)
    if return_at <= departure_at:
        raise HTTPException(status_code=400, detail="回程時間必須晚於出發時間")
    try:
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO trips (owner_type, owner_id, departure_at, return_at, status, created_by_uid)
                   VALUES (%s, %s, %s, %s, 'collecting', 'admin-panel')""",
                (body.owner_type, body.owner_id, departure_at, return_at)
            )
            trip_id = cur.lastrowid
            trip_code = departure_at.strftime("%Y%m%d")
            cur.execute("SELECT COUNT(*) AS cnt FROM trips WHERE owner_type=%s AND owner_id=%s AND trip_code LIKE %s",
                        (body.owner_type, body.owner_id, f"{trip_code}%"))
            cnt = cur.fetchone()["cnt"]
            if cnt > 0:
                trip_code = f"{trip_code}-{cnt + 1}"
            cur.execute("UPDATE trips SET status='confirmed', trip_code=%s WHERE id=%s", (trip_code, trip_id))
        return {"ok": True, "id": trip_id, "trip_code": trip_code}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/api/trips/{trip_id}")
def api_update_trip_times(trip_id: int, body: TripTimesUpdate):
    """編輯一趟旅行的出發／回程時間"""
    _require_db()
    departure_at = _parse_dt(body.departure_at)
    return_at = _parse_dt(body.return_at)
    if return_at <= departure_at:
        raise HTTPException(status_code=400, detail="回程時間必須晚於出發時間")
    try:
        with db_cursor() as cur:
            cur.execute("UPDATE trips SET departure_at=%s, return_at=%s WHERE id=%s", (departure_at, return_at, trip_id))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/trips/{trip_id}")
def api_delete_trip(trip_id: int):
    """刪除一趟旅行（含底下所有行程項目，靠外鍵 CASCADE 一併清除）"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM trips WHERE id=%s", (trip_id,))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/trips/{trip_id}/items")
def api_add_trip_item(trip_id: int, body: ItineraryItemCreate):
    """新增一個行程項目到指定旅行（座標會自動地理編碼，查不到也不影響新增）"""
    _require_db()
    scheduled_at = _parse_dt(body.scheduled_at)
    try:
        with db_cursor() as cur:
            cur.execute("SELECT owner_type, owner_id FROM trips WHERE id=%s", (trip_id,))
            trip = cur.fetchone()
            if not trip:
                raise HTTPException(status_code=404, detail="找不到此旅行")
            lat, lon = geocode_location(body.location_name)
            cur.execute(
                """INSERT INTO itineraries (owner_type, owner_id, trip_id, scheduled_at, location_name, latitude, longitude, created_by_uid)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'admin-panel')""",
                (trip["owner_type"], trip["owner_id"], trip_id, scheduled_at, body.location_name.strip(), lat, lon)
            )
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/api/itinerary-items/{item_id}")
def api_update_trip_item(item_id: int, body: ItineraryItemUpdate):
    """編輯行程項目的時間／地點（地點若有變更會重新地理編碼），並重置提醒狀態"""
    _require_db()
    scheduled_at = _parse_dt(body.scheduled_at)
    try:
        with db_cursor() as cur:
            cur.execute("SELECT location_name FROM itineraries WHERE id=%s", (item_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="找不到此行程項目")
            if body.location_name.strip() != row["location_name"]:
                lat, lon = geocode_location(body.location_name)
                cur.execute(
                    "UPDATE itineraries SET scheduled_at=%s, location_name=%s, latitude=%s, longitude=%s, notified=0 WHERE id=%s",
                    (scheduled_at, body.location_name.strip(), lat, lon, item_id)
                )
            else:
                cur.execute("UPDATE itineraries SET scheduled_at=%s, notified=0 WHERE id=%s", (scheduled_at, item_id))
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/itinerary-items/{item_id}")
def api_delete_trip_item(item_id: int):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM itineraries WHERE id=%s", (item_id,))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 💳 繳費功能 REST API（個人版限定）
# ==========================================
class BillCreate(BaseModel):
    bill_name: str
    amount: int
    due_date: str  # "YYYY-MM-DD"
    installments_remaining: Optional[int] = None

class BillUpdate(BaseModel):
    bill_name: str
    amount: int
    due_date: str
    installments_remaining: Optional[int] = None

@router.get("/api/bills")
def api_list_bills(owner_id: str):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                """SELECT id, bill_name, amount, due_date, installments_remaining, status, is_paid, created_at
                   FROM bills WHERE owner_id=%s ORDER BY due_date ASC""",
                (owner_id,)
            )
            rows = cur.fetchall()
            for r in rows:
                r["due_date"] = r["due_date"].isoformat()
                r["created_at"] = r["created_at"].isoformat()
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/bills")
def api_create_bill(owner_id: str, body: BillCreate):
    _require_db()
    due = _parse_dt(body.due_date + " 00:00").date()
    try:
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO bills (owner_id, bill_name, amount, due_date, installments_remaining)
                   VALUES (%s, %s, %s, %s, %s)""",
                (owner_id, body.bill_name.strip(), body.amount, due, body.installments_remaining)
            )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/api/bills/{bill_id}")
def api_update_bill(bill_id: int, body: BillUpdate):
    _require_db()
    due = _parse_dt(body.due_date + " 00:00").date()
    try:
        with db_cursor() as cur:
            cur.execute(
                """UPDATE bills SET bill_name=%s, amount=%s, due_date=%s, installments_remaining=%s
                   WHERE id=%s""",
                (body.bill_name.strip(), body.amount, due, body.installments_remaining, bill_id)
            )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/bills/{bill_id}")
def api_delete_bill(bill_id: int):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM bills WHERE id=%s", (bill_id,))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/bill-payments")
def api_list_bill_payments(owner_id: str):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                """SELECT bp.id, bp.bill_id, b.bill_name, bp.amount, bp.paid_at
                   FROM bill_payments bp JOIN bills b ON b.id = bp.bill_id
                   WHERE bp.owner_id=%s ORDER BY bp.paid_at DESC""",
                (owner_id,)
            )
            rows = cur.fetchall()
            for r in rows:
                r["paid_at"] = r["paid_at"].isoformat()
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/bill-payments/{payment_id}")
def api_delete_bill_payment(payment_id: int):
    """刪除一筆核銷紀錄：把對應帳單的已繳狀態改回未繳，並還原到繳費前的到期日/期數
    （若該筆核銷曾同時寫入一筆一般支出，一併刪除該支出，避免帳目對不起來）"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT bill_id, expense_id FROM bill_payments WHERE id=%s", (payment_id,))
            payment = cur.fetchone()
            if not payment:
                raise HTTPException(status_code=404, detail="找不到此核銷紀錄")

            cur.execute("SELECT status, due_date, installments_remaining FROM bills WHERE id=%s", (payment["bill_id"],))
            bill = cur.fetchone()
            if bill:
                if bill["status"] == "completed":
                    # 是最後一期才核銷完成的：期數補回1、狀態改回active，到期日當初沒被更動過不用還原
                    cur.execute(
                        "UPDATE bills SET is_paid=0, status='active', installments_remaining=1 WHERE id=%s",
                        (payment["bill_id"],)
                    )
                else:
                    new_installments = (bill["installments_remaining"] + 1) if bill["installments_remaining"] is not None else None
                    reverted_due = bill["due_date"] - relativedelta(months=1)
                    cur.execute(
                        "UPDATE bills SET is_paid=0, due_date=%s, installments_remaining=%s WHERE id=%s",
                        (reverted_due, new_installments, payment["bill_id"])
                    )

            if payment["expense_id"]:
                cur.execute("DELETE FROM expenses WHERE id=%s", (payment["expense_id"],))

            cur.execute("DELETE FROM bill_payments WHERE id=%s", (payment_id,))
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# 🐷 存錢功能 REST API（個人版限定）
# ==========================================
class SavingsJarCreate(BaseModel):
    jar_name: str
    target_amount: Optional[int] = None

class SavingsJarUpdate(BaseModel):
    jar_name: str
    balance: int
    target_amount: Optional[int] = None

@router.get("/api/savings-jars")
def api_list_jars(owner_id: str):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT id, jar_name, balance, target_amount, created_at FROM savings_jars WHERE owner_id=%s ORDER BY created_at ASC",
                (owner_id,)
            )
            rows = cur.fetchall()
            for r in rows:
                r["created_at"] = r["created_at"].isoformat()
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/savings-jars")
def api_create_jar(owner_id: str, body: SavingsJarCreate):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM savings_jars WHERE owner_id=%s", (owner_id,))
            if cur.fetchone()["cnt"] >= 6:
                raise HTTPException(status_code=400, detail="每人最多只能建立 6 個存錢筒")
            cur.execute(
                "INSERT INTO savings_jars (owner_id, jar_name, target_amount) VALUES (%s, %s, %s)",
                (owner_id, body.jar_name.strip(), body.target_amount)
            )
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/api/savings-jars/{jar_id}")
def api_update_jar(jar_id: int, body: SavingsJarUpdate):
    """可直接修正餘額（例如手動調整誤差），也可改名稱／目標金額"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE savings_jars SET jar_name=%s, balance=%s, target_amount=%s WHERE id=%s",
                (body.jar_name.strip(), body.balance, body.target_amount, jar_id)
            )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/savings-jars/{jar_id}")
def api_delete_jar(jar_id: int):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM savings_jars WHERE id=%s", (jar_id,))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# 💰 支付方式 REST API（個人版限定）
# ==========================================
class PaymentMethodCreate(BaseModel):
    method_name: str

@router.get("/api/payment-methods")
def api_list_payment_methods(owner_id: str):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT id, method_name FROM payment_methods WHERE owner_id=%s ORDER BY created_at ASC", (owner_id,))
        return cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/payment-methods")
def api_create_payment_method(owner_id: str, body: PaymentMethodCreate):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO payment_methods (owner_id, method_name) VALUES (%s, %s)",
                (owner_id, body.method_name.strip())
            )
        return {"ok": True}
    except Exception as e:
        if "Duplicate" in str(e):
            raise HTTPException(status_code=400, detail="這個支付方式已經登記過了")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/payment-methods/{method_id}")
def api_delete_payment_method(method_id: int):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM payment_methods WHERE id=%s", (method_id,))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# 🖥️ 監控後台整體設定 REST API（開關、跑馬燈、各功能狀態標籤）
# ------------------------------------------
# 給 index.html 在載入時呼叫一次：決定要不要整頁擋下顯示維護畫面、
# 要不要顯示跑馬燈、每個功能圖示要不要顯示「Beta／維護中」標籤。
# 這裡是唯讀查詢，實際的開關/編輯都在中控台（admin-panel）那邊操作。
# ==========================================
@router.get("/api/dashboard-settings")
def api_dashboard_settings():
    _require_db()

    # 兩段查詢刻意分開包 try/except：任一段失敗（例如 feature_switches 這張表
    # 還沒跑 migration）都不該連累另一段，尤其 dashboard_enabled 這種「總開關」
    # 絕對不能因為別的表出錯就整支 API 失敗、被前端誤判成「讀不到＝當作開放」。
    settings = {}
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT `key`, `value` FROM bot_settings WHERE `key` IN "
                "('dashboard_enabled', 'marquee_enabled', 'marquee_text', 'marquee_color', 'marquee_speed_seconds')"
            )
            settings = {r["key"]: r["value"] for r in cur.fetchall()}
    except Exception as e:
        log_error("dashboard-settings讀取(bot_settings)", e)
        raise HTTPException(status_code=500, detail=str(e))

    feature_statuses = {}
    try:
        with db_cursor() as cur:
            cur.execute("SELECT feature_key, status FROM feature_switches")
            feature_statuses = {r["feature_key"]: r["status"] for r in cur.fetchall()}
    except Exception as e:
        log_error("dashboard-settings讀取(feature_switches)", e)
        # feature_statuses 查不到就給空字典，不影響 dashboard_enabled／跑馬燈這些核心設定

    return {
        "dashboard_enabled": settings.get("dashboard_enabled", "1") == "1",
        "marquee_enabled": settings.get("marquee_enabled", "0") == "1",
        "marquee_text": settings.get("marquee_text") or "",
        "marquee_color": settings.get("marquee_color") or "#F59E0B",
        "marquee_speed_seconds": int(settings.get("marquee_speed_seconds") or 18),
        "feature_statuses": feature_statuses,
    }
