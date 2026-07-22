"""
收據辨識：拍照辨識品項金額，個人版直接記入個人帳本，
群組版辨識完成後詢問團單名稱，寫入名稱、單號、品項後即完成登記
（分攤細節後續在中控後台的團單核銷頁面調整）。
"""
import re
import json
import random
import httpx
from datetime import datetime, timedelta

from pydantic import BaseModel, Field
from typing import List

from app.db import db_cursor
from app.logging_utils import log_error
from app.line_client import send_line_reply, ai_client, download_line_image
from google.genai import types

class ReceiptItemModel(BaseModel):
    item_name: str = Field(default="")
    price: int = Field(default=0)

class ReceiptExtraction(BaseModel):
    items: List[ReceiptItemModel] = Field(default_factory=list)
    total_amount: int = Field(default=0)

def extract_receipt(image_bytes: bytes) -> ReceiptExtraction:
    prompt = "請辨識這張收據或發票圖片，列出每個品項名稱與金額（整數），並提供收據總金額。若品項名稱無法辨識，可用「品項1」「品項2」等命名代替，但金額務必盡量準確判讀。"
    result = ai_client.models.generate_content(
        model='gemini-2.5-flash',
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            prompt
        ],
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=ReceiptExtraction, temperature=0.1),
    )
    return result.parsed

EDIT_ORDER_ITEM_PATTERN = re.compile(r'^修改\s+(\d{4})\s+(\d+)\s+(.+?)\s+(\d+)$')

def try_handle_edit_order_item(group_id: str, clean_text: str, reply_token: str) -> bool:
    """輸入「修改 1234 2 拿鐵 65」：修改單號1234的第2個品項為 拿鐵 $65（適用收據辨識或一般團單品項修正）"""
    m = EDIT_ORDER_ITEM_PATTERN.match(clean_text)
    if not m:
        return False
    order_code, index_str, new_item_name, new_price_str = m.groups()
    index = int(index_str)
    new_price = int(new_price_str)

    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT id FROM order_items WHERE group_id=%s AND order_code=%s ORDER BY id ASC",
                (group_id, order_code)
            )
            rows = cur.fetchall()
            if not rows or index < 1 or index > len(rows):
                send_line_reply(reply_token, f"❌ 找不到單號 #{order_code} 的第 {index} 項，請確認團單號碼與項次是否正確。")
                return True
            target_item_id = rows[index - 1]["id"]
            cur.execute(
                "UPDATE order_items SET item_name=%s, price=%s WHERE id=%s",
                (new_item_name.strip(), new_price, target_item_id)
            )
            cur.execute(
                """UPDATE orders SET total_amount=(
                       SELECT COALESCE(SUM(price),0) FROM order_items WHERE order_code=%s AND group_id=%s
                   ) WHERE order_code=%s AND group_id=%s""",
                (order_code, group_id, order_code, group_id)
            )
        send_line_reply(reply_token, f"✅ 已修改單號 #{order_code} 第 {index} 項為：{new_item_name.strip()} ${new_price}")
    except Exception as e:
        log_error("團單品項修改", e, group_id)
        send_line_reply(reply_token, "⚠️ 修改失敗，請稍後再試一次。")
    return True

