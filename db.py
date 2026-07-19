import sqlite3
import json
import time
from traceback import print_exc

DB_PATH = "./data/vinted_notifications.db"
DB_TIMEOUT_SECONDS = 30

DEFAULT_MESSAGE_TEMPLATE = """🆕 Title : {title}
💶 Price : {price}
🛍️ Brand : {brand}
Condition : {condition}
<a href="{image}">&#8205;</a>"""


def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECONDS)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {DB_TIMEOUT_SECONDS * 1000}")
    return conn


def configure_database_runtime():
    """Enable persistent SQLite settings before worker processes start."""
    conn = None
    try:
        conn = get_db_connection()
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def create_or_update_sqlite_db(db_path):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Using the sql script
        with open(db_path, "r", encoding="utf-8") as sql_file:
            sql_script = sql_file.read()
            cursor.executescript(sql_script)

        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def is_item_in_db_by_id(id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT() FROM items WHERE item=?", (id,))
        if cursor.fetchone()[0]:
            return True
        return False
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_last_timestamp(query_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT last_item FROM queries WHERE id=?", (query_id,))
        result = cursor.fetchone()
        if result:
            return result[0]
        return None
    except Exception:
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def update_last_timestamp(query_id, timestamp):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE queries SET last_item=? WHERE id=?", (timestamp, query_id)
        )
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def add_item_to_db(id, title, query_id, price, timestamp, photo_url, currency="EUR"):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Insert into db the id and the query_id related to the item
        cursor.execute(
            "INSERT INTO items (item, title, price, currency, timestamp, photo_url, query_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, title, price, currency, timestamp, photo_url, query_id),
        )
        # Update the last item for the query
        cursor.execute(
            "UPDATE queries SET last_item=? WHERE id=?", (timestamp, query_id)
        )
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_queries():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, query, last_item, query_name FROM queries")
        return cursor.fetchall()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def is_query_in_db(processed_query):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # replace spaces in searched_text by % to match any query containing the searched text

        cursor.execute(
            "SELECT COUNT() FROM queries WHERE query = ?", (processed_query,)
        )
        if cursor.fetchone()[0]:
            return True
        return False
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def add_query_to_db(query, name=None):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        if name:
            cursor.execute(
                "INSERT INTO queries (query, last_item, query_name) VALUES (?, NULL, ?)",
                (query, name),
            )
        else:
            cursor.execute(
                "INSERT INTO queries (query, last_item) VALUES (?, NULL)", (query,)
            )
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_query_id_by_rowid(rowid):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        query = f"SELECT id FROM (SELECT id, ROW_NUMBER() OVER (ORDER BY ROWID) rn FROM queries) t WHERE rn={rowid}"
        cursor.execute(query)
        result = cursor.fetchone()
        if result:
            return result[0]
        return None
    except Exception:
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def remove_query_from_db(query_number):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Delete items associated with this query using query_id
        cursor.execute("DELETE FROM items WHERE query_id=?", (query_number,))
        # Delete the query
        cursor.execute("DELETE FROM queries WHERE id=?", (query_number,))
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def remove_all_queries_from_db():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Delete all items first to maintain foreign key integrity
        cursor.execute("DELETE FROM items")
        # Then delete all queries
        cursor.execute("DELETE FROM queries")
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def update_query_in_db(query_id, query, name):
    """
    Update an existing query in the database.

    Args:
        query_id (int): The ID of the query to update
        query (str): The new query URL
        name (str, optional): The new name for the query

    Returns:
        bool: True if the query was updated successfully, False otherwise
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE queries SET query=?, query_name=? WHERE id=?",
            (query, name, query_id),
        )
        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def add_to_allowlist(country):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO allowlist VALUES (?)", (country,))
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def remove_from_allowlist(country):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM allowlist WHERE country=?", (country,))
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_allowlist():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM allowlist")
        # Get list of countries
        countries = [country[0] for country in cursor.fetchall()]
        # Return 0 if there are no countries in the allowlist
        if not countries:
            return 0
        return countries
    finally:
        if conn:
            conn.close()


def clear_allowlist():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM allowlist")
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_parameter(key):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM parameters WHERE key=?", (key,))
        result = cursor.fetchone()
        return result[0] if result else None
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def set_parameter(key, value):
    """Create or update a configuration parameter."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO parameters (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def set_parameters(values):
    """Create or update several configuration values atomically."""
    conn = None
    try:
        conn = get_db_connection()
        conn.executemany(
            """
            INSERT INTO parameters (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            [(str(key), str(value)) for key, value in values.items()],
        )
        conn.commit()
        return True
    except Exception:
        if conn:
            conn.rollback()
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def get_all_parameters():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM parameters")
        return {row[0]: row[1] for row in cursor.fetchall()}
    except Exception:
        print_exc()
        return {}
    finally:
        if conn:
            conn.close()


_ITEM_SORTS = {
    "date_desc": "i.timestamp DESC",
    "date_asc": "i.timestamp ASC",
    "price_asc": "CAST(i.price AS REAL) ASC",
    "price_desc": "CAST(i.price AS REAL) DESC",
    "title_asc": "i.title COLLATE NOCASE ASC",
}


def _items_filter_sql(cursor, query, search, price_min, price_max):
    """Build a parameterised WHERE clause for item lookups.

    Returns (where_sql, params). Returns (None, None) when a query filter names
    a query that does not exist, so the caller can short-circuit to empty.
    All user values go into params, never into the SQL text.
    """
    clauses = []
    params = []
    if query:
        cursor.execute("SELECT id FROM queries WHERE query=?", (query,))
        row = cursor.fetchone()
        if not row:
            return None, None
        clauses.append("i.query_id = ?")
        params.append(row[0])
    if search:
        clauses.append("i.title LIKE ?")
        params.append(f"%{search}%")
    if price_min is not None:
        clauses.append("CAST(i.price AS REAL) >= ?")
        params.append(price_min)
    if price_max is not None:
        clauses.append("CAST(i.price AS REAL) <= ?")
        params.append(price_max)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def get_items(
    limit=50,
    query=None,
    offset=0,
    search=None,
    price_min=None,
    price_max=None,
    sort="date_desc",
):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        where, params = _items_filter_sql(cursor, query, search, price_min, price_max)
        if where is None:
            return []
        order_by = _ITEM_SORTS.get(sort, _ITEM_SORTS["date_desc"])
        cursor.execute(
            f"""
            SELECT i.item, i.title, i.price, i.currency, i.timestamp,
                   q.query, i.photo_url, q.query_name
            FROM items i JOIN queries q ON i.query_id = q.id
            {where}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        )
        return cursor.fetchall()
    except Exception:
        print_exc()
        return []
    finally:
        if conn:
            conn.close()


def count_items(query=None, search=None, price_min=None, price_max=None):
    """Count items matching the same filters as get_items (for pagination)."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        where, params = _items_filter_sql(cursor, query, search, price_min, price_max)
        if where is None:
            return 0
        cursor.execute(
            f"SELECT COUNT(*) FROM items i JOIN queries q ON i.query_id = q.id {where}",
            params,
        )
        return cursor.fetchone()[0]
    except Exception:
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()


def get_total_items_count():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM items")
        return cursor.fetchone()[0]
    except Exception:
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()


def get_total_queries_count():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM queries")
        return cursor.fetchone()[0]
    except Exception:
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()


def get_last_found_item():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT i.item, i.title, i.price, i.currency, i.timestamp, q.query, i.photo_url FROM items i JOIN queries q ON i.query_id = q.id ORDER BY i.timestamp DESC LIMIT 1"
        )
        return cursor.fetchone()
    except Exception:
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def get_items_per_day():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get total items
        cursor.execute("SELECT COUNT(*) FROM items")
        total_items = cursor.fetchone()[0]

        if total_items == 0:
            return 0

        # Get earliest and latest timestamps
        cursor.execute("SELECT MIN(timestamp), MAX(timestamp) FROM items")
        min_timestamp, max_timestamp = cursor.fetchone()

        # Calculate number of days (add 1 to include both start and end days)
        import datetime

        min_date = datetime.datetime.fromtimestamp(min_timestamp).date()
        max_date = datetime.datetime.fromtimestamp(max_timestamp).date()
        days_diff = (max_date - min_date).days + 1

        # Ensure at least 1 day to avoid division by zero
        days_diff = max(1, days_diff)

        # Calculate items per day
        return round(total_items / days_diff, 1)
    except Exception:
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()


# ============================================================
# Quiet-hours configuration
# ============================================================


def migrate_quiet_hours_schema():
    """
    Add quiet-hours settings to existing installations.

    The migration is idempotent and preserves any values already chosen
    by the user. Quiet hours are enabled by default from 01:00 to 06:00
    in Europe/London, so remote servers follow UK local time correctly.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.executemany(
            "INSERT OR IGNORE INTO parameters (key, value) VALUES (?, ?)",
            [
                ("quiet_hours_enabled", "True"),
                ("quiet_hours_start", "01:00"),
                ("quiet_hours_end", "06:00"),
                ("quiet_hours_timezone", "Europe/London"),
            ],
        )
        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


# ============================================================
# Multi-user Telegram support
# ============================================================

def migrate_multi_user_schema():
    """
    Create the multi-user Telegram tables for an existing installation.

    The configured telegram_chat_id is treated as the administrator and
    is subscribed to every existing query that currently has no subscribers.
    This migration is idempotent and may be run safely on every start.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.executescript(
            """
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

            CREATE TABLE IF NOT EXISTS query_subscriptions
            (
                query_id   INTEGER NOT NULL,
                chat_id    TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (query_id, chat_id),
                FOREIGN KEY (query_id) REFERENCES queries (id) ON DELETE CASCADE,
                FOREIGN KEY (chat_id) REFERENCES telegram_users (chat_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_query_subscriptions_chat
                ON query_subscriptions (chat_id);

            CREATE INDEX IF NOT EXISTS idx_query_subscriptions_query
                ON query_subscriptions (query_id);
            """
        )

        cursor.execute(
            "SELECT value FROM parameters WHERE key='telegram_chat_id'"
        )
        row = cursor.fetchone()
        admin_chat_id = str(row[0]).strip() if row and row[0] is not None else ""

        if admin_chat_id:
            # Exactly one configured administrator is allowed. A previous
            # administrator remains an approved user but loses admin rights.
            cursor.execute(
                """
                UPDATE telegram_users
                SET is_admin=0, updated_at=CURRENT_TIMESTAMP
                WHERE chat_id<>? AND is_admin=1
                """,
                (admin_chat_id,),
            )
            cursor.execute(
                """
                INSERT INTO telegram_users
                    (chat_id, display_name, status, is_admin)
                VALUES (?, 'Primary user', 'approved', 1)
                ON CONFLICT(chat_id) DO UPDATE SET
                    status='approved',
                    is_admin=1,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (admin_chat_id,),
            )
        else:
            cursor.execute(
                """
                UPDATE telegram_users
                SET is_admin=0, updated_at=CURRENT_TIMESTAMP
                WHERE is_admin=1
                """
            )

            # Preserve all existing searches by assigning otherwise-unowned
            # queries to the configured primary Telegram account.
            cursor.execute(
                """
                INSERT OR IGNORE INTO query_subscriptions (query_id, chat_id)
                SELECT q.id, ?
                FROM queries q
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM query_subscriptions s
                    WHERE s.query_id = q.id
                )
                """,
                (admin_chat_id,),
            )

        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def migrate_query_uniqueness():
    """
    Merge duplicate query rows without losing items or subscriptions, then
    enforce one shared row per normalised Vinted URL.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            """
            SELECT query
            FROM queries
            GROUP BY query
            HAVING COUNT(*) > 1
            """
        )

        for (query_url,) in cursor.fetchall():
            cursor.execute(
                """
                SELECT id, last_item, query_name
                FROM queries
                WHERE query=?
                ORDER BY id
                """,
                (query_url,),
            )
            rows = cursor.fetchall()
            canonical_id = rows[0][0]
            duplicate_ids = [row[0] for row in rows[1:]]
            last_items = [row[1] for row in rows if row[1] is not None]
            query_names = [
                str(row[2]).strip()
                for row in rows
                if row[2] is not None and str(row[2]).strip()
            ]

            cursor.execute(
                """
                UPDATE queries
                SET last_item=?, query_name=?
                WHERE id=?
                """,
                (
                    max(last_items) if last_items else None,
                    query_names[0] if query_names else None,
                    canonical_id,
                ),
            )

            for duplicate_id in duplicate_ids:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO query_subscriptions
                        (query_id, chat_id, created_at)
                    SELECT ?, chat_id, created_at
                    FROM query_subscriptions
                    WHERE query_id=?
                    """,
                    (canonical_id, duplicate_id),
                )
                cursor.execute(
                    "UPDATE items SET query_id=? WHERE query_id=?",
                    (canonical_id, duplicate_id),
                )
                cursor.execute(
                    "DELETE FROM query_subscriptions WHERE query_id=?",
                    (duplicate_id,),
                )
                cursor.execute(
                    "DELETE FROM queries WHERE id=?",
                    (duplicate_id,),
                )

        cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_queries_query_unique
            ON queries (query)
            """
        )
        conn.commit()
        return True
    except Exception:
        if conn:
            conn.rollback()
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def migrate_fork_identity():
    """Point installations that still use the upstream URL at this fork."""
    conn = None
    try:
        conn = get_db_connection()
        conn.execute(
            """
            UPDATE parameters
            SET value='https://github.com/FrancesOkolo/Vinted-Notifications'
            WHERE key='github_url'
              AND value='https://github.com/Fuyucch1/Vinted-Notifications'
            """
        )
        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def register_telegram_user(chat_id, display_name=None):
    """
    Register a Telegram account as pending without changing an existing
    approved or revoked status.
    """
    chat_id = str(chat_id)
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO telegram_users
                (chat_id, display_name, status, is_admin)
            VALUES (?, ?, 'pending', 0)
            ON CONFLICT(chat_id) DO UPDATE SET
                display_name=COALESCE(excluded.display_name, telegram_users.display_name),
                updated_at=CURRENT_TIMESTAMP
            """,
            (chat_id, display_name),
        )
        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def approve_telegram_user(chat_id, display_name=None):
    chat_id = str(chat_id)
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO telegram_users
                (chat_id, display_name, status, is_admin)
            VALUES (?, ?, 'approved', 0)
            ON CONFLICT(chat_id) DO UPDATE SET
                display_name=COALESCE(excluded.display_name, telegram_users.display_name),
                status='approved',
                updated_at=CURRENT_TIMESTAMP
            """,
            (chat_id, display_name),
        )
        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def revoke_telegram_user(chat_id):
    chat_id = str(chat_id)
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE telegram_users
            SET status='revoked', updated_at=CURRENT_TIMESTAMP
            WHERE chat_id=? AND is_admin=0
            """,
            (chat_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def get_telegram_user(chat_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT chat_id, display_name, status, is_admin
            FROM telegram_users
            WHERE chat_id=?
            """,
            (str(chat_id),),
        )
        return cursor.fetchone()
    except Exception:
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def get_telegram_users():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT chat_id, display_name, status, is_admin
            FROM telegram_users
            ORDER BY is_admin DESC, status, display_name, chat_id
            """
        )
        return cursor.fetchall()
    except Exception:
        print_exc()
        return []
    finally:
        if conn:
            conn.close()


def is_telegram_user_approved(chat_id):
    user = get_telegram_user(chat_id)
    return bool(user and user[2] == "approved")


def is_telegram_user_admin(chat_id):
    user = get_telegram_user(chat_id)
    return bool(user and user[2] == "approved" and int(user[3]) == 1)


def get_queries(chat_id=None, enabled_only=False):
    """
    Return all queries, or only those subscribed to by chat_id.

    When enabled_only is True, paused queries (enabled=0) are excluded — the
    scraper uses this so a paused query stops making requests without being
    deleted. The tuple layout remains compatible with the original
    application: (id, query, last_item, query_name)
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if chat_id is None:
            cursor.execute(
                """
                SELECT id, query, last_item, query_name
                FROM queries
                {where}
                ORDER BY id
                """.format(where="WHERE enabled=1" if enabled_only else "")
            )
        else:
            cursor.execute(
                """
                SELECT q.id, q.query, q.last_item, q.query_name
                FROM queries q
                JOIN query_subscriptions s ON s.query_id=q.id
                WHERE s.chat_id=?{enabled}
                ORDER BY s.created_at, q.id
                """.format(enabled=" AND q.enabled=1" if enabled_only else ""),
                (str(chat_id),),
            )

        return cursor.fetchall()
    except Exception:
        print_exc()
        return []
    finally:
        if conn:
            conn.close()


def get_query_by_url(processed_query):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, query, last_item, query_name
            FROM queries
            WHERE query=?
            """,
            (processed_query,),
        )
        return cursor.fetchone()
    except Exception:
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def is_query_in_db(processed_query, chat_id=None):
    query = get_query_by_url(processed_query)
    if not query:
        return False

    if chat_id is None:
        return True

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT 1
            FROM query_subscriptions
            WHERE query_id=? AND chat_id=?
            """,
            (query[0], str(chat_id)),
        )
        return cursor.fetchone() is not None
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def add_query_to_db(query, name=None, chat_id=None):
    """
    Add a shared query and subscribe chat_id to it.

    Returns:
        (query_id, query_created, subscription_created)
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO queries (query, last_item, query_name)
            VALUES (?, NULL, ?)
            ON CONFLICT(query) DO NOTHING
            """,
            (query, name),
        )
        query_created = cursor.rowcount > 0

        cursor.execute("SELECT id FROM queries WHERE query=?", (query,))
        row = cursor.fetchone()
        if row is None:
            conn.rollback()
            return None, False, False

        query_id = row[0]
        if not query_created and name:
            cursor.execute(
                """
                UPDATE queries
                SET query_name=COALESCE(query_name, ?)
                WHERE id=?
                """,
                (name, query_id),
            )

        subscription_created = False
        if chat_id is not None:
            chat_id = str(chat_id)
            cursor.execute(
                """
                SELECT 1
                FROM telegram_users
                WHERE chat_id=? AND status='approved'
                """,
                (chat_id,),
            )
            if cursor.fetchone() is None:
                conn.rollback()
                return query_id, query_created, False

            cursor.execute(
                """
                INSERT OR IGNORE INTO query_subscriptions (query_id, chat_id)
                VALUES (?, ?)
                """,
                (query_id, chat_id),
            )
            subscription_created = cursor.rowcount > 0

        conn.commit()
        return query_id, query_created, subscription_created
    except Exception:
        print_exc()
        return None, False, False
    finally:
        if conn:
            conn.close()


def get_query_subscribers(query_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT s.chat_id
            FROM query_subscriptions s
            JOIN telegram_users u ON u.chat_id=s.chat_id
            WHERE s.query_id=? AND u.status='approved'
            ORDER BY u.is_admin DESC, s.created_at
            """,
            (query_id,),
        )
        return [row[0] for row in cursor.fetchall()]
    except Exception:
        print_exc()
        return []
    finally:
        if conn:
            conn.close()


