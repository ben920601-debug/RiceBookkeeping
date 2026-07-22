"""
測試限定功能（旅行模式／收據辨識／繳費功能／存錢功能／支付方式）的密碼驗證與單一模式互斥機制。
（群組團單分攤已於 V1.7 下放為群組主要功能，不再需要密碼開通，因此不在這份清單裡。）

同一時間同一個 owner（個人或群組）只能有一個模式是開啟的，避免多個模式
同時監聽訊息造成資料輸入互相干擾。這個模組是這幾個功能模組共用的「守門員」，
只直接操作它們的暫存狀態表（trip_sessions / pending_group_expense /
pending_receipt_naming），不 import 那幾個模組本身，避免循環依賴。
"""
from datetime import datetime, timedelta
from typing import Optional

from app.config import TEST_MODE_PASSWORD, TEST_MODE_HOURS
from app.db import db_cursor, is_db_ready
from app.logging_utils import log_error
from app.line_client import send_line_reply
from app.feature_switches import is_feature_enabled, get_feature_maintenance_message

TEST_FEATURE_KEYWORDS = {
    "旅行模式": "itinerary",
    "收據辨識": "receipt_ocr",
    "繳費功能": "bill_payment",
    "存錢功能": "savings",
    "支付方式": "payment_method",
}
TEST_FEATURE_LABELS = {v: k for k, v in TEST_FEATURE_KEYWORDS.items()}
PENDING_PASSWORD_TIMEOUT_MIN = 5  # 密碼請求超過此時間未輸入就視為過期，避免使用者很久後亂打字誤觸


def is_test_mode_active(owner_type: str, owner_id: str, feature: str) -> bool:
    if not is_db_ready() or not TEST_MODE_PASSWORD:
        return False
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT expires_at FROM test_mode_sessions WHERE owner_type=%s AND owner_id=%s AND feature=%s",
                (owner_type, owner_id, feature)
            )
            row = cur.fetchone()
            return bool(row and row["expires_at"] > datetime.now())
    except Exception as e:
        log_error("測試模式檢查", e, owner_id)
        return False


