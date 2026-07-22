"""
繳費功能（個人版限定，測試密碼開通）。

指令：
  新增繳費 帳單名稱 金額 到期日[YYYY-MM-DD] [期數，選填]
  查看繳費
  在「繳費模式」開通期間，輸入「帳單名稱 金額」（跟一般快速記帳同樣格式）
  會先比對是否有符合的未繳費項目，命中就核銷；沒命中才會當一般訊息繼續往下處理。

核銷成功時：
  - 寫入 bill_payments 一筆核銷紀錄
  - 同時寫入 expenses 一筆一般支出（分類固定「保險」？不，用帳單名稱本身當項目名稱，
    分類交給自動歸類判斷；找不到就是「其他」）
  - 有設定分期期數的話遞減，期數歸零則整筆帳單狀態改為 completed（停止提醒）；
    沒設定期數（無限期）或期數還沒歸零，則自動把 due_date 往後推一個月，reminded 重置

背景排程：每天檢查一次，繳費日前 2 天且尚未提醒過的帳單，主動推播提醒。
"""
import re
import asyncio
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

from app.db import db_cursor
from app.logging_utils import log_error
from app.line_client import send_line_reply, push_line_message
from app.categorize import resolve_category

BILL_ADD_PATTERN = re.compile(
    r'^新增繳費\s+(.+?)\s+(\d+)\s+(\d{4}-\d{1,2}-\d{1,2})(?:\s+(\d+))?$'
)


