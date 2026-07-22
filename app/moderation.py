"""
敏感詞語意判斷。

中控後台為每個敏感詞填寫「細項說明」（語意/情境描述），這裡不是單純比對
使用者輸入是否「包含」該詞的文字本身，而是把所有敏感詞連同說明一起交給
AI，讓 AI 理解語意去判斷使用者這句話是否符合任何一個敏感詞代表的情境
（例如換句話說、隱晦講法、同音字等文字變形也要能判斷出來）。

⚠️ 取捨提醒：這代表「幾乎每一則訊息」都會多一次 AI 呼叫（只要中控後台有
設定任何敏感詞），跟原本快速記帳層盡量避免呼叫 AI 的設計方向是相反的，
會增加一點延遲與 Gemini 用量成本。這是配合「要語意判斷、不能只比文字」
這個需求必然要付出的代價，不是程式碼的疏漏。
"""
from pydantic import BaseModel, Field
from google.genai import types

from app.cache import get_sensitive_words_detail
from app.logging_utils import log_error


class SensitiveCheckResult(BaseModel):
    triggered: bool = Field(default=False)
    matched_word: str = Field(default="")


def check_sensitive_content(ai_client, text: str):
    """回傳 (是否命中, 命中的敏感詞)。中控後台沒有設定任何敏感詞時，
    直接放行、不呼叫 AI（避免完全沒設定時還多花一次不必要的請求）。"""
    words = get_sensitive_words_detail()
    if not words:
        return False, None

    lines = []
    for w in words:
        desc = (w.get("description") or "").strip()
        lines.append(f"・「{w['word']}」：{desc}" if desc else f"・「{w['word']}」")

    prompt = (
        "請判斷以下使用者訊息，是否符合下面任何一個敏感詞所描述的情境或語意"
        "（要理解語意本身，不是單純比對文字有沒有出現；換句話說、隱晦講法、"
        "同音字、注音文等變形也要能判斷出來）：\n"
        + "\n".join(lines)
        + f"\n\n使用者訊息：『{text}』\n\n"
        "若命中，triggered 設為 true，matched_word 填最符合的那一個敏感詞（用清單裡的原文字）；"
        "若都不符合，triggered 設為 false，matched_word 留空字串。"
    )
    try:
        result = ai_client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SensitiveCheckResult, temperature=0.1),
        ).parsed
        if result and result.triggered:
            return True, (result.matched_word or "").strip()
    except Exception as e:
        log_error("敏感詞語意判斷", e)
    return False, None
