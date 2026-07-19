-- ==========================================
-- 記帳米粒 V1.5 Migration
-- 涵蓋：旅行模式重新設計（多輪對話式規劃）、AI人格資料庫化
-- 執行方式：mysql -u buddy -p RiceBookkeeping < migration_v1.5_test_features.sql
-- ==========================================

-- 1. 旅行主檔：一趟旅行（出發～回程），確認完成後才會有正式的 trip_code
CREATE TABLE IF NOT EXISTS trips (
    id                BIGINT AUTO_INCREMENT PRIMARY KEY,
    owner_type        ENUM('user','group') NOT NULL,
    owner_id          VARCHAR(64) NOT NULL,
    trip_code         VARCHAR(20) DEFAULT NULL,     -- 確認完成後才會寫入（依出發日期年月日，同日多筆會加 -2、-3 等後綴）
    departure_at      DATETIME NOT NULL,
    return_at         DATETIME NOT NULL,
    status            ENUM('collecting','confirmed') NOT NULL DEFAULT 'collecting',
    ai_route_summary  TEXT DEFAULT NULL,
    created_by_uid    VARCHAR(64) NOT NULL,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_owner_trip_code (owner_type, owner_id, trip_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. 旅行對話流程的暫存狀態（每個 owner 同時只會有一筆進行中的旅行規劃對話）
CREATE TABLE IF NOT EXISTS trip_sessions (
    owner_type   ENUM('user','group') NOT NULL,
    owner_id     VARCHAR(64) NOT NULL,
    stage        VARCHAR(30) NOT NULL,   -- pending_departure / pending_return / collecting / pending_location_confirm / pending_review
    trip_id      BIGINT DEFAULT NULL,
    draft_json   TEXT DEFAULT NULL,      -- 暫存尚未確認的行程項目（日期時間、地點候選、經緯度）
    updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (owner_type, owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. itineraries 表補上 trip_id 關聯（原本的行程提醒機制不變，只是多了旅行分組）
ALTER TABLE itineraries
    ADD COLUMN IF NOT EXISTS trip_id BIGINT DEFAULT NULL,
    ADD CONSTRAINT fk_itineraries_trip FOREIGN KEY (trip_id) REFERENCES trips(id) ON DELETE CASCADE;

-- 4. AI人格與敏感詞已在 V1.3 改為資料庫驅動（bot_settings / sensitive_words / keyword_replies），
--    這裡補上「AI人格」的預設值，供中控後台編輯（key = 'ai_persona'）
INSERT IGNORE INTO bot_settings (`key`, `value`) VALUES
    ('ai_persona', '你是一個親切、幽默的記帳助理「記帳米粒」。');
