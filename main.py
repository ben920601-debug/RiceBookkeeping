import os
import re
import json
import math
import random
import time
import asyncio
import httpx
import certifi
from datetime import datetime, timedelta
from contextlib import contextmanager
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Literal, List, Optional
from fastapi.middleware.cors import CORSMiddleware

# LINE SDK v3
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent

# Google GenAI & MySQL
from google import genai
from google.genai import types
import pymysql
from pymysql.cursors import DictCursor
from dbutils.pooled_db import PooledDB  # 🔌 連線池：取代每次手動開關連線，避免逾時被斷線與連線數暴增

from dotenv import load_dotenv

load_dotenv()

# ==========================================
# 🔒 SSL 憑證修正（常見於 macOS：Python 找不到系統根憑證）
# ------------------------------------------
# 用 certifi 提供的憑證包，直接指定給整個程式（含 LINE SDK、httpx）使用，
# 不用再手動 export SSL_CERT_FILE，每次開新終端機都要重設。
# ==========================================
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

app = FastAPI(title="記帳米粒 ｜ V1.3 MySQL 版")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# ==========================================
# ⚙️ 1. 核心客戶端與資料庫初始化
# ==========================================
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 🧪 測試限定功能（行程模式／群組團單／收據辨識）共用的驗證密碼與開通時數
TEST_MODE_PASSWORD = os.getenv("TEST_MODE_PASSWORD", "")
TEST_MODE_HOURS = int(os.getenv("TEST_MODE_HOURS", "16"))

MY_LIFF_ID = "2010446205-W1G1WDQQ" 

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER"),
    "password": os.getenv("MYSQL_PASSWORD"),
    "database": os.getenv("MYSQL_DATABASE", "jizhang_mili"),
    "charset": "utf8mb4",
    "cursorclass": DictCursor,
    "autocommit": True,
}

# ==========================================
# 🔌 連線池（DBUtils PooledDB）
# ------------------------------------------
# 舊版每次都 pymysql.connect() 再手動 close()，長時間閒置或雲端資料庫
# 主動斷線時容易出現「連線逾時被斷開」或短時間內連線數暴增的問題。
# 改用連線池後：
#   - mincached/maxcached：常駐可回收的連線，減少重複建立連線的開銷
#   - maxconnections：連線數上限，避免暴增拖垮資料庫
#   - ping=1：每次向池子借用連線時都會自動檢查連線是否還活著，
#             失效就自動重連，從根本解決「連線逾時被斷開」的問題
# ==========================================
DB_POOL = None

def _init_pool():
    global DB_POOL
    DB_POOL = PooledDB(
        creator=pymysql,
        mincached=2,
        maxcached=5,
        maxconnections=20,
        blocking=True,
        ping=1,
        **MYSQL_CONFIG,
    )

def get_db_connection():
    """從連線池借用一條連線；用完呼叫 .close() 只是歸還給池子，不會真的斷線"""
    if DB_POOL is None:
        _init_pool()
    return DB_POOL.connection()

@contextmanager
def db_cursor():
    """統一管理連線與游標的 context manager，離開時自動歸還連線給連線池"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()

DB_READY = False
try:
    _init_pool()
    with db_cursor() as _cur:
        _cur.execute("SELECT 1")
    DB_READY = True
    print("🔥 [DATABASE] MySQL 連線池就位！", flush=True)
except Exception as e:
    DB_READY = False
    print(f"❌ [DATABASE] MySQL 連線初始化異常: {e}", flush=True)

# ==========================================
# 🛡️ 2. 全域型別設定
# ==========================================
# 🚀 V1.3 改版：SENSITIVE_KEYWORDS / SPECIFIC_KEYWORDS 不再寫死在程式碼裡，
# 改成從資料庫的 sensitive_words / keyword_replies 表讀取，
# 並在中控後台開放線上新增、編輯、刪除 —— 修改內容不用再改程式碼、也不用重新部署。
# 這裡保留「初次啟動、資料庫還沒有任何資料時」的預設值，僅在資料表為空時會自動灌入一次。
DEFAULT_SENSITIVE_KEYWORDS = ["政治", "選舉", "總統", "政黨", "戰爭", "吸毒", "賭博", "情色", "自殺", "殺人"]

DEFAULT_KEYWORD_REPLIES = {
    "電鍋": (
        "說到電鍋我最熟了，畢竟他也是我的創造者！\n"
        "他創造我之外呢，也創造了飯匙在不同地方服務大眾😄\n"
        "如有興趣，歡迎到下方點選前往IG或是找@denguword1220\n"
        "非常期待與您有更多的互動😆"
    )
}

# ------------------------------------------
# 🗄️ 輕量快取：避免每一則訊息都去查資料庫
# 中控後台修改資料後，最多 CACHE_TTL 秒內會自動生效，不需要重啟服務
# ------------------------------------------
CACHE_TTL = 5  # 秒（原本15秒，縮短以減少「關閉機器人」等設定生效的延遲）
_cache = {"ts": 0, "bot_enabled": True, "keyword_replies": {}, "sensitive_words": [], "maintenance_message": "", "ai_persona": ""}

DEFAULT_MAINTENANCE_MESSAGE = "🤖 系統維護中，請稍後再試。"
DEFAULT_AI_PERSONA = "你是一個親切、幽默的記帳助理「記帳米粒」。"

def _seed_defaults_if_empty():
    """服務第一次啟動、資料表全空時，把原本寫死的內容灌進資料庫一次，之後就都用資料庫版本"""
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

def log_stat_event(event_type: str, target_id: str = None):
    """統計用事件記錄：機器人回覆則數、敏感詞觸發則數，供中控後台總覽頁使用"""
    if not DB_READY:
        return
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO stat_events (event_type, target_id) VALUES (%s, %s)",
                (event_type, target_id)
            )
    except Exception:
        pass  # 統計記錄失敗不影響主流程

def log_error(source: str, message: str, target_id: str = None):
    """統一錯誤記錄：畫面上印出來方便看 log，同時寫進 error_logs 表供中控後台檢視"""
    print(f"❌ [{source}] {message}", flush=True)
    if not DB_READY:
        return
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO error_logs (source, message, target_id) VALUES (%s, %s, %s)",
                (source, str(message)[:2000], target_id)
            )
    except Exception:
        pass  # 記錄失敗就算了，不能讓記錄本身又炸掉主流程

# ※ 中控後台的登入驗證（帳密、JWT）已搬到獨立專案 admin-panel，這裡不再需要。

# 所有快取/預設值相關函式都定義完成後，這裡才是真正安全的呼叫時機
if DB_READY:
    _seed_defaults_if_empty()

class SingleRecord(BaseModel):
    record_type: Literal["expense", "income"] = Field(default="expense")
    amount: int = Field(default=0)
    item: str = Field(default="")
    category: str = Field(default="生活雜費")

class SingleSettlement(BaseModel):
    payer_name: str = Field(default="")
    receiver_name: str = Field(default="")
    amount: int = Field(default=0)

class GroupOrderItem(BaseModel):
    buyer_name: str = Field(default="")
    item_name: str = Field(default="")
    price: int = Field(default=0)

class SuperRouter(BaseModel):
    intent: Literal["record", "chat", "order_start", "order_end", "order_item", "settle_start", "settle_pay", "settle_end"] = Field(
        description="核心意圖分流"
    )
    records: Optional[List[SingleRecord]] = Field(default_factory=list)
    settlement: Optional[SingleSettlement] = Field(default=None)
    order_items: Optional[List[GroupOrderItem]] = Field(default_factory=list)
    ai_reply: Optional[str] = Field(default="", description="與使用者的聊天回應")

# ==========================================
# ⚡ 3. 核心工具組
# ==========================================
def send_line_reply(reply_token: str, text: str):
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
            )
        log_stat_event("reply")
    except Exception as e:
        log_error("LINE回覆", e)

def get_real_mentions(event) -> list:
    """🎯 核心修復：過濾掉機器人自身的 Tag，只抓取真實成員的 ID"""
    real_tagged_ids = []
    mention = getattr(event.message, "mention", None)
    if mention and mention.mentionees:
        text = getattr(event.message, "text", "")
        for m in mention.mentionees:
            u_id = getattr(m, "user_id", None)
            if u_id:
                try:
                    tagged_text = text[m.index : m.index + m.length]
                    if "米粒" in tagged_text:
                        continue
                except:
                    pass
                real_tagged_ids.append(u_id)
    return real_tagged_ids

def get_mentions_with_amounts(event) -> list:
    """回傳 [{"user_id":..., "amount": int|None}, ...]。
    amount 是該次 @tag 後方緊接著的數字（例如「@小明 100」），沒有寫金額則為 None。
    用於分攤功能判斷使用者是要「指定金額」還是單純「tag出要平分的人」。"""
    results = []
    mention = getattr(event.message, "mention", None)
    if not (mention and mention.mentionees):
        return results
    text = getattr(event.message, "text", "")
    for m in mention.mentionees:
        u_id = getattr(m, "user_id", None)
        if not u_id:
            continue
        try:
            tagged_text = text[m.index : m.index + m.length]
            if "米粒" in tagged_text:
                continue
        except Exception:
            pass
        amount = None
        try:
            after = text[m.index + m.length: m.index + m.length + 15]
            amt_match = re.match(r'\s*\$?(\d+)', after)
            if amt_match:
                amount = int(amt_match.group(1))
        except Exception:
            pass
        results.append({"user_id": u_id, "amount": amount})
    return results

def fetch_line_profile_name(user_id: str, target_id: str = None) -> str:
    """🎯 核心修復：升級為群組成員 API，未加好友也能抓到真實暱稱"""
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    
    if target_id:
        url = None
        if target_id.startswith("C"):
            url = f"https://api.line.me/v2/bot/group/{target_id}/member/{user_id}"
        elif target_id.startswith("R"):
            url = f"https://api.line.me/v2/bot/room/{target_id}/member/{user_id}"
            
        if url:
            try:
                res = httpx.get(url, headers=headers, timeout=5.0, follow_redirects=True, verify=certifi.where())
                if res.status_code == 200:
                    return res.json().get("displayName", f"成員({user_id[:4]})")
                else:
                    print(f"⚠️ LINE API 回傳狀態碼: {res.status_code}, 網址: {res.url}", flush=True)
            except Exception as e:
                print(f"⚠️ 請求群組 API 異常: {e}", flush=True)
            
    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    try:
        res = httpx.get(url, headers=headers, timeout=5.0, follow_redirects=True, verify=certifi.where())
        if res.status_code == 200:
            return res.json().get("displayName", f"成員({user_id[:4]})")
    except Exception as e:
        print(f"⚠️ 請求全域個人資料 API 異常: {e}", flush=True)
        
    return f"成員({user_id[:4]})"

def resolve_id_to_name(target_id: str, user_id: str) -> str:
    """查詢群組成員暱稱快取，查不到就打 LINE API 並寫回快取表(對應原本 group_members 子集合)"""
    if not DB_READY or not user_id:
        return "群組夥伴"
    if not user_id.startswith("U"):
        return user_id

    # 個人聊天情境：target_id 是使用者自己的 U-id，不是真正的群組 ID，
    # groups 表裡不會有這筆資料，直接呼叫 LINE API 取得暱稱即可，不寫入 group_members 快取
    if not (target_id.startswith("C") or target_id.startswith("R")):
        return fetch_line_profile_name(user_id, None)

    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT display_name FROM group_members WHERE group_id=%s AND user_id=%s",
                (target_id, user_id)
            )
            row = cur.fetchone()
            if row:
                return row["display_name"]

            real_name = fetch_line_profile_name(user_id, target_id)
            cur.execute(
                """INSERT INTO group_members (group_id, user_id, display_name)
                   VALUES (%s, %s, %s)
                   ON DUPLICATE KEY UPDATE display_name = VALUES(display_name)""",
                (target_id, user_id, real_name)
            )
            return real_name
    except Exception as e:
        print(f"⚠️ resolve_id_to_name 查詢異常: {e}", flush=True)
    return f"成員({user_id[:4]})"

def push_line_message(target_id: str, text: str):
    """主動推播（非回覆使用者訊息，用於行程提醒等背景排程主動發起的通知）"""
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=target_id, messages=[TextMessage(text=text)])
            )
        log_stat_event("push", target_id)
    except Exception as e:
        log_error("LINE主動推播", e, target_id)

# ==========================================
# 🧪 5. 測試限定功能：密碼驗證機制
# ------------------------------------------
# 「行程模式」「群組團單」「收據辨識」三個功能目前僅供測試，
# 觸發對應關鍵字後，機器人會要求輸入密碼，密碼比對成功後
# 針對該 owner（個人或群組）開啟 TEST_MODE_HOURS 小時的功能授權，
# 效期一到，下次判斷時就會自動視為未開通，不需要額外排程清除。
# ==========================================
TEST_FEATURE_KEYWORDS = {
    "旅行模式": "itinerary",
    "群組團單": "group_split",
    "收據辨識": "receipt_ocr",
}
TEST_FEATURE_LABELS = {v: k for k, v in TEST_FEATURE_KEYWORDS.items()}
PENDING_PASSWORD_TIMEOUT_MIN = 5  # 密碼請求超過此時間未輸入就視為過期，避免使用者很久後亂打字誤觸

def is_test_mode_active(owner_type: str, owner_id: str, feature: str) -> bool:
    if not DB_READY or not TEST_MODE_PASSWORD:
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
    expires = datetime.now() + timedelta(hours=TEST_MODE_HOURS)
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO test_mode_sessions (owner_type, owner_id, feature, expires_at)
               VALUES (%s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE expires_at=VALUES(expires_at)""",
            (owner_type, owner_id, feature, expires)
        )
    return expires

