"""
記帳米粒 LINE Bot ｜ 主程式進入點

只保留：FastAPI 應用程式本體、webhook 入口、機器人最核心的身份
（快速記帳、開團／核銷、關鍵字回覆、敏感詞過濾、Gemini 對話分流），
以及把各功能模組（旅行模式／群組團單／收據辨識／測試模式守門員）
組裝起來的邏輯。各功能模組的實作細節都在 app/ 底下對應的檔案。
"""
import re
import json
import random
import asyncio
from datetime import datetime
from typing import Literal, List, Optional

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent
from google.genai import types

from app.config import MY_LIFF_ID
from app.db import db_cursor, is_db_ready
from app.logging_utils import log_error, log_stat_event
from app.cache import is_bot_enabled, get_keyword_replies, get_sensitive_words, get_maintenance_message, get_ai_persona
from app.line_client import line_handler, ai_client, send_line_reply, resolve_id_to_name, get_real_mentions

from app.features.test_mode import try_handle_test_mode_gate, is_test_mode_active
from app.features.itinerary import try_handle_trip_flow, try_handle_itinerary_confirm_reply, itinerary_reminder_loop
from app.features.group_split import try_handle_group_split_reply, try_start_group_split_question
from app.features.receipt import try_handle_receipt_naming_reply, try_handle_edit_order_item, handle_receipt_image

from app.api.routes import router as api_router

app = FastAPI(title="記帳米粒 ｜ 模組化版")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


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
    if not is_db_ready(): return
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
    # 優先序：密碼驗證 > 旅行模式多輪對話 > 行程「有/無」回覆 > 收據團單命名回覆 > 群組分攤「均分/@tag/跳過」回覆
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

    if is_group and try_handle_receipt_naming_reply(target_id, _gate_text, reply_token):
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
    if not is_db_ready() or not is_bot_enabled():
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
