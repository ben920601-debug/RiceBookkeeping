import os
import re
import json
import random
import asyncio
import httpx
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Literal, List, Optional

# LINE SDK v3
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# Google GenAI & Firebase SDK
from google import genai
from google.genai import types
import firebase_admin
from firebase_admin import credentials, firestore

from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="飯糰小幫手 ｜ 揪團結算 SaaS 完全體")

# ==========================================
# ⚙️ 1. 核心客戶端與資料庫初始化
# ==========================================
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

MY_LIFF_ID = "2010446205-W1G1WDQQ" 

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

if os.path.exists("firebase-adminsdk.json"):
    try:
        cred = credentials.Certificate("firebase-adminsdk.json")
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("🔥 [DATABASE] 成功建立 Firestore 安全連線通道！", flush=True)
    except Exception as e:
        db = None
else:
    db = None

# ==========================================
# 🛡️ 2. 全域狀態機與強型別定義
# ==========================================
SENSITIVE_KEYWORDS = ["政治", "選舉", "總統", "政黨", "戰爭", "吸毒", "賭博", "情色", "自殺", "殺人"]
PENDING_CONFIRMATIONS = {}

# 🚀 【核心狀態機快取】紀錄目前各個群組的運作模式
# 格式: { "group_id": { "mode": "order"/"settle", "data": {...} } }
GROUP_STATES = {}

class SingleRecord(BaseModel):
    record_type: Literal["expense", "income"] = Field(default="expense")
    amount: int = Field(default=0)
    item: str = Field(default="")
    category: str = Field(default="生活雜費")
    note: str = Field(default="")

class SingleSettlement(BaseModel):
    payer_name: str = Field(description="付出款項、要把錢還給別人的那個人名字")
    receiver_name: str = Field(description="收到款項、拿回錢的那個人名字")
    amount: int = Field(default=0, description="核銷、還錢的具體金額")

class GroupOrderItem(BaseModel):
    buyer_name: str = Field(description="點餐或購買這個東西的人的名字，如果自稱我，請寫發話者")
    item_name: str = Field(description="購買的品項名稱")
    price: int = Field(description="該品項的單價金額")

class SuperRouter(BaseModel):
    intent: Literal["record", "chat_with_record", "chat", "analyze", "sensitive", "settlement", "order_item", "order_end", "order_start", "settle_start", "settle_pay"] = Field(
        description="核心意圖分流。order_item:訂單模式中紀錄品項金額, order_end:訂單結束, order_start:開啟訂單, settle_start:進入結算, settle_pay:我給某某多少錢"
    )
    records: Optional[List[SingleRecord]] = Field(default_factory=list)
    settlement: Optional[SingleSettlement] = Field(default=None)
    order_items: Optional[List[GroupOrderItem]] = Field(default_factory=list, description="點單模式中拆解出來的品項與金額清單")
    target_payer: Optional[str] = Field(default="", description="訂單結束時，指定最後買單付款的人名字")
    target_order_id: Optional[str] = Field(default="", description="結算模式中，使用者輸入的日期與編號代碼，例如 0620 #8821")
    ai_reply: Optional[str] = Field(default="")

# ==========================================
# ⚡ 3. 核心工具組
# ==========================================
def get_line_user_profile(user_id: str) -> str:
    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    try:
        res = httpx.get(url, headers=headers, timeout=5.0)
        if res.status_code == 200: return res.json().get("displayName", "未知成員")
    except Exception: pass
    return "群組夥伴"

