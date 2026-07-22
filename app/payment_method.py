"""
支付方式偵測（個人版限定，測試密碼開通）。

固定格式「項目 金額 支付方式」，依空白分隔判斷最後一段是否為使用者已在
監控後台登記的常用支付方式；命中就拆出來單獨記錄到 expenses.payment_method，
沒命中則照舊只解析「項目 金額」兩段，payment_method 預設為「現金」。
"""
from app.db import db_cursor
from app.logging_utils import log_error

DEFAULT_PAYMENT_METHOD = "現金"


def get_registered_methods(owner_id: str) -> list:
    try:
        with db_cursor() as cur:
            cur.execute("SELECT method_name FROM payment_methods WHERE owner_id=%s", (owner_id,))
            return [r["method_name"] for r in cur.fetchall()]
    except Exception as e:
        log_error("支付方式清單查詢", e, owner_id)
        return []


def try_extract_payment_method(text: str, owner_id: str):
    """回傳 (item_name, amount, payment_method) 或 None（格式不符合、或第三段不是已登記的支付方式時）。
    格式：項目(可能含空白) 金額 支付方式，依空白切分，最後一段須符合已登記的支付方式清單（不分大小寫）。"""
    parts = text.strip().split()
    if len(parts) < 3:
        return None
    maybe_method = parts[-1]
    maybe_amount = parts[-2]
    if not maybe_amount.isdigit():
        return None

    methods = get_registered_methods(owner_id)
    matched = next((m for m in methods if m.lower() == maybe_method.lower()), None)
    if not matched:
        return None

    item_name = " ".join(parts[:-2]).strip()
    if not item_name:
        return None
    return item_name, int(maybe_amount), matched
