"""
全域設定與環境變數。
其他模組一律從這裡 import 設定值，不要各自 os.getenv，
避免同一個變數在不同檔案裡讀到不一致的預設值。
"""
import os
import certifi
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

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 🧪 測試限定功能（旅行模式／群組團單／收據辨識）共用的驗證密碼與開通時數
TEST_MODE_PASSWORD = os.getenv("TEST_MODE_PASSWORD", "")
TEST_MODE_HOURS = int(os.getenv("TEST_MODE_HOURS", "16"))

MY_LIFF_ID = "2010446205-W1G1WDQQ"

MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER"),
    "password": os.getenv("MYSQL_PASSWORD"),
    "database": os.getenv("MYSQL_DATABASE", "jizhang_mili"),
    "charset": "utf8mb4",
    "autocommit": True,
}