def analyze_with_gemini_sync(user_text: str, current_mode: str = "normal") -> SuperRouter:
    """【大腦】根據目前的群組狀態模式，動態調整 Prompt 節流過濾"""
    prompt = f"""
    你是一個具備頂級控場能力的記帳助理「飯糰小幫手」。目前群組處於【{current_mode}】模式。
    請透視分析使用者的語意輸入：『{user_text}』進行強型別分流。
    
    【模式分流規則】：
    1. 如果提及「開啟訂單」、「團購開始」、「開團」，intent 務必歸為 order_start。
    2. 如果提及「訂單結束」、「結單」、「截止」，intent 務必歸為 order_end。
    3. 如果提及「訂單結算」、「結算訂單」，intent 務必歸為 settle_start。
    """
    
    if current_mode == "order":
        prompt += """
        4. 當前為【訂單模式】：群組成員正在點單。只要有提到品項和金額（例如：牛肉麵 150、大杯珍奶 60、小明點了排骨飯95），
           請將 intent 歸類為 order_item，並精準拆解到 order_items 陣列。
           如果對話內容完全沒有提及任何金額與物品（純日常對話閒聊），請將 intent 歸類為 chat，且 ai_reply 留空。
        """
    elif current_mode == "settle":
        prompt += """
        5. 當前為【結算模式】：成員正在平帳交錢。使用者輸入格式必須符合「我給了某某多少錢」或「阿誠 給 小明 150」（不論有沒有tag），
           請將 intent 歸類為 settle_pay，並拆解到 settlement 結構中（若自稱我，名字請填發話者）。
           如果是其他任何無關文字對話，請將 intent 歸類為 chat，且 ai_reply 留空（我們將予以封鎖無視）。
        """
    else:
        prompt += """
        6. 當前為【常態模式】：支援普通記帳(record)與普通核銷平帳(settlement)與分析(analyze)。
        """

    response = ai_client.models.generate_content(
        model='gemini-2.5-flash', contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SuperRouter, temperature=0.1),
    )
    if response.parsed: return response.parsed
    return SuperRouter(**json.loads(response.text))

# ==========================================
# 🧠 4. 核心 Webhook 邏輯與狀態機切換器
# ==========================================
@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature")
    if not signature: raise HTTPException(status_code=400, detail="Missing Signature")
    body = await request.body()
    body_str = body.decode("utf-8")
    if body_str and '"text":"請教導我該如何使用？"' in body_str: return Response(content="OK", status_code=200)
    background_tasks.add_task(handle_line_events_safe, body_str, signature)
    return Response(content="OK", status_code=200)

