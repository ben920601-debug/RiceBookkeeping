# 模組化重構說明

## 這次做了什麼

把原本 2500 行的單一 `main.py` 拆成一個套件（`app/`）+ 精簡後的 `main.py`（566 行）：

```
ricebookkeeping/
├── main.py                    # 進入點：webhook、核心記帳/開團/核銷邏輯、模組組裝
├── .env                       # 不變
└── app/
    ├── config.py               # 環境變數
    ├── db.py                   # 連線池、db_cursor、is_db_ready()
    ├── logging_utils.py        # log_error、log_stat_event
    ├── cache.py                 # bot_settings/關鍵字回覆/敏感詞/AI人格 快取
    ├── geo.py                   # 地理編碼、距離估算
    ├── line_client.py           # LINE SDK / Gemini 客戶端、共用互動函式
    ├── features/
    │   ├── test_mode.py          # 密碼驗證、單一模式互斥
    │   ├── itinerary.py          # 旅行模式（多輪對話 + 提醒排程）
    │   ├── group_split.py        # 群組團單分攤
    │   └── receipt.py            # 收據辨識
    └── api/
        └── routes.py             # 監控後台用的所有 REST API
```

**沒有動到任何資料表結構，不需要跑新的 migration。** 純粹是程式碼搬家，功能邏輯逐行照搬，沒有改寫。

## 部署方式：跟原本幾乎一樣

1. 把 `ricebookkeeping_modular.zip` 解壓縮到您伺服器上**原本放 main.py 的那個資料夾**（會產生 `main.py` 跟 `app/` 目錄）
2. `.env` 檔案完全不用動，繼續放在同一層
3. **啟動指令完全不變**：`uvicorn main:app --host 0.0.0.0 --port 8001`（systemd 設定檔不用改一個字，因為它終究還是執行同一層的 `main.py`，`main:app` 這個變數名稱也沒變）
4. 重啟服務、照慣例跑 `grep -c "..." main.py` 確認新版生效

## 拆分原則（跟之前討論的方向一致）

- **留在 `main.py`**：webhook 入口、資料庫連線與快取的「呼叫」（不是實作）、最原始的記帳/開團/核銷/關鍵字回覆/敏感詞/Gemini 對話分流——這些是骨幹，也是最穩定、最少變動的部分
- **拆到 `app/features/`**：旅行模式、群組分攤、收據辨識、測試模式守門員——這些是還在持續擴充、各自有獨立狀態機的功能模組
- **拆到 `app/api/`**：監控後台用的 REST API，跟 LINE 對話邏輯是完全不同的關注點

## 之後要加新功能時

- 如果是**全新的獨立功能**（例如您之後想加「記帳訂閱付費」）→ 在 `app/features/` 底下新增一個檔案，只要在 `main.py` 裡 import 需要用到的函式即可，幾乎不用碰其他模組
- 如果是**修既有功能**（例如旅行模式要再調整）→ 只需要打開 `app/features/itinerary.py`，完全不會影響到收據辨識或群組分攤的程式碼
- 如果是**後台要加新的查詢/編輯 API** → 只需要打開 `app/api/routes.py`

## 我做了什麼安全檢查

因為這是純粹的程式碼搬移重構，最大風險是「東搬西搬漏東西」，所以我做了兩層檢查：

1. **語法檢查**：每個檔案都個別跑過 `py_compile`，全部通過
2. **靜態符號分析**：寫了一支小工具用 Python 的 `ast` 模組，逐一檢查每個檔案裡用到的每個名稱，是不是都有對應的 import 或本地定義——過程中抓到兩個真正的遺漏（`receipt.py` 重複定義了已搬到 `line_client.py` 的 `download_line_image`、`main.py` 少 import 了 `log_stat_event`），都已修正
3. **呼叫端與定義端比對**：手動核對了 `main.py` 呼叫各模組函式的 10 個地方，參數數量與順序都與模組裡的實際定義完全一致

⚠️ **這個環境暫時連不上 PyPI，沒辦法實際安裝套件、真的把整個服務跑起來測試。** 上面的檢查已經盡量降低風險，但建議部署後還是**優先測過一輪所有功能**（快速記帳、開團核銷、旅行模式、群組分攤、收據辨識、中控後台），有任何報錯把訊息貼給我，我再幫您排查。
