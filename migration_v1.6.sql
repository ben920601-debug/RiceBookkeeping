-- ==========================================
-- 記帳米粒 V1.6 Migration
-- 涵蓋：收據辨識改為「命名團單」流程、單一模式互斥機制不需新資料表
-- 執行方式：mysql -u帳號 -p 資料庫名稱 < migration_v1.6.sql
-- ==========================================

-- 1. orders 表補上團單名稱欄位（收據辨識登記時使用者輸入的名稱）
ALTER TABLE orders ADD COLUMN order_name VARCHAR(100) DEFAULT NULL;

-- 2. 收據辨識：辨識完成、等待使用者輸入團單名稱的暫存狀態
CREATE TABLE IF NOT EXISTS pending_receipt_naming (
    group_id      VARCHAR(64) PRIMARY KEY,
    payer_id      VARCHAR(64) NOT NULL,
    payer_name    VARCHAR(100) NOT NULL,
    items_json    TEXT NOT NULL,
    total_amount  INT NOT NULL,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES `groups`(group_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
