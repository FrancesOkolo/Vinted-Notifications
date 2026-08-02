BEGIN TRANSACTION;

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

INSERT OR IGNORE INTO query_preferences (query_id)
SELECT id FROM queries;

CREATE TRIGGER IF NOT EXISTS queries_create_default_preferences
AFTER INSERT ON queries
BEGIN
    INSERT OR IGNORE INTO query_preferences (query_id) VALUES (NEW.id);
END;

INSERT OR IGNORE INTO parameters (key, value)
VALUES ('fast_query_refresh_delay', '90');

UPDATE parameters
SET value = '1.2.0'
WHERE key = 'version';

COMMIT;
