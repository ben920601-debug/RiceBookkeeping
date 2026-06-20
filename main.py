import os
import re
import json
import random
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Literal, List, Optional

# LINE SDK v3
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# Google GenAI & Firebase SDK
from google import genai
from google.genai import types
import firebase_admin
from firebase_admin import credentials, firestore

from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="飯糰小幫手 ｜ 個人群組雙軌無消耗版")

# ==========================================
# ⚙️ 1. 初始化
# ==========================================
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

line_handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

if os.path.exists("firebase-adminsdk.json"):
    try:
        cred = credentials.Certificate("firebase-adminsdk.json")
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("🔥 [DATABASE] Firestore 雙軌安全連線就位！", flush=True)
    except Exception as e:
        db = None
        print(f"❌ [DATABASE] 初始化異常: {e}", flush=True)
else:
    db = None
    print("❌ [DATABASE] 未尋獲 firebase-adminsdk.json！", flush=True)

# ==========================================
# 🛡️ 2. 強型別定義
# ==========================================
class SingleRecord(BaseModel):
    record_type: Literal["expense", "income"] = Field(default="expense")
    amount: int = Field(default=0)
    item: str = Field(default="")
    category: str = Field(default="生活雜費")
    note: str = Field(default="")

class SingleSettlement(BaseModel):
    payer_name: str = Field(description="付出款項還錢的人名字。若自稱我請填寫『發話者』")
    receiver_name: str = Field(description="收到款項拿回錢的人名字。若自稱我請填寫『發話者』")
    amount: int = Field(default=0)

class GroupOrderItem(BaseModel):
    buyer_name: str = Field(description="點餐者名字。若自稱我或空白請寫『發話者』")
    item_name: str = Field(description="品項名稱")
    price: int = Field(description="單價金額")

class SuperRouter(BaseModel):
    intent: Literal["record", "chat", "analyze", "order_item", "order_end", "order_start", "settle_start", "settle_pay", "settle_query"] = Field(
        description="核心意圖分流。record:普通記帳"
    )
    records: Optional[List[SingleRecord]] = Field(default_factory=list)
    settlement: Optional[SingleSettlement] = Field(default=None)
    order_items: Optional[List[GroupOrderItem]] = Field(default_factory=list)
    target_payer: Optional[str] = Field(default="")
    ai_reply: Optional[str] = Field(default="")

# ==========================================
# ⚡ 3. 暱稱快取工具
# ==========================================
def get_cached_nickname(target_id: str, user_id: str, is_group: bool) -> str:
    if not db: return "記帳夥伴"
    if not is_group: return "個人帳本主"
    try:
        member_ref = db.collection("groups").document(target_id).collection("members").document(user_id)
        doc_snap = member_ref.get()
        if doc_snap.exists:
            return doc_snap.to_dict().get("display_name", "群組夥伴")
    except Exception: pass
    return "群組夥伴"

