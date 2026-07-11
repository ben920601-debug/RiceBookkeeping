import os
import re
import json
import random
import httpx
from datetime import datetime
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
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# Google GenAI & MySQL
from google import genai
from google.genai import types
import pymysql
from pymysql.cursors import DictCursor

from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="記帳米粒 ｜ V1.2 MySQL 版")

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

def get_db_connection():
    """建立一個新的 MySQL 連線；用完即關閉，避免長連線逾時被資料庫斷開"""
    return pymysql.connect(**MYSQL_CONFIG)

@contextmanager
def db_cursor():
    """統一管理連線與游標的 context manager，離開時自動關閉連線"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()

DB_READY = False
try:
    _test_conn = get_db_connection()
    _test_conn.close()
    DB_READY = True
    print("🔥 [DATABASE] MySQL 連線就位！", flush=True)
except Exception as e:
    DB_READY = False
    print(f"❌ [DATABASE] MySQL 連線初始化異常: {e}", flush=True)

# ==========================================
# 🛡️ 2. 全域型別與 V1.1 特定詞設定
# ==========================================
SENSITIVE_KEYWORDS = ["政治", "選舉", "總統", "政黨", "戰爭", "吸毒", "賭博", "情色", "自殺", "殺人"]

# 🚀 V1.1 新增：特定詞觸發指定回覆話語（快速硬編碼攔截層）
SPECIFIC_KEYWORDS = {
    "電鍋": (
        "說到電鍋我最熟了，畢竟他也是我的創造者！\n"
        "他創造我之外呢，也創造了飯匙在不同地方服務大眾😄\n"
        "如有興趣，歡迎到下方點選前往IG或是找@denguword1220\n"
        "非常期待與您有更多的互動😆"

    ),
    "思妤是誰？": (
        "思妤是一個瘋女人！\n"
        "電鍋都會叫他狗東西\n"
        "因為他真的太狗了，快受不了\n"
        "好啦，還是很喜歡他的，嘻嘻😁"
    ),
    "欣俞是誰？": (
        "欣俞是一個非常稱職的店長媽媽！\n"
        "因為他三不五時要照顧我們這些小朋友\n"
        "儘管他很累，但總是先為我們著想\n"
        "謝謝媽媽👩"
    ),
    "哲宇是誰？": (
        "哲宇是一個非常稱職的店長！\n"
        "沒有他管不好的店，只有聽不懂人話員工\n"
        "尤其是他吼人的時候好帥喔😆"
    )
}

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
    except Exception as e:
        print(f"❌ LINE 回覆失敗: {e}", flush=True)

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
                res = httpx.get(url, headers=headers, timeout=5.0, follow_redirects=True)
                if res.status_code == 200:
                    return res.json().get("displayName", f"成員({user_id[:4]})")
                else:
                    print(f"⚠️ LINE API 回傳狀態碼: {res.status_code}, 網址: {res.url}", flush=True)
            except Exception as e:
                print(f"⚠️ 請求群組 API 異常: {e}", flush=True)
            
    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    try:
        res = httpx.get(url, headers=headers, timeout=5.0, follow_redirects=True)
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

    user_text = event.message.text.strip()
    creator_id = event.source.user_id 
    is_group = event.source.type == "group"
    reply_token = event.reply_token 
    
    target_id = event.source.group_id if is_group else creator_id
    root_collection = "groups" if is_group else "users"  # 保留供語意參考，實際寫入以 owner_type 欄位區分

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
            print(f"❌ 群組狀態查詢異常: {e}", flush=True)
            return

    is_bot_tagged = False
    mention = getattr(event.message, "mention", None)
    if mention and mention.mentionees: is_bot_tagged = True
    if any(kw in user_text for kw in ["@記帳米粒", "記帳米粒"]): is_bot_tagged = True
    if is_group and not is_bot_tagged: return 

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
            print(f"❌ 核銷解鎖查詢異常: {e}", flush=True)
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
                print(f"❌ 結算關閉異常: {e}", flush=True)
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
                    print(f"❌ 核銷寫入異常: {e}", flush=True)
                    return

                if final_payer_id == final_receiver_id:
                    send_line_reply(reply_token, f"✅ 【單號 #{active_code} 核銷成功】\n🙋‍♂️ 自行核銷：{payer_name_str}\n💰 紀錄金額：${settle_amount}")
                else:
                    send_line_reply(reply_token, f"✅ 【單號 #{active_code} 核銷成功】\n💸 付款：{payer_name_str}\n📥 收款：{receiver_name_str}\n💰 紀錄金額：${settle_amount}")
                return

    # 移除 Tag 符號以利後續關鍵字或 Regex 精準匹配
    clean_text = user_text.replace("@記帳米粒", "").replace("記帳米粒", "").strip()

    # ====================================================
    # 🎯 ⚡ 【V1.1 Python 層攔截：新增特定詞觸發指定回覆】
    # ====================================================
    for kw, reply_msg in SPECIFIC_KEYWORDS.items():
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
            "👉 輸入「項目 金額」即可\n"
            "   └ 範例：早餐 80 元\n\n"
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

    for kw in SENSITIVE_KEYWORDS:
        if kw in clean_text:
            send_line_reply(reply_token, "🤖 米粒為純財務助理，請勿探討敏感議題喔！")
            return

    # ====================================================
    # ⚡ 🚀 【Python 第一層極速攔截：代點單與記帳直通落庫】
    # ====================================================
    fast_match = re.fullmatch(r'^(.+?)\s*(\d+)\s*(?:元|塊)?$', clean_text)
    if fast_match and current_mode in ["normal", "order"]:
        raw_item_name = fast_match.group(1).strip()
        amount = int(fast_match.group(2))
        
        item_name = re.sub(r'@\S+', '', raw_item_name).strip()
        
        if not item_name.isdigit() and amount > 0:
            if current_mode == "normal":
                creator_name_str = resolve_id_to_name(target_id, creator_id)
                owner_type = "group" if is_group else "user"
                try:
                    with db_cursor() as cur:
                        cur.execute(
                            """INSERT INTO expenses
                               (owner_type, owner_id, record_type, amount, item, category, created_by_uid, created_by_name)
                               VALUES (%s, %s, 'expense', %s, %s, '生活雜費', %s, %s)""",
                            (owner_type, target_id, amount, item_name, creator_id, creator_name_str)
                        )
                    send_line_reply(reply_token, f"✅ 已紀錄：{item_name} ${amount}")
                except Exception as e:
                    print(f"❌ 記帳寫入異常: {e}", flush=True)
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
                    print(f"❌ 團購品項寫入異常: {e}", flush=True)
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
                    print(f"❌ AI 記帳寫入異常: {e}", flush=True)
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
                print(f"❌ 開團寫入異常: {e}", flush=True)

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
                    print(f"❌ AI 團購品項寫入異常: {e}", flush=True)

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
                print(f"❌ 結單處理異常: {e}", flush=True)

        # 5. 純粹對話陪聊
        elif result.intent == "chat" and result.ai_reply:
            send_line_reply(reply_token, f"🤖 {result.ai_reply}")

    except Exception as e:
        print(f"🧠 解析異常: {e}")

@app.get("/")
def health_check(): 
    return {"status": "mysql_active", "version": "v1.2-MySQL"}

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
