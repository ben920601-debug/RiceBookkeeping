"""
記帳分類：先用本地關鍵字比對（免費、即時），比對不到才交給 AI 判斷。
這樣大多數常見品項（早餐、捷運、超商⋯）不用額外呼叫 AI，維持快速記帳的原本速度，
只有比較少見、關鍵字庫沒收錄的品項才會多花一次 AI 呼叫。
"""
from app.logging_utils import log_error

EXPENSE_CATEGORIES = [
    "餐飲", "交通", "購物", "娛樂消遣", "居家生活",
    "醫療保健", "日用品", "水電通訊", "教育學習", "保險", "人情社交", "其他",
]

CATEGORY_KEYWORDS = {
    "餐飲": ["早餐", "午餐", "晚餐", "消夜", "宵夜", "咖啡", "飲料", "手搖", "便當", "餐廳", "小吃",
             "火鍋", "牛排", "燒烤", "吃到飽", "甜點", "蛋糕", "麵包", "星巴克", "麥當勞", "肯德基",
             "必勝客", "披薩", "早午餐", "下午茶", "拉麵", "壽司", "牛肉麵", "滷味", "鹹酥雞"],
    "交通": ["捷運", "公車", "計程車", "小黃", "uber", "Uber", "加油", "停車", "高鐵", "台鐵",
             "機票", "火車", "客運", "過路費", "ETC", "YouBike", "Ubike", "單車", "腳踏車", "停車費", "油錢"],
    "購物": ["衣服", "褲子", "鞋子", "包包", "蝦皮", "momo", "淘寶", "網購", "百貨", "購物",
             "化妝品", "保養品", "配件", "3C", "電腦", "手機殼"],
    "娛樂消遣": ["電影", "KTV", "遊戲", "展覽", "演唱會", "門票", "訂閱", "Netflix", "旅遊", "景點",
                "遊樂園", "桌遊", "撞球", "保齡球"],
    "居家生活": ["家具", "家電", "裝潢", "清潔", "五金", "IKEA", "床包", "窗簾", "傢俱"],
    "醫療保健": ["藥局", "診所", "醫院", "看診", "健檢", "牙醫", "掛號", "藥品", "維他命", "保健食品"],
    "日用品": ["衛生紙", "洗髮精", "沐浴乳", "日用品", "全聯", "家樂福", "大潤發", "好市多", "costco",
               "超市", "便利商店", "7-11", "711", "全家", "萊爾富", "OK超商"],
    "水電通訊": ["電費", "水費", "瓦斯費", "電話費", "網路費", "手機費", "電信費"],
    "教育學習": ["書籍", "課程", "補習", "學費", "文具", "書店", "誠品"],
    "保險": ["保費", "保險"],
    "人情社交": ["禮金", "紅包", "禮物", "聚餐", "請客", "白包"],
}


def classify_category_by_keyword(item_name: str):
    """本地關鍵字比對，命中回傳分類名稱，沒命中回傳 None"""
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(k in item_name for k in keywords):
            return cat
    return None


def ai_classify_category(item_name: str, ai_client) -> str:
    """關鍵字庫沒收錄時，交給 AI 判斷最接近的分類；判斷失敗則歸為「其他」"""
    prompt = (
        f"請判斷消費項目「{item_name}」最符合以下哪一個分類，只回傳分類名稱本身，不要有其他文字或標點：\n"
        + "、".join(EXPENSE_CATEGORIES)
    )
    try:
        result = ai_client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        text = (result.text or "").strip()
        if text in EXPENSE_CATEGORIES:
            return text
    except Exception as e:
        log_error("AI分類判斷", e)
    return "其他"


def resolve_category(item_name: str, ai_client=None) -> str:
    """記帳分類統一入口：關鍵字比對優先，沒命中且有提供 ai_client 才呼叫 AI，
    否則直接歸為「其他」（例如快速記帳層想省下 AI 呼叫時可以不傳 ai_client）"""
    cat = classify_category_by_keyword(item_name)
    if cat:
        return cat
    if ai_client is not None:
        return ai_classify_category(item_name, ai_client)
    return "其他"
