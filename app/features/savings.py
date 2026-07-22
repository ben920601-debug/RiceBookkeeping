"""
存錢功能（個人版限定，測試密碼開通）。每人最多 6 個存錢筒。

指令：
  新增撲滿 名稱 [目標金額，選填]
  存錢 撲滿名稱 金額
  查看存錢
"""
import re

from app.db import db_cursor
from app.logging_utils import log_error
from app.line_client import send_line_reply

MAX_JARS_PER_OWNER = 6

JAR_ADD_PATTERN = re.compile(r'^新增撲滿\s+(\S+)(?:\s+(\d+))?$')
SAVE_PATTERN = re.compile(r'^存錢\s+(\S+)\s+(\d+)$')


def try_add_jar(owner_id: str, clean_text: str, reply_token: str) -> bool:
    m = JAR_ADD_PATTERN.match(clean_text)
    if not m:
        return False
    jar_name, target_str = m.groups()
    target_amount = int(target_str) if target_str else None

    try:
        with db_cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM savings_jars WHERE owner_id=%s", (owner_id,))
            cnt = cur.fetchone()["cnt"]
            if cnt >= MAX_JARS_PER_OWNER:
                send_line_reply(reply_token, f"⚠️ 每人最多只能建立 {MAX_JARS_PER_OWNER} 個存錢筒，目前已達上限。可以先刪除不用的再新增。")
                return True

            cur.execute(
                "INSERT INTO savings_jars (owner_id, jar_name, target_amount) VALUES (%s, %s, %s)",
                (owner_id, jar_name, target_amount)
            )
    except Exception as e:
        log_error("存錢筒新增", e, owner_id)
        if "Duplicate" in str(e):
            send_line_reply(reply_token, f"⚠️ 已經有一個叫「{jar_name}」的存錢筒了，換個名字試試？")
        else:
            send_line_reply(reply_token, "⚠️ 新增失敗，請稍後再試一次。")
        return True

    target_note = f"\n🎯 目標金額：${target_amount}" if target_amount else ""
    send_line_reply(reply_token, f"🐷 已建立存錢筒：{jar_name}{target_note}\n👉 輸入「存錢 {jar_name} 金額」即可開始存！")
    return True


def try_save_money(owner_id: str, clean_text: str, reply_token: str) -> bool:
    m = SAVE_PATTERN.match(clean_text)
    if not m:
        return False
    jar_name, amount_str = m.groups()
    amount = int(amount_str)
    if amount <= 0:
        return False

    try:
        with db_cursor() as cur:
            cur.execute("SELECT id, balance, target_amount, goal_reached_notified FROM savings_jars WHERE owner_id=%s AND jar_name=%s", (owner_id, jar_name))
            jar = cur.fetchone()
            if not jar:
                send_line_reply(reply_token, f"❌ 找不到叫「{jar_name}」的存錢筒，請確認名稱，或輸入「新增撲滿 {jar_name}」先建立。")
                return True

            new_balance = jar["balance"] + amount
            cur.execute("UPDATE savings_jars SET balance=%s WHERE id=%s", (new_balance, jar["id"]))

            cur.execute(
                """INSERT INTO expenses (owner_type, owner_id, record_type, amount, item, category, payment_method, created_by_uid, created_by_name)
                   VALUES ('user', %s, 'expense', %s, %s, '儲蓄', '現金', %s, %s)""",
                (owner_id, amount, f"存錢：{jar_name}", owner_id, jar_name)
            )
            expense_id = cur.lastrowid

            cur.execute(
                "INSERT INTO savings_transactions (jar_id, owner_id, amount, expense_id) VALUES (%s, %s, %s, %s)",
                (jar["id"], owner_id, amount, expense_id)
            )

            goal_note = ""
            if jar["target_amount"] and not jar["goal_reached_notified"] and new_balance >= jar["target_amount"]:
                cur.execute("UPDATE savings_jars SET goal_reached_notified=1 WHERE id=%s", (jar["id"],))
                goal_note = f"\n\n🎉🎉 恭喜！「{jar_name}」已經達到目標金額 ${jar['target_amount']} 了！"
    except Exception as e:
        log_error("存錢寫入", e, owner_id)
        send_line_reply(reply_token, "⚠️ 存錢失敗，請稍後再試一次。")
        return True

    send_line_reply(reply_token, f"🐷 已存入「{jar_name}」：${amount}\n💰 目前餘額：${new_balance}{goal_note}")
    return True


def try_list_jars(owner_id: str, clean_text: str, reply_token: str) -> bool:
    if clean_text not in ("查看存錢", "存錢清單", "撲滿清單"):
        return False
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT jar_name, balance, target_amount FROM savings_jars WHERE owner_id=%s ORDER BY created_at ASC",
                (owner_id,)
            )
            rows = cur.fetchall()
    except Exception as e:
        log_error("存錢筒清單查詢", e, owner_id)
        send_line_reply(reply_token, "⚠️ 查詢失敗，請稍後再試一次。")
        return True

    if not rows:
        send_line_reply(reply_token, "📭 目前還沒有任何存錢筒。\n👉 輸入「新增撲滿 名稱」即可建立（最多 6 個）。")
        return True

    lines = ["🐷 【存錢筒清單】"]
    for r in rows:
        if r["target_amount"]:
            pct = min(100, round(r["balance"] / r["target_amount"] * 100))
            lines.append(f"・{r['jar_name']}：${r['balance']} / ${r['target_amount']}（{pct}%）")
        else:
            lines.append(f"・{r['jar_name']}：${r['balance']}")
    send_line_reply(reply_token, "\n".join(lines))
    return True
