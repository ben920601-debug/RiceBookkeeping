-- ==========================================
-- 記帳米粒 V1.8 Migration
-- 涵蓋：繳費提醒、存錢筒、支付方式（三者皆為個人版限定功能）
-- 執行方式：mysql -u帳號 -p 資料庫名稱 < migration_v1.8.sql
-- ==========================================

-- 1. 繳費項目主檔（週期性帳單定義；owner_id 固定是個人 user_id，不支援群組）
CREATE TABLE IF NOT EXISTS bills (
    id                       BIGINT AUTO_INCREMENT PRIMARY KEY,
    owner_id                 VARCHAR(64) NOT NULL,
    bill_name                VARCHAR(100) NOT NULL,
    amount                   INT NOT NULL,
    due_date                 DATE NOT NULL,                 -- 下一次應繳日期
    installments_remaining   INT DEFAULT NULL,              -- NULL=每月持續無限期；數字=倒數分期期數
    status                   ENUM('active','completed') NOT NULL DEFAULT 'active',
    is_paid                  TINYINT(1) NOT NULL DEFAULT 0, -- 當期是否已繳
    reminded                 TINYINT(1) NOT NULL DEFAULT 0, -- 當期「繳費日前2天」是否已提醒過
    created_at               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_owner_due (owner_id, due_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. 繳費核銷紀錄（刪除此表某一筆，對應 bills 要改回未繳費狀態）
CREATE TABLE IF NOT EXISTS bill_payments (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    bill_id       BIGINT NOT NULL,
    owner_id      VARCHAR(64) NOT NULL,
    amount        INT NOT NULL,
    paid_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expense_id    BIGINT DEFAULT NULL,   -- 對應寫入 expenses 表的那一筆，供刪除核銷時一併處理
    FOREIGN KEY (bill_id) REFERENCES bills(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. 存錢筒（每人最多 6 個，於程式層限制數量）
CREATE TABLE IF NOT EXISTS savings_jars (
    id                      BIGINT AUTO_INCREMENT PRIMARY KEY,
    owner_id                VARCHAR(64) NOT NULL,
    jar_name                VARCHAR(50) NOT NULL,
    balance                 INT NOT NULL DEFAULT 0,
    target_amount           INT DEFAULT NULL,
    goal_reached_notified   TINYINT(1) NOT NULL DEFAULT 0,
    created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_owner_jarname (owner_id, jar_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4. 存錢紀錄
CREATE TABLE IF NOT EXISTS savings_transactions (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    jar_id      BIGINT NOT NULL,
    owner_id    VARCHAR(64) NOT NULL,
    amount      INT NOT NULL,
    expense_id  BIGINT DEFAULT NULL,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (jar_id) REFERENCES savings_jars(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 5. 支付方式清單（使用者於監控後台自訂常用支付方式）
CREATE TABLE IF NOT EXISTS payment_methods (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    owner_id    VARCHAR(64) NOT NULL,
    method_name VARCHAR(50) NOT NULL,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_owner_method (owner_id, method_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 6. expenses 表補上支付方式欄位，預設「現金」
ALTER TABLE expenses ADD COLUMN payment_method VARCHAR(50) NOT NULL DEFAULT '現金';
