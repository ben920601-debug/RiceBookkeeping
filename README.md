# 記帳米粒 (Rice Millet) 🌾 — 智慧團隊記帳與團購 LINE Bot

「記帳米粒」是一款結合 **Python 高速正則匹配** 與 **Gemini 2.5 大語言模型** 的商務型 LINE 記帳助理。專為群組日常記帳、團購點單、防呆核銷而設計，並完美串接雲端後台（LIFF + FastAPI + Firebase），解決群組多人財務混亂、記帳繁瑣、團購催帳耗時的痛點。

---

## 🚀 核心功能亮點

*   **雙層意圖防護網**：首創「Regex 快取」與「Gemini 2.5 Flash 結構化分流（`response_schema`）」雙核心。常規記帳直通落庫（0.1 秒極速反應）；複雜對話與多品項代點單則交由 AI 靈活萃取，完美兼顧低延遲與高智慧。
*   **完美顯示群組暱稱**：深度整合 LINE Group Member API。即使群組內成員「未加機器人好友」，米粒也能精準抓取並記錄其真實名稱，拒絕冷冰冰的 `Uxxx` 亂碼，徹底解決業界技術痛點。
*   **三大智慧模式切換**：
    1.  **一般記帳**：免切換模式，隨手一記（例：`@記帳米粒 午餐 120`）自動入帳。
    2.  **團購代點**：主揪一鍵開團（生成專屬 4 位數單號），支援成員自主叫單或幫人代點（例：`@記帳米粒 @小明 珍奶 50`）。
    3.  **防呆核銷**：鎖定單號開啟核銷模式，支援對帳金額溢繳/重複核銷的智慧防呆攔截，確保帳目一分不差。
*   **雲端監控大後台**：內建 LIFF（LINE Front-end Framework）一鍵登入網址，不論個人或群組夥伴，皆能隨時進入精美網頁版後台查閱帳目明細與統計圖表。

---

## 🛠️ 技術架構

*   **後端框架 (Backend)**：FastAPI (高效能、全異步 Asynchronous 支援)
*   **資料庫 (Database)**：Google Firebase Firestore (無伺服器 NoSQL，即時異步同步)
*   **人工智慧 (AI Engine)**：Google GenAI SDK (Gemini 2.5 Flash / JSON Structured Output)
*   **通訊協議**：LINE Messaging API SDK v3 + LINE Webhook 安全驗證
*   **並行優化**：運用 FastAPI `BackgroundTasks` 機制，秒級回覆 LINE 平台，完全避免 Webhook 3秒逾時機制。

---

## 📂 專案目錄結構

```text
.
├── main.py                 # FastAPI 主程式、LINE Webhook 接收與雙層分流核心
├── README.md               # 專案介紹說明書 (本檔案)
├── .env.example            # 環境變數範例設定檔
└── firebase-adminsdk.json  # Firebase 私鑰授權憑證 (需自行生成並放置)
```

---

## ⚙️ 快速開始 (Quick Start)

### 1. 環境變數配置
在專案根目錄下建立 `.env` 檔案，並填入以下必要憑證：
```env
LINE_CHANNEL_SECRET=your_line_channel_secret
LINE_CHANNEL_ACCESS_TOKEN=your_line_channel_access_token
GEMINI_API_KEY=your_gemini_api_key
```

### 2. 安裝依賴套件
```bash
pip install fastapi uvicorn line-bot-sdk-python-v3 google-genai firebase-admin httpx pydantic python-dotenv
```

### 3. 本地啟動服務
```bash
uvicorn main.py:app --host 0.0.0.0 --port 8003 --reload
```

---

## 📋 系統運作指令速查

### 📝 一般模式（常態）
*   **記帳**：`@記帳米粒 項目 金額` (例：`@記帳米粒 雞肉飯 65`)
*   **查看後台**：`@記帳米粒 查帳` / `報表`
*   **使用說明**：`@記帳米粒 使用說明`

### 🛒 團購模式（開團中）
*   **啟動開團**：`@記帳米粒 開團`
*   **自己點單**：`@記帳米粒 品項 金額` (例：`@記帳米粒 冰美式 45`)
*   **幫人點單**：`@記帳米粒 @成員 品項 金額` (例：`@記帳米粒 @小明 拿鐵 65`)
*   **截止結單**：`@記帳米粒 結單` (自動結算總金額、綁定墊款人並寫入歷史訂單)

### 💳 核銷模式（對帳中）
*   **開啟核銷**：`@記帳米粒 申請核銷 #4位數單號` (解鎖防呆核銷模式)
*   **成員還款登記**：`@記帳米粒 @收款人 歸還金額` (例：`@記帳米粒 @大明 100`)
*   **自己核銷自己**：`@記帳米粒 我核銷 100`
*   **結束核銷**：`@記帳米粒 結算結束`

---

## 📄 版本更新日誌

### V1.1 (2026-06-30) — 當前版本
*   **特定詞極速回覆**：前置 Python 關鍵字硬編碼字典（客服、合作、使用說明），0延遲直接回覆，省去 AI 呼叫成本。
*   **簡化使用說明書**：重新設計結構化導覽，畫面更俐落、降低閱讀負擔。

### V1.0 (2026-03-01)
*   記帳、團購、核銷三大核心功能落庫與 LINE SDK v3 架構初版完成。

---

## 👨‍💻 核心維護者
*   **電鍋科技工作室 (Rice Cooker Tech Studio)**

---
*本專案程式碼架構與邏輯受專屬保護，客製化延伸或商務對接請聯繫官方客服。*
