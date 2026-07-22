"""
地理編碼與通勤時間概略估算。
測試階段用免費、免申請金鑰的 OpenStreetMap Nominatim 做地理編碼，
搭配 Haversine 公式算「直線距離」估算通勤時間 —— 不考慮實際路網、
路況等因素，僅供輔助參考。未來要提升精準度可換成 Google Maps API（需金鑰與計費帳號）。
"""
import math

import httpx
import certifi

from app.logging_utils import log_error


def geocode_location(location_name: str):
    """回傳 (lat, lon)；查不到則回傳 (None, None)"""
    try:
        headers = {"User-Agent": "RiceBookkeepingBot/1.0 (itinerary feature)"}
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
    """概略估算：市區均速抓開車 30km/h、步行 5km/h，僅供參考"""
    speed = 30 if mode == "drive" else 5
    return max(1, round((distance_km / speed) * 60))
