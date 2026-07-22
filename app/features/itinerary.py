"""
旅行模式：多輪對話式行程規劃。

流程：旅行模式 → 問出發時間 → 問回程時間 → 建立草案
     → 逐則輸入地點名稱（無須附時間）→ 米粒依地名判斷地址 → 輸入「結束」
     → 米粒一次判斷拜訪順序並分配時間（無閒聊，直接給結果）
     → 使用者「確定」寫入資料庫，或直接描述修改內容（套用在草稿，尚未寫入DB）
     → 確定後以出發日期作為旅行單號，之後可用「旅行修改 單號」重新進入編輯

另外也包含行程提醒的背景排程（每 60 秒檢查一次，出發前 45 分鐘推播提醒）。
"""
import re
import json
import random
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional, Literal

from pydantic import BaseModel, Field

from app.db import db_cursor, is_db_ready
from app.logging_utils import log_error
from app.line_client import send_line_reply, push_line_message, ai_client
from google.genai import types
from app.geo import geocode_location, haversine_km, estimate_travel_minutes
from app.features.test_mode import is_test_mode_active

# ==========================================
# 🧳 7. 旅行模式：多輪對話式行程規劃
# ------------------------------------------
# 流程：旅行模式 → 問出發時間 → 問回程時間 → 建立草案
#      → 逐則輸入地點名稱（無須附時間）→ 米粒依地名判斷地址 → 輸入「結束」
#      → AI 一次判斷拜訪順序並分配時間（無閒聊，直接給結果）
#      → 使用者「確定」寫入資料庫，或直接描述修改內容（套用在草稿，尚未寫入DB）
#      → 確定後以出發日期作為旅行單號，之後可用「旅行修改 單號」重新進入編輯
# ==========================================
DATETIME_PATTERN_COLON = re.compile(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})')
DATETIME_PATTERN_COMPACT = re.compile(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{2})(\d{2})(?=\s|$)')
TRIP_MODIFY_PATTERN = re.compile(r'^旅行修改\s*[+＋]?\s*(\d{6,20}(?:-\d+)?)$')

def parse_datetime_prefix(text: str):
    """嘗試從文字開頭解析日期時間（支援「YYYY-MM-DD HH:MM」與「YYYY/M/D HHMM」兩種格式），
    回傳 (datetime, 剩餘文字)；解析不到回傳 (None, None)。用於出發/回程時間輸入。"""
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
    """交由 Gemini 判讀日期時間（規則沒抓到格式時的備援，僅用於出發/回程時間）"""
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

class PlaceAddressExtraction(BaseModel):
    place_name: str = Field(default="")
    address: str = Field(default="")
    recognized: bool = Field(default=False)

def ai_search_place_address(text: str):
    """給一個地名／關鍵字，由米粒(AI)依既有知識判斷最可能對應的地點名稱與完整地址（台灣地址優先）。
    不是即時上網搜尋，僅供輔助判讀，判斷不出來或不確定時交還原文字讓使用者自行輸入地址。"""
    prompt = (
        f"請根據這個地名或關鍵字，判斷最可能對應的地點名稱與完整地址（台灣地址優先）。"
        f"若不確定或無法判斷，請將 recognized 設為 false，不要亂猜。\n地名：『{text}』"
    )
    try:
        result = ai_client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=PlaceAddressExtraction, temperature=0.2),
        ).parsed
        if result and result.recognized and result.address:
            return (result.place_name.strip() or text), result.address.strip()
    except Exception as e:
        log_error("地點地址搜尋", e)
    return None, None

def resolve_location_input(text: str):
    """統一的地點輸入解析入口：回傳 (location_name, lat, lon)。
    由米粒依地名判斷實際地址，再用地址做地理編碼；判斷不出來就直接拿原文字做地理編碼，
    地址不準確的話可在最後總覽階段輸入「把第N項改成正確地址」修正。"""
    text = text.strip()
    name, address = ai_search_place_address(text)
    if address:
        lat, lon = geocode_location(address)
        display_name = f"{name}（{address}）" if name and name != address else address
        return display_name, lat, lon
    lat, lon = geocode_location(text)
    return text, lat, lon

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

def generate_trip_code(owner_type: str, owner_id: str, departure_at: datetime) -> str:
    base = departure_at.strftime("%Y%m%d")
    with db_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM trips WHERE owner_type=%s AND owner_id=%s AND trip_code LIKE %s",
            (owner_type, owner_id, f"{base}%")
        )
        cnt = cur.fetchone()["cnt"]
    return base if cnt == 0 else f"{base}-{cnt + 1}"