def set_pending_password(owner_type: str, owner_id: str, feature: str):
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO test_mode_pending (owner_type, owner_id, feature, requested_at)
               VALUES (%s, %s, %s, NOW())
               ON DUPLICATE KEY UPDATE feature=VALUES(feature), requested_at=NOW()""",
            (owner_type, owner_id, feature)
        )


def get_pending_feature(owner_type: str, owner_id: str):
    """取得等待驗證中的功能；若已超過逾時時間則視為過期並自動清除"""
    with db_cursor() as cur:
        cur.execute(
            "SELECT feature, requested_at FROM test_mode_pending WHERE owner_type=%s AND owner_id=%s",
            (owner_type, owner_id)
        )
        row = cur.fetchone()
        if not row:
            return None
        if datetime.now() - row["requested_at"] > timedelta(minutes=PENDING_PASSWORD_TIMEOUT_MIN):
            cur.execute("DELETE FROM test_mode_pending WHERE owner_type=%s AND owner_id=%s", (owner_type, owner_id))
            return None
        return row["feature"]


def clear_pending_password(owner_type: str, owner_id: str):
    with db_cursor() as cur:
        cur.execute("DELETE FROM test_mode_pending WHERE owner_type=%s AND owner_id=%s", (owner_type, owner_id))


def activate_test_mode(owner_type: str, owner_id: str, feature: str):
    """開啟指定測試模式。同一時間同一個 owner 只能有一個模式是開啟的。
    回傳 (expires, closed_feature)：closed_feature 是被自動關閉的舊模式（沒有則為 None）。"""
    expires = datetime.now() + timedelta(hours=TEST_MODE_HOURS)
    closed_feature = None
    with db_cursor() as cur:
        cur.execute(
            "SELECT feature FROM test_mode_sessions WHERE owner_type=%s AND owner_id=%s AND feature<>%s AND expires_at > NOW()",
            (owner_type, owner_id, feature)
        )
        others = cur.fetchall()
        if others:
            closed_feature = others[0]["feature"]
            cur.execute("DELETE FROM test_mode_sessions WHERE owner_type=%s AND owner_id=%s AND feature<>%s", (owner_type, owner_id, feature))
            # 清除其他模式殘留的對話中狀態，避免切換模式後舊的待回覆狀態還卡著
            cur.execute("DELETE FROM trip_sessions WHERE owner_type=%s AND owner_id=%s", (owner_type, owner_id))
            cur.execute("DELETE FROM pending_group_expense WHERE group_id=%s", (owner_id,))
            cur.execute("DELETE FROM pending_receipt_naming WHERE group_id=%s", (owner_id,))

        cur.execute(
            """INSERT INTO test_mode_sessions (owner_type, owner_id, feature, expires_at)
               VALUES (%s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE expires_at=VALUES(expires_at)""",
            (owner_type, owner_id, feature, expires)
        )
    return expires, closed_feature


def has_other_active_test_mode(owner_type: str, owner_id: str, feature: str) -> Optional[str]:
    """查詢是否已經有『其他』模式正在開啟中，回傳該模式名稱；沒有則回傳 None"""
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT feature FROM test_mode_sessions WHERE owner_type=%s AND owner_id=%s AND feature<>%s AND expires_at > NOW() LIMIT 1",
                (owner_type, owner_id, feature)
            )
            row = cur.fetchone()
            return row["feature"] if row else None
    except Exception as e:
        log_error("模式互斥檢查", e, owner_id)
        return None


def try_handle_test_mode_gate(owner_type: str, owner_id: str, clean_text: str, reply_token: str) -> bool:
    """
    統一處理測試功能的密碼流程，回傳 True 代表這則訊息已經被這一層攔截處理完畢，
    外層 handle_text_message 應該直接 return，不要再往下跑其他邏輯。
    """
    if not is_db_ready():
        return False

    # 1) 若正在等待密碼輸入，這一則訊息就當作密碼本身來比對
    try:
        pending_feature = get_pending_feature(owner_type, owner_id)
    except Exception as e:
        log_error("待驗證密碼查詢", e, owner_id)
        pending_feature = None

    if pending_feature:
        if not TEST_MODE_PASSWORD:
            clear_pending_password(owner_type, owner_id)
            send_line_reply(reply_token, "⚠️ 尚未設定測試密碼，請聯絡管理員設定 TEST_MODE_PASSWORD 後再試。")
            return True
        if clean_text.strip() == TEST_MODE_PASSWORD:
            clear_pending_password(owner_type, owner_id)
            expires, closed_feature = activate_test_mode(owner_type, owner_id, pending_feature)
            label = TEST_FEATURE_LABELS.get(pending_feature, pending_feature)
            closed_note = ""
            if closed_feature:
                closed_label = TEST_FEATURE_LABELS.get(closed_feature, closed_feature)
                closed_note = f"\n（同一時間僅能開啟一種模式，已自動關閉原本開啟中的「{closed_label}」）"
            send_line_reply(
                reply_token,
                f"✅「{label}」測試模式已啟用！\n⏳ 效期至：{expires.strftime('%m/%d %H:%M')}（{TEST_MODE_HOURS} 小時後自動關閉）{closed_note}"
            )
        else:
            clear_pending_password(owner_type, owner_id)
            send_line_reply(reply_token, "❌ 密碼錯誤，測試模式未開啟。若要重試請重新輸入功能關鍵字。")
        return True

    # 2) 沒有等待中的密碼請求時，檢查這則訊息是不是「觸發詞」
    matched_feature = None
    for kw, feature in TEST_FEATURE_KEYWORDS.items():
        if kw in clean_text:
            matched_feature = feature
            break
    if not matched_feature:
        return False

    if not is_feature_enabled(matched_feature):
        send_line_reply(reply_token, get_feature_maintenance_message(matched_feature))
        return True

    if is_test_mode_active(owner_type, owner_id, matched_feature):
        if matched_feature == "itinerary":
            return False  # 已開通的旅行模式再次輸入觸發詞，交給旅行流程處理（開始新的旅行規劃）
        label = TEST_FEATURE_LABELS[matched_feature]
        send_line_reply(reply_token, f"ℹ️「{label}」測試模式目前已經是啟用中的狀態囉！")
        return True

    if not TEST_MODE_PASSWORD:
        send_line_reply(reply_token, "⚠️ 尚未設定測試密碼，請聯絡管理員設定 TEST_MODE_PASSWORD 後再試。")
        return True

    try:
        set_pending_password(owner_type, owner_id, matched_feature)
    except Exception as e:
        log_error("設定待驗證密碼", e, owner_id)
        return True

    label = TEST_FEATURE_LABELS[matched_feature]
    send_line_reply(reply_token, f"🔐「{label}」為測試限定功能，請直接輸入測試密碼以開通（{PENDING_PASSWORD_TIMEOUT_MIN} 分鐘內有效）：")
    return True