def try_handle_test_mode_gate(owner_type: str, owner_id: str, clean_text: str, reply_token: str) -> bool:
    """
    統一處理測試功能的密碼流程，回傳 True 代表這則訊息已經被這一層攔截處理完畢，
    外層 handle_text_message 應該直接 return，不要再往下跑其他邏輯。
    """
    if not DB_READY:
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
            expires = activate_test_mode(owner_type, owner_id, pending_feature)
            label = TEST_FEATURE_LABELS.get(pending_feature, pending_feature)
            send_line_reply(
                reply_token,
                f"✅「{label}」測試模式已啟用！\n⏳ 效期至：{expires.strftime('%m/%d %H:%M')}（{TEST_MODE_HOURS} 小時後自動關閉）"
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

# ==========================================
# 🗺️ 6. 行程模式：地理編碼與通勤估算
# ------------------------------------------
# 測試階段先用免費、免申請金鑰的 OpenStreetMap Nominatim 做地理編碼，
# 搭配 Haversine 公式算「直線距離」概略估算通勤時間 —— 不考慮實際路網、
# 路況、單行道等因素，僅供測試流程驗證使用。未來要提升精準度，
# 可以換成 Google Maps Distance Matrix API（需要金鑰與計費帳號）。
# ==========================================
def geocode_location(location_name: str):
    """回傳 (lat, lon)；查不到則回傳 (None, None)"""
    try:
        headers = {"User-Agent": "RiceBookkeepingBot/1.0 (test-mode itinerary feature)"}
        res = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": location_name, "format": "json", "limit": 1, "countrycodes": "tw"},
            headers=headers, timeout=6.0, verify=certifi.where()
        )
        data = res.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        log_error("地理編碼", e)
    return None, None

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

def estimate_travel_minutes(distance_km: float, mode: str = "drive") -> int:
    """概略估算：市區均速抓開車 30km/h、步行 5km/h，僅供測試參考"""
    speed = 30 if mode == "drive" else 5
    return max(1, round((distance_km / speed) * 60))

# ==========================================
# 🧳 7. 旅行模式：多輪對話式行程規劃
# ------------------------------------------
# 流程：旅行模式 → 問出發時間 → 問回程時間 → 建立草案
#      → 逐筆輸入「日期時間 地點」→ 解析(regex優先,失敗交AI) → 地點確認 → 登記
#      → 輸入「結束」→ AI給路線總覽建議 → 使用者「確定」或描述修改內容
#      → 確定後以出發日期作為旅行單號，之後可用「旅行修改 單號」重新進入編輯
# ==========================================
DATETIME_PATTERN_COLON = re.compile(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})')
DATETIME_PATTERN_COMPACT = re.compile(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{2})(\d{2})(?=\s|$)')
TRIP_MODIFY_PATTERN = re.compile(r'^旅行修改\s*[+＋]?\s*(\d{6,20}(?:-\d+)?)$')

def parse_datetime_prefix(text: str):
    """嘗試從文字開頭解析日期時間（支援「YYYY-MM-DD HH:MM」與「YYYY/M/D HHMM」兩種格式），
    回傳 (datetime, 剩餘文字)；解析不到回傳 (None, None)"""
    text = text.strip()
    m = DATETIME_PATTERN_COLON.match(text)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        try:
            return datetime(y, mo, d, h, mi), text[m.end():].strip()
        except ValueError:
            return None, None
    m = DATETIME_PATTERN_COMPACT.match(text)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        try:
            return datetime(y, mo, d, h, mi), text[m.end():].strip()
        except ValueError:
            return None, None
    return None, None

class SimpleDateTimeExtraction(BaseModel):
    datetime_str: str = Field(default="")
    recognized: bool = Field(default=False)

def ai_extract_datetime_only(text: str):
    """交由 Gemini 判讀日期時間（規則沒抓到格式時的備援）"""
    prompt = (
        f"請從這段文字判讀出一個日期時間，用「YYYY-MM-DD HH:MM」24小時制格式回傳。"
        f"若文字只提到時間、沒提到日期，可合理推斷為最近的未來日期。若完全無法判讀請將 recognized 設為 false。\n"
        f"目前時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n文字：『{text}』"
    )
    try:
        result = ai_client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SimpleDateTimeExtraction, temperature=0.1),
        ).parsed
        if result and result.recognized and result.datetime_str:
            try:
                return datetime.strptime(result.datetime_str, "%Y-%m-%d %H:%M")
            except Exception:
                return None
    except Exception as e:
        log_error("AI日期時間辨識", e)
    return None