def handle_receipt_image(owner_type: str, owner_id: str, is_group: bool, creator_id: str, creator_name: str, reply_token: str, message_id: str):
    try:
        image_bytes = download_line_image(message_id)
    except httpx.TimeoutException as e:
        log_error("收據圖片下載逾時", e, owner_id)
        send_line_reply(reply_token, "⚠️ 收據辨識失敗：圖片下載逾時，請確認網路狀況後重新傳送一次。")
        return
    except Exception as e:
        log_error("收據圖片下載", e, owner_id)
        send_line_reply(reply_token, "⚠️ 收據辨識失敗：圖片下載發生問題，請重新傳送一次。")
        return

    try:
        extraction = extract_receipt(image_bytes)
    except Exception as e:
        log_error("收據辨識(AI串接)", e, owner_id)
        send_line_reply(reply_token, f"⚠️ 收據辨識失敗：辨識服務暫時連不上，請稍後再試一次。\n（若持續發生，麻煩回報管理員：{str(e)[:80]}）")
        return

    if extraction is None:
        send_line_reply(reply_token, "⚠️ 收據辨識失敗：沒有取得可用的辨識結果，請稍後再試一次。")
        return

    items = [{"item_name": i.item_name or "未命名品項", "price": i.price} for i in extraction.items if i.price > 0]
    if not items:
        send_line_reply(reply_token, "⚠️ 收據辨識失敗：沒有辨識到任何品項金額，可能是照片模糊不清、角度傾斜或收據不完整，請重新拍攝清晰、平整的照片後再試一次。")
        return

    total = extraction.total_amount if extraction.total_amount > 0 else sum(i["price"] for i in items)
    item_lines = "\n".join(f"・{i['item_name']}：${i['price']}" for i in items)

    if not is_group:
        try:
            with db_cursor() as cur:
                for i in items:
                    cur.execute(
                        """INSERT INTO expenses
                           (owner_type, owner_id, record_type, amount, item, category, created_by_uid, created_by_name)
                           VALUES ('user', %s, 'expense', %s, %s, '生活雜費', %s, %s)""",
                        (owner_id, i["price"], i["item_name"], creator_id, creator_name)
                    )
            send_line_reply(reply_token, f"🧾 收據辨識完成，已記入個人帳本：\n{item_lines}\n💰 合計：${total}")
        except Exception as e:
            log_error("收據個人記帳寫入", e, owner_id)
            send_line_reply(reply_token, "⚠️ 辨識成功但寫入失敗，請稍後再試一次。")
        return

    # 群組情境：暫存後詢問團單名稱
    try:
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO pending_receipt_naming (group_id, payer_id, payer_name, items_json, total_amount)
                   VALUES (%s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE
                       payer_id=VALUES(payer_id), payer_name=VALUES(payer_name),
                       items_json=VALUES(items_json), total_amount=VALUES(total_amount), created_at=NOW()""",
                (owner_id, creator_id, creator_name, json.dumps(items, ensure_ascii=False), total)
            )
    except Exception as e:
        log_error("收據待命名建立", e, owner_id)
        send_line_reply(reply_token, "⚠️ 辨識成功但登記失敗，請稍後再試一次。")
        return

    send_line_reply(
        reply_token,
        f"🧾 收據辨識完成：\n{item_lines}\n💰 合計：${total}\n\n"
        f"請幫這筆團單取個名稱（例如：7/19 全家團購），輸入名稱後即完成登記。"
    )

RECEIPT_NAMING_TIMEOUT_MIN = 10

def try_handle_receipt_naming_reply(group_id: str, clean_text: str, reply_token: str) -> bool:
    """處理收據辨識完成後，等待使用者輸入團單名稱的回覆"""
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT payer_id, payer_name, items_json, total_amount, created_at FROM pending_receipt_naming WHERE group_id=%s",
                (group_id,)
            )
            row = cur.fetchone()
    except Exception as e:
        log_error("收據待命名查詢", e, group_id)
        return False

    if not row:
        return False
    if datetime.now() - row["created_at"] > timedelta(minutes=RECEIPT_NAMING_TIMEOUT_MIN):
        try:
            with db_cursor() as cur:
                cur.execute("DELETE FROM pending_receipt_naming WHERE group_id=%s", (group_id,))
        except Exception:
            pass
        return False  # 過期視為沒有待處理，讓訊息照正常流程走

    order_name = clean_text.strip()
    if not order_name:
        return False

    items = json.loads(row["items_json"])
    payer_id, payer_name = row["payer_id"], row["payer_name"]
    total = row["total_amount"]

    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM pending_receipt_naming WHERE group_id=%s", (group_id,))
            code_str = str(random.randint(1000, 9999))
            cur.execute(
                """INSERT INTO orders (group_id, order_code, order_name, order_date, total_amount, master_payer_id, master_payer_name)
                   VALUES (%s, %s, %s, CURDATE(), %s, %s, %s)""",
                (group_id, code_str, order_name, total, payer_id, payer_name)
            )
            order_pk = cur.lastrowid
            for i in items:
                cur.execute(
                    """INSERT INTO order_items (group_id, order_code, order_id, buyer_id, buyer_name, item_name, price)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (group_id, code_str, order_pk, payer_id, payer_name, i["item_name"], i["price"])
                )
    except Exception as e:
        log_error("收據團單寫入", e, group_id)
        send_line_reply(reply_token, "⚠️ 登記失敗，請稍後再試一次。")
        return True

    send_line_reply(
        reply_token,
        f"✅ 登記完成！團單「{order_name}」單號：#{code_str}，共 ${total}\n如需修改請至後台修正。"
    )
    return True

