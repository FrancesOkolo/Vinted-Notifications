"""Durable catalogue-query observability and discovery progress.

This module deliberately contains no HTTP client code.  A successful catalogue
response can therefore be recorded without issuing another Vinted request, and
a failed response can be recorded without advancing discovery progress.

Only listing metadata needed by the existing local filtering/notification
pipeline is persisted.  Execution and observation tables use a keyed BLAKE2
digest instead of the raw listing id.  The raw listing id is retained only in a
short, sanitised durable pending snapshot because downstream item persistence
and notification links require it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

import db

BOOTSTRAP_NEWNESS_SECONDS = 20 * 60
TIMESTAMP_RESOLUTION_SLOP_SECONDS = 2
DEFAULT_RETENTION_DAYS = 90
DEFAULT_PENDING_LEASE_SECONDS = 120
_ITEM_KEY_DOMAIN = b"vn-catalogue-item-v1"
_QUERY_KEY_DOMAIN = b"vn-catalogue-query-v1"
_SAFE_OUTCOME = re.compile(r"[^a-z0-9_.:-]+")
_TERMINAL_DISPOSITIONS = {
    "accepted",
    "locally_rejected",
    "already_known",
    "discarded",
}


@dataclass(frozen=True)
class SuccessResult:
    """Result of recording one successful catalogue response."""

    candidate_ids: frozenset
    metrics: Mapping[str, Any]
    already_recorded: bool = False

    def __iter__(self):
        return iter(self.candidate_ids)

    def __contains__(self, value):
        return value in self.candidate_ids

    def __len__(self):
        return len(self.candidate_ids)


def _digest(value: Any, *, domain: bytes) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("value must not be empty")
    return hashlib.blake2b(
        text.encode("utf-8", errors="replace"),
        digest_size=20,
        key=domain,
    ).hexdigest()


def item_key(item_id: Any) -> str:
    """Return the stable, domain-separated BLAKE2 key for a listing id."""

    return _digest(item_id, domain=_ITEM_KEY_DOMAIN)


def query_fingerprint(query_url: Any) -> str:
    """Return a non-reversible fingerprint instead of storing a search URL."""

    return _digest(query_url, domain=_QUERY_KEY_DOMAIN)


def _value(source: Any, *names: str, default=None):
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
        return default
    for name in names:
        if hasattr(source, name):
            return getattr(source, name)
    return default


def _clean_text(value: Any, *, maximum: int, default: Optional[str] = ""):
    if value is None:
        return default
    text = str(value).replace("\x00", "").strip()
    return text[:maximum]


def _clean_url(value: Any, *, maximum: int = 2048):
    text = _clean_text(value, maximum=maximum, default=None)
    if not text:
        return None
    try:
        parts = urlsplit(text)
    except (TypeError, ValueError):
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None
    # Tracking fragments and query parameters are unnecessary for item links.
    # CDN photo query strings can be required, so retain them for photos below.
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, "", ""))[
        :maximum
    ]


def _clean_photo_url(value: Any, *, maximum: int = 2048):
    text = _clean_text(value, maximum=maximum, default=None)
    if not text:
        return None
    try:
        parts = urlsplit(text)
    except (TypeError, ValueError):
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None
    return text[:maximum]


def _clean_timestamp(value: Any):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result < 0:
        return None
    return result


def _embedded_country(item: Any):
    raw = _value(item, "raw_data", default={})
    if not isinstance(raw, Mapping):
        return None
    user = raw.get("user")
    user = user if isinstance(user, Mapping) else {}
    country = user.get("country")
    country = country if isinstance(country, Mapping) else {}
    for candidate in (
        user.get("country_iso_code"),
        user.get("country_code"),
        country.get("iso_code"),
        country.get("code"),
        raw.get("country_iso_code"),
    ):
        code = str(candidate or "").strip().upper()
        if len(code) == 2 and code.isalpha():
            return code
    return None


def item_snapshot(item: Any, country_code: Optional[str] = None) -> dict:
    """Build a privacy-minimised pending snapshot from an item or mapping.

    Seller ids, usernames, descriptions, arbitrary API payloads and query URLs
    are intentionally excluded.  A two-letter catalogue-provided country code
    may be retained because the existing allowlist needs it and it avoids an
    additional per-seller request.
    """

    listing_id = _value(item, "item_id", "id")
    if listing_id is None or not str(listing_id).strip():
        raise ValueError("item snapshot requires a listing id")

    code = str(country_code or _embedded_country(item) or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        code = None

    return {
        "item_id": listing_id,
        "title": _clean_text(_value(item, "title"), maximum=500),
        "brand": _clean_text(_value(item, "brand", "brand_title"), maximum=200),
        "condition": _clean_text(_value(item, "condition"), maximum=200),
        "price": _clean_text(_value(item, "price"), maximum=64),
        "currency": _clean_text(_value(item, "currency"), maximum=8).upper(),
        "photo_url": _clean_photo_url(_value(item, "photo_url", "photo")),
        "item_url": _clean_url(_value(item, "item_url", "url")),
        "listed_at": _clean_timestamp(
            _value(item, "listed_at", "raw_timestamp", "timestamp")
        ),
        "country_code": code,
    }


@contextmanager
def _cursor_scope(cursor=None, *, immediate=False):
    """Yield a cursor and own the transaction only when one was not supplied."""

    if cursor is not None:
        yield cursor
        return

    connection = db.get_db_connection()
    try:
        if immediate:
            connection.execute("BEGIN IMMEDIATE")
        owned_cursor = connection.cursor()
        yield owned_cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def migrate_schema(cursor=None):
    """Create/upgrade observability state and seed it from legacy data.

    The migration is idempotent.  Legacy item observations are seeded before
    duplicate ``items`` rows are removed, preserving each query's history.
    A partial unique index then makes the existing global item-id dedupe safe
    under concurrent query processing.
    """

    with _cursor_scope(cursor, immediate=cursor is None) as cur:
        # Execute each DDL statement inside _cursor_scope's transaction.
        # sqlite3.executescript() can implicitly commit an existing transaction.
        schema_sql = """
            CREATE TABLE IF NOT EXISTS catalogue_query_executions
            (
                id                        INTEGER PRIMARY KEY AUTOINCREMENT,
                query_id                  INTEGER NOT NULL,
                query_fingerprint         TEXT NOT NULL,
                started_at                REAL NOT NULL,
                finished_at               REAL,
                processing_finished_at    REAL,
                outcome                   TEXT NOT NULL DEFAULT 'started',
                http_status               INTEGER,
                duration_ms               INTEGER,
                requested_limit           INTEGER NOT NULL DEFAULT 0,
                returned_count            INTEGER NOT NULL DEFAULT 0,
                unique_returned_count     INTEGER NOT NULL DEFAULT 0,
                fresh_count               INTEGER NOT NULL DEFAULT 0,
                already_known_count       INTEGER NOT NULL DEFAULT 0,
                accepted_count            INTEGER NOT NULL DEFAULT 0,
                locally_rejected_count    INTEGER NOT NULL DEFAULT 0,
                notifications_generated   INTEGER NOT NULL DEFAULT 0,
                cross_query_overlap_count INTEGER NOT NULL DEFAULT 0,
                pending_count             INTEGER NOT NULL DEFAULT 0,
                bootstrapped              INTEGER NOT NULL DEFAULT 0,
                anchor_found              INTEGER NOT NULL DEFAULT 0,
                window_saturated          INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_catalogue_executions_query_time
                ON catalogue_query_executions (query_id, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_catalogue_executions_outcome_time
                ON catalogue_query_executions (outcome, started_at DESC);

            CREATE TABLE IF NOT EXISTS catalogue_query_execution_items
            (
                execution_id       INTEGER NOT NULL,
                item_key           TEXT NOT NULL,
                result_position     INTEGER NOT NULL,
                considered_new      INTEGER NOT NULL DEFAULT 0,
                already_known       INTEGER NOT NULL DEFAULT 0,
                cross_query_overlap INTEGER NOT NULL DEFAULT 0,
                accepted            INTEGER NOT NULL DEFAULT 0,
                locally_rejected    INTEGER NOT NULL DEFAULT 0,
                notification_count  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (execution_id, item_key),
                FOREIGN KEY (execution_id)
                    REFERENCES catalogue_query_executions (id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_catalogue_execution_items_key
                ON catalogue_query_execution_items (item_key, execution_id);

            CREATE TABLE IF NOT EXISTS query_progress
            (
                query_id                INTEGER PRIMARY KEY,
                query_fingerprint       TEXT,
                anchor_item_key         TEXT,
                anchor_seen_at          REAL,
                successful_observations INTEGER NOT NULL DEFAULT 0,
                last_success_started_at REAL,
                last_execution_id       INTEGER,
                updated_at              REAL NOT NULL,
                FOREIGN KEY (query_id) REFERENCES queries (id) ON DELETE CASCADE,
                FOREIGN KEY (last_execution_id)
                    REFERENCES catalogue_query_executions (id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS query_item_observations
            (
                query_id          INTEGER NOT NULL,
                item_key         TEXT NOT NULL,
                first_seen_at     REAL NOT NULL,
                last_seen_at      REAL NOT NULL,
                listing_timestamp REAL,
                first_execution_id INTEGER,
                last_execution_id  INTEGER,
                seen_count         INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (query_id, item_key),
                FOREIGN KEY (query_id) REFERENCES queries (id) ON DELETE CASCADE,
                FOREIGN KEY (first_execution_id)
                    REFERENCES catalogue_query_executions (id) ON DELETE SET NULL,
                FOREIGN KEY (last_execution_id)
                    REFERENCES catalogue_query_executions (id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_query_observations_item
                ON query_item_observations (item_key, query_id);

            CREATE TABLE IF NOT EXISTS pending_query_items
            (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id  INTEGER NOT NULL,
                query_id      INTEGER NOT NULL,
                item_key      TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN (
                                  'pending', 'accepted', 'locally_rejected',
                                  'already_known', 'discarded'
                              )),
                attempts      INTEGER NOT NULL DEFAULT 0,
                available_at  REAL NOT NULL DEFAULT 0,
                locked_until  REAL NOT NULL DEFAULT 0,
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL,
                UNIQUE (query_id, item_key),
                FOREIGN KEY (execution_id)
                    REFERENCES catalogue_query_executions (id) ON DELETE CASCADE,
                FOREIGN KEY (query_id) REFERENCES queries (id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_pending_query_items_due
                ON pending_query_items
                   (status, available_at, locked_until, execution_id, id);
            """
        for statement in schema_sql.split(";"):
            if statement.strip():
                cur.execute(statement)
        execution_columns = {
            row[1]
            for row in cur.execute(
                "PRAGMA table_info(catalogue_query_executions)"
            ).fetchall()
        }
        for name, definition in (
            ("unique_returned_count", "INTEGER NOT NULL DEFAULT 0"),
            ("anchor_found", "INTEGER NOT NULL DEFAULT 0"),
            ("window_saturated", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in execution_columns:
                cur.execute(
                    f"ALTER TABLE catalogue_query_executions "
                    f"ADD COLUMN {name} {definition}"
                )
        progress_columns = {
            row[1] for row in cur.execute("PRAGMA table_info(query_progress)")
        }
        if "last_success_started_at" not in progress_columns:
            cur.execute(
                "ALTER TABLE query_progress ADD COLUMN last_success_started_at REAL"
            )

        now = time.time()
        # Startup is the only time this migration runs in production, so any
        # request still marked started belongs to a process that did not finish.
        # It must not remain an immortal/in-progress row after a restart.
        cur.execute(
            """
            UPDATE catalogue_query_executions
            SET outcome='abandoned_restart',
                finished_at=COALESCE(finished_at, ?),
                processing_finished_at=COALESCE(processing_finished_at, ?)
            WHERE outcome='started'
            """,
            (now, now),
        )
        query_state = {
            row[0]: (row[1], row[2])
            for row in cur.execute(
                "SELECT id, query, last_item FROM queries"
            ).fetchall()
        }

        # Seed all per-query legacy observations before enforcing global item
        # uniqueness; an item may historically have appeared under >1 query.
        legacy_rows = cur.execute("""
            SELECT item, query_id, timestamp
            FROM items
            WHERE item IS NOT NULL AND query_id IS NOT NULL
            ORDER BY rowid
            """).fetchall()
        newest_by_query = {}
        for legacy_id, query_id, listed_at in legacy_rows:
            if query_id not in query_state:
                continue
            key = item_key(legacy_id)
            timestamp = _clean_timestamp(listed_at)
            cur.execute(
                """
                INSERT INTO query_item_observations
                    (
                        query_id, item_key, first_seen_at, last_seen_at,
                        listing_timestamp, seen_count
                    )
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(query_id, item_key) DO UPDATE SET
                    last_seen_at=MAX(last_seen_at, excluded.last_seen_at),
                    listing_timestamp=COALESCE(
                        excluded.listing_timestamp, listing_timestamp
                    )
                """,
                (query_id, key, now, now, timestamp),
            )
            sort_value = timestamp if timestamp is not None else float("-inf")
            current = newest_by_query.get(query_id)
            if current is None or sort_value > current[0]:
                newest_by_query[query_id] = (sort_value, key)

        for query_id, (query_url, last_item) in query_state.items():
            newest = newest_by_query.get(query_id)
            legacy_boundary = _clean_timestamp(last_item)
            initialized = newest is not None or legacy_boundary is not None
            cur.execute(
                """
                INSERT OR IGNORE INTO query_progress
                    (
                        query_id, query_fingerprint, anchor_item_key,
                        anchor_seen_at, successful_observations,
                        last_success_started_at, updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    query_id,
                    query_fingerprint(query_url),
                    newest[1] if newest else None,
                    now if newest else None,
                    int(initialized),
                    legacy_boundary,
                    now,
                ),
            )

        # Keep one canonical copy of every globally known item.  Observation
        # rows above preserve the removed rows' per-query history.
        cur.execute("""
            DELETE FROM items
            WHERE item IS NOT NULL
              AND rowid NOT IN (
                  SELECT MIN(rowid)
                  FROM items
                  WHERE item IS NOT NULL
                  GROUP BY item
              )
            """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_items_item_unique
            ON items (item)
            WHERE item IS NOT NULL
            """)
    return True


def start_execution(
    query_id,
    query_url,
    requested_limit,
    started_at=None,
    *,
    cursor=None,
):
    """Start one catalogue request and return its durable execution id."""

    started = time.time() if started_at is None else float(started_at)
    limit = max(0, int(requested_limit or 0))
    with _cursor_scope(cursor, immediate=cursor is None) as cur:
        result = cur.execute(
            """
            INSERT INTO catalogue_query_executions
                (
                    query_id, query_fingerprint, started_at,
                    requested_limit, outcome
                )
            VALUES (?, ?, ?, ?, 'started')
            """,
            (query_id, query_fingerprint(query_url), started, limit),
        )
        return result.lastrowid


def _safe_outcome(outcome):
    text = str(outcome or "request_error").strip().lower()[:80]
    text = _SAFE_OUTCOME.sub("_", text).strip("_")
    return text or "request_error"


def record_failure(
    execution_id,
    outcome,
    http_status=None,
    duration_ms=None,
    *,
    finished_at=None,
    cursor=None,
):
    """Finish a failed execution without touching query progress."""

    finished = time.time() if finished_at is None else float(finished_at)
    status = None if http_status is None else int(http_status)
    duration = None if duration_ms is None else max(0, int(duration_ms))
    with _cursor_scope(cursor, immediate=cursor is None) as cur:
        result = cur.execute(
            """
            UPDATE catalogue_query_executions
            SET outcome=?, http_status=?, duration_ms=?, finished_at=?,
                processing_finished_at=?
            WHERE id=? AND outcome='started'
            """,
            (
                _safe_outcome(outcome),
                status,
                duration,
                finished,
                finished,
                execution_id,
            ),
        )
        return result.rowcount == 1


def _query_values(cur, statement, values):
    values = list(values)
    if not values:
        return []
    placeholders = ",".join("?" for _ in values)
    return cur.execute(statement.format(placeholders=placeholders), values).fetchall()


def _execution_metrics(cur, execution_id):
    row = cur.execute(
        """
        SELECT returned_count, unique_returned_count, fresh_count,
               already_known_count,
               accepted_count, locally_rejected_count,
               notifications_generated, cross_query_overlap_count,
               pending_count, bootstrapped, anchor_found, window_saturated
        FROM catalogue_query_executions
        WHERE id=?
        """,
        (execution_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown execution id {execution_id}")
    names = (
        "returned_count",
        "unique_returned_count",
        "fresh_count",
        "already_known_count",
        "accepted_count",
        "locally_rejected_count",
        "notifications_generated",
        "cross_query_overlap_count",
        "pending_count",
        "bootstrapped",
        "anchor_found",
        "window_saturated",
    )
    return dict(zip(names, row))


def record_success(
    execution_id,
    query_id,
    query_url,
    snapshots: Iterable[Any],
    duration_ms,
    *,
    finished_at=None,
    cursor=None,
):
    """Record one newest-first successful result and durably queue candidates.

    On the first observation of a query only, the historical 20-minute rule is
    retained as a flood-safe bootstrap.  Thereafter, first-seen ids before the
    previous result anchor are candidates regardless of listing age.  If the
    previous anchor has fallen out of the finite result window, every unseen id
    in the returned window is considered.  An empty success stores a null
    anchor and still advances successful progress.
    """

    finished = time.time() if finished_at is None else float(finished_at)
    raw_snapshots = [item_snapshot(item) for item in (snapshots or ())]
    normalised = []
    seen_keys = set()
    for snapshot in raw_snapshots:
        key = item_key(snapshot["item_id"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        normalised.append((key, snapshot))

    with _cursor_scope(cursor, immediate=cursor is None) as cur:
        execution = cur.execute(
            """
            SELECT outcome, query_id, query_fingerprint, started_at,
                   requested_limit
            FROM catalogue_query_executions
            WHERE id=?
            """,
            (execution_id,),
        ).fetchone()
        if execution is None:
            raise ValueError(f"unknown execution id {execution_id}")
        if int(execution[1]) != int(query_id):
            raise ValueError("execution/query mismatch")
        fingerprint = query_fingerprint(query_url)
        if execution[2] != fingerprint:
            raise ValueError("execution/query URL mismatch")
        if execution[0] != "started":
            metrics = _execution_metrics(cur, execution_id)
            return SuccessResult(frozenset(), metrics, already_recorded=True)

        current_query = cur.execute(
            "SELECT query, enabled FROM queries WHERE id=?",
            (query_id,),
        ).fetchone()
        discard_outcome = None
        if current_query is None:
            discard_outcome = "discarded_removed"
        elif not bool(current_query[1]):
            discard_outcome = "discarded_paused"
        elif query_fingerprint(current_query[0]) != execution[2]:
            discard_outcome = "discarded_query_changed"
        if discard_outcome is not None:
            record_failure(
                execution_id,
                discard_outcome,
                http_status=200,
                duration_ms=duration_ms,
                finished_at=finished,
                cursor=cur,
            )
            return SuccessResult(
                frozenset(),
                _execution_metrics(cur, execution_id),
            )

        request_started = float(execution[3])
        requested_limit = max(0, int(execution[4] or 0))

        progress = cur.execute(
            """
            SELECT anchor_item_key, successful_observations,
                   last_success_started_at
            FROM query_progress
            WHERE query_id=?
            """,
            (query_id,),
        ).fetchone()
        if progress is None:
            cur.execute(
                """
                INSERT INTO query_progress
                    (query_id, query_fingerprint, successful_observations,
                     last_success_started_at, updated_at)
                VALUES (?, ?, 0, NULL, ?)
                """,
                (query_id, fingerprint, finished),
            )
            anchor_key, success_count, previous_success_started = None, 0, None
        else:
            anchor_key, success_count, previous_success_started = progress
        bootstrap = int(success_count) == 0

        keys = [entry[0] for entry in normalised]
        observed_keys = {
            row[0]
            for row in _query_values(
                cur,
                """
                SELECT item_key
                FROM query_item_observations
                WHERE query_id=? AND item_key IN ({placeholders})
                """.replace("query_id=?", f"query_id={int(query_id)}"),
                keys,
            )
        }
        overlap_keys = {
            row[0]
            for row in _query_values(
                cur,
                """
                SELECT DISTINCT item_key
                FROM query_item_observations
                WHERE query_id<>? AND item_key IN ({placeholders})
                """.replace("query_id<>?", f"query_id<>{int(query_id)}"),
                keys,
            )
        }

        raw_ids = [snapshot["item_id"] for _, snapshot in normalised]
        known_raw = {
            str(row[0])
            for row in _query_values(
                cur,
                "SELECT item FROM items WHERE item IN ({placeholders})",
                raw_ids,
            )
        }
        claimed_keys = {
            row[0]
            for row in _query_values(
                cur,
                """
                SELECT item_key
                FROM pending_query_items
                WHERE query_id=? AND item_key IN ({placeholders})
                """.replace("query_id=?", f"query_id={int(query_id)}"),
                keys,
            )
        }

        anchor_position = None
        if not bootstrap and anchor_key is not None:
            try:
                anchor_position = keys.index(anchor_key)
            except ValueError:
                anchor_position = None

        fresh_keys = set()
        candidate_ids = set()
        pending_count = 0
        already_known_count = 0
        for position, (key, snapshot) in enumerate(normalised):
            listed_at = snapshot.get("listed_at")
            since_previous_success = bool(
                listed_at is not None
                and previous_success_started is not None
                and listed_at
                >= previous_success_started - TIMESTAMP_RESOLUTION_SLOP_SECONDS
            )
            if bootstrap:
                considered_new = bool(
                    key not in observed_keys
                    and listed_at is not None
                    and request_started - BOOTSTRAP_NEWNESS_SECONDS
                    <= listed_at
                    <= request_started + 300
                )
            elif anchor_position is not None:
                # Newest-first rows with second-resolution timestamps can
                # reorder around the old anchor. The successful-request
                # boundary prevents an unseen same-second item behind that
                # anchor from being lost permanently.
                considered_new = key not in observed_keys and (
                    position < anchor_position or since_previous_success
                )
            else:
                # The previous anchor may have fallen beyond the finite result
                # window.  The last successful request start is the safe lower
                # bound: an old tail item entering the page is not a new deal.
                considered_new = bool(
                    key not in observed_keys and since_previous_success
                )

            globally_known = str(snapshot["item_id"]) in known_raw
            already_known = key in observed_keys or globally_known
            if already_known:
                already_known_count += 1
            if considered_new:
                fresh_keys.add(key)

            cur.execute(
                """
                INSERT INTO catalogue_query_execution_items
                    (
                        execution_id, item_key, result_position,
                        considered_new, already_known, cross_query_overlap
                    )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    key,
                    position,
                    int(considered_new),
                    int(already_known),
                    int(key in overlap_keys),
                ),
            )
            cur.execute(
                """
                INSERT INTO query_item_observations
                    (
                        query_id, item_key, first_seen_at, last_seen_at,
                        listing_timestamp, first_execution_id,
                        last_execution_id, seen_count
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(query_id, item_key) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    listing_timestamp=COALESCE(
                        query_item_observations.listing_timestamp,
                        excluded.listing_timestamp
                    ),
                    last_execution_id=excluded.last_execution_id,
                    seen_count=query_item_observations.seen_count + 1
                """,
                (
                    query_id,
                    key,
                    finished,
                    finished,
                    snapshot.get("listed_at"),
                    execution_id,
                    execution_id,
                ),
            )

            if considered_new and not globally_known:
                inserted = cur.execute(
                    """
                    INSERT OR IGNORE INTO pending_query_items
                        (
                            execution_id, query_id, item_key, snapshot_json,
                            status, attempts, available_at, locked_until,
                            created_at, updated_at
                        )
                    VALUES (?, ?, ?, ?, 'pending', 0, ?, 0, ?, ?)
                    """,
                    (
                        execution_id,
                        query_id,
                        key,
                        json.dumps(
                            snapshot,
                            ensure_ascii=True,
                            separators=(",", ":"),
                        ),
                        finished,
                        finished,
                        finished,
                    ),
                ).rowcount
                if inserted:
                    claimed_keys.add(key)
                    pending_count += 1
                    candidate_ids.add(snapshot["item_id"])
                else:
                    # Another execution of this same query owns the claim.
                    already_known_count += 1
                    cur.execute(
                        """
                        UPDATE catalogue_query_execution_items
                        SET already_known=1
                        WHERE execution_id=? AND item_key=?
                        """,
                        (execution_id, key),
                    )

        new_anchor = keys[0] if keys else None
        cur.execute(
            """
            UPDATE query_progress
            SET query_fingerprint=?, anchor_item_key=?, anchor_seen_at=?,
                successful_observations=successful_observations + 1,
                last_success_started_at=?, last_execution_id=?, updated_at=?
            WHERE query_id=?
            """,
            (
                fingerprint,
                new_anchor,
                finished if new_anchor else None,
                request_started,
                execution_id,
                finished,
                query_id,
            ),
        )
        cur.execute(
            """
            UPDATE catalogue_query_executions
            SET outcome='success', http_status=200, duration_ms=?,
                finished_at=?, processing_finished_at=?, returned_count=?,
                unique_returned_count=?, fresh_count=?, already_known_count=?,
                cross_query_overlap_count=?, pending_count=?, bootstrapped=?
                , anchor_found=?, window_saturated=?
            WHERE id=? AND outcome='started'
            """,
            (
                max(0, int(duration_ms or 0)),
                finished,
                finished if pending_count == 0 else None,
                len(raw_snapshots),
                len(normalised),
                len(fresh_keys),
                already_known_count,
                len(overlap_keys),
                pending_count,
                int(bootstrap),
                int(anchor_position is not None),
                int(requested_limit > 0 and len(raw_snapshots) >= requested_limit),
                execution_id,
            ),
        )
        metrics = _execution_metrics(cur, execution_id)
        return SuccessResult(frozenset(candidate_ids), metrics)


def _pending_rows(cur, *, limit, now, lease_seconds):
    first = cur.execute(
        """
        SELECT p.execution_id, p.query_id
        FROM pending_query_items p
        JOIN queries q ON q.id=p.query_id
        WHERE p.status='pending' AND p.available_at<=?
          AND p.locked_until<=? AND q.enabled=1
        ORDER BY p.execution_id, p.id
        LIMIT 1
        """,
        (now, now),
    ).fetchone()
    if first is None:
        return None
    execution_id, query_id = first
    rows = cur.execute(
        """
        SELECT id, item_key, snapshot_json
        FROM pending_query_items
        WHERE execution_id=? AND query_id=? AND status='pending'
          AND available_at<=? AND locked_until<=?
        ORDER BY id
        LIMIT ?
        """,
        (execution_id, query_id, now, now, max(1, int(limit))),
    ).fetchall()
    ids = [row[0] for row in rows]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        cur.execute(
            f"""
            UPDATE pending_query_items
            SET locked_until=?, attempts=attempts + 1, updated_at=?
            WHERE id IN ({placeholders})
            """,
            [now + max(1, int(lease_seconds)), now, *ids],
        )
    snapshots = []
    corrupt_ids = []
    for pending_id, key, payload in rows:
        try:
            snapshot = json.loads(payload)
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("item_id") is None
                or item_key(snapshot["item_id"]) != key
            ):
                raise ValueError("pending snapshot identity mismatch")
        except (TypeError, ValueError, json.JSONDecodeError):
            corrupt_ids.append(pending_id)
            continue
        snapshot["_pending_id"] = pending_id
        snapshot["_item_key"] = key
        snapshots.append(snapshot)
    if corrupt_ids:
        placeholders = ",".join("?" for _ in corrupt_ids)
        cur.execute(
            f"""
            UPDATE pending_query_items
            SET status='discarded', snapshot_json='{{}}', locked_until=0,
                updated_at=?
            WHERE id IN ({placeholders})
            """,
            [now, *corrupt_ids],
        )
        remaining = cur.execute(
            """
            SELECT COUNT(*)
            FROM pending_query_items
            WHERE execution_id=? AND status='pending'
            """,
            (execution_id,),
        ).fetchone()[0]
        if remaining == 0:
            cur.execute(
                """
                UPDATE catalogue_query_executions
                SET processing_finished_at=COALESCE(processing_finished_at, ?)
                WHERE id=?
                """,
                (now, execution_id),
            )
    return execution_id, query_id, snapshots


def pending_batch(
    limit=100,
    *,
    lease_seconds=DEFAULT_PENDING_LEASE_SECONDS,
    now=None,
    cursor=None,
):
    """Lease and return one ``(execution_id, query_id, snapshots)`` batch."""

    current = time.time() if now is None else float(now)
    with _cursor_scope(cursor, immediate=cursor is None) as cur:
        return _pending_rows(
            cur,
            limit=limit,
            now=current,
            lease_seconds=lease_seconds,
        )


get_pending_batch = pending_batch


def classify_pending(
    execution_id,
    query_id,
    item_id,
    disposition,
    notification_generated=0,
    *,
    retry_delay=0,
    now=None,
    cursor=None,
):
    """Finalize or release one pending listing and update execution counters.

    ``disposition='retry'`` releases the lease without changing counters.
    Terminal classifications are idempotent and wipe the completed snapshot
    while retaining its hashed global claim for deduplication.
    """

    current = time.time() if now is None else float(now)
    disposition = str(disposition or "").strip().lower()
    key = item_key(item_id)
    notifications = max(0, int(notification_generated or 0))
    with _cursor_scope(cursor, immediate=cursor is None) as cur:
        row = cur.execute(
            """
            SELECT id, status
            FROM pending_query_items
            WHERE execution_id=? AND query_id=? AND item_key=?
            """,
            (execution_id, query_id, key),
        ).fetchone()
        if row is None:
            # URL-reset and deletion races must fail closed. A missing durable
            # claim must never authorize an old in-memory batch to commit.
            return False

        pending_id, previous_status = row
        if disposition == "retry":
            if previous_status != "pending":
                return True
            cur.execute(
                """
                UPDATE pending_query_items
                SET available_at=?, locked_until=0, updated_at=?
                WHERE id=?
                """,
                (current + max(0, float(retry_delay or 0)), current, pending_id),
            )
            return True
        if disposition not in _TERMINAL_DISPOSITIONS:
            raise ValueError(f"unsupported pending disposition: {disposition}")
        if previous_status != "pending":
            return previous_status == disposition

        accepted = int(disposition == "accepted")
        rejected = int(disposition == "locally_rejected")
        newly_known = int(disposition == "already_known")
        cur.execute(
            """
            UPDATE catalogue_query_execution_items
            SET accepted=MAX(accepted, ?),
                locally_rejected=MAX(locally_rejected, ?),
                already_known=MAX(already_known, ?),
                notification_count=MAX(notification_count, ?)
            WHERE execution_id=? AND item_key=?
            """,
            (accepted, rejected, newly_known, notifications, execution_id, key),
        )
        cur.execute(
            """
            UPDATE catalogue_query_executions
            SET accepted_count=accepted_count + ?,
                locally_rejected_count=locally_rejected_count + ?,
                already_known_count=already_known_count + ?,
                notifications_generated=notifications_generated + ?
            WHERE id=?
            """,
            (accepted, rejected, newly_known, notifications, execution_id),
        )
        cur.execute(
            """
            UPDATE pending_query_items
            SET status=?, snapshot_json='{}', locked_until=0, updated_at=?
            WHERE id=?
            """,
            (disposition, current, pending_id),
        )
        remaining = cur.execute(
            """
            SELECT COUNT(*)
            FROM pending_query_items
            WHERE execution_id=? AND status='pending'
            """,
            (execution_id,),
        ).fetchone()[0]
        if remaining == 0:
            cur.execute(
                """
                UPDATE catalogue_query_executions
                SET processing_finished_at=COALESCE(processing_finished_at, ?)
                WHERE id=?
                """,
                (current, execution_id),
            )
        return True


classify_pending_item = classify_pending


def reset_query_state_with_cursor(cursor, query_id, new_query):
    """Reset discovery state atomically when a query URL is replaced.

    Historical execution metrics remain available, while unprocessed work for
    the old URL and its anchor/observations are removed.  The next success is a
    flood-safe bootstrap for the new URL.
    """

    now = time.time()
    pending_executions = [
        row[0]
        for row in cursor.execute(
            """
            SELECT DISTINCT execution_id
            FROM pending_query_items
            WHERE query_id=? AND status='pending'
            """,
            (query_id,),
        ).fetchall()
    ]
    if pending_executions:
        placeholders = ",".join("?" for _ in pending_executions)
        cursor.execute(
            f"""
            UPDATE catalogue_query_executions
            SET outcome=CASE
                    WHEN outcome='started' THEN 'discarded_query_changed'
                    ELSE outcome
                END,
                finished_at=COALESCE(finished_at, ?),
                processing_finished_at=COALESCE(processing_finished_at, ?)
            WHERE id IN ({placeholders})
            """,
            [now, now, *pending_executions],
        )
    cursor.execute(
        "DELETE FROM pending_query_items WHERE query_id=?",
        (query_id,),
    )
    cursor.execute(
        "DELETE FROM query_item_observations WHERE query_id=?",
        (query_id,),
    )
    cursor.execute("DELETE FROM query_progress WHERE query_id=?", (query_id,))
    cursor.execute(
        """
        INSERT INTO query_progress
            (query_id, query_fingerprint, successful_observations, updated_at)
        VALUES (?, ?, 0, ?)
        """,
        (query_id, query_fingerprint(new_query), now),
    )
    return True


def per_query_report(days=30, *, now=None, cursor=None):
    """Return aggregate execution metrics without exposing query contents."""

    current = time.time() if now is None else float(now)
    cutoff = current - max(0, float(days)) * 86400
    with _cursor_scope(cursor) as cur:
        rows = cur.execute(
            """
            SELECT query_id,
                   COUNT(*) AS executions,
                   SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END),
                   SUM(CASE
                         WHEN outcome NOT IN ('started','success','abandoned_restart')
                          AND outcome NOT LIKE 'discarded_%' THEN 1 ELSE 0
                       END),
                   SUM(CASE WHEN outcome LIKE 'discarded_%' THEN 1 ELSE 0 END),
                   SUM(CASE
                         WHEN outcome IN ('started','abandoned_restart') THEN 1
                         ELSE 0
                       END),
                   COALESCE(SUM(returned_count), 0),
                   COALESCE(SUM(unique_returned_count), 0),
                   COALESCE(SUM(fresh_count), 0),
                   COALESCE(SUM(already_known_count), 0),
                   COALESCE(SUM(accepted_count), 0),
                   COALESCE(SUM(locally_rejected_count), 0),
                   COALESCE(SUM(notifications_generated), 0),
                   COALESCE(SUM(cross_query_overlap_count), 0),
                   AVG(CASE WHEN duration_ms IS NOT NULL THEN duration_ms END),
                   MAX(started_at)
            FROM catalogue_query_executions
            WHERE started_at>=?
            GROUP BY query_id
            ORDER BY query_id
            """,
            (cutoff,),
        ).fetchall()
    fields = (
        "query_id",
        "executions",
        "successful_executions",
        "failed_executions",
        "discarded_executions",
        "incomplete_executions",
        "returned_items",
        "unique_returned_items",
        "fresh_items",
        "already_known_items",
        "accepted_items",
        "locally_rejected_items",
        "notifications_generated",
        "cross_query_overlaps",
        "average_duration_ms",
        "latest_started_at",
    )
    return [dict(zip(fields, row)) for row in rows]


def overlap_report(days=30, *, now=None, minimum_shared=1, cursor=None):
    """Return pairwise returned-id overlap and Jaccard similarity."""

    current = time.time() if now is None else float(now)
    cutoff = current - max(0, float(days)) * 86400
    with _cursor_scope(cursor) as cur:
        rows = cur.execute(
            """
            SELECT DISTINCT e.query_id, i.item_key
            FROM catalogue_query_executions e
            JOIN catalogue_query_execution_items i ON i.execution_id=e.id
            WHERE e.outcome='success' AND e.started_at>=?
            """,
            (cutoff,),
        ).fetchall()
    keys_by_query = {}
    for query_id, key in rows:
        keys_by_query.setdefault(query_id, set()).add(key)
    result = []
    query_ids = sorted(keys_by_query)
    for index, left_id in enumerate(query_ids):
        left = keys_by_query[left_id]
        for right_id in query_ids[index + 1 :]:
            right = keys_by_query[right_id]
            shared = len(left & right)
            if shared < max(0, int(minimum_shared)):
                continue
            union = len(left | right)
            result.append(
                {
                    "query_id_a": left_id,
                    "query_id_b": right_id,
                    "shared_item_ids": shared,
                    "query_a_item_ids": len(left),
                    "query_b_item_ids": len(right),
                    "jaccard_similarity": shared / union if union else 0.0,
                }
            )
    result.sort(
        key=lambda row: (-row["shared_item_ids"], row["query_id_a"], row["query_id_b"])
    )
    return result


def get_efficiency_report(days=30, *, now=None, cursor=None):
    """Return privacy-safe execution, yield, and overlap aggregates.

    The report intentionally contains query ids/names and aggregate counts
    only.  Raw item digests, listing snapshots, URLs, sellers, subscribers,
    and notification recipients never leave the persistence layer.
    """

    try:
        days = int(days)
    except (TypeError, ValueError) as exc:
        raise ValueError("days must be between 1 and 365") from exc
    if not 1 <= days <= 365:
        raise ValueError("days must be between 1 and 365")
    current = time.time() if now is None else float(now)
    cutoff = current - days * 86400

    with _cursor_scope(cursor) as cur:
        names = {
            int(row[0]): (row[1] or f"Query {row[0]}")
            for row in cur.execute("SELECT id, query_name FROM queries").fetchall()
        }
        query_rows = per_query_report(days=days, now=current, cursor=cur)
        overlap_rows = overlap_report(days=days, now=current, cursor=cur)
        bounds = cur.execute(
            """
            SELECT MIN(started_at), MAX(started_at)
            FROM catalogue_query_executions
            WHERE started_at>=?
            """,
            (cutoff,),
        ).fetchone()

    queries = []
    for row in query_rows:
        executions = int(row["executions"] or 0)
        successes = int(row["successful_executions"] or 0)
        returned = int(row["returned_items"] or 0)
        unique_returned = int(row["unique_returned_items"] or 0)
        overlaps = int(row["cross_query_overlaps"] or 0)
        queries.append(
            {
                "query_id": row["query_id"],
                "query_name": names.get(
                    int(row["query_id"]), f"Query {row['query_id']}"
                ),
                "execution_count": executions,
                "success_count": successes,
                "failed_count": int(row["failed_executions"] or 0),
                "discarded_count": int(row["discarded_executions"] or 0),
                "incomplete_count": int(row["incomplete_executions"] or 0),
                "success_rate": successes / executions if executions else 0.0,
                "avg_duration_ms": row["average_duration_ms"],
                "returned_count": returned,
                "unique_returned_count": unique_returned,
                "fresh_count": int(row["fresh_items"] or 0),
                "already_known_count": int(row["already_known_items"] or 0),
                "accepted_count": int(row["accepted_items"] or 0),
                "locally_rejected_count": int(row["locally_rejected_items"] or 0),
                "notifications_generated": int(row["notifications_generated"] or 0),
                "overlap_return_count": overlaps,
                "overlap_rate": overlaps / returned if returned else 0.0,
                "evidence_status": ("useful" if successes >= 50 else "limited"),
                "recommendation": "Collect evidence; no automatic change.",
            }
        )

    overlaps = []
    for row in overlap_rows:
        left_id = int(row["query_id_a"])
        right_id = int(row["query_id_b"])
        overlaps.append(
            {
                "query_a_id": left_id,
                "query_b_id": right_id,
                "query_a_name": names.get(left_id, f"Query {left_id}"),
                "query_b_name": names.get(right_id, f"Query {right_id}"),
                "shared_item_count": int(row["shared_item_ids"]),
                "query_a_item_count": int(row["query_a_item_ids"]),
                "query_b_item_count": int(row["query_b_item_ids"]),
                "overlap_rate": float(row["jaccard_similarity"]),
                "recommendation": "Manual exactness review only.",
            }
        )

    count_fields = (
        "execution_count",
        "success_count",
        "failed_count",
        "discarded_count",
        "incomplete_count",
        "returned_count",
        "unique_returned_count",
        "fresh_count",
        "already_known_count",
        "accepted_count",
        "locally_rejected_count",
        "notifications_generated",
        "overlap_return_count",
    )
    summary = {
        field: sum(int(row.get(field) or 0) for row in queries)
        for field in count_fields
    }
    summary.update(
        {
            "data_start": bounds[0] if bounds else None,
            "data_end": bounds[1] if bounds else None,
            "evidence_note": (
                "Read-only evidence. Full result windows are right-censored; "
                "overlap does not prove that searches can be merged."
            ),
        }
    )
    return {
        "days": days,
        "summary": summary,
        "queries": queries,
        "overlaps": overlaps,
    }


def prune_retention(days=DEFAULT_RETENTION_DAYS, *, now=None, cursor=None):
    """Prune old execution detail while preserving durable query progress."""

    current = time.time() if now is None else float(now)
    cutoff = current - max(1, float(days)) * 86400
    with _cursor_scope(cursor, immediate=cursor is None) as cur:
        # Terminal claims are no longer needed once accepted ids exist in the
        # global items table and the analysis retention window has elapsed.
        pending_deleted = cur.execute(
            """
            DELETE FROM pending_query_items
            WHERE status<>'pending' AND updated_at<?
            """,
            (cutoff,),
        ).rowcount
        execution_deleted = cur.execute(
            """
            DELETE FROM catalogue_query_executions
            WHERE started_at<? AND outcome<>'started'
              AND NOT EXISTS (
                  SELECT 1 FROM pending_query_items p
                  WHERE p.execution_id=catalogue_query_executions.id
                    AND p.status='pending'
              )
            """,
            (cutoff,),
        ).rowcount
        return {
            "terminal_pending_deleted": pending_deleted,
            "executions_deleted": execution_deleted,
        }


retention = prune_retention
get_per_query_report = per_query_report
get_overlap_report = overlap_report
