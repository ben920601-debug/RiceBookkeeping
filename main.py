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
_cache = {"ts": 0, "bot_enabled": True, "keyword_replies": {}, "sensitive_words": [], "maintenance_message": ""}

DEFAULT_MAINTENANCE_MESSAGE = "🤖 系統維護中，請稍後再試。"

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
    except Exception as e:
        print(f"⚠️ 預設資料灌入失敗（若資料表尚未建立，請先執行 migration.sql）: {e}", flush=True)

def _refresh_cache_if_stale():
    if time.time() - _cache["ts"] < CACHE_TTL:
        return
    try:
        with db_cursor() as cur:
            cur.execute("SELECT `key`, `value` FROM bot_settings WHERE `key` IN ('bot_enabled', 'maintenance_message')")
            settings_rows = {r["key"]: r["value"] for r in cur.fetchall()}
            _cache["bot_enabled"] = settings_rows.get("bot_enabled", "1") == "1"
            _cache["maintenance_message"] = settings_rows.get("maintenance_message") or DEFAULT_MAINTENANCE_MESSAGE

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
    "行程模式": "itinerary",
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
# 🗓️ 7. 行程模式：新增／查詢／提醒推播
# ==========================================
ITINERARY_ADD_PATTERN = re.compile(
    r'^(?:新增行程|行程)?\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})\s+(\d{1,2}:\d{2})\s+(.+)$'
)

def try_add_itinerary(owner_type: str, owner_id: str, creator_id: str, clean_text: str, reply_token: str) -> bool:
    """輸入格式：2026/07/20 14:30 台北市政府（前面可加「新增行程」或「行程」字樣皆可辨識）"""
    m = ITINERARY_ADD_PATTERN.match(clean_text)
    if not m:
        return False

    date_str, time_str, location_name = m.groups()
    date_str = date_str.replace("/", "-")
    location_name = location_name.strip()
    try:
        scheduled_at = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        send_line_reply(reply_token, "⚠️ 日期時間格式看不懂，請用「YYYY-MM-DD HH:MM 地點」的格式輸入，例如：\n2026-07-20 14:30 台北市政府")
        return True

    if scheduled_at <= datetime.now():
        send_line_reply(reply_token, "⚠️ 這個時間已經過去了，請輸入未來的行程時間。")
        return True

    lat, lon = geocode_location(location_name)

    try:
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO itineraries
                   (owner_type, owner_id, scheduled_at, location_name, latitude, longitude, created_by_uid)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (owner_type, owner_id, scheduled_at, location_name, lat, lon, creator_id)
            )
    except Exception as e:
        log_error("行程新增", e, owner_id)
        send_line_reply(reply_token, "⚠️ 行程登記失敗，請稍後再試一次。")
        return True

    geo_note = "" if lat is not None else "\n⚠️ 這個地點沒有查到座標，屆時提醒訊息將不會包含通勤估算。"
    send_line_reply(
        reply_token,
        f"🗓️ 已登記行程：\n📍 {location_name}\n🕒 {scheduled_at.strftime('%Y-%m-%d %H:%M')}\n👉 出發前 15 分鐘會主動提醒您！{geo_note}"
    )
    return True

def try_list_itineraries(owner_type: str, owner_id: str, clean_text: str, reply_token: str) -> bool:
    if "查看行程" not in clean_text and "行程清單" not in clean_text:
        return False
    try:
        with db_cursor() as cur:
            cur.execute(
                """SELECT scheduled_at, location_name FROM itineraries
                   WHERE owner_type=%s AND owner_id=%s AND scheduled_at > NOW()
                   ORDER BY scheduled_at ASC LIMIT 10""",
                (owner_type, owner_id)
            )
            rows = cur.fetchall()
    except Exception as e:
        log_error("行程查詢", e, owner_id)
        send_line_reply(reply_token, "⚠️ 查詢失敗，請稍後再試一次。")
        return True

    if not rows:
        send_line_reply(reply_token, "📭 目前沒有登記中的未來行程。\n👉 輸入「2026-07-20 14:30 台北市政府」即可新增。")
    else:
        lines = ["🗓️ 【未來行程清單】"]
        for r in rows:
            lines.append(f"・{r['scheduled_at'].strftime('%m/%d %H:%M')}　{r['location_name']}")
        send_line_reply(reply_token, "\n".join(lines))
    return True

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
    """由背景排程每分鐘呼叫一次：找出 14~16 分鐘後即將開始、還沒提醒過的行程"""
    if not DB_READY:
        return
    now = datetime.now()
    window_start = now + timedelta(minutes=14)
    window_end = now + timedelta(minutes=16)
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

    code_str = str(random.randint(1000, 9999))
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO orders (group_id, order_code, order_date, total_amount, master_payer_id, master_payer_name)
               VALUES (%s, %s, CURDATE(), %s, %s, %s)""",
            (group_id, code_str, total, payer_id, payer_name)
        )
        order_pk = cur.lastrowid
        item_label = "、".join(i["item_name"] for i in items) if len(items) <= 3 else f"{items[0]['item_name']}等{len(items)}項"
        for idx, p in enumerate(participants):
            share = base_share + (remainder if idx == 0 else 0)  # 餘數歸給第一位（通常是付款人自己）
            cur.execute(
                """INSERT INTO order_items (group_id, order_code, order_id, buyer_id, buyer_name, item_name, price)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (group_id, code_str, order_pk, p["user_id"], p["display_name"], item_label, share)
            )
    return code_str