class ItineraryItemExtraction(BaseModel):
    datetime_str: str = Field(default="")
    location_name: str = Field(default="")
    recognized: bool = Field(default=False)

def ai_extract_itinerary_item(text: str):
    """交由 Gemini 同時判讀「日期時間」與「地點」（規則沒抓到格式時的備援，也用於處理 Google 地圖連結）"""
    prompt = (
        f"請從這段文字中判讀出「日期時間」與「地點」。日期時間請用「YYYY-MM-DD HH:MM」24小時制格式回傳；"
        f"若文字中包含 Google 地圖連結或不完整地址，請盡量判斷出實際地點名稱（店家名稱、地標或地址）。"
        f"若完全無法判讀日期時間，請將 recognized 設為 false。\n"
        f"目前時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n文字：『{text}』"
    )
    try:
        result = ai_client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=ItineraryItemExtraction, temperature=0.1),
        ).parsed
        if result and result.recognized:
            dt = None
            try:
                dt = datetime.strptime(result.datetime_str, "%Y-%m-%d %H:%M")
            except Exception:
                dt = None
            return dt, (result.location_name.strip() if result.location_name else None)
    except Exception as e:
        log_error("AI行程項目辨識", e)
    return None, None

def is_maps_url(text: str) -> bool:
    return bool(re.search(r'(maps\.google|goo\.gl/maps|maps\.app\.goo\.gl|google\.com/maps)', text, re.IGNORECASE))

def try_extract_latlon_from_url(text: str):
    """Google地圖連結常見的座標格式：.../@25.033,121.565,17z 或 ?q=25.033,121.565"""
    m = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', text)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r'[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)', text)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None

# --- 旅行對話狀態（trip_sessions）存取 ---
def get_trip_session(owner_type: str, owner_id: str):
    try:
        with db_cursor() as cur:
            cur.execute("SELECT * FROM trip_sessions WHERE owner_type=%s AND owner_id=%s", (owner_type, owner_id))
            return cur.fetchone()
    except Exception as e:
        log_error("旅行對話狀態查詢", e, owner_id)
        return None

