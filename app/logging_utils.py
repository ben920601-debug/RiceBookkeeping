"""
統一的錯誤記錄與統計事件記錄，寫進 error_logs / stat_events 供中控後台檢視。
"""
from app.db import db_cursor, is_db_ready


def log_stat_event(event_type: str, target_id: str = None):
    """統計用事件記錄：機器人回覆則數、敏感詞觸發則數，供中控後台總覽頁使用"""
    if not is_db_ready():
        return
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO stat_events (event_type, target_id) VALUES (%s, %s)",
                (event_type, target_id)
            )
    except Exception:
        pass  # 統計記錄失敗不影響主流程


def log_error(source: str, message, target_id: str = None):
    """統一錯誤記錄：畫面上印出來方便看 log，同時寫進 error_logs 表供中控後台檢視"""
    print(f"❌ [{source}] {message}", flush=True)
    if not is_db_ready():
        return
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO error_logs (source, message, target_id) VALUES (%s, %s, %s)",
                (source, str(message)[:2000], target_id)
            )
    except Exception:
        pass  # 記錄失敗就算了，不能讓記錄本身又炸掉主流程
