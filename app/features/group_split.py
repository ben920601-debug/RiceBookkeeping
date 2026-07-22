"""
群組團單：一般模式輸入「品項 金額」後，詢問「均分／@tag／@tag+金額指定／跳過」，
建立分攤團單並產生核銷單號。
"""
import json
import random

from datetime import datetime, timedelta

from app.db import db_cursor
from app.logging_utils import log_error
from app.line_client import send_line_reply, get_mentions_with_amounts, resolve_id_to_name
from app.categorize import resolve_category

def get_group_member_list(group_id: str) -> list:
    """取得目前快取到的群組成員清單（可能不完整，僅限機器人曾經互動過的成員）"""
    try:
        with db_cursor() as cur:
            cur.execute("SELECT user_id, display_name FROM group_members WHERE group_id=%s", (group_id,))
            return cur.fetchall()
    except Exception as e:
        log_error("群組成員查詢", e, group_id)
        return []

def create_split_order(group_id: str, payer_id: str, payer_name: str, items: list, participants: list) -> str:
    """
    items: [{"item_name": str, "price": int}, ...]（該筆花費的品項明細，用於算總額與訂單品項名稱）
    participants: [{"user_id": str, "display_name": str}, ...]（要平均分攤的人）
    回傳新產生的 4 碼團單號
    """
    total = sum(i["price"] for i in items)
    n = max(1, len(participants))
    base_share = total // n
    remainder = total - base_share * n
    custom = [{"user_id": p["user_id"], "display_name": p["display_name"],
               "amount": base_share + (remainder if idx == 0 else 0)} for idx, p in enumerate(participants)]
    return create_split_order_custom(group_id, payer_id, payer_name, items, custom)

def create_split_order_custom(group_id: str, payer_id: str, payer_name: str, items: list, participant_amounts: list) -> str:
    """
    participant_amounts: [{"user_id": str, "display_name": str, "amount": int}, ...]（每人分攤的確切金額）
    用於「均分」（外層先算好平分金額）與「指定金額」（使用者自己在 @tag 後面打金額）共用。
    回傳新產生的 4 碼團單號
    """
    total = sum(i["price"] for i in items)
    code_str = str(random.randint(1000, 9999))
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO orders (group_id, order_code, order_date, total_amount, master_payer_id, master_payer_name)
               VALUES (%s, %s, CURDATE(), %s, %s, %s)""",
            (group_id, code_str, total, payer_id, payer_name)
        )
        order_pk = cur.lastrowid
        item_label = "、".join(i["item_name"] for i in items) if len(items) <= 3 else f"{items[0]['item_name']}等{len(items)}項"
        for p in participant_amounts:
            cur.execute(
                """INSERT INTO order_items (group_id, order_code, order_id, buyer_id, buyer_name, item_name, price)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (group_id, code_str, order_pk, p["user_id"], p["display_name"], item_label, p["amount"])
            )
    return code_str

