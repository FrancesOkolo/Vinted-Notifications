BEGIN TRANSACTION;

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

INSERT OR IGNORE INTO parameters (key, value)
VALUES ('catalogue_request_spacing_seconds', '12');

UPDATE parameters
SET value = '1.2.1'
WHERE key = 'version';

COMMIT;