def try_handle_group_split_reply(group_id: str, event, clean_text: str, creator_id: str, reply_token: str) -> bool:
    """處理群組團單詢問後，使用者回覆「均分／@tag／跳過」"""
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
    real_tagged_ids = get_real_mentions(event)
    is_skip = any(k in clean_text for k in ["跳過", "不分攤", "算了", "略過"])
    is_split_even = any(k in clean_text for k in ["均分", "平分", "平攤"])

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
        f"3️⃣ 回覆「跳過」→ 記一般花費，不分攤"
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
    except Exception as e:
        log_error("收據圖片下載", e, owner_id)
        send_line_reply(reply_token, "⚠️ 圖片下載失敗，請重新傳送一次。")
        return

    try:
        extraction = extract_receipt(image_bytes)
    except Exception as e:
        log_error("收據辨識", e, owner_id)
        send_line_reply(reply_token, "⚠️ 收據辨識失敗，可能圖片不夠清晰，請重新拍攝後再試一次。")
        return

    items = [{"item_name": i.item_name or "未命名品項", "price": i.price} for i in extraction.items if i.price > 0]
    if not items:
        send_line_reply(reply_token, "⚠️ 沒有辨識到任何品項金額，請確認收據是否清晰完整。")
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
        f"這筆怎麼記？\n1️⃣ 回覆「均分」→ 平分給已知群組成員\n2️⃣ tag 出實際分攤的人\n3️⃣ 回覆「跳過」→ 記一般花費\n\n"
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
    # 優先序：密碼驗證 > 行程「有/無」回覆 > 群組分攤「均分/@tag/跳過」回覆
    # 這幾層都是「上一則機器人訊息在等待使用者回覆」的情境，
    # 必須搶在核銷、開團等既有邏輯之前處理，否則會被其他規則誤判掉。
    # ====================================================
    _gate_text = user_text.replace("@記帳米粒", "").replace("記帳米粒", "").strip()

    if try_handle_test_mode_gate(owner_type, target_id, _gate_text, reply_token):
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
    # 🗓️ 【測試限定：行程模式 - 新增／查詢行程】
    # ====================================================
    if is_test_mode_active(owner_type, target_id, "itinerary"):
        if try_add_itinerary(owner_type, target_id, creator_id, clean_text, reply_token):
            return
        if try_list_itineraries(owner_type, target_id, clean_text, reply_token):
            return

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
        你是一個親切、幽默的記帳助理「記帳米粒」。目前位於【{root_collection}】環境，模式為【{current_mode}】。
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
    """🧾 測試限定：收據辨識入口。圖片訊息無法像文字一樣 @tag 機器人，
    因此只要該使用者/群組已開通「收據辨識」測試模式，收到圖片就會直接處理，
    不用另外要求 tag。"""
    if not DB_READY or not is_bot_enabled():
        return

    is_group = event.source.type == "group"
    creator_id = event.source.user_id
    target_id = event.source.group_id if is_group else creator_id
    owner_type = "group" if is_group else "user"
    reply_token = event.reply_token
    message_id = event.message.id

    if not is_test_mode_active(owner_type, target_id, "receipt_ocr"):
        return  # 未開通測試模式時，靜默忽略圖片，避免一般使用者誤傳照片收到困惑訊息

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