# --- AI 一次性安排拜訪順序與時間（取代逐項詢問，且不含任何閒聊） ---
class TripStopPlan(BaseModel):
    original_index: int = Field(default=0)
    datetime_str: str = Field(default="")

class TripArrangement(BaseModel):
    stops: List[TripStopPlan] = Field(default_factory=list)

def ai_arrange_trip(departure_at: datetime, return_at: datetime, locations: list) -> list:
    """locations: [{"location_name","lat","lon"}, ...]（無序、無時間）
    回傳依建議順序排列、並附上建議時間的清單：[{"location_name","lat","lon","scheduled_at"(ISO字串)}, ...]
    務必保證涵蓋所有輸入地點，AI若遺漏會由程式碼兜底補上，不會憑空遺失使用者輸入的地點。"""
    loc_lines = []
    for i, l in enumerate(locations):
        coord_note = f"（座標：{l['lat']:.4f},{l['lon']:.4f}）" if l.get("lat") is not None else ""
        loc_lines.append(f"{i + 1}. {l['location_name']}{coord_note}")
    prompt = (
        f"這是一趟旅行，出發時間：{departure_at.strftime('%Y-%m-%d %H:%M')}，"
        f"回程時間：{return_at.strftime('%Y-%m-%d %H:%M')}。\n"
        f"以下是使用者提供的地點（編號僅為輸入順序，不代表拜訪順序）：\n" + "\n".join(loc_lines) + "\n\n"
        f"請安排一個合理的拜訪順序，並給每個地點分配具體到達時間（YYYY-MM-DD HH:MM），"
        f"時間需介於出發與回程之間，並依地點間距離給予合理間隔（至少間隔1小時，跨天則安排在適當時段）。"
        f"stops 陣列的排列順序就是建議的拜訪順序，每個元素的 original_index 對應到上面的編號，"
        f"務必涵蓋全部 {len(locations)} 個地點，不可遺漏或重複。"
    )
    arranged = []
    used_indices = set()
    try:
        result = ai_client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=TripArrangement, temperature=0.2),
        ).parsed
        if result and result.stops:
            for stop in result.stops:
                idx = stop.original_index - 1
                if 0 <= idx < len(locations) and idx not in used_indices:
                    try:
                        dt = datetime.strptime(stop.datetime_str, "%Y-%m-%d %H:%M")
                    except Exception:
                        continue
                    loc = locations[idx]
                    arranged.append({"location_name": loc["location_name"], "lat": loc.get("lat"), "lon": loc.get("lon"), "scheduled_at": dt.isoformat()})
                    used_indices.add(idx)
    except Exception as e:
        log_error("AI旅行安排", e)

    # 兜底：AI 若遺漏或整個判讀失敗，把沒被安排到的地點依序補在最後，時間平均分配於出發~回程之間
    missing = [i for i in range(len(locations)) if i not in used_indices]
    if missing:
        span_seconds = max(3600.0, (return_at - departure_at).total_seconds())
        step = span_seconds / (len(missing) + 1)
        base_dt = datetime.fromisoformat(arranged[-1]["scheduled_at"]) if arranged else departure_at
        for n, idx in enumerate(missing, start=1):
            loc = locations[idx]
            dt = base_dt + timedelta(seconds=step * n)
            if dt > return_at:
                dt = return_at
            arranged.append({"location_name": loc["location_name"], "lat": loc.get("lat"), "lon": loc.get("lon"), "scheduled_at": dt.isoformat()})

    arranged.sort(key=lambda x: x["scheduled_at"])
    return arranged

def build_draft_summary_text(arranged: list) -> str:
    if not arranged:
        return "（目前尚未安排任何地點）"
    lines = []
    for i, a in enumerate(arranged):
        dt = datetime.fromisoformat(a["scheduled_at"])
        lines.append(f"{i + 1}. {dt.strftime('%m/%d %H:%M')}　{a['location_name']}")
    return "\n".join(lines)

def send_trip_review_draft(arranged: list, reply_token: str):
    """顯示AI安排結果，直接給結果、不加任何閒聊評論"""
    summary = build_draft_summary_text(arranged)
    send_line_reply(
        reply_token,
        f"🗺️ 【米粒安排結果】\n{summary}\n\n"
        f"回覆「確定」完成規劃並寫入資料庫，或直接輸入想修改的內容（例如：把第2項改成15:00、刪除第3項、新增 台北101）。"
    )

