"""
LINE SDK / Gemini 客戶端初始化，以及所有功能模組都會用到的共用互動函式：
回覆訊息、主動推播、解析 @tag、查詢/快取成員暱稱、下載圖片。
"""
import re

import httpx
import certifi

from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from google import genai

from app.config import LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, GEMINI_API_KEY
from app.db import db_cursor, is_db_ready
from app.logging_utils import log_error, log_stat_event

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GEMINI_API_KEY)


def send_line_reply(reply_token: str, text: str):
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
            )
        log_stat_event("reply")
    except Exception as e:
        log_error("LINE回覆", e)


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


def get_real_mentions(event) -> list:
    """過濾掉機器人自身的 Tag，只抓取真實成員的 ID"""
    real_tagged_ids = []
    mention = getattr(event.message, "mention", None)
    if mention and mention.mentionees:
        text = getattr(event.message, "text", "")
        for m in mention.mentionees:
            u_id = getattr(m, "user_id", None)
            if u_id:
                try:
                    tagged_text = text[m.index: m.index + m.length]
                    if "米粒" in tagged_text:
                        continue
                except Exception:
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
            tagged_text = text[m.index: m.index + m.length]
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
    """升級為群組成員 API，未加好友也能抓到真實暱稱"""
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
    """查詢群組成員暱稱快取，查不到就打 LINE API 並寫回快取表"""
    if not is_db_ready() or not user_id:
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


def download_line_image(message_id: str) -> bytes:
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    res = httpx.get(url, headers=headers, timeout=15.0, verify=certifi.where())
    res.raise_for_status()
    return res.content
