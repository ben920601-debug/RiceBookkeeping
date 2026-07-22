"""
中控後台可調整項目的輕量快取：機器人開關、關鍵字自動回覆、敏感詞、維護訊息、AI人格。
中控後台改資料後，最多 CACHE_TTL 秒內自動生效，不需要重啟服務。
"""
import time

from app.db import db_cursor, is_db_ready

DEFAULT_SENSITIVE_KEYWORDS = ["政治", "選舉", "總統", "政黨", "戰爭", "吸毒", "賭博", "情色", "自殺", "殺人"]

DEFAULT_KEYWORD_REPLIES = {
    "電鍋": (
        "說到電鍋我最熟了，畢竟他也是我的創造者！\n"
        "他創造我之外呢，也創造了飯匙在不同地方服務大眾😄\n"
        "如有興趣，歡迎到下方點選前往IG或是找@denguword1220\n"
        "非常期待與您有更多的互動😆"
    )
}

CACHE_TTL = 5  # 秒
_cache = {"ts": 0, "bot_enabled": True, "keyword_replies": {}, "sensitive_words": [], "maintenance_message": "", "ai_persona": ""}

DEFAULT_MAINTENANCE_MESSAGE = "🤖 系統維護中，請稍後再試。"
DEFAULT_AI_PERSONA = "你是一個親切、幽默的記帳助理「記帳米粒」。"


def seed_defaults_if_empty():
    """服務第一次啟動、資料表全空時，把預設內容灌進資料庫一次，之後就都用資料庫版本"""
    try:
        with db_cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM keyword_replies")
            if cur.fetchone()["cnt"] == 0:
                for kw, reply in DEFAULT_KEYWORD_REPLIES.items():
                    cur.execute(
                        "INSERT IGNORE INTO keyword_replies (keyword, reply_text, enabled) VALUES (%s, %s, 1)",
                        (kw, reply)
                    )
            cur.execute("SELECT COUNT(*) AS cnt FROM sensitive_words")
            if cur.fetchone()["cnt"] == 0:
                for w in DEFAULT_SENSITIVE_KEYWORDS:
                    cur.execute("INSERT IGNORE INTO sensitive_words (word) VALUES (%s)", (w,))
            cur.execute("INSERT IGNORE INTO bot_settings (`key`, `value`) VALUES ('bot_enabled', '1')")
            cur.execute(
                "INSERT IGNORE INTO bot_settings (`key`, `value`) VALUES ('maintenance_message', %s)",
                (DEFAULT_MAINTENANCE_MESSAGE,)
            )
            cur.execute(
                "INSERT IGNORE INTO bot_settings (`key`, `value`) VALUES ('ai_persona', %s)",
                (DEFAULT_AI_PERSONA,)
            )
    except Exception as e:
        print(f"⚠️ 預設資料灌入失敗（若資料表尚未建立，請先執行 migration.sql）: {e}", flush=True)


def _refresh_cache_if_stale():
    if time.time() - _cache["ts"] < CACHE_TTL:
        return
    try:
        with db_cursor() as cur:
            cur.execute("SELECT `key`, `value` FROM bot_settings WHERE `key` IN ('bot_enabled', 'maintenance_message', 'ai_persona')")
            settings_rows = {r["key"]: r["value"] for r in cur.fetchall()}
            _cache["bot_enabled"] = settings_rows.get("bot_enabled", "1") == "1"
            _cache["maintenance_message"] = settings_rows.get("maintenance_message") or DEFAULT_MAINTENANCE_MESSAGE
            _cache["ai_persona"] = settings_rows.get("ai_persona") or DEFAULT_AI_PERSONA

            cur.execute("SELECT keyword, reply_text FROM keyword_replies WHERE enabled=1")
            _cache["keyword_replies"] = {r["keyword"]: r["reply_text"] for r in cur.fetchall()}

            cur.execute("SELECT word FROM sensitive_words")
            _cache["sensitive_words"] = [r["word"] for r in cur.fetchall()]

            _cache["ts"] = time.time()
    except Exception as e:
        print(f"⚠️ 設定快取更新失敗，沿用舊值: {e}", flush=True)


def is_bot_enabled() -> bool:
    _refresh_cache_if_stale()
    return _cache["bot_enabled"]


def get_keyword_replies() -> dict:
    _refresh_cache_if_stale()
    return _cache["keyword_replies"]


def get_sensitive_words() -> list:
    _refresh_cache_if_stale()
    return _cache["sensitive_words"]


def get_maintenance_message() -> str:
    _refresh_cache_if_stale()
    return _cache["maintenance_message"] or DEFAULT_MAINTENANCE_MESSAGE


def get_ai_persona() -> str:
    _refresh_cache_if_stale()
    return _cache["ai_persona"] or DEFAULT_AI_PERSONA


# 模組載入時（若資料庫已就緒）就先灌一次預設值，跟原本 main.py 的啟動順序一致
if is_db_ready():
    seed_defaults_if_empty()