def copy_query_subscriptions(source_chat_id, target_chat_id):
    """Copy one approved user's subscriptions to another approved user.

    Existing target subscriptions are retained, so the operation is safe to
    repeat. Returns the number of newly copied subscriptions, or ``None`` when
    either account is not approved or the database operation fails.
    """
    source_chat_id = str(source_chat_id)
    target_chat_id = str(target_chat_id)
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM telegram_users
            WHERE chat_id IN (?, ?) AND status='approved'
            """,
            (source_chat_id, target_chat_id),
        )
        expected_accounts = 1 if source_chat_id == target_chat_id else 2
        if cursor.fetchone()[0] != expected_accounts:
            return None

        cursor.execute(
            """
            INSERT OR IGNORE INTO query_subscriptions (query_id, chat_id)
            SELECT query_id, ?
            FROM query_subscriptions
            WHERE chat_id=?
            """,
            (target_chat_id, source_chat_id),
        )
        copied = cursor.rowcount
        conn.commit()
        return copied
    except Exception:
        if conn:
            conn.rollback()
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def get_query_id_by_rowid(rowid, chat_id=None):
    """
    Resolve the displayed 1-based query number safely.
    """
    try:
        row_number = int(rowid)
    except (TypeError, ValueError):
        return None

    if row_number < 1:
        return None

    queries = get_queries(chat_id=chat_id)
    if row_number > len(queries):
        return None

    return queries[row_number - 1][0]


def remove_query_subscription(query_id, chat_id):
    """
    Unsubscribe one user. If no subscribers remain, delete the query and
    its stored items to avoid continuing to scrape an unused search.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            DELETE FROM query_subscriptions
            WHERE query_id=? AND chat_id=?
            """,
            (query_id, str(chat_id)),
        )
        removed = cursor.rowcount > 0

        if removed:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM query_subscriptions
                WHERE query_id=?
                """,
                (query_id,),
            )
            if cursor.fetchone()[0] == 0:
                cursor.execute("DELETE FROM items WHERE query_id=?", (query_id,))
                cursor.execute("DELETE FROM queries WHERE id=?", (query_id,))

        conn.commit()
        return removed
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def remove_all_query_subscriptions(chat_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        chat_id = str(chat_id)

        cursor.execute(
            """
            SELECT query_id
            FROM query_subscriptions
            WHERE chat_id=?
            """,
            (chat_id,),
        )
        query_ids = [row[0] for row in cursor.fetchall()]

        cursor.execute(
            "DELETE FROM query_subscriptions WHERE chat_id=?",
            (chat_id,),
        )

        for query_id in query_ids:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM query_subscriptions
                WHERE query_id=?
                """,
                (query_id,),
            )
            if cursor.fetchone()[0] == 0:
                cursor.execute("DELETE FROM items WHERE query_id=?", (query_id,))
                cursor.execute("DELETE FROM queries WHERE id=?", (query_id,))

        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def remove_query_from_db(query_number):
    """
    Administrative/global delete used by the web interface.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM query_subscriptions WHERE query_id=?",
            (query_number,),
        )
        cursor.execute("DELETE FROM items WHERE query_id=?", (query_number,))
        cursor.execute("DELETE FROM queries WHERE id=?", (query_number,))
        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def remove_all_queries_from_db():
    """
    Administrative/global delete used by the web interface.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM query_subscriptions")
        cursor.execute("DELETE FROM items")
        cursor.execute("DELETE FROM queries")
        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def _add_message_template_variable(
    template,
    label,
    placeholder,
    before_placeholder=None,
):
    """Add or fill one labelled placeholder without replacing custom text."""
    if placeholder in template:
        return template

    lines = template.splitlines()
    label_lower = label.lower()

    for index, line in enumerate(lines):
        if line.strip().lower().startswith(label_lower):
            lines[index] = line.rstrip() + " " + placeholder
            return "\n".join(lines)

    new_line = f"{label} : {placeholder}"
    if before_placeholder:
        for index, line in enumerate(lines):
            if before_placeholder in line:
                lines.insert(index, new_line)
                return "\n".join(lines)

    lines.append(new_line)
    return "\n".join(lines)


def migrate_message_template():
    """
    Add the condition variable to an existing message template.

    A migration marker ensures this changes the user's template only once;
    later custom edits remain under the user's control.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT value FROM parameters WHERE key=?",
            ("message_template_v2_migrated",),
        )
        marker = cursor.fetchone()
        if marker and str(marker[0]).lower() == "true":
            return True

        cursor.execute(
            "SELECT value FROM parameters WHERE key=?",
            ("message_template",),
        )
        row = cursor.fetchone()
        template = row[0] if row and row[0] else DEFAULT_MESSAGE_TEMPLATE

        template = _add_message_template_variable(
            template,
            "Condition",
            "{condition}",
            before_placeholder="{image}",
        )
        cursor.execute(
            """
            INSERT INTO parameters (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            ("message_template", template),
        )
        cursor.execute(
            """
            INSERT INTO parameters (key, value)
            VALUES (?, 'True')
            ON CONFLICT(key) DO UPDATE SET value='True'
            """,
            ("message_template_v2_migrated",),
        )
        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def _strip_description_placeholder(template):
    """Remove the unreliable description placeholder without losing other text."""
    cleaned_lines = []
    for line in template.splitlines():
        if "{description}" not in line:
            cleaned_lines.append(line)
            continue

        cleaned = line.replace("{description}", "").rstrip()
        label_only = cleaned.strip().lower().replace(" ", "")
        if label_only not in {"", "description", "description:"}:
            cleaned_lines.append(cleaned)

    return "\n".join(cleaned_lines).strip()


def migrate_remove_description_field():
    """Remove the unreliable description field from saved message templates."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT value FROM parameters WHERE key=?",
            ("message_template_description_removed_v2",),
        )
        marker = cursor.fetchone()
        if marker and str(marker[0]).lower() == "true":
            return True

        cursor.execute(
            "SELECT value FROM parameters WHERE key=?",
            ("message_template",),
        )
        row = cursor.fetchone()
        template = row[0] if row and row[0] else DEFAULT_MESSAGE_TEMPLATE
        template = _strip_description_placeholder(template)
        cursor.execute(
            """
            INSERT INTO parameters (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            ("message_template", template),
        )

        cursor.execute(
            """
            INSERT INTO parameters (key, value)
            VALUES (?, 'True')
            ON CONFLICT(key) DO UPDATE SET value='True'
            """,
            ("message_template_description_removed_v2",),
        )
        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


# --- Notification outbox ----------------------------------------------------
# Telegram alerts are persisted here before an item is marked "seen", so a
# crash or restart between finding an item and delivering it cannot lose the
# notification. Rows are deleted on successful delivery.


def migrate_pending_notifications_table():
    """Create the durable notification outbox table if it does not exist."""
    conn = None
    try:
        conn = get_db_connection()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                url TEXT,
                button_text TEXT,
                chat_ids TEXT,
                query_id INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(pending_notifications)")
        }
        if "query_id" not in columns:
            conn.execute(
                "ALTER TABLE pending_notifications ADD COLUMN query_id INTEGER"
            )
        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def enqueue_notification(content, url, button_text, chat_ids, query_id=None):
    """Persist one Telegram notification for delivery. Returns the new row id."""
    conn = None
    try:
        chat_ids_json = (
            json.dumps([str(c) for c in chat_ids]) if chat_ids else None
        )
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO pending_notifications
                (
                    content, url, button_text, chat_ids, query_id,
                    attempts, next_attempt_at
                )
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (
                content,
                url,
                button_text,
                chat_ids_json,
                query_id,
                time.time(),
            ),
        )
        conn.commit()
        return cursor.lastrowid
    except Exception:
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def get_due_notifications(limit=10):
    """Return notifications whose next attempt time has arrived.

    Each row is
    (id, content, url, button_text, chat_ids_json, query_id, attempts).
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                id, content, url, button_text, chat_ids, query_id, attempts
            FROM pending_notifications
            WHERE next_attempt_at <= ?
            ORDER BY id
            LIMIT ?
            """,
            (time.time(), limit),
        )
        return cursor.fetchall()
    except Exception:
        print_exc()
        return []
    finally:
        if conn:
            conn.close()


def delete_notification(notification_id):
    """Remove a notification once delivered (or permanently undeliverable)."""
    conn = None
    try:
        conn = get_db_connection()
        conn.execute(
            "DELETE FROM pending_notifications WHERE id=?", (notification_id,)
        )
        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def reschedule_notification(notification_id, attempts, next_attempt_at):
    """Record a failed attempt and defer the next try."""
    conn = None
    try:
        conn = get_db_connection()
        conn.execute(
            """
            UPDATE pending_notifications
            SET attempts=?, next_attempt_at=?
            WHERE id=?
            """,
            (attempts, next_attempt_at, notification_id),
        )
        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def count_pending_notifications():
    """Return how many notifications are still awaiting delivery."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM pending_notifications")
        return cursor.fetchone()[0]
    except Exception:
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()


