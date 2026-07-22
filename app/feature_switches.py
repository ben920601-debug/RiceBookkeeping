"""
功能狀態開關（正常／維護中／Beta）。中控台可以直接調整某個功能的狀態，
不用改程式碼、不用重新部署，bot 端最多 CACHE_TTL 秒內會讀到最新值。

「維護中」的功能會直接擋下，回覆固定的維護訊息（可在中控台自訂文字）；
「Beta」只是一個標記狀態，不影響功能是否可用，主要給監控後台的 UI 顯示標籤用。
"""
import time

from app.db import db_cursor, is_db_ready

CACHE_TTL = 5  # 秒
_cache = {"ts": 0, "switches": {}}

DEFAULT_MAINTENANCE_MESSAGE = "🛠️ 此功能維護中，請稍後再試。"


def _refresh_cache_if_stale():
    if time.time() - _cache["ts"] < CACHE_TTL:
        return
    if not is_db_ready():
        return
    try:
        with db_cursor() as cur:
            cur.execute("SELECT feature_key, status, label, maintenance_message FROM feature_switches")
            _cache["switches"] = {r["feature_key"]: r for r in cur.fetchall()}
            _cache["ts"] = time.time()
    except Exception as e:
        print(f"⚠️ 功能狀態快取更新失敗，沿用舊值: {e}", flush=True)


def get_feature_status(feature_key: str) -> str:
    """回傳 'normal' / 'maintenance' / 'beta'；查不到資料一律視為 normal（不影響原本行為）"""
    _refresh_cache_if_stale()
    row = _cache["switches"].get(feature_key)
    return row["status"] if row else "normal"


def is_feature_enabled(feature_key: str) -> bool:
    return get_feature_status(feature_key) != "maintenance"


def get_feature_maintenance_message(feature_key: str) -> str:
    _refresh_cache_if_stale()
    row = _cache["switches"].get(feature_key)
    if row and row.get("maintenance_message"):
        return row["maintenance_message"]
    return DEFAULT_MAINTENANCE_MESSAGE


def get_all_feature_statuses() -> dict:
    """回傳 {feature_key: status}，供監控後台的 REST API 一次性提供給前端顯示標籤用"""
    _refresh_cache_if_stale()
    return {k: v["status"] for k, v in _cache["switches"].items()}