def set_trip_session(owner_type: str, owner_id: str, stage: str, trip_id=None, draft: dict = None):
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO trip_sessions (owner_type, owner_id, stage, trip_id, draft_json)
               VALUES (%s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE stage=VALUES(stage), trip_id=VALUES(trip_id), draft_json=VALUES(draft_json), updated_at=NOW()""",
            (owner_type, owner_id, stage, trip_id, json.dumps(draft, ensure_ascii=False, default=str) if draft is not None else None)
        )

def clear_trip_session(owner_type: str, owner_id: str):
    with db_cursor() as cur:
        cur.execute("DELETE FROM trip_sessions WHERE owner_type=%s AND owner_id=%s", (owner_type, owner_id))

def build_trip_summary_text(trip_id: int) -> str:
    with db_cursor() as cur:
        cur.execute(
            "SELECT scheduled_at, location_name FROM itineraries WHERE trip_id=%s ORDER BY scheduled_at ASC",
            (trip_id,)
        )
        rows = cur.fetchall()
    if not rows:
        return "（目前尚未登記任何地點）"
    return "\n".join(f"{i+1}. {r['scheduled_at'].strftime('%m/%d %H:%M')}　{r['location_name']}" for i, r in enumerate(rows))

def ai_review_trip_route(departure_at: datetime, return_at: datetime, summary_text: str) -> str:
    prompt = (
        f"這是一趟旅行的行程安排，出發時間：{departure_at.strftime('%Y-%m-%d %H:%M')}，"
        f"回程時間：{return_at.strftime('%Y-%m-%d %H:%M')}。行程列表：\n{summary_text}\n\n"
        f"請用簡短親切的口吻（3-5句話內），評論這個行程安排是否合理（例如時間會不會太趕、順序是否需要調整），"
        f"若有明顯問題請具體指出，沒有問題就給予正面回饋即可。"
    )
    try:
        result = ai_client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        return (result.text or "").strip() or "行程看起來安排得不錯！"
    except Exception as e:
        log_error("AI路線建議", e)
        return "（AI路線建議暫時無法取得，不影響行程登記）"

class TripModification(BaseModel):
    action: Literal["edit", "delete", "add", "update_times", "unclear"] = Field(default="unclear")
    target_index: Optional[int] = Field(default=None)
    new_datetime_str: str = Field(default="")
    new_location: str = Field(default="")
    new_departure_str: str = Field(default="")
    new_return_str: str = Field(default="")

def ai_apply_trip_modification(summary_text: str, departure_at: datetime, return_at: datetime, user_text: str) -> TripModification:
    prompt = (
        f"目前旅行行程如下（編號. 時間 地點）：\n{summary_text}\n"
        f"出發時間：{departure_at.strftime('%Y-%m-%d %H:%M')}，回程時間：{return_at.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"使用者想這樣修改：『{user_text}』\n\n"
        f"請判斷這是以下哪一種操作，並填入對應欄位（用不到的欄位留空字串）：\n"
        f"- edit：修改某一項的時間或地點（填 target_index，以及要改的 new_datetime_str 和/或 new_location）\n"
        f"- delete：刪除某一項（填 target_index）\n"
        f"- add：新增一項（填 new_datetime_str 與 new_location）\n"
        f"- update_times：修改整趟旅行的出發/回程時間（填 new_departure_str 和/或 new_return_str）\n"
        f"- unclear：看不懂使用者想做什麼\n"
        f"日期時間格式一律用「YYYY-MM-DD HH:MM」24小時制。"
    )
    try:
        result = ai_client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=TripModification, temperature=0.1),
        ).parsed
        return result or TripModification()
    except Exception as e:
        log_error("AI旅行修改判讀", e)
        return TripModification()

def generate_trip_code(owner_type: str, owner_id: str, departure_at: datetime) -> str:
    base = departure_at.strftime("%Y%m%d")
    with db_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM trips WHERE owner_type=%s AND owner_id=%s AND trip_code LIKE %s",
            (owner_type, owner_id, f"{base}%")
        )
        cnt = cur.fetchone()["cnt"]
    return base if cnt == 0 else f"{base}-{cnt + 1}"

def send_trip_review(owner_type: str, owner_id: str, trip: dict, reply_token: str):
    summary = build_trip_summary_text(trip["id"])
    ai_summary = ai_review_trip_route(trip["departure_at"], trip["return_at"], summary)
    try:
        with db_cursor() as cur:
            cur.execute("UPDATE trips SET ai_route_summary=%s WHERE id=%s", (ai_summary, trip["id"]))
    except Exception as e:
        log_error("旅行路線建議寫入", e, owner_id)
    set_trip_session(owner_type, owner_id, "pending_review", trip_id=trip["id"])
    send_line_reply(
        reply_token,
        f"🗺️ 【行程總覽】\n{summary}\n\n🤖 AI建議：{ai_summary}\n\n"
        f"這樣安排OK嗎？回覆「確定」完成規劃並取得旅行單號，或直接輸入想修改的內容（例如：把第2項改成15:00 台北101、刪除第3項）。"
    )

def try_handle_trip_flow(owner_type: str, owner_id: str, creator_id: str, clean_text: str, reply_token: str) -> bool:
    """旅行模式的多輪對話總路由。回傳 True 代表這則訊息已被旅行流程處理完畢。"""
    if not is_test_mode_active(owner_type, owner_id, "itinerary"):
        return False

    session = get_trip_session(owner_type, owner_id)

    # 隨時可用「取消旅行」中止目前的規劃對話
    if session and "取消旅行" in clean_text:
        if session.get("trip_id") and session["stage"] != "pending_review":
            try:
                with db_cursor() as cur:
                    cur.execute("DELETE FROM trips WHERE id=%s AND status='collecting'", (session["trip_id"],))
            except Exception as e:
                log_error("旅行草案刪除", e, owner_id)
        clear_trip_session(owner_type, owner_id)
        send_line_reply(reply_token, "🚫 已取消本次旅行規劃。")
        return True

    # 「旅行修改 單號」：重新進入某趟已完成旅行的編輯
    m = TRIP_MODIFY_PATTERN.match(clean_text)
    if m and not session:
        trip_code = m.group(1)
        try:
            with db_cursor() as cur:
                cur.execute(
                    "SELECT * FROM trips WHERE owner_type=%s AND owner_id=%s AND trip_code=%s",
                    (owner_type, owner_id, trip_code)
                )
                trip = cur.fetchone()
        except Exception as e:
            log_error("旅行修改查詢", e, owner_id)
            send_line_reply(reply_token, "⚠️ 查詢失敗，請稍後再試一次。")
            return True
        if not trip:
            send_line_reply(reply_token, f"❌ 找不到旅行單號 #{trip_code}，請確認號碼是否正確。")
            return True
        send_trip_review(owner_type, owner_id, trip, reply_token)
        return True

    # 觸發詞：開始一趟新旅行（前提：目前沒有進行中的規劃對話）
    if "旅行模式" in clean_text and not session:
        set_trip_session(owner_type, owner_id, "pending_departure")
        send_line_reply(
            reply_token,
            "🧳 開始規劃新旅行！請問這趟旅行的出發時間？\n"
            "（格式：2026-07-19 19:00 或 2026/7/19 1900，也可以直接描述，我會請AI協助判讀）\n"
            "隨時可輸入「取消旅行」中止規劃。"
        )
        return True

    if not session:
        return False

    stage = session["stage"]

    # --- Stage 1：等待出發時間 ---
    if stage == "pending_departure":
        dt, _ = parse_datetime_prefix(clean_text)
        if dt is None:
            dt = ai_extract_datetime_only(clean_text)
        if dt is None:
            send_line_reply(reply_token, "⚠️ 看不懂這個時間，請用「YYYY-MM-DD HH:MM」格式再試一次，例如：2026-07-19 19:00")
            return True
        set_trip_session(owner_type, owner_id, "pending_return", draft={"departure_at": dt.isoformat()})
        send_line_reply(reply_token, f"📅 出發時間：{dt.strftime('%Y-%m-%d %H:%M')}\n請問預計的回程時間？")
        return True

    # --- Stage 2：等待回程時間 ---
    if stage == "pending_return":
        draft = json.loads(session["draft_json"] or "{}")
        departure_at = datetime.fromisoformat(draft["departure_at"])
        dt, _ = parse_datetime_prefix(clean_text)
        if dt is None:
            dt = ai_extract_datetime_only(clean_text)
        if dt is None:
            send_line_reply(reply_token, "⚠️ 看不懂這個時間，請用「YYYY-MM-DD HH:MM」格式再試一次。")
            return True
        if dt <= departure_at:
            send_line_reply(reply_token, "⚠️ 回程時間必須晚於出發時間，請重新輸入。")
            return True

        try:
            with db_cursor() as cur:
                cur.execute(
                    """INSERT INTO trips (owner_type, owner_id, departure_at, return_at, status, created_by_uid)
                       VALUES (%s, %s, %s, %s, 'collecting', %s)""",
                    (owner_type, owner_id, departure_at, dt, creator_id)
                )
                trip_pk = cur.lastrowid
        except Exception as e:
            log_error("旅行建立", e, owner_id)
            send_line_reply(reply_token, "⚠️ 旅行建立失敗，請稍後再試一次。")
            return True

        set_trip_session(owner_type, owner_id, "collecting", trip_id=trip_pk)
        send_line_reply(
            reply_token,
            f"✅ 已建立旅行草案！\n📅 {departure_at.strftime('%Y-%m-%d %H:%M')} → {dt.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"請開始輸入行程地點，格式：日期時間 地點，例如：\n2026-07-19 20:00 台北101\n（也可以直接貼 Google 地圖連結）\n\n"
            f"全部輸入完畢後，請輸入「結束」進行總覽確認。"
        )
        return True

    # --- Stage 3：收集行程地點 ---
    if stage == "collecting":
        trip_id = session["trip_id"]

        if clean_text in ("結束", "完成", "結束規劃"):
            try:
                with db_cursor() as cur:
                    cur.execute("SELECT * FROM trips WHERE id=%s", (trip_id,))
                    trip = cur.fetchone()
            except Exception as e:
                log_error("旅行查詢", e, owner_id)
                send_line_reply(reply_token, "⚠️ 查詢失敗，請稍後再試一次。")
                return True
            send_trip_review(owner_type, owner_id, trip, reply_token)
            return True

        dt, remainder = parse_datetime_prefix(clean_text)
        location_text = remainder
        maps_lat, maps_lon = (None, None)
        if dt is None or not remainder:
            if is_maps_url(clean_text):
                maps_lat, maps_lon = try_extract_latlon_from_url(clean_text)
            ai_dt, ai_loc = ai_extract_itinerary_item(clean_text)
            dt = dt or ai_dt
            location_text = location_text or ai_loc

        if dt is None or not location_text:
            send_line_reply(
                reply_token,
                "⚠️ 看不懂日期時間或地點，請用「日期時間 地點」格式再試一次，例如：\n2026-07-19 20:00 台北101\n或輸入「結束」完成規劃。"
            )
            return True

        if maps_lat is not None:
            lat, lon = maps_lat, maps_lon
        else:
            lat, lon = geocode_location(location_text)

        draft = {"scheduled_at": dt.isoformat(), "location_name": location_text, "lat": lat, "lon": lon}
        set_trip_session(owner_type, owner_id, "pending_location_confirm", trip_id=trip_id, draft=draft)
        geo_note = "" if lat is not None else "\n⚠️ 這個地點沒有查到座標，將不會有通勤估算，但仍可正常登記。"
        send_line_reply(
            reply_token,
            f"📍 地點解讀為：「{location_text}」\n🕒 {dt.strftime('%m/%d %H:%M')}\n"
            f"這樣正確嗎？回覆「對」確認登記，或直接重新輸入地點名稱修正。{geo_note}"
        )
        return True

    # --- Stage 4：等待地點確認 ---
    if stage == "pending_location_confirm":
        draft = json.loads(session["draft_json"] or "{}")
        trip_id = session["trip_id"]

        if any(k in clean_text for k in ["對", "是", "正確", "沒錯", "confirm", "OK", "ok"]):
            dt = datetime.fromisoformat(draft["scheduled_at"])
            try:
                with db_cursor() as cur:
                    cur.execute(
                        """INSERT INTO itineraries
                           (owner_type, owner_id, trip_id, scheduled_at, location_name, latitude, longitude, created_by_uid)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        (owner_type, owner_id, trip_id, dt, draft["location_name"], draft.get("lat"), draft.get("lon"), creator_id)
                    )
            except Exception as e:
                log_error("行程項目登記", e, owner_id)
                send_line_reply(reply_token, "⚠️ 登記失敗，請稍後再試一次。")
                return True
            set_trip_session(owner_type, owner_id, "collecting", trip_id=trip_id)
            send_line_reply(reply_token, f"✅ 已登記：{dt.strftime('%m/%d %H:%M')} {draft['location_name']}\n請繼續輸入下一個行程，或輸入「結束」完成規劃。")
            return True

        # 不是確認回覆 → 當作重新輸入的地點名稱，沿用原本的日期時間重新解析
        new_location_text = clean_text
        maps_lat, maps_lon = (None, None)
        if is_maps_url(new_location_text):
            maps_lat, maps_lon = try_extract_latlon_from_url(new_location_text)
            ai_loc = ai_extract_location_name(new_location_text) if maps_lat is None else None
            if ai_loc:
                new_location_text = ai_loc
        lat, lon = (maps_lat, maps_lon) if maps_lat is not None else geocode_location(new_location_text)

        draft.update({"location_name": new_location_text, "lat": lat, "lon": lon})
        set_trip_session(owner_type, owner_id, "pending_location_confirm", trip_id=trip_id, draft=draft)
        dt = datetime.fromisoformat(draft["scheduled_at"])
        geo_note = "" if lat is not None else "\n⚠️ 這個地點沒有查到座標，將不會有通勤估算，但仍可正常登記。"
        send_line_reply(
            reply_token,
            f"📍 地點解讀為：「{new_location_text}」\n🕒 {dt.strftime('%m/%d %H:%M')}\n"
            f"這樣正確嗎？回覆「對」確認登記，或直接重新輸入地點名稱修正。{geo_note}"
        )
        return True

    # --- Stage 5：等待總覽確認或修改指示 ---
    if stage == "pending_review":
        trip_id = session["trip_id"]
        try:
            with db_cursor() as cur:
                cur.execute("SELECT * FROM trips WHERE id=%s", (trip_id,))
                trip = cur.fetchone()
        except Exception as e:
            log_error("旅行查詢", e, owner_id)
            send_line_reply(reply_token, "⚠️ 查詢失敗，請稍後再試一次。")
            return True
        if not trip:
            clear_trip_session(owner_type, owner_id)
            return False

        if any(k in clean_text for k in ["確定", "OK", "ok", "沒問題", "可以"]):
            if trip["status"] != "confirmed":
                trip_code = generate_trip_code(owner_type, owner_id, trip["departure_at"])
                try:
                    with db_cursor() as cur:
                        cur.execute("UPDATE trips SET status='confirmed', trip_code=%s WHERE id=%s", (trip_code, trip_id))
                except Exception as e:
                    log_error("旅行確認寫入", e, owner_id)
                    send_line_reply(reply_token, "⚠️ 確認失敗，請稍後再試一次。")
                    return True
            else:
                trip_code = trip["trip_code"]
            clear_trip_session(owner_type, owner_id)
            send_line_reply(
                reply_token,
                f"🎉 旅行規劃完成！旅行單號：#{trip_code}\n"
                f"👉 之後可輸入「旅行修改 {trip_code}」重新編輯，行程開始前 45 分鐘我會主動提醒您！"
            )
            return True

        # 其餘文字視為修改指示，交給 AI 判讀
        summary = build_trip_summary_text(trip_id)
        mod = ai_apply_trip_modification(summary, trip["departure_at"], trip["return_at"], clean_text)

        if mod.action == "delete" and mod.target_index:
            try:
                with db_cursor() as cur:
                    cur.execute("SELECT id FROM itineraries WHERE trip_id=%s ORDER BY scheduled_at ASC", (trip_id,))
                    rows = cur.fetchall()
                    if 1 <= mod.target_index <= len(rows):
                        cur.execute("DELETE FROM itineraries WHERE id=%s", (rows[mod.target_index - 1]["id"],))
            except Exception as e:
                log_error("旅行修改-刪除", e, owner_id)

        elif mod.action == "edit" and mod.target_index:
            try:
                with db_cursor() as cur:
                    cur.execute("SELECT id, scheduled_at, location_name FROM itineraries WHERE trip_id=%s ORDER BY scheduled_at ASC", (trip_id,))
                    rows = cur.fetchall()
                    if 1 <= mod.target_index <= len(rows):
                        target = rows[mod.target_index - 1]
                        new_dt = target["scheduled_at"]
                        new_loc = target["location_name"]
                        if mod.new_datetime_str:
                            try:
                                new_dt = datetime.strptime(mod.new_datetime_str, "%Y-%m-%d %H:%M")
                            except Exception:
                                pass
                        if mod.new_location:
                            new_loc = mod.new_location
                        lat, lon = geocode_location(new_loc) if mod.new_location else (None, None)
                        if mod.new_location:
                            cur.execute("UPDATE itineraries SET scheduled_at=%s, location_name=%s, latitude=%s, longitude=%s, notified=0 WHERE id=%s",
                                        (new_dt, new_loc, lat, lon, target["id"]))
                        else:
                            cur.execute("UPDATE itineraries SET scheduled_at=%s, notified=0 WHERE id=%s", (new_dt, target["id"]))
            except Exception as e:
                log_error("旅行修改-編輯", e, owner_id)

        elif mod.action == "add" and mod.new_datetime_str and mod.new_location:
            try:
                new_dt = datetime.strptime(mod.new_datetime_str, "%Y-%m-%d %H:%M")
                lat, lon = geocode_location(mod.new_location)
                with db_cursor() as cur:
                    cur.execute(
                        """INSERT INTO itineraries (owner_type, owner_id, trip_id, scheduled_at, location_name, latitude, longitude, created_by_uid)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        (owner_type, owner_id, trip_id, new_dt, mod.new_location, lat, lon, creator_id)
                    )
            except Exception as e:
                log_error("旅行修改-新增", e, owner_id)

        elif mod.action == "update_times":
            try:
                new_dep = datetime.strptime(mod.new_departure_str, "%Y-%m-%d %H:%M") if mod.new_departure_str else trip["departure_at"]
                new_ret = datetime.strptime(mod.new_return_str, "%Y-%m-%d %H:%M") if mod.new_return_str else trip["return_at"]
                with db_cursor() as cur:
                    cur.execute("UPDATE trips SET departure_at=%s, return_at=%s WHERE id=%s", (new_dep, new_ret, trip_id))
            except Exception as e:
                log_error("旅行修改-時間", e, owner_id)

        else:
            send_line_reply(reply_token, "⚠️ 不太確定您要修改的內容，請具體描述，例如：「把第2項改成15:00 台北101」「刪除第3項」「新增 2026-07-20 09:00 早餐店」。")
            return True

        try:
            with db_cursor() as cur:
                cur.execute("SELECT * FROM trips WHERE id=%s", (trip_id,))
                trip = cur.fetchone()
        except Exception as e:
            log_error("旅行查詢", e, owner_id)
            return True
        send_trip_review(owner_type, owner_id, trip, reply_token)
        return True

    return False

def ai_extract_location_name(text: str):
    """輔助函式：僅需要地點名稱時使用（例如地點確認階段重新輸入 Google 地圖連結）"""
    _, loc = ai_extract_itinerary_item(text)
    return loc

def send_itinerary_reminder(it: dict):
    owner_type = it["owner_type"]
    owner_id = it["owner_id"]
    lines = [f"⏰ 【行程提醒】{it['scheduled_at'].strftime('%H:%M')} 即將前往：{it['location_name']}"]

    if it["latitude"] is not None and it["longitude"] is not None:
        try:
            with db_cursor() as cur:
                cur.execute(
                    """SELECT location_name, latitude, longitude, scheduled_at FROM itineraries
                       WHERE owner_type=%s AND owner_id=%s AND scheduled_at > %s
                       ORDER BY scheduled_at ASC LIMIT 1""",
                    (owner_type, owner_id, it["scheduled_at"])
                )
                nxt = cur.fetchone()
            if nxt and nxt["latitude"] is not None:
                dist = haversine_km(float(it["latitude"]), float(it["longitude"]), float(nxt["latitude"]), float(nxt["longitude"]))
                mins = estimate_travel_minutes(dist)
                lines.append(
                    f"🚗 下一站「{nxt['location_name']}」（{nxt['scheduled_at'].strftime('%H:%M')}）\n"
                    f"　　約 {dist:.1f} 公里，車程估計 {mins} 分鐘\n"
                    f"　　（直線距離估算，僅供測試參考，非實際路網路徑）"
                )
        except Exception as e:
            log_error("通勤估算", e, owner_id)

    lines.append("\n💰 這趟行程有花費要記錄嗎？回覆「有」開始登記，或回覆「無」略過。")
    push_line_message(owner_id, "\n".join(lines))

    try:
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO pending_itinerary_confirm (owner_type, owner_id, itinerary_id, created_at)
                   VALUES (%s, %s, %s, NOW())
                   ON DUPLICATE KEY UPDATE itinerary_id=VALUES(itinerary_id), created_at=NOW()""",
                (owner_type, owner_id, it["id"])
            )
    except Exception as e:
        log_error("行程待確認寫入", e, owner_id)