# --- Query pause/enable + counts -------------------------------------------


def migrate_query_enabled_column():
    """Add the queries.enabled column to older databases if it's missing."""
    conn = None
    try:
        conn = get_db_connection()
        columns = [row[1] for row in conn.execute("PRAGMA table_info(queries)")]
        if "enabled" not in columns:
            conn.execute(
                "ALTER TABLE queries ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
            conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def set_query_enabled(query_id, enabled):
    """Pause (enabled=0) or resume (enabled=1) a single query."""
    conn = None
    try:
        conn = get_db_connection()
        conn.execute(
            "UPDATE queries SET enabled=? WHERE id=?",
            (1 if enabled else 0, query_id),
        )
        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def get_query_enabled_map():
    """Return {query_id: bool} of whether each query is enabled."""
    conn = None
    try:
        conn = get_db_connection()
        rows = conn.execute("SELECT id, enabled FROM queries").fetchall()
        return {row[0]: bool(row[1]) for row in rows}
    except Exception:
        print_exc()
        return {}
    finally:
        if conn:
            conn.close()


def get_query_item_counts():
    """Return {query_id: number of items found} across all queries."""
    conn = None
    try:
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT query_id, COUNT(*) FROM items GROUP BY query_id"
        ).fetchall()
        return {row[0]: row[1] for row in rows}
    except Exception:
        print_exc()
        return {}
    finally:
        if conn:
            conn.close()