# --- 修改指示的 AI 判讀（套用在草稿上，尚未寫入資料庫） ---
class TripModification(BaseModel):
    action: Literal["edit", "delete", "add", "update_times", "unclear"] = Field(default="unclear")
    target_index: Optional[int] = Field(default=None)
    new_datetime_str: str = Field(default="")
    new_location: str = Field(default="")
    new_departure_str: str = Field(default="")
    new_return_str: str = Field(default="")

def ai_apply_trip_modification(summary_text: str, departure_at: datetime, return_at: datetime, user_text: str) -> TripModification:
    prompt = (
        f"目前旅行行程草稿如下（編號. 時間 地點）：\n{summary_text}\n"
        f"出發時間：{departure_at.strftime('%Y-%m-%d %H:%M')}，回程時間：{return_at.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"使用者想這樣修改：『{user_text}』\n\n"
        f"請判斷這是以下哪一種操作，並填入對應欄位（用不到的欄位留空字串）：\n"
        f"- edit：修改某一項的時間或地點（填 target_index，以及要改的 new_datetime_str 和/或 new_location）\n"
        f"- delete：刪除某一項（填 target_index）\n"
        f"- add：新增一項（填 new_location，new_datetime_str 可留空由系統自動安排）\n"
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

    # 「旅行修改 單號」：把既有旅行（不論是否已確認）的行程項目載入草稿，重新進入編輯
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
        try:
            with db_cursor() as cur:
                cur.execute(
                    "SELECT scheduled_at, location_name, latitude, longitude FROM itineraries WHERE trip_id=%s ORDER BY scheduled_at ASC",
                    (trip["id"],)
                )
                rows = cur.fetchall()
        except Exception as e:
            log_error("旅行修改-載入項目", e, owner_id)
            send_line_reply(reply_token, "⚠️ 查詢失敗，請稍後再試一次。")
            return True
        arranged = [{
            "location_name": r["location_name"],
            "lat": float(r["latitude"]) if r["latitude"] is not None else None,
            "lon": float(r["longitude"]) if r["longitude"] is not None else None,
            "scheduled_at": r["scheduled_at"].isoformat(),
        } for r in rows]
        set_trip_session(owner_type, owner_id, "pending_review", trip_id=trip["id"], draft={"arranged": arranged})
        send_trip_review_draft(arranged, reply_token)
        return True

    # 觸發詞：開始一趟新旅行（前提：目前沒有進行中的規劃對話）
    if "旅行模式" in clean_text and not session:
        set_trip_session(owner_type, owner_id, "pending_departure")
        send_line_reply(
            reply_token,
            "🧳 開始規劃新旅行！請問這趟旅行的出發時間？\n"
            "（格式：2026-07-19 19:00 或 2026/7/19 1900，也可以直接描述，米粒會協助判讀）\n"
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

        set_trip_session(owner_type, owner_id, "collecting", trip_id=trip_pk, draft={"locations": []})
        send_line_reply(
            reply_token,
            f"✅ 已建立旅行草案！\n📅 {departure_at.strftime('%Y-%m-%d %H:%M')} → {dt.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"請依序輸入想去的地點名稱（不用附時間），米粒會協助搜尋地址；地址如果不準確，最後總覽時可以直接說要修正。\n"
            f"全部輸入完畢後，請輸入「結束」，米粒會直接安排拜訪順序與時間。"
        )
        return True

    # --- Stage 3：收集地點（無須時間、無須逐項確認） ---
    if stage == "collecting":
        trip_id = session["trip_id"]
        draft = json.loads(session["draft_json"] or '{"locations": []}')

        if clean_text in ("結束", "完成", "結束規劃"):
            if not draft.get("locations"):
                send_line_reply(reply_token, "⚠️ 目前還沒有任何地點，請先輸入至少一個地點名稱。")
                return True
            try:
                with db_cursor() as cur:
                    cur.execute("SELECT * FROM trips WHERE id=%s", (trip_id,))
                    trip = cur.fetchone()
            except Exception as e:
                log_error("旅行查詢", e, owner_id)
                send_line_reply(reply_token, "⚠️ 查詢失敗，請稍後再試一次。")
                return True
            arranged = ai_arrange_trip(trip["departure_at"], trip["return_at"], draft["locations"])
            draft["arranged"] = arranged
            set_trip_session(owner_type, owner_id, "pending_review", trip_id=trip_id, draft=draft)
            send_trip_review_draft(arranged, reply_token)
            return True

        name, lat, lon = resolve_location_input(clean_text)
        draft.setdefault("locations", []).append({"location_name": name, "lat": lat, "lon": lon})
        set_trip_session(owner_type, owner_id, "collecting", trip_id=trip_id, draft=draft)
        geo_note = "" if lat is not None else "（座標查詢失敗，仍會加入，不影響後續安排）"
        send_line_reply(
            reply_token,
            f"📍 已加入：{name}{geo_note}（目前共 {len(draft['locations'])} 個地點）\n繼續輸入下一個地點，或輸入「結束」開始安排。"
        )
        return True

    # --- Stage 4：等待總覽確認或修改指示（作用於草稿，尚未寫入資料庫） ---
    if stage == "pending_review":
        trip_id = session["trip_id"]
        draft = json.loads(session["draft_json"] or "{}")
        arranged = draft.get("arranged", [])
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
            trip_code = trip["trip_code"] if trip["status"] == "confirmed" else generate_trip_code(owner_type, owner_id, trip["departure_at"])
            try:
                with db_cursor() as cur:
                    cur.execute("DELETE FROM itineraries WHERE trip_id=%s", (trip_id,))
                    for a in arranged:
                        cur.execute(
                            """INSERT INTO itineraries (owner_type, owner_id, trip_id, scheduled_at, location_name, latitude, longitude, created_by_uid)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                            (owner_type, owner_id, trip_id, datetime.fromisoformat(a["scheduled_at"]), a["location_name"], a.get("lat"), a.get("lon"), creator_id)
                        )
                    cur.execute("UPDATE trips SET status='confirmed', trip_code=%s WHERE id=%s", (trip_code, trip_id))
            except Exception as e:
                log_error("旅行確認寫入", e, owner_id)
                send_line_reply(reply_token, "⚠️ 確認失敗，請稍後再試一次。")
                return True
            clear_trip_session(owner_type, owner_id)
            send_line_reply(
                reply_token,
                f"🎉 旅行規劃完成！旅行單號：#{trip_code}\n"
                f"👉 之後可輸入「旅行修改 {trip_code}」重新編輯，行程開始前 45 分鐘我會主動提醒您！"
            )
            return True

        # 其餘文字視為修改指示，交給 AI 判讀，套用在草稿（尚未寫入資料庫）
        summary = build_draft_summary_text(arranged)
        mod = ai_apply_trip_modification(summary, trip["departure_at"], trip["return_at"], clean_text)

        if mod.action == "delete" and mod.target_index and 1 <= mod.target_index <= len(arranged):
            arranged.pop(mod.target_index - 1)
        elif mod.action == "edit" and mod.target_index and 1 <= mod.target_index <= len(arranged):
            target = arranged[mod.target_index - 1]
            if mod.new_datetime_str:
                try:
                    target["scheduled_at"] = datetime.strptime(mod.new_datetime_str, "%Y-%m-%d %H:%M").isoformat()
                except Exception:
                    pass
            if mod.new_location:
                name, lat, lon = resolve_location_input(mod.new_location)
                target["location_name"], target["lat"], target["lon"] = name, lat, lon
            arranged.sort(key=lambda x: x["scheduled_at"])
        elif mod.action == "add" and mod.new_location:
            name, lat, lon = resolve_location_input(mod.new_location)
            try:
                dt = datetime.strptime(mod.new_datetime_str, "%Y-%m-%d %H:%M") if mod.new_datetime_str else trip["departure_at"]
            except Exception:
                dt = trip["departure_at"]
            arranged.append({"location_name": name, "lat": lat, "lon": lon, "scheduled_at": dt.isoformat()})
            arranged.sort(key=lambda x: x["scheduled_at"])
        elif mod.action == "update_times":
            try:
                new_dep = datetime.strptime(mod.new_departure_str, "%Y-%m-%d %H:%M") if mod.new_departure_str else trip["departure_at"]
                new_ret = datetime.strptime(mod.new_return_str, "%Y-%m-%d %H:%M") if mod.new_return_str else trip["return_at"]
                with db_cursor() as cur:
                    cur.execute("UPDATE trips SET departure_at=%s, return_at=%s WHERE id=%s", (new_dep, new_ret, trip_id))
                trip["departure_at"], trip["return_at"] = new_dep, new_ret
            except Exception as e:
                log_error("旅行修改-時間", e, owner_id)
        else:
            send_line_reply(reply_token, "⚠️ 不太確定您要修改的內容，請具體描述，例如：「把第2項改成15:00」「刪除第3項」「新增 台北101」。")
            return True

        draft["arranged"] = arranged
        set_trip_session(owner_type, owner_id, "pending_review", trip_id=trip_id, draft=draft)
        send_trip_review_draft(arranged, reply_token)
        return True

    return False


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
    if not is_db_ready():
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