def try_handle_group_split_reply(group_id: str, event, clean_text: str, creator_id: str, reply_token: str) -> bool:
    """處理群組團單詢問後，使用者回覆「均分／@tag／@tag+金額指定／跳過」"""
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT payer_id, payer_name, items_json, total_amount, created_at FROM pending_group_expense WHERE group_id=%s",
                (group_id,)
            )
            row = cur.fetchone()
    except Exception as e:
        log_error("待分攤花費查詢", e, group_id)
        return False

    if not row:
        return False
    if datetime.now() - row["created_at"] > timedelta(minutes=10):
        try:
            with db_cursor() as cur:
                cur.execute("DELETE FROM pending_group_expense WHERE group_id=%s", (group_id,))
        except Exception:
            pass
        return False  # 過期視為沒有待處理，讓訊息照正常流程走

    items = json.loads(row["items_json"])
    payer_id, payer_name = row["payer_id"], row["payer_name"]
    mentions = get_mentions_with_amounts(event)
    real_tagged_ids = [m["user_id"] for m in mentions]
    is_skip = any(k in clean_text for k in ["跳過", "不分攤", "算了", "略過"])
    is_split_even = any(k in clean_text for k in ["均分", "平分", "平攤"]) and not real_tagged_ids

    if not (is_skip or is_split_even or real_tagged_ids):
        return False  # 不是在回答這個問題

    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM pending_group_expense WHERE group_id=%s", (group_id,))
    except Exception as e:
        log_error("待分攤花費清除", e, group_id)

    if is_skip:
        try:
            with db_cursor() as cur:
                for i in items:
                    cur.execute(
                        """INSERT INTO expenses
                           (owner_type, owner_id, record_type, amount, item, category, created_by_uid, created_by_name)
                           VALUES ('group', %s, 'expense', %s, %s, %s, %s, %s)""",
                        (group_id, i["price"], i["item_name"], resolve_category(i["item_name"]), payer_id, payer_name)
                    )
            send_line_reply(reply_token, f"✅ 已記為一般花費（不分攤）：共 ${row['total_amount']}")
        except Exception as e:
            log_error("跳過分攤寫入", e, group_id)
            send_line_reply(reply_token, "⚠️ 紀錄失敗，請稍後再試一次。")
        return True

    # 🎯 指定金額模式：使用者在每個 @tag 後面都有打金額，例如「@小明 100 @小華 50」
    has_all_amounts = bool(mentions) and all(m["amount"] is not None for m in mentions)

    if has_all_amounts:
        participant_amounts = [
            {"user_id": m["user_id"], "display_name": resolve_id_to_name(group_id, m["user_id"]), "amount": m["amount"]}
            for m in mentions
        ]
        specified_total = sum(p["amount"] for p in participant_amounts)
        try:
            code_str = create_split_order_custom(group_id, payer_id, payer_name, items, participant_amounts)
        except Exception as e:
            log_error("指定金額分攤建單", e, group_id)
            send_line_reply(reply_token, "⚠️ 分攤登記失敗，請稍後再試一次。")
            return True

        names = "、".join(f"{p['display_name']} ${p['amount']}" for p in participant_amounts)
        mismatch_note = ""
        if specified_total != row["total_amount"]:
            mismatch_note = f"\n⚠️ 提醒：指定金額總和 ${specified_total} 與原始花費 ${row['total_amount']} 不同，已依您指定的金額登記，如需調整可用「修改」指令。"
        send_line_reply(
            reply_token,
            f"✅ 已依指定金額登記分攤！團單號：#{code_str}\n👥 {names}{mismatch_note}\n👉 之後可輸入「核銷 #{code_str}」開始對帳。"
        )
        return True

    # 🎯 均分模式：純 @tag（沒有金額）或直接回覆「均分」
    if real_tagged_ids:
        participants = [{"user_id": uid, "display_name": resolve_id_to_name(group_id, uid)} for uid in real_tagged_ids]
    else:
        members = get_group_member_list(group_id)
        participants = members if members else [{"user_id": payer_id, "display_name": payer_name}]

    try:
        code_str = create_split_order(group_id, payer_id, payer_name, items, participants)
    except Exception as e:
        log_error("分攤建單", e, group_id)
        send_line_reply(reply_token, "⚠️ 分攤登記失敗，請稍後再試一次。")
        return True

    names = "、".join(p["display_name"] for p in participants)
    send_line_reply(
        reply_token,
        f"✅ 已登記分攤！團單號：#{code_str}\n💰 總額：${row['total_amount']}\n👥 分攤成員：{names}\n👉 之後可輸入「核銷 #{code_str}」開始對帳。"
    )
    return True

def try_start_group_split_question(group_id: str, item_name: str, amount: int, payer_id: str, payer_name: str, reply_token: str) -> bool:
    """一般模式下輸入「品項 金額」時，若群組團單測試模式已啟用，改為詢問分攤方式而非直接記帳"""
    try:
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO pending_group_expense (group_id, payer_id, payer_name, items_json, total_amount, source)
                   VALUES (%s, %s, %s, %s, %s, 'text')
                   ON DUPLICATE KEY UPDATE
                       payer_id=VALUES(payer_id), payer_name=VALUES(payer_name),
                       items_json=VALUES(items_json), total_amount=VALUES(total_amount),
                       source='text', created_at=NOW()""",
                (group_id, payer_id, payer_name, json.dumps([{"item_name": item_name, "price": amount}], ensure_ascii=False), amount)
            )
    except Exception as e:
        log_error("待分攤花費建立", e, group_id)
        return False

    send_line_reply(
        reply_token,
        f"💰 {item_name} ${amount}，這筆怎麼記？\n"
        f"1️⃣ 回覆「均分」→ 平分給已知的群組成員\n"
        f"2️⃣ tag 出實際分攤的人（例如 @小明 @小華）→ 平分給這些人\n"
        f"3️⃣ tag 並在後面加金額（例如 @小明 100 @小華 50）→ 指定每人分攤的金額\n"
        f"4️⃣ 回覆「跳過」→ 記一般花費，不分攤"
    )
    return True