# ==========================================
# 🌐 4. Webhook 核心流動控制（完美相容個人與群組）
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
    if not db: return
    
    user_text = event.message.text.strip()
    creator_id = event.source.user_id 
    is_group = event.source.type == "group"
    
    # 🎯 關鍵分流：群組就寫入 groups，個人私聊就寫入 users
    target_id = event.source.group_id if is_group else creator_id
    root_collection = "groups" if is_group else "users"

    # 1. 初始化或讀取當前模式（個人聊天永遠是 normal 常態模式）
    current_mode = "normal"
    active_code = ""
    master_payer_name = ""
    
    if is_group:
        group_doc_ref = db.collection("groups").document(target_id)
        group_snap = group_doc_ref.get()
        if group_snap.exists:
            g_data = group_snap.to_dict()
            current_mode = g_data.get("state", "normal")
            active_code = g_data.get("active_order_code", "")
            master_payer_name = g_data.get("master_payer", "")
        else:
            group_doc_ref.set({"group_id": target_id, "state": "normal", "created_at": datetime.utcnow()})
    else:
        # 個人模式下如果還沒有文件，就幫忙初始化
        user_doc_ref = db.collection("users").document(target_id)
        if not user_doc_ref.get().exists:
            user_doc_ref.set({"user_id": target_id, "created_at": datetime.utcnow()})

    # 2. 去噪過濾機制
    is_triggered = False
    if not is_group:
        is_triggered = True  # 個人私聊全部放行處理
    else:
        if any(kw in user_text for kw in ["@飯糰", "飯糰", "開團", "結單", "結算"]):
            is_triggered = True
        elif current_mode == "order" and re.search(r'\d+', user_text):
            is_triggered = True
        elif current_mode == "settle" and any(k in user_text for k in ["給", "還", "付", "誰沒", "未付"]):
            is_triggered = True

    if not is_triggered: return

    user_text = user_text.replace("@飯糰", "").replace("飯糰", "").strip()
    creator_name = get_cached_nickname(target_id, creator_id, is_group)

    # 🧠 3. 送入大腦拆解
    try:
        prompt = f"""
        你是一個記帳助理「飯糰小幫手」。目前環境是【{root_collection}】架構，模式為【{current_mode}】。
        請分析使用者的語意輸入：『{user_text}』進行強型別分流。
        
        【分流規則】：
        - 如果使用者是單純輸入花費（例如：晚餐 120、高鐵 1200），intent 務必填寫 "record"。
        """
        
        result = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SuperRouter, temperature=0.1)
        ).parsed
        
        # ----------------------------------------------------
        # 核心意圖處理（全面支援群組與個人）
        # ----------------------------------------------------
        
        # F. 常態模式普通記帳 (record) -> ⭐️ 修正：個人與群組皆完美支持！
        if result.intent == "record" and current_mode == "normal":
            if result.records:
                for rec in result.records:
                    if rec.amount > 0:
                        # 依據 root_collection (users 或 groups) 動態寫入正確的位置
                        db.collection(root_collection).document(target_id).collection("expenses").document().set({
                            "type": rec.record_type, 
                            "amount": rec.amount, 
                            "item": rec.item, 
                            "category": rec.category,
                            "timestamp": datetime.utcnow(), 
                            "created_by_name": creator_name
                        })

        # A. 開團 (order_start) - 僅限群組
        elif result.intent == "order_start" and is_group:
            db.collection("groups").document(target_id).update({
                "state": "order", "active_order_code": str(random.randint(1000, 9999)), "order_items_temp": []
            })

        # B. 點餐品項蒐集 (order_item) - 僅限群組
        elif result.intent == "order_item" and current_mode == "order" and is_group:
            if result.order_items:
                g_ref = db.collection("groups").document(target_id)
                temp_items = g_ref.get().to_dict().get("order_items_temp", [])
                for item in result.order_items:
                    buyer = creator_name if item.buyer_name == "發話者" or not item.buyer_name else item.buyer_name.strip()
                    temp_items.append({
                        "buyer": buyer, "item": item.item_name, "price": item.price, "timestamp": datetime.utcnow().isoformat()
                    })
                g_ref.update({"order_items_temp": temp_items})

        # C. 截止結單 (order_end) - 僅限群組
        elif result.intent == "order_end" and current_mode == "order" and is_group:
            g_ref = db.collection("groups").document(target_id)
            g_data = g_ref.get().to_dict()
            temp_items = g_data.get("order_items_temp", [])
            if temp_items:
                m_payer = creator_name if not result.target_payer or result.target_payer == "發話者" else result.target_payer.strip()
                total_amt = sum(i["price"] for i in temp_items)
                code_str = g_data.get("active_order_code", str(random.randint(1000, 9999)))
                order_doc_id = f"{datetime.now().strftime('%Y%m%d')}_{code_str}"
                
                g_ref.collection("orders").document(order_doc_id).set({
                    "order_date": datetime.now().strftime("%Y-%m-%d"), "order_code": code_str,
                    "total_amount": total_amt, "master_payer_name": m_payer, "items": temp_items, "timestamp": datetime.utcnow()
                })
            g_ref.update({"state": "normal", "order_items_temp": []})

        # D. 開啟催款控制台 (settle_start) - 僅限群組
        elif result.intent == "settle_start" and is_group:
            match_code = re.search(r'(\d{4})', user_text)
            if match_code:
                db.collection("groups").document(target_id).update({"state": "settle", "active_order_code": match_code.group(1)})

        # E. 登記付款核銷 (settle_pay) - 僅限群組
        elif result.intent == "settle_pay" and current_mode == "settle" and is_group:
            if result.settlement:
                s = result.settlement
                p_name = creator_name if s.payer_name == "發話者" or not s.payer_name else s.payer_name
                r_name = master_payer_name if s.receiver_name == "發話者" or not s.receiver_name else s.receiver_name
                if p_name != r_name:
                    db.collection("groups").document(target_id).collection("settlements").document().set({
                        "payer_name": p_name, "receiver_name": r_name, "amount": s.amount, "order_code_ref": active_code, "timestamp": datetime.utcnow()
                    })

    except Exception as e:
        print(f"🧠 雙軌大腦處理異常: {e}")

@app.get("/")
def health_check(): 
    return {"status": "dual_mode_active"}
