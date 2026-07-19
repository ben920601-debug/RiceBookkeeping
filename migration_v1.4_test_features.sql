-- ==========================================
-- 記帳米粒 V1.4 測試功能 Migration
-- 涵蓋：驗證密碼機制、行程模式、群組團單分攤、收據辨識
-- 執行方式：mysql -u帳號 -p 資料庫名稱 < migration_v1.4_test_features.sql
-- ==========================================

-- 1. 測試功能：待驗證密碼（每個 owner 同時只能有一筆等待中的驗證）
CREATE TABLE IF NOT EXISTS test_mode_pending (
    owner_type    ENUM('user','group') NOT NULL,
    owner_id      VARCHAR(64) NOT NULL,
    feature       VARCHAR(30) NOT NULL,
    requested_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (owner_type, owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. 測試功能：已啟用的測試模式（16 小時效期，用 expires_at 判斷是否還有效，
--    到期後不用額外清除，查詢時比對時間即可自動視為失效）
CREATE TABLE IF NOT EXISTS test_mode_sessions (
    owner_type    ENUM('user','group') NOT NULL,
    owner_id      VARCHAR(64) NOT NULL,
    feature       VARCHAR(30) NOT NULL,
    expires_at    DATETIME NOT NULL,
    PRIMARY KEY (owner_type, owner_id, feature)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. 行程模式：登記的行程
CREATE TABLE IF NOT EXISTS itineraries (
    id                   BIGINT AUTO_INCREMENT PRIMARY KEY,
    owner_type           ENUM('user','group') NOT NULL,
    owner_id             VARCHAR(64) NOT NULL,
    scheduled_at         DATETIME NOT NULL,
    location_name        VARCHAR(255) NOT NULL,
    latitude             DECIMAL(10,7) DEFAULT NULL,
    longitude            DECIMAL(10,7) DEFAULT NULL,
    related_order_code   VARCHAR(4) DEFAULT NULL,
    notified             TINYINT(1) NOT NULL DEFAULT 0,
    created_by_uid       VARCHAR(64) NOT NULL,
    created_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_owner_time (owner_type, owner_id, scheduled_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4. 行程模式：15 分鐘提醒推播後，等待使用者回覆「有/無」花費的暫存狀態
CREATE TABLE IF NOT EXISTS pending_itinerary_confirm (
    owner_type    ENUM('user','group') NOT NULL,
    owner_id      VARCHAR(64) NOT NULL,
    itinerary_id  BIGINT NOT NULL,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (owner_type, owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 5. 群組團單分攤：暫存一筆待確認「均分／@tag／跳過」的花費
--    （文字輸入或收據辨識來源皆共用此表；items_json 供收據多品項使用）
CREATE TABLE IF NOT EXISTS pending_group_expense (
    group_id       VARCHAR(64) PRIMARY KEY,
    payer_id       VARCHAR(64) NOT NULL,
    payer_name     VARCHAR(100) NOT NULL,
    items_json     TEXT NOT NULL,
    total_amount   INT NOT NULL,
    source         ENUM('text','receipt') NOT NULL DEFAULT 'text',
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES `groups`(group_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