def check_and_send_itinerary_reminders():
    """由背景排程每分鐘呼叫一次：找出 44~46 分鐘後即將開始、還沒提醒過的行程"""
    if not DB_READY:
        return
    now = datetime.now()
    window_start = now + timedelta(minutes=44)
    window_end = now + timedelta(minutes=46)
    try:
        with db_cursor() as cur:
            cur.execute(
                """SELECT * FROM itineraries
                   WHERE notified=0 AND scheduled_at BETWEEN %s AND %s""",
                (window_start, window_end)
            )
            due_items = cur.fetchall()
            for it in due_items:
                cur.execute("UPDATE itineraries SET notified=1 WHERE id=%s", (it["id"],))
    except Exception as e:
        log_error("行程排程查詢", e)
        return

    for it in due_items:
        send_itinerary_reminder(it)

def try_handle_itinerary_confirm_reply(owner_type: str, owner_id: str, clean_text: str, is_group: bool, target_id: str, reply_token: str) -> bool:
    """處理行程提醒推播後，使用者回覆「有／無」是否要記錄花費"""
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT itinerary_id FROM pending_itinerary_confirm WHERE owner_type=%s AND owner_id=%s",
                (owner_type, owner_id)
            )
            row = cur.fetchone()
    except Exception as e:
        log_error("行程待確認查詢", e, owner_id)
        return False

    if not row:
        return False

    positive = any(k in clean_text for k in ["有", "要", "記錄", "登記"])
    negative = any(k in clean_text for k in ["無", "沒有", "不用", "略過", "skip"])
    if not (positive or negative):
        return False  # 不是在回答這個問題，讓訊息繼續往下走正常流程

    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM pending_itinerary_confirm WHERE owner_type=%s AND owner_id=%s", (owner_type, owner_id))
    except Exception as e:
        log_error("行程待確認清除", e, owner_id)

    if negative:
        send_line_reply(reply_token, "👌 好的，這趟行程不記錄花費。")
        return True

    if is_group:
        code_str = str(random.randint(1000, 9999))
        try:
            with db_cursor() as cur:
                cur.execute(
                    "UPDATE `groups` SET state='order', active_order_code=%s WHERE group_id=%s",
                    (code_str, target_id)
                )
                cur.execute(
                    "UPDATE itineraries SET related_order_code=%s WHERE id=%s",
                    (code_str, row["itinerary_id"])
                )
            send_line_reply(reply_token, f"🚀 已開啟本次行程消費登記！單號：#{code_str}\n👉 請大家直接輸入「品項 金額」登記花費，行程結束後輸入「結單」結算。")
        except Exception as e:
            log_error("行程開團寫入", e, target_id)
    else:
        send_line_reply(reply_token, "👌 好的，請直接輸入「項目 金額」，我會記錄到您的個人帳本。")
    return True

async def itinerary_reminder_loop():
    """背景排程：每 60 秒檢查一次是否有即將開始的行程需要提醒"""
    while True:
        try:
            await asyncio.to_thread(check_and_send_itinerary_reminders)
        except Exception as e:
            log_error("行程排程迴圈", e)
        await asyncio.sleep(60)