def try_add_bill(owner_id: str, clean_text: str, reply_token: str) -> bool:
    m = BILL_ADD_PATTERN.match(clean_text)
    if not m:
        return False
    bill_name, amount_str, due_str, installments_str = m.groups()
    amount = int(amount_str)
    try:
        due_date = datetime.strptime(due_str, "%Y-%m-%d").date()
    except ValueError:
        send_line_reply(reply_token, "⚠️ 到期日格式看不懂，請用「YYYY-MM-DD」，例如：2026-08-05")
        return True
    installments = int(installments_str) if installments_str else None

    try:
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO bills (owner_id, bill_name, amount, due_date, installments_remaining)
                   VALUES (%s, %s, %s, %s, %s)""",
                (owner_id, bill_name.strip(), amount, due_date, installments)
            )
    except Exception as e:
        log_error("繳費項目新增", e, owner_id)
        send_line_reply(reply_token, "⚠️ 新增失敗，請稍後再試一次。")
        return True

    period_note = f"，共 {installments} 期" if installments else "（每月持續，無期數限制）"
    send_line_reply(
        reply_token,
        f"💳 已登記繳費項目：{bill_name.strip()}\n💰 金額：${amount}\n📅 到期日：{due_date}{period_note}\n"
        f"👉 到期日前 2 天會主動提醒您；繳費後輸入「{bill_name.strip()} {amount}」即可核銷。"
    )
    return True


def try_list_bills(owner_id: str, clean_text: str, reply_token: str) -> bool:
    if clean_text not in ("查看繳費", "繳費清單"):
        return False
    try:
        with db_cursor() as cur:
            cur.execute(
                """SELECT bill_name, amount, due_date, installments_remaining, is_paid
                   FROM bills WHERE owner_id=%s AND status='active' ORDER BY due_date ASC""",
                (owner_id,)
            )
            rows = cur.fetchall()
    except Exception as e:
        log_error("繳費清單查詢", e, owner_id)
        send_line_reply(reply_token, "⚠️ 查詢失敗，請稍後再試一次。")
        return True

    if not rows:
        send_line_reply(reply_token, "📭 目前沒有登記中的繳費項目。\n👉 輸入「新增繳費 帳單名稱 金額 到期日」即可新增。")
        return True

    lines = ["💳 【繳費項目清單】"]
    for r in rows:
        status = "✅ 已繳" if r["is_paid"] else "🔴 未繳"
        period = f"（剩 {r['installments_remaining']} 期）" if r["installments_remaining"] is not None else ""
        lines.append(f"・{r['bill_name']} ${r['amount']} - {r['due_date']} {status}{period}")
    send_line_reply(reply_token, "\n".join(lines))
    return True


def try_reconcile_bill(owner_id: str, item_name: str, amount: int, reply_token: str) -> bool:
    """在快速記帳格式「項目 金額」比對是否命中未繳費的帳單；命中才回傳 True 並處理核銷。"""
    try:
        with db_cursor() as cur:
            cur.execute(
                """SELECT id, bill_name, amount, due_date, installments_remaining FROM bills
                   WHERE owner_id=%s AND status='active' AND is_paid=0
                     AND bill_name=%s AND amount=%s
                   ORDER BY due_date ASC LIMIT 1""",
                (owner_id, item_name, amount)
            )
            bill = cur.fetchone()
    except Exception as e:
        log_error("繳費核銷查詢", e, owner_id)
        return False

    if not bill:
        return False

    category = resolve_category(item_name)
    try:
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO expenses (owner_type, owner_id, record_type, amount, item, category, payment_method, created_by_uid, created_by_name)
                   VALUES ('user', %s, 'expense', %s, %s, %s, '現金', %s, %s)""",
                (owner_id, amount, item_name, category, owner_id, item_name)
            )
            expense_id = cur.lastrowid

            cur.execute(
                "INSERT INTO bill_payments (bill_id, owner_id, amount, expense_id) VALUES (%s, %s, %s, %s)",
                (bill["id"], owner_id, amount, expense_id)
            )

            new_installments = bill["installments_remaining"]
            if new_installments is not None:
                new_installments -= 1

            if new_installments is not None and new_installments <= 0:
                cur.execute(
                    "UPDATE bills SET is_paid=1, installments_remaining=0, status='completed' WHERE id=%s",
                    (bill["id"],)
                )
                finish_note = "\n🎉 這是最後一期，繳費項目已全部繳清！"
            else:
                next_due = bill["due_date"] + relativedelta(months=1)
                cur.execute(
                    """UPDATE bills SET is_paid=1, installments_remaining=%s,
                       due_date=%s, reminded=0 WHERE id=%s""",
                    (new_installments, next_due, bill["id"])
                )
                finish_note = f"\n📅 下一期到期日已自動更新為：{next_due}"
    except Exception as e:
        log_error("繳費核銷寫入", e, owner_id)
        send_line_reply(reply_token, "⚠️ 核銷失敗，請稍後再試一次。")
        return True

    send_line_reply(reply_token, f"✅ 繳費核銷成功：{item_name} ${amount}（分類：{category}）{finish_note}")
    return True


def check_and_send_bill_reminders():
    """由背景排程呼叫：找出 2 天後到期、尚未提醒過的帳單，主動推播提醒"""
    target_date = (datetime.now() + timedelta(days=2)).date()
    try:
        with db_cursor() as cur:
            cur.execute(
                """SELECT id, owner_id, bill_name, amount, due_date FROM bills
                   WHERE status='active' AND is_paid=0 AND reminded=0 AND due_date=%s""",
                (target_date,)
            )
            due_bills = cur.fetchall()
            for b in due_bills:
                cur.execute("UPDATE bills SET reminded=1 WHERE id=%s", (b["id"],))
    except Exception as e:
        log_error("繳費提醒排程查詢", e)
        return

    for b in due_bills:
        push_line_message(
            b["owner_id"],
            f"💳 【繳費提醒】{b['bill_name']} 將於 {b['due_date']}（2天後）到期\n"
            f"💰 金額：${b['amount']}\n👉 繳費後請輸入「{b['bill_name']} {b['amount']}」完成核銷。"
        )


async def bill_reminder_loop():
    """背景排程：每天檢查一次是否有 2 天後到期、尚未提醒過的繳費項目"""
    while True:
        try:
            await asyncio.to_thread(check_and_send_bill_reminders)
        except Exception as e:
            log_error("繳費提醒排程迴圈", e)
        await asyncio.sleep(24 * 60 * 60)
