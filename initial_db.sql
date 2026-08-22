-- init_schema.sql
-- Initial Scheme

PRAGMA foreign_keys = ON;

/* ============================
   Tables
   ============================ */

-- Queries table
CREATE TABLE IF NOT EXISTS queries
(
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    query     TEXT NOT NULL UNIQUE,
    last_item NUMERIC,
    query_name TEXT,
    enabled   INTEGER NOT NULL DEFAULT 1
);

-- Per-query monitoring and deal-evaluation preferences. Keeping these in a
-- separate one-to-one table preserves the legacy queries tuple layout used by
-- the scraper, Web UI and Telegram commands.
CREATE TABLE IF NOT EXISTS query_preferences
(
    query_id                    INTEGER PRIMARY KEY,
    poll_mode                   TEXT NOT NULL DEFAULT 'normal'
                                CHECK (poll_mode IN ('normal', 'fast')),
    monitor_during_quiet_hours  INTEGER NOT NULL DEFAULT 0
                                CHECK (monitor_during_quiet_hours IN (0, 1)),
    deal_evaluator_enabled      INTEGER NOT NULL DEFAULT 0
                                CHECK (deal_evaluator_enabled IN (0, 1)),
    deal_excellent_max          TEXT,
    deal_good_max               TEXT,
    deal_currency               TEXT NOT NULL DEFAULT 'GBP',
    FOREIGN KEY (query_id) REFERENCES queries (id) ON DELETE CASCADE
);

CREATE TRIGGER IF NOT EXISTS queries_create_default_preferences
AFTER INSERT ON queries
BEGIN
    INSERT OR IGNORE INTO query_preferences (query_id) VALUES (NEW.id);
END;

-- Items table
CREATE TABLE IF NOT EXISTS items
(
    item      NUMERIC,
    title     TEXT,
    price     NUMERIC,
    currency  TEXT,
    timestamp NUMERIC,
    photo_url TEXT,
    query_id  INTEGER,
    FOREIGN KEY (query_id) REFERENCES queries (id)
);

-- Allowlist table
CREATE TABLE IF NOT EXISTS allowlist
(
    country TEXT
);



-- Approved and pending Telegram accounts
CREATE TABLE IF NOT EXISTS telegram_users
(
    chat_id      TEXT PRIMARY KEY,
    display_name TEXT,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'approved', 'revoked')),
    is_admin     INTEGER NOT NULL DEFAULT 0
                 CHECK (is_admin IN (0, 1)),
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- A query is scraped once, but can notify several Telegram accounts.
CREATE TABLE IF NOT EXISTS query_subscriptions
(
    query_id   INTEGER NOT NULL,
    chat_id    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (query_id, chat_id),
    FOREIGN KEY (query_id) REFERENCES queries (id) ON DELETE CASCADE,
    FOREIGN KEY (chat_id) REFERENCES telegram_users (chat_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_query_subscriptions_chat
    ON query_subscriptions (chat_id);

CREATE INDEX IF NOT EXISTS idx_query_subscriptions_query
    ON query_subscriptions (query_id);

-- AI verdicts are evaluated asynchronously by one serialized worker. The
-- durable queue keeps a slow or interrupted OpenAI call from delaying or
-- losing the primary item alert.
CREATE TABLE IF NOT EXISTS pending_ai_evaluations
(
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id                NUMERIC NOT NULL,
    query_id               INTEGER NOT NULL,
    title                  TEXT,
    brand                  TEXT,
    condition              TEXT,
    price                  TEXT,
    currency               TEXT,
    photo_url              TEXT,
    item_url               TEXT,
    chat_ids               TEXT NOT NULL,
    parent_notification_id INTEGER,
    delivered_chat_ids     TEXT NOT NULL DEFAULT '[]',
    handled_chat_ids       TEXT NOT NULL DEFAULT '[]',
    result_content         TEXT,
    evaluation_started_at  REAL,
    attempts               INTEGER NOT NULL DEFAULT 0,
    next_attempt_at        REAL NOT NULL DEFAULT 0,
    locked_until           REAL NOT NULL DEFAULT 0,
    last_error             TEXT,
    created_at             REAL NOT NULL,
    UNIQUE (item_id, query_id),
    FOREIGN KEY (query_id) REFERENCES queries (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pending_ai_evaluations_due
    ON pending_ai_evaluations (next_attempt_at, locked_until, id);


-- Parameters table
CREATE TABLE IF NOT EXISTS parameters
(
    key   TEXT PRIMARY KEY,
    value TEXT
);

/* ============================
   Initial data
   ============================ */

INSERT INTO parameters (key, value)
VALUES ('telegram_enabled', 'False'),
       ('telegram_token', ''),
       ('telegram_chat_id', ''),
       ('telegram_process_running', 'False'),

       ('rss_enabled', 'False'),
       ('rss_port', '8080'),
       ('rss_max_items', '100'),
       ('rss_process_running', 'False'),

       ('version', '1.0.3'),
       ('github_url', 'https://github.com/FrancesOkolo/Vinted-Notifications'),

       ('items_per_query', '20'),
       ('query_refresh_delay', '180'),
       ('fast_query_refresh_delay', '90'),
       ('catalogue_request_spacing_seconds', '12'),
       ('quiet_hours_enabled', 'True'),
       ('quiet_hours_start', '01:00'),
       ('quiet_hours_end', '06:00'),
       ('quiet_hours_timezone', 'Europe/London'),
       ('quiet_hours_days', '0,1,2,3,4,5,6'),

       ('proxy_list', ''),
       ('proxy_list_link', ''),
       ('check_proxies', 'False'),
       ('last_proxy_check_time', '0'),
       ('banwords', ''),
       ('message_template', '🆕 Title : {title}
💶 Price : {price}
🛍️ Brand : {brand}
Condition : {condition}
<a href="{image}">&#8205;</a>'),
       ('message_template_v2_migrated', 'True');