# ==========================================
# 🍱 8. 群組團單：均分／@tag／跳過 分攤流程
# ==========================================
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
                           VALUES ('group', %s, 'expense', %s, %s, '生活雜費', %s, %s)""",
                        (group_id, i["price"], i["item_name"], payer_id, payer_name)
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

# ==========================================
# 🧾 9. 收據辨識：Gemini 圖片辨識與品項修改
# ==========================================
class ReceiptItemModel(BaseModel):
    item_name: str = Field(default="")
    price: int = Field(default=0)

class ReceiptExtraction(BaseModel):
    items: List[ReceiptItemModel] = Field(default_factory=list)
    total_amount: int = Field(default=0)

def download_line_image(message_id: str) -> bytes:
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    res = httpx.get(url, headers=headers, timeout=15.0, verify=certifi.where())
    res.raise_for_status()
    return res.content

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
        send_line_reply(reply_token, f"⚠️ 收據辨識失敗：AI 辨識服務串接發生問題，請稍後再試一次。\n（若持續發生，麻煩回報管理員：{str(e)[:80]}）")
        return

    if extraction is None:
        send_line_reply(reply_token, "⚠️ 收據辨識失敗：AI 沒有回傳可用的結果，請稍後再試一次。")
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

    # 群組情境：暫存後詢問分攤方式
    try:
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO pending_group_expense (group_id, payer_id, payer_name, items_json, total_amount, source)
                   VALUES (%s, %s, %s, %s, %s, 'receipt')
                   ON DUPLICATE KEY UPDATE
                       payer_id=VALUES(payer_id), payer_name=VALUES(payer_name),
                       items_json=VALUES(items_json), total_amount=VALUES(total_amount),
                       source='receipt', created_at=NOW()""",
                (owner_id, creator_id, creator_name, json.dumps(items, ensure_ascii=False), total)
            )
    except Exception as e:
        log_error("收據待分攤建立", e, owner_id)
        send_line_reply(reply_token, "⚠️ 辨識成功但登記失敗，請稍後再試一次。")
        return

    send_line_reply(
        reply_token,
        f"🧾 收據辨識完成：\n{item_lines}\n💰 合計：${total}\n\n"
        f"這筆怎麼記？\n1️⃣ 回覆「均分」→ 平分給已知群組成員\n2️⃣ tag 出實際分攤的人\n3️⃣ tag 並加金額（例如 @小明 100 @小華 50）→ 指定金額\n4️⃣ 回覆「跳過」→ 記一般花費\n\n"
        f"若品項或金額有誤，登記完成後可輸入「修改 單號 項次 品項 金額」修正，例如：修改 1234 2 拿鐵 65"
    )

# ==========================================
# 🌐 4. Webhook 核心主線
# ==========================================
@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature")
    if not signature: raise HTTPException(status_code=400, detail="Missing Signature")
    body = await request.body()
    body_str = body.decode("utf-8")
    background_tasks.add_task(handle_line_events_safe, body_str, signature)
    return Response(content="OK", status_code=200)

def handle_line_events_safe(body_str: str, signature: str):
    try: line_handler.handle(body_str, signature)
    except InvalidSignatureError: pass