def handle_line_events_safe(body_str: str, signature: str):
    try: handler.handle(body_str, signature)
    except InvalidSignatureError: print("❌ 簽章密鑰驗證失敗！")

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_text = event.message.text.strip()
    creator_id = event.source.user_id 
    is_group = event.source.type == "group"
    target_id = event.source.group_id if is_group else creator_id

    # 📥 讀取或初始化群組當前狀態
    if target_id not in GROUP_STATES:
        GROUP_STATES[target_id] = {"mode": "normal", "order_items": []}
    
    current_state = GROUP_STATES[target_id]
    current_mode = current_state["mode"]

    # 🚀 【群組 Tag 與主動過濾核心】
    is_mentioned = False
    if is_group:
        mention = getattr(event.message, "mention", None)
        if mention and mention.mentionees: is_mentioned = True
        if any(kw in user_text for kw in ["@飯糰", "飯糰", "開啟訂單", "訂單結束", "訂單結算"]): is_mentioned = True
        
        # 💡 防火牆 A：若在訂單模式，且有提到數字（可能是點餐），強制放行給 AI 漏斗過濾
        if current_mode == "order" and re.search(r'\d+', user_text): is_mentioned = True
        # 💡 防火牆 B：若在結算模式，且提到「給」或「還」，強制放行核對
        if current_mode == "settle" and any(k in user_text for k in ["給", "還", "付"]): is_mentioned = True
        
        if not is_mentioned and creator_id not in PENDING_CONFIRMATIONS:
            return # 沒被叫到，直接安靜忽略

        user_text = user_text.replace("@飯糰", "").replace("飯糰", "").strip()

    reply_str = ""

    # 🧠 調度精準大腦分析
    try:
        result = analyze_with_gemini_sync(user_text, current_mode)
        creator_name = get_line_user_profile(creator_id)

        # 1️⃣ 意圖：開啟訂單
        if result.intent == "order_start":
            GROUP_STATES[target_id] = {"mode": "order", "order_items": []}
            reply_str = "🚀 【飯團團購模式・正式啟動】\n🤖 小幫手已進入高效點單過濾狀態！\n👉 請大家直接輸入「品項 + 金額」（例如：排骨飯 95），我會自動幫忙歸檔。非點單的閒聊文字我會主動無視喔！"

        # 2️⃣ 意圖：訂單模式中的品項紀錄
        elif result.intent == "order_item" and current_mode == "order":
            if result.order_items:
                for item in result.order_items:
                    buyer = item.buyer_name.strip()
                    if buyer == "發話者" or not buyer: buyer = creator_name
                    
                    # 暫存入全域快取
                    GROUP_STATES[target_id]["order_items"].append({
                        "buyer": buyer, "item": item.item_name, "price": item.price
                    })
                
                # 拼裝即時反饋
                lines = [f"・{i['buyer']}：{i['item']} ${i['price']}" for i in result.order_items]
                reply_str = "📝 【訂單已自動掛載】\n" + "\n".join(lines)
            else:
                return # 閒聊，直接無視

        # 3️⃣ 意圖：訂單結束（結單封裝）
        elif result.intent == "order_end" and current_mode == "order":
            order_items = current_state["order_items"]
            if not order_items:
                GROUP_STATES[target_id] = {"mode": "normal", "order_items": []}
                reply_str = "🛑 該團購因無人點單，已自動取消並退回常態模式。"
            else:
                master_payer = result.target_payer.strip() if result.target_payer else creator_name
                if master_payer == "發話者": master_payer = creator_name
                
                # 生成隨機單號與日期
                date_str = datetime.now().strftime("%m%d")
                code_str = str(random.randint(1000, 9999))
                order_doc_id = f"{datetime.now().strftime('%Y%m%d')}_{code_str}"
                
                total_amt = sum(i["price"] for i in order_items)
                
                # 寫入 Firestore 封存
                if db:
                    db.collection("groups").document(target_id).collection("orders").document(order_doc_id).set({
                        "order_date": datetime.now().strftime("%Y-%m-%d"),
                        "order_code": code_str,
                        "total_amount": total_amt,
                        "master_payer_name": master_payer,
                        "items": order_items,
                        "status": "pending",
                        "timestamp": datetime.utcnow()
                    })
                
                # 清空狀態機，退回常態
                GROUP_STATES[target_id] = {"mode": "normal", "order_items": []}
                
                reply_str = f"🏁 【團購訂單安全截止】\n📅 訂單日期：{date_str}\n🔢 結算編號：#{code_str}\n💰 總金額：${total_amt:,} 元\n💳 買單墊款人：{master_payer}\n\n🤖 數據已成功安全封存！後續若要開始向成員收錢，請在群組輸入「訂單結算 {date_str} #{code_str}」即可調閱進行催款核銷！"

        # 4️⃣ 意圖：發動訂單結算模式
        elif result.intent == "settle_start":
            # 解析日期與單號
            match_code = re.search(r'(\d{4})\s*#?(\d{4})', user_text)
            if not match_code:
                reply_str = "⚠️ 請輸入正確的結算格式！例如：「訂單結算 0620 #8821」"
            else:
                req_date = match_code.group(1) # 如 "0620"
                req_code = match_code.group(2) # 如 "8821"
                
                # 去資料庫檢索
                order_found = None
                if db:
                    orders = db.collection("groups").document(target_id).collection("orders").where("order_code", "==", req_code).stream()
                    for doc in orders:
                        order_found = doc.to_dict()
                        break
                
                if not order_found:
                    reply_str = f"❌ 找不到編號為 #{req_code} 的訂單，請確認日期與編號是否正確！"
                else:
                    # 鎖定群組進入「結算模式防火牆」
                    GROUP_STATES[target_id] = {
                        "mode": "settle",
                        "active_order_code": req_code,
                        "master_payer": order_found["master_payer_name"]
                    }
                    
                    items_list = order_found["items"]
                    lines = [f"・{i['buyer']} 点了 【{i['item']}】 🪙 應付：${i['price']}" for i in items_list]
                    
                    reply_str = f"🔔 【飯糰訂單催款控制台 ｜ 結算模式】\n🔢 訂單單號：#{req_code}\n💳 墊款債權人：{order_found['master_payer_name']}\n\n📋 應收明細：\n" + "\n".join(lines) + f"\n\n🛑 【防火牆已啟動】：現在群組內只能輸入「我給了 @某某 多少錢」來核銷付款，其他閒聊訊息一律不予理會！"

        # 5️⃣ 意圖：結算模式下的勾稽付款
        elif result.intent == "settle_pay" and current_mode == "settle":
            if result.settlement:
                s = result.settlement
                p_name = s.payer_name.strip()
                r_name = s.receiver_name.strip()
                
                if p_name == "發話者" or not p_name: p_name = creator_name
                if r_name == "發話者" or not r_name: r_name = current_state["master_payer"]
                
                # 寫入核銷專區副集合
                if db:
                    db.collection("groups").document(target_id).collection("settlements").document().set({
                        "payer_name": p_name, "receiver_name": r_name, "amount": s.amount,
                        "order_code_ref": current_state["active_order_code"], "timestamp": datetime.utcnow()
                    })
                
                reply_str = f"🤝 【訂單核銷對帳成功】\n✅ {p_name} 成功還給 {r_name} ${s.amount:,} 元！\n\n填單完畢若要結束結算功能，請輸入「結算結束」即可恢復正常聊天。"
            else:
                return # 閒聊或雜訊，直接無視阻斷

        # 6️⃣ 意圖：結束結算模式
        elif "結算結束" in user_text and current_mode == "settle":
            GROUP_STATES[target_id] = {"mode": "normal", "order_items": []}
            reply_str = "🔓 【結算解鎖】已安全關閉結算控制台，群組已恢復常態對話與記帳模式！"

        # 7️⃣ 常規模式下保留原本的記帳/核銷
        elif current_mode == "normal":
            if result.intent == "record" and result.records:
                db_success = save_records_to_db_v2(target_id, True, creator_id, result.records)
                if db_success:
                    lines = [f"➖ 支出 ${r.amount} ({r.item})" for r in result.records]
                    reply_str = f"👥 【群組公帳】{creator_name} 記帳成功！\n" + "\n".join(lines)
            elif result.intent == "settlement" and result.settlement:
                # 呼叫舊有的智能核銷
                s = result.settlement
                p = s.payer_name.replace("發話者", creator_name) if s.payer_name else creator_name
                r = s.receiver_name if s.receiver_name else "管理員"
                if db:
                    db.collection("groups").document(target_id).collection("settlements").document().set({
                        "payer_name": p, "receiver_name": r, "amount": s.amount, "timestamp": datetime.utcnow()
                    })
                reply_str = f"🤝 【群組常規核銷成功】\n💸 付款人：{p}\n📥 收款人：{r}\n💰 金額：${s.amount:,} 元"
            elif result.intent == "analyze":
                summary_text = get_monthly_quick_summary_v2(target_id, True)
                dashboard_url = f"https://liff.line.me/{MY_LIFF_ID}?groupId={target_id}"
                reply_str = f"{summary_text}\n\n🌐 雲端可視化大後台：\n{dashboard_url}"
            else:
                if result.ai_reply: reply_str = result.ai_reply

    except Exception as e:
        print(f"運行異常: {e}")
        if current_mode in ["order", "settle"]: return # 揪團中出錯直接安靜，絕不干擾群組

    if not reply_str or reply_str.strip() == "": return

    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(to=target_id, messages=[TextMessage(text=reply_str)]))
    except Exception as e: print(f"❌ LINE 發送失敗: {e}")

@app.get("/")
def health_check(): return {"status": "healthy", "version": "v4.0-Group-Order-SaaS"}
