-- ==========================================
-- 記帳米粒 MySQL Schema
-- ==========================================

-- 1. groups：群組狀態
CREATE TABLE IF NOT EXISTS `groups` (
    group_id            VARCHAR(64) PRIMARY KEY,
    state                ENUM('normal','order','settle') NOT NULL DEFAULT 'normal',
    active_order_code    VARCHAR(4) DEFAULT NULL,
    created_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. group_members：群組成員暱稱快取
CREATE TABLE IF NOT EXISTS group_members (
    group_id      VARCHAR(64) NOT NULL,
    user_id       VARCHAR(64) NOT NULL,
    display_name  VARCHAR(100) NOT NULL,
    updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (group_id, user_id),
    FOREIGN KEY (group_id) REFERENCES `groups`(group_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. expenses：一般記帳(個人版 + 群組版共用)
CREATE TABLE IF NOT EXISTS expenses (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    owner_type       ENUM('user','group') NOT NULL,
    owner_id         VARCHAR(64) NOT NULL,
    record_type      ENUM('expense','income') NOT NULL DEFAULT 'expense',
    amount           INT NOT NULL,
    item             VARCHAR(255) NOT NULL,
    category         VARCHAR(100) NOT NULL DEFAULT '生活雜費',
    created_by_uid   VARCHAR(64) NOT NULL,
    created_by_name  VARCHAR(100) NOT NULL,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_owner_time (owner_type, owner_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4. orders：已結單的團購單
CREATE TABLE IF NOT EXISTS orders (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    group_id            VARCHAR(64) NOT NULL,
    order_code          VARCHAR(4) NOT NULL,
    order_date          DATE NOT NULL,
    total_amount        INT NOT NULL DEFAULT 0,
    master_payer_id     VARCHAR(64) NOT NULL,
    master_payer_name   VARCHAR(100) NOT NULL,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES `groups`(group_id) ON DELETE CASCADE,
    INDEX idx_group_code (group_id, order_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 5. order_items：團購品項(order_id 為 NULL = 尚未結單的暫存品項)
CREATE TABLE IF NOT EXISTS order_items (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    group_id      VARCHAR(64) NOT NULL,
    order_code    VARCHAR(4) NOT NULL,
    order_id      BIGINT DEFAULT NULL,
    buyer_id      VARCHAR(64) NOT NULL,
    buyer_name    VARCHAR(100) NOT NULL,
    item_name     VARCHAR(255) NOT NULL,
    price         INT NOT NULL,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES `groups`(group_id) ON DELETE CASCADE,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL,
    INDEX idx_temp_lookup (group_id, order_code, order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 6. settlements：核銷紀錄
CREATE TABLE IF NOT EXISTS settlements (
    id                BIGINT AUTO_INCREMENT PRIMARY KEY,
    group_id          VARCHAR(64) NOT NULL,
    order_code_ref    VARCHAR(4) NOT NULL,
    payer_id          VARCHAR(64) NOT NULL,
    payer_name        VARCHAR(100) NOT NULL,
    receiver_id       VARCHAR(64) NOT NULL,
    receiver_name     VARCHAR(100) NOT NULL,
    amount            INT NOT NULL,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES `groups`(group_id) ON DELETE CASCADE,
    INDEX idx_settle_lookup (group_id, order_code_ref, payer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;