@line_handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    if not DB_READY: return
    if not is_bot_enabled():
        send_line_reply(event.reply_token, get_maintenance_message())
        return

    user_text = event.message.text.strip()
    creator_id = event.source.user_id 
    is_group = event.source.type == "group"
    reply_token = event.reply_token 
    
    target_id = event.source.group_id if is_group else creator_id
    root_collection = "groups" if is_group else "users"  # 保留供語意參考，實際寫入以 owner_type 欄位區分
    owner_type = "group" if is_group else "user"

    current_mode = "normal"
    active_code = ""
    
    if is_group:
        try:
            with db_cursor() as cur:
                cur.execute(
                    "SELECT state, active_order_code FROM `groups` WHERE group_id=%s",
                    (target_id,)
                )
                row = cur.fetchone()
                if row:
                    current_mode = row["state"]
                    active_code = row["active_order_code"] or ""
                else:
                    cur.execute(
                        "INSERT INTO `groups` (group_id, state) VALUES (%s, 'normal')",
                        (target_id,)
                    )
        except Exception as e:
            log_error("群組狀態查詢", e, target_id)
            return

    is_bot_tagged = False
    mention = getattr(event.message, "mention", None)
    if mention and mention.mentionees: is_bot_tagged = True
    if any(kw in user_text for kw in ["@記帳米粒", "記帳米粒"]): is_bot_tagged = True
    if is_group and not is_bot_tagged: return 

    # ====================================================
    # 🧪 【測試限定功能：密碼驗證 / 待回覆狀態攔截層】
    # ------------------------------------------------------
    # 優先序：密碼驗證 > 旅行模式多輪對話 > 行程「有/無」回覆 > 群組分攤「均分/@tag/跳過」回覆
    # 這幾層都是「上一則機器人訊息在等待使用者回覆」的情境，
    # 必須搶在核銷、開團等既有邏輯之前處理，否則會被其他規則誤判掉。
    # ====================================================
    _gate_text = user_text.replace("@記帳米粒", "").replace("記帳米粒", "").strip()

    if try_handle_test_mode_gate(owner_type, target_id, _gate_text, reply_token):
        return

    if try_handle_trip_flow(owner_type, target_id, creator_id, _gate_text, reply_token):
        return

    if try_handle_itinerary_confirm_reply(owner_type, target_id, _gate_text, is_group, target_id, reply_token):
        return

    if is_group and try_handle_group_split_reply(target_id, event, _gate_text, creator_id, reply_token):
        return

    # ====================================================
    # 🎯 🛠️ 【核銷解鎖與防呆邏輯】
    # ====================================================
    is_settle_trigger = any(k in user_text for k in ["核銷", "還錢", "平帳", "給錢", "付清"])
    if is_group and current_mode == "normal" and is_settle_trigger:
        code_match = re.search(r'#?(\d{4})', user_text)
        if not code_match:
            send_line_reply(reply_token, "⚠️ 必須輸入對應的 4 位數團購單號才可開啟核銷模式。\n👉 範例：『@記帳米粒 申請核銷 #1234』")
            return
            
        req_code = code_match.group(1)
        try:
            with db_cursor() as cur:
                cur.execute(
                    "SELECT master_payer_id FROM orders WHERE group_id=%s AND order_code=%s ORDER BY id DESC LIMIT 1",
                    (target_id, req_code)
                )
                order_found = cur.fetchone()
                if not order_found:
                    send_line_reply(reply_token, f"❌ 找不到本群組內編號為 #{req_code} 的團購單。")
                    return

                cur.execute(
                    "UPDATE `groups` SET state='settle', active_order_code=%s WHERE group_id=%s",
                    (req_code, target_id)
                )
        except Exception as e:
            log_error("核銷解鎖查詢", e, target_id)
            return
            
        payer_str = resolve_id_to_name(target_id, order_found.get("master_payer_id") or creator_id)
        send_line_reply(reply_token, f"🔓 成功解鎖結算模式！鎖定單號：#{req_code}\n💳 墊款買單人：{payer_str}\n👉 請開始核銷對帳（如：@記帳米粒 我核銷我自己 150）")
        return

    # ====================================================
    # 🎯 🛠️ 【結算模式：互相核銷與自行核銷】
    # ====================================================
    if is_group and current_mode == "settle":
        if any(k in user_text for k in ["結算結束", "關閉結算", "核銷截止", "核銷完畢","截止","結束"]):
            try:
                with db_cursor() as cur:
                    cur.execute(
                        "UPDATE `groups` SET state='normal', active_order_code='' WHERE group_id=%s",
                        (target_id,)
                    )
            except Exception as e:
                log_error("結算關閉", e, target_id)
            send_line_reply(reply_token, "🔓 結算完畢！已安全關閉對帳並恢復一般模式。")
            return

        if any(k in user_text for k in ["給", "還", "付", "收", "核銷"]):
            clean_text_settle = re.sub(r'#?\d{4}', '', user_text)
            amount_match = re.search(r'\d+', clean_text_settle)
            settle_amount = int(amount_match.group()) if amount_match else 0
            if settle_amount <= 0: return

            real_tagged_ids = get_real_mentions(event)

            if len(real_tagged_ids) >= 2:
                final_payer_id = real_tagged_ids[0]
                final_receiver_id = real_tagged_ids[1]
            elif len(real_tagged_ids) == 1:
                final_payer_id = real_tagged_ids[0]
                final_receiver_id = creator_id
            else:
                final_payer_id = creator_id
                final_receiver_id = creator_id
                
            if final_payer_id and final_receiver_id:
                try:
                    with db_cursor() as cur:
                        cur.execute(
                            "SELECT id FROM orders WHERE group_id=%s AND order_code=%s ORDER BY id DESC LIMIT 1",
                            (target_id, active_code)
                        )
                        current_order = cur.fetchone()
                        if not current_order:
                            return
                        order_pk = current_order["id"]

                        cur.execute(
                            """SELECT COALESCE(SUM(price), 0) AS total FROM order_items
                               WHERE order_id=%s AND buyer_id=%s""",
                            (order_pk, final_payer_id)
                        )
                        payer_expected_total = cur.fetchone()["total"]

                        cur.execute(
                            """SELECT COALESCE(SUM(amount), 0) AS total FROM settlements
                               WHERE group_id=%s AND order_code_ref=%s AND payer_id=%s""",
                            (target_id, active_code, final_payer_id)
                        )
                        payer_already_paid = cur.fetchone()["total"]

                        remaining_debt = payer_expected_total - payer_already_paid

                        if remaining_debt <= 0:
                            send_line_reply(reply_token, f"❌ 登記拒絕！成員 {resolve_id_to_name(target_id, final_payer_id)} 在單號 #{active_code} 中並無欠款紀錄。")
                            return
                        elif settle_amount > remaining_debt:
                            send_line_reply(reply_token, f"❌ 入帳失敗！金額溢繳！\n⚠️ 該成員此單賸餘應付為：${remaining_debt} 元，您輸入的 ${settle_amount} 元不符合規範，拒絕入帳。")
                            return

                        payer_name_str = resolve_id_to_name(target_id, final_payer_id)
                        receiver_name_str = resolve_id_to_name(target_id, final_receiver_id)

                        cur.execute(
                            """INSERT INTO settlements
                               (group_id, order_code_ref, payer_id, payer_name, receiver_id, receiver_name, amount)
                               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                            (target_id, active_code, final_payer_id, payer_name_str, final_receiver_id, receiver_name_str, settle_amount)
                        )
                except Exception as e:
                    log_error("核銷寫入", e, target_id)
                    return

                if final_payer_id == final_receiver_id:
                    send_line_reply(reply_token, f"✅ 【單號 #{active_code} 核銷成功】\n🙋‍♂️ 自行核銷：{payer_name_str}\n💰 紀錄金額：${settle_amount}")
                else:
                    send_line_reply(reply_token, f"✅ 【單號 #{active_code} 核銷成功】\n💸 付款：{payer_name_str}\n📥 收款：{receiver_name_str}\n💰 紀錄金額：${settle_amount}")
                return

    # 移除 Tag 符號以利後續關鍵字或 Regex 精準匹配
    clean_text = user_text.replace("@記帳米粒", "").replace("記帳米粒", "").strip()

    # ====================================================
    # 🧾 【測試限定：收據辨識 - 修改已登記的品項】
    # ====================================================
    if is_group and is_test_mode_active(owner_type, target_id, "receipt_ocr"):
        if try_handle_edit_order_item(target_id, clean_text, reply_token):
            return

    # ====================================================
    # 🎯 ⚡ 【V1.1 Python 層攔截：新增特定詞觸發指定回覆】
    # ====================================================
    for kw, reply_msg in get_keyword_replies().items():
        if kw in clean_text:
            send_line_reply(reply_token, reply_msg)
            return

    # ====================================================
    # 📖 【Python 層攔截：系統說明書與報表派發】
    # ====================================================
    if any(k in clean_text for k in ["報表", "查帳", "大後台", "網址", "網站", "入口", "登入"]) and current_mode == "normal":
        if is_group:
            # 群組情境：走到這裡代表已被 tag(is_bot_tagged 檢查已在前面攔截),提供帶 groupId 的後台網址
            send_line_reply(reply_token, f"📊 【記帳米粒 ｜ 雲端監控後台】\n🟢 入口如下：\nhttps://liff.line.me/{MY_LIFF_ID}?groupId={target_id}")
        else:
            # 個人情境：不提供網址(避免 groupId 誤帶入個人 user_id 導致資料混淆),改導引至圖文選單
            send_line_reply(reply_token, "📊 個人記帳報表請點選下方圖文選單即可查看喔！")
        return

    # 配合 V1.1 更新：精簡優化版的使用說明導覽
    if any(k in clean_text for k in ["使用說明", "怎麼用", "功能", "規定", "教學"]):
        instructions = (
            "🌾 【記帳米粒 | 快速上手指南】\n"
            "-------------------------\n"
            "💰 1. 一般記帳 \n"
            "👉 輸入「項目 金額」記支出\n"
            "   └ 範例：早餐 80 元\n"
            "👉 開頭加「收入」或「+」記收入\n"
            "   └ 範例：收入 薪水 30000 / +接案 5000\n\n"
            "🛒 2. 團隊開團 (揪團模式)\n"
            "👉 輸入「開團」啟動專屬單號\n"
            "   └ 自點：冰美式 55\n"
            "   └ 代點：@小明 雞排 95\n"
            "   └ 結單：輸入「結單」鎖定明細\n\n"
            "💳 3. 防呆核銷 (對帳模式)\n"
            "👉 輸入「申請核銷 #單號」解鎖\n"
            "   └ 銷帳：@大明 已還 100\n"
            "   └ 關閉：輸入「結算結束」恢復常態\n\n"
            "📊 輸入「查帳」或「報表」可開啟監控後台網址！"
        )
        send_line_reply(reply_token, instructions)
        return

    for kw in get_sensitive_words():
        if kw in clean_text:
            log_stat_event("sensitive_block", target_id)
            send_line_reply(reply_token, "🤖 米粒為純財務助理，請勿探討敏感議題喔！")
            return

    # ====================================================
    # ⚡ 🚀 【Python 第一層極速攔截：代點單與記帳直通落庫】
    # ------------------------------------------------------
    # 🆕 一般模式下，開頭加「收入」或「+」可以快速記成收入，
    #    例如：「收入 薪水 30000」「+薪水 30000」，其餘輸入一律當支出。
    #    （揪團模式不受影響，維持原本的代點單邏輯）
    # ====================================================
    income_prefix_match = None
    if current_mode == "normal":
        income_prefix_match = re.match(r'^(?:收入|\+)\s*(.+)$', clean_text)
    is_income_quick = bool(income_prefix_match)
    text_for_fast_match = income_prefix_match.group(1) if income_prefix_match else clean_text

    fast_match = re.fullmatch(r'^(.+?)\s*(\d+)\s*(?:元|塊)?$', text_for_fast_match)
    if fast_match and current_mode in ["normal", "order"]:
        raw_item_name = fast_match.group(1).strip()
        amount = int(fast_match.group(2))
        
        item_name = re.sub(r'@\S+', '', raw_item_name).strip()
        
        if not item_name.isdigit() and amount > 0:
            if current_mode == "normal":
                creator_name_str = resolve_id_to_name(target_id, creator_id)
                record_type = "income" if is_income_quick else "expense"

                # 🍱 測試限定：群組團單分攤 —— 群組、支出（非收入）、且該群組已開通測試模式時，
                # 改為詢問「均分／@tag／跳過」，而不是直接寫入一般記帳
                if is_group and not is_income_quick and is_test_mode_active("group", target_id, "group_split"):
                    if try_start_group_split_question(target_id, item_name, amount, creator_id, creator_name_str, reply_token):
                        return

                try:
                    with db_cursor() as cur:
                        cur.execute(
                            """INSERT INTO expenses
                               (owner_type, owner_id, record_type, amount, item, category, created_by_uid, created_by_name)
                               VALUES (%s, %s, %s, %s, %s, '生活雜費', %s, %s)""",
                            (owner_type, target_id, record_type, amount, item_name, creator_id, creator_name_str)
                        )
                    if is_income_quick:
                        send_line_reply(reply_token, f"💰 已紀錄收入：{item_name} ${amount}")
                    else:
                        send_line_reply(reply_token, f"✅ 已紀錄：{item_name} ${amount}")
                except Exception as e:
                    log_error("記帳寫入", e, target_id)
                    send_line_reply(reply_token, "⚠️ 紀錄失敗，請稍後再試一次。")
                return
                
            elif current_mode == "order" and is_group:
                real_tagged_ids = get_real_mentions(event)
                        
                actual_buyer_id = real_tagged_ids[0] if real_tagged_ids else creator_id
                actual_buyer_name = resolve_id_to_name(target_id, actual_buyer_id)
                
                try:
                    with db_cursor() as cur:
                        cur.execute(
                            """INSERT INTO order_items
                               (group_id, order_code, buyer_id, buyer_name, item_name, price)
                               VALUES (%s, %s, %s, %s, %s, %s)""",
                            (target_id, active_code, actual_buyer_id, actual_buyer_name, item_name, amount)
                        )
                    send_line_reply(reply_token, f"📝 已接單：{item_name} ${amount}")
                except Exception as e:
                    log_error("團購品項寫入", e, target_id)
                    send_line_reply(reply_token, "⚠️ 接單失敗，請稍後再試一次。")
                return

    # ====================================================
    # 🧠 🧠 【第二層：Gemini 核心大腦 - 複雜萃取與自然陪聊】
    # ====================================================
    try:
        prompt = f"""
        {get_ai_persona()}目前位於【{root_collection}】環境，模式為【{current_mode}】。
        使用者輸入了：『{clean_text}』
        
        【分流任務】：
        1. 判定 intent (record, order_start, order_end, order_item, chat)。
        2. 如果對話中包含「花費與金額」（例如：今天買咖啡花了150元），請提取出紀錄 (intent="record")，並在 ai_reply 中給予親切的聊天回覆。
        3. 如果是純閒聊，intent="chat"，請在 ai_reply 陪使用者自然對話。
        4. 開團(order_start) 或 結單(order_end) 等控制指令，請在 ai_reply 給予親切的確認回覆。
        """

        result = ai_client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SuperRouter, temperature=0.3),
        ).parsed

        # 1. AI 萃取記帳與陪聊
        if result.intent == "record":
            if result.records:
                creator_name_str = resolve_id_to_name(target_id, creator_id)
                owner_type = "group" if is_group else "user"
                try:
                    with db_cursor() as cur:
                        for rec in result.records:
                            if rec.amount > 0:
                                cur.execute(
                                    """INSERT INTO expenses
                                       (owner_type, owner_id, record_type, amount, item, category, created_by_uid, created_by_name)
                                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                                    (owner_type, target_id, rec.record_type, rec.amount, rec.item, rec.category, creator_id, creator_name_str)
                                )
                except Exception as e:
                    log_error("AI記帳寫入", e, target_id)
                reply_text = result.ai_reply if result.ai_reply else f"✅ 已為您紀錄花費。"
                send_line_reply(reply_token, f"🤖 {reply_text}")

        # 2. 開團模式
        elif result.intent == "order_start" and is_group:
            code_str = str(random.randint(1000, 9999))
            try:
                with db_cursor() as cur:
                    cur.execute(
                        "UPDATE `groups` SET state='order', active_order_code=%s WHERE group_id=%s",
                        (code_str, target_id)
                    )
                reply_text = result.ai_reply if result.ai_reply else f"🚀 【團購已啟動】本團單號：#{code_str}\n👉 請大家叫單時記得「@記帳米粒 品項 金額」喔！"
                send_line_reply(reply_token, reply_text)
            except Exception as e:
                log_error("開團寫入", e, target_id)

        # 3. AI 萃取複雜點單與代點單
        elif result.intent == "order_item" and current_mode == "order" and is_group:
            if result.order_items:
                real_tagged_ids = get_real_mentions(event)
                actual_buyer_id = real_tagged_ids[0] if real_tagged_ids else creator_id
                actual_buyer_name = resolve_id_to_name(target_id, actual_buyer_id)
                
                reply_lines = []
                try:
                    with db_cursor() as cur:
                        for item in result.order_items:
                            clean_item_name = re.sub(r'@\S+', '', item.item_name).strip()
                            cur.execute(
                                """INSERT INTO order_items
                                   (group_id, order_code, buyer_id, buyer_name, item_name, price)
                                   VALUES (%s, %s, %s, %s, %s, %s)""",
                                (target_id, active_code, actual_buyer_id, actual_buyer_name, clean_item_name, item.price)
                            )
                            reply_lines.append(f"📝 已接單：{clean_item_name} ${item.price}")
                    send_line_reply(reply_token, "\n".join(reply_lines))
                except Exception as e:
                    log_error("AI團購品項寫入", e, target_id)

        # 4. 截止結單
        elif result.intent == "order_end" and current_mode == "order" and is_group:
            try:
                with db_cursor() as cur:
                    cur.execute(
                        """SELECT COALESCE(SUM(price), 0) AS total, COUNT(*) AS cnt
                           FROM order_items WHERE group_id=%s AND order_code=%s AND order_id IS NULL""",
                        (target_id, active_code)
                    )
                    agg = cur.fetchone()
                    total_amt = agg["total"]
                    item_count = agg["cnt"]

                    if item_count > 0:
                        creator_name_str = resolve_id_to_name(target_id, creator_id)
                        cur.execute(
                            """INSERT INTO orders (group_id, order_code, order_date, total_amount, master_payer_id, master_payer_name)
                               VALUES (%s, %s, CURDATE(), %s, %s, %s)""",
                            (target_id, active_code, total_amt, creator_id, creator_name_str)
                        )
                        new_order_id = cur.lastrowid
                        cur.execute(
                            "UPDATE order_items SET order_id=%s WHERE group_id=%s AND order_code=%s AND order_id IS NULL",
                            (new_order_id, target_id, active_code)
                        )
                        reply_text = result.ai_reply if result.ai_reply else f"🏁 【團購截止 ｜ 單號 #{active_code}】\n💰 總金額：${total_amt} 元\n💳 墊款：{creator_name_str}\n\n🤖 數據已更新！"
                        send_line_reply(reply_token, reply_text)
                    else:
                        send_line_reply(reply_token, "🛑 因無人叫單，本團已直接關閉。")

                    cur.execute(
                        "UPDATE `groups` SET state='normal', active_order_code='' WHERE group_id=%s",
                        (target_id,)
                    )
            except Exception as e:
                log_error("結單處理", e, target_id)

        # 5. 純粹對話陪聊
        elif result.intent == "chat" and result.ai_reply:
            send_line_reply(reply_token, f"🤖 {result.ai_reply}")

    except Exception as e:
        log_error("Gemini解析", e, target_id)

@line_handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    """🧾 收據辨識入口。圖片訊息無法像文字一樣 @tag 機器人，
    因此只要該使用者/群組已開通「收據辨識」測試模式，收到圖片就會直接處理，
    不用另外要求 tag。未開通時也一律要回覆，不能靜默忽略（避免使用者誤以為機器人壞了）。"""
    if not DB_READY or not is_bot_enabled():
        return

    is_group = event.source.type == "group"
    creator_id = event.source.user_id
    target_id = event.source.group_id if is_group else creator_id
    owner_type = "group" if is_group else "user"
    reply_token = event.reply_token
    message_id = event.message.id

    if not is_test_mode_active(owner_type, target_id, "receipt_ocr"):
        send_line_reply(reply_token, "🔐「收據辨識」為測試限定功能，請先輸入「收據辨識」並完成密碼驗證後，才能傳照片辨識喔！")
        return

    creator_name = resolve_id_to_name(target_id, creator_id)
    handle_receipt_image(owner_type, target_id, is_group, creator_id, creator_name, reply_token, message_id)

@app.on_event("startup")
async def start_background_scheduler():
    """🗓️ 行程模式提醒推播的背景排程，每 60 秒檢查一次是否有即將開始的行程"""
    asyncio.create_task(itinerary_reminder_loop())

@app.get("/")
def health_check(): 
    return {"status": "mysql_active", "version": "v1.4-MySQL-test-features"}

# ==========================================
# 🖥️ 5. 監控後台 REST API（供 index.html 呼叫，取代原本直連 Firestore 的寫法）
# ==========================================
class ExpenseUpdate(BaseModel):
    item: str
    amount: int

class GroupStateUpdate(BaseModel):
    state: Literal["normal", "order", "settle"]

class OrderItemUpdate(BaseModel):
    buyer_name: str
    item_name: str
    price: int


def _require_db():
    if not DB_READY:
        raise HTTPException(status_code=503, detail="資料庫尚未就緒")


@app.get("/api/expenses")
def api_list_expenses(
    owner_type: Literal["user", "group"],
    owner_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    record_type: Optional[str] = None,
):
    """個人/群組首頁流水帳、進階查詢共用：依日期區間、收支類型篩選"""
    _require_db()
    sql = "SELECT id, record_type, amount, item, category, created_by_name, created_at FROM expenses WHERE owner_type=%s AND owner_id=%s"
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


@app.patch("/api/expenses/{expense_id}")
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


@app.delete("/api/expenses/{expense_id}")
def api_delete_expense(expense_id: int):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM expenses WHERE id=%s", (expense_id,))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/groups/{group_id}")
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


@app.patch("/api/groups/{group_id}/state")
def api_update_group_state(group_id: str, body: GroupStateUpdate):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("UPDATE `groups` SET state=%s WHERE group_id=%s", (body.state, group_id))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/groups/{group_id}/payer-summary")
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


@app.get("/api/groups/{group_id}/orders")
def api_list_orders(group_id: str):
    """歷史揪團訂單清單，並附上每個訂單的成員應付/已付明細（後端算好，前端不用再算）"""
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute(
                """SELECT id, order_code, order_date, total_amount, master_payer_name, created_at
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
                    "SELECT id, buyer_name, item_name, price FROM order_items WHERE order_id=%s ORDER BY id ASC",
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


@app.patch("/api/order-items/{item_id}")
def api_update_order_item(item_id: int, body: OrderItemUpdate):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT order_id FROM order_items WHERE id=%s", (item_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="找不到此品項")
            cur.execute(
                "UPDATE order_items SET buyer_name=%s, item_name=%s, price=%s WHERE id=%s",
                (body.buyer_name, body.item_name, body.price, item_id)
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


@app.delete("/api/order-items/{item_id}")
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


@app.delete("/api/groups/{group_id}/orders/{order_id}")
def api_delete_order(group_id: str, order_id: int):
    _require_db()
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM order_items WHERE order_id=%s AND group_id=%s", (order_id, group_id))
            cur.execute("DELETE FROM orders WHERE id=%s AND group_id=%s", (order_id, group_id))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)